"""네이버 스마트스토어(커머스 API) 주문 수집.

- 인증: OAuth2 client_credentials + bcrypt 전자서명(네이버 고유 방식)
  POST https://api.commerce.naver.com/external/v1/oauth2/token
  client_secret_sign = base64(bcrypt.hashpw(f"{client_id}_{timestamp}", salt=client_secret))
  timestamp는 밀리초. 서버 시계가 네이버보다 빠르면 거절되므로 3초 빼고 만든다.
- 주문조회: GET /external/v1/pay-order/seller/product-orders
  from/to는 'yyyy-MM-ddTHH:mm:ss.000+09:00', 한 번에 24시간까지.
- 발송처리: POST /external/v1/pay-order/seller/product-orders/dispatch
  productOrderId 단위. 주문번호(orderId)만 알면 됨 — 상품주문 ID는 전송 시점에 조회한다.

RMS(rental-system)에서 실운영으로 검증한 연동을 HMS 규율로 다시 지었다.

★몰별 함정(RMS 실운영에서 확인)
1) 상태 필터에 PAYMENT_WAITING/PRODUCT_PREPARE를 넣으면 400이 난다(이 엔드포인트가
   받는 enum이 아님). 필터를 아예 빼고 전 상태를 받아 우리 쪽에서 걸러낸다 —
   발주확인(PRODUCT_PREPARE)된 주문도 놓치지 않기 위해서다.
2) 레이트리밋이 빡빡하다(초당 5회). 호출 사이 250ms 간격 + 응답 헤더
   GNCP-GW-RateLimit-Remaining이 2 이하면 1초 쉬어 429를 사전에 피한다.
3) 응답 구조가 버전에 따라 {data:{contents:[…]}} / {data:[…]} / {contents:[…]}로
   달라진 적이 있다 — 세 경우를 모두 받는다.
4) 한 주문(orderId)이 상품 수만큼 productOrder로 쪼개져 온다. 주문 단위로 합치지 않으면
   같은 주문이 여러 건으로 늘어나 중복판정이 무너진다.
5) 금액: productOrder.totalPaymentAmount(상품, 할인 후) + deliveryFeeAmount(택배비).
   둘 다 0이면 order.generalPaymentAmount(전체 결제액)로 대신한다.
"""
import base64
import time

from datetime import datetime, timedelta

import requests

from .base import (MallAdapter, MallError, cached_token, check_cooldown, drop_token,
                   pick_product_code, register, set_cooldown, store_token)

BASE_URL = "https://api.commerce.naver.com/external/v1"
TOKEN_URL = f"{BASE_URL}/oauth2/token"
ORDERS_PATH = "/pay-order/seller/product-orders"
DISPATCH_PATH = "/pay-order/seller/product-orders/dispatch"
ORDER_IDS_PATH = "/pay-order/seller/orders/{order_id}/product-order-ids"

TIMEOUT = 20
PAGE_SIZE = 300
MAX_PAGES = 40                      # 하루 주문 12,000건 상한 — 사실상 무제한
DEFAULT_COURIER = "CJGLS"           # 네이버 택배사 코드표의 CJ대한통운

# 우리가 '신규 주문'으로 가져올 상태 — 발송 전 단계만.
# (DELIVERING/DELIVERED/PURCHASE_DECIDED는 이미 나간 주문, CANCELED류는 취소)
NEW_ORDER_STATUSES = {"PAYED", "PRODUCT_PREPARE"}

# ★같은 스마트스토어 계정에 렌탈(RMS)과 판매(HMS) 상품이 함께 올라가 있다.
#   렌탈 주문이 HMS로 들어오면 판매팀이 렌탈 주문에 노트북을 '판매 출고'하는 사고가 난다.
#   RMS는 반대로 판매 상품을 걸러낸다(화이트리스트+미분류 태깅) — 그 거울상이다.
#   상품명에 이 말이 들어가면 상품번호 등록 전이라도 렌탈로 보고 제외한다.
RENTAL_KEYWORDS = ("렌탈", "렌트", "대여", "임대", "사용기간")


def _id_list(value):
    """설정 칸의 '12345, 67890' 같은 문자열을 상품번호 집합으로."""
    import re as _re
    return {t for t in _re.split(r"[,\s]+", str(value or "").strip()) if t}

RATE_HEADER = "GNCP-GW-RateLimit-Remaining"
RATE_FLOOR = 2                      # 잔여 한도가 이 밑이면 잠깐 쉰다


def _s(v):
    return str(v or "").strip()


def _num(v):
    if v is None or isinstance(v, bool):
        return 0
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return 0


