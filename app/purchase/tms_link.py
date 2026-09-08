"""TMS 데이터 직접 반영 — 연동 창구(data-bridge)에서 행 단위로 받아 기존 반영 규칙에 태운다.

대표 결정(2026-09-02): 무결성이 우선 — 엑셀을 거치지 않는다.

    data-bridge(D:\\data-bridge) : TMS를 SELECT만으로 30초마다 읽어 원본 구조 그대로 사본을 유지하고,
                                   HTTP 창구로 화면형 행(JSON)·삭제 보존 행·복구 이벤트를 준다.
    이 파일                        : 2분마다 창구에서 '지난 틱 이후 바뀐 행'만 받아 autosync와 같은 반영 함수
                                   (_prepare/apply_rows, upsert_asset_sales, apply_sale_slips)에 넣는다.

  규칙은 엑셀 경로와 같다 — 빈 칸 채우기·3방향 정정 감지·정방향 상태 추종·복귀 후보.
  엑셀 경로와 다른 점: 행 신원(키ID)과 변경 시각 기반 증분, TMS 삭제·복구가 자산에 표시된다.

★안전 규칙
  - 창구 사본이 낡았으면(stale) 아무것도 반영하지 않는다 — 낡은 값으로 덮지 않는다.
  - 삭제는 지우지 않는다. assets.tms_deleted_at 표시 + 이력만. TMS에서 복구되면 표시를 지운다.
  - 실패해도 서버는 계속 돈다. 커서는 성공한 틱에서만 전진하고 3분을 겹쳐 다시 본다(반영은 멱등).
  - 설정(창구 주소·토큰)이 없으면 틱마다 조용히 건너뛴다 — 엑셀 자동 반영은 그대로 돈다.
"""
import datetime as _dt
import json
import os
import threading
import time

import requests
from flask import abort, current_app, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import tx
from . import bp

TICK_SECONDS = 120
OVERLAP_SECONDS = 180
# 반영 순서 = autosync ORDER_HINT 와 같다(매입현황이 전표·거래처를 먼저 세운다)
SCREEN_ORDER = ("매입현황", "재고내역", "판매현황", "재고현황", "판매내역", "판매미수금관리")
STATE_FILE = config.DATA_DIR / "tms_link_state.json"
_last = {}                       # 마지막 실행 요약(메모리) — 상태 화면용


# ---------------------------------------------------------------- 설정·상태
BLOCKED_MSG = "OWS_NO_TMS_SYNC=1 — 이 서버는 연동 창구에 접속하지 않습니다(검증용)"
CONFIG_FILE = config.DATA_DIR / "datalink.json"     # 배포용: {"url","token","enabled"} — 서버 재시작 없이 다음 틱부터 적용


def load_config():
    """우선순위: 환경변수 OWS_DATALINK_URL/TOKEN > data/datalink.json > settings['datalink'] {url, token, enabled}."""
    cfg = {"url": "", "token": "", "enabled": True}
    try:
        with tx() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='datalink'").fetchone()
        if row:
            cfg.update(json.loads(row["value"]) or {})
    except Exception:                                            # noqa: BLE001
        pass
    try:
        if CONFIG_FILE.exists():
            cfg.update(json.loads(CONFIG_FILE.read_text("utf-8")) or {})
    except (OSError, ValueError):
        pass
    cfg["url"] = (os.getenv("OWS_DATALINK_URL") or cfg.get("url") or "").rstrip("/")
    cfg["token"] = os.getenv("OWS_DATALINK_TOKEN") or cfg.get("token") or ""
    cfg["enabled"] = bool(cfg.get("enabled", True)) and bool(cfg["url"]) and bool(cfg["token"])
    # ★OWS_NO_TMS_SYNC=1 이면 창구에 접속하지 않는다(2026-09-08). enabled 는 그대로 두고
    #   '막혔다'만 표시한다 — 설정이 없는 것과 막아 둔 것은 원인이 달라 화면 문구도 달라야 한다.
    cfg["blocked"] = BLOCKED_MSG if os.getenv("OWS_NO_TMS_SYNC") == "1" else ""
    return cfg


def _read_state():
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, STATE_FILE)


