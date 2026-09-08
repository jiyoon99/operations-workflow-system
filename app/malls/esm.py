"""ESM(G마켓·옥션 통합 / ESM PLUS) 주문 수집.

- base: https://sa2.esmplus.com — 주문·배송 계열은 전부 POST + JSON
  * 주문조회   POST /shipping/v1/Order/RequestOrders
  * 주문확인   POST /shipping/v1/Order/OrderCheck/{OrderNo}   (발송처리 선행 단계)
  * 발송처리   POST /shipping/v1/Delivery/ShippingInfo
- 인증: 발급받은 Secret Key로 HS256 JWT를 '직접' 만들어 Authorization: Bearer 로 보낸다.
  PyJWT를 새로 들이지 않으려고 표준 라이브러리(hmac/hashlib/base64/json)로만 서명한다.
  ★클레임 구성(kid/iss/domain/aud/iat/exp)과 Secret Key 인코딩(원문 UTF-8 vs base64)은
    키 발급 후 ESM 규격서로 반드시 확인할 것. 여기 값은 조사자료 기준의 잠정 구성이다.

몰별 함정(왜 이렇게 짰는지)
 1) 레이트리밋: 주문조회/입금확인중조회는 '판매자 ID당 5초에 1회'(2025-04-23 시행).
    초과하면 ResultCode 3000이 온다. → call_interval=5.5로 넉넉히 두고, 3000/429는
    원인을 구분해 알린 뒤 짧게 백오프 재시도한다. 지마켓·옥션 ID가 달라도 보수적으로
    모든 호출 사이에 간격을 둔다(공용 프록시/IP 단위 제한 가능성 때문).
 2) 조회기간: G마켓 31일 / 옥션 180일로 서로 다르다. 짧은 쪽(31일)에 맞추지 않으면
    지마켓 호출만 실패하므로 max_window_days=25로 통일해 자른다.
 3) 사이트 분기: 지마켓·옥션이 같은 엔드포인트를 쓰고 '사이트 구분값 + 판매자 ID'로만
    나뉜다. 둘 다 설정돼 있으면 각각 호출해 결과를 합친다(한쪽만 있어도 동작해야 하므로
    gmarket_id/auction_id는 required_keys에 넣지 않는다 — 넣으면 단일 채널 판매자가
    영원히 '키 대기' 상태가 된다).
 4) 주문 쪼개짐: 한 주문번호가 상품 수만큼 여러 행으로 내려온다 → 주문번호 단위로 합치고
    대표 상품명 + 나머지는 optionName에 " / "로 잇는다.
 5) 상태: 1=결제완료(주문확인 전), 2=배송준비중(주문확인 완료)까지만 수집한다.
    3=배송중 / 4=배송완료 / 5=구매결정완료는 이미 나간 건이라 가져오지 않는다.
 6) 인코딩: 응답이 JSON(UTF-8)이라 고도몰(XML)과 달리 별도 디코딩은 필요 없지만,
    서버가 charset을 안 붙이고 내려주면 requests가 latin-1로 오해하므로 항상
    resp.content를 UTF-8로 직접 디코딩한다(한글 수취인/주소 깨짐 방지).
 7) 발송처리: ShippingDate는 '호출일 기준 2일 이내'만 허용되고, 주문확인(OrderCheck)이
    선행돼야 한다. 스타배송 주문은 계약 택배사만 가능(CJ대한통운=10013 / 한진 10007 /
    롯데 10008) — 예시 운영사는 CJ이므로 기본값 10013.
 8) 페이징: 규격서상 페이징 파라미터가 명확히 확인되지 않았다. 잘려 들어오는 사고를 막으려
    PageNo를 올려가며 더 읽되, 서버가 파라미터를 무시하고 같은 페이지를 반복하면
    (첫/끝 주문번호+건수 시그니처가 같으면) 즉시 멈춘다. 무한루프 방지.
"""
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime

import requests

from .base import MallAdapter, MallError, register

BASE_URL = "https://sa2.esmplus.com"
ORDERS_PATH = "/shipping/v1/Order/RequestOrders"
ORDER_CHECK_PATH = "/shipping/v1/Order/OrderCheck/{order_no}"
SHIPPING_PATH = "/shipping/v1/Delivery/ShippingInfo"

# 사이트 구분값 — 클레임 API의 SiteType(1=옥션, 2=G마켓) 기준.
# ★주문조회 body의 사이트 구분 필드명/값도 같은지 규격서로 확인 필요.
SITE_AUCTION = 1
SITE_GMARKET = 2

