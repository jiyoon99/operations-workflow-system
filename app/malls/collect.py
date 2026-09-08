"""몰 주문 수집 실행 — 어댑터가 가져온 주문을 기존 중복판정 로직에 태워 저장한다.

엑셀 임포트와 완전히 같은 경로(new_unique_orders)를 쓰므로, 같은 주문이
엑셀로도 들어오고 API로도 들어와도 중복 생성되지 않는다.
"""
import json
import re
from datetime import datetime, timedelta

from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require, require_any
from ..db import get_db, tx
from ..importers import new_unique_orders
from ..orders.mapping import insert_import_dict, row_to_import_dict, writeback_changed
from ..settings import _int_or_400, bp
from . import MALLS
from .base import (MallError, collect_guard, cooldown_left, get_adapter,
                   implemented_codes)
# 어댑터 등록 — import해야 @register가 실행된다. 새 몰을 추가하면 여기에도 넣을 것.
from . import (  # noqa: F401
    coupang, esm, godomall, kakao, lotteon, smartstore, st11, temu, toss,
)


def _mall_settings(conn, code=None):
    row = conn.execute("SELECT value FROM settings WHERE key='malls'").fetchone()
    data = {}
    if row:
        try:
            data = json.loads(row["value"]) or {}
        except ValueError:
            data = {}
    return data.get(code, {}) if code else data


def _last_sync(conn, code):
    row = conn.execute("SELECT value FROM settings WHERE key='mall_sync'").fetchone()
    if not row:
        return None
    try:
        return (json.loads(row["value"]) or {}).get(code)
    except ValueError:
        return None


def _save_sync(conn, code, info):
    row = conn.execute("SELECT value FROM settings WHERE key='mall_sync'").fetchone()
    data = {}
    if row:
        try:
            data = json.loads(row["value"]) or {}
        except ValueError:
            data = {}
    data[code] = info
    conn.execute(
        "INSERT INTO settings(key, value, updated_at, updated_by) VALUES('mall_sync',?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, "
        "updated_by=excluded.updated_by",
        (json.dumps(data, ensure_ascii=False), config.now_iso(),
         g.user["display_name"] if g.get("user") else ""))


@bp.get("/mall-status")
def mall_status():
    """몰별 연동 준비 상태 — 화면에서 '수집 가능/사유'를 보여준다.

    수집을 실행하는 사람(orders.import)도 어떤 몰을 수집할 수 있는지 봐야 하므로
    settings.manage만으로 막지 않는다. 키 값 자체는 내려주지 않는다.
    """
    require_any("settings.manage", "orders.import")
    conn = get_db()
    settings = _mall_settings(conn)
    impl = set(implemented_codes())
    out = []
    for m in MALLS:
        s = settings.get(m["code"], {})
        adapter, why = get_adapter(m["code"], s)
        from .scheduler import interval_for
        out.append({
            "code": m["code"], "name": m["name"],
            "implemented": m["code"] in impl,
            "enabled": bool(s.get("enabled")),
            "ready": adapter is not None,
            "reason": why,
            "lastSync": _last_sync(conn, m["code"]),
            "intervalMin": interval_for(m["code"]),   # 자동수집 주기(시스템 결정)
        })
    return jsonify(out)