def _minus_seconds(ts, sec):
    """창구 시각('YYYY-MM-DD HH:MM:SS')에서 sec 초 앞 — 겹쳐서 다시 보기 위해."""
    try:
        t = _dt.datetime.fromisoformat(str(ts)[:19])
    except ValueError:
        return ts
    return (t - _dt.timedelta(seconds=sec)).strftime("%Y-%m-%d %H:%M:%S")


class Client:
    """연동 창구 HTTP 클라이언트(GET 전용)."""

    def __init__(self, url, token):
        # ★검증 서버·시험이 운영 폴더의 data/datalink.json 을 읽어 **살아 있는 창구**에 붙던 길을
        #   여기서 끊는다(2026-09-08). 예전엔 OWS_NO_TMS_SYNC 가 '데몬을 안 띄운다'만 뜻해서,
        #   화면 라우트를 한 번 부르면 그대로 접속됐다. 되돌려 쓰기가 생긴 뒤로는 그 길이
        #   TMS 쓰기까지 닿는다. 시험이 쓰는 가짜 Client 는 이 클래스가 아니라 영향이 없다.
        if os.getenv("OWS_NO_TMS_SYNC") == "1":
            raise RuntimeError(BLOCKED_MSG)
        self.url, self.token = url, token

    def get(self, path, timeout=180, **params):
        # timeout: 화면 안에서 부르는 짧은 조회(numbering.py 관리번호 검증)는 창구 장애에 3분씩 매달리면 안 된다
        r = requests.get(self.url + path, params={k: v for k, v in params.items() if v is not None},
                         headers={"X-Datalink-Token": self.token}, timeout=timeout)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400 or not data.get("ok", True):
            raise RuntimeError(f"연동 창구 오류 {path}: {data.get('error') or r.status_code}")
        return data

    def post(self, path, body, timeout=60):
        """창구에 쓰기를 맡긴다(2026-09-08) — 지금은 /tms-write 하나뿐이다.

        ★거부(400)와 충돌(409)은 '오류'가 아니라 답이다 — 큐가 다르게 처리해야 해서
          따로 구분해 올린다(무장 전이면 재시도 횟수를 올리면 안 되고, 충돌이면 다시 읽어야 한다).
        """
        from .tms_push import TmsWriteConflict, TmsWriteRefused
        r = requests.post(self.url + path, json=body,
                          headers={"X-Datalink-Token": self.token}, timeout=timeout)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 409 or data.get("conflict"):
            raise TmsWriteConflict(data.get("error") or "TMS 쪽이 먼저 바뀌었습니다")
        if r.status_code == 400:
            raise TmsWriteRefused(data.get("error") or "창구가 거부했습니다")
        if r.status_code >= 400 or not data.get("ok", True):
            raise RuntimeError(f"연동 창구 오류 {path}: {data.get('error') or r.status_code}")
        return data


# ---------------------------------------------------------------- 반영
def _log_sync(conn, name, tag, rows, res, note, errors=0):
    conn.execute(
        "INSERT INTO tms_sync_log(filename, file_hash, size, rows, created, updated, errors, synced_at, note) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (f"연동:{name}", f"datalink:{name}:{tag}", 0, rows, res.get("created", 0), res.get("updated", 0),
         errors, config.now_iso(), note))


