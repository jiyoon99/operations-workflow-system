"""설정 블루프린트 — 사용자/권한/카테고리/감사로그/시스템 설정/백업.

대표가 여기서 직접 계정을 만들고 권한·카테고리 스코프를 부여한다(2026-07-28 결정 #3, #4).
"""
import json
import re
import sqlite3

from flask import Blueprint, abort, current_app, g, jsonify, request, send_file

from .. import audit, config
from ..auth import drop_user_sessions, hash_password
from ..auth.perms import (PERM_CODES, PERM_LABELS, registry, require, require_any,
                          user_category_ids, user_perm_set)
from ..db import backup_dir, get_db, list_backups, run_manual_backup, tx

bp = Blueprint("admin", __name__, url_prefix="/api")


# ---------------------------------------------------------------- helpers

def _int_or_400(value, what="값"):
    try:
        return int(value)
    except (TypeError, ValueError):
        abort(400, description=f"{what}이(가) 올바르지 않습니다: {value!r}")


def _money_or_400(value, what="금액"):
    """금액 전용 파서 — 음수를 막는다.

    ★_int_or_400은 부호를 안 본다. 매입가에 '-'가 섞여 들어가면(오타·붙여넣기)
      재고 자산가치가 깎이고 마진이 부풀려지는데, 원인을 화면에서 찾기 어렵다
      (2026-07-30 감사에서 실제로 PATCH -50000이 200으로 저장되는 것을 확인).
    """
    n = _int_or_400(value, what)
    if n < 0:
        abort(400, description=f"{what}은(는) 0보다 작을 수 없습니다: {n}")
    return n


def _validate_category_ids(conn, ids):
    """카테고리 ID 목록 검증 — 비수치/미존재 값은 400 (500 금지)."""
    parsed = sorted({_int_or_400(c, "카테고리 ID") for c in ids})
    valid = {r["id"] for r in conn.execute("SELECT id FROM categories").fetchall()}
    bad = [c for c in parsed if c not in valid]
    if bad:
        abort(400, description="존재하지 않는 카테고리가 포함되어 있습니다.")
    return parsed


def _get_user_or_404(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        abort(404, description="사용자를 찾을 수 없습니다.")
    return row


def _require_admin_for_admin_target(target_row):
    """관리자 계정을 건드리는 작업은 관리자만 가능(권한 상승 차단)."""
    if target_row["is_admin"] and not g.user["is_admin"]:
        abort(403, description="관리자 계정은 관리자만 수정할 수 있습니다.")


def _check_grantable(perms, target_user_id=None):
    """부여하려는 권한이 허용 범위인지 검사 — 권한 상승 차단.

    관리자가 아니면 ①자기 계정의 권한은 못 고치고 ②자신이 가진 권한만 남에게 줄 수 있다.
    (이게 없으면 users.manage 하나로 스스로에게 전 권한을 줘서 사실상 관리자가 된다.)
    """
    if g.user["is_admin"]:
        return
    if target_user_id is not None and int(target_user_id) == int(g.user["id"]):
        abort(403, description="본인 계정의 권한은 스스로 바꿀 수 없습니다. 관리자에게 요청하세요.")
    mine = user_perm_set(g.user["id"])
    over = sorted(set(perms) - mine)
    if over:
        labels = ", ".join(PERM_LABELS.get(p, p) for p in over)
        abort(403, description=f"본인이 가지지 않은 권한은 부여할 수 없습니다: {labels}")


def _assert_can_reset_password(target_user_id):
    """비밀번호 재설정도 권한 상승 통로다 — _check_grantable과 같은 잣대를 댄다.

    나보다 권한이 많은 계정의 비밀번호를 바꿀 수 있으면, 그 계정으로 로그인해서
    '내가 못 주는 권한'을 그대로 쓰게 된다(권한 부여 검사를 우회하는 뒷문).
    본인 비밀번호는 현재 비밀번호를 확인하는 /api/auth/password로만 바꾼다.
    """
    if g.user["is_admin"]:
        return
    if int(target_user_id) == int(g.user["id"]):
        abort(403, description="본인 비밀번호는 [비밀번호 변경]에서 현재 비밀번호를 확인한 뒤 바꿔 주세요.")
    over = sorted(user_perm_set(target_user_id) - user_perm_set(g.user["id"]))
    if over:
        labels = ", ".join(PERM_LABELS.get(p, p) for p in over)
        abort(403, description=f"본인보다 권한이 많은 계정의 비밀번호는 재설정할 수 없습니다: {labels}")


def _assert_not_last_admin(conn, user_id):
    """마지막 활성 관리자 보호 — 비활성화/강등 불가."""
    others = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE is_admin=1 AND enabled=1 AND id != ?", (user_id,)
    ).fetchone()["c"]
    if others == 0:
        abort(400, description="마지막 관리자 계정은 비활성화하거나 강등할 수 없습니다.")


