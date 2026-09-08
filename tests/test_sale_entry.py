"""판매 전표 직접 등록·수정(2026-09-02 대표 방침 "모든 데이터는 OWS·RMS에서 직접 등록·관리").

  - 전표 생성·번호대(S{YYMMDD}-500~, TMS 001~499와 분리)
  - 라인 가드: 렌탈 귀속 거부 · 살아 있는 다른 전표 라인 거부 · 주문이 잡은 자산 거부
  - 헤더·라인 수정, 반입(복귀 후보 / 즉시 복귀), 전표 취소, 미수 집계
  - 연동 행이 ows_edited_at 전표·명세를 덮지 않음, 옵션가 헤더 매핑(헤더 없는 행은 예전 그대로)
  - 순이익 산식 = TMS 판매상세 실측(2026-09-03, docs/SALE_ENTRY.md §1-6) — 실제 전표 2건을 그대로 고정
  - 되돌리기(전표 취소 되돌리기 · 반입 취소 · 라인 취소 되돌리기) — OWS 취소분만, 자산 경합 409
  - 회전 이력(/sale-slips/rounds) — 같은 자산의 판매→반입→재판매 회차
★임시 DB만 쓴다 — 운영·dev DB(data/*.db)에는 절대 붙지 않는다.
"""
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import auth as auth_mod  # noqa: E402
from app import config, create_app  # noqa: E402

PW = "admin-pass-1"
USER_PW = "user-pass-12"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-se-"))
        self.db_path = self.tmp / "t.db"
        self.app = create_app(db_path=self.db_path)
        self.app.testing = True
        self.c = self.app.test_client()
        auth_mod._login_failures.clear()
        self.c.post("/api/auth/setup", json={
            "username": "admin", "displayName": "대표", "password": PW})
        self.cat = self.c.get("/api/categories").get_json()[0]["id"]
        self.sid = self.c.post("/api/suppliers", json={"name": "시험거래처"}).get_json()["id"]
        self.bid = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-08-01", "supplierId": self.sid,
            "totalAmount": 0}).get_json()["id"]
        self.today = config.now().strftime("%y%m%d")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def sql(self, q, *args):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(q, args).fetchall()]
            conn.commit()
            return rows
        finally:
            conn.close()

    def asset(self, no, price=300000, status="ready", division="sale", model="L480"):
        """자산 하나 — 번호·상태·사업부를 시험 값으로 못 박는다(자동발번이라 SQL로 고친다)."""
        a = self.c.post("/api/assets", json={
            "categoryId": self.cat, "batchId": self.bid, "model": model, "qty": 1,
            "purchasePrice": price}).get_json()[0]
        self.sql("UPDATE assets SET asset_no=?, status=?, division=? WHERE id=?",
                 no, status, division, a["id"])
        return a["id"]

    def slip(self, lines, **head):
        body = {"channel": "방문구매", "customer": "시험거래처", "saleDate": "2026-09-02"}
        body.update(head)
        body["lines"] = lines
        return self.c.post("/api/sale-slips", json=body)

    def line(self, no, price=500000, **kw):
        return {"assetNo": no, "salePrice": price, **kw}

    def events(self, aid):
        return [r["action"] for r in self.sql(
            "SELECT action FROM asset_events WHERE asset_id=? ORDER BY id", aid)]

    def apply_slips(self, rows):
        from app.db import tx
        from app.purchase.sales import apply_sale_slips
        with self.app.app_context():
            with tx(write=True) as conn:
                return apply_sale_slips(conn, rows, actor="연동")

    def upsert(self, rows):
        from app.db import tx
        from app.purchase.sales import upsert_asset_sales
        with self.app.app_context():
            with tx(write=True) as conn:
                return upsert_asset_sales(conn, rows, actor="연동")

    def line_of(self, slip, no):
        return next(x for x in slip["lines"] if x["assetNo"] == no)


class TestCreate(Base):
    def test_전표_생성_번호대_500부터(self):
        a1 = self.asset("260901-0001")
        a2 = self.asset("260901-0002", price=200000)
        r = self.slip([self.line("260901-0001", 500000), self.line("260901-0002", 400000)],
                      vat=10000, fee=5000, shipping=3000)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d["slipNo"], f"S{self.today}-500")
        s = d["slip"]
        self.assertTrue(s["isOws"])
        self.assertEqual(s["source"], "ows")
        self.assertTrue(s["owsEditedAt"])
        self.assertEqual(s["stage"], "판매")
        self.assertEqual(s["qty"], 2)
        self.assertEqual(s["itemSaleSum"], 900000)
        self.assertEqual(s["saleAmount"], 900000)
        self.assertEqual(s["diffAmount"], 0)
        self.assertEqual(s["purchaseAmount"], 500000)
        # 라인 순이익(TMS 산식) = 판매가 − 총원가 − 판매수수료(3%) − (판매부가세 10% − 매입부가세 10%)
        l1 = self.line_of(s, "260901-0001")
        self.assertEqual((l1["saleVat"], l1["buyVat"], l1["netVat"], l1["saleFee"], l1["feeRate"]),
                         (50000, 30000, 20000, 15000, 3.0))
        self.assertEqual(l1["profit"], 500000 - 300000 - 15000 - 20000)
        l2 = self.line_of(s, "260901-0002")
        self.assertEqual(l2["profit"], 400000 - 200000 - 12000 - 20000)
        self.assertEqual(s["itemProfit"], 165000 + 168000)
        # 순이익액 = Σ라인 순이익 − 헤더 부가세·수수료·배송비(TMS는 늘 0 — 있으면 그대로 뺀다)
        self.assertEqual(s["profit"], 333000 - 10000 - 5000 - 3000)
        # 두 번째 전표는 501
        self.asset("260901-0003")
        r2 = self.slip([self.line("260901-0003")])
        self.assertEqual(r2.get_json()["slipNo"], f"S{self.today}-501")
        # 라인이 tms_sales 에 ows 출처로 남는다(정산 칸도 저장)
        rows = self.sql("SELECT * FROM tms_sales WHERE slip_no=? ORDER BY id", f"S{self.today}-500")
        self.assertEqual([x["asset_no"] for x in rows], ["260901-0001", "260901-0002"])
        self.assertEqual(rows[0]["source"], "ows")
        self.assertTrue(rows[0]["ows_edited_at"])
        self.assertEqual(rows[0]["asset_id"], a1)
        self.assertEqual(rows[1]["asset_id"], a2)
        self.assertEqual(rows[0]["seller"], "시험거래처")
        self.assertEqual(rows[0]["purchase_price"], 300000)
        self.assertEqual((rows[0]["sale_vat"], rows[0]["buy_vat"], rows[0]["sale_fee"], rows[0]["fee_rate"],
                          rows[0]["tms_profit"]), (50000, 30000, 15000, 3.0, 165000))

    def test_TMS_번호가_같은_날_있어도_OWS_번호대는_500부터(self):
        self.apply_slips([{"판매전표": f"S{self.today}-001", "판매금액": "1000"},
                          {"판매전표": f"S{self.today}-499", "판매금액": "1000"}])
        self.asset("260901-0001")
        r = self.slip([self.line("260901-0001")])
        self.assertEqual(r.get_json()["slipNo"], f"S{self.today}-500")

    def test_라인_추가가_자산을_출고완료로_옮기고_이력을_남긴다(self):
        aid = self.asset("260901-0001", status="ready")
        self.sql("UPDATE assets SET stock_listed=1, product_code='X' WHERE id=?", aid)
        r = self.slip([self.line("260901-0001", 450000)])
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        a = self.c.get(f"/api/assets/{aid}").get_json()
        self.assertEqual(a["status"], "shipped")
        self.assertFalse(a["stockListed"], "나간 물건이 몰 재고 전송 대상으로 남았다")
        ev = self.events(aid)
        self.assertIn("판매", ev)
        self.assertIn("재고해제", ev)
        # 자산 상세의 판매 정보에도 OWS 전표가 붙는다
        self.assertEqual(a["tmsSale"]["slipNo"], f"S{self.today}-500")
        self.assertEqual(a["tmsSale"]["salePrice"], 450000)
        # 감사 로그
        self.assertTrue(self.sql("SELECT 1 FROM audit_log WHERE action='sale_slip_create'"))

    def test_이미_출고완료면_상태는_두고_기록만(self):
        aid = self.asset("260901-0001", status="shipped")
        r = self.slip([self.line("260901-0001")])
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertTrue(any("출고완료" in w for w in r.get_json()["warnings"]))
        self.assertEqual(self.sql("SELECT status FROM assets WHERE id=?", aid)[0]["status"], "shipped")

    def test_라인도_판매금액도_없으면_거부(self):
        r = self.slip([])
        self.assertEqual(r.status_code, 400)
        # 판매금액만 적은 전표(TMS에도 있는 형태)는 된다
        r = self.slip([], saleAmount=150000)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        s = r.get_json()["slip"]
        self.assertEqual((s["saleAmount"], s["itemSaleSum"], s["diffAmount"]), (150000, 0, 150000))

    def test_판매가_음수는_거부_옵션가_음수는_허용(self):
        self.asset("260901-0001")
        r = self.slip([self.line("260901-0001", -1)])
        self.assertEqual(r.status_code, 400)
        # 옵션가는 원가다 — 탈거가 양수 = 회수(원가에서 뺌, TMS 관례), 음수 = 원가에 더함
        r = self.slip([self.line("260901-0001", 500000, removalPrice=-20000, upgrade1Price=30000,
                                 upgrade1Item="RAM 16G")])
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        l = r.get_json()["slip"]["lines"][0]
        self.assertEqual((l["optionCost"], l["totalCost"]), (50000, 350000))
        self.assertEqual(l["profit"], 500000 - 350000 - 15000 - (50000 - 35000))
        self.assertEqual(l["upgrade1Item"], "RAM 16G")
        # 옵션가는 매출이 아니다 — 판매금액은 판매가 그대로
        self.assertEqual(r.get_json()["slip"]["saleAmount"], 500000)