@bp.post("/malls/<code>/collect")
def collect(code):
    """한 몰의 주문을 수집한다. days로 조회 기간을 정한다(기본 3일)."""
    require("orders.import")
    body = request.get_json(silent=True) or {}
    days = _int_or_400(body.get("days") or 3, "조회 기간")
    if not 1 <= days <= 30:
        abort(400, description="조회 기간은 1~30일 사이여야 합니다.")
    dry = bool(body.get("dryRun"))

    with tx() as conn:
        s = _mall_settings(conn, code)
    adapter, why = get_adapter(code, s)
    if adapter is None:
        abort(400, description=why)

    until = config.now()
    since = until - timedelta(days=days)
    try:
        # 자동수집과 겹치면 같은 주문을 두 번 부른다 — 호출 예산 보호(한 몰은 한 번에 하나만)
        with collect_guard(code, adapter.name):
            fetched = adapter.collect_orders(since, until)
    except MallError as e:
        with tx(write=True) as conn:
            _save_sync(conn, code, {"at": config.now_iso(), "ok": False, "error": str(e)})
        abort(502, description=str(e))
    except Exception as e:
        abort(502, description=f"{adapter.name} 수집 중 오류: {e}")

    if dry:
        with tx() as conn:
            existing = [row_to_import_dict(r) for r in conn.execute("SELECT * FROM orders").fetchall()]
        added, updates = new_unique_orders([dict(d) for d in existing], fetched, now=config.now_iso())
        return jsonify({"mall": adapter.name, "fetched": len(fetched), "added": len(added),
                        "duplicates": len(fetched) - len(added), "shippingUpdates": updates,
                        "dryRun": True,
                        "preview": [{"orderNumber": o.get("orderNumber"), "productName": o.get("productName"),
                                     "recipient": o.get("recipient"), "amount": o.get("amount")}
                                    for o in added[:20]]})

    skipped = list(getattr(adapter, "skipped_rental", []) or [])
    with tx(write=True) as conn:
        rows = conn.execute("SELECT * FROM orders").fetchall()
        existing = [row_to_import_dict(r) for r in rows]
        snapshot = {d["_rowId"]: json.dumps(d, ensure_ascii=False, sort_keys=True, default=str)
                    for d in existing}
        added, updates = new_unique_orders(existing, fetched, now=config.now_iso())
        for o in added:
            insert_import_dict(conn, o, f"API:{adapter.name}")
        changed = writeback_changed(conn, snapshot, existing)
        info = {"at": config.now_iso(), "ok": True, "fetched": len(fetched),
                "added": len(added), "shippingUpdates": updates}
        if skipped:
            # 렌탈 주문을 몇 건 걸렀는지 남긴다 — '조용히 사라졌다'가 되면 안 된다
            info["skippedRental"] = len(skipped)
            info["skippedSample"] = skipped[:5]
        _save_sync(conn, code, info)
        audit.log("orders_imported", target=f"{adapter.name} API",
                  detail={"fetched": len(fetched), "added": len(added),
                          "duplicates": len(fetched) - len(added), "shippingUpdates": updates})
    return jsonify({"mall": adapter.name, "fetched": len(fetched), "added": len(added),
                    "duplicates": len(fetched) - len(added), "shippingUpdates": updates,
                    "updatedExisting": changed,
                    "skippedRental": len(skipped), "skippedSample": skipped[:5]})


@bp.post("/malls/collect-all")
def collect_all():
    """준비된 몰 전부 즉시 수집 — 주문관리의 [🔄 새로고침] 버튼.

    자동수집 주기와 무관하게 지금 돈다. 단 429 쿨다운 중이거나 이미 수집 중인 몰은
    건너뛰고 사유를 알려준다(호출 예산 보호 장치는 새로고침도 뚫을 수 없다).
    """
    require("orders.import")
    from flask import current_app
    from .scheduler import _collect_one
    with tx() as conn:
        settings = _mall_settings(conn)
    results = []
    for code, s in (settings or {}).items():
        if not isinstance(s, dict) or not s.get("enabled"):
            continue
        adapter, _why = get_adapter(code, s)
        if adapter is None:
            continue
        left = cooldown_left(code)
        if left:
            results.append({"mall": adapter.name, "ok": False,
                            "error": f"호출 한도 초과로 쉬는 중 — {left // 60}분 {left % 60}초 뒤 재개"})
            continue
        results.append(_collect_one(current_app._get_current_object(), code, adapter))
    if not results:
        abort(400, description="수집할 수 있는 몰이 없습니다. 설정 > API 관리에서 키를 넣고 "
                               "[이 몰 사용]을 켜 주세요.")
    return jsonify({"results": results})


@bp.get("/malls/<code>/goods")
def search_goods(code):
    """상품 실시간 조회 — 수기 주문 입력에서 자체상품코드로 찾아 채워 넣는다.

    상품명·짧은설명·가격은 몰에서 수시로 바뀌므로 OWS에 저장해 두지 않고
    입력하는 그 순간 몰에 물어본다(캐시 없음).
    """
    require("orders.edit")
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"mall": code, "goods": [], "message": "두 글자 이상 입력하세요."})
    field = request.args.get("field") or "auto"
    if field not in ("auto", "code", "name"):
        abort(400, description="검색 기준이 올바르지 않습니다.")
    size = _int_or_400(request.args.get("size") or 20, "조회 건수")
    if not 1 <= size <= 100:
        abort(400, description="조회 건수는 1~100 사이여야 합니다.")

    with tx() as conn:
        s = _mall_settings(conn, code)
    adapter, why = get_adapter(code, s)
    if adapter is None:
        abort(400, description=why)
    try:
        goods = adapter.search_goods(q, field=field, size=size)
    except MallError as e:
        abort(502, description=str(e))
    except Exception as e:
        abort(502, description=f"{adapter.name} 상품 조회 중 오류: {e}")
    return jsonify({"mall": adapter.name, "goods": goods, "count": len(goods)})


