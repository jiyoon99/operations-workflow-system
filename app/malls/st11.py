"""11번가(SK플래닛) 셀러 API 주문 수집.

- GET https://api.11st.co.kr/rest/ordservices/complete/{start}/{end}   (결제완료 = 신규주문)
- GET https://api.11st.co.kr/rest/ordservices/packaging/{start}/{end}  (발주확인 = 상품준비중)
- 인증: 헤더 `openapikey: {32자 키}` 하나뿐(OAuth/HMAC 없음) + 셀러오피스에 등록한 IP만 허용
- 응답은 XML, 기본 인코딩 EUC-KR. 파라미터가 대부분 URL 경로에 들어가는 구식 REST 스타일.

★ 몰별 함정(하나라도 놓치면 조용히 깨진다)
 1) 인코딩: response.text를 쓰면 requests가 latin-1로 추측해 한글이 전부 깨진다.
    반드시 response.content(bytes)를 euc-kr → cp949 → utf-8 순으로 디코딩한다.
    cp949는 euc-kr의 확장(확장완성형)이라 euc-kr이 실패하는 글자를 건져낸다.
    ※ 오류 메시지를 찍을 때도 마찬가지다 — 여기서 .text를 쓰면 오류 원인이 깨져서 안 보인다.
 2) XML 선언: 디코딩한 str에 `<?xml ... encoding="EUC-KR"?>`가 남아 있으면 파서에 따라
    "Unicode strings with encoding declaration are not supported"로 죽는다.
    (CPython 기본 ElementTree는 통과하지만) 파서를 바꿔도 안전하도록 선언을 떼고 넘긴다.
 3) 네임스페이스: 태그가 ns2:order 처럼 접두사가 붙어 온다. 접두사/URI가 바뀌어도 깨지지 않게
    tag.split('}')[-1](로컬명)으로만 비교한다. find('ordNo')는 안 먹는다.
 4) 조회기간: 공식 상한이 비공개라 실무 관례대로 3일씩 끊어 호출한다(max_window_days=3).
    시간 형식은 yyyyMMddHHmm(초 없음).
 5) ★한 주문(ordNo)이 상품 수만큼 ns2:order로 쪼개져 온다. 주문 단위로 합쳐야 한다.
    상품행마다 주문을 만들면 같은 주문이 여러 건으로 늘어나 중복판정이 무너진다.
 6) 오류가 HTTP 200 + 오류 XML로 오는 경우가 있어 본문의 결과코드를 항상 확인한다.
    -500 인증된 IP 아님 / -400 SSL 필요 / 004 트래픽 초과.

키 발급 전이라 실제 호출로 검증하지 못했다. 필드명은 조사자료(docs/research/API_11번가.json)와
공개 구현들을 교차 확인한 것이라, 후보를 여러 개 두고 먼저 잡히는 값을 쓰도록 방어했다.
"""
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

from .base import MallAdapter, MallError, check_cooldown, register, set_cooldown

BASE_URL = "https://api.11st.co.kr/rest"
TIMEOUT = 25  # 초 — 11번가는 응답이 느린 편이라 넉넉히 준다

# 수집 대상 단계. complete=결제완료(신규), packaging=발주확인(상품준비중).
# 배송중/배송완료 단계 엔드포인트는 아예 부르지 않으므로 이미 나간 건은 들어오지 않는다.
DEFAULT_STAGES = ("complete", "packaging")
STAGE_LABELS = {"complete": "결제완료", "packaging": "발주확인"}

# 주문일시 후보(스펙서마다 이름이 달라 순서대로 찾는다)
ORDER_DATE_TAGS = ("ordDt", "ordDtm", "ordDate", "ordPrdRegDt", "payDt", "rgstDy")

