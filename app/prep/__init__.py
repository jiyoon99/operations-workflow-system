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
        "SELECT id, name, note FROM prep_options WHERE enabled=1 ORDER BY sort, id"
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


def options_for_orders(conn, rows):
    """주문 여러 건의 옵션 + 체크 상태를 한 번에. {order_id: [옵션…]}"""
    if not rows:
        return {}
    opts, by_option = load_rules(conn)
    if not opts:
        return {r["id"]: [] for r in rows}
    by_id = {o["id"]: o for o in opts}

    matched = {r["id"]: match_option_ids(opts, by_option, r) for r in rows}
    order_ids = [oid for oid, ids in matched.items() if ids]
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
    for oid, ids in matched.items():
        items = []
        for opt_id in ids:
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
    return {
        "id": row["id"],
        "name": row["name"],
        "note": row["note"],
        "enabled": bool(row["enabled"]),
        "sort": row["sort"],
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
        items = options_for_order(conn, row)
    return jsonify({
        "options": items,
        "remaining": [o["name"] for o in items if not o["checked"]],
    })
