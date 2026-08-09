"""매입/자산 관리 — 거래처, 입고배치, 자산(자산번호 기준 이력관리), 수리비.

핵심 요구(2026-07-28 대표):
- 자산번호 자동 발번 = YYYYMMDD + 당일 순번 4자리 (예: 202607280001)
- 단, 기존 TMS 자산번호 이관을 위해 수동 지정/수정 가능 (UNIQUE 유지, 변경 이력 기록)
- 자산번호 기준 이력관리(asset_events 타임라인)가 최우선
"""
import json

from flask import Blueprint, abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import get_db, tx
from ..settings import _int_or_400, _money_or_400

bp = Blueprint("purchase", __name__, url_prefix="/api")

# 자산 상태. reserved/shipped/returning은 주문 흐름이 관리 — 수동 변경 불가.
# TMS는 '수리·A/S·불량·도색대기'를 등급 목록에 섞어 두었지만, 재고 집계 정확도를 위해
# HMS는 품질등급(GRADES)과 작업상태(여기)를 분리한다(2026-07-28 대표 결정).
ASSET_STATUSES = {
    "in_stock": "입고",
    "refurbishing": "정비중",
    "repair": "수리",
    "as": "A/S",
    "painting": "도색대기",
    "defective": "불량",
    "ready": "판매가능",
    "reserved": "주문매칭",
    "shipped": "출고완료",
    "returning": "회수중",
    "scrapped": "폐기",
    # 전표가 취소되면 그 전표로 들어온 자산도 함께 무효가 된다(재고에서 빠진다).
    "cancelled": "매입취소",
    # 거래처로 되돌려 보낸 물건 — 우리 재고가 아니다.
    "returned": "거래처반품",
}
# 매칭 가능한(재고로 잡히는) 상태
AVAILABLE_STATUSES = ("in_stock", "refurbishing", "ready")

# ★판매 가능 자산 = 상태가 맞고 + '실물을 받은' 것.
#   가입고(V) 전표로 등록한 자산은 아직 물건이 안 들어온 상태라 재고로 세면 안 된다.
#   이걸 빠뜨리면 안 받은 노트북이 셋팅 화면에서 배정된다(2026-07-30 E2E에서 확인).
#   schema.sql에 'received … 가입고 자산은 0으로 시작'이라고 의도가 적혀 있었으나
#   실제 코드가 한 번도 0을 넣지 않아 죽은 필드였다.
RECEIVED_SQL = " AND a.received = 1"
MANUAL_STATUSES = {"in_stock", "refurbishing", "repair", "as", "painting",
                   "defective", "ready", "scrapped"}

# 품질등급 — TMS 목록에서 작업상태를 뺀 것
GRADES = ["미정", "NU", "S+A", "S+S", "SS", "SA", "AS", "AA", "SB", "AB", "A급", "B급", "C급"]

# 재고 3단계(2026-08-04 대표 지시)
#   양품   = 바로 판매 가능
#   실재고 = 부분적 수리·보수 후 사용 가능한 상태(재고로 친다)
#   가재고 = 입고는 됐지만 완전한 수리가 끝나야 사용 가능 — ★출고 금지.
#            매칭(자산번호 등록)까지는 되지만, 매입에서 양품/실재고로 바꾸기 전에는
#            출고 확인·송장 발급이 막힌다.
TIERS = ("양품", "실재고", "가재고")
# 화면에 그대로 띄우는 설명 — 구분을 모르는 담당자가 있을 수 있어 문구를 한곳에서 관리한다
# (대표 지시 2026-08-04). 여기만 고치면 매입·셋팅 어디서든 같은 말이 나온다.
TIER_HELP = {
    "양품": "즉시 SSD, RAM 및 윈도우 셋팅 후 출고 가능한 제품",
    "실재고": "시트지, 도색, 간단한 짜깁기 이후 출고 가능한 제품",
    "가재고": "매입은 완료하였으나, 바로 판매가 불가능한 제품",
}


def tier_for_grade(grade):
    """등급만 있고 재고구분이 없을 때 어느 칸으로 볼지 — 대표 기준(2026-08-04).

    등급이 매겨졌다는 건 검수가 끝났다는 뜻이라 양품으로 본다.
    아직 '미정'이면 손볼 데가 있는지 모르는 상태라 실재고로 둔다.
    가재고는 사람이 직접 옮기는 칸이라 여기서 자동으로 정하지 않는다.
    """
    g = (grade or "").strip()
    return "실재고" if (not g or g == "미정") else "양품"

# 매입 전표 단계
STAGES = {"provisional": "가입고", "purchased": "매입"}
PURCHASE_TYPES = ["일반매입", "위탁매입", "반품매입", "기타"]


# ---------------------------------------------------------------- helpers

def asset_event(conn, asset_id, action, detail=None):
    """자산 이력 기록 — 모든 자산 변경은 이 함수를 거친다."""
    actor = g.user["display_name"] if g.get("user") else ""
    conn.execute(
        "INSERT INTO asset_events(asset_id, ts, action, actor, detail) VALUES(?,?,?,?,?)",
        (asset_id, config.now_iso(), action, actor,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None),
    )


def scope_clause(alias="a"):
    """카테고리 스코프 — 스코프 밖 데이터는 서버 쿼리 레벨에서 제외(원칙)."""
    user = g.user
    if user["is_admin"] or user["all_categories"]:
        return "", []
    ids = [r["category_id"] for r in get_db().execute(
        "SELECT category_id FROM user_categories WHERE user_id=?", (user["id"],)).fetchall()]
    if not ids:
        return f" AND {alias}.category_id = -1", []  # 스코프 없음 → 아무것도 안 보임
    ph = ",".join("?" * len(ids))
    return f" AND {alias}.category_id IN ({ph})", ids


def _check_asset_scope(conn, category_id):
    """쓰기 시 스코프 검증 — 자기 카테고리 밖 자산은 만들/수정할 수 없다."""
    user = g.user
    if user["is_admin"] or user["all_categories"]:
        return
    ids = {r["category_id"] for r in conn.execute(
        "SELECT category_id FROM user_categories WHERE user_id=?", (user["id"],)).fetchall()}
    if category_id not in ids:
        abort(403, description="담당 카테고리가 아닌 자산은 등록/수정할 수 없습니다.")


# ★★TMS와 번호대를 나눈다 (대표 결정 2026-07-31).
#
# 두 시스템을 함께 쓰는 동안 규칙이 같으면 같은 번호가 서로 다른 물건에 붙는다.
# 실제로 확인했다: HMS가 새 매입을 등록하면 P260731-001 / 260731-0001이 나오는데,
# 같은 날 TMS에서 등록해도 똑같은 번호가 나온다. 그러면 다음 이관 때 HMS가
# '이미 있는 물건'으로 보고 건너뛰어 TMS 실물이 조용히 누락된다
# (시리얼이 양쪽에 있으면 오류로 막히지만 TMS 시리얼 채움률이 13%뿐이다).
#
# 형식·자릿수는 그대로 두고 시작 번호만 나눈다 — 라벨·서류가 지금과 똑같이 보이고,
# 번호만 봐도 어느 시스템에서 만든 것인지 알 수 있다.
#   자산: TMS 0001~4999 / HMS 5000~9999   (TMS 하루 최대 실적 323번)
#   전표: TMS 001~499   / HMS 500~999     (TMS 하루 최대 실적 29번)
# TMS를 접으면 아래 두 값을 0으로 되돌리면 된다.
HMS_ASSET_SEQ_START = 5000
HMS_SLIP_SEQ_START = 500


def next_asset_no(conn):
    """관리번호 = YYMMDD-NNNN (TMS와 같은 형식, 번호대만 HMS 몫을 쓴다)."""
    prefix = config.now().strftime("%y%m%d")
    row = conn.execute(
        "SELECT MAX(CAST(substr(asset_no, 8) AS INTEGER)) AS m FROM assets "
        "WHERE asset_no GLOB ? AND CAST(substr(asset_no, 8) AS INTEGER) >= ?",
        (prefix + "-[0-9][0-9][0-9][0-9]", HMS_ASSET_SEQ_START),
    ).fetchone()
    # 오늘 HMS가 쓴 번호가 없으면 우리 몫의 첫 번호부터 시작한다.
    seq = (row["m"] + 1) if row["m"] else HMS_ASSET_SEQ_START
    if seq > 9999:
        abort(400, description="당일 관리번호 발번 한도(9999)를 초과했습니다.")
    return f"{prefix}-{seq:04d}"


def next_slip_no(conn, stage):
    """전표번호 = V/P + YYMMDD-NNN (가입고 V / 매입 P). 번호대는 HMS 몫."""
    head = "V" if stage == "provisional" else "P"
    prefix = f"{head}{config.now().strftime('%y%m%d')}-"
    row = conn.execute(
        "SELECT MAX(CAST(substr(slip_no, 9) AS INTEGER)) AS m FROM purchase_batches "
        "WHERE slip_no GLOB ? AND CAST(substr(slip_no, 9) AS INTEGER) >= ?",
        (prefix + "[0-9][0-9][0-9]", HMS_SLIP_SEQ_START),
    ).fetchone()
    seq = (row["m"] + 1) if row["m"] else HMS_SLIP_SEQ_START
    if seq > 999:
        abort(400, description="당일 전표번호 발번 한도(999)를 초과했습니다.")
    return f"{prefix}{seq:03d}"


SPEC_FIELDS = ("cpu", "gpu", "ram", "ssd", "inch", "battery", "charger", "location")


