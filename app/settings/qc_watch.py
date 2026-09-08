"""QC 프로그램 폴더 실시간 감시 — 셋팅/QC 단계를 자동으로 따라오게 한다.

대표 지시(2026-08-05): `\\\\127.0.0.1\\order-data` 를 보고
제작대기 / 제작완료 / 검수완료 체크가 실시간으로 반영되게 할 것.

★왜 파일을 보나
  QC 프로그램(구 order-workflow)이 그 폴더의 orders.json 에 작업 결과를 쓴다.
  OWS가 그 파일을 주기적으로 읽어 '더 진행된 단계'만 따라간다 —
  대표가 매번 손으로 올리던 것을 없앤 것뿐, 넣는 방식은 [데이터 이관]과 같다.

★안전 규칙 (설정 화면 이관과 동일)
  - **전진만** 반영한다(미완료 → 완료). 되돌리지 않는다.
    OWS에서도 같은 주문을 만지므로, 파일이 옛것이면 되돌리기가 일을 지운다.
  - 파일이 안 바뀌었으면(수정시각+크기 동일) 읽지도 않는다 — 네트워크 부담 0.
  - 폴더가 없거나 끊겨도 서버는 계속 돈다. 다음 주기에 다시 시도한다.
  - 계정(users.json)은 건드리지 않는다. 주문 단계만 본다.
"""
import json
import os
import threading
import time

from flask import jsonify, request

from .. import audit, config
from ..auth.perms import require, require_any
from ..db import tx
from . import bp
from .qc_import import _plan, _s
# ★qc_shipped 는 이 파일의 _cfg/_orders_file 을 쓰므로 위에서 import 하면 순환이 된다.
#   쓰는 자리(sync_once)에서 늦게 불러온다.

# 기본 감시 대상. 설정에서 바꿀 수 있다(설정 → 데이터 이관).
DEFAULT_PATH = r"\\127.0.0.1\order-data"
TICK_SECONDS = 20          # 실시간감을 주되 네트워크를 두드리지 않는 간격
SETTING_KEY = "qc_watch"

# ★2026-08-07 대표 지시로 QC 실시간 연동을 껐다.
#   "셋팅 및 QC는 OWS를 바로 관련 사람들이 사용할 예정이니까. 앞으로 OWS에서 데이터가 쌓일거야."
#
#   ★DB에 저장된 설정이 '켬'이어도 이 상수가 이긴다. 일부러 그렇게 했다 —
#     '꺼 달라'는 지시가 예전 설정값보다 위다. 저장값만 바꿔 두면 다음에 누가
#     설정 화면을 잘못 눌러 되살아난다.
#   되살리려면 환경변수 OWS_QC_WATCH=1.
QC_LIVE = os.getenv("OWS_QC_WATCH") == "1"

_last_seen = {"sig": None, "at": "", "applied": 0, "error": ""}


def _cfg(conn):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (SETTING_KEY,)).fetchone()
    try:
        d = json.loads(row["value"]) if row else {}
    except Exception:                                            # noqa: BLE001
        d = {}
    return {"enabled": QC_LIVE and bool(d.get("enabled", True)),
            "path": _s(d.get("path")) or DEFAULT_PATH}


def qc_live():
    """QC 프로그램을 실시간으로 따라가는 중인가. 껐으면 아무 데도 붙지 않는다."""
    return QC_LIVE


def _orders_file(base):
    """<폴더>/data/orders.json 또는 <폴더>/orders.json."""
    for cand in (os.path.join(base, "data", "orders.json"),
                 os.path.join(base, "orders.json")):
        if os.path.isfile(cand):
            return cand
    return ""


def _signature(path):
    """파일이 바뀌었는지 — 수정시각+크기. 내용을 읽지 않고 판단해 네트워크를 아낀다."""
    st = os.stat(path)
    return f"{int(st.st_mtime)}:{st.st_size}"


