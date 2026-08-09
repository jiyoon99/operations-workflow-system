"""쇼핑몰 엑셀 임포터 (순수 stdlib).

하프북 주문 워크플로 원본(src/importers.py) 이식본.
- 헤더 조합으로 채널(주문수집/고도몰/카카오/쿠팡/일반) 자동감지
- 채널별 헤더 매핑 → 원본과 동일한 camelCase 주문 dict 생성
- 주문수집/고도몰 다중 행 그룹핑 로직 그대로 유지
- 이식 시 수정 1건: channel에 금액 같은 숫자 오염값이 들어오면 빈 문자열로 대체
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from uuid import uuid4

from .excel import read_first_sheet


def _first(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = str(row.get(key, "")).strip()
        if value:
            return value
    return ""


def _clean_channel(value: str) -> str:
    # 이식 시 추가한 방어: 일부 파일에서 채널(플랫폼) 자리에 금액 같은
    # 숫자 오염값이 들어오는 케이스가 있어, 값이 전부 숫자면 채널명이 아니라고 보고
    # 빈 문자열로 대체한다(이후 "기타" 폴백이 적용된다).
    text = str(value or "").strip()
    if text and re.fullmatch(r"[0-9][0-9,.\s-]*", text):
        return ""
    return text


def _ordered_at(row: dict[str, str]) -> str:
    # 채널마다 주문일 컬럼명이 달라서, 가장 흔한 헤더부터 순서대로 찾는다.
    return _first(
        row,
        "주문일시",
        "주문일",
        "주문 시간",
        "주문시간",
        "주문일자",
        "결제일시",
        "결제일",
        "등록일시",
        "등록일",
        "접수일시",
        "접수일",
    )


def _address(row: dict[str, str]) -> str:
    # 배송지 주소는 본주소와 상세주소가 분리되는 경우가 많아서 합칠 수 있으면 합친다.
    address = _first(
        row,
        "배송지주소",
        "배송지 주소",
        "배송주소",
        "배송 주소",
        "배송지",
        "주소",
        "기본주소",
        "도로명주소",
        "전체주소",
        "수취인 주소",
        "수취인주소",
    )
    detail = _first(
        row,
        "상세주소",
        "상세 주소",
        "나머지 주소",
        "나머지주소",
        "수취인 나머지 주소",
    )
    if address and detail and detail not in address:
        return f"{address} {detail}".strip()
    return address or detail


def _number(value: str, fallback: int = 0) -> int:
    try:
        cleaned = re.sub(r"[^0-9.-]", "", str(value).replace(",", ""))
        return int(float(cleaned)) if cleaned else fallback
    except (TypeError, ValueError):
        return fallback


def _order_number(row: dict[str, str]) -> str:
    return _first(
        row,
        "주문번호",
        "주문 번호",
        "주문ID",
        "주문 ID",
        "주문코드",
        "주문 코드",
        "마켓주문번호",
        "마켓 주문번호",
        "쇼핑몰 주문번호",
        "상품주문번호",
        "상품 주문번호",
        "결제번호",
        "묶음배송번호",
    )


def _collected_group_identity(row: dict[str, str]) -> tuple[str, ...]:
    # 주문수집 파일은 한 주문이 여러 행으로 풀려 들어온다.
    # 주문번호가 없을 때도 같은 배송/수취 정보면 같은 주문 그룹으로 묶기 위한 식별자다.
    return (
        _first(row, "플랫폼"),
        _order_number(row),
        _ordered_at(row),
        _first(row, "수취인 이름"),
        _first(row, "연락처", "수령인 연락처", "수취인 연락처"),
        _first(row, "우편번호", "배송지우편번호", "배송지 우편번호"),
        _first(row, "주소", "배송지주소", "배송지 주소", "배송주소", "기본주소") or _address(row),
    )


def _is_collected_primary_product(product_line: str, main_product: str) -> bool:
    # 같은 노트북을 여러 대 샀거나 서로 다른 노트북을 같이 산 경우 모두 수량에 반영한다.
    # 액세서리/프로그램 옵션은 product_line에 있어도 주 상품 수량으로 보지 않는다.
    if not product_line:
        return False
    if product_line == main_product:
        return True
    return "노트북" in product_line and "노트북" in main_product


def _collected_orders(rows: list[dict[str, str]], source_file: str) -> list[dict]:
    # 주문수집 파일은 한 주문의 여러 상품 줄에도 주문일시/수취인이 반복될 수 있다.
    # 같은 주문번호 또는 같은 배송/수취 정보가 연속되면 하나의 주문으로 묶는다.
    groups: list[list[dict[str, str]]] = []
    current_identity: tuple[str, ...] | None = None
    for row in rows:
        identity = _collected_group_identity(row)
        has_identity = any(identity)
        same_order_number = bool(identity[1] and current_identity and identity[1] == current_identity[1])
        same_collection_order = has_identity and current_identity == identity
        starts_order = bool(_first(row, "주문일시", "수취인 이름")) and not (same_order_number or same_collection_order)
        if starts_order or not groups:
            groups.append([row])
            current_identity = identity if has_identity else None
        else:
            groups[-1].append(row)
            if has_identity and current_identity is None:
                current_identity = identity

    orders = []
    for group_index, group in enumerate(groups, 1):
        first = group[0]
        # 첫 상품은 대표 상품명으로 두고, 뒤에 나온 노트북/옵션 행은 optionName에 보관한다.
        # 프론트는 이 구조를 다시 상품 블록으로 풀어서 나다은 님 같은 복수 노트북 주문을 보여준다.
        product_lines = [_first(row, "상품명 + 옵션명") for row in group if _first(row, "상품명 + 옵션명")]
        product_name = product_lines[0] if product_lines else ""
        extra_options = [line for line in product_lines[1:] if line != product_name]
        registered_option = _first(first, "등록옵션명")
        option_name = " / ".join([value for value in [registered_option, *extra_options] if value])
        quantity = sum(
            _number(_first(row, "수량"), 1)
            for row in group
            if _is_collected_primary_product(_first(row, "상품명 + 옵션명"), product_name)
        )
        # 채널명은 숫자 오염 방어를 거친 값을 쓴다(전부 숫자면 "" → "기타" 폴백).
        platform = _clean_channel(_first(first, "플랫폼"))
        # 주문번호가 없는 수집 파일은 내용 서명으로 안정적인 임시번호를 만든다.
        # 같은 파일을 다시 넣었을 때 번호가 흔들리면 중복 판정이 약해지므로 서명 항목을 신중히 유지한다.
        signature = "|".join([
            platform, _first(first, "주문일시"), _first(first, "수취인 이름"),
            product_name, option_name, str(group_index),
        ])
        short_id = hashlib.sha256(signature.encode()).hexdigest()[:10].upper()
        actual_order_number = _order_number(first)
        order_number = actual_order_number or f"수집-{short_id}"
        orders.append({
            "importKey": f"주문수집:{platform or '기타'}:{order_number}",
            "channel": platform or "기타",
            "sourceFile": source_file,
            "orderNumber": order_number,
            "orderedAt": _ordered_at(first),
            "productName": product_name,
            "optionName": option_name,
            "productCode": registered_option,
            "quantity": max(1, quantity),
            "amount": _number(_first(first, "총 상품결제금액")),
            "recipient": _first(first, "수취인 이름"),
            "phone": _first(first, "연락처", "수령인 연락처", "수취인 연락처"),
            "postalCode": _first(first, "우편번호", "배송지우편번호", "배송지 우편번호"),
            "address": _first(first, "주소", "배송지주소", "배송지 주소", "배송주소", "기본주소") or _address(first),
            "deliveryMessage": _first(first, "배송메세지", "배송메시지"),
            "courier": "",
            "trackingNumber": "",
        })
    return orders


def _kakao(row: dict[str, str], source_file: str) -> dict:
    order_number = _first(row, "주문번호", "결제번호", "주문 번호")
    return {
        "importKey": f"카카오:{order_number}:{_first(row, '채널상품번호', '판매자상품번호')}:{_first(row, '옵션')}",
        "channel": "카카오", "sourceFile": source_file, "orderNumber": order_number,
        "orderedAt": _ordered_at(row), "productName": _first(row, "상품명", "주문상품명"),
        "optionName": _first(row, "옵션", "옵션명"), "productCode": _first(row, "판매자상품번호", "채널상품번호", "상품코드"),
        "quantity": _number(_first(row, "수량", "주문수량"), 1), "amount": _number(_first(row, "정산기준금액", "상품금액", "결제금액")),
        "recipient": _first(row, "수령인명", "수령인", "받는분"), "phone": _first(row, "하이픈포함 수령인연락처1", "수령인연락처1", "수령인연락처", "연락처"),
        "postalCode": _first(row, "우편번호", "배송지우편번호"), "address": _address(row),
        "deliveryMessage": _first(row, "배송메세지", "배송메시지"), "courier": _first(row, "택배사코드", "택배사"),
        "trackingNumber": _first(row, "송장번호"),
    }


def _coupang(row: dict[str, str], source_file: str) -> dict:
    order_number = _first(row, "주문번호", "주문 번호")
    return {
        "importKey": f"쿠팡:{order_number}:{_first(row, '옵션ID', '노출상품ID')}",
        "channel": "쿠팡", "sourceFile": source_file, "orderNumber": order_number,
        "orderedAt": _ordered_at(row), "productName": _first(row, "등록상품명", "노출상품명(옵션명)", "상품명"),
        "optionName": _first(row, "등록옵션명", "옵션명", "옵션"), "productCode": _first(row, "업체상품코드", "옵션ID", "노출상품ID", "상품코드"),
        "quantity": _number(_first(row, "구매수(수량)", "수량", "주문수량"), 1), "amount": _number(_first(row, "결제액", "결제금액", "상품금액")),
        "recipient": _first(row, "수취인이름", "수령인", "받는분"), "phone": _first(row, "수취인전화번호", "연락처", "수령인연락처"),
        "postalCode": _first(row, "우편번호", "배송지우편번호"), "address": _address(row),
        "deliveryMessage": _first(row, "배송메세지", "배송메시지"), "courier": _first(row, "택배사"),
        "trackingNumber": _first(row, "운송장번호"),
    }


def _godomall(row: dict[str, str], source_file: str) -> dict:
    order_number = _first(row, "주문 번호")
    address = " ".join(value for value in [
        _first(row, "수취인 주소"), _first(row, "수취인 나머지 주소"),
    ] if value)
    return {
        "importKey": f"고도몰:{order_number}:{_first(row, '상품주문번호', '주문코드(순서)')}:{_first(row, '상품코드')}",
        "channel": "고도몰", "sourceFile": source_file, "orderNumber": order_number,
        "orderedAt": _ordered_at(row), "productName": _first(row, "상품명", "주문 상품명"),
        "optionName": _first(row, "옵션정보", "텍스트옵션정보"), "productCode": _first(row, "자체상품코드", "상품코드"),
        "quantity": _number(_first(row, "상품수량"), 1), "amount": _number(_first(row, "판매가", "총 결제 금액")),
        "recipient": _first(row, "수취인 이름"), "phone": _first(row, "수취인 핸드폰 번호", "수취인 전화번호"),
        "postalCode": _first(row, "수취인 구 우편번호 (6자리)"), "address": address,
        "deliveryMessage": _first(row, "주문시 남기는 글"), "courier": _first(row, "배송 업체 번호"),
        "trackingNumber": _first(row, "송장 번호"),
    }


def _godomall_orders(rows: list[dict[str, str]], source_file: str) -> list[dict]:
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        order_number = _first(row, "주문 번호")
        if order_number:
            groups.setdefault(order_number, []).append(row)

    orders = []
    for order_number, items in groups.items():
        main = next((row for row in items if not _first(row, "상품명").startswith("[추가]")), items[0])
        order = _godomall(main, source_file)
        additions = []
        for row in items:
            if row is main:
                continue
            name = _first(row, "상품명")
            if not name:
                continue
            quantity = _number(_first(row, "상품수량"), 1)
            additions.append(f"{name} x{quantity}" if quantity > 1 else name)
        order["optionName"] = " / ".join(value for value in [order["optionName"], *additions] if value)
        order["amount"] = _number(_first(main, "총 결제 금액"), order["amount"])
        order["importKey"] = f"고도몰:{order_number}"
        orders.append(order)
    return orders


def import_workbook(content: bytes, source_file: str) -> list[dict]:
    # 헤더 조합을 보고 채널별 파서를 고른다. 새 포맷이 오면 여기서 분기 추가가 필요하다.
    rows = read_first_sheet(content, source_file)
    if not rows:
        raise ValueError("엑셀에 주문 데이터가 없습니다.")
    headers = set(rows[0].keys())
    if {"플랫폼", "상품명 + 옵션명", "수취인 이름"} <= headers:
        imported = _collected_orders(rows, source_file)
        importer = None
    elif {"주문 번호", "상품명", "상품수량", "수취인 이름"} <= headers:
        imported = _godomall_orders(rows, source_file)
        importer = None
    elif ({"결제번호", "채널상품번호"} <= headers) or "수령인명" in headers:
        importer = _kakao
    elif ({"묶음배송번호", "수취인이름"} <= headers) or "수취인이름" in headers:
        importer = _coupang
    elif ({"주문번호", "상품명"} <= headers or {"주문 번호", "상품명"} <= headers) and headers.intersection({"수령인", "받는분", "수령인명", "수취인이름"}):
        importer = _kakao
    else:
        detected = ", ".join(list(headers)[:12]) or "열 이름 없음"
        raise ValueError(f"지원하지 않는 엑셀 양식입니다. 감지된 열: {detected}")

    now = datetime.now(timezone.utc).isoformat()
    orders = []
    source_orders = imported if importer is None else (importer(row, source_file) for row in rows)
    for order in source_orders:
        if not order["orderNumber"] or not order["productName"]:
            continue
        order.update({
            "id": str(uuid4()), "managementNumber": "",
            "preparing": False, "preparingBy": "", "preparingAt": "",
            "productionDone": False, "productionBy": "", "productionAt": "",
            "softwareInspectionDone": False, "softwareInspectionBy": "", "softwareInspectionAt": "",
            "shippingDone": False, "shippingBy": "", "shippingAt": "", "createdAt": now, "updatedAt": now,
        })
        orders.append(order)
    if not orders:
        raise ValueError("주문번호와 상품명이 있는 주문 행을 찾지 못했습니다.")
    return orders
