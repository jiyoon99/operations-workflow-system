"""창구 동시 마감 — 이중입력 경보 (2026-09-03, docs/CUTOVER_PLAN.md §5).

대표 방침(2026-09-02): 앞으로 모든 등록·관리는 OWS·RMS에서 한다. 판매·매입 창구를 OWS로 옮긴 날(마감일 D) 뒤에도
습관처럼 TMS에 전표를 넣으면 같은 물건이 두 원장에 생긴다. 이 파일은 연동 사본의 변경 피드(HIS변경요약)에서
D 이후의 **'추가'**만 골라 첫 화면 '오늘 할 일'에 띄우고, 사람이 '정상(되돌리기·정리)' 또는 'OWS 재입력 완료'로 닫는다.

  원천   : 연동 창구 GET /changes?after=<HIS 키ID 커서>&tables=HB_TBL판매H,HB_TBL매입H,HB_TBL재고
           (`after`는 날짜가 아니라 HIS변경요약 키ID — 마감일을 저장할 때 창구 status.his_cursor 를 기준선으로 잡고,
            그 뒤로는 tms_link 상태 파일의 `dual_after` 로 이어 읽는다)
  위반   : 변경구분 '추가' 이고 변경일시 >= D 00:00. '수정'은 위반이 아니다(입금 확인·반입·정정 — 검증 L-1, 위양성 매일 남).
  기록   : tms_dual_entries (HIS 키ID UNIQUE — 같은 변경은 한 번만, 재스캔 멱등). 원본 행(전표번호·거래처·수량·금액·입력자)은
           창구 /tables/<표>?after_key=키ID-1&limit=1 로 한 건 읽어 채운다(창구가 키ID 동등 필터를 안 받는다 — 키ID 일치 확인).
  표시   : 매입H 가 수량 ≤1·매입금액 0 이면 '임시매입 의심'(판매 목적 임시 매입 패턴), 재고 행이 마감일 전 전표에 붙으면 '옛 전표에 추가'.
  호출   : tms_link.sync_once 끝에서 틱마다(2분). 실패해도 틱을 깨지 않는다. 마감일이 없으면 커서만 유지하고 아무것도 안 만든다.
  번호대 : 창구 status.invariants(자산 일련 ≥5000·전표 ≥500 침범 행 수)를 같이 받아 둔다 — 마감과 무관하게 0이 아니면 오늘 할 일.

★TMS 는 읽기 전용 — 여기서 TMS 값을 고치거나 OWS 에 자동 반영하지 않는다(판정은 사람).
"""
import datetime as _dt
import json
import re

from flask import current_app, g, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import tx
from . import bp

SETTING_KEY = "cutover"
STATE_KEY = "dual_after"                       # tms_link 상태 파일 안의 HIS 키ID 커서
TABLES = ("HB_TBL판매H", "HB_TBL매입H", "HB_TBL재고")
KIND = {"HB_TBL판매H": "판매", "HB_TBL매입H": "매입", "HB_TBL재고": "재고"}
STATUSES = ("open", "ok", "reentered")
STATUS_LABEL = {"open": "확인 필요", "ok": "정상(되돌리기·정리)", "reentered": "OWS 재입력 완료"}
PAGE = 500
MAX_PAGES = 10                                 # 틱당 최대 5,000건 — 남으면 다음 틱이 이어 읽는다
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_last = {}                                     # 마지막 점검 요약(메모리) — 화면용


# ---------------------------------------------------------------- 설정
def load_cutover(conn):
    cfg = {"date": "", "enabled": True, "baselineCursor": 0, "baselineAt": "", "setBy": "", "setAt": ""}
    row = conn.execute("SELECT value FROM settings WHERE key=?", (SETTING_KEY,)).fetchone()
    if row:
        try:
            cfg.update(json.loads(row["value"]) or {})
        except ValueError:
            pass
    return cfg


def is_active(cfg):
    return bool(cfg.get("enabled", True)) and bool(cfg.get("date"))


