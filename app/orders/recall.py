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
from datetime import datetime, timedelta

from flask import abort, current_app, g, jsonify, request

from .. import audit, config
from ..auth.perms import require, require_any
from ..cj import cj2_mss_track, cj2_reg_book, cj2_track
from ..db import get_db, tx
from ..purchase import ASSET_STATUSES, _unlist, asset_event
from . import bp, _get_order_or_404
from .waybill import (_cj_settings, _is_real, _next_wid, _pickup, _safe_label_text,
                      check_pickup_date)

CJ_STAGE = {
    "01": "집화지시", "02": "집화완료", "03": "집화실패", "11": "간선상차", "12": "간선하차",
    "21": "배송지시", "41": "배송출발", "42": "배송중", "82": "미배송", "91": "배송완료",
}
TERMINAL_STAGES = ("91",)
# ★자동 '출고 확인'을 켜는 단계 — **간선상차(11)부터**(대표 2026-09-04:
#   "송장이 고객에게 자동으로 들어갔어도 간선상차가 되기 전까지는 출고확인으로 넘기면 안 된다.
#    간선상차 기준으로 우리 제품이 정상적으로 출고되었는지가 중요하다").
#   02(집화완료)는 기사가 물건을 받아 스캔만 한 상태라 아직 터미널에 실리지 않았다 —
#   여기서 마감하면 실제로 안 나간 물건이 '출고'로 잡힌다. 그래서 02를 뺐다.
#   01(집화지시)은 기사 배정만, 03(집화실패)은 안 나간 것. 91(배송완료)은 당연히 포함
#   (추적을 3시간마다 보므로 11을 못 보고 뒤 단계부터 볼 수 있다 — 뒤 단계도 전부 넣는다).
SHIP_CONFIRM_STAGES = ("11", "12", "21", "41", "42", "82", "91")
# 예전 이름 — 뜻이 '움직이기 시작함'이라 그대로 두면 02가 섞여 오해를 부른다.
MOVING_STAGES = SHIP_CONFIRM_STAGES
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


def _actor_name():
    """지금 요청의 사용자 이름 — 데몬 스레드처럼 사용자가 없으면 'CJ배송추적'."""
    try:
        u = g.get("user")
    except RuntimeError:                                 # 앱 컨텍스트 밖
        return "CJ배송추적"
    if u is None:
        return "CJ배송추적"
    try:
        return u["display_name"] or "CJ배송추적"         # sqlite Row 도 dict 도 같은 문법
    except (KeyError, IndexError, TypeError):
        return "CJ배송추적"


