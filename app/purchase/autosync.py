"""TMS 엑셀 자동 반영 — 폴더에 새 파일이 들어오면 알아서 최신화한다.

대표 결정(2026-07-30): TMS와 OWS를 함께 쓰되, OWS에 입력을 안 하는 경우가 있으니
주기적으로 TMS 데이터를 긁어와 최신화한다.

★내보내기도 이제 자동이다 (2026-08-07)
  예전엔 대표가 하루 1회 TMS에서 엑셀을 내려받아 이 폴더에 넣었다. 지금은 185에서
  tools/tms_export 가 2시간마다 그 일을 대신한다. 이 파일이 하는 일은 그대로다 —
  폴더에 새 파일이 들어오면 반영한다. 손으로 넣어도 똑같이 동작한다.

    tools/tms_export : TMS 화면을 열고 엑셀 내보내기만 누른다 (읽기 전용)
    이 파일          : 폴더를 지켜보다가 새 파일이 들어오면 자동으로 반영

  ★내보내기 쪽이 죽어도 여기는 상관없다. 반대도 마찬가지다. 일부러 갈라 놨다.

★안전 규칙
  - '빈 칸 채우기'로만 반영한다. 사람이 넣은 값은 절대 덮어쓰지 않는다.
  - 같은 파일을 두 번 반영하지 않는다(내용 해시로 판별) — 손대지 않은 파일은 그냥 넘긴다.
  - 반영 순서를 지킨다: 매입현황이 전표번호를 가진 유일한 파일이라 이걸 먼저 넣어야
    전표·거래처 구조가 선다.
  - 실패해도 서버는 계속 돈다. 다음 주기에 다시 시도한다.
"""
import hashlib
import threading
import time
from pathlib import Path

from flask import jsonify

from .. import audit, config
from ..auth.perms import require
from ..db import tx
from . import bp

# 폴더를 얼마나 자주 들여다볼지. 파일이 그대로면 아무 일도 안 하므로 짧아도 부담이 없다.
TICK_SECONDS = 600            # 10분
EXPORT_DIR = config.ROOT / "tms-export"

# 파일별 반영 실패 기록 — 대시보드 경보용(파일명 → {error, at, count}).
# 성공하면 지워진다. 서버 메모리라 재시작하면 비지만, 실패가 계속이면 곧 다시 찬다.
_failures = {}

# ★반영 순서. 매입현황에만 매입전표 번호가 있어서 이걸 먼저 넣어야
#   전표와 거래처가 만들어지고, 나머지 파일이 그 위에 빈 칸을 채운다.
ORDER_HINT = ("매입현황", "재고내역", "판매현황", "재고현황")


def _rank(name):
    for i, key in enumerate(ORDER_HINT):
        if key in name:
            return i
    return len(ORDER_HINT)


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _done_hashes(conn):
    return {r["file_hash"] for r in conn.execute("SELECT file_hash FROM tms_sync_log")}


def pending_files(conn):
    """아직 반영하지 않은 엑셀 목록 — 반영 순서대로 돌려준다."""
    if not EXPORT_DIR.exists():
        return []
    done = _done_hashes(conn)
    out = []
    for p in sorted(EXPORT_DIR.glob("*.xlsx")):
        if p.name.startswith("~$"):          # 엑셀이 열어 둔 임시 파일
            continue
        try:
            digest = _digest(p)
        except OSError:
            continue                          # 복사 중일 수 있다 — 다음 주기에 다시 본다
        if digest in done:
            continue
        out.append({"path": p, "name": p.name, "hash": digest, "size": p.stat().st_size})
    out.sort(key=lambda x: (_rank(x["name"]), x["name"]))
    return out