def apply_screen(app, name, rows, tag):
    """화면 하나의 행들을 autosync._apply_one 과 같은 규칙으로 반영한다(엑셀 대신 JSON 행)."""
    from .autosync import _is_asset_sheet
    from .migration import _prepare, apply_rows
    from .sales import apply_sale_slips, is_sale_asset_sheet, is_sale_slip_sheet, upsert_asset_sales

    if not rows:
        return {"created": 0, "updated": 0}
    with tx(write=True) as conn:
        if is_sale_slip_sheet(rows):
            res = apply_sale_slips(conn, rows, actor="연동")
            _log_sync(conn, name, tag, len(rows), res, "판매 전표")
            audit.log("tms_link_sales", target=name, detail={"rows": len(rows), **res})
            return res
        if not _is_asset_sheet(rows, "datalink"):
            _log_sync(conn, name, tag, len(rows), {}, "자산 표가 아니라 건너뜀")
            return {"created": 0, "updated": 0, "skipped": True}
        ready, dup, errors, updates, locked = _prepare(conn, rows, fill_blanks=True)
        res = apply_rows(conn, ready, updates, actor="연동")
        bits = []
        if is_sale_asset_sheet(rows):
            sres = upsert_asset_sales(conn, rows, actor="연동")
            if sres["created"] or sres["updated"]:
                bits.append("판매명세 신규 %d/갱신 %d" % (sres["created"], sres["updated"]))
        if res.get("statusUpdated"):
            bits.append("상태갱신 %d건" % res["statusUpdated"])
        if res.get("corrected"):
            bits.append("TMS정정 %d건" % res["corrected"])
        if res.get("correctionsFrozen"):
            bits.append("★정정 %d건 폭주 — 반영 보류" % res["correctionsFrozen"])
        if res.get("statusReverts"):
            alerts = res.get("statusAlerts") or []
            bits.append("재고복귀 후보 %d건(자동 반영 안 함): %s" % (
                res["statusReverts"], " / ".join(alerts[:5])))
        if locked:
            bits.append("정정잠금 %d건(TMS 값 반영 안 함)" % len(locked))
        bits.extend(errors[:3])
        _log_sync(conn, name, tag, len(rows), res, "; ".join(bits), len(errors))
        audit.log("tms_link", target=name,
                  detail={"rows": len(rows), **{k: v for k, v in res.items() if k != "statusAlerts"},
                          "errors": len(errors), "locked": len(locked)})
    app.logger.info("TMS 연동 반영 %s — 행 %d / 신규 %s / 채움 %s / 상태갱신 %s / 오류 %s",
                    name, len(rows), res["created"], res["updated"], res.get("statusUpdated", 0), len(errors))
    return res


def apply_deletions(conn, items):
    """TMS에서 삭제된 재고 행 → 자산에 표시만(tms_deleted_at) + 이력. 지우지 않는다."""
    from . import asset_event
    n = 0
    ts = config.now_iso()
    for it in items:
        no = str(it.get("관리번호") or "").strip()
        if not no:
            continue
        row = conn.execute("SELECT id, tms_deleted_at FROM assets WHERE asset_no=?", (no,)).fetchone()
        if row is None or row["tms_deleted_at"]:
            continue
        note = "TMS에서 삭제됨(%s) — 사본 키ID %s, 사본에 원본 보존" % (str(it.get("_deleted_at") or "")[:19], it.get("키ID"))
        conn.execute("UPDATE assets SET tms_deleted_at=?, tms_deleted_note=?, updated_at=? WHERE id=?",
                     (ts, note, ts, row["id"]))
        asset_event(conn, row["id"], "TMS삭제",
                    {"키ID": it.get("키ID"), "deletedAt": it.get("_deleted_at"), "restoredTo": it.get("_restored_to")})
        n += 1
    return n


def apply_restores(conn, events):
    """TMS 복구(undo) 이벤트 → 삭제 표시를 지운다. 복구된 행 자체는 화면 행 반영으로 다시 들어온다."""
    from . import asset_event
    n = 0
    ts = config.now_iso()
    for e in events:
        d = e.get("detail") or {}
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except ValueError:
                d = {}
        if d.get("변경구분") != "재고":
            continue
        no = str(e.get("mgmt_no") or d.get("관리번호") or "").strip()
        if not no:
            continue
        row = conn.execute("SELECT id, tms_deleted_at FROM assets WHERE asset_no=?", (no,)).fetchone()
        if row is None or not row["tms_deleted_at"]:
            continue
        conn.execute("UPDATE assets SET tms_deleted_at='', tms_deleted_note='', updated_at=? WHERE id=?",
                     (ts, row["id"]))
        asset_event(conn, row["id"], "TMS복구", d)
        n += 1
    return n


