"""제공 옵션 — 「이 쇼핑몰 주문에는 이것도 챙겨야 한다」를 우리가 직접 지정한다.

왜 필요한가(2026-07-29 대표 요청):
고도몰은 상품에 옵션(리브레오피스 설치·리커버리 복구 영역)을 걸 수 있어서 주문에 그 내용이
실려 온다. 그런데 카카오쇼핑 같은 곳은 옵션 자체를 만들 수 없어서, 우리가 실제로는 제공하는
옵션인데 주문서에는 아무 표시도 없이 들어온다. 셋팅·QC 담당자는 화면에 안 보이니 그냥
넘어가고, 고객은 받아 보고 나서야 "리브레오피스가 없다"고 한다.

그래서 몰이 알려주지 않는 옵션을 **우리가 규칙으로 붙여서** 작업 화면에 띄운다.
담당자는 하나씩 체크하며 챙기고, 다 체크해야 제작 완료로 넘어간다.

설계 메모
- 규칙은 「쇼핑몰 + 조건」 두 축이다. 몰 단위가 기본이고(카카오쇼핑 전체),
  필요하면 상품코드·상품명·옵션명에 특정 문자열이 들어간 주문만 걸 수 있다.
- 규칙은 몇 개 되지 않는다(수십 개 수준). 그래서 주문마다 SQL을 던지지 않고
  규칙을 한 번 읽어 파이썬에서 맞춰 본다 — 셋팅 목록 100건에 N+1을 만들지 않기 위해서다.
- 체크 기록(order_option_checks)은 옵션이 규칙에서 빠져도 지우지 않는다.
  규칙을 잠깐 껐다 켜면 담당자가 이미 챙긴 것을 다시 체크하게 되기 때문이다.
"""
import json
import sqlite3

from flask import Blueprint, abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require, require_any
from ..db import get_db, tx

bp = Blueprint("prep", __name__, url_prefix="/api")

# 조건 종류 — 화면 라벨과 함께 정의해 둔다(프론트가 문자열을 따로 갖지 않게)
MATCH_TYPES = {
    "all": "이 쇼핑몰의 모든 주문",
    "code": "상품코드에 포함",
    "product": "상품명에 포함",
    "option": "옵션명에 포함",
}

MAX_OPTIONS = 60          # 체크 목록이 이보다 길어지면 작업 화면이 못 쓰게 된다
MAX_RULES_PER_OPTION = 30


# ---------------------------------------------------------------- 매칭

def load_rules(conn):
    """활성 옵션 + 활성 규칙을 한 번에 읽는다. 요청당 1회 호출을 전제로 한다."""
    opts = conn.execute(
        "SELECT id, name, note, kind FROM prep_options WHERE enabled=1 ORDER BY sort, id"
    ).fetchall()
    if not opts:
        return [], {}
    rules = conn.execute(
        "SELECT option_id, channel, match_type, match_value FROM prep_option_rules "
        "WHERE enabled=1"
    ).fetchall()
    by_option = {}
    for r in rules:
        by_option.setdefault(r["option_id"], []).append(r)
    return opts, by_option


def _rule_hits(rule, row):
    """규칙 하나가 주문에 걸리는가."""
    channel = (rule["channel"] or "").strip()
    if channel and (row["channel"] or "").strip() != channel:
        return False
    mtype = rule["match_type"] or "all"
    if mtype == "all":
        return True
    value = (rule["match_value"] or "").strip().lower()
    if not value:
        # 조건을 고르고 값을 비워 두면 '모든 주문'이 돼 버린다 — 그건 사고다.
        return False
    field = {
        "code": row["product_code"],
        "product": row["product_name"],
        "option": row["option_name"],
    }.get(mtype, "")
    return value in (field or "").lower()


def match_option_ids(opts, by_option, row):
    """이 주문에 붙는 옵션 id 목록(정의 순서 유지)."""
    out = []
    for o in opts:
        for rule in by_option.get(o["id"], ()):
            if _rule_hits(rule, row):
                out.append(o["id"])
                break
    return out


# 판매 구성 칩을 쓰는 채널(2026-08-14 대표 "쿠팡 진행") — 옵션 없이 제목이 옵션표인 곳.
# ★고도몰처럼 옵션 문구가 따로 오는 채널에 켜면 옵션 칩과 이중 기입 위험이 있다 —
#   넓힐 때는 채널의 옵션 구조를 확인하고 여기에만 추가한다.
CONFIG_CHIP_CHANNELS = ("쿠팡",)