# 수집 대상 조회상태: 1=결제완료(주문확인 전), 2=배송준비중(주문확인 완료)
COLLECT_STATUSES = (1, 2)

# 날짜조건: 1=주문일 (결제완료일/발송마감일 등 다른 값이 있으나 신규주문 수집은 주문일 기준)
DATE_TYPE_ORDER = 1

CJ_COURIER_CODE = "10013"      # CJ대한통운 (예시 운영사 기본 택배사)

PAGE_SIZE = 200                # 서버가 무시할 수 있음 — 페이징 종료 판단용 힌트일 뿐
MAX_PAGES = 30                 # 안전장치(한 조회창에서 최대 6,000건)
RATE_RETRY = 2                 # 레이트리밋(3000/429) 재시도 횟수
RATE_WAIT = 6.0                # 재시도 전 대기(초) — 5초 제한보다 길게

TIMEOUT = 25                   # 초. 규칙상 20 이상

# 성공으로 볼 ResultCode. ★규격서 확인 후 보정 필요(미확인 코드는 실패로 본다).
OK_CODES = {"", "0", "00", "000", "0000", "200", "ok", "true", "success"}

# 응답 상태값에 이 말이 섞여 있으면 이미 나간/끝난 건이라 수집하지 않는다(2차 방어).
EXCLUDE_STATUS_WORDS = ("배송중", "배송완료", "구매결정", "취소", "반품", "교환", "미수령")