# ★OWS가 스스로 뽑은 엑셀은 절대 되먹이지 않는다.
#   주문 내려받기(app/orders/__init__.py:551)에 '자산번호' 칸이 있어서, 그 파일을
#   tms-export 폴더에 잘못 떨구면 자산 표로 오인돼 없는 자산이 자동 등록된다.
#   (2026-07-31 ows-orders-20260731.xlsx 가 실제로 이 폴더를 통과했다. 그날은 자산번호가
#    채워진 줄이 1개뿐이라 우연히 아무것도 안 만들어졌을 뿐이다.)
OUR_EXPORT_PREFIXES = ("ows-", "ows_")
# 주문 표에만 있는 칸 — 하나라도 있으면 자산 표가 아니다
ORDER_ONLY_HEADERS = {"주문번호", "수취인", "쇼핑몰", "배송메시지", "송장번호", "작업단계", "주문상태"}
# 자산 표라면 관리번호 말고도 이 중 하나는 반드시 있다
ASSET_COMPANION = {"model", "serial", "supplier", "purchase_date", "purchase_price",
                   "grade", "tms_status", "category", "maker"}


def _is_asset_sheet(rows, filename=""):
    """자산 표인가? 관리번호 칸 '하나만' 보고 판단하면 안 된다.

    ★tms-export 폴더에는 자산 표만 있는 게 아니다. 매입내역·판매내역은 '전표' 표이고
      미수금·거래처·통계는 아예 다른 표다. 이걸 자산으로 넣으면 관리번호가 없어
      전부 오류가 나거나, 더 나쁘게는 엉뚱한 값이 자산으로 들어간다.

    ★게다가 우리 주문 엑셀에도 '자산번호' 칸이 있다. 관리번호 칸만 보면 그것까지
      자산 표로 통과시킨다 — 사람 승인 없이 가짜 자산이 생긴다. 그래서 세 겹으로 막는다.
    """
    if not rows:
        return False
    name = (filename or "").lower()
    if name.startswith(OUR_EXPORT_PREFIXES):
        return False                                   # ① 우리가 뽑은 파일은 되먹이지 않는다
    from .migration import HEADER_MAP, _norm_header
    keys = set(rows[0].keys())
    if any(str(k).strip() in ORDER_ONLY_HEADERS for k in keys):
        return False                                   # ② 주문 표 고유 칸이 보이면 자산이 아니다
    mapped = {HEADER_MAP.get(_norm_header(k)) for k in keys}
    if "asset_no" not in mapped:
        return False
    return bool(mapped & ASSET_COMPANION)              # ③ 관리번호 혼자면 자산 표로 안 본다