def _user_row_payload(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "displayName": row["display_name"],
        "isAdmin": bool(row["is_admin"]),
        "allCategories": bool(row["all_categories"]),
        "enabled": bool(row["enabled"]),
        "createdAt": row["created_at"],
        "perms": sorted(user_perm_set(row["id"])),
        "categoryIds": user_category_ids(row["id"]),
    }


# ---------------------------------------------------------------- users

@bp.get("/perm-registry")
def perm_registry():
    require("users.manage")
    return jsonify(registry())


@bp.get("/users")
def list_users():
    require("users.manage")
    rows = get_db().execute("SELECT * FROM users ORDER BY id").fetchall()
    return jsonify([_user_row_payload(r) for r in rows])


@bp.post("/users")
def create_user():
    require("users.manage")
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    display_name = (body.get("displayName") or "").strip() or username
    password = body.get("password") or ""
    is_admin = bool(body.get("isAdmin"))
    if len(username) < 3 or len(password) < 8:
        abort(400, description="아이디는 3자 이상, 비밀번호는 8자 이상이어야 합니다.")
    if is_admin and not g.user["is_admin"]:
        abort(403, description="관리자 계정은 관리자만 만들 수 있습니다.")
    perms = body.get("perms") or []
    if not isinstance(perms, list):
        abort(400, description="perms는 배열이어야 합니다.")
    bad = [p for p in perms if p not in PERM_CODES]
    if bad:
        abort(400, description=f"알 수 없는 권한 코드: {', '.join(map(str, bad))}")
    _check_grantable(perms)
    category_ids = body.get("categoryIds") or []
    if not isinstance(category_ids, list):
        abort(400, description="categoryIds는 배열이어야 합니다.")
    with tx(write=True) as conn:
        dup = conn.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if dup:
            abort(409, description="이미 존재하는 아이디입니다.")
        cat_ids = _validate_category_ids(conn, category_ids)
        ts = config.now_iso()
        cur = conn.execute(
            "INSERT INTO users(username, display_name, pw_hash, is_admin, all_categories, enabled, created_at, updated_at) "
            "VALUES(?,?,?,?,?,1,?,?)",
            (username, display_name, hash_password(password),
             1 if is_admin else 0, 1 if body.get("allCategories") else 0, ts, ts),
        )
        user_id = cur.lastrowid
        for p in sorted(set(perms)):
            conn.execute("INSERT INTO user_perms(user_id, perm) VALUES(?,?)", (user_id, p))
        for cid in cat_ids:
            conn.execute(
                "INSERT INTO user_categories(user_id, category_id) VALUES(?,?)", (user_id, cid)
            )
        audit.log("user_created", target=username,
                  detail={"isAdmin": is_admin, "perms": sorted(set(perms))})
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return jsonify(_user_row_payload(row)), 201


