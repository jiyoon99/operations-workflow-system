"""고도몰5(NHN커머스) 주문 수집 + 상품 실시간 조회.

스펙: 고도몰 open API 스펙 정의서(91p) 3.1 상품조회 / 4.1 주문조회 / 4.2 주문상태변경

- 인증: partner_key + key를 POST 파라미터로. 응답은 XML(UTF-8)
- 응답 공통 구조:
    <data><header><code/><msg/><total/><max_page/><now_page/></header>
          <return> …order_data | goods_data 반복… </return></data>
  ★ code는 header 아래에 있다 — root.find("code")로는 못 찾는다(오류를 놓쳐 조용히 0건이 된다).
- 반복 노드명은 order_data / goods_data (스네이크)이고, 그 하위는 orderInfoData 등
  카멜케이스다. 섞여 있으니 스펙대로 정확히 쓴다.
- 주문조회 기간 최대 30일, 초당 100회 초과 시 429
"""
import json
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .base import MallAdapter, MallError, register

PROD_HOST = "https://openhub.godo.co.kr"
SANDBOX_HOST = "http://sbopenhub.godo.co.kr"
PATHS = {
    "search": "/godomall5/order/Order_Search.php",
    "status": "/godomall5/order/Order_Status.php",
    "goods": "/godomall5/goods/Goods_Search.php",
}

# 수집 대상 주문상태 (결제완료·상품준비중)
COLLECT_STATUSES = ("p1", "g1")
# 상품줄 단위 상태 — 취소(c)·반품(b)·교환(e)·환불(r)·교환추가(z)로 넘어간 줄은 보내지 않는다.
# 이걸 안 거르면 고객이 취소한 상품을 꺼내 포장하게 된다.
DEAD_LINE_PREFIXES = ("c", "b", "e", "r", "z")
PAGE_SIZE = 300          # RMS 실운영에서 확인된 최대 허용 size
MAX_PAGES = 100          # 폭주 방지 — 한 창(window)당 최대 3만 건

# 상품상태 코드(goodsState)
GOODS_STATE = {"n": "새상품", "u": "중고상품", "r": "반품/재고상품"}


def _text(node, *names):
    if node is None:
        return ""
    for n in names:
        el = node.find(n)
        if el is not None and (el.text or "").strip():
            return el.text.strip()
    return ""


def _int(v):
    try:
        return int(float(str(v).replace(",", "") or 0))
    except (TypeError, ValueError):
        return 0


def _norm_date(v):
    """주문일 표기를 'YYYY-MM-DD…'로 통일해 저장한다.

    점·슬래시 표기가 섞이면 기간 조회가 달을 섞어 넣고 빼먹는다(실제 발생).
    """
    v = (v or "").strip()
    if len(v) >= 10 and v[4] in "./" and v[7] in "./":
        return f"{v[:4]}-{v[5:7]}-{v[8:10]}{v[10:]}"
    return v


def _option_text(gnode):
    """옵션을 사람이 읽는 문장으로. 고도몰은 optionInfo를 JSON으로 준다.

    코드 원문을 그대로 두면 화면·송장에 개발자용 문자열이 찍히고,
    고객이 직접 적은 입력옵션(optionTextInfo)은 통째로 사라진다.
    """
    parts = []
    for field in ("optionInfo", "optionTextInfo"):
        raw = _text(gnode, field)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            parts.append(raw)                      # JSON이 아니면 원문이라도 보여 준다
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict):
                name = item.get("optionName") or item.get("name") or item.get("title") or ""
                value = item.get("optionValue") or item.get("value") or item.get("text") or ""
                pair = f"{name}: {value}".strip(": ").strip()
                if pair:
                    parts.append(pair)
            elif isinstance(item, list):
                # ★고도몰은 배열의 배열로도 준다: ["옵션명","옵션값","",추가금액,null]
                #   dict만 처리하면 파이썬 표현이 그대로 저장돼 화면·송장에
                #   ['제품등급선택 (필수)', 'A급 외관 / S급 배터리', '', -30000, None] 이 찍힌다
                #   (2026-07-29 라이브 20건 확인).
                name = str(item[0]).strip() if len(item) > 0 and item[0] else ""
                value = str(item[1]).strip() if len(item) > 1 and item[1] else ""
                pair = f"{name}: {value}".strip(": ").strip()
                if not pair:
                    continue
                extra = item[3] if len(item) > 3 else None
                if isinstance(extra, (int, float)) and extra:
                    pair += f" ({int(extra):+,}원)"
                parts.append(pair)
            elif item:
                parts.append(str(item))
    return " / ".join(parts)


