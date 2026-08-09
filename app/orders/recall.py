"""회수(반품·A/S 수거) 예약 + 배송 추적.

CJ 규칙(rental-system 실운영에서 확인된 것):
- 회수는 보내는분=고객, 받는분=우리 회수지
- 회수 송장번호는 접수 시 채번하지 않는다. 기사가 집화할 때 CJ가 부여하므로 공란이 정상
- cust_use_no는 같은 날 재접수 충돌(ORA-00001)을 피하려고 시각을 붙여 유일화
- 취소 PK는 (접수일자, cust_use_no, 접수구분)이라 실접수일과 접수구분(회수=02)을 반드시 맞춰야 한다

회수 대상 자산은 waybills.asset_ids/asset_prev에 기록한다 — 주문 없이 접수되는 A/S 회수도
취소·입고 시 정확히 되돌리기 위해서다.
"""
import json
import threading
import time
from datetime import datetime

from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require, require_any
from ..cj import cj2_reg_book, cj2_track
from ..db import get_db, tx
from ..purchase import ASSET_STATUSES, asset_event
from . import bp, _get_order_or_404
from .waybill import _cj_settings, _is_real, _next_wid, _pickup, _safe_label_text

CJ_STAGE = {
    "01": "집화지시", "02": "집화완료", "03": "집화실패", "11": "간선상차", "12": "간선하차",
    "21": "배송지시", "41": "배송출발", "42": "배송중", "82": "미배송", "91": "배송완료",
}
TERMINAL_STAGES = ("91",)
RETURNABLE = ("in_stock", "refurbishing", "repair", "as", "painting", "defective", "ready", "scrapped")


def _mark_returning(conn, asset_ids, wid, note):
    """회수 대상 자산을 '회수중'으로 바꾸고 직전 상태를 기록한다."""
    prev = {}
    for aid in asset_ids:
        a = conn.execute("SELECT id, status FROM assets WHERE id=?", (aid,)).fetchone()
        if a is None:
            continue
        prev[str(a["id"])] = a["status"]
        conn.execute("UPDATE assets SET status='returning', updated_at=? WHERE id=?",
                     (config.now_iso(), a["id"]))
        asset_event(conn, a["id"], "회수예약", {"wid": wid, **note})
    return prev


def _restore_from_recall(conn, w, to_status=None):
    """회수 취소/입고 시 자산 상태를 되돌린다.

    to_status 지정(=입고 처리)이면 그 상태로, 없으면(=예약 취소) 회수 직전 상태로 되돌린다.
    예약을 취소하면 물건은 아직 고객에게 있으므로 'shipped'로 돌아가는 것이 정상이다 —
    입고 시 고를 수 있는 상태(RETURNABLE)로 검사하면 안 된다.
    """
    try:
        ids = json.loads(w["asset_ids"] or "[]")
        prev = json.loads(w["asset_prev"] or "{}")
    except ValueError:
        ids, prev = [], {}
    moved = []
    ts = config.now_iso()
    for aid in ids:
        a = conn.execute("SELECT id, asset_no, status FROM assets WHERE id=?", (aid,)).fetchone()
        if a is None or a["status"] != "returning":
            continue
        back = to_status or prev.get(str(aid)) or "refurbishing"
        if back not in ASSET_STATUSES:
            back = "refurbishing"
        conn.execute("UPDATE assets SET status=?, updated_at=? WHERE id=?", (back, ts, aid))
        # ★물건이 돌아왔으면 그 주문에서 '나간' 것이 아니다 — 연결을 끊는다.
        #   안 끊으면 예전 주문에 걸린 채 재고로 풀려, 새 주문에도 매칭돼
        #   같은 한 대가 두 고객에게 나갈 수 있다(2026-07-29 전수조사 확인).
        #   회수 '취소'(to_status 없음)는 물건이 아직 고객에게 있으므로 연결을 유지한다.
        if to_status and w["order_id"]:
            conn.execute("DELETE FROM order_assets WHERE order_id=? AND asset_id=?",
                         (w["order_id"], aid))
            asset_event(conn, aid, "매칭해제", {"orderId": w["order_id"], "사유": "회수입고"})
        asset_event(conn, aid, "회수입고" if to_status else "회수취소",
                    {"wid": w["wid"], "상태": back})
        moved.append(a["asset_no"])
    return moved


