"""주문 관리 — 보드/수기등록/준비·QC 상태머신/자산 매칭.

상태머신은 검증된 order-workflow 원본 설계를 계승한다:
- 단계별 「불리언+담당자+시각」: 준비중(preparing) → 제작완료(production) → SW검수(inspection) → 출고확인(shipping)
- 담당자 클레임: 체크한 사람 = 담당자. 타인 점유 단계는 체크/해제 모두 409 (관리자는 예외)
- 409 응답에 최신 주문을 동봉해 프론트가 화면을 자동 복구: {"error": ..., "order": {...}}
- 관리번호 입력은 자산번호 FK 매칭으로 승격 (자산 상태 검증 + 자산 이력 기록)
"""
import json

from flask import Blueprint, abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import ORDER_READ_PERMS, require, require_any
from ..db import get_db, tx
from ..prep import options_for_order as prep_options_for_order
from ..prep import options_for_orders as prep_options_for_orders
from ..prep import unchecked_for_order as prep_unchecked_for_order
from ..purchase import ASSET_STATUSES, AVAILABLE_STATUSES, asset_event
from ..settings import _int_or_400, _money_or_400

bp = Blueprint("orders", __name__, url_prefix="/api")

# 수기 주문 채널(엑셀/API 수집 채널과 구분)
MANUAL_CHANNELS = ("수기", "전화", "방문")

# 쇼핑몰 관점 주문 상태 — 내부 QC 단계(준비/제작/검수)와는 다른 축이다.
# 셋팅 탭은 '작업이 어디까지 됐나', 주문관리 탭은 '고객 주문이 어느 단계인가'를 본다.
MALL_STATUSES = [
    {"code": "unpaid", "label": "입금대기"},
    {"code": "preparing", "label": "준비중"},      # 결제완료 ~ 출고 전
    {"code": "shipping", "label": "배송중"},        # 송장 발급 또는 출고 확인
    {"code": "delivered", "label": "배송완료"},     # 배송완료/구매확정
    {"code": "cancelled", "label": "취소"},
]
MALL_STATUS_LABELS = {s["code"]: s["label"] for s in MALL_STATUSES}


def mall_status_of(row, delivered=False):
    """주문 한 건의 쇼핑몰 관점 상태를 계산한다.

    delivered: 이 주문에 '배송완료된 출고 송장'이 있는지(호출부가 조회해 넘긴다)
    """
    if row["cancelled_at"]:
        return "cancelled"
    if delivered or row["delivered_at"] or row["archived_at"]:
        # 보관(archived)은 기존 프로그램에서 출고 후 마무리된 건이라 배송완료로 본다
        return "delivered"
    if row["shipping_done"] or (row["tracking_no"] or "").strip():
        return "shipping"
    if (row["pay_status"] or "paid") == "unpaid":
        return "unpaid"
    return "preparing"


# SQL에서도 같은 규칙을 쓰기 위한 CASE 식(집계용). delivered 송장 여부를 서브쿼리로 판단.
_MALL_STATUS_SQL = """
CASE
  WHEN o.cancelled_at != '' THEN 'cancelled'
  WHEN o.delivered_at != '' OR o.archived_at != '' OR EXISTS (
       SELECT 1 FROM waybills w WHERE w.order_id = o.id
       AND w.type='forward' AND w.status='delivered') THEN 'delivered'
  WHEN o.shipping_done = 1 OR o.tracking_no != '' THEN 'shipping'
  WHEN o.pay_status = 'unpaid' THEN 'unpaid'
  ELSE 'preparing'
END
"""

_DETAIL_FIELDS = {
    # bodyKey: (컬럼, 라벨)
    "recipient": ("recipient", "수취인"),
    "phone": ("phone", "연락처"),
    "postalCode": ("postal_code", "우편번호"),
    "address": ("address", "주소"),
    "deliveryMessage": ("delivery_message", "배송메시지"),
    "memo": ("memo", "메모"),
    "productName": ("product_name", "상품명"),
    "optionName": ("option_name", "옵션"),
    "productCode": ("product_code", "상품코드"),
    "receiveMethod": ("receive_method", "수령방식"),
    "orderedAt": ("ordered_at", "주문일시"),
    "orderNo": ("order_no", "주문번호"),
    "channel": ("channel", "채널"),
}


# ---------------------------------------------------------------- payload

def can_see_pii():
    """고객 연락처·주소를 볼 수 있는가.

    셋팅(QC) 담당자는 '무엇을 만들어 어느 자산으로 내보내나'만 알면 되고
    연락처·주소는 필요 없다. 배송 담당자는 송장을 찍어야 하므로 필요하다.
    권한을 새로 만들지 않고, 필요 없는 사람에게만 가린다(대표 방침: 메뉴 단위 권한).
    """
    user = g.get("user")
    if not user:
        return False
    if user["is_admin"]:
        return True
    perms = g.get("perms") or set()
    return bool({"orders.view", "orders.edit", "shipping.view", "orders.ship"} & set(perms))


def _mask_tail(value, keep=4):
    v = (value or "").strip()
    if len(v) <= keep:
        return "***" if v else ""
    return "*" * (len(v) - keep) + v[-keep:]


