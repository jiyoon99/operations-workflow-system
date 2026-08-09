"""QC 프로그램 데이터 이관을 격리 환경에서 미리 돌려본다(운영 DB 무변경).

사용: python scripts/qc_migrate_probe.py <QC폴더경로>
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = Path(sys.argv[1])
    tmp = Path(tempfile.mkdtemp(prefix="qc-probe-"))
    app = create_app(db_path=tmp / "probe.db")
    app.testing = True
    c = app.test_client()
    c.post("/api/auth/setup", json={"username": "probe", "displayName": "검증",
                                    "password": "probe-pass-1234"})

    print("=== 미리보기 ===")
    r = c.post("/api/migrate/qc/preview", json={"path": str(src)})
    if r.status_code != 200:
        print("실패:", r.status_code, r.get_json())
        return 1
    p = r.get_json()
    print(f"주문  : 전체 {p['orders']['total']} → 생성 {p['orders']['toCreate']} / 중복 {p['orders']['duplicates']}")
    print(f"사용자: 전체 {p['users']['total']} → 생성 {p['users']['toCreate']} / 중복 {p['users']['duplicates']}")
    for u in p["users"]["list"]:
        print(f"    {u['username']:12s} {u['displayName']:6s} {u['role']:14s} → {u['grant']}")
    print(f"자산  : 생성 {p['assets']['toCreate']}  예: {p['assets']['sample'][:5]}")

    print("\n=== 실행 ===")
    r = c.post("/api/migrate/qc", json={"path": str(src)})
    if r.status_code != 200:
        print("실패:", r.status_code, r.get_json())
        return 1
    res = r.get_json()
    print(json.dumps(res, ensure_ascii=False, indent=1))

    print("\n=== 결과 확인 ===")
    orders = c.get("/api/orders?view=all").get_json()["orders"]
    print("주문 조회:", len(orders), "건")
    assets = c.get("/api/assets").get_json()
    print("자산 조회:", len(assets), "대")
    users = c.get("/api/users").get_json()
    print("사용자:", len(users), "명")
    with_assets = [o for o in orders if o["assets"]]
    print("자산 매칭된 주문:", len(with_assets))
    if with_assets:
        o = with_assets[0]
        print("  예:", o["orderNumber"], o["recipient"],
              "→", [a["assetNo"] for a in o["assets"]])
    staff = c.get("/api/reports/staff").get_json()
    print("담당자 실적:", [(s["name"], s["production"], s["inspection"], s["shipping"]) for s in staff[:5]])

    print("\n=== 재실행(중복 방지) ===")
    r2 = c.post("/api/migrate/qc", json={"path": str(src)})
    print(json.dumps(r2.get_json(), ensure_ascii=False, indent=1))
    print("재실행 후 주문:", len(c.get("/api/orders?view=all").get_json()["orders"]), "건")

    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
