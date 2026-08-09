"""주문 상품의 '지금 몰 정보' — 스펙(짧은설명)과 재고를 셋팅 화면에 띄운다.

대표 요청(2026-07-29): 셋팅 탭에서 상품명 아래에 스펙이 나열되고, 그 다음 옵션,
그리고 제품별 재고수량이 보여야 한다. 같은 재고를 여러 고객이 사면 줄어드는 것까지
감안해 고도몰과 맞춘다 — 그래서 우리 DB에 저장해 두지 않고 그때그때 몰에 물어본다.

★호출 예산 보호가 핵심이다. 셋팅 화면은 5초마다 새로 그리는데 그때마다 몰을 부르면
  하루에 수만 번이 된다. 그래서
   ① 상품코드 단위로 캐시(기본 10분) ② 한 번 부를 때 화면에 뜬 코드만 ③ 코드당 1회
   ④ 몰 호출 실패는 조용히 넘긴다(스펙이 안 보이는 것뿐, 작업은 계속돼야 한다).
"""
import re
import threading
import time

from flask import abort, g, jsonify, request

from ..auth.perms import ORDER_READ_PERMS, require_any
from ..db import tx
from ..malls.base import cooldown_left, get_adapter
from . import bp

# ★malls.collect는 함수 안에서 import한다 — 모듈 최상단에서 부르면
#   app.orders ↔ app.malls가 서로를 부르는 순환이 되어 앱이 아예 안 뜬다.

CACHE_TTL = 600          # 초 — 재고는 자주 바뀌지만 10분이면 실무에 충분하다
MAX_CODES = 60           # 한 번에 받을 상품코드 수(화면에 뜨는 만큼)
# ★한 요청에서 몰에 새로 물어볼 최대 개수.
#   전부 모아 한 번에 답하면 54개 조회에 27초가 걸려 그동안 화면이 텅 빈다
#   (대표: "스펙이 왜 안 나와?" 2026-07-30). 조금씩 채워 넣고 나머지는 다음 요청에 맡긴다.
MAX_NEW_LOOKUPS = 8

_cache = {}              # (mall, code) -> {"at": epoch, "data": {...}}
_lock = threading.Lock()


def _cached(key):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit["at"] < CACHE_TTL:
            return hit["data"]
    return None


def _store(key, data):
    with _lock:
        _cache[key] = {"at": time.time(), "data": data}


def lookup(adapter, code):
    """상품코드 하나 조회. 실패하면 None(화면은 스펙 없이 그대로 돈다)."""
    key = (adapter.code, code.strip().lower())
    hit = _cached(key)
    if hit is not None:
        return hit
    try:
        rows = adapter.search_goods(code, field="code", size=5)
    except Exception:                                    # noqa: BLE001
        return None                                      # 캐시하지 않는다 — 다음에 다시 시도
    exact = next((r for r in rows if (r.get("goodsCd") or "").strip().lower()
                  == code.strip().lower()), None) or (rows[0] if rows else None)
    if exact is None:
        _store(key, {})                                  # 없는 코드는 캐시해 재조회를 아낀다
        return {}
    data = {
        "code": exact.get("goodsCd") or code,
        # ★자사몰 상품 페이지 주소는 goodsCd가 아니라 goodsNo로 만든다.
        #   이미 받아 온 값이라 추가 호출은 없다(셋팅 화면에서 상품명 클릭에 쓴다).
        "goodsNo": exact.get("goodsNo") or "",
        "name": exact.get("goodsNm") or "",
        "spec": (exact.get("shortDescription") or "").strip(),
        "stock": exact.get("stock"),
        "soldOut": bool(exact.get("soldOut")),
        "price": exact.get("price"),
        "stateLabel": exact.get("stateLabel") or "",
    }
    _store(key, data)
    return data


def model_of(code, name=""):
    """상품코드에서 '모델명'을 뽑는다 — 창고 재고와 이어 주기 위한 열쇠.

    고도몰 자체상품코드는 「모델_CPU_그래픽」 규칙이라 첫 칸이 모델이다.
      NT371B5M_i7-7_내장   → NT371B5M
      Z16 Gen1_R7p-6_RX6500M → Z16 Gen1
    코드가 없으면(다른 몰) 상품명 안의 영문+숫자 덩어리를 모델로 본다.
    """
    import re as _re
    c = (code or "").strip()
    # ★고도몰 코드 규칙에만 기대면 쿠팡·스마트스토어는 상품ID가 숫자라
    #   모델을 못 뽑아 '창고 0대'가 뜬다(실제로는 90대 있어도).
    #   숫자만인 코드는 코드가 아니라 몰의 내부 번호이므로 상품명에서 모델을 찾는다.
    if c and not c.isdigit():
        head = c.split("_")[0].strip()
        if head and not head.isdigit():
            return head
    m = _re.search(r"[A-Za-z]{1,4}[0-9][A-Za-z0-9-]{2,}", name or "")
    return m.group(0) if m else ""


