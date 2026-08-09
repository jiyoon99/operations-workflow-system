"""A/S 관리 — 접수 → 회수 → 수리 → 반송.

자산번호(관리번호)와 연결해 그 제품의 A/S 이력이 자산 타임라인에도 남게 한다.
수리비는 자산 원가에 반영할지 선택할 수 있다(회사 부담이면 자산 수리비로 기록).
"""
import json

from flask import Blueprint, abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require, require_any
from ..db import get_db, tx
from ..purchase import ASSET_STATUSES, asset_event
from ..settings import _int_or_400

bp = Blueprint("asvc", __name__, url_prefix="/api")

AS_TYPES = {"repair": "수리", "exchange": "교환", "refund": "환불", "inspect": "점검"}
AS_STATUSES = {
    "received": "접수",
    "collecting": "회수 중",
    "repairing": "수리 중",
    "done": "수리 완료",
    "returned": "반송 완료",
    "cancelled": "취소",
}
CHARGE = {"company": "무상(회사 부담)", "customer": "유상(고객 청구)"}
OPEN_STATUSES = ("received", "collecting", "repairing", "done")


def _next_ticket_no(conn):
    prefix = f"AS-{config.now().strftime('%y%m%d')}-"
    row = conn.execute(
        "SELECT MAX(CAST(substr(ticket_no, 11) AS INTEGER)) AS m FROM as_tickets WHERE ticket_no GLOB ?",
        (prefix + "[0-9][0-9]",)).fetchone()
    return f"{prefix}{(row['m'] or 0) + 1:02d}"


def _ticket_payload(r):
    return {
        "id": r["id"], "ticketNo": r["ticket_no"],
        "orderId": r["order_id"], "assetId": r["asset_id"],
        "assetNo": r["asset_no"] if "asset_no" in r.keys() else None,
        "model": r["model"] if "model" in r.keys() else None,
        "customer": r["customer"], "phone": r["phone"], "address": r["address"],
        "channel": r["channel"], "symptom": r["symptom"],
        "asType": r["as_type"], "asTypeLabel": AS_TYPES.get(r["as_type"], r["as_type"]),
        "status": r["status"], "statusLabel": AS_STATUSES.get(r["status"], r["status"]),
        "cost": r["cost"], "chargeTo": r["charge_to"], "chargeLabel": CHARGE.get(r["charge_to"], r["charge_to"]),
        "result": r["result"], "receivedAt": r["received_at"], "closedAt": r["closed_at"],
        "assignee": r["assignee"], "createdBy": r["created_by"], "createdAt": r["created_at"],
    }


_SELECT = ("SELECT t.*, a.asset_no, a.model FROM as_tickets t "
           "LEFT JOIN assets a ON a.id = t.asset_id WHERE 1=1")


@bp.get("/as-meta")
def as_meta():
    require_any("as.view", "as.manage")
    return jsonify({
        "types": [{"code": k, "label": v} for k, v in AS_TYPES.items()],
        "statuses": [{"code": k, "label": v} for k, v in AS_STATUSES.items()],
        "charges": [{"code": k, "label": v} for k, v in CHARGE.items()],
    })


@bp.get("/as-tickets")
def list_tickets():
    require_any("as.view", "as.manage")
    sql = _SELECT
    params = []
    view = request.args.get("view", "open")
    if view == "open":
        ph = ",".join("?" * len(OPEN_STATUSES))
        sql += f" AND t.status IN ({ph})"
        params.extend(OPEN_STATUSES)
    elif view == "closed":
        sql += " AND t.status IN ('returned','cancelled')"
    if request.args.get("status"):
        sql += " AND t.status = ?"
        params.append(request.args["status"])
    q = (request.args.get("q") or "").strip()
    if q:
        sql += (" AND (t.ticket_no LIKE ? OR t.customer LIKE ? OR t.phone LIKE ? "
                "OR t.symptom LIKE ? OR a.asset_no LIKE ?)")
        params.extend(["%" + q + "%"] * 5)
    sql += " ORDER BY t.id DESC LIMIT 300"
    rows = get_db().execute(sql, params).fetchall()
    return jsonify([_ticket_payload(r) for r in rows])