def _config_chips_for(conn, rows, config_opts, spec_cache):
    """주문별 '판매 구성' 칩 — 제목 파싱 구성이 코드 기준과 다를 때만 붙는다.
    {order_id: [(옵션행, 동적 라벨), …]}"""
    from ..malls.collect import parse_code_caps
    out = {}
    for r in rows:
        if (r["channel"] or "") not in CONFIG_CHIP_CHANNELS:
            continue
        code = (r["product_code"] or "").strip()
        if not code:
            continue
        if code not in spec_cache:
            spec_cache[code] = conn.execute(
                "SELECT * FROM code_specs WHERE code=?", (code,)).fetchone()
        spec = spec_cache[code]
        if spec is None:
            continue
        text = " ".join(x for x in (r["product_name"], r["option_name"]) if x)
        sold_gb, sold_cap = parse_code_caps(text)
        items = []
        for o in config_opts:
            if o["kind"] == "config_ram":
                if not (sold_gb and spec["ram_gb"]
                        and (spec["ram_gen"] or "").startswith("DDR")):
                    continue
                if sold_gb == spec["ram_gb"]:
                    continue
                label = f"판매 구성: 램 {sold_gb}GB (기준 {spec['ram_gb']}GB)"
            elif o["kind"] == "config_storage":
                if not (sold_cap and spec["storage_cap"] and spec["storage_type"]):
                    continue
                if sold_cap == spec["storage_cap"]:
                    continue
                label = f"판매 구성: 저장장치 {sold_cap} (기준 {spec['storage_cap']})"
            else:
                continue
            items.append((o, label))
        if items:
            out[r["id"]] = items
    return out


def options_for_orders(conn, rows):
    """주문 여러 건의 옵션 + 체크 상태를 한 번에. {order_id: [옵션…]}

    규칙 기반 옵션에 더해, 쿠팡처럼 제목이 옵션표인 채널에는 '판매 구성' 칩을
    동적으로 붙인다(제목 파싱 구성 ≠ 코드 기준일 때만 — 같으면 칩 자체가 없다).
    """
    if not rows:
        return {}
    opts, by_option = load_rules(conn)
    config_opts = [o for o in opts if (o["kind"] or "").strip()]
    rule_opts = [o for o in opts if not (o["kind"] or "").strip()]
    if not opts:
        return {r["id"]: [] for r in rows}
    by_id = {o["id"]: o for o in rule_opts}

    matched = {r["id"]: match_option_ids(rule_opts, by_option, r) for r in rows}
    spec_cache = {}
    config_matched = (_config_chips_for(conn, rows, config_opts, spec_cache)
                      if config_opts else {})
    order_ids = [oid for oid in matched
                 if matched[oid] or config_matched.get(oid)]
    checks = {}
    if order_ids:
        # SQLite 변수 상한(999)을 넘지 않게 나눠 조회한다
        for i in range(0, len(order_ids), 400):
            chunk = order_ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for c in conn.execute(
                    f"SELECT order_id, option_id, checked_by, checked_at "
                    f"FROM order_option_checks WHERE order_id IN ({marks})", chunk).fetchall():
                checks[(c["order_id"], c["option_id"])] = c

    out = {}
    for r in rows:
        oid = r["id"]
        items = []
        for opt_id in matched.get(oid, []):
            o = by_id[opt_id]
            c = checks.get((oid, opt_id))
            items.append({
                "id": opt_id,
                "name": o["name"],
                "note": o["note"],
                "checked": c is not None,
                "checkedBy": c["checked_by"] if c else "",
                "checkedAt": c["checked_at"] if c else "",
            })
        for o, label in config_matched.get(oid, []):
            c = checks.get((oid, o["id"]))
            items.append({
                "id": o["id"],
                "name": label,                    # 동적 라벨 — 주문마다 내용이 다르다
                "note": o["note"],
                "checked": c is not None,
                "checkedBy": c["checked_by"] if c else "",
                "checkedAt": c["checked_at"] if c else "",
            })
        out[oid] = items
    return out


def options_for_order(conn, row):
    return options_for_orders(conn, [row]).get(row["id"], [])


def unchecked_for_order(conn, row):
    """아직 안 챙긴 옵션 이름 목록 — 제작 완료를 막는 근거."""
    return [o["name"] for o in options_for_order(conn, row) if not o["checked"]]


# ---------------------------------------------------------------- 설정 API

