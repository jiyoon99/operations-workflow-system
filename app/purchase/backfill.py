"""사업부 최초 판정(백필) — 화면/API 경로.

왜 스크립트가 아니라 API인가
  라이브 DB에 이행 스크립트를 직접 돌리지 않는다(사내 규칙, 2026-07-31 사고).
  반영은 화면/API로만 한다. 그래서 같은 판정 규칙을 여기에 둔다.
  scripts/backfill_division.py 는 '사본에서 미리 세어 보는' 용도로 남는다 — 규칙은 동일하다.

판정 규칙(대표 확정 2026-08-12) — rental 로 바꾸는 경우만 있다. 반대 방향은 없다.
  R1  RMS가 보유 중인 관리번호            → 렌탈 (상태 무관)
  R2  TMS 재고상태가 '렌탈' 또는 '반납'   → 렌탈
  R3  TMS 관리번호 오배정 13건            → 렌탈 + 잠금(division_locked=1)

건드리지 않는 것
  - RMS에 같은 관리번호가 2건 이상(중복 등록) — 어느 실물인지 특정할 수 없다.
    대표 지시: "중복등록 데이터로 남겨주면 자산번호를 바꾸면 되니 그대로 남겨줘."
  - division_locked=1 — 사람이 이미 판단한 건. 자동 판정이 뒤집지 못한다.
  - 이미 같은 값인 건 — 멱등(두 번 돌려도 결과가 같다).

RMS 근거를 왜 요청 본문으로 받나
  OWS는 185에서 돌고 RMS DB는 240에 있다. 185에서 그 파일을 읽을 수 없다.
  그래서 240 쪽이 '관리번호별 보유 대수'를 실어 보낸다.
  이관(transfers.py)이 rmsAssets 를 실어 보내는 것과 같은 이유다.

되돌리기
  적용 전 값을 settings['division_backfill_<runId>'] 에 통째로 남긴다.
  POST .../backfill-division/undo {runId} 로 복원한다.
"""
import json

from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import DIVISION_RENTAL, get_db, tx
from . import asset_event, bp

TMS_RENTAL = ("렌탈", "반납")

# 결정 1(2026-08-04) — 같은 판매전표에 여러 사람이 서로 다른 자산번호로 붙어 있어
# 정상 판매일 수 없다. 렌탈로 확정하고 잠근다. 전표 기록 자체는 지우지 않는다.
EXCEPTION_13 = (
    "241216-0016", "250102-0001", "250102-0002", "250102-0003", "250102-0004",
    "250102-0005", "250107-0072", "250116-0016", "250121-0047", "250225-0027",
    "250314-0021", "250509-0044", "250925-0001",
)
EXCEPTION_NOTE = ("TMS 관리번호 오배정 — 같은 판매전표에 여러 사람이 서로 다른 자산번호로 붙어 있음. "
                  "OWS 판매전표는 무시하고 렌탈로 확정(2026-08-04 대표 확인). 전표 기록은 보존.")

NOT_ASSETS = ("테스트", "2026-0031")          # 시험용 더미 / 자산 실체 없음
_SNAP_KEY = "division_backfill_"
_TMS_XLSX = config.ROOT / "tms-export" / "재고항목현황.xlsx"


def _load_tms():
    """TMS 재고항목현황 → {관리번호: 재고상태}. 파일이 없거나 깨졌으면 R2 없이 진행한다."""
    if not _TMS_XLSX.exists():
        return {}, "TMS 파일이 없습니다 — R2 없이 진행합니다."
    try:
        from ..importers.excel import read_first_sheet
        rows = read_first_sheet(_TMS_XLSX.read_bytes(), str(_TMS_XLSX))
    except Exception as e:                      # noqa: BLE001 - 근거 파일이 깨져도 백필은 계속
        return {}, f"TMS 파일을 읽지 못했습니다({e.__class__.__name__}) — R2 없이 진행합니다."
    if not rows:
        return {}, "TMS 파일이 비어 있습니다 — R2 없이 진행합니다."
    mcol = next((c for c in rows[0] if "관리번호" in c), "")
    scol = next((c for c in rows[0] if "재고상태" in c), "")
    if not mcol or not scol:
        return {}, "TMS 파일에 관리번호/재고상태 칸이 없습니다 — R2 없이 진행합니다."
    return {(r.get(mcol) or "").strip().upper(): (r.get(scol) or "").strip()
            for r in rows if (r.get(mcol) or "").strip()}, ""


