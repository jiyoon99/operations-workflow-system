"""토스쇼핑(Toss Shopping) 주문 수집.

- 인증: OAuth2 client_credentials
  POST https://oauth2.cert.toss.im/token (x-www-form-urlencoded)
  grant_type=client_credentials, client_id, client_secret, scope=toss-shopping-fep:write
  → access_token(JWT). expires_in을 보고 만료 1분 전에 스스로 갱신한다(인스턴스 캐시).
- 주문조회: GET {base}/api/v3/shopping-fep/orders/v2 (Authorization: Bearer …)
  startDate/endDate(yyyy-MM-dd, 최대 31일), status, limit(최대 50), nextCursor 페이징
- 송장전송: PUT {base}/api/v3/shopping-fep/orders/products/delivery

★몰별 함정(반드시 기억)
1) 레이트리밋 초과가 HTTP 200 + body errorCode=TOO_MANY_REQUEST로 온다.
   상태코드만 보면 "성공인데 주문 0건"으로 착각해 주문을 통째로 흘린다 → body를 항상 검사한다.
2) 웹훅이 없다. 폴링 전용이라 조회 기간이 겹쳐 같은 주문이 여러 번 들어온다.
   startDate/endDate가 '날짜' 단위라 인접한 창(window)이 경계 날짜를 공유하는 것도 겹침 원인이다.
   → orderId 기준으로 합쳐서 내보낸다(임포터 중복판정 이전에 1차 정리).
3) 조회 기간 최대 31일 → max_window_days=25로 여유를 둔다.
4) Direct API 키는 호출 서버 IP 등록이 필수다. IP가 안 맞으면 인증정보가 맞아도 거절된다(403 계열).
   → 인증 실패(401)와 IP/권한 문제(403)를 메시지에서 구분해 준다.
5) 한 주문이 상품별로 상태가 다를 수 있다(한 상품만 배송중). 상품 단위로 상태를 걸러
   '결제완료~상품준비중'인 것만 담는다. 이미 배송중/완료된 건은 가져오지 않는다.
"""
import re
import time

from datetime import timedelta

import requests

from .. import config
from .base import (MallAdapter, MallError, cached_token, check_cooldown,
                   drop_token, register, set_cooldown, store_token)

TOKEN_URL = "https://oauth2.cert.toss.im/token"
TOKEN_SCOPE = "toss-shopping-fep:write"          # 문서상 고정값

PROD_BASE = "https://shopping-fep.toss.im"
ALPHA_BASE = "https://shopping-fep-alpha.toss.im"   # 테스트(alpha) — 별도 승인 필요

ORDERS_PATH = "/api/v3/shopping-fep/orders/v2"
DELIVERY_PATH = "/api/v3/shopping-fep/orders/products/delivery"

# 수집 대상 상태 — 결제완료(PAID) ~ 상품준비중(PREPARING_PRODUCT)까지만.
# DELIVERING/DELIVERED/CONFIRMED_ORDER/CANCELED_PAYMENT는 신규 주문이 아니므로 제외.
COLLECT_STATUSES = ("PAID", "PREPARING_PRODUCT")

PAGE_LIMIT = 50          # limit 최대 50 (기본 20)
MAX_PAGES = 200          # 커서 페이징 무한루프 방지용 상한
TIMEOUT = 20             # 모든 호출 공통 타임아웃(초)
RATE_LIMIT_CODE = "TOO_MANY_REQUEST"
# 성공 응답에도 code/resultType 필드가 실려 올 수 있어, 아래 값들은 에러로 보지 않는다.
SUCCESS_CODES = {"SUCCESS", "OK", "0", "00", "200", "0000", "NORMAL"}
# 택배사 코드는 토스의 '택배사 정보 조회 API' 값을 써야 한다. 키 발급 후 실제 코드로 확인할 것.
DEFAULT_COURIER = "CJGLS"