# ── 매입 화면용: 고도몰에 그 제품코드가 실제로 있는가 ────────────────────────
#
# ★왜 따로 두는가 (대표 지시 2026-08-07)
#   "제품 제목이 필요한 게 아니라, 그 제품코드가 확실히 있는지 확인하고 매칭하려는 것."
#   기존 /api/product-codes 는 우리 assets 를 모아 보여 주므로 '우리가 이미 쓰던 코드'만
#   나온다. 몰에 올려는 뒀는데 아직 한 대도 매입 안 한 코드는 안 뜬다. 그 코드를 손으로
#   타이핑하면 오타 하나로 몰 재고 연동이 통째로 어긋난다.
#
# ★권한을 넓히는 이유
#   /malls/<code>/goods 는 orders.edit 이라 매입만 보는 사람은 못 쓴다. 여기서 하는 일은
#   '코드가 있나 없나' 조회뿐이라 purchase.view 로도 열어 준다. 쓰기는 일절 없다.
#
# ★캐시를 두는 이유
#   글자를 칠 때마다 부르는 자리다. 몰 API를 두들기면 수집이 한도에 걸린다.
_CODE_CACHE = {}                       # q -> (만료시각, 결과)
_CODE_CACHE_TTL = 120                  # 초
_CODE_CACHE_MAX = 200


def _cache_get(key):
    hit = _CODE_CACHE.get(key)
    if not hit:
        return None
    until, val = hit
    if until < datetime.now().timestamp():
        _CODE_CACHE.pop(key, None)
        return None
    return val


def _cache_put(key, val):
    if len(_CODE_CACHE) > _CODE_CACHE_MAX:
        _CODE_CACHE.clear()
    _CODE_CACHE[key] = (datetime.now().timestamp() + _CODE_CACHE_TTL, val)


@bp.get("/mall-product-codes")
def mall_product_codes():
    """몰에 등록된 제품코드 찾기 — 매입 화면의 제품코드 칸에서 대조용으로 쓴다.

    ★오류를 내지 않는다. 몰을 안 켰거나 몰이 죽어 있어도 200으로 ok:false 만 돌려준다.
      글자마다 부르는 자리라 여기서 400/502 를 내면 화면이 빨간 토스트로 뒤덮인다.
      매입 화면의 기존 자동완성(우리 자산 기준)은 그대로 돌아야 한다.
    """
    require_any("purchase.view", "orders.edit")
    q = (request.args.get("q") or "").strip()
    code = (request.args.get("mall") or "godomall").strip()
    if len(q) < 2:
        return jsonify({"ok": True, "codes": [], "exact": False,
                        "message": "두 글자 이상 입력하세요."})

    key = "%s|%s" % (code, q.lower())
    cached = _cache_get(key)
    if cached is not None:
        return jsonify(cached)

    with tx() as conn:                                   # 읽기 전용
        s = _mall_settings(conn, code)
    adapter, why = get_adapter(code, s)
    if adapter is None:
        return jsonify({"ok": False, "codes": [], "exact": False, "reason": why})

    try:
        # field="code" — 상품명이 아니라 자체상품코드로만 찾는다. 제목은 필요 없다.
        goods = adapter.search_goods(q, field="code", size=20)
    except MallError as e:
        return jsonify({"ok": False, "codes": [], "exact": False, "reason": str(e)})
    except Exception as e:                                       # noqa: BLE001
        return jsonify({"ok": False, "codes": [], "exact": False,
                        "reason": "%s 상품 조회 중 오류: %s" % (adapter.name, e)})

    out, seen = [], set()
    for g in goods:
        c = (g.get("goodsCd") or "").strip()
        if not c or c in seen:
            continue
        seen.add(c)
        out.append({
            "code": c,
            "name": g.get("goodsNm") or "",
            "stock": g.get("stock") or 0,
            "soldOut": bool(g.get("soldOut")),
            "stateLabel": g.get("stateLabel") or "",
        })
    res = {
        "ok": True, "mall": adapter.name, "codes": out,
        # 지금 친 글자가 몰의 코드와 '똑같이' 있는가 — 매칭해도 되는지 판단하는 값
        "exact": any(c["code"].lower() == q.lower() for c in out),
    }
    _cache_put(key, res)
    return jsonify(res)


# ── 제품코드 모델 속성(2026-08-13 대표) ──────────────────────────────────────
#
# 고도몰에 코드로 등록된 상품의 스펙 텍스트에는 DDR3/4/5, NVMe·M.2 SATA·2.5 SATA가
# 정확히 구분돼 적혀 있다(대표 확인). 그걸 파싱해 코드 단위로 저장해 두면
# 옵션 자동 기입("16GB 추가")이 D4/D5 어느 부품인지 스스로 고를 수 있다.
# ★자산 스펙(실물)에는 절대 복사하지 않는다 — 몰 스펙은 '출고 사양'이라
#   램 없이 매입된 실물에 그대로 적으면 거짓 스펙이 된다.

RAM_GENS = ("DDR3", "DDR4", "DDR5", "LPDDR3", "LPDDR4", "LPDDR5")
# 저장장치 4종(2026-08-13 대표 확정): 2.5 HDD / 2.5 SSD / M.2 SATA / M.2 NVMe
STORAGE_TYPES = ("M.2 NVMe", "M.2 SATA", "2.5 SSD", "2.5 HDD")
# 램 모듈로 존재하는 용량(GB) — 저장장치 용량(128G~)과 겹치지 않아 구분 축이 된다
RAM_SIZES = (2, 4, 6, 8, 12, 16, 24, 32, 64)


