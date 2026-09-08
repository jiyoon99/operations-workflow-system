"""롯데온(LotteON) 주문 수집.

- 주문조회: POST https://openapi.lotteon.com/v1/openapi/delivery/v1/SellerDeliveryOrdersSearch
  롯데온은 '주문조회' API가 따로 없고 배송 카테고리의 '출고/회수지시(주문정보) 조회'가
  사실상 신규 주문 수집 창구다(공식 FAQ 명시). 그래서 odPrgsStepCd=11(출고지시)로 부른다.
- 인증: 헤더 Authorization: Bearer {인증키}. OAuth/HMAC 서명 없음.
  ★인증키는 판매자센터에 등록한 고정 IP에서만 통한다(미등록 IP=403, 잘못/만료된 키=401).
  ★인증키 유효기간 1년 — 만료 전 재발급 필요(설정의 key_expires_at는 알림용).
- ★조회기간 1일 초과 불가(공통 체크코드 2003) → max_window_days=1로 하루씩 잘라 부른다.
  경계에서 '1일 초과'로 튕기지 않도록 각 구간 끝에서 1초를 뺀다(다음 구간이 그 시각부터 시작).
- ★수집만으로는 롯데온 주문이 '상품준비중'으로 넘어가지 않는다. '연동완료 통보'를 따로
  호출해야 하고, 통보 전까지는 고객이 즉시취소할 수 있는 상태로 남는다.
  → mark_collected()를 만들어 두었지만 collect_orders에서 자동 호출하지 않는다.
    (수집 성공 = 우리 DB 저장 성공을 확인한 뒤에 통보해야 주문이 붕 뜨지 않는다.
     저장 트랜잭션 커밋 이후 호출하도록 나중에 배선할 것.)
- 샌드박스가 없다. 여기 있는 쓰기 계열(mark_collected/upload_invoice)은 전부 실주문 상태를
  바꾸는 부수효과가 있으므로 키 발급 후에도 실주문 1건으로만 단계 검증할 것.
- 응답 키 이름이 문서 버전마다 달라서(odNo/orderNo, rcvrNm/receiverName …) 단일 키를 믿지 않고
  _first()로 후보를 순서대로 훑는다. 응답 껍데기 이름도 마찬가지라 _find_rows()로 찾아낸다.
- ★2026-08-31 실주문 응답으로 실제 키 확정(주문번호·주문일시·상품코드만 오고 수취인/상품명이
  비던 사고 수정). 실키는 후보 맨 앞에 둔다:
    수취인=dvpCustNm(주문자=odrNm) / 휴대폰=dvpMphnNo(주문자=mphnNo) / 우편번호=dvpZipNo
    주소=dvpStnmZipAddr+dvpStnmDtlAddr(도로명) / 상품명=spdNm / 단품명=sitmNm
    판매자 상품코드(자사 제품코드 축)=epdNo / 롯데온 상품번호=spdNo / 주문완료일시=odCmptDttm
    금액: 판매금액=slAmt·단가=slPrc·고객실결제=actualAmt·혜택합=fvrAmtSum
          분담: 업체=prEntpShrAmtSum / 롯데=prSfcoShrAmtSum / 추가옵션=pdAdtnOptJsn(JSON 배열)
"""
import json
from datetime import timedelta

import requests

from .. import config
from .base import MallAdapter, MallError, register

BASE_URL = "https://openapi.lotteon.com"
SEARCH_PATH = "/v1/openapi/delivery/v1/SellerDeliveryOrdersSearch"
# 연동완료 통보(apiNo=210). 조사 자료에 정확한 오퍼레이션명이 남아 있지 않아 관례 이름으로 두고,
# 설정에 mark_path가 있으면 그 값을 쓴다(키 발급 후 API센터에서 실제 경로 확인 필요).
MARK_PATH = "/v1/openapi/delivery/v1/SellerDeliveryOrdersIfCplInform"
# 배송상태 통보 V2 — 송장 전송(발송완료)
INVOICE_PATH = "/v1/openapi/delivery/v2/SellerDeliveryProgressStateInform"