def _num(v):
    """숫자/문자/None을 int로. '12,000' 같은 콤마 표기도 받는다."""
    if v is None or isinstance(v, bool):
        return 0
    try:
        return int(float(str(v).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def _txt(d, *names):
    """dict에서 후보 키를 순서대로 찾아 첫 값(문자열)을 돌려준다.

    토스 문서가 api-1(가이드)/api-2(레퍼런스)로 이원화돼 있고 필드명이 개정되는 일이 있어,
    핵심 필드는 후보를 여러 개 두고 방어적으로 읽는다.
    """
    if not isinstance(d, dict):
        return ""
    for n in names:
        v = d.get(n)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return ""


def _sub(d, *names):
    """중첩 dict 후보를 찾아 돌려준다(없으면 빈 dict)."""
    if not isinstance(d, dict):
        return {}
    for n in names:
        v = d.get(n)
        if isinstance(v, dict) and v:
            return v
    return {}


def _unit_price(p):
    """상품 1개 단가. price가 {salePrice, normalPrice} 형태로 올 수 있어 둘 다 받는다."""
    price = p.get("price")
    if isinstance(price, dict):
        for k in ("salePrice", "sellingPrice", "paymentPrice", "amount", "value", "normalPrice"):
            v = _num(price.get(k))
            if v:
                return v
        return 0
    v = _num(price)
    if v:
        return v
    for k in ("salePrice", "sellingPrice", "unitPrice", "productPrice"):
        v = _num(p.get(k))
        if v:
            return v
    return 0


def _line_amount(p):
    """상품 한 줄의 금액.

    라인 합계 필드가 있으면 그대로 쓰고, 없으면 단가×수량으로 계산한다.
    (price가 단가인지 라인합계인지 문서만으로는 단정할 수 없어 합계 필드를 우선한다.
     키 발급 후 실제 응답으로 한 번 검증할 것.)
    """
    for k in ("totalPrice", "totalAmount", "paymentAmount", "lineAmount", "totalSalePrice"):
        v = _num(p.get(k))
        if v:
            return v
    return _unit_price(p) * max(1, _num(p.get("quantity") or p.get("count") or 1))


def _prod_label(p):
    """상품명 + 옵션을 사람이 읽는 한 덩어리로."""
    name = _txt(p, "name", "productName", "goodsName", "title")
    opt = _txt(p, "option", "optionName", "optionTitle", "optionValue")
    if name and opt:
        return f"{name} ({opt})"
    return name or opt


def _prod_status(p, order):
    """상품 상태(없으면 주문 상태로 대체)."""
    return (_txt(p, "status", "orderProductStatus", "productStatus")
            or _txt(order, "status", "orderStatus")).upper()


@register
class TossAdapter(MallAdapter):
    code = "toss"
    name = "토스쇼핑"
    max_window_days = 25          # 몰 제한 31일 — 경계 날짜 겹침까지 감안해 여유를 둔다
    call_interval = 0.3           # 읽기 초당 50회 제한이라 여유롭지만 폴링이라 천천히
    # ★ 레지스트리(app/malls/__init__.py)의 toss fields 키와 정확히 일치해야 한다.
    #   use_alpha는 bool(선택)이라 필수에서 제외한다.
    required_keys = ("client_id", "client_secret")

    def __init__(self, settings):
        super().__init__(settings)
        self._sess_obj = None
        self._token_val = ""
        self._token_exp = 0.0     # epoch 초. 만료 1분 전에 미리 갱신한다.

    # ---------------- 공통 인프라 ----------------
    def _base(self):
        """운영/테스트 서버 선택 — 설정 use_alpha가 참이면 alpha."""
        return ALPHA_BASE if self.s.get("use_alpha") else PROD_BASE

    def _sess(self):
        if self._sess_obj is None:
            self._sess_obj = requests.Session()
        return self._sess_obj

    def _token(self):
        """access_token 발급 — ★프로세스 전역 캐시로 만료까지 재사용한다.

        어댑터 인스턴스는 수집 때마다 새로 만들어지므로 인스턴스 캐시는 소용이 없다.
        매 수집마다 새 토큰을 받으면 발급 한도를 갉아먹는다(RMS 시행착오 계승).
        """
        now = time.time()
        client_id = (self.s.get("client_id") or "").strip()
        hit = cached_token("toss", client_id)
        if hit:
            return hit

        data = {
            "grant_type": "client_credentials",
            "client_id": (self.s.get("client_id") or "").strip(),
            "client_secret": (self.s.get("client_secret") or "").strip(),
            "scope": TOKEN_SCOPE,
        }
        try:
            r = self._sess().post(
                TOKEN_URL, data=data, timeout=TIMEOUT,
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "Accept": "application/json", "User-Agent": "OWS/1.0"})
        except requests.Timeout as e:
            raise MallError("토스쇼핑 토큰 발급이 시간 내에 끝나지 않았습니다(네트워크 확인).") from e
        except requests.RequestException as e:
            raise MallError(f"토스쇼핑 토큰 발급 호출 실패: {e}") from e

        body = {}
        try:
            body = r.json() or {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        err = _txt(body, "error", "errorCode", "code")
        desc = _txt(body, "error_description", "errorMessage", "message", "reason")

        if r.status_code in (400, 401):
            # invalid_client = client_id/secret 불일치. 여기서 IP 문제와 헷갈리지 않게 문구를 나눈다.
            raise MallError(
                "토스쇼핑 인증에 실패했습니다 — Client ID / Client Secret을 확인해 주세요. "
                f"(응답: {err or r.status_code}{': ' + desc if desc else ''})")
        if r.status_code == 403:
            raise MallError(
                "토스쇼핑이 인증을 거부했습니다 — 셀러 어드민 [쇼핑 > 연동 관리]에 이 서버의 "
                "공인 IP가 등록돼 있는지, Direct API 키의 권한(scope)이 맞는지 확인해 주세요. "
                f"({desc or err or '상세 메시지 없음'})")
        if r.status_code == 429:
            raise MallError("토스쇼핑 토큰 발급이 호출 한도를 넘었습니다. 잠시 후 다시 시도해 주세요.")
        if r.status_code >= 500:
            raise MallError(f"토스쇼핑 인증 서버 오류({r.status_code}). 잠시 후 다시 시도해 주세요.")
        if r.status_code != 200:
            raise MallError(f"토스쇼핑 토큰 발급 실패({r.status_code}): {desc or err or r.text[:200]}")

        token = _txt(body, "access_token", "accessToken")
        if not token:
            raise MallError(f"토스쇼핑 토큰 응답에 access_token이 없습니다: {str(body)[:200]}")
        expires = _num(body.get("expires_in") or body.get("expiresIn")) or 3600
        store_token("toss", client_id, token, expires)
        return token

    @staticmethod
    def _error_of(data):
        """응답 body에서 (에러코드, 메시지)를 뽑는다.

        ★TOO_MANY_REQUEST가 HTTP 200으로 오기 때문에 이 검사는 상태코드와 무관하게 항상 돈다.
        """
        if not isinstance(data, dict):
            return "", ""
        err = data.get("error")
        if isinstance(err, dict):
            return (_txt(err, "errorCode", "code", "error"),
                    _txt(err, "errorMessage", "message", "reason", "error_description"))
        code = _txt(data, "errorCode", "code")
        msg = _txt(data, "errorMessage", "message", "reason", "error_description")
        if not code and isinstance(err, str):
            code, msg = err, msg or _txt(data, "error_description")
        # resultType=FAIL 인데 코드가 비어 있는 경우도 실패로 본다
        if not code and _txt(data, "resultType", "result").upper() in ("FAIL", "FAILURE", "ERROR"):
            code = "FAIL"
        if code.upper() in SUCCESS_CODES:
            return "", ""
        return code, msg

    @staticmethod
    def _payload(data):
        """성공 응답의 실제 알맹이. {resultType, success:{…}} 래핑을 벗긴다."""
        if not isinstance(data, dict):
            return {}
        for k in ("success", "data", "result", "body"):
            v = data.get(k)
            if isinstance(v, dict):
                return v
        return data

    def _api(self, method, path, params=None, json_body=None):
        """공통 호출 — 토큰 부착, 상태코드/보디 에러 판별, 레이트리밋 재시도."""
        check_cooldown(self.code, self.name)   # 쿨다운 중엔 호출 자체를 안 한다
        url = self._base() + path
        delay = 1.0
        for attempt in range(3):
            headers = {
                "Authorization": f"Bearer {self._token()}",
                "Accept": "application/json",
                "User-Agent": "OWS/1.0",
            }
            try:
                r = self._sess().request(method, url, params=params, json=json_body,
                                         headers=headers, timeout=TIMEOUT)
            except requests.Timeout as e:
                raise MallError(f"토스쇼핑 응답이 {TIMEOUT}초 안에 오지 않았습니다(네트워크 확인).") from e
            except requests.RequestException as e:
                raise MallError(f"토스쇼핑 호출 실패: {e}") from e

            if r.status_code == 401:
                # 토큰 만료/무효 — 한 번은 새 토큰으로 재시도하고, 그래도 안 되면 인증 문제로 본다.
                self._token_val, self._token_exp = "", 0.0
                if attempt == 0:
                    continue
                raise MallError("토스쇼핑 인증이 거부됐습니다(401) — Client ID / Client Secret을 확인해 주세요.")
            if r.status_code == 403:
                raise MallError(
                    "토스쇼핑이 접근을 거부했습니다(403) — 호출 서버 IP가 셀러 어드민에 등록돼 있는지, "
                    "키 권한이 주문/배송까지 포함하는지 확인해 주세요.")
            if r.status_code == 429:
                if attempt < 2:
                    time.sleep(delay)
                    delay *= 2
                    continue
                set_cooldown(self.code)
                raise MallError("토스쇼핑 호출 한도를 초과했습니다(429) — "
                                "5분 쉬었다가 자동으로 재개됩니다.")
            if r.status_code >= 500:
                if attempt < 2:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise MallError(f"토스쇼핑 서버 오류({r.status_code}). 잠시 후 다시 시도해 주세요.")

            try:
                data = r.json()
            except ValueError as e:
                raise MallError(f"토스쇼핑 응답을 해석하지 못했습니다: {r.text[:200]}") from e
            if not isinstance(data, dict):
                raise MallError(f"토스쇼핑 응답 형식이 예상과 다릅니다: {str(data)[:200]}")

            code, msg = self._error_of(data)
            # ★핵심: 상태코드가 200이어도 body에 TOO_MANY_REQUEST가 실려 온다.
            if code == RATE_LIMIT_CODE:
                if attempt < 2:
                    time.sleep(delay)
                    delay *= 2
                    continue
                set_cooldown(self.code)          # 계속 때리면 차단이 길어진다 — 5분 휴식
                raise MallError(
                    "토스쇼핑 호출 한도를 초과했습니다(TOO_MANY_REQUEST) — "
                    "5분 쉬었다가 자동으로 재개됩니다.")
            if code:
                if code in ("UNAUTHORIZED", "INVALID_TOKEN", "EXPIRED_TOKEN"):
                    self._token_val, self._token_exp = "", 0.0
                    drop_token("toss", (self.s.get("client_id") or "").strip())
                    raise MallError(f"토스쇼핑 인증 오류({code}): {msg or 'Client ID/Secret을 확인해 주세요.'}")
                if code in ("FORBIDDEN", "NOT_ALLOWED_IP", "UNAUTHORIZED_IP", "ACCESS_DENIED"):
                    raise MallError(f"토스쇼핑 접근 거부({code}): {msg or '호출 서버 IP 등록 여부를 확인해 주세요.'}")
                raise MallError(f"토스쇼핑 오류({code}): {msg or '메시지 없음'}")

            if r.status_code >= 400:
                raise MallError(f"토스쇼핑 호출 실패({r.status_code}): {msg or r.text[:200]}")
            return self._payload(data)

        raise MallError("토스쇼핑 호출을 3회 시도했지만 실패했습니다.")

    # ---------------- 주문 수집 ----------------
    def collect_orders(self, since, until):
        merged = {}     # orderId -> {"raw": 주문원본, "products": [상품…], "seen": {상품ID}}
        for start, end in self.windows(since, until):
            for status in COLLECT_STATUSES:
                # status는 한 번에 하나만 확실히 먹히므로 상태별로 나눠 부른다.
                # 창(window)이 겹쳐 같은 주문이 두 번 와도 merged에서 합쳐진다.
                self._fetch_window(start, end, status, merged)
        return [self._build(v) for v in merged.values() if v["products"]]

    def _fetch_window(self, start, end, status, merged):
        """한 창·한 상태를 커서 페이징으로 끝까지 읽는다."""
        cursor = ""
        seen_cursors = set()
        for _ in range(MAX_PAGES):
            params = {
                # 날짜 단위(yyyy-MM-dd)라 시각은 버려진다 → 경계 날짜가 겹치는 건 정상, merged가 흡수.
                "startDate": start.strftime("%Y-%m-%d"),
                "endDate": end.strftime("%Y-%m-%d"),
                "status": status,
                "limit": PAGE_LIMIT,
            }
            if cursor:
                params["nextCursor"] = cursor
            if self.s.get("partner_name"):
                params["partnerName"] = self.s["partner_name"]

            payload = self._api("GET", ORDERS_PATH, params=params)
            self.pace()   # 호출 사이 간격 — 레이트리밋 회피

            rows = payload.get("orders")
            if not isinstance(rows, list):
                for k in ("content", "list", "items", "data"):
                    if isinstance(payload.get(k), list):
                        rows = payload[k]
                        break
            for row in rows or []:
                self._absorb(row, merged)

            cursor = _txt(payload, "nextCursor", "nextToken", "cursor")
            if not cursor or not rows:
                return
            if cursor in seen_cursors:
                # 같은 커서가 되돌아오면 무한루프 — 더 읽어도 새 주문이 없다.
                return
            seen_cursors.add(cursor)
        raise MallError(
            "토스쇼핑 주문이 너무 많아 한 번에 다 읽지 못했습니다. 조회 기간을 줄여 다시 시도해 주세요.")

    def _absorb(self, row, merged):
        """주문 한 건을 orderId 기준으로 합친다(상품행 분할·창 겹침 대비)."""
        if not isinstance(row, dict):
            return
        order_no = _txt(row, "orderId", "orderNo", "orderNumber", "id")
        if not order_no:
            return
        products = row.get("products")
        if not isinstance(products, list):
            products = row.get("orderProducts") if isinstance(row.get("orderProducts"), list) else []

        slot = merged.get(order_no)
        if slot is None:
            slot = merged[order_no] = {"raw": row, "products": [], "seen": set()}

        for idx, p in enumerate(products):
            if not isinstance(p, dict):
                continue
            # 상태 필터: 결제완료~상품준비중만. 이미 배송중/완료면 신규 주문이 아니다.
            st = _prod_status(p, row)
            if st and st not in COLLECT_STATUSES:
                continue
            # 상품 단위 중복 제거 — 상태별/창별로 같은 상품이 두 번 올 수 있다.
            key = _txt(p, "orderProductId", "productId", "id") or f"#{idx}:{_prod_label(p)}"
            if key in slot["seen"]:
                continue
            slot["seen"].add(key)
            slot["products"].append(p)

    def _build(self, slot):
        """합쳐진 주문 하나를 임포터용 dict로."""
        row, products = slot["raw"], slot["products"]
        order_no = _txt(row, "orderId", "orderNo", "orderNumber", "id")
        main = products[0]

        # 대표 상품명 + 나머지는 optionName에 " / "로 잇는다(대표 상품의 옵션이 맨 앞).
        parts = []
        main_opt = _txt(main, "option", "optionName", "optionTitle", "optionValue")
        if main_opt:
            parts.append(main_opt)
        for p in products[1:]:
            label = _prod_label(p)
            if label:
                parts.append(label)

        qty = sum(max(1, _num(p.get("quantity") or p.get("count") or 1)) for p in products)
        # 금액은 '수집한 상품'만 더한다. 일부 상품이 이미 배송중이라 빠진 경우
        # 주문 총액을 쓰면 과다 계상되기 때문이다.
        amount = sum(_line_amount(p) for p in products)
        if not amount:
            amount = _num(_txt(row, "totalAmount", "totalPrice", "paymentAmount", "paidAmount"))

        rcv = _sub(row, "receiver", "receiverInfo", "shippingAddress", "delivery", "recipient")
        addr = " ".join(x for x in [
            _txt(rcv, "address1", "address", "baseAddress", "roadAddress"),
            _txt(rcv, "address2", "detailAddress", "addressDetail"),
        ] if x)

        return self.order(
            importKey=f"토스쇼핑:{order_no}",
            orderNumber=order_no,
            orderedAt=_txt(row, "orderedAt", "orderDate", "paidAt", "createdAt"),
            productName=_txt(main, "name", "productName", "goodsName", "title"),
            optionName=" / ".join(parts),
            productCode=_txt(main, "productId", "externalProductId", "sellerProductCode",
                             "productCode", "orderProductId"),
            quantity=max(1, qty),
            amount=amount,
            recipient=_txt(rcv, "name", "receiverName", "recipientName") or _txt(row, "ordererName"),
            phone=_txt(rcv, "phone", "mobilePhone", "phoneNumber", "tel", "safeNumber"),
            postalCode=_txt(rcv, "zipCode", "zipcode", "postalCode", "postCode"),
            address=addr,
            deliveryMessage=_txt(row, "deliveryMessage", "deliveryMemo", "message")
                            or _txt(rcv, "deliveryMessage", "message", "memo"),
        )

    # ---------------- 송장 전송 ----------------
    def upload_invoice(self, order_no, invoice_no, courier_code="", **kw):
        """송장 전송 — PUT /orders/products/delivery.

        토스는 '주문' 단위가 아니라 '주문상품(orderProductId)' 단위로 송장을 등록한다.
        상품준비중(PREPARING_PRODUCT) 상태에서 등록하면 자동으로 배송중으로 넘어간다.
        (실제 배선은 나중에 — 여기서는 시그니처와 동작만 갖춰둔다.)
        """
        # 하이픈/공백 제거. 토스는 기본적으로 숫자 송장만 받고 일부 택배사만 영문을 허용한다.
        tracking = re.sub(r"[^0-9A-Za-z]", "", str(invoice_no or ""))
        if not tracking:
            raise MallError("송장번호가 비어 있어 토스쇼핑에 전송할 수 없습니다.")

        company = (courier_code or kw.get("delivery_company")
                   or (self.s.get("courier_code") or "").strip() or DEFAULT_COURIER)

        ids = kw.get("order_product_ids") or []
        if not ids and kw.get("order_product_id"):
            ids = [kw["order_product_id"]]
        if not ids:
            ids = self._resolve_order_product_ids(order_no)

        for pid in ids:
            payload = {
                "orderProductId": _num(pid),
                "deliveryCompany": company,
                "trackingNumber": tracking,
            }
            if self.s.get("partner_name"):
                payload["partnerName"] = self.s["partner_name"]
            self._api("PUT", DELIVERY_PATH, json_body=payload)
            self.pace()
        return True

    def _resolve_order_product_ids(self, order_no, days=None):
        """주문번호로 주문상품ID를 역추적한다(호출 측이 ID를 못 줄 때만).

        조회 API에만 orderProductId가 실려 오므로 최근 기간을 훑어 찾는다.
        불필요한 호출을 줄이려면 수집 때 받아둔 ID를 order_product_ids로 넘겨 주는 편이 낫다.
        """
        until = config.now()
        since = until - timedelta(days=days or self.max_window_days)
        merged = {}
        for start, end in self.windows(since, until):
            for status in COLLECT_STATUSES:
                self._fetch_window(start, end, status, merged)
        slot = merged.get(str(order_no))
        ids = []
        if slot:
            for p in slot["products"]:
                pid = _txt(p, "orderProductId", "id")
                if pid:
                    ids.append(pid)
        if not ids:
            raise MallError(
                f"토스쇼핑 주문 {order_no}의 주문상품ID를 찾지 못했습니다 — "
                "이미 배송중이거나 최근 조회 기간을 벗어난 주문일 수 있습니다.")
        return ids