def order_payload(conn, row, prep_map=None):
    """prep_map: 목록에서 여러 건의 '챙길 옵션'을 한 번에 계산해 넘긴다(N+1 방지)."""
    delivered = conn.execute(
        "SELECT 1 FROM waybills WHERE order_id=? AND type='forward' AND status='delivered' LIMIT 1",
        (row["id"],)).fetchone() is not None
    ms = mall_status_of(row, delivered)
    pii = can_see_pii()
    assets = conn.execute(
        "SELECT a.id, a.asset_no, a.model, a.maker, a.grade, a.status, oa.matched_by, oa.matched_at "
        "FROM order_assets oa JOIN assets a ON a.id = oa.asset_id "
        "WHERE oa.order_id=? ORDER BY a.asset_no", (row["id"],)
    ).fetchall()
    # ★종류를 함께 내려준다 — 회수 송장을 출고 송장으로 착각하면 인쇄·재발급·취소가 모두 막힌다
    waybills = conn.execute(
        "SELECT wid, invoice_no, status, type, created_at FROM waybills WHERE order_id=? "
        "ORDER BY created_at DESC", (row["id"],)
    ).fetchall()
    return {
        "id": row["id"],
        "channel": row["channel"],
        "orderNumber": row["order_no"],
        "orderedAt": row["ordered_at"],
        "productName": row["product_name"],
        "optionName": row["option_name"],
        "productCode": row["product_code"],
        # 택배가 아니면 송장이 필요 없다 — 셋팅·배송 화면이 이 값을 보고 판단한다
        "receiveMethod": row["receive_method"],
        "quantity": row["quantity"],
        "amount": row["amount"],
        "recipient": row["recipient"],       # 누구 물건인지는 작업자도 알아야 한다
        # 연락처·주소는 배송에 필요한 사람만 본다(셋팅 전용 계정에는 가려서 내려간다)
        "phone": row["phone"] if pii else _mask_tail(row["phone"]),
        "postalCode": row["postal_code"] if pii else "",
        "address": row["address"] if pii else "",
        "deliveryMessage": row["delivery_message"] if pii else "",
        "memo": row["memo"],                 # 작업 지시라 셋팅·배송 담당자가 봐야 한다
        "piiMasked": not pii,
        "feeAmount": row["fee_amount"],
        "feeRate": row["fee_rate"],
        "shippingCost": row["shipping_cost"],
        "refundAmount": row["refund_amount"],
        "refundReason": row["refund_reason"],
        # 실제로 손에 남는 돈 — 화면·리포트가 같은 정의를 쓰게 서버가 계산해 준다
        "netAmount": (row["amount"] or 0) - (row["fee_amount"] or 0) - (row["refund_amount"] or 0),
        "categoryId": row["category_id"],
        "isReview": bool(row["is_review"]),
        "reviewNote": row["review_note"],
        "courier": row["courier"],
        "trackingNumber": row["tracking_no"],
        # 쇼핑몰 관점 상태(입금대기/준비중/배송중/배송완료/취소)
        "mallStatus": ms,
        "mallStatusLabel": MALL_STATUS_LABELS.get(ms, ms),
        "payStatus": row["pay_status"],
        "deliveredAt": row["delivered_at"],
        "preparing": bool(row["preparing"]),
        "preparingBy": row["preparing_by"],
        "preparingAt": row["preparing_at"],
        "productionDone": bool(row["production_done"]),
        "productionBy": row["production_by"],
        "productionAt": row["production_at"],
        "softwareInspectionDone": bool(row["inspection_done"]),
        "softwareInspectionBy": row["inspection_by"],
        "softwareInspectionAt": row["inspection_at"],
        "shippingDone": bool(row["shipping_done"]),
        "shippingBy": row["shipping_by"],
        "shippingAt": row["shipping_at"],
        "cancelledAt": row["cancelled_at"],
        "cancelledBy": row["cancelled_by"],
        "cancelReason": row["cancel_reason"],
        "archivedAt": row["archived_at"],
        # 왜 보관됐는지 — '중복이라 내린 것'과 '출고돼서 내린 것'은 뜻이 다르다
        "archiveReason": row["archive_reason"],
        "duplicateOf": row["duplicate_of"],
        "pendingShippingUpdate": json.loads(row["pending_update"]) if row["pending_update"] else None,
        "createdBy": row["created_by"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "assets": [
            {"assetId": a["id"], "assetNo": a["asset_no"], "model": a["model"], "maker": a["maker"],
             "grade": a["grade"], "status": a["status"],
             "statusLabel": ASSET_STATUSES.get(a["status"], a["status"]),
             "matchedBy": a["matched_by"], "matchedAt": a["matched_at"]}
            for a in assets
        ],
        "waybills": [
            {"wid": w["wid"], "invoiceNo": w["invoice_no"], "status": w["status"],
             "type": w["type"], "createdAt": w["created_at"]}
            for w in waybills
        ],
        # 몰이 실어 주지 않는 '우리가 챙기는 옵션' — 셋팅·QC가 체크하고 송장에도 찍힌다
        "prepOptions": (prep_map.get(row["id"], []) if prep_map is not None
                        else prep_options_for_order(conn, row)),
    }


def _get_order_or_404(conn, oid):
    """주문 한 건을 꺼내는 유일한 통로 — 담당 분류(카테고리) 검사도 여기서 한다.

    상세 조회·수정·송장 발급·회수가 모두 이 함수를 지나므로, 여기 한 곳에 두면
    담당 밖 주문을 만지는 경로가 남지 않는다.
    """
    scope, params = _order_scope_clause()
    row = conn.execute(
        "SELECT o.* FROM orders o WHERE o.id=?" + scope, [oid] + params).fetchone()
    if row is None:
        abort(404, description="주문을 찾을 수 없습니다.")
    return row


def _conflict(conn, oid, msg):
    """409 + 최신 주문 동봉 — 프론트 자동 복구 계약(원본 설계 계승)."""
    row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    resp = jsonify({"error": msg, "order": order_payload(conn, row) if row else None})
    resp.status_code = 409
    return resp


def _touch(conn, oid):
    conn.execute("UPDATE orders SET updated_at=? WHERE id=?", (config.now_iso(), oid))


def _order_scope_clause():
    """카테고리 스코프 — 카테고리 미지정 주문(수집 직후)은 전원 조회 가능."""
    user = g.user
    if user["is_admin"] or user["all_categories"]:
        return "", []
    ids = [r["category_id"] for r in get_db().execute(
        "SELECT category_id FROM user_categories WHERE user_id=?", (user["id"],)).fetchall()]
    if not ids:
        return " AND o.category_id IS NULL", []
    ph = ",".join("?" * len(ids))
    return f" AND (o.category_id IS NULL OR o.category_id IN ({ph}))", ids


def settlement_settings(conn):
    """정산 설정 — 몰별 판매수수료율(%)과 기본 출고 택배비.

    {"rates": {"쿠팡": 10.8, "고도몰": 3.4, "_default": 0}, "shippingCost": 3000}
    비어 있으면 0 — 설정하기 전까지는 아무것도 임의로 깎지 않는다.
    """
    row = conn.execute("SELECT value FROM settings WHERE key='settlement'").fetchone()
    data = {}
    if row:
        try:
            data = json.loads(row["value"]) or {}
        except ValueError:
            data = {}
    # ★택배비가 음수면 원가가 마이너스가 되어 마진이 부풀려지고, 글자가 섞이면
    #   조용히 0이 되어 택배비가 통째로 사라진다. 어느 쪽이든 화면에서 원인을 못 찾는다.
    try:
        ship = int(float(str(data.get("shippingCost") or 0).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        ship = 0
    return {"rates": data.get("rates") or {}, "shippingCost": max(0, ship)}


def fee_for(cfg, channel, amount):
    """이 주문에 붙는 판매수수료 — (금액, 적용요율)."""
    rates = cfg["rates"]
    rate = rates.get(channel)
    if rate is None:
        rate = rates.get("_default", 0)
    try:
        rate = float(rate or 0)
    except (TypeError, ValueError):
        rate = 0.0
    return (round(int(amount or 0) * rate / 100), rate) if rate else (0, 0.0)


def apply_settlement_defaults(conn, oid, row):
    """출고 확인 시 수수료·택배비를 자동으로 채운다(사람이 확정한 값은 건드리지 않는다)."""
    cfg = settlement_settings(conn)
    sets, params = [], []
    if not row["fee_manual"]:
        fee, rate = fee_for(cfg, row["channel"], row["amount"])
        if fee or rate:
            sets += ["fee_amount=?", "fee_rate=?"]
            params += [fee, rate]
    if not row["shipping_cost"] and cfg["shippingCost"]:
        sets.append("shipping_cost=?")
        params.append(cfg["shippingCost"])
    if sets:
        conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=?", params + [oid])


def recalc_fee_for_amount(conn, oid, row):
    """금액이 바뀌면 자동 산정된 수수료도 다시 계산한다.

    몰 정산액에 맞춰 금액을 고쳤는데 수수료가 옛 금액 기준으로 굳어 있으면
    실입금·마진이 조용히 틀어진다. 사람이 확정한 수수료는 그대로 둔다.
    """
    if row["fee_manual"] or not row["fee_rate"]:
        return None
    new_row = conn.execute("SELECT channel, amount FROM orders WHERE id=?", (oid,)).fetchone()
    fee = round((new_row["amount"] or 0) * row["fee_rate"] / 100)
    if fee != row["fee_amount"]:
        conn.execute("UPDATE orders SET fee_amount=? WHERE id=?", (fee, oid))
        return fee
    return None


def _apply_settlement(conn, oid, row, body):
    """정산 값(수수료·택배비·환불)을 반영하고 바뀐 것만 돌려준다.

    상세 저장과 정산 저장이 같은 규칙을 쓰도록 한 곳에 모았다.
    화면의 [저장]은 이 둘을 한 번의 요청으로 함께 처리한다 — 요청을 두 번 보내면
    앞의 저장이 updated_at을 바꿔 뒤의 저장이 '다른 사용자가 먼저 수정했습니다'로
    막히고, 정산만 저장된 채 고친 내용이 사라진다(2026-07-29 확인).

    ★row는 트랜잭션 시작 시점 값이라 금액 검사에는 쓰지 않는다. 상세 저장에서
      금액을 함께 바꿨을 수 있으므로 환불액 상한은 '지금 값'으로 다시 읽어 검사한다.
    """
    now = conn.execute("SELECT amount, fee_amount, shipping_cost, refund_amount, refund_reason "
                       "FROM orders WHERE id=?", (oid,)).fetchone()
    changes = {}
    fee_changed = False
    for key, col, label in (("feeAmount", "fee_amount", "판매수수료"),
                            ("shippingCost", "shipping_cost", "출고 택배비"),
                            ("refundAmount", "refund_amount", "환불액")):
        if key not in body:
            continue
        v = _int_or_400(body.get(key) or 0, label)
        if v < 0:
            abort(400, description=f"{label}는 0 이상이어야 합니다.")
        if key == "refundAmount" and v > (now["amount"] or 0):
            abort(400, description="환불액이 주문 금액보다 클 수 없습니다.")
        if v == now[col]:
            continue                        # 안 바뀐 값은 건드리지 않는다
        conn.execute(f"UPDATE orders SET {col}=? WHERE id=?", (v, oid))
        changes[label] = {"from": now[col], "to": v}
        if key == "feeAmount":
            fee_changed = True
    if fee_changed:
        # ★수수료를 '실제로 바꿨을 때만' 수동 확정으로 표시한다.
        #   화면이 세 값을 함께 보내므로, 환불액만 고쳐도 수동으로 굳으면
        #   그 주문이 수수료 자동계산·소급 적용에서 영원히 빠진다(2026-07-29 확인).
        conn.execute("UPDATE orders SET fee_rate=0, fee_manual=1 WHERE id=?", (oid,))
    if "refundAmount" in body or "refundReason" in body:
        v = _int_or_400(body.get("refundAmount") or 0, "환불액") if "refundAmount" in body \
            else (now["refund_amount"] or 0)
        reason = (body.get("refundReason") or "").strip()
        # ★환불 사유만 고친 것도 '바뀐 것'이다. 이걸 빼면 사유만 수정했을 때
        #   '변경할 항목이 없습니다'로 저장 전체가 막혔다.
        if "refundReason" in body and reason != (now["refund_reason"] or ""):
            changes["환불 사유"] = {"from": now["refund_reason"], "to": reason}
        conn.execute("UPDATE orders SET refund_at=?, refund_reason=? WHERE id=?",
                     (config.now_iso() if v else "", reason, oid))
    return changes


def _asset_ids_from_nos(conn, nos):
    """관리번호 목록 → 자산 id 목록. 못 찾은 번호는 따로 돌려준다.

    스캐너가 넣는 공백·개행·중복을 여기서 정리해 화면이 신경 쓰지 않게 한다.
    """
    seen, cleaned = set(), []
    for raw in nos:
        no = str(raw or "").strip()
        if no and no.upper() not in seen:
            seen.add(no.upper())
            cleaned.append(no)
    if not cleaned:
        return [], []
    ph = ",".join("?" * len(cleaned))
    found = {r["asset_no"].upper(): r["id"] for r in conn.execute(
        f"SELECT id, asset_no FROM assets WHERE UPPER(asset_no) IN ({ph})",
        [n.upper() for n in cleaned]).fetchall()}
    ids = [found[n.upper()] for n in cleaned if n.upper() in found]
    unknown = [n for n in cleaned if n.upper() not in found]
    return ids, unknown


# 주문일은 몰·엑셀마다 '2026-07-20', '2026.07.20 14:10', '2026/07/20'로 제각각 들어온다.
# 글자 그대로 비교하면 기간 조회가 달을 섞어 넣고 빼먹고, 정렬 순서도 뒤집힌다
# (대시 0x2D < 점 0x2E라 점 형식이 항상 위로 온다). 조회·정렬은 이 식으로 통일한다.
_ORDERED_DATE = "REPLACE(REPLACE(SUBSTR(o.ordered_at,1,10),'.','-'),'/','-')"
# 정렬용 — 날짜가 아닌 값(이관 데이터에 이름이 섞여 들어온 건 등)은 맨 뒤로 보낸다.
# 그냥 정렬하면 한글이 숫자보다 커서 쓰레기 3건이 목록 최상단을 차지한다.
_ORDERED_SORT = (
    f"CASE WHEN {_ORDERED_DATE} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
    f"THEN {_ORDERED_DATE} ELSE '' END"
)


def normalize_date(value):
    """저장 전에 날짜 표기를 'YYYY-MM-DD…' 한 가지로 맞춘다(시각 부분은 보존)."""
    v = (value or "").strip()
    if len(v) >= 10 and v[4] in "./" and v[7] in "./":
        return v[:4] + "-" + v[5:7] + "-" + v[8:10] + v[10:]
    if len(v) >= 8 and v[:8].isdigit():          # 20260720 / 20260720 14:10
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}{v[8:]}"
    return v


def _digits(s):
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _DIGITS_ONLY(col):
    """SQL에서 전화번호의 하이픈·공백·괄호를 걷어낸 형태를 만든다.

    저장된 값이 '010-1234-5678'이든 '010 1234 5678'이든 '01012345678'로 쳐서 찾을 수 있어야 한다.
    (SQLite에는 정규식이 없어 REPLACE를 겹쳐 쓴다.)
    """
    expr = col
    for ch in ("-", " ", "(", ")", "."):
        expr = f"REPLACE({expr},'{ch}','')"
    return expr


def _log_order(action, row, detail=None):
    audit.log(action, target=f"#{row['id']} {row['recipient'] or row['order_no']}", detail=detail)


# ---------------------------------------------------------------- list/detail

def _orders_filter_sql(skip=()):
    """목록 화면의 필터를 SQL로 — 엑셀 내보내기·상단 집계가 '보고 있는 그대로' 쓰게 공유한다.

    skip: 이 조건은 빼고 만든다. 상태 카드는 상태를 빼야(자기 자신을 세야) 하고,
          쇼핑몰 버튼은 채널을 빼야 몰별 개수가 나온다.
    """
    clause, params = _order_scope_clause()
    view = request.args.get("view", "active")
    sql = "SELECT o.* FROM orders o WHERE 1=1" + clause
    if view == "active":
        sql += " AND o.cancelled_at = '' AND o.archived_at = ''"
    elif view == "cancelled":
        sql += " AND o.cancelled_at != ''"
    elif view == "archived":
        sql += " AND o.archived_at != ''"
    since = (request.args.get("since") or "").strip()
    if since:
        sql += " AND o.updated_at > ?"
        params.append(since)
    channel = (request.args.get("channel") or "").strip()
    if channel and "channel" not in skip:
        if channel == "(미지정)":          # 쇼핑몰 값이 비어 있는 건만
            sql += " AND o.channel = ''"
        else:
            sql += " AND o.channel = ?"
            params.append(channel)
    # 쇼핑몰 관점 상태 필터(입금대기/준비중/배송중/배송완료/취소)
    mall_status = (request.args.get("mallStatus") or "").strip()
    if mall_status and "mallStatus" not in skip:
        if mall_status not in MALL_STATUS_LABELS:
            abort(400, description="알 수 없는 주문 상태입니다.")
        sql += f" AND ({_MALL_STATUS_SQL}) = ?"
        params.append(mall_status)
    # 주문일 기간 — '어제 들어온 주문', '이번 달 주문'을 뽑을 수 있어야 한다.
    # ordered_at은 '2026-07-20' / '2026-07-20 10:00:00' / ISO가 섞여 있어 앞 10자로 비교한다.
    date_from = (request.args.get("from") or "").strip()
    if date_from:
        sql += f" AND {_ORDERED_DATE} >= ?"
        params.append(normalize_date(date_from)[:10])
    date_to = (request.args.get("to") or "").strip()
    if date_to:
        sql += f" AND {_ORDERED_DATE} <= ?"
        params.append(normalize_date(date_to)[:10])

    q = (request.args.get("q") or "").strip()
    if q:
        conds = ["o.order_no LIKE ?", "o.recipient LIKE ?", "o.phone LIKE ?",
                 "o.product_name LIKE ?", "o.address LIKE ?", "o.memo LIKE ?"]
        params.extend(["%" + q + "%"] * len(conds))
        # 전화번호는 하이픈 유무로 갈리면 안 된다(01012345678로 쳐도 010-1234-5678이 나와야).
        # 숫자가 아예 없는 검색어에는 이 조건을 붙이지 않는다(빈 번호가 전부 걸린다).
        digits = _digits(q)
        if digits:
            conds.append(f"{_DIGITS_ONLY('o.phone')} LIKE ?")
            params.append("%" + digits + "%")
        sql += " AND (" + " OR ".join(conds) + ")"
    return sql, params


@bp.get("/orders")
def list_orders():
    require_any(*ORDER_READ_PERMS)
    sql, params = _orders_filter_sql()
    # 이관 등 대량 데이터에서도 전체를 볼 수 있게 한도를 조절 가능하게 둔다
    try:
        limit = min(max(int(request.args.get("limit", 500)), 1), 3000)
    except ValueError:
        limit = 500
    conn = get_db()
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM (" + sql + ")", params).fetchone()["c"]
    sql += " ORDER BY " + _ORDERED_SORT + " DESC, o.id DESC LIMIT ?"
    rows = conn.execute(sql, params + [limit]).fetchall()
    prep_map = prep_options_for_orders(conn, rows)   # 목록 전체를 한 번에(N+1 방지)
    same_customer = _same_customer_counts(conn, rows)
    payloads = []
    for r in rows:
        p = order_payload(conn, r, prep_map)
        # 같은 연락처의 다른 주문 수 — 엑셀 업로드분과 API 수집분이 '같은 고객'임을
        # 목록에서 바로 보이게 한다(대표 요청 2026-07-29). 0이면 표시하지 않는다.
        p["sameCustomerCount"] = same_customer.get(_digits(r["phone"]), 1) - 1
        payloads.append(p)
    return jsonify({
        "serverTime": config.now_iso(),
        "total": total,
        "shown": len(rows),
        "orders": payloads,
    })


def _same_customer_counts(conn, rows):
    """화면에 뜬 주문들의 전화번호(숫자만) → 전체 주문에서의 건수. 쿼리 1번(N+1 금지).

    고객 테이블이 따로 없으므로 전화번호를 사람 식별자로 쓴다(고객 이력과 같은 기준).
    9자리 미만(빈 값·안심번호 조각)은 사람 구분이 안 되므로 세지 않는다.
    취소 주문도 포함한다 — '이 사람이 전에 주문했다가 취소했다'도 고객 이력이다.
    """
    phones = {_digits(r["phone"]) for r in rows}
    phones = {p for p in phones if len(p) >= 9}
    if not phones:
        return {}
    marks = ",".join("?" * len(phones))
    expr = _DIGITS_ONLY("phone")
    counted = conn.execute(
        f"SELECT {expr} AS d, COUNT(*) AS c FROM orders "
        f"WHERE {expr} IN ({marks}) GROUP BY {expr}", list(phones)).fetchall()
    return {r["d"]: r["c"] for r in counted}


@bp.get("/orders/export")
def export_orders():
    """지금 보고 있는 조건 그대로 주문을 엑셀로 — 정산·세무·몰 대사에 쓴다.

    고객 명부가 통째로 빠져나가는 경로라 '주문 보기' 권한을 요구한다
    (셋팅 작업 권한만으로는 못 받는다).
    """
    require_any("orders.view", "orders.edit")
    from flask import Response

    from ..importers import write_xlsx
    sql, params = _orders_filter_sql()
    sql += " ORDER BY " + _ORDERED_SORT + " DESC, o.id DESC LIMIT 5000"
    conn = get_db()
    rows = conn.execute(sql, params).fetchall()
    assets = {}
    for r in conn.execute(
            "SELECT oa.order_id, a.asset_no FROM order_assets oa "
            "JOIN assets a ON a.id = oa.asset_id").fetchall():
        assets.setdefault(r["order_id"], []).append(r["asset_no"])
    headers = ["주문일", "쇼핑몰", "주문번호", "주문상태", "상품명", "옵션", "상품코드", "수량",
               "금액", "수취인", "연락처", "우편번호", "주소", "배송메시지", "내부메모",
               "자산번호", "택배사", "송장번호", "작업단계"]
    data = [[
        (r["ordered_at"] or "")[:10], r["channel"], r["order_no"],
        MALL_STATUS_LABELS.get(mall_status_of(r), ""),
        r["product_name"], r["option_name"], r["product_code"], r["quantity"], r["amount"],
        r["recipient"], r["phone"], r["postal_code"], r["address"], r["delivery_message"],
        r["memo"], ", ".join(assets.get(r["id"], [])), r["courier"], r["tracking_no"],
        "출고" if r["shipping_done"] else "검수" if r["inspection_done"]
        else "제작" if r["production_done"] else "준비" if r["preparing"] else "대기",
    ] for r in rows]
    audit.log("orders_exported", target=f"{len(rows)}건")
    return Response(
        write_xlsx(headers, data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=hms-orders-{config.today_str()}.xlsx"})


@bp.post("/orders/settlement-backfill")
def settlement_backfill():
    """이미 출고된 주문에 수수료·택배비를 소급 적용한다.

    요율을 처음 넣었을 때 과거분까지 맞추기 위한 것. 손으로 넣은 값은 건드리지 않는다.
    dryRun이면 얼마가 바뀌는지만 알려준다(대표가 숫자를 보고 결정하도록).
    """
    require("orders.edit")
    body = request.get_json(silent=True) or {}
    dry = bool(body.get("dryRun"))
    frm = (body.get("from") or "").strip()
    to = (body.get("to") or "").strip()

    # 사람이 확정한 수수료(0원 직거래 포함)는 건드리지 않는다
    sql = ("SELECT * FROM orders WHERE shipping_done=1 AND cancelled_at='' "
           "AND fee_manual=0 AND fee_amount=0 AND fee_rate=0")
    params = []
    if frm:
        sql += " AND substr(shipping_at,1,10) >= ?"
        params.append(frm[:10])
    if to:
        sql += " AND substr(shipping_at,1,10) <= ?"
        params.append(to[:10])

    with tx(write=not dry) as conn:
        cfg = settlement_settings(conn)
        if not cfg["rates"] and not cfg["shippingCost"]:
            abort(400, description="설정 > 정산에서 수수료율이나 택배비를 먼저 입력하세요.")
        rows = conn.execute(sql, params).fetchall()
        changed, fee_sum, ship_sum = 0, 0, 0
        by_channel = {}
        touched = []
        for r in rows:
            fee, rate = fee_for(cfg, r["channel"], r["amount"])
            ship = cfg["shippingCost"] if not r["shipping_cost"] else 0
            if not fee and not ship:
                continue
            changed += 1
            fee_sum += fee
            ship_sum += ship
            ch = by_channel.setdefault(r["channel"] or "(미지정)", {"count": 0, "fee": 0})
            ch["count"] += 1
            ch["fee"] += fee
            touched.append(r["id"])
            if not dry:
                conn.execute(
                    "UPDATE orders SET fee_amount=?, fee_rate=?, shipping_cost=? WHERE id=?",
                    (fee, rate, r["shipping_cost"] or ship, r["id"]))
        if not dry:
            audit.log("settlement_backfill", target=f"{changed}건",
                      detail={"fee": fee_sum, "shipping": ship_sum, "from": frm, "to": to,
                              "주문": touched[:100]})   # 어떤 주문이 바뀌었는지 남긴다
    return jsonify({"dryRun": dry, "scanned": len(rows), "changed": changed,
                    "feeTotal": fee_sum, "shippingTotal": ship_sum,
                    "byChannel": [{"channel": k, **v} for k, v in
                                  sorted(by_channel.items(), key=lambda kv: -kv[1]["fee"])]})


@bp.post("/orders/bulk")
def bulk_orders():
    """여러 주문에 같은 작업을 한 번에 — 건별 성공/실패를 그대로 돌려준다.

    한 건이 걸려도 나머지는 진행한다(전부 실패시키면 어디까지 됐는지 알 수 없다).
    CJ를 호출하는 송장 발급은 여기 넣지 않는다 — 외부 호출은 트랜잭션 밖에서
    건별로 처리해야 해서 화면이 순차로 부른다.
    """
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").strip()
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        abort(400, description="처리할 주문을 선택하세요.")
    if len(ids) > 200:
        abort(400, description="한 번에 200건까지 처리할 수 있습니다.")
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        abort(400, description="주문 번호가 올바르지 않습니다.")

    if action == "shipping":
        require_any("orders.work", "orders.ship")     # 출고 확인은 배송 담당도 한다
    elif action in ("preparing", "production", "softwareInspection"):
        require("orders.work")
    elif action == "cancel":
        require("orders.cancel")
    elif action in ("payStatus", "delivered", "archive", "unarchive", "review"):
        require("orders.edit")
    else:
        abort(400, description="알 수 없는 일괄 작업입니다.")

    value = body.get("value", True)
    reason = (body.get("reason") or "").strip()
    if action == "cancel" and (not reason or len(reason) > 500):
        abort(400, description="취소 사유를 1~500자로 입력하세요.")

    actor = g.user["display_name"]
    is_admin = bool(g.user["is_admin"])
    now = config.now_iso()
    ok, failed = [], []
    with tx(write=True) as conn:
        for oid in ids:
            # 카테고리 스코프 밖 주문은 애초에 보이지 않으므로 여기서도 건드리지 않는다
            scope, sp = _order_scope_clause()
            row = conn.execute(
                "SELECT o.* FROM orders o WHERE o.id=?" + scope, [oid] + sp).fetchone()
            if row is None:
                failed.append({"id": oid, "reason": "주문을 찾을 수 없습니다."})
                continue
            label = row["recipient"] or row["order_no"] or f"#{oid}"
            if action in ("preparing", "production", "softwareInspection", "shipping"):
                err = _stage_check(conn, row, action, bool(value), actor, is_admin)
                if err:
                    failed.append({"id": oid, "label": label, "reason": err})
                    continue
            elif action == "cancel":
                # 단건 취소와 같은 가드 — 나간 물건이 '판매가능' 재고로 둔갑하면 안 된다
                if row["cancelled_at"]:
                    failed.append({"id": oid, "label": label, "reason": "이미 취소된 주문입니다."})
                    continue
                if row["archived_at"]:
                    failed.append({"id": oid, "label": label, "reason": "보관된 주문은 취소할 수 없습니다."})
                    continue
                if row["shipping_done"]:
                    failed.append({"id": oid, "label": label,
                                   "reason": "출고 확인된 주문입니다. 출고 확인을 해제한 후 취소하세요."})
                    continue
                wb = conn.execute(
                    "SELECT wid FROM waybills WHERE order_id=? AND type='forward' "
                    "AND status IN ('issued','test') LIMIT 1",
                    (oid,)).fetchone()
                if wb:
                    failed.append({"id": oid, "label": label,
                                   "reason": f"발행된 송장({wb['wid']})을 먼저 취소하세요."})
                    continue
                conn.execute(
                    "UPDATE orders SET cancelled_at=?, cancelled_by=?, cancel_reason=?, preparing=0 "
                    "WHERE id=?", (now, actor, reason, oid))
                for a in conn.execute(
                        "SELECT a.id, a.status, oa.prev_status FROM order_assets oa "
                        "JOIN assets a ON a.id=oa.asset_id WHERE oa.order_id=?", (oid,)).fetchall():
                    if a["status"] in ("reserved", "shipped"):
                        back = a["prev_status"] if a["prev_status"] in AVAILABLE_STATUSES else "ready"
                        conn.execute("UPDATE assets SET status=?, updated_at=? WHERE id=?",
                                     (back, now, a["id"]))
                        asset_event(conn, a["id"], "매칭해제", {"orderId": oid, "사유": "주문취소"})
                conn.execute("DELETE FROM order_assets WHERE order_id=?", (oid,))
                _log_order("order_cancelled", row, {"reason": reason, "일괄": True})
            elif action == "payStatus":
                v = "unpaid" if str(value) == "unpaid" else "paid"
                conn.execute("UPDATE orders SET pay_status=? WHERE id=?", (v, oid))
            elif action == "delivered":
                conn.execute("UPDATE orders SET delivered_at=? WHERE id=?",
                             (now if value else "", oid))
            elif action == "archive":
                # 끝난 주문을 목록에서 치운다(지우는 게 아니라 '보관' 보기로 옮기는 것)
                # ★배송완료(구매확정)도 '끝난 주문'이다. 예전엔 출고 확인만 인정해서,
                #   [배송완료 처리]를 한 주문을 보관하려 하면 전부 실패했다(2026-07-29 전수조사).
                if not row["shipping_done"] and not row["cancelled_at"] \
                        and not (row["delivered_at"] or "").strip():
                    failed.append({"id": oid, "label": label,
                                   "reason": "아직 진행 중인 주문입니다 — 출고 확인·배송완료·취소 중 "
                                             "하나가 끝나야 보관할 수 있습니다."})
                    continue
                conn.execute("UPDATE orders SET archived_at=? WHERE id=?", (now, oid))
            elif action == "unarchive":
                conn.execute("UPDATE orders SET archived_at='' WHERE id=?", (oid,))
            elif action == "review":
                # 리뷰어 출고 지정/해제 — 셋팅·QC 화면에 눈에 띄게 뜬다
                conn.execute("UPDATE orders SET is_review=?, review_note=? WHERE id=?",
                             (1 if value else 0,
                              (reason or "제품X 빈박스출고") if value else "", oid))
            _touch(conn, oid)
            ok.append(oid)
        audit.log("orders_bulk", target=f"{action} {len(ok)}건",
                  detail={"action": action, "ok": len(ok), "failed": len(failed),
                          "ids": ok[:50], "reason": reason or None,
                          # 왜 안 됐는지도 남긴다 — 화면을 닫아도 나중에 확인할 수 있게
                          "실패": [f"{f.get('label') or f['id']}: {f['reason']}" for f in failed[:30]]})
    return jsonify({"ok": len(ok), "done": ok, "failed": failed})


@bp.get("/orders/customer-search")
def customer_search():
    """이전에 보낸 고객 찾기 — 재구매 고객 정보를 다시 타이핑하지 않게 한다."""
    require("orders.edit")
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    clause, params = _order_scope_clause()
    digits = _digits(q)
    conds = ["o.recipient LIKE ?", "o.phone LIKE ?"]
    args = ["%" + q + "%", "%" + q + "%"]
    if digits:
        conds.append(f"{_DIGITS_ONLY('o.phone')} LIKE ?")
        args.append("%" + digits + "%")
    rows = get_db().execute(
        "SELECT o.recipient, o.phone, o.postal_code, o.address, MAX(o.ordered_at) AS last_at, "
        "COUNT(*) AS cnt FROM orders o WHERE o.recipient != ''" + clause +
        " AND (" + " OR ".join(conds) + ")"
        # 같은 사람의 최신 주소 한 줄만 — 배송지가 바뀌었으면 최근 것을 쓴다
        " GROUP BY o.recipient, " + _DIGITS_ONLY("o.phone") +
        " ORDER BY last_at DESC LIMIT 8", params + args).fetchall()
    return jsonify([
        {"recipient": r["recipient"], "phone": r["phone"], "postalCode": r["postal_code"],
         "address": r["address"], "lastAt": (r["last_at"] or "")[:10], "count": r["cnt"]}
        for r in rows])


@bp.get("/orders/customer-history")
def customer_history():
    """같은 고객의 다른 주문 — 재구매·재발송·클레임 이력을 상세에서 바로 본다.

    고객 테이블이 따로 없으므로 전화번호(숫자만)를 사람 식별자로 쓴다.
    번호가 없으면 이름+주소 앞부분이라도 맞춰 본다.
    """
    require_any(*ORDER_READ_PERMS)
    oid = request.args.get("orderId", type=int)
    if not oid:
        abort(400, description="주문을 지정하세요.")
    conn = get_db()
    clause, params = _order_scope_clause()
    base = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if base is None:
        abort(404, description="주문을 찾을 수 없습니다.")

    phone = _digits(base["phone"])
    if phone and len(phone) >= 9:
        where = f" AND {_DIGITS_ONLY('o.phone')} = ?"
        args = [phone]
    elif (base["recipient"] or "").strip():
        where = " AND o.recipient = ? AND substr(o.address,1,10) = ?"
        args = [base["recipient"], (base["address"] or "")[:10]]
    else:
        return jsonify({"orders": [], "asTickets": [], "count": 0, "matchedBy": ""})

    rows = conn.execute(
        "SELECT o.* FROM orders o WHERE o.id != ?" + clause + where +
        " ORDER BY " + _ORDERED_SORT + " DESC, o.id DESC LIMIT 50",
        [oid] + params + args).fetchall()
    # A/S 증상 기록은 A/S 권한이 있는 사람만 본다
    can_as = g.user["is_admin"] or bool({"as.view", "as.manage"} & set(g.get("perms") or set()))
    tickets = conn.execute(
        "SELECT ticket_no, as_type, status, symptom, received_at FROM as_tickets "
        f"WHERE {_DIGITS_ONLY('phone')} = ? ORDER BY id DESC LIMIT 20",
        (phone,)).fetchall() if (phone and can_as) else []
    return jsonify({
        "matchedBy": "전화번호" if phone and len(phone) >= 9 else "이름+주소",
        "count": len(rows),
        "orders": [{"id": r["id"], "channel": r["channel"], "orderNumber": r["order_no"],
                    "orderedAt": r["ordered_at"], "productName": r["product_name"],
                    "amount": r["amount"], "memo": r["memo"],
                    "mallStatus": mall_status_of(r),
                    "mallStatusLabel": MALL_STATUS_LABELS.get(mall_status_of(r), "")}
                   for r in rows],
        "asTickets": [{"ticketNo": t["ticket_no"], "asType": t["as_type"], "status": t["status"],
                       "symptom": t["symptom"], "receivedAt": t["received_at"]} for t in tickets],
    })


@bp.get("/orders/today")
def orders_today():
    """오늘 출고 대수 — 대시보드 KPI 전용.

    ★화면에서 목록을 받아 세면 안 된다. 대시보드는 진행 중(view=active)만 받는데,
      실제 운영은 출고 확인 직후 '보관'으로 정리하므로(출고→보관 중앙값 1.6분)
      센 순간 대부분이 목록에서 빠져 하루 종일 0건으로 보였다(2026-07-31 감사).
      취소분만 빼고 보관·완료를 포함해 서버가 직접 센다.
    """
    require_any(*ORDER_READ_PERMS)
    clause, params = _order_scope_clause()
    today = config.now_iso()[:10]
    row = get_db().execute(
        "SELECT COUNT(*) AS cnt FROM orders o WHERE o.cancelled_at = ''"
        + clause + " AND substr(o.shipping_at,1,10) = ?", params + [today]).fetchone()
    return jsonify({"shippedToday": row["cnt"], "date": today})


@bp.get("/orders/summary")
def orders_summary():
    """쇼핑몰별 × 상태별 주문 집계 — 주문관리 화면 상단에 쓴다.

    ★목록과 같은 조건(보기·기간·검색)으로 센다. 예전에는 항상 전체를 세서
      '배송완료 563건' 카드를 눌러도 목록은 0건이 나왔다(2026-07-29 전수조사 확인).
      상태 카드는 상태 조건만, 쇼핑몰 버튼은 채널·상태 조건을 빼고 센다.
    """
    require_any(*ORDER_READ_PERMS)
    conn = get_db()

    # 상태 카드 — 지금 고른 쇼핑몰·기간·검색은 반영하고, 상태만 뺀다
    st_sql, st_params = _orders_filter_sql(skip=("mallStatus",))
    st_rows = conn.execute(
        f"SELECT ({_MALL_STATUS_SQL}) AS st, COUNT(*) AS cnt, COALESCE(SUM(o.amount),0) AS amount "
        f"FROM ({st_sql}) o GROUP BY st", st_params).fetchall()
    by_status = {s["code"]: {"count": 0, "amount": 0} for s in MALL_STATUSES}
    for r in st_rows:
        if r["st"] in by_status:
            by_status[r["st"]] = {"count": r["cnt"], "amount": r["amount"]}

    # 쇼핑몰 버튼 — 채널·상태를 빼고 센다(각 몰이 몇 건인지 보여주는 것이므로)
    m_sql, m_params = _orders_filter_sql(skip=("channel", "mallStatus"))
    m_rows = conn.execute(
        f"SELECT o.channel AS channel, ({_MALL_STATUS_SQL}) AS st, COUNT(*) AS cnt, "
        f"COALESCE(SUM(o.amount),0) AS amount FROM ({m_sql}) o GROUP BY o.channel, st",
        m_params).fetchall()
    malls = {}
    for r in m_rows:
        ch = r["channel"] or "(미지정)"
        m = malls.setdefault(ch, {"channel": ch, "total": 0, "amount": 0,
                                  **{s["code"]: 0 for s in MALL_STATUSES}})
        m[r["st"]] = m.get(r["st"], 0) + r["cnt"]
        m["total"] += r["cnt"]
        m["amount"] += r["amount"]
    return jsonify({
        "statuses": [{"code": s["code"], "label": s["label"], **by_status[s["code"]]}
                     for s in MALL_STATUSES],
        "malls": sorted(malls.values(), key=lambda m: -m["total"]),
    })


@bp.get("/orders/<int:oid>")
def order_detail(oid):
    require_any(*ORDER_READ_PERMS)
    conn = get_db()
    row = _get_order_or_404(conn, oid)
    return jsonify(order_payload(conn, row))


# ---------------------------------------------------------------- create (수기)

@bp.post("/orders")
def create_order():
    require("orders.edit")
    body = request.get_json(silent=True) or {}
    recipient = (body.get("recipient") or "").strip()
    product_name = (body.get("productName") or "").strip()
    if not recipient or not product_name:
        abort(400, description="수취인과 상품명은 필수입니다.")
    channel = (body.get("channel") or "수기").strip() or "수기"
    quantity = _int_or_400(body.get("quantity") or 1, "수량")
    amount = _money_or_400(body.get("amount") or 0, "금액")
    category_id = body.get("categoryId")
    if category_id is not None and category_id != "":
        category_id = _int_or_400(category_id, "카테고리 ID")
    else:
        category_id = None
    ts = config.now_iso()
    with tx(write=True) as conn:
        if category_id is not None and conn.execute(
                "SELECT id FROM categories WHERE id=?", (category_id,)).fetchone() is None:
            abort(400, description="존재하지 않는 카테고리입니다.")
        cur = conn.execute(
            "INSERT INTO orders(channel, order_no, ordered_at, product_name, option_name, product_code, "
            "receive_method, "
            "quantity, amount, recipient, phone, postal_code, address, delivery_message, memo, category_id, "
            "created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (channel, (body.get("orderNo") or "").strip(),
             (body.get("orderedAt") or "").strip() or ts,
             product_name, (body.get("optionName") or "").strip(),
             (body.get("productCode") or "").strip(),
             (body.get("receiveMethod") or "").strip(), quantity, amount,
             recipient, (body.get("phone") or "").strip(),
             (body.get("postalCode") or "").strip(), (body.get("address") or "").strip(),
             (body.get("deliveryMessage") or "").strip(), (body.get("memo") or "").strip(),
             category_id, g.user["display_name"], ts, ts),
        )
        oid = cur.lastrowid
        conn.execute("UPDATE orders SET dedupe_key=?, import_key=? WHERE id=?",
                     (f"manual-{oid}", f"manual-{oid}", oid))
        row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        _log_order("order_created", row, {"channel": channel})
        payload = order_payload(conn, row)
    return jsonify(payload), 201


# ---------------------------------------------------------------- 상태머신

def _stage_check(conn, row, stage, value, actor, is_admin):
    """단계 체크/해제. 반환값이 문자열이면 그것이 409 사유."""
    oid = row["id"]
    now = config.now_iso()

    if row["cancelled_at"]:
        return "취소된 주문입니다. 복구 후 처리하세요."
    if row["archived_at"]:
        return "보관된 주문은 변경할 수 없습니다."

    if stage == "preparing":
        if value:
            if row["preparing"] and row["preparing_by"] and row["preparing_by"] != actor and not is_admin:
                return f"'{row['preparing_by']}'님이 이미 준비 중입니다."
            if row["production_done"]:
                return "이미 제작 완료된 주문입니다."
            conn.execute("UPDATE orders SET preparing=1, preparing_by=?, preparing_at=? WHERE id=?",
                         (actor, now, oid))
        else:
            if row["preparing"] and row["preparing_by"] and row["preparing_by"] != actor and not is_admin:
                return f"'{row['preparing_by']}'님이 준비 중인 주문입니다. 본인만 해제할 수 있습니다."
            conn.execute("UPDATE orders SET preparing=0 WHERE id=?", (oid,))
        return None

    if stage == "production":
        if value:
            # 타인이 준비 중인 주문을 가로채지 못하게(클레임 가드는 제작완료에도 적용)
            if row["preparing"] and row["preparing_by"] and row["preparing_by"] != actor and not is_admin:
                return f"'{row['preparing_by']}'님이 준비 중인 주문입니다."
            # ★몰이 알려주지 않는 '우리가 챙기는 옵션'을 다 체크해야 넘어간다.
            #   이게 이 기능의 목적이다 — 카카오처럼 옵션을 못 거는 몰에서 주문서에
            #   아무 표시가 없어 리브레오피스·리커버리를 빠뜨리는 일을 막는다.
            missing = prep_unchecked_for_order(conn, row)
            if missing:
                return ("아직 챙기지 않은 옵션이 있습니다: "
                        + ", ".join(missing[:4])
                        + (f" 외 {len(missing) - 4}건" if len(missing) > 4 else "")
                        + " — 확인 후 체크해 주세요.")
            conn.execute(
                "UPDATE orders SET production_done=1, production_by=?, production_at=?, preparing=0 WHERE id=?",
                (actor, now, oid))
        else:
            if row["inspection_done"]:
                return "SW 검수가 완료된 주문입니다. 검수를 먼저 해제하세요."
            conn.execute("UPDATE orders SET production_done=0 WHERE id=?", (oid,))
        return None

    if stage == "softwareInspection":
        if value:
            if not row["production_done"]:
                return "제작 완료 후에 검수할 수 있습니다."
            conn.execute("UPDATE orders SET inspection_done=1, inspection_by=?, inspection_at=? WHERE id=?",
                         (actor, now, oid))
        else:
            # 검수 해제 시 출고 확인 연쇄 해제(원본 규칙).
            # 출고 확인까지 갔던 주문이면 자산도 함께 원복해야 'shipped' 고착이 없다.
            if row["shipping_done"]:
                for a in conn.execute(
                        "SELECT a.id, a.status FROM order_assets oa JOIN assets a ON a.id=oa.asset_id "
                        "WHERE oa.order_id=?", (oid,)).fetchall():
                    if a["status"] == "shipped":
                        conn.execute("UPDATE assets SET status='reserved', updated_at=? WHERE id=?", (now, a["id"]))
                        asset_event(conn, a["id"], "출고취소", {
                            "orderId": oid, "주문번호": row["order_no"] or "",
                            "수취인": row["recipient"] or "", "사유": "검수 해제"})
            conn.execute("UPDATE orders SET inspection_done=0, shipping_done=0 WHERE id=?", (oid,))
        return None

    if stage == "shipping":
        if value:
            if not (row["production_done"] and row["inspection_done"]):
                return "제작 완료와 SW 검수가 끝나야 출고 확인할 수 있습니다."
            # ★가재고 출고 금지(2026-08-04 대표) — 입고는 됐지만 완전한 수리 전인 물건이다.
            #   매칭(자산번호 등록)까지는 허용하되, 매입에서 양품/실재고로 바꾸기 전에는
            #   여기서 막는다. 안 막으면 수리 안 끝난 노트북이 고객에게 나간다.
            prov = [a["asset_no"] for a in conn.execute(
                "SELECT a.asset_no FROM order_assets oa JOIN assets a ON a.id=oa.asset_id "
                "WHERE oa.order_id=? AND a.tier='가재고'", (oid,)).fetchall()]
            if prov:
                return (f"가재고 자산({', '.join(prov)})이 매칭돼 있어 출고할 수 없습니다. "
                        "매입 화면에서 해당 자산을 양품 또는 실재고로 바꾼 뒤 출고 확인하세요.")
            # ★출고일·출고자는 '처음 출고한 때'를 지킨다.
            #   자산 매칭을 고치려고 단계를 껐다 켜는 일이 잦은데, 그때마다 오늘 날짜로
            #   덮으면 지난달 출고분이 이번 달 매출로 넘어가고 담당자 실적도 바뀐다.
            #   (출고일 자체를 고쳐야 하면 그건 별도 기능으로 다뤄야 한다.)
            keep_at = (row["shipping_at"] or "").strip()
            keep_by = (row["shipping_by"] or "").strip()
            conn.execute("UPDATE orders SET shipping_done=1, shipping_by=?, shipping_at=? WHERE id=?",
                         (keep_by or actor, keep_at or now, oid))
            # 수수료·택배비도 이미 확정된 값이 있으면 다시 덮지 않는다(같은 이유)
            if not keep_at:
                apply_settlement_defaults(conn, oid, row)
            # 매칭 자산 → 출고완료
            for a in conn.execute(
                    "SELECT a.id, a.asset_no, a.status FROM order_assets oa JOIN assets a ON a.id=oa.asset_id "
                    "WHERE oa.order_id=?", (oid,)).fetchall():
                if a["status"] != "shipped":
                    conn.execute("UPDATE assets SET status='shipped', updated_at=? WHERE id=?", (now, a["id"]))
                    # ★'누구에게 나갔는지'를 자산 이력 자체에 남긴다.
                    #   주문번호만 적어 두면 나중에 주문이 고쳐지거나 지워졌을 때
                    #   추적이 끊긴다. TMS 이관분에는 수령자가 남아 있는데
                    #   정작 HMS가 새로 출고한 건에 없으면 앞뒤가 안 맞는다(2026-07-31).
                    asset_event(conn, a["id"], "출고", {
                        "orderId": oid,
                        "주문번호": row["order_no"] or "",
                        "수취인": row["recipient"] or "",
                        "채널": row["channel"] or "",
                        "출고일": (keep_at or now)[:10],
                    })
        else:
            conn.execute("UPDATE orders SET shipping_done=0 WHERE id=?", (oid,))
            for a in conn.execute(
                    "SELECT a.id, a.status FROM order_assets oa JOIN assets a ON a.id=oa.asset_id "
                    "WHERE oa.order_id=?", (oid,)).fetchall():
                if a["status"] == "shipped":
                    conn.execute("UPDATE assets SET status='reserved', updated_at=? WHERE id=?", (now, a["id"]))
                    asset_event(conn, a["id"], "출고취소", {"orderId": oid})
        return None

    return "알 수 없는 단계입니다."


@bp.patch("/orders/<int:oid>")
def patch_order(oid):
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").strip()
    actor = g.user["display_name"]
    is_admin = bool(g.user["is_admin"])
    push_after = False           # 출고 확인 뒤 몰에 송장번호를 보낼지(트랜잭션 밖에서 처리)

    with tx(write=True) as conn:
        row = _get_order_or_404(conn, oid)

        if action in ("preparing", "production", "softwareInspection", "shipping"):
            # 출고 확인은 배송 담당의 일이다 — 셋팅 작업 권한이 없어도 할 수 있어야 한다
            if action == "shipping":
                require_any("orders.work", "orders.ship")
            else:
                require("orders.work")
            err = _stage_check(conn, row, action, bool(body.get("value")), actor, is_admin)
            if err:
                return _conflict(conn, oid, err)
            _touch(conn, oid)
            _log_order("order_stage", row, {"stage": action, "value": bool(body.get("value"))})
            if action == "shipping" and body.get("value"):
                fresh = conn.execute("SELECT tracking_no, mall_sent_at FROM orders WHERE id=?",
                                     (oid,)).fetchone()
                push_after = bool((fresh["tracking_no"] or "").strip() and not fresh["mall_sent_at"])

        elif action == "details":
            require("orders.edit")
            expected = (body.get("expectedUpdatedAt") or "").strip()
            if expected and expected != row["updated_at"]:
                return _conflict(conn, oid, "다른 사용자가 먼저 수정했습니다. 최신 내용을 확인하세요.")
            changes = {}
            fields = body.get("fields") or {}
            for key, (col, label) in _DETAIL_FIELDS.items():
                if key in fields:
                    v = (fields.get(key) or "").strip()
                    if v != row[col]:
                        conn.execute(f"UPDATE orders SET {col}=? WHERE id=?", (v, oid))
                        changes[label] = v
            for key, col, label in (("quantity", "quantity", "수량"), ("amount", "amount", "금액")):
                if key in fields:
                    # ★음수가 들어가면 상단 상태 카드의 매출 합계가 그만큼 깎이고
                    #   수량이 음수면 셋팅 스캔칸 수 계산도 어긋난다. 화면에선 원인을 못 찾는다.
                    v = _money_or_400(fields[key], label)
                    if v != row[col]:
                        # 금액을 바꿨으면 '바꾸기 전 값'도 남긴다 — 몰 원장 없이 되돌릴 수 있게
                        changes[label] = {"from": row[col], "to": v} if key == "amount" else v
                        conn.execute(f"UPDATE orders SET {col}=? WHERE id=?", (v, oid))
                        if key == "amount":
                            newfee = recalc_fee_for_amount(conn, oid, row)
                            if newfee is not None:
                                changes["판매수수료(자동 재계산)"] = {"from": row["fee_amount"], "to": newfee}
            if "categoryId" in fields:
                cid = fields.get("categoryId")
                cid = None if cid in (None, "") else _int_or_400(cid, "카테고리 ID")
                if cid is not None and conn.execute(
                        "SELECT id FROM categories WHERE id=?", (cid,)).fetchone() is None:
                    abort(400, description="존재하지 않는 카테고리입니다.")
                conn.execute("UPDATE orders SET category_id=? WHERE id=?", (cid, oid))
                changes["카테고리"] = cid
            # 정산 값을 함께 보냈으면 같은 저장 안에서 처리한다(요청 1번 = 원자적).
            # 금액 변경 뒤에 실행해야 환불액 상한이 '새 금액' 기준으로 검사된다.
            settle_changes = {}
            if isinstance(body.get("settlement"), dict):
                settle_changes = _apply_settlement(conn, oid, row, body["settlement"])
            if not changes and not settle_changes:
                # 아무것도 안 바뀐 저장은 오류가 아니다. 예전엔 400을 냈는데,
                # 화면이 [저장] 하나로 상세+정산을 보내므로 '고칠 게 없었을' 뿐인 경우가 흔하다.
                return jsonify(order_payload(conn, row))
            _touch(conn, oid)
            if changes:
                _log_order("order_updated", row, changes)
            if settle_changes:
                _log_order("order_settlement", row, settle_changes)

        elif action == "assets":
            require("orders.work")
            asset_ids = body.get("assetIds")
            if asset_ids is None:
                # 바코드 스캐너·복사붙여넣기는 관리번호 문자열로 들어온다
                asset_ids, unknown = _asset_ids_from_nos(conn, body.get("assetNos") or [])
                if unknown:
                    return _conflict(conn, oid,
                                     f"등록되지 않은 관리번호입니다: {', '.join(unknown[:5])}")
            err = _set_order_assets(conn, row, asset_ids, actor)
            if err:
                return _conflict(conn, oid, err)
            _touch(conn, oid)

        elif action == "settlement":
            # 실입금 정산 — 몰이 실제로 보내준 금액과 맞추는 손보기
            require("orders.edit")
            changes = _apply_settlement(conn, oid, row, body)
            if changes:
                _touch(conn, oid)
                _log_order("order_settlement", row, changes)

        elif action == "cancel":
            require("orders.cancel")
            reason = (body.get("reason") or "").strip()
            if not reason or len(reason) > 500:
                abort(400, description="취소 사유를 1~500자로 입력하세요.")
            if row["archived_at"]:
                abort(400, description="보관된 주문은 취소할 수 없습니다.")
            # 이미 나간 물건이 '판매가능' 재고로 둔갑하는 것을 방지:
            # 출고 확인·발행 송장을 먼저 정리해야 취소할 수 있다.
            if row["shipping_done"]:
                abort(400, description="출고 확인된 주문입니다. 출고 확인을 해제한 후 취소하세요.")
            wb = conn.execute(
                "SELECT wid FROM waybills WHERE order_id=? AND type='forward' "
                    "AND status IN ('issued','test') LIMIT 1",
                (oid,)).fetchone()
            if wb:
                abort(400, description=f"발행된 송장({wb['wid']})이 있습니다. 송장을 먼저 취소하세요.")
            now = config.now_iso()
            conn.execute(
                "UPDATE orders SET cancelled_at=?, cancelled_by=?, cancel_reason=?, preparing=0 WHERE id=?",
                (now, actor, reason, oid))
            # 매칭 자산 원복 — 매칭 전 상태(prev_status)로 되돌린다
            for a in conn.execute(
                    "SELECT a.id, a.status, oa.prev_status FROM order_assets oa "
                    "JOIN assets a ON a.id=oa.asset_id WHERE oa.order_id=?", (oid,)).fetchall():
                if a["status"] in ("reserved", "shipped"):
                    back = a["prev_status"] if a["prev_status"] in AVAILABLE_STATUSES else "ready"
                    conn.execute("UPDATE assets SET status=?, updated_at=? WHERE id=?", (back, now, a["id"]))
                    asset_event(conn, a["id"], "매칭해제", {"orderId": oid, "사유": "주문취소"})
            conn.execute("DELETE FROM order_assets WHERE order_id=?", (oid,))
            _touch(conn, oid)
            _log_order("order_cancelled", row, {"reason": reason})

        elif action == "restoreCancel":
            require("orders.cancel")
            if not row["cancelled_at"]:
                abort(400, description="취소된 주문이 아닙니다.")
            conn.execute(
                "UPDATE orders SET cancelled_at='', cancelled_by='', cancel_reason='', restored_at=?, restored_by=? WHERE id=?",
                (config.now_iso(), actor, oid))
            _touch(conn, oid)
            _log_order("order_restored", row)

        elif action == "applyShippingUpdate":
            require("orders.edit")
            pending = json.loads(row["pending_update"]) if row["pending_update"] else None
            if not pending:
                abort(400, description="대기 중인 배송지 변경이 없습니다.")
            col_map = {"recipient": "recipient", "phone": "phone", "postalCode": "postal_code",
                       "address": "address", "deliveryMessage": "delivery_message"}
            applied = {}
            for field, change in (pending.get("changed") or {}).items():
                col = col_map.get(field)
                if col and (change.get("incoming") or "").strip():
                    conn.execute(f"UPDATE orders SET {col}=? WHERE id=?",
                                 (change["incoming"].strip(), oid))
                    applied[field] = change["incoming"].strip()
            conn.execute("UPDATE orders SET pending_update=NULL WHERE id=?", (oid,))
            _touch(conn, oid)
            _log_order("order_updated", row, {"배송지변경반영": applied})

        elif action == "payStatus":
            require("orders.edit")
            v = "unpaid" if body.get("value") == "unpaid" else "paid"
            conn.execute("UPDATE orders SET pay_status=? WHERE id=?", (v, oid))
            _touch(conn, oid)
            _log_order("order_updated", row, {"입금상태": "입금대기" if v == "unpaid" else "결제완료"})

        elif action == "delivered":
            require("orders.edit")
            v = config.now_iso() if body.get("value", True) else ""
            conn.execute("UPDATE orders SET delivered_at=? WHERE id=?", (v, oid))
            _touch(conn, oid)
            _log_order("order_updated", row, {"배송완료": bool(v)})

        elif action == "dismissShippingUpdate":
            require("orders.edit")
            conn.execute("UPDATE orders SET pending_update=NULL WHERE id=?", (oid,))
            _touch(conn, oid)
            _log_order("order_updated", row, {"배송지변경": "무시"})

        else:
            abort(400, description="알 수 없는 action입니다.")

        row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        payload = order_payload(conn, row)

    # ★몰 호출은 트랜잭션 밖에서. 실패해도 출고는 그대로 두고 사유만 남긴다
    #   (재전송은 배송/송장 화면의 '몰 전송 대기' 목록에서 한다).
    if push_after:
        from flask import current_app
        from ..malls.invoice_push import push_for_order
        try:
            ok, msg = push_for_order(current_app, oid)
            payload["mallPush"] = {"ok": ok, "message": msg}
        except Exception as e:                                    # noqa: BLE001
            current_app.logger.exception("송장 몰 전송 실패 | order=%s", oid)
            payload["mallPush"] = {"ok": False, "message": str(e)}
    return jsonify(payload)


def _set_order_assets(conn, row, asset_ids, actor):
    """주문 ↔ 자산 매칭 목록 갱신. 오류 메시지 반환 시 409 처리.

    ★검증을 전부 선행한 뒤에 쓰기를 시작한다 — 중간에 실패해 409를 돌려주면서
      앞부분만 커밋되는 부분 반영을 막기 위함(원칙 #1).
    """
    if row["cancelled_at"]:
        return "취소된 주문입니다. 복구 후 매칭하세요."
    if row["archived_at"]:
        return "보관된 주문의 자산은 변경할 수 없습니다."
    if row["shipping_done"]:
        return "출고 확인된 주문의 자산은 변경할 수 없습니다. 출고 확인을 먼저 해제하세요."
    # ★송장이 이미 나갔으면 자산을 바꿀 수 없다.
    #   라벨에 관리번호가 찍혀 박스에 붙은 상태라, 여기서 자산을 빼면
    #   고객에게 나간 노트북이 '판매가능' 재고로 되살아나 다음 주문에 다시 붙는다
    #   (= 같은 물건 두 번 판매). 바꾸는 경우도 종이와 기록이 어긋나 A/S 때 대조가 안 된다.
    #   고쳐야 하면 송장을 먼저 취소하고(배송 화면) 다시 발급하는 게 맞는 순서다.
    wb = conn.execute(
        "SELECT wid, invoice_no FROM waybills WHERE order_id=? AND type='forward' "
        "AND status IN ('issued','test','pending') LIMIT 1", (row["id"],)).fetchone()
    if wb:
        return (f"송장({wb['invoice_no'] or wb['wid']})이 이미 발급돼 자산을 바꿀 수 없습니다."
                " 배송/송장 화면에서 송장을 취소한 뒤 다시 매칭하세요.")
    try:
        wanted = {int(a) for a in asset_ids}
    except (TypeError, ValueError):
        abort(400, description="assetIds가 올바르지 않습니다.")
    current = {r["asset_id"] for r in conn.execute(
        "SELECT asset_id FROM order_assets WHERE order_id=?", (row["id"],)).fetchall()}
    now = config.now_iso()

    # 1) 검증 단계 — 쓰기 없음
    to_add = []
    for aid in sorted(wanted - current):
        a = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
        if a is None:
            return f"존재하지 않는 자산입니다: #{aid}"
        if not a["received"]:
            return (f"자산 {a['asset_no']}은(는) 아직 입고확인이 안 됐습니다"
                    " — 매입 화면에서 입고확인 후 매칭하세요.")
        if a["status"] not in AVAILABLE_STATUSES:
            other = conn.execute(
                "SELECT o.id, o.recipient FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
                "WHERE oa.asset_id=? AND o.cancelled_at='' LIMIT 1", (aid,)).fetchone()
            where = f" (주문 #{other['id']} {other['recipient']})" if other else ""
            return f"자산 {a['asset_no']}은(는) {ASSET_STATUSES.get(a['status'])} 상태라 매칭할 수 없습니다{where}."
        # ★진행 중인 A/S에 걸린 물건은 고객 소유다 — 상태가 판매 가능이어도 내보내면 안 된다
        ticket = conn.execute(
            "SELECT ticket_no, customer FROM as_tickets WHERE asset_id=? "
            "AND status IN ('received','collecting','repairing','done') LIMIT 1", (aid,)).fetchone()
        if ticket:
            return (f"자산 {a['asset_no']}은(는) A/S 진행 중입니다"
                    f"({ticket['ticket_no']} · {ticket['customer']}). 고객에게 돌려보낼 물건이라 매칭할 수 없습니다.")
        # ★이미 다른 살아 있는 주문에 걸린 물건은 두 번 내보낼 수 없다.
        #   (회수 입고 때 연결을 끊지만, 데이터가 꼬인 경우를 여기서 한 번 더 막는다)
        dup = conn.execute(
            "SELECT o.id, o.recipient FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
            "WHERE oa.asset_id=? AND o.id != ? AND o.cancelled_at='' AND o.archived_at='' LIMIT 1",
            (aid, row["id"])).fetchone()
        if dup:
            return (f"자산 {a['asset_no']}은(는) 이미 다른 주문에 배정돼 있습니다"
                    f"(주문 #{dup['id']} {dup['recipient']}). 그 주문에서 먼저 해제하세요.")
        to_add.append(a)

    # 2) 반영 단계 — 여기서부터는 실패하지 않는다
    for a in to_add:
        conn.execute(
            "INSERT INTO order_assets(order_id, asset_id, prev_status, matched_by, matched_at) "
            "VALUES(?,?,?,?,?)", (row["id"], a["id"], a["status"], actor, now))
        conn.execute("UPDATE assets SET status='reserved', updated_at=? WHERE id=?", (now, a["id"]))
        asset_event(conn, a["id"], "주문매칭", {
            "orderId": row["id"], "주문번호": row["order_no"] or "",
            "수취인": row["recipient"] or "", "채널": row["channel"] or ""})

    for aid in sorted(current - wanted):
        link = conn.execute(
            "SELECT oa.prev_status, a.status FROM order_assets oa JOIN assets a ON a.id=oa.asset_id "
            "WHERE oa.order_id=? AND oa.asset_id=?", (row["id"], aid)).fetchone()
        conn.execute("DELETE FROM order_assets WHERE order_id=? AND asset_id=?", (row["id"], aid))
        if link and link["status"] in ("reserved", "shipped"):
            # 매칭 전 상태로 복원 — 정비중/입고 자산이 '판매가능'으로 승격되지 않게
            back = link["prev_status"] if link["prev_status"] in AVAILABLE_STATUSES else "ready"
            conn.execute("UPDATE assets SET status=?, updated_at=? WHERE id=?", (back, now, aid))
            asset_event(conn, aid, "매칭해제", {
                "orderId": row["id"], "주문번호": row["order_no"] or "",
                "수취인": row["recipient"] or ""})

    if wanted != current:
        _log_order("order_assets_matched", row, {"assetIds": sorted(wanted)})
    return None


# ---------------------------------------------------------------- 자산 검색(매칭용)

@bp.get("/orders/asset-search")
def asset_search():
    """매칭 후보 자산 검색 — 자산번호/모델/시리얼로 매칭 가능 상태만."""
    require("orders.work")
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    # ★매칭 가능한 것만 주면 '왜 안 되는지'를 알 수 없다.
    #   번호는 맞는데 이미 출고됐거나 다른 주문에 잡혀 있으면, 담당자는 번호를 잘못 친 줄 알고
    #   같은 번호를 계속 다시 찍는다(2026-07-30 대표: "자산번호를 적었는데 매칭이 안 된다").
    #   그래서 상태와 무관하게 찾아 주고, 쓸 수 있는지(available)와 사유를 함께 내려준다.
    rows = get_db().execute(
        "SELECT a.id, a.asset_no, a.maker, a.model, a.grade, a.status, a.cpu, a.ram, a.ssd, "
        "a.location, a.received, a.tier, c.name AS category_name, "
        "(SELECT o.order_no FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
        "  WHERE oa.asset_id=a.id LIMIT 1) AS held_by "
        "FROM assets a LEFT JOIN categories c ON c.id=a.category_id "
        "WHERE (a.asset_no LIKE ? OR a.model LIKE ? OR a.serial LIKE ?) "
        "ORDER BY (a.status IN (" + ",".join("?" * len(AVAILABLE_STATUSES)) + ") AND a.received=1) DESC, "
        "a.asset_no LIMIT 20",
        (*(("%" + q + "%",) * 3), *AVAILABLE_STATUSES),
    ).fetchall()

    def _why(r):
        # ★가입고(V)로 들어와 아직 실물을 안 받은 자산 — 상태는 '입고'로 보이지만 쓰면 안 된다
        if not r["received"]:
            return "아직 입고확인이 안 된 자산입니다(가입고 상태). 입고확인 후 사용하세요."
        if r["status"] in AVAILABLE_STATUSES:
            # ★가재고는 매칭까지는 되지만 출고가 막힌다(2026-08-04). 스캔하는 순간
            #   알려 줘야 담당자가 나중에 송장 단계에서 막히고 되돌아오지 않는다.
            if r["tier"] == "가재고":
                return ("가재고입니다 — 매칭은 되지만 출고할 수 없습니다. "
                        "매입 화면에서 양품 또는 실재고로 바꾸세요.")
            return ""
        label = ASSET_STATUSES.get(r["status"], r["status"])
        where = f" (주문 {r['held_by']})" if r["held_by"] else ""
        if r["status"] == "shipped":
            return "이미 출고된 자산입니다" + where
        if r["status"] == "reserved":
            return "다른 주문에 이미 배정됨" + where
        return f"지금은 매칭할 수 없는 상태입니다({label})" + where

    return jsonify([
        {"assetId": r["id"], "assetNo": r["asset_no"], "maker": r["maker"], "model": r["model"],
         "grade": r["grade"], "status": r["status"], "tier": r["tier"],
         "shippable": bool(r["received"]) and r["status"] in AVAILABLE_STATUSES
                      and r["tier"] != "가재고",
         "statusLabel": ASSET_STATUSES.get(r["status"], r["status"]),
         "available": bool(r["received"]) and r["status"] in AVAILABLE_STATUSES,
         "reason": _why(r),
         "spec": " / ".join(x for x in (r["cpu"], r["ram"], r["ssd"]) if x),
         "location": r["location"], "categoryName": r["category_name"]}
        for r in rows
    ])


# ---------------------------------------------------------------- 알림

_NOTIFY_ACTIONS = ("order_created", "order_updated", "order_stage", "order_cancelled",
                   "order_restored", "order_assets_matched", "orders_imported", "waybill_issued")


@bp.get("/notifications")
def notifications():
    require_any(*ORDER_READ_PERMS)
    ph = ",".join("?" * len(_NOTIFY_ACTIONS))
    rows = get_db().execute(
        f"SELECT ts, username, action, target, detail FROM audit_log WHERE action IN ({ph}) "
        "ORDER BY id DESC LIMIT 100", _NOTIFY_ACTIONS,
    ).fetchall()
    return jsonify([
        {"ts": r["ts"], "username": r["username"], "action": r["action"], "target": r["target"],
         "detail": json.loads(r["detail"]) if r["detail"] else None}
        for r in rows
    ])


# 서브모듈 라우트 등록 (bp 정의 이후에 import해야 함)
from . import importing, product_info, waybill, recall  # noqa: E402,F401


@bp.get("/orders/ship-today/preview")
def ship_today_preview():
    """[금일 출고 확인]을 누르면 무엇이 나가는지 — 출고 확인까지 끝난 주문.

    ★대표 결정(2026-08-05): QC 프로그램과 같은 흐름으로 간다.
      제작 완료 → (송장 출력 시 자동) 출고 확인 → [금일 출고 확인] → 준비 목록에서 사라짐.
      사라진 건은 [출고 기록 조회]에 쌓인다.
    """
    require_any("orders.ship", "orders.work", "orders.view")
    with tx() as conn:
        scope, params = _order_scope_clause()
        rows = conn.execute(
            "SELECT o.id, o.channel, o.order_no, o.recipient, o.product_name, o.quantity, "
            "       o.amount, o.tracking_no, o.inspection_by, o.inspection_at, o.receive_method "
            "FROM orders o WHERE o.cancelled_at='' AND o.archived_at='' "
            "  AND o.shipping_done=0 AND o.inspection_done=1" + scope,
            params).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"], "channel": r["channel"], "orderNumber": r["order_no"],
                "recipient": r["recipient"], "productName": r["product_name"],
                "quantity": r["quantity"], "amount": r["amount"],
                "trackingNumber": r["tracking_no"],
                "receiveMethod": r["receive_method"],
                "confirmedBy": r["inspection_by"], "confirmedAt": r["inspection_at"],
                # 택배인데 송장이 없으면 알려 준다 — 송장 없이 내보내면 추적이 끊긴다
                "noWaybill": (not (r["tracking_no"] or "").strip()
                              and (r["receive_method"] or "택배") == "택배"),
            })
    return jsonify({"orders": out, "count": len(out),
                    "noWaybill": sum(1 for x in out if x["noWaybill"])})


@bp.post("/orders/ship-today")
def ship_today():
    """출고 확인된 주문을 오늘 출고로 마감하고 준비 목록에서 내린다."""
    require_any("orders.ship", "orders.work")
    body = request.get_json(silent=True) or {}
    want = body.get("ids")
    want = {int(x) for x in want} if isinstance(want, list) else None
    ts = config.now_iso()
    done, skipped = 0, 0
    with tx(write=True) as conn:
        scope, params = _order_scope_clause()
        rows = conn.execute(
            "SELECT o.id, o.recipient, o.tracking_no, o.receive_method FROM orders o "
            "WHERE o.cancelled_at='' AND o.archived_at='' AND o.shipping_done=0 "
            "  AND o.inspection_done=1" + scope, params).fetchall()
        for r in rows:
            if want is not None and r["id"] not in want:
                skipped += 1
                continue
            conn.execute(
                "UPDATE orders SET shipping_done=1, shipping_by=?, shipping_at=?, "
                "  archived_at=?, updated_at=? WHERE id=?",
                (g.user["display_name"], ts, ts, ts, r["id"]))
            _log_order("ship_today", r,
                       detail={"trackingNumber": r["tracking_no"],
                               "receiveMethod": r["receive_method"] or "택배"})
            done += 1
        if done:
            audit.log("ship_today", target=f"{done}건 출고 마감",
                      detail={"shipped": done, "skipped": skipped})
    return jsonify({"ok": True, "shipped": done, "skipped": skipped})
