"""Phase 3 잔여(회수/추적) · Phase 4~5(몰 수집) · Phase 6(리포트) 테스트."""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth as auth_mod  # noqa: E402
from app import config, create_app  # noqa: E402
from app.malls.base import MallAdapter, MallError, get_adapter, register  # noqa: E402
from app.settings import _MASK as MASK  # noqa: E402

ADMIN_PW = "admin-pass-1"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-p456-"))
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

    def make_shipped_order(self, **kw):
        body = {"recipient": "홍길동", "productName": "노트북", "phone": "010-1111-2222",
                "address": "서울시 강남구 1", "postalCode": "06000", "amount": 500000}
        body.update(kw)
        o = self.client.post("/api/orders", json=body).get_json()
        a = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480",
            "purchasePrice": 200000, "qty": 1}).get_json()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "production", "value": True})
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "softwareInspection", "value": True})
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})
        return o, a


class TestRecall(Base):
    def test_recall_creates_waybill_and_marks_asset(self):
        o, a = self.make_shipped_order()
        r = self.client.post(f"/api/orders/{o['id']}/recall", json={"reason": "단순 변심"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        res = r.get_json()
        self.assertTrue(res["simulated"])          # CJ 미설정 → 테스트 예약
        self.assertTrue(res["wid"].startswith("WB-"))
        # 회수는 송장번호를 미리 채번하지 않는다(CJ 규칙)
        wb = [w for w in self.client.get("/api/waybills?type=recall").get_json()]
        self.assertEqual(len(wb), 1)
        self.assertEqual(wb[0]["invoiceNo"], "")
        self.assertEqual(wb[0]["type"], "recall")
        # 자산은 회수중으로
        asset = self.client.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(asset["status"], "returning")
        self.assertIn("회수예약", [e["action"] for e in asset["events"]])

    def test_recall_duplicate_blocked(self):
        o, _ = self.make_shipped_order()
        self.client.post(f"/api/orders/{o['id']}/recall", json={})
        r = self.client.post(f"/api/orders/{o['id']}/recall", json={})
        self.assertEqual(r.status_code, 409)

    def test_recall_received_returns_asset_to_stock(self):
        o, a = self.make_shipped_order()
        wid = self.client.post(f"/api/orders/{o['id']}/recall", json={}).get_json()["wid"]
        r = self.client.post(f"/api/waybills/{wid}/received", json={"status": "refurbishing"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["assets"], [a["assetNo"]])
        asset = self.client.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(asset["status"], "refurbishing")
        self.assertIn("회수입고", [e["action"] for e in asset["events"]])
        # 회수 송장은 완료 처리
        wb = self.client.get("/api/waybills?type=recall").get_json()[0]
        self.assertEqual(wb["status"], "delivered")

    def test_as_recall(self):
        o, a = self.make_shipped_order()
        t = self.client.post("/api/as-tickets", json={
            "customer": "홍길동", "phone": "010-1111-2222", "address": "서울시 강남구 1",
            "symptom": "액정 불량", "assetId": a["id"]}).get_json()
        r = self.client.post(f"/api/as-tickets/{t['id']}/recall", json={})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        # A/S 상태가 회수 중으로 바뀐다
        d = self.client.get(f"/api/as-tickets/{t['id']}").get_json()
        self.assertEqual(d["status"], "collecting")
        self.assertIn("회수예약", [e["action"] for e in d["events"]])

    def test_track_sync_requires_cj_config(self):
        r = self.client.post("/api/waybills/track-sync")
        self.assertEqual(r.status_code, 400)
        self.assertIn("CJ", r.get_json()["error"])


class TestMallCollect(Base):
    def test_mall_status_lists_all_malls(self):
        from app.malls import MALLS
        rows = self.client.get("/api/mall-status").get_json()
        self.assertEqual(len(rows), len(MALLS))
        self.assertTrue(all(m["implemented"] for m in rows))   # 전부 어댑터 구현됨
        godo = next(m for m in rows if m["code"] == "godomall")
        self.assertFalse(godo["ready"])            # 키가 없으므로 아직 수집 불가
        self.assertIn("사용", godo["reason"])

    def test_collect_blocked_without_keys(self):
        r = self.client.post("/api/malls/godomall/collect", json={"days": 1})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/malls/coupang/collect", json={"days": 1})
        self.assertEqual(r.status_code, 400)

    def test_collect_uses_dedupe_pipeline(self):
        """가짜 어댑터로 수집 → 기존 중복판정 로직을 그대로 탄다."""
        @register
        class FakeAdapter(MallAdapter):
            code = "fake"
            name = "테스트몰"
            required_keys = ("token",)

            def collect_orders(self, since, until):
                return [
                    self.order(orderNumber="F-1", productName="노트북A", recipient="김하나",
                               phone="010-1", address="서울 A", quantity=1, amount=100000),
                    self.order(orderNumber="F-2", productName="노트북B", recipient="이두리",
                               phone="010-2", address="서울 B", quantity=2, amount=200000),
                ]

        self.client.put("/api/settings", json={"malls": {"fake": {"token": "T", "enabled": True}}})
        # 미리보기는 저장하지 않는다
        r = self.client.post("/api/malls/fake/collect", json={"days": 1, "dryRun": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["added"], 2)
        self.assertEqual(len(self.client.get("/api/orders").get_json()["orders"]), 0)
        # 실제 수집
        r = self.client.post("/api/malls/fake/collect", json={"days": 1})
        self.assertEqual(r.get_json()["added"], 2)
        orders = self.client.get("/api/orders").get_json()["orders"]
        self.assertEqual(len(orders), 2)
        self.assertEqual(orders[0]["channel"], "테스트몰")
        # 다시 수집해도 중복 생성되지 않는다
        r = self.client.post("/api/malls/fake/collect", json={"days": 1})
        self.assertEqual(r.get_json()["added"], 0)
        self.assertEqual(r.get_json()["duplicates"], 2)
        self.assertEqual(len(self.client.get("/api/orders").get_json()["orders"]), 2)
        # 마지막 수집 기록이 남는다
        st = self.client.get("/api/mall-status").get_json()
        fake = next(m for m in st if m["code"] == "fake") if any(m["code"] == "fake" for m in st) else None
        # fake는 MALLS 레지스트리에 없으므로 mall-status에는 안 나온다(정상)
        self.assertIsNone(fake)

    def test_adapter_gate_reports_reason(self):
        adapter, why = get_adapter("godomall", {"enabled": False})
        self.assertIsNone(adapter)
        self.assertIn("사용", why)
        adapter, why = get_adapter("godomall", {"enabled": True})
        self.assertIsNone(adapter)
        self.assertIn("인증정보", why)
        adapter, why = get_adapter("godomall", {"enabled": True, "partner_key": "p", "user_key": "u"})
        self.assertIsNotNone(adapter)
        self.assertEqual(why, "")
        # 이제 8곳 모두 어댑터가 있으므로, 없는 코드로 '미구현' 안내를 확인한다
        adapter, why = get_adapter("nosuchmall", {"enabled": True})
        self.assertIsNone(adapter)
        self.assertIn("구현", why)


class TestReports(Base):
    def test_summary_margin(self):
        o, a = self.make_shipped_order(amount=500000)
        # 수리비 30,000 추가 → 원가 230,000
        self.client.post(f"/api/assets/{a['id']}/repairs", json={"description": "청소", "cost": 30000})
        s = self.client.get("/api/reports/summary").get_json()
        self.assertEqual(s["sales"]["orders"], 1)
        self.assertEqual(s["sales"]["revenue"], 500000)
        self.assertEqual(s["sales"]["buyCost"], 200000)
        self.assertEqual(s["sales"]["repairCost"], 30000)
        self.assertEqual(s["sales"]["cost"], 230000)
        self.assertEqual(s["sales"]["margin"], 270000)
        self.assertEqual(s["sales"]["marginRate"], 54.0)

    def test_channels_and_staff(self):
        self.make_shipped_order(channel="쿠팡", amount=300000)
        self.make_shipped_order(channel="쿠팡", amount=200000, recipient="김철수")
        self.make_shipped_order(channel="고도몰", amount=100000, recipient="박영희")
        chans = self.client.get("/api/reports/channels").get_json()
        top = chans[0]
        self.assertEqual(top["channel"], "쿠팡")
        self.assertEqual(top["orders"], 2)
        self.assertEqual(top["revenue"], 500000)
        staff = self.client.get("/api/reports/staff").get_json()
        self.assertEqual(staff[0]["name"], "대표")
        self.assertEqual(staff[0]["shipping"], 3)
        self.assertEqual(staff[0]["production"], 3)

    def test_aging_and_stock(self):
        self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "재고품", "purchasePrice": 150000, "qty": 2})
        s = self.client.get("/api/reports/summary").get_json()
        self.assertEqual(s["stock"]["assets"], 2)
        self.assertEqual(s["stock"]["amount"], 300000)
        aging = self.client.get("/api/reports/aging").get_json()
        self.assertEqual(len(aging), 2)
        self.assertIn("days", aging[0])

    def test_monthly_trend(self):
        self.make_shipped_order(amount=400000)
        rows = self.client.get("/api/reports/monthly").get_json()
        ym = config.now().strftime("%Y-%m")
        cur = next((r for r in rows if r["month"] == ym), None)
        self.assertIsNotNone(cur)
        self.assertEqual(cur["revenue"], 400000)
        self.assertEqual(cur["orders"], 1)

    def test_reports_require_permission(self):
        self.client.post("/api/users", json={
            "username": "noreport", "displayName": "권한없음", "password": "no-report-pw-1",
            "perms": ["orders.view"], "allCategories": True})
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "noreport", "password": "no-report-pw-1"})
        self.assertEqual(c.get("/api/reports/summary").status_code, 403)


class TestLotteonMapping(Base):
    """롯데온 실응답 매핑(2026-08-31 실주문으로 키 확정 — 주문번호/일시/상품코드만 오고
    수취인·연락처·주소·상품명·판매자코드가 비던 사고). 실키: dvpCustNm/dvpMphnNo/dvpZipNo/
    dvpStnmZipAddr+DtlAddr/spdNm/sitmNm/epdNo/odCmptDttm/slAmt. 이 행 모양이 바뀌면
    수집이 다시 반쪽이 된다 — 실측 응답 그대로(값만 가짜) 핀한다."""

    ROW = {
        "odNo": "2026083118432195", "odSeq": "1", "procSeq": "1",
        "odPrgsStepCd": "11", "odTypCd": "10", "odQty": 1.0,
        "odCmptDttm": "20260831143001",
        "odrNm": "주문자", "mphnNo": "01000000000",
        "dvpCustNm": "김수취", "dvpMphnNo": "01012345678", "dvpTelNo": "",
        "dvpZipNo": "06000", "dvpStnmZipAddr": "서울특별시 강남구 테헤란로 1",
        "dvpStnmDtlAddr": "101동 202호",
        "dvMsg": "문 앞에 놓아주세요",
        "spdNm": "레노버 씽크패드 X13 Gen3 초경량 노트북", "sitmNm": "단일옵션",
        "spdNo": "LO2746331973", "sitmNo": "LO2746331973_2746331974",
        "epdNo": "X13 Gen3_i5-12_내장 AA",
        "slPrc": 690000.0, "slAmt": 690000.0, "actualAmt": 652920.0,
        "fvrAmtSum": 37080.0, "prEntpShrAmtSum": 0.0, "prSfcoShrAmtSum": 29664.0,
        "pdAdtnOptJsn": [],
        "ifCplYN": "N",
    }

    def _adapter(self):
        from app.malls.lotteon import LotteonAdapter
        return LotteonAdapter({"api_key": "LK"})

    def test_실응답_행이_전부_매핑된다(self):
        out = self._adapter()._build_orders([dict(self.ROW)])
        self.assertEqual(len(out), 1)
        o = out[0]
        self.assertEqual(o["orderNumber"], "2026083118432195")
        self.assertEqual(o["orderedAt"], "2026-08-31 14:30:01", "주문일시(odCmptDttm)가 안 잡힌다")
        self.assertEqual(o["recipient"], "김수취", "수취인(dvpCustNm)이 안 잡힌다")
        self.assertEqual(o["phone"], "01012345678", "연락처(dvpMphnNo)가 안 잡힌다")
        self.assertEqual(o["postalCode"], "06000")
        self.assertEqual(o["address"], "서울특별시 강남구 테헤란로 1 101동 202호",
                         "주소(dvpStnmZipAddr+DtlAddr)가 안 잡힌다")
        self.assertEqual(o["productName"], "레노버 씽크패드 X13 Gen3 초경량 노트북",
                         "상품명(spdNm)이 안 잡힌다")
        # ★등급 꼬리(" AA")째 그대로 저장한다(2026-09-01 적대 리뷰) — 롯데온은 상품명·옵션
        # 어디에도 등급이 없어 이 꼬리가 유일한 등급 운반체다(떼면 영구 소실). 고도몰
        # 스펙·재고 대조는 sku_of/lookup_any 폴백이 꼬리를 떼어 맞춘다.
        self.assertEqual(o["productCode"], "X13 Gen3_i5-12_내장 AA",
                         "판매자 상품코드(epdNo)가 등급 꼬리째 보존돼야 한다")
        # 롯데온 내부번호는 제품코드가 아니라 몰 상품 식별자 축으로(대표 9/1)
        self.assertEqual(o["mallProductId"], "LO2746331973")
        self.assertEqual(o["mallItemId"], "LO2746331973_2746331974")
        self.assertEqual(o["optionName"], "단일옵션")
        self.assertEqual(o["deliveryMessage"], "문 앞에 놓아주세요")
        # 매출 = 판매금액(slAmt) − 업체 분담 할인(0) — 고객 실결제(652,920)로 과소잡지 않는다
        self.assertEqual(o["amount"], 690000)
        self.assertEqual(o["quantity"], 1)

    def test_업체_분담_할인은_매출에서_뺀다(self):
        row = dict(self.ROW)
        row["prEntpShrAmtSum"] = 20000.0
        o = self._adapter()._build_orders([row])[0]
        self.assertEqual(o["amount"], 670000, "우리(업체) 분담 쿠폰만 매출에서 빠져야 한다")

    def test_롯데온_내부번호는_제품코드_칸에_안_들어간다(self):
        """대표 9/1 "롯데온 고유코드는 필요하지 않아" — epdNo(판매자 코드 계열)가 없으면
        제품코드는 비워 둔다(LO번호가 박히면 고도몰 매칭만 막고, 빈 칸 보강도 못 받는다).
        내부번호는 mallProductId 로 보존된다."""
        row = dict(self.ROW)
        row["epdNo"] = ""
        o = self._adapter()._build_orders([row])[0]
        self.assertEqual(o["productCode"], "", "몰 내부번호가 제품코드 칸에 들어가면 안 된다")
        self.assertEqual(o["mallProductId"], "LO2746331973", "내부번호 보존 축이 비었다")

    def test_추가옵션_JSON이_옵션으로_붙는다(self):
        row = dict(self.ROW)
        row["pdAdtnOptJsn"] = '[{"optNm": "RAM 16GB 추가", "optPrc": 40000}]'
        o = self._adapter()._build_orders([row])[0]
        self.assertIn("RAM 16GB 추가", o["optionName"], "추가옵션(pdAdtnOptJsn)이 옵션에 안 남는다")

    def test_반쪽_저장된_기존_주문은_재수집이_채운다(self):
        """키 누락 사고로 상품명/전화/금액이 비어 저장된 주문(라이브 #2502)은
        고친 어댑터로 재수집하면 빈 칸이 보강된다 — 있는 값은 덮지 않는다."""
        from app.importers.dedupe import new_unique_orders
        existing = [{"importKey": "롯데온:2026083118432195", "orderNumber": "2026083118432195",
                     "channel": "롯데온", "recipient": "", "phone": "", "postalCode": "",
                     "address": "", "deliveryMessage": "", "productName": "",
                     "productCode": "LO2746331973", "orderedAt": "", "amount": 0,
                     "quantity": 1, "optionName": "", "archivedAt": "", "cancelledAt": "",
                     "shippingDone": False}]
        fetched = self._adapter()._build_orders([dict(self.ROW)])
        added, _updates = new_unique_orders(existing, fetched, now="2026-08-31T19:00:00")
        self.assertEqual(added, [], "같은 주문번호가 새 줄로 또 들어가면 안 된다")
        ex = existing[0]
        self.assertEqual(ex["productName"], "레노버 씽크패드 X13 Gen3 초경량 노트북",
                         "재수집이 빈 상품명을 안 채운다")
        self.assertEqual(ex["phone"], "01012345678")
        self.assertEqual(ex["orderedAt"], "2026-08-31 14:30:01")
        self.assertEqual(ex["amount"], 690000, "0원 금액이 재수집으로 안 채워진다")
        # ★박힌 몰 내부번호(LO번호)는 자사 코드 축(epdNo)으로 자동 교체된다(대표 9/1
        #   "롯데온 고유코드는 필요하지 않아" — 칸 비우는 수작업 없이 재수집이 바로잡음)
        self.assertEqual(ex["productCode"], "X13 Gen3_i5-12_내장 AA",
                         "몰 내부번호가 자사 코드로 승격 안 된다")
        # 수취인은 바로 덮지 않고 '배송지 변경 확인'으로 뜬다(운영자 승인 후 반영)
        self.assertIn("pendingShippingUpdate", ex)

    def test_코드_모양인_기존_제품코드는_덮지_않는다(self):
        """승격 규칙의 안전핀 — 사람이 넣은(또는 이미 올바른) 자사 코드는 재수집 값과
        달라도 절대 안 덮는다. 승격은 '몰 내부번호 → 자사 코드' 한 방향뿐이다."""
        from app.importers.dedupe import new_unique_orders
        existing = [{"importKey": "롯데온:2026083118432195", "orderNumber": "2026083118432195",
                     "channel": "롯데온", "recipient": "김수취", "phone": "01012345678",
                     "postalCode": "06000", "address": "서울특별시 강남구 테헤란로 1 101동 202호",
                     "deliveryMessage": "문 앞에 놓아주세요", "productName": "이미 있는 이름",
                     "productCode": "840 G3_i7-6_내장", "orderedAt": "2026-08-31 14:30:01",
                     "amount": 690000, "quantity": 1, "optionName": "단일옵션",
                     "archivedAt": "", "cancelledAt": "", "shippingDone": False}]
        fetched = self._adapter()._build_orders([dict(self.ROW)])
        new_unique_orders(existing, fetched, now="2026-09-01T12:00:00")
        self.assertEqual(existing[0]["productCode"], "840 G3_i7-6_내장",
                         "코드 모양인 기존 값이 재수집 값으로 덮였다")

    def test_보강_값이_DB까지_써진다(self):
        """merge 가 채운 상품명/주문일시/금액이 writeback UPDATE 에 실려 실제로 저장돼야
        한다(2026-09-01 발견: UPDATE 에 컬럼이 빠져 메모리에서만 채워지고 버려졌다 —
        재시작·재수집을 해도 화면이 그대로던 원인)."""
        import json as _json

        from app.db import tx
        from app.importers.dedupe import new_unique_orders
        from app.orders.mapping import insert_import_dict, row_to_import_dict, writeback_changed
        half = {"importKey": "롯데온:2026083118432195", "orderNumber": "2026083118432195",
                "channel": "롯데온", "sourceFile": "API:lotteon", "orderedAt": "",
                "productName": "", "optionName": "", "productCode": "LO2746331973",
                "quantity": 1, "amount": 0, "recipient": "", "phone": "",
                "postalCode": "", "address": "", "deliveryMessage": "", "memo": ""}
        fetched = self._adapter()._build_orders([dict(self.ROW)])
        with self.app.app_context():
            with tx(write=True) as conn:
                oid = insert_import_dict(conn, half, "시험")
                rows = conn.execute("SELECT * FROM orders").fetchall()
                existing = [row_to_import_dict(r) for r in rows]
                snapshot = {d["_rowId"]: _json.dumps(d, ensure_ascii=False,
                                                     sort_keys=True, default=str)
                            for d in existing}
                added, _u = new_unique_orders(existing, fetched, now="2026-09-01T10:00:00")
                self.assertEqual(added, [], "같은 주문이 새 줄로 들어가면 안 된다")
                changed = writeback_changed(conn, snapshot, existing)
                self.assertGreaterEqual(changed, 1, "보강 변경이 DB 반영 대상으로 안 잡혔다")
                row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        self.assertEqual(row["product_name"], "레노버 씽크패드 X13 Gen3 초경량 노트북",
                         "상품명 보강이 DB 에 안 써졌다")
        self.assertEqual(row["ordered_at"], "2026-08-31 14:30:01")
        self.assertEqual(row["amount"], 690000)
        self.assertEqual(row["phone"], "01012345678")
        self.assertEqual(row["address"], "서울특별시 강남구 테헤란로 1 101동 202호")
        self.assertTrue(row["pending_update"], "수취인은 '배송지 변경 확인'으로 떠야 한다")
        # 몰 내부번호 → 자사 코드 승격이 writeback 을 타고 DB까지 써져야 한다(대표 9/1)
        self.assertEqual(row["product_code"], "X13 Gen3_i5-12_내장 AA",
                         "제품코드 승격이 DB 에 안 써졌다")


class TestAllMallAdapters(Base):
    """몰 8곳 어댑터 — 키만 넣으면 바로 쓸 수 있어야 한다."""

    # 몰코드: 연결에 필요한 최소 키
    KEYS = {
        "godomall": {"partner_key": "P", "user_key": "U"},
        "coupang": {"vendor_id": "A001", "access_key": "AK", "secret_key": "SK", "wing_id": "W"},
        "smartstore": {"client_id": "CI", "client_secret": "CS"},
        # 카카오는 채널 구분(101=톡스토어 / 1=선물하기)이 필수다
        "kakao": {"agency_admin_key": "AA", "seller_rest_key": "SR", "channel_ids": "101"},
        "toss": {"client_id": "CI", "client_secret": "CS"},
        "st11": {"api_key": "K" * 32},
        "lotteon": {"api_key": "LK"},
        "esm": {"secret_key": "SK", "master_id": "MID"},
        "temu": {"app_key": "AK", "app_secret": "AS", "access_token": "AT"},
    }

    def test_all_malls_have_adapters(self):
        from app.malls.base import _IMPLS
        rows = self.client.get("/api/mall-status").get_json()
        self.assertEqual(len(rows), len(self.KEYS))   # 몰을 추가하면 KEYS에도 넣는다
        for r in rows:
            self.assertTrue(r["implemented"], f"{r['name']} 어댑터 없음")
            self.assertIn(r["code"], _IMPLS)

    def test_required_keys_match_registry(self):
        """어댑터가 요구하는 키가 설정 화면 항목과 정확히 일치해야 한다.
        (철자가 다르면 키를 넣어도 영원히 '키 대기' 상태가 된다)"""
        from app.malls import MALLS
        from app.malls.base import _IMPLS
        for m in MALLS:
            cls = _IMPLS[m["code"]]
            field_keys = {f["key"] for f in m["fields"]}
            for k in cls.required_keys:
                self.assertIn(k, field_keys, f"{m['name']}: '{k}'가 설정 항목에 없음")

    def test_each_mall_becomes_ready_with_keys(self):
        """키를 넣고 [이 몰 사용]을 켜면 등록된 몰 전부 '수집 가능'이 된다."""
        for code, keys in self.KEYS.items():
            self.client.put("/api/settings", json={"malls": {code: {**keys, "enabled": True}}})
        rows = self.client.get("/api/mall-status").get_json()
        not_ready = [r["name"] + ": " + r["reason"] for r in rows if not r["ready"]]
        self.assertEqual(not_ready, [], f"수집 준비가 안 된 몰: {not_ready}")

    def test_missing_key_reports_which_one(self):
        """키가 빠지면 어떤 항목이 비었는지 알려준다."""
        self.client.put("/api/settings", json={"malls": {
            "coupang": {"vendor_id": "A001", "enabled": True}}})
        rows = self.client.get("/api/mall-status").get_json()
        coupang = next(r for r in rows if r["code"] == "coupang")
        self.assertFalse(coupang["ready"])
        self.assertIn("access_key", coupang["reason"])

    def test_adapters_build_order_dicts(self):
        """어댑터가 만드는 주문 dict가 임포터와 같은 형식이어야 한다."""
        from app.malls.base import _IMPLS
        for code, keys in self.KEYS.items():
            adapter = _IMPLS[code](dict(keys, enabled=True))
            o = adapter.order(orderNumber="X-1", productName="노트북", recipient="홍길동")
            for field in ("importKey", "channel", "orderNumber", "productName", "recipient",
                          "quantity", "amount", "address", "phone"):
                self.assertIn(field, o, f"{code}: {field} 누락")
            self.assertEqual(o["channel"], adapter.name)
            self.assertTrue(o["importKey"])
            # 조회 기간 분할이 몰 제한을 넘지 않는다
            from datetime import timedelta
            wins = adapter.windows(config.now() - timedelta(days=30), config.now())
            for start, end in wins:
                self.assertLessEqual((end - start).days, adapter.max_window_days,
                                     f"{code}: 조회창이 몰 제한을 초과")

    def test_connection_test_endpoint_guards(self):
        """연결 테스트는 키가 없으면 이유를 알려주고, 있으면 실제 호출을 시도한다."""
        r = self.client.post("/api/malls/coupang/test")
        self.assertEqual(r.status_code, 400)
        self.assertIn("사용", r.get_json()["error"])
        # 키를 넣으면 게이트는 통과하고 실제 호출 단계로 간다(네트워크 없으면 502)
        self.client.put("/api/settings", json={"malls": {
            "coupang": {**self.KEYS["coupang"], "enabled": True}}})
        r = self.client.post("/api/malls/coupang/test")
        self.assertIn(r.status_code, (200, 502))

    def test_cj_test_endpoint_guards(self):
        r = self.client.post("/api/cj/test")
        self.assertEqual(r.status_code, 400)
        self.assertIn("고객코드", r.get_json()["error"])