# 진행단계 코드: 11=출고지시(신규), 12=상품준비, 13=발송완료, 14=배송완료 / 23=회수지시
STEP_SHIP_ORDER = "11"
STEP_SHIPPED = "13"
# 수집 대상: 출고지시(=결제완료 후 넘어온 신규) ~ 상품준비중. 13 이상(발송/배송완료)은 제외.
COLLECT_STEPS = ("11", "12")
# 주문유형: 10=주문, 30=교환, 40=반품, 50=AS. 신규 주문만 받고 교환/반품 재배송은 별도로 다룬다.
COLLECT_ORDER_TYPES = ("10",)

# 한 번에 통보/전송할 수 있는 최대 건수(배송상태 통보 V2 기준 500건)
BATCH_LIMIT = 500
# 운송장번호 1건당 최대 30자
INVOICE_NO_MAXLEN = 30

# 성공으로 볼 결과코드(문서상 0000, 구현체마다 0/200/OK도 있음)
_OK_CODES = ("0000", "000", "00", "0", "200", "OK", "SUCCESS", "TRUE")
# 주문 행을 식별하는 주문번호 후보 키(소문자 비교용)
_ORDER_NO_KEYS = ("odno", "orderno", "ordno", "odnum", "ordernumber")


def _first(node, *names, default=""):
    """응답 dict에서 후보 키를 순서대로 찾아 첫 값(문자열)을 돌려준다.

    롯데온 문서/버전에 따라 같은 값이 odNo/orderNo, rcvrNm/receiverName처럼 다르게 온다.
    대소문자 표기(odNo vs ODNO)도 흔들려서 정확 매칭 후 소문자 매칭까지 시도한다.
    """
    if not isinstance(node, dict):
        return default
    for name in names:
        if name in node:
            value = node[name]
            if value is not None and str(value).strip() != "":
                return str(value).strip()
    lowered = {str(k).lower(): v for k, v in node.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return default


def _int(value):
    try:
        return int(float(str(value).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def _fmt_api_dt(dt):
    """조회 파라미터 형식 yyyymmddhhmmss."""
    return dt.strftime("%Y%m%d%H%M%S")


def _fmt_dt(value):
    """롯데온의 압축 일시(20260728143000)를 사람이 읽는 형식으로 편다.

    중복판정(dedupe)이 'YYYY-MM-DD HH:MM' 패턴을 찾아 분 단위로 비교하기 때문에,
    압축 형태로 두면 같은 주문이 엑셀로도 들어왔을 때 같은 건으로 묶이지 않는다.
    """
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 14:
        return (f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]} "
                f"{digits[8:10]}:{digits[10:12]}:{digits[12:14]}")
    if len(digits) == 12:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}"
    if len(digits) == 8 and len(text) == 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return text


def _looks_like_order(node):
    if not isinstance(node, dict):
        return False
    keys = {str(k).lower() for k in node}
    return any(k in keys for k in _ORDER_NO_KEYS)


def _find_rows(payload):
    """응답에서 주문 행 리스트를 찾아낸다.

    감싸는 이름(data/orderList/odList/sellerDeliveryOrders…)이 문서마다 달라서
    '주문번호 키를 가진 dict들의 리스트'를 너비우선으로 찾는 편이 안전하다.
    """
    queue = [payload]
    single = None
    guard = 0
    while queue and guard < 5000:
        node = queue.pop(0)
        guard += 1
        if isinstance(node, list):
            rows = [x for x in node if _looks_like_order(x)]
            if rows:
                return rows
            queue.extend(x for x in node if isinstance(x, (dict, list)))
        elif isinstance(node, dict):
            if single is None and _looks_like_order(node):
                single = node
            queue.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return [single] if single else []