@bp.get("/as-tickets/<int:tid>")
def ticket_detail(tid):
    require_any("as.view", "as.manage")
    conn = get_db()
    row = conn.execute(_SELECT + " AND t.id = ?", (tid,)).fetchone()
    if row is None:
        abort(404, description="A/S 건을 찾을 수 없습니다.")
    events = conn.execute(
        "SELECT * FROM as_events WHERE ticket_id=? ORDER BY id DESC LIMIT 100", (tid,)).fetchall()
    out = _ticket_payload(row)
    # 진행 중인 회수 예약이 있으면 화면이 [회수 예약] 버튼 대신 그 사실을 보여 준다
    # ★취소되지 않은 회수 건이 있으면(입고 완료 포함) 회수 버튼을 다시 보여주면 안 된다.
    #   이미 받아 와 수리 중인데 버튼이 살아나면, 눌러서 기사가 고객 집으로 또 간다.
    wb = conn.execute(
        "SELECT wid, status FROM waybills WHERE as_ticket_id=? AND type='recall' "
        "AND status != 'canceled' ORDER BY created_at DESC LIMIT 1", (tid,)).fetchone()
    out["recallWid"] = wb["wid"] if wb else ""
    out["recallDone"] = bool(wb and wb["status"] == "delivered")
    rb = conn.execute(
        "SELECT wid, invoice_no FROM waybills WHERE as_ticket_id=? AND type='forward' "
        "AND status IN ('issued','test','pending') ORDER BY created_at DESC LIMIT 1", (tid,)).fetchone()
    out["returnWid"] = rb["wid"] if rb else ""
    out["returnInvoiceNo"] = rb["invoice_no"] if rb else ""
    out["events"] = [
        {"ts": e["ts"], "action": e["action"], "actor": e["actor"],
         "detail": json.loads(e["detail"]) if e["detail"] else None}
        for e in events
    ]
    return jsonify(out)


def _notify(tid, code):
    """A/S 단계 안내 문자 — 실패해도 본 작업은 그대로 둔다."""
    from flask import current_app

    from ..notify import notify_async
    ok, msg = notify_async(current_app, tid, code)
    return {"ok": ok, "message": msg}


def _as_event(conn, tid, action, detail=None):
    conn.execute(
        "INSERT INTO as_events(ticket_id, ts, action, actor, detail) VALUES(?,?,?,?,?)",
        (tid, config.now_iso(), action, g.user["display_name"],
         json.dumps(detail, ensure_ascii=False) if detail is not None else None))


@bp.post("/as-tickets")
def create_ticket():
    require("as.manage")
    body = request.get_json(silent=True) or {}
    customer = (body.get("customer") or "").strip()
    symptom = (body.get("symptom") or "").strip()
    if not customer or not symptom:
        abort(400, description="고객명과 증상은 필수입니다.")
    as_type = (body.get("asType") or "repair").strip()
    if as_type not in AS_TYPES:
        abort(400, description="알 수 없는 A/S 유형입니다.")
    charge_to = (body.get("chargeTo") or "company").strip()
    if charge_to not in CHARGE:
        abort(400, description="알 수 없는 비용 부담 구분입니다.")
    asset_id = body.get("assetId")
    order_id = body.get("orderId")
    ts = config.now_iso()
    with tx(write=True) as conn:
        if asset_id not in (None, ""):
            asset_id = _int_or_400(asset_id, "자산 ID")
            if conn.execute("SELECT id FROM assets WHERE id=?", (asset_id,)).fetchone() is None:
                abort(400, description="존재하지 않는 자산입니다.")
        else:
            asset_id = None
        if order_id not in (None, ""):
            order_id = _int_or_400(order_id, "주문 ID")
        else:
            order_id = None
        ticket_no = _next_ticket_no(conn)
        cur = conn.execute(
            "INSERT INTO as_tickets(ticket_no, order_id, asset_id, customer, phone, address, channel, "
            "symptom, as_type, status, charge_to, received_at, assignee, created_by, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'received',?,?,?,?,?,?)",
            (ticket_no, order_id, asset_id, customer, (body.get("phone") or "").strip(),
             (body.get("address") or "").strip(), (body.get("channel") or "").strip(),
             symptom, as_type, charge_to,
             (body.get("receivedAt") or "").strip() or config.now().strftime("%Y-%m-%d"),
             (body.get("assignee") or "").strip() or g.user["display_name"],
             g.user["display_name"], ts, ts))
        tid = cur.lastrowid
        _as_event(conn, tid, "접수", {"유형": AS_TYPES[as_type], "증상": symptom})
        if asset_id:
            asset_event(conn, asset_id, "A/S접수", {"ticketNo": ticket_no, "증상": symptom})
        audit.log("as_created", target=f"{ticket_no} {customer}", detail={"type": as_type})
    # 문자는 트랜잭션 밖에서 — 외부 호출이 DB 잠금을 쥐면 안 된다(원칙 #1)
    sms = _notify(tid, "received")
    return jsonify({"id": tid, "ticketNo": ticket_no, "sms": sms}), 201


