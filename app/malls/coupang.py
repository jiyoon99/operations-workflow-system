"""쿠팡(Coupang WING) 발주서 수집.

- GET  {HOST}/v2/providers/openapi/apis/api/v5/vendors/{vendorId}/ordersheets
- 인증: HMAC-SHA256 자가서명. Authorization 헤더에
  "CEA algorithm=HmacSHA256, access-key=..., signed-date=..., signature=..." 를 넣는다.
  ★서명 대상 문자열 = signed-date + METHOD + path + query(정렬하지 않은 '원본' 쿼리스트링).
    요청 URL과 서명에 쓰는 쿼리가 한 글자라도 다르면 401이다. 그래서 여기서는 urlencode
    결과를 '한 번만' 만들어 URL과 서명에 함께 쓴다(requests의 params= 로 넘기면 라이브러리가
    다시 인코딩하면서 서명과 어긋날 수 있어 쓰지 않는다).
  ★signed-date는 UTC "yymmddTHHMMSSZ". 이 PC 시각이 UTC 기준 몇 분만 틀어져도 서명이 거부된다.
- 'X-Requested-By'(판매자 WING ID) 헤더도 필수다. 빠지면 인증 실패로 떨어진다.

몰별 함정(docs/research/API_쿠팡.json 조사 기준):
 1) 조회기간 최대 31일 → max_window_days=25로 여유를 뒀다(대량 구간 타임아웃도 함께 회피).
 2) 호출 한도가 판매자ID당 초당 10회 수준이고 사전 고지 없이 바뀐다 → call_interval 0.6초로
    보수적으로 잡고, 429는 두 번까지 백오프한 뒤 사람이 읽을 메시지로 올린다.
 3) 샌드박스가 없다 — 실계정/실주문으로만 동작한다. 그래서 읽기(GET)만 수집 경로에 두고,
    송장 전송(POST)은 상위에서 명시적으로 부를 때만 나가게 했다.
 4) 키 유효기간 180일 → 만료되면 401. 설정의 key_expires_at을 401 메시지에 함께 안내한다.
 5) 호출 IP 등록제(최대 10개) → 미등록이면 403. 401(서명·키 문제)과 구분해서 안내한다.
 6) 상태 필터는 한 번에 하나만 먹는다 → ACCEPT(결제완료), INSTRUCT(상품준비중)를 각각 조회한다.
    DEPARTURE/DELIVERING/FINAL_DELIVERY(배송지시·배송중·배송완료)는 수집하지 않는다.
 7) 한 orderId가 여러 shipmentBox(배송박스)로 쪼개져 내려온다 → 주문번호 단위로 합친다.
    또 조회 구간 경계 날짜가 겹쳐 같은 박스가 두 번 올 수 있어 shipmentBoxId로 중복을 막는다
    (안 막으면 수량·금액이 이중계상된다).
 8) 부분취소 수량은 shippingCount가 아니라 cancelCount에 반영된다 → 실수량 = shipping - cancel.
 9) 고객 전화번호는 안심번호(safeNumber)로만 내려온다. 그대로 phone에 쓴다.
10) 응답 본문이 HTTP 200이어도 body의 code가 200이 아니면 실패다(잘못된 vendorId·파라미터 등).
"""
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

from .base import (MallAdapter, MallError, check_cooldown, pick_product_code,
                   register, set_cooldown)

HOST = "https://api-gateway.coupang.com"
# 경로에 API 버전이 박혀 있다(발주서 v5, 단건/송장 v4). 쿠팡이 버전을 올리면 여기만 고치면 된다.
ORDERSHEETS_PATH = "/v2/providers/openapi/apis/api/v5/vendors/{vendor}/ordersheets"
ORDERSHEET_ONE_PATH = "/v2/providers/openapi/apis/api/v4/vendors/{vendor}/{order_id}/ordersheets"
INVOICE_PATH = "/v2/providers/openapi/apis/api/v4/vendors/{vendor}/orders/invoices"

