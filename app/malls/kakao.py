"""카카오쇼핑(톡스토어 + 선물하기) 주문 수집.

- Base: https://kapi.kakao.com  (샌드박스 없음 — 운영 환경으로 바로 호출한다)
- 인증: OAuth가 아니라 '3중 헤더'다. 셋 중 하나만 틀려도 전부 실패한다.
    Authorization:        KakaoAK {연동대행사 앱 ADMIN 키}
    Target-Authorization: KakaoAK {판매자 REST API 키}
    channel-ids:          101(톡스토어) / 1(선물하기) / "1,101"(둘 다)
  ★연동대행사 앱과 판매자 앱은 서로 다른 카카오계정/앱이어야 한다. 같은 앱 키를 두 헤더에
    넣으면 카카오가 code=-2로 거절한다 → _raise_api_error에서 이 경우를 제일 먼저 안내한다
    (대표가 가장 많이 밟는 함정이라 호출 전에도 한 번 걸러 준다).
- 주문조회: GET /v2/shopping/orders (변경일시 기준 v2)
    ★조회기간 최대 1일(24시간) → max_window_days = 1. 요청당 최대 100건,
    lastOrderId + lastModifiedAt 커서 페이징.
- 수집 대상: ShippingWaiting(배송준비중). 배송중/배송완료/구매확정/클레임 건은 담지 않는다.
- 송장전송: POST /v1/shopping/orders/deliveries/invoices (요청당 최대 100건, 비동기 처리)

몰별 함정(키 발급 전에 미리 코드에 반영해 둔 것)
 1) 조회창이 24시간뿐이라 며칠치를 요청하면 self.windows()가 하루 단위로 쪼개 여러 번 부른다.
    windows()가 만든 구간은 정확히 24시간이라 경계에서 '기간 초과'로 거절당할 수 있어
    끝을 1초 당겨 보낸다(다음 구간 시작이 그 시각이라 누락은 없다).
 2) 응답 JSON 키 이름이 문서 개정마다 다르게 적혀 있다(products/productList,
    receiver/shippingAddress 등). 후보 키를 순서대로 찾는 _pick/_node 헬퍼로 읽는다.
    한 키만 믿고 짜면 키 발급 후 "연동은 됐는데 값이 전부 빈칸"이 된다.
 3) 날짜 파라미터 이름도 문서판마다 startDate/endDate와 lastModifiedAt 계열이 섞여 있다.
    기본은 startDate/endDate로 부르고, 카카오가 '파라미터 이름/형식' 문제로 400을 주면
    다음 후보로 바꿔 재시도한다. 한 번 성공하면 그 조합으로 고정한다.
 4) 레이트리밋 수치가 공개돼 있지 않다. 호출 사이 pace()를 두고 429/5xx는 백오프 재시도.
 5) 송장 등록이 비동기라 응답은 사실상 상태코드뿐이다. 반영 확인은 최소 3초 뒤에 재조회.
 6) 인코딩 함정은 없다(전부 UTF-8 JSON). 대신 일시가 ISO8601/epoch로 섞여 와서
    _dt_text로 'YYYY-MM-DD HH:MM:SS'(KST)로 통일한다 — 엑셀 임포터·중복판정과 같은 모양.

★현재는 코드만 준비된 상태다(실호출하지 않았다). 키 발급 후 설정 화면의 [연결 테스트]로 확인한다.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import requests

from .. import config
from .base import MallAdapter, MallError, register

BASE_URL = "https://kapi.kakao.com"
ORDERS_PATH = "/v2/shopping/orders"                       # 변경 주문내역 조회 v2
INVOICE_PATH = "/v1/shopping/orders/deliveries/invoices"  # 배송상품 발송처리
COMPANIES_PATH = "/v1/shopping/delivery/companies"        # 택배사 코드 조회

TIMEOUT = 25          # 지시 기준(20초 이상)보다 여유를 둔다
PAGE_SIZE = 100       # 요청당 최대 100건
MAX_PAGES = 60        # 커서 페이징 무한루프 방지(하루치 6,000건이면 충분)
MAX_RETRY = 2         # 429/5xx 백오프 재시도 횟수
DEFAULT_COURIER = "CJGLS"  # CJ대한통운 — OWS 출고는 전부 CJ라 기본값으로 둔다

# ---- 수집 대상 주문상태 -------------------------------------------------------
# 카카오 OrderStatus: ShippingWaiting / ShippingProgress / ShippingComplete /
#                     ReturnRequest / ExchangeRequest / PurchaseDecision / Cancel... 등
# 우리가 담는 건 '결제완료~상품준비중'뿐이다. 채널(선물하기)에 따라 결제완료 단계를 다른
# 이름으로 내려주는 사례가 있어 동의어 후보를 함께 둔다(비교는 소문자·구분자 제거 후).
COLLECT_STATUSES = (
    "ShippingWaiting",     # 배송준비중 = 결제완료 직후 (기본 수집 대상)
    "PaymentComplete",     # 결제완료 (동의어 후보)
    "PayComplete",
    "OrderComplete",
    "ShippingReady",
    "DeliveryReady",
)

ORDER_ID_KEYS = ("orderId", "orderNumber", "orderNo", "id")
STATUS_KEYS = ("orderStatus", "status", "orderStatusCode", "orderState", "deliveryStatus")
ORDERED_AT_KEYS = ("orderedAt", "orderDate", "orderedDate", "orderDateTime",
                   "paymentDate", "paidAt", "paymentedAt", "createdAt", "regDate")
MODIFIED_AT_KEYS = ("lastModifiedAt", "modifiedAt", "updatedAt", "lastUpdatedAt", "changedAt")
PRODUCTS_KEYS = ("products", "productList", "orderProducts", "orderProductList",
                 "items", "orderItems", "goods")
ORDERS_KEYS = ("orders", "orderList", "elements", "contents", "list", "results")

# 날짜 파라미터 후보 — (시작 키, 끝 키, 날짜 포맷). 앞에서부터 시도한다.
DATE_PARAM_STYLES = (
    ("startDate", "endDate", "offset"),
    ("startDate", "endDate", "naive"),
    ("lastModifiedAtFrom", "lastModifiedAtTo", "offset"),
    ("startModifiedAt", "endModifiedAt", "offset"),
)

# 400 응답이 '날짜 파라미터 이름/형식' 문제로 보이는지 판단할 때 쓰는 힌트 단어.
_PARAM_HINTS = ("startdate", "enddate", "lastmodified", "modifiedat",
                "parameter", "param", "argument", "파라미터", "필수", "인자", "형식")


class _ParamStyleError(Exception):
    """날짜 파라미터 조합이 안 맞아 보일 때 내부에서만 쓰는 신호(밖으로 새지 않는다)."""


# ---- 응답 읽기 헬퍼 -----------------------------------------------------------
def _s(value) -> str:
    return "" if value is None else str(value).strip()


def _pick(node, *keys) -> str:
    """dict에서 후보 키를 순서대로 찾아 첫 값을 문자열로 돌려준다.

    문서판마다 키 이름이 달라서(productName/itemName 등) 한 키만 믿으면 값이 통째로 빈다.
    """
    if not isinstance(node, dict):
        return ""
    for key in keys:
        value = node.get(key)
        if isinstance(value, (str, int, float)) and _s(value):
            return _s(value)
    return ""


def _node(node, *keys) -> dict:
    """dict 안의 하위 dict를 후보 키 순서대로 찾는다(없으면 빈 dict)."""
    if not isinstance(node, dict):
        return {}
    for key in keys:
        value = node.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def _list(node, *keys) -> list:
    """dict 안의 dict 목록을 후보 키 순서대로 찾는다."""
    if not isinstance(node, dict):
        return []
    for key in keys:
        value = node.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def _int(value) -> int:
    try:
        return int(float(_s(value).replace(",", "") or 0))
    except (TypeError, ValueError):
        return 0


def _norm_status(value) -> str:
    """상태 비교용 정규화 — 대소문자/언더바/공백 차이를 없앤다."""
    return _s(value).replace("_", "").replace("-", "").replace(" ", "").casefold()


_COLLECT_SET = {_norm_status(x) for x in COLLECT_STATUSES}


def _line_key(product) -> tuple:
    """상품줄 식별키 — 같은 줄을 두 번 합치지 않기 위한 것.

    카카오가 상품주문번호를 주면 그걸 쓰고, 없으면 내용(상품명·옵션·수량·금액)으로 만든다.
    같은 상품을 정말로 두 줄에 나눠 주는 경우는 카카오가 수량으로 합쳐 주므로 사실상 없다.
    """
    product_order_id = _pick(product, "productOrderId", "orderProductId",
                             "productOrderNumber", "orderProductNo")
    if product_order_id:
        return ("id", product_order_id)
    return ("content",
            _pick(product, "productName", "name", "title", "itemName", "goodsName"),
            _pick(product, "optionName", "optionTitle", "option", "optionValue"),
            _pick(product, "quantity", "qty", "count", "orderQuantity"),
            _pick(product, "totalPrice", "price", "salePrice", "paymentAmount"),
            _pick(product, "sellerProductCode", "sellerProductId", "productCode",
                  "productId", "channelProductId", "itemId"))


def _dt_text(value) -> str:
    """카카오가 주는 일시를 'YYYY-MM-DD HH:MM:SS'(KST)로 맞춘다.

    엑셀 임포터와 중복판정(dedupe)이 이 모양을 기준으로 동작한다. 형식을 통일해야
    같은 주문이 엑셀/API 양쪽으로 들어와도 한 건으로 묶인다.
    """
    text = _s(value)
    if not text:
        return ""
    if text.isdigit() and len(text) in (10, 13):  # epoch(초/밀리초)로 주는 판도 있다
        seconds = int(text) / (1000 if len(text) == 13 else 1)
        return datetime.fromtimestamp(seconds, config.KST).strftime("%Y-%m-%d %H:%M:%S")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text  # 해석 못 하면 원문 유지(중복판정이 문자열 비교로 처리한다)
    if dt.tzinfo is not None:
        dt = dt.astimezone(config.KST)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _kst(dt: datetime) -> datetime:
    """tz 없는 datetime이 들어와도 KST로 본다(OWS는 config.now()=KST 기준)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=config.KST)
    return dt.astimezone(config.KST)


