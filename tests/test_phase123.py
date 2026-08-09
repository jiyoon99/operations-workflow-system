"""Phase 1(매입/자산) · Phase 2(주문/QC 상태머신/매칭/임포트) · Phase 3(송장) 테스트."""
import io
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth as auth_mod  # noqa: E402
from app import config, create_app  # noqa: E402
from app.importers import write_xlsx  # noqa: E402

ADMIN_PW = "admin-pass-1"
USER_PW = "user-pass-12"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hms-p123-"))
        self.db_path = self.tmp / "test.db"
        self.app = create_app(db_path=self.db_path)
        self.app.testing = True
        self.client = self.app.test_client()
        auth_mod._login_failures.clear()
        r = self.client.post("/api/auth/setup", json={
            "username": "admin", "displayName": "대표", "password": ADMIN_PW})
        assert r.status_code == 200
        self.cats = self.client.get("/api/categories").get_json()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers
    def make_supplier(self, name="테스트상사"):
        return self.client.post("/api/suppliers", json={"name": name}).get_json()

    def make_batch(self, supplier_id, amount=1000000, **kw):
        body = {"supplierId": supplier_id, "purchaseDate": "2026-07-28", "totalAmount": amount}
        body.update(kw)
        return self.client.post("/api/purchase-batches", json=body).get_json()

    def make_asset(self, **kw):
        body = {"categoryId": self.cats[0]["id"], "maker": "삼성", "model": "NT551",
                "grade": "SA", "purchasePrice": 300000, "qty": 1}
        body.update(kw)
        r = self.client.post("/api/assets", json=body)
        assert r.status_code == 201, r.get_data(as_text=True)
        return r.get_json()

    def make_order(self, **kw):
        body = {"recipient": "홍길동", "productName": "하프북 노트북", "phone": "010-1234-5678",
                "address": "서울시 강남구 테헤란로 1", "postalCode": "06000",
                "productCode": "NT551-i5", "optionName": "가방+마우스", "quantity": 1, "amount": 450000}
        body.update(kw)
        r = self.client.post("/api/orders", json=body)
        assert r.status_code == 201, r.get_data(as_text=True)
        return r.get_json()

    def stage(self, oid, action, value=True, client=None):
        return (client or self.client).patch(f"/api/orders/{oid}", json={"action": action, "value": value})

    def worker_client(self, username, perms):
        r = self.client.post("/api/users", json={
            "username": username, "displayName": username, "password": USER_PW,
            "perms": perms, "allCategories": True})
        assert r.status_code == 201
        c = self.app.test_client()
        assert c.post("/api/auth/login", json={"username": username, "password": USER_PW}).status_code == 200
        return c

    def db(self):
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn


class TestAssets(Base):
    def test_auto_numbering_and_manual_tms(self):
        """관리번호 = YYMMDD-NNNN (TMS와 같은 형식).

        ★2026-07-31부터 HMS는 5000번대부터 발번한다 — TMS와 함께 쓰는 동안
          같은 번호가 서로 다른 물건에 붙지 않게 번호대를 나눴다.
        """
        from app.purchase import HMS_ASSET_SEQ_START as START
        a1 = self.make_asset()[0]
        a2 = self.make_asset()[0]
        today = config.now().strftime("%y%m%d")
        self.assertEqual(a1["assetNo"], f"{today}-{START:04d}")
        self.assertEqual(a2["assetNo"], f"{today}-{START + 1:04d}")
        # TMS 이관 번호 직접 지정
        t = self.make_asset(assetNo="TMS-2023-0777")[0]
        self.assertEqual(t["assetNo"], "TMS-2023-0777")
        # 수동 번호가 있어도 자동 발번 순번은 이어진다
        a3 = self.make_asset()[0]
        self.assertEqual(a3["assetNo"], f"{today}-{START + 2:04d}")
        # 중복 번호 409
        r = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "assetNo": "TMS-2023-0777", "qty": 1})
        self.assertEqual(r.status_code, 409)

    def test_bulk_create_and_rename_history(self):
        created = self.make_asset(qty=3)
        self.assertEqual(len(created), 3)
        aid = created[0]["id"]
        # 자산번호 수정(TMS 이관) → 이력 기록
        r = self.client.patch(f"/api/assets/{aid}", json={"assetNo": "TMS-0001"})
        self.assertEqual(r.status_code, 200)
        detail = self.client.get(f"/api/assets/{aid}").get_json()
        self.assertEqual(detail["assetNo"], "TMS-0001")
        actions = [e["action"] for e in detail["events"]]
        self.assertIn("번호변경", actions)
        self.assertIn("등록", actions)

    def test_repair_cost_accumulates(self):
        aid = self.make_asset(purchasePrice=200000)[0]["id"]
        self.client.post(f"/api/assets/{aid}/repairs", json={"description": "SSD 교체", "cost": 50000})
        self.client.post(f"/api/assets/{aid}/repairs", json={"description": "배터리", "cost": 30000})
        d = self.client.get(f"/api/assets/{aid}").get_json()
        self.assertEqual(d["repairTotal"], 80000)
        self.assertEqual(d["costTotal"], 280000)
        self.assertIn("수리", [e["action"] for e in d["events"]])

    def test_category_scope_hides_assets(self):
        self.make_asset(categoryId=self.cats[0]["id"])
        self.make_asset(categoryId=self.cats[1]["id"])
        # cats[0]만 담당하는 사용자
        r = self.client.post("/api/users", json={
            "username": "scoped1", "displayName": "스코프", "password": USER_PW,
            "perms": ["purchase.view"], "categoryIds": [self.cats[0]["id"]]})
        self.assertEqual(r.status_code, 201)
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "scoped1", "password": USER_PW})
        rows = c.get("/api/assets").get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["categoryId"], self.cats[0]["id"])

    def test_supplier_batch_flow(self):
        s = self.make_supplier()
        b = self.make_batch(s["id"], amount=5000000)
        a = self.make_asset(batchId=b["id"], purchasePrice=250000)[0]
        batches = self.client.get("/api/purchase-batches").get_json()
        self.assertEqual(batches[0]["assetCount"], 1)
        self.assertEqual(batches[0]["assignedAmount"], 250000)
        d = self.client.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(d["batch"]["supplierName"], "테스트상사")