# 수집 대상 — 결제완료 ~ 상품준비중까지만. 이미 나간 건은 가져오지 않는다.
COLLECT_STATUSES = ("ACCEPT", "INSTRUCT")
KST_OFFSET = "+09:00"      # 조회 날짜에 붙이는 시간대 — 쿠팡 v5가 요구한다
MAX_PER_PAGE = 50          # 쿠팡 상한
PAGE_LIMIT = 200           # nextToken이 안 끝나는 이상 상황 방어(최대 1만 건)
TIMEOUT = 25               # 초 — 대량 구간은 응답이 느리다
DEFAULT_COURIER = "CJGLS"  # 기본 택배사(CJ대한통운)

# ★같은 계정에 렌탈(RMS)·판매(OWS) 상품이 함께 있다. 렌탈 주문이 OWS로 들어오면
#   판매 출고 사고가 난다(2026-08-05 대표 지적, 실측 쿠팡 1건·스마트스토어 1건 유입).
#   스마트스토어에 이미 있는 분류를 그대로 옮긴다 — 두 몰이 다르게 굴면 한쪽에만 구멍이 생긴다.
RENTAL_KEYWORDS = ("렌탈", "렌트", "대여", "임대", "사용기간")


def _s(v):
    """None/숫자를 안전하게 문자열로."""
    return "" if v is None else str(v).strip()


def _int(v):
    try:
        return int(float(str(v).replace(",", "") or 0))
    except (TypeError, ValueError):
        return 0


def _id_list(value):
    """설정 칸의 '12345, 67890' 같은 문자열을 상품번호 집합으로."""
    import re as _re
    return {t for t in _re.split(r"[,\s]+", str(value or "").strip()) if t}


def _money(v):
    """쿠팡 금액 — 숫자로도 오고 객체로도 온다.

    ★2026-07-28부터 쿠팡이 금액 필드를 **Money 객체**로 바꿔 보내기 시작했다.
        "orderPrice": {"currencyCode": "KRW", "units": 376740, "nanos": 0}
      _int()는 dict를 숫자로 못 읽어 0을 돌려줬고, 그때부터 들어온 쿠팡 주문
      **38건의 금액이 전부 0원**이 됐다(2026-08-05 실측, 7/27까지는 정상).
      매출·마진이 그만큼 비어 있었다. 두 형태를 모두 받는다.
    """
    if isinstance(v, dict):
        # nanos는 1원 미만 단위(10억분의 1) — 원화에서는 반올림해 더한다
        return _int(v.get("units")) + round(_int(v.get("nanos")) / 1_000_000_000)
    return _int(v)


def _id(v):
    """쿠팡 내부 ID는 숫자형(long)이다. 숫자면 int로, 아니면 원문 그대로 보낸다."""
    t = _s(v)
    return int(t) if t.isdigit() else t