def _save_cutover(conn, cfg, actor=""):
    conn.execute("INSERT OR REPLACE INTO settings(key, value, updated_at, updated_by) VALUES(?,?,?,?)",
                 (SETTING_KEY, json.dumps(cfg, ensure_ascii=False), config.now_iso(), actor))


# ---------------------------------------------------------------- 점검(틱)
def _fetch_row(cli, table, key):
    """원본 행 1건 — 창구는 키ID 동등 필터를 받지 않으므로 after_key=키ID-1·limit=1 로 읽고 키ID 일치를 확인한다."""
    try:
        data = cli.get(f"/tables/{table}", after_key=int(key) - 1, limit=1, include_deleted=1)
    except Exception:                                            # noqa: BLE001 — 원본을 못 읽어도 경보는 남긴다
        return None
    items = data.get("items") or []
    return items[0] if items and int(items[0].get("키ID") or 0) == int(key) else None


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def describe(table, row, cutover_date=""):
    """원본 행 → 화면에 보일 칸. 행이 없으면(삭제·조회 실패) 빈 값."""
    out = {"slip_no": "", "asset_no": "", "party": "", "qty": 0, "amount": 0.0, "actor": "", "pattern": ""}
    if not row:
        out["pattern"] = "원본 없음"                            # 사본에 행이 없다 — 추가 직후 삭제됐거나(사본없음 삭제) 창구 조회 실패
        return out
    if table == "HB_TBL판매H":
        out.update(slip_no=str(row.get("전표번호") or ""), party=str(row.get("거래처명") or ""),
                   qty=int(_num(row.get("수량"))), amount=_num(row.get("판매금액")), actor=str(row.get("판매자") or ""))
    elif table == "HB_TBL매입H":
        out.update(slip_no=str(row.get("전표번호") or ""), party=str(row.get("거래처명") or ""),
                   qty=int(_num(row.get("수량"))), amount=_num(row.get("매입금액")), actor=str(row.get("매입자") or ""))
        if out["qty"] <= 1 and out["amount"] == 0:
            out["pattern"] = "임시매입 의심"                   # 판매 목적 임시 매입 패턴(매입가 0·1라인) — 계획서 §5
    else:                                                        # HB_TBL재고 = 매입상세 추가의 그림자
        out.update(slip_no=str(row.get("매입전표번호") or ""), asset_no=str(row.get("관리번호") or ""),
                   party=str(row.get("매입처명") or ""), qty=1, amount=_num(row.get("매입가")),
                   actor=str(row.get("매입자") or ""))
        m = re.match(r"^P(\d{6})-", out["slip_no"])
        if m and cutover_date and ("20" + m.group(1)[:2] + "-" + m.group(1)[2:4] + "-" + m.group(1)[4:6]) < cutover_date:
            out["pattern"] = "옛 전표에 추가"
    return out