def _option_payload(conn, row):
    rules = conn.execute(
        "SELECT id, channel, match_type, match_value, enabled FROM prep_option_rules "
        "WHERE option_id=? ORDER BY id", (row["id"],)
    ).fetchall()
    # 연결 부품(있으면) — 화면이 "이 옵션 체크 = 원가 자동 기입"임을 보여줄 수 있게
    part = None
    if row["part_id"]:
        p = conn.execute("SELECT id, name, price, enabled FROM parts WHERE id=?",
                         (row["part_id"],)).fetchone()
        if p:
            part = {"id": p["id"], "name": p["name"], "price": p["price"],
                    "enabled": bool(p["enabled"])}
    # 구분별 연결(2026-08-13) — DDR4/DDR5·NVMe/SATA에 따라 다른 부품이 들어간다
    part_map = []
    try:
        raw_pm = json.loads(row["part_map"] or "") or []
    except ValueError:
        raw_pm = []
    for e in raw_pm:
        if not isinstance(e, dict):
            continue
        p = conn.execute("SELECT id, name, price, enabled FROM parts WHERE id=?",
                         (e.get("partId"),)).fetchone()
        part_map.append({"gen": e.get("gen"), "partId": e.get("partId"),
                         "part": ({"id": p["id"], "name": p["name"], "price": p["price"],
                                   "enabled": bool(p["enabled"])} if p else None)})
    return {
        "id": row["id"],
        "name": row["name"],
        "note": row["note"],
        "enabled": bool(row["enabled"]),
        "sort": row["sort"],
        "kind": (row["kind"] or "") if "kind" in row.keys() else "",
        "partId": row["part_id"],
        "partQty": row["part_qty"] or 1,
        "part": part,
        "partMap": part_map,
        "rules": [
            {
                "id": r["id"],
                "channel": r["channel"],
                "matchType": r["match_type"],
                "matchValue": r["match_value"],
                "enabled": bool(r["enabled"]),
                "label": _rule_label(r),
            }
            for r in rules
        ],
    }


def _rule_label(r):
    where = r["channel"] or "모든 쇼핑몰"
    if (r["match_type"] or "all") == "all":
        return f"{where} · 모든 주문"
    return f"{where} · {MATCH_TYPES.get(r['match_type'], r['match_type'])} '{r['match_value']}'"


@bp.get("/prep-options")
def list_options():
    """옵션 목록. 설정 담당은 편집하려고, 셋팅 담당은 무엇이 있는지 보려고 부른다."""
    require_any("settings.manage", "orders.work", "orders.view")
    conn = get_db()
    rows = conn.execute("SELECT * FROM prep_options ORDER BY sort, id").fetchall()
    return jsonify({
        "options": [_option_payload(conn, r) for r in rows],
        "matchTypes": [{"value": k, "label": v} for k, v in MATCH_TYPES.items()],
    })


@bp.post("/prep-options")
def create_option():
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        abort(400, description="옵션 이름을 입력하세요. (예: 리브레오피스 설치)")
    if len(name) > 60:
        abort(400, description="옵션 이름이 너무 깁니다(60자 이내).")
    note = (body.get("note") or "").strip()
    with tx(write=True) as conn:
        if conn.execute("SELECT COUNT(*) AS c FROM prep_options").fetchone()["c"] >= MAX_OPTIONS:
            abort(400, description=f"옵션은 최대 {MAX_OPTIONS}개까지 만들 수 있습니다.")
        dup = conn.execute("SELECT id FROM prep_options WHERE name=?", (name,)).fetchone()
        if dup:
            abort(409, description="같은 이름의 옵션이 이미 있습니다.")
        max_sort = conn.execute(
            "SELECT COALESCE(MAX(sort), -1) AS m FROM prep_options").fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO prep_options(name, note, enabled, sort, created_at, created_by) "
            "VALUES(?,?,1,?,?,?)",
            (name, note, max_sort + 1, config.now_iso(), g.user["display_name"]))
        audit.log("prep_option_created", target=name)
        row = conn.execute("SELECT * FROM prep_options WHERE id=?", (cur.lastrowid,)).fetchone()
        payload = _option_payload(conn, row)
    return jsonify(payload), 201