def readable_option(raw):
    """이미 저장된 '파이썬 표현' 옵션을 사람이 읽는 문장으로 되돌린다.

    수집 당시 파서가 배열 형태를 처리하지 못해 원문이 그대로 들어간 값을 정정할 때 쓴다.
    뒤에 ' / 윈도우 복구 프로그램'처럼 정상 옵션이 붙어 있으면 그대로 살린다.
    """
    import ast
    text = (raw or "").strip()
    if not text.startswith("["):
        return text
    # ★' / '로 자르면 안 된다 — 옵션값 자체에 그 구분자가 들어 있다('A급 외관 / S급 배터리').
    #   마지막 ']'로 자르는 것도 안 된다 — 뒤 옵션에 '[노트북쿨러]'처럼 대괄호가 또 나온다.
    #   따옴표 안을 건너뛰며 괄호 짝을 세어 '첫 리스트가 끝나는 자리'를 찾는다.
    end, depth, quote = -1, 0, None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote and text[i - 1] != "\\":
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return text
    head, tail = text[:end + 1], text[end + 1:].lstrip(" /").strip()
    try:
        data = ast.literal_eval(head)
    except (ValueError, SyntaxError):
        return text
    items = data if isinstance(data, list) and data and isinstance(data[0], list) else [data]
    parts = []
    for item in items:
        if not isinstance(item, list):
            continue
        name = str(item[0]).strip() if len(item) > 0 and item[0] else ""
        value = str(item[1]).strip() if len(item) > 1 and item[1] else ""
        pair = f"{name}: {value}".strip(": ").strip()
        if not pair:
            continue
        extra = item[3] if len(item) > 3 else None
        if isinstance(extra, (int, float)) and extra:
            pair += f" ({int(extra):+,}원)"
        parts.append(pair)
    fixed = " / ".join(parts)
    return (fixed + (" / " + tail if tail else "")) if fixed else text


