"""테무(Temu) 주문 수집.

- 단일 게이트웨이: POST https://<host>/openapi/router  (host는 설정 region으로 교체)
- 인증: app_key + access_token + MD5 서명(sign). 공통 파라미터 type/app_key/access_token/
  timestamp(초)/data_type="JSON" 에 업무 파라미터를 더해 키 기준 오름차순 정렬 →
  "k1v1k2v2..." 로 이어붙이고 앞뒤에 app_secret을 붙인 뒤 MD5 대문자 hexdigest.
- 주문 조회: type="bg.order.list.v2.get" (create_after / create_before = 초 단위 epoch)
- 조회 기간 7일, 약 20 QPS 제한 → call_interval 0.3

★몰별 함정(반드시 읽을 것)
1) 수취인 개인정보(이름·연락처·주소)가 암호화되어 온다. 평문은 별도 권한이 필요한
   bg.order.decryptshippinginfo.get 로만 얻을 수 있다. 권한이 없으면 그 필드만 비우고
   주문 자체는 수집한다(수집 전체가 실패하면 안 됨). 이때 memo에 사유를 남겨 화면에서
   "왜 주소가 비었는지"를 알 수 있게 했다.
2) PENDING(결제 확정 전) 주문은 제외한다. 배송중/배송완료/취소/환불 건도 신규 주문이
   아니므로 제외한다(수집 대상 = 결제완료~상품준비중).
3) 응답 구조가 문서 로그인 장벽 때문에 확정되지 않았다(docs/research/API_테무.json 참고).
   그래서 키 후보를 순서대로 찾는 _first/_pick_list 헬퍼를 쓰고, 어떤 후보에도 걸리지
   않으면 원본 일부를 담은 MallError로 올려 디버깅이 가능하게 했다.
4) 서명 문자열과 실제 전송값이 조금이라도 다르면 sign 불일치가 난다. 그래서 중첩값은
   미리 compact JSON 문자열로 만들어 params에 넣고, 그 문자열 그대로 전송한다.
   한글이 섞여도 서버가 JSON을 먼저 디코드해 검증하므로 UTF-8로 해시하면 된다.
5) 금액 단위(원/센트)와 통화가 문서상 미확정이다. 소수점이 있으면 반올림해 정수로 만들고,
   KRW가 아닌 통화면 memo에 통화를 남겨 대표가 확인할 수 있게 했다.
6) ★★키가 나오면 **금액 기준부터 맞춰야 한다** — 지금 이 경로는 정산 가산율을 안 건다.
   대표 확정(2026-09-07): 테무는 부가세를 뺀 판매가로 주문을 주고, 정산은 거기에
   +10.75% 다. 엑셀 임포터는 그래서 주문 합계에 가산율을 건다
   (app/importers/mall_excel.py temu_settlement_amount).
   여기서 같이 걸지 않은 이유는 위 3)·5) 그대로다 — 이 어댑터가 amount 로 집는
   goodsAmount/salePrice 류가 '우리 판매가'인지 '소비자 결제 총액'인지 응답 스펙이
   확정되지 않았다. 소비자 결제 총액이면 부가세가 이미 들어 있어 한 번 더 곱하는 순간
   매출이 통째로 부풀고, 아무 화면에서도 안 걸린다. 그래서 확인 전에는 안 건다.
   ★이 어댑터와 엑셀은 importKey(`테무:{주문ID}`)를 공유하므로, 켜는 순간 같은 주문이
     수집 경로에 따라 다른 금액이 되고 '먼저 들어온 쪽이 이긴다'. 켜기 전에 실주문
     1건을 테무 정산 명세서와 대조해 기준을 확정하고, 그 결과대로 여기서도
     temu_settlement_amount 를 부르거나 부르지 않도록 정해야 한다.

키 발급 전에는 설정에서 '테무 사용'이 꺼져 있어 어댑터 자체가 만들어지지 않는다.
"""
import hashlib
import json
import time
from datetime import datetime

