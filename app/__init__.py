"""HMS 앱 팩토리.

전역 인증 게이트(원칙 #2): /api/* 는 명시적 공개 allowlist 외 전부 세션 필수.
라우트별 데코레이터(opt-in) 방식은 누락이 구조적으로 발생하므로 쓰지 않는다.
"""
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

from . import config
from .db import close_db, init_db

# 공개 API — 여기 없는 /api/* 경로는 전부 로그인 필수
PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/bootstrap",
    "/api/auth/login",
    "/api/auth/setup",
}


def _setup_file_log(app):
    """오류를 파일에 남긴다.

    서버는 작업스케줄러가 콘솔 없이(pythonw) 띄우므로, 파일에 안 남기면
    '화면이 안 돼요'가 났을 때 원인을 찾을 방법이 아예 없다.
    """
    if app.config.get("TESTING") or os.getenv("HMS_NO_FILE_LOG") == "1":
        return
    log_dir = Path(app.config["DB_PATH"]).parent / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "hms.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
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
        return jsonify({"ok": True, "service": "HMS", "time": config.now_iso()})

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
    if not app.config.get("TESTING") and os.getenv("HMS_NO_TRACKER") != "1":
        from .malls.scheduler import start_collector
        from .orders.recall import start_tracker
        start_tracker(app)
        start_collector(app)
        # TMS 엑셀 자동 반영 — tms-export 폴더에 새 파일이 들어오면 알아서 최신화한다.
        # 검증 서버에서까지 돌 필요는 없으므로 끌 수 있게 해 둔다.
        if os.getenv("HMS_NO_TMS_SYNC") != "1":
            from .purchase.autosync import start_tms_autosync
            start_tms_autosync(app)   # 라우트는 purchase 패키지에서 이미 등록됨
        # QC 프로그램 폴더(NAS) 감시 — 셋팅/QC 단계를 실시간으로 따라온다.
        # ★같은 스위치로 끈다(검증 서버가 남의 NAS를 두드리면 안 된다).
        if os.getenv("HMS_NO_TMS_SYNC") != "1":
            from .settings.qc_watch import start_qc_watch
            start_qc_watch(app)

    return app