class TestTmsFormula(Base):
    """TMS 판매상세 실측 전표(2026-09-03 D:\\data-bridge tms_mirror 사본)의 값을 그대로 재현한다.

    ★이 값이 틀리면 원장 연속성이 깨진다 — 산식을 바꾸려면 대표 확인 후 여기부터 고친다.
    """

    def test_실측_S251103_001_자사몰_기본형(self):
        # 판매가 99,000 / 매입가 38,500 / 포장료 4,000 / 수수율 3%
        # → 판매부가세 9,900 · 매입부가세 4,250 · 실부가세 5,650 · 판매수수료 2,970 · 순이익 47,880
        self.asset("251103-0001", price=38500)
        r = self.slip([self.line("251103-0001", 99000, packingFee=4000)], channel="자사몰(업무관리)")
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        s = r.get_json()["slip"]
        l = s["lines"][0]
        self.assertEqual((l["optionCost"], l["totalCost"]), (0, 42500))
        self.assertEqual((l["saleVat"], l["buyVat"], l["netVat"]), (9900, 4250, 5650))
        self.assertEqual((l["feeRate"], l["saleFee"]), (3.0, 2970))
        self.assertEqual(l["profit"], 47880)
        self.assertEqual((s["itemProfit"], s["profit"], s["saleAmount"]), (47880, 47880, 99000))
        row = self.sql("SELECT tms_profit, buy_vat, sale_vat, sale_fee FROM tms_sales WHERE asset_no='251103-0001'")[0]
        self.assertEqual((row["tms_profit"], row["buy_vat"], row["sale_vat"], row["sale_fee"]), (47880, 4250, 9900, 2970))

    def test_실측_S251212_001_탈거_업그레이드형(self):
        # 판매가 370,000 / 매입가 264,000 / 업1 45,000(DDR4 램 X->8G) / 업2 35,000(X->M/N 256G) /
        # 기타구성 2,000 / 탈거 90,000 / 포장 4,000 → 부품옵션합계 −8,000 · 총원가 260,000 ·
        # 판매부가세 37,000 · 매입부가세 26,000 · 실부가세 11,000 · 수수료 11,100 · 순이익 87,900
        self.asset("251202-0074", price=264000)
        r = self.slip([self.line("251202-0074", 370000, upgrade1Price=45000, upgrade1Item="DDR4 램 X->8G",
                                 upgrade2Price=35000, upgrade2Item="X->M/N 256G", extraPrice=2000,
                                 extraItems="유선마우스,마우스패드", chargerPrice=0, removalPrice=90000,
                                 packingFee=4000)], channel="쿠팡")
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        l = r.get_json()["slip"]["lines"][0]
        self.assertEqual((l["optionCost"], l["totalCost"]), (-8000, 260000))
        self.assertEqual((l["saleVat"], l["buyVat"], l["netVat"], l["saleFee"]), (37000, 26000, 11000, 11100))
        self.assertEqual(l["profit"], 87900)
        # 탈거가는 양수로 저장된다(TMS 관례) — 화면의 '＋ 원가에 더함'만 음수
        self.assertEqual(self.sql("SELECT removal_price FROM tms_sales WHERE asset_no='251202-0074'")[0]["removal_price"], 90000)

    def test_수수율_직접_지정과_정산칸_덮어쓰기(self):
        self.asset("251103-0001", price=38500)
        # 수수율 0%(B2B·방문 등 TMS 386행) → 수수료 0
        r = self.slip([self.line("251103-0001", 99000, packingFee=4000, feeRate=0)])
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        l = r.get_json()["slip"]["lines"][0]
        self.assertEqual((l["feeRate"], l["saleFee"], l["profit"]), (0.0, 0, 99000 - 42500 - 5650))
        no = r.get_json()["slipNo"]
        # 판매가만 고치면 부가세·수수료가 규칙으로 다시 계산된다(수수율은 저장값 유지)
        r = self.c.patch(f"/api/sale-slips/{no}/lines/251103-0001", json={"salePrice": 109000})
        l = r.get_json()["slip"]["lines"][0]
        self.assertEqual((l["saleVat"], l["saleFee"], l["feeRate"]), (10900, 0, 0.0))
        # 정산 칸을 직접 보내면 그 값(면세·특약)
        r = self.c.patch(f"/api/sale-slips/{no}/lines/251103-0001", json={"saleVat": 0, "saleFee": 1000})
        l = r.get_json()["slip"]["lines"][0]
        self.assertEqual((l["saleVat"], l["saleFee"]), (0, 1000))
        self.assertEqual(l["profit"], 109000 - 42500 - 1000 - (0 - 4250))
        # 수수율을 바꾸면 수수료가 따라온다
        r = self.c.patch(f"/api/sale-slips/{no}/lines/251103-0001", json={"feeRate": 3})
        l = r.get_json()["slip"]["lines"][0]
        self.assertEqual(l["saleFee"], 3270)
        self.assertEqual(self.c.patch(f"/api/sale-slips/{no}/lines/251103-0001", json={"feeRate": 101}).status_code, 400)

    def test_반올림은_올림(self):
        # 90,950 × 3% = 2,728.5 → TMS는 2,729(.5 올림). 파이썬 round 는 2,728(짝수)로 틀린다.
        from app.purchase.sale_entry import SALE_FEE_RATE_DEFAULT, SALE_VAT_RATE, halfup
        self.assertEqual((SALE_VAT_RATE, SALE_FEE_RATE_DEFAULT), (10, 3.0))
        self.assertEqual(halfup(2728.5), 2729)
        self.asset("260901-0001", price=50000)
        r = self.slip([self.line("260901-0001", 90950)])
        self.assertEqual(r.get_json()["slip"]["lines"][0]["saleFee"], 2729)


