"""자산 카테고리 재판정 — 미리보기 → 적용 → 되돌리기 (2026-09-03, A5 재고 카테고리 정합).

무엇을 하나
  기존 자산의 카테고리가 TMS 중분류와 안 맞는다(실측 2026-09-03 사본: 15,714대 중 불일치 13,468 — 노트북 12,166대가
  'PC'로, 태블릿·모니터 795대가 '노트북'으로). 연동 규칙(migration.category_decision)을 바꿔도 이미 들어온 자산은
  그대로라, 같은 규칙으로 '지금 값 → 목표'를 한 번에 미리 보고 사람이 [적용]을 눌러 맞춘다. 되돌리기도 된다.
  화면: 매입 ▸ 자산 ▸ 자산 목록 [🗂 분류 재판정](purchase.js openReclass). 라이브 반영은 이 버튼으로만(코드 배포는 값을 안 바꾼다).

판정 근거(우선순위 = migration.category_decision 과 같다)
  ① 연동 창구 사본 HB_TBL재고 의 중분류 → TMS_SUBCAT_TO_CATEGORY  ② 빈값이면 모델 마스터(models) 중분류
  ③ 대분류(자유 입력값 노트북·데스크탑·모니터·태블릿·웨어러블)  ④ 못 정하면 '판정 불가' — 안 바꾼다(기본값으로 밀지 않는다)
  창구 설정이 없으면(검증 서버·창구 장애) 모델 마스터만으로 판정하고 응답 basis='master' + warning 으로 밝힌다.

사람이 고친 값 불가침(보호 — 기본 제외, protectedIds 로 개별 포함)
  ⓐ 이력 '수정'에 카테고리 변경이 있다(update_asset 이 남기는 유일한 흔적)
  ⓑ 첫 이력이 OWS 수기 '등록'(_add_assets — 사람이 카테고리를 고른 경로)이거나 관리번호가 OWS 번호대(≥5000)
  ⓒ tms_shadow.category(자동이 마지막으로 정한 값)가 있는데 지금 값과 다르다 — 자동 판정 뒤 사람이 바꿨다
  ⓓ tms_lock=1(번호·상태 정정 자산)은 '잠금'으로 따로 센다 — 기본 제외, includeLocked 로 포함

안전장치
  - 창구 호출은 tx 밖(외부 호출을 tx(write=True) 안에서 하지 않는 규칙). 적용 직전 재계산 = 멱등(두 번 눌러도 0건).
  - UPDATE … WHERE id=? AND category_id IS ? — 읽었을 때의 값일 때만(TMS상태갱신과 같은 경합 방지). 못 바꾼 건 skipped.
  - 자산별 이력 '분류재판정' {from,to,근거,ids:[전ID,후ID],audit} + tms_shadow.category 기록 + audit_log 1건(건수표).
  - 되돌리기는 그 audit 의 이력을 ids 로 역적용(이름이 아니라 id — 그 뒤 카테고리가 개명돼도 된다), 지금 값이 후ID 일 때만.
"""
import json
from collections import Counter

from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import tx
from ..settings import _int_or_400
from . import asset_event, bp
from .migration import (category_decision, category_ids, category_names, model_key, model_master_map,
                        set_shadow_category)

STOCK_TABLE = "HB_TBL재고"
# '출고 완료 포함' 체크가 다루는 상태 — 재고에서 빠진 것(출고·폐기·매입취소·거래처반품). 기본 포함(결정 ④).
GONE_STATUSES = ("shipped", "scrapped", "cancelled", "returned")
EVENT_APPLY = "분류재판정"
EVENT_UNDO = "분류재판정취소"
AUDIT_APPLY = "assets_reclassified"
AUDIT_UNDO = "assets_reclass_undone"
PROTECT_LABELS = {"edited": "카테고리 수정 이력", "manual": "OWS 수기 등록", "band": "OWS 번호대",
                  "shadow": "자동 판정 뒤 사람이 바꿈"}
SAMPLE = 20
PROTECTED_LIMIT = 200
MASTER_ONLY_WARNING = ("연동 창구 설정이 없어 모델 마스터만으로 판정했습니다 — 창구가 있는 서버(185)에서는 "
                       "TMS 재고 표의 중분류로 판정합니다.")


# ---------------------------------------------------------------- 입력
def _flag(v, default):
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _id_set(v, what):
    if v is None:
        return set()
    if not isinstance(v, list):
        abort(400, description=f"{what}은(는) 배열이어야 합니다.")
    return {_int_or_400(x, what) for x in v}