def push_outbox(app, cli, limit=30):
    """OWS 큐(tms_outbox)를 창구로 밀어 넣는다 — 한 건씩 짧은 쓰기 트랜잭션으로.

    ★창구 호출(HTTP)을 DB 잠금 안에서 하면 그동안 전 시스템의 쓰기가 멈춘다(원칙 #1).
      그래서 '보낼 것 읽기'와 '결과 쓰기'만 트랜잭션이고 호출은 그 사이 밖에서 한다.
    """
    from ..db import get_db, tx
    from . import tms_push
    out = {"sent": 0, "applied": 0, "conflict": 0, "failed": 0, "skipped": 0}
    rows = tms_push.pending(get_db(), limit)
    if not rows:
        return out
    for r in rows:
        row = dict(r)
        try:
            payload = json.loads(row["payload"] or "{}") or {}
        except (ValueError, TypeError):
            with tx(write=True) as c:
                tms_push.mark(c, row["id"], "failed", error="보낼 값을 읽지 못했습니다")
            out["failed"] += 1
            continue
        try:                                                     # ← 트랜잭션 밖
            res = cli.post("/tms-write", {
                "kind": row["kind"], "keyId": row["tms_key_id"], "values": payload,
                "actor": row["created_by"] or "OWS"})
        except tms_push.TmsWriteConflict as e:
            with tx(write=True) as c:
                tms_push.mark(c, row["id"], "conflict", error=str(e))
            out["conflict"] += 1
            continue
        except tms_push.TmsWriteRefused as e:
            # 무장 전·규칙 위반 — 다시 시도해도 같으니 시도 횟수를 올리지 않고 큐에 그대로 둔다
            with tx(write=True) as c:
                c.execute("UPDATE tms_outbox SET last_error=?, updated_at=? WHERE id=?",
                          (str(e)[:300], config.now_iso(), row["id"]))
            out["skipped"] += 1
            continue
        except Exception as e:                                   # noqa: BLE001
            with tx(write=True) as c:
                tms_push.mark(c, row["id"], "failed", error=str(e))
            out["failed"] += 1
            continue
        out["sent"] += 1
        with tx(write=True) as c:
            tms_push.mark(c, row["id"], "applied", action_id=res.get("actionId") or "",
                          before=res.get("before"), after=res.get("after"),
                          error="" if res.get("applied") else (res.get("reason") or ""))
        out["applied"] += 1
        if res.get("applied"):
            audit.log("tms_push_applied", target=f"{row['asset_no']} {row['kind']}",
                      detail={"values": payload, "actionId": res.get("actionId"),
                              "before": res.get("before")})
    return out