import requests

from .. import config
from .base import MallAdapter, MallError, register

ROUTER_PATH = "/openapi/router"

# region 설정값 → 게이트웨이 호스트. 마켓(지역)별로 토큰·포털이 분리되어 있어
# 글로벌 외 지역을 쓰는 경우를 대비해 매핑을 둔다. 모르는 값이면 그 값을 호스트로 본다.
REGION_HOSTS = {
    "": "openapi-b-global.temu.com",
    "global": "openapi-b-global.temu.com",
    "us": "openapi-b-us.temu.com",
    "eu": "openapi-b-eu.temu.com",
}

METHOD_ORDER_LIST = "bg.order.list.v2.get"
METHOD_DECRYPT = "bg.order.decryptshippinginfo.get"
# ★송장 전송 메서드명은 공개 문서에서 확인되지 않았다(파트너 로그인 후 확인 필요).
#   플레이오토/사방넷이 Order V2의 발송 API로 송장 전송을 서비스 중이라
#   bg.logistics.shipment.* 계열로 추정한다. 실제 배선 전 반드시 검증할 것.
METHOD_SHIP = "bg.logistics.shipment.create"

# 주문 조회 시 서버에 요청하는 상태값. 문서 미확인이라 "발송대기(상품준비중)"로 추정되는
# 1을 쓴다. 서버 필터를 신뢰하지 않고 아래 _status_ok로 한 번 더 거른다.
COLLECT_ORDER_STATUS = 1
# 숫자 상태코드로 올 때 수집할 값(0=결제 확정 전으로 추정 → 제외).
COLLECT_STATUS_CODES = (1,)

# 문자열 상태값이 올 때의 판정 키워드.
# ★순서가 중요하다: "UN_SHIPPING"(발송대기=수집 대상)에 SHIP이, "PENDING_SHIPMENT"에
#   PENDING이 들어 있어 단순 부분일치로 판정하면 정상 주문을 통째로 버린다.
#   그래서 (1)확실한 제외 → (2)확실한 수집 → (3)애매한 PENDING/SHIP 제외 순으로 본다.
DENY_WORDS = ("UNPAID", "UN_PAID", "WAIT_PAY", "WAITPAY", "TO_PAY", "PAYING",
              "CANCEL", "REFUND", "RETURN", "CLOSE", "TRANSIT", "DELIVER",
              "RECEIPT", "RECEIVED", "COMPLETE", "FINISH")
COLLECT_WORDS = ("UN_SHIP", "UNSHIP", "TO_SHIP", "WAIT_SHIP", "AWAIT", "PENDING_SHIP",
                 "PREPAR", "PROCESS", "CONFIRM", "PLACED", "PAID")

PAGE_SIZE = 50
MAX_PAGES = 200          # 무한 페이징 방지(이론상 1만 건)
DECRYPT_FAIL_LIMIT = 3   # 복호화가 계속 실패하면 그만 시도한다(수집 지연 방지)

# 응답에서 주문 목록이 담길 만한 자리 후보(문서 미확정 → 순서대로 탐색)
LIST_PATHS = (
    "result.pageItems", "result.page_items", "result.orderList", "result.order_list",
    "result.dataList", "result.data_list", "result.items", "result.list",
    "result.orders", "result.orderSnList", "result.parentOrderList",
    "result.parent_order_list", "result.data.pageItems", "result.data.list",
    "pageItems", "page_items", "orderList", "order_list", "items", "list", "orders",
)
# 주문 안에서 상품행이 담길 자리 후보
ITEM_PATHS = (
    "orderItemList", "order_item_list", "orderItems", "order_items",
    "itemList", "item_list", "skuList", "sku_list", "goodsList", "goods_list",
    "orderGoodsList", "order_goods_list", "subOrderList", "sub_order_list",
    "orderSnList", "children",
)