# ---------------------------------------------------------------- 창구(tx 밖)
def _tms_rows():
    """창구 사본 HB_TBL재고 전량(관리번호 → 행, 삭제 행 제외). 창구 설정이 없으면 (None, "master")."""
    from . import tms_link
    from .masters import _fetch_table
    cfg = tms_link.load_config()
    if not cfg["enabled"]:
        return None, "master"
    try:
        items = _fetch_table(tms_link.Client(cfg["url"], cfg["token"]), STOCK_TABLE)
    except Exception as e:                                       # noqa: BLE001
        abort(502, description="연동 창구에서 재고 표를 받지 못했습니다: %s" % str(e)[:160])
    live = {}
    for it in items:
        if it.get("_deleted_at"):
            continue
        no = str(it.get("관리번호") or "").strip()
        if no:
            live[no] = it
    return live, "datalink"


# ---------------------------------------------------------------- 판정
def _decide(conn, tms):
    """자산 전부의 (현재, 목표, 근거, 보호, 잠금, 재고 이탈). tms 는 관리번호→창구 행(None 이면 모델 마스터만)."""
    from . import numbering
    cats = category_ids(conn)
    names = category_names(conn)
    masters_map = model_master_map(conn)
    edited = {r["asset_id"] for r in conn.execute(
        "SELECT DISTINCT asset_id FROM asset_events WHERE action='수정' "
        "AND (detail LIKE '%\"categoryId\"%' OR detail LIKE '%\"카테고리\"%')").fetchall()}
    manual = {r["asset_id"] for r in conn.execute(
        "SELECT e.asset_id FROM asset_events e "
        "JOIN (SELECT asset_id, MIN(id) AS mid FROM asset_events GROUP BY asset_id) m ON m.mid = e.id "
        "WHERE e.action='등록'").fetchall()}
    out = []
    for r in conn.execute(
            "SELECT id, asset_no, category_id, model, status, division, tms_lock, tms_shadow "
            "FROM assets ORDER BY id").fetchall():
        row = tms.get(r["asset_no"]) if tms else None
        sub = main = ""
        model = r["model"] or ""
        if row is not None:
            sub, main = str(row.get("중분류") or ""), str(row.get("대분류") or "")
            model = str(row.get("모델명") or "").strip() or model
        tgt, basis = category_decision(cats, sub, main, masters_map.get(model_key(model)))
        try:
            shadow = json.loads(r["tms_shadow"]) if r["tms_shadow"] else {}
        except (ValueError, TypeError):
            shadow = {}
        if not isinstance(shadow, dict):
            shadow = {}
        protected = None
        if r["id"] in edited:
            protected = "edited"
        elif r["id"] in manual:
            protected = "manual"
        else:
            p = numbering.parse_no(r["asset_no"])
            if p and numbering.is_ows_band(p[1]):
                protected = "band"
            elif "category" in shadow and str(shadow["category"]) != str(r["category_id"]):
                protected = "shadow"
        out.append({
            "id": r["id"], "assetNo": r["asset_no"], "fromId": r["category_id"], "toId": tgt,
            "from": names.get(r["category_id"], "미지정" if r["category_id"] is None else str(r["category_id"])),
            "to": names.get(tgt) if tgt else None, "basis": basis, "status": r["status"],
            "division": r["division"] or "sale", "locked": bool(r["tms_lock"]),
            "gone": r["status"] in GONE_STATUSES, "protected": protected, "inTms": row is not None,
        })
    return out, names


def _is_change(d):
    return d["toId"] is not None and d["toId"] != d["fromId"]


def _selected(decisions, include_gone, include_locked, protected_ids=(), only_ids=None):
    """이번에 바꿀 자산 — 보호는 개별 포함(protected_ids)만, 잠금·재고 이탈은 체크 둘로."""
    for d in decisions:
        if not _is_change(d):
            continue
        if only_ids is not None and d["id"] not in only_ids:
            continue
        if d["protected"] and d["id"] not in protected_ids:
            continue
        if d["locked"] and not include_locked:
            continue
        if d["gone"] and not include_gone:
            continue
        yield d


