"""몰 주문 수집 실행 — 어댑터가 가져온 주문을 기존 중복판정 로직에 태워 저장한다.

엑셀 임포트와 완전히 같은 경로(new_unique_orders)를 쓰므로, 같은 주문이
엑셀로도 들어오고 API로도 들어와도 중복 생성되지 않는다.
"""
import json
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

    상품명·짧은설명·가격은 몰에서 수시로 바뀌므로 HMS에 저장해 두지 않고
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
# ★대표 지시(2026-08-05): "RMS로 들어가는 렌탈 상품이 HMS에 뜬다. 상품번호로 안 뜨게 하자."
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