def scan(app, cli, state):
    """틱마다 — 변경 피드에서 마감일 이후 '추가'를 골라 tms_dual_entries 에 남긴다. 반환은 요약(틱 summary 에 실림)."""
    with app.app_context():
        with tx() as conn:
            cfg = load_cutover(conn)
        summary = {"at": config.now_iso(), "active": is_active(cfg), "date": cfg.get("date") or "",
                   "checked": 0, "new": 0, "after": int(state.get(STATE_KEY) or 0)}
        try:
            summary["invariants"] = (cli.get("/status")["status"] or {}).get("invariants") or {}
        except Exception as e:                                   # noqa: BLE001
            summary["invariantsError"] = str(e)[:120]
        if not summary["active"]:
            summary["skipped"] = "마감일 없음"
            _last.clear()
            _last.update(summary)
            return summary
        after = int(state.get(STATE_KEY) or cfg.get("baselineCursor") or 0)
        floor = f"{cfg['date']} 00:00:00"
        now = config.now_iso()
        for _ in range(MAX_PAGES):
            data = cli.get("/changes", after=after, tables=",".join(TABLES), limit=PAGE)
            items = data.get("items") or []
            if not items:
                break
            with tx(write=True) as conn:
                for it in items:
                    summary["checked"] += 1
                    table = it.get("테이블명") or ""
                    if it.get("변경구분") != "추가" or table not in KIND:
                        continue
                    changed_at = str(it.get("변경일시") or "")[:19]
                    if changed_at < floor:
                        continue
                    key = int(it.get("변경키ID") or 0)
                    d = describe(table, _fetch_row(cli, table, key) if key else None, cfg["date"])
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO tms_dual_entries(his_key, table_name, kind, tms_key, slip_no, asset_no, party, "
                        "qty, amount, actor, pattern, changed_at, detected_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (int(it["키ID"]), table, KIND[table], key, d["slip_no"], d["asset_no"], d["party"], d["qty"],
                         d["amount"], d["actor"], d["pattern"], changed_at, now))
                    summary["new"] += cur.rowcount if cur.rowcount > 0 else 0
            after = int(items[-1]["키ID"])
            state[STATE_KEY] = after                             # 페이지마다 전진 — 틱이 끊겨도 본 것은 다시 안 본다(멱등이라 겹쳐도 무해)
            if not data.get("next_after"):
                break
        summary["after"] = after
        if summary["new"]:
            app.logger.warning("이중입력 경보: 마감(%s) 뒤 TMS 신규 입력 %d건 감지", cfg["date"], summary["new"])
        _last.clear()
        _last.update(summary)
        return summary


# ---------------------------------------------------------------- 조회
def _counts(conn):
    out = {"판매": 0, "매입": 0, "재고": 0, "total": 0}
    for r in conn.execute("SELECT kind, COUNT(*) AS c FROM tms_dual_entries WHERE status='open' GROUP BY kind"):
        out[r["kind"]] = r["c"]
        out["total"] += r["c"]
    return out


def _item(r):
    return {"id": r["id"], "kind": r["kind"], "table": r["table_name"], "tmsKey": r["tms_key"], "slipNo": r["slip_no"],
            "assetNo": r["asset_no"], "party": r["party"], "qty": r["qty"], "amount": r["amount"], "actor": r["actor"],
            "pattern": r["pattern"], "changedAt": r["changed_at"], "detectedAt": r["detected_at"], "status": r["status"],
            "statusLabel": STATUS_LABEL.get(r["status"], r["status"]), "note": r["note"],
            "resolvedAt": r["resolved_at"], "resolvedBy": r["resolved_by"]}


@bp.get("/cutover")
def cutover_get():
    """설정 + 미처리 건수 + 목록(all=1 이면 처리한 것도)."""
    require("purchase.view")
    show_all = (request.args.get("all") or "") == "1"
    with tx() as conn:
        cfg = load_cutover(conn)
        counts = _counts(conn)
        rows = conn.execute(
            "SELECT * FROM tms_dual_entries" + ("" if show_all else " WHERE status='open'") +
            " ORDER BY changed_at DESC, id DESC LIMIT 300").fetchall()
    from .tms_link import _read_state, load_config
    return jsonify({"config": cfg, "active": is_active(cfg), "open": counts, "items": [_item(r) for r in rows],
                    "last": _last, "invariants": _last.get("invariants") or {},
                    "cursor": int(_read_state().get(STATE_KEY) or 0), "linkEnabled": load_config()["enabled"],
                    "statusLabels": STATUS_LABEL})


@bp.get("/cutover/alerts")
def cutover_alerts():
    """첫 화면 '오늘 할 일'용 — 가볍게 건수만."""
    require("purchase.view")
    with tx() as conn:
        cfg = load_cutover(conn)
        counts = _counts(conn)
    inv = _last.get("invariants") or {}
    return jsonify({"active": is_active(cfg), "date": cfg.get("date") or "", "open": counts,
                    "invariants": {"assetNo": int(inv.get("asset_no_ge_5000") or 0), "slipNo": int(inv.get("slip_no_ge_500") or 0),
                                   "checkedAt": inv.get("checked_at") or ""},
                    "lastAt": _last.get("at") or ""})