@bp.patch("/users/<int:user_id>")
def update_user(user_id):
    require("users.manage")
    body = request.get_json(silent=True) or {}
    with tx(write=True) as conn:
        row = _get_user_or_404(conn, user_id)
        _require_admin_for_admin_target(row)
        changes = {}

        if "displayName" in body:
            name = (body.get("displayName") or "").strip()
            if not name:
                abort(400, description="표시 이름을 입력하세요.")
            conn.execute("UPDATE users SET display_name=? WHERE id=?", (name, user_id))
            changes["displayName"] = name

        if "enabled" in body:
            enabled = bool(body["enabled"])
            if not enabled and row["is_admin"]:
                _assert_not_last_admin(conn, user_id)
            conn.execute("UPDATE users SET enabled=? WHERE id=?", (1 if enabled else 0, user_id))
            if not enabled:
                drop_user_sessions(conn, user_id)
            changes["enabled"] = enabled

        if "isAdmin" in body:
            if not g.user["is_admin"]:
                abort(403, description="관리자 지정/해제는 관리자만 할 수 있습니다.")
            make_admin = bool(body["isAdmin"])
            if not make_admin and row["is_admin"]:
                _assert_not_last_admin(conn, user_id)
            conn.execute("UPDATE users SET is_admin=? WHERE id=?", (1 if make_admin else 0, user_id))
            changes["isAdmin"] = make_admin

        if "password" in body:
            pw = body.get("password") or ""
            if len(pw) < 8:
                abort(400, description="비밀번호는 8자 이상이어야 합니다.")
            _assert_can_reset_password(user_id)
            conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(pw), user_id))
            drop_user_sessions(conn, user_id)
            changes["password"] = "reset"

        if not changes:
            abort(400, description="변경할 항목이 없습니다.")
        conn.execute("UPDATE users SET updated_at=? WHERE id=?", (config.now_iso(), user_id))
        audit.log("user_updated", target=row["username"], detail=changes)
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return jsonify(_user_row_payload(row))


@bp.delete("/users/<int:user_id>")
def delete_user(user_id):
    """계정을 지운다.

    ★작업 이력은 사라지지 않는다. 누가 셋팅했는지·누가 출고했는지는 전부
      '이름 텍스트'로 저장돼 있어(orders.preparing_by, assets.created_by 등)
      계정을 지워도 기록에 그대로 남는다. 감사 로그도 아이디를 글자로 갖고 있다.
      지워지는 것은 '로그인할 수 있는 자격'과 권한·분류 설정뿐이다.

    ★막아야 하는 것
      ① 자기 자신 — 지우는 순간 스스로 쫓겨나고 되돌릴 방법이 없다.
      ② 마지막 관리자 — 아무도 계정을 만들 수 없게 된다. 더 나쁘게는 계정이 0개가 되면
        최초 설정 화면이 열려, 사내망의 아무 PC나 먼저 관리자를 차지할 수 있다.
      ③ 관리자 계정을 관리자가 아닌 사람이 지우는 것.
    """
    require("users.manage")
    with tx(write=True) as conn:
        row = _get_user_or_404(conn, user_id)
        _require_admin_for_admin_target(row)
        if int(user_id) == int(g.user["id"]):
            abort(400, description="본인 계정은 삭제할 수 없습니다. 다른 관리자에게 요청하세요.")
        if row["is_admin"]:
            _assert_not_last_admin(conn, user_id)
        # 남은 계정이 하나도 없으면 최초 설정 화면이 열린다 — 그 상태를 만들지 않는다
        if conn.execute("SELECT COUNT(*) AS c FROM users WHERE id != ?", (user_id,)).fetchone()["c"] == 0:
            abort(400, description="마지막 계정은 삭제할 수 없습니다.")
        # ★ON DELETE CASCADE에만 기대지 않는다(연결마다 PRAGMA가 켜져 있어야 동작한다).
        #   남으면 나중에 같은 번호를 쓰는 계정에 남의 권한이 붙는다.
        drop_user_sessions(conn, user_id)
        conn.execute("DELETE FROM user_perms WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM user_categories WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        audit.log("user_deleted", target=row["username"],
                  detail={"표시이름": row["display_name"], "관리자": bool(row["is_admin"])})
    return jsonify({"ok": True, "deleted": row["username"]})