# ---------------------------------------------------------------- 공통 헬퍼
def _first(obj, *paths, default=""):
    """키 이름 후보를 순서대로 찾아 첫 값을 돌려준다("a.b" 중첩 경로 지원).

    테무 문서가 camelCase/snake_case를 섞어 쓰고 응답 스펙이 확정되지 않아,
    한 필드마다 후보를 여러 개 두고 먼저 걸리는 값을 쓴다.
    """
    for path in paths:
        cur = obj
        ok = True
        for part in str(path).split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None and cur != "" and cur != [] and cur != {}:
            return cur
    return default


def _pick_list(obj, paths):
    """리스트가 들어 있는 자리를 찾는다.

    '키가 아예 없음(None)'과 '키는 있는데 빈 목록([])'을 구분해야
    주문 0건과 구조 불일치를 헷갈리지 않는다.
    """
    for path in paths:
        cur = obj
        ok = True
        for part in str(path).split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and isinstance(cur, list):
            return cur
    return None


def _int(v, default=0):
    try:
        return int(round(float(str(v).replace(",", "").strip() or default)))
    except (TypeError, ValueError):
        return default


def _money(v):
    """금액 → 정수. 단위(원/센트)는 문서 미확정이라 값 그대로 정수화만 한다."""
    return _int(v, 0)