def parse_code_caps(text):
    """스펙 텍스트에서 (기준 램 GB, 기준 저장 용량 라벨)을 읽는다 — 매입 부족분 계산용.

    ★공백을 지우면 'DDR4 8GB'가 'DDR48GB'가 돼 48GB로 오독된다 — 여기서는 공백을
      살린 채 대문자만 만든다(parse_code_spec의 세대 판별과 정규화가 다른 이유).
    램은 DDR·RAM·램 낱말 근처의 숫자를 우선하고, 모듈로 존재하는 용량(RAM_SIZES)만
    인정한다. 저장 용량은 TB 우선, 없으면 100GB 이상 첫 숫자(램과 안 겹침).
    라벨은 부품 이름 규약("256G"/"1TB")과 같게 만든다 — 이름이 곧 매칭 키다.
    """
    u = (text or "").upper()
    ram_gb = 0
    m = (re.search(r"(?:LP)?DDR[345]X?[^0-9]{0,6}(\d{1,3})\s*G", u)
         or re.search(r"(\d{1,3})\s*GB?[^0-9A-Z가-힣]{0,3}(?:(?:LP)?DDR|RAM|램)", u)
         or re.search(r"(?:RAM|램)[^0-9]{0,6}(\d{1,3})\s*G", u))
    if m and int(m.group(1)) in RAM_SIZES:
        ram_gb = int(m.group(1))
    if not ram_gb:
        m = re.search(r"\b(\d{1,2})\s*GB?\b", u)
        if m and int(m.group(1)) in RAM_SIZES:
            ram_gb = int(m.group(1))
    cap = ""
    mt = re.search(r"([12])\s*TB", u)
    if mt:
        cap = mt.group(1) + "TB"
    else:
        for m2 in re.finditer(r"(\d{3,4})\s*GB?", u):
            v = int(m2.group(1))
            if v >= 100:
                cap = f"{v}G"
                break
    return ram_gb, cap


def parse_code_spec(text):
    """상품명+짧은설명 텍스트에서 (램 세대, 온보드 여부, 저장장치 방식)을 읽는다.

    ★LPDDR4 안에 'DDR4'가 들어 있다 — LP를 먼저 잡고, 일반 DDR은 (?<!LP)로 가른다.
      온보드+확장슬롯 하이브리드(예: "4GB 온보드(LPDDR4) + DDR4 슬롯")면 슬롯 세대(DDR4)를
      ram_gen으로, 온보드 표기는 ram_onboard로 따로 남긴다 — 업글 옵션은 슬롯에 꽂는다.
    ★저장장치는 4종(2.5 HDD/2.5 SSD/M.2 SATA/M.2 NVMe, 대표 확정)으로 가른다.
      'SATA 2.5인치'만 있고 SSD/HDD 낱말이 없으면 비워 둔다 — HDD인지 SSD인지
      추측으로 부품을 고르면 안 되는 자리다(비어 있으면 자동 기입이 보류하고 알린다).
      듀얼 드라이브(SSD+HDD) 표기는 SSD 쪽을 기준으로 잡는다(주 저장장치).
    """
    raw = text or ""
    t = re.sub(r"\s", "", raw.upper())
    onboard = ("온보드" in raw) or ("ONBOARD" in t) or ("ON-BOARD" in t)
    lp = re.search(r"LPDDR([345])X?", t)
    if lp:
        onboard = True                      # LP 램은 납땜(온보드) — 슬롯 업글 대상이 아니다
    slot = re.search(r"(?<!LP)DDR([345])", t)
    ram_gen = f"DDR{slot.group(1)}" if slot else (f"LPDDR{lp.group(1)}" if lp else "")
    if "NVME" in t:
        storage = "M.2 NVMe"
    elif re.search(r"M\.?2SATA", t):
        storage = "M.2 SATA"
    elif "SSD" in t and ("SATA" in t or re.search(r"2[.,]5", t)):
        storage = "2.5 SSD"
    elif "HDD" in t:
        storage = "2.5 HDD"                 # 노트북 HDD는 2.5인치뿐이다
    else:
        storage = ""
    return ram_gen, onboard, storage


def _code_spec_payload(r):
    return {"code": r["code"], "ramGen": r["ram_gen"], "ramOnboard": bool(r["ram_onboard"]),
            "ramGb": r["ram_gb"], "storageType": r["storage_type"],
            "storageCap": r["storage_cap"], "source": r["source"],
            "specText": r["spec_text"], "updatedAt": r["updated_at"],
            "updatedBy": r["updated_by"]}