def _apply_one(app, item):
    """엑셀 한 개를 '빈 칸 채우기'로 반영한다. migrate 라우트와 같은 코드를 쓴다."""
    from ..importers import read_first_sheet
    from .migration import _prepare, apply_rows

    from .sales import apply_sale_slips, is_sale_slip_sheet

    rows = read_first_sheet(item["path"].read_bytes(), item["name"])

    # 판매내역·판매미수금관리는 자산 표가 아니라 '전표' 표다. 예전엔 그냥 건너뛰어서
    # 매출·정산 이력이 OWS에 아예 안 들어왔다(2026-08-04 대표 지시로 이관 시작).
    if is_sale_slip_sheet(rows):
        with tx(write=True) as conn:
            res = apply_sale_slips(conn, rows, actor="자동반영")
            conn.execute(
                "INSERT INTO tms_sync_log(filename, file_hash, size, rows, created, updated, "
                "errors, synced_at, note) VALUES(?,?,?,?,?,?,0,?,?)",
                (item["name"], item["hash"], item["size"], len(rows),
                 res["created"], res["updated"], config.now_iso(), "판매 전표"))
            audit.log("tms_autosync_sales", target=item["name"],
                      detail={"rows": len(rows), **res})
        app.logger.info("TMS 자동반영(판매전표) %s — 신규 %s / 채움 %s",
                        item["name"], res["created"], res["updated"])
        return {"created": res["created"], "updated": res["updated"]}

    if not _is_asset_sheet(rows, item["name"]):
        # 자산 표가 아니면 기록만 남기고 넘어간다 — 다음 주기에 또 들여다보지 않게.
        with tx(write=True) as conn:
            conn.execute(
                "INSERT INTO tms_sync_log(filename, file_hash, size, rows, created, updated, "
                "errors, synced_at, note) VALUES(?,?,?,?,0,0,0,?,?)",
                (item["name"], item["hash"], item["size"], len(rows), config.now_iso(),
                 "자산 표가 아니라 건너뜀(관리번호 칸 없음)"))
        app.logger.info("TMS 자동반영 건너뜀(자산 표 아님): %s", item["name"])
        return {"created": 0, "updated": 0, "skipped": True}

    with tx(write=True) as conn:
        ready, dup, errors, updates, locked = _prepare(conn, rows, fill_blanks=True)
        res = apply_rows(conn, ready, updates, actor="자동반영")
        # 상태갱신·복귀후보는 note 로 남긴다 — 화면 '비고' 칸에서 바로 보인다
        note_bits = []
        # ★판매현황(자산별)은 자산 빈칸 채우기'와' 자산 단위 판매 원장 둘 다 탄다(2026-08-25)
        from .sales import is_sale_asset_sheet, upsert_asset_sales
        if is_sale_asset_sheet(rows):
            sres = upsert_asset_sales(conn, rows, actor="자동반영")
            if sres["created"] or sres["updated"]:
                note_bits.append("판매명세 신규 %d/갱신 %d" % (sres["created"], sres["updated"]))
        if res.get("statusUpdated"):
            note_bits.append("상태갱신 %d건" % res["statusUpdated"])
        if res.get("corrected"):
            note_bits.append("TMS정정 %d건" % res["corrected"])
        if res.get("correctionsFrozen"):
            note_bits.append("★정정 %d건 폭주 — 반영 보류(엑셀 확인 필요)"
                             % res["correctionsFrozen"])
        if res.get("statusReverts"):
            alerts = res.get("statusAlerts") or []
            note_bits.append("재고복귀 후보 %d건(자동 반영 안 함): %s" % (
                res["statusReverts"], " / ".join(alerts[:5])
                + (" 외 %d건" % (res["statusReverts"] - 5) if res["statusReverts"] > 5 else "")))
        # ★번호를 바로잡아 잠근 자산 — 일부러 안 받은 것이라 오류가 아니다.
        #   그래도 숫자는 남긴다(잠금이 도는지 눈으로 확인할 수 있게).
        if locked:
            note_bits.append("정정잠금 %d건(TMS 값 반영 안 함)" % len(locked))
        note_bits.extend(errors[:3])
        conn.execute(
            "INSERT INTO tms_sync_log(filename, file_hash, size, rows, created, updated, "
            "errors, synced_at, note) VALUES(?,?,?,?,?,?,?,?,?)",
            (item["name"], item["hash"], item["size"], len(rows),
             res["created"], res["updated"], len(errors), config.now_iso(),
             "; ".join(note_bits)))
        audit.log("tms_autosync", target=item["name"],
                  detail={"rows": len(rows),
                          **{k: v for k, v in res.items() if k != "statusAlerts"},
                          "errors": len(errors), "locked": len(locked)})
    app.logger.info("TMS 자동반영 %s — 신규 %s / 채움 %s / 상태갱신 %s / 정정잠금 %s / 오류 %s",
                    item["name"], res["created"], res["updated"],
                    res.get("statusUpdated", 0), len(locked), len(errors))
    return res