def _sign_value(v):
    """서명용 문자열화 — 전송값과 완전히 같은 표현이어야 한다."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return str(v)


def _ts_to_text(v):
    """주문일시 → 'YYYY-MM-DD HH:MM:SS'(KST).

    epoch가 초/밀리초 둘 다 올 수 있어 자릿수로 구분한다. 이미 문자열 날짜면 그대로 쓴다.
    """
    if v in (None, ""):
        return ""
    s = str(v).strip()
    if not s.replace(".", "", 1).isdigit():
        return s                       # 이미 사람이 읽는 날짜 문자열
    num = float(s)
    if num > 1e11:                     # 밀리초로 판단
        num /= 1000.0
    try:
        return datetime.fromtimestamp(num, config.KST).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return s


def _snippet(obj, limit=400):
    """디버깅용 원본 일부 — 키를 몰라도 구조는 보이게 한다."""
    try:
        text = json.dumps(obj, ensure_ascii=False)[:limit]
    except (TypeError, ValueError):
        text = str(obj)[:limit]
    return text


@register
class TemuAdapter(MallAdapter):
    code = "temu"
    name = "테무"
    max_window_days = 7            # 조회 기간 제한(문서 미확정 → 보수적으로 7일)
    call_interval = 0.3            # 약 20 QPS 제한 → 여유 있게
    # ★레지스트리(app/malls/__init__.py) fields의 key와 철자가 정확히 같아야 한다.
    #   region은 비워도 글로벌 게이트웨이로 동작하므로 필수에서 뺀다.
    required_keys = ("app_key", "app_secret", "access_token")

    def __init__(self, settings):
        super().__init__(settings)
        self._decrypt_cache = {}     # 주문번호 → 수취인 정보(같은 주문 재호출 방지)
        self._decrypt_off = ""       # 값이 있으면 복호화 포기 사유(주문은 계속 수집)
        self._decrypt_fails = 0

    # ------------------------------------------------------------ 호출 계층
    def _endpoint(self):
        """설정 region으로 게이트웨이 주소를 만든다.

        region에 별칭(global/us/eu), 호스트, 전체 URL 중 무엇을 넣어도 동작하게 했다.
        (대표가 문서를 보고 지역별 주소를 그대로 붙여넣는 경우가 많다.)
        """
        region = (self.s.get("region") or "").strip().lower().rstrip("/")
        host = REGION_HOSTS.get(region, region or REGION_HOSTS[""])
        if host.startswith("http://") or host.startswith("https://"):
            return host if ROUTER_PATH in host else host + ROUTER_PATH
        if ROUTER_PATH.strip("/") in host:
            return f"https://{host}"
        return f"https://{host}{ROUTER_PATH}"

    def _sign(self, params):
        """정렬 → k1v1k2v2… → 앞뒤에 app_secret → MD5 대문자."""
        secret = (self.s.get("app_secret") or "").strip()
        parts = [secret]
        for k in sorted(params):
            if k == "sign" or params[k] is None:
                continue
            parts.append(str(k))
            parts.append(_sign_value(params[k]))
        parts.append(secret)
        return hashlib.md5("".join(parts).encode("utf-8")).hexdigest().upper()

    def _call(self, method, biz=None, timeout=25):
        """게이트웨이 호출 1회. 실패 원인(인증/권한/IP/레이트리밋)을 구분해 올린다."""
        params = {
            "type": method,
            "app_key": (self.s.get("app_key") or "").strip(),
            "access_token": (self.s.get("access_token") or "").strip(),
            "timestamp": int(time.time()),          # 초 단위(밀리초 아님)
            "data_type": "JSON",
        }
        for k, v in (biz or {}).items():
            if v is None:
                continue
            # 중첩값은 서명과 전송이 어긋나지 않도록 미리 같은 문자열로 고정한다.
            params[k] = _sign_value(v) if isinstance(v, (dict, list)) else v
        params["sign"] = self._sign(params)

        try:
            resp = requests.post(
                self._endpoint(), json=params, timeout=timeout,
                headers={"Content-Type": "application/json;charset=UTF-8",
                         "Accept": "application/json", "User-Agent": "OWS/1.0"})
        except requests.Timeout as e:
            raise MallError(f"테무 응답이 {timeout}초 안에 오지 않았습니다(네트워크/게이트웨이 확인).") from e
        except requests.RequestException as e:
            raise MallError(f"테무 호출 실패: {e}") from e

        if resp.status_code == 429:
            raise MallError("테무 호출 한도(약 20 QPS)를 넘었습니다. 잠시 후 다시 수집해 주세요.")
        if resp.status_code in (401, 403):
            raise MallError("테무 인증/권한 오류입니다(HTTP %s). App Key·App Secret·Access Token과 "
                            "앱 권한 승인 여부를 확인해 주세요." % resp.status_code)
        if resp.status_code >= 500:
            raise MallError(f"테무 서버 오류(HTTP {resp.status_code}). 잠시 후 다시 시도해 주세요.")
        if resp.status_code != 200:
            raise MallError(f"테무 호출 실패(HTTP {resp.status_code}): {resp.text[:200]}")

        try:
            data = resp.json()
        except ValueError as e:
            raise MallError(f"테무 응답이 JSON이 아닙니다: {resp.text[:200]}") from e
        if not isinstance(data, dict):
            raise MallError(f"테무 응답 구조가 달라 파싱하지 못했습니다(원본 일부): {_snippet(data)}")
        self._check_error(data, method)
        return data

    def _check_error(self, data, method):
        """success/errorCode를 보고 원인을 한국어로 구분한다(코드표가 미공개라 문구도 함께 본다)."""
        success = data.get("success")
        code = _first(data, "errorCode", "error_code", "code", "sub_code", default="")
        msg = str(_first(data, "errorMsg", "error_msg", "errorMessage", "message", "msg",
                         "sub_msg", default=""))
        ok_codes = ("", "0", "1000000", "200", "SUCCESS")
        if success is True and str(code) in ok_codes:
            return
        if success is None and str(code) in ok_codes and not msg:
            return

        low = msg.lower()
        if any(w in low for w in ("sign", "signature")):
            raise MallError(f"테무 서명(sign) 검증 실패입니다. App Secret이 정확한지 확인해 주세요. "
                            f"[{code}] {msg}")
        if any(w in low for w in ("token", "expire", "unauthor", "invalid app", "app_key", "appkey")):
            raise MallError(f"테무 인증 실패입니다. Access Token이 만료·무효이거나 App Key가 다릅니다. "
                            f"셀러센터에서 앱 승인·토큰 재발급을 확인해 주세요. [{code}] {msg}")
        if any(w in low for w in ("permission", "not allow", "no access", "denied", "scope", "privilege")):
            raise MallError(f"테무 권한이 없습니다({method}). 셀러센터에서 해당 API 권한을 부여한 뒤 "
                            f"토큰을 재발급해야 합니다. [{code}] {msg}")
        if "ip" in low and any(w in low for w in ("white", "allow", "list", "bind", "register")):
            raise MallError(f"호출 IP가 테무에 등록되지 않았습니다. 파트너 콘솔에 서버 IP를 등록해 주세요. "
                            f"[{code}] {msg}")
        if any(w in low for w in ("rate", "qps", "frequen", "too many", "limit exceed", "flow")):
            raise MallError(f"테무 호출 한도를 넘었습니다(약 20 QPS). 잠시 후 다시 시도해 주세요. [{code}] {msg}")
        raise MallError(f"테무 오류[{code}]: {msg or '메시지 없음'} (원본 일부: {_snippet(data, 200)})")

    # ------------------------------------------------------------ 주문 수집
    def collect_orders(self, since, until):
        """결제완료~상품준비중 신규 주문만 모아 주문 단위로 합쳐 돌려준다."""
        agg = {}          # 주문번호 → 누적 dict (여러 상품행/여러 페이지를 합침)
        order_seq = []    # 수집 순서 보존
        seen_nodes = 0    # 상태와 무관하게 파싱한 주문 노드 수(구조 검증용)
        got_no = 0        # 주문번호를 뽑아낸 노드 수

        for start, end in self.windows(since, until):
            page = 1
            while page <= MAX_PAGES:
                data = self._call(METHOD_ORDER_LIST, {
                    # 초 단위 epoch. since/until은 KST tz-aware라 timestamp()가 정확하다.
                    "create_after": int(start.timestamp()),
                    "create_before": int(end.timestamp()),
                    "page_number": page,
                    "page_size": PAGE_SIZE,
                    "order_status": COLLECT_ORDER_STATUS,
                })
                rows = _pick_list(data, LIST_PATHS)
                if rows is None:
                    # 목록 자리를 못 찾았다 → 스펙이 바뀐 것. 원본을 보여 디버깅 가능하게.
                    raise MallError("테무 응답 구조가 달라 파싱하지 못했습니다(원본 일부): "
                                    + _snippet(data))
                for node in rows:
                    if not isinstance(node, dict):
                        continue
                    seen_nodes += 1
                    if self._merge_node(node, agg, order_seq):
                        got_no += 1
                self.pace()
                if len(rows) < PAGE_SIZE:
                    break
                page += 1

        if seen_nodes and not got_no:
            # 주문은 왔는데 주문번호 키를 하나도 못 찾은 경우 = 구조 불일치
            raise MallError("테무 응답 구조가 달라 파싱하지 못했습니다(주문번호 키를 찾지 못함).")

        return [self._finish(agg[no]) for no in order_seq]

    def _status_ok(self, node):
        """PENDING(결제 전)·배송중/완료/취소 건을 걸러낸다."""
        status = _first(node, "orderStatus", "order_status", "parentOrderStatus",
                        "parent_order_status", "status", "fulfillmentStatus", default=None)
        if status is None:
            return True                      # 상태 키를 못 찾으면 서버 필터를 신뢰한다
        if isinstance(status, bool):
            return True
        if isinstance(status, (int, float)) or str(status).strip().lstrip("-").isdigit():
            return int(float(status)) in COLLECT_STATUS_CODES
        text = str(status).upper()
        if any(w in text for w in DENY_WORDS):
            return False                     # 결제 전·취소/환불·이미 배송된 건
        if any(w in text for w in COLLECT_WORDS):
            return True                      # 발송대기/상품준비중
        if "PENDING" in text:
            return False                     # 그냥 PENDING = 결제 확정 전 → 제외
        if "SHIP" in text:
            return False                     # SHIPPED/PARTIALLY_SHIPPED 등
        return True

    def _merge_node(self, node, agg, order_seq):
        """주문 노드 하나를 누적한다. 주문번호를 찾았으면 True."""
        order_no = str(_first(node, "parentOrderSn", "parent_order_sn", "orderSn", "order_sn",
                              "orderNumber", "order_number", "orderId", "order_id",
                              "parentOrderId", "parent_order_id", default="")).strip()
        if not order_no:
            return False
        if not self._status_ok(node):
            return True                      # 번호는 찾았으니 구조는 정상, 대상만 아님

        cur = agg.get(order_no)
        if cur is None:
            cur = {
                "orderNumber": order_no, "orderedAt": "", "names": [], "options": [],
                "productCode": "", "quantity": 0, "amount": 0, "total_hint": 0,
                "currency": "", "node": node,
            }
            agg[order_no] = cur
            order_seq.append(order_no)

        if not cur["orderedAt"]:
            cur["orderedAt"] = _ts_to_text(_first(
                node, "createTime", "create_time", "createdAt", "created_at",
                "parentOrderTime", "parent_order_time", "orderTime", "order_time",
                "payTime", "pay_time", default=""))
        if not cur["currency"]:
            cur["currency"] = str(_first(node, "currency", "currencyCode", "currency_code",
                                         default="")).upper()

        # 주문 총액은 상품행마다 반복될 수 있어 합치지 않고 최댓값만 기억한다(이중 계상 방지).
        cur["total_hint"] = max(cur["total_hint"], _money(_first(
            node, "orderAmount", "order_amount", "payAmount", "pay_amount",
            "totalAmount", "total_amount", "orderTotalAmount", "actualPaymentAmount",
            default=0)))

        for line in self._lines(node):
            name = str(_first(line, "goodsName", "goods_name", "productName", "product_name",
                              "skuName", "sku_name", "goodsTitle", "title", default="")).strip()
            if name:
                cur["names"].append(name)
            opt = str(_first(line, "specName", "spec_name", "optionName", "option_name",
                             "skuSpec", "sku_spec", "goodsSpec", "specifications",
                             "productSpec", default="")).strip()
            if opt:
                cur["options"].append(opt)
            if not cur["productCode"]:
                cur["productCode"] = str(_first(
                    line, "skuId", "sku_id", "goodsId", "goods_id", "productSkuId",
                    "product_sku_id", "outSkuSn", "out_sku_sn", "skuCode", "sku_code",
                    "productCode", default="")).strip()
            cur["quantity"] += _int(_first(line, "quantity", "goodsCount", "goods_count",
                                           "itemCount", "item_count", "purchaseQuantity",
                                           "num", default=0), 0)
            cur["amount"] += _money(_first(line, "goodsAmount", "goods_amount", "itemAmount",
                                           "item_amount", "itemTotalAmount", "totalAmount",
                                           "salePrice", "sale_price", "orderPrice",
                                           default=0))
        return True

    def _lines(self, node):
        """상품행 목록. 중첩 리스트가 없으면 노드 자체를 한 행으로 본다(플랫 응답 대비)."""
        rows = _pick_list(node, ITEM_PATHS)
        if rows is None:
            return [node]
        return [r for r in rows if isinstance(r, dict)] or [node]

    def _finish(self, cur):
        """누적본 → 임포터 주문 dict. 대표 상품명 1개 + 나머지는 optionName에 ' / '로 잇는다."""
        names = cur["names"]
        main = names[0] if names else ""
        extras = [x for x in (cur["options"] + names[1:]) if x]
        # 같은 옵션/상품명이 반복돼도 한 번만 남긴다(가독성).
        seen, uniq = set(), []
        for x in extras:
            if x not in seen:
                seen.add(x)
                uniq.append(x)

        ship = self._shipping(cur["node"], cur["orderNumber"])
        memo = []
        if ship.get("note"):
            memo.append(ship["note"])
        if cur["currency"] and cur["currency"] != "KRW":
            memo.append(f"통화 {cur['currency']} — 금액 단위 확인 필요")

        return self.order(
            importKey=f"테무:{cur['orderNumber']}",
            orderNumber=cur["orderNumber"],
            orderedAt=cur["orderedAt"],
            productName=main,
            optionName=" / ".join(uniq),
            productCode=cur["productCode"],
            quantity=max(1, cur["quantity"]),
            amount=cur["amount"] or cur["total_hint"],
            recipient=ship.get("recipient", ""),
            phone=ship.get("phone", ""),
            postalCode=ship.get("postalCode", ""),
            address=ship.get("address", ""),
            deliveryMessage=ship.get("deliveryMessage", ""),
            memo=" / ".join(memo),
        )

    # ------------------------------------------------ 수취인 정보(암호화 → 복호화)
    def _shipping(self, node, order_no):
        """수취인 정보를 채운다. 복호화 권한이 없으면 빈 값 + 사유 메모로 남긴다.

        ★핵심 정책: 복호화 실패로 주문 수집 전체가 죽으면 안 된다.
          - 권한/인증 계열 실패 → 이후 주문은 아예 시도하지 않고(호출 낭비 방지) 빈 값 처리
          - 일시적 실패도 DECRYPT_FAIL_LIMIT회 연속이면 같은 방식으로 중단
          송장 출력 전에 대표가 셀러센터에서 PII 권한을 부여하고 토큰을 재발급해야 한다.
        """
        info = {"recipient": "", "phone": "", "postalCode": "", "address": "",
                "deliveryMessage": "", "note": ""}
        # 1) 응답에 평문(또는 마스킹 평문)이 함께 오는 경우 먼저 쓴다.
        self._fill_from(info, node)
        for key in ("receiptAddressInfo", "receipt_address_info", "shippingInfo",
                    "shipping_info", "addressInfo", "address_info", "receiverInfo"):
            sub = node.get(key) if isinstance(node, dict) else None
            if isinstance(sub, dict):
                self._fill_from(info, sub)

        # 2) 개인정보가 비어 있으면 복호화 API로 채운다.
        need = not (info["recipient"] and info["address"])
        if not need:
            return info
        if self._decrypt_off:
            info["note"] = self._decrypt_off
            return info

        try:
            plain = self._decrypt(order_no)
        except MallError as e:
            text = str(e)
            fatal = any(w in text for w in ("권한", "인증", "서명", "만료"))
            self._decrypt_fails += 1
            if fatal or self._decrypt_fails >= DECRYPT_FAIL_LIMIT:
                self._decrypt_off = ("수취인 정보 복호화 실패 — 주소·연락처가 비어 있습니다. "
                                     "셀러센터에서 개인정보 복호화(bg.order.decryptshippinginfo.get) "
                                     f"권한 부여 후 토큰을 재발급해 주세요. 사유: {text}")
            info["note"] = self._decrypt_off or f"수취인 정보 복호화 실패: {text}"
            return info

        self._decrypt_fails = 0
        if plain:
            self._fill_from(info, plain)
        if not (info["recipient"] or info["address"]):
            info["note"] = "수취인 정보가 암호화되어 있어 주소·연락처를 채우지 못했습니다(복호화 권한 확인 필요)."
        return info

    def _decrypt(self, order_no):
        """bg.order.decryptshippinginfo.get 호출(주문당 1회, 결과 캐시).

        파라미터명(parent_order_sn)은 문서 미확정이라 응답이 비면 다른 이름으로
        바꿔봐야 할 수 있다 — 실패 시 원본 일부가 MallError에 담겨 나온다.
        """
        if order_no in self._decrypt_cache:
            return self._decrypt_cache[order_no]
        data = self._call(METHOD_DECRYPT, {"parent_order_sn": order_no})
        self.pace()
        plain = _first(data, "result.decryptShippingInfo", "result.decrypt_shipping_info",
                       "result.shippingInfo", "result.shipping_info", "result.addressInfo",
                       "result.address_info", "result", default=None)
        if isinstance(plain, list):
            plain = plain[0] if plain and isinstance(plain[0], dict) else None
        if plain is not None and not isinstance(plain, dict):
            plain = None
        self._decrypt_cache[order_no] = plain
        return plain

    @staticmethod
    def _fill_from(info, src):
        """이름이 제각각인 수취인 필드를 표준 키로 옮긴다(빈 값만 채운다)."""
        if not isinstance(src, dict):
            return
        pairs = (
            ("recipient", ("receiverName", "receiver_name", "recipientName", "recipient_name",
                           "consigneeName", "consignee_name", "name", "buyerName")),
            ("phone", ("receiverPhone", "receiver_phone", "recipientPhone", "mobile",
                       "phoneNumber", "phone_number", "telephone", "tel", "contactPhone")),
            ("postalCode", ("postCode", "post_code", "postalCode", "postal_code",
                            "zipCode", "zip_code", "zipcode")),
            ("deliveryMessage", ("buyerMemo", "buyer_memo", "remark", "note",
                                 "deliveryMessage", "delivery_message", "orderNote")),
        )
        for key, cands in pairs:
            if not info.get(key):
                val = str(_first(src, *cands, default="")).strip()
                if val:
                    info[key] = val
        if not info.get("address"):
            # 주소는 여러 조각(1행/2행/시/구/주)으로 쪼개져 오는 경우가 많아 이어붙인다.
            whole = str(_first(src, "fullAddress", "full_address", "detailAddress",
                               "detail_address", default="")).strip()
            if whole:
                info["address"] = whole
            else:
                parts = [str(_first(src, *cands, default="")).strip() for cands in (
                    ("regionName1", "region_name1", "province", "state"),
                    ("regionName2", "region_name2", "city"),
                    ("regionName3", "region_name3", "district", "county"),
                    ("addressLine1", "address_line1", "address1", "addressLine"),
                    ("addressLine2", "address_line2", "address2"),
                )]
                joined = " ".join(p for p in parts if p)
                if joined:
                    info["address"] = joined

    # ------------------------------------------------------------ 송장 전송
    def upload_invoice(self, order_no, invoice_no, courier_code="", **kw):
        """송장(운송장) 전송.

        ★아직 실제 배선 전이다. 정확한 메서드명·파라미터명은 파트너 로그인 후
          문서에서 확인해야 한다(METHOD_SHIP 상수 주석 참고).
        ★택배사 코드는 테무가 인식하는 값이어야 한다. 미지원 택배사로 올리면 추적이 안 돼
          사기(fraud) 분류 위험이 있다는 경고가 있으므로, 코드가 비면 아예 보내지 않는다.
        ★발송/추적 권한은 별도 부여 + 토큰 재발급이 필요하다(권한 없으면 여기서 MallError).
        """
        if not order_no or not invoice_no:
            raise MallError("테무 송장 전송: 주문번호와 송장번호가 모두 필요합니다.")
        if not courier_code:
            raise MallError("테무 송장 전송: 테무가 인식하는 택배사 코드가 필요합니다. "
                            "미지원 택배사로 올리면 추적 불가로 사기 분류될 수 있습니다.")
        biz = {
            "parent_order_sn": order_no,
            "tracking_number": invoice_no,
            "ship_company_id": courier_code,
        }
        # 부분출고 등 추가 파라미터는 호출부에서 그대로 넘길 수 있게 열어둔다.
        for k, v in (kw or {}).items():
            if v not in (None, ""):
                biz[k] = v
        data = self._call(METHOD_SHIP, biz)
        self.pace()
        return True