class TestGuards(Base):
    def test_렌탈_귀속_자산은_거부되고_아무것도_저장되지_않는다(self):
        self.asset("260901-0001")
        self.asset("260901-0002", division="rental")
        r = self.slip([self.line("260901-0001"), self.line("260901-0002")])
        self.assertEqual(r.status_code, 400)
        self.assertIn("렌탈", r.get_json()["error"])
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM sale_slips")[0]["n"], 0, "일부만 저장됐다")
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM tms_sales")[0]["n"], 0)
        self.assertEqual(self.sql("SELECT status FROM assets WHERE asset_no='260901-0001'")[0]["status"],
                         "ready", "거부된 전표가 자산 상태를 바꿨다")

    def test_살아있는_다른_전표_라인에_있는_자산은_거부(self):
        self.asset("260901-0001")
        first = self.slip([self.line("260901-0001")]).get_json()["slipNo"]
        r = self.slip([self.line("260901-0001")])
        self.assertEqual(r.status_code, 400)
        self.assertIn(first, r.get_json()["error"])
        # 그 라인을 취소하면 다시 팔 수 있다
        r = self.c.delete(f"/api/sale-slips/{first}/lines/260901-0001", json={"reason": "오등록"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = self.slip([self.line("260901-0001")])
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))

    def test_TMS_연동_명세에_살아있는_자산도_거부(self):
        self.asset("260901-0001", status="shipped")
        self.upsert([{"판매전표": "S260820-001", "관리번호": "260901-0001", "수령자성함": "구매자",
                      "판매채널": "자사몰", "판매가": "400000", "진행상태": "판매"}])
        r = self.slip([self.line("260901-0001")])
        self.assertEqual(r.status_code, 400)
        self.assertIn("S260820-001", r.get_json()["error"])

    def test_주문이_잡은_자산과_없는_번호는_거부(self):
        aid = self.asset("260901-0001")
        oid = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "구매자", "productName": "L480",
            "quantity": 1, "amount": 550000}).get_json()["id"]
        self.c.patch(f"/api/orders/{oid}", json={"action": "assets", "assetIds": [aid]})
        r = self.slip([self.line("260901-0001")])
        self.assertEqual(r.status_code, 400)
        r = self.slip([self.line("999999-9999")])
        self.assertEqual(r.status_code, 400)
        self.assertIn("등록되지 않은", r.get_json()["error"])

    def test_check_assets가_가부와_사유를_준다(self):
        self.asset("260901-0001")
        self.asset("260901-0002", division="rental")
        r = self.c.post("/api/sale-slips/check-assets",
                        json={"assetNos": ["260901-0001", "260901-0002", "없는번호"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        items = {x["assetNo"]: x for x in r.get_json()["items"]}
        self.assertTrue(items["260901-0001"]["ok"])
        self.assertFalse(items["260901-0002"]["ok"])
        self.assertIn("렌탈", items["260901-0002"]["reason"])
        self.assertFalse(items["없는번호"]["ok"])

    def test_같은_전표에_같은_자산_두_번은_거부(self):
        self.asset("260901-0001")
        r = self.slip([self.line("260901-0001"), self.line("260901-0001")])
        self.assertEqual(r.status_code, 400)


class TestEdit(Base):
    def setUp(self):
        super().setUp()
        self.asset("260901-0001", price=300000)
        self.asset("260901-0002", price=200000)
        self.no = self.slip([self.line("260901-0001", 500000),
                             self.line("260901-0002", 400000)]).get_json()["slipNo"]

    def test_헤더_수정과_판매금액_직접_지정(self):
        r = self.c.patch(f"/api/sale-slips/{self.no}", json={
            "customer": "바뀐거래처", "fee": 20000, "saleAmount": 880000})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        s = r.get_json()["slip"]
        self.assertEqual(s["customer"], "바뀐거래처")
        self.assertEqual((s["saleAmount"], s["itemSaleSum"], s["diffAmount"]), (880000, 900000, -20000))
        # 순이익액 = Σ라인 순이익(165,000+168,000) − 헤더 수수료. ★판매차이금액은 TMS처럼 순이익에 안 넣는다
        self.assertEqual(s["profit"], 333000 - 20000)
        # 헤더 판매처는 OWS 라인의 판매처에도 실린다
        self.assertEqual(self.sql("SELECT seller FROM tms_sales WHERE slip_no=?", self.no)[0]["seller"],
                         "바뀐거래처")
        # 라인을 더하면 차액(할인)은 그대로 얹힌다
        self.asset("260901-0003", price=100000)
        r = self.c.post(f"/api/sale-slips/{self.no}/lines", json={"lines": [self.line("260901-0003", 100000)]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        s = r.get_json()["slip"]
        self.assertEqual((s["qty"], s["itemSaleSum"], s["saleAmount"]), (3, 1000000, 980000))
        self.assertEqual(self.line_of(s, "260901-0003")["profit"], 100000 - 100000 - 3000 - 0)

    def test_라인_수정이_합계를_다시_센다(self):
        r = self.c.patch(f"/api/sale-slips/{self.no}/lines/260901-0001",
                         json={"salePrice": 550000, "upgrade2Price": 40000, "upgrade2Item": "SSD 1TB"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        s = r.get_json()["slip"]
        self.assertEqual(s["itemSaleSum"], 550000 + 400000)
        self.assertEqual(s["saleAmount"], 950000)
        l = self.line_of(s, "260901-0001")
        self.assertEqual((l["optionCost"], l["totalCost"], l["saleVat"], l["buyVat"], l["saleFee"]),
                         (40000, 340000, 55000, 34000, 16500))
        self.assertEqual(l["profit"], 550000 - 340000 - 16500 - (55000 - 34000))
        aid = self.sql("SELECT id FROM assets WHERE asset_no='260901-0001'")[0]["id"]
        self.assertIn("판매수정", self.events(aid))

    def test_상세_조회와_입금_수정_미수_집계(self):
        d = self.c.get(f"/api/sale-slips/{self.no}").get_json()
        self.assertEqual(d["slipNo"], self.no)
        self.assertEqual(len(d["lines"]), 2)
        self.assertEqual(d["liveCount"], 2)
        r = self.c.patch(f"/api/sale-slips/{self.no}", json={
            "paidStatus": "중도금", "paidAmount": 300000, "paidAt": "2026-09-02"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rc = self.c.get("/api/sale-slips/receivables").get_json()
        self.assertEqual(rc["total"], 600000, rc)
        self.assertEqual(rc["customers"][0]["customer"], "시험거래처")
        # 목록에도 OWS 전표가 뜨고 배지 정보가 실린다
        lst = self.c.get("/api/sale-slips").get_json()
        row = next(x for x in lst["slips"] if x["slipNo"] == self.no)
        self.assertTrue(row["isOws"])
        self.assertTrue(row["owsEditedAt"])
        self.assertEqual(lst["total"]["sale"], 900000)
        # 입금확인을 켜면 미수에서 빠진다
        self.c.patch(f"/api/sale-slips/{self.no}", json={"paidAmount": 900000, "paidConfirmed": True,
                                                        "paidStatus": "완납"})
        self.assertEqual(self.c.get("/api/sale-slips/receivables").get_json()["total"], 0)

    def test_반입은_기본_복귀후보_명시하면_즉시_복귀(self):
        aid1 = self.sql("SELECT id FROM assets WHERE asset_no='260901-0001'")[0]["id"]
        aid2 = self.sql("SELECT id FROM assets WHERE asset_no='260901-0002'")[0]["id"]
        r = self.c.post(f"/api/sale-slips/{self.no}/lines/260901-0001/return",
                        json={"reason": "변심", "returnDate": "2026-09-03"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertTrue(d["asset"]["candidate"])
        self.assertFalse(d["asset"]["restocked"])
        self.assertEqual(self.sql("SELECT status FROM assets WHERE id=?", aid1)[0]["status"], "shipped")
        self.assertIn("복귀후보", self.events(aid1))
        self.assertIn("반입", self.events(aid1))
        # ↩ 복귀 후보 대기열에 뜬다
        cand = self.c.get("/api/assets/revert-candidates").get_json()
        self.assertEqual([x["assetNo"] for x in cand["rows"]], ["260901-0001"])
        # 합계는 살아 있는 라인만
        s = d["slip"]
        self.assertEqual((s["qty"], s["itemSaleSum"], s["saleAmount"]), (1, 400000, 400000))
        l = self.line_of(s, "260901-0001")
        self.assertEqual((l["stage"], l["returnDate"], l["prevStage"]), ("반입", "2026-09-03", "판매"))
        self.assertTrue(l["owsUndoable"])
        # 두 번째는 실물 확인 → 즉시 복귀(기존 복귀 규칙 그대로)
        r = self.c.post(f"/api/sale-slips/{self.no}/lines/260901-0002/return",
                        json={"reason": "확인", "restock": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["asset"]["restocked"])
        a = self.c.get(f"/api/assets/{aid2}").get_json()
        self.assertEqual((a["status"], a["tier"]), ("in_stock", "실재고"))
        self.assertIn("재고복귀", self.events(aid2))
        # 전부 반입되면 전표도 반입
        # ★전표 반입일은 라인 반입일 중 가장 늦은 날이다(sale_entry `max(...)`). 두 번째 반입은
        #   returnDate 를 안 줘서 서버 기본값(오늘)이 들어가므로, 전표 날짜는 늘 '오늘'이다.
        #   여기에 날짜를 박아 두면 그 날 하루만 통과한다 — 실제로 2026-09-03 이 박혀 있어
        #   다음 날부터 계속 실패했다(2026-09-08 확인).
        s = r.get_json()["slip"]
        today = config.now().strftime("%Y-%m-%d")
        self.assertEqual((s["stage"], s["qty"], s["returnDate"]), ("반입", 0, today))
        self.assertEqual(self.c.get("/api/sale-slips").get_json()["total"]["count"], 0)
        # 반입된 라인은 다시 못 고친다
        self.assertEqual(self.c.patch(f"/api/sale-slips/{self.no}/lines/260901-0001",
                                      json={"salePrice": 1}).status_code, 400)

    def test_라인_취소는_표시만_물리삭제_없음(self):
        r = self.c.delete(f"/api/sale-slips/{self.no}/lines/260901-0002", json={"reason": "오등록"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rows = self.sql("SELECT asset_no, stage, cancel_date, cancel_kind, prev_stage FROM tms_sales "
                        "WHERE slip_no=? ORDER BY id", self.no)
        self.assertEqual(len(rows), 2, "라인이 물리 삭제됐다")
        self.assertEqual((rows[1]["stage"], rows[1]["cancel_kind"], rows[1]["prev_stage"]), ("판매취소", "line", "판매"))
        self.assertTrue(rows[1]["cancel_date"])
        s = r.get_json()["slip"]
        self.assertEqual((s["qty"], s["saleAmount"], s["stage"]), (1, 500000, "판매"))
        self.assertEqual(self.c.delete(f"/api/sale-slips/{self.no}/lines/260901-0002",
                                       json={}).status_code, 400)

    def test_전표_취소(self):
        self.assertEqual(self.c.post(f"/api/sale-slips/{self.no}/cancel", json={}).status_code, 400,
                         "사유 없이 취소됐다")
        r = self.c.post(f"/api/sale-slips/{self.no}/cancel", json={"reason": "거래 무산"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        s = r.get_json()["slip"]
        self.assertEqual(s["stage"], "판매취소")
        self.assertEqual((s["prevStage"], s["cancelReason"]), ("판매", "거래 무산"))
        self.assertTrue(s["owsUndoable"])
        self.assertTrue(all(l["stage"] == "판매취소" and l["cancelKind"] == "slip" for l in s["lines"]))
        self.assertEqual(s["saleAmount"], 900000, "취소분 금액은 남겨야 따로 보인다")
        lst = self.c.get("/api/sale-slips").get_json()
        self.assertEqual(lst["total"]["count"], 0)
        self.assertEqual(lst["excluded"][0]["stage"], "판매취소")
        self.assertEqual(self.c.get("/api/sale-slips/receivables").get_json()["total"], 0)
        # 취소된 전표는 더 못 고친다 / 두 번 취소 안 됨
        self.assertEqual(self.c.patch(f"/api/sale-slips/{self.no}", json={"memo": "x"}).status_code, 400)
        self.assertEqual(self.c.post(f"/api/sale-slips/{self.no}/cancel", json={"reason": "x"}).status_code, 400)
        # 자산은 복귀 후보로만
        cand = self.c.get("/api/assets/revert-candidates").get_json()
        self.assertEqual(sorted(x["assetNo"] for x in cand["rows"]), ["260901-0001", "260901-0002"])
        # 취소된 전표의 자산은 다시 팔 수 있다
        self.assertEqual(self.slip([self.line("260901-0001")]).status_code, 201)


class TestUndo(Base):
    """되돌리기(2026-09-03) — 매입 전표 uncancel 과 같은 관례. OWS에서 한 취소·반입만 되돌린다."""

    def setUp(self):
        super().setUp()
        self.aid1 = self.asset("260901-0001", price=300000)
        self.aid2 = self.asset("260901-0002", price=200000)
        self.no = self.slip([self.line("260901-0001", 500000),
                             self.line("260901-0002", 400000)]).get_json()["slipNo"]

    def status(self, aid):
        return self.sql("SELECT status FROM assets WHERE id=?", aid)[0]["status"]

    def test_전표_취소_되돌리기(self):
        self.assertEqual(self.c.post(f"/api/sale-slips/{self.no}/uncancel").status_code, 400, "취소 안 된 전표가 되돌려졌다")
        self.c.post(f"/api/sale-slips/{self.no}/cancel", json={"reason": "실수"})
        self.assertEqual(len(self.c.get("/api/assets/revert-candidates").get_json()["rows"]), 2)
        r = self.c.post(f"/api/sale-slips/{self.no}/uncancel")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(sorted(x["assetNo"] for x in d["assets"]), ["260901-0001", "260901-0002"])
        s = d["slip"]
        self.assertEqual((s["stage"], s["cancelDate"], s["cancelReason"], s["prevStage"]), ("판매", "", "", ""))
        self.assertFalse(s["owsUndoable"])
        self.assertEqual((s["qty"], s["saleAmount"], s["itemProfit"]), (2, 900000, 333000))
        self.assertTrue(all(l["stage"] == "판매" and not l["cancelDate"] and not l["cancelKind"] for l in s["lines"]))
        # 자산: 판매 확정 상태 그대로, 열려 있던 복귀 후보는 닫힌다
        self.assertEqual(self.status(self.aid1), "shipped")
        ev = self.events(self.aid1)
        self.assertIn("판매취소해제", ev)
        self.assertIn("복귀후보종결", ev)
        self.assertEqual(self.c.get("/api/assets/revert-candidates").get_json()["rows"], [])
        # 목록 집계에 다시 들어간다 / 감사 기록
        self.assertEqual(self.c.get("/api/sale-slips").get_json()["total"]["count"], 1)
        self.assertTrue(self.sql("SELECT 1 FROM audit_log WHERE action='sale_slip_uncancel'"))
        # 되돌린 전표는 다시 고칠 수 있다
        self.assertEqual(self.c.patch(f"/api/sale-slips/{self.no}", json={"memo": "복원 후"}).status_code, 200)

    def test_재고로_되돌린_자산도_판매_상태로_복원(self):
        self.c.post(f"/api/sale-slips/{self.no}/cancel", json={"reason": "실수", "restock": True})
        self.assertEqual(self.status(self.aid1), "in_stock")
        r = self.c.post(f"/api/sale-slips/{self.no}/uncancel")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.status(self.aid1), "shipped")
        self.assertTrue(all(x["statusChanged"] for x in r.get_json()["assets"]))

    def test_라인_단독_취소분은_전표_되돌리기에_안_살아난다(self):
        self.c.delete(f"/api/sale-slips/{self.no}/lines/260901-0002", json={"reason": "오등록"})
        self.c.post(f"/api/sale-slips/{self.no}/cancel", json={"reason": "실수"})
        r = self.c.post(f"/api/sale-slips/{self.no}/uncancel")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        s = r.get_json()["slip"]
        self.assertEqual([x["assetNo"] for x in r.get_json()["assets"]], ["260901-0001"])
        self.assertEqual((self.line_of(s, "260901-0001")["stage"], self.line_of(s, "260901-0002")["stage"]),
                         ("판매", "판매취소"))
        self.assertEqual(s["qty"], 1)
        # 단독 취소 라인은 라인 되돌리기로
        r = self.c.post(f"/api/sale-slips/{self.no}/lines/260901-0002/uncancel")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual((r.get_json()["slip"]["qty"], r.get_json()["asset"]["assetNo"]), (2, "260901-0002"))
        self.assertEqual(self.c.post(f"/api/sale-slips/{self.no}/lines/260901-0002/uncancel").status_code, 400)

    def test_그_사이_다른_전표에_팔렸으면_409_전체_롤백(self):
        self.c.post(f"/api/sale-slips/{self.no}/cancel", json={"reason": "실수"})
        other = self.slip([self.line("260901-0001", 480000)]).get_json()["slipNo"]
        r = self.c.post(f"/api/sale-slips/{self.no}/uncancel")
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertIn(other, r.get_json()["error"])
        s = self.c.get(f"/api/sale-slips/{self.no}").get_json()
        self.assertEqual(s["stage"], "판매취소", "일부만 되돌아갔다")
        self.assertTrue(all(l["stage"] == "판매취소" for l in s["lines"]))
        self.assertEqual(self.c.get("/api/sale-slips").get_json()["total"]["count"], 1)

    def test_그_사이_주문이_잡았으면_409(self):
        self.c.post(f"/api/sale-slips/{self.no}/cancel", json={"reason": "실수", "restock": True})
        oid = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "구매자", "productName": "L480",
            "quantity": 1, "amount": 550000}).get_json()["id"]
        self.c.patch(f"/api/orders/{oid}", json={"action": "assets", "assetIds": [self.aid1]})
        r = self.c.post(f"/api/sale-slips/{self.no}/uncancel")
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertIn("260901-0001", r.get_json()["error"])
        self.assertEqual(self.c.get(f"/api/sale-slips/{self.no}").get_json()["stage"], "판매취소")

    def test_반입_취소(self):
        r = self.c.post(f"/api/sale-slips/{self.no}/lines/260901-0001/return",
                        json={"reason": "변심", "restock": True, "returnDate": "2026-09-03"})
        self.assertEqual(self.status(self.aid1), "in_stock")
        self.assertEqual(r.get_json()["slip"]["qty"], 1)
        r = self.c.post(f"/api/sale-slips/{self.no}/lines/260901-0001/unreturn")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual((d["asset"]["assetNo"], d["asset"]["status"], d["asset"]["statusChanged"]),
                         ("260901-0001", "shipped", True))
        l = self.line_of(d["slip"], "260901-0001")
        self.assertEqual((l["stage"], l["returnDate"], l["prevStage"], l["owsUndoable"]), ("판매", "", "", False))
        self.assertEqual((d["slip"]["qty"], d["slip"]["stage"], d["slip"]["saleAmount"]), (2, "판매", 900000))
        self.assertIn("반입취소", self.events(self.aid1))
        self.assertTrue(self.sql("SELECT 1 FROM audit_log WHERE action='sale_slip_line_unreturn'"))
        # 반입 아닌 라인은 400
        self.assertEqual(self.c.post(f"/api/sale-slips/{self.no}/lines/260901-0001/unreturn").status_code, 400)

    def test_전부_반입된_전표는_반입_취소로_되살아난다(self):
        for no in ("260901-0001", "260901-0002"):
            self.c.post(f"/api/sale-slips/{self.no}/lines/{no}/return", json={"reason": "변심"})
        self.assertEqual(self.c.get(f"/api/sale-slips/{self.no}").get_json()["stage"], "반입")
        r = self.c.post(f"/api/sale-slips/{self.no}/lines/260901-0002/unreturn")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual((r.get_json()["slip"]["stage"], r.get_json()["slip"]["qty"]), ("판매", 1))
        # 복귀 후보에 올라 있던 것은 닫히고, 아직 반입인 0001만 남는다
        self.assertEqual([x["assetNo"] for x in self.c.get("/api/assets/revert-candidates").get_json()["rows"]],
                         ["260901-0001"])

    def test_취소된_전표의_라인은_되돌릴_수_없다(self):
        self.c.post(f"/api/sale-slips/{self.no}/cancel", json={"reason": "실수"})
        self.assertEqual(self.c.post(f"/api/sale-slips/{self.no}/lines/260901-0001/uncancel").status_code, 400)
        self.assertEqual(self.c.post(f"/api/sale-slips/{self.no}/lines/260901-0001/unreturn").status_code, 400)

    def test_연동_전표의_취소_반입은_TMS가_정본이라_409(self):
        # TMS에서 취소된 전표
        self.apply_slips([{"판매전표": "S260820-001", "판매금액": "500000", "진행상태": "판매취소"}])
        r = self.c.post("/api/sale-slips/S260820-001/uncancel")
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertFalse(self.c.get("/api/sale-slips/S260820-001").get_json()["owsUndoable"])
        # TMS에서 반입된 명세
        self.asset("260901-0003", status="shipped")
        self.apply_slips([{"판매전표": "S260820-002", "판매금액": "500000", "진행상태": "판매"}])
        self.upsert([{"판매전표": "S260820-002", "관리번호": "260901-0003", "수령자성함": "구매자",
                      "판매채널": "자사몰", "판매가": "500000", "진행상태": "반입", "반입일": "2026-08-25"}])
        r = self.c.post("/api/sale-slips/S260820-002/lines/260901-0003/unreturn")
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertFalse(self.c.get("/api/sale-slips/S260820-002").get_json()["lines"][0]["owsUndoable"])
        # 단, OWS에서 취소한 연동 전표(cancel_date 표시)는 되돌린다 — 상태는 취소 직전 값으로
        self.apply_slips([{"판매전표": "S260820-003", "판매금액": "500000", "진행상태": "판매완료"}])
        self.c.post("/api/sale-slips/S260820-003/cancel", json={"reason": "OWS에서 취소"})
        s = self.c.get("/api/sale-slips/S260820-003").get_json()
        self.assertEqual((s["stage"], s["prevStage"], s["owsUndoable"]), ("판매취소", "판매완료", True))
        r = self.c.post("/api/sale-slips/S260820-003/uncancel")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["slip"]["stage"], "판매완료")
        # 연동 전표 헤더 금액은 여전히 TMS 값 그대로(다시 세지 않는다)
        self.assertEqual(r.get_json()["slip"]["saleAmount"], 500000)


class TestRounds(Base):
    """회전 이력(2026-09-03) — 같은 자산의 판매→반입→재판매 행 전부에 회차를 매긴다."""

    def test_회차와_반입일_취소일(self):
        self.asset("260901-0001")
        first = self.slip([self.line("260901-0001", 500000)], saleDate="2026-08-01").get_json()["slipNo"]
        self.c.post(f"/api/sale-slips/{first}/lines/260901-0001/return", json={"reason": "변심", "returnDate": "2026-08-05", "restock": True})
        second = self.slip([self.line("260901-0001", 450000)], saleDate="2026-08-10").get_json()["slipNo"]
        self.c.delete(f"/api/sale-slips/{second}/lines/260901-0001", json={"reason": "오등록", "restock": True})
        third = self.slip([self.line("260901-0001", 430000)], saleDate="2026-08-20").get_json()["slipNo"]
        r = self.c.get("/api/sale-slips/rounds?assetNo=260901-0001,없는번호")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()["assets"]
        self.assertNotIn("없는번호", d)
        h = d["260901-0001"]
        self.assertEqual(h["count"], 2, "취소 행은 회차에서 빠져야 한다")
        self.assertEqual([(x["slipNo"], x["round"], x["stage"]) for x in h["rows"]],
                         [(first, 1, "반입"), (second, None, "판매취소"), (third, 2, "판매")])
        self.assertEqual(h["rows"][0]["returnDate"], "2026-08-05")
        self.assertTrue(h["rows"][1]["cancelDate"])
        self.assertEqual(h["rows"][2]["salePrice"], 430000)
        # POST 도 같다
        r = self.c.post("/api/sale-slips/rounds", json={"assetNos": ["260901-0001"]})
        self.assertEqual(r.get_json()["assets"]["260901-0001"]["count"], 2)
        # 전표가 통째로 취소되면 그 행도 회차에서 빠진다
        self.c.post(f"/api/sale-slips/{third}/cancel", json={"reason": "무산"})
        self.assertEqual(self.c.get("/api/sale-slips/rounds?assetNo=260901-0001").get_json()["assets"]["260901-0001"]["count"], 1)
        # 판매 자산 목록(집계)은 건드리지 않았다 — 취소 제외 규칙 그대로
        sa = self.c.get("/api/sale-assets?from=2026-08-01&to=2026-08-31").get_json()
        self.assertEqual(sorted(x["slipNo"] for x in sa["items"]), [first])

    def test_정적_경로(self):
        self.assertEqual(self.c.get("/api/sale-slips/rounds").get_json(), {"assets": {}})
        self.assertEqual(self.c.post("/api/sale-slips/rounds", json={"assetNos": "x"}).status_code, 400)


class TestLinkProtection(Base):
    """연동(tms_link/autosync/엑셀)이 같은 함수로 들어온다 — 사람이 OWS에서 고친 값을 덮지 않는다."""

    def test_OWS_전표는_연동이_전혀_안_덮는다(self):
        self.asset("260901-0001")
        no = self.slip([self.line("260901-0001")]).get_json()["slipNo"]
        res = self.apply_slips([{"판매전표": no, "거래처명": "TMS이름", "판매금액": "1",
                                 "입금액": "999", "입금확인": "1", "납부확인": "완납"}])
        self.assertEqual(res["protected"], 1)
        self.assertEqual(res["updated"], 0)
        row = self.sql("SELECT customer, sale_amount, paid_amount, paid_confirmed FROM sale_slips WHERE slip_no=?", no)[0]
        self.assertEqual(row["customer"], "시험거래처")
        self.assertEqual(row["sale_amount"], 500000)
        self.assertEqual((row["paid_amount"], row["paid_confirmed"]), (0, 0), "OWS 전표의 입금까지 덮였다")

    def test_OWS에서_고친_연동_전표는_편집칸_불가침_입금만_따라간다(self):
        self.apply_slips([{"판매전표": "S260820-001", "거래처명": "원래이름", "판매일": "2026-08-20",
                           "판매금액": "500000", "입금액": "0", "입금확인": "0", "납부확인": "미수"}])
        r = self.c.patch("/api/sale-slips/S260820-001", json={"customer": "OWS이름", "memo": "OWS메모"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(r.get_json()["slip"]["isOws"])
        self.assertTrue(r.get_json()["slip"]["owsEditedAt"])
        # 다음 연동 틱 — 거래처명이 달라져 오고 메모는 비어 오고 입금이 들어왔다
        res = self.apply_slips([{"판매전표": "S260820-001", "거래처명": "원래이름", "판매일": "2026-08-20",
                                 "판매금액": "500000", "입금액": "500000", "입금확인": "1", "납부확인": "완납"}])
        self.assertEqual(res["protected"], 1)
        row = self.sql("SELECT customer, memo, paid_amount, paid_confirmed, paid_status FROM sale_slips "
                       "WHERE slip_no='S260820-001'")[0]
        self.assertEqual(row["customer"], "OWS이름", "★OWS에서 고친 칸이 덮였다")
        self.assertEqual(row["memo"], "OWS메모")
        self.assertEqual((row["paid_amount"], row["paid_confirmed"], row["paid_status"]), (500000, 1, "완납"))
        # 연동 전표의 입금 4칸은 OWS에서 못 고친다(연동이 되돌리므로)
        self.assertEqual(self.c.patch("/api/sale-slips/S260820-001", json={"paidAmount": 1}).status_code, 400)

    def test_안_고친_연동_전표는_TMS_값을_따라간다(self):
        """2026-09-03: 예전 '빈 칸만 채우기'는 입력 도중의 전표를 굳혀 헤더 수량·금액이 TMS와 달라졌다
        (S260831-001 OWS 6대/9,042,000 vs TMS 42대/15,017,400). 아무도 안 고친 연동 전표는 TMS가 정본."""
        self.apply_slips([{"판매전표": "S260820-002", "판매처명": "", "판매채널": "자사몰(업무관리)", "수량": "6",
                           "판매금액": "9042000", "송장번호": "123", "진행상태": "판매"}])
        res = self.apply_slips([{"판매전표": "S260820-002", "판매처명": "채움", "판매채널": "카카오", "수량": "42",
                                 "판매금액": "15017400", "송장번호": "", "진행상태": "판매완료"}])
        self.assertEqual((res["updated"], res["protected"]), (1, 0))
        row = self.sql("SELECT customer, channel, head_qty, sale_amount, waybill, stage FROM sale_slips "
                       "WHERE slip_no='S260820-002'")[0]
        self.assertEqual((row["customer"], row["channel"], row["head_qty"], row["sale_amount"]),
                         ("채움", "카카오", 42, 15017400), "★TMS 값을 따라가야 한다(빈 칸 채우기가 아니다)")
        self.assertEqual(row["waybill"], "", "TMS가 비운 칸도 따라간다")
        self.assertEqual(row["stage"], "판매완료")
        # 같은 값이 다시 오면 아무것도 안 바꾼다(멱등) — 행에 없는 칸은 손대지 않는다
        res = self.apply_slips([{"판매전표": "S260820-002", "판매처명": "채움", "수량": "42"}])
        self.assertEqual((res["updated"], res["skipped"]), (0, 1))
        self.assertEqual(self.sql("SELECT channel FROM sale_slips WHERE slip_no='S260820-002'")[0]["channel"], "카카오")

    def test_입금확인_True_False는_1과_0이다(self):
        """창구 JSON 은 bit 칸을 'True'/'False' 로 준다 — 숫자 변환만 하면 0이 되어 완납 전표가 미수금으로 잡혔다
        (2026-09-03 실측 S260831-001·S260903-001). 엑셀의 1/0 도 그대로."""
        self.apply_slips([{"판매전표": "S260831-001", "거래처명": "완납처", "판매일": "2026-08-31", "판매금액": "15017400",
                           "입금액": "15017400", "입금확인": "True", "납부확인": "완납"},
                          {"판매전표": "S260831-002", "거래처명": "미수처", "판매일": "2026-08-31", "판매금액": "1000",
                           "입금액": "0", "입금확인": "False", "납부확인": "미수"},
                          {"판매전표": "S260831-003", "거래처명": "엑셀처", "판매일": "2026-08-31", "판매금액": "1000",
                           "입금액": "1000", "입금확인": 1, "납부확인": "완납"}])
        rows = {r["slip_no"]: r["paid_confirmed"] for r in self.sql("SELECT slip_no, paid_confirmed FROM sale_slips")}
        self.assertEqual((rows["S260831-001"], rows["S260831-002"], rows["S260831-003"]), (1, 0, 1))
        rc = self.c.get("/api/sale-slips/receivables").get_json()
        self.assertEqual([c["customer"] for c in rc["customers"]], ["미수처"], "완납 전표가 미수금에 섞였다")
        # 다시 와도 값이 흔들리지 않는다(멱등)
        res = self.apply_slips([{"판매전표": "S260831-001", "거래처명": "완납처", "판매일": "2026-08-31",
                                 "판매금액": "15017400", "입금액": "15017400", "입금확인": "True", "납부확인": "완납"}])
        self.assertEqual(res["updated"], 0)


    def test_OWS에서_고친_명세는_연동이_안_덮는다(self):
        self.asset("260901-0001")
        no = self.slip([self.line("260901-0001", 500000)]).get_json()["slipNo"]
        res = self.upsert([{"판매전표": no, "관리번호": "260901-0001", "수령자성함": "TMS",
                            "판매채널": "자사몰", "판매가": "1", "진행상태": "판매취소"}])
        self.assertEqual((res["created"], res["updated"], res["protected"]), (0, 0, 1))
        row = self.sql("SELECT sale_price, stage, customer FROM tms_sales WHERE slip_no=?", no)[0]
        self.assertEqual((row["sale_price"], row["stage"], row["customer"]), (500000, "판매", ""))

    def test_OWS_전표에_팔린_자산이_다른_TMS_전표로_오면_새_명세를_안_만든다(self):
        self.asset("260901-0001")
        self.slip([self.line("260901-0001", 500000)])
        res = self.upsert([{"판매전표": "S260902-003", "관리번호": "260901-0001", "수령자성함": "TMS",
                            "판매채널": "자사몰", "판매가": "500000", "진행상태": "판매"},
                           {"판매전표": "S260902-003", "관리번호": "260901-0009", "수령자성함": "TMS",
                            "판매채널": "자사몰", "판매가": "1", "진행상태": "판매"}])
        self.assertEqual((res["created"], res["protected"]), (1, 1))
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM tms_sales WHERE asset_no='260901-0001'")[0]["n"], 1,
                         "같은 기계가 두 전표에 팔린 것으로 잡혔다(매출 이중)")

    def test_옵션가_헤더_매핑(self):
        from app.purchase.sales import SALE_ASSET_MAP
        for h, col in (("업그레이드1가", "upgrade1_price"), ("업그레이드2가", "upgrade2_price"),
                       ("탈거가", "removal_price"), ("기타구성가", "extra_price"),
                       ("충전기가", "charger_price"), ("포장료", "packing_fee"),
                       ("판매부가세", "sale_vat"), ("판매수수료", "sale_fee"),
                       ("매입부가세", "buy_vat"), ("판매수수율", "fee_rate"),
                       ("업그레이드1항목", "upgrade1_item"), ("업그레이드2항목", "upgrade2_item"),
                       ("탈거항목", "removal_item"), ("기타구성품", "extra_items"),
                       ("상세취소일", "cancel_date"), ("상세상태", "stage")):
            self.assertEqual(SALE_ASSET_MAP.get(h), col, h)
        res = self.upsert([{"판매전표": "S260820-001", "관리번호": "260901-0001", "수령자성함": "구매자",
                            "판매채널": "자사몰", "판매가": "500,000", "진행상태": "판매",
                            "업그레이드1가": "30000", "업그레이드1항목": "RAM 16G",
                            "업그레이드2가": "0", "탈거가": "10000", "탈거항목": "HDD",
                            "기타구성가": "5000", "기타구성품": "가방", "충전기가": "20000",
                            "포장료": "3000", "판매부가세": "50000", "판매수수료": "15000",
                            "매입부가세": "35800", "판매수수율": "3", "순이익": "120800",
                            "상세취소일": "", "상세상태": "판매"}])
        self.assertEqual(res["created"], 1)
        row = self.sql("SELECT * FROM tms_sales WHERE slip_no='S260820-001'")[0]
        self.assertEqual(row["sale_price"], 500000)
        self.assertEqual((row["upgrade1_price"], row["upgrade1_item"]), (30000, "RAM 16G"))
        self.assertEqual((row["removal_price"], row["removal_item"]), (10000, "HDD"))
        self.assertEqual((row["extra_price"], row["extra_items"]), (5000, "가방"))
        self.assertEqual((row["charger_price"], row["packing_fee"]), (20000, 3000))
        self.assertEqual((row["sale_vat"], row["sale_fee"], row["buy_vat"], row["fee_rate"]), (50000, 15000, 35800, 3.0))
        self.assertEqual((row["stage"], row["cancel_date"]), ("판매", ""))
        # 연동 라인의 순이익은 TMS 값이 정본 — 전표 상세도 그 값을 보여 준다
        self.apply_slips([{"판매전표": "S260820-001", "판매금액": "500000", "진행상태": "판매"}])
        l = self.c.get("/api/sale-slips/S260820-001").get_json()["lines"][0]
        self.assertEqual((l["profit"], l["profitSrc"], l["buyVat"], l["optionCost"]), (120800, "tms", 35800, 45000))
        # 상세상태·상세취소일이 오면 상태가 따라온다(엑셀 날짜 숫자도)
        res = self.upsert([{"판매전표": "S260820-001", "관리번호": "260901-0001", "수령자성함": "구매자",
                            "판매채널": "자사몰", "판매가": "500000", "진행상태": "판매",
                            "상세취소일": "2026-08-25", "상세상태": "판매취소"}])
        self.assertEqual(res["updated"], 1)
        row = self.sql("SELECT stage, cancel_date, upgrade1_price FROM tms_sales WHERE slip_no='S260820-001'")[0]
        self.assertEqual((row["stage"], row["cancel_date"]), ("판매취소", "2026-08-25"))
        self.assertEqual(row["upgrade1_price"], 30000, "헤더가 없는 칸을 건드렸다")

    def test_헤더가_없는_행은_예전처럼_동작(self):
        res = self.upsert([{"판매전표": "S260820-001", "관리번호": "260901-0001", "수령자성함": "구매자",
                            "판매채널": "자사몰", "모델명": "L480", "판매일": "2026-08-20 10:00",
                            "매입가": "300000", "판매가": "550000", "순이익": "180000", "등급": "AA",
                            "진행상태": "판매"}])
        self.assertEqual((res["created"], res["updated"], res["skipped"], res["protected"]), (1, 0, 0, 0))
        row = self.sql("SELECT * FROM tms_sales WHERE slip_no='S260820-001'")[0]
        self.assertEqual(row["sale_price"], 550000)
        self.assertEqual((row["upgrade1_price"], row["buy_vat"], row["fee_rate"]), (0, 0, 0))
        self.assertEqual(row["cancel_date"], "")
        self.assertEqual(row["source"], "tms")
        self.assertEqual(row["ows_edited_at"], "")
        # 정정도 예전처럼 따라온다
        res = self.upsert([{"판매전표": "S260820-001", "관리번호": "260901-0001", "수령자성함": "정정",
                            "판매채널": "자사몰", "판매가": "600000", "진행상태": "판매"}])
        self.assertEqual(res["updated"], 1)
        self.assertEqual(self.sql("SELECT sale_price FROM tms_sales WHERE slip_no='S260820-001'")[0]["sale_price"],
                         600000)


class TestPermsAndScreen(Base):
    def _worker(self, perms):
        r = self.c.post("/api/users", json={
            "username": "worker1", "displayName": "worker1", "password": USER_PW,
            "perms": perms, "categoryIds": [], "allCategories": True, "isAdmin": False})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        self.assertEqual(c.post("/api/auth/login", json={"username": "worker1", "password": USER_PW}).status_code, 200)
        return c

    def test_금액_권한_없으면_403(self):
        self.asset("260901-0001")
        w = self._worker(["purchase.view", "purchase.edit"])
        self.assertEqual(w.post("/api/sale-slips", json={"lines": [self.line("260901-0001")]}).status_code, 403)
        self.assertEqual(w.post("/api/sale-slips/check-assets", json={"assetNos": ["260901-0001"]}).status_code, 403)
        self.assertEqual(w.get("/api/sale-slips/rounds?assetNo=260901-0001").status_code, 403)
        no = self.slip([self.line("260901-0001")]).get_json()["slipNo"]
        self.assertEqual(w.get(f"/api/sale-slips/{no}").status_code, 403)
        self.assertEqual(w.post(f"/api/sale-slips/{no}/uncancel").status_code, 403)
        self.assertEqual(w.post(f"/api/sale-slips/{no}/lines/260901-0001/unreturn").status_code, 403)
        # 등록 권한 없이 금액만 있으면 읽기만
        r = self.c.post("/api/users", json={
            "username": "worker2", "displayName": "worker2", "password": USER_PW,
            "perms": ["purchase.view", "purchase.money"], "categoryIds": [], "allCategories": True,
            "isAdmin": False})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c2 = self.app.test_client()
        c2.post("/api/auth/login", json={"username": "worker2", "password": USER_PW})
        self.assertEqual(c2.get(f"/api/sale-slips/{no}").status_code, 200)
        self.assertEqual(c2.patch(f"/api/sale-slips/{no}", json={"memo": "x"}).status_code, 403)
        self.assertEqual(c2.post(f"/api/sale-slips/{no}/uncancel").status_code, 403)

    def test_정적_경로가_전표번호_경로에_안_먹힌다(self):
        # /sale-slips/receivables · /sale-slips/import · /sale-slips/rounds 가 <slip_no> 로 오인되면 안 된다
        self.assertEqual(self.c.get("/api/sale-slips/receivables").status_code, 200)
        self.assertEqual(self.c.get("/api/sale-slips/rounds").status_code, 200)
        self.assertEqual(self.c.get("/api/sale-slips/없는전표").status_code, 404)
        self.assertEqual(self.c.post("/api/sale-slips/import").status_code, 400)

    def test_화면_배선(self):
        js = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")
        for needle in ('id="ss-new"', "openSaleSlipForm", "openSaleSlipDetail", "data-ssopen",
                       "/api/sale-slips/check-assets", "function slipSourceChip",
                       "OWS 등록", "OWS 수정", "TMS 연동", "askSaleRelease", "/return",
                       'id="ss-upload"', "/api/sale-slips/import",
                       # 되돌리기 · 회전 이력(2026-09-03)
                       'id="sd2-uncancel-slip"', "/uncancel", "/unreturn", "data-lunret", "data-luncancel",
                       "owsUndoable", "/api/sale-slips/rounds", "회차", "function saleLineCalc"):
            self.assertIn(needle, js, needle)
        # 옵션 원가 칸 목록·부가세율·기본 수수율은 서버와 같아야 한다(순이익 미리보기 = 저장값)
        from app.purchase.sale_entry import LINE_OPTION_COST_COLS, SALE_FEE_RATE_DEFAULT, SALE_VAT_RATE
        blk = js.split("const SALE_OPTION_COST_KEYS = [", 1)[1].split("];", 1)[0]
        keys = [k.strip().strip('"') for k in blk.replace("\n", "").split(",") if k.strip()]
        camel = {"upgrade1_price": "upgrade1Price", "upgrade2_price": "upgrade2Price",
                 "extra_price": "extraPrice", "charger_price": "chargerPrice"}
        self.assertEqual(keys, [camel[c] for c in LINE_OPTION_COST_COLS])
        self.assertEqual(float(re.search(r"const SALE_VAT_RATE = ([\d.]+);", js).group(1)), SALE_VAT_RATE)
        self.assertEqual(float(re.search(r"const SALE_FEE_RATE = ([\d.]+);", js).group(1)), SALE_FEE_RATE_DEFAULT)
        # 옛 계약(옵션가를 매출에 더하던 SALE_TOTAL_KEYS)은 남아 있으면 안 된다
        self.assertNotIn("SALE_TOTAL_KEYS", js)
        self.assertNotIn("lineTotal", js)

    def test_자산_상세_판매_정보는_회차_전부(self):
        """§5-5(2026-09-03): 자산 상세 💻 판매 정보가 rounds 창구로 회차 전부를 그린다(서버 tmsSale 은 최신 1행 그대로 — 마진 줄용)."""
        # 서버: 반입→재판매 자산의 상세는 tmsSale(취소 아닌 최신 1행)을 유지하고, rounds 는 행 전부를 준다
        aid = self.asset("260901-0001")
        first = self.slip([self.line("260901-0001", 500000)], saleDate="2026-08-01").get_json()["slipNo"]
        self.c.post(f"/api/sale-slips/{first}/lines/260901-0001/return",
                    json={"reason": "변심", "returnDate": "2026-08-05", "restock": True})
        second = self.slip([self.line("260901-0001", 430000)], saleDate="2026-08-20").get_json()["slipNo"]
        d = self.c.get(f"/api/assets/{aid}").get_json()
        self.assertEqual((d["tmsSale"]["slipNo"], d["tmsSale"]["salePrice"]), (second, 430000))
        rr = self.c.get(f"/api/sale-slips/rounds?assetNo={d['assetNo']}").get_json()["assets"]["260901-0001"]
        self.assertEqual((rr["count"], [x["round"] for x in rr["rows"]]), (2, [1, 2]))
        # 화면: 자산 상세가 rounds 창구를 부르고 회차 표를 그린다(예전 최신 1행 카드는 권한·오류 폴백)
        js = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")
        detail = js.split("async function renderAssetDetail(", 1)[1].split("\nasync function ", 1)[0]
        for needle in ("/api/sale-slips/rounds?assetNo=", "assetSaleInfoHtml(a, rounds)"):
            self.assertIn(needle, detail, needle)
        fn = js.split("function assetSaleInfoHtml(", 1)[1].split("\nfunction ", 1)[0]
        for needle in ("회차", "반입일", "취소일", "rounds.count", "a.tmsSale"):
            self.assertIn(needle, fn, needle)


if __name__ == "__main__":
    unittest.main()


class TestSaleCostSnapshot(Base):
    """판매 시점 원가 스냅샷(2026-09-03 대표 승인 "원가 기준으로 실어올 건데 …
    TMS 값을 그대로 살려오는 게 낫겠다").

    ★왜 스냅샷이어야 하나 — 자산의 매입가는 나중에 고쳐지고 수리·부품비는 팔린 뒤에도 붙는다.
      '지금 값'으로 마진을 다시 계산하면 지난달 장부가 오늘 조용히 바뀐다(라이브에서 이미
      151행이 판매 당시 매입가와 다르다). 그래서 판매하던 그 순간의 제조원가를 얼려 둔다.
    ★TMS를 끊는 날 이 칸이 대조 기준이 된다 — 없으면 "OWS 숫자가 맞다"를 증명할 길이 없다.
    """

    def _tms_row(self, **kw):
        """연동(창구 판매현황)에서 온 행 한 줄을 그대로 반영한다."""
        from app.purchase.sales import upsert_asset_sales
        from app.db import tx
        row = {"판매전표": "S260824-001", "관리번호": "260819-0023", "수령자성함": "최단비",
               "판매채널": "카카오", "판매일": "2026-08-24", "진행상태": "판매",
               "매입가": "77000", "판매가": "290000", "순이익": "65550"}
        row.update(kw)
        with self.app.app_context():
            with tx(write=True) as conn:
                return upsert_asset_sales(conn, [row], actor="시험")

    def test_창구가_보내는_원가_칸이_그대로_저장된다(self):
        self._tms_row(제조원가="207500", 수리비="0", 부품비="0",
                      매입부가세="20750", 실부가세="8250", 판매수수료="8700")
        r = self.sql("SELECT * FROM tms_sales WHERE asset_no='260819-0023'")[0]
        self.assertEqual(r["manufacture_cost"], 207500)
        self.assertEqual(r["buy_vat"], 20750)
        self.assertEqual(r["net_vat"], 8250)
        self.assertEqual(r["sale_fee"], 8700)
        self.assertEqual(r["tms_profit"], 65550, "TMS 순이익은 받은 그대로여야 한다")

    def test_원가_칸이_안_오면_옛_행이_그대로다(self):
        """창구가 아직 그 칸을 안 싣던 시절의 행이 0으로 덮이면 안 된다."""
        self._tms_row(제조원가="207500")
        self._tms_row()                       # 제조원가 헤더 없이 다시 옴
        r = self.sql("SELECT manufacture_cost FROM tms_sales WHERE asset_no='260819-0023'")[0]
        self.assertEqual(r["manufacture_cost"], 207500, "없는 칸을 0으로 덮었다")

    def _calc(self):
        """이 명세 한 줄을 서버 산식으로 계산한다(전표 헤더 없이 라인만 본다)."""
        from app.db import tx
        from app.purchase.sale_entry import _line_calc, _slip_lines
        with self.app.app_context():
            with tx() as conn:
                return _line_calc(_slip_lines(conn, "S260824-001")[0])

    def test_라인_원가는_판매_시점_제조원가를_쓴다(self):
        """자산의 지금 매입가가 아니라 팔던 순간의 원가여야 한다."""
        self.asset("260819-0023", price=77000)          # OWS 자산은 매입가 77,000
        self._tms_row(제조원가="207500", 판매수수료="8700", 실부가세="8250")
        c = self._calc()
        self.assertEqual(c["total_cost"], 207500,
                         "자산의 지금 매입가(77,000)로 세면 지난 마진이 오늘 바뀐다")
        self.assertEqual(c["profit"], 65550, "TMS 순이익이 정본이다")
        self.assertEqual(c["src"], "tms")

    def test_제조원가가_없으면_명세_매입가로_물러선다(self):
        self.asset("260819-0023", price=77000)
        self._tms_row()                                  # 제조원가 없음
        self.assertEqual(self._calc()["total_cost"], 77000)

    def test_옵션가는_판매_시점_원가에_더해진다(self):
        self.asset("260819-0023", price=77000)
        self._tms_row(제조원가="200000", 업그레이드1가="45000", 탈거가="90000", 포장료="4000")
        # 200,000 + (45,000 − 90,000) + 4,000 = 159,000
        self.assertEqual(self._calc()["total_cost"], 159000)

    def test_자산_상세_마진이_판매_시점_원가로_계산된다(self):
        aid = self.asset("260819-0023", price=77000)
        self._tms_row(제조원가="207500", 판매수수료="8700", 실부가세="8250")
        s = self.c.get(f"/api/assets/{aid}").get_json()["tmsSale"]
        self.assertEqual(s["cost"], 207500)
        self.assertEqual(s["costSource"], "snapshot")
        self.assertEqual(s["margin"], 290000 - 207500)
        self.assertEqual(s["tmsProfit"], 65550)
        # 화면이 "여기서 수수료·실부가세를 더 빼면 TMS 순이익"이라 말할 수 있어야 한다
        self.assertEqual(s["margin"] - s["saleFee"] - s["netVat"], s["tmsProfit"])

    def test_스냅샷이_없으면_옛_순서_그대로_물러선다(self):
        """★스냅샷만 앞에 끼워 넣는다 — 자산 → 명세 순서는 예전 그대로여야 한다
        (뒤집으면 우리 자산인데 남의 매입가로 세게 된다)."""
        aid = self.asset("260819-0023", price=77000)
        self._tms_row(매입가="300000")            # 명세 매입가는 다른 값
        s = self.c.get(f"/api/assets/{aid}").get_json()["tmsSale"]
        self.assertEqual(s["costSource"], "asset")
        self.assertEqual(s["cost"], 77000, "우리 자산의 매입가를 써야 한다")

    def test_우리_자산이_아니면_명세_매입가로_센다(self):
        from app.db import tx
        aid = self.asset("260819-0023", price=77000)
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE assets SET purchase_price=0 WHERE id=?", (aid,))
        self._tms_row(매입가="300000")
        s = self.c.get(f"/api/assets/{aid}").get_json()["tmsSale"]
        self.assertEqual((s["costSource"], s["cost"]), ("slip", 300000))

    def test_OWS가_고친_명세는_원가를_덮지_않는다(self):
        from app.db import tx
        self._tms_row(제조원가="207500")
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE tms_sales SET ows_edited_at='2026-09-03' "
                             "WHERE asset_no='260819-0023'")
        self._tms_row(제조원가="999999")
        r = self.sql("SELECT manufacture_cost FROM tms_sales WHERE asset_no='260819-0023'")[0]
        self.assertEqual(r["manufacture_cost"], 207500, "사람이 고친 명세를 연동이 덮었다")

    def test_1회_채우기가_창구_전량을_판매명세로만_돌린다(self):
        """★자산 빈칸 채우기·상태 갱신까지 태우면 1만 3천 행에 자산 이력이 요동친다."""
        src = (Path(__file__).resolve().parent.parent / "app" / "purchase" / "tms_link.py"
               ).read_text("utf-8")
        blk = src.split("sale_cost_backfill", 1)[1][:1200]
        self.assertIn('names="판매현황", since=None', blk, "전량으로 받지 않는다")
        self.assertIn("upsert_asset_sales(conn, rows", blk,
                      "apply_screen 을 태우면 자산 이관까지 돈다")
        self.assertIn('"제조원가" in rows[0]', blk, "칸이 없는 창구에서도 돌면 0으로 덮는다")
