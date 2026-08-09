"""종료 전 스냅샷 — stop_server.bat이 호출. 서버 실행 중에도 안전(sqlite 온라인 백업)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.db import backup_dir, make_backup  # noqa: E402

if config.DB_PATH.exists():
    make_backup(config.DB_PATH, backup_dir(config.DB_PATH) / "shutdown-latest.db")
    print("snapshot ok")
else:
    print("no db")