def _true_up_tms_batch_totals(conn):
    """기계가 만든([TMS 이관]) 전표의 금액을 자산 합으로 따라가게 한다(2026-08-10 대표 승인).

    전표가 처음 만들어진 '뒤에' 다음 수집이 자산을 더 붙이면 전표 금액만 옛값에 남아
    '⚠ 자산 합계가 더 큼' 경고가 떴다(2026-08-10 실측 12전표 — 차액이 전부
    '늦게 붙은 자산 합'과 일치, 부가세·판매가 가설은 기각). TMS 엑셀에는 전표 총액
    칸이 없어 자산 합이 곧 정답이다. ★사람이 만든 전표는 절대 건드리지 않는다.
    """
    sums = {r["batch_id"]: r["s"] for r in conn.execute(
        "SELECT batch_id, COALESCE(SUM(purchase_price),0) AS s FROM assets "
        "WHERE batch_id IS NOT NULL GROUP BY batch_id").fetchall()}
    ts = config.now_iso()
    n = 0
    for b in conn.execute(
            "SELECT id, total_amount FROM purchase_batches "
            "WHERE memo='[TMS 이관]' AND cancelled_at=''").fetchall():
        want = sums.get(b["id"], 0)
        if want and want != (b["total_amount"] or 0):
            conn.execute(
                "UPDATE purchase_batches SET total_amount=?, updated_at=? WHERE id=?",
                (want, ts, b["id"]))
            n += 1
    return n


def sync_once(app):
    """폴더를 한 번 훑어 새 파일만 반영한다. 반영한 파일 수를 돌려준다."""
    with app.app_context():
        # 전표 금액 따라가기 — 새 파일이 없어도 틱(10분)마다 한 번(백로그 자가치유)
        try:
            with tx(write=True) as conn:
                fixed = _true_up_tms_batch_totals(conn)
            if fixed:
                app.logger.info("TMS 전표 금액 따라가기: %d건", fixed)
        except Exception:                                        # noqa: BLE001
            app.logger.exception("TMS 전표 금액 따라가기 실패")
        # 판매 원장 자가 시드(2026-08-26 대표 "버튼 없이 그냥 동기화") — tms_sales 가
        # 비어 있는데 폴더에 판매현황이 있으면 한 번 채운다. 이미 반영된(해시 기록된)
        # 파일이라 pending 으로는 안 잡히기 때문에 여기서 따로 본다. 채워진 뒤엔 0비용.
        try:
            with tx() as conn:
                empty = conn.execute("SELECT 1 FROM tms_sales LIMIT 1").fetchone() is None
            if empty:
                from .sales import is_sale_asset_sheet, upsert_asset_sales
                from ..importers import read_first_sheet
                files = sorted(EXPORT_DIR.glob("판매현황*.xlsx"),
                               key=lambda x: x.stat().st_mtime, reverse=True)
                for f in files[:1]:
                    rows = read_first_sheet(f.read_bytes(), f.name)
                    if not is_sale_asset_sheet(rows):
                        continue
                    with tx(write=True) as conn:
                        res = upsert_asset_sales(conn, rows, actor="자동시드")
                    app.logger.info("판매 원장 자가 시드: %s — 신규 %d / 매칭 %d",
                                    f.name, res["created"], res["matched"])
        except Exception:                                        # noqa: BLE001
            app.logger.exception("판매 원장 자가 시드 실패")
        with tx() as conn:
            items = pending_files(conn)
        if not items:
            return 0
        n = 0
        for item in items:
            try:
                _apply_one(app, item)
                n += 1
                _failures.pop(item["name"], None)                # 성공하면 경보 해제
            except Exception as e:                               # noqa: BLE001
                # 한 파일이 잘못돼도 나머지는 반영한다. 실패한 파일은 기록이 안 남으므로
                # 다음 주기에 자동으로 다시 시도된다.
                app.logger.exception("TMS 자동반영 실패: %s", item["name"])
                # ★대시보드 경보용(2026-08-09 대표 승인) — 조용한 실패가 반복되면
                #   대표가 첫 화면에서 바로 본다. 서버 재시작 시 초기화(다시 실패하면 다시 뜸).
                old = _failures.get(item["name"]) or {"count": 0}
                _failures[item["name"]] = {
                    "error": str(e)[:200], "at": config.now_iso(),
                    "count": old["count"] + 1}
        return n