@register
class CoupangAdapter(MallAdapter):
    code = "coupang"
    name = "쿠팡"
    max_window_days = 25   # 몰 제한 31일 — 여유를 둔다
    call_interval = 0.6    # 초당 10회 제한 대비 보수적으로
    # ★레지스트리(app/malls/__init__.py)의 fields key와 철자가 정확히 같아야 한다.
    #   key_expires_at은 만료 알림용이라 필수에서 뺐다(비어 있어도 수집 자체는 된다).
    required_keys = ("vendor_id", "access_key", "secret_key", "wing_id")

    # ---- 인증/호출 ---------------------------------------------------
    def _vendor(self):
        return _s(self.s.get("vendor_id"))

    def _auth(self, method, path, query):
        """쿠팡 CEA HMAC 서명 헤더를 만든다.

        message = signed-date + METHOD + path + query
        (query는 '?' 없이, 실제 전송하는 문자열 그대로. 키 정렬을 하면 안 된다.)
        """
        signed_date = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")
        message = f"{signed_date}{method}{path}{query}"
        sig = hmac.new(
            _s(self.s.get("secret_key")).encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return (f"CEA algorithm=HmacSHA256, access-key={_s(self.s.get('access_key'))}, "
                f"signed-date={signed_date}, signature={sig}")

    def _headers(self, method, path, query):
        return {
            "Authorization": self._auth(method, path, query),
            "X-Requested-By": _s(self.s.get("wing_id")),   # 판매자 ID — 필수 헤더
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "User-Agent": "OWS/1.0",
        }

    def _expiry_hint(self):
        """키 만료일이 설정돼 있으면 인증 오류 메시지에 덧붙일 안내."""
        exp = _s(self.s.get("key_expires_at"))
        if not exp:
            return " (WING 키 유효기간 180일도 함께 확인하세요)"
        return (f" (설정된 키 만료일: {exp} — 지났다면 WING에서 재발급 후 "
                f"Secret Key를 새로 저장해야 합니다)")

    def _call(self, method, path, params=None, body=None):
        """공통 호출 — 실패 원인을 구분해 MallError로 올린다."""
        check_cooldown(self.code, self.name)   # 429 쿨다운 중엔 호출 자체를 안 한다
        # 서명 대상과 실제 URL이 100% 같아야 하므로 쿼리 문자열을 한 번만 만들어 공유한다.
        query = urlencode(params or {}, encoding="utf-8")
        url = f"{HOST}{path}" + (f"?{query}" if query else "")
        # 한글(수취인/주소)이 섞여도 깨지지 않게 UTF-8로 직접 인코딩해 보낸다.
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")

        resp = None
        for attempt in range(3):
            # 재시도할 때마다 signed-date를 새로 만든다(오래된 서명은 거부된다).
            headers = self._headers(method, path, query)
            try:
                resp = requests.request(method, url, headers=headers, data=payload, timeout=TIMEOUT)
            except requests.Timeout as e:
                raise MallError(
                    f"쿠팡 응답이 {TIMEOUT}초 안에 오지 않았습니다. 조회 기간을 짧게 나눠 "
                    f"다시 시도하세요.") from e
            except requests.RequestException as e:
                raise MallError(f"쿠팡 서버에 연결하지 못했습니다: {e}") from e

            if resp.status_code == 429 and attempt < 2:
                time.sleep(2 * (attempt + 1))   # 2초 → 4초 백오프 후 재시도
                continue
            break

        return self._body(resp)

    def _body(self, resp):
        """HTTP 상태코드와 응답 본문을 해석한다."""
        code = resp.status_code
        text = (resp.text or "")[:300]
        if code == 401:
            raise MallError(
                "쿠팡 인증에 실패했습니다(401). Access Key/Secret Key가 맞는지, 이 PC의 시각이 "
                "정확한지(UTC 기준 몇 분만 틀어져도 서명이 거부됩니다) 확인하세요."
                + self._expiry_hint())
        if code == 403:
            raise MallError(
                "쿠팡이 호출을 거부했습니다(403). WING > 판매자정보 > OPEN API 키에 이 서버의 "
                "공인 IP가 등록돼 있는지 확인하세요(최대 10개). 키를 방금 발급했다면 접근 권한 "
                "활성화까지 24시간 이상 걸릴 수 있습니다.")
        if code == 429:
            set_cooldown(self.code)     # 계속 때리면 차단이 길어진다 — 5분 휴식
            raise MallError(
                "쿠팡 호출 한도를 넘었습니다(429) — 5분 쉬었다가 자동으로 재개됩니다. "
                "반복되면 자동수집 주기를 늘려 주세요.")
        if code in (500, 502, 503, 504):
            raise MallError(f"쿠팡 서버 오류({code})입니다. 잠시 후 다시 시도하세요.")

        try:
            data = resp.json()
        except ValueError as e:
            raise MallError(f"쿠팡 응답을 해석하지 못했습니다({code}): {text}") from e
        if not isinstance(data, dict):
            raise MallError(f"쿠팡 응답 형식이 예상과 다릅니다: {text}")

        body_code = _s(data.get("code"))
        msg = _s(data.get("message")) or "메시지 없음"
        if code >= 400 or (body_code and body_code not in ("200", "OK", "SUCCESS")):
            raise MallError(f"쿠팡 오류({body_code or code}): {msg}")
        return data

    # ---- 주문 수집 ---------------------------------------------------
    def collect_orders(self, since, until):
        path = ORDERSHEETS_PATH.format(vendor=self._vendor())
        self.skipped_rental = []   # 렌탈이라 걸러낸 것 — 화면에 무엇이 빠졌는지 보여 준다
        buckets = {}        # orderId -> 누적 dict
        order_seq = []      # 응답 순서 보존(엑셀 임포트와 비슷한 정렬감)
        seen_boxes = set()  # 구간 경계에서 같은 박스가 다시 와도 이중계상되지 않게

        for start, end in self.windows(since, until):
            for status in COLLECT_STATUSES:   # 상태 필터는 한 번에 하나만 가능
                next_token = ""
                for _ in range(PAGE_LIMIT):
                    params = {
                        # ★v5는 타임존을 붙인 'yyyy-MM-dd+09:00' 형식만 받는다.
                        #   날짜만 보내면 400 "startTime/endTime not valid"가 난다.
                        #   시간을 붙인 ISO(…T00:00:00+09:00)도 거부한다 — 날짜+오프셋 그대로여야 한다
                        #   (2026-07-29 실제 호출로 네 가지 형식을 시험해 확인).
                        "createdAtFrom": start.strftime("%Y-%m-%d") + KST_OFFSET,
                        "createdAtTo": end.strftime("%Y-%m-%d") + KST_OFFSET,
                        "status": status,
                        "maxPerPage": str(MAX_PER_PAGE),
                    }
                    if next_token:
                        params["nextToken"] = next_token
                    data = self._call("GET", path, params=params)
                    self.pace()   # 호출 사이 간격 — 429 예방

                    rows = data.get("data") or []
                    if isinstance(rows, dict):     # 단건이 dict로 오는 경우 방어
                        rows = [rows]
                    for box in rows:
                        self._absorb(box, buckets, order_seq, seen_boxes)

                    prev, next_token = next_token, _s(data.get("nextToken"))
                    # 같은 토큰이 되돌아오면 무한루프다 — 끊는다.
                    if not next_token or next_token == prev:
                        break

        out = []
        for order_id in order_seq:
            row = self._to_order(buckets[order_id])
            if row:
                out.append(row)
        return out

    def _classify(self, product_id, product_name, option):
        """sale(수집) / rental(제외) / unknown(수집+표시) — 스마트스토어와 같은 원칙.

        ★애매한 것은 버리지 않는다. 진짜 판매 주문이 조용히 사라지면 출고 누락 사고가 된다.
          확실한 렌탈만 제외한다. 우선순위: 판매 등록 > 렌탈 등록 > 렌탈 키워드 > 미분류.
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
            return "unknown"                  # 분류표를 쓰는데 등록이 안 된 상품
        return "sale"                         # 분류표 미사용 — 기존 동작 그대로

    def _absorb(self, box, buckets, order_seq, seen_boxes):
        """shipmentBox 한 개를 주문번호 버킷에 합친다."""
        if not isinstance(box, dict):
            return
        status = _s(box.get("status"))
        # API에서 이미 걸러 오지만, 조회 중 상태가 바뀐 건이 섞일 수 있어 2차 방어.
        if status and status not in COLLECT_STATUSES:
            return
        box_id = _s(box.get("shipmentBoxId"))
        if box_id:
            if box_id in seen_boxes:
                return   # 조회 구간이 겹쳐 두 번 온 박스 — 수량/금액 이중계상 방지
            seen_boxes.add(box_id)
        order_id = _s(box.get("orderId"))
        if not order_id:
            return

        b = buckets.get(order_id)
        if b is None:
            b = buckets[order_id] = {
                "orderId": order_id, "lines": [], "boxes": [], "orderedAt": "",
                "recipient": "", "phone": "", "postalCode": "", "address": "", "message": "",
            }
            order_seq.append(order_id)
        if box_id:
            b["boxes"].append(box_id)

        if not b["orderedAt"]:
            b["orderedAt"] = _s(box.get("orderedAt")) or _s(box.get("paidAt"))

        recv = box.get("receiver") or {}
        orderer = box.get("orderer") or {}
        if not b["recipient"]:
            b["recipient"] = _s(recv.get("name")) or _s(orderer.get("name"))
        if not b["phone"]:
            # 안심번호(safeNumber)가 사실상 유일한 연락처다. 구 필드도 순서대로 시도.
            b["phone"] = (_s(recv.get("safeNumber")) or _s(recv.get("receiverNumber"))
                          or _s(orderer.get("safeNumber")))
        if not b["postalCode"]:
            b["postalCode"] = _s(recv.get("postCode"))
        if not b["address"]:
            b["address"] = " ".join(x for x in [_s(recv.get("addr1")), _s(recv.get("addr2"))] if x)
        if not b["message"]:
            b["message"] = _s(box.get("parcelPrintMessage"))

        for it in (box.get("orderItems") or []):
            if not isinstance(it, dict):
                continue
            # 부분취소는 cancelCount에 들어온다(shippingCount는 줄지 않는다).
            qty = _int(it.get("shippingCount")) - _int(it.get("cancelCount"))
            if qty <= 0:
                continue   # 전량 취소된 상품행은 수집 대상이 아니다
            name = _s(it.get("sellerProductName")) or _s(it.get("vendorItemName"))
            opt = _s(it.get("sellerProductItemName")) or _s(it.get("firstSellerProductItemName"))
            if opt == name:
                opt = ""   # 옵션 없는 단품은 상품명과 같은 값이 오기도 한다
            price = _money(it.get("orderPrice")) or _money(it.get("salesPrice")) * qty
            # ★제품코드는 '등록상품명(판매자 관리용)'에도 들어온다(2026-08-04 대표 확인).
            #   쿠팡 WING의 그 칸은 발주서에 쓰이는 판매자용 이름이고, 업무관리은 거기에
            #   고도몰과 같은 제품코드(NT371B5M_i7-7_내장)를 넣어 두고 있다.
            #   판매자상품코드(externalVendorSkuCode)가 비어 오면 여기서 코드를 뽑는다 —
            #   안 그러면 쿠팡 내부 번호(95787471151)가 코드 자리에 들어가 고도몰과 안 맞는다.
            b["lines"].append({
                "name": name,
                "opt": opt,
                "qty": qty,
                "amount": price,
                "code": (pick_product_code(it.get("externalVendorSkuCode"), name)
                         or _s(it.get("externalVendorSkuCode"))
                         or _s(it.get("vendorItemId")) or _s(it.get("sellerProductId"))),
                # 렌탈/판매 분류에 쓸 쿠팡 상품번호(등록상품ID) — 코드와 달리 몰이 정한 값이라
                # 상품명이 바뀌어도 그대로다.
                "pid": _s(it.get("sellerProductId")) or _s(it.get("productId")),
                # ★상품 페이지 링크용(2026-08-14 대표) — URL은 '노출상품ID'(productId)를 쓴다.
                #   등록상품ID(sellerProductId)를 넣으면 열리지 않는다: 대표 실물 확인
                #   등록 16063461329 안에 노출 9616118750 + 옵션ID 6개.
                #   vendorItemId까지 있으면 '그 옵션'이 선택된 채로 열린다(제목=옵션표라 중요).
                "urlPid": _s(it.get("productId")),
                "itemId": _s(it.get("vendorItemId")),
            })

    def _to_order(self, b):
        """주문 단위 버킷 → 임포터 주문 dict."""
        lines = b["lines"]
        if not lines:
            return None   # 전량 취소 등으로 남은 상품행이 없으면 버린다
        main = lines[0]
        # ★렌탈은 RMS 몫이라 가져오지 않는다. 무엇을 걸렀는지는 남겨서 화면에 보여 준다.
        opt_all = " ".join(x.get("opt") or "" for x in lines)
        klass = self._classify(main.get("pid"), main["name"], opt_all)
        if klass == "rental":
            self.skipped_rental.append(
                {"orderNumber": b["orderId"], "productId": _s(main.get("pid")),
                 "productName": main["name"], "recipient": b["recipient"]})
            return None
        # 대표 상품명은 첫 행, 나머지 상품은 optionName에 " / "로 잇는다(엑셀 임포트와 같은 표현).
        parts = []
        if main["opt"]:
            parts.append(main["opt"])
        for line in lines[1:]:
            parts.append(f"{line['name']} ({line['opt']})" if line["opt"] else line["name"])
        return self.order(
            # ★한 주문이 박스 여러 개로 쪼개져도 importKey는 주문번호 하나로 유지한다
            #   (박스별로 키를 나누면 같은 주문이 여러 건으로 들어가 중복판정을 빠져나간다).
            importKey=f"쿠팡:{b['orderId']}",
            orderNumber=b["orderId"],
            orderedAt=b["orderedAt"].replace("T", " ")[:19],
            productName=main["name"],
            optionName=" / ".join(x for x in parts if x),
            productCode=main["code"],
            # 상품명 링크가 '그 상품(그 옵션)'으로 가게 — 노출상품ID + 옵션ID
            mallProductId=main.get("urlPid") or "",
            mallItemId=main.get("itemId") or "",
            quantity=max(1, sum(line["qty"] for line in lines)),
            amount=sum(line["amount"] for line in lines),
            recipient=b["recipient"],
            phone=b["phone"],
            postalCode=b["postalCode"],
            address=b["address"],
            deliveryMessage=b["message"],
        )

    # ---- 송장 전송 ---------------------------------------------------
    def _fetch_ordersheet(self, order_no):
        """단건 발주서 조회 — 송장 전송에 필요한 shipmentBoxId/vendorItemId를 얻는다.

        수집 주문 dict에는 쿠팡 내부 ID를 담을 자리가 없어서(임포터 스키마가 고정),
        송장 전송 시점에 주문번호로 다시 조회해 내부 ID를 찾는다.
        """
        path = ORDERSHEET_ONE_PATH.format(vendor=self._vendor(), order_id=_s(order_no))
        data = self._call("GET", path)
        rows = data.get("data") or []
        if isinstance(rows, dict):
            rows = [rows]
        if not rows:
            raise MallError(f"쿠팡에서 주문 {order_no}를 찾지 못했습니다.")
        return rows

    def upload_invoice(self, order_no, invoice_no, courier_code="", **kw):
        """송장 전송(POST .../v4/vendors/{vendorId}/orders/invoices).

        ★상품준비중(INSTRUCT) 상태의 주문만 받는다. 결제완료(ACCEPT)라면 쿠팡의
          '상품준비중 처리' API를 먼저 호출해야 하고, 그 배선은 상위(출고 흐름)에서 한다.
        ★같은 송장번호를 6개월 안에 다시 넣으면 중복 오류다(같은 수취인은 예외).
        ★이미 배송지시/배송중 상태면 이 API가 아니라 updateInvoices(정정)를 써야 한다.
        kw로 shipment_box_id / vendor_item_id를 주면 단건 조회를 건너뛴다(호출 1회 절약).
        """
        vendor = self._vendor()
        invoice_no = _s(invoice_no)
        if not invoice_no:
            raise MallError("송장번호가 비어 있어 쿠팡에 전송할 수 없습니다.")
        courier = _s(courier_code) or _s(self.s.get("courier_code")) or DEFAULT_COURIER

        box_id = _s(kw.get("shipment_box_id"))
        item_id = _s(kw.get("vendor_item_id"))
        targets = []
        if box_id and item_id:
            targets.append((box_id, item_id))
        else:
            # 한 주문이 여러 박스/여러 상품으로 쪼개져 있으면 전부에 같은 송장을 붙인다.
            for box in self._fetch_ordersheet(order_no):
                bid = _s(box.get("shipmentBoxId"))
                for it in (box.get("orderItems") or []):
                    vid = _s(it.get("vendorItemId"))
                    if bid and vid:
                        targets.append((bid, vid))
            self.pace()
        if not targets:
            raise MallError(f"쿠팡 주문 {order_no}에서 송장을 붙일 상품을 찾지 못했습니다.")

        body = {
            "vendorId": vendor,
            "orderSheetInvoiceApplyDtos": [
                {
                    "shipmentBoxId": _id(bid),
                    "orderId": _id(order_no),
                    "vendorItemId": _id(vid),
                    "deliveryCompanyCode": courier,
                    "invoiceNumber": invoice_no,
                    "splitShipping": False,
                    "preSplitShipped": False,
                }
                for bid, vid in targets
            ],
        }
        data = self._call("POST", INVOICE_PATH.format(vendor=vendor), body=body)

        # 건별 부분 성공이 가능한 API라 응답 목록을 하나씩 확인해야 한다
        # (HTTP 200이어도 특정 박스만 NOT_FOUND_SHIPMENT_BOX/INVALID_STATUS로 실패할 수 있다).
        result = data.get("data") or {}
        rows = result.get("responseList") if isinstance(result, dict) else None
        fails = []
        for r in (rows or []):
            if not isinstance(r, dict):
                continue
            code = _s(r.get("resultCode"))
            if r.get("succeed") is False or (code and code not in ("OK", "SUCCESS")):
                fails.append(f"{_s(r.get('shipmentBoxId')) or '?'}: "
                             f"{_s(r.get('resultMessage')) or code or '사유 미상'}")
        if fails:
            raise MallError("쿠팡 송장 전송이 일부 실패했습니다 — " + " / ".join(fails))
        return True
