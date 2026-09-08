"""OWS 앱 팩토리.

전역 인증 게이트(원칙 #2): /api/* 는 명시적 공개 allowlist 외 전부 세션 필수.
라우트별 데코레이터(opt-in) 방식은 누락이 구조적으로 발생하므로 쓰지 않는다.
"""
import hmac
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

from . import config
from .db import close_db, get_db, init_db

# 공개 API — 여기 없는 /api/* 경로는 전부 로그인 필수
PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/bootstrap",
    "/api/auth/login",
    "/api/auth/setup",
}

# RMS(렌탈 시스템)와 자산을 주고받는 서버 간 창구. 사람 세션이 아니라 공유 시크릿으로 연다.
# ★공개가 아니다 — 토큰이 맞아야만 통과한다. 토큰 미설정 시 전부 401(기본 OFF).
BRIDGE_PREFIX = "/api/bridge/"


def _bridge_token():
    """연동 토큰. 환경변수 우선, 없으면 data/bridge_token.txt.

    ★서버는 작업 스케줄러(OperationsSystemAutoStart)가 띄우므로 .bat에 넣은 환경변수가
      안 닿는다. 그래서 DB 옆 파일도 읽는다 — 배포할 때 파일 하나만 두면 된다.
      (.bat은 한글이 들어가면 통째로 조용히 실패하므로 손대지 않는 게 안전하다.)
    ★파일이 없거나 비어 있으면 창구는 잠긴 채로 뜬다(기본 OFF).
    """
    t = (os.getenv("OWS_BRIDGE_TOKEN") or "").strip()
    if t:
        return t
    try:
        return (config.DB_PATH.parent / "bridge_token.txt").read_text("utf-8").strip()
    except OSError:
        return ""


def _setup_file_log(app):
    """오류를 파일에 남긴다.

    서버는 작업스케줄러가 콘솔 없이(pythonw) 띄우므로, 파일에 안 남기면
    '화면이 안 돼요'가 났을 때 원인을 찾을 방법이 아예 없다.
    """
    if app.config.get("TESTING") or os.getenv("OWS_NO_FILE_LOG") == "1":
        return
    log_dir = Path(app.config["DB_PATH"]).parent / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "ows.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    except OSError:
        return                       # 로그를 못 써도 서버는 떠야 한다
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    handler.setLevel(logging.INFO)
    if not any(isinstance(h, RotatingFileHandler) for h in app.logger.handlers):
        app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)
    root = logging.getLogger("waitress")   # 서버 계층 오류도 같은 파일로
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    root.setLevel(logging.INFO)


_STATIC_REF = re.compile(r'"(/static/(?:css|js)/[A-Za-z0-9_.\-]+)"')


def _stamp(static_folder, m):
    """/static/js/app.js → /static/js/app.js?v=<수정시각>. 파일이 없으면 그대로 둔다."""
    ref = m.group(1)
    f = Path(static_folder) / ref[len("/static/"):]
    try:
        return f'"{ref}?v={int(f.stat().st_mtime)}"'
    except OSError:
        return m.group(0)