def _rms_counts(body):
    """{관리번호: 보유 대수}. rmsCounts(대수 dict) 또는 rmsNos(목록) 둘 다 받는다."""
    raw = body.get("rmsCounts")
    if isinstance(raw, dict):
        out = {}
        for k, v in raw.items():
            no = str(k or "").strip().upper()
            if not no:
                continue
            try:
                out[no] = int(v)
            except (TypeError, ValueError):
                out[no] = 1
        return out
    out = {}
    for x in (body.get("rmsNos") or []):
        no = str(x or "").strip().upper()
        if no:
            out[no] = out.get(no, 0) + 1
    return out


def _decide(no, tms, rms):
    """관리번호 하나의 판정 → (locked, note, 규칙) 또는 None(=건드리지 않음)."""
    if no in NOT_ASSETS:
        return None
    if rms.get(no, 0) > 1:                      # 중복 등록 — 실물을 특정할 수 없다
        return None
    if no in EXCEPTION_13:
        return 1, EXCEPTION_NOTE, "R3 예외(잠금)"
    if no in rms:
        return 0, "", "R1 RMS 보유"
    if tms.get(no) in TMS_RENTAL:
        return 0, "", "R2 TMS 렌탈/반납"
    return None


def _run_id(conn):
    n = conn.execute("SELECT COUNT(*) AS c FROM settings WHERE key LIKE ?",
                     (_SNAP_KEY + "%",)).fetchone()["c"]
    return "BF-%s-%02d" % (config.now_iso()[:10].replace("-", ""), n + 1)


def _do_backfill(body):
    """판정 → dry면 세어만 보고, 아니면 적용하고 되돌리기 근거를 남긴다."""
    dry = body.get("dry") is not False           # ★기본은 시험. 명시적으로 dry=false 여야 쓴다.
    rms = _rms_counts(body)
    tms, warn = _load_tms()

    conn = get_db()
    rows = conn.execute(
        "SELECT id, asset_no, division, division_locked, model, status FROM assets "
        "WHERE TRIM(COALESCE(asset_no,'')) <> ''").fetchall()

    plan, skipped_locked, already = [], 0, 0
    by_rule = {}
    for a in rows:
        no = (a["asset_no"] or "").strip().upper()
        d = _decide(no, tms, rms)
        if d is None:
            continue
        locked, note, rule = d
        by_rule[rule] = by_rule.get(rule, 0) + 1
        if a["division_locked"]:                 # 사람이 이미 판단한 건은 자동 판정이 못 뒤집는다
            skipped_locked += 1
            continue
        if a["division"] == DIVISION_RENTAL and (a["division_locked"] or 0) == locked:
            already += 1
            continue
        plan.append({"id": a["id"], "no": a["asset_no"], "locked": locked, "note": note,
                     "rule": rule, "prev": a["division"], "prevLocked": a["division_locked"] or 0,
                     "model": a["model"], "status": a["status"]})

    sale_now = conn.execute(
        "SELECT COUNT(*) AS c FROM assets WHERE division='sale'").fetchone()["c"]
    out = {
        "dry": dry, "warn": warn,
        "rmsGiven": len(rms), "tmsFound": len(tms),
        "byRule": by_rule, "willChange": len(plan),
        "skippedLocked": skipped_locked, "alreadyRental": already,
        "saleNow": sale_now, "saleAfter": sale_now - len(plan),
        "sample": [{"no": p["no"], "rule": p["rule"], "prev": p["prev"],
                    "model": p["model"], "status": p["status"]} for p in plan[:20]],
    }
    if dry or not plan:
        out["runId"] = ""
        out["note"] = ("시험 계산입니다 — 아무것도 바꾸지 않았습니다." if dry
                       else "바꿀 것이 없습니다.")
        return out

    with tx(write=True) as wconn:
        run_id = _run_id(wconn)
        ts, actor = config.now_iso(), g.user["display_name"]
        snap = [{"id": p["id"], "no": p["no"], "d": p["prev"], "l": p["prevLocked"]} for p in plan]
        # ★스냅샷을 먼저 남긴다 — 적용 도중 끊겨도 되돌릴 근거가 남아야 한다.
        wconn.execute(
            "INSERT INTO settings(key, value, updated_at, updated_by) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (_SNAP_KEY + run_id,
             json.dumps({"at": ts, "by": actor, "items": snap}, ensure_ascii=False), ts, actor))
        for p in plan:
            wconn.execute(
                "UPDATE assets SET division=?, division_locked=?, division_note=?, "
                " division_since=?, division_by=?, division_ref=?, updated_at=? "
                "WHERE id=? AND division_locked=0",
                (DIVISION_RENTAL, p["locked"], p["note"], ts, actor, run_id, ts, p["id"]))
            asset_event(wconn, p["id"], "사업부 최초 판정",
                        {"사업부": "렌탈", "근거": p["rule"], "이전": p["prev"], "묶음": run_id})
        audit.log("division_backfill", target=run_id,
                  detail={"count": len(plan), "byRule": by_rule,
                          "tms": len(tms), "rms": len(rms)})
    out["runId"] = run_id
    out["note"] = "%d건을 렌탈로 표시했습니다. 되돌리기 번호 %s" % (len(plan), run_id)
    return out