@bp.put("/users/<int:user_id>/perms")
def set_user_perms(user_id):
    require("users.manage")
    body = request.get_json(silent=True) or {}
    perms = body.get("perms")
    if not isinstance(perms, list):
        abort(400, description="perms 배열이 필요합니다.")
    bad = [p for p in perms if p not in PERM_CODES]
    if bad:
        abort(400, description=f"알 수 없는 권한 코드: {', '.join(bad)}")
    _check_grantable(perms, user_id)
    with tx(write=True) as conn:
        row = _get_user_or_404(conn, user_id)
        _require_admin_for_admin_target(row)
        conn.execute("DELETE FROM user_perms WHERE user_id=?", (user_id,))
        for p in sorted(set(perms)):
            conn.execute("INSERT INTO user_perms(user_id, perm) VALUES(?,?)", (user_id, p))
        audit.log("perms_updated", target=row["username"], detail={"perms": sorted(set(perms))})
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return jsonify(_user_row_payload(row))


@bp.put("/users/<int:user_id>/categories")
def set_user_categories(user_id):
    require("users.manage")
    body = request.get_json(silent=True) or {}
    all_categories = bool(body.get("allCategories"))
    ids = body.get("categoryIds") or []
    if not isinstance(ids, list):
        abort(400, description="categoryIds는 배열이어야 합니다.")
    with tx(write=True) as conn:
        row = _get_user_or_404(conn, user_id)
        _require_admin_for_admin_target(row)
        # 담당 분류도 권한이다 — 스스로 '전체 보기'를 켜는 것을 막는다(권한 편집과 같은 규칙)
        if not g.user["is_admin"] and int(user_id) == int(g.user["id"]):
            abort(403, description="본인의 담당 분류는 스스로 바꿀 수 없습니다. 관리자에게 요청하세요.")
        cat_ids = [] if all_categories else _validate_category_ids(conn, ids)
        conn.execute("UPDATE users SET all_categories=?, updated_at=? WHERE id=?",
                     (1 if all_categories else 0, config.now_iso(), user_id))
        conn.execute("DELETE FROM user_categories WHERE user_id=?", (user_id,))
        for cid in cat_ids:
            conn.execute(
                "INSERT INTO user_categories(user_id, category_id) VALUES(?,?)", (user_id, cid)
            )
        audit.log("user_categories_updated", target=row["username"],
                  detail={"allCategories": all_categories, "categoryIds": cat_ids})
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return jsonify(_user_row_payload(row))


# ---------------------------------------------------------------- categories

@bp.get("/categories")
def list_categories():
    rows = get_db().execute("SELECT * FROM categories ORDER BY sort, id").fetchall()
    return jsonify([
        {"id": r["id"], "name": r["name"], "sort": r["sort"], "enabled": bool(r["enabled"])}
        for r in rows
    ])


@bp.post("/categories")
def create_category():
    require_any("settings.manage", "purchase.edit")
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        abort(400, description="카테고리 이름을 입력하세요.")
    with tx(write=True) as conn:
        dup = conn.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
        if dup:
            abort(409, description="이미 존재하는 카테고리입니다.")
        max_sort = conn.execute("SELECT COALESCE(MAX(sort), -1) AS m FROM categories").fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO categories(name, sort, enabled, created_at) VALUES(?,?,1,?)",
            (name, max_sort + 1, config.now_iso()),
        )
        audit.log("category_created", target=name)
        cid = cur.lastrowid
    return jsonify({"id": cid, "name": name}), 201


@bp.patch("/categories/<int:cat_id>")
def update_category(cat_id):
    require_any("settings.manage", "purchase.edit")
    body = request.get_json(silent=True) or {}
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
        if row is None:
            abort(404, description="카테고리를 찾을 수 없습니다.")
        changes = {}
        if "name" in body:
            name = (body.get("name") or "").strip()
            if not name:
                abort(400, description="카테고리 이름을 입력하세요.")
            dup = conn.execute(
                "SELECT id FROM categories WHERE name=? AND id != ?", (name, cat_id)
            ).fetchone()
            if dup:
                abort(409, description="이미 존재하는 카테고리 이름입니다.")
            conn.execute("UPDATE categories SET name=? WHERE id=?", (name, cat_id))
            changes["name"] = name
        if "enabled" in body:
            conn.execute("UPDATE categories SET enabled=? WHERE id=?",
                         (1 if body["enabled"] else 0, cat_id))
            changes["enabled"] = bool(body["enabled"])
        if "sort" in body:
            sort_val = _int_or_400(body["sort"], "정렬 값")
            conn.execute("UPDATE categories SET sort=? WHERE id=?", (sort_val, cat_id))
            changes["sort"] = sort_val
        if not changes:
            abort(400, description="변경할 항목이 없습니다.")
        audit.log("category_updated", target=row["name"], detail=changes)
    return jsonify({"ok": True})