def create_app(db_path=None):
    app = Flask(
        __name__,
        static_folder=str(config.ROOT / "static"),
        static_url_path="/static",
    )
    app.config["DB_PATH"] = Path(db_path or config.DB_PATH)
    # css/js는 주소에 ?v=<파일시각>이 붙으므로(index 참고) 오래 캐시해도 안전하다.
    # 파일이 바뀌면 주소가 바뀌어 브라우저가 새로 받는다.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000    # 1년
    app.json.ensure_ascii = False
    init_db(app.config["DB_PATH"])
    app.teardown_appcontext(close_db)
    _setup_file_log(app)

    from .auth import bp as auth_bp
    from .auth import load_session, touch_session
    from .auth.perms import user_perm_set
    from .asvc import bp as as_bp
    from .orders import bp as orders_bp
    from .prep import bp as prep_bp
    from .purchase import bp as purchase_bp
    from .reports import bp as reports_bp
    from .settings import bp as admin_bp
    from . import malls  # noqa: F401  (settings 블루프린트에 /mall-registry 라우트 등록)
    from . import notify  # noqa: F401  (고객 안내 문자 라우트)
    from .malls import invoice_push  # noqa: F401  (송장번호 몰 전송 라우트)
    from .malls import stock_sync  # noqa: F401  (재고 연동 토글·미리보기 라우트, 기본 전부 OFF)
    from .cj import test_route  # noqa: F401  (settings 블루프린트에 /cj/test 등록)
    from .cj import tools_route  # noqa: F401  (RMS 이식·견본PDF·주소정제·실발행 왕복 시험)
    from .settings import changelog  # noqa: F401  (업데이트 내역 — 좌측 상단 📢)
    from .settings import qc_import  # noqa: F401  (QC 프로그램 데이터 이관)
    from .settings import qc_watch  # noqa: F401  (QC 폴더 실시간 감시)
    from .settings import qc_shipped  # noqa: F401  (QC에서 출고된 고객 대조·반영)

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(purchase_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(as_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(prep_bp)

    @app.before_request
    def _global_gate():
        path = request.path
        if not path.startswith("/api/"):
            return None  # 정적 파일/쉘 페이지 — 데이터 없음
        if path in PUBLIC_API_PATHS:
            return None
        # ★RMS 연동 창구(/api/bridge/…) — 사람 세션이 아니라 서버끼리 부르는 자리다.
        #   기본 OFF: OWS_BRIDGE_TOKEN을 안 넣으면 이 경로 자체가 401이다.
        if path.startswith(BRIDGE_PREFIX):
            token = _bridge_token()
            got = request.headers.get("X-Bridge-Token") or ""
            if not token or not hmac.compare_digest(token, got):
                return jsonify({"error": "연동 토큰이 올바르지 않습니다."}), 401
            g.user = {"id": None, "username": "rms-bridge",
                      "display_name": "RMS 연동", "is_admin": 1, "all_categories": 1}
            g.perms = set()
            return None
        sess = load_session(request.cookies.get(config.COOKIE_NAME))
        if sess is None:
            return jsonify({"error": "로그인이 필요합니다."}), 401
        touch_session(sess)
        g.user = sess
        g.perms = set() if sess["is_admin"] else user_perm_set(sess["id"])
        return None

    @app.get("/")
    def index():
        """쉘 페이지 — css/js 주소에 파일 시각(?v=)을 붙여 내려 준다.

        ★버전을 붙이는 진짜 이유는 '캐시를 오래 쓰기 위해서'다.
          아래 SEND_FILE_MAX_AGE_DEFAULT로 css/js를 1년간 캐시하게 해 두고,
          파일이 바뀌면 주소가 바뀌어 새로 받게 만든다.
          (버전만 붙이고 캐시 기간을 안 늘리면 Werkzeug 기본값이 no-cache라
           매 요청 서버에 다시 물어보므로 실익이 없다 — 2026-07-31 감사 지적.)

        ★파일을 못 읽어도 화면은 떠야 한다. 인코딩이 바뀌거나 저장 중이면
          예외가 나는데, 그때 500을 주면 첫 화면이 통째로 안 뜬다.
        """
        path = Path(app.static_folder) / "index.html"
        try:
            html = path.read_text("utf-8")
            html = _STATIC_REF.sub(lambda m: _stamp(app.static_folder, m), html)
        except (OSError, ValueError):
            # ValueError = UnicodeDecodeError. 버전만 못 붙일 뿐 화면은 그대로 띄운다.
            app.logger.exception("index.html 읽기 실패 — 버전 표시 없이 내려보낸다")
            try:
                html = path.read_text("utf-8", errors="replace")
            except OSError:
                return jsonify({"error": "첫 화면 파일을 읽을 수 없습니다."}), 500
        resp = app.make_response(html)
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        resp.headers["Cache-Control"] = "no-cache"   # 쉘 자체는 늘 새로 받는다
        return resp

    @app.get("/api/health")
    def health():
        # 프로세스만 살아 있고 DB가 잠기거나 끊긴 상태도 '정상'으로 보이면 안 된다.
        get_db().execute("SELECT 1").fetchone()
        return jsonify({"ok": True, "service": "OWS", "database": True,
                        "time": config.now_iso()})

    @app.errorhandler(HTTPException)
    def _json_error(e):
        return jsonify({"error": e.description or e.name}), e.code

    @app.errorhandler(Exception)
    def _unhandled(e):
        # 어디서 터졌는지 남긴다 — '화면이 안 돼요'의 원인을 찾을 유일한 단서다.
        # ★기록에 실패하더라도 사용자에게는 반드시 한국어 안내가 나가야 한다.
        #   (여기서 예외가 나면 Flask 기본 영어 500 페이지가 대신 뜬다 — 실제 발생)
        try:
            user = g.get("user")
            who = user["username"] if user is not None else "-"   # sqlite3.Row라 .get()이 없다
        except Exception:                                          # noqa: BLE001
            who = "-"
        try:
            app.logger.exception("unhandled error | %s %s | user=%s",
                                 request.method, request.path, who)
        except Exception:                                          # noqa: BLE001
            pass
        return jsonify({"error": "서버 내부 오류가 발생했습니다."}), 500

    # 배송 추적·자동수집 데몬 — 테스트에서는 띄우지 않는다(스레드 누수 방지)
    if not app.config.get("TESTING") and os.getenv("OWS_NO_TRACKER") != "1":
        from .malls.scheduler import start_collector
        from .orders.recall import start_tracker
        start_tracker(app)
        start_collector(app)
        # 송장 발급 즉시 몰 전송의 그물 — 놓친 최근 발급분을 10분마다 보낸다(2026-09-08).
        from .malls.invoice_push import start_push_sweeper
        start_push_sweeper(app)
        # TMS 엑셀 자동 반영 — tms-export 폴더에 새 파일이 들어오면 알아서 최신화한다.
        # 검증 서버에서까지 돌 필요는 없으므로 끌 수 있게 해 둔다.
        if os.getenv("OWS_NO_TMS_SYNC") != "1":
            from .purchase.autosync import start_tms_autosync
            start_tms_autosync(app)   # 라우트는 purchase 패키지에서 이미 등록됨
            # 연동 창구(data-bridge) 직접 반영 — 엑셀을 거치지 않는 행 단위 반영(2026-09-02 대표: 무결성 우선).
            # 창구 주소·토큰이 없으면 틱마다 조용히 건너뛴다.
            from .purchase.tms_link import start_tms_link
            start_tms_link(app)
        # ★QC 프로그램 폴더 감시 — 2026-08-07 대표 지시로 껐다.
        #   "셋팅 및 QC는 OWS를 바로 관련 사람들이 사용할 예정이니까.
        #    앞으로 OWS에서 데이터가 쌓일거야."
        #
        #   이제 OWS가 원본이다. 남의 프로그램 파일을 실시간으로 따라가면 OWS에서 한 일이
        #   덮이거나 되살아난다 — 2026-08-05에 실제로 그랬다(정리한 줄을 20초마다 되살려
        #   794회 병합). 원본이 둘이면 반드시 그렇게 된다.
        #
        #   코드와 라우트는 그대로 두었다. 마지막 대조가 한 번 더 필요하면
        #   OWS_QC_WATCH=1 로 켜고 [설정 ▸ 데이터 이관]에서 손으로 돌리면 된다.
        if os.getenv("OWS_QC_WATCH") == "1" and os.getenv("OWS_NO_TMS_SYNC") != "1":
            from .settings.qc_watch import start_qc_watch
            start_qc_watch(app)
            app.logger.warning("QC 폴더 실시간 감시가 켜져 있습니다(OWS_QC_WATCH=1) — "
                               "OWS와 QC 두 곳이 같은 주문을 만집니다.")

    return app