@bp.get("/code-specs")
def get_code_spec():
    require_any("purchase.view", "orders.view", "setup.view")
    code = (request.args.get("code") or "").strip()
    if not code:
        abort(400, description="제품코드를 지정하세요.")
    row = get_db().execute("SELECT * FROM code_specs WHERE code=?", (code,)).fetchone()
    return jsonify(_code_spec_payload(row) if row
                   else {"code": code, "ramGen": "", "ramOnboard": False, "ramGb": 0,
                         "storageType": "", "storageCap": "", "source": "",
                         "specText": ""})


@bp.post("/code-specs/sync")
def sync_code_spec():
    """몰에서 이 코드의 상품 스펙을 불러와 파싱·저장한다.

    ★제품코드는 몰마다 동일하다(대표 확인 2026-08-13: 스마트스토어 판매자상품코드 =
      고도몰 자체상품코드). 그래서 고도몰 → 나머지 몰 순서로, 상품 조회를 지원하고
      연동이 켜진 몰을 차례로 뒤져 처음 정확히 일치하는 상품을 쓴다 — 고도몰에
      없는 코드도 다른 몰에 있으면 스펙이 잡힌다.
    ★사람이 확정한 값(source='manual')은 덮지 않는다 — 파서가 몰 표기 변화로
      틀린 값을 되씌우면 원인을 찾기 어렵다. 원문은 spec_text로 남겨 대조 가능하게.
    """
    require_any("purchase.edit", "orders.edit")
    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").strip()
    if not code:
        abort(400, description="제품코드를 지정하세요.")
    with tx() as conn:
        all_s = _mall_settings(conn)
    hit, found = None, None
    tried, errors = [], []
    order = ["godomall"] + [m["code"] for m in MALLS if m["code"] != "godomall"]
    for mall_code in order:
        adapter, _why = get_adapter(mall_code, all_s.get(mall_code, {}))
        if adapter is None:
            continue                                   # 연동이 꺼진 몰
        if "search_goods" not in type(adapter).__dict__:
            continue                                   # 상품 조회 미지원 몰
        tried.append(adapter.name)
        try:
            goods = adapter.search_goods(code, field="code", size=20)
        except MallError as e:
            errors.append(f"{adapter.name}: {e}")
            continue                                   # 이 몰이 죽어도 다음 몰을 본다
        except Exception as e:                                       # noqa: BLE001
            errors.append(f"{adapter.name}: {e}")
            continue
        # ★첫 정확 일치가 아니라 '대표 등록'을 고른다(2026-09-01 적대 리뷰 #7) —
        #   같은 코드의 테스트 등록(스펙 빈값)이 먼저 오면 빈 판정이 code_specs 에 박힌다.
        from ..orders.product_info import pick_exact_goods   # 순환 import 방지(함수 안)
        hit = pick_exact_goods(goods, code)
        if hit is not None:
            found = mall_code
            break
    if hit is None:
        if not tried:
            abort(400, description="상품 조회를 지원하는 몰 연동이 켜져 있지 않습니다 — "
                                   "설정 ▸ API 관리에서 고도몰(또는 스마트스토어)을 확인하세요.")
        msg = f"{' · '.join(tried)}에서 상품코드 '{code}'를 찾지 못했습니다."
        if errors:
            msg += " (오류: " + " / ".join(errors)[:200] + ")"
        abort(404, description=msg)
    text = " ".join(x for x in (hit.get("goodsNm"), hit.get("shortDescription"),
                                hit.get("modelNo")) if x)
    ram_gen, onboard, storage = parse_code_spec(text)
    ram_gb, storage_cap = parse_code_caps(text)
    ts = config.now_iso()
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM code_specs WHERE code=?", (code,)).fetchone()
        if row and row["source"] == "manual":
            # 원문 스냅샷만 갱신 — 판정값은 사람 것이 정본
            conn.execute("UPDATE code_specs SET spec_text=? WHERE code=?", (text[:500], code))
            row = conn.execute("SELECT * FROM code_specs WHERE code=?", (code,)).fetchone()
            return jsonify({**_code_spec_payload(row), "kept": "manual"})
        conn.execute(
            "INSERT INTO code_specs(code, ram_gen, ram_onboard, ram_gb, storage_type, "
            "storage_cap, source, spec_text, updated_at, updated_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET ram_gen=excluded.ram_gen, "
            "ram_onboard=excluded.ram_onboard, ram_gb=excluded.ram_gb, "
            "storage_type=excluded.storage_type, storage_cap=excluded.storage_cap, "
            "source=excluded.source, spec_text=excluded.spec_text, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (code, ram_gen, 1 if onboard else 0, ram_gb, storage, storage_cap, found,
             text[:500], ts, g.user["display_name"]))
        audit.log("code_spec_synced", target=code,
                  detail={"ramGen": ram_gen, "onboard": onboard, "ramGb": ram_gb,
                          "storage": storage, "storageCap": storage_cap, "mall": found})
        row = conn.execute("SELECT * FROM code_specs WHERE code=?", (code,)).fetchone()
    return jsonify(_code_spec_payload(row))