@register
class SmartStoreAdapter(MallAdapter):
    code = "smartstore"
    name = "스마트스토어"           # ★주문 channel 값과 같아야 송장 자동전송이 맞물린다
    max_window_days = 1             # from~to 한 번에 24시간까지
    call_interval = 0.25            # 초당 5회 제한 — 여유를 두고 초당 4회

    required_keys = ("client_id", "client_secret")

    # ---- 인증 -------------------------------------------------------
    def _access_token(self):
        """토큰 발급 — ★프로세스 전역 캐시로 만료까지 재사용한다.

        네이버는 토큰 발급 자체에 한도가 있다(RMS가 여기서 시행착오를 겪었다).
        어댑터 인스턴스는 수집 때마다 새로 만들어지므로, 인스턴스에 캐시하면
        30분마다 새 토큰을 받게 돼 발급 한도를 갉아먹는다. 토큰 수명은 약 3시간 —
        전역 캐시면 하루 발급이 8회면 끝난다.
        """
        client_id = _s(self.s.get("client_id"))
        hit = cached_token("smartstore", client_id)
        if hit:
            return hit
        try:
            import bcrypt
        except ImportError:
            raise MallError("bcrypt 라이브러리가 없습니다. venv에 'pip install bcrypt' 후 "
                            "서버를 재시작해 주세요.")
        client_id = _s(self.s.get("client_id"))
        client_secret = _s(self.s.get("client_secret"))
        # 서버 시계가 네이버보다 빠르면 '미래 요청'으로 거절된다 — 3초 빼서 만든다
        timestamp = str(int((time.time() - 3) * 1000))
        password = f"{client_id}_{timestamp}".encode("utf-8")
        try:
            signature = base64.b64encode(
                bcrypt.hashpw(password, client_secret.encode("utf-8"))).decode("utf-8")
        except ValueError:
            raise MallError("Client Secret 형식이 올바르지 않습니다 — 커머스API센터에서 "
                            "발급받은 값(bcrypt salt 형식)을 그대로 붙여넣어 주세요.")
        r = requests.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "timestamp": timestamp,
            "client_secret_sign": signature,
            "type": "SELF",
        }, timeout=TIMEOUT)
        body = r.json() if r.content else {}
        token = _s(body.get("access_token"))
        if not token:
            msg = _s(body.get("message")) or _s(body.get("error_description")) or r.text[:200]
            if "ip" in msg.lower():
                raise MallError(f"스마트스토어가 이 서버의 IP를 거부했습니다 — 커머스API센터에 "
                                f"호출 IP를 등록했는지 확인하세요. (응답: {msg})")
            raise MallError(f"스마트스토어 인증에 실패했습니다 — Client ID/Secret을 확인해 "
                            f"주세요. (응답: {msg})")
        store_token("smartstore", client_id, token, _num(body.get("expires_in") or 10800))
        return token

    def _call(self, method, path, params=None, body=None):
        check_cooldown(self.code, self.name)   # 쿨다운 중엔 호출 자체를 안 한다(예산 보호)
        headers = {"Authorization": f"Bearer {self._access_token()}",
                   "Content-Type": "application/json"}
        r = requests.request(method, BASE_URL + path, headers=headers,
                             params=params, json=body, timeout=TIMEOUT)
        # 잔여 한도가 바닥나기 전에 쉰다 — 429가 나면 이미 늦다
        remaining = _s(r.headers.get(RATE_HEADER) or r.headers.get(RATE_HEADER.lower()))
        if remaining.isdigit() and int(remaining) <= RATE_FLOOR:
            time.sleep(1.0)
        if r.status_code == 429:
            # ★RMS 시행착오 계승: 429 후 계속 때리면 차단이 길어진다 — 5분 쉰다.
            set_cooldown(self.code)
            raise MallError("스마트스토어 호출 한도를 초과했습니다 — 5분 쉬었다가 "
                            "자동으로 재개됩니다.")
        if r.status_code == 401:
            drop_token("smartstore", _s(self.s.get("client_id")))   # 다음 호출에서 재발급
            raise MallError("스마트스토어 인증이 만료되었습니다 — 다시 시도해 주세요.")
        try:
            data = r.json() if r.content else {}
        except ValueError:
            raise MallError(f"스마트스토어 응답을 해석할 수 없습니다(HTTP {r.status_code}).")
        if r.status_code >= 400:
            msg = _s((data or {}).get("message")) or str(data)[:200]
            raise MallError(f"스마트스토어 오류(HTTP {r.status_code}): {msg}")
        return data

    # ---- 주문 수집 ---------------------------------------------------
    @staticmethod
    def _contents(data):
        """응답 구조 3종({data:{contents}}, {data:[…]}, {contents}) 전부 수용."""
        if not isinstance(data, dict):
            return []
        inner = data.get("data", data)
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
        if isinstance(inner, dict):
            raw = inner.get("contents", inner.get("data", []))
            return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []
        raw = data.get("contents", [])
        return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []

    # ---- 렌탈/판매 분류 ----------------------------------------------
    def _classify(self, product_id, product_name, option):
        """sale(수집) / rental(제외) / unknown(수집+표시).

        RMS의 분류 원칙 계승: 애매한 것은 버리지 않는다. 진짜 판매 주문이 조용히
        사라지면 출고 누락 사고가 되기 때문. 확실한 렌탈만 제외한다.
        우선순위: 판매 등록 > 렌탈 등록 > 렌탈 키워드 > 미분류.
        """
        pid = _s(product_id)
        sale_ids = _id_list(self.s.get("sale_product_ids"))
        rental_ids = _id_list(self.s.get("rental_product_ids"))
        if pid and pid in sale_ids:
            return "sale"                     # 판매 등록이 최우선 — 키워드 오판을 이긴다
        if pid and pid in rental_ids:
            return "rental"
        text = f"{product_name} {option}"
        if any(k in text for k in RENTAL_KEYWORDS):
            return "rental"
        if sale_ids or rental_ids:
            return "unknown"                  # 분류표를 쓰기 시작했는데 등록이 안 된 상품
        return "sale"                         # 분류표 미사용 — 전부 판매 취급(기존 동작)

    def collect_orders(self, since, until):
        buckets = {}                # orderId → 누적
        order_seq = []
        seen_po = set()             # productOrderId — 구간 경계 중복 방어
        # 제외한 렌탈 주문 기록 — '조용히 사라졌다'가 되지 않게 수집 결과에 함께 보고한다
        self.skipped_rental = []

        for start, end in self.windows(since, until):
            page = 1
            while page <= MAX_PAGES:
                params = {
                    "from": start.strftime("%Y-%m-%dT%H:%M:%S.000") + "+09:00",
                    "to": end.strftime("%Y-%m-%dT%H:%M:%S.000") + "+09:00",
                    # ★상태 필터를 보내지 않는다 — PRODUCT_PREPARE를 넣으면 400,
                    #   안 가져오면 발주확인된 주문을 놓친다. 전부 받아서 아래에서 거른다.
                    "page": page,
                    "size": PAGE_SIZE,
                }
                data = self._call("GET", ORDERS_PATH, params=params)
                self.pace()
                items = self._contents(data)
                for it in items:
                    self._absorb(it, buckets, order_seq, seen_po)
                if len(items) < PAGE_SIZE:
                    break
                page += 1

        return [buckets[oid] for oid in order_seq]

    def _absorb(self, item, buckets, order_seq, seen_po):
        content = item.get("content") or {}
        po = content.get("productOrder") or content
        order = content.get("order") or content

        status = _s(po.get("productOrderStatus") or content.get("productOrderStatus")
                    or item.get("productOrderStatus"))
        if status not in NEW_ORDER_STATUSES:
            return                      # 배송중·완료·취소는 신규 주문이 아니다

        po_id = _s(po.get("productOrderId") or item.get("productOrderId"))
        if po_id and po_id in seen_po:
            return
        if po_id:
            seen_po.add(po_id)

        order_id = _s(order.get("orderId") or po.get("orderId")) or po_id
        if not order_id:
            return

        name = _s(po.get("productName")) or "상품"
        option = _s(po.get("productOption") or po.get("productOptionText")
                    or po.get("optionName"))

        # ★렌탈 상품 주문은 HMS(판매)로 가져오지 않는다 — RMS(렌탈) 몫이다.
        product_id = _s(po.get("productId") or po.get("productNo"))
        # ★제품코드는 '판매자상품코드'에 들어온다(대표 확인 2026-08-04).
        #   네이버는 응답 스키마에서 이 칸 이름이 여러 가지라 알려진 후보를 모두 본다.
        #   상품 단위(sellerProductCode)와 옵션 단위(optionManageCode) 둘 다 쓰인다.
        #   못 찾으면 예전처럼 네이버 내부 상품번호가 들어간다(재고 대조는 안 되지만 주문은 살아 있다).
        sku = pick_product_code(
            po.get("sellerProductCode"), po.get("sellerManagementCode"),
            po.get("sellerProductManagementCode"), po.get("optionManageCode"),
            po.get("sellerCustomCode"), po.get("productClassCode"),
            (po.get("productOption") or ""), name)
        klass = self._classify(product_id, name, option)
        if klass == "rental":
            self.skipped_rental.append(
                f"{_s(order.get('ordererName')) or '?'} · {name[:30]}"
                + (f" (상품번호 {product_id})" if product_id else ""))
            return

        qty = _num(po.get("quantity")) or 1
        goods = _num(po.get("totalPaymentAmount")) or _num(po.get("unitPrice")) * qty
        deliv = (_num(po.get("deliveryFeeAmount")) or _num(po.get("deliveryAmount"))
                 or _num(order.get("deliveryFeeAmount")))
        amount = goods + deliv
        if not amount:
            amount = _num(order.get("generalPaymentAmount"))

        sa = po.get("shippingAddress") or content.get("shippingAddress") or {}
        ordered = _s(order.get("paymentDate") or order.get("orderDate"))[:10]

        bucket = buckets.get(order_id)
        if bucket is None:
            buckets[order_id] = self.order(
                orderNumber=order_id,
                orderedAt=ordered,
                productName=name,
                optionName=option,
                # 분류표를 쓰는데 등록이 안 된 상품 — 버리지 않고 눈에 띄게 표시한다
                # (memo는 셋팅 화면에 빨간 📌로 뜬다). 렌탈이면 설정에서 제외 등록하면 된다.
                memo=("⚠분류 미등록 상품 — 렌탈 주문이면 설정>스마트스토어에서 "
                      f"상품번호 {product_id}를 렌탈로 등록하세요" if klass == "unknown" else ""),
                productCode=sku or product_id,
                quantity=qty,
                amount=amount,
                recipient=_s(sa.get("name")) or _s(order.get("ordererName")),
                phone=_s(sa.get("tel1") or sa.get("tel2")) or _s(order.get("ordererTel")),
                postalCode=_s(sa.get("zipCode")),
                address=" ".join(x for x in (_s(sa.get("baseAddress")),
                                             _s(sa.get("detailedAddress"))) if x),
                deliveryMessage=_s(po.get("shippingMemo")),
            )
            order_seq.append(order_id)
        else:
            # 같은 주문의 다른 상품 — 상품명을 잇고 수량·금액을 합친다
            if name not in bucket["productName"]:
                bucket["productName"] = f"{bucket['productName']} + {name}"[:300]
            if option and option not in bucket["optionName"]:
                bucket["optionName"] = (f"{bucket['optionName']} / {option}"
                                        if bucket["optionName"] else option)[:300]
            bucket["quantity"] += qty
            bucket["amount"] += amount

    # ---- 연결 테스트 -------------------------------------------------
    def test_connection(self):
        until = datetime.now()
        since = until - timedelta(days=1)
        orders = self.collect_orders(since, until)
        return (f"연결 성공 — 최근 1일 발송 전 주문 {len(orders)}건을 읽었습니다"
                f"(저장하지 않음).")

    # ---- 송장 전송 ---------------------------------------------------
    def upload_invoice(self, order_no, invoice_no, courier_code="", **kw):
        """발송처리 — 주문번호(orderId)로 상품주문 ID들을 조회해 전부 발송 처리한다."""
        invoice_no = _s(invoice_no)
        if not invoice_no:
            raise MallError("송장번호가 비어 있어 스마트스토어에 전송할 수 없습니다.")
        courier = _s(courier_code) or _s(self.s.get("courier_code")) or DEFAULT_COURIER

        data = self._call("GET", ORDER_IDS_PATH.format(order_id=_s(order_no)))
        self.pace()
        inner = data.get("data", data)
        po_ids = [_s(x) for x in (inner if isinstance(inner, list) else
                                  (inner or {}).get("productOrderIds", [])) if _s(x)]
        if not po_ids:
            raise MallError(f"스마트스토어 주문 {order_no}의 상품주문을 찾지 못했습니다.")

        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000") + "+09:00"
        body = {"dispatchProductOrders": [
            {"productOrderId": pid, "deliveryMethod": "DELIVERY",
             "deliveryCompanyCode": courier, "trackingNumber": invoice_no,
             "dispatchDate": now}
            for pid in po_ids
        ]}
        result = self._call("POST", DISPATCH_PATH, body=body)
        self.pace()
        # 부분 실패가 successProductOrderIds/failProductOrderInfos로 온다
        inner = result.get("data", result) or {}
        fails = inner.get("failProductOrderInfos") or []
        if fails:
            first = fails[0] if isinstance(fails[0], dict) else {}
            raise MallError(f"스마트스토어 발송처리 일부 실패({len(fails)}건): "
                            f"{_s(first.get('message')) or _s(first.get('code'))}")
        return True
