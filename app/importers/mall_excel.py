"""쇼핑몰 엑셀 임포터 (순수 stdlib).

업무관리 주문 워크플로 원본(src/importers.py) 이식본.
- 헤더 조합으로 채널(주문수집/고도몰/카카오/쿠팡/일반) 자동감지
- 채널별 헤더 매핑 → 원본과 동일한 camelCase 주문 dict 생성
- 주문수집/고도몰 다중 행 그룹핑 로직 그대로 유지
- 이식 시 수정 1건: channel에 금액 같은 숫자 오염값이 들어오면 빈 문자열로 대체
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
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


# ---------------------------------------------------------------- 테무
# 테무 '주문 내보내기' 엑셀(2026-09-01 실파일로 확정). 다른 몰과 다른 점만 적는다:
#   ① 헤더가 1행이 아니다 — 위 5줄이 발송 주의사항 안내문이고 6행이 헤더다
#      (read_first_sheet의 헤더 자동탐지가 처리한다).
#   ② '수령인 이름' 열이 두 개다(앞에 실제 이름, 뒤는 빈 칸) — excel._row_dict가
#      빈 칸으로 덮지 않게 막는다. 안 그러면 수취인이 통째로 사라진다.
#   ③ 주소가 서양식으로 쪼개져 온다(도로명 → 시/군/구 → 시/도). 한국 순서로 뒤집어 합친다.
#   ④ 전화번호가 국제표기(+82 10 …)다. 국내 표기(010-…)로 되돌린다.
#   ⑤ 날짜가 '2026년 9월 1일 07:54 KST(UTC+9)' 꼴이다.
#   ⑥ ★제품코드: '제공 sku'(판매자 SKU)만 쓴다. 'SKU ID'는 테무 내부번호(130992207601233)라
#      제품코드 칸에 넣으면 고도몰 스펙·자산 재고 매칭을 막기만 한다(롯데온 LO번호와 같은 함정).
#      내부번호는 mallItemId/mallProductId로 보존한다.
#   ⑦ 금액: '할인 후 기본 가격 총액'(= 기본가 − 판매자 할인)이 우리 판매가다.
#      소매가/Temu 할인은 테무가 소비자에게 매긴 값이라 우리 매출이 아니다
#      (롯데온에서 '업체 분담만 빼고 롯데 분담은 안 뺀' 것과 같은 기준).
#   ⑧ ★그 판매가는 **부가세가 빠진 값**이다 — 아래 _TEMU_SETTLE_* 참고.
_TEMU_DENY = ("취소", "환불", "반품", "CANCEL", "REFUND", "RETURN")

# ★테무 정산 가산율 +10.75% (대표 확정 2026-09-07: "테무는 부가세 미포함가로 주문수집이
#   되고, 테무가 정산해줄 때는 결제금액(부가세 미포함가) + 10.75% 다").
#   다른 몰은 전부 '소비자가 낸 돈(부가세 포함)'을 금액으로 주는데 테무만 부가세를 뺀
#   판매가를 준다. OWS 전체가 orders.amount 를 '부가세 포함 총액'으로 보고 매출·마진·
#   부가세를 계산하므로(reports 가 SUM(amount) 그대로 매출로 쓴다), 여기서 올려놓지
#   않으면 테무 매출만 실입금보다 계속 낮게 잡힌다.
#
#   실파일 대조로 확인한 것(backups/code-claude-temu-20260901/temu_sample.xlsx 52열):
#     · 파일의 '제품 세금'(26,335)은 소매가 263,347 의 10%지 우리 기본가 260,610 의
#       부가세가 아니다 — 그걸 더하면 274원 틀린다. 파일에 정산액 칸은 없다.
#     · 라이브 실주문 243,636 × 1.1 = 268,000(정가)이 정확히 떨어진다 → 테무가 우리
#       판매가에서 부가세를 빼고 기록한다는 대표 설명과 맞는다.
#
#   ★float 를 안 쓰는 이유: 260610*1.1075 가 파이썬에서 288625.57499999995 로 나오고,
#     정확히 .5 로 떨어지는 값(260,600 → 288,614.5)에서 round() 는 짝수 반올림으로
#     288,614 를 주는데 세무 관행(사사오입)은 288,615 다. 1.1075 = 443/400 이라
#     정수식으로 재면 두 함정이 다 없다.
_TEMU_SETTLE_NUM, _TEMU_SETTLE_DEN = 443, 400      # 1.1075
_TEMU_SETTLE_LABEL = "+10.75%"


def temu_settlement_amount(base: int) -> int:
    """테무 판매가(부가세 미포함) → 실제 정산 입금액. 원 단위 사사오입.

    ★주문 '합계'에 한 번만 건다(행마다 걸지 않는다). 행별로 걸면 행마다 최대 0.5원씩
      오차가 쌓여 2행이면 1원, 4행이면 2원이 테무 입금액과 어긋난다. 테무도 주문 단위로
      입금하므로 대조 축을 주문에 맞춘다.
    """
    base = int(base or 0)
    if base <= 0:
        return base
    return (base * _TEMU_SETTLE_NUM + _TEMU_SETTLE_DEN // 2) // _TEMU_SETTLE_DEN


def _temu_amount(row: dict[str, str]) -> int:
    """이 상품행의 판매가(부가세 미포함) — 할인 후 기본가 총액 우선, 없으면 기본가 − 판매자 할인.

    ★여기서는 가산율을 걸지 않는다. 이 함수는 '엑셀이 준 원값'만 돌려주고,
      정산 보정은 주문 합계가 끝난 뒤 _temu_orders 에서 한 번만 건다.
    """
    after = _number(_first(row, "할인 후 기본 가격 총액"))
    if after:
        return after
    base = _number(_first(row, "기본 가격 총액"))
    if base:
        return max(0, base - _number(_first(row, "판매자 할인")))
    unit = _number(_first(row, "상품 기본 가격", "활동 상품 기본 가격"))
    # ★수량은 '발송할 수량'이 먼저다 — 부분취소 행(구매 3, 발송 1)에서 '구매 수량'으로
    #   곱하면 안 보내는 2대까지 매출로 잡힌다. 제외 판정(_temu_skip_reason)과 수량
    #   합산(아래)이 이미 '발송할 수량'을 기준으로 쓰는데 여기만 어긋나 있었다.
    return unit * max(1, _number(_first(row, "발송할 수량", "구매 수량"), 1))


# 테무 한국어 날짜: '2026년 9월 1일 07:54 KST(UTC+9)' / '2026년 9월 1일 오후 7:54 …'
_RE_TEMU_DATE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"
                           r"(?:\s*(오전|오후))?(?:\s*(\d{1,2}):(\d{2}))?")
# ISO 계열('2026-09-01 07:54' / '2026/09/01')도 받아 준다
_RE_ISO_DATE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:\D+(\d{1,2}):(\d{2}))?")


def _temu_datetime(value: str) -> str:
    """'2026년 9월 1일 07:54 KST(UTC+9)' → '2026-09-01 07:54'.

    ★모양이 확실할 때만 변환하고, 아니면 원문을 그대로 둔다(2026-09-01 적대 리뷰).
      예전에는 '4자리·1~2자리·1~2자리'를 아무 데서나 집어 조립해서, 영문 표기
      ('Sep 1, 2026 07:54')를 만나면 시각의 시·분을 월·일로 읽어 '2026-07-54' 같은
      **없는 날짜**를 만들었다. 그 주문은 어느 달 조회에도 안 잡히는 유령이 된다.
    ★'오후'는 12를 더한다(안 더하면 12시간이 틀어진다).
    """
    text = str(value or "").strip()
    if not text:
        return ""
    m = _RE_TEMU_DATE.search(text)
    ampm = ""
    if m:
        y, mo, d, ampm, hh, mm = m.groups()
    else:
        m = _RE_ISO_DATE.search(text)
        if not m:
            return text                    # 모르는 표기 — 원문 보존(가짜 날짜를 만들지 않는다)
        y, mo, d, hh, mm = m.groups()
    y, mo, d = int(y), int(mo), int(d)
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return text                        # 달·일이 말이 안 되면 손대지 않는다
    date = f"{y:04d}-{mo:02d}-{d:02d}"
    if hh is None:
        return date
    hh = int(hh)
    if ampm == "오후" and hh < 12:
        hh += 12
    elif ampm == "오전" and hh == 12:
        hh = 0
    if not (0 <= hh <= 23):
        return date
    return f"{date} {hh:02d}:{mm}"


def _temu_phone(value: str) -> str:
    """'+82 010 1234 5678' → '010-1234-5678'. 국내 번호가 아니면 숫자만 정리해 그대로.

    ★테무는 국가번호 뒤에도 국내 앞자리 0을 그대로 남겨 보낸다(실측 '+82 010 …').
      무조건 0을 덧붙이면 '00101234…'가 되어 문자·송장이 전부 실패한다(2026-09-01).
    """
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return ""
    if digits.startswith("82"):
        rest = digits[2:]                  # 국가번호 제거
        digits = rest if rest.startswith("0") else "0" + rest
    if len(digits) == 11 and digits.startswith("010"):
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if len(digits) == 10 and digits.startswith("0"):
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return digits


def _temu_address(row: dict[str, str]) -> str:
    """서양식으로 쪼개진 주소를 한국 순서(시/도 → 시/군/구 → 도로명)로 합친다."""
    parts = [
        _first(row, "배송 주"),                                   # 서울특별시
        _first(row, "배송 도시"),                                  # 도봉구
        _first(row, "지역"),
        _first(row, "배송 주소 1"), _first(row, "배송 주소 2"), _first(row, "배송 주소 3"),
    ]
    out: list[str] = []
    for part in parts:
        part = str(part or "").strip()
        # 같은 값이 여러 칸에 반복되는 경우가 있어 이미 담긴 조각은 건너뛴다
        if part and part not in out:
            out.append(part)
    return " ".join(out)


def _temu_skip_reason(row: dict[str, str]) -> str:
    """이 상품행을 건너뛸 이유. 없으면 빈 문자열.

    ★'주문 상태'(주문 단위)와 '주문 상품 상태'(상품 단위)를 합쳐서 보면 안 된다
      (2026-09-01 적대 리뷰): 주문 단위에 '부분 취소'가 찍히면 아직 보내야 하는
      멀쩡한 상품행까지 통째로 버려진다. 행 제외 근거는 **상품 단위 상태**만 쓴다.
    """
    item_status = _first(row, "주문 상품 상태").upper()
    if any(word in item_status for word in _TEMU_DENY):
        return "취소"
    # 보낼 수량이 0이면 전량 취소된 행이다(합계에 넣으면 0원 유령 주문이 된다)
    if _first(row, "발송할 수량") and _number(_first(row, "발송할 수량"), 0) <= 0:
        return "수량0"
    return ""


def _temu_orders(rows: list[dict[str, str]], source_file: str) -> tuple[list[dict], int]:
    """테무 엑셀 → (주문 dict 목록, 건너뛴 상품행 수).

    한 주문(주문 ID)의 여러 상품행을 하나로 묶는다.
    """
    groups: dict[str, list[dict[str, str]]] = {}
    order_seq: list[str] = []
    skipped = 0
    for row in rows:
        if _temu_skip_reason(row):
            skipped += 1
            continue
        order_no = _first(row, "주문 ID", "주문 ID(Order ID)", "주문번호")
        if not order_no:
            continue
        if order_no not in groups:
            groups[order_no] = []
            order_seq.append(order_no)
        groups[order_no].append(row)

    orders = []
    for order_no in order_seq:
        items = groups[order_no]
        first = items[0]
        names, options, quantity, amount = [], [], 0, 0
        codes, sku_ids, item_ids = [], [], []
        for row in items:
            name = _first(row, "제품 이름", "고객 주문별 제품 이름")
            if name and name not in names:
                names.append(name)
            opt = _first(row, "선택 사항")
            # 'Single Color'는 테무가 옵션 없는 상품에 붙이는 기본값이라 옵션으로 보지 않는다
            if opt and opt.strip().lower() not in ("single color", "단일 색상", "단일색상") \
                    and opt not in options:
                options.append(opt)
            # ★수량은 '대표 상품과 이름이 같은 줄'만 더한다(2026-09-01 적대 리뷰).
            #   무조건 합치면 노트북 1대 + 가방 1개 주문이 '노트북 2대'가 되어 그 수량이
            #   그대로 송장에 실린다. 주문수집 경로의 '노트북끼리' 휴리스틱은 '노트북 가방'
            #   같은 부속품도 걸리므로, 테무에서는 완전 일치만 인정한다(가장 예측 가능).
            #   대표와 다른 상품행은 optionName 에 남아 셋팅 화면 칩으로 보인다.
            if name and names and name == names[0]:
                quantity += _number(_first(row, "발송할 수량", "구매 수량"), 1)
            amount += _temu_amount(row)
            code = _first(row, "제공 sku", "판매자 SKU", "SKU 코드")
            if code and code not in codes:
                codes.append(code)
            sku = _first(row, "SKU ID")
            if sku and sku not in sku_ids:
                sku_ids.append(sku)
            item = _first(row, "상품 주문 ID")
            if item and item not in item_ids:
                item_ids.append(item)

        product_name = names[0] if names else ""
        extras = [x for x in (options + names[1:]) if x]
        # ★정산 보정은 주문 합계가 확정된 뒤 딱 한 번. 원값은 메모로 남겨 대표가 화면에서
        #   '몰이 준 금액 ↔ 우리가 잡은 금액'을 바로 대조할 수 있게 한다(금액만 바꿔 놓고
        #   근거를 안 남기면 나중에 아무도 왜 다른지 설명하지 못한다).
        settled = temu_settlement_amount(amount)
        settle_memo = (f"테무 정산 {_TEMU_SETTLE_LABEL}: 몰 표기 {amount:,}원(부가세 미포함)"
                       f" → {settled:,}원") if settled != amount else ""
        orders.append({
            "importKey": f"테무:{order_no}",
            "channel": "테무",
            "sourceFile": source_file,
            "orderNumber": order_no,
            "orderedAt": _temu_datetime(_first(first, "구매 날짜", "주문일시")),
            "productName": product_name,
            "optionName": " / ".join(extras),
            # ★판매자 SKU만 — 없으면 빈 값으로 둔다(테무 내부번호를 넣지 않는다).
            #   비어 있으면 업로드 단계에서 상품명으로 자사 코드를 찾아 채운다.
            "productCode": codes[0] if codes else "",
            "mallProductId": sku_ids[0] if sku_ids else "",
            "mallItemId": item_ids[0] if item_ids else "",
            "quantity": max(1, quantity),
            "amount": settled,
            "memo": settle_memo,
            "recipient": " ".join(x for x in [_first(first, "수령인 이름"),
                                              _first(first, "수령인 성")] if x),
            "phone": _temu_phone(_first(first, "수령인 전화번호")),
            "postalCode": _first(first, "배송 우편번호(다음 우편번호로 발송해야 합니다.)",
                                 "배송 우편번호", "우편번호"),
            "address": _temu_address(first),
            "deliveryMessage": "",
            "courier": _first(first, "배송사"),
            "trackingNumber": _first(first, "추적 번호"),
        })
    return orders, skipped


# ---------------------------------------------------------------- ESM(옥션·G마켓)
# ESM PLUS '발송관리' 엑셀. 59열이며 API 수집(app/malls/esm.py)과 같은 주문을 다룬다.
# ★이 양식은 숫자·날짜가 엑셀 원시값으로 들어온다(우리 리더가 <v> 태그를 그대로 읽는다):
#   주문번호 2569834423 → '2.569834423E9' (지수표기!)  · 주문일 → '46267.3708…' (일련번호)
#   그대로 쓰면 주문번호가 깨져 API 수집분과 같은 주문인 줄 모르고 두 번 들어온다.
# ★'수령인명' 열이 있어 카카오 분기에 먼저 걸린다 — 반드시 카카오보다 앞에서 잡아야 한다.

# 발송이 끝났거나 취소·반품된 건은 작업 목록에 올리지 않는다(고객이 취소한 걸 포장하면 안 된다).
_ESM_DENY = ("발송완료", "배송완료", "취소", "반품", "환불", "교환", "미결제", "결제대기")


def _esm_num_text(value: str) -> str:
    """엑셀이 숫자로 저장한 주문번호·배송번호를 사람이 쓰는 문자열로 되돌린다.

    '2.569834423E9' → '2569834423' / '2569834423.0' → '2569834423'.
    ★_number() 를 쓰면 안 된다 — 정규식이 'E'만 지워 '2.5698344239' 가 된다.
    """
    text = str(value or "").strip().replace(",", "")
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return text                       # 애초에 문자열 주문번호면 그대로 둔다
    return str(int(number)) if number == int(number) else text


# 엑셀 날짜 일련번호의 기준일. 1900년 윤년 버그 때문에 1899-12-30 이 기준이다.
_EXCEL_EPOCH = datetime(1899, 12, 30)


def _esm_datetime(value: str) -> str:
    """엑셀 일련번호('46266.8388…') 또는 문자열 날짜 → 'YYYY-MM-DD HH:MM:SS'."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        serial = float(text)
    except ValueError:
        return text                       # '2026-09-01 20:08:00' 처럼 이미 읽히는 형태
    if not (1 <= serial < 100000):        # 날짜로 보기 어려운 값은 건드리지 않는다
        return text
    moment = _EXCEL_EPOCH + timedelta(days=serial)
    # 초 단위 반올림 오차(…59.9999초)를 없앤다
    moment += timedelta(seconds=0.5)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _esm_channel(row: dict[str, str]) -> str:
    """'옥션(qpsgj260)' → 'ESM(옥션)'. API 수집분(esm.py channel)과 같은 표기라야
    같은 채널로 모인다 — 표기가 다르면 화면에서 두 몰처럼 갈린다."""
    seller = _first(row, "판매아이디", "판매자ID", "판매아이디(사이트)")
    site = seller.split("(", 1)[0].strip() if seller else ""
    if "지마켓" in site or "G마켓" in site or "gmarket" in site.lower():
        site = "G마켓"
    elif "옥션" in site or "auction" in site.lower():
        site = "옥션"
    return f"ESM({site})" if site else "ESM"