def _flatten(row):
    """주문 행 안에 단품 리스트가 중첩돼 오는 경우 단품 단위로 편다.

    응답이 단품 1행씩 평평하게 오는 문서도 있고, 주문 아래 단품 배열을 두는 문서도 있다.
    어느 쪽이든 '단품 행 + 주문 공통값' 형태로 맞춰 두면 뒤쪽 합치기 로직이 동일하게 돈다.
    """
    children = []
    for key, value in row.items():
        if not isinstance(value, list) or not value:
            continue
        items = [x for x in value if isinstance(x, dict)]
        if not items:
            continue
        # 상품명·수량·단품일련번호 중 하나라도 있으면 단품 리스트로 본다.
        marker = ("prdnm", "productname", "goodsnm", "odqty", "odseq", "sitmno", "saleprc")
        if any(any(k in {str(kk).lower() for kk in item} for k in marker) for item in items):
            children.extend(items)
    if not children:
        return [row]
    parent = {k: v for k, v in row.items() if not isinstance(v, (list, dict))}
    return [{**parent, **child} for child in children]


def _extra_options(line):
    """추가옵션(pdAdtnOptJsn) — JSON 배열(문자열로 오기도 한다)을 옵션 조각으로 편다.

    실측 건은 빈 배열이라 안쪽 모양을 다 모른다 — dict 안의 문자열/숫자 값만 조심스럽게
    이어 붙인다(고도몰 옵션 배열 미파싱으로 챙길 옵션이 빠졌던 사고의 재발 방지)."""
    raw = line.get("pdAdtnOptJsn") if isinstance(line, dict) else None
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text in ("[]", "{}"):
            return []
        try:
            raw = json.loads(text)
        except ValueError:
            return [text[:120]]
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict):
            piece = " ".join(str(v).strip() for v in item.values()
                             if isinstance(v, (str, int, float)) and str(v).strip())
            if piece:
                out.append(piece[:120])
        elif isinstance(item, (str, int, float)) and str(item).strip():
            out.append(str(item).strip()[:120])
    return out