def _asset_payload(row, extra=None):
    out = {
        "id": row["id"],
        "assetNo": row["asset_no"],
        "batchId": row["batch_id"],
        "categoryId": row["category_id"],
        "maker": row["maker"],
        "model": row["model"],
        "serial": row["serial"],
        "grade": row["grade"],
        "purchasePrice": row["purchase_price"],
        "salePrice": row["sale_price"],
        "status": row["status"],
        "statusLabel": ASSET_STATUSES.get(row["status"], row["status"]),
        "notes": row["notes"],
        "createdBy": row["created_by"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    for f in SPEC_FIELDS:
        out[f] = row[f]
    out["received"] = bool(row["received"])
    out["receivedAt"] = row["received_at"]
    out["receivedBy"] = row["received_by"]
    out["tier"] = row["tier"]                       # 양품/실재고/가재고
    out["productCode"] = row["product_code"]        # 쇼핑몰 재고의 축(대표 수기 입력)
    out["stockListed"] = bool(row["stock_listed"])  # 재고반영 여부(기본 꺼짐)
    out["stockListedBy"] = row["stock_listed_by"]
    if extra:
        out.update(extra)
    return out


@bp.get("/purchase-meta")
def purchase_meta():
    """매입 화면이 쓰는 선택지 모음(등급/상태/단계/매입구분)."""
    require("purchase.view")
    return jsonify({
        "grades": GRADES,
        "statuses": [{"code": k, "label": v} for k, v in ASSET_STATUSES.items()],
        "manualStatuses": sorted(MANUAL_STATUSES),
        "availableStatuses": list(AVAILABLE_STATUSES),
        "stages": [{"code": k, "label": v} for k, v in STAGES.items()],
        "purchaseTypes": PURCHASE_TYPES,
        "tiers": list(TIERS),
        "tierHelp": TIER_HELP,
    })


def _get_asset_or_404(conn, asset_id):
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if row is None:
        abort(404, description="자산을 찾을 수 없습니다.")
    return row


# ---------------------------------------------------------------- suppliers

@bp.get("/suppliers")
def list_suppliers():
    require("purchase.view")
    rows = get_db().execute(
        "SELECT s.*, "
        " (SELECT COUNT(*) FROM purchase_batches b WHERE b.supplier_id = s.id) AS batch_count, "
        " (SELECT COALESCE(SUM(b.total_amount),0) FROM purchase_batches b WHERE b.supplier_id = s.id) AS total_amount "
        "FROM suppliers s ORDER BY s.name"
    ).fetchall()
    return jsonify([
        {"id": r["id"], "name": r["name"], "contact": r["contact"], "phone": r["phone"],
         "memo": r["memo"], "enabled": bool(r["enabled"]),
         "batchCount": r["batch_count"], "totalAmount": r["total_amount"]}
        for r in rows
    ])


@bp.post("/suppliers")
def create_supplier():
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        abort(400, description="거래처 이름을 입력하세요.")
    with tx(write=True) as conn:
        if conn.execute("SELECT id FROM suppliers WHERE name=?", (name,)).fetchone():
            abort(409, description="이미 존재하는 거래처입니다.")
        cur = conn.execute(
            "INSERT INTO suppliers(name, contact, phone, memo, created_at) VALUES(?,?,?,?,?)",
            (name, (body.get("contact") or "").strip(), (body.get("phone") or "").strip(),
             (body.get("memo") or "").strip(), config.now_iso()),
        )
        audit.log("supplier_created", target=name)
        sid = cur.lastrowid
    return jsonify({"id": sid, "name": name}), 201


@bp.patch("/suppliers/<int:sid>")
def update_supplier(sid):
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
        if row is None:
            abort(404, description="거래처를 찾을 수 없습니다.")
        changes = {}
        if "name" in body:
            name = (body.get("name") or "").strip()
            if not name:
                abort(400, description="거래처 이름을 입력하세요.")
            dup = conn.execute("SELECT id FROM suppliers WHERE name=? AND id != ?", (name, sid)).fetchone()
            if dup:
                abort(409, description="이미 존재하는 거래처 이름입니다.")
            conn.execute("UPDATE suppliers SET name=? WHERE id=?", (name, sid))
            changes["name"] = name
        for field, col in (("contact", "contact"), ("phone", "phone"), ("memo", "memo")):
            if field in body:
                conn.execute(f"UPDATE suppliers SET {col}=? WHERE id=?",
                             ((body.get(field) or "").strip(), sid))
                changes[field] = (body.get(field) or "").strip()
        if "enabled" in body:
            conn.execute("UPDATE suppliers SET enabled=? WHERE id=?", (1 if body["enabled"] else 0, sid))
            changes["enabled"] = bool(body["enabled"])
        if not changes:
            abort(400, description="변경할 항목이 없습니다.")
        audit.log("supplier_updated", target=row["name"], detail=changes)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- batches

def _batch_payload(r):
    return {
        "id": r["id"], "slipNo": r["slip_no"], "stage": r["stage"],
        "stageLabel": STAGES.get(r["stage"], r["stage"]),
        "supplierId": r["supplier_id"], "supplierName": r["supplier_name"],
        "purchaseDate": r["purchase_date"], "totalAmount": r["total_amount"], "memo": r["memo"],
        "purchaseType": r["purchase_type"], "channel": r["channel"],
        "receiveMethod": r["receive_method"], "address": r["address"],
        "vat": r["vat"], "fee": r["fee"], "shippingFee": r["shipping_fee"],
        "shippingCod": bool(r["shipping_cod"]), "trackingNo": r["tracking_no"],
        "paid": bool(r["paid"]), "paidAmount": r["paid_amount"], "paidAt": r["paid_at"],
        "paidMemo": r["paid_memo"],
        "unpaidAmount": max(0, (r["total_amount"] or 0) - (r["paid_amount"] or 0)),
        "confirmedAt": r["confirmed_at"], "confirmedBy": r["confirmed_by"],
        "assetCount": r["asset_count"], "assignedAmount": r["assigned_amount"],
        "pendingCount": r["pending_count"],       # 아직 입고확인 안 된 자산 수
        "cancelledAt": r["cancelled_at"], "cancelledBy": r["cancelled_by"],
        "cancelReason": r["cancel_reason"],
        "returnedAt": r["returned_at"], "returnReason": r["return_reason"],
        "createdBy": r["created_by"], "createdAt": r["created_at"],
    }


_BATCH_SELECT = (
    "SELECT b.*, s.name AS supplier_name, "
    " (SELECT COUNT(*) FROM assets a WHERE a.batch_id = b.id) AS asset_count, "
    " (SELECT COALESCE(SUM(a.purchase_price),0) FROM assets a WHERE a.batch_id = b.id) AS assigned_amount, "
    " (SELECT COUNT(*) FROM assets a WHERE a.batch_id = b.id AND a.received = 0) AS pending_count "
    "FROM purchase_batches b LEFT JOIN suppliers s ON s.id = b.supplier_id WHERE 1=1"
)


@bp.get("/purchase-batches")
def list_batches():
    require("purchase.view")
    sql = _BATCH_SELECT
    params = []
    if request.args.get("stage"):
        sql += " AND b.stage = ?"
        params.append(request.args["stage"])
    if request.args.get("supplierId"):
        sql += " AND b.supplier_id = ?"
        params.append(_int_or_400(request.args["supplierId"], "거래처 ID"))
    if request.args.get("unpaid") == "1":
        sql += " AND b.paid = 0 AND b.stage = 'purchased'"
    # 취소 전표는 기본으로 감춘다 — 목록이 취소분으로 지저분해지면 진행 중인 일을 놓친다.
    # TMS도 '취소포함' 체크를 꺼 둔 상태가 기본이다.
    if request.args.get("includeCancelled") != "1":
        sql += " AND b.cancelled_at = ''"
    q = (request.args.get("q") or "").strip()
    if q:
        sql += " AND (b.slip_no LIKE ? OR s.name LIKE ? OR b.memo LIKE ? OR b.tracking_no LIKE ?)"
        params.extend(["%" + q + "%"] * 4)
    sql += " ORDER BY b.purchase_date DESC, b.id DESC LIMIT 300"
    rows = get_db().execute(sql, params).fetchall()
    return jsonify([_batch_payload(r) for r in rows])


@bp.get("/purchase-batches/<int:bid>")
def batch_detail(bid):
    require("purchase.view")
    conn = get_db()
    row = conn.execute(_BATCH_SELECT + " AND b.id = ?", (bid,)).fetchone()
    if row is None:
        abort(404, description="전표를 찾을 수 없습니다.")
    clause, params = scope_clause("a")
    assets = conn.execute(
        "SELECT a.*, c.name AS category_name FROM assets a "
        "LEFT JOIN categories c ON c.id = a.category_id WHERE a.batch_id = ?" + clause +
        " ORDER BY a.asset_no", [bid] + params
    ).fetchall()
    out = _batch_payload(row)
    out["assets"] = [_asset_payload(a, {"categoryName": a["category_name"]}) for a in assets]
    return jsonify(out)


def _batch_fields_from_body(body, conn, stage=None):
    """전표 공통 필드 파싱. (컬럼, 값) 목록과 감사용 dict를 돌려준다.

    stage를 넘기면 그 단계에 맞는 규칙을 적용한다(매입 전표는 거래처를 비울 수 없다).
    """
    sets, changes = [], {}

    def put(col, val, label=None):
        sets.append((col, val))
        changes[label or col] = val

    if "supplierId" in body:
        sid = body.get("supplierId")
        if sid in (None, ""):
            # ★매입 확정된 전표에서 거래처를 비우면 매입가·매입일의 근거가 사라진다.
            #   생성·확정 때는 거래처를 필수로 받으면서 수정만 열어 두면 규칙이 어긋난다.
            if stage == "purchased":
                abort(400, description="매입 전표의 거래처는 비울 수 없습니다.")
            put("supplier_id", None, "거래처")
        else:
            sid = _int_or_400(sid, "거래처 ID")
            if conn.execute("SELECT id FROM suppliers WHERE id=?", (sid,)).fetchone() is None:
                abort(400, description="존재하지 않는 거래처입니다.")
            put("supplier_id", sid, "거래처")
    if "purchaseDate" in body:
        v = (body.get("purchaseDate") or "").strip()
        if not v:
            abort(400, description="일자를 입력하세요.")
        put("purchase_date", v, "일자")
    # 신청자(requester)는 받지 않는다 — 등록한 사람이 곧 신청자이므로 created_by로 기록된다.
    for key, col, label in (
        ("memo", "memo", "메모"), ("purchaseType", "purchase_type", "매입구분"),
        ("channel", "channel", "매입방법"), ("receiveMethod", "receive_method", "수령방식"),
        ("address", "address", "주소"), ("trackingNo", "tracking_no", "송장번호"),
    ):
        if key in body:
            put(col, (body.get(key) or "").strip(), label)
    for key, col, label in (
        ("totalAmount", "total_amount", "매입금액"), ("vat", "vat", "부가세"),
        ("fee", "fee", "수수료"), ("shippingFee", "shipping_fee", "배송비"),
    ):
        if key in body:
            put(col, _money_or_400(body[key] or 0, label), label)
    if "shippingCod" in body:
        put("shipping_cod", 1 if body["shippingCod"] else 0, "착불")
    if "paid" in body:
        put("paid", 1 if body["paid"] else 0, "납부확인")
    if "paidAmount" in body:
        put("paid_amount", _money_or_400(body["paidAmount"] or 0, "지급액"), "지급액")
    if "paidAt" in body:
        put("paid_at", (body.get("paidAt") or "").strip(), "지급일")
    if "paidMemo" in body:
        put("paid_memo", (body.get("paidMemo") or "").strip(), "지급메모")
    return sets, changes


@bp.post("/purchase-batches")
def create_batch():
    """전표 생성. stage='provisional'이면 가입고, 아니면 매입."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    stage = (body.get("stage") or "purchased").strip()
    if stage not in STAGES:
        abort(400, description="알 수 없는 단계입니다.")
    purchase_date = (body.get("purchaseDate") or "").strip()
    if not purchase_date:
        abort(400, description="일자를 입력하세요.")
    with tx(write=True) as conn:
        sets, changes = _batch_fields_from_body(body, conn)
        if stage == "purchased" and not any(c == "supplier_id" and v for c, v in sets):
            abort(400, description="매입 전표에는 거래처가 필요합니다.")
        ts = config.now_iso()
        slip_no = next_slip_no(conn, stage)
        cols = ["slip_no", "stage", "purchase_date", "created_by", "created_at", "updated_at"]
        vals = [slip_no, stage, purchase_date, g.user["display_name"], ts, ts]
        for col, val in sets:
            if col != "purchase_date":
                cols.append(col)
                vals.append(val)
        conn.execute(
            f"INSERT INTO purchase_batches({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
        bid = conn.execute("SELECT id FROM purchase_batches WHERE slip_no=?", (slip_no,)).fetchone()["id"]

        # ★전표와 자산을 한 번에 저장한다(TMS의 [상세추가] → 저장과 같은 흐름).
        #   전표당 평균 32대, 최대 366대라 따로 넣으면 중간에 끊겼을 때 반쪽 전표가 남는다.
        #   한 트랜잭션이므로 자산 한 줄이라도 잘못되면 전표까지 통째로 취소된다.
        items = body.get("assets")
        created = []
        if items:
            if not isinstance(items, list):
                abort(400, description="assets는 목록이어야 합니다.")
            if len(items) > 200:
                abort(400, description="한 번에 등록할 수 있는 줄은 200개까지입니다.")
            received = 0 if stage == "provisional" else 1
            for n, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    abort(400, description=f"{n}번째 줄이 올바르지 않습니다.")
                created.extend(_add_assets(conn, item, bid, received, label=f"{n}번째 줄: "))

        audit.log("batch_created", target=slip_no,
                  detail={"stage": stage, "assets": len(created), **changes})
    return jsonify({"id": bid, "slipNo": slip_no, "stage": stage,
                    "assets": created, "assetCount": len(created)}), 201


@bp.patch("/purchase-batches/<int:bid>")
def update_batch(bid):
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM purchase_batches WHERE id=?", (bid,)).fetchone()
        if row is None:
            abort(404, description="전표를 찾을 수 없습니다.")
        sets, changes = _batch_fields_from_body(body, conn, stage=row["stage"])
        if not sets:
            abort(400, description="변경할 항목이 없습니다.")
        for col, val in sets:
            conn.execute(f"UPDATE purchase_batches SET {col}=? WHERE id=?", (val, bid))
        conn.execute("UPDATE purchase_batches SET updated_at=? WHERE id=?", (config.now_iso(), bid))
        audit.log("batch_updated", target=row["slip_no"] or str(bid), detail=changes)
    return jsonify({"ok": True})


# 취소해도 되는 자산 상태 — 아직 우리 손 안에 있는 것만.
# 주문에 매칭됐거나 출고·회수 중인 물건이 걸려 있으면 전표를 무를 수 없다.
# ★'returned'(거래처반품)도 포함한다. 빠져 있으면 반품 처리한 전표를 취소하려 할 때
#   "주문에 나갔거나 회수 중인 자산이 있어"라는 엉뚱한 문구가 떠서
#   담당자가 주문 화면에서 매칭을 풀려고 헛수고를 한다(실제 원인은 자기가 한 반품).
_CANCELABLE_ASSET = ("in_stock", "refurbishing", "ready", "repair", "as", "painting",
                     "defective", "returned")


def _batch_or_404(conn, bid):
    row = conn.execute("SELECT * FROM purchase_batches WHERE id=?", (bid,)).fetchone()
    if row is None:
        abort(404, description="전표를 찾을 수 없습니다.")
    return row


def _blocking_assets(conn, bid):
    """전표를 무를 수 없게 만드는 자산들 — 왜 안 되는지 사람 말로 돌려준다."""
    rows = conn.execute(
        "SELECT asset_no, status FROM assets WHERE batch_id=? AND status NOT IN "
        f"({','.join('?' * len(_CANCELABLE_ASSET))})",
        [bid] + list(_CANCELABLE_ASSET)).fetchall()
    return [f"{r['asset_no']}({ASSET_STATUSES.get(r['status'], r['status'])})" for r in rows]


@bp.post("/purchase-batches/<int:bid>/cancel")
def cancel_batch(bid):
    """전표 취소 — 지우지 않고 '취소'로 표시한다.

    ★삭제가 아니라 취소인 이유: 전표번호는 이미 소비됐고, 없던 일로 만들면
      번호가 왜 비었는지 나중에 아무도 설명할 수 없다. TMS도 '매입취소'로 남긴다
      (465건 중 6건, 결번은 그대로 둔다).
    딸린 자산은 함께 '매입취소' 상태가 되어 재고에서 빠진다.
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "").strip()
    with tx(write=True) as conn:
        row = _batch_or_404(conn, bid)
        if row["cancelled_at"]:
            abort(400, description="이미 취소된 전표입니다.")
        blocking = _blocking_assets(conn, bid)
        if blocking:
            abort(400, description=(
                "주문에 나갔거나 회수 중인 자산이 있어 취소할 수 없습니다: "
                + ", ".join(blocking[:5])
                + (f" 외 {len(blocking) - 5}대" if len(blocking) > 5 else "")))
        ts = config.now_iso()
        actor = g.user["display_name"]
        assets = conn.execute("SELECT id, asset_no, status FROM assets WHERE batch_id=?",
                              (bid,)).fetchall()
        for a in assets:
            # ★취소 직전 상태를 이력에 남긴다 — 되돌릴 때 그대로 복원하기 위해서다.
            #   이게 없으면 판매가능·불량·도색대기가 전부 '입고'로 뭉개져
            #   셋팅을 다 끝낸 자산을 손으로 다시 올려야 한다.
            conn.execute("UPDATE assets SET status='cancelled', updated_at=? WHERE id=?",
                         (ts, a["id"]))
            asset_event(conn, a["id"], "매입취소",
                        {"전표": row["slip_no"], "사유": reason, "이전상태": a["status"]})
        conn.execute(
            "UPDATE purchase_batches SET cancelled_at=?, cancelled_by=?, cancel_reason=?, "
            "updated_at=? WHERE id=?", (ts, actor, reason, ts, bid))
        audit.log("batch_cancelled", target=row["slip_no"],
                  detail={"assets": len(assets), "reason": reason})
    return jsonify({"ok": True, "cancelledAssets": len(assets)})


@bp.post("/purchase-batches/<int:bid>/uncancel")
def uncancel_batch(bid):
    """취소 되돌리기 — 잘못 취소한 경우. 자산도 함께 입고 상태로 돌린다."""
    require("purchase.edit")
    with tx(write=True) as conn:
        row = _batch_or_404(conn, bid)
        if not row["cancelled_at"]:
            abort(400, description="취소된 전표가 아닙니다.")
        ts = config.now_iso()
        n = 0
        for a in conn.execute(
                "SELECT id FROM assets WHERE batch_id=? AND status='cancelled'", (bid,)).fetchall():
            # 취소할 때 적어 둔 '이전상태'로 되돌린다. 못 찾으면 입고로.
            prev = conn.execute(
                "SELECT detail FROM asset_events WHERE asset_id=? AND action='매입취소' "
                "ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
            back = "in_stock"
            if prev and prev["detail"]:
                try:
                    back = json.loads(prev["detail"]).get("이전상태") or "in_stock"
                except (ValueError, TypeError):
                    pass
            if back not in ASSET_STATUSES or back == "cancelled":
                back = "in_stock"
            conn.execute("UPDATE assets SET status=?, updated_at=? WHERE id=?",
                         (back, ts, a["id"]))
            asset_event(conn, a["id"], "매입취소 해제",
                        {"전표": row["slip_no"], "복원상태": back})
            n += 1
        conn.execute(
            "UPDATE purchase_batches SET cancelled_at='', cancelled_by='', cancel_reason='', "
            "updated_at=? WHERE id=?", (ts, bid))
        audit.log("batch_uncancelled", target=row["slip_no"], detail={"assets": n})
    return jsonify({"ok": True, "restoredAssets": n})


@bp.post("/purchase-batches/<int:bid>/return")
def return_batch(bid):
    """매입 반품 — 거래처로 되돌려 보냈다는 기록.

    TMS에도 '반품일' 칸이 있지만 실사용은 0건이었다(14,966건 전수 확인).
    그래도 기록할 곳은 있어야 하므로 전표 단위로 남기고, 자산은 재고에서 뺀다.
    일부만 반품하는 경우는 자산 상태를 개별로 '거래처반품'으로 바꾸면 된다.
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "").strip()
    date = (body.get("returnedAt") or "").strip() or config.now().strftime("%Y-%m-%d")
    only = body.get("assetIds")            # 일부만 반품할 때
    with tx(write=True) as conn:
        row = _batch_or_404(conn, bid)
        if row["cancelled_at"]:
            abort(400, description="취소된 전표는 반품 처리할 수 없습니다.")
        if only:
            ids = [_int_or_400(x, "자산 ID") for x in only]
            ph = ",".join("?" * len(ids))
            targets = conn.execute(
                f"SELECT id, asset_no, status FROM assets WHERE batch_id=? AND id IN ({ph})",
                [bid] + ids).fetchall()
        else:
            targets = conn.execute(
                "SELECT id, asset_no, status FROM assets WHERE batch_id=?", (bid,)).fetchall()
        bad = [f"{t['asset_no']}({ASSET_STATUSES.get(t['status'], t['status'])})"
               for t in targets if t["status"] not in _CANCELABLE_ASSET]
        if bad:
            abort(400, description="주문에 나갔거나 회수 중인 자산은 반품 처리할 수 없습니다: "
                                   + ", ".join(bad[:5]))
        ts = config.now_iso()
        for t in targets:
            conn.execute("UPDATE assets SET status='returned', updated_at=? WHERE id=?",
                         (ts, t["id"]))
            asset_event(conn, t["id"], "거래처반품",
                        {"전표": row["slip_no"], "반품일": date, "사유": reason})
        # 전체 반품일 때만 전표에 반품일을 남긴다(일부 반품은 자산 이력에만)
        if not only:
            conn.execute(
                "UPDATE purchase_batches SET returned_at=?, return_reason=?, updated_at=? WHERE id=?",
                (date, reason, ts, bid))
        audit.log("batch_returned", target=row["slip_no"],
                  detail={"assets": len(targets), "partial": bool(only), "reason": reason})
    return jsonify({"ok": True, "returnedAssets": len(targets)})


@bp.post("/purchase-batches/<int:bid>/confirm")
def confirm_batch(bid):
    """가입고 → 매입 전환(입고확인). 매입 전표번호를 새로 부여한다."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM purchase_batches WHERE id=?", (bid,)).fetchone()
        if row is None:
            abort(404, description="전표를 찾을 수 없습니다.")
        if row["stage"] != "provisional":
            abort(400, description="가입고 전표만 매입으로 확정할 수 있습니다.")
        # ★취소된 전표를 확정하면 매입번호만 하나 소비하고 전표는 취소인 채로 목록에서 사라진다
        #   (P번호 결번이 생기고 왜 비었는지 설명할 수 없다). 반품 쪽엔 이미 같은 가드가 있다.
        if row["cancelled_at"]:
            abort(400, description="취소된 전표는 매입 확정할 수 없습니다. 먼저 취소를 되돌리세요.")
        sets, changes = _batch_fields_from_body(body, conn)
        for col, val in sets:
            conn.execute(f"UPDATE purchase_batches SET {col}=? WHERE id=?", (val, bid))
        cur = conn.execute("SELECT supplier_id FROM purchase_batches WHERE id=?", (bid,)).fetchone()
        if not cur["supplier_id"]:
            abort(400, description="매입 확정에는 거래처가 필요합니다.")
        ts = config.now_iso()
        slip_no = next_slip_no(conn, "purchased")
        conn.execute(
            "UPDATE purchase_batches SET stage='purchased', slip_no=?, confirmed_at=?, confirmed_by=?, "
            "updated_at=? WHERE id=?",
            (slip_no, ts, g.user["display_name"], ts, bid))
        # 가입고 상태로 묶여 있던 자산을 '받은 것'으로 바꾸고 이력에 남긴다.
        # ★이 UPDATE가 없으면 received가 영영 0으로 남아 재고에 안 잡힌다.
        conn.execute(
            "UPDATE assets SET received=1, received_at=?, received_by=?, updated_at=? "
            "WHERE batch_id=? AND received=0",
            (ts, g.user["display_name"], ts, bid))
        for a in conn.execute("SELECT id FROM assets WHERE batch_id=?", (bid,)).fetchall():
            asset_event(conn, a["id"], "입고확인", {"slipNo": slip_no, "from": row["slip_no"]})
        audit.log("batch_confirmed", target=slip_no,
                  detail={"from": row["slip_no"], **changes})
    return jsonify({"ok": True, "slipNo": slip_no})


# ---------------------------------------------------------------- assets

def _assets_filter_sql():
    """자산 목록 조건을 SQL로 — 엑셀 내보내기가 '보고 있는 그대로' 받게 공유한다.

    예전에는 내보내기가 상태만 반영해서, 검색·등급·분류로 걸러 놓고 받아도
    전체가 통째로 나왔다(2026-07-29 전수조사 확인).
    """
    clause, params = scope_clause("a")
    # ★전표번호를 함께 준다 — 목록에서 '이 자산이 어느 전표로 들어왔는지'가 바로 보여야
    #   전표를 찾으러 다른 화면을 뒤지지 않는다(대표 요청 2026-08-04).
    #   LEFT JOIN이어야 한다. 전표에 안 묶인 이관·수기 자산이 통째로 사라지면 안 된다.
    # ★취소·반품 여부도 함께 뽑는다. 안 그러면 취소한 전표가 목록에서 살아 있는 매입으로
    #   보이고, 대표가 그 전표 기준으로 매입가·거래처·미지급을 판단한다(2026-08-04 감사).
    sql = ("SELECT a.*, c.name AS category_name, b.slip_no AS slip_no, "
           "       b.cancelled_at AS slip_cancelled, b.returned_at AS slip_returned "
           "FROM assets a "
           "LEFT JOIN categories c ON c.id = a.category_id "
           "LEFT JOIN purchase_batches b ON b.id = a.batch_id WHERE 1=1" + clause)
    if request.args.get("status"):
        sql += " AND a.status = ?"
        params.append(request.args["status"])
    if request.args.get("categoryId"):
        sql += " AND a.category_id = ?"
        params.append(_int_or_400(request.args["categoryId"], "카테고리 ID"))
    if request.args.get("batchId"):
        sql += " AND a.batch_id = ?"
        params.append(_int_or_400(request.args["batchId"], "입고 ID"))
    if request.args.get("grade"):
        sql += " AND a.grade = ?"
        params.append(request.args["grade"])
    q = (request.args.get("q") or "").strip()
    if q:
        sql += (" AND (a.asset_no LIKE ? OR a.model LIKE ? OR a.serial LIKE ? OR a.maker LIKE ? "
                "OR a.notes LIKE ? OR a.cpu LIKE ? OR a.location LIKE ?)")
        params.extend(["%" + q + "%"] * 7)
    return sql, params


@bp.get("/assets")
def list_assets():
    require("purchase.view")
    sql, params = _assets_filter_sql()
    sql += " ORDER BY a.id DESC LIMIT 500"
    rows = get_db().execute(sql, params).fetchall()
    return jsonify([_asset_payload(r, {
        "categoryName": r["category_name"], "slipNo": r["slip_no"] or "",
        "slipCancelled": bool(r["slip_cancelled"]),
        "slipReturned": bool(r["slip_returned"])}) for r in rows])


# 제품코드를 넣어 봐야 소용없는 상태 — 다시 팔 물건이 아니다.
#   출고완료(이미 나감) · 폐기 · 매입취소 · 거래처반품
# 나머지(입고·정비중·수리·A/S·도색대기·불량·판매가능·주문매칭·회수중)는 언젠가 재고가 되므로
# 코드를 넣어 두는 게 맞다. 특히 수리·A/S는 고쳐서 파는 물건이라 빠지면 안 된다.
CODE_TARGET_EXCLUDE = ("shipped", "scrapped", "cancelled", "returned")


@bp.post("/purchase-batches/<int:bid>/sync-amount")
def sync_batch_amount(bid):
    """전표 매입금액을 그 전표에 달린 자산 매입가 합으로 맞춘다.

    ★자산을 담아 등록하면 화면이 합계를 자동으로 넣어 주지만, 나중에 자산 매입가를
      고치면 전표 총액이 그대로 남아 '금액 불일치' 경고가 뜬다(2026-08-04 대표).
      그때 이 버튼 하나로 맞춘다 — 취소·반품된 전표는 금액을 건드리지 않는다.
    """
    require("purchase.edit")
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM purchase_batches WHERE id=?", (bid,)).fetchone()
        if row is None:
            abort(404, description="전표를 찾을 수 없습니다.")
        if row["cancelled_at"]:
            abort(400, description="취소된 전표는 금액을 바꿀 수 없습니다.")
        total = conn.execute(
            "SELECT COALESCE(SUM(purchase_price),0) AS s FROM assets WHERE batch_id=?",
            (bid,)).fetchone()["s"]
        if total == (row["total_amount"] or 0):
            return jsonify({"changed": False, "totalAmount": total})
        conn.execute("UPDATE purchase_batches SET total_amount=?, updated_at=? WHERE id=?",
                     (total, config.now_iso(), bid))
        audit.log("batch_amount_synced", target=row["slip_no"] or str(bid),
                  detail={"from": row["total_amount"], "to": total})
    return jsonify({"changed": True, "totalAmount": total})


@bp.get("/product-codes")
def product_codes():
    """제품코드 자동완성 — 이미 쓰고 있는 코드를 그대로 다시 쓰게 한다.

    ★같은 물건에 코드를 조금씩 다르게 적으면(840 G3_i7-6_내장 / 840G3_i7-6_내장)
      셋팅 화면의 재고 대조가 통째로 어긋난다. 손으로 다시 타이핑하지 않게 하는 것이
      이 기능의 목적이다(대표 요청 2026-08-05: "모든 제품코드가 들어가는 곳에").

    코드마다 함께 준다 —
      · 재고 수(출고 가능 / 전체)         → 지금 팔 수 있는지 바로 보인다
      · 대표 상품명                       → 코드만 봐선 뭔지 모른다
      · 그 코드로 들어온 지난 주문의 옵션 → 수기 주문에서 옵션을 그대로 가져다 쓴다
    """
    require("purchase.view")
    q = (request.args.get("q") or "").strip()
    conn = get_db()
    clause, params = scope_clause("a")

    where = "TRIM(a.product_code) <> ''" + clause
    args = list(params)
    if q:
        where += " AND a.product_code LIKE ?"
        args.append(f"%{q}%")

    marks = ",".join("?" * len(AVAILABLE_STATUSES))
    rows = conn.execute(
        "SELECT a.product_code AS code, COUNT(*) AS total, "
        f"  SUM(CASE WHEN a.status IN ({marks}) AND a.received = 1 "
        "        AND a.tier <> '가재고' THEN 1 ELSE 0 END) AS shippable, "
        "  MAX(a.maker || ' ' || a.model) AS model "
        "FROM assets a WHERE " + where +
        " GROUP BY a.product_code ORDER BY shippable DESC, total DESC LIMIT 30",
        list(AVAILABLE_STATUSES) + args).fetchall()

    out = []
    for r in rows:
        code = r["code"]
        # 지난 주문에서 쓰던 옵션 — 몰에서 실제로 팔린 조합이라 가장 믿을 만하다
        opts = [x["option_name"] for x in conn.execute(
            "SELECT option_name, COUNT(*) n FROM orders "
            "WHERE TRIM(option_name) <> '' AND (product_code = ? OR product_name LIKE ?) "
            "GROUP BY option_name ORDER BY n DESC LIMIT 5", (code, f"%{code}%")).fetchall()]
        name = conn.execute(
            "SELECT product_name FROM orders WHERE product_code = ? "
            "AND TRIM(product_name) <> '' ORDER BY id DESC LIMIT 1", (code,)).fetchone()
        out.append({
            "code": code, "total": r["total"], "shippable": r["shippable"] or 0,
            "model": (r["model"] or "").strip(),
            "productName": (name["product_name"] if name else ""),
            "options": opts,
        })
    return jsonify({"codes": out})


@bp.get("/assets/uncoded")
def uncoded_assets():
    """제품코드가 아직 없는 자산을 '모델별'로 묶어서 준다 — 코드 입력 안내 화면용.

    ★같은 모델은 대개 같은 제품코드라, 모델로 묶어 두면 한 번에 넣을 수 있다
      (대표 2026-08-04). 이미 판매된 것과 다시 안 팔 것은 빼고 보여준다.
    """
    require("purchase.view")
    scope, params = scope_clause("a")
    marks = ",".join("?" * len(CODE_TARGET_EXCLUDE))
    rows = get_db().execute(
        "SELECT a.*, c.name AS category_name FROM assets a "
        "LEFT JOIN categories c ON c.id = a.category_id "
        f"WHERE TRIM(a.product_code) = '' AND a.status NOT IN ({marks})" + scope +
        " ORDER BY a.model, a.asset_no",
        list(CODE_TARGET_EXCLUDE) + params).fetchall()

    groups = {}
    for r in rows:
        key = (r["model"] or "").strip() or "(모델 없음)"
        g = groups.setdefault(key, {
            "model": key, "makers": set(), "count": 0,
            "byStatus": {}, "byGrade": {}, "byTier": {}, "assets": [],
        })
        g["count"] += 1
        if r["maker"]:
            g["makers"].add(r["maker"])
        for field, bucket in (("status", "byStatus"), ("grade", "byGrade"), ("tier", "byTier")):
            v = r[field] or ""
            label = ASSET_STATUSES.get(v, v) if field == "status" else (v or "미정")
            g[bucket][label] = g[bucket].get(label, 0) + 1
        g["assets"].append(_asset_payload(r, {"categoryName": r["category_name"]}))
    out = []
    for g in groups.values():
        g["maker"] = " / ".join(sorted(g.pop("makers"))) or ""
        out.append(g)
    # 대수가 많은 모델부터 — 한 번에 많이 처리되는 것을 위로
    out.sort(key=lambda x: (-x["count"], x["model"]))
    return jsonify({"groups": out, "total": len(rows), "models": len(out),
                    "excluded": [ASSET_STATUSES[s] for s in CODE_TARGET_EXCLUDE]})


@bp.get("/assets/to-convert")
def assets_to_convert():
    """양품이 아닌 자산(실재고·가재고)을 카테고리별로 묶어 준다 — 전환 안내 화면용.

    ★가재고는 출고가 막혀 있어 누군가 손을 대야 나간다(2026-08-04 대표).
      어디에 몇 대가 묶여 있는지 한눈에 보여 주고, 그 자리에서 수리 내역을 적고
      양품/실재고로 올릴 수 있게 한다. 이미 나갔거나 안 팔 것은 제외한다.
    """
    require("purchase.view")
    scope, params = scope_clause("a")
    marks = ",".join("?" * len(CODE_TARGET_EXCLUDE))
    rows = get_db().execute(
        "SELECT a.*, c.name AS category_name, b.slip_no, "
        "(SELECT COALESCE(SUM(r.cost),0) FROM asset_repairs r WHERE r.asset_id=a.id) AS repair_cost, "
        "(SELECT COUNT(*) FROM asset_repairs r WHERE r.asset_id=a.id) AS repair_count "
        "FROM assets a LEFT JOIN categories c ON c.id = a.category_id "
        "LEFT JOIN purchase_batches b ON b.id = a.batch_id "
        f"WHERE a.tier <> '양품' AND a.status NOT IN ({marks})" + scope +
        " ORDER BY a.tier DESC, c.name, a.model, a.asset_no",
        list(CODE_TARGET_EXCLUDE) + params).fetchall()

    groups = {}
    for r in rows:
        key = r["category_name"] or "(분류 없음)"
        g_ = groups.setdefault(key, {"category": key, "count": 0, "byTier": {},
                                     "repairCost": 0, "assets": []})
        g_["count"] += 1
        g_["byTier"][r["tier"]] = g_["byTier"].get(r["tier"], 0) + 1
        g_["repairCost"] += r["repair_cost"] or 0
        g_["assets"].append(_asset_payload(r, {
            "categoryName": r["category_name"], "slipNo": r["slip_no"] or "",
            "repairCost": r["repair_cost"] or 0, "repairCount": r["repair_count"] or 0}))
    out = sorted(groups.values(), key=lambda x: (-x["count"], x["category"]))
    by_tier = {}
    for r in rows:
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
    return jsonify({"groups": out, "total": len(rows), "byTier": by_tier,
                    "repairCost": sum(r["repair_cost"] or 0 for r in rows),
                    "tierHelp": TIER_HELP})


@bp.get("/assets/summary")
def assets_summary():
    require("purchase.view")
    clause, params = scope_clause("a")
    rows = get_db().execute(
        # ★아직 실물을 안 받은 가입고분은 재고 요약에서 뺀다 — 이걸 세면
        #   대시보드가 매입 화면보다 큰 숫자를 말한다(2026-07-31 감사).
        "SELECT a.category_id, c.name AS category_name, a.grade, a.status, COUNT(*) AS cnt "
        "FROM assets a LEFT JOIN categories c ON c.id = a.category_id "
        "WHERE a.received = 1" + clause +
        " GROUP BY a.category_id, a.grade, a.status", params
    ).fetchall()
    return jsonify([
        {"categoryId": r["category_id"], "categoryName": r["category_name"],
         "grade": r["grade"], "status": r["status"], "count": r["cnt"]}
        for r in rows
    ])


# 재고 현황을 '한눈에' 읽기 위한 3분류 — 상태 11개를 그대로 늘어놓으면 눈에 안 들어온다.
STOCK_BUCKETS = {
    "ready": ("ready",),                                       # 지금 팔 수 있는 것
    "working": ("in_stock", "refurbishing", "repair", "painting"),   # 손보면 팔 수 있는 것
    "held": ("reserved", "as", "returning", "defective"),      # 묶여 있거나 못 파는 것
}
STOCK_GONE = ("shipped", "scrapped")                           # 재고에서 빠진 것


def _bucket_of(status):
    for name, codes in STOCK_BUCKETS.items():
        if status in codes:
            return name
    return "gone"


@bp.get("/assets/stock-by-model")
def assets_stock_by_model():
    """제품(모델)별 재고 현황 — 지금 무엇이 몇 대 남았는지 한 장에 본다.

    같은 모델이어도 스펙이 다르면 실질적으로 다른 상품이라, 모델을 큰 줄로 묶고
    스펙 조합을 그 아래 줄로 편다(화면에서 펼쳐 보게).
    """
    require("purchase.view")
    clause, params = scope_clause("a")
    rows = get_db().execute(
        "SELECT a.category_id, c.name AS category_name, "
        "       COALESCE(NULLIF(TRIM(a.maker),''),'') AS maker, "
        "       COALESCE(NULLIF(TRIM(a.model),''),'') AS model, "
        "       a.cpu, a.ram, a.ssd, a.inch, a.grade, a.status, "
        "       COUNT(*) AS cnt, "
        "       SUM(a.purchase_price) AS buy_sum, SUM(a.sale_price) AS sale_sum, "
        # ★평균은 '값이 있는 것'만 나눠야 한다. 매입가 공란(0원, TMS 이관분)까지 분모에 넣으면
        #   평균 매입가가 실제보다 낮게 나와 마진이 부풀려 보인다(판매가는 원래 이렇게 세고 있었다).
        "       SUM(CASE WHEN a.purchase_price > 0 THEN 1 ELSE 0 END) AS buy_n, "
        "       SUM(CASE WHEN a.sale_price > 0 THEN 1 ELSE 0 END) AS sale_n "
        "FROM assets a LEFT JOIN categories c ON c.id = a.category_id WHERE 1=1"
        + clause + RECEIVED_SQL +
        " GROUP BY a.category_id, maker, model, a.cpu, a.ram, a.ssd, a.inch, a.grade, a.status",
        params).fetchall()

    products = {}
    for r in rows:
        name = " ".join(x for x in (r["maker"], r["model"]) if x) or "(모델 미입력)"
        p = products.setdefault(name, {
            "product": name, "maker": r["maker"], "model": r["model"],
            "categoryId": r["category_id"], "categoryName": r["category_name"] or "미지정",
            "ready": 0, "working": 0, "held": 0, "gone": 0, "stock": 0, "total": 0,
            "grades": {}, "buySum": 0, "buyCount": 0, "buyMissing": 0,
            "saleSum": 0, "saleCount": 0,
            "variants": {},
        })
        bucket = _bucket_of(r["status"])
        n = r["cnt"]
        p[bucket] += n
        p["total"] += n
        if bucket != "gone":
            p["stock"] += n
            p["grades"][r["grade"]] = p["grades"].get(r["grade"], 0) + n
            p["buySum"] += r["buy_sum"] or 0
            p["buyCount"] += r["buy_n"] or 0     # ★매입가가 실제로 있는 대수만
            p["buyMissing"] += n - (r["buy_n"] or 0)   # 매입가 미입력 — 화면에 알려 준다
            p["saleSum"] += r["sale_sum"] or 0
            p["saleCount"] += r["sale_n"] or 0

        spec = " / ".join(x for x in (r["cpu"], r["ram"], r["ssd"], r["inch"]) if x) or "스펙 미입력"
        v = p["variants"].setdefault(spec, {"spec": spec, "ready": 0, "working": 0,
                                            "held": 0, "gone": 0, "stock": 0})
        v[bucket] += n
        if bucket != "gone":
            v["stock"] += n

    out = []
    for p in products.values():
        p["variants"] = sorted(p["variants"].values(),
                               key=lambda v: (-v["stock"], -v["ready"], v["spec"]))
        p["avgBuy"] = round(p["buySum"] / p["buyCount"]) if p["buyCount"] else 0
        p["avgSale"] = round(p["saleSum"] / p["saleCount"]) if p["saleCount"] else 0
        p["stockValue"] = p["buySum"]          # 실제 매입가 합(평균×수량은 반올림이 쌓인다)
        p["gradeList"] = sorted(p["grades"].items(), key=lambda kv: -kv[1])
        # 팔린 적 있는데 남은 게 얼마 없으면 매입 신호
        p["lowStock"] = bool(p["gone"] and p["ready"] <= 2)
        for k in ("buySum", "buyCount", "saleSum", "saleCount", "grades"):
            p.pop(k)
        out.append(p)
    # 팔 수 있는 게 많은 순 → 그다음 보유 많은 순. 재고 0(품절)은 자연히 아래로 간다.
    out.sort(key=lambda p: (-p["ready"], -p["stock"], p["product"]))

    totals = {k: sum(p[k] for p in out) for k in ("ready", "working", "held", "stock", "total")}
    totals["products"] = len(out)
    totals["soldOut"] = sum(1 for p in out if p["stock"] == 0 and p["gone"])
    totals["lowStock"] = sum(1 for p in out if p["lowStock"] and p["stock"])
    totals["stockValue"] = sum(p["stockValue"] for p in out)
    return jsonify({"products": out, "totals": totals,
                    "buckets": {"ready": "판매가능", "working": "작업중", "held": "보류"}})


def _asset_input(body):
    """자산 등록/수정 공통 입력 파싱(스펙 필드 포함)."""
    out = {}
    for key in ("maker", "model", "serial", "notes") + SPEC_FIELDS:
        if key in body:
            out[key] = (body.get(key) or "").strip()
    return out


@bp.get("/assets/model-brief")
def asset_model_brief():
    """모델 하나의 '지금 상태' 요약 — 매입 입력 중에 재고를 대조하러 탭을 옮기지 않게 한다.

    대표 요청(2026-07-30): "재고내역과 자주 대조하는데 탭이 따로 있으니 사용성이 떨어진다."
    그래서 화면을 옮기는 대신, 모델명을 치는 순간 필요한 답을 옆에 띄운다.
      ① 지금 몇 대 있나(입고확인 끝난 것만)  ② 등급이 어떻게 섞여 있나
      ③ 최근에 어디서 얼마에 샀나(단가 감각)  ④ 아직 안 들어온 가입고분이 있나
    """
    require("purchase.view")
    model = (request.args.get("model") or "").strip()
    maker = (request.args.get("maker") or "").strip()
    if len(model) < 2:
        return jsonify({"model": model, "stock": 0, "byGrade": [], "recentBuys": [],
                        "pending": 0, "reason": "모델명을 2자 이상 입력하세요."})

    key = model.upper().replace(" ", "")
    clause, params = scope_clause("a")
    conn = get_db()
    marks = ",".join("?" * len(AVAILABLE_STATUSES))
    like = "%" + key + "%"

    # ① 재고 — 입고확인이 끝난 것만 센다(가입고분은 아직 물건이 없다)
    base = ("FROM assets a WHERE REPLACE(UPPER(a.model),' ','') LIKE ?" + clause)
    stock_rows = conn.execute(
        f"SELECT a.grade, COUNT(*) AS n {base} AND a.status IN ({marks}) AND a.received=1 "
        "GROUP BY a.grade ORDER BY n DESC",
        [like] + params + list(AVAILABLE_STATUSES)).fetchall()
    stock = sum(r["n"] for r in stock_rows)

    # ② 아직 안 들어온 가입고분 — '곧 들어올 물건'을 알아야 중복 매입을 안 한다
    pending = conn.execute(
        f"SELECT COUNT(*) AS n {base} AND a.received=0", [like] + params).fetchone()["n"]

    # ③ 최근 매입 — 어디서 얼마에 샀는지(단가 판단 근거)
    recent = conn.execute(
        "SELECT a.purchase_price, a.grade, a.created_at, b.purchase_date, "
        "       s.name AS supplier_name, b.slip_no "
        "FROM assets a LEFT JOIN purchase_batches b ON b.id = a.batch_id "
        "LEFT JOIN suppliers s ON s.id = b.supplier_id "
        "WHERE REPLACE(UPPER(a.model),' ','') LIKE ? AND a.purchase_price > 0" + clause +
        " ORDER BY COALESCE(b.purchase_date, a.created_at) DESC LIMIT 5",
        [like] + params).fetchall()

    prices = [r["purchase_price"] for r in recent]
    return jsonify({
        "model": model, "maker": maker,
        "stock": stock,
        "pending": pending,                      # 가입고 대기(아직 실물 없음)
        "byGrade": [{"grade": r["grade"], "count": r["n"]} for r in stock_rows],
        "avgBuy": round(sum(prices) / len(prices)) if prices else None,
        "recentBuys": [
            {"price": r["purchase_price"], "grade": r["grade"],
             "date": r["purchase_date"] or (r["created_at"] or "")[:10],
             "supplier": r["supplier_name"] or "", "slipNo": r["slip_no"] or ""}
            for r in recent
        ],
        "reason": "",
    })


def _add_assets(conn, body, batch_id, received, label=""):
    """자산 N대를 그 트랜잭션 안에서 만든다. create_assets와 전표 동시저장이 공유한다.

    ★한 전표에 자산을 따로따로 넣으면 중간에 실패했을 때 반쪽 상태가 된다.
      TMS는 [상세추가]로 쌓은 뒤 한 번에 저장하므로(전표당 평균 32대, 최대 366대)
      HMS도 같은 트랜잭션에서 처리해야 한다.
    label은 오류 메시지에 '3번째 줄'처럼 위치를 알려 주기 위한 것.
    """
    category_id = _int_or_400(body.get("categoryId"), "카테고리 ID")
    qty = _int_or_400(body.get("qty") or 1, "수량")
    if not 1 <= qty <= 100:
        abort(400, description=f"{label}수량은 1~100 사이여야 합니다.")
    manual_no = (body.get("assetNo") or "").strip()
    if manual_no and qty != 1:
        abort(400, description=f"{label}관리번호를 직접 지정할 때는 1개씩만 등록할 수 있습니다.")
    purchase_price = _money_or_400(body.get("purchasePrice") or 0, "매입가")
    sale_price = _money_or_400(body.get("salePrice") or 0, "판매가")
    grade = (body.get("grade") or "미정").strip()
    if grade not in GRADES:
        abort(400, description=f"{label}등급은 {', '.join(GRADES)} 중 하나여야 합니다.")
    tier = (body.get("tier") or "양품").strip()
    if tier not in TIERS:
        abort(400, description=f"{label}재고 구분은 {', '.join(TIERS)} 중 하나여야 합니다.")
    # 제품코드를 등록 시점에 넣을 수 있다 — 나중에 따로 채우러 다니지 않아도 된다(2026-08-04)
    product_code = (body.get("productCode") or "").strip()
    if len(product_code) > 60:
        abort(400, description=f"{label}제품코드는 60자 이내여야 합니다.")
    spec = _asset_input(body)

    if conn.execute("SELECT id FROM categories WHERE id=?", (category_id,)).fetchone() is None:
        abort(400, description=f"{label}존재하지 않는 카테고리입니다.")
    _check_asset_scope(conn, category_id)

    # ★시리얼 중복은 등록 시점에 막는다.
    #   같은 시리얼이 두 대면 어느 쪽이 진짜인지 알 수 없어 이력이 통째로 흔들린다.
    #   빈 시리얼은 정상(모니터·부속 등 시리얼이 없는 물건이 있다) — 값이 있을 때만 본다.
    serial = (spec.get("serial") or "").strip()
    if serial:
        dup = conn.execute(
            "SELECT asset_no FROM assets WHERE TRIM(serial)=? LIMIT 1", (serial,)).fetchone()
        if dup:
            abort(409, description=(
                f"{label}시리얼 {serial}은(는) 이미 자산 {dup['asset_no']}에 등록돼 있습니다."
                " 같은 기기를 두 번 등록하려는 게 아닌지 확인하세요."))
        if qty > 1:
            abort(400, description=f"{label}여러 대를 한 번에 등록할 때는 시리얼을 비워 두세요"
                                   "(개체마다 달라 나중에 각각 입력합니다).")

    created = []
    ts = config.now_iso()
    cols = ["asset_no", "batch_id", "category_id", "grade", "tier", "product_code",
            "purchase_price", "sale_price",
            "status", "received", "created_by", "created_at", "updated_at"]
    extra_cols = [k for k in spec]
    for _i in range(qty):
        asset_no = manual_no or next_asset_no(conn)
        if conn.execute("SELECT id FROM assets WHERE asset_no=?", (asset_no,)).fetchone():
            abort(409, description=f"{label}이미 존재하는 관리번호입니다: {asset_no}")
        vals = [asset_no, batch_id, category_id, grade, tier, product_code,
                purchase_price, sale_price,
                "in_stock", received, g.user["display_name"], ts, ts]
        for k in extra_cols:
            # 시리얼은 개체마다 다르므로 여러 대 등록 시에는 비운다
            vals.append("" if (k == "serial" and qty > 1) else spec[k])
        all_cols = cols + extra_cols
        cur = conn.execute(
            f"INSERT INTO assets({','.join(all_cols)}) VALUES({','.join('?' * len(all_cols))})", vals)
        aid = cur.lastrowid
        asset_event(conn, aid, "등록", {
            "관리번호": asset_no, "전표": batch_id,
            "수동지정": bool(manual_no), "매입가": purchase_price,
            "입고확인": bool(received),
        })
        created.append({"id": aid, "assetNo": asset_no})
    return created


@bp.post("/assets")
def create_assets():
    """자산 등록. qty>1이면 같은 스펙으로 연번 발번. assetNo 지정 시(TMS 이관) qty=1만."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    batch_id = body.get("batchId")
    with tx(write=True) as conn:
        received = 1
        if batch_id not in (None, ""):
            batch_id = _int_or_400(batch_id, "전표 ID")
            b = conn.execute("SELECT id, stage, cancelled_at FROM purchase_batches WHERE id=?",
                             (batch_id,)).fetchone()
            if b is None:
                abort(400, description="존재하지 않는 전표입니다.")
            # ★취소된 전표는 목록에서 숨겨져 있다. 거기에 자산을 붙이면
            #   재고에는 있는데 어느 매입 건인지 화면에서 되짚을 수 없는 유령 자산이 된다.
            if b["cancelled_at"]:
                abort(400, description="취소된 전표에는 자산을 추가할 수 없습니다."
                                       " 취소를 되돌린 뒤 추가하세요.")
            # ★가입고(V) = 아직 실물이 안 들어온 단계 → 재고로 세지 않는다
            if b["stage"] == "provisional":
                received = 0
        else:
            batch_id = None
        created = _add_assets(conn, body, batch_id, received)
        audit.log("assets_created",
                  target=created[0]["assetNo"] if len(created) == 1 else f"{len(created)}대",
                  detail={"count": len(created), "batchId": batch_id,
                          "assetNos": [c["assetNo"] for c in created][:20]})
    return jsonify(created), 201


@bp.get("/assets/<int:aid>")
def asset_detail(aid):
    require("purchase.view")
    conn = get_db()
    row = _get_asset_or_404(conn, aid)
    clause, params = scope_clause("a")
    if clause:
        ok = conn.execute(f"SELECT a.id FROM assets a WHERE a.id=? {clause}", [aid] + params).fetchone()
        if ok is None:
            abort(403, description="담당 카테고리가 아닌 자산입니다.")
    repairs = conn.execute(
        "SELECT * FROM asset_repairs WHERE asset_id=? ORDER BY repair_date DESC, id DESC", (aid,)
    ).fetchall()
    events = conn.execute(
        "SELECT * FROM asset_events WHERE asset_id=? ORDER BY id DESC LIMIT 100", (aid,)
    ).fetchall()
    orders = conn.execute(
        "SELECT o.id, o.channel, o.order_no, o.recipient, o.shipping_done, oa.matched_by, oa.matched_at "
        "FROM order_assets oa JOIN orders o ON o.id = oa.order_id WHERE oa.asset_id=? "
        "ORDER BY oa.matched_at DESC", (aid,)
    ).fetchall()
    batch = None
    if row["batch_id"]:
        # ★LEFT JOIN이어야 한다. 거래처가 아직 없는 가입고(V) 전표는 INNER JOIN이면
        #   행이 통째로 사라져, 전표에 속해 있는데도 '전표 없음'으로 보인다.
        #   전표번호(slip_no)도 함께 준다 — 없으면 어느 전표인지 식별할 수 없다.
        b = conn.execute(
            "SELECT b.*, s.name AS supplier_name FROM purchase_batches b "
            "LEFT JOIN suppliers s ON s.id = b.supplier_id WHERE b.id=?", (row["batch_id"],)
        ).fetchone()
        if b:
            batch = {"id": b["id"], "slipNo": b["slip_no"], "stage": b["stage"],
                     "stageLabel": STAGES.get(b["stage"], b["stage"]),
                     "supplierName": b["supplier_name"] or "",
                     "purchaseDate": b["purchase_date"], "totalAmount": b["total_amount"],
                     # ★취소·반품을 감추면 죽은 전표를 살아있는 매입으로 읽는다
                     "cancelledAt": b["cancelled_at"], "cancelReason": b["cancel_reason"],
                     "returnedAt": b["returned_at"], "returnReason": b["return_reason"]}
    repair_total = sum(r["cost"] for r in repairs)
    return jsonify(_asset_payload(row, {
        "batch": batch,
        "repairTotal": repair_total,
        "costTotal": row["purchase_price"] + repair_total,
        "repairs": [
            {"id": r["id"], "repairDate": r["repair_date"], "description": r["description"],
             "cost": r["cost"], "vat": r["vat"], "net": r["net"], "parts": r["parts"],
             "createdBy": r["created_by"]}
            for r in repairs
        ],
        "events": [
            {"ts": e["ts"], "action": e["action"], "actor": e["actor"],
             "detail": json.loads(e["detail"]) if e["detail"] else None}
            for e in events
        ],
        "orders": [
            {"orderId": o["id"], "channel": o["channel"], "orderNo": o["order_no"],
             "recipient": o["recipient"], "shipped": bool(o["shipping_done"]),
             "matchedBy": o["matched_by"], "matchedAt": o["matched_at"]}
            for o in orders
        ],
    }))


@bp.post("/assets/product-code")
def bulk_product_code():
    """선택한 자산들에 같은 제품코드를 한 번에 기입한다 — 전표 화면의 일괄 버튼.

    ★한 전표에 여러 모델이 섞여 들어온다(2026-08-04 대표). 그래서 '전표 전체'가 아니라
      화면에서 체크한 자산에만 넣는다 — 같은 모델만 골라 일괄, 나머지는 각개 수정.
      빈 코드를 보내면 지우기다. 지워지는 자산은 재고반영도 함께 꺼진다(유령 상태 방지).
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        abort(400, description="자산을 선택하세요.")
    if len(ids) > 500:
        abort(400, description="한 번에 500대까지 처리할 수 있습니다.")
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        abort(400, description="자산 번호가 올바르지 않습니다.")
    pc = (body.get("productCode") or "").strip()
    if len(pc) > 60:
        abort(400, description="제품코드는 60자 이내여야 합니다.")
    # ★등급·재고구분도 같이 넣을 수 있다(2026-08-04 대표) — 코드만 넣고 등급을 따로
    #   돌면 같은 목록을 두 번 훑게 된다. 보내지 않은 항목은 건드리지 않는다.
    grade = body.get("grade")
    if grade is not None:
        grade = str(grade).strip()
        if grade and grade not in GRADES:
            abort(400, description=f"등급은 {', '.join(GRADES)} 중 하나여야 합니다.")
    tier = body.get("tier")
    if tier is not None:
        tier = str(tier).strip()
        if tier and tier not in TIERS:
            abort(400, description=f"재고 구분은 {', '.join(TIERS)} 중 하나여야 합니다.")
    if not pc and not grade and not tier and "productCode" not in body:
        abort(400, description="바꿀 값을 입력하세요.")
    now = config.now_iso()
    done, skipped = [], []
    with tx(write=True) as conn:
        for aid in ids:
            row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            if row is None:
                skipped.append({"id": aid, "reason": "자산을 찾을 수 없습니다."})
                continue
            _check_asset_scope(conn, row["category_id"])
            touched = False
            if "productCode" in body and (row["product_code"] or "") != pc:
                conn.execute("UPDATE assets SET product_code=? WHERE id=?", (pc, aid))
                asset_event(conn, aid, "제품코드",
                            {"from": row["product_code"], "to": pc, "일괄": True})
                if not pc and row["stock_listed"]:
                    conn.execute("UPDATE assets SET stock_listed=0, stock_listed_at='', "
                                 "stock_listed_by='' WHERE id=?", (aid,))
                    asset_event(conn, aid, "재고해제", {"사유": "제품코드 삭제(일괄)"})
                touched = True
            if grade and grade != row["grade"]:
                conn.execute("UPDATE assets SET grade=? WHERE id=?", (grade, aid))
                asset_event(conn, aid, "등급", {"from": row["grade"], "to": grade, "일괄": True})
                touched = True
            if tier and tier != row["tier"]:
                conn.execute("UPDATE assets SET tier=? WHERE id=?", (tier, aid))
                asset_event(conn, aid, "재고구분", {"from": row["tier"], "to": tier, "일괄": True})
                touched = True
            if not touched:
                continue                       # 이미 같은 값 — 이력만 어지럽히지 않는다
            conn.execute("UPDATE assets SET updated_at=? WHERE id=?", (now, aid))
            done.append(row["asset_no"])
        audit.log("product_code_bulk", target=(pc or "(지움)"),
                  detail={"count": len(done), "skipped": len(skipped),
                          "등급": grade or "", "재고구분": tier or ""})
    return jsonify({"ok": len(done), "done": done, "skipped": skipped, "productCode": pc,
                    "grade": grade or "", "tier": tier or ""})


@bp.post("/assets/stock-listing")
def bulk_stock_listing():
    """여러 자산의 '재고반영'을 한 번에 켜거나 끈다 — 매입 화면의 일괄 버튼.

    ★수리를 다녀온 물건을 몰아서 올리는 실무 흐름(2026-08-04 대표).
      켤 때는 제품코드가 있는 것만 켜지고, 없는 것은 건너뛰어 사유와 함께 알려 준다.
      끄는 것은 제품코드와 무관하게 항상 된다(위험한 방향이 아니므로).
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        abort(400, description="자산을 선택하세요.")
    if len(ids) > 500:
        abort(400, description="한 번에 500대까지 처리할 수 있습니다.")
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        abort(400, description="자산 번호가 올바르지 않습니다.")
    on = bool(body.get("on"))
    now = config.now_iso()
    actor = g.user["display_name"]
    done, skipped = [], []
    with tx(write=True) as conn:
        for aid in ids:
            row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            if row is None:
                skipped.append({"id": aid, "reason": "자산을 찾을 수 없습니다."})
                continue
            _check_asset_scope(conn, row["category_id"])
            if on and not (row["product_code"] or "").strip():
                skipped.append({"id": aid, "assetNo": row["asset_no"],
                                "reason": "제품코드가 없어 재고반영할 수 없습니다."})
                continue
            if bool(row["stock_listed"]) == on:
                continue                       # 이미 원하는 상태 — 이력만 어지럽히지 않는다
            conn.execute(
                "UPDATE assets SET stock_listed=?, stock_listed_at=?, stock_listed_by=?, "
                "updated_at=? WHERE id=?",
                (1 if on else 0, now if on else "", actor if on else "", now, aid))
            asset_event(conn, aid, "재고반영" if on else "재고해제", {"일괄": True})
            done.append(row["asset_no"])
        audit.log("stock_listing", target=f"{'반영' if on else '해제'} {len(done)}대",
                  detail={"skipped": len(skipped)})
    return jsonify({"ok": len(done), "done": done, "skipped": skipped})


@bp.patch("/assets/<int:aid>")
def update_asset(aid):
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    with tx(write=True) as conn:
        row = _get_asset_or_404(conn, aid)
        _check_asset_scope(conn, row["category_id"])
        changes = {}

        if "assetNo" in body:  # TMS 이관 등 — 번호 변경은 이력에 남긴다
            new_no = (body.get("assetNo") or "").strip()
            if not new_no or len(new_no) > 30:
                abort(400, description="관리번호는 1~30자여야 합니다.")
            if new_no != row["asset_no"]:
                if conn.execute("SELECT id FROM assets WHERE asset_no=? AND id != ?", (new_no, aid)).fetchone():
                    abort(409, description="이미 존재하는 관리번호입니다.")
                conn.execute("UPDATE assets SET asset_no=? WHERE id=?", (new_no, aid))
                asset_event(conn, aid, "번호변경", {"from": row["asset_no"], "to": new_no})
                changes["관리번호"] = {"from": row["asset_no"], "to": new_no}

        if "batchId" in body:
            b = body.get("batchId")
            b = None if b in (None, "") else _int_or_400(b, "전표 ID")
            if b is not None and conn.execute(
                    "SELECT id FROM purchase_batches WHERE id=?", (b,)).fetchone() is None:
                abort(400, description="존재하지 않는 전표입니다.")
            if b != row["batch_id"]:
                conn.execute("UPDATE assets SET batch_id=? WHERE id=?", (b, aid))
                changes["전표"] = b

        if "categoryId" in body:
            cid = _int_or_400(body["categoryId"], "카테고리 ID")
            if conn.execute("SELECT id FROM categories WHERE id=?", (cid,)).fetchone() is None:
                abort(400, description="존재하지 않는 카테고리입니다.")
            _check_asset_scope(conn, cid)
            if cid != row["category_id"]:
                conn.execute("UPDATE assets SET category_id=? WHERE id=?", (cid, aid))
                changes["categoryId"] = cid

        for field in ("maker", "model", "serial", "notes") + SPEC_FIELDS:
            if field in body:
                v = (body.get(field) or "").strip()
                if v != row[field]:
                    # ★수정으로도 시리얼이 겹치면 안 된다 — 등록만 막으면 우회된다
                    if field == "serial" and v:
                        dup = conn.execute(
                            "SELECT asset_no FROM assets WHERE TRIM(serial)=? AND id<>? LIMIT 1",
                            (v, aid)).fetchone()
                        if dup:
                            abort(409, description=(
                                f"시리얼 {v}은(는) 이미 자산 {dup['asset_no']}에 등록돼 있습니다."))
                    conn.execute(f"UPDATE assets SET {field}=? WHERE id=?", (v, aid))
                    changes[field] = v

        if "grade" in body:
            grade = (body.get("grade") or "").strip()
            if grade not in GRADES:
                abort(400, description=f"등급은 {', '.join(GRADES)} 중 하나여야 합니다.")
            if grade != row["grade"]:
                conn.execute("UPDATE assets SET grade=? WHERE id=?", (grade, aid))
                changes["등급"] = {"from": row["grade"], "to": grade}

        if "tier" in body:
            tier = (body.get("tier") or "").strip()
            if tier not in TIERS:
                abort(400, description=f"재고 구분은 {', '.join(TIERS)} 중 하나여야 합니다.")
            if tier != row["tier"]:
                conn.execute("UPDATE assets SET tier=? WHERE id=?", (tier, aid))
                asset_event(conn, aid, "재고구분", {"from": row["tier"], "to": tier})
                changes["재고구분"] = {"from": row["tier"], "to": tier}

        if "productCode" in body:
            # 제품코드 = 쇼핑몰 재고의 축(예: 840 G3_i7-6_내장).
            # ★대표가 직접 기입한다 — 모델·CPU에서 자동으로 만들지 않는다(2026-08-04 지시).
            pc = (body.get("productCode") or "").strip()
            if len(pc) > 60:
                abort(400, description="제품코드는 60자 이내여야 합니다.")
            if pc != row["product_code"]:
                conn.execute("UPDATE assets SET product_code=? WHERE id=?", (pc, aid))
                asset_event(conn, aid, "제품코드", {"from": row["product_code"], "to": pc})
                changes["제품코드"] = {"from": row["product_code"], "to": pc}
                # ★코드를 지우면 재고반영도 함께 끈다(2026-08-04 검증 결함 ②).
                #   켤 때는 '코드 필수'로 막는데 지우기로 우회되면, 화면엔 '반영 켜짐'인데
                #   집계에선 조용히 빠지는 유령 상태가 된다 — 직원은 몰에 올라간 줄 안다.
                if not pc and row["stock_listed"]:
                    conn.execute(
                        "UPDATE assets SET stock_listed=0, stock_listed_at='', "
                        "stock_listed_by='' WHERE id=?", (aid,))
                    asset_event(conn, aid, "재고해제", {"사유": "제품코드 삭제"})
                    changes["재고반영"] = False

        if "stockListed" in body:
            # 재고반영 — 사람이 체크했을 때만 몰 재고에 세어질 자격이 생긴다.
            # ★제품코드를 넣었다고 자동으로 켜지지 않는다. 수리를 다녀와서 올리는
            #   경우가 있어 반영 시점은 사람이 정한다(2026-08-04 지시).
            want = bool(body.get("stockListed"))
            # ★코드가 이 요청에서 지워졌으면 그 '새 값'으로 판정해야 한다.
            #   옛 값으로 판정하면 '코드 지우기 + 켜기'를 한 번에 보내 가드를 우회한다.
            effective_code = ((body.get("productCode") or "") if "productCode" in body
                              else row["product_code"]) or ""
            if want and not effective_code.strip():
                abort(400, description="재고반영은 제품코드가 있어야 켤 수 있습니다.")
            if "재고반영" in changes:
                pass                                   # 코드 삭제로 이미 꺼졌다 — 중복 기록 방지
            elif want != bool(row["stock_listed"]):
                conn.execute(
                    "UPDATE assets SET stock_listed=?, stock_listed_at=?, stock_listed_by=? WHERE id=?",
                    (1 if want else 0, config.now_iso() if want else "",
                     g.user["display_name"] if want else "", aid))
                asset_event(conn, aid, "재고반영" if want else "재고해제", {})
                changes["재고반영"] = want

        for key, col, label in (("purchasePrice", "purchase_price", "매입가"),
                                ("salePrice", "sale_price", "판매가")):
            if key in body:
                price = _money_or_400(body[key], label)
                if price != row[col]:
                    conn.execute(f"UPDATE assets SET {col}=? WHERE id=?", (price, aid))
                    changes[label] = price

        if "status" in body:
            status = (body.get("status") or "").strip()
            if status not in ASSET_STATUSES:
                abort(400, description="알 수 없는 상태입니다.")
            if status != row["status"]:
                # ★'회수중'을 빼먹으면 회수 입고가 스킵돼 한 대가 두 고객에게 나간다
                #   (orders/recall.py 주석이 경고하는 바로 그 경로가 매입 화면으로 열려 있었다)
                if status not in MANUAL_STATUSES or row["status"] in ("reserved", "shipped", "returning"):
                    abort(400, description="주문매칭/출고 상태는 주문 흐름에서만 변경됩니다. 먼저 매칭을 해제하세요.")
                conn.execute("UPDATE assets SET status=? WHERE id=?", (status, aid))
                asset_event(conn, aid, "상태변경", {
                    "from": ASSET_STATUSES[row["status"]], "to": ASSET_STATUSES[status]})
                changes["status"] = {"from": row["status"], "to": status}

        if not changes:
            abort(400, description="변경할 항목이 없습니다.")
        conn.execute("UPDATE assets SET updated_at=? WHERE id=?", (config.now_iso(), aid))
        if set(changes) - {"관리번호", "status"}:  # 번호/상태는 전용 이벤트로 이미 기록됨
            asset_event(conn, aid, "수정",
                        {k: v for k, v in changes.items() if k not in ("관리번호", "status")})
        audit.log("asset_updated", target=row["asset_no"], detail=changes)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- repairs

def split_vat(total):
    """총액(부가세 포함) → (공급가, 부가세). 부가세 = 총액 × 10/110, 원 단위 반올림.

    ★대표 요청(2026-08-04): 총 금액만 넣으면 원 금액과 부가세가 자동으로 나와야 한다.
      세금계산서와 맞추려면 공급가 + 부가세 = 총액이 정확히 떨어져야 하므로
      부가세를 반올림한 뒤 공급가는 '총액 - 부가세'로 되돌려 계산한다.
    """
    total = int(total or 0)
    vat = round(total * 10 / 110)
    return total - vat, vat


@bp.post("/assets/<int:aid>/repairs")
def add_repair(aid):
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    desc = (body.get("description") or "").strip()
    parts = (body.get("parts") or "").strip()
    if not desc and not parts:
        abort(400, description="수리 내용이나 교체 부품을 입력하세요.")
    if not desc:
        desc = f"부품 교체: {parts}"
    if len(parts) > 200:
        abort(400, description="교체 부품은 200자 이내여야 합니다.")
    cost = _money_or_400(body.get("cost") or 0, "수리비용")
    net, vat = split_vat(cost)
    repair_date = (body.get("repairDate") or "").strip() or config.now().strftime("%Y-%m-%d")
    with tx(write=True) as conn:
        row = _get_asset_or_404(conn, aid)
        _check_asset_scope(conn, row["category_id"])
        cur = conn.execute(
            "INSERT INTO asset_repairs(asset_id, repair_date, description, cost, vat, net, parts, "
            "created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (aid, repair_date, desc, cost, vat, net, parts,
             g.user["display_name"], config.now_iso()),
        )
        asset_event(conn, aid, "수리", {"description": desc, "cost": cost,
                                       "공급가": net, "부가세": vat,
                                       "교체부품": parts, "date": repair_date})
        # 수리했으면 재고 구분을 함께 바꿀 수 있다 — 고쳐 놓고 가재고로 남으면 출고가 막힌다
        new_tier = (body.get("tier") or "").strip()
        if new_tier:
            if new_tier not in TIERS:
                abort(400, description=f"재고 구분은 {', '.join(TIERS)} 중 하나여야 합니다.")
            if new_tier != row["tier"]:
                conn.execute("UPDATE assets SET tier=? WHERE id=?", (new_tier, aid))
                asset_event(conn, aid, "재고구분",
                            {"from": row["tier"], "to": new_tier, "사유": "수리 완료"})
        conn.execute("UPDATE assets SET updated_at=? WHERE id=?", (config.now_iso(), aid))
        audit.log("repair_added", target=row["asset_no"],
                  detail={"cost": cost, "vat": vat, "description": desc, "parts": parts})
        rid = cur.lastrowid
    return jsonify({"id": rid, "cost": cost, "net": net, "vat": vat}), 201


@bp.get("/assets/duplicates")
def asset_duplicates():
    """같은 실물이 두 번 등록된 것 찾기 — 시리얼이 같으면 거의 확실한 중복이다.

    재고 수와 자산가치가 부풀려지는 원인이라 매입 화면에서 바로 보이게 한다.
    """
    require("purchase.view")
    clause, params = scope_clause("a")
    rows = get_db().execute(
        "SELECT a.serial, a.maker, a.model, COUNT(*) AS cnt, "
        "       GROUP_CONCAT(a.asset_no) AS nos, GROUP_CONCAT(a.id) AS ids, "
        "       GROUP_CONCAT(a.status) AS statuses "
        "FROM assets a WHERE TRIM(a.serial) != ''" + clause +
        " GROUP BY UPPER(TRIM(a.serial)) HAVING cnt > 1 ORDER BY cnt DESC LIMIT 200",
        params).fetchall()
    out = []
    for r in rows:
        ids = [int(x) for x in (r["ids"] or "").split(",") if x]
        nos = (r["nos"] or "").split(",")
        sts = (r["statuses"] or "").split(",")
        out.append({
            "serial": r["serial"], "maker": r["maker"], "model": r["model"], "count": r["cnt"],
            "assets": [{"id": i, "assetNo": n, "status": s,
                        "statusLabel": ASSET_STATUSES.get(s, s)}
                       for i, n, s in zip(ids, nos, sts)],
        })
    return jsonify({"groups": out, "count": len(out)})


@bp.post("/assets/bulk")
def bulk_assets():
    """여러 자산의 상태·위치·등급을 한 번에 — 단건 규칙(주문매칭/출고는 손대지 않음)을 그대로 지킨다."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        abort(400, description="처리할 자산을 선택하세요.")
    if len(ids) > 500:
        abort(400, description="한 번에 500대까지 처리할 수 있습니다.")
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        abort(400, description="자산 번호가 올바르지 않습니다.")

    status = (body.get("status") or "").strip()
    location = body.get("location")
    grade = (body.get("grade") or "").strip()
    if status and status not in MANUAL_STATUSES:
        abort(400, description="여기서 지정할 수 없는 상태입니다(주문매칭/출고는 주문 흐름에서만).")
    if grade and grade not in GRADES:
        abort(400, description="알 수 없는 등급입니다.")
    if not status and location is None and not grade:
        abort(400, description="바꿀 항목을 하나 이상 지정하세요.")

    now = config.now_iso()
    ok, failed = [], []
    with tx(write=True) as conn:
        for aid in ids:
            row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            if row is None:
                failed.append({"id": aid, "reason": "자산을 찾을 수 없습니다."})
                continue
            _check_asset_scope(conn, row["category_id"])
            changes = {}
            if status and status != row["status"]:
                if row["status"] in ("reserved", "shipped", "returning"):
                    failed.append({"id": aid, "label": row["asset_no"],
                                   "reason": "주문에 매칭/출고된 자산입니다. 매칭을 먼저 해제하세요."})
                    continue
                conn.execute("UPDATE assets SET status=? WHERE id=?", (status, aid))
                asset_event(conn, aid, "상태변경", {
                    "from": ASSET_STATUSES.get(row["status"], row["status"]),
                    "to": ASSET_STATUSES[status], "일괄": True})
                changes["status"] = status
            if location is not None and str(location).strip() != row["location"]:
                conn.execute("UPDATE assets SET location=? WHERE id=?", (str(location).strip(), aid))
                changes["위치"] = str(location).strip()
            if grade and grade != row["grade"]:
                conn.execute("UPDATE assets SET grade=? WHERE id=?", (grade, aid))
                changes["등급"] = grade
            if changes:
                conn.execute("UPDATE assets SET updated_at=? WHERE id=?", (now, aid))
                if set(changes) - {"status"}:
                    asset_event(conn, aid, "수정", {k: v for k, v in changes.items() if k != "status"})
            ok.append(aid)
        audit.log("assets_bulk", target=f"{len(ok)}대",
                  detail={"status": status or None, "location": location, "grade": grade or None,
                          "ok": len(ok), "failed": len(failed)})
    return jsonify({"ok": len(ok), "done": ok, "failed": failed})


@bp.get("/assets/export")
def export_assets():
    """현재 조건의 자산 목록을 엑셀로 내려받기(이관 양식과 동일 컬럼)."""
    require("purchase.view")
    from flask import Response

    from ..importers import write_xlsx
    sql, params = _assets_filter_sql()          # 화면 목록과 같은 조건
    # ★예전에는 asset_no 오름차순 LIMIT 5000이라, 15,107대 중 '가장 오래된' 5,000대만
    #   나가고 최근 1년치가 통째로 빠졌다. 게다가 잘렸다는 표시가 없어 그게 전부인 줄 알았다
    #   (2026-08-04 감사). 최신순으로 바꾸고, 잘리면 파일명과 첫 줄로 알려 준다.
    total = get_db().execute(
        "SELECT COUNT(*) FROM (" + sql + ")", params).fetchone()[0]
    rows = get_db().execute(sql + " ORDER BY a.id DESC LIMIT 5000", params).fetchall()
    cut = total > len(rows)
    headers = ["관리번호", "매입전표", "대분류", "브랜드", "모델명", "시리얼번호", "매입가", "판매가", "등급",
               "보관위치", "CPU", "그래픽", "RAM", "SSD", "인치", "배터리효율", "충전기유무",
               "재고상태", "매입상세비고"]
    data = [[
        r["asset_no"], r["slip_no"] or "", r["category_name"] or "", r["maker"], r["model"], r["serial"],
        r["purchase_price"], r["sale_price"], r["grade"], r["location"], r["cpu"], r["gpu"],
        r["ram"], r["ssd"], r["inch"], r["battery"], r["charger"],
        ASSET_STATUSES.get(r["status"], r["status"]), r["notes"],
    ] for r in rows]
    if cut:
        # 파일을 열자마자 보이도록 맨 위에 한 줄 박는다 — 파일명만으로는 놓친다
        data.insert(0, [f"※ 전체 {total:,}대 중 최근 {len(rows):,}대만 담겼습니다."
                        " 조건(상태·분류·검색)을 좁혀 나눠 받으세요."] + [""] * (len(headers) - 1))
    name = (f"hms-assets-{config.today_str()}"
            + (f"-일부{len(rows)}of{total}" if cut else "") + ".xlsx")
    content = write_xlsx(headers, data)
    return Response(content, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@bp.delete("/assets/<int:aid>/repairs/<int:rid>")
def delete_repair(aid, rid):
    require("purchase.edit")
    with tx(write=True) as conn:
        row = _get_asset_or_404(conn, aid)
        _check_asset_scope(conn, row["category_id"])
        rep = conn.execute("SELECT * FROM asset_repairs WHERE id=? AND asset_id=?", (rid, aid)).fetchone()
        if rep is None:
            abort(404, description="수리 기록을 찾을 수 없습니다.")
        conn.execute("DELETE FROM asset_repairs WHERE id=?", (rid,))
        asset_event(conn, aid, "수리삭제", {"description": rep["description"], "cost": rep["cost"]})
        audit.log("repair_deleted", target=row["asset_no"],
                  detail={"cost": rep["cost"], "description": rep["description"]})
    return jsonify({"ok": True})


# 서브모듈 라우트 등록 (bp 정의 이후에 import해야 함)
from . import autosync, migration, sales, spec_options  # noqa: E402,F401