@bp.patch("/as-tickets/<int:tid>")
def update_ticket(tid):
    require("as.manage")
    body = request.get_json(silent=True) or {}
    new_status = ""
    notify_code = ""
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM as_tickets WHERE id=?", (tid,)).fetchone()
        if row is None:
            abort(404, description="A/S 건을 찾을 수 없습니다.")
        changes = {}
        for key, col, label in (
            ("customer", "customer", "고객명"), ("phone", "phone", "연락처"),
            ("address", "address", "주소"), ("channel", "channel", "채널"),
            ("symptom", "symptom", "증상"), ("result", "result", "처리내용"),
            ("assignee", "assignee", "담당자"), ("receivedAt", "received_at", "접수일"),
        ):
            if key in body:
                v = (body.get(key) or "").strip()
                if v != row[col]:
                    conn.execute(f"UPDATE as_tickets SET {col}=? WHERE id=?", (v, tid))
                    changes[label] = v
        if "asType" in body:
            v = (body.get("asType") or "").strip()
            if v not in AS_TYPES:
                abort(400, description="알 수 없는 A/S 유형입니다.")
            conn.execute("UPDATE as_tickets SET as_type=? WHERE id=?", (v, tid))
            changes["유형"] = AS_TYPES[v]
        if "chargeTo" in body:
            v = (body.get("chargeTo") or "").strip()
            if v not in CHARGE:
                abort(400, description="알 수 없는 비용 부담 구분입니다.")
            conn.execute("UPDATE as_tickets SET charge_to=? WHERE id=?", (v, tid))
            changes["비용부담"] = CHARGE[v]
        if "cost" in body:
            v = _int_or_400(body["cost"], "수리비")
            conn.execute("UPDATE as_tickets SET cost=? WHERE id=?", (v, tid))
            changes["수리비"] = v
        if "status" in body:
            v = (body.get("status") or "").strip()
            if v not in AS_STATUSES:
                abort(400, description="알 수 없는 상태입니다.")
            if v != row["status"]:
                new_status = v          # 트랜잭션 밖에서 안내 문자를 보낼지 판단할 값
                closed = config.now_iso() if v in ("returned", "cancelled") else ""
                conn.execute("UPDATE as_tickets SET status=?, closed_at=? WHERE id=?", (v, closed, tid))
                _as_event(conn, tid, "상태변경",
                          {"from": AS_STATUSES.get(row["status"]), "to": AS_STATUSES[v]})
                changes["상태"] = AS_STATUSES[v]
                if row["asset_id"]:
                    asset_event(conn, row["asset_id"], "A/S" + AS_STATUSES[v],
                                {"ticketNo": row["ticket_no"]})
                    # 고객에게 돌려보냈으면 우리 재고가 아니다 — 재고 수량에서 빼야 한다.
                    # (안 그러면 남의 물건이 계속 보유 재고로 잡혀 재고·자산가치가 부풀려진다)
                    # ★'회수중'도 포함한다 — 물건을 받아 고친 뒤 입고 처리를 건너뛰고 바로
                    #   반송하는 경우가 실무에서 흔한데, 빼면 자산이 회수중에 갇혀 재고로 남는다.
                    if v == "returned":
                        conn.execute(
                            "UPDATE assets SET status='shipped', updated_at=? WHERE id=? "
                            "AND status NOT IN ('shipped','scrapped')",
                            (config.now_iso(), row["asset_id"]))
        if not changes:
            abort(400, description="변경할 항목이 없습니다.")
        conn.execute("UPDATE as_tickets SET updated_at=? WHERE id=?", (config.now_iso(), tid))
        if set(changes) - {"상태"}:
            _as_event(conn, tid, "수정", {k: v for k, v in changes.items() if k != "상태"})
        audit.log("as_updated", target=row["ticket_no"], detail=changes)
        notify_code = new_status if new_status in ("done",) else ""
    # 문자는 트랜잭션 밖에서 보낸다
    sms = _notify(tid, notify_code) if notify_code else None
    return jsonify({"ok": True, "sms": sms})