@register
class GodomallAdapter(MallAdapter):
    code = "godomall"
    name = "고도몰"
    max_window_days = 25          # 몰 제한 30일 — 여유를 둔다
    call_interval = 0.5
    required_keys = ("partner_key", "user_key")

    def _url(self, kind="search"):
        host = SANDBOX_HOST if self.s.get("sandbox") else PROD_HOST
        return host + PATHS[kind]

    def _auth(self):
        return {"partner_key": self.s.get("partner_key", ""),
                "key": self.s.get("user_key", "")}

    def _post(self, url, params, timeout=20):
        body = urlencode(params, encoding="utf-8").encode("utf-8")
        req = Request(url, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "HMS/1.0",
        })
        try:
            with urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except Exception as e:
            raise MallError(f"고도몰 호출 실패: {e}") from e
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            head = raw[:200].decode("utf-8", "replace")
            raise MallError(f"고도몰 응답을 해석하지 못했습니다: {head}") from e
        self._check_error(root)
        return root

    def _check_error(self, root):
        """<data><header><code>…  — header 아래를 봐야 오류를 잡는다."""
        header = root.find("header")
        code = _text(header, "code") or _text(root, "code", "resultCode")
        msg = _text(header, "msg") or _text(root, "message", "msg")
        if code and code not in ("000", "0", "200"):
            raise MallError(f"고도몰 오류({code}): {msg or '메시지 없음'}")

    # ------------------------------------------------------------ 주문 수집

    def collect_orders(self, since, until):
        orders = []
        for start, end in self.windows(since, until):
            orders.extend(self._collect_window(start, end))
        return orders

    def _collect_window(self, start, end):
        """page 번호 페이징으로 끝까지 훑는다 — RMS 실운영에서 검증된 방식 그대로.

        ★예전 구현은 lastOrder를 '요청 커서'로 보냈는데, 그건 스펙에 없는 발명이었다.
          lastOrder는 응답에 실려 오는 '마지막 페이지냐' 표시일 뿐이다(2026-07-29 정정).
          dateType도 modify(수정일)가 아니라 order(주문일) — RMS가 그렇게 운영 중이고,
          날짜는 시각 없이 yyyy-MM-dd로 보낸다.
        """
        out = []
        seen_nos = set()          # 같은 페이지가 되돌아오면 무한루프 — 주문번호로 감지
        truncated = False
        for page in range(1, MAX_PAGES + 1):
            params = dict(self._auth(), **{
                "dateType": "order",
                "startDate": start.strftime("%Y-%m-%d"),
                "endDate": end.strftime("%Y-%m-%d"),
                "size": str(PAGE_SIZE),
                "page": str(page),
            })
            root = self._post(self._url("search"), params)
            nodes = self._order_nodes(root)
            self.pace()
            if not nodes:
                break
            # 중복 페이지 감지 — page를 무시하는 서버가 같은 목록을 반복해 줄 때 멈춘다
            page_nos = {_text(n, "orderNo") for n in nodes} - {""}
            if page_nos and not (page_nos - seen_nos):
                break
            seen_nos |= page_nos
            out.extend(self._parse(nodes))
            # 마지막 페이지 판정: lastOrder=true 또는 한 페이지를 못 채움
            flag = (_text(root.find("header"), "lastOrder")
                    or _text(root, "lastOrder") or "").lower()
            if flag == "true" or len(nodes) < PAGE_SIZE:
                break
            if page == MAX_PAGES:
                truncated = True
        if truncated:
            raise MallError(
                f"고도몰 주문이 너무 많아 일부만 가져왔습니다({len(out)}건). "
                "조회 기간을 짧게 나눠 다시 수집하세요.")
        return out

    @staticmethod
    def _order_nodes(root):
        # 스펙은 order_data. 과거 표기(orderData)도 받아 준다.
        nodes = list(root.iter("order_data"))
        return nodes or list(root.iter("orderData"))

    @staticmethod
    def _live_lines(node):
        """살아 있는 상품줄만 — 취소·반품·교환으로 빠진 줄은 주문에서 뺀다."""
        out = []
        for g in node.findall(".//orderGoodsData"):
            st = _text(g, "orderStatus")
            if st and st[:1].lower() in DEAD_LINE_PREFIXES:
                continue
            out.append(g)
        return out

    def _parse(self, nodes):
        out = []
        for node in nodes:
            status = _text(node, "orderStatus")
            if COLLECT_STATUSES and status and status not in COLLECT_STATUSES:
                continue
            order_no = _text(node, "orderNo")
            info = node.find("orderInfoData")
            goods = self._live_lines(node)
            if not order_no or not goods:
                continue        # 전량 취소된 주문은 가져오지 않는다
            main = goods[0]
            option_names = []
            qty = 0
            line_sum = 0
            for gnode in goods:
                nm = _text(gnode, "goodsNm", "goodsName")
                if gnode is not main and nm:
                    option_names.append(nm)          # 유상 옵션·추가상품은 옵션명으로 남긴다
                cnt = max(1, _int(_text(gnode, "goodsCnt", "ea")))
                qty += cnt
                # goodsPrice는 '상품가격'(단가)이라 수량을 곱해야 그 줄의 금액이 된다
                line_sum += _int(_text(gnode, "goodsPrice", "price")) * cnt
            for extra in node.findall(".//addGoodsData"):   # 유상 추가상품
                nm = _text(extra, "goodsNm", "addGoodsNm")
                cnt = max(1, _int(_text(extra, "goodsCnt", "addGoodsCnt")))
                if nm:
                    option_names.append(f"{nm}{f' x{cnt}' if cnt > 1 else ''}")
                line_sum += _int(_text(extra, "goodsPrice", "addGoodsPrice")) * cnt
            opt = _option_text(main)
            if opt:
                option_names.insert(0, opt)
            addr = " ".join(x for x in [
                _text(info, "receiverAddress", "orderAddress"),
                _text(info, "receiverAddressSub", "orderAddressSub"),
            ] if x)
            # 금액은 주문 총액을 1순위로 — 상품행만 더하면 옵션·추가상품이 빠진다.
            # (배송비 포함 여부는 키 발급 후 실주문 1건으로 대조 확인 필요)
            amount = (_int(_text(node, "totalGoodsPrice"))
                      or _int(_text(node, "settlePrice", "totalPrice"))
                      or line_sum)
            out.append(self.order(
                importKey=f"고도몰:{order_no}",
                orderNumber=order_no,
                orderedAt=_norm_date(_text(node, "orderDate", "regDt")),
                productName=_text(main, "goodsNm", "goodsName"),
                optionName=" / ".join(x for x in option_names if x),
                productCode=_text(main, "goodsCd", "goodsCode"),
                quantity=max(1, qty),
                amount=amount,
                recipient=_text(info, "receiverName") or _text(node, "orderName"),
                phone=_text(info, "receiverCellPhone", "receiverPhone"),
                postalCode=_text(info, "receiverZonecode", "receiverZipcode"),
                address=addr,
                deliveryMessage=_text(info, "orderMemo", "deliveryMemo"),
            ))
        return out

    # ------------------------------------------------------------ 상품 조회

    def search_goods(self, keyword="", field="auto", size=20):
        """자체상품코드/상품명으로 상품을 '실시간' 조회한다.

        상품 정보(특히 가격)는 수시로 바뀌므로 저장해 두지 않고 매번 몰에 물어본다.
        field=auto면 코드·상품명 양쪽으로 찾아 합친다(코드 일치가 위로).
        """
        keyword = (keyword or "").strip()
        size = max(1, min(int(size or 20), PAGE_SIZE))
        if not keyword:
            return []
        fields = {"code": ["goodsCd"], "name": ["goodsNm"]}.get(field, ["goodsCd", "goodsNm"])
        out, seen = [], set()
        for f in fields:
            for g in self._goods_page({f: keyword, "size": str(size), "page": "1"}):
                if g["goodsNo"] in seen:
                    continue
                seen.add(g["goodsNo"])
                out.append(g)
            if len(out) >= size:
                break
        return out[:size]

    def _goods_page(self, extra):
        root = self._post(self._url("goods"), dict(self._auth(), **extra))
        return [self._goods(n) for n in root.iter("goods_data")]

    @staticmethod
    def _goods(n):
        state = _text(n, "goodsState")
        price = _int(_text(n, "goodsPrice"))
        return {
            "goodsNo": _text(n, "goodsNo"),
            "goodsCd": _text(n, "goodsCd"),                  # 자체상품코드
            "goodsNm": _text(n, "goodsNm"),
            "shortDescription": _text(n, "shortDescription"),  # 짧은 설명(250자)
            "price": price,
            "priceText": _text(n, "goodsPriceString"),        # 가격대체문구(있으면 우선 표기)
            "fixedPrice": _int(_text(n, "fixedPrice")),
            "modelNo": _text(n, "goodsModelNo"),
            "maker": _text(n, "makerNm"),
            "state": state,
            "stateLabel": GOODS_STATE.get(state, ""),
            "stock": _int(_text(n, "totalStock")),
            "soldOut": _text(n, "soldOutFl") == "y",
            "sellFl": _text(n, "goodsSellFl"),
            "openFl": _text(n, "goodsOpenFl"),
        }

    # ------------------------------------------------------------ 송장 전송

    def upload_invoice(self, order_no, invoice_no, courier_code="", sno=""):
        """송장 전송 — 주문상태를 배송중(d1)으로 바꾸면서 송장번호를 넣는다."""
        params = dict(self._auth(), **{
            "orderNo": order_no,
            "orderStatus": "d1",
            "invoiceNo": invoice_no,
        })
        if sno:
            params["sno"] = sno
        if courier_code:
            params["invoiceCompanySno"] = courier_code
        self._post(self._url("status"), params)
        return True