def our_stock(conn, models):
    """창고에 실제로 남은 판매 가능 수량 — 몰 API가 없어도 모든 쇼핑몰에 보여줄 수 있다.

    자산 model 표기가 '삼성 NT371B5M'처럼 앞뒤가 붙는 경우가 있어 포함 검색을 쓴다.

    ★포함 검색은 뒤에 글자가 붙는 '다른 모델'까지 끌어온다. 실측(2026-07-31):
        갤럭시탭 S6  → 실제 2대인데 45대(갤럭시탭 S6 Lite SM-P610 43대를 같이 셈)
        THINKPAD T470 → 실제 1대인데 11대(T470S 10대)
      반대로 DB400T3 → DB400T3A 는 같은 모델이라 포함 검색이라야 맞다.
      둘 다 '모델명 + 알파벳' 꼴이라 규칙으로는 못 가른다. 그래서 숫자를 고르는 대신
      '무엇을 셌는지'를 같이 돌려준다 — 화면이 내역을 보여 주고 사람이 판단한다.

    돌려주는 값: {검색모델: {"count": 총합, "byModel": [{"model": 자산모델, "count": n}, ...]}}
    """
    from ..purchase import AVAILABLE_STATUSES
    out = {}
    marks = ",".join("?" * len(AVAILABLE_STATUSES))
    for m in models:
        if not m:
            continue
        like = "%" + m.upper().replace(" ", "") + "%"
        rows = conn.execute(
            f"SELECT model, COUNT(*) AS c FROM assets WHERE status IN ({marks}) AND received=1 "
            "AND REPLACE(UPPER(model),' ','') LIKE ? GROUP BY model ORDER BY c DESC",
            list(AVAILABLE_STATUSES) + [like]).fetchall()
        key = m.upper().replace(" ", "")
        detail = [{"model": r["model"] or "(모델없음)", "count": r["c"],
                   # 검색어와 표기가 완전히 같은 것만 '확실'로 본다
                   "exact": (r["model"] or "").upper().replace(" ", "") == key}
                  for r in rows]
        # ★등급별로도 나눠 준다. 주문은 'AA급'을 팔았는데 창고 90대가 전부 A급·B급이면
        #   '창고 90대'는 실무에서 쓸모없는 숫자다(2026-08-03 실측: 등급이 적힌 주문 89건 중
        #   59건이 요구 등급 재고 0대). 앞 글자=외관, 뒤 글자=배터리(B급만 단일).
        grades = conn.execute(
            f"SELECT grade, COUNT(*) AS c FROM assets WHERE status IN ({marks}) AND received=1 "
            "AND REPLACE(UPPER(model),' ','') LIKE ? GROUP BY grade ORDER BY c DESC",
            list(AVAILABLE_STATUSES) + [like]).fetchall()
        # ★재고 3단계(2026-08-04) — 셋팅/QC가 '지금 바로 나갈 수 있는 게 몇 대인지'를
        #   알아야 한다. 가재고는 매칭은 되지만 출고가 막히므로 총합에 섞여 있으면
        #   담당자가 있는 줄 알고 잡았다가 송장 단계에서 되돌아온다.
        tiers = conn.execute(
            f"SELECT tier, COUNT(*) AS c FROM assets WHERE status IN ({marks}) AND received=1 "
            "AND REPLACE(UPPER(model),' ','') LIKE ? GROUP BY tier",
            list(AVAILABLE_STATUSES) + [like]).fetchall()
        by_tier = {(r["tier"] or "양품"): r["c"] for r in tiers}
        out[m] = {
            "count": sum(d["count"] for d in detail),
            "byModel": detail,
            "byGrade": [{"grade": (r["grade"] or "미정"), "count": r["c"]} for r in grades],
            "byTier": by_tier,
            # 지금 바로 출고 가능한 것(가재고 제외) — 화면이 강조해서 보여준다
            "shippable": by_tier.get("양품", 0) + by_tier.get("실재고", 0),
        }
    return out


# 제품코드 = 「모델_CPU_그래픽」(예: 840 G3_i7-6_내장). 모델·그래픽에 공백이 들어갈 수 있고,
# 몰에 따라 뒤에 등급(AA급3)·사은품(+한컴)이 붙어 온다 — 떼어 내고 코드만 남긴다.
_RE_GRADE_TAIL = re.compile(r"\s*(S\+?[SABC]|[SA][SAB]|[SABC])\s*급\s*\d*")