def _do_undo(run_id):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (_SNAP_KEY + run_id,)).fetchone()
    if row is None:
        abort(404, description="그 번호의 백필 기록이 없습니다.")
    try:
        snap = (json.loads(row["value"]) or {}).get("items") or []
    except ValueError:
        abort(400, description="백필 기록이 손상되었습니다 — 되돌릴 수 없습니다.")
    with tx(write=True) as wconn:
        ts, actor = config.now_iso(), g.user["display_name"]
        n = 0
        for it in snap:
            # ★백필 이후에 사람이 다시 판단했거나 이관된 건(division_ref가 바뀜)은 건드리지 않는다.
            cur = wconn.execute("SELECT division_ref FROM assets WHERE id=?",
                                (it["id"],)).fetchone()
            if cur is None or cur["division_ref"] != run_id:
                continue
            wconn.execute(
                "UPDATE assets SET division=?, division_locked=?, division_note='', "
                " division_ref='', division_since=?, division_by=?, updated_at=? WHERE id=?",
                (it["d"], it["l"], ts, actor, ts, it["id"]))
            asset_event(wconn, it["id"], "사업부 최초 판정 되돌림",
                        {"묶음": run_id, "복원": it["d"]})
            n += 1
        audit.log("division_backfill_undo", target=run_id, detail={"count": n})
    return {"runId": run_id, "restored": n, "inSnapshot": len(snap)}


@bp.post("/assets/backfill-division")
def backfill_division():
    require("purchase.edit")
    return jsonify(_do_backfill(request.get_json(silent=True) or {}))


@bp.post("/assets/backfill-division/undo")
def backfill_division_undo():
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    return jsonify(_do_undo((body.get("runId") or "").strip()))


@bp.post("/bridge/assets/backfill-division")
def bridge_backfill_division():
    """240에서 RMS 보유 목록을 실어 보내 최초 판정을 돌리는 자리(토큰 자격)."""
    return jsonify(_do_backfill(request.get_json(silent=True) or {}))


@bp.post("/bridge/assets/backfill-division/undo")
def bridge_backfill_undo():
    body = request.get_json(silent=True) or {}
    return jsonify(_do_undo((body.get("runId") or "").strip()))