GODO_ORDER_XML = """<?xml version="1.0" encoding="utf-8"?>
<data>
  <header><code>000</code><msg>success</msg><lastOrder>{more}</lastOrder></header>
  <return>
    <order_data>
      <orderNo>{no}</orderNo><orderStatus>p1</orderStatus><orderDate>2026-07-20 10:00:00</orderDate>
      <orderInfoData>
        <receiverName>김수취</receiverName><receiverCellPhone>010-1234-5678</receiverCellPhone>
        <receiverZonecode>06000</receiverZonecode><receiverAddress>서울시 강남구</receiverAddress>
        <receiverAddressSub>1층</receiverAddressSub><orderMemo>부재시 경비실</orderMemo>
      </orderInfoData>
      <orderGoodsData>
        <goodsNm>삼성 노트북</goodsNm><goodsCd>NT371B5L_i7</goodsCd>
        <goodsCnt>1</goodsCnt><goodsPrice>453000</goodsPrice><optionInfo>8G/256G</optionInfo>
      </orderGoodsData>
    </order_data>
  </return>
</data>"""

GODO_GOODS_XML = """<?xml version="1.0" encoding="utf-8"?>
<data>
  <header><code>000</code><msg>success</msg><total>1</total><max_page>1</max_page><now_page>1</now_page></header>
  <return>
    <goods_data>
      <goodsNo>1024</goodsNo><goodsCd>{cd}</goodsCd><goodsNm>{nm}</goodsNm>
      <shortDescription>i7-7500U / 8G / SSD 256G / 15.6인치</shortDescription>
      <goodsPrice>470000</goodsPrice><fixedPrice>590000</fixedPrice>
      <goodsState>u</goodsState><totalStock>3</totalStock><soldOutFl>n</soldOutFl>
      <goodsModelNo>NT371B5M</goodsModelNo><makerNm>삼성전자</makerNm>
    </goods_data>
  </return>
</data>"""


class TestAuditFixes0729(Base):
    """2026-07-29 종합 검증(56에이전트) 확정 결함의 회귀 테스트."""

    def test_date_filter_handles_dotted_dates(self):
        """주문일이 '2026.07.20'과 '2026-07-20'로 섞여 있어도 기간 조회가 맞아야 한다.

        실데이터에서 517건이 점 형식, 104건이 대시 형식이었다(글자 비교로는 달이 섞였다).
        """
        for od in ("2026.07.15 14:10", "2026-07-15", "2026/07/15", "2026.06.30", "2026-08-01"):
            self.client.post("/api/orders", json={
                "recipient": f"고객{od}", "productName": "노트북", "orderedAt": od})
        r = self.client.get("/api/orders?view=all&from=2026-07-01&to=2026-07-31").get_json()
        self.assertEqual(r["shown"], 3, [o["orderedAt"] for o in r["orders"]])
        r = self.client.get("/api/orders?view=all&from=2026-07-15&to=2026-07-15").get_json()
        self.assertEqual(r["shown"], 3)
        # 조회 조건 자체가 점 형식으로 들어와도 동작
        r = self.client.get("/api/orders?view=all&from=2026.07.01&to=2026.07.31").get_json()
        self.assertEqual(r["shown"], 3)

    def test_list_sorted_by_real_date(self):
        """정렬도 표기에 휘둘리면 안 된다(대시 < 점이라 점 형식이 늘 위로 왔다)."""
        self.client.post("/api/orders", json={
            "recipient": "옛날", "productName": "N", "orderedAt": "2026.01.05"})
        self.client.post("/api/orders", json={
            "recipient": "최근", "productName": "N", "orderedAt": "2026-07-20"})
        r = self.client.get("/api/orders?view=all").get_json()
        self.assertEqual([o["recipient"] for o in r["orders"]], ["최근", "옛날"])

    def test_export_uses_same_date_filter(self):
        self.client.post("/api/orders", json={
            "recipient": "7월", "productName": "N", "orderedAt": "2026.07.10"})
        self.client.post("/api/orders", json={
            "recipient": "6월", "productName": "N", "orderedAt": "2026.06.10"})
        r = self.client.get("/api/orders/export?view=all&from=2026-07-01&to=2026-07-31")
        self.assertEqual(r.status_code, 200)
        # 같은 조건의 목록이 1건이면 엑셀도 1건이어야 한다(같은 함수를 쓴다)
        self.assertEqual(
            self.client.get("/api/orders?view=all&from=2026-07-01&to=2026-07-31").get_json()["shown"], 1)

    def test_recall_waybill_not_treated_as_shipment(self):
        """회수 송장이 생겨도 그 주문의 출고 송장 자리를 차지하면 안 된다."""
        o, _a = self.make_shipped_order()
        fwd = self.client.post(f"/api/orders/{o['id']}/waybill", json={}).get_json()["wid"]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})
        self.client.post(f"/api/orders/{o['id']}/recall", json={"reason": "반품"})
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        kinds = {w["wid"]: w["type"] for w in d["waybills"]}
        self.assertEqual(kinds[fwd], "forward")
        self.assertIn("recall", kinds.values())          # 종류가 내려와야 화면이 구분할 수 있다

    def test_recall_does_not_block_reissue(self):
        """회수 송장 때문에 '이미 발행된 송장이 있습니다'로 막히면 교환 흐름이 죽는다."""
        o, _a = self.make_shipped_order()
        wid = self.client.post(f"/api/orders/{o['id']}/waybill", json={}).get_json()["wid"]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})
        self.client.post(f"/api/orders/{o['id']}/recall", json={})
        self.client.post(f"/api/waybills/{wid}/cancel")   # 출고 송장만 취소
        r = self.client.post(f"/api/orders/{o['id']}/waybill", json={})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))

    def test_as_recall_asset_is_not_sellable(self):
        """★A/S로 받은 고객 물건이 판매 재고로 섞이면 남의 노트북을 팔게 된다."""
        # ★'중복 매칭 허용'(2026-08-31, 기본 켜짐)을 끄고 차단 모드의 안전핀을 검증한다
        #   — A/S 자산은 원 주문 연결이 남아 있어 허용 모드에선 검색 후보(available)로 뜬다
        self.assertEqual(self.client.put("/api/settings", json={
            "order_asset_duplicate": {"enabled": False}}).status_code, 200)
        o, a = self.make_shipped_order()
        t = self.client.post("/api/as-tickets", json={
            "customer": "홍길동", "phone": "010-1111-2222", "address": "서울시 강남구 1",
            "symptom": "액정 불량", "assetId": a["id"]}).get_json()
        wid = self.client.post(f"/api/as-tickets/{t['id']}/recall", json={}).get_json()["wid"]
        # 담당자가 '정비중'(판매 가능)을 골라도 A/S 건은 A/S 상태로 강제된다
        self.client.post(f"/api/waybills/{wid}/received", json={"status": "refurbishing"})
        self.assertEqual(self.client.get(f"/api/assets/{a['id']}").get_json()["status"], "as")
        # 매칭 후보로 고를 수 없다.
        # ★검색 결과에서 통째로 감추지는 않는다 — 담당자가 스캔했을 때 '없는 번호'로 보이면
        #   번호를 잘못 친 줄 알고 같은 번호를 계속 다시 찍는다(2026-07-30 대표 지적).
        #   대신 available=False로 내려 화면이 [추가]를 막고 사유를 보여 준다.
        found = self.client.get(f"/api/orders/asset-search?q={a['assetNo']}").get_json()
        hit = next((x for x in found if x["assetNo"] == a["assetNo"]), None)
        self.assertIsNotNone(hit)
        self.assertFalse(hit["available"])
        self.assertTrue(hit["reason"])

        # 화면을 우회해 직접 붙이려 해도 서버가 막는다(진짜 안전장치)
        o2 = self.client.post("/api/orders", json={
            "recipient": "김철수", "productName": "노트북", "phone": "010-3333-4444",
            "address": "서울시 강남구 2", "postalCode": "06000", "amount": 400000}).get_json()
        r = self.client.patch(f"/api/orders/{o2['id']}",
                              json={"action": "assets", "assetIds": [a["id"]]})
        self.assertGreaterEqual(r.status_code, 400, "A/S 자산이 주문에 붙으면 안 된다")

    def test_open_as_asset_cannot_be_matched(self):
        """상태가 판매 가능이더라도 진행 중 A/S에 걸린 자산은 매칭을 거부한다."""
        a = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1}).get_json()[0]
        self.client.patch(f"/api/assets/{a['id']}", json={"status": "ready"})
        self.client.post("/api/as-tickets", json={
            "customer": "홍길동", "phone": "010-1111-2222", "symptom": "불량", "assetId": a["id"]})
        o2 = self.client.post("/api/orders", json={
            "recipient": "다른고객", "productName": "노트북"}).get_json()
        r = self.client.patch(f"/api/orders/{o2['id']}", json={
            "action": "assets", "assetIds": [a["id"]]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("A/S 진행 중", r.get_json()["error"])

    def test_as_returned_removes_from_stock(self):
        """반송 완료면 우리 재고가 아니다 — 재고 수량에서 빠져야 한다."""
        o, a = self.make_shipped_order()
        t = self.client.post("/api/as-tickets", json={
            "customer": "홍길동", "phone": "010-1111-2222", "address": "서울 1",
            "symptom": "불량", "assetId": a["id"]}).get_json()
        wid = self.client.post(f"/api/as-tickets/{t['id']}/recall", json={}).get_json()["wid"]
        self.client.post(f"/api/waybills/{wid}/received", json={"status": "as"})
        before = self.client.get("/api/assets/stock-by-model").get_json()["totals"]["stock"]
        self.client.patch(f"/api/as-tickets/{t['id']}", json={"status": "returned"})
        self.assertEqual(self.client.get(f"/api/assets/{a['id']}").get_json()["status"], "shipped")
        after = self.client.get("/api/assets/stock-by-model").get_json()["totals"]["stock"]
        self.assertEqual(after, before - 1)


class TestInvoicePush(Base):
    """송장번호를 쇼핑몰에 되쏘기 — 지금까지 대표가 손으로 입력하던 부분."""

    def setUp(self):
        super().setUp()

        @register
        class PushFake(MallAdapter):
            code = "pushfake"
            name = "전송몰"
            required_keys = ("token",)
            sent = []
            fail = False
            attempts = 0
            snos = []           # upload_invoice 가 받은 sno
            fetched = {}        # order_no -> sno (fetch_sno 폴백이 돌려줄 값)
            fetch_calls = []

            def fetch_sno(self, order_no, ordered_at=""):
                PushFake.fetch_calls.append((order_no, ordered_at))
                return PushFake.fetched.get(order_no, "")

            def collect_orders(self, since, until):
                return []

            def upload_invoice(self, order_no, invoice_no, courier_code="", sno=""):
                PushFake.attempts += 1
                if PushFake.fail:
                    raise MallError("몰이 거부했습니다")
                PushFake.sent.append((order_no, invoice_no, courier_code))
                PushFake.snos.append(sno)
                return True
        PushFake.sent = []
        PushFake.fail = False
        PushFake.attempts = 0
        PushFake.snos = []
        PushFake.fetched = {}
        PushFake.fetch_calls = []
        self.Fake = PushFake

    def _shipped(self, **kw):
        body = {"channel": "전송몰", "orderNo": "M-1001", "recipient": "홍길동",
                "productName": "노트북", "phone": "010-1111-2222",
                "address": "서울시 강남구 1", "postalCode": "06000", "amount": 500000}
        body.update(kw)
        o = self.client.post("/api/orders", json=body).get_json()
        a = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1}).get_json()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("production", "softwareInspection", "shipping"):
            self.client.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        self.client.post(f"/api/orders/{o['id']}/waybill", json={})   # 송장번호 채번
        # ★CJ 미설정이라 테스트 송장(999…)이 붙는다. 999는 몰 전송에서 제외되므로
        #   전송 기능 자체를 검증하려면 실제 발행처럼 번호를 바꿔 둔다.
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE orders SET tracking_no='612345678901' WHERE id=?", (o["id"],))
        conn.commit()
        conn.close()
        return o

    def _shipped_with_test_invoice(self):
        """테스트 송장(999…) 그대로인 주문 — 몰로 나가면 안 되는 건."""
        o = self.client.post("/api/orders", json={
            "channel": "전송몰", "orderNo": "M-9001", "recipient": "홍길동",
            "productName": "노트북", "phone": "010-1111-2222",
            "address": "서울시 강남구 1", "postalCode": "06000", "amount": 100000}).get_json()
        a = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1}).get_json()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("production", "softwareInspection", "shipping"):
            self.client.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        self.client.post(f"/api/orders/{o['id']}/waybill", json={})
        return o

    def test_test_invoice_never_pushed_to_mall(self):
        """★테스트 발행(999…) 송장은 몰로 나가면 안 된다.

        CJ 미설정 상태에서 만들어지는 가짜 번호다. 몰에 올리면 고객 화면에
        추적되지 않는 엉터리 송장번호가 뜨고 배송 안내까지 나간다(2026-07-29 전수조사 확정).
        """
        self.client.put("/api/settings", json={"malls": {
            "pushfake": {"token": "T", "enabled": True, "push_invoice": True}}})
        o = self._shipped_with_test_invoice()
        r = self.client.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.Fake.sent, [], "테스트 송장이 몰로 전송됐다")
        self.assertFalse(r.get_json()["mallPush"]["ok"])
        self.assertIn("테스트", r.get_json()["mallPush"]["message"])

    def test_off_by_default(self):
        """설정에서 켜지 않으면 몰로 나가지 않는다(고객에게 배송 안내가 나가는 동작)."""
        self.client.put("/api/settings", json={"malls": {
            "pushfake": {"token": "T", "enabled": True}}})
        o = self._shipped()
        r = self.client.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.Fake.sent, [])
        self.assertFalse(r.get_json()["mallPush"]["ok"])
        self.assertIn("꺼져", r.get_json()["mallPush"]["message"])

    def test_push_on_shipping_when_enabled(self):
        self.client.put("/api/settings", json={"malls": {
            "pushfake": {"token": "T", "enabled": True, "push_invoice": True,
                         "cj_courier_code": "00034"}}})
        o = self._shipped()
        r = self.client.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})
        self.assertTrue(r.get_json()["mallPush"]["ok"], r.get_json()["mallPush"])
        self.assertEqual(len(self.Fake.sent), 1)
        order_no, invoice, courier = self.Fake.sent[0]
        self.assertEqual(order_no, "M-1001")
        self.assertTrue(invoice)
        self.assertEqual(courier, "00034")
        # 대기 목록에서 빠진다
        self.assertEqual(self.client.get("/api/orders/invoice-push/pending").get_json(), [])

    def test_failure_keeps_shipment_and_allows_resend(self):
        """전송이 실패해도 출고는 유지되고, 재전송 목록에 사유와 함께 남는다."""
        self.client.put("/api/settings", json={"malls": {
            "pushfake": {"token": "T", "enabled": True, "push_invoice": True}}})
        self.Fake.fail = True
        o = self._shipped()
        r = self.client.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["shippingDone"])            # 출고는 그대로
        self.assertFalse(r.get_json()["mallPush"]["ok"])
        pending = self.client.get("/api/orders/invoice-push/pending").get_json()
        self.assertEqual(len(pending), 1)
        self.assertIn("거부", pending[0]["error"])
        # 몰이 정상으로 돌아오면 재전송으로 해결된다
        self.Fake.fail = False
        res = self.client.post("/api/orders/invoice-push", json={"ids": [o["id"]]}).get_json()
        self.assertEqual(res["ok"], 1)
        self.assertEqual(self.client.get("/api/orders/invoice-push/pending").get_json(), [])

    def test_manual_order_reports_reason(self):
        self.client.put("/api/settings", json={"malls": {
            "pushfake": {"token": "T", "enabled": True, "push_invoice": True}}})
        o = self._shipped(channel="수기", orderNo="")
        res = self.client.post("/api/orders/invoice-push", json={"ids": [o["id"]]}).get_json()
        self.assertEqual(res["ok"], 0)
        self.assertIn("채널", res["failed"][0]["message"])

    def test_requires_ship_perm(self):
        r = self.client.post("/api/users", json={
            "username": "nopush", "displayName": "무권한", "password": "nopush-pw-1234",
            "perms": ["orders.view"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "nopush", "password": "nopush-pw-1234"})
        self.assertEqual(c.get("/api/orders/invoice-push/pending").status_code, 403)
        self.assertEqual(c.post("/api/orders/invoice-push", json={"ids": [1]}).status_code, 403)

    # ---------------- 2026-09-08 대표 "송장 발급하면 고도몰에도 반영되게" — 출고 확인 세 경로 전부
    def _ready(self, order_no="M-2001"):
        """출고 확인 '직전'까지 — 송장은 있고(실발행처럼 번호를 바꿔 둔다) 아직 출고 확인은 안 한 주문."""
        o = self.client.post("/api/orders", json={
            "channel": "전송몰", "orderNo": order_no, "recipient": "홍길동",
            "productName": "노트북", "phone": "010-1111-2222",
            "address": "서울시 강남구 1", "postalCode": "06000", "amount": 500000}).get_json()
        a = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1}).get_json()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("production", "softwareInspection"):
            self.client.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        wid = self.client.post(f"/api/orders/{o['id']}/waybill", json={}).get_json()["wid"]
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE orders SET tracking_no='612345678902' WHERE id=?", (o["id"],))
        conn.execute("UPDATE waybills SET invoice_no='612345678902', status='issued' WHERE wid=?", (wid,))
        conn.commit()
        conn.close()
        return o, wid

    def _enable(self):
        self.client.put("/api/settings", json={"malls": {
            "pushfake": {"token": "T", "enabled": True, "push_invoice": True}}})

    def test_금일_출고_확인도_몰로_보낸다(self):
        """[🚚 금일 출고 확인]은 여러 건을 한 번에 마감한다 — 행 체크와 같은 규칙으로 몰에 보내야 한다.
        예전에는 이 경로만 빠져 있어, 일괄 마감한 날은 전부 손으로 입력해야 했다."""
        self._enable()
        o, _ = self._ready()
        r = self.client.post("/api/orders/ship-today", json={"ids": [o["id"]]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d["shipped"], 1)
        self.assertEqual(d["mallPush"]["sent"], 1, d["mallPush"])
        self.assertEqual([s[0] for s in self.Fake.sent], ["M-2001"])
        got = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertTrue(got["mallSentAt"], "주문에 '몰 전송됨'이 안 남았다")
        self.assertEqual(self.client.get("/api/orders/invoice-push/pending").get_json(), [])

    def test_CJ_간선상차_자동_출고_확인도_몰로_보낸다(self):
        """★09-04 이후 출고 확인의 대부분은 사람이 아니라 CJ 추적(간선상차 11)이 한다.
        그 경로가 몰에 안 보내면 '자동으로 나갔는데 몰에는 안 올라간' 건이 매일 쌓인다.
        몰 호출은 DB 잠금 밖에서 해야 하므로 after 목록으로 넘겨 트랜잭션 뒤에 보낸다."""
        self._enable()
        o, wid = self._ready("M-2002")
        from app.db import tx
        from app.orders import recall as rc
        res = {"ok": True, "data": {"PROC_LIST": [{"CRG_ST_CD": "11", "CRG_ST_NM": "간선상차"}]}}
        after = []
        with self.app.app_context():
            with tx(write=True) as conn:
                w = conn.execute("SELECT * FROM waybills WHERE wid=?", (wid,)).fetchone()
                self.assertTrue(rc._sync_one(conn, w, res, after=after))
        self.assertIn(("mall_push", o["id"]), after, "자동 출고 확인이 몰 전송을 예약하지 않았다")
        self.assertEqual(self.Fake.sent, [], "DB 잠금 안에서 몰을 불렀다(원칙 #1 위반)")
        with self.app.app_context():
            rc._notify_after(after)
        self.assertEqual([s[0] for s in self.Fake.sent], ["M-2002"])
        got = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertTrue(got["shippingDone"])
        self.assertTrue(got["mallSentAt"])

    def test_자동_출고_확인이_안_된_단계면_몰에_안_보낸다(self):
        """집화완료(02)는 아직 출고 확인이 아니다 — 몰에도 배송중이라고 알리면 안 된다."""
        self._enable()
        o, wid = self._ready("M-2003")
        from app.db import tx
        from app.orders import recall as rc
        res = {"ok": True, "data": {"PROC_LIST": [{"CRG_ST_CD": "02", "CRG_ST_NM": "집화완료"}]}}
        after = []
        with self.app.app_context():
            with tx(write=True) as conn:
                w = conn.execute("SELECT * FROM waybills WHERE wid=?", (wid,)).fetchone()
                rc._sync_one(conn, w, res, after=after)
            rc._notify_after(after)
        self.assertEqual(self.Fake.sent, [])
        self.assertFalse(self.client.get(f"/api/orders/{o['id']}").get_json()["shippingDone"])

    def test_전송_실패는_주문에_사유가_남고_화면_칩으로_보인다(self):
        self._enable()
        self.Fake.fail = True
        o, _ = self._ready("M-2004")
        r = self.client.post("/api/orders/ship-today", json={"ids": [o["id"]]}).get_json()
        self.assertEqual(r["mallPush"]["failed"], 1)
        self.assertIn("거부", r["mallPush"]["messages"][0])
        got = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(got["mallSentAt"], "")
        self.assertIn("거부", got["mallSendError"])
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js").read_text("utf-8")
        self.assertIn("🛒 몰 전송", js, "셋팅 보드에 '몰 전송됨' 칩이 없다")
        self.assertIn("⚠ 몰 미전송", js, "셋팅 보드에 실패 칩이 없다")
        self.assertIn("o.mallSendError", js)

    # ---------------- 2026-09-08 대표 "송장 뽑았는데 고도몰에 왜 안 들어가 있지" — 발급 즉시 전송 + 자동 스윕
    def _inspected(self, order_no):
        """SW 검수까지 끝나 송장을 뽑을 수 있는 주문(아직 송장 없음)."""
        o = self.client.post("/api/orders", json={
            "channel": "전송몰", "orderNo": order_no, "recipient": "홍길동",
            "productName": "노트북", "phone": "010-1111-2222",
            "address": "서울시 강남구 1", "postalCode": "06000", "amount": 500000}).get_json()
        a = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1}).get_json()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("production", "softwareInspection"):
            self.client.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        return o

    def _issue_real_like(self, o, invoice="612345678930"):
        """CJ 미설정 환경에서 '실발행처럼' 송장을 뽑는다 — 테스트 번호(999…) 대신 진짜 모양의 번호.
        발행본 상태도 실발행(issued)으로 맞춘다(스윕은 실발행본만 본다)."""
        from unittest import mock
        from app.orders import waybill as wb
        with mock.patch.object(wb, "_next_test_invoice", return_value=invoice):
            r = self.client.post(f"/api/orders/{o['id']}/waybill", json={})
        if r.status_code == 201:
            conn = sqlite3.connect(self.db_path)
            conn.execute("UPDATE waybills SET status='issued' WHERE wid=?", (r.get_json()["wid"],))
            conn.commit()
            conn.close()
        return r

    def test_송장_발급_즉시_몰로_보낸다(self):
        """기본값 — 대표가 몰 관리자에서 손으로 송장을 넣던 시점이 '뽑은 직후'다."""
        self._enable()
        o = self._inspected("M-3001")
        r = self._issue_real_like(o)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        d = r.get_json()
        self.assertTrue(d["mallPush"] and d["mallPush"]["ok"], d.get("mallPush"))
        self.assertEqual([s[0] for s in self.Fake.sent], ["M-3001"])
        got = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertTrue(got["mallSentAt"])
        self.assertFalse(got["shippingDone"], "발급이 출고 확인까지 켜면 안 된다(09-03 규칙)")
        self.assertEqual(self.client.get("/api/orders/invoice-push/pending").get_json(), [])
        # 나중에 출고 확인을 해도 두 번 보내지 않는다
        r2 = self.client.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(len(self.Fake.sent), 1)

    def test_테스트_발행은_발급_때도_안_나간다(self):
        self._enable()
        o = self._inspected("M-3002")
        r = self.client.post(f"/api/orders/{o['id']}/waybill", json={})     # 999… 테스트 번호
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertIsNone(r.get_json()["mallPush"])
        self.assertEqual(self.Fake.sent, [])
        self.assertEqual(self.client.get(f"/api/orders/{o['id']}").get_json()["mallSendError"], "")

    def test_발급_즉시를_끄면_출고_확인_때_보낸다(self):
        """몰별 스위치 [송장 발급 즉시 전송]을 끄면 예전 동작(출고 확인 시점) 그대로다."""
        self.client.put("/api/settings", json={"malls": {
            "pushfake": {"token": "T", "enabled": True, "push_invoice": True, "push_on_issue": False}}})
        o = self._inspected("M-3003")
        r = self._issue_real_like(o, "612345678931")
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertIsNone(r.get_json()["mallPush"])
        self.assertEqual(self.Fake.sent, [])
        # 자동 스윕도 이 몰은 건드리지 않는다 — 출고 확인 전에 배송중이 되면 안 된다
        from app.malls import invoice_push as ip
        ip.reset_sweep_tries()
        self.assertEqual(ip.sweep_pending(self.app)["sent"], 0)
        self.assertEqual(self.Fake.sent, [])
        r2 = self.client.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})
        self.assertTrue(r2.get_json()["mallPush"]["ok"], r2.get_json().get("mallPush"))
        self.assertEqual([s[0] for s in self.Fake.sent], ["M-3003"])

    def test_자동_전송이_꺼진_몰은_발급_때_오류를_남기지_않는다(self):
        """꺼진 몰에서 발급할 때마다 '꺼져 있습니다'가 대기 목록에 쌓이면 진짜 실패가 묻힌다."""
        self.client.put("/api/settings", json={"malls": {
            "pushfake": {"token": "T", "enabled": True}}})
        o = self._inspected("M-3004")
        r = self._issue_real_like(o, "612345678932")
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertIsNone(r.get_json()["mallPush"])
        self.assertEqual(self.Fake.sent, [])
        self.assertEqual(self.client.get(f"/api/orders/{o['id']}").get_json()["mallSendError"], "")

    def test_스윕이_놓친_최근_발급분을_보내고_옛_건은_건드리지_않는다(self):
        """재시작 직후·몰 장애 뒤를 위한 그물. 48시간 지난 건은 고도몰 상태가 뒤로 갈 수 있어 사람 몫."""
        from datetime import timedelta
        from app import config
        from app.malls import invoice_push as ip
        self._enable()
        self.Fake.fail = True                                   # 발급 순간에는 몰이 죽어 있었다
        o1 = self._inspected("M-3005")
        r1 = self._issue_real_like(o1, "612345678933")
        self.assertEqual(r1.status_code, 201, r1.get_data(as_text=True))
        self.assertFalse(r1.get_json()["mallPush"]["ok"])
        self.assertIn("거부", self.client.get(f"/api/orders/{o1['id']}").get_json()["mallSendError"])
        o2 = self._inspected("M-3006")
        self.assertEqual(self._issue_real_like(o2, "612345678934").status_code, 201)
        self.Fake.fail = False
        conn = sqlite3.connect(self.db_path)                    # o2 는 사흘 전 발급으로 만든다
        conn.execute("UPDATE waybills SET created_at=? WHERE order_id=?",
                     ((config.now() - timedelta(days=3)).isoformat(timespec="seconds"), o2["id"]))
        conn.commit()
        conn.close()
        ip.reset_sweep_tries()
        res = ip.sweep_pending(self.app)
        self.assertEqual(res["sent"], 1, res)
        self.assertEqual([s[0] for s in self.Fake.sent], ["M-3005"])
        self.assertTrue(self.client.get(f"/api/orders/{o1['id']}").get_json()["mallSentAt"])
        self.assertEqual(self.client.get(f"/api/orders/{o1['id']}").get_json()["mallSendError"], "")
        self.assertEqual(self.client.get(f"/api/orders/{o2['id']}").get_json()["mallSentAt"], "")
        # 옛 건은 여전히 사람이 보는 대기 목록에 남는다
        pend = self.client.get("/api/orders/invoice-push/pending").get_json()
        self.assertEqual([p["orderNumber"] for p in pend], ["M-3006"])

    def test_스윕은_계속_거부되는_건을_영원히_두드리지_않는다(self):
        """상한은 자동 스윕에만 — 사람이 대기 목록에서 다시 보내면 나간다."""
        from app.malls import invoice_push as ip
        self._enable()
        self.Fake.fail = True
        o = self._inspected("M-3007")
        self.assertEqual(self._issue_real_like(o, "612345678935").status_code, 201)
        ip.reset_sweep_tries()
        first = self.Fake.attempts
        for _ in range(ip.MAX_TRIES + 3):
            ip.sweep_pending(self.app)
        self.assertEqual(self.Fake.attempts - first, ip.MAX_TRIES)
        self.Fake.fail = False
        res = self.client.post("/api/orders/invoice-push", json={"ids": [o["id"]]}).get_json()
        self.assertEqual(res["ok"], 1, res)
        self.assertEqual([s[0] for s in self.Fake.sent], ["M-3007"])

    def test_주문상품_번호는_수집_원본에서_꺼내_보내고_없으면_몰에_물어본다(self):
        """고도몰 sno — 2026-09-08 첫 실전송 5건 거부(898)의 수정. 수집분은 raw 에서, 옛 수집분은 fetch_sno 로."""
        self._enable()
        o1, _ = self._ready("M-4001")
        o2, _ = self._ready("M-4002")
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE orders SET raw=? WHERE id=?", (json.dumps({"mallSno": "11|12"}), o1["id"]))
        conn.execute("UPDATE orders SET raw=NULL, ordered_at='2026-09-08 09:00' WHERE id=?", (o2["id"],))
        conn.commit()
        conn.close()
        self.Fake.fetched["M-4002"] = "77"
        res = self.client.post("/api/orders/invoice-push", json={"ids": [o1["id"], o2["id"]]}).get_json()
        self.assertEqual(res["ok"], 2, res)
        self.assertEqual(self.Fake.snos, ["11|12", "77"])
        self.assertEqual(self.Fake.fetch_calls, [("M-4002", "2026-09-08 09:00")],
                         "raw 에 있는 건은 몰에 묻지 않고, 없는 건만 한 번 묻는다")

    def test_배선_발급_즉시_스위치와_스윕_데몬(self):
        from app.malls import COMMON_FIELDS
        f = next(x for x in COMMON_FIELDS if x["key"] == "push_on_issue")
        self.assertIs(f.get("default"), True, "스위치 기본값이 켜짐이어야 한다(옛 설정에도 적용)")
        self.assertEqual(f["type"], "bool")
        root = Path(__file__).resolve().parent.parent
        self.assertIn("start_push_sweeper", (root / "app" / "__init__.py").read_text("utf-8"))
        self.assertIn("f.default", (root / "static" / "js" / "app.js").read_text("utf-8"),
                      "설정 화면이 스위치 기본값(켜짐)을 모른다")
        self.assertIn("mallPush", (root / "static" / "js" / "setup.js").read_text("utf-8"),
                      "셋팅 보드 발급 토스트가 몰 전송 결과를 안 보여 준다")
        # 몰 설정 API 가 default 를 화면까지 실어 나른다
        regs = self.client.get("/api/mall-registry").get_json()
        field = next((x for m in regs for x in (m.get("fields") or []) if x.get("key") == "push_on_issue"), None)
        self.assertIsNotNone(field, "/api/mall-registry 가 push_on_issue 칸을 안 준다")
        self.assertIs(field.get("default"), True)