@bp.patch("/prep-options/<int:opt_id>")
def update_option(opt_id):
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM prep_options WHERE id=?", (opt_id,)).fetchone()
        if row is None:
            abort(404, description="옵션을 찾을 수 없습니다.")
        changes = {}
        if "name" in body:
            name = (body.get("name") or "").strip()
            if not name:
                abort(400, description="옵션 이름을 입력하세요.")
            dup = conn.execute(
                "SELECT id FROM prep_options WHERE name=? AND id != ?", (name, opt_id)).fetchone()
            if dup:
                abort(409, description="같은 이름의 옵션이 이미 있습니다.")
            conn.execute("UPDATE prep_options SET name=? WHERE id=?", (name, opt_id))
            changes["name"] = name
        if "note" in body:
            conn.execute("UPDATE prep_options SET note=? WHERE id=?",
                         ((body.get("note") or "").strip(), opt_id))
            changes["note"] = True
        if "enabled" in body:
            conn.execute("UPDATE prep_options SET enabled=? WHERE id=?",
                         (1 if body["enabled"] else 0, opt_id))
            changes["enabled"] = bool(body["enabled"])
        if "sort" in body:
            try:
                sort_val = int(body["sort"])
            except (TypeError, ValueError):
                abort(400, description="정렬 값이 올바르지 않습니다.")
            conn.execute("UPDATE prep_options SET sort=? WHERE id=?", (sort_val, opt_id))
            changes["sort"] = sort_val
        # 부품 연결(2026-08-10) — 이 옵션 체크 = 매칭 자산에 우리 매입 단가 자동 기입
        if "partId" in body:
            pid = body.get("partId")
            if pid in (None, "", 0):
                conn.execute("UPDATE prep_options SET part_id=NULL WHERE id=?", (opt_id,))
                changes["partId"] = None
            else:
                try:
                    pid = int(pid)
                except (TypeError, ValueError):
                    abort(400, description="부품 선택이 올바르지 않습니다.")
                if conn.execute("SELECT id FROM parts WHERE id=?", (pid,)).fetchone() is None:
                    abort(400, description="선택한 부품이 단가표에 없습니다.")
                conn.execute("UPDATE prep_options SET part_id=? WHERE id=?", (pid, opt_id))
                changes["partId"] = pid
        if "partQty" in body:
            try:
                pq = max(1, min(int(body.get("partQty") or 1), 10))
            except (TypeError, ValueError):
                abort(400, description="부품 수량이 올바르지 않습니다.")
            conn.execute("UPDATE prep_options SET part_qty=? WHERE id=?", (pq, opt_id))
            changes["partQty"] = pq
        if "partMap" in body:
            # 구분별 연결(2026-08-13): [{gen:"DDR4", partId:3}, ...] — 제품코드 스펙이
            # 어느 구분인지에 따라 다른 부품이 기입된다. 빈 목록 = 구분별 연결 해제.
            pm = body.get("partMap")
            if not pm:
                conn.execute("UPDATE prep_options SET part_map='' WHERE id=?", (opt_id,))
                changes["partMap"] = []
            else:
                from ..purchase import PART_GEN_AXES
                if not isinstance(pm, list):
                    abort(400, description="구분별 연결 형식이 올바르지 않습니다.")
                clean, seen = [], set()
                for e in pm:
                    gen = (e.get("gen") or "").strip() if isinstance(e, dict) else ""
                    if gen not in PART_GEN_AXES:
                        abort(400, description=f"구분 '{gen}'은 지원하지 않습니다.")
                    if gen in seen:
                        abort(400, description=f"구분 '{gen}'이 두 번 연결돼 있습니다.")
                    try:
                        pid2 = int(e.get("partId"))
                    except (TypeError, ValueError):
                        abort(400, description=f"'{gen}' 구분의 부품 선택이 올바르지 않습니다.")
                    if conn.execute("SELECT id FROM parts WHERE id=?", (pid2,)).fetchone() is None:
                        abort(400, description=f"'{gen}' 구분에 연결하려는 부품이 단가표에 없습니다.")
                    seen.add(gen)
                    clean.append({"gen": gen, "partId": pid2})
                conn.execute("UPDATE prep_options SET part_map=? WHERE id=?",
                             (json.dumps(clean, ensure_ascii=False), opt_id))
                changes["partMap"] = clean
        if not changes:
            abort(400, description="변경할 항목이 없습니다.")
        audit.log("prep_option_updated", target=row["name"], detail=changes)
        row = conn.execute("SELECT * FROM prep_options WHERE id=?", (opt_id,)).fetchone()
        payload = _option_payload(conn, row)
    return jsonify(payload)