def _insert_recall(conn, *, wid, order_id, as_ticket_id, party, items_text, cust_use_no,
                   rcpt_ymd, pickup_date, asset_ids, asset_prev):
    ts = config.now_iso()
    conn.execute(
        "INSERT INTO waybills(wid, order_id, as_ticket_id, type, cj_kind, invoice_no, status, "
        "recipient, phone, postal_code, address, items, cust_use_no, cj_rcpt_ymd, scheduled_date, "
        "box_qty, asset_ids, asset_prev, created_by, created_at, updated_at) "
        "VALUES(?,?,?,'recall','return','','pending',?,?,?,?,?,?,?,?,1,?,?,?,?,?)",
        (wid, order_id, as_ticket_id, party.get("name", ""), party.get("tel", ""),
         party.get("zip", ""), party.get("addr", ""), items_text, cust_use_no, rcpt_ymd,
         pickup_date, json.dumps(asset_ids), json.dumps(asset_prev),
         g.user["display_name"], ts, ts))


def _call_cj_recall(cfg, *, party, items_text, cust_use_no, rcpt_ymd, pickup_date, remark):
    res = cj2_reg_book(
        cfg, kind="return", sender=party, receiver=_pickup(cfg),
        items=[{"name": items_text[:60], "qty": 1}], cust_use_no=cust_use_no,
        rcpt_ymd=rcpt_ymd, colct_ymd=(pickup_date or "").replace("-", ""), remark=remark[:60])
    if not res.get("ok"):
        raise RuntimeError(f"CJ 회수 접수 실패: {res.get('detail') or res.get('result_cd')}")
    return res


@bp.post("/orders/<int:oid>/recall")
def recall_order(oid):
    """주문 반품 회수 예약."""
    require("orders.ship")
    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "").strip() or "반품 회수"
    pickup_date = (body.get("pickupDate") or "").strip()

    with tx(write=True) as conn:
        row = _get_order_or_404(conn, oid)
        dup = conn.execute(
            "SELECT wid FROM waybills WHERE order_id=? AND type='recall' "
            "AND status IN ('issued','test','pending')", (oid,)).fetchone()
        if dup:
            abort(409, description=f"이미 회수 예약이 있습니다({dup['wid']}). 취소 후 다시 예약하세요.")
        if not (row["recipient"] or "").strip() or not (row["address"] or "").strip():
            abort(400, description="수취인과 주소가 있어야 회수를 예약할 수 있습니다.")
        cfg = _cj_settings(conn)
        real = _is_real(cfg)
        if real and not (row["phone"] or "").strip():
            abort(400, description="실접수에는 고객 전화번호가 필요합니다.")

        assets = conn.execute(
            "SELECT a.id, a.asset_no FROM order_assets oa JOIN assets a ON a.id=oa.asset_id "
            "WHERE oa.order_id=? ORDER BY a.asset_no", (oid,)).fetchall()
        asset_ids = [a["id"] for a in assets]
        asset_nos = [a["asset_no"] for a in assets]
        party = {"name": row["recipient"], "tel": row["phone"], "zip": row["postal_code"],
                 "addr": row["address"], "addr_detail": ""}
        items_text = _safe_label_text(
            f"[회수] {(row['product_code'] or row['product_name'] or '상품').strip()}"
            + (f" / 자산 {','.join(asset_nos)}" if asset_nos else ""))
        wid = _next_wid(conn)
        today = config.today_str()
        cust_use_no = f"HBR{oid}-{config.now().strftime('%H%M%S')}"
        prev = _mark_returning(conn, asset_ids, wid, {"사유": reason})
        _insert_recall(conn, wid=wid, order_id=oid, as_ticket_id=None, party=party,
                       items_text=items_text, cust_use_no=cust_use_no,
                       rcpt_ymd=today if real else "", pickup_date=pickup_date,
                       asset_ids=asset_ids, asset_prev=prev)

    cj_response = None
    try:
        if real:
            cj_response = _call_cj_recall(cfg, party=party, items_text=items_text,
                                          cust_use_no=cust_use_no, rcpt_ymd=today,
                                          pickup_date=pickup_date, remark=reason)
    except Exception as e:
        with tx(write=True) as conn:   # 실패 시 자산 상태까지 원복
            w = conn.execute("SELECT * FROM waybills WHERE wid=?", (wid,)).fetchone()
            if w:
                _restore_from_recall(conn, w)
                conn.execute("DELETE FROM waybills WHERE wid=? AND status='pending'", (wid,))
            audit.log("recall_failed", target=f"{wid} #{oid}", detail={"error": str(e)})
        abort(502, description=str(e))

    with tx(write=True) as conn:
        ts = config.now_iso()
        conn.execute("UPDATE waybills SET status=?, cj_response=?, updated_at=? WHERE wid=?",
                     ("issued" if real else "test",
                      json.dumps(cj_response, ensure_ascii=False) if cj_response else None, ts, wid))
        audit.log("recall_created", target=f"{wid} #{oid} {row['recipient']}",
                  detail={"reason": reason, "simulated": not real, "assets": asset_nos})
    return jsonify({"wid": wid, "simulated": not real,
                    "note": "회수 송장번호는 기사가 집화할 때 CJ가 부여합니다(지금은 공란이 정상)."}), 201


