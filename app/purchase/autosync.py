"""TMS 엑셀 자동 반영 — 폴더에 새 파일이 들어오면 알아서 최신화한다.

대표 결정(2026-07-30): TMS와 HMS를 함께 쓰되, HMS에 입력을 안 하는 경우가 있으니
주기적으로 TMS 데이터를 긁어와 최신화한다.

★왜 '내보내기'는 자동화하지 않는가
  TMS는 개발사(jd-soft) 서버의 Blazor 앱이라 내보내기를 자동화하려면 로그인 세션으로
  화면을 조작해야 한다. 그건 1순위 규칙("TMS에 어떤 데이터도 건들지 않는다")을 위협하고,
  세션이 끊기면 조용히 실패한다. 그래서 역할을 나눈다.

    대표: 하루 1회 TMS에서 엑셀 내보내기 → tms-export 폴더에 저장 (몇 번의 클릭)
    HMS : 폴더를 지켜보다가 새 파일이 들어오면 자동으로 반영

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


# ★HMS가 스스로 뽑은 엑셀은 절대 되먹이지 않는다.
#   주문 내려받기(app/orders/__init__.py:551)에 '자산번호' 칸이 있어서, 그 파일을
#   tms-export 폴더에 잘못 떨구면 자산 표로 오인돼 없는 자산이 자동 등록된다.
#   (2026-07-31 hms-orders-20260731.xlsx 가 실제로 이 폴더를 통과했다. 그날은 자산번호가
#    채워진 줄이 1개뿐이라 우연히 아무것도 안 만들어졌을 뿐이다.)
OUR_EXPORT_PREFIXES = ("hms-", "hms_")
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
    # 매출·정산 이력이 HMS에 아예 안 들어왔다(2026-08-04 대표 지시로 이관 시작).
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
        ready, dup, errors, updates = _prepare(conn, rows, fill_blanks=True)
        res = apply_rows(conn, ready, updates, actor="자동반영")
        conn.execute(
            "INSERT INTO tms_sync_log(filename, file_hash, size, rows, created, updated, "
            "errors, synced_at, note) VALUES(?,?,?,?,?,?,?,?,?)",
            (item["name"], item["hash"], item["size"], len(rows),
             res["created"], res["updated"], len(errors), config.now_iso(),
             "; ".join(errors[:3])))
        audit.log("tms_autosync", target=item["name"],
                  detail={"rows": len(rows), **res, "errors": len(errors)})
    app.logger.info("TMS 자동반영 %s — 신규 %s / 채움 %s / 오류 %s",
                    item["name"], res["created"], res["updated"], len(errors))
    return res


def sync_once(app):
    """폴더를 한 번 훑어 새 파일만 반영한다. 반영한 파일 수를 돌려준다."""
    with app.app_context():
        with tx() as conn:
            items = pending_files(conn)
        if not items:
            return 0
        n = 0
        for item in items:
            try:
                _apply_one(app, item)
                n += 1
            except Exception:                                    # noqa: BLE001
                # 한 파일이 잘못돼도 나머지는 반영한다. 실패한 파일은 기록이 안 남으므로
                # 다음 주기에 자동으로 다시 시도된다.
                app.logger.exception("TMS 자동반영 실패: %s", item["name"])
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

    t = threading.Thread(target=loop, name="hms-tms-autosync", daemon=True)
    t.start()
    return t


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