@bp.delete("/prep-options/<int:opt_id>")
def delete_option(opt_id):
    """삭제하면 지금까지의 체크 기록도 함께 사라진다 — 화면에서 한 번 더 확인받는다.

    잠시 안 쓸 거라면 [사용 안 함]으로 꺼 두는 쪽이 안전하다.
    """
    require("settings.manage")
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM prep_options WHERE id=?", (opt_id,)).fetchone()
        if row is None:
            abort(404, description="옵션을 찾을 수 없습니다.")
        if (row["kind"] or "").strip():
            abort(400, description="시스템 옵션(판매 구성)은 삭제할 수 없습니다 — "
                                   "안 쓰려면 [사용 안 함]으로 꺼 두세요.")
        used = conn.execute(
            "SELECT COUNT(*) AS c FROM order_option_checks WHERE option_id=?", (opt_id,)
        ).fetchone()["c"]
        conn.execute("DELETE FROM prep_options WHERE id=?", (opt_id,))
        audit.log("prep_option_deleted", target=row["name"], detail={"체크기록": used})
    return jsonify({"ok": True, "removedChecks": used})


# ---------------------------------------------------------------- 규칙

def _valid_rule_body(body):
    channel = (body.get("channel") or "").strip()
    mtype = (body.get("matchType") or "all").strip()
    if mtype not in MATCH_TYPES:
        abort(400, description="조건 종류가 올바르지 않습니다.")
    value = (body.get("matchValue") or "").strip()
    if mtype != "all" and not value:
        abort(400, description="조건에 넣을 값을 입력하세요. (비워 두면 모든 주문에 걸립니다)")
    if mtype == "all":
        value = ""
    if not channel and mtype == "all":
        # '모든 쇼핑몰의 모든 주문'은 사실상 전 주문 강제다. 실수로 만들기 쉬워 막는다.
        abort(400, description="쇼핑몰을 고르거나 조건을 지정하세요. "
                               "(모든 쇼핑몰 + 모든 주문은 전체 주문에 걸려 작업이 멈춥니다)")
    return channel, mtype, value


@bp.post("/prep-options/<int:opt_id>/rules")
def create_rule(opt_id):
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    channel, mtype, value = _valid_rule_body(body)
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM prep_options WHERE id=?", (opt_id,)).fetchone()
        if row is None:
            abort(404, description="옵션을 찾을 수 없습니다.")
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM prep_option_rules WHERE option_id=?", (opt_id,)
        ).fetchone()["c"]
        if cnt >= MAX_RULES_PER_OPTION:
            abort(400, description=f"규칙은 옵션당 최대 {MAX_RULES_PER_OPTION}개입니다.")
        dup = conn.execute(
            "SELECT id FROM prep_option_rules WHERE option_id=? AND channel=? AND match_type=? "
            "AND match_value=?", (opt_id, channel, mtype, value)).fetchone()
        if dup:
            abort(409, description="같은 조건이 이미 있습니다.")
        conn.execute(
            "INSERT INTO prep_option_rules(option_id, channel, match_type, match_value, enabled, created_at) "
            "VALUES(?,?,?,?,1,?)", (opt_id, channel, mtype, value, config.now_iso()))
        audit.log("prep_rule_created", target=row["name"],
                  detail={"쇼핑몰": channel or "전체", "조건": mtype, "값": value})
        payload = _option_payload(conn, row)
    return jsonify(payload), 201


@bp.patch("/prep-option-rules/<int:rule_id>")
def update_rule(rule_id):
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    with tx(write=True) as conn:
        rule = conn.execute(
            "SELECT * FROM prep_option_rules WHERE id=?", (rule_id,)).fetchone()
        if rule is None:
            abort(404, description="조건을 찾을 수 없습니다.")
        if "enabled" in body and len(body) == 1:
            conn.execute("UPDATE prep_option_rules SET enabled=? WHERE id=?",
                         (1 if body["enabled"] else 0, rule_id))
        else:
            channel, mtype, value = _valid_rule_body(body)
            conn.execute(
                "UPDATE prep_option_rules SET channel=?, match_type=?, match_value=?, enabled=? "
                "WHERE id=?",
                (channel, mtype, value, 1 if body.get("enabled", True) else 0, rule_id))
        opt = conn.execute(
            "SELECT * FROM prep_options WHERE id=?", (rule["option_id"],)).fetchone()
        audit.log("prep_rule_updated", target=opt["name"] if opt else str(rule_id))
        payload = _option_payload(conn, opt) if opt else {"ok": True}
    return jsonify(payload)


@bp.delete("/prep-option-rules/<int:rule_id>")
def delete_rule(rule_id):
    require("settings.manage")
    with tx(write=True) as conn:
        rule = conn.execute(
            "SELECT * FROM prep_option_rules WHERE id=?", (rule_id,)).fetchone()
        if rule is None:
            abort(404, description="조건을 찾을 수 없습니다.")
        conn.execute("DELETE FROM prep_option_rules WHERE id=?", (rule_id,))
        opt = conn.execute(
            "SELECT * FROM prep_options WHERE id=?", (rule["option_id"],)).fetchone()
        audit.log("prep_rule_deleted", target=opt["name"] if opt else str(rule_id))
        payload = _option_payload(conn, opt) if opt else {"ok": True}
    return jsonify(payload)


