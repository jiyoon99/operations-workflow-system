"""OWS 통합 계정 인증 — 로그인/세션(DB 영속)/최초 설정.

- PBKDF2-SHA256 해시, 로그인 실패 5회 시 5분 차단
- 세션은 sessions 테이블에 영속(서버 재시작 시 로그아웃 없음)
- GET 쿼리스트링 로그인 같은 우회 경로는 두지 않는다(기존 시스템 결함 교정)
"""
import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta

from flask import Blueprint, abort, g, jsonify, request

from .. import audit, config
from ..db import get_db, tx
from .perms import MENUS, PERM_CODES, user_category_ids, user_perm_set

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

_login_lock = threading.Lock()
_login_failures = {}  # (ip, username) -> {"count": int, "blocked_until": float}


# ---------------------------------------------------------------- passwords

def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", pw.encode("utf-8"), salt.encode("ascii"), config.PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${config.PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac(
            "sha256", pw.encode("utf-8"), salt.encode("ascii"), int(iters)
        ).hex()
        return hmac.compare_digest(calc, digest)
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------- sessions

def create_session(conn, user_id, ip):
    token = secrets.token_urlsafe(32)
    created = config.now()
    expires = created + timedelta(hours=config.SESSION_HOURS)
    conn.execute(
        "INSERT INTO sessions(token, user_id, created_at, expires_at, last_seen, ip) VALUES(?,?,?,?,?,?)",
        (
            token,
            user_id,
            created.isoformat(timespec="seconds"),
            expires.isoformat(timespec="seconds"),
            created.isoformat(timespec="seconds"),
            ip,
        ),
    )
    return token


def load_session(token):
    """유효한 세션이면 사용자 row(세션 필드 포함)를, 아니면 None을 반환."""
    if not token:
        return None
    row = get_db().execute(
        "SELECT s.token AS s_token, s.expires_at AS s_expires, s.last_seen AS s_last_seen, u.* "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return None
    if row["s_expires"] < config.now_iso() or not row["enabled"]:
        # 의도적 autocommit 단문 쓰기(tx 미경유): 만료 세션 정리는 단일 문장이라
        # 원자적이고, 요청마다 쓰기 트랜잭션을 여는 경합을 피한다. (원칙 #1의 명시적 예외)
        get_db().execute("DELETE FROM sessions WHERE token=?", (token,))
        return None
    return row


def touch_session(row):
    """last_seen 갱신(5분 스로틀 — 요청마다 쓰지 않는다).

    의도적 autocommit 단문 쓰기(tx 미경유): 세션 메타데이터 단일 UPDATE는 원자적이며,
    전역 게이트 경로에서 쓰기 트랜잭션·일별 백업 트리거를 피한다. (원칙 #1의 명시적 예외)
    """
    try:
        last = datetime.fromisoformat(row["s_last_seen"])
    except (TypeError, ValueError):
        last = None
    if last is None or (config.now() - last).total_seconds() > 300:
        get_db().execute(
            "UPDATE sessions SET last_seen=? WHERE token=?",
            (config.now_iso(), row["s_token"]),
        )


def drop_user_sessions(conn, user_id):
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def _purge_expired_sessions(conn):
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (config.now_iso(),))