class TestAutoCollect(Base):
    """[주문 자동수집] 스위치가 실제로 동작해야 한다(켜도 아무 일 없으면 화면이 거짓말)."""

    def setUp(self):
        super().setUp()

        @register
        class AutoFake(MallAdapter):
            code = "autofake"
            name = "자동몰"
            required_keys = ("token",)
            calls = []

            def collect_orders(self, since, until):
                AutoFake.calls.append((since, until))
                return [self.order(orderNumber=f"AUTO-{len(AutoFake.calls)}",
                                   productName="노트북", recipient="자동고객",
                                   phone="010-1", address="서울 1", amount=100000)]
        AutoFake.calls = []
        self.Fake = AutoFake

    def _run(self):
        from app.malls.scheduler import collect_due_malls
        return collect_due_malls(self.app)

    def test_disabled_mall_collects_nothing(self):
        """[이 몰 사용]이 꺼져 있으면 수집하지 않는다(끄기 = 유일한 스위치)."""
        self.client.put("/api/settings", json={"malls": {
            "autofake": {"token": "T", "enabled": False}}})
        self.assertEqual(self._run(), [])
        self.assertEqual(self.Fake.calls, [])
        self.assertEqual(self.client.get("/api/orders?view=all").get_json()["shown"], 0)

    def test_enabled_switch_collects(self):
        """★키 + [이 몰 사용]만으로 자동수집된다 — 별도 자동수집 스위치는 없다
        (2026-07-29 대표 결정: 키를 넣었다는 것 자체가 수집하겠다는 뜻)."""
        self.client.put("/api/settings", json={"malls": {
            "autofake": {"token": "T", "enabled": True}}})
        res = self._run()
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0]["ok"])
        self.assertEqual(res[0]["added"], 1)
        orders = self.client.get("/api/orders?view=all").get_json()["orders"]
        self.assertEqual(orders[0]["channel"], "자동몰")
        # 마지막 수집 기록이 남아 화면에 보인다
        st = self.client.get("/api/mall-status").get_json()
        self.assertTrue(all("lastSync" in m for m in st))

    def test_respects_interval(self):
        """주기가 안 됐으면 다시 부르지 않는다(몰 호출 차단 방지). 주기는 시스템이 정한다."""
        self.client.put("/api/settings", json={"malls": {
            "autofake": {"token": "T", "enabled": True}}})
        self._run()
        self.assertEqual(len(self.Fake.calls), 1)
        self._run()                                              # 바로 다시 돌려도
        self.assertEqual(len(self.Fake.calls), 1)                # 호출은 그대로

    def test_failed_mall_backs_off_longer(self):
        """실패한 몰은 30분 백오프 — 키가 틀렸는데 계속 두드리면 예산 낭비·차단 위험."""
        import json as _json
        from app import config as _config
        from datetime import timedelta as _td
        def boom(self_, since, until):
            from app.malls.base import MallError
            raise MallError("키가 만료되었습니다")
        orig = self.Fake.collect_orders
        self.Fake.collect_orders = boom
        self.client.put("/api/settings", json={"malls": {
            "autofake": {"token": "T", "enabled": True}}})
        self._run()                                              # 실패 기록
        self.Fake.collect_orders = orig
        # 마지막 시도를 20분 전으로 되돌린다 — 보통 주기(15분)면 다시 돌 시각이지만
        # 실패 백오프(30분)에는 못 미친다 → 아직 부르면 안 된다
        conn = sqlite3.connect(self.db_path)
        sync = _json.loads(conn.execute(
            "SELECT value FROM settings WHERE key='mall_sync'").fetchone()[0])
        sync["autofake"]["at"] = (_config.now() - _td(minutes=20)).isoformat()
        conn.execute("UPDATE settings SET value=? WHERE key='mall_sync'",
                     (_json.dumps(sync, ensure_ascii=False),))
        conn.commit(); conn.close()
        self.Fake.calls.clear()
        self._run()
        self.assertEqual(self.Fake.calls, [], "실패 직후 백오프를 무시하고 또 두드렸다")

    def test_collect_all_refresh_ignores_interval(self):
        """[🔄 새로고침]은 주기와 무관하게 즉시 동기화한다."""
        self.client.put("/api/settings", json={"malls": {
            "autofake": {"token": "T", "enabled": True}}})
        self._run()                                              # 자동수집 1회(주기 시작)
        self.Fake.calls.clear()
        r = self.client.post("/api/malls/collect-all", json={})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(self.Fake.calls), 1, "새로고침이 주기에 막혔다")
        res = r.get_json()["results"]
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0]["ok"])
        # 같은 주문이 또 와도 중복 저장되지 않는다
        self.assertEqual(self.client.get("/api/orders?view=all").get_json()["shown"], 1)

    def test_dedupes_across_runs(self):
        self.client.put("/api/settings", json={"malls": {
            "autofake": {"token": "T", "enabled": True}}})
        self._run()
        self.Fake.calls.clear()

        def same_order(self_, since, until):                      # 같은 주문을 또 준다
            self_.__class__.calls.append((since, until))
            return [self_.order(orderNumber="AUTO-1", productName="노트북",
                                recipient="자동고객", phone="010-1", address="서울 1",
                                amount=100000)]
        self.Fake.collect_orders = same_order
        # 주기를 무시하는 새로고침으로 같은 주문을 다시 받아 본다
        self.client.post("/api/malls/collect-all", json={})
        self.assertEqual(self.client.get("/api/orders?view=all").get_json()["shown"], 1)

    def test_failure_is_recorded_not_raised(self):
        """한 몰이 실패해도 예외로 죽지 않고 사유가 화면에 남아야 한다."""
        def boom(self_, since, until):
            from app.malls.base import MallError
            raise MallError("키가 만료되었습니다")
        self.Fake.collect_orders = boom
        self.client.put("/api/settings", json={"malls": {
            "autofake": {"token": "T", "enabled": True}}})
        res = self._run()
        self.assertFalse(res[0]["ok"])
        self.assertIn("만료", res[0]["error"])

    def test_missing_keys_skipped(self):
        self.client.put("/api/settings", json={"malls": {
            "autofake": {"enabled": True}}})   # 토큰 없음
        self.assertEqual(self._run(), [])


class TestArchiveOrders(Base):
    """끝난 주문을 목록에서 치우기 — 지우는 게 아니라 '보관' 보기로 옮긴다."""

    def test_archive_and_restore(self):
        o, _a = self.make_shipped_order()
        r = self.client.post("/api/orders/bulk", json={"action": "archive", "ids": [o["id"]]}).get_json()
        self.assertEqual(r["ok"], 1)
        self.assertEqual(self.client.get("/api/orders?view=active").get_json()["shown"], 0)
        arch = self.client.get("/api/orders?view=archived").get_json()
        self.assertEqual(arch["shown"], 1)
        self.assertTrue(arch["orders"][0]["archivedAt"])
        self.client.post("/api/orders/bulk", json={"action": "unarchive", "ids": [o["id"]]})
        self.assertEqual(self.client.get("/api/orders?view=active").get_json()["shown"], 1)

    def test_cannot_archive_unfinished(self):
        o = self.client.post("/api/orders", json={
            "recipient": "진행중", "productName": "노트북"}).get_json()
        r = self.client.post("/api/orders/bulk", json={"action": "archive", "ids": [o["id"]]}).get_json()
        self.assertEqual(r["ok"], 0)
        self.assertIn("출고 확인", r["failed"][0]["reason"])

    def test_bulk_failure_reasons_are_logged(self):
        """실패 사유가 화면에서 사라져도 나중에 확인할 수 있어야 한다."""
        o = self.client.post("/api/orders", json={
            "recipient": "진행중", "productName": "노트북"}).get_json()
        self.client.post("/api/orders/bulk", json={"action": "archive", "ids": [o["id"]]})
        logs = self.client.get("/api/audit").get_json()
        entry = next(x for x in logs if x["action"] == "orders_bulk")
        self.assertEqual(entry["detail"]["failed"], 1)
        self.assertTrue(any("출고 확인" in s for s in entry["detail"]["실패"]))


class TestAsReturnWaybill(Base):
    """A/S 흐름 완결 — 수리한 물건을 고객에게 돌려보내는 송장."""

    def _ticket(self, **kw):
        body = {"customer": "홍길동", "phone": "010-1111-2222", "address": "서울시 강남구 1",
                "symptom": "액정 불량"}
        body.update(kw)
        return self.client.post("/api/as-tickets", json=body).get_json()

    def test_issue_and_expose(self):
        o, a = self.make_shipped_order()
        t = self._ticket(assetId=a["id"])
        self.assertEqual(self.client.get(f"/api/as-tickets/{t['id']}").get_json()["returnWid"], "")
        r = self.client.post(f"/api/as-tickets/{t['id']}/return-waybill", json={})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        res = r.get_json()
        self.assertTrue(res["simulated"])            # CJ 미설정 → 테스트 발행
        d = self.client.get(f"/api/as-tickets/{t['id']}").get_json()
        self.assertEqual(d["returnWid"], res["wid"])
        self.assertEqual(d["status"], "done")        # 반송 송장이 나갔으면 수리 완료
        # 반송은 '출고' 송장이고 회수와 섞이지 않는다
        wb = next(w for w in self.client.get("/api/waybills").get_json() if w["wid"] == res["wid"])
        self.assertEqual(wb["type"], "forward")
        self.assertIn("A/S반송", wb["items"])
        self.assertEqual(self.client.get(f"/api/waybills/{res['wid']}/pdf").status_code, 200)

    def test_needs_address(self):
        t = self._ticket(address="")
        r = self.client.post(f"/api/as-tickets/{t['id']}/return-waybill", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("주소", r.get_json()["error"])

    def test_no_duplicate(self):
        t = self._ticket()
        self.client.post(f"/api/as-tickets/{t['id']}/return-waybill", json={})
        r = self.client.post(f"/api/as-tickets/{t['id']}/return-waybill", json={})
        self.assertEqual(r.status_code, 409)

    def test_recall_and_return_coexist(self):
        """회수 송장이 있어도 반송 송장을 낼 수 있어야 A/S가 끝난다."""
        o, a = self.make_shipped_order()
        t = self._ticket(assetId=a["id"])
        self.client.post(f"/api/as-tickets/{t['id']}/recall", json={})
        r = self.client.post(f"/api/as-tickets/{t['id']}/return-waybill", json={})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        d = self.client.get(f"/api/as-tickets/{t['id']}").get_json()
        self.assertTrue(d["recallWid"])
        self.assertTrue(d["returnWid"])
        self.assertNotEqual(d["recallWid"], d["returnWid"])

    def test_returned_clears_stock_even_without_receiving(self):
        """회수 입고를 건너뛰고 바로 반송해도 자산이 '회수중'에 갇히면 안 된다.

        2026-07-29 화면 검증에서 발견: 이 경우 고객에게 간 물건이 재고 1대로 계속 잡혔다.
        """
        o, a = self.make_shipped_order()
        t = self._ticket(assetId=a["id"])
        self.client.post(f"/api/as-tickets/{t['id']}/recall", json={})   # 회수 예약만
        self.assertEqual(self.client.get(f"/api/assets/{a['id']}").get_json()["status"], "returning")
        self.client.post(f"/api/as-tickets/{t['id']}/return-waybill", json={})
        self.client.patch(f"/api/as-tickets/{t['id']}", json={"status": "returned"})
        self.assertEqual(self.client.get(f"/api/assets/{a['id']}").get_json()["status"], "shipped")
        stock = self.client.get("/api/assets/stock-by-model").get_json()["totals"]["stock"]
        self.assertEqual(stock, 0, "고객에게 돌려준 물건이 재고로 남아 있다")

    def test_requires_as_manage(self):
        t = self._ticket()
        r = self.client.post("/api/users", json={
            "username": "asview", "displayName": "조회", "password": "asview-pw-1234",
            "perms": ["as.view"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "asview", "password": "asview-pw-1234"})
        self.assertEqual(c.post(f"/api/as-tickets/{t['id']}/return-waybill", json={}).status_code, 403)


class TestRepairCostSplit(Base):
    """회수 후 다시 판 노트북의 수리비가 두 번 빠지면 안 된다."""

    def test_repair_counted_once_across_resale(self):
        o1, a = self.make_shipped_order(amount=500000)
        self.client.post(f"/api/assets/{a['id']}/repairs", json={"description": "액정", "cost": 60000})
        # 회수 → 입고 → 다시 판매
        wid = self.client.post(f"/api/orders/{o1['id']}/recall", json={"reason": "반품"}).get_json()["wid"]
        self.client.post(f"/api/waybills/{wid}/received", json={"status": "ready"})
        o2 = self.client.post("/api/orders", json={
            "recipient": "재판매고객", "productName": "노트북", "amount": 450000}).get_json()
        self.client.patch(f"/api/orders/{o2['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("production", "softwareInspection", "shipping"):
            self.client.patch(f"/api/orders/{o2['id']}", json={"action": act, "value": True})
        # o1은 회수 완료라 매출·원가에서 빠지고, 수리비는 o2에 한 번만 붙어야 한다
        s = self.client.get("/api/reports/summary").get_json()["sales"]
        self.assertEqual(s["orders"], 1)
        self.assertEqual(s["repairCost"], 60000)
        rows = self.client.get("/api/reports/ledger").get_json()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["repairCost"], 60000)

    def test_repair_split_when_asset_in_two_live_orders(self):
        """어쩌다 두 유효 주문에 걸리면 나눠 담아 총액이 실제 수리비를 넘지 않게."""
        o1, a = self.make_shipped_order(amount=500000)
        self.client.post(f"/api/assets/{a['id']}/repairs", json={"description": "청소", "cost": 100000})
        # 회수 없이 두 번째 주문에 강제로 물린다(데이터가 꼬인 상황 재현)
        with self.app.app_context():
            import sqlite3 as _s
            conn = _s.connect(self.db_path)
            o2 = self.client.post("/api/orders", json={
                "recipient": "둘째", "productName": "노트북", "amount": 300000}).get_json()
            conn.execute("INSERT INTO order_assets(order_id, asset_id, prev_status, matched_by, matched_at)"
                         " VALUES(?,?,?,?,?)", (o2["id"], a["id"], "ready", "테스트", config.now_iso()))
            conn.execute("UPDATE orders SET shipping_done=1, shipping_at=? WHERE id=?",
                         (config.now_iso(), o2["id"]))
            conn.commit(); conn.close()
        s = self.client.get("/api/reports/summary").get_json()["sales"]
        self.assertEqual(s["repairCost"], 100000)      # 200,000으로 부풀지 않는다
        total = sum(r["repairCost"] for r in self.client.get("/api/reports/ledger").get_json()["rows"])
        self.assertEqual(total, 100000)


class TestCategoryScope(Base):
    """담당 분류를 나눠 쓰기 시작하면 곧바로 필요한 검사들."""

    def _scoped_user(self, cat_id):
        r = self.client.post("/api/users", json={
            "username": "scopeduser", "displayName": "담당자", "password": "scoped-pw-1234",
            "perms": ["orders.view", "orders.edit", "orders.ship", "shipping.view", "orders.work"],
            "categoryIds": [cat_id]})
        assert r.status_code == 201, r.get_data(as_text=True)
        c = self.app.test_client()
        assert c.post("/api/auth/login", json={
            "username": "scopeduser", "password": "scoped-pw-1234"}).status_code == 200
        return c

    def test_order_detail_respects_scope(self):
        mine, other = self.cats[0]["id"], self.cats[1]["id"]
        o_mine = self.client.post("/api/orders", json={
            "recipient": "내담당", "productName": "노트북", "categoryId": mine}).get_json()
        o_other = self.client.post("/api/orders", json={
            "recipient": "남담당", "productName": "노트북", "categoryId": other}).get_json()
        c = self._scoped_user(mine)
        self.assertEqual(c.get(f"/api/orders/{o_mine['id']}").status_code, 200)
        self.assertEqual(c.get(f"/api/orders/{o_other['id']}").status_code, 404)
        # 수정도 막힌다
        self.assertEqual(c.patch(f"/api/orders/{o_other['id']}", json={
            "action": "details", "fields": {"recipient": "탈취"}}).status_code, 404)
        self.assertEqual(self.client.get(f"/api/orders/{o_other['id']}")
                         .get_json()["recipient"], "남담당")

    def test_waybill_list_and_pdf_respect_scope(self):
        """송장 목록은 주문번호를 몰라도 고객 이름·전화·주소가 나열되는 곳이다."""
        other = self.cats[1]["id"]
        o, _a = self.make_shipped_order(recipient="남의고객", categoryId=other)
        wid = self.client.post(f"/api/orders/{o['id']}/waybill", json={}).get_json()["wid"]
        c = self._scoped_user(self.cats[0]["id"])
        self.assertEqual(c.get("/api/waybills").get_json(), [])
        self.assertEqual(c.get(f"/api/waybills/{wid}/pdf").status_code, 404)
        self.assertEqual(c.post("/api/waybills/print", json={"wids": [wid]}).status_code, 404)
        # 관리자는 그대로 본다
        self.assertEqual(len(self.client.get("/api/waybills").get_json()), 1)

    def test_cannot_widen_own_scope(self):
        """담당 분류도 권한이다 — 스스로 '전체 보기'를 켜면 안 된다."""
        r = self.client.post("/api/users", json={
            "username": "selfcat", "displayName": "본인", "password": "selfcat-pw-1234",
            "perms": ["users.manage", "orders.view"], "categoryIds": [self.cats[0]["id"]]})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        uid = r.get_json()["id"]
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "selfcat", "password": "selfcat-pw-1234"})
        r = c.put(f"/api/users/{uid}/categories", json={"allCategories": True})
        self.assertEqual(r.status_code, 403)
        self.assertIn("본인", r.get_json()["error"])

    def test_qc_import_cannot_grant_beyond_importer(self):
        """이관 파일이 '내가 못 주는 권한'을 우회로 부여하면 안 된다."""
        import io as _io
        r = self.client.post("/api/users", json={
            "username": "limited", "displayName": "제한", "password": "limited-pw-1234",
            "perms": ["settings.view", "settings.manage", "users.manage", "orders.view"],
            "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "limited", "password": "limited-pw-1234"})
        payload = [{"username": "sneaky", "role": "sales_manager",   # 매입 수정·리포트 포함
                    "passwordHash": auth_mod.hash_password("sneaky-pw-1")}]
        r = c.post("/api/migrate/qc", data={
            "files": (_io.BytesIO(json.dumps(payload).encode()), "users.json")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 403)
        self.assertIn("가지지 않은", r.get_json()["error"])
        self.assertNotIn("sneaky", [u["username"] for u in self.client.get("/api/users").get_json()])

    def test_qc_import_does_not_open_all_categories(self):
        import io as _io
        payload = [{"username": "migrated", "role": "worker",
                    "passwordHash": auth_mod.hash_password("migrated-pw-1")}]
        r = self.client.post("/api/migrate/qc", data={
            "files": (_io.BytesIO(json.dumps(payload).encode()), "users.json")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        made = next(u for u in self.client.get("/api/users").get_json()
                    if u["username"] == "migrated")
        self.assertFalse(made["isAdmin"])
        self.assertTrue(made["allCategories"])   # 관리자가 이관하면 전체 보기(기존 운영과 동일)


class TestCustomerPrivacy(Base):
    """셋팅(QC) 담당자에게 고객 명부가 통째로 새면 안 된다."""

    def _worker(self):
        r = self.client.post("/api/users", json={
            "username": "qcworker", "displayName": "셋팅담당", "password": "qcworker-pw-1234",
            "perms": ["setup.view", "orders.work"], "allCategories": True})
        assert r.status_code == 201, r.get_data(as_text=True)
        c = self.app.test_client()
        assert c.post("/api/auth/login", json={
            "username": "qcworker", "password": "qcworker-pw-1234"}).status_code == 200
        return c

    def test_setup_worker_sees_work_info_not_contacts(self):
        self.client.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북", "phone": "010-1234-5678",
            "address": "서울시 강남구 1", "postalCode": "06000", "memo": "당일출고희망"})
        c = self._worker()
        o = c.get("/api/orders?view=all").get_json()["orders"][0]
        self.assertEqual(o["recipient"], "홍길동")     # 누구 물건인지는 알아야 한다
        self.assertEqual(o["memo"], "당일출고희망")     # 작업 지시도 봐야 한다
        self.assertEqual(o["address"], "")             # 주소는 필요 없다
        self.assertEqual(o["postalCode"], "")
        self.assertNotIn("1234", o["phone"])           # 번호 앞자리가 새면 안 된다
        self.assertTrue(o["phone"].endswith("5678"))   # 대조용 뒷자리만
        self.assertTrue(o["piiMasked"])

    def test_shipping_worker_sees_contacts(self):
        """배송 담당자는 송장을 찍어야 하므로 그대로 본다."""
        self.client.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북", "phone": "010-1234-5678",
            "address": "서울시 강남구 1"})
        r = self.client.post("/api/users", json={
            "username": "shipper", "displayName": "배송", "password": "shipper-pw-1234",
            "perms": ["shipping.view", "orders.ship"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "shipper", "password": "shipper-pw-1234"})
        o = c.get("/api/orders?view=all").get_json()["orders"][0]
        self.assertEqual(o["phone"], "010-1234-5678")
        self.assertEqual(o["address"], "서울시 강남구 1")
        self.assertFalse(o["piiMasked"])

    def test_export_needs_order_view(self):
        """고객 명부를 통째로 받는 엑셀은 셋팅 권한으로 못 받는다."""
        c = self._worker()
        self.assertEqual(c.get("/api/orders/export?view=all").status_code, 403)
        self.assertEqual(self.client.get("/api/orders/export?view=all").status_code, 200)

    def test_as_history_hidden_without_as_perm(self):
        o, a = self.make_shipped_order(phone="010-5555-6666")
        self.client.post("/api/as-tickets", json={
            "customer": "홍길동", "phone": "01055556666", "symptom": "액정 불량", "assetId": a["id"]})
        # 관리자는 본다
        self.assertEqual(len(self.client.get(
            f"/api/orders/customer-history?orderId={o['id']}").get_json()["asTickets"]), 1)
        # A/S 권한 없는 직원은 못 본다
        r = self.client.post("/api/users", json={
            "username": "noas", "displayName": "무A/S", "password": "noas-pw-1234",
            "perms": ["orders.view"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "noas", "password": "noas-pw-1234"})
        self.assertEqual(c.get(f"/api/orders/customer-history?orderId={o['id']}")
                         .get_json()["asTickets"], [])


class TestSettlementFixes(Base):
    """정산 수수료 4가지 결함(2026-07-29 확정)의 회귀 테스트."""

    def _fees(self, rates, ship=0):
        self.client.put("/api/settings", json={"settlement": {"rates": rates, "shippingCost": ship}})

    def test_rate_matches_actual_order_channel(self):
        """요율은 주문에 저장된 채널 값 기준이어야 걸린다('고도몰5'로 넣으면 안 붙었다)."""
        self._fees({"고도몰": 3.4})
        o, _a = self.make_shipped_order(channel="고도몰", amount=1000000)
        self.assertEqual(self.client.get(f"/api/orders/{o['id']}").get_json()["feeAmount"], 34000)
        # 설정 화면이 쓰는 채널 목록에 실제 채널이 들어 있어야 한다
        malls = [m["channel"] for m in self.client.get("/api/orders/summary").get_json()["malls"]]
        self.assertIn("고도몰", malls)

    def test_amount_change_recalculates_auto_fee(self):
        """몰 정산액에 맞춰 금액을 고치면 자동 수수료도 따라와야 한다."""
        self._fees({"쿠팡": 10})
        o, _a = self.make_shipped_order(channel="쿠팡", amount=500000)
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(d["feeAmount"], 50000)
        self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "details", "expectedUpdatedAt": d["updatedAt"], "fields": {"amount": 400000}})
        d2 = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(d2["feeAmount"], 40000)          # 옛 금액 기준으로 굳지 않는다
        self.assertEqual(d2["netAmount"], 360000)

    def test_manual_fee_survives_amount_change_and_backfill(self):
        """사람이 확정한 수수료는 자동 재계산·소급 적용이 덮어쓰면 안 된다."""
        self._fees({"쿠팡": 10})
        o, _a = self.make_shipped_order(channel="쿠팡", amount=500000)
        self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "settlement", "feeAmount": 0})       # 직거래로 0원 확정
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "details", "expectedUpdatedAt": d["updatedAt"], "fields": {"amount": 400000}})
        self.assertEqual(self.client.get(f"/api/orders/{o['id']}").get_json()["feeAmount"], 0)
        r = self.client.post("/api/orders/settlement-backfill", json={}).get_json()
        self.assertEqual(r["changed"], 0)                 # 소급 대상에서 빠진다
        self.assertEqual(self.client.get(f"/api/orders/{o['id']}").get_json()["feeAmount"], 0)

    def test_details_and_settlement_save_together(self):
        """★[저장] 한 번에 상세와 정산이 같이 저장돼야 한다.

        2026-07-29 확인: 화면이 정산·상세를 따로 보내던 시절, 앞의 정산 저장이
        수정시각을 바꿔 뒤의 상세 저장이 '다른 사용자가 먼저 수정했습니다'로 막혔다.
        아무도 동시에 만지지 않았는데 100% 재현됐고, 고친 내용은 통째로 사라졌다.
        """
        self._fees({"쿠팡": 10})
        o, _a = self.make_shipped_order(channel="쿠팡", amount=500000)
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        r = self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "details", "expectedUpdatedAt": d["updatedAt"],
            "fields": {"recipient": "김하나", "memo": "정산 대사"},
            "settlement": {"feeAmount": 44000, "shippingCost": 3000,
                           "refundAmount": 0, "refundReason": ""},
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d2 = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(d2["recipient"], "김하나", "상세가 저장되지 않았다")
        self.assertEqual(d2["memo"], "정산 대사")
        self.assertEqual(d2["feeAmount"], 44000, "정산이 저장되지 않았다")
        self.assertEqual(d2["shippingCost"], 3000)

    def test_refund_reason_only_change_saves(self):
        """환불 사유만 고쳐도 저장돼야 한다(예전엔 '변경할 항목이 없습니다'로 전체가 막혔다)."""
        o, _a = self.make_shipped_order(channel="쿠팡", amount=500000)
        self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "settlement", "refundAmount": 10000, "refundReason": "일부 파손"})
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        r = self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "details", "expectedUpdatedAt": d["updatedAt"],
            "fields": {"memo": "사유 정정"},
            "settlement": {"refundAmount": 10000, "refundReason": "액정 흠집 보상"},
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d2 = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(d2["refundReason"], "액정 흠집 보상")
        self.assertEqual(d2["memo"], "사유 정정")

    def test_save_with_no_change_is_not_an_error(self):
        """아무것도 안 고치고 [저장]을 눌러도 오류가 뜨면 안 된다."""
        o, _a = self.make_shipped_order(channel="쿠팡", amount=500000)
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        r = self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "details", "expectedUpdatedAt": d["updatedAt"],
            "fields": {"recipient": d["recipient"]},
            "settlement": {"feeAmount": d["feeAmount"], "shippingCost": d["shippingCost"],
                           "refundAmount": d["refundAmount"], "refundReason": d["refundReason"]},
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_refund_cap_uses_new_amount_in_same_save(self):
        """같은 저장에서 금액을 올렸으면 환불 상한도 새 금액으로 봐야 한다."""
        o, _a = self.make_shipped_order(channel="쿠팡", amount=100000)
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        r = self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "details", "expectedUpdatedAt": d["updatedAt"],
            "fields": {"amount": 300000},
            "settlement": {"refundAmount": 250000, "refundReason": "반품"},
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.client.get(f"/api/orders/{o['id']}").get_json()["refundAmount"], 250000)

    def test_settlement_only_marks_manual_when_fee_actually_changed(self):
        """환불액·택배비만 고쳤는데 그 주문이 수수료 자동계산에서 영구 제외되면 안 된다."""
        self._fees({"쿠팡": 10})
        o, _a = self.make_shipped_order(channel="쿠팡", amount=500000)
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(d["feeAmount"], 50000)
        # 화면은 세 값을 함께 보낸다 — 수수료는 그대로 두고 환불액만 바꾼 상황
        r = self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "settlement", "feeAmount": 50000, "shippingCost": 0,
            "refundAmount": 30000, "refundReason": "부분 보상"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 금액을 고치면 수수료가 여전히 자동으로 따라와야 한다
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "details", "expectedUpdatedAt": d["updatedAt"], "fields": {"amount": 400000}})
        self.assertEqual(self.client.get(f"/api/orders/{o['id']}").get_json()["feeAmount"], 40000)
        # 수수료를 실제로 바꾸면 그때는 수동 확정된다
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "settlement", "feeAmount": 12345})
        self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "details", "expectedUpdatedAt": d["updatedAt"], "fields": {"amount": 300000}})
        self.assertEqual(self.client.get(f"/api/orders/{o['id']}").get_json()["feeAmount"], 12345)

    def test_amount_change_audit_keeps_previous_value(self):
        """금액을 잘못 바꿔도 되돌릴 수 있게 '바꾸기 전 값'이 남아야 한다."""
        o, _a = self.make_shipped_order(amount=500000)
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "details", "expectedUpdatedAt": d["updatedAt"], "fields": {"amount": 139000}})
        logs = self.client.get("/api/audit").get_json()
        entry = next(x for x in (logs if isinstance(logs, list) else logs.get("logs", []))
                     if x["action"] == "order_updated" and "금액" in json.dumps(x.get("detail") or {}, ensure_ascii=False))
        detail = entry["detail"]["금액"]
        self.assertEqual(detail["from"], 500000)
        self.assertEqual(detail["to"], 139000)

    def test_backfill_records_touched_orders(self):
        o, _a = self.make_shipped_order(channel="쿠팡", amount=500000)
        self._fees({"쿠팡": 10})
        self.client.post("/api/orders/settlement-backfill", json={})
        logs = self.client.get("/api/audit").get_json()
        rows = logs if isinstance(logs, list) else logs.get("logs", [])
        entry = next(x for x in rows if x["action"] == "settlement_backfill")
        self.assertIn(o["id"], entry["detail"]["주문"])