def _fmt_dt(dt: datetime, style: str) -> str:
    dt = _kst(dt)
    if style == "naive":
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    return dt.isoformat(timespec="seconds")  # 2026-07-28T09:00:00+09:00


def _looks_like_param_problem(message: str) -> bool:
    low = _s(message).casefold()
    return any(hint in low for hint in _PARAM_HINTS)


@register
class KakaoAdapter(MallAdapter):
    code = "kakao"
    name = "카카오쇼핑"
    # ★카카오는 주문조회 창이 최대 1일(24시간)이다. 이 값을 늘리면 조회가 통째로 실패한다.
    max_window_days = 1
    # 공식 레이트리밋 수치가 공개돼 있지 않아 보수적으로 잡는다.
    call_interval = 0.6
    # ★레지스트리(app/malls/__init__.py)의 fields key와 철자가 정확히 같아야 한다.
    #   오타가 나면 값을 넣어도 영원히 '키 대기' 상태가 된다.
    required_keys = ("agency_admin_key", "seller_rest_key", "channel_ids")

    def __init__(self, settings):
        super().__init__(settings)
        self._sess = None
        self._style_idx = 0          # 현재 쓰는 날짜 파라미터 조합
        self._style_locked = False   # 한 번 성공하면 고정(이후 400은 진짜 오류로 본다)
        # 문서를 확인해 파라미터 이름을 아는 경우 설정에 직접 넣으면 탐색 없이 그걸 쓴다.
        if _s(self.s.get("date_from_param")) and _s(self.s.get("date_to_param")):
            self._style_locked = True

    # ---- 인증/세션 -----------------------------------------------------------
    def _channel_ids(self) -> str:
        """channel-ids 헤더 값 정리 — '101, 1' 같은 입력을 '101,1'로 다듬는다."""
        raw = _s(self.s.get("channel_ids")).replace(";", ",").replace(" ", ",")
        ids = []
        for part in raw.split(","):
            part = part.strip()
            if part and part not in ids:
                ids.append(part)
        if not ids:
            raise MallError("채널 ID가 비어 있습니다. 설정에서 101(톡스토어) 또는 1(선물하기), "
                            "둘 다면 1,101 을 입력하세요.")
        unknown = [x for x in ids if x not in ("1", "101")]
        if unknown:
            raise MallError(f"채널 ID 값이 올바르지 않습니다: {', '.join(unknown)} "
                            "— 101(톡스토어) / 1(선물하기)만 사용합니다.")
        return ",".join(ids)

    def _headers(self) -> dict:
        agency = _s(self.s.get("agency_admin_key"))
        seller = _s(self.s.get("seller_rest_key"))
        # ★같은 앱 키를 양쪽에 넣으면 카카오가 -2로 거절한다. 호출 전에 미리 잡아 준다.
        if agency and agency == seller:
            raise MallError(
                "연동대행사 ADMIN 키와 판매자 REST API 키가 같은 값입니다. 카카오쇼핑은 "
                "연동대행사 앱과 판매자 앱이 서로 다른 카카오계정/앱이어야 하며, 같은 앱 키를 "
                "두 헤더(Authorization / Target-Authorization)에 함께 쓰면 -2 오류가 납니다. "
                "판매자 키는 판매자센터 > 정보관리 > 판매채널 정보 > API 인증키에서 확인하세요.")
        return {
            "Authorization": f"KakaoAK {agency}",
            "Target-Authorization": f"KakaoAK {seller}",
            "channel-ids": self._channel_ids(),
            "Accept": "application/json",
            "User-Agent": "OWS/1.0",
        }

    def _session(self):
        if self._sess is None:
            self._sess = requests.Session()
        return self._sess

    # ---- HTTP ----------------------------------------------------------------
    def _call(self, method, path, params=None, json_body=None, retry=0, probe=False):
        """공통 호출. probe=True인 주문조회에서만 날짜 파라미터 후보 전환을 허용한다.

        (송장 전송 같은 다른 호출에서 내부 신호가 밖으로 새면 사용자에게
         엉뚱한 메시지가 보이므로 여기서 경로를 갈라 둔다.)
        """
        url = BASE_URL + path
        try:
            resp = self._session().request(
                method, url, params=params, json=json_body,
                headers=self._headers(), timeout=TIMEOUT)
        except requests.Timeout as e:
            raise MallError(f"카카오쇼핑 응답이 {TIMEOUT}초 안에 오지 않았습니다. "
                            "잠시 후 다시 시도하세요.") from e
        except requests.RequestException as e:
            raise MallError(f"카카오쇼핑 호출 실패(네트워크): {e}") from e

        # 429(호출 한도)·5xx(일시 장애)는 잠깐 쉬었다가 다시 시도한다.
        if resp.status_code in (429, 500, 502, 503, 504) and retry < MAX_RETRY:
            time.sleep(self.call_interval * (retry + 1) * 3)
            return self._call(method, path, params=params, json_body=json_body,
                              retry=retry + 1, probe=probe)

        if resp.status_code >= 400:
            self._raise_api_error(resp, probe=probe)

        if not (resp.content or b"").strip():
            return {}  # 발송처리 등은 본문 없이 상태코드만 온다
        try:
            data = resp.json()
        except ValueError as e:
            head = (resp.text or "")[:200]
            raise MallError(f"카카오쇼핑 응답을 해석하지 못했습니다: {head}") from e
        # 본문 안에 오류코드를 담아 200으로 주는 경우도 있어 한 번 더 본다.
        if isinstance(data, dict):
            self._check_body_error(data)
        if probe:
            self._style_locked = True  # 이 조합으로 성공 — 이후엔 파라미터를 더 바꾸지 않는다
        return data

    def _check_body_error(self, data):
        code = _s(data.get("code", data.get("resultCode", data.get("errorCode"))))
        if not code or code in ("0", "200", "OK", "SUCCESS"):
            return
        message = _s(data.get("msg") or data.get("message") or data.get("errorMessage"))
        if code == "-2":
            raise MallError(
                "카카오쇼핑 인증 조합 오류(-2). 연동대행사 ADMIN 키와 판매자 REST API 키를 "
                "각각 맞게 넣었는지 확인하세요. 같은 앱의 키를 두 헤더에 함께 쓰면 이 오류가 "
                f"납니다. (응답: {message or '메시지 없음'})")
        raise MallError(f"카카오쇼핑 오류({code}): {message or '메시지 없음'}")

    def _raise_api_error(self, resp, probe=False):
        """HTTP 오류를 원인별 한국어 메시지로 바꾼다(인증 / IP·권한 / 한도 구분)."""
        code = None
        message = ""
        try:
            data = resp.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            code = data.get("code", data.get("errorCode"))
            message = _s(data.get("msg") or data.get("message") or data.get("errorMessage"))
        if not message:
            message = (resp.text or "")[:200]
        tail = (f" (HTTP {resp.status_code}"
                f"{f', code {code}' if code is not None else ''}: {message or '메시지 없음'})")

        # ★-2: 연동대행사 키와 판매자 키를 잘못 넣은 대표 사례.
        #   다만 카카오 공통 규격에서 -2는 '필수 인자 오류'로도 쓰여, 메시지 내용으로 갈라준다.
        if _s(code) == "-2":
            if probe and not self._style_locked and _looks_like_param_problem(message):
                raise _ParamStyleError(message)
            raise MallError(
                "카카오쇼핑 인증 조합 오류(-2). 연동대행사 ADMIN 키와 판매자 REST API 키를 "
                "각각 맞게 넣었는지 확인하세요. 같은 앱의 키를 두 헤더(Authorization / "
                "Target-Authorization)에 함께 쓰면 이 오류가 납니다. 요청 파라미터가 원인일 "
                "수도 있습니다." + tail)

        if resp.status_code == 401 or _s(code) in ("-401", "401"):
            raise MallError("카카오쇼핑 인증 실패(401). ADMIN 키/판매자 REST API 키 값과 "
                            "'KakaoAK ' 접두사, 판매자센터에서 키를 재발급하지 않았는지 "
                            "확인하세요." + tail)
        if resp.status_code == 403 or _s(code) in ("-403", "403"):
            raise MallError("카카오쇼핑 권한 오류(403). ①앱에 등록한 허용 IP 목록에 이 서버 IP가 "
                            "들어 있는지 ②연동대행사-판매자 연결(POST /v1/store/register)이 "
                            "끝났는지 ③channel-ids(101 톡스토어 / 1 선물하기)가 실제 입점 채널과 "
                            "맞는지 확인하세요." + tail)
        if resp.status_code == 429 or _s(code) in ("-10", "-102"):
            raise MallError("카카오쇼핑 호출 한도 초과(429). 잠시 후 다시 시도하거나 조회 기간을 "
                            "줄이세요." + tail)
        if resp.status_code == 404:
            raise MallError("카카오쇼핑 엔드포인트를 찾지 못했습니다(404). API 버전/경로가 바뀌었을 "
                            "수 있습니다." + tail)
        if resp.status_code >= 500:
            raise MallError("카카오쇼핑 서버 일시 오류입니다. 잠시 후 다시 시도하세요." + tail)
        if (resp.status_code == 400 and probe and not self._style_locked
                and _looks_like_param_problem(message)):
            raise _ParamStyleError(message)
        raise MallError("카카오쇼핑 요청이 거절됐습니다." + tail)

    # ---- 주문 수집 -----------------------------------------------------------
    def collect_orders(self, since, until):
        """변경일시 기준으로 '배송준비중' 주문을 모아 온다.

        조회창이 24시간이라 self.windows()가 하루 단위로 잘라 준다. 같은 주문번호가
        여러 상품행/여러 페이지로 흩어져 와도 마지막에 한 건으로 합친다.
        """
        raw_rows = []
        for start, end in self.windows(since, until):
            raw_rows.extend(self._fetch_window(start, end))
            self.pace()
        return self._merge_rows(raw_rows)

    def _date_params(self, start, end) -> dict:
        """조회 구간 파라미터 — 24시간 경계를 아슬아슬하게 치지 않도록 끝에서 1초를 뺀다.

        windows()가 만든 구간은 정확히 24시간이라 몰이 '기간 초과'로 볼 수 있다.
        끝을 1초 당겨도 다음 구간의 시작이 그 시각이라 주문이 누락되지 않는다.
        """
        from_key = _s(self.s.get("date_from_param"))
        to_key = _s(self.s.get("date_to_param"))
        style = _s(self.s.get("date_format")) or "offset"
        if not (from_key and to_key):
            from_key, to_key, style = DATE_PARAM_STYLES[self._style_idx]
        if _kst(end) - _kst(start) >= timedelta(days=1):
            end = end - timedelta(seconds=1)
        return {from_key: _fmt_dt(start, style), to_key: _fmt_dt(end, style)}

    def _get_orders_page(self, start, end, cursor):
        """한 페이지 조회 — 날짜 파라미터 조합이 안 맞으면 다음 후보로 바꿔 재시도한다."""
        while True:
            params = self._date_params(start, end)
            params["size"] = str(PAGE_SIZE)
            # 서버에서 1차로 걸러 받고(전송량 절감), 응답에서 한 번 더 확인한다.
            params["orderStatus"] = _s(self.s.get("order_status")) or COLLECT_STATUSES[0]
            params.update(cursor)
            try:
                return self._call("GET", ORDERS_PATH, params=params, probe=True)
            except _ParamStyleError as e:
                if self._style_idx + 1 >= len(DATE_PARAM_STYLES):
                    raise MallError(
                        "카카오쇼핑 주문조회 파라미터를 서버가 받지 않습니다. 문서의 조회 파라미터 "
                        f"이름/형식을 확인해 주세요(마지막 응답: {e}). 설정에 "
                        "date_from_param / date_to_param 을 직접 지정할 수도 있습니다.") from e
                self._style_idx += 1
                self.pace()

    def _fetch_window(self, start, end) -> list:
        """24시간 구간 하나를 커서 페이징으로 끝까지 읽는다."""
        rows = []
        cursor = {}
        for _page in range(MAX_PAGES):
            data = self._get_orders_page(start, end, cursor)
            page = self._orders_of(data)
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                break
            if isinstance(data, dict) and data.get("hasNext") is False:
                break
            nxt = self._next_cursor(data, page)
            if not nxt or nxt == cursor:
                break  # 커서가 안 움직이면 같은 페이지를 무한히 읽게 되므로 중단
            cursor = nxt
            self.pace()
        else:
            raise MallError(f"카카오쇼핑 주문이 너무 많아 {MAX_PAGES}페이지에서 멈췄습니다. "
                            "조회 기간을 더 짧게 잡아 다시 수집하세요.")
        return rows

    def _orders_of(self, data) -> list:
        """응답에서 주문 배열을 꺼낸다(감싸는 키가 문서판마다 다르다)."""
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        rows = _list(data, *ORDERS_KEYS)
        if rows:
            return rows
        inner = _node(data, "data", "result", "body")
        return _list(inner, *ORDERS_KEYS) if inner else []

    def _next_cursor(self, data, page) -> dict:
        """다음 페이지 커서(lastOrderId + lastModifiedAt)를 만든다.

        서버가 커서를 직접 내려주면 그 값을, 없으면 마지막 주문에서 뽑아 쓴다.
        """
        top = data if isinstance(data, dict) else {}
        last_id = _pick(top, "lastOrderId", "nextLastOrderId", "nextCursor", "cursor")
        last_at = _pick(top, "lastModifiedAt", "nextLastModifiedAt")
        if not (last_id or last_at) and page:
            tail = page[-1]
            last_id = _pick(tail, *ORDER_ID_KEYS)
            last_at = _pick(tail, *MODIFIED_AT_KEYS)
        cursor = {}
        if last_id:
            cursor["lastOrderId"] = last_id
        if last_at:
            cursor["lastModifiedAt"] = last_at
        return cursor

    # ---- 파싱/병합 -----------------------------------------------------------
    def _merge_rows(self, raw_rows) -> list:
        """주문번호 단위로 합친다.

        한 주문이 여러 상품행(products[])으로 오고, 페이징 때문에 같은 주문번호가 두 번
        나올 수도 있다. 대표 상품명 하나만 productName에 두고 나머지는 optionName에
        " / "로 잇는다(엑셀 임포터와 같은 규칙이라 화면에서 상품 블록으로 다시 풀린다).

        ★같은 상품줄을 두 번 더하지 않도록 줄 단위로 중복을 거른다.
          변경일시 기준 조회라 하루 이상을 수집하면 이틀 연속 수정된 주문이 두 구간에
          모두 잡히고, 커서 경계에서도 같은 주문이 다시 나올 수 있다. 그대로 합치면
          수량·금액이 2배가 된다(실제 출고 수량이 틀어지는 사고).
        """
        merged = {}
        order_seq = []
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            order_no = _pick(row, *ORDER_ID_KEYS)
            if not order_no:
                continue
            # 상품 배열이 없는 응답(주문 1건=1행)도 있어 주문 자체를 한 줄로 본다.
            products = _list(row, *PRODUCTS_KEYS) or [row]
            order_status = _pick(row, *STATUS_KEYS)
            kept, saw_status = [], bool(order_status)
            for product in products:
                status = _pick(product, *STATUS_KEYS) or order_status
                if status:
                    saw_status = True
                    if _norm_status(status) not in _COLLECT_SET:
                        continue  # 배송중/배송완료/구매확정/취소·반품·교환은 담지 않는다
                kept.append(product)
            if not kept:
                continue

            acc = merged.get(order_no)
            if acc is None:
                acc = {"lines": [], "row": row, "partial": False, "unknown_status": False,
                       "seen": set()}
                merged[order_no] = acc
                order_seq.append(order_no)
            for product in kept:
                key = _line_key(product)
                if key in acc["seen"]:
                    continue  # 이미 담은 상품줄(같은 주문이 다시 내려온 경우)
                acc["seen"].add(key)
                acc["lines"].append(product)
            if len(kept) < len(products):
                acc["partial"] = True   # 일부 상품만 담김 → 주문 총액을 쓰면 과대계상된다
            if not saw_status:
                # 상태 키를 아예 못 찾은 응답이다. 조용히 0건이 되는 것보다 담고 알리는 게 낫다.
                acc["unknown_status"] = True

        return [self._build_order(no, merged[no]) for no in order_seq]

    def _build_order(self, order_no, acc) -> dict:
        row = acc["row"]
        lines = acc["lines"]
        main = lines[0]

        names, options = [], []
        quantity = 0
        line_sum = 0
        for product in lines:
            names.append(_pick(product, "productName", "name", "title", "itemName", "goodsName"))
            options.append(_pick(product, "optionName", "optionTitle", "option", "optionValue"))
            qty = _int(_pick(product, "quantity", "qty", "count", "orderQuantity")) or 1
            quantity += qty
            line_sum += self._line_amount(product, qty)

        product_name = names[0] or _pick(row, "productName", "title")
        # 대표 상품의 옵션을 먼저 두고, 나머지 상품은 "이름 (옵션)"으로 이어 붙인다.
        parts = []
        if options[0]:
            parts.append(options[0])
        for name, option in zip(names[1:], options[1:]):
            label = name or option
            if name and option:
                label = f"{name} ({option})"
            if label:
                parts.append(label)

        # 금액: 일부 상품만 담긴 주문은 주문 총액이 아니라 담긴 줄의 합계를 쓴다(과대계상 방지).
        amount = line_sum
        if not acc["partial"]:
            total = _int(_pick(row, "totalPaymentAmount", "paymentAmount", "totalPrice",
                               "totalAmount", "orderAmount", "settleAmount"))
            amount = total or line_sum

        receiver = self._receiver_of(row)
        channel = _pick(row, "channelId", "channel", "channelType")
        # 톡스토어/선물하기를 함께 쓰면 주문번호 체계가 달라 채널까지 넣어 유일하게 만든다.
        import_key = f"{self.name}:{channel}:{order_no}" if channel else f"{self.name}:{order_no}"

        return self.order(
            importKey=import_key,
            orderNumber=order_no,
            orderedAt=_dt_text(_pick(row, *ORDERED_AT_KEYS)),
            productName=product_name,
            optionName=" / ".join(parts),
            productCode=_pick(main, "sellerProductCode", "sellerProductId", "productCode",
                              "productId", "channelProductId", "itemId"),
            quantity=max(1, quantity),
            amount=amount,
            recipient=receiver["name"],
            phone=receiver["phone"],
            postalCode=receiver["zip"],
            address=receiver["address"],
            deliveryMessage=receiver["message"],
            memo=("카카오 주문상태를 응답에서 찾지 못했습니다(배송중 건이 섞였을 수 있으니 확인)"
                  if acc["unknown_status"] else ""),
        )

    def _line_amount(self, product, qty) -> int:
        """상품 한 줄의 금액 — 줄 합계가 있으면 그대로, 없으면 단가×수량."""
        total = _int(_pick(product, "totalPrice", "totalPaymentAmount", "paymentAmount",
                           "totalAmount", "amount", "settleAmount"))
        if total:
            return total
        unit = _int(_pick(product, "price", "salePrice", "unitPrice", "productPrice",
                          "discountedPrice"))
        return unit * max(1, qty)

    def _receiver_of(self, row) -> dict:
        """수취인 정보 — receiver / shippingAddress / delivery 어디에 들어 있든 찾아낸다."""
        node = _node(row, "receiver", "receiverInfo", "shippingAddress", "deliveryAddress",
                     "delivery", "deliveryInfo", "shipping", "shippingInfo")
        inner = _node(node, "receiver", "receiverInfo")
        if inner:
            node = inner
        addr_node = _node(node, "address", "addressInfo") or node

        base = _pick(addr_node, "address", "baseAddress", "basicAddress", "address1",
                     "roadAddress", "streetAddress", "mainAddress")
        detail = _pick(addr_node, "detailAddress", "addressDetail", "address2",
                       "remainAddress", "subAddress")
        if detail and detail in base:
            detail = ""  # 이미 합쳐 내려주는 판이 있어 중복 방지
        return {
            "name": (_pick(node, "name", "receiverName", "recipientName", "receiver")
                     or _pick(row, "receiverName", "ordererName")),
            "phone": (_pick(node, "phoneNumber", "phone", "mobile", "cellPhone", "tel",
                            "receiverPhoneNumber", "phoneNumber1")
                      or _pick(row, "receiverPhoneNumber", "phoneNumber")),
            "zip": _pick(addr_node, "zipCode", "zipcode", "postCode", "postalCode",
                         "zoneCode", "zonecode"),
            "address": " ".join(x for x in (base, detail) if x),
            "message": (_pick(node, "deliveryMessage", "deliveryMemo", "shippingMessage",
                              "message", "memo", "deliveryRequest", "receiverMessage")
                        or _pick(row, "deliveryMessage", "deliveryMemo", "shippingMessage")),
        }

    # ---- 송장 전송 -----------------------------------------------------------
    def upload_invoice(self, order_no, invoice_no, courier_code="", **kw):
        """배송상품 발송처리 — POST /v1/shopping/orders/deliveries/invoices

        - 택배 배송(SHIPPING)은 deliveryCompanyCode + invoiceNumber가 필수다.
          택배사 코드는 delivery_companies()로 조회한다(CJ대한통운 = CJGLS).
        - 요청당 최대 100건까지 넣을 수 있지만 여기서는 1건씩 보낸다.
        - ★비동기 처리라 응답은 사실상 상태코드뿐이다. 반영을 확인하려면 최소 3초 뒤에
          주문조회로 다시 읽어야 한다.
        - 부분 발송(상품별 송장)은 product_order_ids로 대상 상품을 지정한다.
        ※실제 배선은 나중이라 여기서는 호출 형태만 갖춰 둔다(아직 호출하지 않았다).
        """
        order_no = _s(order_no)
        invoice_no = _s(invoice_no)
        if not order_no or not invoice_no:
            raise MallError("송장 전송에는 주문번호와 운송장번호가 모두 필요합니다.")
        item = {
            "orderId": order_no,
            "deliveryMethod": _s(kw.get("delivery_method")) or "SHIPPING",
            "deliveryCompanyCode": (_s(courier_code) or _s(self.s.get("courier_code"))
                                    or DEFAULT_COURIER),
            "invoiceNumber": invoice_no,
        }
        product_ids = kw.get("product_order_ids") or kw.get("productOrderIds")
        if product_ids:
            item["productOrderIds"] = [_s(x) for x in product_ids if _s(x)]
        self._call("POST", INVOICE_PATH, json_body={"orders": [item]})
        return True

    def delivery_companies(self):
        """택배사 코드 목록 조회 — 설정에서 코드를 고를 때 쓴다(CJGLS = CJ대한통운)."""
        data = self._call("GET", COMPANIES_PATH)
        rows = (_list(data, "companies", "deliveryCompanies", "elements", "list")
                or (data if isinstance(data, list) else []))
        return [{"code": _pick(x, "code", "deliveryCompanyCode", "companyCode"),
                 "name": _pick(x, "name", "companyName", "deliveryCompanyName")}
                for x in rows if isinstance(x, dict)]