def sku_of(code, name=""):
    """주문에서 제품코드를 뽑는다. 코드칸에 없으면 상품명에서 찾는다(쿠팡이 그렇다)."""
    for raw in (code, name):
        t = (raw or "").split("/")[0].strip()
        if t.count("_") != 2:
            continue
        t = _RE_GRADE_TAIL.sub("", t).split("+")[0].strip()
        parts = [x.strip() for x in t.split("_")]
        if all(parts):
            return "_".join(parts)
    return ""


def _put_code_stock(dst, sku, by_code):
    """제품코드 대조 결과를 응답에 담는다. 코드가 없거나 등록 자산이 없으면 그 사실을 알린다."""
    info = by_code.get(sku or "") or {}
    dst["sku"] = sku or ""
    dst["codeStock"] = info.get("count", 0)
    dst["codeTier"] = info.get("byTier", {})
    dst["codeShippable"] = info.get("shippable", 0)
    dst["codeRegistered"] = bool(info.get("registered"))


def stock_by_code(conn, codes):
    """제품코드로 정확히 대조한 재고 — 셋팅/QC가 믿을 수 있는 숫자.

    ★모델명 근사 대조(our_stock)는 'NT371B5M'이 'NT371B5M2'까지 끌어오는 등
      틀릴 수밖에 없다(2026-07-31 실측: 갤럭시탭 S6 2대인데 45대로 표시).
      제품코드(840 G3_i7-6_내장)는 대표가 직접 매긴 값이라 대조가 정확하다.
      코드가 아직 안 들어간 자산은 여기 안 잡힌다 — 그 사실을 화면이 따로 알린다.
    """
    from ..purchase import AVAILABLE_STATUSES
    out = {}
    marks = ",".join("?" * len(AVAILABLE_STATUSES))
    for code in codes:
        code = (code or "").strip()
        if not code:
            continue
        rows = conn.execute(
            f"SELECT tier, COUNT(*) AS c FROM assets WHERE status IN ({marks}) AND received=1 "
            "AND TRIM(product_code)=? GROUP BY tier",
            list(AVAILABLE_STATUSES) + [code]).fetchall()
        by_tier = {(r["tier"] or "양품"): r["c"] for r in rows}
        total = sum(by_tier.values())
        out[code] = {
            "count": total,
            "byTier": by_tier,
            "shippable": by_tier.get("양품", 0) + by_tier.get("실재고", 0),
            "registered": total > 0,       # 이 코드로 등록된 자산이 하나라도 있나
        }
    return out


def _shop_url():
    """설정에 적어 둔 자사몰 주소 — 상품명을 눌러 상품 페이지로 갈 때 쓴다."""
    from ..malls.collect import _mall_settings              # 순환 import 방지
    with tx() as conn:
        return str((_mall_settings(conn, "godomall") or {}).get("shop_url") or "").strip().rstrip("/")