@bp.post("/as-tickets/<int:tid>/recall")
def recall_as(tid):
    """A/S 수거 예약 — 접수된 A/S 건의 제품을 회수한다."""
    require("as.manage")
    body = request.get_json(silent=True) or {}
    pickup_date = (body.get("pickupDate") or "").strip()
    with tx(write=True) as conn:
        t = conn.execute("SELECT * FROM as_tickets WHERE id=?", (tid,)).fetchone()
        if t is None:
            abort(404, description="A/S 건을 찾을 수 없습니다.")
        if not (t["customer"] or "").strip() or not (t["address"] or "").strip():
            abort(400, description="고객명과 주소를 먼저 입력하세요.")
        # ★회수(recall)만 본다. 예전엔 종류를 안 가려서, 반송 송장을 먼저 발급한 A/S 건은
        #   그 반송 송장 번호를 들이대며 "이미 회수 예약이 있습니다"라고 거부했다.
        #   그 건은 이 화면에서 영영 회수를 걸 수 없었다(2026-07-29 전수조사).
        dup = conn.execute(
            "SELECT wid FROM waybills WHERE as_ticket_id=? AND type='recall' "
            "AND status IN ('issued','test','pending')", (tid,)).fetchone()
        if dup:
            abort(409, description=f"이미 회수 예약이 있습니다({dup['wid']}).")
        cfg = _cj_settings(conn)
        real = _is_real(cfg)
        if real and not (t["phone"] or "").strip():
            abort(400, description="실접수에는 고객 전화번호가 필요합니다.")

        party = {"name": t["customer"], "tel": t["phone"], "zip": "",
                 "addr": t["address"], "addr_detail": ""}
        items_text = _safe_label_text(f"[A/S회수] {t['ticket_no']} {t['symptom']}")[:60]
        wid = _next_wid(conn)
        today = config.today_str()
        cust_use_no = f"HBA{tid}-{config.now().strftime('%H%M%S')}"
        asset_ids = [t["asset_id"]] if t["asset_id"] else []
        prev = _mark_returning(conn, asset_ids, wid, {"ticketNo": t["ticket_no"]})
        # ★order_id는 넣지 않는다 — 넣으면 그 주문의 반품 회수와 뒤섞인다
        _insert_recall(conn, wid=wid, order_id=None, as_ticket_id=tid, party=party,
                       items_text=items_text, cust_use_no=cust_use_no,
                       rcpt_ymd=today if real else "", pickup_date=pickup_date,
                       asset_ids=asset_ids, asset_prev=prev)
        ticket = dict(t)

    cj_response = None
    try:
        if real:
            cj_response = _call_cj_recall(cfg, party=party, items_text=items_text,
                                          cust_use_no=cust_use_no, rcpt_ymd=today,
                                          pickup_date=pickup_date, remark=ticket["symptom"] or "A/S 회수")
    except Exception as e:
        with tx(write=True) as conn:
            w = conn.execute("SELECT * FROM waybills WHERE wid=?", (wid,)).fetchone()
            if w:
                _restore_from_recall(conn, w)
                conn.execute("DELETE FROM waybills WHERE wid=? AND status='pending'", (wid,))
        abort(502, description=str(e))

    with tx(write=True) as conn:
        ts = config.now_iso()
        conn.execute("UPDATE waybills SET status=?, cj_response=?, updated_at=? WHERE wid=?",
                     ("issued" if real else "test",
                      json.dumps(cj_response, ensure_ascii=False) if cj_response else None, ts, wid))
        conn.execute("UPDATE as_tickets SET status='collecting', updated_at=? WHERE id=?", (ts, tid))
        conn.execute(
            "INSERT INTO as_events(ticket_id, ts, action, actor, detail) VALUES(?,?,?,?,?)",
            (tid, ts, "회수예약", g.user["display_name"], json.dumps({"wid": wid}, ensure_ascii=False)))
        audit.log("as_recall_created", target=f"{wid} {ticket['ticket_no']}")
    # 회수 예약 안내 문자(트랜잭션 밖)
    from flask import current_app

    from ..notify import notify_async
    ok, msg = notify_async(current_app, tid, "collecting")
    return jsonify({"wid": wid, "simulated": not real, "sms": {"ok": ok, "message": msg}}), 201