def _esm_skip_reason(row: dict[str, str]) -> str:
    state = _first(row, "주문상태", "주문 상태")
    for word in _ESM_DENY:
        if word in state:
            return state or word
    return ""


def _esm_orders(rows: list[dict[str, str]], source_file: str) -> tuple[list[dict], int]:
    """ESM 발송관리 엑셀 → (주문 목록, 건너뛴 행 수)."""
    orders, skipped = [], 0
    for row in rows:
        if _esm_skip_reason(row):
            skipped += 1
            continue
        order_no = _esm_num_text(_first(row, "주문번호", "주문 번호"))
        if not order_no:
            continue
        # ★자체 제품코드는 '판매자 관리코드' 칸에 있다. '상품번호'(F648513503)는 ESM
        #   내부번호라 제품코드로 쓰면 고도몰 스펙 매칭이 통째로 어긋난다(롯데온 LO… 사고와 같다).
        orders.append({
            "importKey": f"ESM:{order_no}",   # API 수집분과 같은 키 — 두 번 안 들어온다
            "channel": _esm_channel(row), "sourceFile": source_file,
            "orderNumber": order_no,
            "orderedAt": _esm_datetime(_first(row, "주문일(결제확인전)", "결제일", "주문일")),
            "productName": _first(row, "상품명"),
            "optionName": _first(row, "옵션", "추가구성"),
            "productCode": _first(row, "판매자 관리코드", "판매자 상세관리코드"),
            "mallProductId": _first(row, "상품번호"),
            "quantity": _number(_first(row, "수량"), 1),
            "amount": _number(_first(row, "판매금액", "판매단가")),
            "recipient": _first(row, "수령인명") or _first(row, "구매자명"),
            "phone": _first(row, "수령인 휴대폰", "수령인 전화번호", "구매자 휴대폰"),
            "postalCode": _first(row, "우편번호"),
            "address": _first(row, "주소") or _address(row),
            "deliveryMessage": _first(row, "배송시 요구사항"),
            "courier": _first(row, "택배사명(발송방법)"),
            "trackingNumber": _esm_num_text(_first(row, "송장번호")),
        })
    return orders, skipped


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
    # 테무 먼저 본다 — '주문 ID'·'SKU ID'는 다른 몰과 겹치지 않는 조합이다
    if {"주문 ID", "SKU ID"} <= headers and ("제품 이름" in headers or "고객 주문별 제품 이름" in headers):
        imported, temu_skipped = _temu_orders(rows, source_file)
        if not imported and temu_skipped:
            # 전부 취소·발송완료라 가져올 게 없는 것을 '양식이 안 맞다'로 오해하면 안 된다
            raise ValueError(f"테무 파일에서 가져올 신규 주문이 없습니다 "
                             f"(취소·전량취소로 건너뛴 상품행 {temu_skipped}건).")
        importer = None
    elif {"플랫폼", "상품명 + 옵션명", "수취인 이름"} <= headers:
        imported = _collected_orders(rows, source_file)
        importer = None
    elif {"주문 번호", "상품명", "상품수량", "수취인 이름"} <= headers:
        imported = _godomall_orders(rows, source_file)
        importer = None
    # ★ESM(옥션·G마켓)은 카카오보다 먼저 본다 — '수령인명' 열이 있어 뒤에 두면
    #   카카오 분기가 먼저 삼킨다(그러면 주문번호가 지수표기로 깨진 채 들어온다).
    elif {"판매아이디", "판매자 관리코드"} <= headers and "주문상태" in headers:
        imported, esm_skipped = _esm_orders(rows, source_file)
        if not imported and esm_skipped:
            raise ValueError(f"ESM(옥션·G마켓) 파일에서 가져올 신규 주문이 없습니다 "
                             f"(발송완료·취소로 건너뛴 행 {esm_skipped}건).")
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