def recall_arrived(conn, w, *, back=None, via="수동"):
    """회수 물건이 우리 손에 들어왔다 — 송장·자산·A/S 접수 건을 한 번에 '입고'로 옮긴다.

    세 경로가 전부 여기로 온다(2026-09-07 대표 "회수중에서 우리 쪽으로 배송완료되면
    자동으로 입고완료로 넘어가야 함"):
      ① 사람이 [📥 입고 처리]를 누른 것            — mark_recall_received
      ② 3시간 추적 데몬이 CJ 91(배달완료)을 본 것  — _sync_one
      ③ 회수 송장번호 수집이 91 을 받아 온 것       — sweep_recall_invoices
    예전에는 ①만 접수 건을 움직였고 ②③은 송장만 delivered 로 바꿔서, 물건이 이미
    들어와 있어도 접수 건은 영원히 '회수 중'이었다(라이브 WB-20260901-01 이 6일째 그 상태).

    ★A/S 건의 자산은 무조건 'as' — 판매 가능 상태로 두면 남의 수리품이 다른 주문에 매칭돼
      팔려 나간다. 주문 반품 회수는 호출자가 고른 상태(back)로 돌아간다.
    ★접수 건은 '입고 완료'(arrived)에 세운다 — 수리 시작은 사람이 누른다(대표 흐름).
    돌려주는 값: (되돌린 자산번호 목록, 문자를 보낼 A/S id 또는 None).
    문자는 트랜잭션 밖에서 호출자가 보낸다(원칙 #1 — 외부 호출이 DB 잠금을 쥐면 안 된다).
    """
    ts = config.now_iso()
    if w["as_ticket_id"]:
        back = "as"
    if back not in RETURNABLE:
        back = "refurbishing"
    if w["status"] != "delivered":
        # 추적이 아니라 사람이 눌렀으면 CJ 단계도 '회수완료'로 맞춘다(이미 91 이면 그대로)
        conn.execute(
            "UPDATE waybills SET status='delivered', "
            "  cj_stage_nm=CASE WHEN cj_stage_cd='91' THEN cj_stage_nm ELSE '회수완료' END, "
            "  cj_stage_at=CASE WHEN cj_stage_cd='91' THEN cj_stage_at ELSE ? END, "
            "  cj_stage_cd='91', updated_at=? WHERE wid=?", (ts, ts, w["wid"]))
    moved = _restore_from_recall(conn, w, to_status=back)
    notify_tid = None
    tid = w["as_ticket_id"]
    if tid:
        cur = conn.execute("SELECT status, asset_id, ticket_no FROM as_tickets WHERE id=?",
                           (tid,)).fetchone()
        # 이미 수리 중/완료면 되돌리지 않는다(사람이 앞서 나간 것을 데몬이 뒤집으면 안 된다)
        if cur and cur["status"] in ("received", "collecting"):
            conn.execute("UPDATE as_tickets SET status='arrived', updated_at=? WHERE id=?",
                         (ts, tid))
            conn.execute(
                "INSERT INTO as_events(ticket_id, ts, action, actor, detail) VALUES(?,?,?,?,?)",
                (tid, ts, "회수입고", _actor_name(),
                 json.dumps({"wid": w["wid"], "경로": via,
                             **({"송장번호": w["invoice_no"]} if w["invoice_no"] else {})},
                            ensure_ascii=False)))
            if cur["asset_id"]:
                asset_event(conn, cur["asset_id"], "A/S입고 완료",
                            {"ticketNo": cur["ticket_no"], "경로": via})
            notify_tid = tid
    return moved, notify_tid


def _auto_as_return_delivered(conn, w, code):
    """A/S 반송(택배 출고) 송장이 배달완료(91)되면 접수 건을 자동으로 종료한다
    (2026-09-07 대표 "종료의 경우 그냥 쭉 쌓이는 구조" — 사람이 [종료]를 안 눌러도 쌓인다).

    상태 바꾸기(asvc update_ticket 의 'returned')와 같은 기록을 남긴다 — 자산은 고객에게
    돌아갔으니 우리 재고가 아니다(shipped). 이미 끝났거나 취소된 건은 건드리지 않는다.
    """
    if w["type"] != "forward" or not w["as_ticket_id"] or code not in TERMINAL_STAGES:
        return False
    t = conn.execute("SELECT * FROM as_tickets WHERE id=?", (w["as_ticket_id"],)).fetchone()
    if t is None or t["status"] in ("returned", "cancelled"):
        return False
    ts = config.now_iso()
    conn.execute("UPDATE as_tickets SET status='returned', closed_at=?, updated_at=? WHERE id=?",
                 (ts, ts, t["id"]))
    conn.execute(
        "INSERT INTO as_events(ticket_id, ts, action, actor, detail) VALUES(?,?,?,?,?)",
        (t["id"], ts, "상태변경", _actor_name(),
         json.dumps({"from": "택배 출고", "to": "반송 완료", "wid": w["wid"],
                     "송장번호": w["invoice_no"] or "", "사유": "CJ 배달완료 자동 종료"},
                    ensure_ascii=False)))
    if t["asset_id"]:
        asset_event(conn, t["asset_id"], "A/S반송 완료",
                    {"ticketNo": t["ticket_no"], "사유": "CJ 배달완료 자동 종료"})
        conn.execute(
            "UPDATE assets SET status='shipped', updated_at=? WHERE id=? "
            "AND status NOT IN ('shipped','scrapped')", (ts, t["asset_id"]))
    audit.log("as_closed_auto_cj", target=f"{t['ticket_no']} {t['customer']}",
              detail={"wid": w["wid"], "invoiceNo": w["invoice_no"]})
    return True