@bp.post("/categories/reorder")
def reorder_categories():
    """전체 순서를 한 트랜잭션으로 반영 — 스왑 2회 PATCH의 부분 실패(sort 중복) 방지."""
    require_any("settings.manage", "purchase.edit")
    ids = (request.get_json(silent=True) or {}).get("ids")
    if not isinstance(ids, list) or not ids:
        abort(400, description="ids 배열이 필요합니다.")
    parsed = [_int_or_400(i, "카테고리 ID") for i in ids]
    if len(set(parsed)) != len(parsed):
        abort(400, description="중복된 카테고리 ID가 있습니다.")
    with tx(write=True) as conn:
        valid = {r["id"] for r in conn.execute("SELECT id FROM categories").fetchall()}
        if set(parsed) != valid:
            abort(400, description="전체 카테고리 ID 목록을 순서대로 보내야 합니다.")
        for idx, cid in enumerate(parsed):
            conn.execute("UPDATE categories SET sort=? WHERE id=?", (idx, cid))
        audit.log("categories_reordered", detail={"ids": parsed})
    return jsonify({"ok": True})


# ---------------------------------------------------------------- audit log

@bp.get("/audit")
def list_audit():
    require("audit.view")
    q_action = (request.args.get("action") or "").strip()
    q_user = (request.args.get("username") or "").strip()
    try:
        limit = min(max(int(request.args.get("limit", 200)), 1), 500)
    except ValueError:
        limit = 200
    sql = "SELECT * FROM audit_log WHERE 1=1"
    params = []
    if q_action:
        sql += " AND action LIKE ?"
        params.append(q_action + "%")
    if q_user:
        sql += " AND username LIKE ?"
        params.append("%" + q_user + "%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = get_db().execute(sql, params).fetchall()
    return jsonify([
        {
            "ts": r["ts"], "username": r["username"], "action": r["action"],
            "target": r["target"],
            "detail": json.loads(r["detail"]) if r["detail"] else None,
        }
        for r in rows
    ])


# ---------------------------------------------------------------- settings KV

# 이 이름이 포함된 필드는 응답에서 마스킹된다(원칙 #8 시크릿 분리).
_SECRET_FIELD_HINTS = ("key", "secret", "token", "password", "biz_reg")
_MASK = "••••••••••••"
# 예전에 쓰던 표기. 화면에서 이 글자가 값 칸에 들어가 있었기 때문에,
# 그 뒤에 키를 붙여넣어 '•••SECRET•••실제키'로 저장된 값이 남아 있다(2026-07-29 쿠팡).
# 새 값을 저장할 때 이것도 함께 걷어낸다.
_LEGACY_MASKS = ("•••SECRET•••",)
# 점으로 둘러싸인 SECRET(옛 표기) 또는 점 뭉치 — 쪼개져 섞인 것까지 걷어낸다.
# ●(화면의 저장됨 표시)도 함께 — 어떤 경로로든 키에 섞이면 인증이 깨진다.
_MASK_RE = re.compile("[•●]*SECRET[•●]*|[•●]+")


def _declared_field_types():
    """몰 레지스트리가 정의한 필드 타입 — 이름 추측보다 이게 정확하다.

    'key_expires_at'(키 만료일)처럼 이름에 key가 들어가지만 비밀이 아닌 항목이
    마스킹돼 화면에서 사라지고, 다시 저장하면 지워지는 일이 있었다(2026-07-29 확인).
    """
    from ..malls import COMMON_FIELDS, MALLS
    types = {}
    for m in MALLS:
        for f in list(m.get("fields") or []) + list(COMMON_FIELDS):
            types.setdefault(f["key"], f.get("type"))
    return types


def _is_secret_key(key, declared):
    t = declared.get(key)
    if t is not None:
        return t == "secret"                     # 정의가 있으면 그것을 따른다
    return any(h in key.lower() for h in _SECRET_FIELD_HINTS)


def _mask_value(obj, declared=None):
    if declared is None:
        declared = _declared_field_types()
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and v and _is_secret_key(k, declared):
                out[k] = _MASK
            else:
                out[k] = _mask_value(v, declared)
        return out
    if isinstance(obj, list):
        return [_mask_value(v, declared) for v in obj]
    return obj


def _clean_secret(key, value):
    """저장하기 전에 API 키를 손본다.

    ★가려진 값(•••SECRET•••)이 칸에 남아 있는 상태에서 그 뒤에 키를 붙여넣으면
      '•••SECRET•••실제키'가 저장된다. 그러면 서버에 보내는 인증 헤더에 그 점들이
      섞여 들어가 요청이 나가기도 전에 터진다. 대표가 실제로 겪은 오류
      (2026-07-29 쿠팡: latin-1 codec can't encode characters in position 37-39)가 이것이다.
      화면에서도 막았지만, 붙여넣기 경로가 여럿이라 저장 단계에서 한 번 더 걷어낸다.
    """
    if not isinstance(value, str):
        return value
    # ★가림 표기가 '통째로' 남아 있다는 보장이 없다. 칸 가운데에 커서를 놓고 붙여넣으면
    #   '•••SECRET••[실제키]•'처럼 쪼개진 채 섞인다(2026-07-29 쿠팡 secret_key가 이랬다).
    #   그래서 점(U+2022)과 옛 표기의 SECRET 글자를 패턴으로 걷어낸다.
    #   API 키에 가운뎃점이 들어가는 몰은 없으므로 점은 전부 지워도 안전하다.
    cleaned = _MASK_RE.sub("", value).strip()
    if not cleaned:
        return cleaned
    # 인증 헤더에 실리는 값이라 ASCII만 들어갈 수 있다. 한글·특수문자는 눈에 안 보여
    # 원인을 찾기 어려우므로 저장 자체를 막고 무엇이 잘못됐는지 알려 준다.
    bad = sorted({ch for ch in cleaned if ord(ch) > 127})
    if bad:
        abort(400, description=(
            f"'{key}' 값에 넣을 수 없는 문자가 있습니다: {' '.join(bad[:5])}\n"
            "칸을 완전히 비운 뒤(Ctrl+A → Delete) 키를 다시 붙여넣어 주세요. "
            "한글이나 설명이 섞이면 안 됩니다."))
    return cleaned


def _merge_secrets(new, old, declared=None):
    """클라이언트가 보낸 값과 기존 값을 병합한다.

    - 마스크(•••SECRET•••)를 그대로 돌려보내면 기존 값을 유지
    - 마스크 뒤에 새 값을 붙여 보냈으면 마스크만 걷어내고 새 값을 쓴다
    - ★new에 없는 하위 키는 기존 값을 그대로 남긴다(부분 저장 지원).
      화면은 한 번에 한 몰만 보내므로, 통째로 교체하면 다른 몰의 API 키가 전부 사라진다.
    - 리스트는 인덱스로 짝을 지어 재귀. 시크릿이 든 리스트는 재정렬 시 주의.
    """
    if declared is None:
        declared = _declared_field_types()
    if isinstance(new, dict):
        out = dict(old) if isinstance(old, dict) else {}
        for k, v in new.items():
            old_v = old.get(k) if isinstance(old, dict) else None
            if v == _MASK:
                out[k] = old_v
            elif isinstance(v, str) and _is_secret_key(k, declared):
                out[k] = _clean_secret(k, v)
            else:
                out[k] = _merge_secrets(v, old_v, declared)
        return out
    if isinstance(new, list):
        old_list = old if isinstance(old, list) else []
        return [
            _merge_secrets(v, old_list[i] if i < len(old_list) else None, declared)
            for i, v in enumerate(new)
        ]
    return new


@bp.get("/settings")
def get_settings():
    require("settings.manage")
    rows = get_db().execute("SELECT key, value FROM settings").fetchall()
    out = {}
    for r in rows:
        try:
            out[r["key"]] = _mask_value(json.loads(r["value"]))
        except ValueError:
            out[r["key"]] = None
    return jsonify(out)


@bp.put("/settings")
def put_settings():
    require("settings.manage")
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not body:
        abort(400, description="설정 객체가 필요합니다.")
    with tx(write=True) as conn:
        for key, value in body.items():
            old_row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            old = json.loads(old_row["value"]) if old_row else {}
            merged = _merge_secrets(value, old)
            # ★수수료율 표는 '보낸 그대로'가 정답이다. 병합하면 화면에서 지운 요율이
            #   되살아나 '비우면 0%'라는 안내와 어긋난다(2026-07-29 확인).
            if key == "settlement" and isinstance(value, dict) and "rates" in value:
                merged["rates"] = value["rates"]
            conn.execute(
                "INSERT INTO settings(key, value, updated_at, updated_by) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (key, json.dumps(merged, ensure_ascii=False), config.now_iso(),
                 g.user["display_name"]),
            )
        audit.log("settings_updated", detail={"keys": sorted(body.keys())})
    return jsonify({"ok": True})