@bp.patch("/code-specs")
def set_code_spec():
    """사람이 세대·방식을 확정한다 — 이후 sync가 와도 안 덮인다(source='manual')."""
    require_any("purchase.edit", "orders.edit")
    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").strip()
    if not code:
        abort(400, description="제품코드를 지정하세요.")
    ram_gen = (body.get("ramGen") or "").strip()
    storage = (body.get("storageType") or "").strip()
    if ram_gen and ram_gen not in RAM_GENS:
        abort(400, description=f"램 세대는 {', '.join(RAM_GENS)} 중 하나여야 합니다.")
    if storage and storage not in STORAGE_TYPES:
        abort(400, description=f"저장장치 방식은 {', '.join(STORAGE_TYPES)} 중 하나여야 합니다.")
    try:
        ram_gb = int(body.get("ramGb") or 0)
    except (TypeError, ValueError):
        abort(400, description="기준 램 용량(GB)이 올바르지 않습니다.")
    if ram_gb and ram_gb not in RAM_SIZES:
        abort(400, description=f"기준 램 용량은 {', '.join(map(str, RAM_SIZES))}GB 중 하나여야 합니다.")
    storage_cap = (body.get("storageCap") or "").strip().upper()
    if storage_cap and not re.fullmatch(r"\d{2,4}G|[12]TB", storage_cap):
        abort(400, description="기준 저장 용량은 '256G'·'1TB' 형식이어야 합니다.")
    ts = config.now_iso()
    with tx(write=True) as conn:
        conn.execute(
            "INSERT INTO code_specs(code, ram_gen, ram_onboard, ram_gb, storage_type, "
            "storage_cap, source, spec_text, updated_at, updated_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET ram_gen=excluded.ram_gen, "
            "ram_onboard=excluded.ram_onboard, ram_gb=excluded.ram_gb, "
            "storage_type=excluded.storage_type, storage_cap=excluded.storage_cap, "
            "source='manual', updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (code, ram_gen, 1 if body.get("ramOnboard") else 0, ram_gb, storage,
             storage_cap, "manual", "", ts, g.user["display_name"]))
        audit.log("code_spec_set", target=code,
                  detail={"ramGen": ram_gen, "onboard": bool(body.get("ramOnboard")),
                          "ramGb": ram_gb, "storage": storage, "storageCap": storage_cap})
        row = conn.execute("SELECT * FROM code_specs WHERE code=?", (code,)).fetchone()
    return jsonify(_code_spec_payload(row))


@bp.post("/malls/<code>/test")
def test_connection(code):
    """연결 테스트 — 최근 1일치를 읽어보기만 하고 저장하지 않는다."""
    require("settings.manage")
    with tx() as conn:
        s = _mall_settings(conn, code)
    adapter, why = get_adapter(code, s)
    if adapter is None:
        abort(400, description=why)
    until = config.now()
    try:
        with collect_guard(code, adapter.name):
            fetched = adapter.collect_orders(until - timedelta(days=1), until)
    except MallError as e:
        abort(502, description=str(e))
    except Exception as e:
        abort(502, description=f"{adapter.name} 연결 실패: {e}")
    return jsonify({"ok": True, "mall": adapter.name, "fetched": len(fetched),
                    "message": f"연결 성공 — 최근 1일 주문 {len(fetched)}건을 읽었습니다(저장하지 않음)."})


@bp.get("/malls/godomall/couriers")
def godomall_couriers():
    """고도몰 택배사 목록 + CJ대한통운 sno(RMS 이식 2026-09-08) — 설정 화면 [🔍 택배사 조회].

    읽기 전용 조회(Code_Search)라 몰에 아무 변경도 생기지 않는다. 송장 전송이 택배사 없이
    올라가면 고객 배송조회가 안 붙으므로, 켜기 전에 여기서 번호를 눈으로 확인한다.
    """
    require("settings.manage")
    with tx() as conn:
        s = _mall_settings(conn, "godomall")
    adapter, why = get_adapter("godomall", s)
    if adapter is None:
        abort(400, description=why)
    try:
        couriers = adapter.list_couriers()
    except MallError as e:
        abort(502, description=str(e))
    except Exception as e:                                        # noqa: BLE001
        abort(502, description=f"고도몰 택배사 조회 실패: {e}")
    cj = next((c["sno"] for c in couriers
               if "CJ" in c["name"].replace(" ", "").upper() or "대한통운" in c["name"]), "")
    return jsonify({"ok": True, "couriers": couriers, "cjSno": cj})