def start_tms_autosync(app):
    """tms-export 폴더 감시 데몬."""
    def loop():
        time.sleep(30)          # 서버가 다 뜬 뒤에 시작
        while True:
            try:
                sync_once(app)
            except Exception:                                    # noqa: BLE001
                app.logger.exception("TMS 자동반영 루프 오류")
            time.sleep(TICK_SECONDS)

    t = threading.Thread(target=loop, name="ows-tms-autosync", daemon=True)
    t.start()
    return t


# ── 내보내기 쪽(tools/tms_export) 상태 읽기 ────────────────────────────
#    화면에서 보이게만 한다. 여기서 그걸 실행하거나 고치지 않는다 — 완전히 별개 프로그램이고,
#    그쪽이 통째로 없어도 이 함수는 조용히 빈 값을 돌려줘야 한다.
EXPORTER_DIR = config.ROOT / "tools" / "tms_export"
LOGIN_MARK = EXPORT_DIR / "_TMS-로그인-필요.txt"


def exporter_state():
    import json as _json

    state = {"installed": False, "screens": 0, "loginNeeded": LOGIN_MARK.exists(),
             "lastRun": None, "updated": [], "failed": []}
    try:
        state["installed"] = (EXPORTER_DIR / "venv" / "Scripts" / "python.exe").exists()
        conf = EXPORTER_DIR / "screens.json"
        if conf.exists():
            data = _json.loads(conf.read_text(encoding="utf-8"))
            state["screens"] = sum(1 for s in data.get("screens", []) if s.get("enabled", True))
        last = EXPORTER_DIR / "logs" / "last-run.json"
        if last.exists():
            data = _json.loads(last.read_text(encoding="utf-8"))
            state["lastRun"] = data.get("at")
            state["updated"] = data.get("updated") or []
            state["failed"] = data.get("failed") or []
            # ★멈춤 감지(2026-08-09) — 2시간 주기인데 5시간 넘게 새 실행이 없으면
            #   스케줄러가 죽었거나 로그인이 풀린 것이다. 대시보드가 경보로 띄운다.
            try:
                import datetime as _dt
                ran = _dt.datetime.fromisoformat(state["lastRun"]).replace(tzinfo=None)
                now = _dt.datetime.fromisoformat(config.now_iso()).replace(tzinfo=None)
                hours = (now - ran).total_seconds() / 3600
                state["staleHours"] = round(hours, 1)
                state["stale"] = hours > 5
            except (TypeError, ValueError):
                pass
    except Exception:                                        # noqa: BLE001
        pass                                                  # 상태 표시가 서버를 흔들면 안 된다
    return state


@bp.get("/tms-sync/status")
def tms_sync_status():
    """자동 반영 현황 — 마지막으로 언제 무엇이 들어왔는지."""
    require("purchase.view")
    with tx() as conn:
        rows = conn.execute(
            "SELECT * FROM tms_sync_log ORDER BY id DESC LIMIT 20").fetchall()
        waiting = [x["name"] for x in pending_files(conn)]
    return jsonify({
        "folder": str(EXPORT_DIR),
        "intervalMinutes": TICK_SECONDS // 60,
        "exporter": exporter_state(),             # TMS에서 받아오는 쪽 상태
        "failures": [{"file": k, **v} for k, v in _failures.items()],  # 반영 실패 경보
        "waiting": waiting,                       # 아직 반영 안 된 파일
        "history": [
            {"file": r["filename"], "rows": r["rows"], "created": r["created"],
             "updated": r["updated"], "errors": r["errors"],
             "syncedAt": r["synced_at"], "note": r["note"]}
            for r in rows
        ],
    })


@bp.post("/tms-sync/run")
def tms_sync_run():
    """지금 바로 반영 — 기다리지 않고 손으로 돌릴 때."""
    require("purchase.edit")
    from flask import current_app
    n = sync_once(current_app._get_current_object())
    return jsonify({"ok": True, "applied": n})