# 이미 출고된 건을 걸러내는 '텍스트' 표식.
# ※ 숫자 상태코드(101/102/…)는 공식 규격서를 아직 못 봐서 추측하지 않는다.
#    잘못 찍으면 멀쩡한 신규주문이 조용히 사라지므로 확실한 한글 라벨만 본다.
SHIPPED_MARKS = ("배송중", "배송완료", "배송출발", "구매확정", "취소", "반품", "교환")


def _local(tag):
    """'{ns}ordNo' → 'ordNo'. 네임스페이스 접두사를 떼어낸다."""
    return tag.split("}")[-1] if isinstance(tag, str) else ""


def _fields(node):
    """한 노드 아래 자식들을 {로컬태그명: 텍스트}로 평탄화한다.

    11번가 주문 XML은 중첩이 얕고 태그명이 유일해서 flat 파싱이 안전하다.
    같은 이름이 여러 번 나오면 먼저 나온 값이 대표라 첫 값을 유지한다.
    """
    out = {}
    for el in node.iter():
        if el is node:
            continue
        name = _local(el.tag)
        text = (el.text or "").strip()
        if name and text and name not in out:
            out[name] = text
    return out


def _pick(data, *names):
    for n in names:
        v = (data.get(n) or "").strip()
        if v:
            return v
    return ""