@bp.post("/prep-options/preview")
def preview_rule():
    """이 조건을 저장하면 '지금 작업 중인 주문' 몇 건에 붙는지 미리 보여준다.

    규칙은 과거 주문에도 소급 적용된다. 제작 완료가 안 된 주문은 그 순간부터
    체크를 요구받으므로, 저장 전에 몇 건이 걸리는지 눈으로 보고 결정해야 한다.
    """
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    channel, mtype, value = _valid_rule_body(body)
    fake = {"channel": channel, "match_type": mtype, "match_value": value}
    conn = get_db()
    rows = conn.execute(
        "SELECT id, channel, product_code, product_name, option_name, recipient, production_done "
        "FROM orders WHERE cancelled_at='' AND archived_at=''"
    ).fetchall()
    hit = [r for r in rows if _rule_hits(fake, r)]
    pending = [r for r in hit if not r["production_done"]]
    return jsonify({
        "total": len(hit),
        "pending": len(pending),
        "samples": [
            {"orderId": r["id"], "channel": r["channel"], "productName": r["product_name"],
             "optionName": r["option_name"], "recipient": r["recipient"]}
            for r in pending[:5]
        ],
    })


# ---------------------------------------------------------------- 체크

# ---------------------------------------------------------------- 자산 라벨 레이아웃
#   (2026-08-27 대표) XP-DT427B 50×80 감열 라벨 — 요소별 위치·크기를 설정에서 조정.
#   인쇄 자체는 화면(qr.js + 브라우저 인쇄)이 한다. 여기는 레이아웃 KV 만.


@bp.get("/asset-label")
def get_asset_label():
    require_any("setup.view", "orders.view", "purchase.view", "settings.manage")

    def _kv_json(key):
        row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        try:
            return json.loads(row["value"]) if row and row["value"] else None
        except (ValueError, TypeError):
            return None

    lrow = get_db().execute("SELECT value FROM settings WHERE key='asset_label_logo'").fetchone()
    # optionLayout = 옵션라벨(2026-08-31 대표 — 셋팅·QC 작업대용 옵션표). 로고는 두 라벨 공용.
    # optionAutoPrint = 셋팅/QC [제작 완료] 체크 시 자동 인쇄(설정 ▸ 라벨 ▸ 옵션 라벨에서
    #   켜고 끔, 전 작업대 공통). 저장 전 기본은 켜짐.
    return jsonify({"layout": _kv_json("asset_label"),
                    "optionLayout": _kv_json("option_label"),
                    "optionAutoPrint": bool((_kv_json("option_label_auto") or {}).get("enabled", True)),
                    "logo": (lrow["value"] if lrow else "") or ""})