def sync_once(app, force=False):
    """한 번 훑고 '더 진행된 단계'만 반영한다. (읽은 건수, 갱신 건수) 반환."""
    with app.app_context():
        with tx() as conn:
            cfg = _cfg(conn)
    if not cfg["enabled"]:
        return 0, 0

    path = _orders_file(cfg["path"])
    if not path:
        # ★조용히 멈추면 안 된다. 예전에는 여기서 값만 담고 끝나서, 네트워크가 끊긴 줄
        #   아무도 모른 채 며칠이 갔다(2026-08-06 실측: 폴더 연결이 끊겨 하루 종일 0회 반영).
        #   상태가 바뀔 때만 한 번 로그에 남긴다 — 20초마다 같은 줄을 쌓지 않는다.
        msg = f"orders.json을 찾지 못했습니다: {cfg['path']}"
        if _last_seen.get("error") != msg:
            app.logger.warning("QC 폴더 연결 실패 — %s", msg)
        _last_seen["error"] = msg
        _last_seen["found"] = False
        return 0, 0
    if _last_seen.get("error") and _last_seen.get("found") is False:
        app.logger.info("QC 폴더 연결 복구 — %s", path)
    _last_seen["found"] = True

    sig = _signature(path)
    if not force and sig == _last_seen["sig"]:
        return 0, 0                       # 안 바뀌었으면 읽지도 않는다

    with open(path, encoding="utf-8") as fh:
        orders = json.load(fh)
    if not isinstance(orders, list):
        _last_seen["error"] = "orders.json 형식이 목록이 아닙니다."
        return 0, 0

    ts = config.now_iso()
    advanced = stages = 0
    with app.app_context():
        with tx(write=True) as conn:
            p = _plan(conn, orders, [])
            # ★새 주문은 만들지 않는다. 몰 수집·수기 등록이 주문을 만들고,
            #   이 감시는 '단계만' 따라간다 — 여기서 주문까지 만들면 어디서 생긴
            #   주문인지 알 수 없게 되고, 취소·보관 판단도 두 곳으로 갈린다.
            for key, _o, fwd in p["advOrders"]:
                cols = list(fwd.keys())
                conn.execute(
                    "UPDATE orders SET " + ", ".join(f"{c}=?" for c in cols) +
                    ", updated_at=? WHERE import_key=?",
                    [fwd[c] for c in cols] + [ts, key])
                advanced += 1
                stages += len(cols)
            if advanced:
                audit.log("qc_watch_sync", target=f"주문 {advanced}건 / 단계 {stages}개",
                          detail={"advanced": advanced, "stages": stages,
                                  "rows": len(orders), "path": path})
            # ★같은 주문이 우리 쪽에 두 줄이면 자동으로 목록에서 내린다
            #   (대표 2026-08-05: "매칭하여 중복으로 매출은 1건만 반영하되 목록에서 없애 달라").
            #   ★출고완료로 찍지 않는다 — 매출은 이미 반영된 그 한 건만 남는다.
            #   ★상대 줄이 이미 출고완료인, 의심의 여지가 없는 것만 손댄다.
            #   파일이 바뀐 주기에만 돈다(_signature) — 20초마다 백업 11개를 읽지 않는다.
            try:
                from .qc_shipped import _plan_shipped, archive_duplicates, merge_duplicates
                plan = _plan_shipped(conn, cfg["path"])
                dup_n, _ = archive_duplicates(conn, plan, actor="자동")
                if dup_n:
                    audit.log("qc_dup_archive_batch", target=f"{dup_n}건 자동으로 목록에서 내림",
                              detail={"archived": dup_n, "by": "자동"})
                # ★양쪽 다 출고완료라 매출이 두 번 잡힌 쌍도 자동으로 합친다
                #   (대표 2026-08-05: "매출은 1건만 반영하되 목록에서 없애 달라").
                #   금액이 있는 줄을 남기고 자산을 그 줄로 옮긴다.
                mg = merge_duplicates(conn, actor="자동")
                if mg["merged"]:
                    audit.log("qc_dup_merge_batch",
                              target=f"{mg['merged']}쌍 자동으로 합침",
                              detail={**mg, "by": "자동"})
            except Exception as e:                               # noqa: BLE001
                dup_n = 0            # 정리에 실패해도 단계 반영은 살린다
                app.logger.warning("중복 자동 정리 실패: %s", e)

    _last_seen.update({"sig": sig, "at": ts, "applied": advanced, "error": "",
                       "rows": len(orders), "newOrders": len(p["newOrders"]),
                       "dupArchived": dup_n})
    if advanced or dup_n:
        app.logger.info("QC 폴더 반영 — 주문 %s건 / 단계 %s개 / 중복정리 %s건 (%s)",
                        advanced, stages, dup_n, path)
    return len(orders), advanced


def start_qc_watch(app):
    """QC 폴더 감시 데몬."""
    def loop():
        time.sleep(20)          # 서버가 다 뜬 뒤에 시작
        while True:
            try:
                sync_once(app)
            except Exception as e:                               # noqa: BLE001
                # 네트워크가 잠깐 끊긴 것뿐일 수 있다 — 조용히 기록하고 다음 주기에 재시도
                _last_seen["error"] = str(e)[:200]
                app.logger.warning("QC 폴더 감시 실패: %s", e)
            time.sleep(TICK_SECONDS)

    t = threading.Thread(target=loop, name="ows-qc-watch", daemon=True)
    t.start()
    return t


@bp.get("/qc-watch/status")
def qc_watch_status():
    # 셋팅 담당자도 '연결이 끊겼는지'는 알아야 한다 — 모르고 종일 손으로 체크하게 된다
    require_any("settings.manage", "setup.view")
    with tx() as conn:
        cfg = _cfg(conn)
    path = _orders_file(cfg["path"])
    return jsonify({
        "enabled": cfg["enabled"], "folder": cfg["path"],
        # ★2026-08-07 대표 지시로 아예 꺼 둔 상태인지 — 화면이 '고장'과 구분해 보여 준다
        "live": QC_LIVE,
        "file": path, "found": bool(path),
        "intervalSeconds": TICK_SECONDS,
        "lastAt": _last_seen.get("at", ""),
        "lastApplied": _last_seen.get("applied", 0),
        "rows": _last_seen.get("rows", 0),
        "newOrders": _last_seen.get("newOrders", 0),
        "dupArchived": _last_seen.get("dupArchived", 0),
        "error": _last_seen.get("error", ""),
    })


@bp.post("/qc-watch")
def qc_watch_save():
    """감시 폴더·사용 여부 저장."""
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    cfg = {"enabled": bool(body.get("enabled", True)),
           "path": _s(body.get("path")) or DEFAULT_PATH}
    with tx(write=True) as conn:
        conn.execute(
            "INSERT INTO settings(key, value, updated_at, updated_by) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (SETTING_KEY, json.dumps(cfg, ensure_ascii=False), config.now_iso(),
             __import__("flask").g.user["display_name"]))
        audit.log("qc_watch_config", detail=cfg)
    _last_seen["sig"] = None          # 설정이 바뀌었으니 다음 주기에 다시 읽는다
    return jsonify({"ok": True, **cfg})


@bp.post("/qc-watch/run")
def qc_watch_run():
    """지금 바로 한 번 — 기다리지 않고 손으로 돌릴 때."""
    require("settings.manage")
    from flask import current_app
    rows, advanced = sync_once(current_app._get_current_object(), force=True)
    return jsonify({"ok": True, "rows": rows, "advanced": advanced,
                    "error": _last_seen.get("error", "")})