def sync_once(app, full=False):
    """창구에서 변경분을 받아 반영한다. full=True 면 전량(첫 가동·재대사)."""
    with app.app_context():
        cfg = load_config()
        if not cfg["enabled"]:
            _last.clear()
            _last.update(at=config.now_iso(), skipped="설정 없음(OWS_DATALINK_URL/TOKEN 또는 settings.datalink)")
            return {"skipped": "disabled"}
        cli = Client(cfg["url"], cfg["token"])
        fresh = (cli.get("/status")["status"].get("freshness") or {})
        if fresh.get("stale"):
            _last.clear()
            _last.update(at=config.now_iso(), skipped="연동 사본이 낡음 — 반영 보류", freshness=fresh)
            app.logger.warning("TMS 연동 반영 보류: 사본이 낡음 %s", fresh)
            return {"skipped": "stale"}
        state = _read_state()
        since = None if (full or not state.get("cursor")) else _minus_seconds(state["cursor"], OVERLAP_SECONDS)
        tag = _dt.datetime.now().strftime("%Y%m%d%H%M%S%f")      # tms_sync_log.file_hash 는 UNIQUE — 틱마다 다르게
        summary = {"at": tag, "since": since, "screens": {}, "deleted": 0, "restored": 0, "freshness": fresh}
        server_time = None
        for name in SCREEN_ORDER:
            data = cli.get("/screens", names=name, since=since)
            server_time = server_time or data["server_time"]
            rows = data["screens"][name]["items"]
            res = apply_screen(app, name, rows, tag) if rows else {"created": 0, "updated": 0}
            summary["screens"][name] = {"rows": len(rows),
                                        **{k: v for k, v in res.items() if k != "statusAlerts"}}
        # ★판매 전표 헤더 1회 재대사(2026-09-03) — 안 고친 연동 전표가 TMS 값을 따라가도록 규칙을 바꾼 뒤(sales.py),
        #   그 전에 굳어 있던 전표(2026-08-12~09-03 헤더 수량·금액 불일치 16건 + 입금확인 0으로 굳은 2건)를
        #   두 화면 전량(각 ~440행)으로 한 번 다시 받아 맞춘다. 마커는 상태 파일에 — 이 틱이 끝까지 성공해야
        #   저장되므로 중간에 실패하면 다음 틱에 다시 한다(반영은 멱등).
        if since is not None and not state.get("sale_head_resync"):
            summary["saleHeadResync"] = {}
            for name in ("판매내역", "판매미수금관리"):
                rows = cli.get("/screens", names=name, since=None)["screens"][name]["items"]
                res = apply_screen(app, name, rows, tag + "-resync") if rows else {"created": 0, "updated": 0}
                summary["saleHeadResync"][name] = {"rows": len(rows), "updated": res.get("updated", 0)}
            state["sale_head_resync"] = tag
            app.logger.info("판매 전표 헤더 재대사(1회): %s", summary["saleHeadResync"])
        elif since is None:
            state["sale_head_resync"] = state.get("sale_head_resync") or tag      # 전량 틱이면 이미 맞았다
        # ★TMS 수리비·부품비·재고비고 1회 채우기(2026-09-03, migration.sync_tms_costs) — 창구 재고내역이 그 칸을 싣기 시작한 뒤
        #   '바뀐 행'만 오는 증분 틱으로는 이미 값이 있는 자산 2,572대가 영영 안 채워진다. 첫 증분 틱에 재고내역 전량을 한 번
        #   더 받아 맞춘다(반영은 멱등·사람 기록 무접촉). 마커는 상태 파일에 — 틱이 끝까지 성공해야 저장된다.
        if since is not None and not state.get("cost_backfill"):
            rows = cli.get("/screens", names="재고내역", since=None)["screens"]["재고내역"]["items"]
            if rows and "수리비" in rows[0]:
                res = apply_screen(app, "재고내역", rows, tag + "-costs")
                summary["costBackfill"] = {"rows": len(rows), "costsSynced": res.get("costsSynced", 0), "updated": res.get("updated", 0)}
                state["cost_backfill"] = tag
                app.logger.info("TMS 수리비·부품비 1회 채우기: %s", summary["costBackfill"])
            else:
                summary["costBackfill"] = {"skipped": "창구 재고내역에 수리비 칸 없음"}
        elif since is None:
            state["cost_backfill"] = state.get("cost_backfill") or tag      # 전량 틱이면 이미 맞았다
        # ★판매 시점 원가 스냅샷 1회 채우기(2026-09-03 대표 승인). 창구 판매현황이 제조원가·수리비·
        #   부품비·매입부가세·실부가세를 싣기 시작했지만, 증분 틱은 '바뀐 행'만 받으므로 이미 팔린
        #   행(약 1만 3천)은 영영 안 채워진다. 첫 증분 틱에 판매현황 전량을 한 번 더 받아 **판매 명세만**
        #   맞춘다(upsert_asset_sales 직접 호출 — 자산 빈칸 채우기·상태 갱신은 태우지 않는다.
        #   그쪽은 재고내역이 이미 맡고 있고, 1만 3천 행에 다시 태우면 자산 이력이 불필요하게 요동친다).
        #   반영은 멱등이고 OWS에서 고친 명세(ows_edited_at)는 값이 보호된다.
        if since is not None and not state.get("sale_cost_backfill"):
            rows = cli.get("/screens", names="판매현황", since=None)["screens"]["판매현황"]["items"]
            if rows and "제조원가" in rows[0]:
                from .sales import upsert_asset_sales
                with tx(write=True) as conn:
                    sres = upsert_asset_sales(conn, rows, actor="원가채우기")
                    _log_sync(conn, "판매현황", tag + "-salecost", len(rows), sres, "판매 시점 원가 1회 채우기")
                summary["saleCostBackfill"] = {"rows": len(rows), **sres}
                state["sale_cost_backfill"] = tag
                app.logger.info("판매 시점 원가 1회 채우기: %s", summary["saleCostBackfill"])
            else:
                summary["saleCostBackfill"] = {"skipped": "창구 판매현황에 제조원가 칸 없음"}
        elif since is None:
            state["sale_cost_backfill"] = state.get("sale_cost_backfill") or tag
        del_since = _minus_seconds(state.get("deleted_since") or server_time, OVERLAP_SECONDS) \
            if state.get("deleted_since") else None
        dele = cli.get("/deleted", table="HB_TBL재고", since=del_since or "2000-01-01", limit=2000)
        rest = cli.get("/restores", after=int(state.get("restore_after") or 0), limit=1000)
        with tx(write=True) as conn:
            summary["deleted"] = apply_deletions(conn, dele.get("items") or [])
            summary["restored"] = apply_restores(conn, rest.get("items") or [])
            if summary["deleted"] or summary["restored"]:
                audit.log("tms_link_delete_restore",
                          detail={"deleted": summary["deleted"], "restored": summary["restored"]})
        # 공용 마스터(모델·거래처) — 화면 반영 뒤. 실패해도 이 틱을 깨지 않는다(masters.py 안에서 처리, 커서는 state 에 함께 저장)
        from .masters import sync_masters
        summary["masters"] = sync_masters(app, cli, state, full=full)
        # 이중입력 경보(2026-09-03, cutover.py) — 마감일 뒤 TMS 판매H·매입H·재고 '추가'를 변경 피드에서 골라 둔다.
        #   마감일이 없으면 커서만 유지. 실패해도 이 틱을 깨지 않는다(커서 dual_after 는 state 에 함께 저장).
        try:
            from .cutover import scan as _cutover_scan
            summary["cutover"] = _cutover_scan(app, cli, state)
        except Exception as e:                                   # noqa: BLE001
            summary["cutover"] = {"error": str(e)[:200]}
            app.logger.warning("이중입력 경보 점검 실패: %s", e)
        # ★OWS → TMS 되돌려 쓰기(2026-09-08 대표) — 받아 온 것을 다 반영한 **뒤에** 민다.
        #   순서가 중요하다: 먼저 밀면 방금 민 값이 같은 틱의 화면 반영에서 되돌아온다.
        #   실패해도 이 틱을 깨지 않는다(큐에 남아 다음 틱에 다시 간다).
        try:
            summary["push"] = push_outbox(app, cli)
        except Exception as e:                                   # noqa: BLE001
            summary["push"] = {"error": str(e)[:200]}
            app.logger.warning("TMS 되돌려 쓰기 실패: %s", e)
        state.update(cursor=server_time, deleted_since=server_time, last_ok=tag,
                     restore_after=max([int(e.get("id") or 0) for e in (rest.get("items") or [])]
                                       + [int(state.get("restore_after") or 0)]))
        _write_state(state)
        _last.clear()
        _last.update(summary)
        app.logger.info("TMS 연동 틱: %s", {k: v.get("rows") for k, v in summary["screens"].items()})
        return summary