@bp.get("/order-channels")
def order_channels():
    """주문에 실제로 들어 있는 쇼핑몰 값 목록.

    정산 화면이 요율 칸을 만들 때 쓴다. 주문 조회 권한 없이 설정만 맡은 사람도
    요율을 넣을 수 있어야 하므로, 개인정보 없이 채널 이름과 건수만 내려준다.
    """
    require("settings.manage")
    rows = get_db().execute(
        "SELECT channel, COUNT(*) AS cnt FROM orders WHERE channel != '' "
        "GROUP BY channel ORDER BY cnt DESC").fetchall()
    return jsonify([{"channel": r["channel"], "count": r["cnt"]} for r in rows])


# ---------------------------------------------------------------- backups

@bp.get("/backups")
def backups():
    require("settings.manage")
    from ..db import mirror_dir
    db_path = current_app.config["DB_PATH"]
    rows = list_backups(db_path)
    # 백업이 '있다'가 아니라 '쓸 수 있다'를 보여준다 — 빈 백업만 있으면 없는 것과 같다
    for r in rows:
        r["counts"] = _backup_counts(backup_dir(db_path) / r["name"])
    mirror = mirror_dir()
    mirror_ok = None
    if mirror is not None:
        try:
            mirror.mkdir(parents=True, exist_ok=True)
            probe = mirror / ".hms-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            mirror_ok = True
        except OSError:
            mirror_ok = False
    return jsonify({
        "backups": rows,
        "intervalHours": config.BACKUP_INTERVAL_HOURS,
        "retentionDays": config.BACKUP_RETENTION_DAYS,
        "mirror": str(mirror) if mirror else "",
        "mirrorWritable": mirror_ok,
        "mirrorCount": len(list(mirror.glob("hms-*.db"))) if (mirror and mirror.exists()) else 0,
    })


