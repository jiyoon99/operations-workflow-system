"""HMS 서버 실행 — 단일 프로세스 가드(원칙 #10) + 종료 스냅샷 백업."""
import atexit
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Windows 콘솔(cp949)에서 한글/특수문자 로그가 깨지거나 예외 나지 않도록 UTF-8 강제
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app import config, create_app  # noqa: E402
from app.db import backup_dir, make_backup  # noqa: E402


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not _port_free(config.PORT):
        print(f"[HMS] 포트 {config.PORT}가 이미 사용 중입니다. 서버가 이미 실행 중인지 확인하세요.")
        sys.exit(1)

    app = create_app()
    config.PID_FILE.write_text(str(os.getpid()), encoding="ascii")

    def _on_shutdown():
        try:
            make_backup(app.config["DB_PATH"], backup_dir(app.config["DB_PATH"]) / "shutdown-latest.db")
        except Exception:
            pass
        try:
            config.PID_FILE.unlink()
        except OSError:
            pass

    atexit.register(_on_shutdown)

    print(f"[HMS] 하프북 관리 시스템 시작 — http://localhost:{config.PORT} (LAN: 서버IP:{config.PORT})")
    try:
        from waitress import serve
        serve(app, host=config.HOST, port=config.PORT, threads=8)
    except ImportError:
        app.run(host=config.HOST, port=config.PORT, threaded=True)


if __name__ == "__main__":
    main()
