"""몰 주문 자동수집 데몬.

설정에서 [주문 자동수집]을 켠 몰만, 정해진 주기로 주문을 가져온다.
스위치가 있는데 아무 일도 안 하면 화면이 거짓말을 하는 것이라 실제로 동작하게 한다.

원칙
- 배송추적 데몬(app/orders/recall.py:start_tracker)과 같은 모양으로 만든다(검증된 패턴).
- 몰 API 호출은 절대 tx(write) 안에서 하지 않는다 — 그동안 전 시스템 쓰기가 잠긴다.
- 한 몰이 실패해도 다른 몰 수집은 계속한다. 실패는 mall_sync에 남아 화면에 보인다.
- 키가 없거나 스위치가 꺼져 있으면 아무것도 하지 않는다(기본 상태에서 조용하다).
"""
import threading
import time
from datetime import timedelta

from .. import audit, config
from ..db import get_db, tx
from ..importers import new_unique_orders
from ..orders.mapping import insert_import_dict, row_to_import_dict, writeback_changed
from .base import MallError, collect_guard, cooldown_left, get_adapter

# ★몰별 자동수집 주기(분) — 몰 호출 한도에 맞춰 시스템이 정한다.
#   대표가 주기를 직접 고를 필요가 없다(2026-07-29 결정: 키를 넣었다는 것 자체가
#   수집하겠다는 뜻이므로, 자동수집 스위치·주기 칸을 없애고 여기서 관리한다).
#   값의 근거: 호출 예산이 넉넉한 몰은 짧게, 한도가 빡빡하거나 미확인인 몰은 길게.
MALL_INTERVAL_MIN = {
    "coupang": 10,      # 초당 10회 수준 — 여유
    "godomall": 10,     # RMS 실운영에서 검증된 수준
    "smartstore": 15,   # 초당 5회 + 토큰 발급 한도 — 보수적으로
    "kakao": 15,
    "toss": 15,         # TOO_MANY_REQUEST에 민감
    "st11": 20,         # 한도 비공개(코드 004) — 보수적으로
    "esm": 20,
    "lotteon": 20,
    "temu": 30,
}
DEFAULT_INTERVAL_MIN = 15     # 목록에 없는 몰(새 몰)의 기본값
MIN_INTERVAL_MIN = 5          # 너무 짧으면 몰이 호출을 차단한다
FAILURE_BACKOFF_MIN = 30      # 실패한 몰은 더 길게 쉰다 — 키가 틀렸는데 15분마다
                              # 두드리면 호출 예산만 낭비하고 차단 위험만 커진다
COLLECT_DAYS = 3              # 놓친 건이 있어도 따라잡도록 최근 며칠을 겹쳐 본다
TICK_SECONDS = 60


def interval_for(code):
    return MALL_INTERVAL_MIN.get(code, DEFAULT_INTERVAL_MIN)


def _due(sync, interval_min):
    """이 몰을 지금 수집할 때가 됐는가."""
    if not sync or not sync.get("at"):
        return True
    try:
        from datetime import datetime
        last = datetime.fromisoformat(sync["at"])
    except (TypeError, ValueError):
        return True
    return (config.now() - last) >= timedelta(minutes=max(MIN_INTERVAL_MIN, interval_min))


def collect_due_malls(app):
    """수집할 때가 된 몰을 한 바퀴 돈다. 돌아온 값은 몰별 결과(로그·테스트용)."""
    from .collect import _mall_settings, _save_sync       # 순환 import 방지

    with app.app_context():
        with tx() as conn:
            settings = _mall_settings(conn)
            row = conn.execute("SELECT value FROM settings WHERE key='mall_sync'").fetchone()
            import json as _json
            try:
                syncs = _json.loads(row["value"]) if row else {}
            except ValueError:
                syncs = {}

        results = []
        for code, s in (settings or {}).items():
            # 키가 있고 [이 몰 사용]이 켜져 있으면 수집한다 — 별도 스위치는 없다.
            if not isinstance(s, dict) or not s.get("enabled"):
                continue
            sync = syncs.get(code)
            interval = interval_for(code)
            if sync and not sync.get("ok"):
                # 실패한 몰은 백오프 — 키 문제라면 고칠 때까지 자주 두드릴 이유가 없다
                interval = max(interval, FAILURE_BACKOFF_MIN)
            if not _due(sync, interval):
                continue
            adapter, why = get_adapter(code, s)
            if adapter is None:
                continue                     # 키 미입력 등 — 화면의 '수집 가능' 표시로 이미 안내된다
            if cooldown_left(code):
                continue                     # 429 쿨다운 중 — 조용히 다음 틱을 기다린다
            results.append(_collect_one(app, code, adapter))
        return results


def _collect_one(app, code, adapter):
    """한 몰 수집 — 몰 호출은 트랜잭션 밖에서, 저장만 짧은 쓰기로."""
    from .collect import _save_sync

    until = config.now()
    since = until - timedelta(days=COLLECT_DAYS)
    try:
        # 수동 수집·연결 테스트와 겹치면 호출 예산만 두 배로 쓴다 — 한 몰은 한 번에 하나만
        with collect_guard(code, adapter.name):
            fetched = adapter.collect_orders(since, until)      # ← 트랜잭션 밖
    except MallError as e:
        with tx(write=True) as conn:
            _save_sync(conn, code, {"at": config.now_iso(), "ok": False, "error": str(e),
                                    "auto": True})
        app.logger.warning("자동수집 실패 | %s | %s", code, e)
        return {"mall": code, "ok": False, "error": str(e)}
    except Exception as e:                                       # noqa: BLE001
        with tx(write=True) as conn:
            _save_sync(conn, code, {"at": config.now_iso(), "ok": False,
                                    "error": f"{adapter.name} 수집 중 오류: {e}", "auto": True})
        app.logger.exception("자동수집 오류 | %s", code)
        return {"mall": code, "ok": False, "error": str(e)}

    import json as _json
    with tx(write=True) as conn:
        rows = conn.execute("SELECT * FROM orders").fetchall()
        existing = [row_to_import_dict(r) for r in rows]
        snapshot = {d["_rowId"]: _json.dumps(d, ensure_ascii=False, sort_keys=True, default=str)
                    for d in existing}
        added, updates = new_unique_orders(existing, fetched, now=config.now_iso())
        for o in added:
            insert_import_dict(conn, o, f"자동수집:{adapter.name}")
        changed = writeback_changed(conn, snapshot, existing)
        info = {"at": config.now_iso(), "ok": True, "fetched": len(fetched),
                "added": len(added), "shippingUpdates": updates, "auto": True}
        skipped = list(getattr(adapter, "skipped_rental", []) or [])
        if skipped:
            info["skippedRental"] = len(skipped)
            info["skippedSample"] = skipped[:5]
        _save_sync(conn, code, info)
        if added or updates:
            audit.log("orders_auto_collected", target=f"{adapter.name} {len(added)}건",
                      detail={"fetched": len(fetched), "added": len(added),
                              "shippingUpdates": updates})
    return {"mall": code, "ok": True, "fetched": len(fetched), "added": len(added),
            "updatedExisting": changed}


def start_collector(app):
    """자동수집 데몬 — 1분마다 '수집할 때가 된 몰'이 있는지만 본다."""
    def loop():
        while True:
            time.sleep(TICK_SECONDS)
            try:
                collect_due_malls(app)
            except Exception:                                    # noqa: BLE001
                app.logger.exception("자동수집 루프 오류")

    t = threading.Thread(target=loop, name="ows-mall-collector", daemon=True)
    t.start()
    return t