@bp.post("/asset-label-logo")
def set_asset_label_logo():
    """라벨 로고(흑백 권장 — 감열이라 회색은 디더링됨). data URL 로 저장, 빈 값 = 제거."""
    require("settings.manage")
    data = str((request.get_json(silent=True) or {}).get("dataUrl") or "")
    if data and not data.startswith("data:image/"):
        abort(400, description="이미지 파일이 아닙니다.")
    if len(data) > 400_000:
        abort(400, description="로고가 너무 큽니다(300KB 이내 이미지로 줄여 주세요).")
    with tx(write=True) as conn:
        conn.execute(
            "INSERT INTO settings(key, value, updated_at, updated_by) "
            "VALUES('asset_label_logo',?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (data, config.now_iso(), g.user["display_name"]))
        audit.log("asset_label_logo_saved", target="자산 라벨 로고",
                  detail={"bytes": len(data), "removed": not data})
    return jsonify({"ok": True})


@bp.post("/asset-label")
def set_asset_label():
    """라벨 레이아웃 저장 — body 에 layout(자산 라벨)·optionLayout(옵션 라벨) 중 온 것만 고친다."""
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    slots = [("layout", "asset_label", "자산 라벨 레이아웃"),
             ("optionLayout", "option_label", "옵션 라벨 레이아웃")]
    todo = []
    for field, kv_key, label in slots:
        if field not in body:
            continue
        layout = body.get(field)
        if not isinstance(layout, dict):
            abort(400, description="레이아웃 값이 올바르지 않습니다.")
        raw = json.dumps(layout, ensure_ascii=False)
        if len(raw) > 8000:
            abort(400, description="레이아웃이 너무 큽니다.")
        todo.append((kv_key, label, raw))
    # 자동 인쇄 켜고 끄기(2026-08-31 대표) — 레이아웃과 같은 화면에서 저장한다
    auto = body.get("optionAutoPrint")
    if auto is not None:
        todo.append(("option_label_auto", "옵션라벨 자동인쇄",
                     json.dumps({"enabled": bool(auto)})))
    if not todo:
        abort(400, description="레이아웃 값이 올바르지 않습니다.")
    with tx(write=True) as conn:
        for kv_key, label, raw in todo:
            conn.execute(
                "INSERT INTO settings(key, value, updated_at, updated_by) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (kv_key, raw, config.now_iso(), g.user["display_name"]))
            audit.log("asset_label_saved", target=label, detail={"bytes": len(raw)})
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 셋팅 필수 참고
#   (2026-08-26 대표) 셋팅하면서 꼭 봐야 할 유의사항 — 보드 상단(부품 수량 아래)에
#   노란 카드로 뜬다. 문구는 설정 ▸ API 관리 ▸ 제공 옵션에서 고친다. 비우면 안 뜬다.


@bp.get("/setup-notice")
def get_setup_notice():
    require_any("setup.view", "orders.view", "settings.manage")
    row = get_db().execute("SELECT value, updated_at, updated_by FROM settings "
                           "WHERE key='setup_notice'").fetchone()
    return jsonify({"text": (row["value"] if row else "") or "",
                    "updatedAt": row["updated_at"] if row else "",
                    "updatedBy": (row["updated_by"] if row else "") or ""})


@bp.post("/setup-notice")
def set_setup_notice():
    require("settings.manage")
    text = str((request.get_json(silent=True) or {}).get("text") or "").strip()
    if len(text) > 2000:
        abort(400, description="참고 사항은 2,000자 이내로 적어 주세요.")
    with tx(write=True) as conn:
        conn.execute(
            "INSERT INTO settings(key, value, updated_at, updated_by) "
            "VALUES('setup_notice',?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (text, config.now_iso(), g.user["display_name"]))
        audit.log("setup_notice_saved", target="셋팅 참고사항",
                  detail={"length": len(text), "empty": not text})
    return jsonify({"ok": True})


@bp.post("/orders/<int:oid>/options/<int:opt_id>")
def toggle_check(oid, opt_id):
    """셋팅·QC 담당자가 '챙겼다'고 표시. 몸으로 하는 일이라 되돌리기도 쉬워야 한다."""
    require_any("orders.work", "orders.edit")
    checked = bool((request.get_json(silent=True) or {}).get("checked", True))
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if row is None:
            abort(404, description="주문을 찾을 수 없습니다.")
        if row["cancelled_at"]:
            abort(409, description="취소된 주문입니다.")
        if row["archived_at"]:
            abort(409, description="보관된 주문입니다.")
        # ★출고된 주문의 체크를 풀면 부품 원가 행이 지워져, 이미 마감된 달의 마진과
        #   세무 대장이 사후에 바뀐다(2026-08-14 검토). 체크는 열어 두되 해제만 막는다
        #   — 잘못 들어간 원가는 자산 상세의 수리 기록에서 사유를 남기고 지운다.
        if row["shipping_done"] and not checked:
            abort(409, description="이미 출고된 주문입니다 — 체크를 풀면 그 자산의 부품 원가가 "
                                   "지워져 지난 달 마진이 바뀝니다. 잘못 들어간 원가는 "
                                   "매입 ▸ 자산 상세의 수리 기록에서 지워 주세요.")
        valid = {o["id"] for o in options_for_order(conn, row)}
        if opt_id not in valid:
            abort(400, description="이 주문에 해당하지 않는 옵션입니다. 화면을 새로고침해 주세요.")
        if checked:
            conn.execute(
                "INSERT INTO order_option_checks(order_id, option_id, checked_by, checked_at) "
                "VALUES(?,?,?,?) ON CONFLICT(order_id, option_id) DO UPDATE SET "
                "checked_by=excluded.checked_by, checked_at=excluded.checked_at",
                (oid, opt_id, g.user["display_name"], config.now_iso()))
        else:
            conn.execute("DELETE FROM order_option_checks WHERE order_id=? AND option_id=?",
                         (oid, opt_id))

        # ── 부품 원가 자동 기입(대표 2026-08-10) ─────────────────────────
        #  "고객이 옵션으로 램/SSD 추가를 골랐으면, 우리 매입 단가가 자동으로 원가에."
        #  체크 = 부품을 실제로 꽂았다는 신호다. 체크하면 매칭된 자산들에 오늘 단가를
        #  기입하고, 해제하면 되돌린다. 금액은 고객 옵션가가 아니라 ★부품 단가표의
        #  우리 매입 단가다. 작업자는 지금 누르던 칩 그대로 누르면 된다 — 추가 입력 0.
        part_applied, part_cost, part_warn = 0, 0, ""
        opt = conn.execute("SELECT * FROM prep_options WHERE id=?", (opt_id,)).fetchone()
        opt_kind = (opt["kind"] or "").strip() if opt else ""
        if opt and opt_kind:
            # ── 판매 구성 칩(쿠팡 — 제목이 옵션표): 자산 실물 기준 회수/장착을 그때 계산
            from ..purchase import (_apply_part_to_asset, revert_option_parts,
                                    sold_config_actions)
            if checked:
                aids = [x["asset_id"] for x in conn.execute(
                    "SELECT asset_id FROM order_assets WHERE order_id=?", (oid,)).fetchall()]
                if not aids:
                    part_warn = "아직 자산이 매칭되지 않았습니다 — 매칭되는 순간 자동 기입됩니다."
                today = config.now().strftime("%Y-%m-%d")
                for aid in aids:
                    a = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
                    if a is None:
                        continue
                    if conn.execute(
                            "SELECT 1 FROM asset_repairs WHERE asset_id=? AND order_id=? "
                            "AND prep_option_id=?", (aid, oid, opt_id)).fetchone():
                        continue                     # 이미 기입됨(멱등)
                    actions, why = sold_config_actions(conn, a, row, opt_kind)
                    if actions is None:
                        part_warn = why
                        continue
                    if not actions:
                        continue                     # 실물이 이미 판매 구성과 같다
                    got = 0
                    for p_, rm in actions:
                        got += _apply_part_to_asset(conn, a, p_, today,
                                                    g.user["display_name"],
                                                    order_id=oid, prep_option_id=opt_id,
                                                    remove=rm, dedup=False)
                        a = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
                    part_applied += 1
                    part_cost += got
                    if why:
                        part_warn = why
            else:
                reverted = revert_option_parts(conn, oid, option_id=opt_id,
                                               actor=g.user["display_name"])
                if reverted:
                    part_warn = f"자동 기입했던 부품 원가 {reverted}건을 되돌렸습니다."
        elif opt and (opt["part_id"] or (opt["part_map"] or "").strip()):
            from ..purchase import (_apply_part_to_asset, resolve_option_part,
                                    revert_option_parts)
            if checked:
                # 구분별 연결이면 주문 제품코드의 스펙(DDR4/DDR5·NVMe/SATA)으로 부품 확정
                part, why = resolve_option_part(conn, opt, row)
                if part is None:
                    part_warn = why or ("연결된 부품을 찾지 못해 원가를 기입하지 못했습니다 — "
                                        "매입 ▸ 기준정보 ▸ 부품 단가표를 확인하세요.")
                elif not part["price"]:
                    part_warn = ("연결된 부품의 단가가 없어 원가를 기입하지 못했습니다 — "
                                 "매입 ▸ 기준정보 ▸ 부품 단가표를 확인하세요.")
                else:
                    aids = [r["asset_id"] for r in conn.execute(
                        "SELECT asset_id FROM order_assets WHERE order_id=?", (oid,)).fetchall()]
                    today = config.now().strftime("%Y-%m-%d")
                    for aid in aids:
                        a = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
                        if a is None:
                            continue
                        got = _apply_part_to_asset(conn, a, part, today, g.user["display_name"],
                                                   qty=opt["part_qty"] or 1,
                                                   order_id=oid, prep_option_id=opt_id)
                        if got:
                            part_applied += 1
                            part_cost += got
                    if not aids:
                        part_warn = "아직 자산이 매칭되지 않았습니다 — 매칭되는 순간 자동 기입됩니다."
            else:
                reverted = revert_option_parts(conn, oid, option_id=opt_id,
                                               actor=g.user["display_name"])
                if reverted:
                    part_warn = f"자동 기입했던 부품 원가 {reverted}건을 되돌렸습니다."
        items = options_for_order(conn, row)
    return jsonify({
        "options": items,
        "remaining": [o["name"] for o in items if not o["checked"]],
        "partApplied": part_applied, "partCost": part_cost,
        "partMessage": part_warn,
    })