def start_tms_link(app):
    """연동 창구 직접 반영 데몬 — 설정이 없으면 틱마다 건너뛴다."""
    def loop():
        time.sleep(45)
        while True:
            try:
                sync_once(app)
            except Exception as e:                               # noqa: BLE001
                app.logger.exception("TMS 연동 반영 루프 오류")
                _last.update(error=str(e)[:200], errorAt=config.now_iso())
            time.sleep(TICK_SECONDS)

    t = threading.Thread(target=loop, name="ows-tms-link", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------- 화면용 API
@bp.get("/tms-link/status")
def tms_link_status():
    require("purchase.view")
    cfg = load_config()
    link = None
    if cfg["enabled"]:
        try:
            link = Client(cfg["url"], cfg["token"]).get("/status")["status"].get("freshness")
        except Exception as e:                                   # noqa: BLE001
            link = {"error": str(e)[:200]}
    return jsonify({"config": {"url": cfg["url"], "enabled": cfg["enabled"], "tokenSet": bool(cfg["token"])},
                    "intervalSeconds": TICK_SECONDS, "state": _read_state(), "last": _last, "datalink": link})


@bp.post("/tms-link/run")
def tms_link_run():
    require("purchase.edit")
    full = (request.args.get("full") or "").strip() == "1"
    return jsonify({"ok": True, "result": sync_once(current_app._get_current_object(), full=full)})


@bp.post("/tms-link/config")
def tms_link_config():
    """창구 주소·토큰 저장(settings.datalink). 환경변수가 있으면 그쪽이 우선한다."""
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    val = {"url": str(body.get("url") or "").strip(), "token": str(body.get("token") or "").strip(),
           "enabled": bool(body.get("enabled", True))}
    with tx(write=True) as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key, value, updated_at, updated_by) VALUES('datalink',?,?,?)",
                     (json.dumps(val, ensure_ascii=False), config.now_iso(), ""))
        audit.log("tms_link_config", detail={"url": val["url"], "enabled": val["enabled"], "tokenSet": bool(val["token"])})
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 되돌려 쓰기 화면 API
@bp.get("/tms-outbox")
def tms_outbox_list():
    """설정 ▸ 데이터 이관 카드 — 밀린 것·보낸 것과 창구 무장 상태."""
    require("purchase.view")
    from ..db import get_db
    from .tms_push import STATUS_LABEL, _item, counts
    show_all = (request.args.get("all") or "") == "1"
    conn = get_db()
    sql = "SELECT o.*, a.model FROM tms_outbox o LEFT JOIN assets a ON a.id=o.asset_id"
    if not show_all:
        sql += " WHERE o.status IN ('queued','conflict','failed')"
    rows = conn.execute(sql + " ORDER BY o.id DESC LIMIT 200").fetchall()
    cfg = load_config()
    # 창구가 살아 있으면 무장 상태를 물어본다 — 죽어 있어도 화면은 떠야 한다
    gate = {"reachable": False, "armed": None, "error": cfg.get("blocked") or ""}
    if cfg["enabled"] and not cfg.get("blocked"):
        try:
            g_ = Client(cfg["url"], cfg["token"]).get("/tms-write", timeout=8)
            gate = {"reachable": True, "armed": bool(g_.get("armed")), "error": "",
                    "kinds": g_.get("kinds") or {}}
        except Exception as e:                                   # noqa: BLE001
            gate["error"] = str(e)[:200]
    return jsonify({"counts": counts(conn), "items": [_item(r) for r in rows],
                    "statusLabels": STATUS_LABEL, "linkEnabled": cfg["enabled"], "gate": gate,
                    "last": _last.get("push") or {}})