@bp.post("/malls/<code>/backfill-amounts")
def backfill_amounts(code):
    """금액이 0으로 들어온 주문의 진짜 금액을 몰에서 다시 받아 채운다.

    ★왜 필요한가 (2026-08-05 실측)
      쿠팡이 금액 필드를 숫자에서 **Money 객체**로 바꿔 보내기 시작했다(7/28~).
        "orderPrice": {"currencyCode": "KRW", "units": 376740, "nanos": 0}
      기존 파서가 dict를 숫자로 못 읽어 **38건이 전부 0원**으로 들어왔고,
      그만큼 매출·마진이 비어 있었다. 파서는 고쳤지만 이미 들어온 건은 그대로다.

    ★안전 규칙
      - **금액이 0인 주문만** 건드린다. 값이 있는 주문은 덮어쓰지 않는다
        (사람이 고쳐 넣은 금액을 몰 값으로 되돌리면 안 된다).
      - 몰 조회(GET)만 한다. 송장 전송 같은 쓰기는 하지 않는다.
      - 외부 호출은 트랜잭션 밖에서 한다(원칙 #1).
    """
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    dry = bool(body.get("dryRun"))

    with tx() as conn:
        st = _mall_settings(conn, code)
    adapter, why = get_adapter(code, st)
    if adapter is None:
        abort(400, description=why)

    with tx() as conn:
        rows = conn.execute(
            "SELECT id, order_no, ordered_at FROM orders "
            "WHERE channel=? AND cancelled_at='' AND amount=0 AND order_no<>'' "
            "  AND import_key NOT LIKE '주문수집%' ORDER BY ordered_at",
            (adapter.name,)).fetchall()
    if not rows:
        return jsonify({"ok": True, "filled": 0, "checked": 0, "amount": 0, "items": []})

    # ---- 몰 조회는 트랜잭션 밖에서
    days = sorted({(r["ordered_at"] or "")[:10].replace(".", "-") for r in rows if r["ordered_at"]})
    found = {}
    errors = []
    for day in days:
        try:
            d0 = datetime.fromisoformat(day)
            for o in (adapter.collect_orders(d0, d0) or []):
                no = str(o.get("orderNumber") or "").strip()
                amt = int(o.get("amount") or 0)
                if no and amt:
                    found[no] = max(found.get(no, 0), amt)
        except Exception as e:                                   # noqa: BLE001
            errors.append(f"{day}: {str(e)[:80]}")

    items = [{"id": r["id"], "orderNumber": r["order_no"],
              "amount": found.get((r["order_no"] or "").strip(), 0)} for r in rows]
    items = [x for x in items if x["amount"]]
    if dry:
        return jsonify({"ok": True, "dryRun": True, "checked": len(rows),
                        "filled": len(items), "amount": sum(x["amount"] for x in items),
                        "items": items, "errors": errors})

    ts = config.now_iso()
    filled = 0
    with tx(write=True) as conn:
        for x in items:
            # 다시 확인 — 그 사이 사람이 금액을 넣었으면 덮어쓰지 않는다
            cur = conn.execute("SELECT amount FROM orders WHERE id=?", (x["id"],)).fetchone()
            if cur is None or cur["amount"]:
                continue
            conn.execute("UPDATE orders SET amount=?, updated_at=? WHERE id=?",
                         (x["amount"], ts, x["id"]))
            filled += 1
        if filled:
            audit.log("mall_amount_backfill",
                      target=f"{adapter.name} {filled}건 금액 채움",
                      detail={"filled": filled, "amount": sum(x["amount"] for x in items),
                              "errors": errors[:3]})
    return jsonify({"ok": True, "checked": len(rows), "filled": filled,
                    "amount": sum(x["amount"] for x in items), "errors": errors})


# ─────────────────── 렌탈 주문 골라내기(RMS 몫) ───────────────────
# ★대표 지시(2026-08-05): "RMS로 들어가는 렌탈 상품이 OWS에 뜬다. 상품번호로 안 뜨게 하자."
#
# ★수집 단계 필터만으로는 부족하다
#   어댑터의 렌탈 분류는 **앞으로 들어올 주문**만 막는다. 이미 들어와 화면에 떠 있는 건
#   그대로 남아 작업자가 만들려고 든다(실측 #663 스마트스토어 렌탈, 8일째 제작 대기).
#   그래서 화면에서 [렌탈로 보내기] 한 번으로
#     ① 그 주문을 작업 목록에서 내리고
#     ② 그 상품번호를 몰 설정에 등록해 **다음부터는 아예 안 들어오게** 한다.
#   상품번호를 몰에서 찾아 손으로 옮겨 적을 필요가 없다.

RENTAL_WORDS = ("렌탈", "렌털", "렌트", "대여", "임대", "사용기간")


def _rental_hits(text):
    t = str(text or "")
    return [w for w in RENTAL_WORDS if w in t]