@bp.post("/waybills/<wid>/received")
def mark_recall_received(wid):
    """회수 입고 처리 — 물건이 돌아왔을 때 자산을 재고로 되돌린다."""
    require_any("orders.ship", "waybills.manage", "as.manage")
    body = request.get_json(silent=True) or {}
    back = (body.get("status") or "refurbishing").strip()
    if back not in RETURNABLE:
        abort(400, description="회수 후 상태가 올바르지 않습니다.")
    with tx(write=True) as conn:
        w = conn.execute("SELECT * FROM waybills WHERE wid=?", (wid,)).fetchone()
        if w is None:
            abort(404, description="회수 건을 찾을 수 없습니다.")
        if w["type"] != "recall":
            abort(400, description="회수 송장이 아닙니다.")
        if w["status"] in ("delivered", "canceled"):
            abort(400, description="이미 처리된 회수 건입니다.")
        # ★A/S로 받은 물건은 '고객 소유'다. 판매 가능 상태로 두면 다른 주문에 매칭돼
        #   남의 수리품이 출고되고, 정작 그 고객에게 보낼 물건이 사라진다.
        if w["as_ticket_id"]:
            back = "as"
        ts = config.now_iso()
        conn.execute("UPDATE waybills SET status='delivered', cj_stage_cd='91', cj_stage_nm='회수완료', "
                     "cj_stage_at=?, updated_at=? WHERE wid=?", (ts, ts, wid))
        moved = _restore_from_recall(conn, w, to_status=back)
        if w["as_ticket_id"]:
            conn.execute("UPDATE as_tickets SET status='repairing', updated_at=? WHERE id=? AND status='collecting'",
                         (ts, w["as_ticket_id"]))
            conn.execute(
                "INSERT INTO as_events(ticket_id, ts, action, actor, detail) VALUES(?,?,?,?,?)",
                (w["as_ticket_id"], ts, "회수입고", g.user["display_name"],
                 json.dumps({"wid": wid}, ensure_ascii=False)))
        audit.log("recall_received", target=wid, detail={"assets": moved, "status": back})
    # 실제로 반영한 상태를 돌려준다 — A/S 회수는 고른 값과 다를 수 있어 화면이 알려야 한다
    return jsonify({"ok": True, "assets": moved, "status": back})