def _set_cookie(resp, token):
    resp.set_cookie(
        config.COOKIE_NAME,
        token,
        max_age=config.SESSION_HOURS * 3600,
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


def user_payload(user_row):
    is_admin = bool(user_row["is_admin"])
    perms = sorted(PERM_CODES) if is_admin else sorted(user_perm_set(user_row["id"]))
    all_cats = is_admin or bool(user_row["all_categories"])
    pset = set(perms)
    # 접근 가능한 메뉴 — 메뉴 접근 권한이나 그 메뉴의 하위 권한이 하나라도 있으면 보인다
    menus = [
        m["menu"] for m in MENUS
        if is_admin or m["view"] in pset or any(c in pset for c, _ in m["perms"])
    ]
    return {
        "id": user_row["id"],
        "username": user_row["username"],
        "displayName": user_row["display_name"],
        "isAdmin": is_admin,
        "allCategories": all_cats,
        "categoryIds": [] if all_cats else user_category_ids(user_row["id"]),
        "perms": perms,
        "menus": menus,
    }


# ---------------------------------------------------------------- lockout

def _client_ip():
    return request.remote_addr or "?"


def _lockout_key(username):
    return (_client_ip(), (username or "").strip().lower())


def _check_blocked(username):
    with _login_lock:
        rec = _login_failures.get(_lockout_key(username))
        if rec and rec["blocked_until"] > time.monotonic():
            remain = int(rec["blocked_until"] - time.monotonic()) + 1
            abort(429, description=f"로그인 실패가 반복되어 잠시 차단되었습니다. {remain}초 후 다시 시도하세요.")


def _record_failure(username):
    with _login_lock:
        rec = _login_failures.setdefault(_lockout_key(username), {"count": 0, "blocked_until": 0.0})
        rec["count"] += 1
        if rec["count"] >= config.LOGIN_FAILURE_LIMIT:
            rec["count"] = 0
            rec["blocked_until"] = time.monotonic() + config.LOGIN_BLOCK_SECONDS


def _reset_failures(username):
    with _login_lock:
        _login_failures.pop(_lockout_key(username), None)


# ---------------------------------------------------------------- routes

def _user_count():
    return get_db().execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


@bp.get("/bootstrap")
def bootstrap():
    return jsonify({"needsSetup": _user_count() == 0})


@bp.post("/setup")
def setup():
    """최초 1회 관리자 생성. 계정이 하나라도 있으면 잠긴다."""
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    display_name = (body.get("displayName") or "").strip() or username
    password = body.get("password") or ""
    if len(username) < 3 or len(password) < 8:
        abort(400, description="아이디는 3자 이상, 비밀번호는 8자 이상이어야 합니다.")
    with tx(write=True) as conn:
        if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] > 0:
            abort(409, description="이미 초기 설정이 완료되었습니다.")
        ts = config.now_iso()
        cur = conn.execute(
            "INSERT INTO users(username, display_name, pw_hash, is_admin, all_categories, enabled, created_at, updated_at) "
            "VALUES(?,?,?,1,1,1,?,?)",
            (username, display_name, hash_password(password), ts, ts),
        )
        user_id = cur.lastrowid
        token = create_session(conn, user_id, _client_ip())
        g.user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        audit.log("setup", target=username, detail={"displayName": display_name})
    resp = jsonify(user_payload(g.user))
    return _set_cookie(resp, token)


@bp.post("/login")
def login():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        abort(400, description="아이디와 비밀번호를 입력하세요.")
    _check_blocked(username)
    user = get_db().execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()
    if user is None or not user["enabled"] or not verify_password(password, user["pw_hash"]):
        _record_failure(username)
        with tx(write=True):
            audit.log("login_failed", target=username, detail={"ip": _client_ip()})
        abort(401, description="아이디 또는 비밀번호가 올바르지 않습니다.")
    _reset_failures(username)
    with tx(write=True) as conn:
        _purge_expired_sessions(conn)
        token = create_session(conn, user["id"], _client_ip())
        g.user = user
        audit.log("login", target=user["username"], detail={"ip": _client_ip()})
    resp = jsonify(user_payload(user))
    return _set_cookie(resp, token)


@bp.post("/logout")
def logout():
    with tx(write=True) as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (g.user["s_token"],))
        audit.log("logout", target=g.user["username"])
    resp = jsonify({"ok": True})
    resp.delete_cookie(config.COOKIE_NAME, path="/")
    return resp


@bp.get("/me")
def me():
    return jsonify(user_payload(g.user))


@bp.patch("/password")
def change_password():
    """본인 비밀번호 변경(현재 비밀번호 확인 필수).

    - 현재 비밀번호 검증 실패는 로그인과 같은 (ip, 아이디) 잠금 카운터·감사로그 적용
      (탈취 세션으로 무제한·무흔적 브루트포스 차단)
    - 성공 시 현재 세션을 제외한 이 계정의 다른 세션 전부 무효화
      (쿠키 탈취 의심 시 비밀번호 변경으로 침입 세션을 끊을 수 있어야 함)
    """
    body = request.get_json(silent=True) or {}
    current = body.get("currentPassword") or ""
    new = body.get("newPassword") or ""
    if len(new) < 8:
        abort(400, description="새 비밀번호는 8자 이상이어야 합니다.")
    _check_blocked(g.user["username"])
    if not verify_password(current, g.user["pw_hash"]):
        _record_failure(g.user["username"])
        with tx(write=True):
            audit.log("password_change_failed", target=g.user["username"],
                      detail={"ip": _client_ip()})
        abort(403, description="현재 비밀번호가 올바르지 않습니다.")
    _reset_failures(g.user["username"])
    with tx(write=True) as conn:
        conn.execute(
            "UPDATE users SET pw_hash=?, updated_at=? WHERE id=?",
            (hash_password(new), config.now_iso(), g.user["id"]),
        )
        conn.execute(
            "DELETE FROM sessions WHERE user_id=? AND token != ?",
            (g.user["id"], g.user["s_token"]),
        )
        audit.log("password_changed", target=g.user["username"])
    return jsonify({"ok": True})