@bp.post("/tms-outbox/<int:rid>/<action>")
def tms_outbox_action(rid, action):
    """한 줄을 다시 보내거나(retry) 접는다(cancel).

    ★취소는 '안 보내기로 한다'는 뜻이지 TMS를 되돌리는 게 아니다 — 이미 반영된 값은 그대로다
      (되돌리려면 before 값을 보고 사람이 판단한다).
    """
    require("purchase.edit")
    if action not in ("retry", "cancel"):
        abort(404)
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM tms_outbox WHERE id=?", (rid,)).fetchone()
        if row is None:
            abort(404, description="큐에서 찾을 수 없습니다.")
        if row["status"] == "applied":
            abort(400, description="이미 TMS에 반영된 줄입니다.")
        ts = config.now_iso()
        if action == "retry":
            conn.execute("UPDATE tms_outbox SET status='queued', attempts=0, last_error='', "
                         "updated_at=? WHERE id=?", (ts, rid))
        else:
            conn.execute("UPDATE tms_outbox SET status='cancelled', updated_at=? WHERE id=?", (ts, rid))
        audit.log("tms_outbox_" + action, target=f"{row['asset_no']} #{rid}",
                  detail={"payload": row["payload"], "was": row["status"]})
    return jsonify({"ok": True})


@bp.post("/tms-outbox/push")
def tms_outbox_push_now():
    """[지금 보내기] — 2분 틱을 기다리지 않고 큐를 민다."""
    require("purchase.edit")
    cfg = load_config()
    if cfg.get("blocked"):
        abort(400, description=cfg["blocked"])
    if not cfg["enabled"]:
        abort(400, description="연동 창구 설정이 없습니다(설정 ▸ 데이터 이관).")
    out = push_outbox(current_app, Client(cfg["url"], cfg["token"]))
    _last["push"] = out
    return jsonify({"ok": True, **out})
