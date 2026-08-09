"""orders 테이블 row ↔ 임포터 주문 dict(camelCase, order-workflow 원본 형식) 변환.

임포터/중복판정 모듈(app.importers)은 원본 그대로 이식되어 camelCase dict를 다루므로,
DB 경계에서 이 모듈로만 변환한다.
"""
import json

from .. import config

# dict키 ↔ 컬럼 (단순 문자열 필드)
_STR_FIELDS = [
    ("importKey", "import_key"),
    ("orderNumber", "order_no"),
    ("sourceFile", "source_file"),
    ("channel", "channel"),
    ("orderedAt", "ordered_at"),
    ("productName", "product_name"),
    ("optionName", "option_name"),
    ("productCode", "product_code"),
    ("recipient", "recipient"),
    ("phone", "phone"),
    ("postalCode", "postal_code"),
    ("address", "address"),
    ("deliveryMessage", "delivery_message"),
    ("memo", "memo"),
    ("archivedAt", "archived_at"),
    ("cancelledAt", "cancelled_at"),
    ("updatedAt", "updated_at"),
]


def row_to_import_dict(row) -> dict:
    """dedupe 판정에 필요한 필드를 원본 dict 형식으로."""
    out = {key: (row[col] or "") for key, col in _STR_FIELDS}
    out["quantity"] = row["quantity"]
    out["amount"] = row["amount"]
    out["shippingDone"] = bool(row["shipping_done"])
    out["pendingShippingUpdate"] = json.loads(row["pending_update"]) if row["pending_update"] else None
    out["_rowId"] = row["id"]
    return out


def insert_import_dict(conn, order: dict, created_by: str) -> int:
    """임포트된 신규 주문 dict → orders INSERT."""
    from ..importers import coupang_cross_import_key, exact_order_content_key, order_dedupe_key

    ts = config.now_iso()
    qty = order.get("quantity")
    try:
        qty = max(1, int(str(qty).strip() or 1))
    except (TypeError, ValueError):
        qty = 1
    amount = order.get("amount")
    try:
        amount = int(float(str(amount).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        amount = 0
    cur = conn.execute(
        "INSERT INTO orders(channel, order_no, import_key, dedupe_key, content_key, cross_key, "
        "source_file, ordered_at, product_name, option_name, product_code, quantity, amount, "
        "recipient, phone, postal_code, address, delivery_message, memo, raw, created_by, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            (order.get("channel") or "").strip(),
            (order.get("orderNumber") or "").strip(),
            (order.get("importKey") or "").strip(),
            str(order_dedupe_key(order)),
            str(exact_order_content_key(order)),
            str(coupang_cross_import_key(order) or ""),
            (order.get("sourceFile") or "").strip(),
            (order.get("orderedAt") or "").strip(),
            (order.get("productName") or "").strip(),
            (order.get("optionName") or "").strip(),
            (order.get("productCode") or "").strip(),
            qty, amount,
            (order.get("recipient") or "").strip(),
            (order.get("phone") or "").strip(),
            (order.get("postalCode") or "").strip(),
            (order.get("address") or "").strip(),
            (order.get("deliveryMessage") or "").strip(),
            (order.get("memo") or "").strip(),
            json.dumps(order, ensure_ascii=False, default=str),
            created_by, ts, ts,
        ),
    )
    return cur.lastrowid


def writeback_changed(conn, before_json: dict, after: list):
    """new_unique_orders가 기존 주문 dict를 변경(배송지 대기/정보 보강)한 경우 DB 반영.

    before_json: {rowId: 스냅샷 json 문자열}, after: 변경 가능성이 있는 dict 목록.
    변경된 row 수를 반환한다.
    """
    changed = 0
    for o in after:
        rid = o.get("_rowId")
        if rid is None:
            continue
        now_json = json.dumps(o, ensure_ascii=False, sort_keys=True, default=str)
        if before_json.get(rid) == now_json:
            continue
        conn.execute(
            "UPDATE orders SET phone=?, postal_code=?, address=?, delivery_message=?, "
            "recipient=?, amount=?, pending_update=?, updated_at=? WHERE id=?",
            (
                (o.get("phone") or "").strip(),
                (o.get("postalCode") or "").strip(),
                (o.get("address") or "").strip(),
                (o.get("deliveryMessage") or "").strip(),
                (o.get("recipient") or "").strip(),
                int(float(str(o.get("amount") or 0).replace(",", "") or 0)),
                json.dumps(o["pendingShippingUpdate"], ensure_ascii=False)
                if o.get("pendingShippingUpdate") else None,
                config.now_iso(), rid,
            ),
        )
        changed += 1
    return changed