def _notify_after(after):
    """트랜잭션이 끝난 뒤 바깥일을 한다 — [(kind, id), ...]. 실패해도 조용히.

    kind 가 "mall_push" 면 주문의 송장번호를 쇼핑몰에 보낸다(2026-09-08 대표 "고도몰 가서 내가
    직접 입력해야 하니까"). 그 밖은 A/S 안내 문자 trigger 다. 둘 다 외부 호출이라 DB 잠금 밖에서.
    """
    if not after:
        return
    for code, ident in after:
        try:
            if code == "mall_push":
                from ..malls.invoice_push import push_for_order
                push_for_order(current_app, ident)
            else:
                from ..notify import notify_trigger
                notify_trigger(current_app, ident, code)
        except Exception:                                # noqa: BLE001
            pass


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
        rcpt_ymd=rcpt_ymd, colct_ymd=(pickup_date or "").replace("-", ""),
        box_type=cfg.get("box_type") or "01", frt_dv=cfg.get("frt_dv") or "03",
        remark=remark[:60])
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
        check_pickup_date(_cj_settings(conn), pickup_date)   # CJ 쉬는 날은 예약 못 받는다
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
        check_pickup_date(_cj_settings(conn), pickup_date)   # CJ 쉬는 날은 예약 못 받는다
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

        # ★우편번호를 함께 보낸다(2026-08-31) — 주문 회수와 같은 CJ 규격.
        party = {"name": t["customer"], "tel": t["phone"], "zip": t["postal_code"] or "",
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

    from ..notify import notify_trigger
    ok, msg = notify_trigger(current_app, tid, "collecting")
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
        #   (recall_arrived 가 A/S 건이면 'as'로 강제한다 — 데몬·수집 경로와 같은 관문)
        if w["as_ticket_id"]:
            back = "as"
        moved, as_tid = recall_arrived(conn, w, back=back, via="입고 처리 버튼")
        audit.log("recall_received", target=wid, detail={"assets": moved, "status": back})
    # 회수 입고 시점 문자(2026-08-31 대표 — 직접 만든 양식이 이 시점을 고를 수 있다).
    # 트랜잭션 밖 + 실패해도 입고 처리는 그대로.
    sms = None
    if as_tid:
        from ..notify import notify_trigger
        ok_sms, msg = notify_trigger(current_app, as_tid, "collected")
        if msg:
            sms = {"ok": ok_sms, "message": msg}
    # 실제로 반영한 상태를 돌려준다 — A/S 회수는 고른 값과 다를 수 있어 화면이 알려야 한다
    return jsonify({"ok": True, "assets": moved, "status": back, "sms": sms})


# ---------------------------------------------------------------- 추적

def _auto_ship_confirm(conn, w, code):
    """출고 송장이 **간선상차**된 순간 그 주문을 자동으로 출고 마감한다
    (2026-09-04 대표 — "간선상차 기준으로 우리가 제품이 정상적으로 출고되었는지가 중요하다").

    [금일 출고 확인](orders/ship-today)과 같은 기록을 남기되 **보관은 하지 않는다** —
    ★고도몰 기준(2026-08-31 대표): 송장이 실려 움직이는 동안은 '배송중'이고,
    보관(archived)은 주문관리 상태 산식이 '배송완료'로 읽는다. 배송완료 도장과 보관은
    CJ 91(배달완료)에서 _auto_delivered 가 한다. 3시간 추적 데몬에서도 불리므로
    g.user 없이도 안전해야 한다(asset_event/audit.log 는 사용자 없으면 빈 값,
    담당자는 'CJ배송추적')."""
    if w["type"] == "recall" or (w["cj_kind"] or "") == "return":
        return False
    if code not in SHIP_CONFIRM_STAGES or not w["order_id"]:
        return False
    row = conn.execute("SELECT * FROM orders WHERE id=?", (w["order_id"],)).fetchone()
    if row is None or row["cancelled_at"] or row["shipping_done"]:
        return False
    ts = config.now_iso()
    conn.execute(
        "UPDATE orders SET shipping_done=1, shipping_by='CJ배송추적', shipping_at=?, "
        "  updated_at=? WHERE id=?",
        (ts, ts, row["id"]))
    for a in conn.execute(
            "SELECT a.id, a.status FROM order_assets oa JOIN assets a ON a.id=oa.asset_id "
            "WHERE oa.order_id=?", (row["id"],)).fetchall():
        if a["status"] != "shipped":
            conn.execute("UPDATE assets SET status='shipped', updated_at=? WHERE id=?",
                         (ts, a["id"]))
            asset_event(conn, a["id"], "출고", {
                "orderId": row["id"], "주문번호": row["order_no"] or "",
                "수취인": row["recipient"] or "", "채널": row["channel"] or "",
                "출고일": ts[:10], "사유": "CJ 배송 시작 자동 마감"})
        _unlist(conn, a["id"], "출고(CJ 배송 시작 자동 마감)")
    audit.log("ship_auto_cj",
              target=f"#{row['id']} {row['recipient'] or row['order_no']}",
              detail={"wid": w["wid"], "invoiceNo": w["invoice_no"],
                      "stage": CJ_STAGE.get(code, code)})
    return True


def _auto_delivered(conn, w, code):
    """CJ 배달완료(91) — ★고도몰 기준의 '배송완료'(2026-08-31 대표). 주문에 배송완료
    도장(delivered_at)을 찍고, 이때 보관(archived)까지 해 주문관리 활성 목록에서 내린다.
    (배송중 동안은 _auto_ship_confirm 이 보관하지 않아 '배송중'으로 남아 있다)"""
    if w["type"] == "recall" or (w["cj_kind"] or "") == "return":
        return False
    if code not in TERMINAL_STAGES or not w["order_id"]:
        return False
    row = conn.execute("SELECT * FROM orders WHERE id=?", (w["order_id"],)).fetchone()
    if row is None or row["cancelled_at"]:
        return False
    if row["delivered_at"] and row["archived_at"]:
        return False
    ts = config.now_iso()
    conn.execute(
        "UPDATE orders SET delivered_at=CASE WHEN delivered_at='' THEN ? ELSE delivered_at END, "
        "  archived_at=CASE WHEN archived_at='' THEN ? ELSE archived_at END, "
        "  updated_at=? WHERE id=?",
        (ts, ts, ts, row["id"]))
    audit.log("delivered_auto_cj",
              target=f"#{row['id']} {row['recipient'] or row['order_no']}",
              detail={"wid": w["wid"], "invoiceNo": w["invoice_no"]})
    return True


def _apply_stage(conn, w, code, name, *, after=None, via="CJ 추적"):
    """CJ 단계 하나를 송장에 반영하고, 그 단계가 뜻하는 자동 처리를 전부 건다.

    추적 데몬(_sync_one)과 회수 송장번호 수집(sweep_recall_invoices)이 같은 관문을 쓴다 —
    두 경로가 다르게 굴면 '어느 쪽이 먼저 봤느냐'에 따라 접수 건이 다른 자리에 선다.
      · 출고 송장: 간선상차부터 주문 출고 마감, 91 이면 배송완료 도장+보관
      · 회수 송장 91: 자산 되돌리기 + A/S 접수 건 '입고 완료'(recall_arrived)
      · A/S 반송 송장 91: 접수 건 자동 종료(_auto_as_return_delivered)
    after 에 (문자 trigger, A/S id) 를 모아 준다 — 문자는 트랜잭션 밖에서 보낸다.
    """
    ts = config.now_iso()
    is_recall = w["type"] == "recall" or (w["cj_kind"] or "") == "return"
    status = "delivered" if code in TERMINAL_STAGES else w["status"]
    conn.execute(
        "UPDATE waybills SET cj_stage_cd=?, cj_stage_nm=?, cj_stage_at=?, status=?, updated_at=? WHERE wid=?",
        (code, name, ts, status, ts, w["wid"]))
    try:
        if is_recall:
            if code in TERMINAL_STAGES and w["status"] != "delivered":
                w2 = conn.execute("SELECT * FROM waybills WHERE wid=?", (w["wid"],)).fetchone()
                _moved, tid = recall_arrived(conn, w2, via=via)
                if tid and after is not None:
                    after.append(("collected", tid))
                if w2["as_ticket_id"]:
                    audit.log("as_recall_arrived_auto", target=w["wid"],
                              detail={"invoiceNo": w["invoice_no"] or "", "ticketId": w2["as_ticket_id"]})
        else:
            # ★출고 송장이 움직이기 시작했으면 주문도 자동으로 출고 마감(2026-08-31 대표),
            #   배달완료(91)면 고도몰 기준의 '배송완료' 도장+보관까지
            if _auto_ship_confirm(conn, w, code) and after is not None and w["order_id"]:
                # 자동 출고 확인도 사람이 누른 것과 같이 몰에 송장번호를 보낸다(2026-09-08).
                #   ★09-04 이후 출고 확인의 대부분이 이 경로(간선상차)라, 여기서 안 보내면
                #   '자동으로 나가는데 몰에는 안 올라가는' 건이 매일 쌓인다.
                after.append(("mall_push", w["order_id"]))
            _auto_delivered(conn, w, code)
            _auto_as_return_delivered(conn, w, code)
    except Exception:
        pass          # 자동 처리 실패가 단계 반영까지 되돌리면 안 된다


def _sync_one(conn, w, res, after=None):
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
    _apply_stage(conn, w, code, name, after=after, via="CJ 추적")
    return True


def _recall_sweep_dates(conn, limit=10):
    """아직 안 끝난 회수 송장의 CJ 접수일 목록(YYYYMMDD, 최근순).

    ★회수는 CJ 가 집화할 때 채번하므로 번호 기준 추적(_trackable → ReqOneGdsTrc)에는
      영원히 안 잡힌다. 접수일 기준 조회(ReqMssGdsTrc)로만 번호와 단계가 온다 —
      데몬이 이 날짜들만 골라 물어보면 호출 수가 접수일 수만큼으로 묶인다.
    """
    cutoff = (config.now() - timedelta(days=31)).strftime("%Y%m%d")
    rows = conn.execute(
        "SELECT DISTINCT cj_rcpt_ymd AS d FROM waybills WHERE type='recall' AND status='issued' "
        "AND TRIM(cj_rcpt_ymd) != '' AND cj_rcpt_ymd >= ? AND cj_stage_cd NOT IN ('91') "
        "ORDER BY d DESC LIMIT ?", (cutoff, limit)).fetchall()
    return [r["d"] for r in rows]


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
            after = []                                   # (문자 trigger, A/S id)
            try:
                with tx(write=True) as c2:              # ← 짧은 쓰기만
                    if _sync_one(c2, w, res, after=after):
                        changed += 1
            except Exception:
                after = []
            _notify_after(after)                         # ← 트랜잭션 밖(원칙 #1)
        time.sleep(0.3)   # CJ 과호출 방지
    return changed


def sweep_recall_invoices(cfg, dates):
    """★회수 송장번호 수집 — 예약(접수) 기준 추적(ReqMssGdsTrc)으로 CUST_USE_NO↔INVC_NO를
    받아, 번호가 비어 있던 회수 송장에 채우고 배송단계도 함께 반영한다.

    회수는 기사가 집화할 때 CJ가 채번하므로 이 경로가 없으면 회수 건은
    '집화 시 번호 부여' 상태에서 영원히 멈춘다(RMS 실운영에서 확인된 유일한 수집 경로).
    읽기전용 조회라 배송 예약이 생기지 않는다. SND_YN은 'N' 고정 — 'Y'로 보내면
    CJ가 전송완료로 마킹해 다시 못 받는다(처리 실패 시 영구 유실).

    dates: 물어볼 CJ 접수일 목록(YYYYMMDD). 화면 버튼은 최근 N일 전부, 3시간 데몬과
    A/S [🔄 CJ 확인]은 아직 안 끝난 회수 송장의 접수일만 넘긴다(호출 수를 묶는다).
    ★단계 반영은 추적 데몬과 같은 관문(_apply_stage) — 회수 91 이면 자산 되돌리기 +
      A/S 접수 건 '입고 완료'까지 한 번에 간다(2026-09-07). 예전에는 여기서 자산만
      되돌리고 접수 건은 안 건드려 '물건은 들어왔는데 회수 중'이 됐다.
    CJ 호출은 전부 트랜잭션 밖, 반영은 짧은 쓰기 트랜잭션 한 번, 문자는 그 뒤에.
    """
    conn = get_db()
    # 접수키 → 송장. 회수/출고 가리지 않고 다 담아 두고, 반영 단계에서 가른다.
    by_cuse = {}
    for r in conn.execute(
            "SELECT * FROM waybills WHERE TRIM(cust_use_no) != ''").fetchall():
        by_cuse[(r["cust_use_no"] or "").strip().upper()] = dict(r)

    out = {"dates": 0, "rows": 0, "matched": 0, "filled": 0, "staged": 0,
           "arrived": 0, "errors": [], "unmatched": []}
    updates = []                       # (wid, invoice_no|None, (stage_cd, stage_nm)|None)
    for req_dt in dates:
        try:
            res = cj2_mss_track(cfg, req_dt, snd_yn="N")
        except Exception as e:                                   # noqa: BLE001
            out["errors"].append(f"{req_dt}: {e}")
            continue
        out["dates"] += 1
        if not res.get("ok"):
            # '해당 일자 데이터 없음'도 E로 오는 경우가 있어 기록만 하고 계속 간다
            out["errors"].append(f"{req_dt}: [{res.get('result_cd')}] {res.get('detail')}")
            continue
        for row in (res.get("rows") or []):
            out["rows"] += 1
            cuse = str(row.get("CUST_USE_NO") or "").strip().upper()
            invc = "".join(ch for ch in str(row.get("INVC_NO") or "") if ch.isdigit())
            w = by_cuse.get(cuse)
            if not w:
                if cuse and len(out["unmatched"]) < 20:
                    out["unmatched"].append({"custUseNo": row.get("CUST_USE_NO"),
                                             "invoiceNo": invc, "reqDt": req_dt})
                continue
            out["matched"] += 1
            # ★취소/실패 송장은 되살리지 않는다 — 운영자가 취소한 건이 살아 보이면 안 된다
            if w["status"] in ("canceled", "failed"):
                continue
            # ★접수구분 교차 방지 — 출고(01) 행이 회수 송장에, 또는 반대로 붙지 않게
            rd = str(row.get("RCPT_DV") or "").strip()
            is_recall = w["type"] == "recall" or w["cj_kind"] == "return"
            if rd in ("01", "02") and ((rd == "02") != is_recall):
                if len(out["unmatched"]) < 20:
                    out["unmatched"].append({"custUseNo": row.get("CUST_USE_NO"),
                                             "reason": f"접수구분 불일치(CJ {rd})"})
                continue
            new_invc = None
            if invc and not (w["invoice_no"] or "").strip():
                new_invc = invc
                w["invoice_no"] = invc          # 같은 스윕에서 중복 채움 방지
            st = str(row.get("CRG_ST") or "").strip()
            new_stage = None
            if st and st != (w["cj_stage_cd"] or ""):
                new_stage = (st, CJ_STAGE.get(st, row.get("CRG_ST_NM") or st))
                w["cj_stage_cd"] = st
            if new_invc or new_stage:
                updates.append((w["wid"], new_invc, new_stage))

    # ---- 반영은 짧은 쓰기 트랜잭션 한 번
    after = []                                          # (문자 trigger, A/S id)
    if updates:
        with tx(write=True) as c2:
            ts = config.now_iso()
            for wid, invc, stage in updates:
                w = c2.execute("SELECT * FROM waybills WHERE wid=?", (wid,)).fetchone()
                if w is None or w["status"] in ("canceled", "failed"):
                    continue
                if invc and not (w["invoice_no"] or "").strip():
                    c2.execute("UPDATE waybills SET invoice_no=?, updated_at=? WHERE wid=?",
                               (invc, ts, wid))
                    out["filled"] += 1
                    w = c2.execute("SELECT * FROM waybills WHERE wid=?", (wid,)).fetchone()
                if stage:
                    before = len(after)
                    _apply_stage(c2, w, stage[0], stage[1], after=after, via="회수 송장번호 수집")
                    out["staged"] += 1
                    out["arrived"] += len(after) - before
            audit.log("recall_invoice_sync",
                      target=f"번호 {out['filled']}건 · 단계 {out['staged']}건",
                      detail={"dates": out["dates"], "rows": out["rows"],
                              "arrived": out["arrived"], "errors": len(out["errors"])})
    _notify_after(after)                                # ← 트랜잭션 밖(원칙 #1)
    return out


@bp.post("/waybills/recall-invoice-sync")
def recall_invoice_sync():
    """배송/송장 화면의 [회수 송장번호 수집] — 최근 N일(기본 14, 최대 31)을 전부 물어본다."""
    require_any("orders.ship", "waybills.manage")
    body = request.get_json(silent=True) or {}
    try:
        days = max(1, min(int(body.get("days") or 14), 31))
    except (TypeError, ValueError):
        days = 14
    cfg = _cj_settings(get_db())
    if not _is_real(cfg):
        abort(400, description="CJ 운영 설정(운영+실발행 무장)이 있어야 수집할 수 있습니다.")
    today = config.now().date()
    dates = [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(days)]
    return jsonify(sweep_recall_invoices(cfg, dates))


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


def tracker_tick(app):
    """추적 데몬 한 바퀴 — 번호 있는 송장 추적 + 번호 없는 회수 송장의 접수일 기준 수집.

    ★회수 수집을 여기에 붙인 이유(2026-09-07): 회수 송장은 기사가 집화할 때 CJ 가 채번하므로
      번호 기준 추적(_trackable)에 절대 안 잡힌다. 사람이 배송/송장 화면에서 [회수 송장번호
      수집]을 누르지 않으면 회수 건은 접수 뒤 영원히 '회수 중'이었다(라이브에서 6일째 확인).
      아직 안 끝난 회수 송장의 접수일만 골라 묻는다 — 보통 하루에 한두 번 호출이다.
    """
    with app.app_context():
        conn = get_db()
        cfg = _cj_settings(conn)
        if not _is_real(cfg):
            return {"tracked": 0, "sweepDates": 0}
        targets = _trackable(conn)
        changed = _run_tracking(None, cfg, targets)
        dates = _recall_sweep_dates(conn)
        swept = sweep_recall_invoices(cfg, dates) if dates else None
        return {"tracked": changed, "sweepDates": len(dates),
                "arrived": (swept or {}).get("arrived", 0)}


def start_tracker(app):
    """3시간 주기 배송추적 데몬. 운영 설정이 없으면 아무것도 하지 않는다."""
    def loop():
        while True:
            time.sleep(3 * 3600)
            try:
                tracker_tick(app)
            except Exception:
                app.logger.exception("배송추적 갱신 실패")

    t = threading.Thread(target=loop, name="ows-cj-tracker", daemon=True)
    t.start()
    return t