def _summary(decisions, include_gone, include_locked, protected_ids=()):
    """(현재→목표)별 건수표 + 합계 + before/after(예상). 묶음은 서로 겹치지 않는다(보호 > 잠금 > 출고 순)."""
    sel = {d["id"] for d in _selected(decisions, include_gone, include_locked, protected_ids)}
    tot = {"assets": len(decisions), "inTms": 0, "change": 0, "same": 0, "undecided": 0,
           "protected": 0, "locked": 0, "gone": 0, "willApply": len(sel)}
    before = Counter(d["from"] for d in decisions)
    after = Counter(before)
    changes = {}
    for d in decisions:
        if d["inTms"]:
            tot["inTms"] += 1
        if d["toId"] is None:
            tot["undecided"] += 1
            continue
        if d["toId"] == d["fromId"]:
            tot["same"] += 1
            continue
        tot["change"] += 1
        c = changes.setdefault((d["from"], d["to"]), {
            "from": d["from"], "to": d["to"], "count": 0, "alive": 0, "gone": 0, "lockedAlive": 0,
            "lockedGone": 0, "protected": 0, "willApply": 0,
            "statuses": Counter(), "divisions": Counter(), "basis": Counter()})
        c["count"] += 1
        c["statuses"][d["status"]] += 1
        c["divisions"][d["division"]] += 1
        c["basis"][d["basis"] or ""] += 1
        if d["protected"]:
            c["protected"] += 1
            tot["protected"] += 1
        elif d["locked"]:
            c["lockedGone" if d["gone"] else "lockedAlive"] += 1
        else:
            c["gone" if d["gone"] else "alive"] += 1
        if d["locked"]:
            tot["locked"] += 1
        if d["gone"]:
            tot["gone"] += 1
        if d["id"] in sel:
            c["willApply"] += 1
            after[d["from"]] -= 1
            after[d["to"]] += 1
    rows = []
    for c in sorted(changes.values(), key=lambda x: -x["count"]):
        rows.append({**c, "statuses": dict(c["statuses"]), "divisions": dict(c["divisions"]),
                     "basis": dict(c["basis"])})
    return {"totals": tot, "changes": rows, "before": dict(before),
            "after": {k: v for k, v in after.items() if v}}


def _brief(d):
    return {"id": d["id"], "assetNo": d["assetNo"], "from": d["from"], "to": d["to"], "basis": d["basis"],
            "status": d["status"], "division": d["division"], "locked": d["locked"], "gone": d["gone"],
            "protected": d["protected"], "protectedLabel": PROTECT_LABELS.get(d["protected"] or "", "")}


def _last_apply(conn):
    row = conn.execute("SELECT id, ts, username, detail FROM audit_log WHERE action=? ORDER BY id DESC LIMIT 1",
                       (AUDIT_APPLY,)).fetchone()
    if row is None:
        return None
    try:
        d = json.loads(row["detail"] or "{}") or {}
    except ValueError:
        d = {}
    return {"id": row["id"], "ts": row["ts"], "by": row["username"], "applied": d.get("applied", 0),
            "undoneAt": d.get("undoneAt"), "restored": d.get("restored")}


# ---------------------------------------------------------------- 라우트
@bp.get("/assets/reclass/preview")
def reclass_preview():
    """저장 없이 계산만. ?includeShipped=1&includeLocked=0 — 화면은 서버가 준 묶음으로 체크 상태를 바로 다시 센다."""
    require("purchase.view")
    include_gone = _flag(request.args.get("includeShipped"), True)
    include_locked = _flag(request.args.get("includeLocked"), False)
    tms, basis = _tms_rows()                                     # ★창구 호출은 tx 밖
    with tx() as conn:
        decisions, _names = _decide(conn, tms)
        summ = _summary(decisions, include_gone, include_locked)
        cat_order = [r["name"] for r in conn.execute("SELECT name FROM categories ORDER BY sort, id").fetchall()]
        last = _last_apply(conn)
    protected = [d for d in decisions if _is_change(d) and d["protected"]]
    return jsonify({
        "basis": basis, "tmsRows": len(tms) if tms else 0,
        "warning": None if basis == "datalink" else MASTER_ONLY_WARNING,
        "includeShipped": include_gone, "includeLocked": include_locked,
        "categories": cat_order, **summ,
        "protected": [_brief(d) for d in protected[:PROTECTED_LIMIT]], "protectedCount": len(protected),
        "sample": [_brief(d) for d in decisions if _is_change(d) and not d["protected"]][:SAMPLE],
        "undecidedSample": [_brief(d) for d in decisions if d["toId"] is None][:SAMPLE],
        "lastApply": last,
    })