class TestAsSms(Base):
    """A/S 안내 문자 — ★실제 발송은 어떤 테스트에서도 일어나면 안 된다."""

    def setUp(self):
        super().setUp()
        # 발송 함수를 가로채 '실제로 불렸는지'만 기록한다(네트워크 차단)
        from app import notify as notify_mod
        self.sent = []
        self._orig_send = notify_mod.send_sms

        def fake_send(cfg, phone, text, subject=None):
            self.sent.append({"phone": phone, "text": text})
            return {"ok": True, "msgType": "SMS", "detail": "fake"}
        notify_mod.send_sms = fake_send
        self.addCleanup(setattr, notify_mod, "send_sms", self._orig_send)
        self.notify_mod = notify_mod

    def _ticket(self, **kw):
        body = {"customer": "홍길동", "phone": "010-1234-5678", "address": "서울 1",
                "symptom": "액정 세로줄"}
        body.update(kw)
        return self.client.post("/api/as-tickets", json=body).get_json()

    def _arm(self):
        """실발송 조건을 모두 갖춘다 — 그래도 임시 DB라 나가면 안 된다."""
        self.client.put("/api/settings", json={"sms": {
            "apiKey": "TESTKEY", "sender": "0212345678", "armed": True,
            "firstLiveConfirmedAt": config.now_iso(),   # 시험 발송을 마친 상태로 둔다
            "quietFrom": 0, "quietTo": 0}})             # 시간대에 흔들리지 않게

    def test_default_is_simulation(self):
        t = self._ticket()
        self.assertEqual(self.sent, [])
        log = self.client.get(f"/api/sms/log?ticketId={t['id']}").get_json()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["status"], "simulated")
        self.assertIn("홍길동", log[0]["text"])
        self.assertIn("접수", log[0]["text"])

    def test_never_sends_from_non_live_db(self):
        """★검증 서버(임시 DB)에서는 무장해도 절대 나가지 않는다.

        라이브 DB를 복사해 다른 포트로 띄우면 API 키까지 복사되므로,
        이 가드가 없으면 검증하다 실제 고객에게 문자가 간다.
        """
        self._arm()
        t = self._ticket()
        self.assertEqual(self.sent, [], "임시 DB인데 실제 발송이 시도되었다")
        log = self.client.get(f"/api/sms/log?ticketId={t['id']}").get_json()
        self.assertEqual(log[0]["status"], "simulated")
        self.assertIn("운영 데이터가 아니라", log[0]["reason"])

    def test_env_var_alone_cannot_make_it_live(self):
        """★OWS_DB로 사본을 가리켜도 '운영'으로 둔갑하면 안 된다.

        예전 판정은 열린 DB를 config.DB_PATH와 비교했는데, 그 값 자체가 OWS_DB에서 나와
        검증 서버에서 항상 참이 됐다(설정=API 키도 사본에 함께 딸려온다).
        """
        self._arm()
        orig = config.DB_PATH
        config.DB_PATH = self.db_path            # 환경변수로 사본을 가리킨 상황 재현
        self.addCleanup(setattr, config, "DB_PATH", orig)
        t = self._ticket()
        self.assertEqual(self.sent, [], "환경변수만으로 실발송이 뚫렸다")
        log = self.client.get(f"/api/sms/log?ticketId={t['id']}").get_json()
        self.assertEqual(log[0]["status"], "simulated")

    def test_sends_when_production_and_armed(self):
        """운영 서버로 판정되고 무장·시험확인까지 끝났을 때만 실제 발송 경로를 탄다."""
        self._arm()
        orig_prod = self.notify_mod.is_production_server
        self.addCleanup(setattr, self.notify_mod, "is_production_server", orig_prod)
        self.notify_mod.is_production_server = lambda p: (True, "")
        t = self._ticket()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["phone"], "01012345678")
        log = self.client.get(f"/api/sms/log?ticketId={t['id']}").get_json()
        self.assertEqual(log[0]["status"], "sent")

    def test_no_duplicate_for_same_step(self):
        """같은 단계 안내가 두 번 나가면 안 된다."""
        t = self._ticket()
        r = self.client.post(f"/api/as-tickets/{t['id']}/sms",
                             json={"event": "received"}).get_json()
        self.assertFalse(r["ok"])
        self.assertIn("이미", r["message"])
        log = self.client.get(f"/api/sms/log?ticketId={t['id']}").get_json()
        self.assertEqual(len([x for x in log if x["event"] == "received"]), 1)

    def test_bad_phone_is_skipped_with_reason(self):
        t = self._ticket(phone="")
        log = self.client.get(f"/api/sms/log?ticketId={t['id']}").get_json()
        self.assertEqual(log[0]["status"], "skipped")
        self.assertIn("번호", log[0]["reason"])
        self.assertEqual(self.sent, [])

    def test_recall_and_return_send_notices(self):
        o, a = self.make_shipped_order()
        t = self._ticket(assetId=a["id"])
        self.client.post(f"/api/as-tickets/{t['id']}/recall", json={})
        self.client.post(f"/api/as-tickets/{t['id']}/return-waybill", json={})
        log = self.client.get(f"/api/sms/log?ticketId={t['id']}").get_json()
        events = {x["event"]: x for x in log}
        self.assertIn("collecting", events)
        self.assertIn("returned", events)
        self.assertRegex(events["returned"]["text"], r"\d{9,}")   # 송장번호가 문구에 들어간다

    def test_event_can_be_turned_off(self):
        self.client.put("/api/settings", json={"sms": {
            "events": {"received": {"on": False, "text": "안 보냄"}}}})
        t = self._ticket()
        log = self.client.get(f"/api/sms/log?ticketId={t['id']}").get_json()
        self.assertEqual(log, [], "꺼 둔 안내가 발송 시도되었다")
        # 꺼져 있어도 손으로는 보낼 수 있다
        r = self.client.post(f"/api/as-tickets/{t['id']}/sms",
                             json={"event": "received", "force": True}).get_json()
        self.assertTrue(r["ok"])

    def test_custom_text_and_variables(self):
        self.client.put("/api/settings", json={"sms": {"events": {
            "received": {"on": True, "text": "{고객명}님 {접수번호} 접수 / 증상 {증상} / {없는변수}"}}}})
        t = self._ticket(customer="김철수", symptom="배터리 부풀음")
        log = self.client.get(f"/api/sms/log?ticketId={t['id']}").get_json()
        self.assertIn("김철수님", log[0]["text"])
        self.assertIn("배터리 부풀음", log[0]["text"])
        self.assertNotIn("{", log[0]["text"])         # 안 채워진 자리는 남지 않는다

    def _production(self):
        """운영 서버로 판정되게 한다(발송 로직 자체를 검증하기 위한 것)."""
        orig = self.notify_mod.is_production_server
        self.addCleanup(setattr, self.notify_mod, "is_production_server", orig)
        self.notify_mod.is_production_server = lambda p: (True, "")

    def test_cancelled_ticket_never_notified(self):
        """취소된 A/S에는 자동·수동 어느 쪽으로도 나가지 않는다."""
        t = self._ticket()
        self.client.patch(f"/api/as-tickets/{t['id']}", json={"status": "cancelled"})
        r = self.client.post(f"/api/as-tickets/{t['id']}/sms",
                             json={"event": "done", "force": True}).get_json()
        self.assertFalse(r["ok"])
        self.assertIn("취소", r["message"])
        self.assertEqual(self.sent, [])

    def test_done_requires_result_text(self):
        """처리 내용이 비면 '처리: ' 뒤가 빈 문자가 나간다 — 막는다."""
        t = self._ticket()
        r = self.client.patch(f"/api/as-tickets/{t['id']}", json={"status": "done"}).get_json()
        self.assertFalse(r["sms"]["ok"])
        self.assertIn("처리 내용", r["sms"]["message"])

    def test_paid_repair_without_amount_blocked(self):
        """유상인데 금액이 0이면 '청구 안내'가 금액 없이 나간다 — 막는다."""
        t = self._ticket()
        self.client.patch(f"/api/as-tickets/{t['id']}", json={
            "chargeTo": "customer", "result": "메인보드 교체"})
        r = self.client.post(f"/api/as-tickets/{t['id']}/sms",
                             json={"event": "done", "force": True}).get_json()
        self.assertFalse(r["ok"])
        self.assertIn("수리비", r["message"])

    def test_return_notice_needs_invoice(self):
        t = self._ticket()
        r = self.client.post(f"/api/as-tickets/{t['id']}/sms",
                             json={"event": "returned", "force": True}).get_json()
        self.assertFalse(r["ok"])
        self.assertIn("송장번호", r["message"])

    def test_safe_number_is_skipped_with_guidance(self):
        t = self._ticket(phone="0504-1234-5678")
        log = self.client.get(f"/api/sms/log?ticketId={t['id']}").get_json()
        self.assertEqual(log[0]["status"], "skipped")
        self.assertIn("안심번호", log[0]["reason"])

    def test_quiet_hours_block_live_send(self):
        """밤에는 실제로 보내지 않는다."""
        self._arm()
        self._production()
        self.client.put("/api/settings", json={"sms": {"quietFrom": 0, "quietTo": 24}})
        t = self._ticket()
        self.assertEqual(self.sent, [])
        log = self.client.get(f"/api/sms/log?ticketId={t['id']}").get_json()
        self.assertEqual(log[0]["status"], "skipped")
        self.assertIn("야간", log[0]["reason"])

    def test_daily_cap_blocks(self):
        self._arm()
        self._production()
        self.client.put("/api/settings", json={"sms": {"dailyCap": 1}})
        self._ticket(customer="첫번째")
        self.assertEqual(len(self.sent), 1)
        t2 = self._ticket(customer="두번째")
        self.assertEqual(len(self.sent), 1, "상한을 넘겨 발송되었다")
        log = self.client.get(f"/api/sms/log?ticketId={t2['id']}").get_json()
        self.assertIn("상한", log[0]["reason"])

    def test_test_send_only_to_saved_number(self):
        """시험 발송은 저장된 대표 번호로만 — 요청으로 번호를 받지 않는다."""
        self._arm()
        self._production()
        r = self.client.post("/api/sms/test", json={"phone": "010-9999-9999"})
        self.assertEqual(r.status_code, 400)          # 시험 번호 미저장
        self.client.put("/api/settings", json={"sms": {"testPhone": "010-1111-2222"}})
        r = self.client.post("/api/sms/test", json={"phone": "010-9999-9999"}).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(self.sent[-1]["phone"], "01011112222")   # 요청 번호는 무시된다

    def test_default_texts_fit_in_sms(self):
        """기본 문구는 단문(90바이트) 안에 들어가야 한다 — 장문은 요금이 3배다."""
        from app.notify import EVENTS
        for e in EVENTS:
            r = self.client.post("/api/sms/preview", json={"text": e["default"]}).get_json()
            self.assertEqual(r["msgType"], "SMS",
                             f"{e['label']} 기본 문구가 {r['bytes']}바이트라 장문으로 나간다")

    def test_preview_reports_length(self):
        r = self.client.post("/api/sms/preview", json={
            "text": "짧은 안내입니다."}).get_json()
        self.assertEqual(r["msgType"], "SMS")
        r = self.client.post("/api/sms/preview", json={"text": "가" * 60}).get_json()
        self.assertEqual(r["msgType"], "LMS")         # 90바이트 초과는 장문
        self.assertGreater(r["bytes"], 90)

    def test_api_key_is_masked(self):
        self._arm()
        s = self.client.get("/api/settings").get_json()["sms"]
        self.assertNotEqual(s.get("apiKey"), "TESTKEY")
        cfg = self.client.get("/api/sms/config").get_json()
        self.assertTrue(cfg["hasApiKey"])
        self.assertNotIn("apiKey", cfg)               # 설정 조회에 키 값이 실려 나가지 않는다

    def test_permissions(self):
        t = self._ticket()
        r = self.client.post("/api/users", json={
            "username": "asview2", "displayName": "조회", "password": "asview2-pw-1234",
            "perms": ["as.view"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "asview2", "password": "asview2-pw-1234"})
        self.assertEqual(c.post(f"/api/as-tickets/{t['id']}/sms",
                                json={"event": "received"}).status_code, 403)
        self.assertEqual(c.get("/api/sms/config").status_code, 403)
        self.assertEqual(c.get(f"/api/sms/log?ticketId={t['id']}").status_code, 200)  # 조회는 가능


class TestSmallAuditFixes(Base):
    """전수조사에서 나온 잔손질 항목들."""

    def test_settlement_rate_can_be_removed(self):
        """요율 칸을 비우면 실제로 지워져야 한다('비우면 0%' 안내와 맞게)."""
        self.client.put("/api/settings", json={"settlement": {
            "rates": {"쿠팡": 10, "고도몰": 3}, "shippingCost": 3000}})
        self.client.put("/api/settings", json={"settlement": {
            "rates": {"쿠팡": 10}, "shippingCost": 3000}})       # 고도몰을 지운 상태로 저장
        s = self.client.get("/api/settings").get_json()["settlement"]
        self.assertEqual(s["rates"], {"쿠팡": 10})
        self.assertEqual(s["shippingCost"], 3000)

    def test_expiry_date_is_not_masked(self):
        """'키 만료일'은 비밀이 아니다 — 저장 후에도 화면에 남아야 한다."""
        self.client.put("/api/settings", json={"malls": {
            "coupang": {"vendor_id": "A001", "access_key": "AK", "secret_key": "SK",
                        "key_expires_at": "2027-01-31"}}})
        m = self.client.get("/api/settings").get_json()["malls"]["coupang"]
        self.assertEqual(m["key_expires_at"], "2027-01-31")      # 가려지면 안 된다
        self.assertNotEqual(m["access_key"], "AK")               # 진짜 비밀은 가려진다
        # 다시 저장해도 만료일이 사라지지 않는다
        self.client.put("/api/settings", json={"malls": {
            "coupang": dict(m, memo="확인")}})
        again = self.client.get("/api/settings").get_json()["malls"]["coupang"]
        self.assertEqual(again["key_expires_at"], "2027-01-31")

    def test_waybills_manage_can_cancel(self):
        """'송장 관리(취소·재발행)' 권한이 설명대로 동작해야 한다."""
        o, _a = self.make_shipped_order()
        wid = self.client.post(f"/api/orders/{o['id']}/waybill", json={}).get_json()["wid"]
        r = self.client.post("/api/users", json={
            "username": "wbmgr", "displayName": "송장관리", "password": "wbmgr-pw-1234",
            "perms": ["shipping.view", "waybills.manage"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "wbmgr", "password": "wbmgr-pw-1234"})
        self.assertEqual(c.post(f"/api/waybills/{wid}/cancel").status_code, 200)
        self.assertEqual(c.post(f"/api/orders/{o['id']}/waybill", json={}).status_code, 201)

    def test_recall_receive_reports_actual_status(self):
        """A/S 회수는 고른 값과 다르게 들어가므로 실제 상태를 알려줘야 한다."""
        o, a = self.make_shipped_order()
        t = self.client.post("/api/as-tickets", json={
            "customer": "홍길동", "phone": "010-1", "address": "서울 1",
            "symptom": "불량", "assetId": a["id"]}).get_json()
        wid = self.client.post(f"/api/as-tickets/{t['id']}/recall", json={}).get_json()["wid"]
        r = self.client.post(f"/api/waybills/{wid}/received", json={"status": "in_stock"}).get_json()
        self.assertEqual(r["status"], "as")                      # 고른 값이 아니라 실제 반영값

    def test_repair_cost_can_be_corrected(self):
        """수리비를 고쳐 적으면 자산 원가에도 다시 반영돼야 한다."""
        o, a = self.make_shipped_order()
        t = self.client.post("/api/as-tickets", json={
            "customer": "홍길동", "phone": "010-1", "symptom": "불량",
            "assetId": a["id"], "chargeTo": "company"}).get_json()
        self.client.patch(f"/api/as-tickets/{t['id']}", json={"cost": 50000})
        r = self.client.post(f"/api/as-tickets/{t['id']}/to-asset-repair")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.client.patch(f"/api/as-tickets/{t['id']}", json={"cost": 80000})
        r = self.client.post(f"/api/as-tickets/{t['id']}/to-asset-repair")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        detail = self.client.get(f"/api/assets/{a['id']}").get_json()
        total = sum(x["cost"] for x in detail["repairs"])
        self.assertEqual(total, 80000, "정정이 아니라 중복으로 쌓였다")
        # 같은 금액으로 또 누르면 막힌다
        self.assertEqual(self.client.post(f"/api/as-tickets/{t['id']}/to-asset-repair").status_code, 409)

    def test_asset_export_respects_filters(self):
        self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "찾을모델", "grade": "AA", "qty": 1})
        self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "다른모델", "grade": "B급", "qty": 1})
        listed = self.client.get("/api/assets?q=찾을모델").get_json()["rows"]
        self.assertEqual(len(listed), 1)
        r = self.client.get("/api/assets/export?q=찾을모델")
        self.assertEqual(r.status_code, 200)
        from app.importers import write_xlsx  # noqa: F401  (엑셀 생성 경로 확인용)
        self.assertGreater(len(r.data), 500)


