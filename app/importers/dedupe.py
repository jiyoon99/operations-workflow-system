"""주문 중복 판정/병합 (순수 stdlib).

업무관리 주문 워크플로 원본(src/orders/dedupe.py) 이식본.
- 다층 중복키(주문번호 → fingerprint → syntheticContent → importKey + exactContent/쿠팡 교차)
- 중복 병합(빈 배송/금액 정보만 보강, 작업 상태는 보존)
- 같은 주문번호의 배송지 변경은 pendingShippingUpdate로 운영자 확인 유도
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

SHIPPING_UPDATE_FIELDS = ("recipient", "phone", "postalCode", "address", "deliveryMessage")
# 빈 칸만 보강하는 필드 — 어댑터 키 누락으로 반쪽 저장된 주문(2026-08-31 롯데온 사고)이
# 재수집 때 스스로 복구되도록 상품명/제품코드/주문일시도 포함한다(값이 있으면 안 덮는다).
DUPLICATE_DETAIL_FIELDS = ("phone", "postalCode", "address", "deliveryMessage",
                           "productName", "productCode", "orderedAt")


def normalized_order_value(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def normalized_address_value(value: object) -> str:
    text = normalized_order_value(value)
    return re.sub(r"[,\.\-_/()]+", "", text)


def normalized_phone_value(value: object) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def normalized_order_number(value: object) -> str:
    return normalized_order_value(value)


def is_synthetic_order_number(value: object) -> bool:
    return bool(re.fullmatch(r"수집-[0-9a-f]{10}", str(value or "").strip(), flags=re.IGNORECASE))


def normalized_order_minute(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = re.sub(r"[./]", "-", text)
    normalized = re.sub(r"\s+", " ", normalized)
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", normalized)
    if not match:
        return normalized_order_value(value)
    year, month, day, hour, minute = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d} {int(hour):02d}:{minute}"


def normalized_coupang_product_identity(order: dict) -> str:
    channel = normalized_order_value(order.get("channel"))
    if channel != "쿠팡":
        return ""
    product_code = normalized_order_value(order.get("productCode"))
    product_name = normalized_order_value(order.get("productName"))
    if product_code and not product_code.isdigit():
        return product_code
    if " / " in product_name:
        return product_name.split(" / ", 1)[0].strip()
    return product_name


def order_fingerprint(order: dict) -> tuple[str, ...]:
    return (
        normalized_order_value(order.get("recipient")),
        normalized_order_value(order.get("productName")),
        normalized_order_value(order.get("optionName")),
        normalized_address_value(order.get("address")),
    )


def order_dedupe_key(order: dict) -> tuple[str, ...]:
    # 중복 판정의 1차 키다. 실제 주문번호가 있으면 가장 신뢰하고,
    # 주문수집처럼 임시번호만 있는 주문은 내용 기반 키로 내려간다.
    order_number = normalized_order_number(order.get("orderNumber"))
    if order_number and not is_synthetic_order_number(order.get("orderNumber")):
        return ("orderNumber", order_number)
    fingerprint = order_fingerprint(order)
    phone = normalized_phone_value(order.get("phone"))
    if all(fingerprint) and phone:
        return ("fingerprint", *fingerprint, phone)
    if all(fingerprint):
        return ("fingerprint", *fingerprint)
    if is_synthetic_order_number(order.get("orderNumber")):
        synthetic_fingerprint = (
            normalized_order_value(order.get("channel")),
            normalized_order_value(order.get("orderedAt")),
            normalized_order_value(order.get("recipient")),
            normalized_order_value(order.get("productName")),
            normalized_order_value(order.get("optionName")),
            normalized_order_value(order.get("quantity")),
        )
        if all(synthetic_fingerprint[:5]):
            return ("syntheticContent", *synthetic_fingerprint)
    return ("importKey", normalized_order_value(order.get("importKey")))


def exact_order_content_key(order: dict) -> tuple[str, ...] | None:
    # 주문번호가 바뀌어 들어오는 일부 엑셀을 대비한 강한 내용 비교 키다.
    # 주소/전화/금액/배송메시지까지 맞아야 하므로 오판 위험이 낮다.
    values = (
        normalized_order_value(order.get("channel")),
        normalized_order_value(order.get("orderedAt")),
        normalized_order_value(order.get("recipient")),
        normalized_phone_value(order.get("phone")),
        normalized_order_value(order.get("postalCode")),
        normalized_address_value(order.get("address")),
        normalized_order_value(order.get("productName")),
        normalized_order_value(order.get("optionName")),
        normalized_order_value(order.get("quantity")),
        normalized_order_value(order.get("amount")),
        normalized_order_value(order.get("deliveryMessage")),
    )
    required_indexes = (0, 1, 2, 6, 8)
    if all(values[index] for index in required_indexes):
        return ("exactContent", *values)
    return None


def coupang_cross_import_key(order: dict) -> tuple[str, ...] | None:
    # 쿠팡은 주문수집 파일에서는 수집-... 임시번호로, DeliveryList에서는
    # 실제 주문번호로 들어올 수 있다. 두 파일을 같은 주문으로 묶기 위한 보강 키다.
    values = (
        normalized_order_value(order.get("channel")),
        normalized_order_minute(order.get("orderedAt")),
        normalized_order_value(order.get("recipient")),
        normalized_coupang_product_identity(order),
        normalized_order_value(order.get("quantity")),
    )
    if values[0] == "쿠팡" and all(values):
        return ("coupangCrossImport", *values)
    return None


def order_duplicate_keys(order: dict) -> list[tuple[str, ...]]:
    # 하나의 주문에 여러 중복 후보 키를 부여한다.
    # import 시에는 이 중 하나라도 기존 주문과 맞으면 새 주문을 추가하지 않는다.
    keys = [order_dedupe_key(order)]
    exact_key = exact_order_content_key(order)
    if exact_key and exact_key not in keys:
        keys.append(exact_key)
    coupang_key = coupang_cross_import_key(order)
    if coupang_key and coupang_key not in keys:
        keys.append(coupang_key)
    return keys


def is_shaped_product_code(value: object) -> bool:
    """자사 제품코드 축(「모델_CPU_그래픽」) 모양인가 — 밑줄 2개, 세 칸 전부 값.

    malls/base.py product_code_in 과 같은 모양 판정(여기는 순수 stdlib 모듈이라 복제).
    """
    t = str(value or "").split("/")[0].strip()
    return t.count("_") == 2 and all(x.strip() for x in t.split("_"))


def merge_duplicate_order_details(existing: dict, imported: dict, now: str) -> bool:
    # 중복으로 판단된 주문을 버리기 전에, 기존 주문의 빈 배송/금액 정보만 보강한다.
    # 제작/SW검수/출고 상태는 작업 기록이므로 여기서 덮어쓰지 않는다.
    changed = False
    for field in DUPLICATE_DETAIL_FIELDS:
        if not str(existing.get(field, "")).strip() and str(imported.get(field, "")).strip():
            existing[field] = imported.get(field, "")
            changed = True
    # ★제품코드 '축 승격'(2026-09-01 대표 "롯데온 고유코드는 필요하지 않아"): 기존 칸에
    #   몰 내부번호(LO2746331973·숫자 상품ID — 코드 모양이 아님)가 박혀 있고 새로 온 값이
    #   자사 코드 축이면 교체한다. 빈 칸 보강과 달리 '잘못된 축의 값'을 바로잡는 규칙 —
    #   코드 모양인 기존 값(사람이 넣은 자사 코드 포함)은 절대 덮지 않는다.
    ex_code = str(existing.get("productCode", "")).strip()
    im_code = str(imported.get("productCode", "")).strip()
    if (ex_code and im_code and ex_code != im_code
            and not is_shaped_product_code(ex_code) and is_shaped_product_code(im_code)):
        existing["productCode"] = im_code
        changed = True
    existing_amount = normalized_order_value(existing.get("amount"))
    imported_amount = normalized_order_value(imported.get("amount"))
    if existing_amount in {"", "0"} and imported_amount not in {"", "0"}:
        existing["amount"] = imported.get("amount", 0)
        changed = True
    if changed:
        existing["updatedAt"] = now
    return changed


def shipping_update_candidate(existing: dict, imported: dict, now: str) -> dict | None:
    current = {field: str(existing.get(field, "")).strip() for field in SHIPPING_UPDATE_FIELDS}
    incoming = {field: str(imported.get(field, "")).strip() for field in SHIPPING_UPDATE_FIELDS}
    changed = {
        field: {"current": current[field], "incoming": incoming[field]}
        for field in SHIPPING_UPDATE_FIELDS
        if incoming[field] and incoming[field] != current[field]
    }
    if not changed:
        return None
    candidate = {
        "detectedAt": now,
        "sourceFile": imported.get("sourceFile", ""),
        "orderNumber": imported.get("orderNumber", ""),
        "fields": {field: incoming[field] for field in SHIPPING_UPDATE_FIELDS},
        "changed": changed,
    }
    product_changed = any(
        str(imported.get(field, "")).strip() and str(imported.get(field, "")).strip() != str(existing.get(field, "")).strip()
        for field in ("productName", "optionName", "quantity")
    )
    if product_changed:
        candidate["contentChangeWarning"] = True
    return candidate


def new_unique_orders(existing: list[dict], imported: list[dict], now: str | None = None) -> tuple[list[dict], int]:
    # 저장된 주문 전체를 기준으로 본다. 출고 완료/보관 주문도 같은 기준에 포함된다.
    detected_at = now or datetime.now(timezone.utc).isoformat()
    keys = {order["importKey"] for order in existing}
    by_fingerprint = {
        duplicate_key: order
        for order in existing
        for duplicate_key in order_duplicate_keys(order)
    }
    by_order_number = {
        normalized_order_number(order.get("orderNumber")): order
        for order in existing
        if normalized_order_number(order.get("orderNumber")) and not is_synthetic_order_number(order.get("orderNumber"))
    }
    added = []
    shipping_updates = 0
    for order in imported:
        # 실제 주문번호 매칭은 배송지 변경 감지 대상이다. 같은 주문번호인데 주소/전화가
        # 달라졌다면 바로 덮지 않고 pendingShippingUpdate로 남겨 운영자가 확인하게 한다.
        key = order["importKey"]
        duplicate_keys = order_duplicate_keys(order)
        order_number = normalized_order_number(order.get("orderNumber"))
        matched = by_order_number.get(order_number) if order_number and not is_synthetic_order_number(order.get("orderNumber")) else None
        if matched:
            if not matched.get("archivedAt") and not matched.get("cancelledAt") and not matched.get("shippingDone"):
                candidate = shipping_update_candidate(matched, order, detected_at)
                if candidate:
                    matched["pendingShippingUpdate"] = candidate
                    matched["updatedAt"] = detected_at
                    shipping_updates += 1
            merge_duplicate_order_details(matched, order, detected_at)
            continue
        duplicate = next((by_fingerprint[duplicate_key] for duplicate_key in duplicate_keys if duplicate_key in by_fingerprint), None)
        if key in keys or duplicate:
            if duplicate:
                merge_duplicate_order_details(duplicate, order, detected_at)
            continue
        keys.add(key)
        for duplicate_key in duplicate_keys:
            by_fingerprint[duplicate_key] = order
        if order_number and not is_synthetic_order_number(order.get("orderNumber")):
            by_order_number[order_number] = order
        added.append(order)
    return added, shipping_updates


def duplicate_cleanup_keys(order: dict) -> list[tuple[str, ...]]:
    # 이미 저장되어 버린 중복을 정리할 때 쓰는 키다.
    # 실제 주문번호만 같은 케이스는 import 단계에서 이미 막히므로, 여기서는 내용/쿠팡 교차 키 위주로 묶는다.
    keys: list[tuple[str, ...]] = []
    dedupe_key = order_dedupe_key(order)
    if dedupe_key[0] in {"fingerprint", "syntheticContent", "importKey"}:
        keys.append(dedupe_key)
    for key in (exact_order_content_key(order), coupang_cross_import_key(order)):
        if key and key not in keys:
            keys.append(key)
    return keys


def duplicate_keep_sort_key(order: dict) -> tuple[int, int, int, str]:
    # 중복 그룹에서 어떤 주문을 남길지 결정한다.
    # 작업 진행 상태가 많은 주문을 우선 보존하고, 그 다음 정보가 많이 채워진 주문을 고른다.
    progress = sum(
        1
        for field in ("preparing", "productionDone", "softwareInspectionDone", "shippingDone")
        if order.get(field)
    )
    filled_fields = sum(
        1
        for field in (
            "managementNumber", "phone", "postalCode", "address", "deliveryMessage",
            "amount", "productionBy", "softwareInspectionBy", "shippingBy",
        )
        if str(order.get(field, "")).strip() not in {"", "0"}
    )
    real_order_number = bool(normalized_order_number(order.get("orderNumber")) and not is_synthetic_order_number(order.get("orderNumber")))
    return (-progress, -filled_fields, -int(real_order_number), str(order.get("createdAt", "")))


def merge_duplicate_cleanup_details(kept: dict, removed: dict, now: str) -> bool:
    # 중복 정리 버튼은 실제 행을 제거하므로, 제거될 주문에만 있던 유용한 정보를
    # 남길 주문으로 최대한 옮긴 뒤 삭제한다.
    changed = merge_duplicate_order_details(kept, removed, now)
    for field in ("courier", "trackingNumber", "memo"):
        if not str(kept.get(field, "")).strip() and str(removed.get(field, "")).strip():
            kept[field] = removed.get(field, "")
            changed = True
    if is_synthetic_order_number(kept.get("orderNumber")) and not is_synthetic_order_number(removed.get("orderNumber")):
        for field in ("orderNumber", "importKey", "sourceFile"):
            if str(removed.get(field, "")).strip():
                kept[field] = removed.get(field, "")
                changed = True
    if changed:
        kept["updatedAt"] = now
    return changed


def cleanup_duplicate_orders(orders: list[dict], now: str | None = None) -> dict:
    # /api/orders/dedupe에서 호출된다. orders 리스트를 제자리에서 수정하며,
    # 취소 주문은 운영 기록으로 보존하기 위해 자동 정리 대상에서 제외한다.
    detected_at = now or datetime.now(timezone.utc).isoformat()
    parent = list(range(len(orders)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    by_key: dict[tuple[str, ...], int] = {}
    for index, order in enumerate(orders):
        if order.get("cancelledAt"):
            continue
        for key in duplicate_cleanup_keys(order):
            if key in by_key:
                union(by_key[key], index)
            else:
                by_key[key] = index

    groups: dict[int, list[dict]] = {}
    for index, order in enumerate(orders):
        groups.setdefault(find(index), []).append(order)

    removed_ids: set[str] = set()
    removed_orders: list[dict] = []
    kept_orders: list[dict] = []
    for group in groups.values():
        candidates = [order for order in group if not order.get("cancelledAt")]
        if len(candidates) < 2:
            continue
        kept = sorted(candidates, key=duplicate_keep_sort_key)[0]
        kept_orders.append(kept)
        for duplicate in candidates:
            if duplicate is kept:
                continue
            merge_duplicate_cleanup_details(kept, duplicate, detected_at)
            removed_ids.add(str(duplicate.get("id")))
            removed_orders.append(duplicate)

    if removed_ids:
        orders[:] = [order for order in orders if str(order.get("id")) not in removed_ids]

    return {
        "removed": len(removed_ids),
        "groups": len(kept_orders),
        "keptOrderNumbers": [order.get("orderNumber", "") for order in kept_orders[:20]],
        "removedOrderNumbers": [order.get("orderNumber", "") for order in removed_orders[:20]],
    }