@bp.post("/as-tickets/<int:tid>/to-asset-repair")
def push_cost_to_asset(tid):
    """A/S 수리비를 해당 자산의 수리 내역으로 옮겨 원가에 반영한다(회사 부담일 때)."""
    require("as.manage")
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM as_tickets WHERE id=?", (tid,)).fetchone()
        if row is None:
            abort(404, description="A/S 건을 찾을 수 없습니다.")
        if not row["asset_id"]:
            abort(400, description="자산이 연결되지 않은 A/S 건입니다.")
        if row["cost"] <= 0:
            abort(400, description="수리비가 0원입니다.")
        if row["charge_to"] != "company":
            abort(400, description="고객 청구(유상) 건은 자산 원가에 반영하지 않습니다.")
        # ★수리비를 고쳐 적었으면 다시 반영할 수 있어야 한다.
        #   전에는 '이미 반영했습니다'로 막혀, 틀린 금액이 자산 원가에 굳었다.
        dup = conn.execute(
            "SELECT id, cost FROM asset_repairs WHERE asset_id=? AND description LIKE ?",
            (row["asset_id"], f"%{row['ticket_no']}%")).fetchone()
        desc = f"[{row['ticket_no']}] {row['result'] or row['symptom']}"
        if dup:
            if dup["cost"] == row["cost"]:
                abort(409, description="이미 같은 금액으로 반영돼 있습니다.")
            conn.execute(
                "UPDATE asset_repairs SET cost=?, description=?, repair_date=? WHERE id=?",
                (row["cost"], desc, config.now().strftime("%Y-%m-%d"), dup["id"]))
            asset_event(conn, row["asset_id"], "수리비정정",
                        {"ticketNo": row["ticket_no"], "from": dup["cost"], "to": row["cost"]})
            _as_event(conn, tid, "자산원가정정", {"from": dup["cost"], "to": row["cost"]})
            audit.log("as_cost_to_asset", target=row["ticket_no"],
                      detail={"from": dup["cost"], "to": row["cost"]})
            return jsonify({"ok": True, "updated": True})
        conn.execute(
            "INSERT INTO asset_repairs(asset_id, repair_date, description, cost, created_by, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (row["asset_id"], config.now().strftime("%Y-%m-%d"), desc, row["cost"],
             g.user["display_name"], config.now_iso()))
        asset_event(conn, row["asset_id"], "수리",
                    {"ticketNo": row["ticket_no"], "cost": row["cost"], "출처": "A/S"})
        _as_event(conn, tid, "자산원가반영", {"cost": row["cost"]})
        audit.log("as_cost_to_asset", target=row["ticket_no"], detail={"cost": row["cost"]})
    return jsonify({"ok": True})


@bp.get("/as-tickets/asset-search")
def as_asset_search():
    """A/S 접수 시 자산 찾기 — 출고된 자산도 검색된다(고객이 쓰던 물건이므로)."""
    require("as.manage")
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    rows = get_db().execute(
        "SELECT a.id, a.asset_no, a.maker, a.model, a.status, "
        " (SELECT o.id FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
        "  WHERE oa.asset_id=a.id ORDER BY oa.matched_at DESC LIMIT 1) AS order_id, "
        " (SELECT o.recipient FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
        "  WHERE oa.asset_id=a.id ORDER BY oa.matched_at DESC LIMIT 1) AS recipient "
        "FROM assets a WHERE a.asset_no LIKE ? OR a.serial LIKE ? OR a.model LIKE ? "
        "ORDER BY a.asset_no LIMIT 20", ("%" + q + "%",) * 3).fetchall()
    return jsonify([
        # ★한글 이름을 서버가 함께 준다 — 화면이 매입 메뉴 데이터를 미리 받아 뒀는지에
        #   기대면, 매입 권한이 없는 A/S 담당에게는 'shipped' 같은 영어가 그대로 보인다.
        {"assetId": r["id"], "assetNo": r["asset_no"], "maker": r["maker"], "model": r["model"],
         "status": r["status"], "statusLabel": ASSET_STATUSES.get(r["status"], r["status"]),
         "orderId": r["order_id"], "recipient": r["recipient"]}
        for r in rows
    ])