class TestRoleCanDoTheirJob(Base):
    """역할별로 '자기 일을 끝까지' 할 수 있어야 한다(2026-07-29 전수조사)."""

    def _user(self, name, perms):
        r = self.client.post("/api/users", json={
            "username": name, "displayName": name, "password": f"{name}-pw-1234",
            "perms": perms, "allCategories": True})
        assert r.status_code == 201, r.get_data(as_text=True)
        c = self.app.test_client()
        assert c.post("/api/auth/login", json={
            "username": name, "password": f"{name}-pw-1234"}).status_code == 200
        return c

    def test_shipping_staff_can_confirm_dispatch(self):
        """배송 담당은 셋팅 권한이 없어도 출고 확인을 할 수 있어야 한다."""
        o, _a = self.make_shipped_order()
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": False})
        c = self._user("shipper2", ["shipping.view", "orders.ship"])
        r = c.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self.client.get(f"/api/orders/{o['id']}").get_json()["shippingDone"])
        # 다른 단계는 여전히 셋팅 권한이 필요하다
        self.assertEqual(c.patch(f"/api/orders/{o['id']}", json={
            "action": "production", "value": False}).status_code, 403)

    def test_as_staff_can_print_return_label(self):
        """A/S 담당은 자기가 발급한 반송 송장을 인쇄할 수 있어야 한다."""
        c = self._user("asonly", ["as.view", "as.manage"])
        t = c.post("/api/as-tickets", json={
            "customer": "홍길동", "phone": "010-1", "address": "서울 1", "symptom": "불량"}).get_json()
        wid = c.post(f"/api/as-tickets/{t['id']}/return-waybill", json={}).get_json()["wid"]
        self.assertEqual(c.get(f"/api/waybills/{wid}/pdf").status_code, 200)

    def test_purchase_staff_can_manage_categories(self):
        """제품 분류는 매입 메뉴 안에 있고 자산 등록에 쓰는 값이라 매입 담당이 관리한다."""
        c = self._user("buyer", ["purchase.view", "purchase.edit"])
        r = c.post("/api/categories", json={"name": "검증분류"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        cid = r.get_json()["id"]
        self.assertEqual(c.patch(f"/api/categories/{cid}", json={"name": "이름변경"}).status_code, 200)

    def test_settings_staff_can_open_settlement(self):
        """설정만 맡은 사람도 정산 요율을 넣을 수 있어야 한다(주문 조회 권한 없이)."""
        self.client.post("/api/orders", json={
            "channel": "쿠팡", "recipient": "홍길동", "productName": "노트북"})
        c = self._user("setter", ["settings.view", "settings.manage"])
        r = c.get("/api/order-channels")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("쿠팡", [x["channel"] for x in r.get_json()])
        self.assertEqual(c.put("/api/settings", json={
            "settlement": {"rates": {"쿠팡": 10}, "shippingCost": 3000}}).status_code, 200)
        # 주문 목록은 여전히 막혀 있다(개인정보)
        self.assertEqual(c.get("/api/orders").status_code, 403)


class TestSummaryMatchesList(Base):
    """상단 카드 숫자와 목록이 같은 기준이어야 한다(눌렀는데 0건 나오면 안 된다)."""

    def _make(self, n, **kw):
        for i in range(n):
            body = {"recipient": f"고객{i}", "productName": "노트북", "amount": 100000}
            body.update(kw)
            self.client.post("/api/orders", json=body)

    def test_card_count_matches_list_under_same_view(self):
        o, _a = self.make_shipped_order()                       # 출고 → 배송중
        self.client.post("/api/orders/bulk", json={"action": "archive", "ids": [o["id"]]})
        self._make(2)                                           # 진행 중 2건
        # 진행 중 보기: 카드에 보관 건이 섞이면 안 된다
        s = self.client.get("/api/orders/summary?view=active").get_json()
        prep = next(x for x in s["statuses"] if x["code"] == "preparing")
        lst = self.client.get("/api/orders?view=active&mallStatus=preparing").get_json()
        self.assertEqual(prep["count"], lst["shown"])
        self.assertEqual(prep["count"], 2)
        # 전체 보기: 배송완료(보관) 1건이 카드에도 목록에도 나온다
        s = self.client.get("/api/orders/summary?view=all").get_json()
        done = next(x for x in s["statuses"] if x["code"] == "delivered")
        lst = self.client.get("/api/orders?view=all&mallStatus=delivered").get_json()
        self.assertEqual(done["count"], lst["shown"])
        self.assertEqual(done["count"], 1)

    def test_card_respects_period_and_channel(self):
        self._make(1, channel="쿠팡", orderedAt="2026-07-10")
        self._make(1, channel="고도몰", orderedAt="2026-06-10")
        s = self.client.get("/api/orders/summary?view=all&from=2026-07-01&to=2026-07-31").get_json()
        self.assertEqual(sum(x["count"] for x in s["statuses"]), 1)
        # 쇼핑몰을 고르면 상태 카드는 그 몰만, 몰 버튼은 전체 몰이 그대로 보인다
        s = self.client.get("/api/orders/summary?view=all&channel=쿠팡").get_json()
        self.assertEqual(sum(x["count"] for x in s["statuses"]), 1)
        self.assertEqual({m["channel"] for m in s["malls"]}, {"쿠팡", "고도몰"})

    def test_unassigned_channel_filter(self):
        """쇼핑몰이 비어 있는 건(이관 데이터)만 골라 볼 수 있어야 한다."""
        self._make(2, channel="쿠팡")
        o = self.client.post("/api/orders", json={
            "recipient": "채널없음", "productName": "노트북"}).get_json()
        with sqlite3.connect(self.db_path) as conn:      # 수기 등록은 '수기'로 채워지므로 직접
            conn.execute("UPDATE orders SET channel='' WHERE id=?", (o["id"],))
        r = self.client.get("/api/orders?view=all&channel=(미지정)").get_json()
        self.assertEqual(r["shown"], 1)
        self.assertEqual(r["orders"][0]["recipient"], "채널없음")


class TestNoDoubleAssignment(Base):
    """★같은 한 대가 두 고객에게 나가는 것을 막는다(2026-07-29 전수조사 확인)."""

    def test_recall_receive_releases_old_order(self):
        """반품으로 돌아온 물건은 예전 주문에서 풀려야 한다."""
        o1, a = self.make_shipped_order(recipient="첫고객")
        wid = self.client.post(f"/api/orders/{o1['id']}/recall", json={"reason": "반품"}).get_json()["wid"]
        self.client.post(f"/api/waybills/{wid}/received", json={"status": "ready"})
        d1 = self.client.get(f"/api/orders/{o1['id']}").get_json()
        self.assertEqual(d1["assets"], [], "회수했는데 예전 주문에 자산이 그대로 붙어 있다")
        # 새 주문에는 정상적으로 매칭된다
        o2 = self.client.post("/api/orders", json={
            "recipient": "다음고객", "productName": "노트북"}).get_json()
        r = self.client.patch(f"/api/orders/{o2['id']}", json={
            "action": "assets", "assetIds": [a["id"]]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_cannot_match_asset_already_on_live_order(self):
        """데이터가 꼬여도 두 주문에 같은 자산을 붙일 수 없다."""
        # ★'중복 매칭 허용'(2026-08-31, 기본 켜짐)을 끄고 차단 모드의 안전핀을 검증한다
        self.assertEqual(self.client.put("/api/settings", json={
            "order_asset_duplicate": {"enabled": False}}).status_code, 200)
        o1 = self.client.post("/api/orders", json={"recipient": "A", "productName": "N"}).get_json()
        a = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1}).get_json()[0]
        self.client.patch(f"/api/orders/{o1['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        # 강제로 판매가능 상태로 돌려 놓아도(꼬인 데이터 재현) 매칭은 거부된다
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE assets SET status='ready' WHERE id=?", (a["id"],))
        o2 = self.client.post("/api/orders", json={"recipient": "B", "productName": "N"}).get_json()
        r = self.client.patch(f"/api/orders/{o2['id']}", json={
            "action": "assets", "assetIds": [a["id"]]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("이미 다른 주문", r.get_json()["error"])

    def test_recall_cancel_keeps_link(self):
        """예약만 취소한 경우는 물건이 아직 고객에게 있으므로 연결을 유지한다."""
        o, a = self.make_shipped_order()
        wid = self.client.post(f"/api/orders/{o['id']}/recall", json={}).get_json()["wid"]
        self.client.post(f"/api/waybills/{wid}/cancel")
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(len(d["assets"]), 1, "회수 예약만 취소했는데 매칭이 풀렸다")


class TestLegacyPurchaseTable(unittest.TestCase):
    """구형 테이블(supplier_id NOT NULL)이 있어도 가입고 전표가 만들어져야 한다."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-legacy-"))
        self.db_path = self.tmp / "legacy.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE purchase_batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
              purchase_date TEXT NOT NULL, total_amount INTEGER NOT NULL DEFAULT 0,
              memo TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE suppliers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
              contact TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
              memo TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL DEFAULT '');
            INSERT INTO suppliers(name, created_at) VALUES('기존거래처','2026-01-01');
            INSERT INTO purchase_batches(supplier_id, purchase_date, total_amount, memo,
              created_by, created_at) VALUES(1,'2026-01-05',500000,'기존전표','대표','2026-01-05');
        """)
        conn.commit()
        conn.close()
        self.app = create_app(db_path=self.db_path)
        self.app.testing = True
        self.client = self.app.test_client()
        auth_mod._login_failures.clear()
        self.client.post("/api/auth/setup", json={
            "username": "admin", "displayName": "대표", "password": ADMIN_PW})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_existing_rows_survive_rebuild(self):
        rows = self.client.get("/api/purchase-batches").get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["memo"], "기존전표")
        self.assertEqual(rows[0]["totalAmount"], 500000)

    def test_provisional_slip_without_supplier(self):
        """가입고는 거래처 없이 만들 수 있어야 한다(택배로 물건만 먼저 오는 경우)."""
        r = self.client.post("/api/purchase-batches", json={
            "stage": "provisional", "purchaseDate": "2026-07-29", "memo": "택배 선입고"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        made = self.client.get("/api/purchase-batches").get_json()
        self.assertTrue(any(b["memo"] == "택배 선입고" for b in made))

    def test_purchase_slip_still_needs_supplier(self):
        """매입 확정에는 거래처가 필요하다(이 규칙은 유지)."""
        r = self.client.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-07-29"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("거래처", r.get_json()["error"])


class TestLegacyRebuildKeepsAssetsUsable(unittest.TestCase):
    """★전표 테이블을 다시 만든 뒤에도 자산 등록이 되어야 한다.

    2026-07-29 운영 사고: _rebuild_purchase_batches의 ALTER TABLE ... RENAME이
    assets.batch_id의 REFERENCES까지 purchase_batches_old로 따라 바꿨고,
    그 테이블을 지우면서 assets가 '없는 테이블'을 참조하게 됐다.
    그 결과 자산 신규 등록·엑셀 이관·전표 연결이 전부 '서버 내부 오류'로 막혔다.

    앞의 TestLegacyPurchaseTable은 assets를 미리 만들지 않아 이 경로를 타지 않는다.
    여기서는 '정상 스키마(assets가 purchase_batches를 참조) + 구형 전표 테이블'이라는
    실제 라이브 조건을 그대로 만든다.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-legacy-fk-"))
        self.db_path = self.tmp / "legacyfk.db"
        schema = (Path(__file__).resolve().parent.parent / "app" / "schema.sql").read_text("utf-8")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(schema)                 # 정상 스키마 — assets가 purchase_batches 참조
        conn.executescript("""
            PRAGMA foreign_keys=OFF;
            DROP TABLE purchase_batches;
            CREATE TABLE purchase_batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
              purchase_date TEXT NOT NULL, total_amount INTEGER NOT NULL DEFAULT 0,
              memo TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL, created_at TEXT NOT NULL);
            INSERT INTO suppliers(name, created_at) VALUES('기존거래처','2026-01-01');
            INSERT INTO purchase_batches(supplier_id, purchase_date, total_amount, memo,
              created_by, created_at) VALUES(1,'2026-01-05',500000,'기존전표','대표','2026-01-05');
        """)
        conn.commit()
        conn.close()
        self.app = create_app(db_path=self.db_path)   # 여기서 마이그레이션(재생성)이 돈다
        self.app.testing = True
        self.client = self.app.test_client()
        auth_mod._login_failures.clear()
        self.client.post("/api/auth/setup", json={
            "username": "admin", "displayName": "대표", "password": ADMIN_PW})
        # '노트북'은 이제 기본 시드(sort 0)라 POST 하면 409 — 시드된 것을 쓴다(2026-09-03, A5)
        self.cat = next(c for c in self.client.get("/api/categories").get_json() if c["name"] == "노트북")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_assets_table_does_not_point_at_dropped_table(self):
        conn = sqlite3.connect(self.db_path)
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='assets'").fetchone()[0]
        conn.close()
        self.assertNotIn("purchase_batches_old", sql,
                         "자산 테이블이 사라진 테이블을 참조한다 — 자산 등록이 전부 막힌다")

    def test_asset_can_still_be_registered(self):
        r = self.client.post("/api/assets", json={
            "categoryId": self.cat["id"], "assetNo": "FK-TEST-1",
            "model": "NT371B5M", "maker": "삼성", "grade": "AA", "qty": 1})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))

    def test_asset_can_be_linked_to_slip(self):
        """전표에 연결한 자산도 등록돼야 한다 — 참조가 깨졌을 때 가장 먼저 막히는 길이다."""
        slip = self.client.post("/api/purchase-batches", json={
            "stage": "provisional", "purchaseDate": "2026-07-29", "memo": "가입고"}).get_json()
        r = self.client.post("/api/assets", json={
            "categoryId": self.cat["id"], "assetNo": "FK-TEST-2", "model": "15U470",
            "maker": "LG", "grade": "AA", "qty": 1, "batchId": slip["id"]})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))

    def test_existing_slip_survived(self):
        rows = self.client.get("/api/purchase-batches").get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["memo"], "기존전표")


class TestBackupSafety(Base):
    """백업이 '있다'가 아니라 '쓸 수 있다'를 보장한다(2026-07-28 빈 백업 사고)."""

    def test_backup_contains_data(self):
        self.client.post("/api/orders", json={"recipient": "홍길동", "productName": "노트북"})
        r = self.client.post("/api/backups/run")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rows = self.client.get("/api/backups").get_json()["backups"]
        made = next(x for x in rows if x["name"] == r.get_json()["name"])
        self.assertIsNotNone(made["counts"])
        self.assertGreaterEqual(made["counts"]["orders"], 1)   # 빈 백업이면 여기서 걸린다

    def test_backup_repeats_within_a_day(self):
        """하루 1회로 두면 아침 백업 뒤 작업이 통째로 빠진다 — 주기가 지나면 또 만들어야 한다.

        2026-07-28 실제 사고: 21:13 백업 → 그 뒤 626건 이관 → 다음 날까지 새 백업이 안 생겨
        '주문 0건짜리 백업'만 남아 있었다.
        """
        import os
        from app import db as db_mod
        bdir = db_mod.backup_dir(self.db_path)
        self.client.post("/api/orders", json={"recipient": "A", "productName": "노트북"})
        first = sorted(p.name for p in bdir.glob("ows-*.db"))
        self.assertEqual(len(first), 1)
        # 아침에 백업이 만들어진 상황을 재현 — 5시간 전으로 돌린다(기본 주기 4시간)
        old = config.now().timestamp() - 5 * 3600
        for p in bdir.glob("ows-*.db"):
            os.utime(p, (old, old))
        self.client.post("/api/orders", json={"recipient": "B", "productName": "노트북"})
        # 백업은 '쓰기 직전' 상태를 담는다. 주기가 지났으므로 새로 만들어져 있어야 한다.
        newest = max(bdir.glob("ows-*.db"), key=lambda p: p.stat().st_mtime)
        self.assertLess(config.now().timestamp() - newest.stat().st_mtime, 60,
                        "주기가 지났는데 새 백업이 만들어지지 않았다")
        conn = sqlite3.connect(f"file:{newest}?mode=ro", uri=True)
        try:
            self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
        finally:
            conn.close()

    def test_backup_not_made_on_every_write(self):
        """주기 안에서는 쓰기마다 백업하지 않는다(디스크·시간 낭비)."""
        from app import db as db_mod
        for i in range(5):
            self.client.post("/api/orders", json={"recipient": f"고객{i}", "productName": "노트북"})
        self.assertEqual(len(list(db_mod.backup_dir(self.db_path).glob("ows-*.db"))), 1)

    def test_backup_download(self):
        name = self.client.post("/api/backups/run").get_json()["name"]
        r = self.client.get(f"/api/backups/{name}/download")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"SQLite format 3"))
        # 경로 조작 차단
        self.assertEqual(self.client.get("/api/backups/..%2F..%2Fows.db/download").status_code, 404)
        self.assertEqual(self.client.get("/api/backups/notows.db/download").status_code, 400)

    def test_mirror_copy(self):
        """2차 사본이 다른 폴더에 실제로 만들어져야 한다(디스크 하나 고장 대비)."""
        from app import db as db_mod
        mirror = self.tmp / "mirror"
        orig_mirror, orig_live = config.BACKUP_MIRROR, config.LIVE_DB_PATH
        config.BACKUP_MIRROR = str(mirror)
        config.LIVE_DB_PATH = self.db_path     # 이 DB를 '운영 DB'로 취급
        self.addCleanup(setattr, config, "BACKUP_MIRROR", orig_mirror)
        self.addCleanup(setattr, config, "LIVE_DB_PATH", orig_live)
        name = self.client.post("/api/backups/run").get_json()["name"]
        self.assertTrue((mirror / name).exists())
        self.assertTrue(db_mod.mirror_dir().exists())

    def test_mirror_skipped_for_non_live_db(self):
        """★테스트·검증 서버(임시 DB)는 2차 백업 폴더를 건드리면 안 된다.

        2026-07-29: 이걸 안 막아 테스트를 돌릴 때마다 D 드라이브에 사본이 쌓여
        1,833개가 생겼다(전부 쓸모없는 빈 DB).
        """
        mirror = self.tmp / "mirror"
        orig = config.BACKUP_MIRROR
        config.BACKUP_MIRROR = str(mirror)
        self.addCleanup(setattr, config, "BACKUP_MIRROR", orig)
        # self.db_path는 임시 DB라 운영 DB 고정 경로와 다르다
        self.client.post("/api/backups/run")
        self.client.post("/api/orders", json={"recipient": "홍길동", "productName": "노트북"})
        self.assertFalse(mirror.exists() and any(mirror.glob("*.db")),
                         "임시 DB인데 2차 백업이 만들어졌다")

    def test_mirror_skipped_even_when_db_path_env_points_at_copy(self):
        """★검증 서버가 'OWS_DB=<라이브 사본>'으로 떠도 미러는 막혀야 한다.

        앞의 테스트만으로는 부족했다. 실제 사고(2026-07-29 ows-20260729-103002.db)는
        라이브를 복사해 OWS_DB로 지정한 경우였고, 그러면 config.DB_PATH 자체가 사본이 되어
        '열린 DB == config.DB_PATH'가 항상 참이 됐다. 그래서 판정 기준을
        환경변수를 타지 않는 config.LIVE_DB_PATH로 바꿨다 — 이 테스트가 그 회귀를 잡는다.
        """
        mirror = self.tmp / "mirror"
        orig_mirror, orig_db = config.BACKUP_MIRROR, config.DB_PATH
        config.BACKUP_MIRROR = str(mirror)
        config.DB_PATH = self.db_path          # OWS_DB=<사본> 으로 뜬 상황을 흉내
        self.addCleanup(setattr, config, "BACKUP_MIRROR", orig_mirror)
        self.addCleanup(setattr, config, "DB_PATH", orig_db)
        self.client.post("/api/backups/run")
        self.assertFalse(mirror.exists() and any(mirror.glob("*.db")),
                         "OWS_DB로 사본을 지정했는데 2차 백업이 만들어졌다(환경변수 구멍)")

    def test_mirror_failure_does_not_break_backup(self):
        """2차 위치가 죽어 있어도 1차 백업과 저장은 계속돼야 한다."""
        orig = config.BACKUP_MIRROR
        config.BACKUP_MIRROR = "Z:/없는드라이브/ows"
        self.addCleanup(setattr, config, "BACKUP_MIRROR", orig)
        r = self.client.post("/api/backups/run")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북"}).status_code, 201)

    def test_backup_requires_permission(self):
        r = self.client.post("/api/users", json={
            "username": "nobackup", "displayName": "무권한", "password": "nobackup-pw-1234",
            "perms": ["orders.view"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "nobackup", "password": "nobackup-pw-1234"})
        self.assertEqual(c.get("/api/backups").status_code, 403)
        self.assertEqual(c.get("/api/backups/ows-x.db/download").status_code, 403)


class TestBundleA(Base):
    """RMS 대조 — '백엔드는 있는데 화면에 버튼이 없던' 것들의 계약 확인."""

    def test_memo_roundtrip(self):
        """주문 메모(작업 지시)가 등록·수정·목록 모두에서 살아 있어야 한다."""
        o = self.client.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북",
            "memo": "웹캠 불가 모델 — 고객 고지 후 USB웹캠 증정"}).get_json()
        self.assertIn("웹캠", o["memo"])
        got = self.client.get("/api/orders?view=all").get_json()["orders"][0]
        self.assertIn("웹캠", got["memo"])          # 목록에 실려야 배지를 띄울 수 있다
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertIn("웹캠", d["memo"])
        r = self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "details", "expectedUpdatedAt": d["updatedAt"],
            "fields": {"memo": "베젤 벌어짐 - 문성진 확인"}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.client.get(f"/api/orders/{o['id']}").get_json()["memo"],
                         "베젤 벌어짐 - 문성진 확인")

    def test_as_detail_exposes_recall_state(self):
        """A/S 상세가 진행 중 회수 예약을 알려줘야 버튼을 감출 수 있다."""
        o, a = self.make_shipped_order()
        t = self.client.post("/api/as-tickets", json={
            "customer": "홍길동", "phone": "010-1111-2222", "address": "서울시 강남구 1",
            "symptom": "액정 불량", "assetId": a["id"]}).get_json()
        self.assertEqual(self.client.get(f"/api/as-tickets/{t['id']}").get_json()["recallWid"], "")
        wid = self.client.post(f"/api/as-tickets/{t['id']}/recall", json={}).get_json()["wid"]
        self.assertEqual(self.client.get(f"/api/as-tickets/{t['id']}").get_json()["recallWid"], wid)

    def test_as_address_editable(self):
        """회수 예약에 주소가 필요하니 상세에서 고칠 수 있어야 한다."""
        t = self.client.post("/api/as-tickets", json={
            "customer": "김철수", "phone": "010-2222-3333", "symptom": "부팅 불가"}).get_json()
        r = self.client.patch(f"/api/as-tickets/{t['id']}", json={"address": "부산시 해운대구 2"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.client.get(f"/api/as-tickets/{t['id']}").get_json()["address"],
                         "부산시 해운대구 2")

    def test_recall_receive_accepts_ui_statuses(self):
        """화면 셀렉트가 보내는 상태 값을 서버가 모두 받아야 한다."""
        for status in ("in_stock", "refurbishing", "repair", "defective", "scrapped"):
            o, a = self.make_shipped_order()
            wid = self.client.post(f"/api/orders/{o['id']}/recall", json={}).get_json()["wid"]
            r = self.client.post(f"/api/waybills/{wid}/received", json={"status": status})
            self.assertEqual(r.status_code, 200, f"{status}: {r.get_data(as_text=True)}")
            self.assertEqual(self.client.get(f"/api/assets/{a['id']}").get_json()["status"], status)


class TestBundleB(Base):
    """RMS 대조 — 검색·조회 실무(하이픈·기간·엑셀·고객 이력)."""

    def test_phone_search_ignores_hyphens(self):
        """010-1234-5678로 저장돼 있어도 01012345678로 찾을 수 있어야 한다."""
        self.client.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북", "phone": "010-1234-5678"})
        self.client.post("/api/orders", json={
            "recipient": "김철수", "productName": "노트북", "phone": "01099998888"})
        for q in ("01012345678", "010-1234-5678", "1234-5678", "12345678"):
            r = self.client.get(f"/api/orders?view=all&q={q}").get_json()
            self.assertEqual(r["shown"], 1, f"검색어 {q}")
            self.assertEqual(r["orders"][0]["recipient"], "홍길동")
        # 반대 방향 — 하이픈 없이 저장된 번호를 하이픈 붙여 검색
        r = self.client.get("/api/orders?view=all&q=010-9999-8888").get_json()
        self.assertEqual(r["orders"][0]["recipient"], "김철수")

    def test_memo_is_searchable(self):
        self.client.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북", "memo": "당일출고희망고객"})
        self.client.post("/api/orders", json={"recipient": "김철수", "productName": "노트북"})
        r = self.client.get("/api/orders?view=all&q=당일출고").get_json()
        self.assertEqual(r["shown"], 1)

    def test_date_range_filter(self):
        for day in ("2026-07-01", "2026-07-15", "2026-07-28"):
            self.client.post("/api/orders", json={
                "recipient": f"고객{day}", "productName": "노트북", "orderedAt": day})
        r = self.client.get("/api/orders?view=all&from=2026-07-10&to=2026-07-20").get_json()
        self.assertEqual(r["shown"], 1)
        self.assertEqual(r["orders"][0]["recipient"], "고객2026-07-15")
        r = self.client.get("/api/orders?view=all&from=2026-07-15").get_json()
        self.assertEqual(r["shown"], 2)
        r = self.client.get("/api/orders?view=all&to=2026-07-01").get_json()
        self.assertEqual(r["shown"], 1)

    def test_date_filter_handles_datetime_values(self):
        """몰이 '2026-07-15 13:20:00'처럼 시각까지 주는 경우도 같은 날로 잡혀야 한다."""
        self.client.post("/api/orders", json={
            "recipient": "시각포함", "productName": "노트북", "orderedAt": "2026-07-15 13:20:00"})
        r = self.client.get("/api/orders?view=all&from=2026-07-15&to=2026-07-15").get_json()
        self.assertEqual(r["shown"], 1)

    def test_export_respects_filters(self):
        self.client.post("/api/orders", json={
            "channel": "쿠팡", "recipient": "홍길동", "productName": "노트북", "amount": 100000})
        self.client.post("/api/orders", json={
            "channel": "고도몰", "recipient": "김철수", "productName": "노트북"})
        r = self.client.get("/api/orders/export?view=all&channel=쿠팡")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r.headers["Content-Type"])
        self.assertIn("ows-orders-", r.headers["Content-Disposition"])
        self.assertGreater(len(r.data), 500)          # 빈 파일이 아니다

    def test_customer_history_by_phone(self):
        """번호 표기가 달라도 같은 사람으로 묶여야 한다."""
        a = self.client.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북A", "phone": "010-1234-5678"}).get_json()
        self.client.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북B", "phone": "01012345678"})
        self.client.post("/api/orders", json={
            "recipient": "남", "productName": "노트북C", "phone": "010-0000-0000"})
        r = self.client.get(f"/api/orders/customer-history?orderId={a['id']}").get_json()
        self.assertEqual(r["matchedBy"], "전화번호")
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["orders"][0]["productName"], "노트북B")
        self.assertTrue(r["orders"][0]["mallStatusLabel"])

    def test_list_shows_same_customer_count_across_channels(self):
        """★엑셀 업로드분과 API 수집분이 '같은 고객'으로 목록에서 바로 보여야 한다.

        대표 요청(2026-07-29): 주문수집 리스트를 직접 올리는 경우와 API로 받는 경우가
        섞여 있는데, 성함·연락처가 맞으면 같은 고객으로 묶어 표시할 것.
        채널이 달라도(수기 vs 스마트스토어) 전화번호가 같으면 한 사람이다.
        """
        self.client.post("/api/orders", json={
            "recipient": "김하나", "productName": "노트북A", "phone": "010-7777-8888",
            "channel": "스마트스토어"})                       # API 수집분 흉내
        self.client.post("/api/orders", json={
            "recipient": "김하나", "productName": "노트북B", "phone": "01077778888",
            "channel": ""})                                   # 수기 업로드분(표기도 다르게)
        self.client.post("/api/orders", json={
            "recipient": "남남", "productName": "노트북C", "phone": "010-0000-1111"})
        rows = self.client.get("/api/orders?view=active").get_json()["orders"]
        kim = [o for o in rows if o["recipient"] == "김하나"]
        other = [o for o in rows if o["recipient"] == "남남"]
        self.assertEqual(len(kim), 2)
        for o in kim:
            self.assertEqual(o["sameCustomerCount"], 1,
                             "채널·표기가 달라도 같은 번호면 같은 고객으로 세야 한다")
        self.assertEqual(other[0]["sameCustomerCount"], 0)

    def test_same_customer_count_ignores_short_phones(self):
        """빈 번호·안심번호 조각(9자리 미만)끼리 같은 고객으로 묶이면 안 된다."""
        self.client.post("/api/orders", json={
            "recipient": "가", "productName": "노트북A", "phone": ""})
        self.client.post("/api/orders", json={
            "recipient": "나", "productName": "노트북B", "phone": ""})
        rows = self.client.get("/api/orders?view=active").get_json()["orders"]
        for o in rows:
            self.assertEqual(o["sameCustomerCount"], 0,
                             "번호 없는 주문끼리 유령 고객으로 묶였다")

    def test_customer_history_falls_back_to_name_address(self):
        a = self.client.post("/api/orders", json={
            "recipient": "무전화", "productName": "노트북A", "address": "서울시 강남구 1"}).get_json()
        self.client.post("/api/orders", json={
            "recipient": "무전화", "productName": "노트북B", "address": "서울시 강남구 1"})
        r = self.client.get(f"/api/orders/customer-history?orderId={a['id']}").get_json()
        self.assertEqual(r["matchedBy"], "이름+주소")
        self.assertEqual(r["count"], 1)

    def test_customer_history_includes_as(self):
        o, asset = self.make_shipped_order(phone="010-5555-6666")
        self.client.post("/api/as-tickets", json={
            "customer": "홍길동", "phone": "01055556666", "symptom": "액정 불량",
            "assetId": asset["id"]})
        r = self.client.get(f"/api/orders/customer-history?orderId={o['id']}").get_json()
        self.assertEqual(len(r["asTickets"]), 1)
        self.assertEqual(r["asTickets"][0]["symptom"], "액정 불량")

    def test_customer_search_dedupes_and_uses_latest(self):
        """같은 사람은 한 줄로, 주소는 가장 최근 것으로."""
        self.client.post("/api/orders", json={
            "recipient": "홍길동", "productName": "N", "phone": "010-1234-5678",
            "address": "예전주소", "orderedAt": "2026-01-01"})
        self.client.post("/api/orders", json={
            "recipient": "홍길동", "productName": "N", "phone": "01012345678",
            "address": "최근주소", "postalCode": "06000", "orderedAt": "2026-07-20"})
        r = self.client.get("/api/orders/customer-search?q=홍길동").get_json()
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["count"], 2)
        self.assertEqual(r[0]["address"], "최근주소")
        self.assertEqual(r[0]["postalCode"], "06000")
        # 번호로도 찾힌다(하이픈 무관)
        self.assertEqual(len(self.client.get("/api/orders/customer-search?q=01012345678").get_json()), 1)

    def test_customer_search_short_query(self):
        self.assertEqual(self.client.get("/api/orders/customer-search?q=홍").get_json(), [])

    def test_customer_search_requires_edit_perm(self):
        r = self.client.post("/api/users", json={
            "username": "viewonly", "displayName": "조회", "password": "viewonly-pw-1234",
            "perms": ["orders.view"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "viewonly", "password": "viewonly-pw-1234"})
        self.assertEqual(c.get("/api/orders/customer-search?q=홍길동").status_code, 403)

    def test_customer_history_needs_order(self):
        self.assertEqual(self.client.get("/api/orders/customer-history").status_code, 400)
        self.assertEqual(
            self.client.get("/api/orders/customer-history?orderId=99999").status_code, 404)


class TestBundleC(Base):
    """RMS 대조 — 일괄 처리(선택 후 한 번에)."""

    def _orders(self, n, **kw):
        out = []
        for i in range(n):
            body = {"recipient": f"고객{i}", "productName": "노트북", "amount": 100000}
            body.update(kw)
            out.append(self.client.post("/api/orders", json=body).get_json())
        return out

    def test_bulk_stage_partial_failure(self):
        """한 건이 막혀도 나머지는 진행하고, 막힌 사유를 알려준다."""
        a, b = self._orders(2)
        # a만 제작·검수까지 끝내 둔다 → b는 출고 확인 불가
        for act in ("production", "softwareInspection"):
            self.client.patch(f"/api/orders/{a['id']}", json={"action": act, "value": True})
        r = self.client.post("/api/orders/bulk", json={
            "action": "shipping", "value": True, "ids": [a["id"], b["id"]]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        res = r.get_json()
        self.assertEqual(res["ok"], 1)
        self.assertEqual(res["done"], [a["id"]])
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual(res["failed"][0]["id"], b["id"])
        self.assertIn("검수", res["failed"][0]["reason"])
        self.assertTrue(self.client.get(f"/api/orders/{a['id']}").get_json()["shippingDone"])
        self.assertFalse(self.client.get(f"/api/orders/{b['id']}").get_json()["shippingDone"])

    def test_bulk_cancel_keeps_single_order_guards(self):
        """일괄 취소도 단건과 같은 규칙을 따른다.

        ★2026-09-03 대표 지시로 규칙이 바뀌었다 — 출고 확인된 주문도 취소되고, 취소하면
          매칭 자산이 재고로 돌아온다("출고확인까지 갔는데 취소한 경우는 자산이 빠져야").
          대신 **아직 집화 전인 송장**이 있으면 일괄에서는 건너뛴다(기사 헛걸음 방지 —
          정말 취소해야 하면 그 건을 열어 단건으로 확인 후 취소한다).
        """
        shipped, asset = self.make_shipped_order()
        plain = self._orders(1)[0]
        r = self.client.post("/api/orders/bulk", json={
            "action": "cancel", "reason": "일괄 정리", "ids": [shipped["id"], plain["id"]]}).get_json()
        # 출고 확인만으로는 더 이상 막지 않는다 — 둘 다 취소되고 자산도 돌아온다
        self.assertEqual(sorted(r["done"]), sorted([shipped["id"], plain["id"]]),
                         f"출고 확인된 건이 취소되지 않았다: {r.get('failed')}")
        self.assertEqual(self.client.get(f"/api/assets/{asset['id']}").get_json()["status"],
                         "in_stock", "취소했는데 자산이 그 주문에 묶인 채로 남았다")

    def test_bulk_cancel_skips_orders_with_open_waybill(self):
        """아직 집화 전인 송장이 붙은 건은 일괄 취소에서 건너뛴다(기사 헛걸음 방지)."""
        o, _a = self.make_shipped_order()
        self.client.post(f"/api/orders/{o['id']}/waybill", json={})
        plain = self._orders(1)[0]
        r = self.client.post("/api/orders/bulk", json={
            "action": "cancel", "reason": "일괄 정리", "ids": [o["id"], plain["id"]]}).get_json()
        self.assertEqual(r["done"], [plain["id"]])
        self.assertIn("집화 전", r["failed"][0]["reason"])

    def test_bulk_cancel_releases_assets(self):
        o = self._orders(1)[0]
        a = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1}).get_json()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.client.post("/api/orders/bulk", json={
            "action": "cancel", "reason": "중복 주문", "ids": [o["id"]]})
        self.assertIn(self.client.get(f"/api/assets/{a['id']}").get_json()["status"],
                      ("ready", "in_stock"))

    def test_bulk_requires_reason_and_perm(self):
        o = self._orders(1)[0]
        self.assertEqual(self.client.post("/api/orders/bulk", json={
            "action": "cancel", "ids": [o["id"]]}).status_code, 400)
        self.assertEqual(self.client.post("/api/orders/bulk", json={
            "action": "nosuch", "ids": [o["id"]]}).status_code, 400)
        self.assertEqual(self.client.post("/api/orders/bulk", json={
            "action": "shipping", "ids": []}).status_code, 400)
        r = self.client.post("/api/users", json={
            "username": "workonly", "displayName": "작업", "password": "workonly-pw-1234",
            "perms": ["setup.view", "orders.work"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "workonly", "password": "workonly-pw-1234"})
        self.assertEqual(c.post("/api/orders/bulk", json={
            "action": "cancel", "reason": "x", "ids": [o["id"]]}).status_code, 403)

    def test_bulk_pay_and_delivered(self):
        a, b = self._orders(2)
        self.client.post("/api/orders/bulk", json={
            "action": "payStatus", "value": "unpaid", "ids": [a["id"], b["id"]]})
        r = self.client.get("/api/orders?view=all&mallStatus=unpaid").get_json()
        self.assertEqual(r["shown"], 2)
        self.client.post("/api/orders/bulk", json={
            "action": "delivered", "value": True, "ids": [a["id"]]})
        self.assertEqual(self.client.get("/api/orders?view=all&mallStatus=delivered").get_json()["shown"], 1)

    def test_match_assets_by_scanned_numbers(self):
        """스캐너가 넣는 관리번호로 바로 매칭 — 공백·중복·대소문자를 흡수한다."""
        o = self._orders(1)[0]
        a1 = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1}).get_json()[0]
        a2 = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L490", "qty": 1}).get_json()[0]
        r = self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "assets",
            "assetNos": [f"  {a1['assetNo']} ", a2["assetNo"].lower(), a1["assetNo"], ""]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        got = {x["assetNo"] for x in self.client.get(f"/api/orders/{o['id']}").get_json()["assets"]}
        self.assertEqual(got, {a1["assetNo"], a2["assetNo"]})

    def test_unknown_scanned_number_reports_which(self):
        o = self._orders(1)[0]
        r = self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "assets", "assetNos": ["없는번호1234"]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("없는번호1234", r.get_json()["error"])

    def test_bulk_assets_status_and_location(self):
        ids = [self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1}).get_json()[0]["id"]
            for _ in range(3)]
        r = self.client.post("/api/assets/bulk", json={
            "ids": ids, "status": "ready", "location": "A동 3층", "grade": "AA"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["ok"], 3)
        for aid in ids:
            a = self.client.get(f"/api/assets/{aid}").get_json()
            self.assertEqual((a["status"], a["location"], a["grade"]), ("ready", "A동 3층", "AA"))
            self.assertIn("상태변경", [e["action"] for e in a["events"]])

    def test_bulk_assets_refuses_reserved(self):
        """주문에 매칭된 자산은 일괄 변경으로도 건드리지 않는다."""
        o = self._orders(1)[0]
        a = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1}).get_json()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        r = self.client.post("/api/assets/bulk", json={"ids": [a["id"]], "status": "scrapped"}).get_json()
        self.assertEqual(r["ok"], 0)
        self.assertIn("매칭", r["failed"][0]["reason"])
        self.assertEqual(self.client.get(f"/api/assets/{a['id']}").get_json()["status"], "reserved")

    def test_bulk_assets_rejects_flow_only_status(self):
        a = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1}).get_json()[0]
        self.assertEqual(self.client.post("/api/assets/bulk", json={
            "ids": [a["id"]], "status": "shipped"}).status_code, 400)
        self.assertEqual(self.client.post("/api/assets/bulk", json={"ids": [a["id"]]}).status_code, 400)

    def test_duplicate_assets_found(self):
        """★중복 감지는 'TMS 이관으로 들어온 중복'을 잡기 위한 기능이다.

        2026-07-30부터 API 등록은 중복 시리얼을 409로 막으므로, 화면으로는 중복을 못 만든다.
        하지만 이관(migration.py)은 별도 INSERT라 여전히 중복이 섞여 들어올 수 있어
        감지 기능은 남아 있어야 한다. 그 상황을 직접 INSERT로 재현한다.
        """
        self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "maker": "삼성", "model": "NT371",
            "serial": "SN-DUP-001", "qty": 1})
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO assets(asset_no, category_id, maker, model, serial, grade, status, "
            "received, created_by, created_at, updated_at) "
            "VALUES('260730-9999',?,?,?,?,'미정','in_stock',1,'이관','2026-07-30','2026-07-30')",
            (self.cats[0]["id"], "삼성", "NT371", "SN-DUP-001"))
        conn.commit()
        conn.close()
        self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "serial": "SN-UNIQ", "qty": 1})
        r = self.client.get("/api/assets/duplicates").get_json()
        self.assertEqual(r["count"], 1)
        g = r["groups"][0]
        self.assertEqual(g["serial"], "SN-DUP-001")
        self.assertEqual(g["count"], 2)
        self.assertEqual(len(g["assets"]), 2)

    def test_duplicates_ignore_blank_serials(self):
        for _ in range(3):
            self.client.post("/api/assets", json={
                "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1})
        self.assertEqual(self.client.get("/api/assets/duplicates").get_json()["count"], 0)

    def test_bulk_waybill_print_merges(self):
        wids = []
        for _ in range(2):
            o, _a = self.make_shipped_order()
            wids.append(self.client.post(f"/api/orders/{o['id']}/waybill", json={}).get_json()["wid"])
        r = self.client.post("/api/waybills/print", json={"wids": wids})
        self.assertEqual(r.status_code, 200)          # 본문은 PDF 바이너리라 텍스트로 읽지 않는다
        self.assertEqual(r.mimetype, "application/pdf")
        from pypdf import PdfReader
        import io as _io
        self.assertEqual(len(PdfReader(_io.BytesIO(r.data)).pages), 2)   # 두 건이 한 파일에

    def test_bulk_print_reports_missing(self):
        self.assertEqual(self.client.post("/api/waybills/print", json={"wids": []}).status_code, 400)
        r = self.client.post("/api/waybills/print", json={"wids": ["WB-없음"]})
        self.assertEqual(r.status_code, 404)
        self.assertIn("WB-없음", r.get_json()["error"])


class TestBundleD(Base):
    """RMS 대조 — 정산 정확도(수수료·택배비·환불·대장·수익성)."""

    def _fees(self, rates=None, ship=0):
        self.client.put("/api/settings", json={"settlement": {
            "rates": rates if rates is not None else {"쿠팡": 10.8, "_default": 3}, "shippingCost": ship}})

    def test_fee_applied_on_shipping(self):
        """출고 확인 시 몰 요율로 수수료가 자동으로 붙는다."""
        self._fees(ship=3000)
        o, _a = self.make_shipped_order(channel="쿠팡", amount=500000)
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(d["feeAmount"], 54000)          # 500,000 × 10.8%
        self.assertEqual(d["feeRate"], 10.8)
        self.assertEqual(d["shippingCost"], 3000)
        self.assertEqual(d["netAmount"], 446000)

    def test_default_rate_for_other_channels(self):
        self._fees()
        o, _a = self.make_shipped_order(channel="전화", amount=100000)
        self.assertEqual(self.client.get(f"/api/orders/{o['id']}").get_json()["feeAmount"], 3000)

    def test_no_settings_uses_default_rates(self):
        """설정 전에도 기본 요율은 붙는다(2026-08-12 대표 "일단 기본 값으로") —
        단 택배비와 모르는 채널은 임의로 깎지 않는다."""
        o, _a = self.make_shipped_order(channel="쿠팡", amount=500000)
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual((d["feeAmount"], d["feeRate"]), (25000, 5.0))   # 기본 5%
        self.assertEqual(d["shippingCost"], 0)        # 택배비는 기본값이 없다
        self.assertEqual(d["netAmount"], 475000)
        o2, _a2 = self.make_shipped_order(channel="사내직거래", amount=100000)
        self.assertEqual(self.client.get(f"/api/orders/{o2['id']}").get_json()["feeAmount"], 0)

    def test_manual_settlement_overrides(self):
        self._fees()
        o, _a = self.make_shipped_order(channel="쿠팡", amount=500000)
        r = self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "settlement", "feeAmount": 51234, "shippingCost": 4500})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(d["feeAmount"], 51234)
        self.assertEqual(d["feeRate"], 0)                # 손으로 넣었으니 요율은 근거가 아니다
        self.assertEqual(d["shippingCost"], 4500)

    def test_partial_refund(self):
        """전액/0 이분법이 아니라 부분환불 금액을 기록한다."""
        o, _a = self.make_shipped_order(amount=500000)
        self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "settlement", "refundAmount": 120000, "refundReason": "액정 흠집 보상"})
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(d["refundAmount"], 120000)
        self.assertEqual(d["refundReason"], "액정 흠집 보상")
        self.assertEqual(d["netAmount"], 380000)
        r = self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "settlement", "refundAmount": 900000})
        self.assertEqual(r.status_code, 400)             # 판매가보다 큰 환불은 막는다

    def test_summary_uses_net_revenue(self):
        self._fees({"쿠팡": 10}, ship=3000)
        o, _a = self.make_shipped_order(channel="쿠팡", amount=500000)   # 매입 200,000
        self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "settlement", "refundAmount": 20000})
        s = self.client.get("/api/reports/summary").get_json()["sales"]
        self.assertEqual(s["revenue"], 500000)
        self.assertEqual(s["fee"], 50000)
        self.assertEqual(s["refund"], 20000)
        self.assertEqual(s["netRevenue"], 430000)
        self.assertEqual(s["shippingCost"], 3000)
        self.assertEqual(s["cost"], 203000)              # 매입 200,000 + 택배 3,000
        self.assertEqual(s["margin"], 227000)            # 430,000 − 203,000

    def test_backfill_dry_run_changes_nothing(self):
        o, _a = self.make_shipped_order(channel="쿠팡", amount=500000)   # 출고 시 기본 5% = 25,000
        self._fees({"쿠팡": 10})                          # 실요율을 나중에 넣은 상황
        r = self.client.post("/api/orders/settlement-backfill", json={"dryRun": True}).get_json()
        self.assertEqual(r["changed"], 1)
        self.assertEqual(r["feeTotal"], 50000)
        self.assertEqual(self.client.get(f"/api/orders/{o['id']}").get_json()["feeAmount"], 25000)
        r = self.client.post("/api/orders/settlement-backfill", json={}).get_json()
        self.assertEqual(r["changed"], 1)
        self.assertEqual(self.client.get(f"/api/orders/{o['id']}").get_json()["feeAmount"], 50000)

    def test_backfill_keeps_manual_values(self):
        o, _a = self.make_shipped_order(channel="쿠팡", amount=500000)
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "settlement", "feeAmount": 7777})
        self._fees({"쿠팡": 10})
        r = self.client.post("/api/orders/settlement-backfill", json={}).get_json()
        self.assertEqual(r["changed"], 0)
        self.assertEqual(self.client.get(f"/api/orders/{o['id']}").get_json()["feeAmount"], 7777)

    def test_backfill_fills_past_orders_with_default_rates(self):
        """저장된 요율이 없어도 소급이 기본 요율로 과거 0원 주문을 채운다
        (옛 '설정 먼저 하라 400' 계약은 기본값 도입으로 폐기)."""
        o, _a = self.make_shipped_order(channel="쿠팡", amount=500000)
        import sqlite3 as _sq
        with _sq.connect(self.db_path) as raw:            # 기본값 도입 전 출고분 재현
            raw.execute("UPDATE orders SET fee_amount=0, fee_rate=0 WHERE id=?", (o["id"],))
        r = self.client.post("/api/orders/settlement-backfill", json={})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["changed"], 1)
        d = self.client.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual((d["feeAmount"], d["feeRate"]), (25000, 5.0))

    def test_ledger_rows_and_totals(self):
        self._fees({"쿠팡": 10}, ship=3000)
        o, a = self.make_shipped_order(channel="쿠팡", amount=500000)
        self.client.post(f"/api/assets/{a['id']}/repairs", json={"description": "청소", "cost": 30000})
        r = self.client.get("/api/reports/ledger").get_json()
        self.assertEqual(r["totals"]["orders"], 1)
        row = r["rows"][0]
        self.assertEqual(row["amount"], 500000)
        self.assertEqual(row["fee"], 50000)
        self.assertEqual(row["netAmount"], 450000)
        self.assertEqual(row["buyCost"], 200000)
        self.assertEqual(row["repairCost"], 30000)
        self.assertEqual(row["cost"], 233000)            # 200,000 + 30,000 + 3,000
        self.assertEqual(row["margin"], 217000)
        self.assertEqual(row["assetNos"], a["assetNo"])

    def test_ledger_export(self):
        self.make_shipped_order(amount=500000)
        r = self.client.get("/api/reports/ledger/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheetml", r.headers["Content-Type"])
        self.assertIn("ows-ledger-", r.headers["Content-Disposition"])

    def test_profitability_by_model(self):
        self._fees({"쿠팡": 10})
        for _ in range(2):
            self.make_shipped_order(channel="쿠팡", amount=500000)
        r = self.client.get("/api/reports/profitability?by=model").get_json()
        row = next(x for x in r["rows"] if "L480" in x["key"])
        self.assertEqual(row["units"], 2)
        self.assertEqual(row["revenue"], 1000000)
        self.assertEqual(row["netRevenue"], 900000)
        self.assertEqual(row["buyCost"], 400000)
        self.assertEqual(row["margin"], 500000)
        self.assertEqual(row["marginPerUnit"], 250000)

    def test_profitability_splits_multi_asset_orders(self):
        """자산 2대가 한 주문에 나가면 매출을 나눠 담아 이중계상하지 않는다."""
        o = self.client.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북 2대", "amount": 1000000}).get_json()
        a1 = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "AAA", "purchasePrice": 200000, "qty": 1}).get_json()[0]
        a2 = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "BBB", "purchasePrice": 300000, "qty": 1}).get_json()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={
            "action": "assets", "assetIds": [a1["id"], a2["id"]]})
        for act in ("production", "softwareInspection", "shipping"):
            self.client.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        r = self.client.get("/api/reports/profitability").get_json()
        total = sum(x["revenue"] for x in r["rows"])
        self.assertEqual(total, 1000000)                 # 2,000,000으로 부풀지 않는다
        aaa = next(x for x in r["rows"] if x["key"] == "AAA")
        self.assertEqual((aaa["revenue"], aaa["buyCost"], aaa["margin"]), (500000, 200000, 300000))

    def test_profitability_bad_key(self):
        self.assertEqual(self.client.get("/api/reports/profitability?by=nope").status_code, 400)

    def test_summary_reports_data_gaps(self):
        """원가가 비면 마진이 부풀려진다 — 얼마나 못 믿을 숫자인지 알려줘야 한다."""
        o, _a = self.make_shipped_order(amount=500000)          # 매입가 200,000 정상
        o2 = self.client.post("/api/orders", json={
            "recipient": "무자산", "productName": "노트북", "amount": 300000}).get_json()
        for act in ("production", "softwareInspection", "shipping"):
            self.client.patch(f"/api/orders/{o2['id']}", json={"action": act, "value": True})
        g = self.client.get("/api/reports/summary").get_json()["dataGaps"]
        self.assertEqual(g["units"], 1)
        self.assertEqual(g["noBuyPrice"], 0)
        self.assertEqual(g["ordersWithoutAsset"], 1)
        # 매입가 0짜리 자산이 나가면 잡힌다
        o3 = self.client.post("/api/orders", json={
            "recipient": "원가없음", "productName": "노트북", "amount": 100000}).get_json()
        a3 = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "X", "purchasePrice": 0, "qty": 1}).get_json()[0]
        self.client.patch(f"/api/orders/{o3['id']}", json={"action": "assets", "assetIds": [a3["id"]]})
        for act in ("production", "softwareInspection", "shipping"):
            self.client.patch(f"/api/orders/{o3['id']}", json={"action": act, "value": True})
        g = self.client.get("/api/reports/summary").get_json()["dataGaps"]
        self.assertEqual(g["noBuyPrice"], 1)

    def test_ledger_requires_reports_perm(self):
        r = self.client.post("/api/users", json={
            "username": "nofin", "displayName": "무권한", "password": "nofin-pw-1234",
            "perms": ["orders.view"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "nofin", "password": "nofin-pw-1234"})
        self.assertEqual(c.get("/api/reports/ledger").status_code, 403)
        self.assertEqual(c.get("/api/reports/profitability").status_code, 403)


class TestStockByModel(Base):
    """제품별 재고 현황 — 지금 몇 대 팔 수 있는지가 한 줄로 나와야 한다."""

    def _asset(self, **kw):
        body = {"categoryId": self.cats[0]["id"], "maker": "삼성", "model": "NT371B5M",
                "purchasePrice": 200000, "salePrice": 470000, "qty": 1,
                "cpu": "i7-7500U", "ram": "8G", "ssd": "256G"}
        body.update(kw)
        return self.client.post("/api/assets", json=body).get_json()[0]

    def _set_status(self, asset, status):
        r = self.client.patch(f"/api/assets/{asset['id']}", json={"status": status})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_buckets_and_totals(self):
        a1, a2, a3 = self._asset(), self._asset(), self._asset()
        self._set_status(a1, "ready")
        self._set_status(a2, "refurbishing")
        self._set_status(a3, "repair")
        r = self.client.get("/api/assets/stock-by-model")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        p = next(x for x in body["products"] if x["product"] == "삼성 NT371B5M")
        self.assertEqual(p["ready"], 1)
        self.assertEqual(p["working"], 2)          # 정비중 + 수리
        self.assertEqual(p["stock"], 3)
        self.assertEqual(p["avgBuy"], 200000)
        self.assertEqual(p["avgSale"], 470000)
        t = body["totals"]
        self.assertEqual((t["ready"], t["working"], t["stock"]), (1, 2, 3))
        self.assertEqual(t["stockValue"], 600000)  # 실제 매입가 합

    def test_stock_value_is_exact_sum(self):
        """평균×수량이 아니라 실제 매입가 합이어야 반올림이 쌓이지 않는다."""
        for price in (100001, 100001, 100001):
            self._set_status(self._asset(purchasePrice=price), "ready")
        t = self.client.get("/api/assets/stock-by-model").get_json()["totals"]
        self.assertEqual(t["stockValue"], 300003)

    def test_low_stock_flag(self):
        """팔린 적 있는데 판매가능이 2대 이하면 '부족'으로 표시한다."""
        o, a = self.make_shipped_order()                    # 모델 L480 1대 출고
        left = self._asset(maker="", model="L480", purchasePrice=200000)
        self._set_status(left, "ready")
        p = next(x for x in self.client.get("/api/assets/stock-by-model").get_json()["products"]
                 if x["product"] == "L480")
        self.assertTrue(p["lowStock"])
        self.assertEqual((p["ready"], p["gone"]), (1, 1))
        # 넉넉히 채우면 해제된다
        for _ in range(3):
            self._set_status(self._asset(maker="", model="L480"), "ready")
        p = next(x for x in self.client.get("/api/assets/stock-by-model").get_json()["products"]
                 if x["product"] == "L480")
        self.assertFalse(p["lowStock"])

    def test_shipped_leaves_stock(self):
        """출고된 자산은 재고에서 빠지고 '누적 출고'로만 남는다."""
        o, a = self.make_shipped_order()
        body = self.client.get("/api/assets/stock-by-model").get_json()
        p = next(x for x in body["products"] if "L480" in x["product"])
        self.assertEqual(p["stock"], 0)
        self.assertEqual(p["gone"], 1)
        self.assertEqual(body["totals"]["stock"], 0)
        self.assertEqual(body["totals"]["soldOut"], 1)   # 품절 제품으로 잡힌다

    def test_variants_split_by_spec(self):
        """같은 모델이어도 스펙이 다르면 하위 줄로 나뉜다."""
        self._set_status(self._asset(ssd="256G"), "ready")
        self._set_status(self._asset(ssd="512G"), "ready")
        self._set_status(self._asset(ssd="512G"), "refurbishing")
        p = next(x for x in self.client.get("/api/assets/stock-by-model").get_json()["products"]
                 if x["product"] == "삼성 NT371B5M")
        self.assertEqual(p["stock"], 3)
        specs = {v["spec"]: v for v in p["variants"]}
        self.assertEqual(specs["i7-7500U / 8G / 512G"]["stock"], 2)
        self.assertEqual(specs["i7-7500U / 8G / 512G"]["ready"], 1)
        self.assertEqual(specs["i7-7500U / 8G / 256G"]["ready"], 1)

    def test_sorted_by_sellable(self):
        self._set_status(self._asset(model="A-few"), "ready")
        for _ in range(3):
            self._set_status(self._asset(model="B-many"), "ready")
        names = [p["product"] for p in
                 self.client.get("/api/assets/stock-by-model").get_json()["products"]]
        self.assertLess(names.index("삼성 B-many"), names.index("삼성 A-few"))

    def test_missing_model_grouped(self):
        self._set_status(self._asset(maker="", model=""), "ready")
        p = next(x for x in self.client.get("/api/assets/stock-by-model").get_json()["products"]
                 if x["product"] == "(모델 미입력)")
        self.assertEqual(p["ready"], 1)
        self.assertEqual(p["variants"][0]["spec"], "i7-7500U / 8G / 256G")

    def test_requires_permission(self):
        r = self.client.post("/api/users", json={
            "username": "nostock", "displayName": "무권한", "password": "nostock-pw-1234",
            "perms": ["orders.view"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "nostock", "password": "nostock-pw-1234"})
        self.assertEqual(c.get("/api/assets/stock-by-model").status_code, 403)


class TestGodomallAdapter(unittest.TestCase):
    """고도몰 XML 파싱 — 네트워크는 타지 않고 _post만 갈아끼운다."""

    def _adapter(self, pages):
        from app.malls.godomall import GodomallAdapter
        import xml.etree.ElementTree as ET
        a = GodomallAdapter({"partner_key": "p", "user_key": "u"})
        a.call_interval = 0
        self.calls = []
        seq = list(pages)

        def fake_post(url, params, timeout=20):
            self.calls.append((url, params))
            root = ET.fromstring(seq.pop(0) if seq else pages[-1])
            a._check_error(root)
            return root
        a._post = fake_post
        return a

    def test_amount_uses_order_total_not_line_sum(self):
        """노트북 2대·유상 옵션이 붙은 주문이 1대 값으로 저장되면 안 된다."""
        xml = """<data><header><code>000</code><lastOrder>false</lastOrder></header><return>
          <order_data><orderNo>A1</orderNo><orderStatus>p1</orderStatus>
            <orderDate>2026.07.20 14:10</orderDate>
            <totalGoodsPrice>1250000</totalGoodsPrice><settlePrice>1253000</settlePrice>
            <orderInfoData><receiverName>홍길동</receiverName></orderInfoData>
            <orderGoodsData><goodsNm>노트북</goodsNm><goodsCd>NB1</goodsCd>
              <goodsCnt>2</goodsCnt><goodsPrice>500000</goodsPrice></orderGoodsData>
            <addGoodsData><goodsNm>SSD 512G로 UP</goodsNm><goodsCnt>1</goodsCnt>
              <goodsPrice>250000</goodsPrice></addGoodsData>
          </order_data></return></data>"""
        a = self._adapter([xml])
        o = a.collect_orders(datetime(2026, 7, 20), datetime(2026, 7, 21))[0]
        self.assertEqual(o["amount"], 1250000)     # 500,000(1대분)으로 줄지 않는다
        self.assertEqual(o["quantity"], 2)
        self.assertIn("SSD 512G로 UP", o["optionName"])
        self.assertEqual(o["orderedAt"][:10], "2026-07-20")   # 날짜 표기 통일

    def test_amount_falls_back_to_line_sum_with_quantity(self):
        """총액 필드를 안 주는 경우(공급사 키)에도 수량을 곱해야 한다."""
        xml = """<data><header><code>000</code><lastOrder>false</lastOrder></header><return>
          <order_data><orderNo>A2</orderNo><orderStatus>p1</orderStatus>
            <orderInfoData><receiverName>홍</receiverName></orderInfoData>
            <orderGoodsData><goodsNm>노트북</goodsNm><goodsCnt>3</goodsCnt>
              <goodsPrice>400000</goodsPrice></orderGoodsData>
          </order_data></return></data>"""
        a = self._adapter([xml])
        o = a.collect_orders(datetime(2026, 7, 20), datetime(2026, 7, 21))[0]
        self.assertEqual(o["amount"], 1200000)
        self.assertEqual(o["quantity"], 3)

    def test_cancelled_lines_are_dropped(self):
        """고객이 취소한 상품줄을 꺼내 포장하면 안 된다."""
        xml = """<data><header><code>000</code><lastOrder>false</lastOrder></header><return>
          <order_data><orderNo>A3</orderNo><orderStatus>p1</orderStatus>
            <orderInfoData><receiverName>홍</receiverName></orderInfoData>
            <orderGoodsData><goodsNm>살아있는상품</goodsNm><orderStatus>g1</orderStatus>
              <goodsCnt>1</goodsCnt><goodsPrice>500000</goodsPrice></orderGoodsData>
            <orderGoodsData><goodsNm>취소된상품</goodsNm><orderStatus>c1</orderStatus>
              <goodsCnt>1</goodsCnt><goodsPrice>300000</goodsPrice></orderGoodsData>
          </order_data></return></data>"""
        a = self._adapter([xml])
        o = a.collect_orders(datetime(2026, 7, 20), datetime(2026, 7, 21))[0]
        self.assertEqual(o["productName"], "살아있는상품")
        self.assertNotIn("취소된상품", o["optionName"])
        self.assertEqual(o["amount"], 500000)

    def test_fully_cancelled_order_skipped(self):
        xml = """<data><header><code>000</code><lastOrder>false</lastOrder></header><return>
          <order_data><orderNo>A4</orderNo><orderStatus>p1</orderStatus>
            <orderInfoData><receiverName>홍</receiverName></orderInfoData>
            <orderGoodsData><goodsNm>취소</goodsNm><orderStatus>c2</orderStatus>
              <goodsCnt>1</goodsCnt><goodsPrice>500000</goodsPrice></orderGoodsData>
          </order_data></return></data>"""
        a = self._adapter([xml])
        self.assertEqual(a.collect_orders(datetime(2026, 7, 20), datetime(2026, 7, 21)), [])

    def test_option_json_becomes_readable(self):
        """옵션이 개발자용 JSON 원문으로 화면에 뜨면 안 된다."""
        xml = """<data><header><code>000</code><lastOrder>false</lastOrder></header><return>
          <order_data><orderNo>A5</orderNo><orderStatus>p1</orderStatus>
            <orderInfoData><receiverName>홍</receiverName></orderInfoData>
            <orderGoodsData><goodsNm>노트북</goodsNm><goodsCnt>1</goodsCnt><goodsPrice>500000</goodsPrice>
              <optionInfo>[{"optionName":"램","optionValue":"16GB"},{"optionName":"용량","optionValue":"512G"}]</optionInfo>
              <optionTextInfo>[{"optionName":"각인","optionValue":"홍길동"}]</optionTextInfo>
            </orderGoodsData>
          </order_data></return></data>"""
        a = self._adapter([xml])
        o = a.collect_orders(datetime(2026, 7, 20), datetime(2026, 7, 21))[0]
        self.assertIn("램: 16GB", o["optionName"])
        self.assertIn("용량: 512G", o["optionName"])
        self.assertIn("각인: 홍길동", o["optionName"])    # 고객이 적은 옵션도 살아 있어야 한다
        self.assertNotIn("optionName", o["optionName"])   # 코드 원문이 그대로 새면 안 된다

    def test_pagination_continues_without_flag(self):
        """다음페이지 플래그가 없어도 한 장을 꽉 채웠으면 계속 가져온다(100건 절단 방지)."""
        import app.malls.godomall as gd
        self.addCleanup(setattr, gd, "PAGE_SIZE", gd.PAGE_SIZE)
        gd.PAGE_SIZE = 1
        full = """<data><header><code>000</code></header><return>
          <order_data><orderNo>{no}</orderNo><orderStatus>p1</orderStatus>
            <orderInfoData><receiverName>홍</receiverName></orderInfoData>
            <orderGoodsData><goodsNm>N</goodsNm><goodsCnt>1</goodsCnt><goodsPrice>1000</goodsPrice></orderGoodsData>
          </order_data></return></data>"""
        empty = """<data><header><code>000</code></header><return></return></data>"""
        a = self._adapter([full.format(no="B2"), full.format(no="B1"), empty])
        got = a.collect_orders(datetime(2026, 7, 20), datetime(2026, 7, 21))
        self.assertEqual([o["orderNumber"] for o in got], ["B2", "B1"])
        self.assertEqual(len(self.calls), 3)

    def test_parses_order_data_node(self):
        """★스펙의 반복 노드는 order_data(스네이크). orderData로 찾으면 0건이 된다."""
        a = self._adapter([GODO_ORDER_XML.format(no="2507201000001", more="false")])
        got = a.collect_orders(datetime(2026, 7, 20), datetime(2026, 7, 21))
        self.assertEqual(len(got), 1)
        o = got[0]
        self.assertEqual(o["orderNumber"], "2507201000001")
        self.assertEqual(o["productName"], "삼성 노트북")
        self.assertEqual(o["productCode"], "NT371B5L_i7")
        self.assertEqual(o["amount"], 453000)
        self.assertEqual(o["recipient"], "김수취")
        self.assertEqual(o["postalCode"], "06000")
        self.assertEqual(o["address"], "서울시 강남구 1층")
        self.assertEqual(o["optionName"], "8G/256G")
        self.assertEqual(o["deliveryMessage"], "부재시 경비실")

    def test_detects_error_in_header(self):
        """오류코드는 header 아래에 온다 — 못 잡으면 실패를 '0건'으로 오인한다."""
        from app.malls.base import MallError
        bad = """<data><header><code>996</code><msg>허용되지 않은 IP</msg></header></data>"""
        a = self._adapter([bad])
        with self.assertRaises(MallError) as cm:
            a.collect_orders(datetime(2026, 7, 20), datetime(2026, 7, 21))
        self.assertIn("996", str(cm.exception))
        self.assertIn("허용되지 않은 IP", str(cm.exception))

    def test_pagination_uses_page_numbers_like_rms(self):
        """★페이징은 page 번호 증가 방식이다 — RMS 실운영에서 검증된 방식.

        예전 구현은 lastOrder를 '요청 커서'로 보냈는데 그건 스펙에 없다.
        lastOrder는 응답의 '이게 마지막 장이냐' 표시일 뿐이다(true=마지막).
        예전엔 그 의미도 반대로 읽어서, 실제 몰을 만나면 두 번째 장부터 전부 놓칠 뻔했다.
        """
        import app.malls.godomall as gd
        self.addCleanup(setattr, gd, "PAGE_SIZE", gd.PAGE_SIZE)
        gd.PAGE_SIZE = 1                       # 1건짜리 페이지로 동작 확인
        a = self._adapter([
            GODO_ORDER_XML.format(no="2507201000002", more="false"),   # 마지막 아님 → 계속
            GODO_ORDER_XML.format(no="2507201000001", more="true"),    # 마지막 → 멈춤
        ])
        got = a.collect_orders(datetime(2026, 7, 20), datetime(2026, 7, 21))
        self.assertEqual([o["orderNumber"] for o in got], ["2507201000002", "2507201000001"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][1]["page"], "1")
        self.assertEqual(self.calls[1][1]["page"], "2")
        self.assertNotIn("lastOrder", self.calls[0][1], "lastOrder는 요청 파라미터가 아니다")

    def test_query_params_match_rms_production(self):
        """dateType=order + 날짜는 시각 없이 — RMS가 그렇게 운영 중이다."""
        a = self._adapter([GODO_ORDER_XML.format(no="2507201000001", more="true")])
        a.collect_orders(datetime(2026, 7, 20), datetime(2026, 7, 21))
        p = self.calls[0][1]
        self.assertEqual(p["dateType"], "order")
        self.assertEqual(p["startDate"], "2026-07-20")
        self.assertEqual(p["endDate"], "2026-07-21")
        self.assertEqual(p["size"], "300")

    def test_pagination_stops_on_duplicate_page(self):
        """page를 무시하고 같은 목록만 주는 서버에서 무한루프에 빠지면 안 된다."""
        import app.malls.godomall as gd
        self.addCleanup(setattr, gd, "PAGE_SIZE", gd.PAGE_SIZE)
        gd.PAGE_SIZE = 1
        same = GODO_ORDER_XML.format(no="2507201000009", more="false")
        a = self._adapter([same, same, same])
        got = a.collect_orders(datetime(2026, 7, 20), datetime(2026, 7, 21))
        self.assertEqual(len(got), 1, "같은 주문이 페이지마다 중복 수집됐다")
        self.assertEqual(len(self.calls), 2, "중복 페이지를 만났으면 바로 멈춰야 한다")

    def test_pagination_stops_without_more(self):
        a = self._adapter([GODO_ORDER_XML.format(no="2507201000001", more="false")])
        a.collect_orders(datetime(2026, 7, 20), datetime(2026, 7, 21))
        self.assertEqual(len(self.calls), 1)

    def test_search_goods_parses_fields(self):
        a = self._adapter([GODO_GOODS_XML.format(cd="NT371B5M_i7", nm="삼성 노트북 중고")])
        g = a.search_goods("NT371", field="code")[0]
        self.assertEqual(g["goodsCd"], "NT371B5M_i7")
        self.assertEqual(g["goodsNm"], "삼성 노트북 중고")
        self.assertEqual(g["shortDescription"], "i7-7500U / 8G / SSD 256G / 15.6인치")
        self.assertEqual(g["price"], 470000)
        self.assertEqual(g["fixedPrice"], 590000)
        self.assertEqual(g["stateLabel"], "중고상품")
        self.assertEqual(g["stock"], 3)
        self.assertFalse(g["soldOut"])
        self.assertTrue(self.calls[0][0].endswith("/godomall5/goods/Goods_Search.php"))
        self.assertEqual(self.calls[0][1]["goodsCd"], "NT371")

    def test_search_goods_auto_merges_and_dedupes(self):
        """auto는 코드·상품명 양쪽으로 찾되 같은 상품을 두 번 담지 않는다."""
        a = self._adapter([GODO_GOODS_XML.format(cd="A1", nm="노트북")] * 2)
        got = a.search_goods("노트북")
        self.assertEqual(len(got), 1)
        self.assertEqual([c[1].get("goodsCd") for c in self.calls], ["노트북", None])
        self.assertEqual(self.calls[1][1]["goodsNm"], "노트북")

    def test_search_goods_empty_keyword(self):
        a = self._adapter([GODO_GOODS_XML.format(cd="A1", nm="노트북")])
        self.assertEqual(a.search_goods("  "), [])
        self.assertEqual(self.calls, [])       # 빈 검색어로 몰을 부르지 않는다

    def test_unsupported_mall_raises(self):
        from app.malls.base import MallError
        from app.malls.coupang import CoupangAdapter
        with self.assertRaises(MallError):
            CoupangAdapter({}).search_goods("아무거나")

    # ---------------- 택배사 코드(RMS 이식 2026-09-08 — "RMS쪽에는 이미 고도몰 연동되어있는데 확인 후 적용")
    COURIERS_XML = """<data><header><code>000</code></header><return>
      <data><invoiceCompanySno>4</invoiceCompanySno><invoiceCompanyName>우체국택배</invoiceCompanyName></data>
      <data><invoiceCompanySno>17</invoiceCompanySno><invoiceCompanyName>CJ대한통운</invoiceCompanyName></data>
      <data><invoiceCompanySno>5</invoiceCompanySno><invoiceCompanyName>한진택배</invoiceCompanyName></data>
    </return></data>"""

    def test_택배사_목록을_읽고_CJ_번호를_찾는다(self):
        a = self._adapter([self.COURIERS_XML])
        got = a.list_couriers()
        self.assertEqual([c["sno"] for c in got], ["4", "17", "5"])
        self.assertEqual(a.cj_courier_sno(), "17")
        self.assertIn("code_type", self.calls[0][1])
        self.assertEqual(self.calls[0][1]["code_type"], "deliveryCompany")
        self.assertTrue(self.calls[0][0].endswith("/godomall5/common/Code_Search.php"))
        # cj_courier_sno 는 자기 조회 결과를 캐시한다 — 위 list_couriers(1) + 첫 조회(2) 뒤로는 안 늘어난다
        self.assertEqual(len(self.calls), 2)
        a.cj_courier_sno()
        self.assertEqual(len(self.calls), 2, "캐시가 안 되어 전송 때마다 몰을 또 부른다")

    def test_송장_전송은_택배사_번호를_반드시_같이_보낸다(self):
        """★예전에는 택배사 없이 송장번호만 올렸다 — 그러면 고객 화면에 배송조회가 안 붙는다(RMS 대조)."""
        ok_xml = "<data><header><code>000</code><msg>ok</msg></header></data>"
        a = self._adapter([self.COURIERS_XML, ok_xml])
        a.upload_invoice("A1", "612345678901", sno="11|12")
        url, params = self.calls[-1]
        self.assertTrue(url.endswith("/godomall5/order/Order_Status.php"))
        self.assertEqual(params["orderStatus"], "d1")
        self.assertEqual(params["invoiceNo"], "612345678901")
        self.assertEqual(params["invoiceCompanySno"], "17", "택배사 번호를 자동으로 찾아 넣어야 한다")
        self.assertEqual(params["sno"], "11|12", "주문상품 번호(sno)를 같이 보내야 한다 — 없으면 898 거부")

    def test_설정에_적힌_택배사_번호가_있으면_몰에_묻지_않는다(self):
        ok_xml = "<data><header><code>000</code><msg>ok</msg></header></data>"
        a = self._adapter([ok_xml])
        a.upload_invoice("A1", "612345678901", courier_code="17", sno="1")
        self.assertEqual(len(self.calls), 1, "설정값이 있는데 택배사 목록을 또 물었다")
        self.assertEqual(self.calls[0][1]["invoiceCompanySno"], "17")

    def test_CJ가_등록돼_있지_않으면_보내지_않고_이유를_말한다(self):
        """엉뚱한 택배사로 올리는 것보다 안 올리는 게 낫다 — 등록된 이름을 알려 사람이 고치게."""
        from app.malls.base import MallError
        xml = """<data><header><code>000</code></header><return>
          <data><invoiceCompanySno>4</invoiceCompanySno><invoiceCompanyName>우체국택배</invoiceCompanyName></data>
        </return></data>"""
        a = self._adapter([xml])
        with self.assertRaises(MallError) as e:
            a.upload_invoice("A1", "612345678901", sno="1")
        self.assertIn("CJ대한통운", str(e.exception))
        self.assertIn("우체국택배", str(e.exception))
        self.assertEqual(len(self.calls), 1, "택배사가 없는데 상태변경까지 나갔다")

    # ---------------- 2026-09-08 첫 실전송: 고도몰 898 "sno 값은 필수 값입니다" (5건 거부)
    def test_수집이_주문상품_번호_sno_를_보관한다(self):
        """살아 있는 상품줄의 sno 를 '|'로 이어 raw 에 남긴다(RMS 와 같은 모양). 취소된 줄은 뺀다."""
        xml = """<data><header><code>000</code><lastOrder>true</lastOrder></header><return>
          <order_data><orderNo>S1</orderNo><orderStatus>p1</orderStatus>
            <orderInfoData><receiverName>홍</receiverName></orderInfoData>
            <orderGoodsData><sno>11</sno><goodsNm>노트북</goodsNm><orderStatus>p1</orderStatus>
              <goodsCnt>1</goodsCnt><goodsPrice>500000</goodsPrice></orderGoodsData>
            <orderGoodsData><sno>12</sno><goodsNm>가방</goodsNm><orderStatus>p1</orderStatus>
              <goodsCnt>1</goodsCnt><goodsPrice>30000</goodsPrice></orderGoodsData>
            <orderGoodsData><sno>13</sno><goodsNm>취소된것</goodsNm><orderStatus>c1</orderStatus>
              <goodsCnt>1</goodsCnt><goodsPrice>10000</goodsPrice></orderGoodsData>
          </order_data></return></data>"""
        a = self._adapter([xml])
        o = a.collect_orders(datetime(2026, 9, 8), datetime(2026, 9, 9))[0]
        self.assertEqual(o["mallSno"], "11|12")

    def test_fetch_sno_는_주문일_앞뒤를_뒤져_그_주문의_sno_를_찾는다(self):
        """옛 수집분(raw 에 sno 없음)은 전송 직전에 몰에 다시 묻는다 — 날짜 조회로만(RMS 검증 방식)."""
        xml = """<data><header><code>000</code><lastOrder>true</lastOrder></header><return>
          <order_data><orderNo>A9</orderNo><orderStatus>p1</orderStatus>
            <orderGoodsData><sno>5</sno><goodsNm>다른주문</goodsNm><goodsCnt>1</goodsCnt><goodsPrice>1</goodsPrice></orderGoodsData>
          </order_data>
          <order_data><orderNo>B1</orderNo><orderStatus>p1</orderStatus>
            <orderGoodsData><sno>7</sno><goodsNm>노트북</goodsNm><goodsCnt>1</goodsCnt><goodsPrice>1</goodsPrice></orderGoodsData>
            <orderGoodsData><sno>8</sno><goodsNm>취소</goodsNm><orderStatus>c1</orderStatus><goodsCnt>1</goodsCnt><goodsPrice>1</goodsPrice></orderGoodsData>
          </order_data></return></data>"""
        a = self._adapter([xml])
        self.assertEqual(a.fetch_sno("B1", "2026-09-08 10:00"), "7")
        url, params = self.calls[0]
        self.assertTrue(url.endswith("/godomall5/order/Order_Search.php"))
        self.assertEqual((params["startDate"], params["endDate"]), ("2026-09-07", "2026-09-09"))
        self.assertEqual(params["dateType"], "order")
        self.assertEqual(a.fetch_sno("없는주문", "2026-09-08"), "")

    def test_송장_전송은_sno_없이는_몰을_부르지_않는다(self):
        """898 은 확정적이다 — 없이 두드려 봐야 거부만 쌓인다. 사유를 남기고 멈춘다."""
        from app.malls.base import MallError
        a = self._adapter(["<data><header><code>000</code></header></data>"])
        with self.assertRaises(MallError) as e:
            a.upload_invoice("A1", "612345678901", courier_code="17")
        self.assertIn("sno", str(e.exception))
        self.assertEqual(self.calls, [], "sno 없이 상태변경이 나갔다")

class TestGoodsSearchRoute(Base):
    def test_requires_setup_and_permission(self):
        # 키가 없으면 400으로 사유를 알려준다
        r = self.client.get("/api/malls/godomall/goods?q=NT371")
        self.assertEqual(r.status_code, 400)
        self.assertIn("사용", r.get_json()["error"])

    def test_short_keyword_skips_call(self):
        r = self.client.get("/api/malls/godomall/goods?q=N")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["goods"], [])

    def test_bad_field_rejected(self):
        r = self.client.get("/api/malls/godomall/goods?q=NT371&field=sql")
        self.assertEqual(r.status_code, 400)

    def test_returns_live_goods(self):
        import xml.etree.ElementTree as ET

        from app.malls.godomall import GodomallAdapter
        self.client.put("/api/settings", json={"malls": {
            "godomall": {"partner_key": "P", "user_key": "U", "enabled": True}}})
        orig = GodomallAdapter._post
        self.addCleanup(setattr, GodomallAdapter, "_post", orig)
        GodomallAdapter._post = lambda self, url, params, timeout=20: ET.fromstring(
            GODO_GOODS_XML.format(cd="NT371B5M_i7", nm="삼성 노트북 중고"))
        GodomallAdapter.call_interval = 0
        r = self.client.get("/api/malls/godomall/goods?q=NT371&field=code")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["goods"][0]["goodsCd"], "NT371B5M_i7")
        self.assertEqual(body["goods"][0]["price"], 470000)

    def test_worker_without_edit_perm_blocked(self):
        r = self.client.post("/api/users", json={
            "username": "wk1", "displayName": "작업자", "password": "worker-pass-1234",
            "perms": ["setup.view", "orders.work"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        r = c.post("/api/auth/login", json={"username": "wk1", "password": "worker-pass-1234"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(c.get("/api/malls/godomall/goods?q=NT371").status_code, 403)


class TestMallStatusView(Base):
    """주문관리의 쇼핑몰 관점 상태(입금대기/준비중/배송중/배송완료)와 몰별 조회."""

    def test_status_derivation(self):
        o1 = self.client.post("/api/orders", json={
            "recipient": "결제전", "productName": "노트북", "amount": 100000}).get_json()
        self.client.patch(f"/api/orders/{o1['id']}", json={"action": "payStatus", "value": "unpaid"})
        o2 = self.client.post("/api/orders", json={
            "recipient": "준비중", "productName": "노트북", "amount": 200000}).get_json()
        o3, _a = self.make_shipped_order(recipient="배송중", amount=300000)
        o4 = self.client.post("/api/orders", json={
            "recipient": "배송완료", "productName": "노트북", "amount": 400000}).get_json()
        self.client.patch(f"/api/orders/{o4['id']}", json={"action": "delivered", "value": True})
        o5 = self.client.post("/api/orders", json={
            "recipient": "취소분", "productName": "노트북", "amount": 500000}).get_json()
        self.client.patch(f"/api/orders/{o5['id']}", json={"action": "cancel", "reason": "변심"})

        got = {o["recipient"]: o["mallStatus"]
               for o in self.client.get("/api/orders?view=all").get_json()["orders"]}
        self.assertEqual(got["결제전"], "unpaid")
        self.assertEqual(got["준비중"], "preparing")
        self.assertEqual(got["배송중"], "shipping")     # 출고 확인됨
        self.assertEqual(got["배송완료"], "delivered")
        self.assertEqual(got["취소분"], "cancelled")

    def test_summary_by_mall_and_status(self):
        for ch, amt in (("쿠팡", 100000), ("쿠팡", 200000), ("고도몰", 300000)):
            self.client.post("/api/orders", json={
                "channel": ch, "recipient": "고객", "productName": "노트북", "amount": amt})
        s = self.client.get("/api/orders/summary").get_json()
        prep = next(x for x in s["statuses"] if x["code"] == "preparing")
        self.assertEqual(prep["count"], 3)
        self.assertEqual(prep["amount"], 600000)
        coupang = next(m for m in s["malls"] if m["channel"] == "쿠팡")
        self.assertEqual(coupang["total"], 2)
        self.assertEqual(coupang["preparing"], 2)
        self.assertEqual(coupang["amount"], 300000)
        # 몰 목록은 건수 많은 순
        self.assertEqual(s["malls"][0]["channel"], "쿠팡")

    def test_status_filter(self):
        o = self.client.post("/api/orders", json={
            "recipient": "입금전", "productName": "노트북"}).get_json()
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "payStatus", "value": "unpaid"})
        self.client.post("/api/orders", json={"recipient": "결제완료", "productName": "노트북"})
        r = self.client.get("/api/orders?view=all&mallStatus=unpaid").get_json()
        self.assertEqual(r["shown"], 1)
        self.assertEqual(r["orders"][0]["recipient"], "입금전")
        r = self.client.get("/api/orders?view=all&mallStatus=preparing").get_json()
        self.assertEqual(r["shown"], 1)
        self.assertEqual(self.client.get("/api/orders?view=all&mallStatus=nosuch").status_code, 400)

    def test_channel_and_status_combined(self):
        self.client.post("/api/orders", json={"channel": "쿠팡", "recipient": "A", "productName": "N"})
        self.client.post("/api/orders", json={"channel": "고도몰", "recipient": "B", "productName": "N"})
        r = self.client.get("/api/orders?view=all&channel=쿠팡&mallStatus=preparing").get_json()
        self.assertEqual(r["shown"], 1)
        self.assertEqual(r["orders"][0]["recipient"], "A")

    def test_limit_and_total(self):
        for i in range(12):
            self.client.post("/api/orders", json={"recipient": f"고객{i}", "productName": "노트북"})
        r = self.client.get("/api/orders?view=all&limit=5").get_json()
        self.assertEqual(r["shown"], 5)
        self.assertEqual(r["total"], 12)
        r = self.client.get("/api/orders?view=all&limit=1000").get_json()
        self.assertEqual(r["shown"], 12)


class TestReviewFixes456(Base):
    """2026-07-28 Phase 3~6 리뷰에서 확인된 결함들의 회귀 테스트."""

    def _user(self, username, perms):
        r = self.client.post("/api/users", json={
            "username": username, "displayName": username, "password": "user-pass-1234",
            "perms": perms, "allCategories": True})
        assert r.status_code == 201, r.get_data(as_text=True)
        c = self.app.test_client()
        assert c.post("/api/auth/login", json={
            "username": username, "password": "user-pass-1234"}).status_code == 200
        return r.get_json(), c

    def test_no_self_privilege_escalation(self):
        """users.manage만 가진 사용자가 스스로에게 전 권한을 줄 수 없어야 한다."""
        u, c = self._user("mgr", ["settings.view", "users.manage"])
        # 본인 권한 편집 자체가 금지
        r = c.put(f"/api/users/{u['id']}/perms",
                  json={"perms": ["users.manage", "settings.manage", "audit.view"]})
        self.assertEqual(r.status_code, 403)
        self.assertIn("본인", r.get_json()["error"])
        # 남에게도 자기가 없는 권한은 못 준다
        victim, _ = self._user("victim", ["orders.view"])
        r = c.put(f"/api/users/{victim['id']}/perms", json={"perms": ["settings.manage"]})
        self.assertEqual(r.status_code, 403)
        self.assertIn("가지지 않은", r.get_json()["error"])
        # 신규 생성으로도 우회 불가
        r = c.post("/api/users", json={"username": "puppet", "password": "puppet-pass-1",
                                       "perms": ["settings.manage", "audit.view"]})
        self.assertEqual(r.status_code, 403)
        # 자기가 가진 권한 범위 안에서는 정상 부여
        r = c.put(f"/api/users/{victim['id']}/perms", json={"perms": ["settings.view"]})
        self.assertEqual(r.status_code, 200)
        # 여전히 설정 API는 막혀 있다
        self.assertEqual(c.get("/api/settings").status_code, 403)

    def test_password_reset_cannot_hijack_stronger_account(self):
        """users.manage로 '나보다 권한 많은 계정'의 비밀번호를 바꿔 탈취할 수 없어야 한다."""
        _hr, c = self._user("hrmgr", ["settings.view", "users.manage"])
        strong, _ = self._user("apimgr", ["settings.view", "settings.manage"])
        r = c.patch(f"/api/users/{strong['id']}", json={"password": "hijacked-pw-1"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("권한이 많은", r.get_json()["error"])
        # 원래 비밀번호는 그대로 살아 있다
        c2 = self.app.test_client()
        self.assertEqual(c2.post("/api/auth/login", json={
            "username": "apimgr", "password": "user-pass-1234"}).status_code, 200)
        # 권한이 같거나 적은 계정은 정상적으로 재설정된다
        weak, _ = self._user("staff", ["settings.view"])
        self.assertEqual(c.patch(f"/api/users/{weak['id']}",
                                 json={"password": "reset-pw-1234"}).status_code, 200)

    def test_password_reset_self_requires_current_password(self):
        """본인 비밀번호는 현재 비밀번호 확인 경로로만 바꾼다(세션 탈취 대비)."""
        me, c = self._user("selfmgr", ["users.manage"])
        r = c.patch(f"/api/users/{me['id']}", json={"password": "new-pw-12345"})
        self.assertEqual(r.status_code, 403)

    def test_qc_import_cannot_create_admin(self):
        """이관 파일로는 관리자 계정이 만들어지지 않는다(임의 해시 주입 차단).

        (권한 범위 검사는 TestCategoryScope에서 따로 본다 — 여기서는 관리자가 이관해도
         구 owner 역할이 '관리자'로 승격되지 않는지를 확인한다.)
        """
        import io
        import json as _json
        c = self.client                              # 관리자가 직접 이관
        payload = [{"username": "svc_backup", "displayName": "백업", "role": "owner",
                    "enabled": True,
                    "passwordHash": auth_mod.hash_password("attacker-pw-1")}]
        r = c.post("/api/migrate/qc", data={
            "files": (io.BytesIO(_json.dumps(payload).encode()), "users.json")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["users"]["created"], 1)
        row = self.client.get("/api/users").get_json()
        made = next(u for u in row if u["username"] == "svc_backup")
        self.assertFalse(made["isAdmin"])            # ★관리자로 승격되지 않는다
        # 로그인은 되지만 관리자 전용 동작(관리자 지정)은 막힌다
        c2 = self.app.test_client()
        self.assertEqual(c2.post("/api/auth/login", json={
            "username": "svc_backup", "password": "attacker-pw-1"}).status_code, 200)
        self.assertEqual(c2.patch(f"/api/users/{made['id']}",
                                  json={"isAdmin": True}).status_code, 403)

    def test_qc_import_rejects_bogus_hash(self):
        import io
        import json as _json
        payload = [{"username": "svc2", "role": "worker", "passwordHash": "notahash"}]
        r = self.client.post("/api/migrate/qc", data={
            "files": (io.BytesIO(_json.dumps(payload).encode()), "users.json")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn("비밀번호 형식", r.get_json()["error"])
        self.assertNotIn("svc2", [u["username"] for u in self.client.get("/api/users").get_json()])

    def test_qc_import_requires_user_management_perm(self):
        """설정 권한만으로는 계정을 만드는 이관을 못 돌린다."""
        _u, c = self._user("setonly", ["settings.view", "settings.manage"])
        import io
        r = c.post("/api/migrate/qc", data={
            "files": (io.BytesIO(b"[]"), "users.json")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 403)

    def test_mask_pasted_in_front_of_key_is_stripped(self):
        """★가려진 값 뒤에 키를 붙여넣어도 정상 저장돼야 한다.

        2026-07-29 실제 사고: 화면이 ●●● 를 입력칸 '값'으로 넣어 둬서, 대표가 그 뒤에
        쿠팡 키를 붙여넣자 '●●●실제키'가 저장됐다. 그 값이 인증 헤더에 실려
        요청이 나가기도 전에 터졌다(latin-1 codec can't encode characters in position 37-39).
        화면에서도 막았지만 저장 단계에서 한 번 더 걷어낸다.
        """
        for prefix in (MASK, "•••SECRET•••", "●●●●●●"):   # 현재·예전 표기 + 화면 점 표시
            r = self.client.put("/api/settings", json={"malls": {
                "coupang": {"access_key": prefix + "REALKEY123", "secret_key": "SK"}}})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            from app.settings import _mask_value  # noqa: F401  (마스킹 전 원값을 직접 확인)
            conn = sqlite3.connect(self.db_path)
            raw = json.loads(conn.execute(
                "SELECT value FROM settings WHERE key='malls'").fetchone()[0])
            conn.close()
            self.assertEqual(raw["coupang"]["access_key"], "REALKEY123",
                             f"가려진 표기({prefix!r})가 키에 남았다")

    def test_key_with_korean_is_refused_with_clear_message(self):
        """키에 한글이 섞이면 저장을 막고 무엇이 문제인지 알려 준다."""
        r = self.client.put("/api/settings", json={"malls": {
            "coupang": {"access_key": "AK쿠팡키"}}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("다시 붙여넣어", r.get_json()["error"])

    def test_empty_secret_keeps_stored_value(self):
        """화면에서 비밀 칸을 비워 둔 채 저장하면 기존 키가 유지돼야 한다."""
        self.client.put("/api/settings", json={"malls": {
            "coupang": {"access_key": "AK-KEEP", "secret_key": "SK-KEEP"}}})
        self.client.put("/api/settings", json={"malls": {
            "coupang": {"access_key": MASK, "secret_key": MASK, "vendor_id": "V2"}}})
        conn = sqlite3.connect(self.db_path)
        raw = json.loads(conn.execute(
            "SELECT value FROM settings WHERE key='malls'").fetchone()[0])
        conn.close()
        self.assertEqual(raw["coupang"]["access_key"], "AK-KEEP")
        self.assertEqual(raw["coupang"]["vendor_id"], "V2")

    def test_saving_one_mall_keeps_others(self):
        """몰 하나를 저장해도 다른 몰의 API 키가 남아 있어야 한다."""
        self.client.put("/api/settings", json={"malls": {
            "coupang": {"vendor_id": "A001", "access_key": "AK", "secret_key": "SK", "enabled": True}}})
        self.client.put("/api/settings", json={"malls": {
            "godomall": {"partner_key": "PK", "user_key": "UK", "enabled": True}}})
        raw = self.client.get("/api/settings").get_json()["malls"]
        self.assertIn("coupang", raw)
        self.assertIn("godomall", raw)
        self.assertEqual(raw["coupang"]["vendor_id"], "A001")
        self.assertEqual(raw["coupang"]["access_key"], MASK)   # 값은 살아 있고 마스킹만
        # 실제 값 보존 확인 — 어댑터가 만들어져야 한다
        st = self.client.get("/api/mall-status").get_json()
        self.assertTrue(next(m for m in st if m["code"] == "godomall")["ready"])
        # 다른 최상위 설정도 보존
        self.client.put("/api/settings", json={"cj": {"cust_id": "00000000"}})
        self.client.put("/api/settings", json={"malls": {"toss": {"client_id": "C", "enabled": True}}})
        s = self.client.get("/api/settings").get_json()
        self.assertEqual(s["cj"]["cust_id"], "00000000")
        self.assertEqual(len(s["malls"]), 3)

    def test_recall_cancel_restores_asset(self):
        """회수 예약을 취소하면 자산이 '회수중'에 고착되지 않는다."""
        o, a = self.make_shipped_order()
        self.client.patch(f"/api/assets/{a['id']}", json={"status": "shipped"}) if False else None
        wid = self.client.post(f"/api/orders/{o['id']}/recall", json={}).get_json()["wid"]
        self.assertEqual(self.client.get(f"/api/assets/{a['id']}").get_json()["status"], "returning")
        r = self.client.post(f"/api/waybills/{wid}/cancel")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        asset = self.client.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(asset["status"], "shipped")   # 회수 직전 상태로 복원
        self.assertIn("회수취소", [e["action"] for e in asset["events"]])
        # 취소 후 재예약 가능
        self.assertEqual(self.client.post(f"/api/orders/{o['id']}/recall", json={}).status_code, 201)

    def test_as_recall_does_not_touch_order(self):
        """A/S 회수는 order_id를 물지 않아 주문 반품 회수와 섞이지 않는다."""
        o, a = self.make_shipped_order()
        t = self.client.post("/api/as-tickets", json={
            "customer": "홍길동", "phone": "010-1", "address": "서울 1",
            "symptom": "불량", "assetId": a["id"]}).get_json()
        self.client.post(f"/api/as-tickets/{t['id']}/recall", json={})
        # 주문 반품 회수는 여전히 가능해야 한다(A/S 회수가 막지 않는다)
        r = self.client.post(f"/api/orders/{o['id']}/recall", json={})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        # A/S 회수 입고 시 A/S 상태가 진행되고 자산도 돌아온다
        as_wid = [w for w in self.client.get("/api/waybills?type=recall").get_json()
                  if "A/S회수" in w["items"]][0]["wid"]
        r = self.client.post(f"/api/waybills/{as_wid}/received", json={"status": "repair"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["assets"], [a["assetNo"]])
        # ★2026-09-07 개편: 입고는 '입고 완료'(arrived)에 선다 — 수리 시작은 사람이 누른다.
        #   (예전엔 곧바로 '수리 중'이 되어 도착만 했는지 손을 댔는지 구분이 안 됐다)
        self.assertEqual(self.client.get(f"/api/as-tickets/{t['id']}").get_json()["status"], "arrived")

    def test_margin_counts_cost_per_order(self):
        """같은 자산이 두 번 팔리면 원가도 두 번 계상돼야 한다(집계 단위 일치)."""
        o1, a = self.make_shipped_order(amount=500000)
        # 회수 → 재고 복귀 → 다른 주문으로 재판매
        wid = self.client.post(f"/api/orders/{o1['id']}/recall", json={}).get_json()["wid"]
        self.client.post(f"/api/waybills/{wid}/received", json={"status": "ready"})
        o2 = self.client.post("/api/orders", json={
            "recipient": "재판매고객", "productName": "노트북", "amount": 400000,
            "address": "서울 2", "phone": "010-2"}).get_json()
        self.client.patch(f"/api/orders/{o2['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.client.patch(f"/api/orders/{o2['id']}", json={"action": "production", "value": True})
        self.client.patch(f"/api/orders/{o2['id']}", json={"action": "softwareInspection", "value": True})
        self.client.patch(f"/api/orders/{o2['id']}", json={"action": "shipping", "value": True})
        s = self.client.get("/api/reports/summary").get_json()
        # 회수 완료된 o1은 매출에서 빠지고, o2만 남는다
        self.assertEqual(s["sales"]["orders"], 1)
        self.assertEqual(s["sales"]["revenue"], 400000)
        self.assertEqual(s["sales"]["buyCost"], 200000)

    def test_returned_order_excluded_from_revenue(self):
        o, a = self.make_shipped_order(amount=500000)
        before = self.client.get("/api/reports/summary").get_json()
        self.assertEqual(before["sales"]["revenue"], 500000)
        wid = self.client.post(f"/api/orders/{o['id']}/recall", json={}).get_json()["wid"]
        self.client.post(f"/api/waybills/{wid}/received", json={"status": "ready"})
        after = self.client.get("/api/reports/summary").get_json()
        self.assertEqual(after["sales"]["revenue"], 0)      # 반품됐으므로 매출 아님
        self.assertEqual(after["sales"]["cost"], 0)
        self.assertEqual(after["stock"]["assets"], 1)       # 재고로 돌아옴

    def test_staff_excludes_undone_stages(self):
        """단계를 해제하면 담당자 실적에서도 빠져야 한다."""
        o, _ = self.make_shipped_order()
        s = self.client.get("/api/reports/staff").get_json()
        self.assertEqual(s[0]["shipping"], 1)
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": False})
        s = self.client.get("/api/reports/staff").get_json()
        self.assertEqual(s[0]["shipping"], 0)
        self.assertEqual(s[0]["production"], 1)

    def test_mall_status_open_to_importers(self):
        """수집을 실행하는 사람(orders.import)도 몰 상태를 볼 수 있어야 한다."""
        _, c = self._user("importer", ["orders.view", "orders.import"])
        from app.malls import MALLS
        r = c.get("/api/mall-status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()), len(MALLS))
        # 설정 변경은 여전히 불가
        self.assertEqual(c.get("/api/settings").status_code, 403)
        self.assertEqual(c.put("/api/settings", json={"malls": {}}).status_code, 403)

    def test_report_scope_limits_stock(self):
        """담당 분류가 제한된 사용자는 자기 분류 재고만 본다."""
        self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "purchasePrice": 100000, "qty": 1})
        self.client.post("/api/assets", json={
            "categoryId": self.cats[1]["id"], "purchasePrice": 300000, "qty": 1})
        r = self.client.post("/api/users", json={
            "username": "scoped", "displayName": "스코프", "password": "scoped-pass-1",
            "perms": ["reports.view"], "categoryIds": [self.cats[0]["id"]]})
        self.assertEqual(r.status_code, 201)
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "scoped", "password": "scoped-pass-1"})
        s = c.get("/api/reports/summary").get_json()
        self.assertEqual(s["stock"]["assets"], 1)
        self.assertEqual(s["stock"]["amount"], 100000)
        aging = c.get("/api/reports/aging").get_json()
        self.assertEqual(len(aging), 1)


if __name__ == "__main__":
    unittest.main()


class TestGodomallOptionText(unittest.TestCase):
    """고도몰 옵션은 배열로도 온다 — 개발자용 원문이 화면·송장에 찍히면 안 된다.

    2026-07-29 라이브 21건 확인:
    ['제품등급선택 (필수)', 'A급 외관 / S급 배터리', '', -30000, None] 이 그대로 저장돼 있었다.
    """

    def _opt(self, xml_option):
        import xml.etree.ElementTree as ET
        from app.malls.godomall import _option_text
        node = ET.fromstring(f"<goods><optionInfo>{xml_option}</optionInfo></goods>")
        return _option_text(node)

    def test_array_option_becomes_readable(self):
        got = self._opt('[["제품등급선택 (필수)","A급 외관 / S급 배터리","",-30000,null]]')
        self.assertEqual(got, "제품등급선택 (필수): A급 외관 / S급 배터리 (-30,000원)")
        self.assertNotIn("[", got, "개발자용 원문이 그대로 남았다")

    def test_array_without_extra_price(self):
        got = self._opt('[["(필수선택) 제품등급","A급 외관 / S급 배터리","",0,null]]')
        self.assertEqual(got, "(필수선택) 제품등급: A급 외관 / S급 배터리")

    def test_dict_option_still_works(self):
        got = self._opt('[{"optionName":"램","optionValue":"16GB"}]')
        self.assertEqual(got, "램: 16GB")

    def test_repair_of_stored_text(self):
        """이미 저장된 원문도 되돌릴 수 있어야 한다(라이브 정정에 쓴 함수)."""
        from app.malls.godomall import readable_option
        raw = ("['제품등급선택 (필수)', 'A급 외관 / A급 배터리', '', -30000, None]"
               " / 리브레오피스 설치 / [노트북쿨러] 노트북 쿨러 블랙")
        got = readable_option(raw)
        self.assertTrue(got.startswith("제품등급선택 (필수): A급 외관 / A급 배터리 (-30,000원)"))
        self.assertIn("리브레오피스 설치", got)
        self.assertIn("[노트북쿨러] 노트북 쿨러 블랙", got, "뒤 옵션이 사라졌다")

    def test_repair_keeps_normal_text(self):
        from app.malls.godomall import readable_option
        for plain in ("리브레오피스 설치", "", "옵션 없음 / 추가 없음"):
            self.assertEqual(readable_option(plain), plain)