@bp.get("/orders/rental-suspects")
def rental_suspects():
    """작업 목록에 남아 있는 '렌탈로 보이는' 주문.

    ★상품명 전체가 아니라 **상품명 자체**만 본다. 옵션 문구까지 보면
      '팬리스', '보노보스' 같은 말에 걸려 판매 상품이 렌탈로 오인된다
      (2026-08-05 실측: '리스'로 찾으면 6건 중 4건이 오탐).
    """
    require_any("orders.view", "setup.view")
    with tx() as conn:
        mall_cfg = _mall_settings(conn)
        rows = conn.execute(
            "SELECT id, channel, order_no, product_code, product_name, recipient, amount, "
            "       ordered_at FROM orders "
            "WHERE cancelled_at='' AND archived_at='' AND shipping_done=0 "
            "ORDER BY ordered_at").fetchall()
    out = []
    for r in rows:
        hits = _rental_hits(r["product_name"])
        if not hits:
            continue
        code = (r["product_code"] or "").strip()
        cfg = (mall_cfg.get(_code_of(r["channel"])) or {}) if r["channel"] else {}
        listed = code and code in _split_ids(cfg.get("rental_product_ids"))
        out.append({
            "id": r["id"], "channel": r["channel"], "orderNumber": r["order_no"],
            "productCode": code, "productName": r["product_name"],
            "recipient": r["recipient"], "amount": r["amount"],
            "orderedAt": r["ordered_at"], "words": hits, "registered": bool(listed),
        })
    return jsonify({"orders": out, "count": len(out)})


def _split_ids(value):
    import re as _re
    return {t for t in _re.split(r"[,\s]+", str(value or "").strip()) if t}


def _code_of(channel):
    """채널 이름 → 몰 코드."""
    for m in MALLS:
        if m["name"] == channel:
            return m["code"]
    return ""


@bp.post("/orders/mark-rental")
def mark_rental():
    """고른 주문을 렌탈(RMS) 몫으로 보내 작업 목록에서 내린다.

    body.register 가 참이면 그 상품번호를 몰 설정의 '렌탈 상품번호'에 등록해
    **다음부터는 수집 자체가 안 되게** 한다.
    ★출고·취소된 주문은 건드리지 않는다. 매출·재고는 손대지 않는다 —
      작업 목록에서 내리고 왜 내렸는지만 남긴다.
    """
    require("orders.edit")
    body = request.get_json(silent=True) or {}
    ids = [int(x) for x in (body.get("ids") or [])]
    register = body.get("register", True)
    if not ids:
        abort(400, description="렌탈로 보낼 주문을 고르세요.")
    ts = config.now_iso()
    moved, registered = 0, {}
    with tx(write=True) as conn:
        cfg = _mall_settings(conn)
        for oid in ids:
            r = conn.execute(
                "SELECT id, channel, product_code, product_name, shipping_done, cancelled_at, "
                "       archived_at FROM orders WHERE id=?", (oid,)).fetchone()
            if r is None or r["shipping_done"] or r["cancelled_at"] or r["archived_at"]:
                continue
            conn.execute(
                "UPDATE orders SET archived_at=?, archive_reason=?, updated_at=? WHERE id=?",
                (ts, "렌탈(RMS) 주문이라 판매 작업 목록에서 내렸습니다. "
                     "매출·재고는 건드리지 않았습니다.", ts, oid))
            audit.log("order_marked_rental", target=f"주문 #{oid} {r['product_name'][:30]}",
                      detail={"channel": r["channel"], "productCode": r["product_code"]})
            moved += 1
            code = (r["product_code"] or "").strip()
            mall = _code_of(r["channel"])
            if register and code and mall:
                cur = cfg.setdefault(mall, {})
                ids_now = _split_ids(cur.get("rental_product_ids"))
                if code not in ids_now:
                    ids_now.add(code)
                    cur["rental_product_ids"] = ", ".join(sorted(ids_now))
                    registered.setdefault(mall, []).append(code)
        if registered:
            # 몰 설정 저장 — 비밀 키는 그대로 두고 목록만 바꾼다(같은 dict를 다시 쓴다)
            conn.execute(
                "INSERT INTO settings(key, value, updated_at, updated_by) VALUES('malls',?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (json.dumps(cfg, ensure_ascii=False), ts, g.user["display_name"]))
            audit.log("mall_rental_registered",
                      target="렌탈 상품번호 등록",
                      detail={k: v for k, v in registered.items()})
    return jsonify({"ok": True, "moved": moved,
                    "registered": {k: sorted(set(v)) for k, v in registered.items()}})


@bp.post("/orders/<int:oid>/unmark-rental")
def unmark_rental(oid):
    """렌탈로 잘못 보낸 주문을 되돌린다(상품번호 등록은 설정에서 지운다)."""
    require("orders.edit")
    ts = config.now_iso()
    with tx(write=True) as conn:
        r = conn.execute("SELECT archive_reason FROM orders WHERE id=?", (oid,)).fetchone()
        if r is None or not (r["archive_reason"] or "").startswith("렌탈"):
            abort(400, description="이 도구로 내린 주문만 되돌릴 수 있습니다.")
        conn.execute("UPDATE orders SET archived_at='', archive_reason='', updated_at=? WHERE id=?",
                     (ts, oid))
        audit.log("order_unmarked_rental", target=f"주문 #{oid}")
    return jsonify({"ok": True})
