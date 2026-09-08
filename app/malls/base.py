"""몰 어댑터 공통 계층.

각 몰 어댑터는 MallAdapter를 상속해 collect_orders만 구현하면 된다.
반환 형식은 엑셀 임포터와 동일한 주문 dict(camelCase)라서, 수집 결과가
기존 중복판정·배송지변경 로직을 그대로 탄다.

키가 없거나 사용이 꺼져 있으면 어댑터는 만들어지지 않는다(수집 시도 자체를 안 함).
"""
import re
import threading
import time
from datetime import timedelta

from .. import config


class MallError(Exception):
    """수집 실패 — 메시지는 사용자에게 그대로 보여준다."""


# ================================================================ 호출 예산 보호
# 몰마다 주문수집 호출·토큰 발급 한도가 빡빡하다. RMS가 스마트스토어에서 겪은
# 시행착오(토큰 남발 → 발급 한도 소진, 429 후 연타 → 차단 연장)를 여기서 막는다.
#
# 세 장치 모두 '프로세스 전역'이다 — 어댑터 인스턴스는 수집 때마다 새로 만들어지므로
# 인스턴스에 캐시하면 아무 소용이 없다(그게 토큰 남발의 원인이었다).

# ① 토큰 캐시 — OAuth 몰(스마트스토어·토스)이 발급받은 토큰을 만료까지 재사용한다.
_TOKEN_CACHE = {}
_TOKEN_LOCK = threading.Lock()



# ---------------------------------------------------------------- 제품코드
# 업무관리 제품코드 = 「모델_CPU_그래픽」 (예: 840 G3_i7-6_내장, P16V Gen1_i7-13_내장).
# 몰마다 이 값이 들어오는 칸이 다르다:
#   고도몰      자체상품코드(goodsCd)           — 그대로
#   쿠팡        등록상품명(sellerProductName)   — 등급 접미사가 붙어 옴
#   스마트스토어  판매자상품코드(sellerProductCode 계열)
# 그래서 '어느 칸에서 왔든' 코드 모양이면 뽑아 쓰도록 한곳에 모아 둔다.
_RE_GRADE_TAIL = re.compile(r"\s*(S\+?[SABC]|[SA][SAB]|[SABC])\s*급\s*\d*")


def product_code_in(text):
    """문자열에서 제품코드를 뽑는다. 코드 모양이 아니면 빈 값.

    등급 접미사(AA급3)와 사은품 표기(+한컴)는 떼어 낸다 — 붙여 두면 등급마다
    다른 상품으로 갈려 재고가 쪼개진다(등급은 옵션에서 따로 읽는다).
    """
    t = ("" if text is None else str(text)).split("/")[0].strip()
    if t.count("_") != 2:
        return ""
    t = _RE_GRADE_TAIL.sub("", t).split("+")[0].strip()
    parts = [x.strip() for x in t.split("_")]
    return "_".join(parts) if all(parts) else ""


def pick_product_code(*candidates):
    """여러 후보 중 '코드 모양'인 첫 값을 고른다. 없으면 빈 값.

    몰이 칸 이름을 바꾸거나 판매자가 다른 칸에 넣어도 버티게 하기 위한 것.
    """
    for v in candidates:
        got = product_code_in(v)
        if got:
            return got
    return ""


def cached_token(kind, ident):
    """살아 있는 토큰이 있으면 돌려준다(만료 60초 전부터는 없다고 답해 미리 갱신)."""
    with _TOKEN_LOCK:
        hit = _TOKEN_CACHE.get((kind, ident))
        if hit and time.time() < hit["expires"] - 60:
            return hit["token"]
    return ""


def store_token(kind, ident, token, ttl_seconds):
    with _TOKEN_LOCK:
        _TOKEN_CACHE[(kind, ident)] = {"token": token,
                                       "expires": time.time() + max(60, ttl_seconds)}


def drop_token(kind, ident):
    """401을 받으면 캐시를 버린다 — 몰 쪽에서 먼저 무효화된 토큰일 수 있다."""
    with _TOKEN_LOCK:
        _TOKEN_CACHE.pop((kind, ident), None)


# ② 429 쿨다운 — 한도 초과를 맞으면 그 몰은 잠시 쉰다. 계속 때리면 차단이 길어진다.
_COOLDOWN = {}
_COOLDOWN_LOCK = threading.Lock()
COOLDOWN_SECONDS = 300        # RMS 실운영 값(5분) 계승


def set_cooldown(code, seconds=COOLDOWN_SECONDS):
    with _COOLDOWN_LOCK:
        _COOLDOWN[code] = time.time() + seconds