@bp.post("/orders/product-info")
def product_info():
    """상품코드 목록 → {코드: {스펙, 재고, 품절}}.

    셋팅 화면이 한 번에 물어본다. 몰이 준비 안 됐거나 쿨다운 중이면 빈 결과를 준다
    (기능이 없는 것처럼 조용히 — 작업을 막지 않는다).
    """
    require_any(*ORDER_READ_PERMS)
    body = request.get_json(silent=True) or {}
    codes = body.get("codes")
    if not isinstance(codes, list):
        abort(400, description="codes 배열이 필요합니다.")
    mall = (body.get("mall") or "godomall").strip()
    # ★스펙을 물어볼 코드는 따로 받는다. 화면에는 여러 몰의 주문이 섞여 있는데
    #   쿠팡·11번가 상품ID를 고도몰에 물어봐야 나올 리 없다 — 호출만 버린다.
    #   (창고 재고는 우리 DB만 보므로 몰과 무관하게 전부 계산해 준다)
    spec_only = body.get("specCodes")
    spec_set = ({str(x or "").strip() for x in spec_only if str(x or "").strip()}
                if isinstance(spec_only, list) else None)

    wanted = []
    for c in codes:
        c = str(c or "").strip()
        if c and c not in wanted:
            wanted.append(c)
    if not wanted:
        return jsonify({"mall": mall, "products": {}, "reason": "", "availableAssets": None})
    wanted = wanted[:MAX_CODES]

    # ★창고 재고는 몰 API가 없어도 항상 보여줄 수 있다(DB만 본다) — 모든 쇼핑몰 공통.
    #   상품코드에서 모델을 뽑아 맞춘다(고도몰 코드 규칙: 모델_CPU_그래픽).
    # 상품명도 함께 받는다 — 코드가 숫자뿐인 몰(쿠팡·스마트스토어)은 이름에서 모델을 뽑는다
    names = body.get("names") or {}
    if not isinstance(names, dict):
        names = {}
    with tx() as conn:
        models = {c: model_of(c, str(names.get(c) or "")) for c in wanted}
        stocks = our_stock(conn, set(models.values()))
        # ★제품코드 대조가 정답이다(2026-08-04 대표). 모델명 근사는 코드가 없을 때의 보조.
        skus = {c: sku_of(c, str(names.get(c) or "")) for c in wanted}
        by_code = stock_by_code(conn, set(skus.values()))
        # ★창고에 붙일 수 있는 자산이 아예 0대면 스캔이 무조건 실패한다.
        #   그 사실을 화면 위에 띄워 줘야 담당자가 번호 탓을 하며 헤매지 않는다.
        from ..purchase import AVAILABLE_STATUSES
        marks = ",".join("?" * len(AVAILABLE_STATUSES))
        avail_total = conn.execute(
            f"SELECT COUNT(*) AS c FROM assets WHERE status IN ({marks}) AND received=1",
            list(AVAILABLE_STATUSES)).fetchone()["c"]
    ours = {c: (stocks.get(models[c]) or {"count": 0, "byModel": [], "byGrade": [], "byTier": {}, "shippable": 0}) for c in wanted}

    # 캐시에 있는 것부터 — 몰을 안 불러도 되는 경우가 대부분이다
    out, missing = {}, []
    for c in wanted:
        if spec_set is not None and c not in spec_set:
            continue                       # 이 몰 상품이 아니다 — 창고 재고만 준다
        hit = _cached((mall, c.lower()))
        if hit is not None:
            if hit:
                out[c] = dict(hit)
        else:
            missing.append(c)
    for c in wanted:                       # 몰 정보가 없어도 창고 재고는 넣어 준다
        out.setdefault(c, {"code": c})
        st = ours.get(c) or {"count": 0, "byModel": [], "byGrade": [], "byTier": {}, "shippable": 0}
        out[c]["ourStock"] = st["count"]
        out[c]["ourStockBy"] = st["byModel"]          # 무엇을 셌는지 — 화면이 내역으로 보여준다
        out[c]["ourStockGrade"] = st.get("byGrade", [])   # 등급별 내역
        out[c]["ourStockTier"] = st.get("byTier", {})     # 양품/실재고/가재고
        out[c]["ourShippable"] = st.get("shippable", 0)   # 가재고 뺀 출고 가능분
        _put_code_stock(out[c], skus.get(c), by_code)
        out[c]["ourModel"] = models.get(c, "")        # 어떤 모델명으로 찾았는지
        # ★아직 몰에 못 물어본 코드는 그렇다고 표시한다. 표시가 없으면 화면이
        #   "이미 다 받았다"고 판단해 다시 묻지 않고, 스펙이 영영 안 채워진다.
        if c in missing:
            out[c]["pending"] = True
    if not missing:
        return jsonify({"mall": mall, "products": out, "reason": "",
                        "availableAssets": avail_total, "shopUrl": _shop_url()})

    if cooldown_left(mall):
        return jsonify({"mall": mall, "products": out, "availableAssets": avail_total,
                        "shopUrl": _shop_url(),
                        "reason": "몰 호출 한도로 잠시 쉬는 중입니다(창고 재고만 표시)."})
    from ..malls.collect import _mall_settings          # 순환 import 방지
    with tx() as conn:
        s = _mall_settings(conn, mall)
    adapter, why = get_adapter(mall, s)
    if adapter is None:
        return jsonify({"mall": mall, "products": out, "reason": why,
                        "availableAssets": avail_total, "shopUrl": _shop_url()})

    todo = missing[:MAX_NEW_LOOKUPS]
    for c in todo:
        data = lookup(adapter, c)
        if data is None:                   # 조회 실패 — 다음 요청에서 다시 시도한다
            continue
        st = ours.get(c) or {"count": 0, "byModel": [], "byGrade": [], "byTier": {}, "shippable": 0}
        out[c] = dict(data, ourStock=st["count"], ourStockBy=st["byModel"],
                      ourStockGrade=st.get("byGrade", []),
                      ourStockTier=st.get("byTier", {}), ourShippable=st.get("shippable", 0),
                      ourModel=models.get(c, ""))      # pending 표시가 사라진다(= 다 받음)
        _put_code_stock(out[c], skus.get(c), by_code)
    left = sum(1 for c in wanted if out.get(c, {}).get("pending"))
    return jsonify({"mall": mall, "products": out, "reason": "",
                    "availableAssets": avail_total, "shopUrl": _shop_url(),
                    # 남은 게 있으면 화면이 곧 다시 물어본다(폴링) — 점점 채워진다
                    "pending": left})