@register
class LotteonAdapter(MallAdapter):
    code = "lotteon"
    name = "롯데온"
    # ★조회기간 1일 초과 불가(체크코드 2003)
    max_window_days = 1
    # 한도는 인증키당 분당 1만 회 수준이라 넉넉하지만, 연속 호출은 조금 띄운다.
    call_interval = 0.3
    # ★레지스트리(app/malls/__init__.py)의 fields key와 정확히 같아야 한다.
    #   거래처번호(vendor_no)는 헤더/바디에 필수가 아니고 화면 표시·검증용이라 필수에서 뺐다.
    #   (필수로 걸면 값이 비었을 때 영원히 '키 대기'가 된다)
    required_keys = ("api_key",)

    def __init__(self, settings):
        super().__init__(settings)
        # 이번 수집에서 읽은 (주문번호, 단품일련번호) — DB 저장이 끝난 뒤
        # mark_collected(adapter.collected_keys)로 연동완료를 통보하는 데 쓴다.
        self.collected_keys = []

    # ---- 공통 호출부
    def _headers(self):
        return {
            "Authorization": f"Bearer {(self.s.get('api_key') or '').strip()}",
            "Accept": "application/json",
            "Accept-Language": "ko",
            "X-Timezone": "GMT+09:00",
            "Content-Type": "application/json",
            "User-Agent": "OWS/1.0",
        }

    def _post(self, path, body, timeout=30):
        url = BASE_URL + path
        try:
            resp = requests.post(url, json=body, headers=self._headers(), timeout=timeout)
        except requests.Timeout as e:
            raise MallError("롯데온 응답이 제한 시간 안에 오지 않았습니다. 잠시 뒤 다시 시도해 주세요.") from e
        except requests.RequestException as e:
            raise MallError(f"롯데온 서버에 연결하지 못했습니다: {e}") from e

        status = resp.status_code
        if status == 401:
            raise MallError("롯데온 인증 실패(401): 인증키가 잘못되었거나 만료되었습니다. "
                            "판매자센터 > 판매자정보 > OpenAPI관리에서 키를 확인/재발급하세요(유효기간 1년).")
        if status == 403:
            raise MallError("롯데온 접근 거부(403): 호출 서버 IP가 등록되지 않았습니다. "
                            "판매자센터 > OpenAPI관리 > 정보설정에서 현재 서버의 고정 IP를 등록하세요.")
        if status == 429:
            raise MallError("롯데온 호출 한도 초과(429): 잠시 뒤 다시 시도하세요. "
                            "반복되면 스토어센터 1:1문의로 한도 증설을 요청해야 합니다.")
        if status == 404:
            raise MallError(f"롯데온 API 경로를 찾을 수 없습니다(404): {path} — API센터에서 경로/버전을 확인하세요.")
        if status >= 500:
            raise MallError(f"롯데온 서버 오류({status})입니다. 잠시 뒤 다시 시도해 주세요.")
        if status != 200:
            head = (resp.text or "")[:200].replace("\n", " ")
            raise MallError(f"롯데온 호출 실패({status}): {head or '응답 본문 없음'}")

        try:
            data = resp.json()
        except ValueError as e:
            head = (resp.text or "")[:200].replace("\n", " ")
            raise MallError(f"롯데온 응답을 해석하지 못했습니다(JSON 아님): {head or '빈 응답'}") from e
        self._check_result(data)
        return data

    def _check_result(self, data):
        """HTTP 200이라도 본문 결과코드가 실패일 수 있어 따로 확인한다."""
        if not isinstance(data, dict):
            return
        # 결과코드/메시지가 최상위에 오기도 하고 header/common 안에 오기도 한다.
        holders = [data]
        for key in ("header", "common", "result", "returnHeader", "responseHeader"):
            value = data.get(key)
            if isinstance(value, dict):
                holders.append(value)
        for holder in holders:
            code = _first(holder, "returnCode", "resultCode", "rtnCode", "code", "chkCd", "checkCode")
            if not code:
                continue
            if str(code).upper() in _OK_CODES:
                continue
            msg = _first(holder, "returnMessage", "resultMessage", "rtnMessage",
                         "message", "msg", "chkMsg", "errorMessage") or "메시지 없음"
            if str(code) == "2003":
                raise MallError("롯데온 조회기간은 1일을 초과할 수 없습니다(체크코드 2003). "
                                "수집 기간을 하루씩 나눠 부르도록 되어 있으니, 이 오류가 보이면 "
                                "서버 시각(GMT+09:00)과 조회 구간 계산을 확인하세요.")
            raise MallError(f"롯데온 오류({code}): {msg}")

    # ---- 주문 수집
    def collect_orders(self, since, until):
        """결제완료~상품준비중 신규 주문(출고지시)을 가져온다. 상태 변경은 하지 않는다."""
        rows = []
        seen = set()
        self.collected_keys = []
        for start, end in self.windows(since, until):
            # 끝 시각에서 1초를 빼 '1일 초과'(2003) 경계를 피한다.
            # 잘려나간 1초는 다음 구간의 시작 시각이라 누락되지 않는다.
            win_end = end - timedelta(seconds=1)
            if win_end < start:
                win_end = start
            # 같은 단품 행이 여러 번 들어와도 한 번만 담는다. 다만 한 구간 안에 똑같이 생긴
            # 행이 정말 2개 있을 수 있으므로(같은 단품 2줄), 구간 내 등장 순번까지 키에 넣어
            # 진짜 중복(구간 사이 겹침)만 걸러낸다.
            local = {}
            for row in self._search_window(start, win_end):
                ident = (_first(row, "odNo", "orderNo", "ordNo", "odNum"),
                         _first(row, "odSeq", "orderSeq", "odSeqNo"),
                         _first(row, "procSeq", "processSeq"),
                         _first(row, "sitmNo", "spdNo", "prdNo", "prdNm"))
                local[ident] = local.get(ident, 0) + 1
                key = ident + (local[ident],)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
        return self._build_orders(rows)

    def _search_window(self, start, end):
        """한 구간(최대 1일)을 조회한다. 총건수가 더 있으면 페이지를 이어 받는다."""
        body = {
            "srchStrtDt": _fmt_api_dt(start),
            "srchEndDt": _fmt_api_dt(end),
            "odPrgsStepCd": STEP_SHIP_ORDER,   # 11=출고지시(=결제완료 후 넘어온 신규)
            "ifCplYN": "",                     # 빈값 = 아직 연동완료 통보를 안 한 신규 건만
        }
        data = self._post(SEARCH_PATH, body)
        rows = _find_rows(data)
        self.pace()

        # 페이징: 파라미터 이름이 문서에 확정돼 있지 않아, 총건수가 받은 건수보다 클 때만
        # 조심스럽게 다음 페이지를 요청한다(설정 page_param으로 이름 교체 가능).
        total = self._total_count(data)
        page_param = (self.s.get("page_param") or "pageNo").strip()
        page = 1
        while total and len(rows) < total and page < 200:
            page += 1
            more = self._post(SEARCH_PATH, {**body, page_param: str(page), "pageSize": "500"})
            new_rows = _find_rows(more)
            self.pace()
            if not new_rows:
                break
            before = len(rows)
            rows.extend(new_rows)
            if len(rows) == before:
                break
            # 서버가 페이지 파라미터를 무시하고 1페이지만 계속 주는 경우를 막는다.
            if new_rows and rows[before:] == rows[:len(new_rows)]:
                del rows[before:]
                break
        return rows

    def _total_count(self, data):
        if not isinstance(data, dict):
            return 0
        for holder in [data] + [v for v in data.values() if isinstance(v, dict)]:
            value = _first(holder, "totCnt", "totalCnt", "totalCount", "ttlCnt", "totCount")
            if value:
                return _int(value)
        return 0

    def _build_orders(self, rows):
        """단품 단위 행들을 주문번호(odNo) 기준으로 합친다.

        롯데온은 한 주문의 단품마다 행(odNo + odSeq)이 따로 오므로 그대로 넣으면
        같은 주문이 여러 건으로 쪼개진다. 대표 상품명 하나 + 나머지는 optionName에 ' / '로 잇는다.
        """
        groups = {}
        order_seq = []
        for raw in rows:
            for row in _flatten(raw):
                # 상태 방어 필터: 요청에 odPrgsStepCd=11을 넣었어도 응답에 다른 단계가 섞여 올 수 있다.
                step = _first(row, "odPrgsStepCd", "odPrgsStepCode", "prgsStepCd", "dlvPrgsStepCd")
                if step and step not in COLLECT_STEPS:
                    continue
                # 이미 연동완료 통보한 건(Y)은 신규가 아니다.
                if _first(row, "ifCplYN", "ifCplYn", "ifComplYn", "ifCplYsno").upper() == "Y":
                    continue
                # 주문(10)만 수집 — 교환/반품/AS 재배송은 별도 흐름에서 다룬다.
                od_type = _first(row, "odTypCd", "odTypeCd", "orderTypeCd")
                if od_type and od_type not in COLLECT_ORDER_TYPES:
                    continue
                od_no = _first(row, "odNo", "orderNo", "ordNo", "odNum")
                if not od_no:
                    continue
                if od_no not in groups:
                    groups[od_no] = []
                    order_seq.append(od_no)
                groups[od_no].append(row)
                # 연동완료 통보는 단품(odNo+odSeq) 단위라 수집한 단품 키를 그대로 모아 둔다.
                # (통보 호출 자체는 여기서 하지 않는다 — mark_collected 주석 참고)
                marker = {"odNo": od_no}
                od_seq = _first(row, "odSeq", "orderSeq", "odSeqNo")
                proc_seq = _first(row, "procSeq", "processSeq")
                if od_seq:
                    marker["odSeq"] = od_seq
                if proc_seq:
                    marker["procSeq"] = proc_seq
                if marker not in self.collected_keys:
                    self.collected_keys.append(marker)

        out = []
        for od_no in order_seq:
            out.append(self._merge(od_no, groups[od_no]))
        return out

    def _merge(self, od_no, lines):
        main = lines[0]
        # ★실측(2026-08-31): 상품명=spdNm, 단품(옵션)명=sitmNm. 옛 후보(prdNm…)는 폴백 유지.
        product_name = _first(main, "spdNm", "prdNm", "prdNmKor", "productName", "goodsNm", "itemNm")
        option_parts = []
        main_option = _first(main, "optNm", "optnNm", "prdOptNm", "optionName", "itemOptNm", "sitmNm")
        if main_option and main_option != product_name:
            option_parts.append(main_option)

        quantity = 0
        amount = 0
        for line in lines:
            qty = _int(_first(line, "odQty", "orderQty", "qty", "dlvQty", "ordQty")) or 1
            quantity += qty
            amount += self._line_amount(line, qty)
            # 추가옵션(pdAdtnOptJsn) — RAM 추가 같은 선택이 실려 오면 옵션에 함께 남긴다
            for extra in _extra_options(line):
                if extra not in option_parts:
                    option_parts.append(extra)
            if line is main:
                continue
            name = _first(line, "spdNm", "prdNm", "prdNmKor", "productName", "goodsNm", "itemNm")
            opt = _first(line, "optNm", "optnNm", "prdOptNm", "optionName", "itemOptNm", "sitmNm")
            label = " ".join(x for x in [name, f"({opt})" if opt and opt != name else ""] if x)
            if label and label != product_name and label not in option_parts:
                option_parts.append(label)

        # 수취인 정보는 주문 공통이지만 행마다 비어 올 수 있어 값이 있는 첫 행에서 집는다.
        # ★실측: 배송지(dvp*) 계열이 진짜 수취인이고, odrNm/mphnNo 는 주문자(마지막 폴백).
        recipient = self._pick(lines, "dvpCustNm", "rcvrNm", "rcvrName", "receiverName",
                               "dlvNm", "rcvNm", "odrNm")
        phone = self._pick(lines, "dvpMphnNo", "rcvrPrtblNo", "rcvrMpNo", "rcvrCellNo",
                           "receiverPhone", "rcvrTelNo", "rcvrHpNo", "dvpTelNo", "mphnNo")
        postal = self._pick(lines, "dvpZipNo", "rcvrZipNo", "rcvrZipCd", "rcvrZipcode",
                            "zipNo", "rcvrPostNo")
        base_addr = self._pick(lines, "dvpStnmZipAddr", "rcvrBaseAddr", "rcvrRoadBaseAddr",
                               "rcvrAddr", "baseAddr", "receiverAddress")
        detail_addr = self._pick(lines, "dvpStnmDtlAddr", "rcvrDtlAddr", "rcvrRoadDtlAddr",
                                 "dtlAddr", "receiverAddressDetail")
        message = self._pick(lines, "dvMsg", "dlvMsg", "dlvMemo", "deliveryMessage", "shipMsg",
                             "rcvrMsg", "dlvReqCn")
        ordered_at = self._pick(lines, "odCmptDttm", "odDtm", "orderDtm", "odYmd", "odDt",
                                "orderDate", "pymCmptDtm", "regDtm", "owhoDttm", "sndInstDtm")

        return self.order(
            importKey=f"롯데온:{od_no}",
            orderNumber=od_no,
            orderedAt=_fmt_dt(ordered_at),
            productName=product_name,
            optionName=" / ".join(option_parts),
            # 판매자상품코드(자사 제품코드 축)만 제품코드 칸에 넣는다. ★실측:
            # epdNo="X13 Gen3_i5-12_내장 AA" — 우리 제품코드 축 그대로.
            # ★롯데온 내부번호(spdNo "LO…"·sitmNo·prdNo)는 여기 안 넣는다(2026-09-01 대표
            #   "롯데온 고유코드는 필요하지 않아") — 제품코드 칸은 고도몰 스펙·자산 재고
            #   매칭의 열쇠라 몰 내부번호가 박히면 매칭만 막는다. 내부번호는 아래
            #   mallProductId/mallItemId(몰 상품 식별자 축)로 보존한다.
            # ★등급 꼬리(" AA")는 여기서 떼지 않는다(적대 리뷰) — 롯데온은 상품명(spdNm=소매
            #   제목)·옵션 어디에도 등급이 없어 이 꼬리가 등급의 유일한 운반체다(떼면 영구
            #   소실). 고도몰 스펙·재고 대조는 product_info 의 sku_of/폴백이 꼬리를 떼어
            #   맞추고, 송장 요구등급(_order_grade)은 이 꼬리를 그대로 읽는다.
            productCode=self._pick(lines, "epdNo", "vndPrdCd", "sellerPrdCd", "prtnPrdCd"),
            mallProductId=self._pick(lines, "spdNo", "prdNo"),
            mallItemId=self._pick(lines, "sitmNo"),
            quantity=max(1, quantity),
            amount=amount,
            recipient=recipient,
            phone=phone,
            postalCode=postal,
            # 2020-05-13부터 지번주소는 안 내려오고 도로명 기준만 온다.
            address=" ".join(x for x in [base_addr, detail_addr] if x),
            deliveryMessage=message,
        )

    def _line_amount(self, line, qty):
        """단품 금액 — ★실측(2026-08-31 실주문)으로 확정.

        실키: slAmt=판매금액(합계) / slPrc=판매단가 / actualAmt=고객 실결제 /
              fvrAmtSum=혜택 합 = 업체 분담(prEntpShrAmtSum) + 롯데 분담(prSfcoShrAmtSum)+α.
        매출 = 판매금액 − '우리(업체) 분담 할인'만. 롯데 분담 쿠폰은 정산에서 보전되므로
        빼지 않는다(고도몰 간편결제 자사포인트 사고와 같은 기준). actualAmt(고객 실결제)는
        롯데 분담분까지 빠져 있어 매출로 쓰면 과소로 잡힌다.
        """
        paid = _int(_first(line, "rlPayAmt", "realPayAmt", "pymAmt", "payAmt",
                           "totPayAmt", "sumAmt", "odAmt"))
        if paid:
            return paid
        entp_share = _int(_first(line, "prEntpShrAmtSum", "entpShrAmt"))
        total = _int(_first(line, "slAmt"))
        if total:
            return max(0, total - entp_share)
        unit = _int(_first(line, "slPrc", "salePrc", "salePrice", "prdPrc", "unitPrc", "sellPrc"))
        return max(0, unit * max(1, qty) - entp_share)

    @staticmethod
    def _pick(lines, *names):
        for line in lines:
            value = _first(line, *names)
            if value:
                return value
        return ""

    # ---- 연동완료 통보 (수집과 분리)
    def mark_collected(self, order_nos):
        """★수집한 주문을 '연동완료'로 통보해 롯데온 상태를 상품준비중으로 넘긴다.

        collect_orders에서 자동으로 부르지 않는다. 통보를 먼저 하고 우리 DB 저장이 실패하면
        롯데온에선 준비중인데 우리 쪽엔 주문이 없는 상태가 되기 때문이다.
        저장 트랜잭션 커밋 이후에 adapter.collected_keys(또는 주문번호 목록)로 호출할 것.
        반대로 통보를 계속 안 하면 고객이 즉시취소할 수 있는 상태로 남으니 배선은 꼭 필요하다.

        order_nos 항목은 "주문번호" 문자열, (주문번호, 단품일련번호) 튜플,
        {"odNo":…, "odSeq":…} dict 모두 받는다.
        """
        items = []
        for entry in order_nos or []:
            if isinstance(entry, dict):
                od_no = _first(entry, "odNo", "orderNo", "orderNumber")
                od_seq = _first(entry, "odSeq", "orderSeq")
                proc_seq = _first(entry, "procSeq", "processSeq")
            elif isinstance(entry, (list, tuple)):
                parts = list(entry) + ["", "", ""]
                od_no, od_seq, proc_seq = (str(parts[0] or ""), str(parts[1] or ""),
                                           str(parts[2] or ""))
            else:
                od_no, od_seq, proc_seq = str(entry or ""), "", ""
            if not od_no:
                continue
            item = {"odNo": od_no, "ifCplYN": "Y"}
            if od_seq:
                item["odSeq"] = od_seq
            if proc_seq:
                item["procSeq"] = proc_seq
            items.append(item)
        if not items:
            return 0

        path = (self.s.get("mark_path") or MARK_PATH).strip()
        sent = 0
        for chunk in _chunks(items, BATCH_LIMIT):
            data = self._post(path, {"odList": chunk})
            self._raise_fail_list(data, "연동완료 통보")
            sent += len(chunk)
            self.pace()
        return sent

    # ---- 송장 전송
    def upload_invoice(self, order_no, invoice_no, courier_code="", **kw):
        """배송상태 통보 V2로 발송완료(13) + 운송장번호를 올린다.

        실제 배선은 나중에 한다(호출 즉시 롯데온 주문이 발송완료로 바뀌는 부수효과가 있다).
        ★이 API로는 운송장 수정/삭제가 안 된다. 잘못 보냈으면 '운송장수정 통보'(apiNo=139)를 써야 한다.
        ★실패 건을 같은 내용으로 반복 전송하면 IP 수신차단 대상이 되므로, 실패는 원인을 보고 고친 뒤 재전송할 것.
        """
        order_no = str(order_no or "").strip()
        numbers = [str(n).strip() for n in (invoice_no if isinstance(invoice_no, (list, tuple))
                                            else [invoice_no]) if str(n or "").strip()]
        if not order_no or not numbers:
            raise MallError("롯데온 송장 전송: 주문번호와 운송장번호가 필요합니다.")
        too_long = [n for n in numbers if len(n) > INVOICE_NO_MAXLEN]
        if too_long:
            raise MallError(f"롯데온 송장 전송: 운송장번호는 {INVOICE_NO_MAXLEN}자를 넘을 수 없습니다({too_long[0]}).")
        dv_co_cd = (courier_code or self.s.get("courier_code") or "").strip()
        if not dv_co_cd:
            # 택배사 코드를 임의로 넣으면 엉뚱한 택배사로 등록되므로 추측하지 않는다.
            # (CJ대한통운 코드는 롯데온 택배사코드표에서 확인해 설정 courier_code에 넣을 것)
            raise MallError("롯데온 송장 전송: 택배사 코드(dvCoCd)가 필요합니다. "
                            "롯데온 택배사코드표에서 확인한 값을 넘겨주세요.")

        item = {
            "odNo": order_no,
            "odPrgsStepCd": STEP_SHIPPED,          # 13=발송완료
            "dvCoCd": dv_co_cd,
            "invcNbr": str(len(numbers)),          # 송장 '개수'(번호가 아니다)
            "invcNoList": numbers,
            "dvTrcStatDttm": _fmt_api_dt(config.now()),
        }
        od_seq = str(kw.get("od_seq") or kw.get("odSeq") or "").strip()
        proc_seq = str(kw.get("proc_seq") or kw.get("procSeq") or "").strip()
        if od_seq:
            item["odSeq"] = od_seq
        if proc_seq:
            item["procSeq"] = proc_seq

        data = self._post(INVOICE_PATH, {"odList": [item]})
        # 부분 실패는 HTTP 200 + returnCode 0000 + failList로 온다 — 성공으로 오해하면 안 된다.
        self._raise_fail_list(data, "송장 전송")
        return True

    def _raise_fail_list(self, data, what):
        fails = []
        queue = [data]
        guard = 0
        while queue and guard < 2000:
            node = queue.pop(0)
            guard += 1
            if isinstance(node, dict):
                for key, value in node.items():
                    if str(key).lower() in ("faillist", "failedlist", "errorlist") \
                            and isinstance(value, list):
                        fails.extend(x for x in value if isinstance(x, dict))
                    elif isinstance(value, (dict, list)):
                        queue.append(value)
            elif isinstance(node, list):
                queue.extend(x for x in node if isinstance(x, (dict, list)))
        if not fails:
            return
        detail = "; ".join(
            f"{_first(f, 'odNo', 'orderNo') or '?'}: "
            f"{_first(f, 'failMsg', 'message', 'msg', 'returnMessage', 'errorMessage') or '사유 미표기'}"
            for f in fails[:5])
        more = f" 외 {len(fails) - 5}건" if len(fails) > 5 else ""
        raise MallError(f"롯데온 {what} 일부 실패({len(fails)}건): {detail}{more}")


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]