def cooldown_left(code):
    """남은 쿨다운(초, 올림). 0이면 호출해도 된다.

    내림(int)으로 하면 방금 건 1초짜리 쿨다운이 0.99초 남았을 때 0으로 보여
    바로 뚫린다 — 남아 있으면 남아 있다고 답해야 한다.
    """
    with _COOLDOWN_LOCK:
        until = _COOLDOWN.get(code, 0)
    left = until - time.time()
    return max(0, int(left) + (1 if left % 1 > 0 else 0)) if left > 0 else 0


def check_cooldown(code, name=""):
    """쿨다운 중이면 호출을 시작하기 전에 막는다(호출 예산을 아끼는 게 목적)."""
    left = cooldown_left(code)
    if left:
        raise MallError(f"{name or code} 호출 한도 초과로 잠시 쉬는 중입니다 — "
                        f"{left // 60}분 {left % 60}초 뒤 자동으로 재개됩니다.")


# ③ 동시 수집 방지 — 자동수집·수동수집·연결테스트가 같은 몰을 겹쳐 부르면
#    호출 예산만 두 배로 쓴다. 한 몰은 한 번에 하나만.
_INFLIGHT = {}
_INFLIGHT_LOCK = threading.Lock()


class collect_guard:
    """with collect_guard(code, name): ... — 이미 진행 중이면 바로 MallError."""

    def __init__(self, code, name=""):
        self.code, self.name = code, name

    def __enter__(self):
        with _INFLIGHT_LOCK:
            lock = _INFLIGHT.setdefault(self.code, threading.Lock())
        if not lock.acquire(blocking=False):
            raise MallError(f"{self.name or self.code} 수집이 이미 진행 중입니다 — "
                            "끝나면 자동으로 반영됩니다.")
        self._lock = lock
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False


class MallAdapter:
    code = ""
    name = ""
    # 몰이 허용하는 최대 조회 기간(일). 이보다 길게 요청하면 나눠서 부른다.
    max_window_days = 7
    # 연속 호출 사이 최소 간격(초) — 몰별 레이트리밋 회피
    call_interval = 0.4

    def __init__(self, settings):
        self.s = settings or {}

    # ---- 하위 클래스가 구현
    def collect_orders(self, since, until):
        raise NotImplementedError

    def search_goods(self, keyword="", field="auto", size=20):
        """상품 실시간 조회 — 지원하는 몰만 구현한다.

        상품명·가격은 수시로 바뀌므로 OWS에 저장해 두지 않고 그때그때 몰에 물어본다.
        """
        raise MallError(f"{self.name}은(는) 상품 조회를 지원하지 않습니다.")

    # ---- 공통 유틸
    def windows(self, since, until):
        """조회 기간을 몰이 허용하는 길이로 잘라 (시작, 끝) 목록을 만든다."""
        out = []
        cur = since
        step = timedelta(days=self.max_window_days)
        while cur < until:
            end = min(cur + step, until)
            out.append((cur, end))
            cur = end
        return out or [(since, until)]

    def pace(self):
        time.sleep(self.call_interval)

    def order(self, **kw):
        """주문 dict 생성 — 임포터와 같은 키를 쓴다."""
        base = {
            "importKey": "", "channel": self.name, "sourceFile": f"API:{self.code}",
            "orderNumber": "", "orderedAt": "", "productName": "", "optionName": "",
            "productCode": "", "quantity": 1, "amount": 0, "recipient": "", "phone": "",
            "postalCode": "", "address": "", "deliveryMessage": "", "memo": "",
        }
        base.update(kw)
        if not base["importKey"]:
            base["importKey"] = f"{self.name}:{base['orderNumber']}"
        return base


# 등록된 어댑터 구현 (없으면 '미구현'으로 표시)
_IMPLS = {}


def register(cls):
    _IMPLS[cls.code] = cls
    return cls


def get_adapter(code, settings):
    """설정이 준비된 몰만 어댑터를 돌려준다. 아니면 (None, 사유)."""
    cls = _IMPLS.get(code)
    if cls is None:
        return None, "아직 수집 어댑터가 구현되지 않았습니다."
    s = settings or {}
    if not s.get("enabled"):
        return None, "설정에서 [이 몰 사용]이 꺼져 있습니다."
    missing = [k for k in cls.required_keys if not (s.get(k) or "").strip()]
    if missing:
        return None, f"필수 인증정보가 비어 있습니다: {', '.join(missing)}"
    return cls(s), ""


def implemented_codes():
    return sorted(_IMPLS.keys())