@bp.post("/assets/reclass/apply")
def reclass_apply():
    """{includeShipped, includeLocked, protectedIds?: [id], ids?: [id]} — 적용 직전 재계산(멱등) + compare-and-set."""
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    include_gone = _flag(body.get("includeShipped"), True)
    include_locked = _flag(body.get("includeLocked"), False)
    protected_ids = _id_set(body.get("protectedIds"), "보호 자산 ID")
    only_ids = _id_set(body.get("ids"), "자산 ID") if body.get("ids") is not None else None
    tms, basis = _tms_rows()                                     # ★창구 호출은 tx 밖
    ts = config.now_iso()
    with tx(write=True) as conn:
        decisions, _names = _decide(conn, tms)                   # ★적용 직전 재계산 — 두 번 눌러도 0건
        # 감사 기록을 먼저 만들어 id 를 얻는다 — 자산 이력이 이 id 를 물고 되돌리기의 열쇠가 된다
        audit.log(AUDIT_APPLY, target="적용 중", detail={"basis": basis})
        audit_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        applied = Counter()
        n = skipped = 0
        for d in _selected(decisions, include_gone, include_locked, protected_ids, only_ids):
            rc = conn.execute(
                "UPDATE assets SET category_id=?, updated_at=? WHERE id=? AND category_id IS ?",
                (d["toId"], ts, d["id"], d["fromId"])).rowcount
            if not rc:
                skipped += 1
                continue
            asset_event(conn, d["id"], EVENT_APPLY,
                        {"from": d["from"], "to": d["to"], "근거": d["basis"],
                         "ids": [d["fromId"], d["toId"]], "audit": audit_id})
            set_shadow_category(conn, d["id"], d["toId"])
            applied[(d["from"], d["to"])] += 1
            n += 1
        before = Counter(d["from"] for d in decisions)
        after = Counter(before)
        for (f, t), c in applied.items():
            after[f] -= c
            after[t] += c
        detail = {"applied": n, "skipped": skipped, "basis": basis,
                  "includeShipped": include_gone, "includeLocked": include_locked,
                  "protectedIncluded": len(protected_ids), "restrictedTo": len(only_ids) if only_ids is not None else None,
                  "changes": [{"from": f, "to": t, "count": c} for (f, t), c in applied.most_common()],
                  "before": dict(before), "after": {k: v for k, v in after.items() if v}}
        conn.execute("UPDATE audit_log SET target=?, detail=? WHERE id=?",
                     (f"{n}대", json.dumps(detail, ensure_ascii=False), audit_id))
    return jsonify({"ok": True, "auditId": audit_id, **detail})


@bp.post("/assets/reclass/undo")
def reclass_undo():
    """{auditId} — 그 적용의 이력 '분류재판정'을 ids 로 역적용. 지금 값이 후ID 일 때만(그 뒤 사람이 바꾼 건 건너뜀)."""
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    audit_id = _int_or_400(body.get("auditId"), "적용 기록 ID")
    ts = config.now_iso()
    with tx(write=True) as conn:
        row = conn.execute("SELECT id, detail FROM audit_log WHERE id=? AND action=?",
                           (audit_id, AUDIT_APPLY)).fetchone()
        if row is None:
            abort(404, description="그 적용 기록이 없습니다.")
        try:
            detail = json.loads(row["detail"] or "{}") or {}
        except ValueError:
            detail = {}
        if detail.get("undoneAt"):
            abort(409, description="이미 되돌린 적용입니다(%s)." % str(detail["undoneAt"])[:16])
        # ★detail 은 우리가 직렬화한 모양 그대로다 — "audit" 가 마지막 키라 '…, "audit": N}' 로 끝난다(1234 가 234 에 안 걸림)
        events = conn.execute(
            "SELECT id, asset_id, detail FROM asset_events WHERE action=? AND detail LIKE ? ORDER BY id",
            (EVENT_APPLY, '%"audit": ' + str(audit_id) + "}")).fetchall()
        restored = skipped = 0
        for e in events:
            try:
                d = json.loads(e["detail"] or "{}") or {}
            except ValueError:
                continue
            if d.get("audit") != audit_id or not isinstance(d.get("ids"), list) or len(d["ids"]) != 2:
                continue
            from_id, to_id = d["ids"]
            rc = conn.execute(
                "UPDATE assets SET category_id=?, updated_at=? WHERE id=? AND category_id IS ?",
                (from_id, ts, e["asset_id"], to_id)).rowcount
            if not rc:
                skipped += 1
                continue
            asset_event(conn, e["asset_id"], EVENT_UNDO, {"from": d.get("to"), "to": d.get("from"), "audit": audit_id})
            set_shadow_category(conn, e["asset_id"], from_id)
            restored += 1
        detail.update(undoneAt=ts, undoneBy=g.user["display_name"], restored=restored, undoSkipped=skipped)
        conn.execute("UPDATE audit_log SET detail=? WHERE id=?", (json.dumps(detail, ensure_ascii=False), audit_id))
        audit.log(AUDIT_UNDO, target=f"{restored}대", detail={"auditId": audit_id, "restored": restored, "skipped": skipped})
    return jsonify({"ok": True, "auditId": audit_id, "restored": restored, "skipped": skipped})