def _backup_counts(path):
    """백업 파일 안에 실제로 무엇이 들어 있는지(빈 백업 조기 발견)."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("orders", "assets", "waybills")}
        finally:
            conn.close()
    except Exception:
        return None


@bp.get("/backups/<name>/download")
def download_backup(name):
    """백업 내려받기 — 대표가 PC 밖(USB·클라우드)에 보관할 수 있어야 한다."""
    require("settings.manage")
    # 목록이 보여주는 파일은 전부 받을 수 있어야 한다(종료 스냅샷 shutdown-latest.db 포함)
    if not re.fullmatch(r"(hms-[A-Za-z0-9_-]+|shutdown-latest)\.db", name or ""):
        abort(400, description="백업 파일 이름이 올바르지 않습니다.")
    path = backup_dir(current_app.config["DB_PATH"]) / name
    if not path.exists():
        abort(404, description="백업 파일을 찾을 수 없습니다.")
    audit.log("backup_downloaded", target=name)
    return send_file(path, as_attachment=True, download_name=name,
                     mimetype="application/octet-stream")


@bp.post("/backups/run")
def run_backup():
    require("settings.manage")
    target = run_manual_backup(current_app.config["DB_PATH"])
    with tx(write=True):
        audit.log("backup_run", target=target.name)
    return jsonify({"ok": True, "name": target.name})