@bp.post("/cutover")
def cutover_set():
    """마감일 저장(settings.manage). 날짜가 바뀌면 창구 status.his_cursor 를 기준선으로 잡고 커서를 거기서 다시 시작한다."""
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    date = str(body.get("date") or "").strip()
    if date and not _DATE_RE.match(date):
        return jsonify({"error": "마감일은 YYYY-MM-DD 형식이어야 합니다."}), 400
    if date:
        try:
            _dt.date.fromisoformat(date)
        except ValueError:
            return jsonify({"error": "없는 날짜입니다."}), 400
    from .tms_link import Client, _read_state, _write_state, load_config
    with tx() as conn:
        cfg = load_cutover(conn)
    actor = g.user["display_name"]
    changed = date != (cfg.get("date") or "")
    cfg["enabled"] = bool(body.get("enabled", cfg.get("enabled", True)))
    if changed:
        baseline = 0
        if body.get("baselineCursor") not in (None, ""):
            baseline = int(body["baselineCursor"])
        else:
            lk = load_config()
            if lk["enabled"] and date:
                try:
                    baseline = int((Client(lk["url"], lk["token"]).get("/status")["status"] or {}).get("his_cursor") or 0)
                except Exception as e:                           # noqa: BLE001
                    return jsonify({"error": f"연동 창구에서 기준선을 못 읽었습니다: {str(e)[:120]}"}), 502
        cfg.update(date=date, baselineCursor=baseline, baselineAt=config.now_iso() if date else "",
                   setBy=actor, setAt=config.now_iso())
        st = _read_state()
        st[STATE_KEY] = baseline
        _write_state(st)
    with tx(write=True) as conn:
        _save_cutover(conn, cfg, actor)
        audit.log("cutover_set", target=date or "(해제)", detail={"date": date, "enabled": cfg["enabled"],
                                                                   "baselineCursor": cfg.get("baselineCursor"), "changed": changed})
    return jsonify({"ok": True, "config": cfg, "changed": changed})


@bp.post("/cutover/scan")
def cutover_scan_now():
    """지금 점검(purchase.edit) — 평소엔 틱마다 자동."""
    require("purchase.edit")
    from .tms_link import Client, _read_state, _write_state, load_config
    lk = load_config()
    if not lk["enabled"]:
        return jsonify({"error": "연동 창구 설정이 없습니다(설정 ▸ 데이터 연동)."}), 400
    state = _read_state()
    res = scan(current_app._get_current_object(), Client(lk["url"], lk["token"]), state)
    _write_state(state)
    return jsonify({"ok": True, "result": res})


@bp.post("/cutover/entries/<int:eid>/status")
def cutover_entry_status(eid):
    """사람 판정(purchase.edit): ok=정상(되돌리기·정리) / reentered=OWS 재입력 완료 / open=다시 열기."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    status = str(body.get("status") or "").strip()
    if status not in STATUSES:
        return jsonify({"error": "status 는 open / ok / reentered 중 하나입니다."}), 400
    note = str(body.get("note") or "").strip()[:200]
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM tms_dual_entries WHERE id=?", (eid,)).fetchone()
        if not row:
            return jsonify({"error": "없는 항목입니다."}), 404
        resolved_at, resolved_by = ("", "") if status == "open" else (config.now_iso(), g.user["display_name"])
        conn.execute("UPDATE tms_dual_entries SET status=?, note=?, resolved_at=?, resolved_by=? WHERE id=?",
                     (status, note, resolved_at, resolved_by, eid))
        audit.log("cutover_entry", target=row["slip_no"] or row["asset_no"] or str(eid),
                  detail={"id": eid, "kind": row["kind"], "from": row["status"], "to": status, "note": note})
        row = conn.execute("SELECT * FROM tms_dual_entries WHERE id=?", (eid,)).fetchone()
    return jsonify({"ok": True, "item": _item(row)})