# ---------------------------------------------------------------- 추적

def _sync_one(conn, w, res):
    """추적 응답을 반영. 변경되면 True. (CJ 호출은 호출자가 트랜잭션 밖에서 한다)"""
    if not res or not res.get("ok"):
        return False
    data = res.get("data") or {}
    rows = data.get("PROC_LIST") or data.get("procList") or []
    if not rows:
        return False
    last = rows[-1]
    code = str(last.get("CRG_ST_CD") or last.get("crgStCd") or "").strip()
    if not code or code == w["cj_stage_cd"]:
        return False
    name = CJ_STAGE.get(code, last.get("CRG_ST_NM") or code)
    ts = config.now_iso()
    status = "delivered" if code in TERMINAL_STAGES else w["status"]
    conn.execute(
        "UPDATE waybills SET cj_stage_cd=?, cj_stage_nm=?, cj_stage_at=?, status=?, updated_at=? WHERE wid=?",
        (code, name, ts, status, ts, w["wid"]))
    return True


def _trackable(conn):
    """추적 대상 — 실발행이고 아직 종결되지 않은 건(21일 이내)."""
    cutoff = config.now().timestamp() - 21 * 86400
    rows = conn.execute(
        "SELECT * FROM waybills WHERE status='issued' AND invoice_no != '' "
        "AND cj_stage_cd NOT IN ('91') ORDER BY created_at DESC LIMIT 200").fetchall()
    out = []
    for r in rows:
        try:
            if datetime.fromisoformat(r["created_at"]).timestamp() < cutoff:
                continue
        except (TypeError, ValueError):
            pass
        out.append(r)
    return out


def _run_tracking(conn_factory, cfg, targets):
    """★CJ 호출은 트랜잭션 밖에서 하고, 응답만 짧은 쓰기 트랜잭션으로 반영한다.
    (DB 락을 쥔 채 외부 API를 기다리면 그 동안 전 시스템의 쓰기가 멈춘다)"""
    changed = 0
    for w in targets:
        try:
            res = cj2_track(cfg, w["invoice_no"])       # ← 트랜잭션 밖
        except Exception:
            res = None
        if res:
            try:
                with tx(write=True) as c2:              # ← 짧은 쓰기만
                    if _sync_one(c2, w, res):
                        changed += 1
            except Exception:
                pass
        time.sleep(0.3)   # CJ 과호출 방지
    return changed


@bp.post("/waybills/track-sync")
def track_sync():
    """수동 추적 새로고침."""
    require_any("orders.ship", "waybills.manage")
    conn = get_db()
    cfg = _cj_settings(conn)
    if not _is_real(cfg):
        abort(400, description="CJ 운영 설정(운영+실발행 무장)이 있어야 추적할 수 있습니다.")
    targets = _trackable(conn)
    changed = _run_tracking(None, cfg, targets)
    return jsonify({"checked": len(targets), "changed": changed})


def start_tracker(app):
    """3시간 주기 배송추적 데몬. 운영 설정이 없으면 아무것도 하지 않는다."""
    def loop():
        while True:
            time.sleep(3 * 3600)
            try:
                with app.app_context():
                    conn = get_db()
                    cfg = _cj_settings(conn)
                    if not _is_real(cfg):
                        continue
                    targets = _trackable(conn)
                    _run_tracking(None, cfg, targets)
            except Exception:
                app.logger.exception("배송추적 갱신 실패")

    t = threading.Thread(target=loop, name="hms-cj-tracker", daemon=True)
    t.start()
    return t