def _b64url(raw: bytes) -> str:
    """JWT용 base64url — 패딩(=)을 떼야 한다."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pick(row, *names):
    """응답 필드 이름이 문서/사이트마다 미세하게 다를 수 있어 후보를 순서대로 본다.

    대소문자 차이(OrderNo vs orderNo)까지 흡수한다.
    """
    if not isinstance(row, dict):
        return ""
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return str(v).strip()
    lowered = {str(k).lower(): v for k, v in row.items()}
    for n in names:
        v = lowered.get(n.lower())
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _int(v):
    try:
        return int(float(str(v).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def _clean_dt(v):
    """'2026-07-28T13:05:00' / '2026-07-28 13:05:00.000' → '2026-07-28 13:05:00'."""
    s = (v or "").strip().replace("T", " ")
    return s[:19]


@register
class EsmAdapter(MallAdapter):
    code = "esm"
    name = "ESM"
    # G마켓 31일 / 옥션 180일 → 짧은 쪽에 맞춰 여유 있게 자른다.
    max_window_days = 25
    # 판매자 ID당 5초 1회 제한 → 5.5초로 넉넉히.
    call_interval = 5.5
    # ★레지스트리(app/malls/__init__.py) fields의 key와 철자가 정확히 같아야 한다.
    #   gmarket_id / auction_id는 '둘 중 하나만' 있어도 되므로 필수에서 뺐고,
    #   둘 다 비었을 때는 수집 시점에 한국어 메시지로 알린다.
    required_keys = ("secret_key", "master_id")

    # ---- 인증 -------------------------------------------------------------
    def _jwt(self):
        """Secret Key로 HS256 JWT를 자가 서명한다.

        base64url(header) + "." + base64url(payload) + "." + base64url(HMAC-SHA256)
        ★header의 kid, payload의 iss/domain/aud, 만료(exp) 길이는 ESM 규격서가 정본이다.
          키 발급 후 반드시 규격서로 확인할 것(특히 사이트/판매자 ID 클레임이 필요한지).
        """
        master_id = (self.s.get("master_id") or "").strip()
        secret = (self.s.get("secret_key") or "").strip()
        if not master_id or not secret:
            raise MallError("ESM 인증정보(Secret Key / 마스터 ID)가 비어 있습니다. 설정에서 입력해 주세요.")
        now = int(time.time())
        header = {"alg": "HS256", "typ": "JWT", "kid": master_id}
        payload = {
            "iss": master_id,
            "domain": "sell",
            "aud": "esmplus",
            "iat": now,
            "exp": now + 300,   # 5분. 호출마다 새로 만들기 때문에 짧게 잡아도 된다.
        }
        # separators로 공백을 없애야 서명 대상 문자열이 서버와 어긋나지 않는다.
        # ensure_ascii=True라 한글 ID가 섞여도 순수 ASCII 세그먼트가 나온다.
        def seg(obj):
            return _b64url(json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))

        signing_input = f"{seg(header)}.{seg(payload)}"
        # ★Secret Key가 base64로 발급되는 규격이면 여기서 base64.b64decode(secret)로 바꿔야 한다.
        sig = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
        return f"{signing_input}.{_b64url(sig)}"

    # ---- HTTP -------------------------------------------------------------
    def _post(self, path, body, timeout=TIMEOUT):
        url = BASE_URL + path
        for attempt in range(RATE_RETRY + 1):
            headers = {
                # 만료 5분짜리 토큰이라 재시도할 때마다 새로 만든다.
                "Authorization": f"Bearer {self._jwt()}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "User-Agent": "OWS/1.0",
            }
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            try:
                resp = requests.post(url, data=raw, headers=headers, timeout=timeout)
            except requests.Timeout as e:
                raise MallError(f"ESM 응답이 {timeout}초 안에 오지 않았습니다. 잠시 후 다시 시도해 주세요.") from e
            except requests.RequestException as e:
                raise MallError(f"ESM 서버에 연결하지 못했습니다(네트워크/방화벽 확인): {e}") from e

            # --- 원인 구분: 인증 / 권한·IP / 레이트리밋 / 서버장애
            if resp.status_code in (401, 403):
                raise MallError(
                    "ESM 인증에 실패했습니다(HTTP %d). Secret Key·마스터 ID가 맞는지, "
                    "ESM+ [ESM+계정(ID)관리 > 셀링툴 관리]에서 셀링툴 사용 설정과 호출 IP 등록이 "
                    "되어 있는지 확인해 주세요. 응답: %s" % (resp.status_code, self._head(resp)))
            if resp.status_code == 429:
                if attempt < RATE_RETRY:
                    time.sleep(RATE_WAIT)
                    continue
                raise MallError("ESM 호출 한도를 넘었습니다(주문조회는 판매자 ID당 5초에 1회). "
                                "잠시 후 다시 시도해 주세요.")
            if resp.status_code == 404:
                raise MallError(f"ESM 엔드포인트를 찾을 수 없습니다({path}). API 경로 또는 사용 권한을 확인해 주세요.")
            if resp.status_code >= 500:
                raise MallError(f"ESM 서버 오류(HTTP {resp.status_code})입니다. 잠시 후 다시 시도해 주세요.")
            if resp.status_code != 200:
                raise MallError(f"ESM 호출 실패(HTTP {resp.status_code}): {self._head(resp)}")

            # charset 미표기 대비 — 항상 UTF-8로 직접 디코딩(한글 깨짐 방지)
            text = resp.content.decode("utf-8", "replace")
            try:
                data = json.loads(text) if text.strip() else {}
            except ValueError as e:
                raise MallError(f"ESM 응답을 해석하지 못했습니다(JSON 아님): {text[:200]}") from e

            code = _pick(data, "ResultCode", "resultCode", "Code", "code")
            msg = _pick(data, "ResultMessage", "resultMessage", "Message", "message", "Msg")
            if str(code) == "3000":
                # 조회 한도 초과 — 5초 제한. 조금 더 기다렸다 재시도한다.
                if attempt < RATE_RETRY:
                    time.sleep(RATE_WAIT)
                    continue
                raise MallError("ESM 주문조회 한도(판매자 ID당 5초 1회)를 넘었습니다. "
                                "자동수집 주기를 1분 이상으로 늘려 주세요.")
            if str(code).lower() not in OK_CODES:
                raise MallError(self._explain(code, msg))
            return data
        raise MallError("ESM 호출을 반복 시도했지만 실패했습니다. 잠시 후 다시 시도해 주세요.")

    @staticmethod
    def _head(resp):
        try:
            return resp.content.decode("utf-8", "replace")[:200]
        except Exception:      # noqa: BLE001 — 진단용 문자열이라 실패해도 흐름을 막지 않는다
            return "(본문 없음)"

    @staticmethod
    def _explain(code, msg):
        """오류 메시지에서 원인을 짚어 사람이 바로 조치할 수 있게 한다."""
        text = msg or "메시지 없음"
        hint = ""
        low = f"{code} {text}".lower()
        if "ip" in low:
            hint = " → 호출 서버 IP가 ESM에 등록되어 있는지 확인해 주세요."
        elif "셀링툴" in text or "미승인" in text or "권한" in text:
            hint = " → ESM+ [셀링툴 관리]에서 셀링툴 사용함/업체 선택이 저장되었는지 확인해 주세요(반영에 1~2일)."
        elif "인증" in text or "토큰" in text or "token" in low or "jwt" in low:
            hint = " → Secret Key·마스터 ID와 JWT 클레임 규격을 확인해 주세요."
        elif "판매자" in text or "seller" in low:
            hint = " → G마켓/옥션 판매자 ID가 마스터 ID에 묶여 있는지 확인해 주세요."
        return f"ESM 오류({code or '코드없음'}): {text}{hint}"

    # ---- 주문 수집 --------------------------------------------------------
    def _sites(self):
        """설정된 판매자 ID만 조회 대상으로 삼는다. (사이트값, 라벨, 판매자ID) 목록."""
        out = []
        gid = (self.s.get("gmarket_id") or "").strip()
        aid = (self.s.get("auction_id") or "").strip()
        if gid:
            out.append((SITE_GMARKET, "G마켓", gid))
        if aid:
            out.append((SITE_AUCTION, "옥션", aid))
        return out

    def collect_orders(self, since, until):
        sites = self._sites()
        if not sites:
            raise MallError("G마켓 판매자 ID와 옥션 판매자 ID가 모두 비어 있습니다. "
                            "설정에서 최소 한 개는 입력해 주세요.")
        # (사이트, 주문번호) → 상품행 묶음. 주문 단위로 합치려고 순서를 유지한 dict를 쓴다.
        grouped = {}
        first_call = True
        for start, end in self.windows(since, until):
            for site, label, seller_id in sites:
                for status in COLLECT_STATUSES:
                    if not first_call:
                        self.pace()      # ★호출 사이 5.5초 — 5초 제한 회피
                    first_call = False
                    for row in self._request_orders(site, seller_id, status, start, end):
                        if not self._collectable(row):
                            continue
                        order_no = _pick(row, "OrderNo", "orderNo", "OrderNumber", "OrderID")
                        if not order_no:
                            continue
                        grouped.setdefault((label, order_no), []).append(row)
        return [self._build(label, order_no, rows) for (label, order_no), rows in grouped.items()]

    def _request_orders(self, site, seller_id, status, start, end):
        """한 사이트·한 상태·한 조회창을 (페이징까지 포함해) 읽는다."""
        rows = []
        seen_pages = set()
        for page in range(1, MAX_PAGES + 1):
            if page > 1:
                self.pace()          # 페이지 넘김도 같은 조회 한도에 걸린다
            body = {
                # ★필드명은 조사자료(조회상태/날짜조건/시작·종료일시/사이트 구분) 기준의 잠정 매핑이다.
                #   키 발급 후 ESM 규격서로 정확한 이름을 확인할 것.
                "SellerId": seller_id,
                "SiteType": site,                       # 1=옥션, 2=G마켓
                "SearchStatus": status,                 # 1=결제완료, 2=배송준비중
                "SearchDateType": DATE_TYPE_ORDER,      # 1=주문일
                # 분 단위까지만 받는다(초를 붙이면 형식 오류가 난다)
                "SearchStartDate": start.strftime("%Y-%m-%d %H:%M"),
                "SearchEndDate": end.strftime("%Y-%m-%d %H:%M"),
                "PageNo": page,
                "PageSize": PAGE_SIZE,
            }
            data = self._post(ORDERS_PATH, body)
            page_rows = self._rows(data)
            if not page_rows:
                break
            # 서버가 PageNo를 무시하고 같은 페이지를 계속 주는 경우를 잡아 무한루프를 막는다.
            sig = (len(page_rows),
                   _pick(page_rows[0], "OrderNo", "orderNo"),
                   _pick(page_rows[-1], "OrderNo", "orderNo"))
            if sig in seen_pages:
                break
            seen_pages.add(sig)
            rows.extend(page_rows)
            if len(page_rows) < PAGE_SIZE:
                break
        return rows

    @staticmethod
    def _rows(data):
        """응답에서 주문 행 목록을 꺼낸다. 감싸는 키 이름이 문서마다 달라 후보를 훑는다."""
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if not isinstance(data, dict):
            return []
        for key in ("Result", "Results", "Data", "Orders", "OrderList", "Items", "List",
                    "result", "data", "orders"):
            v = data.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
            if isinstance(v, dict):
                # {"Result": {"Orders": [...]}} 형태
                for inner in v.values():
                    if isinstance(inner, list) and all(isinstance(r, dict) for r in inner):
                        return inner
        # 마지막 수단: dict 값 중 '딕셔너리들의 리스트'를 찾는다
        for v in data.values():
            if isinstance(v, list) and v and all(isinstance(r, dict) for r in v):
                return v
        return []

    @staticmethod
    def _collectable(row):
        """이미 배송중/완료/취소된 건은 버린다(조회 상태로 걸렀지만 2차 방어)."""
        st = _pick(row, "OrderStatus", "orderStatus", "Status", "OrderStatusName", "StatusName")
        if not st:
            return True
        if st in ("1", "2"):
            return True
        if st in ("3", "4", "5"):
            return False
        return not any(w in st for w in EXCLUDE_STATUS_WORDS)

    def _build(self, site_label, order_no, rows):
        """상품 여러 행 → 주문 1건. 대표 상품명 + 나머지는 optionName에 " / "로 잇는다."""
        main = rows[0]
        options = []
        qty = 0
        amount = 0
        for row in rows:
            opt = _pick(row, "ItemName", "itemName", "OptionName", "GoodsOption", "ItemOption")
            name = _pick(row, "GoodsName", "goodsName", "ProductName", "ItemTitle")
            if row is main:
                if opt:
                    options.append(opt)
            else:
                # 두 번째 행부터는 '상품명(옵션)'을 통째로 옵션칸에 이어붙인다
                label = f"{name}({opt})" if name and opt else (name or opt)
                if label:
                    options.append(label)
            qty += max(1, _int(_pick(row, "Quantity", "quantity", "Qty", "OrderQty", "Ea")))
            # ★Price가 '단가'로 내려오는 규격이면 수량을 곱해야 한다 — 규격서 확인 필요.
            #   현재는 행별 결제금액 합계로 본다.
            amount += _int(_pick(row, "Price", "price", "OrderAmount", "PaymentAmount",
                                 "SellingPrice", "SettlePrice", "GoodsPrice"))

        addr = " ".join(x for x in (
            _pick(main, "Address", "address", "ReceiverAddress", "ReceiverAddr", "Addr"),
            _pick(main, "AddressDetail", "ReceiverAddressDetail", "AddrDetail", "DetailAddress"),
        ) if x)

        return self.order(
            # 몰이름:주문번호. G마켓/옥션은 주문번호 체계가 달라 실무상 겹치지 않는다.
            importKey=f"ESM:{order_no}",
            channel=f"ESM({site_label})",       # 어느 사이트에서 왔는지 화면에서 구분되게
            orderNumber=order_no,
            orderedAt=_clean_dt(_pick(main, "OrderDate", "orderDate", "OrderDateTime", "RegDate")),
            productName=_pick(main, "GoodsName", "goodsName", "ProductName", "ItemTitle"),
            optionName=" / ".join(options),
            productCode=_pick(main, "GoodsNo", "goodsNo", "ItemNo", "GoodsCode", "SellerItemNo"),
            quantity=max(1, qty),
            amount=amount,
            # 수령인이 비면 구매자명으로 대체(선물하기 등에서 비어 오는 경우 대비)
            recipient=_pick(main, "ReceiverName", "receiverName", "BuyerName", "buyerName"),
            phone=_pick(main, "ReceiverPhone", "receiverPhone", "ReceiverMobile",
                        "ReceiverHp", "BuyerPhone"),
            postalCode=_pick(main, "ZipCode", "zipCode", "ReceiverZipCode", "PostNo"),
            address=addr,
            deliveryMessage=_pick(main, "DeliveryMessage", "deliveryMessage", "ShippingMessage",
                                  "DeliveryMemo", "Memo"),
        )

    # ---- 발송처리 ---------------------------------------------------------
    def confirm_order(self, order_no):
        """주문확인(결제완료 → 배송준비중). 발송처리 전에 선행해야 한다."""
        self._post(ORDER_CHECK_PATH.format(order_no=str(order_no)), {})
        return True

    def upload_invoice(self, order_no, invoice_no, courier_code="", shipping_date=None,
                       seller_order_no="", **kw):
        """송장 전송(발송처리).

        ★ShippingDate는 '호출일 기준 2일 이내'만 허용된다. 기본값은 지금 시각.
        ★주문확인(OrderCheck)이 선행돼야 하므로, 실제 배선 시 confirm_order를 먼저 부르거나
          이미 배송준비중인 주문만 넘길 것.
        ★스타배송 주문은 계약 택배사(CJ 10013 / 한진 10007 / 롯데 10008)만 가능하고,
          3PL 주문은 API로 처리되지 않는다.
        """
        if isinstance(shipping_date, datetime):
            ship = shipping_date.strftime("%Y-%m-%dT%H:%M:%S")
        elif shipping_date:
            ship = str(shipping_date)
        else:
            ship = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        body = {
            "OrderNo": str(order_no),
            "ShippingDate": ship,
            "DeliveryCompanyCode": str(courier_code or CJ_COURIER_CODE),
            "InvoiceNo": str(invoice_no),
        }
        if seller_order_no:
            body["SellerOrderNo"] = str(seller_order_no)[:30]   # 규격상 30바이트 제한
        self._post(SHIPPING_PATH, body)
        return True
