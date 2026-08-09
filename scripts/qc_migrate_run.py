"""운영 DB에 QC 프로그램 데이터를 이관한다(관리자 대신 실행).

사용: python scripts/qc_migrate_run.py <QC폴더경로> [--preview]
실행 전 반드시 백업이 있어야 한다(스크립트가 자동으로 하나 더 만든다).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import g  # noqa: E402

from app import config, create_app  # noqa: E402
from app.db import get_db, run_manual_backup  # noqa: E402
from app.settings.qc_import import qc_preview, qc_run  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = sys.argv[1]
    preview_only = "--preview" in sys.argv

    app = create_app()
    with app.app_context():
        if not preview_only:
            target = run_manual_backup(app.config["DB_PATH"])
            print(f"[백업] {target.name}")

    with app.test_request_context("/api/migrate/qc", json={"path": src}):
        conn = get_db()
        admin = conn.execute(
            "SELECT * FROM users WHERE is_admin=1 AND enabled=1 ORDER BY id LIMIT 1").fetchone()
        if admin is None:
            print("관리자 계정이 없습니다. 먼저 HMS에서 계정을 만드세요.")
            return 1
        g.user = admin
        g.perms = set()
        print(f"[실행자] {admin['display_name']} ({admin['username']})")

        resp = (qc_preview if preview_only else qc_run)()
        data = resp.get_json() if hasattr(resp, "get_json") else resp
        print(json.dumps(data, ensure_ascii=False, indent=1)[:2000])

    if not preview_only:
        app2 = create_app()
        with app2.app_context():
            c = get_db()
            for t in ("users", "orders", "assets", "order_assets"):
                print(f"  {t}: {c.execute(f'SELECT COUNT(*) AS c FROM {t}').fetchone()['c']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