class TestSchemaUpgrade(unittest.TestCase):
    """구버전 DB를 열었을 때 마이그레이션이 동작하는지.

    2026-07-28 사고: 신규 설치만 테스트해서, 기존 DB에서 '컬럼 추가 전에 인덱스 생성'
    순서로 서버가 기동 실패했다(no such column: stage). 업그레이드 경로를 반드시 검증한다.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hms-upg-"))
        self.db_path = self.tmp / "old.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_opens_pre_tms_database(self):
        # TMS 반영 전 스키마(구버전)를 흉내낸 DB를 만든다
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript("""
            CREATE TABLE categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
              sort INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE TABLE suppliers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
              contact TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
              memo TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE TABLE purchase_batches (id INTEGER PRIMARY KEY AUTOINCREMENT,
              supplier_id INTEGER NOT NULL, purchase_date TEXT NOT NULL,
              total_amount INTEGER NOT NULL DEFAULT 0, memo TEXT NOT NULL DEFAULT '',
              created_by TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE assets (id INTEGER PRIMARY KEY AUTOINCREMENT, asset_no TEXT NOT NULL UNIQUE,
              batch_id INTEGER, category_id INTEGER, maker TEXT NOT NULL DEFAULT '',
              model TEXT NOT NULL DEFAULT '', serial TEXT NOT NULL DEFAULT '',
              grade TEXT NOT NULL DEFAULT '등급미정', purchase_price INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'in_stock', notes TEXT NOT NULL DEFAULT '',
              created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO categories(name, sort, created_at) VALUES('노트북', 0, '2026-01-01T00:00:00+09:00');
            INSERT INTO suppliers(name, created_at) VALUES('구거래처', '2026-01-01T00:00:00+09:00');
            INSERT INTO purchase_batches(supplier_id, purchase_date, total_amount, created_by, created_at)
              VALUES(1, '2026-01-02', 500000, '대표', '2026-01-02T00:00:00+09:00');
            INSERT INTO assets(asset_no, batch_id, category_id, model, purchase_price, created_at, updated_at)
              VALUES('202601020001', 1, 1, '구모델', 300000, '2026-01-02T00:00:00+09:00', '2026-01-02T00:00:00+09:00');
        """)
        conn.commit()
        conn.close()

        # 새 코드로 열기 — 여기서 예외가 나면 서버가 기동하지 못한다
        app = create_app(db_path=self.db_path)
        app.testing = True
        c = app.test_client()
        self.assertEqual(c.get("/api/health").status_code, 200)

        # 기존 데이터 보존 + 새 컬럼 기본값
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        a = conn.execute("SELECT * FROM assets WHERE asset_no='202601020001'").fetchone()
        self.assertEqual(a["model"], "구모델")
        self.assertEqual(a["cpu"], "")
        self.assertEqual(a["sale_price"], 0)
        b = conn.execute("SELECT * FROM purchase_batches WHERE id=1").fetchone()
        self.assertEqual(b["stage"], "purchased")
        self.assertEqual(b["slip_no"], "")
        self.assertEqual(b["paid"], 1)
        # 기존 카테고리는 유지(시드로 덮어쓰지 않음)
        names = [r["name"] for r in conn.execute("SELECT name FROM categories").fetchall()]
        self.assertEqual(names, ["노트북"])
        # 마이그레이션 후 인덱스가 실제로 만들어졌는지
        idx = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        self.assertIn("idx_batches_stage", idx)
        self.assertIn("idx_batches_slip", idx)
        conn.close()

        # 재기동(같은 DB 두 번 열기)도 안전해야 한다
        app2 = create_app(db_path=self.db_path)
        app2.testing = True
        self.assertEqual(app2.test_client().get("/api/health").status_code, 200)


class TestTmsPurchase(Base):
    """TMS 구조 반영 — 가입고 2단계 / 스펙 필드 / 등급·상태 분리 / 엑셀 이관."""

    def test_spec_fields_roundtrip(self):
        a = self.make_asset(cpu="Intel Core i5-8350U", gpu="Intel UHD Graphics 620",
                            ram="D4 8G", ssd="NVMe 256G", inch="14", battery="O",
                            charger="X", location="A-1", salePrice=350000)[0]
        d = self.client.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(d["cpu"], "Intel Core i5-8350U")
        self.assertEqual(d["gpu"], "Intel UHD Graphics 620")
        self.assertEqual(d["ram"], "D4 8G")
        self.assertEqual(d["ssd"], "NVMe 256G")
        self.assertEqual(d["inch"], "14")
        self.assertEqual(d["battery"], "O")
        self.assertEqual(d["charger"], "X")
        self.assertEqual(d["location"], "A-1")
        self.assertEqual(d["salePrice"], 350000)

    def test_grade_list_matches_tms_without_work_states(self):
        meta = self.client.get("/api/purchase-meta").get_json()
        self.assertIn("S+A", meta["grades"])
        self.assertIn("C급", meta["grades"])
        # 작업상태는 등급이 아니라 상태로
        for work in ("수리", "A/S", "불량", "도색대기"):
            self.assertNotIn(work, meta["grades"])
        labels = [s["label"] for s in meta["statuses"]]
        for work in ("수리", "A/S", "불량", "도색대기"):
            self.assertIn(work, labels)
        # 잘못된 등급은 거부
        r = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "grade": "수리", "qty": 1})
        self.assertEqual(r.status_code, 400)

    def test_work_status_excluded_from_matching(self):
        a = self.make_asset()[0]
        self.client.patch(f"/api/assets/{a['id']}", json={"status": "repair"})
        o = self.make_order()
        r = self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("수리", r.get_json()["error"])

    def test_provisional_to_purchase_flow(self):
        s = self.make_supplier()
        # 1) 가입고 전표(V) — 거래처 없이도 생성 가능
        v = self.client.post("/api/purchase-batches", json={
            "stage": "provisional", "purchaseDate": "2026-07-28"}).get_json()
        self.assertTrue(v["slipNo"].startswith("V"))
        self.assertEqual(v["stage"], "provisional")
        a = self.make_asset(batchId=v["id"])[0]
        # 2) 매입 확정 → P전표 부여 + 입고확인 기록
        r = self.client.post(f"/api/purchase-batches/{v['id']}/confirm", json={
            "supplierId": s["id"], "totalAmount": 300000, "paid": False,
            "channel": "직거래"})   # 매입방법(구 매입채널)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["slipNo"].startswith("P"))
        d = self.client.get(f"/api/purchase-batches/{v['id']}").get_json()
        self.assertEqual(d["stage"], "purchased")
        self.assertFalse(d["paid"])
        self.assertTrue(d["confirmedAt"])
        self.assertEqual(d["channel"], "직거래")
        self.assertEqual(len(d["assets"]), 1)
        # 신청자 필드는 없애고 등록자/확인자로 대체
        self.assertNotIn("requester", d)
        self.assertEqual(d["createdBy"], "대표")
        self.assertEqual(d["confirmedBy"], "대표")
        # 자산 이력에 입고확인 기록
        events = [e["action"] for e in self.client.get(f"/api/assets/{a['id']}").get_json()["events"]]
        self.assertIn("입고확인", events)
        # 이미 확정된 전표는 재확정 불가
        self.assertEqual(self.client.post(f"/api/purchase-batches/{v['id']}/confirm",
                                          json={}).status_code, 400)

    def test_unpaid_filter(self):
        s = self.make_supplier()
        self.make_batch(s["id"], paid=False)
        self.make_batch(s["id"], paid=True)
        unpaid = self.client.get("/api/purchase-batches?unpaid=1").get_json()
        self.assertEqual(len(unpaid), 1)
        self.assertFalse(unpaid[0]["paid"])

    def test_slip_numbering(self):
        """전표번호도 HMS 몫(500번대)부터 — TMS와 겹치지 않게."""
        from app.purchase import HMS_SLIP_SEQ_START as START
        s = self.make_supplier()
        b1 = self.make_batch(s["id"])
        b2 = self.make_batch(s["id"])
        today = config.now().strftime("%y%m%d")
        self.assertEqual(b1["slipNo"], f"P{today}-{START:03d}")
        self.assertEqual(b2["slipNo"], f"P{today}-{START + 1:03d}")

    def _migration_xlsx(self, rows):
        headers = ["관리번호", "대분류", "브랜드", "모델명", "시리얼번호", "매입가", "판매가", "등급",
                   "보관위치", "CPU", "그래픽", "RAM", "SSD", "인치", "배터리효율", "충전기유무",
                   "재고상태", "매입상세비고"]
        return write_xlsx(headers, rows)

    def test_tms_migration(self):
        content = self._migration_xlsx([
            ["260719-0027", "PC", "LENOVO", "L470", "", "77000", "150000", "AA", "A-1",
             "Intel Core i5-7300U", "Intel HD Graphics 620", "D4 8G", "2.5\" 256G", "14",
             "O", "X", "재고", ""],
            # 등급 자리에 작업상태가 들어온 경우 → 상태로 옮기고 등급은 미정
            ["260722-0004", "PC", "LENOVO", "L480", "SN123", "88000", "0", "수리", "",
             "Intel Core i5-8350U", "", "D4 32G", "NVMe 256G", "14", "O", "O", "", "화면 줄"],
        ])
        data = {"files": (io.BytesIO(content), "tms.xlsx")}
        r = self.client.post("/api/assets/migrate/preview", data=data,
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["toCreate"], 2)
        self.assertEqual(len(self.client.get("/api/assets").get_json()), 0)  # 미리보기는 저장 안 함

        data = {"files": (io.BytesIO(content), "tms.xlsx")}
        r = self.client.post("/api/assets/migrate", data=data, content_type="multipart/form-data")
        self.assertEqual(r.get_json()["created"], 2)
        assets = {a["assetNo"]: a for a in self.client.get("/api/assets").get_json()}
        self.assertEqual(assets["260719-0027"]["grade"], "AA")
        self.assertEqual(assets["260719-0027"]["status"], "ready")
        self.assertEqual(assets["260719-0027"]["ram"], "D4 8G")
        self.assertEqual(assets["260719-0027"]["salePrice"], 150000)
        # 등급 '수리' → 상태 repair + 등급 미정
        self.assertEqual(assets["260722-0004"]["grade"], "미정")
        self.assertEqual(assets["260722-0004"]["status"], "repair")

        # 재실행해도 중복 생성되지 않는다
        data = {"files": (io.BytesIO(content), "tms.xlsx")}
        r = self.client.post("/api/assets/migrate", data=data, content_type="multipart/form-data")
        self.assertEqual(r.get_json()["created"], 0)
        self.assertEqual(r.get_json()["duplicates"], 2)
        self.assertEqual(len(self.client.get("/api/assets").get_json()), 2)

    def test_asset_export(self):
        self.make_asset(cpu="i5", ram="8G")
        r = self.client.get("/api/assets/export")
        self.assertEqual(r.status_code, 200)
        self.assertGreater(len(r.data), 500)
        self.assertTrue(r.data.startswith(b"PK"))  # xlsx = zip


class TestOrderStateMachine(Base):
    def test_stage_flow_and_guards(self):
        o = self.make_order()
        oid = o["id"]
        # 검수는 제작 완료 전 불가(409 + 최신 주문 동봉)
        r = self.stage(oid, "softwareInspection")
        self.assertEqual(r.status_code, 409)
        self.assertIn("order", r.get_json())
        # 정상 흐름
        self.assertEqual(self.stage(oid, "preparing").status_code, 200)
        self.assertEqual(self.stage(oid, "production").status_code, 200)
        self.assertEqual(self.stage(oid, "softwareInspection").status_code, 200)
        # 검수 완료 후 제작 해제 불가
        self.assertEqual(self.stage(oid, "production", False).status_code, 409)
        # 검수 해제 → 출고 연쇄 해제 확인
        aid = self.make_asset()[0]["id"]
        self.client.patch(f"/api/orders/{oid}", json={"action": "assets", "assetIds": [aid]})
        self.assertEqual(self.stage(oid, "shipping").status_code, 200)
        self.assertEqual(self.stage(oid, "softwareInspection", False).status_code, 200)
        detail = self.client.get(f"/api/orders/{oid}").get_json()
        self.assertFalse(detail["shippingDone"])

    def test_preparing_claim_conflict(self):
        o = self.make_order()
        w1 = self.worker_client("worker1", ["orders.view", "orders.work"])
        w2 = self.worker_client("worker2", ["orders.view", "orders.work"])
        self.assertEqual(self.stage(o["id"], "preparing", True, w1).status_code, 200)
        # 타인이 준비 중 → 체크/해제 모두 409
        r = self.stage(o["id"], "preparing", True, w2)
        self.assertEqual(r.status_code, 409)
        self.assertIn("worker1", r.get_json()["error"])
        self.assertEqual(self.stage(o["id"], "preparing", False, w2).status_code, 409)
        # 본인 해제는 가능
        self.assertEqual(self.stage(o["id"], "preparing", False, w1).status_code, 200)
        # 관리자는 타인 점유 해제 가능
        self.assertEqual(self.stage(o["id"], "preparing", True, w2).status_code, 200)
        self.assertEqual(self.stage(o["id"], "preparing", False, self.client).status_code, 200)

    def test_asset_matching_and_conflicts(self):
        o1 = self.make_order()
        o2 = self.make_order(recipient="김철수")
        aid = self.make_asset()[0]["id"]
        r = self.client.patch(f"/api/orders/{o1['id']}", json={"action": "assets", "assetIds": [aid]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["assets"][0]["status"], "reserved")
        # 같은 자산을 다른 주문에 매칭 → 409 (어느 주문에 있는지 안내)
        r = self.client.patch(f"/api/orders/{o2['id']}", json={"action": "assets", "assetIds": [aid]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("주문", r.get_json()["error"])
        # 매칭 해제 → 매칭 전 상태(등록 직후 in_stock)로 복귀
        r = self.client.patch(f"/api/orders/{o1['id']}", json={"action": "assets", "assetIds": []})
        self.assertEqual(r.status_code, 200)
        a = self.client.get(f"/api/assets/{aid}").get_json()
        self.assertEqual(a["status"], "in_stock")

    def test_shipping_marks_assets_and_cancel_releases(self):
        o = self.make_order()
        aid = self.make_asset()[0]["id"]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [aid]})
        self.stage(o["id"], "production")
        self.stage(o["id"], "softwareInspection")
        self.stage(o["id"], "shipping")
        self.assertEqual(self.client.get(f"/api/assets/{aid}").get_json()["status"], "shipped")
        # 출고 해제 → reserved 복귀
        self.stage(o["id"], "shipping", False)
        self.assertEqual(self.client.get(f"/api/assets/{aid}").get_json()["status"], "reserved")
        # 주문 취소 → 자산 해제(매칭 전 상태로 복귀)
        r = self.client.patch(f"/api/orders/{o['id']}", json={"action": "cancel", "reason": "고객 변심"})
        self.assertEqual(r.status_code, 200)
        a = self.client.get(f"/api/assets/{aid}").get_json()
        self.assertEqual(a["status"], "in_stock")
        self.assertIn("매칭해제", [e["action"] for e in a["events"]])

    def test_details_optimistic_lock(self):
        o = self.make_order()
        r = self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "details", "expectedUpdatedAt": "2000-01-01T00:00:00+09:00",
            "fields": {"memo": "낡은 화면에서 수정"}})
        self.assertEqual(r.status_code, 409)
        r = self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "details", "expectedUpdatedAt": o["updatedAt"],
            "fields": {"memo": "정상 수정"}})
        self.assertEqual(r.status_code, 200)


class TestImport(Base):
    def _godo_xlsx(self, rows):
        # 실제 고도몰 내보내기 양식 컬럼명 사용 (주소 = "수취인 주소")
        headers = ["주문 번호", "상품명", "상품수량", "판매가", "수취인 이름", "수취인 핸드폰 번호", "수취인 주소"]
        return write_xlsx(headers, rows)

    def test_import_and_dedupe(self):
        content = self._godo_xlsx([
            ["G-1001", "하프북 NT551", "1", "450000", "홍길동", "010-1111-2222", "서울시 강남구"],
            ["G-1002", "하프북 그램", "2", "900000", "김철수", "010-3333-4444", "부산시 해운대구"],
        ])
        data = {"files": (io.BytesIO(content), "godo.xlsx")}
        r = self.client.post("/api/orders/import", data=data, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        res = r.get_json()
        self.assertEqual(res["added"], 2)
        # 같은 파일 재업로드 → 전부 중복
        data = {"files": (io.BytesIO(content), "godo.xlsx")}
        r = self.client.post("/api/orders/import", data=data, content_type="multipart/form-data")
        self.assertEqual(r.get_json()["added"], 0)
        self.assertEqual(r.get_json()["duplicates"], 2)
        orders = self.client.get("/api/orders").get_json()["orders"]
        self.assertEqual(len(orders), 2)
        self.assertEqual(orders[0]["channel"], "고도몰")

    def test_import_preview_no_write(self):
        content = self._godo_xlsx([["G-2001", "노트북", "1", "1000", "박영희", "010-5555-6666", "대구"]])
        data = {"files": (io.BytesIO(content), "godo.xlsx")}
        r = self.client.post("/api/orders/import/preview", data=data, content_type="multipart/form-data")
        self.assertEqual(r.get_json()["added"], 1)
        self.assertEqual(len(self.client.get("/api/orders").get_json()["orders"]), 0)

    def test_shipping_update_detected(self):
        c1 = self._godo_xlsx([["G-3001", "노트북", "1", "1000", "이몽룡", "010-1", "서울시 A"]])
        self.client.post("/api/orders/import", data={"files": (io.BytesIO(c1), "a.xlsx")},
                         content_type="multipart/form-data")
        c2 = self._godo_xlsx([["G-3001", "노트북", "1", "1000", "이몽룡", "010-1", "서울시 B(변경됨)"]])
        r = self.client.post("/api/orders/import", data={"files": (io.BytesIO(c2), "b.xlsx")},
                             content_type="multipart/form-data")
        self.assertEqual(r.get_json()["shippingUpdates"], 1)
        o = self.client.get("/api/orders").get_json()["orders"][0]
        self.assertIsNotNone(o["pendingShippingUpdate"])
        # 반영
        r = self.client.patch(f"/api/orders/{o['id']}", json={"action": "applyShippingUpdate"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("변경됨", r.get_json()["address"])
        self.assertIsNone(r.get_json()["pendingShippingUpdate"])


class TestWaybill(Base):
    def _ready_order(self):
        o = self.make_order()
        aid = self.make_asset()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [aid["id"]]})
        self.stage(o["id"], "production")
        self.stage(o["id"], "softwareInspection")
        return o, aid

    def test_waybill_requires_qc_and_assets(self):
        o = self.make_order()
        r = self.client.post(f"/api/orders/{o['id']}/waybill", json={})
        self.assertEqual(r.status_code, 400)  # QC 미완료
        self.stage(o["id"], "production")
        self.stage(o["id"], "softwareInspection")
        r = self.client.post(f"/api/orders/{o['id']}/waybill", json={})
        self.assertEqual(r.status_code, 400)  # 자산 미매칭
        self.assertIn("자산", r.get_json()["error"])

    def test_waybill_test_issue_contains_asset_no(self):
        o, aid = self._ready_order()
        r = self.client.post(f"/api/orders/{o['id']}/waybill", json={})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        res = r.get_json()
        self.assertTrue(res["simulated"])  # CJ 미설정 → 테스트 발행
        self.assertTrue(res["invoiceNo"].startswith("999"))
        wbs = self.client.get("/api/waybills").get_json()
        self.assertEqual(len(wbs), 1)
        items = wbs[0]["items"]
        # 송장 상품명에 채널·제품코드·자산번호·옵션 표기 (대표 요구)
        self.assertIn("[테스트발행]", items)
        self.assertIn("NT551-i5", items)
        self.assertIn(aid["assetNo"], items)
        self.assertIn("가방+마우스", items)
        # 주문에 송장번호 기록
        od = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(od["trackingNumber"], res["invoiceNo"])

    def test_waybill_pdf_renders(self):
        o, _ = self._ready_order()
        wid = self.client.post(f"/api/orders/{o['id']}/waybill", json={}).get_json()["wid"]
        r = self.client.get(f"/api/waybills/{wid}/pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "application/pdf")
        self.assertGreater(len(r.data), 10000)
        self.assertTrue(r.data.startswith(b"%PDF"))

    def test_waybill_cancel_test(self):
        o, _ = self._ready_order()
        res = self.client.post(f"/api/orders/{o['id']}/waybill", json={}).get_json()
        r = self.client.post(f"/api/waybills/{res['wid']}/cancel")
        self.assertEqual(r.status_code, 200)
        od = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(od["trackingNumber"], "")


class TestReviewFixesP123(Base):
    """2026-07-28 Phase 1~3 적대적 리뷰에서 확인된 결함들의 회귀 테스트."""

    def _ready_order(self, **asset_kw):
        o = self.make_order()
        a = self.make_asset(**asset_kw)[0]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.stage(o["id"], "production")
        self.stage(o["id"], "softwareInspection")
        return o, a

    def test_inspection_uncheck_reverts_shipped_asset(self):
        """검수 해제 → 출고 연쇄 해제 시 자산도 reserved로 원복(shipped 고착 방지)."""
        o, a = self._ready_order()
        self.stage(o["id"], "shipping")
        self.assertEqual(self.client.get(f"/api/assets/{a['id']}").get_json()["status"], "shipped")
        self.stage(o["id"], "softwareInspection", False)
        d = self.client.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(d["status"], "reserved")
        # 이후 매칭 해제하면 정상적으로 재고 복귀(매칭 전 상태)
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": []})
        self.assertEqual(self.client.get(f"/api/assets/{a['id']}").get_json()["status"], "in_stock")

    def test_match_failure_is_all_or_nothing(self):
        """매칭 중 하나라도 실패하면 409 + 아무것도 반영되지 않아야 한다."""
        o1 = self.make_order()
        o2 = self.make_order(recipient="김철수")
        good = self.make_asset()[0]
        taken = self.make_asset()[0]
        self.client.patch(f"/api/orders/{o1['id']}", json={"action": "assets", "assetIds": [taken["id"]]})
        # o2에 [good, taken] 매칭 시도 → taken 때문에 409, good도 반영되면 안 됨
        r = self.client.patch(f"/api/orders/{o2['id']}",
                              json={"action": "assets", "assetIds": [good["id"], taken["id"]]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.client.get(f"/api/assets/{good['id']}").get_json()["status"], "in_stock")
        self.assertEqual(len(self.client.get(f"/api/orders/{o2['id']}").get_json()["assets"]), 0)

    def test_unmatch_restores_previous_status(self):
        """정비중 자산을 매칭했다 해제하면 '판매가능'이 아니라 '정비중'으로 복귀."""
        a = self.make_asset()[0]
        self.client.patch(f"/api/assets/{a['id']}", json={"status": "refurbishing"})
        o = self.make_order()
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": []})
        self.assertEqual(self.client.get(f"/api/assets/{a['id']}").get_json()["status"], "refurbishing")

    def test_cancel_blocked_while_shipped_or_waybill_active(self):
        """출고 확인·발행 송장이 남은 채로 취소하면 출고된 자산이 재고로 둔갑 → 차단."""
        o, a = self._ready_order()
        self.stage(o["id"], "shipping")
        r = self.client.patch(f"/api/orders/{o['id']}", json={"action": "cancel", "reason": "테스트"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("출고 확인", r.get_json()["error"])
        self.stage(o["id"], "shipping", False)
        # 송장 발행 후에도 차단
        wb = self.client.post(f"/api/orders/{o['id']}/waybill", json={}).get_json()
        r = self.client.patch(f"/api/orders/{o['id']}", json={"action": "cancel", "reason": "테스트"})
        self.assertEqual(r.status_code, 400)
        self.assertIn(wb["wid"], r.get_json()["error"])
        # 송장 취소 후에는 취소 가능하고 자산도 복귀
        self.client.post(f"/api/waybills/{wb['wid']}/cancel")
        r = self.client.patch(f"/api/orders/{o['id']}", json={"action": "cancel", "reason": "테스트"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get(f"/api/assets/{a['id']}").get_json()["status"], "in_stock")

    def test_duplicate_waybill_blocked(self):
        o, _ = self._ready_order()
        r1 = self.client.post(f"/api/orders/{o['id']}/waybill", json={})
        self.assertEqual(r1.status_code, 201)
        r2 = self.client.post(f"/api/orders/{o['id']}/waybill", json={})
        self.assertEqual(r2.status_code, 409)
        self.assertIn(r1.get_json()["wid"], r2.get_json()["error"])
        # 취소 후에는 재발행 가능
        self.client.post(f"/api/waybills/{r1.get_json()['wid']}/cancel")
        self.assertEqual(self.client.post(f"/api/orders/{o['id']}/waybill", json={}).status_code, 201)

    def test_label_quantity_not_polluted_by_model_names(self):
        """'RTX3060'·'8Gx2' 같은 표기가 라벨 수량으로 오집계되지 않아야 한다."""
        import re as _re
        o = self.make_order(productCode="LG-15GD870-RTX3060", optionName="램 8Gx2 + 마우스", quantity=2)
        a = self.make_asset()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.stage(o["id"], "production")
        self.stage(o["id"], "softwareInspection")
        self.client.post(f"/api/orders/{o['id']}/waybill", json={})
        items = self.client.get("/api/waybills").get_json()[0]["items"]
        # 동결 렌더러는 item_summary의 x숫자를 전부 더해 수량 칸에 인쇄한다
        printed_qty = sum(int(n) for n in _re.findall(r"[xX]\s*(\d+)", items))
        self.assertEqual(printed_qty, 2)
        # 사람이 읽는 정보는 유지(x만 ×로 치환)
        self.assertIn("LG-15GD870-RT×3060", items)
        self.assertIn("8G×2", items)

    def test_label_carries_code_title_assets_and_options(self):
        """★송장에 쇼핑몰·상품코드·제목·옵션·수량·자산번호가 함께 찍혀야 한다.

        대표 지시(2026-07-29): 셋팅·QC뿐 아니라 포장 담당도 이 종이만 보고 대조한다.
        특히 상품코드가 있어도 제목을 버리면 안 된다 — 몰마다 어느 쪽이 '진짜 모델'인지
        달라서, 코드만 찍히면 무슨 물건인지 알 수 없는 몰이 생긴다.
        """
        import re as _re
        o = self.make_order(channel="카카오쇼핑", productCode="KKO-NT371",
                            productName="삼성 15인치 리퍼 노트북", quantity=2)
        a1 = self.make_asset()[0]
        a2 = self.make_asset()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "assets", "assetIds": [a1["id"], a2["id"]]})
        self.stage(o["id"], "production")
        self.stage(o["id"], "softwareInspection")
        self.client.post(f"/api/orders/{o['id']}/waybill", json={})
        items = self.client.get("/api/waybills").get_json()[0]["items"]

        self.assertIn("카카오쇼핑", items, "쇼핑몰이 없다")
        self.assertIn("KKO-NT371", items, "자체상품코드가 없다")
        self.assertIn("삼성 15인치 리퍼 노트북", items, "상품 제목이 없다(코드만 찍혔다)")
        for asset in (a1, a2):
            self.assertIn(asset["assetNo"], items, "자산번호가 빠졌다 — 포장 대조를 못 한다")
        # 자산번호·모델명이 섞여도 수량 칸은 주문 수량 그대로여야 한다
        self.assertEqual(sum(int(n) for n in _re.findall(r"[xX]\s*(\d+)", items)), 2)
        self.assertLessEqual(len(items), 120, "상품명 칸(120자)을 넘으면 잘려서 인쇄된다")

    def test_label_keeps_customer_request_in_remark(self):
        """메모 칸의 고객 요청('부재시 경비실')을 우리 문구로 덮으면 배송 사고가 난다."""
        o = self.make_order(deliveryMessage="부재시 경비실에 맡겨주세요")
        a = self.make_asset()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.stage(o["id"], "production")
        self.stage(o["id"], "softwareInspection")
        self.client.post(f"/api/orders/{o['id']}/waybill", json={})
        conn = self.db()
        raw = conn.execute(
            "SELECT label FROM waybills ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        conn.close()
        remark = (json.loads(raw or "{}") or {}).get("remark", "")
        self.assertIn("경비실", remark, "고객 배송 요청이 송장에서 사라졌다")

    def test_ids_unique_after_cancel_and_gap(self):
        """취소된 송장이 있어도 wid·테스트 송장번호가 재사용되지 않는다(MAX 기준)."""
        o1, _ = self._ready_order()
        w1 = self.client.post(f"/api/orders/{o1['id']}/waybill", json={}).get_json()
        self.client.post(f"/api/waybills/{w1['wid']}/cancel")
        o2, _ = self._ready_order()
        w2 = self.client.post(f"/api/orders/{o2['id']}/waybill", json={}).get_json()
        self.assertNotEqual(w1["invoiceNo"], w2["invoiceNo"])
        self.assertNotEqual(w1["wid"], w2["wid"])
        # 중간 행이 사라져도 남아 있는 ID와 충돌하지 않는다(PK 충돌 방지)
        conn = self.db()
        conn.execute("DELETE FROM waybills WHERE wid=?", (w1["wid"],))
        conn.commit()
        conn.close()
        o3, _ = self._ready_order()
        w3 = self.client.post(f"/api/orders/{o3['id']}/waybill", json={})
        self.assertEqual(w3.status_code, 201)
        self.assertNotIn(w3.get_json()["wid"], (w1["wid"], w2["wid"]))

    def test_production_respects_preparing_claim(self):
        o = self.make_order()
        w1 = self.worker_client("workerA", ["orders.view", "orders.work"])
        w2 = self.worker_client("workerB", ["orders.view", "orders.work"])
        self.stage(o["id"], "preparing", True, w1)
        r = self.stage(o["id"], "production", True, w2)
        self.assertEqual(r.status_code, 409)
        self.assertIn("workerA", r.get_json()["error"])
        self.assertEqual(self.stage(o["id"], "production", True, w1).status_code, 200)

    def test_cancelled_order_cannot_match_assets(self):
        o = self.make_order()
        a = self.make_asset()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "cancel", "reason": "테스트"})
        r = self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.client.get(f"/api/assets/{a['id']}").get_json()["status"], "in_stock")

    def test_waybill_box_qty_recorded(self):
        o, _ = self._ready_order()
        self.client.post(f"/api/orders/{o['id']}/waybill", json={"boxQty": 3})
        self.assertEqual(self.client.get("/api/waybills").get_json()[0]["boxQty"], 3)


class TestAsService(Base):
    """A/S 접수 → 상태 진행 → 수리비 자산 원가 반영."""

    def _shipped_asset(self):
        o = self.make_order()
        a = self.make_asset()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.stage(o["id"], "production")
        self.stage(o["id"], "softwareInspection")
        self.stage(o["id"], "shipping")
        return o, a

    def test_ticket_flow_and_asset_history(self):
        o, a = self._shipped_asset()
        r = self.client.post("/api/as-tickets", json={
            "customer": "홍길동", "phone": "010-1111-2222", "symptom": "전원이 켜지지 않음",
            "asType": "repair", "chargeTo": "company", "assetId": a["id"], "orderId": o["id"]})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        tid = r.get_json()["id"]
        self.assertTrue(r.get_json()["ticketNo"].startswith("AS-"))
        # 자산 이력에 A/S 접수가 남는다
        events = [e["action"] for e in self.client.get(f"/api/assets/{a['id']}").get_json()["events"]]
        self.assertIn("A/S접수", events)
        # 상태 진행
        for st in ("collecting", "repairing", "done", "returned"):
            r = self.client.patch(f"/api/as-tickets/{tid}", json={"status": st})
            self.assertEqual(r.status_code, 200, st)
        d = self.client.get(f"/api/as-tickets/{tid}").get_json()
        self.assertEqual(d["status"], "returned")
        self.assertTrue(d["closedAt"])
        self.assertIn("상태변경", [e["action"] for e in d["events"]])

    def test_cost_to_asset_repair(self):
        o, a = self._shipped_asset()
        tid = self.client.post("/api/as-tickets", json={
            "customer": "김철수", "symptom": "액정 파손", "assetId": a["id"],
            "chargeTo": "company"}).get_json()["id"]
        self.client.patch(f"/api/as-tickets/{tid}", json={"cost": 120000, "result": "액정 교체"})
        r = self.client.post(f"/api/as-tickets/{tid}/to-asset-repair")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        detail = self.client.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(detail["repairTotal"], 120000)
        self.assertEqual(detail["costTotal"], detail["purchasePrice"] + 120000)
        # 같은 건을 두 번 반영하면 409
        self.assertEqual(self.client.post(f"/api/as-tickets/{tid}/to-asset-repair").status_code, 409)

    def test_customer_charge_not_pushed_to_cost(self):
        o, a = self._shipped_asset()
        tid = self.client.post("/api/as-tickets", json={
            "customer": "박영희", "symptom": "침수", "assetId": a["id"],
            "chargeTo": "customer"}).get_json()["id"]
        self.client.patch(f"/api/as-tickets/{tid}", json={"cost": 200000})
        r = self.client.post(f"/api/as-tickets/{tid}/to-asset-repair")
        self.assertEqual(r.status_code, 400)   # 유상은 자산 원가에 넣지 않는다
        self.assertEqual(self.client.get(f"/api/assets/{a['id']}").get_json()["repairTotal"], 0)

    def test_open_closed_filter_and_search(self):
        o, a = self._shipped_asset()
        t1 = self.client.post("/api/as-tickets", json={
            "customer": "고객A", "symptom": "소음", "assetId": a["id"]}).get_json()
        self.client.post("/api/as-tickets", json={"customer": "고객B", "symptom": "발열"})
        self.assertEqual(len(self.client.get("/api/as-tickets?view=open").get_json()), 2)
        self.client.patch(f"/api/as-tickets/{t1['id']}", json={"status": "cancelled"})
        self.assertEqual(len(self.client.get("/api/as-tickets?view=open").get_json()), 1)
        self.assertEqual(len(self.client.get("/api/as-tickets?view=closed").get_json()), 1)
        found = self.client.get("/api/as-tickets?view=all&q=발열").get_json()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["customer"], "고객B")

    def test_as_requires_permission(self):
        self.create_user_for_as = self.client.post("/api/users", json={
            "username": "noas", "displayName": "권한없음", "password": USER_PW,
            "perms": ["orders.view"], "allCategories": True})
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "noas", "password": USER_PW})
        self.assertEqual(c.get("/api/as-tickets").status_code, 403)
        self.assertEqual(c.post("/api/as-tickets", json={"customer": "x", "symptom": "y"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