def _int(v):
    """'12,000' / '12000.0' / '' 를 모두 안전하게 int로."""
    try:
        return int(float(str(v).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def _fmt_dt(dt):
    """11번가 시간 형식 yyyyMMddHHmm (초 없음)."""
    return dt.strftime("%Y%m%d%H%M")


def _norm_ordered_at(raw):
    """'20260728143005' → '2026-07-28 14:30:05'.

    중복판정(importers/dedupe.py)이 'YYYY-MM-DD HH:MM' 형태를 기대하므로 여기서 맞춘다.
    형식을 못 알아보면 원문을 그대로 넘긴다(임의 가공으로 정보를 잃지 않게).
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) >= 12:
        sec = digits[12:14] if len(digits) >= 14 else "00"
        return (f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]} "
                f"{digits[8:10]}:{digits[10:12]}:{sec}")
    if len(digits) == 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return (raw or "").strip()


def _decode(raw):
    """EUC-KR XML 바이트를 문자열로. 실패 시 cp949 → utf-8 순으로 재시도.

    UTF-8 바이트를 euc-kr로 디코딩하면 '오류 없이 깨진 글자'가 되는 함정이 있어서,
    선언부에 utf-8이 명시돼 있으면 그것을 먼저 쓴다(선언부는 ASCII라 안전하게 읽힌다).
    """
    if not raw:
        return ""
    head = raw[:200].decode("ascii", "ignore").lower()
    if "utf-8" in head or "utf8" in head:
        order = ("utf-8", "euc-kr", "cp949")
    else:
        order = ("euc-kr", "cp949", "utf-8")
    for enc in order:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    # 전부 실패해도 최소한 오류 메시지는 보여야 하므로 글자 일부 유실을 감수한다.
    return raw.decode("cp949", "replace")


def _parse_xml(raw, what="응답"):
    text = _decode(raw)
    # 위 함정 2) — XML 선언에 남은 encoding 속성이 파서를 막는 것을 예방한다.
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1).strip()
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        head = text[:200].replace("\n", " ")
        raise MallError(f"11번가 {what}을 해석하지 못했습니다(XML 아님): {head}") from e
    # 방화벽/차단 페이지가 HTTP 200 + HTML로 오는 경우가 있다. HTML도 형식만 맞으면 XML로
    # '파싱은 되기' 때문에, 그냥 두면 주문 0건으로 보여 "신규주문 없음"과 구분이 안 된다.
    if _local(root.tag).lower() in ("html", "head", "body"):
        shown = " ".join("".join(root.itertext()).split())[:200]
        raise MallError(
            f"11번가가 주문 데이터 대신 웹페이지를 돌려줬습니다(차단 또는 점검일 수 있음): {shown}"
        )
    return root


@register
class St11Adapter(MallAdapter):
    code = "st11"
    name = "11번가"
    # 조회기간 공식 상한이 비공개라 관례대로 3일씩 끊는다.
    max_window_days = 3
    # 호출 한도 수치도 비공개(초과 시 코드 004)라 보수적으로 1초 간격.
    call_interval = 1.0
    # ★ app/malls/__init__.py의 st11 fields 키와 정확히 일치해야 한다(오타 나면 영원히 '키 대기').
    #   seller_id는 API 호출에 쓰이지 않아(키가 셀러를 식별한다) 필수에서 뺀다.
    required_keys = ("api_key",)

    # ---- HTTP
    def _headers(self):
        return {
            # 헤더명은 소문자 openapikey.
            "openapikey": (self.s.get("api_key") or "").strip(),
            "Accept": "application/xml",
            "User-Agent": "HMS/1.0",
        }

    def _get(self, path):
        """경로 파라미터 방식 GET. path는 BASE_URL 뒤에 붙는 부분(앞에 / 포함)."""
        url = f"{BASE_URL}{path}"
        try:
            # HTTPS 필수(http로 부르면 -400). BASE_URL이 https라 여기서 강제된다.
            resp = requests.get(url, headers=self._headers(), timeout=TIMEOUT)
        except requests.Timeout as e:
            raise MallError(
                f"11번가 응답이 {TIMEOUT}초 안에 오지 않았습니다. 잠시 후 다시 시도해 주세요."
            ) from e
        except requests.RequestException as e:
            raise MallError(f"11번가 서버에 연결하지 못했습니다: {e}") from e

        self._check_http(resp)
        root = _parse_xml(resp.content)
        self._check_error(root)
        return root

    def _check_http(self, resp):
        """HTTP 상태코드로 먼저 원인을 구분한다(본문이 XML이 아닐 수도 있어서)."""
        code = resp.status_code
        if code == 200:
            return
        if code in (401, 403):
            raise MallError(
                f"11번가 인증에 실패했습니다(HTTP {code}). OPEN API KEY가 맞는지, "
                "셀러오피스 [Open API 관리]에 이 서버의 공인 IP가 등록돼 있는지 확인해 주세요."
            )
        if code == 429:
            raise MallError(
                "11번가 호출 한도를 초과했습니다(HTTP 429). "
                "수집 주기를 늘리거나 잠시 후 다시 시도해 주세요."
            )
        if 500 <= code < 600:
            # ★11번가는 'IP 미등록'도 HTTP 500으로 준다(2026-07-29 실제 확인 —
            #   본문: <AuthMessage><resultCode>-500</resultCode>[IP]인증된 IP가 아닙니다).
            #   '서버 오류, 나중에 다시'라고만 안내하면 대표가 원인을 영영 못 찾는다.
            body = _decode(resp.content)
            m = re.search(r"<resultMessage>([^<]+)</resultMessage>", body)
            if m:
                msg = m.group(1).strip()
                hint = (" — 셀러오피스 [Open API 관리]에 이 서버의 공인 IP를 등록해 주세요."
                        if "IP" in msg.upper() else "")
                raise MallError(f"11번가: {msg}{hint}")
            raise MallError(f"11번가 서버 오류입니다(HTTP {code}). 잠시 후 다시 시도해 주세요.")
        # ★ resp.text가 아니라 직접 디코딩 — EUC-KR 오류 본문이 깨지면 원인을 못 읽는다.
        head = _decode(resp.content)[:200].replace("\n", " ")
        raise MallError(f"11번가 호출이 실패했습니다(HTTP {code}): {head}")

    def _check_error(self, root):
        """HTTP 200으로 와도 본문이 오류 XML인 경우가 있어 결과코드를 확인한다."""
        data = _fields(root)
        # 응답마다 오류 태그 이름이 달라(result_code/resultCode/errorCode…) 후보를 넓게 본다.
        code = _pick(data, "result_code", "resultCode", "errorCode", "returnCode", "code")
        msg = _pick(data, "result_text", "resultMsg", "resultMessage",
                    "message", "errorMessage", "msg", "text")

        # 정상 응답에도 result_code 0/00/200 같은 값이 실려 오므로 성공값은 통과시킨다.
        if not code or code in ("0", "00", "000", "200", "100"):
            return
        if code == "-500":
            raise MallError(
                "11번가에 등록되지 않은 IP에서 호출했습니다(-500). 셀러오피스 [Open API 관리]에서 "
                "이 서버의 공인 IP를 등록해 주세요(여러 개는 세미콜론으로 구분). "
                "공인 IP가 바뀌면 다시 등록해야 합니다."
            )
        if code == "-400":
            raise MallError(
                "11번가는 HTTPS로만 호출할 수 있습니다(-400). 호출 주소가 https인지 확인해 주세요."
            )
        if code in ("004", "4", "overedTraffic"):
            set_cooldown(self.code)      # 계속 때리면 차단이 길어진다 — 5분 휴식
            raise MallError(
                "11번가 호출 한도를 초과했습니다(004) — 5분 쉬었다가 자동으로 재개됩니다. "
                "반복되면 자동수집 주기를 늘려 주세요."
            )
        # 그 밖의 오류는 메시지에 힌트가 있으면 원인을 짚어 준다.
        if "IP" in msg or "아이피" in msg:
            raise MallError(f"11번가 IP 인증 오류({code}): {msg} — 셀러오피스에 호출 IP를 등록해 주세요.")
        if "키" in msg or "key" in msg.lower() or "인증" in msg:
            raise MallError(f"11번가 인증 오류({code}): {msg} — OPEN API KEY를 다시 확인해 주세요.")
        raise MallError(f"11번가 오류({code}): {msg or '메시지 없음'}")

    # ---- 수집
    def _stages(self):
        """기본은 결제완료+발주확인. 계정 사정상 한쪽만 쓰려면 설정에 stages를 넣어 조절한다."""
        raw = (self.s.get("stages") or "").strip()
        if not raw:
            return DEFAULT_STAGES
        picked = tuple(p.strip() for p in raw.split(",") if p.strip() in DEFAULT_STAGES)
        return picked or DEFAULT_STAGES

    def collect_orders(self, since, until):
        # ordNo 하나에 상품행(ordPrdSeq)이 여러 개 → (주문번호, 상품순번)으로 중복을 걷어내며 모은다.
        # 창(window)이 겹치거나 결제완료/발주확인 양쪽에 같은 건이 잡혀도 수량·금액이 두 번 더해지지 않는다.
        lines = {}      # ordNo -> {ordPrdSeq: 필드dict}
        seq_order = {}  # ordNo -> [ordPrdSeq ...]  (등장 순서 유지 = 첫 행이 대표 상품)
        for stage in self._stages():
            for start, end in self.windows(since, until):
                path = f"/ordservices/{stage}/{_fmt_dt(start)}/{_fmt_dt(end)}"
                try:
                    root = self._get(path)
                except MallError as e:
                    # 어느 단계에서 막혔는지 알려야 원인을 짚을 수 있다.
                    raise MallError(f"[{STAGE_LABELS.get(stage, stage)} 조회] {e}") from e
                for node in root.iter():
                    if _local(node.tag) != "order":
                        continue
                    row = _fields(node)
                    ord_no = _pick(row, "ordNo", "orderNo")
                    if not ord_no or self._already_shipped(row):
                        continue
                    bucket = lines.setdefault(ord_no, {})
                    seqs = seq_order.setdefault(ord_no, [])
                    # ordPrdSeq가 비어 오면 순번을 만들어 붙인다(행이 서로 덮어쓰지 않게).
                    seq = _pick(row, "ordPrdSeq") or f"#{len(seqs) + 1}"
                    if seq in bucket:
                        continue
                    bucket[seq] = row
                    seqs.append(seq)
                # 레이트리밋(004) 회피 — 창마다 반드시 쉬어 간다.
                self.pace()
        return [self._build(no, [lines[no][s] for s in seq_order[no]]) for no in seq_order]

    def _already_shipped(self, row):
        """이미 나간 건 거르기.

        엔드포인트가 이미 단계를 걸러 주지만, 조회 도중 상태가 바뀌어 섞여 들어올 수 있다.
        송장번호(invcNo)가 이미 찍혔거나 상태 라벨이 배송/취소류면 신규주문이 아니다.
        """
        if _pick(row, "invcNo", "invoiceNo"):
            return True
        status = _pick(row, "ordPrdStatNm", "ordStatNm", "dlvStatNm", "ordPrdStat", "ordStat")
        return any(m in status for m in SHIPPED_MARKS)

    def _build(self, ord_no, rows):
        """상품행 여러 개를 주문 1건으로 합친다(대표 상품명 + 나머지는 optionName에 ' / ')."""
        main = rows[0]
        qty = 0
        amount = 0
        extras = []
        for row in rows:
            qty += _int(_pick(row, "ordQty", "ordPrdQty", "qty"))
            amount += _int(_pick(row, "ordPrdPayAmt", "ordPrdAmt", "selPrc"))
            if row is main:
                continue
            nm = _pick(row, "prdNm", "prdNam", "productName")
            opt = _pick(row, "ordOptNm", "ordPrdOptNm", "optNm", "sellerPrdOptNm")
            joined = f"{nm} {opt}".strip() if opt else nm
            if joined:
                extras.append(joined)

        # 대표 상품의 옵션이 맨 앞, 나머지 상품행이 뒤에 " / "로 붙는다.
        main_opt = _pick(main, "ordOptNm", "ordPrdOptNm", "optNm", "sellerPrdOptNm")
        option_parts = ([main_opt] if main_opt else []) + extras

        addr = " ".join(x for x in (
            _pick(main, "rcvrBaseAddr", "rcvrBascAddr", "rcvrAddr"),
            _pick(main, "rcvrDtlsAddr", "rcvrDtlAddr"),
        ) if x)

        return self.order(
            # 주문 단위로 유일 — 상품행마다 만들면 같은 주문이 여러 건으로 늘어난다.
            importKey=f"11번가:{ord_no}",
            orderNumber=ord_no,
            orderedAt=_norm_ordered_at(_pick(main, *ORDER_DATE_TAGS)),
            productName=_pick(main, "prdNm", "prdNam", "productName"),
            optionName=" / ".join(option_parts),
            # 판매자 상품코드가 있으면 그걸(우리 관리코드), 없으면 11번가 상품번호.
            productCode=_pick(main, "sellerPrdCd", "sellerProductCode", "prdNo"),
            quantity=max(1, qty),
            amount=amount,
            recipient=_pick(main, "rcvrNm", "receiverName"),
            # 휴대폰이 우선, 없으면 일반전화.
            phone=_pick(main, "rcvrMphon", "rcvrPrtblTel", "rcvrTlphn", "rcvrTelphon"),
            postalCode=_pick(main, "rcvrMailNo", "rcvrZipNo"),
            address=addr,
            deliveryMessage=_pick(main, "dlvMsg", "dlvReqCont", "ordMsg"),
            # dlvNo(배송번호)·ordPrdSeq는 나중에 송장전송(reqdelivery)에 반드시 필요해서 메모에 남긴다.
            memo=self._trace_memo(rows),
        )

    def _trace_memo(self, rows):
        """송장전송에 필요한 키를 메모에 보존한다(주문 dict 스키마를 늘리지 않기 위해)."""
        parts = []
        for row in rows:
            dlv_no = _pick(row, "dlvNo")
            seq = _pick(row, "ordPrdSeq")
            if dlv_no or seq:
                parts.append(f"dlvNo={dlv_no or '-'},ordPrdSeq={seq or '-'}")
        return "11번가 " + "; ".join(parts) if parts else ""

    # ---- 송장 전송 (실제 배선은 나중에 — 시그니처와 구현만 갖춰 둔다)
    def upload_invoice(self, order_no, invoice_no, courier_code="", **kw):
        """발송처리. 전 파라미터가 URL 경로에 들어가는 GET이다.

        GET /rest/ordservices/reqdelivery/{sendDt}/{dlvMthdCd}/{dlvEtprsCd}/{invcNo}
                                         /{dlvNo}/{partDlvYn}/{ordNo}/{ordPrdSeq}

        ★ 선행조건: 11번가는 발주확인(reqpackaging)을 먼저 해야 발송처리가 받아들여진다.
        ★ dlvEtprsCd(택배사 코드)는 11번가 자체 코드표다. CJ대한통운 코드를 추측해서 기본값으로
          박아두면 엉뚱한 택배사로 발송처리될 수 있어, 확인된 코드를 명시적으로 받도록 했다.
          (키 발급 후 오픈API센터 규격서에서 확정해 설정 courier_code에 넣을 것)
        ★ 실제 주문 상태를 바꾸는 쓰기 호출이다 — 검증은 격리 프로브로만 할 것.
        """
        dlv_no = str(kw.get("dlv_no") or kw.get("dlvNo") or "").strip()
        ord_prd_seq = str(kw.get("ord_prd_seq") or kw.get("ordPrdSeq") or "").strip()
        if not dlv_no or not ord_prd_seq:
            raise MallError(
                "11번가 발송처리에는 배송번호(dlvNo)와 상품순번(ordPrdSeq)이 필요합니다. "
                "주문 수집 시 저장된 값을 함께 넘겨 주세요."
            )
        code = str(courier_code or self.s.get("courier_code") or "").strip()
        if not code.isdigit():
            raise MallError(
                "11번가 택배사 코드(dlvEtprsCd)가 설정되지 않았습니다. "
                "11번가 자체 코드표에서 CJ대한통운 코드를 확인해 설정에 넣어 주세요."
            )
        send_dt = str(kw.get("send_dt") or "").strip()
        if not send_dt:
            from .. import config
            send_dt = _fmt_dt(config.now())
        dlv_mthd_cd = str(kw.get("dlv_mthd_cd") or "01").strip()   # 01 = 택배
        part_dlv_yn = str(kw.get("part_dlv_yn") or "N").strip()    # 부분배송 여부

        seg = [send_dt, dlv_mthd_cd, code, str(invoice_no).strip(),
               dlv_no, part_dlv_yn, str(order_no).strip(), ord_prd_seq]
        if not all(seg):
            raise MallError("11번가 발송처리에 필요한 값이 비어 있습니다.")
        # 경로 파라미터라 값마다 인코딩한다(값에 /가 섞이면 경로가 통째로 어긋난다).
        path = "/ordservices/reqdelivery/" + "/".join(quote(s, safe="") for s in seg)
        self._get(path)
        return True

    def confirm_packaging(self, order_no, ord_prd_seq, dlv_no, add_prd_yn="N", add_prd_no="0"):
        """발주확인(상품준비중 전환). 발송처리 전 선행 단계라 함께 갖춰 둔다.

        GET /rest/ordservices/reqpackaging/{ordNo}/{ordPrdSeq}/{addPrdYn}/{addPrdNo}/{dlvNo}
        ※ 실제 주문 상태를 바꾸는 쓰기 호출이다 — 검증은 격리 프로브로만 할 것.
        """
        seg = [str(order_no).strip(), str(ord_prd_seq).strip(),
               str(add_prd_yn).strip(), str(add_prd_no).strip(), str(dlv_no).strip()]
        if not all(seg):
            raise MallError("11번가 발주확인에 필요한 값이 비어 있습니다.")
        path = "/ordservices/reqpackaging/" + "/".join(quote(s, safe="") for s in seg)
        self._get(path)
        return True
