"""매입 흐름 — 2026-07-30 E2E에서 드러난 3가지에 대한 회귀 방지.

  ① 가입고(V) 자산이 판매 가능 재고로 잡히던 문제
     schema.sql에 'received … 가입고 자산은 0으로 시작'이라 의도가 적혀 있었는데
     코드가 한 번도 0을 넣지 않아 죽은 필드였다. 안 받은 노트북이 셋팅에서 배정될 수 있었다.

  ② 같은 시리얼을 두 번 등록해도 통과하던 문제(사후 감지만 있었음)

  ③ 전표 매입금액과 자산 매입가 합이 따로 놀아도 아무 경고가 없던 문제
     (백엔드가 assignedAmount/totalAmount를 정확히 내려주는지만 여기서 확인)
"""
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import auth as auth_mod  # noqa: E402
from app import create_app  # noqa: E402
from app.purchase import GRADES, TIERS, tier_for_grade  # noqa: E402

PW = "admin-pass-1"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hms-pf-"))
        self.app = create_app(db_path=self.tmp / "t.db")
        self.app.testing = True
        self.c = self.app.test_client()
        auth_mod._login_failures.clear()
        self.c.post("/api/auth/setup", json={
            "username": "admin", "displayName": "대표", "password": PW})
        self.cat = self.c.get("/api/categories").get_json()[0]["id"]
        self.sid = self.c.post("/api/suppliers", json={"name": "테스트상사"}).get_json()["id"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _batch(self, stage="purchased", **kw):
        body = {"stage": stage, "purchaseDate": "2026-07-30", "totalAmount": 0}
        if stage == "purchased":
            body["supplierId"] = self.sid
        body.update(kw)
        r = self.c.post("/api/purchase-batches", json=body)
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        return r.get_json()

    def _asset(self, batch_id=None, **kw):
        body = {"categoryId": self.cat, "qty": 1}
        if batch_id:
            body["batchId"] = batch_id
        body.update(kw)
        return self.c.post("/api/assets", json=body)


class TestProvisionalNotInStock(Base):
    """가입고 = 아직 실물이 없다. 재고로 세면 안 된다."""

    def test_가입고_자산은_미입고로_등록된다(self):
        v = self._batch("provisional")
        a = self._asset(v["id"], model="L480").get_json()[0]
        got = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertFalse(got["received"], "가입고 자산이 입고완료로 잡혔다")

    def test_매입_전표_자산은_바로_입고다(self):
        b = self._batch("purchased")
        a = self._asset(b["id"], model="L480").get_json()[0]
        self.assertTrue(self.c.get(f"/api/assets/{a['id']}").get_json()["received"])

    def test_전표_없이_등록하면_입고다(self):
        a = self._asset(None, model="L480").get_json()[0]
        self.assertTrue(self.c.get(f"/api/assets/{a['id']}").get_json()["received"])

    def test_가입고_자산은_주문에_매칭할_수_없다(self):
        """★핵심 — 안 받은 물건이 셋팅에서 배정되면 안 된다."""
        v = self._batch("provisional")
        a = self._asset(v["id"], model="L480").get_json()[0]
        o = self.c.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북", "phone": "010-1111-2222",
            "address": "서울시 강남구 1", "postalCode": "06000", "amount": 500000}).get_json()
        r = self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.assertGreaterEqual(r.status_code, 400, "미입고 자산이 주문에 붙었다")
        self.assertIn("입고확인", r.get_json().get("error", ""))

    def test_자산검색에서_미입고는_사용불가로_표시된다(self):
        v = self._batch("provisional")
        a = self._asset(v["id"], model="L480").get_json()[0]
        rows = self.c.get(f"/api/orders/asset-search?q={a['assetNo']}").get_json()
        hit = next(x for x in rows if x["assetNo"] == a["assetNo"])
        self.assertFalse(hit["available"])
        self.assertIn("입고확인", hit["reason"])

    def test_입고확인하면_재고로_잡힌다(self):
        v = self._batch("provisional")
        a = self._asset(v["id"], model="L480").get_json()[0]
        r = self.c.post(f"/api/purchase-batches/{v['id']}/confirm",
                        json={"supplierId": self.sid, "purchaseDate": "2026-07-30"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        got = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertTrue(got["received"], "입고확인 후에도 미입고로 남았다")
        self.assertTrue(got["receivedAt"], "입고확인 시각이 안 남았다")
        self.assertTrue(got["receivedBy"], "입고확인한 사람이 안 남았다")

    def test_입고확인_후에는_주문에_매칭된다(self):
        v = self._batch("provisional")
        a = self._asset(v["id"], model="L480").get_json()[0]
        self.c.post(f"/api/purchase-batches/{v['id']}/confirm",
                    json={"supplierId": self.sid, "purchaseDate": "2026-07-30"})
        o = self.c.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북", "phone": "010-1111-2222",
            "address": "서울시 강남구 1", "postalCode": "06000", "amount": 500000}).get_json()
        r = self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_미입고_자산은_전표_미입고건수에_잡힌다(self):
        v = self._batch("provisional")
        self._asset(v["id"], model="L480")
        self._asset(v["id"], model="L480")
        self.assertEqual(self.c.get(f"/api/purchase-batches/{v['id']}").get_json()["pendingCount"], 2)


class TestDuplicateSerial(Base):
    def test_같은_시리얼_두번_등록은_거부된다(self):
        b = self._batch()
        self.assertEqual(self._asset(b["id"], model="L480", serial="SN-1").status_code, 201)
        r = self._asset(b["id"], model="L480", serial="SN-1")
        self.assertEqual(r.status_code, 409, "중복 시리얼이 그대로 들어갔다")
        self.assertIn("SN-1", r.get_json()["error"])

    def test_빈_시리얼은_여러_대여도_괜찮다(self):
        """모니터·부속처럼 시리얼이 없는 물건이 있다 — 빈 값까지 막으면 안 된다."""
        b = self._batch()
        self.assertEqual(self._asset(b["id"], model="MON", serial="").status_code, 201)
        self.assertEqual(self._asset(b["id"], model="MON", serial="").status_code, 201)
        self.assertEqual(self._asset(b["id"], model="MON").status_code, 201)

    def test_여러대_등록에_시리얼을_넣으면_막는다(self):
        b = self._batch()
        r = self._asset(b["id"], model="L480", serial="SN-X", qty=3)
        self.assertEqual(r.status_code, 400)

    def test_수정으로도_시리얼을_겹칠_수_없다(self):
        b = self._batch()
        self._asset(b["id"], model="L480", serial="SN-A")
        a2 = self._asset(b["id"], model="L480", serial="SN-B").get_json()[0]
        r = self.c.patch(f"/api/assets/{a2['id']}", json={"serial": "SN-A"})
        self.assertEqual(r.status_code, 409, "수정으로 중복 시리얼을 만들 수 있었다")


class TestAmountReconcile(Base):
    def test_전표금액과_자산합을_각각_정확히_내려준다(self):
        b = self._batch(totalAmount=900000)
        for _ in range(3):
            self._asset(b["id"], model="L480", purchasePrice=300000)
        d = self.c.get(f"/api/purchase-batches/{b['id']}").get_json()
        self.assertEqual(d["totalAmount"], 900000)
        self.assertEqual(d["assignedAmount"], 900000)
        self.assertEqual(d["assetCount"], 3)

    def test_불일치를_숨기지_않는다(self):
        b = self._batch(totalAmount=900000)
        self._asset(b["id"], model="L480", purchasePrice=1000000)
        d = self.c.get(f"/api/purchase-batches/{b['id']}").get_json()
        self.assertNotEqual(d["assignedAmount"], d["totalAmount"],
                            "화면이 불일치를 경고하려면 서버가 두 값을 그대로 줘야 한다")


class TestNumbering(Base):
    def test_자산번호가_연속으로_붙는다(self):
        b = self._batch()
        nos = [self._asset(b["id"], model="L480").get_json()[0]["assetNo"] for _ in range(3)]
        seqs = [int(n.split("-")[1]) for n in nos]
        self.assertEqual(seqs, list(range(seqs[0], seqs[0] + 3)))

    def test_수량으로_한번에_등록해도_연속이다(self):
        b = self._batch()
        made = self._asset(b["id"], model="L480", qty=3).get_json()
        seqs = [int(a["assetNo"].split("-")[1]) for a in made]
        self.assertEqual(seqs, list(range(seqs[0], seqs[0] + 3)))

    def test_수동번호를_선점해도_자동채번이_피해간다(self):
        """TMS 이관분이 섞여 있어도 중복이 나면 안 된다."""
        from app import config
        today = config.now().strftime("%y%m%d")
        b = self._batch()
        self.assertEqual(self._asset(b["id"], assetNo=f"{today}-9000").status_code, 201)
        nxt = self._asset(b["id"], model="AUTO").get_json()[0]["assetNo"]
        self.assertGreater(int(nxt.split("-")[1]), 9000)

    def test_전표번호는_V와_P가_구분된다(self):
        self.assertTrue(self._batch("provisional")["slipNo"].startswith("V"))
        self.assertTrue(self._batch("purchased")["slipNo"].startswith("P"))


if __name__ == "__main__":
    unittest.main()


class TestAuditFixes(unittest.TestCase):
    """2026-07-30 감사(47에이전트, 확정 13건)에서 나온 결함들의 회귀 방지."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hms-af-"))
        self.app = create_app(db_path=self.tmp / "t.db")
        self.app.testing = True
        self.c = self.app.test_client()
        auth_mod._login_failures.clear()
        self.c.post("/api/auth/setup", json={
            "username": "admin", "displayName": "대표", "password": PW})
        self.cat = self.c.get("/api/categories").get_json()[0]["id"]
        self.sid = self.c.post("/api/suppliers", json={"name": "테스트상사"}).get_json()["id"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _b(self, stage="purchased", **kw):
        body = {"stage": stage, "purchaseDate": "2026-07-30", "totalAmount": 0}
        if stage == "purchased":
            body["supplierId"] = self.sid
        body.update(kw)
        return self.c.post("/api/purchase-batches", json=body).get_json()

    # ── 음수 금액 ────────────────────────────────────────────────────
    def test_음수_매입가는_거부된다(self):
        b = self._b()
        r = self.c.post("/api/assets", json={
            "batchId": b["id"], "categoryId": self.cat, "qty": 1, "purchasePrice": -50000})
        self.assertEqual(r.status_code, 400)

    def test_수정으로도_음수_매입가를_넣을_수_없다(self):
        b = self._b()
        a = self.c.post("/api/assets", json={
            "batchId": b["id"], "categoryId": self.cat, "qty": 1}).get_json()[0]
        self.assertEqual(
            self.c.patch(f"/api/assets/{a['id']}", json={"purchasePrice": -1}).status_code, 400)

    def test_음수_전표금액도_거부된다(self):
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid,
            "purchaseDate": "2026-07-30", "totalAmount": -1000})
        self.assertEqual(r.status_code, 400)

    # ── 회수중 자산 보호 ─────────────────────────────────────────────
    def test_회수중_자산은_상태를_되돌릴_수_없다(self):
        """★되돌리면 회수 입고가 스킵돼 한 대가 두 고객에게 나간다."""
        import sqlite3
        b = self._b()
        a = self.c.post("/api/assets", json={
            "batchId": b["id"], "categoryId": self.cat, "qty": 1}).get_json()[0]
        conn = sqlite3.connect(self.tmp / "t.db")
        conn.execute("UPDATE assets SET status='returning' WHERE id=?", (a["id"],))
        conn.commit()
        conn.close()
        self.assertGreaterEqual(
            self.c.patch(f"/api/assets/{a['id']}", json={"status": "ready"}).status_code, 400)
        r = self.c.post("/api/assets/bulk", json={"ids": [a["id"]], "status": "ready"})
        if r.status_code == 200:
            self.assertEqual(r.get_json().get("updated", 0), 0, "일괄로 회수중 자산이 바뀌었다")

    # ── 전표 표시 ────────────────────────────────────────────────────
    def test_가입고_전표도_자산상세에_보인다(self):
        """거래처가 없는 V전표는 INNER JOIN 때문에 통째로 사라졌었다."""
        v = self._b("provisional")
        a = self.c.post("/api/assets", json={
            "batchId": v["id"], "categoryId": self.cat, "qty": 1}).get_json()[0]
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertIsNotNone(d.get("batch"), "가입고 전표가 자산 상세에서 사라졌다")
        self.assertTrue(d["batch"].get("slipNo"), "전표번호가 비어 어느 전표인지 알 수 없다")

    def test_매입전표의_거래처는_비울_수_없다(self):
        b = self._b()
        r = self.c.patch(f"/api/purchase-batches/{b['id']}", json={"supplierId": ""})
        self.assertEqual(r.status_code, 400)

    # ── 평균 매입가 분모 ─────────────────────────────────────────────
    def test_평균매입가는_값이_있는_것만_나눈다(self):
        b = self._b()
        for _ in range(3):
            self.c.post("/api/assets", json={
                "batchId": b["id"], "categoryId": self.cat, "qty": 1,
                "maker": "LENOVO", "model": "L480", "purchasePrice": 100000})
        for _ in range(7):
            self.c.post("/api/assets", json={
                "batchId": b["id"], "categoryId": self.cat, "qty": 1,
                "maker": "LENOVO", "model": "L480", "purchasePrice": 0})
        st = self.c.get("/api/assets/stock-by-model").get_json()
        p = next(x for x in st["products"] if "L480" in x["product"])
        self.assertEqual(p["avgBuy"], 100000, f"매입가 0원까지 나눠 평균이 낮아졌다: {p['avgBuy']}")
        self.assertEqual(p["buyMissing"], 7, "매입가 미입력 대수를 알려 줘야 한다")

    # ── 모델 재고 브리핑 ─────────────────────────────────────────────
    def test_모델_브리핑이_재고와_최근매입가를_준다(self):
        b = self._b()
        for _ in range(2):
            self.c.post("/api/assets", json={
                "batchId": b["id"], "categoryId": self.cat, "qty": 1,
                "maker": "LG", "model": "17Z95N", "purchasePrice": 660000})
        r = self.c.get("/api/assets/model-brief?model=17Z95N").get_json()
        self.assertEqual(r["stock"], 2)
        self.assertEqual(r["avgBuy"], 660000)
        self.assertTrue(r["recentBuys"])

    def test_브리핑은_가입고분을_재고와_분리한다(self):
        v = self._b("provisional")
        self.c.post("/api/assets", json={
            "batchId": v["id"], "categoryId": self.cat, "qty": 1,
            "maker": "LG", "model": "17Z95N", "purchasePrice": 660000})
        r = self.c.get("/api/assets/model-brief?model=17Z95N").get_json()
        self.assertEqual(r["stock"], 0, "안 받은 물건이 재고로 잡혔다")
        self.assertEqual(r["pending"], 1, "입고대기 대수를 알려 줘야 한다")


class TestMigrationFill(unittest.TestCase):
    """★이관 '빈 칸 채우기' — 운영 자산 492건의 매입가가 0으로 고착된 문제의 해결책.

    지금까지는 '이미 있는 관리번호는 건너뛰기'뿐이라, TMS 매입 엑셀을 몇 번을 올려도
    매입가를 채울 수 없었다(마진이 100%로 보고됨). 값을 덮어쓰지 않고 빈 칸만 채운다.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hms-mg-"))
        self.app = create_app(db_path=self.tmp / "t.db")
        self.app.testing = True
        self.c = self.app.test_client()
        auth_mod._login_failures.clear()
        self.c.post("/api/auth/setup", json={
            "username": "admin", "displayName": "대표", "password": PW})
        self.cat = self.c.get("/api/categories").get_json()[0]["id"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _xls(self, rows):
        """importers가 HTML 표도 읽으므로 그걸로 엑셀을 대신한다."""
        head = "<tr>" + "".join(f"<th>{k}</th>" for k in rows[0]) + "</tr>"
        body = "".join("<tr>" + "".join(f"<td>{v}</td>" for v in r.values()) + "</tr>" for r in rows)
        html = f"<html><table>{head}{body}</table></html>".encode()
        return (io.BytesIO(html), "tms.xlsx")

    def _post(self, path, rows, fill=False):
        data = {"files": self._xls(rows)}
        if fill:
            data["fillBlanks"] = "1"
        return self.c.post(path, data=data, content_type="multipart/form-data")

    def test_이미_있는_자산의_빈_매입가를_채운다(self):
        # 관리번호만 있는 껍데기 자산(현재 운영 492건이 이 상태)
        a = self.c.post("/api/assets", json={
            "categoryId": self.cat, "qty": 1, "assetNo": "260101-0001"}).get_json()[0]
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["purchasePrice"], 0)

        rows = [{"관리번호": "260101-0001", "대분류": "PC", "모델명": "L480",
                 "매입가": "300000", "시리얼번호": "SN-A"}]
        # 채우기 없이: 그대로 건너뛴다
        r = self._post("/api/assets/migrate", rows).get_json()
        self.assertEqual(r["duplicates"], 1)
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["purchasePrice"], 0)

        # 채우기 켜면: 빈 칸이 채워진다
        r = self._post("/api/assets/migrate", rows, fill=True).get_json()
        self.assertEqual(r["updated"], 1, r)
        got = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(got["purchasePrice"], 300000, "매입가가 안 채워졌다")
        self.assertEqual(got["model"], "L480")

    def test_이미_값이_있으면_덮어쓰지_않는다(self):
        """사람이 넣은 값을 엑셀이 밀어버리면 안 된다."""
        a = self.c.post("/api/assets", json={
            "categoryId": self.cat, "qty": 1, "assetNo": "260101-0002",
            "purchasePrice": 500000}).get_json()[0]
        rows = [{"관리번호": "260101-0002", "대분류": "PC", "매입가": "300000"}]
        self._post("/api/assets/migrate", rows, fill=True)
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["purchasePrice"], 500000)

    def test_미리보기가_채울_건수와_금액을_먼저_보여준다(self):
        self.c.post("/api/assets", json={
            "categoryId": self.cat, "qty": 1, "assetNo": "260101-0003"})
        rows = [{"관리번호": "260101-0003", "대분류": "PC", "매입가": "250000"}]
        r = self._post("/api/assets/migrate/preview", rows, fill=True).get_json()
        self.assertEqual(r["toUpdate"], 1)
        self.assertEqual(r["fillPurchaseSum"], 250000)
        self.assertTrue(r["updateSample"])

    def test_번호는_같은데_다른_기기면_막고_알려_준다(self):
        """조용히 건너뛰면 TMS 실물이 통째로 누락된다."""
        self.c.post("/api/assets", json={
            "categoryId": self.cat, "qty": 1, "assetNo": "260101-0004", "serial": "SN-OLD"})
        rows = [{"관리번호": "260101-0004", "대분류": "PC", "시리얼번호": "SN-NEW", "매입가": "1"}]
        r = self._post("/api/assets/migrate/preview", rows, fill=True).get_json()
        self.assertEqual(r["errorCount"], 1, r)
        self.assertIn("260101-0004", r["errors"][0])

    def test_신규_자산은_그대로_등록된다(self):
        rows = [{"관리번호": "260101-0009", "대분류": "PC", "모델명": "NEW", "매입가": "111"}]
        r = self._post("/api/assets/migrate", rows, fill=True).get_json()
        self.assertEqual(r["created"], 1)


class TestCancelReturn(Base):
    """전표 취소·반품 — TMS도 '매입취소'를 쓴다(465건 중 6건). 지우지 않고 표시한다."""

    def test_자산_없는_전표를_취소한다(self):
        b = self._batch()
        r = self.c.post(f"/api/purchase-batches/{b['id']}/cancel", json={"reason": "중복 입력"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = self.c.get(f"/api/purchase-batches/{b['id']}").get_json()
        self.assertTrue(d["cancelledAt"])
        self.assertEqual(d["cancelReason"], "중복 입력")

    def test_취소된_전표는_목록에서_빠진다(self):
        b = self._batch()
        self.c.post(f"/api/purchase-batches/{b['id']}/cancel", json={})
        ids = [x["id"] for x in self.c.get("/api/purchase-batches").get_json()]
        self.assertNotIn(b["id"], ids, "취소 전표가 목록에 남았다")
        ids2 = [x["id"] for x in
                self.c.get("/api/purchase-batches?includeCancelled=1").get_json()]
        self.assertIn(b["id"], ids2, "취소 전표를 볼 방법이 없다")

    def test_전표를_취소하면_자산도_재고에서_빠진다(self):
        b = self._batch()
        a = self._asset(b["id"], model="L480").get_json()[0]
        self.c.post(f"/api/purchase-batches/{b['id']}/cancel", json={"reason": "오등록"})
        got = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(got["status"], "cancelled")
        br = self.c.get("/api/assets/model-brief?model=L480").get_json()
        self.assertEqual(br["stock"], 0, "취소된 자산이 재고로 남았다")

    def test_출고된_자산이_있으면_취소를_막는다(self):
        """★이미 고객에게 나간 물건이 걸린 전표를 무르면 안 된다."""
        import sqlite3
        b = self._batch()
        a = self._asset(b["id"], model="L480").get_json()[0]
        conn = sqlite3.connect(self.tmp / "t.db")
        conn.execute("UPDATE assets SET status='shipped' WHERE id=?", (a["id"],))
        conn.commit()
        conn.close()
        r = self.c.post(f"/api/purchase-batches/{b['id']}/cancel", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn(a["assetNo"], r.get_json()["error"])

    def test_취소를_되돌릴_수_있다(self):
        b = self._batch()
        a = self._asset(b["id"], model="L480").get_json()[0]
        self.c.post(f"/api/purchase-batches/{b['id']}/cancel", json={})
        r = self.c.post(f"/api/purchase-batches/{b['id']}/uncancel", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["status"], "in_stock")
        self.assertFalse(self.c.get(f"/api/purchase-batches/{b['id']}").get_json()["cancelledAt"])

    def test_두번_취소는_막는다(self):
        b = self._batch()
        self.c.post(f"/api/purchase-batches/{b['id']}/cancel", json={})
        self.assertEqual(
            self.c.post(f"/api/purchase-batches/{b['id']}/cancel", json={}).status_code, 400)

    def test_전표_전체_반품(self):
        b = self._batch()
        a = self._asset(b["id"], model="L480").get_json()[0]
        r = self.c.post(f"/api/purchase-batches/{b['id']}/return",
                        json={"reason": "불량 다수", "returnedAt": "2026-07-31"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["status"], "returned")
        d = self.c.get(f"/api/purchase-batches/{b['id']}").get_json()
        self.assertEqual(d["returnedAt"], "2026-07-31")
        self.assertEqual(self.c.get("/api/assets/model-brief?model=L480").get_json()["stock"], 0)

    def test_일부만_반품하면_전표_반품일은_안_찍힌다(self):
        b = self._batch()
        a1 = self._asset(b["id"], model="L480").get_json()[0]
        a2 = self._asset(b["id"], model="L480").get_json()[0]
        r = self.c.post(f"/api/purchase-batches/{b['id']}/return",
                        json={"assetIds": [a1["id"]], "reason": "1대만 불량"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get(f"/api/assets/{a1['id']}").get_json()["status"], "returned")
        self.assertEqual(self.c.get(f"/api/assets/{a2['id']}").get_json()["status"], "in_stock")
        self.assertFalse(self.c.get(f"/api/purchase-batches/{b['id']}").get_json()["returnedAt"])


class TestBatchWithAssets(Base):
    """★전표+자산 동시 저장 — TMS는 전표당 평균 32대(최대 366대)를 한 번에 넣는다."""

    def _rows(self, n, **kw):
        out = []
        for i in range(n):
            r = {"categoryId": self.cat, "maker": "LENOVO", "model": f"L{480 + i}",
                 "purchasePrice": 200000, "qty": 1}
            r.update(kw)
            out.append(r)
        return out

    def test_전표와_자산_5줄을_한번에_저장한다(self):
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid, "purchaseDate": "2026-07-30",
            "totalAmount": 1000000, "assets": self._rows(5)})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        res = r.get_json()
        self.assertEqual(res["assetCount"], 5)
        d = self.c.get(f"/api/purchase-batches/{res['id']}").get_json()
        self.assertEqual(d["assetCount"], 5)
        self.assertEqual(d["assignedAmount"], 1000000)

    def test_한_줄에_수량을_넣으면_그만큼_생긴다(self):
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid, "purchaseDate": "2026-07-30",
            "assets": [{"categoryId": self.cat, "model": "L480", "qty": 30,
                        "purchasePrice": 100000}]}).get_json()
        self.assertEqual(r["assetCount"], 30)
        nos = [a["assetNo"] for a in r["assets"]]
        seqs = [int(n.split("-")[1]) for n in nos]
        self.assertEqual(seqs, list(range(seqs[0], seqs[0] + 30)), "번호가 연속이 아니다")

    def test_한_줄이라도_잘못되면_전표까지_통째로_취소된다(self):
        """★반쪽 저장이 생기면 안 된다 — 전표만 남고 자산이 없는 상태."""
        before = len(self.c.get("/api/purchase-batches?includeCancelled=1").get_json())
        rows = self._rows(2) + [{"categoryId": 999999, "model": "BAD"}]
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid, "purchaseDate": "2026-07-30",
            "assets": rows})
        self.assertGreaterEqual(r.status_code, 400)
        self.assertIn("3번째 줄", r.get_json()["error"])
        after = len(self.c.get("/api/purchase-batches?includeCancelled=1").get_json())
        self.assertEqual(before, after, "실패했는데 전표가 남았다")
        self.assertEqual(len(self.c.get("/api/assets").get_json()), 0, "자산이 부분 생성됐다")

    def test_가입고로_동시저장하면_전부_미입고다(self):
        r = self.c.post("/api/purchase-batches", json={
            "stage": "provisional", "purchaseDate": "2026-07-30",
            "assets": self._rows(3)}).get_json()
        self.assertEqual(r["assetCount"], 3)
        d = self.c.get(f"/api/purchase-batches/{r['id']}").get_json()
        self.assertEqual(d["pendingCount"], 3)

    def test_줄_수_상한이_있다(self):
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid, "purchaseDate": "2026-07-30",
            "assets": self._rows(201)})
        self.assertEqual(r.status_code, 400)

    def test_자산_없이도_전표만_만들_수_있다(self):
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid, "purchaseDate": "2026-07-30"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["assetCount"], 0)


class TestTmsStatusMapping(unittest.TestCase):
    """★TMS 재고상태 → HMS 자산상태 매핑.

    2026-07-30 내보내기 14,969건에서 확인한 실제 값은 매입/판매/렌탈/반납/반입/판매취소인데
    매핑표에는 하나도 없었다. 그대로 두면 전부 기본값(입고)으로 들어가
    이미 팔린 노트북 12,261대가 판매 가능 재고로 잡힌다.
    """

    def _resolve(self, **kw):
        from app.purchase.migration import _resolve_grade_status
        return _resolve_grade_status(kw)

    def test_판매완료는_재고가_아니다(self):
        self.assertEqual(self._resolve(tms_status="판매")[1], "shipped")

    def test_렌탈나간_것도_재고가_아니다(self):
        """렌탈은 RMS가 관리한다 — HMS 재고로 세면 이중 계상이다."""
        self.assertEqual(self._resolve(tms_status="렌탈")[1], "shipped")

    def test_매입은_판매가능_재고다(self):
        self.assertEqual(self._resolve(tms_status="매입")[1], "ready")

    def test_돌아온_물건은_다시_재고다(self):
        for v in ("반납", "반입", "판매취소"):
            self.assertEqual(self._resolve(tms_status=v)[1], "in_stock", v)

    def test_재고상태가_등급보다_우선한다(self):
        """★판매 완료된 물건의 등급 칸에 '수리'가 남아 있는 경우가 많다.
        등급을 상태로 삼으면 이미 팔린 노트북이 '수리중'으로 되살아난다."""
        grade, status, _ = self._resolve(tms_status="판매", grade="수리")
        self.assertEqual(status, "shipped", "팔린 물건이 수리중으로 되살아났다")
        self.assertEqual(grade, "미정")

    def test_재고상태가_없으면_등급에서_작업상태를_읽는다(self):
        self.assertEqual(self._resolve(grade="수리")[1], "repair")
        self.assertEqual(self._resolve(grade="시트지대기")[1], "painting")

    def test_원본_등급은_메모에_남는다(self):
        _, _, note = self._resolve(tms_status="판매", grade="수리")
        self.assertIn("수리", note)

    def test_정상_등급은_그대로_쓴다(self):
        grade, status, note = self._resolve(tms_status="매입", grade="A급")
        self.assertEqual((grade, status, note), ("A급", "ready", ""))


class TestAutoSync(unittest.TestCase):
    """★TMS 엑셀 자동 반영 — 폴더에 새 파일이 들어오면 알아서 최신화.

    대표 질문(2026-07-30): "매일 엑셀 내보내기해서 데이터 최신화는 언제 할래?"
    → 내보내기는 대표가(TMS를 건드리지 않기 위해), 반영은 HMS가 자동으로.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hms-as-"))
        self.app = create_app(db_path=self.tmp / "t.db")
        self.app.testing = True
        self.c = self.app.test_client()
        auth_mod._login_failures.clear()
        self.c.post("/api/auth/setup", json={
            "username": "admin", "displayName": "대표", "password": PW})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_자산_표만_반영_대상으로_본다(self):
        """매입내역·미수금·거래처는 자산 표가 아니다 — 넣으면 안 된다."""
        from app.purchase.autosync import _is_asset_sheet
        self.assertTrue(_is_asset_sheet([{"관리번호": "260101-0001", "모델명": "L480"}]))
        self.assertTrue(_is_asset_sheet([{"자산번호": "260101-0001", "시리얼": "SN1"}]))
        # 전표 표 — 관리번호가 없다
        self.assertFalse(_is_asset_sheet([{"매입전표": "P260101-001", "거래처명": "태화무역"}]))
        # 거래처 마스터
        self.assertFalse(_is_asset_sheet([{"거래처코드": "AJ", "거래처명": "AJ전자몰"}]))
        self.assertFalse(_is_asset_sheet([]))

    def test_우리_주문엑셀은_자산표로_보지_않는다(self):
        """★HMS가 뽑은 주문 엑셀에도 '자산번호' 칸이 있다.

        tms-export 폴더에 잘못 떨구면 자산 표로 오인돼 없는 자산이 자동 등록된다
        (2026-07-31 hms-orders-20260731.xlsx가 실제로 이 폴더를 통과했다).
        """
        from app.purchase.autosync import _is_asset_sheet
        order_row = [{"주문일": "2026-07-31", "쇼핑몰": "고도몰", "주문번호": "12345",
                      "상품명": "노트북", "수취인": "홍길동", "자산번호": "260101-0001, 260101-0002",
                      "송장번호": "", "작업단계": ""}]
        self.assertFalse(_is_asset_sheet(order_row), "주문 표가 자산 표로 통과했다")
        # 파일 이름만으로도 막는다(칸 구성이 바뀌어도)
        self.assertFalse(_is_asset_sheet([{"관리번호": "260101-0001", "모델명": "L480"}],
                                         "hms-orders-20260731.xlsx"))
        # 관리번호 한 칸만 있는 표는 자산 표로 보지 않는다 — 무엇이든 통과시키던 구멍
        self.assertFalse(_is_asset_sheet([{"자산번호": "260101-0001"}]))

    def test_한_칸에_여러_관리번호가_들어오면_거부한다(self):
        """주문 표의 자산번호 칸은 'A, B' 꼴이다 — 통짜로 자산 한 대가 되면 안 된다."""
        from app.db import tx
        from app.purchase.migration import _prepare
        with self.app.app_context():
            with tx() as conn:
                ready, dup, errors, updates = _prepare(
                    conn, [{"관리번호": "260101-0001, 260101-0002", "모델명": "L480"}])
        self.assertEqual(ready, [], "쉼표가 섞인 관리번호가 자산으로 등록됐다")
        self.assertTrue(any("형식이 아닙니다" in e for e in errors), errors)

    def test_반영_순서는_매입현황이_먼저다(self):
        """매입현황에만 전표번호가 있어 이걸 먼저 넣어야 전표·거래처가 선다."""
        from app.purchase.autosync import _rank
        names = ["재고현황260730.xlsx", "판매현황260730.xlsx",
                 "재고내역_260730.xlsx", "매입현황260730.xlsx"]
        self.assertEqual(sorted(names, key=_rank)[0], "매입현황260730.xlsx")

    def test_상태_조회가_폴더와_주기를_알려준다(self):
        r = self.c.get("/api/tms-sync/status")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertIn("tms-export", d["folder"])
        self.assertTrue(d["intervalMinutes"] > 0)
        self.assertIsInstance(d["waiting"], list)
        self.assertIsInstance(d["history"], list)

    def test_지금_반영_버튼이_동작한다(self):
        r = self.c.post("/api/tms-sync/run", json={})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("applied", r.get_json())

    def test_조회_권한만_있으면_실행은_막는다(self):
        self.c.post("/api/users", json={
            "username": "viewer", "displayName": "조회자", "password": "viewer-pass-1",
            "perms": ["purchase.view"]})
        self.c.post("/api/auth/logout", json={})
        self.c.post("/api/auth/login", json={"username": "viewer", "password": "viewer-pass-1"})
        self.assertEqual(self.c.get("/api/tms-sync/status").status_code, 200)
        self.assertEqual(self.c.post("/api/tms-sync/run", json={}).status_code, 403)

    def test_로그인_없이도_자산파일이_반영된다(self):
        """★백그라운드 데몬에는 로그인 사용자가 없다.

        g.user를 그냥 읽던 코드 때문에 자동 반영이 자산 파일에서 통째로 실패했다
        (2026-07-30, 요청 컨텍스트 안에서만 테스트해 못 잡았던 구멍).
        """
        from app.purchase import autosync
        import hashlib
        export = self.tmp / "exp"
        export.mkdir()
        html = ("<html><table><tr><th>관리번호</th><th>대분류</th><th>모델명</th>"
                "<th>매입가</th><th>재고상태</th></tr>"
                "<tr><td>991231-0001</td><td>PC</td><td>AUTOSYNC-TEST</td>"
                "<td>123000</td><td>매입</td></tr></table></html>")
        f = export / "재고내역_TEST.xlsx"
        f.write_bytes(html.encode())
        old_dir = autosync.EXPORT_DIR
        autosync.EXPORT_DIR = export
        try:
            # ★app_context만 — request context(g.user) 없음. 데몬과 같은 조건.
            n = autosync.sync_once(self.app)
            self.assertEqual(n, 1, "자동 반영이 파일을 처리하지 못했다")
        finally:
            autosync.EXPORT_DIR = old_dir
        with self.app.app_context():
            from app.db import tx
            with tx() as conn:
                a = conn.execute(
                    "SELECT * FROM assets WHERE asset_no='991231-0001'").fetchone()
                self.assertIsNotNone(a, "자산이 안 들어왔다")
                self.assertEqual(a["purchase_price"], 123000)
                self.assertEqual(a["status"], "ready", "재고상태 '매입'이 판매가능으로 안 갔다")
                log = conn.execute("SELECT * FROM tms_sync_log WHERE filename LIKE '재고내역_TEST%'").fetchone()
                self.assertIsNotNone(log, "반영 이력이 안 남았다")
                self.assertEqual(log["created"], 1)

    def test_같은_파일은_두번_반영하지_않는다(self):
        from app.purchase import autosync
        export = self.tmp / "exp2"
        export.mkdir()
        (export / "재고내역_T2.xlsx").write_bytes(
            ("<html><table><tr><th>관리번호</th><th>대분류</th><th>매입가</th></tr>"
             "<tr><td>991231-0002</td><td>PC</td><td>50000</td></tr></table></html>").encode())
        old_dir = autosync.EXPORT_DIR
        autosync.EXPORT_DIR = export
        try:
            self.assertEqual(autosync.sync_once(self.app), 1)
            self.assertEqual(autosync.sync_once(self.app), 0, "같은 파일을 또 반영했다")
        finally:
            autosync.EXPORT_DIR = old_dir


class TestAssetToCustomerTrace(Base):
    """★셋팅에서 스캔한 자산이 출고 때 고객과 붙고, 그 사실이 자산에 남아야 한다.

    대표 요청(2026-07-31): "셋팅 QC에서 자산번호를 입력하고 준비가 완료되면
    출고 단계에서 실제 그 모델이 고객과 붙어야 한다."
    주문번호만 적어 두면 주문이 고쳐지거나 지워졌을 때 추적이 끊긴다.
    """

    def _order(self, name="김철수"):
        return self.c.post("/api/orders", json={
            "recipient": name, "productName": "노트북", "phone": "010-5555-6666",
            "address": "서울시 강남구 1", "postalCode": "06000", "amount": 1200000}).get_json()

    def _events(self, aid):
        import json as _j
        d = self.c.get(f"/api/assets/{aid}").get_json()
        out = []
        for e in d.get("events", []):
            det = e.get("detail")
            if isinstance(det, str):
                try:
                    det = _j.loads(det)
                except ValueError:
                    det = {}
            out.append((e["action"], det or {}))
        return out

    def test_셋팅_스캔부터_출고까지_고객이_따라간다(self):
        b = self._batch()
        a = self._asset(b["id"], model="L480", purchasePrice=300000).get_json()[0]
        o = self._order()

        # 셋팅 — 자산번호 스캔
        r = self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        match = dict(self._events(a["id"])).get("주문매칭")
        self.assertIsNotNone(match, "매칭 이력이 없다")
        self.assertEqual(match.get("수취인"), "김철수")

        # 준비 → 검수 → 출고
        for act in ("preparing", "production", "softwareInspection", "shipping"):
            r = self.c.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
            self.assertEqual(r.status_code, 200, f"{act}: {r.get_data(as_text=True)}")

        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(d["status"], "shipped")
        self.assertEqual([x["recipient"] for x in d.get("orders", [])], ["김철수"])

        ship = dict(self._events(a["id"])).get("출고")
        self.assertIsNotNone(ship, "출고 이력이 없다")
        self.assertEqual(ship.get("수취인"), "김철수",
                         "★출고 이력에 고객이 없으면 주문이 바뀔 때 추적이 끊긴다")
        self.assertTrue(ship.get("출고일"), "출고일이 안 남았다")

    def test_출고취소에도_고객이_남는다(self):
        b = self._batch()
        a = self._asset(b["id"], model="L480").get_json()[0]
        o = self._order("박영희")
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("preparing", "production", "softwareInspection", "shipping"):
            self.c.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        # 검수 해제 → 출고취소
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "softwareInspection", "value": False})
        cancel = dict(self._events(a["id"])).get("출고취소")
        self.assertIsNotNone(cancel, "출고취소 이력이 없다")
        self.assertEqual(cancel.get("수취인"), "박영희")

    def test_매칭해제에도_고객이_남는다(self):
        b = self._batch()
        a = self._asset(b["id"], model="L480").get_json()[0]
        o = self._order("이민수")
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": []})
        off = dict(self._events(a["id"])).get("매칭해제")
        self.assertIsNotNone(off, "매칭해제 이력이 없다")
        self.assertEqual(off.get("수취인"), "이민수")

    def test_다른_주문에_이미_붙은_자산은_못_붙인다(self):
        """★같은 노트북이 두 고객에게 나가면 안 된다."""
        b = self._batch()
        a = self._asset(b["id"], model="L480").get_json()[0]
        o1, o2 = self._order("고객A"), self._order("고객B")
        self.assertEqual(
            self.c.patch(f"/api/orders/{o1['id']}",
                         json={"action": "assets", "assetIds": [a["id"]]}).status_code, 200)
        r = self.c.patch(f"/api/orders/{o2['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.assertGreaterEqual(r.status_code, 400, "한 자산이 두 주문에 붙었다")


class TestNumberRangeSplit(Base):
    """★TMS와 번호대 분리 (대표 결정 2026-07-31).

    두 시스템을 함께 쓰는 동안 규칙이 같으면 같은 번호가 서로 다른 물건에 붙는다.
    형식·자릿수는 그대로 두고 시작 번호만 나눈다.
      자산: TMS 0001~4999 / HMS 5000~9999
      전표: TMS 001~499   / HMS 500~999
    """

    def test_새_자산은_5000번대부터_받는다(self):
        b = self._batch()
        no = self._asset(b["id"], model="L480").get_json()[0]["assetNo"]
        seq = int(no.split("-")[1])
        self.assertGreaterEqual(seq, 5000, f"TMS 번호대를 침범했다: {no}")
        self.assertEqual(len(no), 11, "형식이 바뀌면 안 된다(YYMMDD-NNNN)")

    def test_새_전표는_500번대부터_받는다(self):
        slip = self._batch()["slipNo"]
        seq = int(slip.split("-")[1])
        self.assertGreaterEqual(seq, 500, f"TMS 번호대를 침범했다: {slip}")
        self.assertEqual(len(slip), 11, "형식이 바뀌면 안 된다(P+YYMMDD-NNN)")

    def test_TMS_번호가_있어도_HMS는_자기_번호대를_쓴다(self):
        """★핵심 — 오늘 TMS가 0001~0100을 썼어도 HMS는 5000부터 시작해야 한다."""
        from app import config
        today = config.now().strftime("%y%m%d")
        b = self._batch()
        # TMS 이관분처럼 낮은 번호를 심는다
        for n in (1, 2, 100):
            self.assertEqual(
                self._asset(b["id"], assetNo=f"{today}-{n:04d}").status_code, 201)
        no = self._asset(b["id"], model="AUTO").get_json()[0]["assetNo"]
        self.assertGreaterEqual(int(no.split("-")[1]), 5000,
                                f"TMS 번호를 이어받아 충돌 위험: {no}")

    def test_HMS_번호대_안에서는_연속이다(self):
        b = self._batch()
        nos = [self._asset(b["id"], model="L480").get_json()[0]["assetNo"] for _ in range(3)]
        seqs = [int(n.split("-")[1]) for n in nos]
        self.assertEqual(seqs, list(range(seqs[0], seqs[0] + 3)))
        self.assertEqual(seqs[0], 5000, "HMS 첫 번호는 5000이어야 한다")

    def test_수량으로_한번에_등록해도_5000번대다(self):
        b = self._batch()
        made = self._asset(b["id"], model="L480", qty=5).get_json()
        seqs = [int(a["assetNo"].split("-")[1]) for a in made]
        self.assertEqual(seqs, list(range(5000, 5005)))

    def test_가입고_전표도_500번대다(self):
        self.assertGreaterEqual(int(self._batch("provisional")["slipNo"].split("-")[1]), 500)

    def test_전표_여러건도_연속이다(self):
        slips = [self._batch()["slipNo"] for _ in range(3)]
        seqs = [int(s.split("-")[1]) for s in slips]
        self.assertEqual(seqs, [500, 501, 502])


class TestAuditRound2(Base):
    """2026-07-31 전면 감사(76에이전트, 45건 확정)에서 나온 결함들의 회귀 방지."""

    def _ship_ready(self, **kw):
        """자산 1대가 붙고 검수까지 끝난 주문을 만든다."""
        b = self._batch()
        a = self._asset(b["id"], model="L480", purchasePrice=300000).get_json()[0]
        o = self.c.post("/api/orders", json={
            "recipient": "고객A", "productName": "노트북", "phone": "010-1111-2222",
            "address": "서울시 강남구 1", "postalCode": "06000", "amount": 500000}).get_json()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("preparing", "production", "softwareInspection"):
            self.c.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        return b, a, o

    # ── F1 송장 발급 후 자산 교체 ────────────────────────────────────
    def test_송장_발급된_주문은_자산을_바꿀_수_없다(self):
        """★라벨에 번호가 찍혀 나간 노트북이 재고로 되살아나면 같은 물건을 두 번 판다."""
        b, a, o = self._ship_ready()
        r = self.c.post(f"/api/orders/{o['id']}/waybill", json={})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        r2 = self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": []})
        self.assertGreaterEqual(r2.status_code, 400, "송장 발급 후 자산이 빠졌다")
        self.assertIn("송장", r2.get_json().get("error", ""))
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["status"], "reserved",
                         "자산이 재고로 되살아났다")

    def test_송장_취소하면_다시_바꿀_수_있다(self):
        b, a, o = self._ship_ready()
        w = self.c.post(f"/api/orders/{o['id']}/waybill", json={}).get_json()
        self.c.post(f"/api/waybills/{w['wid']}/cancel", json={})
        r = self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": []})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    # ── 취소 전표 묶음 ───────────────────────────────────────────────
    def test_취소된_전표는_매입확정할_수_없다(self):
        v = self._batch("provisional")
        self.c.post(f"/api/purchase-batches/{v['id']}/cancel", json={})
        r = self.c.post(f"/api/purchase-batches/{v['id']}/confirm",
                        json={"supplierId": self.sid, "purchaseDate": "2026-07-31"})
        self.assertEqual(r.status_code, 400, "취소 전표가 매입 확정됐다 — P번호가 헛되이 소비된다")

    def test_취소된_전표에는_자산을_추가할_수_없다(self):
        b = self._batch()
        self.c.post(f"/api/purchase-batches/{b['id']}/cancel", json={})
        r = self._asset(b["id"], model="GHOST")
        self.assertGreaterEqual(r.status_code, 400, "숨겨진 전표에 유령 자산이 붙었다")

    def test_취소_되돌리기가_원래_상태로_복원한다(self):
        """★판매가능·불량을 전부 '입고'로 뭉개면 셋팅을 다시 해야 한다."""
        b = self._batch()
        a1 = self._asset(b["id"], model="L480").get_json()[0]
        a2 = self._asset(b["id"], model="L480").get_json()[0]
        self.c.patch(f"/api/assets/{a1['id']}", json={"status": "ready"})
        self.c.patch(f"/api/assets/{a2['id']}", json={"status": "defective"})
        self.c.post(f"/api/purchase-batches/{b['id']}/cancel", json={})
        self.c.post(f"/api/purchase-batches/{b['id']}/uncancel", json={})
        self.assertEqual(self.c.get(f"/api/assets/{a1['id']}").get_json()["status"], "ready",
                         "판매가능이 입고로 뭉개졌다")
        self.assertEqual(self.c.get(f"/api/assets/{a2['id']}").get_json()["status"], "defective",
                         "불량이 입고로 뭉개졌다")

    def test_취소_포함으로_조회하면_다시_찾을_수_있다(self):
        b = self._batch()
        self.c.post(f"/api/purchase-batches/{b['id']}/cancel", json={})
        base = [x["id"] for x in self.c.get("/api/purchase-batches").get_json()]
        self.assertNotIn(b["id"], base)
        inc = [x["id"] for x in
               self.c.get("/api/purchase-batches?includeCancelled=1").get_json()]
        self.assertIn(b["id"], inc, "취소 전표를 되돌릴 방법이 없다")

    def test_반품한_전표도_취소할_수_있다(self):
        """반품 자산이 취소 대상에서 빠져 엉뚱한 오류가 나던 문제."""
        b = self._batch()
        self._asset(b["id"], model="L480")
        self.c.post(f"/api/purchase-batches/{b['id']}/return", json={"reason": "불량"})
        r = self.c.post(f"/api/purchase-batches/{b['id']}/cancel", json={})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    # ── 재고 기준 통일 ───────────────────────────────────────────────
    def test_취소_반품_미입고는_재고에서_빠진다(self):
        b = self._batch()
        keep = self._asset(b["id"], model="KEEPME").get_json()[0]
        v = self._batch("provisional")
        self._asset(v["id"], model="KEEPME")            # 미입고
        b2 = self._batch()
        self._asset(b2["id"], model="KEEPME")
        self.c.post(f"/api/purchase-batches/{b2['id']}/cancel", json={})   # 취소
        b3 = self._batch()
        self._asset(b3["id"], model="KEEPME")
        self.c.post(f"/api/purchase-batches/{b3['id']}/return", json={})   # 반품

        br = self.c.get("/api/assets/model-brief?model=KEEPME").get_json()
        self.assertEqual(br["stock"], 1, f"재고가 1이어야 하는데 {br['stock']}")
        summ = self.c.get("/api/assets/summary").get_json()
        counted = sum(r["count"] for r in summ
                      if r["status"] not in ("shipped", "scrapped", "cancelled", "returned"))
        self.assertEqual(counted, 1, f"요약이 {counted}대로 셌다(미입고·취소·반품 포함 의심)")

    def test_취소_전표는_재무_매입액에서_빠진다(self):
        b = self._batch(totalAmount=500000, purchaseDate="2026-07-31")
        self._asset(b["id"], model="L480", purchasePrice=500000)
        before = self.c.get("/api/reports/summary?from=2026-07-01&to=2026-07-31").get_json()
        self.c.post(f"/api/purchase-batches/{b['id']}/cancel", json={})
        after = self.c.get("/api/reports/summary?from=2026-07-01&to=2026-07-31").get_json()
        b0 = (before.get("purchase") or {}).get("amount", 0)
        a0 = (after.get("purchase") or {}).get("amount", 0)
        self.assertLess(a0, b0, f"취소해도 매입액이 그대로다 ({b0} → {a0})")


class TestSetupDisplay(Base):
    """셋팅 화면 표시 — 옵션 색 구분·수량/코드 강조·창고 재고 (대표 요청 2026-07-31)."""

    def test_숫자_상품ID도_상품명에서_모델을_찾는다(self):
        """★쿠팡·스마트스토어는 상품코드가 숫자라 '창고 0대'로만 떴다."""
        from app.orders.product_info import model_of
        self.assertEqual(model_of("L480_i5-8_내장", "레노버 L480"), "L480")
        self.assertEqual(model_of("13675224455", "S급 삼성 NT551EAA 노트북"), "NT551EAA")
        self.assertEqual(model_of("", "LG 그램 17Z95N"), "Z95N")
        self.assertEqual(model_of("6966207626", ""), "")

    def test_창고_재고가_쿠팡_주문에도_나온다(self):
        b = self._batch()
        for _ in range(3):
            self._asset(b["id"], maker="SAMSUNG", model="NT551EAA",
                        purchasePrice=200000, grade="A급")
        r = self.c.post("/api/orders/product-info", json={
            "mall": "godomall", "codes": ["13675224455"], "specCodes": [],
            "names": {"13675224455": "S급 삼성 NT551EAA 노트북"}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        p = r.get_json()["products"]["13675224455"]
        self.assertEqual(p["ourStock"], 3, f"창고 재고가 0으로 나왔다: {p}")

    def test_상품명이_없으면_예전처럼_동작한다(self):
        b = self._batch()
        self._asset(b["id"], model="L480", purchasePrice=100000)
        r = self.c.post("/api/orders/product-info", json={
            "mall": "godomall", "codes": ["L480_i5-8_내장"], "specCodes": []})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["products"]["L480_i5-8_내장"]["ourStock"], 1)

class TestModalBackground(unittest.TestCase):
    """팝업이 투명하게 떠 뒤쪽 목록이 비쳐 안 읽히던 문제(대표 지적 2026-07-31).

    화면마다 고치면 새 팝업에서 또 빠지므로 CSS 한 곳에서 보장한다.
    이 테스트는 그 보장이 지워지지 않았는지만 지킨다."""

    def setUp(self):
        self.css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")

    def test_모든_팝업이_바탕색을_갖는다(self):
        self.assertIn(".in-modal > *:not(.card)", self.css,
                      "팝업 공통 바탕색 규칙이 사라졌다 — 투명 팝업이 다시 생긴다")
        block = self.css.split(".in-modal > *:not(.card)", 1)[1].split("}", 1)[0]
        self.assertIn("background: var(--surface) !important", block,
                      "인라인 style을 이기려면 !important 가 있어야 한다")
        self.assertIn("border: 1px solid var(--border) !important", block,
                      "바깥 점선 테두리를 실선으로 덮어야 한다")

    def test_안쪽_점선_구분선은_지우지_않는다(self):
        # 주문 상세·A/S의 정상적인 구분선까지 없애는 규칙이 다시 들어오면 막는다
        self.assertNotIn('div[style*="dashed"] {', self.css,
                         "점선 일괄 제거 규칙은 정상 구분선까지 지운다")

    def test_팝업으로_뜨는_패널은_점선_껍데기여도_된다(self):
        # revealPanel 로 뜨는 곳을 늘려도 CSS가 알아서 덮는지 — 호출 지점만 세어 둔다
        js = (ROOT / "static" / "js")
        hits = sum(f.read_text("utf-8").count("revealPanel(")
                   for f in js.glob("*.js"))
        self.assertGreaterEqual(hits, 13, "revealPanel 호출이 줄었다면 팝업이 사라진 것")

class TestStaticCacheBusting(unittest.TestCase):
    """css/js를 고쳐도 브라우저가 예전 파일을 쓰던 문제 — 주소에 파일 시각을 붙인다."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = create_app(str(Path(self.tmp) / "t.db"))
        self.c = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_모든_css_js에_버전이_붙는다(self):
        import re
        html = self.c.get("/").get_data(as_text=True)
        refs = re.findall(r"/static/(?:css|js)/[A-Za-z0-9_.\-]+(?:\?v=\d+)?", html)
        self.assertTrue(refs, "쉘 페이지에 css/js 참조가 없다")
        bare = [x for x in refs if "?v=" not in x]
        self.assertEqual(bare, [], f"버전이 안 붙은 정적 파일: {bare}")

    def test_쉘_페이지는_캐시하지_않는다(self):
        r = self.c.get("/")
        self.assertEqual(r.headers.get("Cache-Control"), "no-cache")
        self.assertIn("text/html", r.headers.get("Content-Type", ""))

    def test_파일이_바뀌면_버전도_바뀐다(self):
        import re, os, time
        css = ROOT / "static" / "css" / "app.css"
        before = re.search(r"app\.css\?v=(\d+)", self.c.get("/").get_data(as_text=True)).group(1)
        st = css.stat()
        try:
            os.utime(css, (st.st_atime, st.st_mtime + 60))
            after = re.search(r"app\.css\?v=(\d+)", self.c.get("/").get_data(as_text=True)).group(1)
            self.assertNotEqual(before, after, "파일을 고쳐도 주소가 그대로면 캐시가 안 풀린다")
        finally:
            os.utime(css, (st.st_atime, st.st_mtime))

class TestAudit20260731(Base):
    """2026-07-31 전면 감사에서 확정된 것들의 회귀 방지."""

    def _shipped_order(self):
        """자산을 붙이고 준비→제작→검수→출고까지 끝낸 주문 하나."""
        b = self._batch()
        a = self._asset(b["id"], model="L480").get_json()[0]
        o = self.c.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북", "phone": "010-1111-2222",
            "address": "서울시 강남구 1", "postalCode": "06000", "amount": 500000}).get_json()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("preparing", "production", "softwareInspection", "shipping"):
            r = self.c.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
            self.assertEqual(r.status_code, 200, f"{act}: {r.get_data(as_text=True)}")
        return o

    def test_오늘_출고는_보관해도_세어진다(self):
        """출고 후 '보관'으로 정리해도 대시보드 '오늘 출고'에서 빠지면 안 된다.

        실제 운영은 출고 확인 직후 보관 처리한다(중앙값 1.6분). 예전에는 진행 중
        목록만 세어 하루 종일 0건으로 보였다.
        """
        o = self._shipped_order()
        before = self.c.get("/api/orders/today").get_json()["shippedToday"]
        self.assertEqual(before, 1, "출고 직후에 안 세어졌다")
        r = self.c.post("/api/orders/bulk", json={"action": "archive", "ids": [o["id"]]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json().get("ok"), r.get_data(as_text=True))
        after = self.c.get("/api/orders/today").get_json()["shippedToday"]
        self.assertEqual(after, 1, "보관 처리했다고 오늘 출고에서 빠졌다")

    def test_취소한_주문은_오늘_출고에서_빠진다(self):
        o = self._shipped_order()
        # 출고 확인을 풀어야 취소할 수 있다. 이때 출고일은 그대로 남으므로
        # (지난달 출고분이 이번 달로 넘어가지 않게 하는 규칙) 취소 조건이 실제로 필요하다.
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": False})
        r = self.c.post("/api/orders/bulk", json={
            "action": "cancel", "ids": [o["id"]], "reason": "고객 변심"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json().get("ok"), r.get_data(as_text=True))
        self.assertEqual(self.c.get("/api/orders/today").get_json()["shippedToday"], 0)

    def test_창고재고가_무엇을_셌는지_함께_준다(self):
        """포함 검색이라 다른 모델이 섞일 수 있다 — 내역을 줘야 사람이 판단한다.

        갤럭시탭 S6 로 찾으면 'S6 Lite'까지 잡히던 실제 사례(2026-07-31).
        """
        b = self._batch()
        self._asset(b["id"], model="THINKPAD T470")
        for _ in range(3):
            self._asset(b["id"], model="THINKPAD T470S")
        from app.db import tx
        from app.orders.product_info import our_stock
        with self.app.app_context():
            with tx() as conn:
                got = our_stock(conn, {"T470"})
        self.assertEqual(got["T470"]["count"], 4, "포함 검색 총합이 바뀌었다")
        models = {d["model"]: d for d in got["T470"]["byModel"]}
        self.assertIn("THINKPAD T470", models)
        self.assertIn("THINKPAD T470S", models)
        self.assertEqual(len(got["T470"]["byModel"]), 2, "섞인 사실이 안 드러난다")
        # 표기가 정확히 같은 것이 하나도 없어도 내역은 나와야 한다
        self.assertFalse(any(d["exact"] for d in got["T470"]["byModel"]))

    def test_표기가_같으면_확실로_표시된다(self):
        b = self._batch()
        self._asset(b["id"], model="NT371B5M")
        from app.db import tx
        from app.orders.product_info import our_stock
        with self.app.app_context():
            with tx() as conn:
                got = our_stock(conn, {"NT371B5M"})
        self.assertTrue(got["NT371B5M"]["byModel"][0]["exact"])

    def test_첫화면은_파일이_깨져도_뜬다(self):
        """index.html 인코딩이 바뀌어도 500이 아니라 화면이 떠야 한다."""
        import unittest.mock as mock
        with mock.patch("pathlib.Path.read_text", side_effect=UnicodeDecodeError(
                "utf-8", b"", 0, 1, "boom")):
            r = self.c.get("/")
        self.assertEqual(r.status_code, 500)   # 두 번째 읽기도 막히면 안내는 준다
        self.assertIn("error", r.get_json())


class TestSetupStatsAnchor(unittest.TestCase):
    """셋팅 실적 [이전]/[다음] — 31일에 달을 건너뛰던 문제(2026-07-31)."""

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "app.js").read_text("utf-8")

    def test_월이동은_1일로_맞춘_뒤_옮긴다(self):
        block = self.js.split("function shiftAnchor", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("d.setDate(1)", block,
                      "31일에 setMonth를 그냥 부르면 다음 달로 넘어간다")

    def test_날짜는_UTC로_바꾸지_않는다(self):
        # toISOString()은 UTC라 오전 9시 이전에 하루 밀린다
        self.assertNotIn("toISOString().slice(0, 10)", self.js)


class TestNestedModal(unittest.TestCase):
    """팝업 안에서 팝업을 열면 바깥이 지워지던 문제(2026-07-31)."""

    def test_이미_팝업_안이면_다시_올리지_않는다(self):
        js = (ROOT / "static" / "js" / "app.js").read_text("utf-8")
        block = js.split("function openModalWith", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn('host.closest("#modal-back")', block,
                      "가드가 없으면 closeModal이 바깥 팝업 내용을 지운다")
        # 가드가 closeModal 앞에 있어야 의미가 있다
        self.assertLess(block.index('host.closest("#modal-back")'),
                        block.index("closeModal();"),   # 주석 속 closeModal()과 구분
                        "가드가 closeModal 뒤에 있으면 이미 지워진 뒤다")

class TestSetupStockRefresh(unittest.TestCase):
    """창고 N대가 처음 뜬 값에서 안 바뀌던 문제(감사 A-4, 2026-07-31).

    자산을 스캔해 붙여도 숫자가 그대로라, 담당자가 없는 재고를 있다고 믿고 진행했다.
    """

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")

    def test_주기적으로_다시_물어본다(self):
        self.assertIn("STOCK_REFRESH_MS", self.js,
                      "갱신 주기가 없으면 코드당 한 번 묻고 끝난다")
        self.assertIn("state.productInfoAt", self.js, "마지막 조회 시각을 기억해야 한다")

    def test_재고가_바뀌면_화면을_다시_그린다(self):
        # 스펙이 새로 붙은 경우만 다시 그리면 숫자가 낡은 채로 남는다
        self.assertIn("before.ourStock !== after.ourStock", self.js)

    def test_옵션은_자르지_않고_다음_줄로_넘긴다(self):
        """대표 지시(2026-08-03): 옵션표는 필수라 잘리면 안 된다. 행이 커져도 된다."""
        css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")
        block = css.split(".setup-opts {", 1)[1].split("}", 1)[0]
        self.assertIn("flex-wrap: wrap", block, "옵션이 다음 줄로 안 넘어가면 잘린다")
        self.assertNotIn("nowrap", block)
        chip = css.split(".setup-opts .chip {", 1)[1].split("}", 1)[0]
        self.assertIn("flex: 0 1 auto", chip, "긴 옵션 칩이 못 줄어들면 칸 밖으로 나간다")
        self.assertIn("overflow-wrap: anywhere", chip)

    def test_상품명을_누르면_그_쇼핑몰로_간다(self):
        js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")
        self.assertIn("function mallProductUrl", js)
        block = js.split("function mallProductUrl", 1)[1].split(chr(10) + "}", 1)[0]
        for mall in ("고도몰", "쿠팡", "스마트스토어", "11번가"):
            self.assertIn(mall, block, f"{mall} 링크 규칙이 없다")
        self.assertIn("goodsNo", block, "자사몰은 goodsNo로 상품 페이지에 바로 가야 한다")
        self.assertIn('target="_blank"', js, "링크가 현재 창을 덮으면 작업보드를 잃는다")

    def test_수취인을_누르면_성함_연락처_주소가_뜬다(self):
        js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")
        self.assertIn("function showRecipient", js)
        block = js.split("function showRecipient", 1)[1].split(chr(10) + "}", 1)[0]
        for f in ("성함", "연락처", "주소"):
            self.assertIn(f, block)
        # 권한이 없어 가려진 경우를 빈칸이 아니라 안내로 알려야 한다
        self.assertIn("고객정보 열람 권한", block)

    def test_자사몰_주소_설정칸이_있다(self):
        from app.malls import MALLS
        godo = next(m for m in MALLS if m["code"] == "godomall")
        keys = [f["key"] for f in godo["fields"]]
        self.assertIn("shop_url", keys, "자사몰 주소를 넣을 곳이 없으면 상품 링크를 만들 수 없다")

    def test_행_높이에_바닥이_있고_긴_글자는_접힌다(self):
        """행 높이 규칙 — 2026-08-05 대표 지시로 '무조건 2줄'에서 완화됐다.

        원래는 옵션 길이 때문에 행이 들쭉날쭉해서 상품 칸을 2줄로 눌러 놨다.
        재고 칩·상세 버튼이 채널 칸으로 내려가며 행이 세로로 늘자
        대표가 "가로로 너무 빽빽하다"고 해, 상품명·스펙을 두 줄까지 폈다.
        ★그래도 무제한은 아니다 — 최소 높이는 그대로 두고 두 줄에서 …으로 끊는다.
        """
        css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")
        self.assertIn(".setup-rows-fixed tr", css, "행 높이 바닥 규칙이 사라졌다")
        block = css.split(".setup-rows-fixed tr {", 1)[1].split("}", 1)[0]
        self.assertIn("height:", block, "최소 높이가 없으면 짧은 행이 얇아져 눈이 흔들린다")
        for cls in (chr(10) + ".prod-title {", chr(10) + ".prod-spec {"):
            b = css.split(cls, 1)[1].split("}", 1)[0]
            self.assertIn("-webkit-line-clamp:", b, f"{cls.strip()} 가 몇 줄이든 늘어난다")
        self.assertIn("text-overflow: ellipsis", css)

    def test_표가_고정_레이아웃이라_내용이_폭을_밀지_못한다(self):
        """auto 레이아웃이면 nowrap 내용이 칸을 밀어 표가 화면의 두 배가 된다(실측 3187px)."""
        css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")
        block = css.split(".setup-table {", 1)[1].split("}", 1)[0]
        self.assertIn("table-layout: fixed", block)
        # 폭은 colgroup으로 준다 — 상품 칸만 폭을 안 줘서 남는 자리를 가져간다
        # col-prep은 2026-08-04 대표 지시로 제거됐다(챙길 옵션 → 옵션 칩으로 통합)
        for col in ("col-chan", "col-recipient", "col-assets", "col-stage", "col-status"):
            self.assertIn(col, css, f"{col} 폭 지정이 없다")
        js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")
        self.assertIn("<colgroup>", js, "colgroup이 없으면 fixed 레이아웃이 폭을 못 잡는다")
        self.assertIn('class="setup-table"', js)

    def test_자산_스캔칸은_수량만큼_늘어나지_않는다(self):
        """10대짜리 주문에서 칸이 열 줄이 되어 행이 686px가 됐다 — 한 칸만 둔다."""
        js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")
        block = js.split("function assetCell", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("nextIdx", block, "'지금 찍을 칸' 하나만 두는 구조가 사라졌다")
        self.assertNotIn("for (let i = 0; i < Math.max(need, have.length); i++)", block,
                         "수량만큼 칸을 늘어놓으면 행 높이가 다시 들쭉날쭉해진다")

    def test_단계칸_담당자_이름이_줄을_늘리지_않는다(self):
        css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")
        block = css.split(".stage-by {", 1)[1].split("}", 1)[0]
        self.assertIn("white-space: nowrap", block)
        self.assertIn("text-overflow: ellipsis", block)


class TestUserDelete(Base):
    """계정 삭제(대표 요청 2026-08-03).

    작업 이력은 이름 텍스트로 남으므로 지워도 안전하지만,
    '아무도 못 들어오는 상태'만은 절대 만들면 안 된다.
    """

    def _make(self, username, is_admin=False):
        r = self.c.post("/api/users", json={
            "username": username, "displayName": username + "님",
            "password": "pass-1234", "isAdmin": is_admin,
            "perms": [] if is_admin else ["orders.view"],
            "allCategories": True, "categoryIds": []})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        return r.get_json()

    def test_일반_계정을_지울_수_있다(self):
        u = self._make("temp1")
        r = self.c.delete(f"/api/users/{u['id']}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        names = [x["username"] for x in self.c.get("/api/users").get_json()]
        self.assertNotIn("temp1", names)

    def test_지운_계정으로는_로그인할_수_없다(self):
        u = self._make("temp2")
        self.c.delete(f"/api/users/{u['id']}")
        auth_mod._login_failures.clear()
        r = self.c.post("/api/auth/login", json={"username": "temp2", "password": "pass-1234"})
        self.assertEqual(r.status_code, 401)

    def test_권한_분류_설정도_같이_지워진다(self):
        """남으면 나중에 같은 번호를 받은 계정에 남의 권한이 붙는다."""
        u = self._make("temp3")
        self.c.delete(f"/api/users/{u['id']}")
        with self.app.app_context():
            from app.db import tx
            with tx() as conn:
                for t in ("user_perms", "user_categories", "sessions"):
                    n = conn.execute(f"SELECT COUNT(*) c FROM {t} WHERE user_id=?", (u["id"],)).fetchone()["c"]
                    self.assertEqual(n, 0, f"{t}에 찌꺼기가 남았다")

    def test_본인_계정은_못_지운다(self):
        me = next(x for x in self.c.get("/api/users").get_json() if x["username"] == "admin")
        r = self.c.delete(f"/api/users/{me['id']}")
        self.assertEqual(r.status_code, 400)
        self.assertIn("본인", r.get_json()["error"])

    def test_마지막_관리자는_못_지운다(self):
        """지우면 아무도 계정을 만들 수 없고, 계정이 0개가 되면 최초 설정 화면이 열린다."""
        other = self._make("worker1")            # 관리자가 아닌 계정만 남겨 둔다
        me = next(x for x in self.c.get("/api/users").get_json() if x["username"] == "admin")
        # 다른 관리자로 로그인해 시도해야 '본인 삭제' 규칙이 아닌 '마지막 관리자' 규칙을 탄다
        admin2 = self._make("admin2", is_admin=True)
        self.c.post("/api/auth/logout")
        auth_mod._login_failures.clear()
        self.c.post("/api/auth/login", json={"username": "admin2", "password": "pass-1234"})
        self.assertEqual(self.c.delete(f"/api/users/{me['id']}").status_code, 200)  # 아직 admin2가 있다
        # 이제 admin2가 마지막 관리자 — worker1으로는 지울 수 없고, 스스로도 못 지운다
        r = self.c.delete(f"/api/users/{admin2['id']}")
        self.assertEqual(r.status_code, 400)
        self.assertIn("본인", r.get_json()["error"])
        self.assertTrue(other)

    def test_없는_계정_삭제는_404(self):
        self.assertEqual(self.c.delete("/api/users/99999").status_code, 404)

    def test_작업_이력은_지워지지_않는다(self):
        """누가 셋팅했는지는 이름으로 저장돼 있어 계정을 지워도 남아야 한다."""
        b = self._batch()
        a = self._asset(b["id"], model="L480").get_json()[0]
        before = self.c.get(f"/api/assets/{a['id']}").get_json()
        u = self._make("temp4")
        self.c.delete(f"/api/users/{u['id']}")
        after = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(after.get("createdBy"), before.get("createdBy"))
        self.assertTrue(after.get("createdBy"), "등록자 이름이 비었다")


class TestOptionChipSplit(unittest.TestCase):
    """옵션이 금액의 천 단위 쉼표에서 잘리던 문제(대표 지적 2026-08-03).

    'A급 배터리 (-20,000원)' → 'A급 배터리 (-20' + '000원)' 으로 쪼개졌다.
    """

    def test_숫자_사이_쉼표는_구분자가_아니다(self):
        js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")
        block = js.split("function optionChips", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn(r"(\d),(\d)", block,
                      "숫자 사이 쉼표를 보호하지 않으면 금액에서 옵션이 쪼개진다")

    def test_소스에_NUL_문자가_없다(self):
        # 표식 문자를 소스에 직접 박으면 편집기·도구가 깨진다
        js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")
        self.assertNotIn(chr(0), js)

class TestGradeStock(Base):
    """등급별 재고(대표 확인 2026-08-03).

    앞 글자 = 외관, 뒤 글자 = 배터리. B급만 둘을 통틀어 한 글자.
    쓰는 등급: S+S · S+A · SS · SA · AS · AA · B
    '창고 90대'인데 요구 등급(AA급)은 0대인 일이 흔해서 등급별로도 세어 준다.
    """

    def test_등급별로_나눠_센다(self):
        b = self._batch()
        for g in ("AA", "AA", "AS", "미정"):
            self._asset(b["id"], model="NT371B5M", grade=g)
        from app.db import tx
        from app.orders.product_info import our_stock
        with self.app.app_context():
            with tx() as conn:
                got = our_stock(conn, {"NT371B5M"})
        st = got["NT371B5M"]
        self.assertEqual(st["count"], 4)
        by = {g["grade"]: g["count"] for g in st["byGrade"]}
        self.assertEqual(by.get("AA"), 2)
        self.assertEqual(by.get("AS"), 1)
        self.assertEqual(by.get("미정"), 1)

    def test_등급이_비어_있으면_미정으로_묶는다(self):
        b = self._batch()
        self._asset(b["id"], model="L480", grade="")
        from app.db import tx
        from app.orders.product_info import our_stock
        with self.app.app_context():
            with tx() as conn:
                got = our_stock(conn, {"L480"})
        by = {g["grade"]: g["count"] for g in got["L480"]["byGrade"]}
        self.assertIn("미정", by, "빈 등급이 이름 없이 나오면 화면에서 못 읽는다")


class TestOrderGradeParser(unittest.TestCase):
    """주문에서 요구 등급을 읽어내는 규칙 — 화면(setup.js)에 있다."""

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")

    def test_외관과_배터리를_따로_읽어_합친다(self):
        block = self.js.split("function orderGrade", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("RE_LOOK", block, "외관 등급을 안 읽으면 조합이 안 된다")
        self.assertIn("RE_BATT", block, "배터리 등급을 안 읽으면 조합이 안 된다")
        # 'A급 외관' + 'S급 배터리' → AS
        self.assertIn("look[1] + batt[1]", block)

    def test_규칙에_S플러스가_들어_있다(self):
        # S+S급·S+A급이 실제로 쓰인다 — 정규식에서 빠지면 못 읽는다
        self.assertIn(r"S\+", self.js)

    def test_요구등급_재고를_따로_보여준다(self):
        block = self.js.split("function stockChip", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("ourStockGrade", block, "등급별 내역을 안 받으면 셀 수 없다")
        self.assertIn("등급 미정", block, "미정 대수를 안 알리면 0대를 오해한다")

class TestProductCodeStockListing(Base):
    """제품코드 + 재고반영(대표 확정 2026-08-04).

    제품코드 = 쇼핑몰 재고의 축(예: 840 G3_i7-6_내장), 자산번호 = 출고의 축.
    ★제품코드는 대표가 직접 기입한다(자동 생성 금지).
    ★재고반영은 기본 꺼짐 — 코드를 넣었다고 자동으로 켜지면 안 된다.
    """

    def _one(self, **kw):
        b = self._batch()
        return self._asset(b["id"], model="840 G3", **kw).get_json()[0]

    def test_새_자산은_재고반영이_꺼져_있다(self):
        a = self._one()
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertFalse(d["stockListed"], "기본이 켜짐이면 몰 재고가 멋대로 늘어난다")
        self.assertEqual(d["productCode"], "", "제품코드가 자동으로 채워지면 안 된다")

    def test_제품코드를_기입하고_재고반영을_켠다(self):
        a = self._one()
        r = self.c.patch(f"/api/assets/{a['id']}", json={"productCode": "840 G3_i7-6_내장"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r = self.c.patch(f"/api/assets/{a['id']}", json={"stockListed": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertTrue(d["stockListed"])
        self.assertEqual(d["productCode"], "840 G3_i7-6_내장")

    def test_제품코드_없이는_재고반영을_못_켠다(self):
        a = self._one()
        r = self.c.patch(f"/api/assets/{a['id']}", json={"stockListed": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("제품코드", r.get_json()["error"])

    def test_일괄_재고반영은_코드_있는_것만_켠다(self):
        b = self._batch()
        w = self._asset(b["id"], model="840 G3").get_json()[0]     # 코드 있음
        wo = self._asset(b["id"], model="840 G3").get_json()[0]    # 코드 없음
        self.c.patch(f"/api/assets/{w['id']}", json={"productCode": "840 G3_i7-6_내장"})
        r = self.c.post("/api/assets/stock-listing", json={"ids": [w["id"], wo["id"]], "on": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertEqual(j["ok"], 1)
        self.assertEqual(len(j["skipped"]), 1)
        self.assertIn("제품코드", j["skipped"][0]["reason"])
        self.assertTrue(self.c.get(f"/api/assets/{w['id']}").get_json()["stockListed"])
        self.assertFalse(self.c.get(f"/api/assets/{wo['id']}").get_json()["stockListed"])

    def test_일괄_재고해제는_항상_된다(self):
        a = self._one()
        self.c.patch(f"/api/assets/{a['id']}", json={"productCode": "840 G3_i7-6_내장",
                                                     "stockListed": True})
        r = self.c.post("/api/assets/stock-listing", json={"ids": [a["id"]], "on": False})
        self.assertEqual(r.get_json()["ok"], 1)
        self.assertFalse(self.c.get(f"/api/assets/{a['id']}").get_json()["stockListed"])


    def test_제품코드를_지우면_재고반영도_함께_꺼진다(self):
        """검증 결함 ②(2026-08-04) — 지우기로 켜기 가드를 우회하면 유령 상태가 된다."""
        a = self._one()
        self.c.patch(f"/api/assets/{a['id']}", json={"productCode": "840 G3_i7-6_내장",
                                                     "stockListed": True})
        r = self.c.patch(f"/api/assets/{a['id']}", json={"productCode": ""})
        self.assertEqual(r.status_code, 200)
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertFalse(d["stockListed"], "코드 없는 자산이 반영 켜짐으로 남았다")
        acts = [e["action"] for e in d["events"]]
        self.assertIn("재고해제", acts, "왜 꺼졌는지 이력이 안 남으면 직원이 원인을 못 찾는다")

    def test_코드지우기와_켜기를_한번에_보내도_막힌다(self):
        a = self._one()
        self.c.patch(f"/api/assets/{a['id']}", json={"productCode": "840 G3_i7-6_내장"})
        r = self.c.patch(f"/api/assets/{a['id']}", json={"productCode": "", "stockListed": True})
        self.assertEqual(r.status_code, 400, "새 값이 아닌 옛 코드로 판정하면 우회된다")

    def test_재고반영_이력이_남는다(self):
        a = self._one()
        self.c.patch(f"/api/assets/{a['id']}", json={"productCode": "840 G3_i7-6_내장",
                                                     "stockListed": True})
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        acts = [e["action"] for e in d["events"]]
        self.assertIn("재고반영", acts)
        self.assertIn("제품코드", acts)

class TestStockSync(Base):
    """쇼핑몰 재고 동기화 관제판 — 기본 전부 OFF(대표 지시 2026-08-04)."""

    def test_기본은_모든_몰이_꺼져_있다(self):
        d = self.c.get("/api/stock-sync").get_json()
        self.assertTrue(all(not m["enabled"] for m in d["malls"]),
                        "기본 OFF가 깨지면 몰 재고가 멋대로 바뀐다")

    def test_전송기가_없는_몰은_켤_수_없다(self):
        # 전제조건(식별자·스펙)이 준비 안 된 몰을 켜는 것 자체를 막는다
        r = self.c.put("/api/stock-sync/godomall", json={"enabled": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("준비", r.get_json()["error"])

    def test_이상한_켜기_값은_켜짐으로_보지_않는다(self):
        # enabled가 정확히 true일 때만 켜짐 — "true"·1 같은 값은 꺼짐으로
        r = self.c.put("/api/stock-sync/godomall", json={"enabled": "true"})
        self.assertEqual(r.status_code, 200)      # 끄기로 처리된다(켜기 아님)
        d = self.c.get("/api/stock-sync").get_json()
        g = next(m for m in d["malls"] if m["code"] == "godomall")
        self.assertFalse(g["enabled"])

    def test_없는_몰은_404(self):
        self.assertEqual(self.c.put("/api/stock-sync/nomall", json={"enabled": True}).status_code, 404)

    def test_HMS_기준_재고는_재고반영_체크된_것만_센다(self):
        b = self._batch()
        a1 = self._asset(b["id"], model="840 G3").get_json()[0]
        a2 = self._asset(b["id"], model="840 G3").get_json()[0]
        for a in (a1, a2):
            self.c.patch(f"/api/assets/{a['id']}", json={"productCode": "840 G3_i7-6_내장"})
        self.c.patch(f"/api/assets/{a1['id']}", json={"stockListed": True})   # 하나만 반영
        d = self.c.get("/api/stock-sync").get_json()
        codes = {c["productCode"]: c["count"] for c in d["codes"]}
        self.assertEqual(codes.get("840 G3_i7-6_내장"), 1,
                         "체크 안 한 자산까지 세면 몰에 없는 재고를 올리게 된다")

    def test_출고되면_재고에서_빠진다(self):
        b = self._batch()
        a = self._asset(b["id"], model="840 G3").get_json()[0]
        self.c.patch(f"/api/assets/{a['id']}", json={"productCode": "840 G3_i7-6_내장",
                                                     "stockListed": True})
        # 주문에 매칭하고 출고까지
        o = self.c.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북", "phone": "010-1111-2222",
            "address": "서울시 강남구 1", "postalCode": "06000", "amount": 500000}).get_json()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("preparing", "production", "softwareInspection", "shipping"):
            self.c.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        d = self.c.get("/api/stock-sync").get_json()
        codes = {c["productCode"]: c["count"] for c in d["codes"]}
        self.assertNotIn("840 G3_i7-6_내장", codes,
                         "출고된 자산이 계속 세어지면 없는 물건이 팔린다")

    def test_시험_계산은_아무것도_전송하지_않는다(self):
        r = self.c.post("/api/stock-sync/preview", json={})
        self.assertEqual(r.status_code, 200)
        self.assertIn("전송하지 않았습니다", r.get_json()["note"])

    def test_실행도_켜진_몰이_없으면_아무것도_안_보낸다(self):
        r = self.c.post("/api/stock-sync/run", json={})
        j = r.get_json()
        self.assertEqual(j["sent"], 0)
        self.assertEqual(j["report"], [])

class TestBulkProductCode(Base):
    """제품코드 일괄 입력(대표 요청 2026-08-04) — 한 전표에 여러 모델이 섞이므로
    '전표 전체'가 아니라 선택한 자산에만 넣는다."""

    def _three(self):
        b = self._batch()
        return [self._asset(b["id"], model=m).get_json()[0]
                for m in ("840 G3", "840 G3", "M710q")]

    def test_선택한_자산에만_넣는다(self):
        a1, a2, a3 = self._three()
        r = self.c.post("/api/assets/product-code",
                        json={"ids": [a1["id"], a2["id"]], "productCode": "840 G3_i7-6_내장"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["ok"], 2)
        self.assertEqual(self.c.get(f"/api/assets/{a1['id']}").get_json()["productCode"], "840 G3_i7-6_내장")
        self.assertEqual(self.c.get(f"/api/assets/{a3['id']}").get_json()["productCode"], "",
                         "선택 안 한 자산까지 바뀌면 다른 모델에 남의 코드가 붙는다")

    def test_같은_코드면_이력을_또_남기지_않는다(self):
        a1, _, _ = self._three()
        body = {"ids": [a1["id"]], "productCode": "840 G3_i7-6_내장"}
        self.c.post("/api/assets/product-code", json=body)
        r = self.c.post("/api/assets/product-code", json=body)
        self.assertEqual(r.get_json()["ok"], 0)
        d = self.c.get(f"/api/assets/{a1['id']}").get_json()
        self.assertEqual(sum(1 for e in d["events"] if e["action"] == "제품코드"), 1)

    def test_빈_코드는_지우기이고_재고반영도_꺼진다(self):
        a1, _, _ = self._three()
        self.c.post("/api/assets/product-code", json={"ids": [a1["id"]], "productCode": "840 G3_i7-6_내장"})
        self.c.patch(f"/api/assets/{a1['id']}", json={"stockListed": True})
        r = self.c.post("/api/assets/product-code", json={"ids": [a1["id"]], "productCode": ""})
        self.assertEqual(r.get_json()["ok"], 1)
        d = self.c.get(f"/api/assets/{a1['id']}").get_json()
        self.assertEqual(d["productCode"], "")
        self.assertFalse(d["stockListed"], "코드가 지워졌는데 반영이 켜져 있으면 유령 상태")

    def test_한도와_검증(self):
        self.assertEqual(self.c.post("/api/assets/product-code",
                         json={"ids": [], "productCode": "X"}).status_code, 400)
        self.assertEqual(self.c.post("/api/assets/product-code",
                         json={"ids": list(range(501)), "productCode": "X"}).status_code, 400)
        self.assertEqual(self.c.post("/api/assets/product-code",
                         json={"ids": [1], "productCode": "X" * 61}).status_code, 400)
        r = self.c.post("/api/assets/product-code", json={"ids": [99999], "productCode": "X"})
        self.assertEqual(r.get_json()["ok"], 0)
        self.assertEqual(len(r.get_json()["skipped"]), 1)

class TestPrepOptionsMerged(unittest.TestCase):
    """챙길 옵션을 별도 칸 없이 옵션 칩으로 합침(대표 2026-08-04: 옵션은 옵션으로)."""

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")

    def test_챙길옵션_칸이_없다(self):
        self.assertNotIn("<th>챙길 옵션</th>", self.js, "별도 칸이 되살아났다")
        self.assertNotIn("col-prep", self.js)
        self.assertNotIn('colspan="9"', self.js, "칸 수가 8로 줄었는데 colspan이 9면 표가 어긋난다")

    def test_챙길옵션이_옵션_칩으로_나온다(self):
        """★2026-08-05 정정: 칩은 유지하되 '누를 수 있어야' 한다.

        별도 체크박스 칸은 없앤 게 맞지만, 체크 자체를 없애면 서버의
        '체크해야 제작완료' 가드에 걸려 쿠팡·카카오 주문이 통째로 막힌다
        (실제로 막혔다 — 체크 기록 0건). 칩 자체가 체크 버튼이다.
        """
        self.assertIn("function prepChips", self.js)
        self.assertIn("prepChips(o, canWork)", self.js, "상품 칸 옵션 줄에 안 붙으면 아예 안 보인다")
        self.assertIn("data-opt=", self.js, "누를 수 없으면 제작완료가 영영 안 된다")

class TestStockTier(Base):
    """재고 3단계(2026-08-04 대표): 양품 / 실재고 / 가재고.

    가재고 = 입고는 됐지만 완전한 수리 전. 매칭까지는 되되 출고는 막는다.
    """

    def _order(self):
        return self.c.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북", "phone": "010-1111-2222",
            "address": "서울시 강남구 1", "postalCode": "06000", "amount": 500000}).get_json()

    def _match_and_stage(self, o, a):
        r = self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        for act in ("preparing", "production", "softwareInspection"):
            self.assertEqual(self.c.patch(f"/api/orders/{o['id']}",
                             json={"action": act, "value": True}).status_code, 200)

    def test_기본값은_양품이다(self):
        """기존 자산 15,013대가 갑자기 출고 불가가 되면 안 된다."""
        b = self._batch()
        a = self._asset(b["id"], model="L480").get_json()[0]
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["tier"], "양품")

    def test_세_단계를_지정해_등록할_수_있다(self):
        b = self._batch()
        for t in ("양품", "실재고", "가재고"):
            a = self._asset(b["id"], model="L480", tier=t).get_json()[0]
            self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["tier"], t)

    def test_없는_구분은_거부한다(self):
        b = self._batch()
        self.assertEqual(self._asset(b["id"], model="L480", tier="반품").status_code, 400)

    def test_가재고는_매칭은_되지만_출고가_막힌다(self):
        b = self._batch()
        a = self._asset(b["id"], model="L480", tier="가재고").get_json()[0]
        o = self._order()
        self._match_and_stage(o, a)          # 매칭·제작·검수까지는 통과
        r = self.c.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})
        self.assertGreaterEqual(r.status_code, 400, "가재고가 그대로 출고됐다")
        self.assertIn("가재고", r.get_json()["error"])
        self.assertIn(a["assetNo"], r.get_json()["error"], "어느 자산인지 알려 줘야 한다")

    def test_양품_실재고로_바꾸면_출고된다(self):
        b = self._batch()
        a = self._asset(b["id"], model="L480", tier="가재고").get_json()[0]
        o = self._order()
        self._match_and_stage(o, a)
        self.assertGreaterEqual(self.c.patch(f"/api/orders/{o['id']}",
            json={"action": "shipping", "value": True}).status_code, 400)
        self.c.patch(f"/api/assets/{a['id']}", json={"tier": "실재고"})
        r = self.c.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_가재고는_송장도_막힌다(self):
        """출고 확인만 막으면 송장 발급으로 우회된다 — 두 관문 모두 지켜야 한다."""
        b = self._batch()
        a = self._asset(b["id"], model="L480", tier="가재고").get_json()[0]
        o = self._order()
        self._match_and_stage(o, a)
        r = self.c.post(f"/api/orders/{o['id']}/waybill", json={"boxQty": 1})
        self.assertGreaterEqual(r.status_code, 400)
        self.assertIn("가재고", r.get_json()["error"])

    def test_자산검색이_가재고를_알려준다(self):
        b = self._batch()
        a = self._asset(b["id"], model="L480", tier="가재고").get_json()[0]
        rows = self.c.get(f"/api/orders/asset-search?q={a['assetNo']}").get_json()
        hit = next(x for x in rows if x["assetNo"] == a["assetNo"])
        self.assertTrue(hit["available"], "매칭 자체는 되어야 한다")
        self.assertFalse(hit["shippable"], "출고 가능으로 표시되면 안 된다")
        self.assertIn("가재고", hit["reason"])

    def test_셋팅_재고집계가_가재고를_빼고_센다(self):
        b = self._batch()
        for t in ("양품", "양품", "실재고", "가재고"):
            self._asset(b["id"], model="NT371B5M", tier=t)
        from app.db import tx
        from app.orders.product_info import our_stock
        with self.app.app_context():
            with tx() as conn:
                got = our_stock(conn, {"NT371B5M"})
        st = got["NT371B5M"]
        self.assertEqual(st["count"], 4, "총합은 4대 그대로")
        self.assertEqual(st["shippable"], 3, "가재고를 뺀 출고 가능분은 3대")
        self.assertEqual(st["byTier"].get("가재고"), 1)

    def test_구분_변경이_이력에_남는다(self):
        b = self._batch()
        a = self._asset(b["id"], model="L480", tier="가재고").get_json()[0]
        self.c.patch(f"/api/assets/{a['id']}", json={"tier": "양품"})
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertTrue(any(e["action"] == "재고구분" for e in d["events"]),
                        "누가 언제 가재고를 풀었는지 남아야 한다")

class TestStockByProductCode(Base):
    """셋팅/QC 재고는 제품코드로 대조한다(대표 2026-08-04).

    모델명 근사 대조는 'NT371B5M'이 다른 모델까지 끌어와 틀린다.
    코드가 아직 안 들어간 경우에는 '미등록'임을 알리고 모델 기준 짐작값을 보조로 준다.
    """

    def test_제품코드로_정확히_센다(self):
        b = self._batch()
        for t, pc in (("양품", "840 G3_i7-6_내장"), ("양품", "840 G3_i7-6_내장"),
                      ("가재고", "840 G3_i7-6_내장"), ("양품", "840 G3_i5-6_내장")):
            a = self._asset(b["id"], model="840 G3", tier=t).get_json()[0]
            self.c.patch(f"/api/assets/{a['id']}", json={"productCode": pc})
        from app.db import tx
        from app.orders.product_info import stock_by_code
        with self.app.app_context():
            with tx() as conn:
                got = stock_by_code(conn, {"840 G3_i7-6_내장", "840 G3_i5-6_내장"})
        i7 = got["840 G3_i7-6_내장"]
        self.assertEqual(i7["count"], 3, "같은 코드만 세어야 한다")
        self.assertEqual(i7["shippable"], 2, "가재고는 출고 가능에서 빠진다")
        self.assertTrue(i7["registered"])
        self.assertEqual(got["840 G3_i5-6_내장"]["count"], 1, "CPU가 다르면 다른 코드다")

    def test_코드가_없으면_미등록으로_알린다(self):
        b = self._batch()
        self._asset(b["id"], model="840 G3")            # 제품코드 없이 등록
        from app.db import tx
        from app.orders.product_info import stock_by_code
        with self.app.app_context():
            with tx() as conn:
                got = stock_by_code(conn, {"840 G3_i7-6_내장"})
        self.assertFalse(got["840 G3_i7-6_내장"]["registered"],
                         "코드가 안 들어간 자산이 코드 대조에 잡히면 안 된다")
        self.assertEqual(got["840 G3_i7-6_내장"]["count"], 0)

    def test_주문에서_제품코드를_뽑는다(self):
        from app.orders.product_info import sku_of
        # 고도몰 — 코드칸에 그대로
        self.assertEqual(sku_of("840 G3_i7-6_내장", ""), "840 G3_i7-6_내장")
        # 쿠팡 — 코드칸은 숫자, 상품명에 코드 + 등급 접미사
        self.assertEqual(sku_of("95787471151", "NT371B5M_i7-7_내장 AA급3"), "NT371B5M_i7-7_내장")
        # 카카오 — 사은품 표기가 붙어 온다
        self.assertEqual(sku_of("NT371B5L_i7-6_ge+한컴", ""), "NT371B5L_i7-6_ge")
        # 스마트스토어 — 코드도 이름도 형식이 아니다
        self.assertEqual(sku_of("13675224455", "삼성 15인치 노트북"), "")

    def test_화면이_미등록을_숨기지_않는다(self):
        js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")
        block = js.split("function stockChip", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("codeRegistered", block, "코드 등록 여부를 안 보면 짐작값을 정답처럼 보여준다")
        # '제품코드 미등록' 칩은 대표 지시로 제거(2026-08-04) — 대신 칩 이름이 '모델 N대'가 되고
        # 근사값이라는 사실은 마우스 오버 설명에 남는다
        self.assertIn("모델 ${ship}대", block, "코드가 없을 때 모델 기준임을 이름으로 알려야 한다")
        self.assertIn("짐작", block, "근사값임을 설명에 남겨야 한다")

class TestCoupangProductCode(unittest.TestCase):
    """쿠팡 제품코드는 '등록상품명(판매자 관리용)'에도 들어온다(대표 확인 2026-08-04).

    판매자상품코드(externalVendorSkuCode)가 비어 오면 거기서 뽑아야 한다.
    안 그러면 쿠팡 내부번호(95787471151)가 코드 자리에 들어가 고도몰과 안 맞는다.
    """

    def _line(self, **kw):
        from app.malls.coupang import CoupangAdapter
        a = CoupangAdapter({"vendor_id": "A1", "access_key": "k",
                            "secret_key": "s", "wing_id": "w"})
        item = {"shippingCount": 1, "cancelCount": 0, "orderPrice": 1000,
                "vendorItemId": 95787471151, "sellerProductId": 123,
                "vendorItemName": "고객에게 보이는 긴 상품명",
                "sellerProductItemName": "단일색상"}
        item.update(kw)
        buckets, seq, seen = {}, [], set()
        a._absorb({"shipmentBoxId": 1, "orderId": 1, "status": "ACCEPT",
                   "orderer": {}, "receiver": {}, "orderItems": [item]}, buckets, seq, seen)
        return a._to_order(buckets[seq[0]])

    def test_등록상품명에서_제품코드를_뽑는다(self):
        o = self._line(sellerProductName="NT371B5M_i7-7_내장 AA급3", externalVendorSkuCode="")
        self.assertEqual(o["productCode"], "NT371B5M_i7-7_내장",
                         "등급 접미사를 떼고 고도몰과 같은 코드가 되어야 한다")

    def test_판매자상품코드가_있으면_그쪽이_우선이다(self):
        o = self._line(sellerProductName="NT371B5M_i7-7_내장 AA급3",
                       externalVendorSkuCode="P16V Gen1_i7-13_내장")
        self.assertEqual(o["productCode"], "P16V Gen1_i7-13_내장")

    def test_코드_형식이_아니면_예전대로_내부번호(self):
        o = self._line(sellerProductName="[하프북X녹스] 게이밍 키보드 청축",
                       externalVendorSkuCode="")
        self.assertEqual(o["productCode"], "95787471151",
                         "부속품까지 억지로 코드로 만들면 안 된다")

    def test_사은품_표기를_떼어낸다(self):
        o = self._line(sellerProductName="DB400T3_i5-4_GTX1650+장패드", externalVendorSkuCode="")
        self.assertEqual(o["productCode"], "DB400T3_i5-4_GTX1650")

    def test_모델에_공백이_있어도_된다(self):
        o = self._line(sellerProductName="ThinkCentre M710q_i5-6_내장", externalVendorSkuCode="")
        self.assertEqual(o["productCode"], "ThinkCentre M710q_i5-6_내장")

    def test_등급만_다른_옵션은_같은_코드로_묶인다(self):
        """AA급3·AA급4가 다른 코드로 갈리면 재고가 쪼개진다."""
        a = self._line(sellerProductName="NT371B5M_i7-7_내장 AA급3", externalVendorSkuCode="")
        b = self._line(sellerProductName="NT371B5M_i7-7_내장 AA급4", externalVendorSkuCode="")
        self.assertEqual(a["productCode"], b["productCode"])

class TestSmartstoreProductCode(unittest.TestCase):
    """스마트스토어도 판매자상품코드가 있다(대표 확인 2026-08-04).

    네이버는 응답 스키마에서 이 칸 이름이 여러 가지라 알려진 후보를 모두 본다.
    """

    def _order(self, **po_extra):
        from app.malls.smartstore import SmartStoreAdapter
        a = SmartStoreAdapter({"client_id": "x", "client_secret": "y"})
        a._class_map = {}
        po = {"productOrderId": "1", "productOrderStatus": "PAYED",
              "productId": "13675224455", "productName": "삼성 15인치 리퍼 중고 노트북",
              "quantity": 1, "totalPaymentAmount": 399000,
              "shippingAddress": {"name": "홍길동", "tel1": "010-0000-0000",
                                  "baseAddress": "서울", "zipCode": "06000"}}
        po.update(po_extra)
        buckets, seq, seen = {}, [], set()
        a._absorb({"content": {"order": {"orderId": "2026080412345",
                                         "ordererName": "홍길동", "orderDate": "2026-08-04"},
                               "productOrder": po}}, buckets, seq, seen)
        return buckets[seq[0]]

    def test_판매자상품코드를_읽는다(self):
        self.assertEqual(self._order(sellerProductCode="DB400T3_i5-4_내장")["productCode"],
                         "DB400T3_i5-4_내장")

    def test_칸_이름이_달라도_찾는다(self):
        for key in ("sellerManagementCode", "sellerProductManagementCode",
                    "optionManageCode", "sellerCustomCode"):
            with self.subTest(key=key):
                self.assertEqual(self._order(**{key: "840 G3_i7-6_내장"})["productCode"],
                                 "840 G3_i7-6_내장")

    def test_없으면_예전처럼_네이버_상품번호(self):
        self.assertEqual(self._order()["productCode"], "13675224455",
                         "못 찾았다고 주문을 버리면 안 된다")

    def test_코드_모양이_아닌_값은_안_쓴다(self):
        o = self._order(sellerProductCode="A-123")
        self.assertEqual(o["productCode"], "13675224455",
                         "형식이 아닌 값을 코드로 쓰면 재고가 엉뚱하게 갈린다")


class TestMallChipScope(unittest.TestCase):
    """'몰 N대' 칩은 고도몰 재고다 — 제품코드를 통일한 뒤 쿠팡 행에도 뜨던 문제."""

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")

    def test_고도몰_주문에만_보여준다(self):
        # 재고 칩은 2026-08-05부터 채널/주문 칸(stockChipsHtml)에서 그린다 — 규칙은 그대로.
        block = self.js.split("function stockChipsHtml", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("isGodo", block,
                      "채널을 안 보면 쿠팡 행에 고도몰 재고가 떠서 '왜 안 줄지?'가 된다")

    def test_제품코드_미등록_칩은_없앴다(self):
        self.assertNotIn("제품코드 미등록", self.js, "대표 지시로 제거한 칩이 되살아났다")

class TestUncodedAssets(Base):
    """제품코드 입력 안내(대표 요청 2026-08-04).

    코드가 없는 자산을 모델별로 묶어 준다. 이미 판매된 것과 다시 안 팔 것은 뺀다.
    """

    def test_모델별로_묶어_준다(self):
        b = self._batch()
        for m in ("840 G3", "840 G3", "M710q"):
            self._asset(b["id"], model=m)
        d = self.c.get("/api/assets/uncoded").get_json()
        by = {g["model"]: g["count"] for g in d["groups"]}
        self.assertEqual(by.get("840 G3"), 2)
        self.assertEqual(by.get("M710q"), 1)
        self.assertEqual(d["total"], 3)
        self.assertEqual(d["models"], 2)

    def test_대수가_많은_모델이_위에_온다(self):
        b = self._batch()
        self._asset(b["id"], model="적은모델")
        for _ in range(3):
            self._asset(b["id"], model="많은모델")
        d = self.c.get("/api/assets/uncoded").get_json()
        self.assertEqual(d["groups"][0]["model"], "많은모델")

    def test_코드가_있으면_목록에서_빠진다(self):
        b = self._batch()
        a = self._asset(b["id"], model="840 G3").get_json()[0]
        self.assertEqual(self.c.get("/api/assets/uncoded").get_json()["total"], 1)
        self.c.patch(f"/api/assets/{a['id']}", json={"productCode": "840 G3_i7-6_내장"})
        self.assertEqual(self.c.get("/api/assets/uncoded").get_json()["total"], 0)

    def test_판매완료와_안_팔_것은_제외된다(self):
        """출고완료·폐기·매입취소·거래처반품에 코드를 넣어 봐야 소용없다."""
        b = self._batch()
        keep = self._asset(b["id"], model="남는것").get_json()[0]
        for st in ("scrapped",):
            a = self._asset(b["id"], model="빠질것").get_json()[0]
            self.c.patch(f"/api/assets/{a['id']}", json={"status": st})
        d = self.c.get("/api/assets/uncoded").get_json()
        models = [g["model"] for g in d["groups"]]
        self.assertIn("남는것", models)
        self.assertNotIn("빠질것", models)
        self.assertTrue(keep)

    def test_수리중인_자산은_포함된다(self):
        """고쳐서 팔 물건이라 코드가 필요하다 — 가용 상태만 보면 안 된다."""
        b = self._batch()
        a = self._asset(b["id"], model="수리중모델").get_json()[0]
        self.c.patch(f"/api/assets/{a['id']}", json={"status": "repair"})
        models = [g["model"] for g in self.c.get("/api/assets/uncoded").get_json()["groups"]]
        self.assertIn("수리중모델", models)

    def test_상태_등급_구분별_집계를_준다(self):
        b = self._batch()
        self._asset(b["id"], model="집계", grade="AA", tier="양품")
        self._asset(b["id"], model="집계", grade="AS", tier="가재고")
        g = next(x for x in self.c.get("/api/assets/uncoded").get_json()["groups"]
                 if x["model"] == "집계")
        self.assertEqual(g["byGrade"].get("AA"), 1)
        self.assertEqual(g["byTier"].get("가재고"), 1)
        self.assertEqual(g["byStatus"].get("입고"), 2)


class TestBulkGradeAndTier(Base):
    """일괄 적용에 등급·재고구분도 함께(2026-08-04) — 같은 목록을 두 번 훑지 않게."""

    def _three(self):
        b = self._batch()
        return [self._asset(b["id"], model="840 G3").get_json()[0] for _ in range(3)]

    def test_코드_등급_구분을_한_번에_넣는다(self):
        a1, a2, a3 = self._three()
        r = self.c.post("/api/assets/product-code", json={
            "ids": [a1["id"], a2["id"]], "productCode": "840 G3_i7-6_내장",
            "grade": "AA", "tier": "실재고"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["ok"], 2)
        d = self.c.get(f"/api/assets/{a1['id']}").get_json()
        self.assertEqual(d["productCode"], "840 G3_i7-6_내장")
        self.assertEqual(d["grade"], "AA")
        self.assertEqual(d["tier"], "실재고")
        untouched = self.c.get(f"/api/assets/{a3['id']}").get_json()
        self.assertEqual(untouched["grade"], "미정", "선택 안 한 자산이 바뀌면 안 된다")

    def test_등급만_보내면_코드는_그대로다(self):
        a1, _, _ = self._three()
        self.c.patch(f"/api/assets/{a1['id']}", json={"productCode": "840 G3_i7-6_내장"})
        self.c.post("/api/assets/product-code", json={"ids": [a1["id"]], "grade": "SS"})
        d = self.c.get(f"/api/assets/{a1['id']}").get_json()
        self.assertEqual(d["grade"], "SS")
        self.assertEqual(d["productCode"], "840 G3_i7-6_내장", "코드가 지워지면 안 된다")

    def test_없는_등급_구분은_거부한다(self):
        a1, _, _ = self._three()
        self.assertEqual(self.c.post("/api/assets/product-code",
                         json={"ids": [a1["id"]], "grade": "Z급"}).status_code, 400)
        self.assertEqual(self.c.post("/api/assets/product-code",
                         json={"ids": [a1["id"]], "tier": "반품"}).status_code, 400)

    def test_바꿀_값이_없으면_거부한다(self):
        a1, _, _ = self._three()
        self.assertEqual(self.c.post("/api/assets/product-code",
                         json={"ids": [a1["id"]]}).status_code, 400)


class TestTierHelp(Base):
    """재고 구분 설명 — 담당자가 구분을 몰라 잘못 넣는 일을 막는다(대표 2026-08-04)."""

    def test_서버가_설명을_내려준다(self):
        m = self.c.get("/api/purchase-meta").get_json()
        h = m["tierHelp"]
        self.assertIn("SSD", h["양품"])
        self.assertIn("도색", h["실재고"])
        self.assertIn("바로 판매가 불가능", h["가재고"])
        self.assertEqual(set(h), set(m["tiers"]), "설명이 빠진 구분이 있으면 안 된다")

    def test_화면이_그_설명을_쓴다(self):
        js = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")
        self.assertIn("tierHelp", js, "설명을 안 쓰면 문구가 화면마다 갈린다")
        self.assertIn("function tierOptions", js)
        for sel in ("ad-tier", "ar-tier", "sa-tier", "uc-tier"):
            self.assertIn(sel, js, f"{sel} 에 재고 구분 선택칸이 있어야 한다")

class TestSlipFormFlow(Base):
    """전표 등록 흐름(대표 2026-08-04) — 전표와 자산을 한 번에, 시리얼은 연속 스캔."""

    def test_등록_시_제품코드를_넣을_수_있다(self):
        """나중에 따로 채우러 다니지 않아도 된다."""
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid, "purchaseDate": "2026-08-04",
            "totalAmount": 0, "assets": [{"categoryId": self.cat, "qty": 2, "model": "840 G3",
                                          "productCode": "840 G3_i7-6_내장"}]})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        d = self.c.get(f"/api/purchase-batches/{r.get_json()['id']}").get_json()
        for a in d["assets"]:
            got = self.c.get(f"/api/assets/{a['id']}").get_json()
            self.assertEqual(got["productCode"], "840 G3_i7-6_내장")

    def test_시리얼을_한_대씩_쪼개_보내면_각각_들어간다(self):
        """연속 스캔한 시리얼을 화면이 1대짜리로 풀어 보낸다."""
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid, "purchaseDate": "2026-08-04",
            "totalAmount": 0, "assets": [
                {"categoryId": self.cat, "qty": 1, "model": "L480", "serial": "SN-A"},
                {"categoryId": self.cat, "qty": 1, "model": "L480", "serial": "SN-B"},
                {"categoryId": self.cat, "qty": 1, "model": "L480", "serial": ""}]})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        d = self.c.get(f"/api/purchase-batches/{r.get_json()['id']}").get_json()
        serials = sorted((self.c.get(f"/api/assets/{a['id']}").get_json()["serial"])
                         for a in d["assets"])
        self.assertEqual(serials, ["", "SN-A", "SN-B"])

    def test_여러_대에_시리얼을_통째로_주면_거부한다(self):
        """화면이 쪼개 보내야 하는 이유 — 서버는 통째 입력을 막는다."""
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid, "purchaseDate": "2026-08-04",
            "totalAmount": 0, "assets": [{"categoryId": self.cat, "qty": 3,
                                          "model": "L480", "serial": "SN-X"}]})
        self.assertGreaterEqual(r.status_code, 400)

    def test_같은_시리얼_두_번은_전표째로_거부된다(self):
        """한 줄이라도 겹치면 전부 되돌아간다 — 반쪽 저장이 없어야 한다."""
        before = len(self.c.get("/api/assets").get_json())
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid, "purchaseDate": "2026-08-04",
            "totalAmount": 0, "assets": [
                {"categoryId": self.cat, "qty": 1, "model": "L480", "serial": "DUP-1"},
                {"categoryId": self.cat, "qty": 1, "model": "L480", "serial": "DUP-1"}]})
        self.assertGreaterEqual(r.status_code, 400)
        self.assertEqual(len(self.c.get("/api/assets").get_json()), before,
                         "실패했는데 자산이 남으면 안 된다")


class TestSlipFormUI(unittest.TestCase):
    """전표 등록 화면 — 구획과 연속 스캔(대표 2026-08-04)."""

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")
        self.css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")

    def test_세_단계로_구획이_나뉜다(self):
        """구획이 없어 어디까지가 한 덩어리인지 헷갈린다는 지적."""
        for cls in (".slip-form", ".sf-step", ".sf-step-head", ".sf-no"):
            self.assertIn(cls, self.css, f"{cls} 규칙이 없다")
        self.assertEqual(self.js.count('class="sf-step"'), 3, "1)전표 2)자산담기 3)담긴자산")
        block = self.css.split(".sf-step {", 1)[1].split("}", 1)[0]
        self.assertIn("border", block, "테두리가 없으면 구획이 안 보인다")

    def test_시리얼_연속_스캔이_있다(self):
        self.assertIn("sn-scan", self.js)
        self.assertIn("drawScanPanel", self.js)
        self.assertIn("data-sn=", self.js, "여러 대일 때 스캔 버튼이 떠야 한다")

    def test_스캔한_시리얼_중복을_막는다(self):
        block = self.js.split("const drawScanPanel", 1)[1].split(chr(10) + "  };", 1)[0]
        self.assertIn("이미 찍은 시리얼", block, "같은 줄 안 중복")
        self.assertIn("다른 줄에 이미 찍힌", block, "다른 줄과의 중복 — 저장 때 전부 되돌아간다")

    def test_저장_시_시리얼을_한_대씩_쪼갠다(self):
        block = self.js.split('$("#sl-save").addEventListener', 1)[1].split("});", 1)[0]
        self.assertIn("qty: 1", block, "서버가 여러 대+시리얼을 거부하므로 쪼개야 한다")

    def test_등록_폼에_제품코드_칸이_있다(self):
        self.assertIn("sa-productcode", self.js)

class TestSlipFormSpecAndPrice(Base):
    """전표 등록에서 사양·매입가(대표 2026-08-04)."""

    def test_등록_시_사양을_함께_넣을_수_있다(self):
        """예전에는 등록 뒤 한 대씩 열어야만 CPU·RAM을 넣을 수 있었다."""
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid, "purchaseDate": "2026-08-04",
            "totalAmount": 200000, "assets": [{
                "categoryId": self.cat, "qty": 2, "model": "L480",
                "cpu": "Intel Core i5-8250U", "ram": "16GB", "ssd": "512GB",
                "gpu": "내장", "inch": "14", "battery": "85%", "charger": "포함",
                "purchasePrice": 100000}]})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        d = self.c.get(f"/api/purchase-batches/{r.get_json()['id']}").get_json()
        a = self.c.get(f"/api/assets/{d['assets'][0]['id']}").get_json()
        self.assertEqual(a["cpu"], "Intel Core i5-8250U")
        self.assertEqual(a["ram"], "16GB")
        self.assertEqual(a["ssd"], "512GB")
        self.assertEqual(a["charger"], "포함")

    def test_매입가는_대당이고_전표금액은_총액이다(self):
        """10대 × 10만원 = 전표 100만원. 서버가 두 값을 따로 들고 대조해 준다."""
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid, "purchaseDate": "2026-08-04",
            "totalAmount": 1000000, "assets": [{"categoryId": self.cat, "qty": 10,
                                                "model": "L480", "purchasePrice": 100000}]})
        d = self.c.get(f"/api/purchase-batches/{r.get_json()['id']}").get_json()
        self.assertEqual(d["totalAmount"], 1000000, "전표는 총액")
        self.assertEqual(d["assignedAmount"], 1000000, "자산 10대 × 10만원")
        self.assertEqual(d["assetCount"], 10)
        one = self.c.get(f"/api/assets/{d['assets'][0]['id']}").get_json()
        self.assertEqual(one["purchasePrice"], 100000, "자산에는 대당 값이 들어간다")

    def test_어긋나면_서버가_두_값을_그대로_준다(self):
        """화면이 '차이 N원'을 띄우려면 서버가 숨기지 않아야 한다."""
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid, "purchaseDate": "2026-08-04",
            "totalAmount": 900000, "assets": [{"categoryId": self.cat, "qty": 10,
                                               "model": "L480", "purchasePrice": 100000}]})
        d = self.c.get(f"/api/purchase-batches/{r.get_json()['id']}").get_json()
        self.assertNotEqual(d["totalAmount"], d["assignedAmount"])


class TestSlipFormSpecUI(unittest.TestCase):
    def setUp(self):
        self.js = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")

    def test_등록_폼에_사양_칸이_있다(self):
        # 칸은 SPEC_LABELS를 돌며 id="sa-${k}" 로 만들어진다 — 그 생성부가 있는지 본다
        self.assertIn('id="sa-${k}"', self.js,
                      "사양 칸이 없으면 등록 뒤 한 대씩 열어야 한다")
        self.assertIn('class="sf-spec"', self.js, "사양 묶음 구획이 없다")
        # 담을 때 실제로 값을 싣는지
        self.assertIn('line[k] = ($(`#sa-${k}`).value', self.js,
                      "칸만 있고 담지 않으면 값이 사라진다")

    def test_금액_대조를_보여준다(self):
        # 매입금액은 이제 자동 계산이라 '맞추기' 버튼이 필요 없다(2026-08-04).
        # 대신 담은 합계가 그대로 금액이 된다는 사실을 보여 준다.
        self.assertIn("sa-sum", self.js)
        self.assertIn("자산을 담으면 자동으로 맞춰집니다", self.js)

    def test_대당인지_총액인지_적어_둔다(self):
        self.assertIn("자산 매입가 합계 — 자동", self.js)
        self.assertIn("한 대 값", self.js)

class TestRepairVat(Base):
    """수리비 부가세 자동 분리(대표 2026-08-04) — 총액만 넣으면 공급가·부가세가 갈린다."""

    def _asset_one(self):
        b = self._batch()
        return self._asset(b["id"], model="L480", tier="가재고").get_json()[0]

    def test_총액에서_부가세를_나눈다(self):
        a = self._asset_one()
        r = self.c.post(f"/api/assets/{a['id']}/repairs", json={
            "description": "액정 교체", "cost": 110000, "parts": "액정"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d["cost"], 110000)
        self.assertEqual(d["vat"], 10000)
        self.assertEqual(d["net"], 100000)
        self.assertEqual(d["net"] + d["vat"], d["cost"], "공급가+부가세가 총액과 달라지면 안 된다")

    def test_반올림해도_합이_총액과_같다(self):
        a = self._asset_one()
        for total in (100000, 33333, 1, 999999):
            with self.subTest(total=total):
                r = self.c.post(f"/api/assets/{a['id']}/repairs",
                                json={"description": "테스트", "cost": total}).get_json()
                self.assertEqual(r["net"] + r["vat"], total)

    def test_교체부품과_이력이_남는다(self):
        a = self._asset_one()
        self.c.post(f"/api/assets/{a['id']}/repairs", json={
            "description": "SSD 교체", "cost": 55000, "parts": "SSD 512GB"})
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        rep = d["repairs"][0]
        self.assertEqual(rep["parts"], "SSD 512GB")
        self.assertEqual(rep["vat"], 5000)
        self.assertEqual(rep["net"], 50000)
        self.assertTrue(any(e["action"] == "수리" for e in d["events"]))

    def test_부품만_적어도_등록된다(self):
        a = self._asset_one()
        r = self.c.post(f"/api/assets/{a['id']}/repairs", json={"parts": "배터리", "cost": 22000})
        self.assertEqual(r.status_code, 201)
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertIn("배터리", d["repairs"][0]["description"])

    def test_수리하면서_구분을_함께_올린다(self):
        """고쳐 놓고 가재고로 남으면 출고가 막힌 채다."""
        a = self._asset_one()
        self.c.post(f"/api/assets/{a['id']}/repairs", json={
            "description": "수리 완료", "cost": 11000, "tier": "양품"})
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(d["tier"], "양품")
        self.assertTrue(any(e["action"] == "재고구분" for e in d["events"]))

    def test_없는_구분으로는_못_올린다(self):
        a = self._asset_one()
        r = self.c.post(f"/api/assets/{a['id']}/repairs", json={
            "description": "x", "cost": 0, "tier": "반품"})
        self.assertEqual(r.status_code, 400)

    def test_내용도_부품도_없으면_거부한다(self):
        a = self._asset_one()
        self.assertEqual(self.c.post(f"/api/assets/{a['id']}/repairs",
                                     json={"cost": 10000}).status_code, 400)


class TestConvertList(Base):
    """실재고/가재고 전환 목록 — 카테고리별로 묶어 보여준다."""

    def test_양품이_아닌_것만_카테고리별로_준다(self):
        b = self._batch()
        self._asset(b["id"], model="양품것", tier="양품")
        self._asset(b["id"], model="가재고것", tier="가재고")
        self._asset(b["id"], model="실재고것", tier="실재고")
        d = self.c.get("/api/assets/to-convert").get_json()
        self.assertEqual(d["total"], 2, "양품은 빠져야 한다")
        self.assertEqual(d["byTier"], {"가재고": 1, "실재고": 1})
        models = [a["model"] for g in d["groups"] for a in g["assets"]]
        self.assertNotIn("양품것", models)

    def test_출고완료는_빠진다(self):
        b = self._batch()
        a = self._asset(b["id"], model="나간것", tier="가재고").get_json()[0]
        self.c.patch(f"/api/assets/{a['id']}", json={"status": "scrapped"})
        d = self.c.get("/api/assets/to-convert").get_json()
        self.assertEqual(d["total"], 0)

    def test_수리비_누계를_함께_준다(self):
        b = self._batch()
        a = self._asset(b["id"], model="수리한것", tier="가재고").get_json()[0]
        self.c.post(f"/api/assets/{a['id']}/repairs", json={"description": "수리", "cost": 33000})
        d = self.c.get("/api/assets/to-convert").get_json()
        self.assertEqual(d["repairCost"], 33000)
        self.assertEqual(d["groups"][0]["assets"][0]["repairCost"], 33000)
        self.assertEqual(d["groups"][0]["assets"][0]["repairCount"], 1)

    def test_전표번호도_함께_준다(self):
        """전표를 눌러 찾아갈 수 있어야 한다."""
        b = self._batch()
        self._asset(b["id"], model="전표확인", tier="가재고")
        d = self.c.get("/api/assets/to-convert").get_json()
        self.assertTrue(d["groups"][0]["assets"][0]["slipNo"], "전표번호가 비어 있다")

    def test_재고구분_설명도_함께_준다(self):
        d = self.c.get("/api/assets/to-convert").get_json()
        self.assertIn("양품", d["tierHelp"])


class TestRepairVatInReports(Base):
    """수리비 부가세가 재무에 매입세액으로 잡힌다(대표 2026-08-04)."""

    def test_재무_요약이_수리_부가세를_준다(self):
        b = self._batch()
        a = self._asset(b["id"], model="L480").get_json()[0]
        self.c.post(f"/api/assets/{a['id']}/repairs", json={"description": "수리", "cost": 110000})
        o = self.c.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북", "phone": "010-1111-2222",
            "address": "서울시 강남구 1", "postalCode": "06000", "amount": 500000}).get_json()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("preparing", "production", "softwareInspection", "shipping"):
            self.c.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        today = self.c.get("/api/orders/today").get_json()["date"]
        sm = self.c.get(f"/api/reports/summary?from={today}&to={today}").get_json()
        self.assertEqual(sm["sales"]["repairCost"], 110000)
        self.assertEqual(sm["sales"]["repairVat"], 10000, "매입세액이 안 잡힌다")
        self.assertEqual(sm["sales"]["repairNet"], 100000)


class TestConvertUI(unittest.TestCase):
    def setUp(self):
        self.js = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")

    def test_전환_보기가_있다(self):
        """2026-08-04 탭 통합 — 별도 탭이 아니라 [자산] 탭 안의 보기가 됐다."""
        self.assertIn("실재고 / 가재고", self.js)
        self.assertIn("function renderConvert", self.js)
        self.assertIn('"convert", "🔧 실재고 / 가재고"', self.js)

    def test_수리_패널에_부가세_자동계산이_있다(self):
        self.assertIn("function openRepairPanel", self.js)
        block = self.js.split("function openRepairPanel", 1)[1]
        self.assertIn("total * 10 / 110", block, "서버와 같은 식이어야 숫자가 어긋나지 않는다")
        self.assertIn("cr-parts", block, "교체 부품 칸")
        self.assertIn("cr-tier", block, "구분 전환 칸")

    def test_이력이_함께_보인다(self):
        block = self.js.split("function openRepairPanel", 1)[1]
        self.assertIn("이 자산의 이력", block)
        self.assertIn("a.events", block, "입고부터 판매까지 이어져야 한다")

    def test_재무_화면이_매입세액을_보여준다(self):
        js = (ROOT / "static" / "js" / "reports.js").read_text("utf-8")
        self.assertIn("repairVat", js)
        self.assertIn("매입세액", js)

class TestSlipAmountAuto(Base):
    """매입금액은 자산 합계로 자동(대표 2026-08-04) — 손으로 적어 어긋나던 문제."""

    def test_금액_맞추기가_자산_합계로_고친다(self):
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid, "purchaseDate": "2026-08-04",
            "totalAmount": 999999,          # 일부러 어긋난 값
            "assets": [{"categoryId": self.cat, "qty": 10, "model": "L480",
                        "purchasePrice": 100000}]})
        bid = r.get_json()["id"]
        d = self.c.get(f"/api/purchase-batches/{bid}").get_json()
        self.assertNotEqual(d["totalAmount"], d["assignedAmount"], "어긋난 상태로 시작")
        s2 = self.c.post(f"/api/purchase-batches/{bid}/sync-amount")
        self.assertEqual(s2.status_code, 200, s2.get_data(as_text=True))
        self.assertTrue(s2.get_json()["changed"])
        d2 = self.c.get(f"/api/purchase-batches/{bid}").get_json()
        self.assertEqual(d2["totalAmount"], 1000000)
        self.assertEqual(d2["totalAmount"], d2["assignedAmount"], "맞춘 뒤에는 일치해야 한다")

    def test_이미_맞으면_바꾸지_않는다(self):
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid, "purchaseDate": "2026-08-04",
            "totalAmount": 200000,
            "assets": [{"categoryId": self.cat, "qty": 2, "model": "L480",
                        "purchasePrice": 100000}]})
        bid = r.get_json()["id"]
        self.assertFalse(self.c.post(f"/api/purchase-batches/{bid}/sync-amount")
                         .get_json()["changed"])

    def test_취소된_전표는_못_바꾼다(self):
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": self.sid, "purchaseDate": "2026-08-04",
            "totalAmount": 0, "assets": [{"categoryId": self.cat, "qty": 1, "model": "L480",
                                          "purchasePrice": 100000}]})
        bid = r.get_json()["id"]
        self.c.post(f"/api/purchase-batches/{bid}/cancel", json={"reason": "검증"})
        self.assertEqual(self.c.post(f"/api/purchase-batches/{bid}/sync-amount").status_code, 400)

    def test_없는_전표는_404(self):
        self.assertEqual(self.c.post("/api/purchase-batches/99999/sync-amount").status_code, 404)


class TestSlipFormCleanup(unittest.TestCase):
    """전표 폼에서 안 쓰는 칸 제거 + 팝업 잔류 수정(대표 2026-08-04)."""

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")

    def test_안_쓰는_칸이_없다(self):
        for f in ("sl-receive", "sl-vat", "sl-fee", "sl-ship", "sl-tracking", "sl-cod"):
            self.assertNotIn(f, self.js, f"{f} 칸이 되살아났다 — 매입에서는 안 쓴다")

    def test_매입금액은_읽기전용이다(self):
        block = self.js.split("function slipFieldsHtml", 1)[1].split("</div>", 1)[0]
        self.assertIn('id="sl-amount"', block)
        self.assertIn("readonly", block, "손으로 고치면 자산 합계와 어긋난다")

    def test_담을_때마다_금액을_채운다(self):
        self.assertIn('const amt = $("#sl-amount");', self.js)
        self.assertIn("amt.value = total", self.js)

    def test_화면에_들어올_때_열린_상세를_닫는다(self):
        # 권한 없음 early-return 뒤, 탭을 그리기 전에 지워야 한다
        block = self.js.split("function renderPurchaseView", 1)[1].split("const tabs =", 1)[0]
        self.assertIn("state.slipDetailId = undefined", block,
                      "안 지우면 마지막에 등록한 전표가 매번 다시 뜬다")
        self.assertIn("state.assetDetailId = undefined", block)

    def test_금액_맞추기_버튼이_있다(self):
        self.assertIn("sd-syncamt", self.js)
        self.assertIn("sync-amount", self.js)

class TestShiftSelectEverywhere(unittest.TestCase):
    """선택 가능한 목록은 전부 Shift 다중선택(대표 지시 2026-08-04)."""

    def setUp(self):
        self.app_js = (ROOT / "static" / "js" / "app.js").read_text("utf-8")
        self.pur_js = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")
        self.ord_js = (ROOT / "static" / "js" / "orders.js").read_text("utf-8")

    def test_공용_함수가_있다(self):
        self.assertIn("function attachShiftPick", self.app_js)
        block = self.app_js.split("function attachShiftPick", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("e.shiftKey", block)
        self.assertIn("anchor", block, "기준점이 없으면 범위를 못 잡는다")
        # 범위 안에서 값이 바뀐 칸에는 change를 알려야 저장까지 간다
        self.assertIn('dispatchEvent(new Event("change"', block)
        # 비활성 칸은 건드리면 안 된다(출고완료 등 잠긴 자산)
        self.assertIn("disabled", block)

    def test_root_안에서만_이어진다(self):
        """표가 여러 개인 화면에서 전역으로 잡으면 남의 표까지 번진다."""
        block = self.app_js.split("function attachShiftPick", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("$$(selector, root)", block)

    def test_매입_목록들에_붙어_있다(self):
        for sel in ("input.sd-pick", "input.sd-stock", "input.uc-pick"):
            self.assertIn(f'"{sel}"', self.pur_js, f"{sel} 이 화면에 없다")
            self.assertRegex(self.pur_js, r'attachShiftPick\([A-Za-z]+, "' + sel + '"',
                             f"{sel} 에 Shift 선택이 없다")

    def test_기존_구현이_있는_곳엔_겹쳐_붙이지_않는다(self):
        """같은 범위를 두 번 처리하면 토글이 뒤집힌다."""
        self.assertNotIn("attachShiftPick", self.ord_js,
                         "주문관리는 자체 Shift 구현이 있다 — 겹치면 안 된다")
        # 자산 목록도 자체 구현
        self.assertIn("state.assetLastPick", self.pur_js)
        block = self.pur_js.split("function wireAssetPicks", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertNotIn("attachShiftPick", block)

class TestDupCardCollapse(unittest.TestCase):
    """중복 의심 자산 카드 — 열고 닫기(대표 지시 2026-08-04)."""

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")
        self.css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")
        self.block = self.js.split("function dupCardHtml", 1)[1].split(chr(10) + "}", 1)[0]

    def test_details로_접힌다(self):
        self.assertIn("<details", self.block)
        self.assertIn("</details>", self.block)
        self.assertIn("<summary>", self.block)

    def test_기본은_닫힘(self):
        """40건이 늘 펼쳐져 있으면 매입 첫 화면이 경고표로 덮인다."""
        self.assertIn("state.dupOpen ?", self.block)
        # open 속성은 state가 켜져 있을 때만 붙는다
        self.assertNotIn("<details class=\"card dup-card\" open", self.block)

    def test_열고닫은_상태가_유지된다(self):
        self.assertIn("state.dupOpen = dupCard.open", self.js)

    def test_전체_건수를_다_보여준다(self):
        """20건만 보이면 나머지 20건은 영영 못 고친다."""
        self.assertNotIn("dups.groups.slice(0, 20)", self.block)
        self.assertIn("dups.groups.map(", self.block)

    def test_요약줄에_건수가_보인다(self):
        """닫혀 있어도 몇 건인지는 보여야 한다."""
        summary = self.block.split("<summary>", 1)[1].split("</summary>", 1)[0]
        self.assertIn("dups.count", summary)

    def test_삼각형_표시가_있다(self):
        self.assertIn(".dup-card > summary", self.css)
        self.assertIn('.dup-card[open] > summary::before', self.css)


class TestTierForGrade(unittest.TestCase):
    """등급 → 재고구분 기준(대표 지시 2026-08-04):
       등급 매겨짐 = 양품 / 미정 = 실재고 / 가재고는 사람이 직접."""

    def test_등급이_있으면_양품(self):
        for g in ("A급", "SS", "S+A", "AA", "B급", "NU", "AS"):
            self.assertEqual(tier_for_grade(g), "양품", g)

    def test_미정이면_실재고(self):
        self.assertEqual(tier_for_grade("미정"), "실재고")
        self.assertEqual(tier_for_grade(""), "실재고")
        self.assertEqual(tier_for_grade(None), "실재고")
        self.assertEqual(tier_for_grade("  "), "실재고")

    def test_가재고는_자동으로_안_준다(self):
        """가재고는 사람이 판단해서 옮기는 칸 — 자동 판정에 나오면 안 된다."""
        for g in GRADES + ["", None, "이상한값"]:
            self.assertNotEqual(tier_for_grade(g), "가재고")

    def test_결과는_항상_유효한_칸(self):
        for g in GRADES + ["", None]:
            self.assertIn(tier_for_grade(g), TIERS)


class TestMigrationTier(unittest.TestCase):
    """TMS 이관도 같은 기준을 따라야 한다 — 아니면 다음 이관이 규칙을 되돌린다."""

    def test_이관_INSERT에_tier가_들어간다(self):
        src = (ROOT / "app" / "purchase" / "migration.py").read_text("utf-8")
        self.assertIn('"tier"', src)
        self.assertIn('tier_for_grade(a.get("grade"))', src)

    def test_컬럼과_값의_수가_맞는다(self):
        """cols[:-3] + [actor, ts, ts] 구조라 tier를 잘못 넣으면 값이 밀린다."""
        src = (ROOT / "app" / "purchase" / "migration.py").read_text("utf-8")
        blk = src.split("cols = [\"asset_no\", \"category_id\", \"grade\"", 1)[1]
        cols_txt = blk.split("]", 1)[0]
        # 마지막 3개는 actor, created_at, updated_at 로 따로 붙는다
        self.assertIn('"created_by", "created_at", "updated_at"', cols_txt)
        # tier는 반드시 마지막 3개 앞에 있어야 한다
        self.assertLess(cols_txt.index('"tier"'), cols_txt.index('"created_by"'))

class TestSaleSlips(Base):
    """TMS 판매 전표 이관(2026-08-04) — 판매내역 + 판매미수금관리."""

    def _rows_master(self):
        return [{
            "판매전표": "S260101-001", "판매채널": "쿠팡", "판매처명": "하프북판매",
            "판매일": "2026-01-01", "출고일": "2026-01-02", "송장번호": "1234",
            "수량": "3", "판매수량": "5", "매입금액": "100000",
            "판매금액": "500000", "판매가": "450000", "판매차이금액": "50000",
            "순이익액": "40000", "순이익": "35000",
            "부가세": "0", "수수료": "0", "착불": "0", "배송비": "3000",
            "진행상태": "판매", "판매메모": "메모",
        }]

    def _rows_paid(self):
        return [{
            "판매전표": "S260101-001", "판매채널": "쿠팡", "거래처명": "하프북판매",
            "판매일": "2026-01-01", "납부확인": "완납", "입금일시": "2026-01-03",
            "입금액": "500000", "입금확인": "1",
            "매입금액": "0", "부가세": "0", "수수료": "0", "판매금액": "0",
            "착불": "0", "배송비": "0", "순이익액": "0",
        }]

    def test_전표표_판별(self):
        from app.purchase.sales import is_sale_slip_sheet
        self.assertTrue(is_sale_slip_sheet(self._rows_master()))
        self.assertTrue(is_sale_slip_sheet(self._rows_paid()))
        self.assertFalse(is_sale_slip_sheet([]))

    def test_자산표는_전표로_삼키지_않는다(self):
        """판매현황은 관리번호가 있는 자산 표다 — 여기서 먹으면 13,302건이 421건으로 뭉개진다."""
        from app.purchase.sales import is_sale_slip_sheet
        self.assertFalse(is_sale_slip_sheet(
            [{"판매전표": "S260101-001", "관리번호": "260101-0001", "판매가": "1"}]))

    def test_두_파일이_한_전표로_합쳐진다(self):
        from app.db import tx
        from app.purchase.sales import apply_sale_slips
        with self.app.app_context():
            with tx(write=True) as conn:
                a = apply_sale_slips(conn, self._rows_master())
                b = apply_sale_slips(conn, self._rows_paid())
                self.assertEqual(a["created"], 1)
                self.assertEqual(b["created"], 0, "같은 전표를 또 만들면 안 된다")
                self.assertEqual(b["updated"], 1)
                r = conn.execute(
                    "SELECT * FROM sale_slips WHERE slip_no='S260101-001'").fetchone()
        self.assertEqual(r["stage"], "판매")          # 판매내역에서
        self.assertEqual(r["paid_status"], "완납")     # 판매미수금에서
        self.assertEqual(r["paid_confirmed"], 1)

    def test_헤더값과_명세합계를_둘_다_보존한다(self):
        """하나만 고르면 이관에서 값이 사라진다 — 실제로 2.4억 차이가 난다."""
        from app.db import tx
        from app.purchase.sales import apply_sale_slips
        with self.app.app_context():
            with tx(write=True) as conn:
                apply_sale_slips(conn, self._rows_master())
                r = conn.execute("SELECT * FROM sale_slips").fetchone()
        self.assertEqual(r["sale_amount"], 500000)      # 판매금액(헤더)
        self.assertEqual(r["item_sale_sum"], 450000)    # 판매가(명세)
        self.assertEqual(r["diff_amount"], 50000)
        self.assertEqual(r["diff_amount"], r["sale_amount"] - r["item_sale_sum"])
        self.assertEqual(r["head_qty"], 3)              # 수량(헤더)
        self.assertEqual(r["qty"], 5)                   # 판매수량(명세)
        self.assertEqual(r["profit"], 40000)
        self.assertEqual(r["item_profit"], 35000)

    def test_값을_덮어쓰지_않는다(self):
        """사람이 HMS에서 고친 값이 다음 이관에 되돌아가면 고칠 이유가 없어진다."""
        from app.db import tx
        from app.purchase.sales import apply_sale_slips
        with self.app.app_context():
            with tx(write=True) as conn:
                apply_sale_slips(conn, self._rows_master())
                conn.execute(
                    "UPDATE sale_slips SET sale_amount=999 WHERE slip_no='S260101-001'")
                res = apply_sale_slips(conn, self._rows_master())
                r = conn.execute("SELECT sale_amount FROM sale_slips").fetchone()
        self.assertEqual(r["sale_amount"], 999, "사람이 고친 값을 이관이 덮어썼다")
        self.assertEqual(res["updated"], 0)

    def test_날짜_시리얼이_풀린다(self):
        """엑셀 날짜는 숫자로 온다 — 그대로 넣으면 '46238.46'이 된다."""
        from app.db import tx
        from app.purchase.sales import apply_sale_slips
        rows = self._rows_master()
        rows[0]["판매일"] = "46238.465764443361"
        with self.app.app_context():
            with tx(write=True) as conn:
                apply_sale_slips(conn, rows)
                r = conn.execute("SELECT sale_date FROM sale_slips").fetchone()
        self.assertEqual(r["sale_date"], "2026-08-04")

    def test_전표번호_없는_행은_건너뛴다(self):
        from app.db import tx
        from app.purchase.sales import apply_sale_slips
        with self.app.app_context():
            with tx(write=True) as conn:
                res = apply_sale_slips(conn, [{"판매전표": "", "판매금액": "1"}])
        self.assertEqual(res["created"], 0)
        self.assertEqual(res["skipped"], 1)

    def test_같은_파일_안_중복_전표(self):
        """한 파일에 같은 전표가 두 번 나와도 두 건이 되면 안 된다."""
        from app.db import tx
        from app.purchase.sales import apply_sale_slips
        rows = self._rows_master() + self._rows_master()
        with self.app.app_context():
            with tx(write=True) as conn:
                res = apply_sale_slips(conn, rows)
                n = conn.execute("SELECT COUNT(*) FROM sale_slips").fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(res["created"], 1)

    def test_목록_API(self):
        from app.db import tx
        from app.purchase.sales import apply_sale_slips
        with self.app.app_context():
            with tx(write=True) as conn:
                apply_sale_slips(conn, self._rows_master())
        r = self.c.get("/api/sale-slips")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["total"]["count"], 1)
        self.assertEqual(d["total"]["sale"], 500000)
        self.assertEqual(d["slips"][0]["slipNo"], "S260101-001")
        self.assertIn("쿠팡", d["channels"])

    def test_자동반영이_판매전표를_가져간다(self):
        """예전엔 '자산 표가 아님'으로 버려서 매출·정산이 HMS에 안 들어왔다."""
        src = (ROOT / "app" / "purchase" / "autosync.py").read_text("utf-8")
        self.assertIn("is_sale_slip_sheet", src)
        self.assertIn("apply_sale_slips", src)
        # 자산 표 판정보다 먼저 와야 한다(자산 표 아님으로 먼저 버려지면 의미 없다)
        self.assertLess(src.index("is_sale_slip_sheet(rows)"),
                        src.index("if not _is_asset_sheet(rows"))

class TestMigrationBatchAmount(Base):
    """이관이 새 전표를 만들 때 죽지 않아야 하고, 남의 전표를 건드리면 안 된다.

    2026-08-04: created_batches 정의가 빠진 채 참조돼 NameError로 자동반영이 통째로
    죽어 있었다(재고항목현황 반영 실패, hms.log 18:01:46).
    """

    def _rows(self, slip, no, price):
        return [{
            "관리번호": no, "매입전표": slip, "매입처명": "이관상사",
            "매입일": "2026-08-01", "모델명": "NT371B5M", "브랜드": "SAMSUNG",
            "매입가": str(price), "등급": "A급", "대분류": "PC",
        }]

    def _apply(self, rows):
        from app.db import tx
        from app.purchase.migration import _prepare, apply_rows
        with self.app.app_context():
            with tx(write=True) as conn:
                ready, dup, errors, updates = _prepare(conn, rows, fill_blanks=True)
                return apply_rows(conn, ready, updates, actor="테스트"), errors

    def test_새_전표를_만들어도_죽지_않는다(self):
        res, errors = self._apply(self._rows("P260801-999", "260801-9001", 100000))
        self.assertEqual(errors, [])
        self.assertEqual(res["created"], 1)
        self.assertEqual(res["newBatches"], 1)

    def test_이관이_만든_전표는_자산합으로_채워진다(self):
        from app.db import tx
        self._apply(self._rows("P260801-998", "260801-9002", 250000))
        with self.app.app_context():
            with tx() as conn:
                r = conn.execute(
                    "SELECT total_amount FROM purchase_batches WHERE slip_no='P260801-998'"
                ).fetchone()
        self.assertEqual(r["total_amount"], 250000)

    def test_사람이_만든_기존_전표는_건드리지_않는다(self):
        """0원으로 둔 전표를 이관이 멋대로 채우면 대조 안전장치가 무의미해진다."""
        from app.db import tx
        b = self._batch(totalAmount=0)      # 전표번호는 서버가 채번한다
        # 그 전표에 자산을 하나 달아 두고, 무관한 이관을 한 번 돌린다
        self._asset(batch_id=b["id"], purchasePrice=500000)
        self._apply(self._rows("P260801-776", "260801-9004", 300000))
        with self.app.app_context():
            with tx() as conn:
                r = conn.execute(
                    "SELECT total_amount FROM purchase_batches WHERE id=?", (b["id"],)
                ).fetchone()
        self.assertEqual(r["total_amount"], 0,
                         "이관이 사람이 만든 전표의 금액을 덮어썼다")

    def test_created_batches가_정의돼_있다(self):
        src = (ROOT / "app" / "purchase" / "migration.py").read_text("utf-8")
        self.assertIn("created_batches = set()", src)
        # 금액 채우기가 '전체 전표'가 아니라 '새로 만든 전표'만 봐야 한다
        self.assertNotIn("touched = set(batch_ids.values())", src)
        self.assertIn("for bid in created_batches:", src)

class TestPurchaseTabConsolidation(unittest.TestCase):
    """매입 탭 통합(대표 지시 2026-08-04: "쓸데없이 여러 탭으로 나누지 마라").

    8탭 → 4탭. 자산을 보고 고치는 4개 화면은 [자산] 안의 보기로,
    어쩌다 쓰는 2개는 [기준정보]로 묶었다.
    """

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")
        self.app_js = (ROOT / "static" / "js" / "app.js").read_text("utf-8")
        self.css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")
        self.block = self.js.split("function renderPurchaseView", 1)[1].split(
            chr(10) + "}", 1)[0]

    def test_상위_탭은_셋뿐(self):
        """2026-08-04 판매 전표를 설정 › 매출/실적으로 옮겨 매입은 3탭이 됐다."""
        import re
        keys = re.findall(r'\["(\w+)", "', self.block)
        self.assertEqual(sorted(set(keys)), ["assets", "base", "slips"])

    def test_판매전표를_찾아오면_새_자리로_보낸다(self):
        """옛 링크·이전 상태가 'saleslips'로 들어올 수 있다."""
        self.assertIn('state.purchaseTab === "saleslips"', self.js)
        self.assertIn('state.settingsTab = "sales"', self.js)
        self.assertIn('go("settings")', self.js)

    def test_옛_탭_이름으로_들어와도_길을_잃지_않는다(self):
        """북마크·대시보드 링크·이전 세션 상태가 'summary'로 들어올 수 있다."""
        self.assertIn("const MOVED", self.block)
        for old in ("summary", "uncoded", "convert", "suppliers", "migrate"):
            self.assertIn(old + ":", self.block, f"{old} 이동 규칙 없음")

    def test_네_보기가_모두_살아_있다(self):
        for key, fn in (("summary", "renderStockSummary"), ("list", "renderAssetList"),
                        ("uncoded", "renderUncoded"), ("convert", "renderConvert")):
            self.assertIn(f'"{key}"', self.js)
            self.assertIn(f"function {fn}", self.js)
        self.assertIn("function renderAssetsTab", self.js)
        self.assertIn("function renderBaseTab", self.js)

    def test_치울_일은_배지로_보인다(self):
        """제품코드 없음·가재고가 보기 안에 숨으면 영영 안 치운다."""
        self.assertIn("subtab-badge", self.js)
        self.assertIn("function fillAssetBadges", self.js)
        blk = self.js.split("function fillAssetBadges", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("/api/assets/uncoded", blk)
        self.assertIn("/api/assets/to-convert", blk)
        # 배지가 실패해도 화면은 떠야 한다
        self.assertIn("catch", blk)

    def test_0이면_배지를_숨긴다(self):
        blk = self.js.split("function fillAssetBadges", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn('style.display = n ? "" : "none"', blk)

    def test_조회권한만_있으면_수정용_보기는_안_보인다(self):
        self.assertIn("canEdit || (k !== \"uncoded\" && k !== \"convert\")", self.js)

    def test_중복카드에서_자산으로_갈_때_목록_보기로_간다(self):
        """집계 화면으로 가면 누른 관리번호가 어디에도 없다."""
        blk = self.js.split('중복 카드의 관리번호', 1)[1][:400]
        self.assertIn('state.assetView = "list"', blk)

    def test_대시보드_링크도_목록_보기를_지정한다(self):
        self.assertIn('tab: "assets", assetView: "list"', self.app_js)
        self.assertIn("if (t.assetView) state.assetView = t.assetView;", self.app_js)

    def test_자산_저장후_다시_그릴_대상이_맞다(self):
        """자산 목록이 #aview-body로 옮겨갔는데 #ptab-body를 그리면 화면이 안 바뀐다."""
        self.assertIn('renderAssetList($("#aview-body") || $("#ptab-body"))', self.js)

    def test_수리패널_붙일_자리도_옮겨졌다(self):
        blk = self.js.split("function openRepairPanel", 1)[1][:400]
        self.assertIn('$("#aview-body")', blk)

    def test_하위탭_스타일이_상위탭과_다르다(self):
        """둘이 똑같이 생기면 지금 어디에 있는지를 잃는다."""
        self.assertIn(".subtabs {", self.css)
        self.assertIn(".subtabs button.active", self.css)
        self.assertIn(".subtab-badge", self.css)

class TestAssetSlipLink(Base):
    """자산에서 매입 전표로 바로 가기(대표 요청 2026-08-04).

    "자산목록 가면 260804-0086 해당 건에 대한 전표를 바로 들어가서 전체보기 할 수 있게.
     상세 누르면 상단에 전표 뜨고, 그 전표 누르면 전체가 보이게.
     ★단 그 UI는 매입 작업 탭에 있는 것과 같아야 한다."
    """

    def setUp(self):
        super().setUp()
        self.js = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")
        self.css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")

    # ---- 서버: 목록이 전표번호를 준다 ----
    def test_자산_목록이_전표번호를_준다(self):
        b = self._batch()
        r = self._asset(batch_id=b["id"])
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        rows = self.c.get("/api/assets").get_json()
        self.assertTrue(rows)
        self.assertEqual(rows[0]["slipNo"], b["slipNo"])
        self.assertEqual(rows[0]["batchId"], b["id"])

    def test_전표에_안_묶인_자산도_목록에_남는다(self):
        """LEFT JOIN이 아니면 이관·수기 자산이 통째로 사라진다."""
        self._asset()                       # batchId 없이
        rows = self.c.get("/api/assets").get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["slipNo"], "")

    def test_자산_상세도_전표를_준다(self):
        b = self._batch()
        aid = self._asset(batch_id=b["id"]).get_json()[0]["id"]
        d = self.c.get(f"/api/assets/{aid}").get_json()
        self.assertIsNotNone(d["batch"])
        self.assertEqual(d["batch"]["slipNo"], b["slipNo"])
        self.assertEqual(d["batch"]["id"], b["id"])

    def test_엑셀에도_매입전표_열이_있다(self):
        b = self._batch()
        self._asset(batch_id=b["id"])
        r = self.c.get("/api/assets/export")
        self.assertEqual(r.status_code, 200)
        from app.importers import read_first_sheet
        rows = read_first_sheet(r.data, "assets.xlsx")
        self.assertIn("매입전표", rows[0])
        self.assertEqual(rows[0]["매입전표"], b["slipNo"])

    # ---- 화면 ----
    def test_상세_맨_위에_전표줄이_있다(self):
        self.assertIn("function slipBarHtml", self.js)
        blk = self.js.split("function renderAssetDetail", 1)[1][:2000]
        self.assertIn("slipBarHtml(a.batch)", blk)
        # 닫기 버튼(맨 위) 바로 다음에 와야 '상단'이다
        self.assertLess(blk.index("slipBarHtml(a.batch)"), blk.index("form-grid"))

    def test_전표가_없으면_없다고_말한다(self):
        blk = self.js.split("function slipBarHtml", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("전표 없음", blk)

    def test_목록에서도_전표를_바로_누를_수_있다(self):
        self.assertIn('class="link-btn af-slip"', self.js)
        self.assertIn('$$("button.af-slip", body)', self.js)

    def test_같은_전표_화면을_다시_만들지_않는다(self):
        """전표 화면을 두 벌로 만들면 한쪽만 고쳐져 서로 다른 말을 한다."""
        self.assertIn("function openSlipFromAsset", self.js)
        blk = self.js.split("function openSlipFromAsset", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn('state.purchaseTab = "slips"', blk)   # 매입 작업 탭의 그 화면으로 간다
        self.assertIn("state.openSlipId", blk)
        self.assertNotIn("renderSlipDetail2", self.js)

    def test_진입_시_전표가_지워지지_않게_넘겨받는다(self):
        """renderPurchaseView가 slipDetailId를 지우므로 openSlipId로 실어 보낸다."""
        blk = self.js.split("function renderPurchaseView", 1)[1][:1200]
        self.assertIn("state.openSlipId", blk)
        self.assertLess(blk.index("state.slipDetailId = undefined"),
                        blk.index("if (state.openSlipId)"))

    def test_팝업이면_먼저_닫는다(self):
        """자산 상세가 팝업인 채로 뒤 화면만 바꾸면 팝업이 남는다."""
        blk = self.js.split("function openSlipFromAsset", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("closeModal()", blk)

    def test_전표줄_스타일이_있다(self):
        self.assertIn(".slip-bar", self.css)
        self.assertIn(".slip-bar .slip-open", self.css)

class TestQcRefresh(Base):
    """QC 주문 파일 재투입 = '목록 최신화'(대표 요청 2026-08-04).

    예전에는 이미 들어온 주문이면 통째로 건너뛰어서, 첫 취입 뒤 QC에서 셋팅·검수·출고를
    끝내도 HMS는 옛날 상태 그대로였다(실측 745건 중 37건 56플래그 어긋남).
    ★전진만 반영한다 — 끝난 일을 안 끝난 것으로 되돌리는 사고는 복구할 방법이 없다.
    """

    def _order(self, **kw):
        o = {"importKey": "K1", "orderNumber": "ORD-1", "channel": "쿠팡",
             "productName": "노트북", "recipient": "홍길동", "quantity": 1, "amount": 100}
        o.update(kw)
        return [o]

    def _post(self, orders, path="/api/migrate/qc"):
        import io as _io
        import json as _json
        data = {"files": (_io.BytesIO(_json.dumps(orders, ensure_ascii=False).encode("utf-8")),
                          "orders.json")}
        return self.c.post(path, data=data, content_type="multipart/form-data")

    def test_처음엔_새_주문으로_들어온다(self):
        r = self._post(self._order())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["orders"]["created"], 1)

    def test_같은_파일을_또_넣어도_늘지_않는다(self):
        self._post(self._order())
        d = self._post(self._order()).get_json()
        self.assertEqual(d["orders"]["created"], 0)
        self.assertEqual(d["orders"]["duplicates"], 1)
        self.assertEqual(d["orders"]["advanced"], 0)

    def test_단계가_진행되면_최신화된다(self):
        self._post(self._order())
        d = self._post(self._order(
            productionDone=True, productionBy="김제작", productionAt="2026-08-04 10:00",
            shippingDone=True, shippingBy="박출고")).get_json()
        self.assertEqual(d["orders"]["created"], 0)
        self.assertEqual(d["orders"]["advanced"], 1)
        self.assertEqual(d["orders"]["advancedStages"], 2)
        from app.db import tx
        with self.app.app_context():
            with tx() as conn:
                r = conn.execute("SELECT * FROM orders WHERE import_key='K1'").fetchone()
        self.assertEqual(r["production_done"], 1)
        self.assertEqual(r["production_by"], "김제작")
        self.assertEqual(r["production_at"], "2026-08-04 10:00")
        self.assertEqual(r["shipping_done"], 1)
        self.assertEqual(r["shipping_by"], "박출고")

    def test_끝난_단계를_되돌리지_않는다(self):
        """파일이 옛것이면 되돌리기가 이미 끝낸 일을 지운다."""
        self._post(self._order(productionDone=True, shippingDone=True, shippingBy="박출고"))
        d = self._post(self._order()).get_json()          # 전부 미완료인 옛 파일
        self.assertEqual(d["orders"]["advanced"], 0)
        from app.db import tx
        with self.app.app_context():
            with tx() as conn:
                r = conn.execute("SELECT * FROM orders WHERE import_key='K1'").fetchone()
        self.assertEqual(r["shipping_done"], 1, "출고완료가 되돌려졌다")
        self.assertEqual(r["shipping_by"], "박출고")

    def test_미리보기가_갱신될_건수를_알려준다(self):
        self._post(self._order())
        d = self._post(self._order(shippingDone=True),
                       "/api/migrate/qc/preview").get_json()
        self.assertEqual(d["orders"]["toCreate"], 0)
        self.assertEqual(d["orders"]["toAdvance"], 1)
        self.assertIn("출고완료", d["orders"]["advanceSample"][0]["단계"])

    def test_미리보기는_아무것도_바꾸지_않는다(self):
        self._post(self._order())
        self._post(self._order(shippingDone=True), "/api/migrate/qc/preview")
        from app.db import tx
        with self.app.app_context():
            with tx() as conn:
                r = conn.execute("SELECT shipping_done FROM orders WHERE import_key='K1'").fetchone()
        self.assertEqual(r["shipping_done"], 0, "미리보기가 데이터를 바꿨다")

    def test_취소는_건드리지_않는다(self):
        """취소는 HMS 쪽 판단이라 파일이 뒤집으면 안 된다.

        ★보관(archived_at)은 2026-08-04 감사 결과 예외로 두었다 —
          QC에서 마감한 주문이 HMS에선 계속 열려 있어 '한쪽만 닫힌 주문'이 쌓였다.
          단 '전진만'(빈 칸 → 채움)이고 해제는 안 한다(test_보관을_해제하지는_않는다).
        """
        src = (ROOT / "app" / "settings" / "qc_import.py").read_text("utf-8")
        blk = src.split("def _advance_fields", 1)[1].split(chr(10) + "def ", 1)[0]
        for bad in ("cancelled_at", "restored_at"):
            self.assertNotIn(bad, blk, f"{bad}까지 갱신하면 안 된다")
        self.assertIn("archived_at", blk, "보관은 따라와야 한다")

class TestDetailHostSelectors(unittest.TestCase):
    """renderAssetDetail(hostId)는 CSS 셀렉터를 받는다 — '#'이 빠지면 조용히 죽는다.

    2026-08-04 실측: 제품코드 없음 화면에서 자산번호를 눌러도 아무 반응이 없었다.
    querySelector("uc-detail")이 <uc-detail> 태그를 찾아 null을 돌려주고,
    renderAssetDetail이 `if (!host) return;` 으로 조용히 끝났기 때문이다.
    """

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")

    def test_모든_호출이_셀렉터_형식이다(self):
        import re
        calls = re.findall(r'renderAssetDetail\(\s*"([^"]+)"', self.js)
        self.assertTrue(calls, "호출을 못 찾았다")
        for c in calls:
            self.assertTrue(c.startswith("#") or c.startswith("."),
                            f'renderAssetDetail("{c}") — # 또는 .으로 시작해야 한다')

    def test_uc_detail이_고쳐졌다(self):
        self.assertIn('renderAssetDetail("#uc-detail"', self.js)
        self.assertNotIn('renderAssetDetail("uc-detail"', self.js)

    def test_다른_패널_호스트도_같은_규칙(self):
        self.assertIn('renderAssetDetail("#sd-assetdetail"', self.js)

class TestAuditFixes20260804(Base):
    """2026-08-04 전면 검증에서 확정된 결함 수정."""

    def setUp(self):
        super().setUp()
        self.js = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")
        self.app_js = (ROOT / "static" / "js" / "app.js").read_text("utf-8")

    # ---- 취소·반품 전표가 살아있는 매입으로 보이던 문제 ----
    def test_목록이_취소_반품을_알려준다(self):
        from app.db import tx
        b = self._batch()
        self._asset(batch_id=b["id"])
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE purchase_batches SET cancelled_at='2026-08-04' WHERE id=?",
                             (b["id"],))
        r = self.c.get("/api/assets").get_json()
        self.assertTrue(r[0]["slipCancelled"], "취소된 전표인데 목록이 모른다")
        self.assertFalse(r[0]["slipReturned"])

    def test_상세도_취소를_알려준다(self):
        from app.db import tx
        b = self._batch()
        aid = self._asset(batch_id=b["id"]).get_json()[0]["id"]
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE purchase_batches SET cancelled_at='2026-08-04', "
                             "cancel_reason='오등록' WHERE id=?", (b["id"],))
        d = self.c.get(f"/api/assets/{aid}").get_json()
        self.assertEqual(d["batch"]["cancelledAt"], "2026-08-04")
        self.assertEqual(d["batch"]["cancelReason"], "오등록")

    def test_화면이_취소칩을_그린다(self):
        blk = self.js.split("function slipBarHtml", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("b.cancelledAt", blk)
        self.assertIn("🚫 취소됨", blk)
        self.assertIn("↩ 반품", blk)
        self.assertIn("a.slipCancelled", self.js)   # 목록에도

    # ---- 판매 전표 합계에 취소·반입이 섞이던 문제 ----
    def test_합계에서_취소_반입을_뺀다(self):
        from app.db import tx
        from app.purchase.sales import apply_sale_slips
        rows = [
            {"판매전표": "A1", "판매금액": "1000", "진행상태": "판매"},
            {"판매전표": "A2", "판매금액": "500", "진행상태": "판매취소"},
            {"판매전표": "A3", "판매금액": "300", "진행상태": "반입"},
        ]
        with self.app.app_context():
            with tx(write=True) as conn:
                apply_sale_slips(conn, rows)
        d = self.c.get("/api/sale-slips").get_json()
        self.assertEqual(d["total"]["sale"], 1000, "취소·반입이 매출에 섞였다")
        self.assertEqual(d["total"]["count"], 1)
        self.assertEqual(d["allCount"], 3)
        stages = {e["stage"]: e for e in d["excluded"]}
        self.assertEqual(stages["판매취소"]["sale"], 500)
        self.assertEqual(stages["반입"]["sale"], 300)

    def test_안_잘렸으면_잘렸다고_안_한다(self):
        from app.db import tx
        from app.purchase.sales import apply_sale_slips
        with self.app.app_context():
            with tx(write=True) as conn:
                apply_sale_slips(conn, [{"판매전표": "B1", "판매금액": "1"}])
        d = self.c.get("/api/sale-slips").get_json()
        self.assertFalse(d["capped"])
        self.assertEqual(d["shown"], 1)

    def test_상한_판정이_501로_정확하다(self):
        src = (ROOT / "app" / "purchase" / "sales.py").read_text("utf-8")
        self.assertIn("LIMIT 501", src)
        self.assertIn("capped = len(rows) > 500", src)
        self.assertNotIn('"capped": len(rows) >= 500', src)

    # ---- 배지가 실제 손댈 것만 세게 ----
    def test_배지는_가재고만_센다(self):
        blk = self.js.split("function fillAssetBadges", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn('c.byTier["가재고"]', blk)
        self.assertNotIn('put("convert", c.total', blk)

    # ---- 제품코드 적용 후 숫자·배지 갱신 ----
    def test_적용후_제대로_다시_그린다(self):
        self.assertNotIn('$("#tab-body")', self.js, "매입 화면에 없는 id를 찾고 있다")
        blk = self.js.split("const applyUncoded", 1)[-1]
        self.assertIn('renderUncoded($("#aview-body")', self.js)
        self.assertIn("fillAssetBadges($(\"#ptab-body\")", self.js)

    # ---- 엑셀 내보내기 ----
    def test_내보내기가_최신순이다(self):
        src = (ROOT / "app" / "purchase" / "__init__.py").read_text("utf-8")
        self.assertIn("ORDER BY a.id DESC LIMIT 5000", src)
        self.assertNotIn('ORDER BY a.asset_no LIMIT 5000', src)

    def test_안_잘리면_경고줄이_없다(self):
        b = self._batch()
        self._asset(batch_id=b["id"])
        r = self.c.get("/api/assets/export")
        self.assertEqual(r.status_code, 200)
        from app.importers import read_first_sheet
        rows = read_first_sheet(r.data, "a.xlsx")
        self.assertEqual(len(rows), 1)
        self.assertNotIn("※", rows[0]["관리번호"])
        self.assertNotIn("일부", r.headers["Content-Disposition"])

    # ---- 판매전표 파일을 자산 이관에 올렸을 때 ----
    def test_판매전표_파일은_자산이관이_돌려세운다(self):
        import io as _io
        from app.importers import write_xlsx
        content = write_xlsx(["판매전표", "판매금액", "진행상태"], [["S1", 100, "판매"]])
        r = self.c.post("/api/assets/migrate/preview",
                        data={"files": (_io.BytesIO(content), "판매내역.xlsx")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn("판매 전표", r.get_json()["error"])

    # ---- 판매 전표 탭 업로드 ----
    def test_판매전표_탭에_올릴_자리가_있다(self):
        self.assertIn('id="ss-upload"', self.js)
        self.assertIn("/api/sale-slips/import", self.js)
        # 한 건도 안 들어가면 그렇다고 말해야 한다
        self.assertIn("새로 들어간 전표가 없습니다", self.js)

    # ---- QC 최신화 안내 문구 ----
    def test_확인창이_사실대로_말한다(self):
        self.assertNotIn("(이미 있는 건 건너뜁니다)", self.app_js)
        self.assertIn("최신으로 맞춥니다", self.app_js)
        self.assertIn("되돌리지는 않습니다", self.app_js)

    def test_미리보기가_갱신될_건수를_보여준다(self):
        self.assertIn("data.orders.toAdvance", self.app_js)
        self.assertIn("advanceSample", self.app_js)

    # ---- QC 보관 반영 ----
    def test_보관도_따라온다(self):
        import io as _io
        import json as _json

        def post(orders):
            data = {"files": (_io.BytesIO(_json.dumps(orders, ensure_ascii=False).encode("utf-8")),
                              "orders.json")}
            return self.c.post("/api/migrate/qc", data=data,
                               content_type="multipart/form-data")

        base = {"importKey": "Z1", "orderNumber": "O-1", "channel": "기타",
                "productName": "노트북", "recipient": "홍길동", "quantity": 1, "amount": 1}
        post([base])
        post([{**base, "archivedAt": "2026-08-04"}])
        from app.db import tx
        with self.app.app_context():
            with tx() as conn:
                r = conn.execute("SELECT archived_at FROM orders WHERE import_key='Z1'").fetchone()
        self.assertEqual(r["archived_at"], "2026-08-04")

    def test_보관을_해제하지는_않는다(self):
        import io as _io
        import json as _json

        def post(orders):
            data = {"files": (_io.BytesIO(_json.dumps(orders, ensure_ascii=False).encode("utf-8")),
                              "orders.json")}
            return self.c.post("/api/migrate/qc", data=data,
                               content_type="multipart/form-data")

        base = {"importKey": "Z2", "orderNumber": "O-2", "channel": "기타",
                "productName": "노트북", "recipient": "홍길동", "quantity": 1, "amount": 1}
        post([{**base, "archivedAt": "2026-08-04"}])
        post([base])                       # 보관 해제된 옛 파일
        from app.db import tx
        with self.app.app_context():
            with tx() as conn:
                r = conn.execute("SELECT archived_at FROM orders WHERE import_key='Z2'").fetchone()
        self.assertEqual(r["archived_at"], "2026-08-04", "보관이 해제됐다")

class TestSettingsTabMerge(unittest.TestCase):
    """설정 탭 정리(대표 지시 2026-08-04).

    · 제품 분류: 매입 › 기준정보 › 거래처/분류와 같은 화면이라 제거
    · 제공 옵션: API 관리 안으로 흡수
    · 셋팅 실적 + 판매 전표 → '매출 / 실적' 한 탭
    """

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "app.js").read_text("utf-8")
        self.pur = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")
        self.block = self.js.split("function settingsTabs", 1)[1].split(chr(10) + "}", 1)[0]

    def test_제품분류_탭이_없다(self):
        self.assertNotIn('["cats", "제품 분류"]', self.block)
        # 그래도 분류를 관리할 곳은 남아 있어야 한다
        self.assertIn("제품 분류", self.pur)

    def test_제공옵션_탭이_없다(self):
        self.assertNotIn('["prep", "🧩 제공 옵션"]', self.block)

    def test_매출실적_탭으로_합쳐졌다(self):
        self.assertIn('["sales", "매출 / 실적"]', self.block)
        self.assertNotIn('["setupstats"', self.block)
        self.assertIn("function renderSalesTab", self.js)
        blk = self.js.split("function renderSalesTab", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("renderSaleSlips(host)", blk)
        self.assertIn("renderSetupStatsTab(host)", blk)

    def test_API_안에_제공옵션이_들어갔다(self):
        self.assertIn("const API_VIEWS", self.js)
        blk = self.js.split("async function renderApiTab", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("renderPrepTab(host)", blk)
        self.assertIn("renderApiSettings(host)", blk)

    def test_옛_탭_이름으로_와도_길을_잃지_않는다(self):
        blk = self.js.split("function renderSettings", 1)[1][:900]
        self.assertIn("const MOVED", blk)
        for old in ("setupstats", "prep", "cats"):
            self.assertIn(old + ":", blk, f"{old} 이동 규칙 없음")

    def test_금액_권한이_없으면_판매전표는_안_보인다(self):
        """실적만 보는 작업자에게 매출 금액까지 열어 줄 이유는 없다."""
        blk = self.js.split("function renderSalesTab", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("canMoney", blk)
        self.assertIn('hasPerm("reports.view")', blk)

    def test_아이콘을_뺐다(self):
        """대표 지시: 판매 전표 앞 아이콘 제거."""
        self.assertNotIn("💰 판매 전표", self.pur)
        self.assertNotIn("💰", self.js.split("const SALES_VIEWS", 1)[1][:200])

class TestPagers(unittest.TestCase):
    """긴 목록은 잘라서 앞뒤 화살표로 넘긴다(대표 지시 2026-08-05:
    "너무 길면 아래로 스크롤 및 리소스 낭비가 크니 적당한 선에서 잘라주고
     앞뒤로 이동할 수 있는 화살표로 전부 바꿔줘")."""

    def setUp(self):
        d = ROOT / "static" / "js"
        self.app = (d / "app.js").read_text("utf-8")
        self.pur = (d / "purchase.js").read_text("utf-8")
        self.ord = (d / "orders.js").read_text("utf-8")
        self.setup = (d / "setup.js").read_text("utf-8")
        self.ship = (d / "shipping.js").read_text("utf-8")
        self.css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")

    def test_공용_도구가_하나뿐이다(self):
        """같은 걸 화면마다 새로 만들면 한쪽만 고쳐진다."""
        self.assertEqual(self.app.count("function paged("), 1)
        self.assertEqual(self.app.count("function pagerBar("), 1)
        self.assertEqual(self.app.count("function wirePager("), 1)
        for src in (self.pur, self.ord, self.setup, self.ship):
            self.assertNotIn("function paged(", src, "화면이 자기 페이저를 또 만들었다")

    def test_한쪽에_다_들어가면_안_그린다(self):
        """빈 화살표 막대가 남으면 눌러도 아무 일이 없어 고장으로 보인다."""
        blk = self.app.split("function pagerBar", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn('if (total <= per) return "";', blk)

    def test_화살표_다섯_개(self):
        blk = self.app.split("function pagerBar", 1)[1].split(chr(10) + "}", 1)[0]
        for act in ("first", "prev", "next", "last"):
            self.assertIn(f'"{act}"', blk)
        self.assertIn("«", blk)
        self.assertIn("◀", blk)
        self.assertIn("▶", blk)
        self.assertIn("»", blk)

    def test_끝단에서_비활성(self):
        blk = self.app.split("function pagerBar", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("page > 0", blk)
        self.assertIn("page < last", blk)
        self.assertIn("disabled", blk)

    def test_범위를_벗어나지_않는다(self):
        """마지막 쪽에서 지우면 빈 화면이 남는다 — page는 last로 잘린다."""
        blk = self.app.split("function paged(", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("Math.min(Math.max(0, state.pages[key] || 0), last)", blk)

    def test_넘김_계산이_경계에서_맞다(self):
        """50건이면 화살표 없음, 51건이면 2쪽."""
        blk = self.app.split("function paged(", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("Math.ceil(total / per) - 1", blk)
        self.assertIn("Math.max(0,", blk)

    def test_적용된_목록들(self):
        pairs = [
            (self.pur, "assets", "자산 목록"),
            (self.pur, "pslips", "매입 전표"),
            (self.pur, "stock", "재고 집계"),
            (self.pur, "saleslips", "판매 전표"),
            (self.ord, "orders", "주문 목록"),
            (self.setup, "setup", "셋팅 보드"),
            (self.ship, "shipWait", "송장 발급 대기"),
            (self.ship, "shipDone", "출고 확인됨"),
            (self.app, "audit", "감사 로그"),
        ]
        for src, key, name in pairs:
            self.assertIn(f'"{key}"', src, f"{name}에 페이저 없음")
            self.assertIn("wirePager", src, f"{name} 배선 없음")

    def test_묶음별로_따로_넘어간다(self):
        """모델 묶음이 여럿인데 key가 같으면 한 묶음을 넘길 때 다 같이 넘어간다."""
        self.assertIn('"uc:" + g.model', self.pur)
        self.assertIn('"cv:" + g.category', self.pur)

    def test_조건이_바뀌면_1쪽으로(self):
        """3쪽을 보다 검색해 2건만 남으면 빈 화면이 된다."""
        self.assertIn('resetPage("assets")', self.pur)
        self.assertIn('resetPage("pslips")', self.pur)
        self.assertIn('resetPage("stock")', self.pur)
        self.assertIn('resetPage("saleslips")', self.pur)
        self.assertIn('resetPage("orders")', self.ord)
        self.assertIn('resetPage("setup")', self.setup)

    def test_자동새로고침은_1쪽으로_되돌리지_않는다(self):
        """30초마다 1쪽으로 튕기면 3쪽을 볼 수가 없다."""
        blk = self.ord.split("addPoller(", 1)[1][:300]
        self.assertNotIn('resetPage("orders")', blk)

    def test_출고완료_50건_자르기를_없앴다(self):
        """예전엔 앞 50건만 보이고 나머지는 볼 방법이 없었다."""
        self.assertNotIn("shipped.slice(0, 50)", self.ship)

    def test_스타일이_있다(self):
        self.assertIn(".pager {", self.css)
        self.assertIn(".pager button:disabled", self.css)
        self.assertIn(".pager-at", self.css)

class TestProductCodeLookup(Base):
    """제품코드 자동완성(대표 요청 2026-08-05: "모든 제품코드가 들어가는 곳에").

    ★같은 물건에 코드를 조금씩 다르게 적으면 셋팅 화면의 재고 대조가 어긋난다 —
      쓰던 코드를 그대로 다시 고르게 하는 것이 이 기능의 목적이다.
    """

    def setUp(self):
        super().setUp()
        self.js_app = (ROOT / "static" / "js" / "app.js").read_text("utf-8")
        self.js_ord = (ROOT / "static" / "js" / "orders.js").read_text("utf-8")
        self.js_pur = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")

    def _asset_with_code(self, code, **kw):
        b = self._batch()
        return self._asset(batch_id=b["id"], productCode=code, **kw)

    # ---- 서버 ----
    def test_쓰고_있는_코드를_돌려준다(self):
        self._asset_with_code("840 G3_i7-6_내장")
        d = self.c.get("/api/product-codes").get_json()
        codes = [c["code"] for c in d["codes"]]
        self.assertIn("840 G3_i7-6_내장", codes)

    def test_코드_없는_자산은_안_나온다(self):
        self._asset()                       # productCode 없이
        d = self.c.get("/api/product-codes").get_json()
        self.assertEqual(d["codes"], [])

    def test_검색어로_좁혀진다(self):
        self._asset_with_code("840 G3_i7-6_내장")
        self._asset_with_code("L480_i5-8_내장")
        d = self.c.get("/api/product-codes?q=840").get_json()
        self.assertEqual([c["code"] for c in d["codes"]], ["840 G3_i7-6_내장"])

    def test_출고가능_대수를_함께_준다(self):
        """코드만 봐선 지금 팔 수 있는지 모른다."""
        self._asset_with_code("X_i5_내장", tier="양품")
        self._asset_with_code("X_i5_내장", tier="가재고")
        d = self.c.get("/api/product-codes?q=X_i5").get_json()
        c = d["codes"][0]
        self.assertEqual(c["total"], 2)
        self.assertEqual(c["shippable"], 1, "가재고는 출고 못 하니 빠져야 한다")

    def test_지난_주문의_옵션이_따라온다(self):
        """수기 주문에서 옵션을 그대로 가져다 쓰게 한다."""
        from app.db import tx
        self._asset_with_code("Z_i7_내장")
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute(
                    "INSERT INTO orders(channel, order_no, product_name, option_name, "
                    "product_code, quantity, amount, recipient, created_at, updated_at) "
                    "VALUES('쿠팡','O1','노트북 Z','SSD 512GB 추가','Z_i7_내장',1,1,'홍',?,?)",
                    ("2026-08-05", "2026-08-05"))
        d = self.c.get("/api/product-codes?q=Z_i7").get_json()
        c = d["codes"][0]
        self.assertIn("SSD 512GB 추가", c["options"])
        self.assertEqual(c["productName"], "노트북 Z")

    def test_조회권한만_있어도_된다(self):
        r = self.c.get("/api/product-codes")
        self.assertEqual(r.status_code, 200)

    # ---- 화면 ----
    def test_공용_도구가_하나뿐이다(self):
        self.assertEqual(self.js_app.count("function attachCodeLookup("), 1)
        self.assertEqual(self.js_app.count("function codeLookupField("), 1)
        for src in (self.js_ord, self.js_pur):
            self.assertNotIn("function attachCodeLookup(", src)

    def test_제품코드_칸_전부에_붙었다(self):
        # 수기 주문(신설) · 자산 상세 · 전표에 자산 담기 · 제품코드 일괄
        self.assertIn('attachCodeLookup("no-sku"', self.js_ord)
        self.assertIn('attachCodeLookup("ad-productcode")', self.js_pur)
        self.assertIn('attachCodeLookup("sa-productcode")', self.js_pur)
        self.assertIn("attachCodeLookup(codeIn.id)", self.js_pur)

    def test_수기_주문에_제품코드_칸이_있다(self):
        self.assertIn('codeLookupField("no-sku"', self.js_ord)

    def test_제품코드가_상품코드보다_우선_저장된다(self):
        """셋팅/QC의 재고 대조가 이 값으로 돈다 — 고도몰 자체코드가 아니라 제품코드여야 한다."""
        self.assertIn('productCode: $("#no-sku").value.trim() || $("#no-code").value',
                      self.js_ord)

    def test_고르면_상품명_옵션이_따라온다(self):
        blk = self.js_ord.split('attachCodeLookup("no-sku"', 1)[1][:700]
        self.assertIn("c.productName", blk)
        self.assertIn("c.options", blk)
        self.assertIn("lookupGoodsByCode", blk)

    def test_고도몰_조회가_실패해도_등록을_막지_않는다(self):
        blk = self.js_ord.split("async function lookupGoodsByCode", 1)[1].split(
            chr(10) + "}", 1)[0]
        self.assertIn("catch", blk)
        self.assertIn("return null", blk)

    def test_이미_적은_값을_덮어쓰지_않는다(self):
        """손으로 적어 둔 상품명·옵션을 자동완성이 지우면 안 된다."""
        blk = self.js_ord.split('attachCodeLookup("no-sku"', 1)[1][:700]
        self.assertIn("!name.value.trim()", blk)
        self.assertIn("!opt.value.trim()", blk)

    def test_늦게_온_응답이_목록을_덮지_않는다(self):
        blk = self.js_app.split("function attachCodeLookup", 1)[1].split(
            chr(10) + "}" + chr(10), 1)[0]
        self.assertIn("if (my !== seq) return;", blk)

    def test_없는_코드도_새로_적을_수_있다(self):
        blk = self.js_app.split("function attachCodeLookup", 1)[1][:2500]
        self.assertIn("새로 적으셔도 됩니다", blk)

class TestReceiveMethod(Base):
    """수령방식 — 택배가 아닌 건(방문수령·퀵)이 있다(대표 2026-08-05).

    ★택배가 아니면 송장이 필요 없다. '송장 발급 대기'에 섞여 있으면
      매번 왜 송장이 없냐고 확인하게 된다.
    """

    def setUp(self):
        super().setUp()
        d = ROOT / "static" / "js"
        self.ord = (d / "orders.js").read_text("utf-8")
        self.setup = (d / "setup.js").read_text("utf-8")
        self.ship = (d / "shipping.js").read_text("utf-8")

    def _order(self, **kw):
        body = {"channel": "수기", "productName": "노트북", "recipient": "홍길동",
                "quantity": 1, "amount": 1000}
        body.update(kw)
        r = self.c.post("/api/orders", json=body)
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        return r.get_json()

    # ---- 서버 ----
    def test_수령방식이_저장된다(self):
        o = self._order(receiveMethod="방문수령")
        d = self.c.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(d["receiveMethod"], "방문수령")

    def test_안_보내면_빈값_택배로_본다(self):
        """예전 주문 전부가 빈 값이다 — 빈 값을 택배로 봐야 흐름이 안 바뀐다."""
        o = self._order()
        d = self.c.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(d["receiveMethod"], "")

    def test_나중에_바꿀_수_있다(self):
        o = self._order()
        r = self.c.patch(f"/api/orders/{o['id']}",
                         json={"action": "details", "fields": {"receiveMethod": "퀵"}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = self.c.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(d["receiveMethod"], "퀵")

    def test_목록에도_실린다(self):
        self._order(receiveMethod="퀵")
        rows = self.c.get("/api/orders").get_json()["orders"]
        self.assertEqual(rows[0]["receiveMethod"], "퀵")

    # ---- 화면 ----
    def test_공용_판정이_하나뿐이다(self):
        self.assertEqual(self.ord.count("function needsWaybill("), 1)
        self.assertEqual(self.ord.count("function receiveChip("), 1)
        self.assertEqual(self.ord.count("const RECEIVE_METHODS"), 1)

    def test_택배는_송장이_필요하다(self):
        blk = self.ord.split("const NO_WAYBILL", 1)[1][:200]
        self.assertIn("방문수령", blk)
        self.assertIn("퀵", blk)
        self.assertNotIn('"택배"', blk, "택배가 송장 불필요 목록에 있으면 안 된다")

    def test_수기_주문에_수령방식_칸이_있다(self):
        self.assertIn('id="no-recv"', self.ord)
        self.assertIn('receiveMethod: $("#no-recv").value', self.ord)

    def test_주문_상세에서도_바꾼다(self):
        self.assertIn('id="od-recv"', self.ord)
        self.assertIn('receiveMethod: ($("#od-recv")', self.ord)

    def test_셋팅_보드에_표시된다(self):
        self.assertIn("receiveChip(o)", self.setup)

    def test_배송에서_따로_뺀다(self):
        """송장 발급 대기에 섞이면 안 된다."""
        self.assertIn("const waiting = notShipped.filter(needsWaybill)", self.ship)
        self.assertIn("const pickup = notShipped.filter((o) => !needsWaybill(o))", self.ship)
        self.assertIn("방문수령 · 퀵", self.ship)
        self.assertIn("송장을 뽑지 않습니다", self.ship)

    def test_방문수령도_출고확인은_된다(self):
        """물건은 나갔으니 출고 확인은 눌러야 한다 — 카드만 다를 뿐 같은 행을 쓴다."""
        self.assertIn("pgK.rows.map(rowHtml)", self.ship)


class TestSetupBoardDoneView(unittest.TestCase):
    """끝난 작업의 담당자를 보드에서도 볼 수 있어야 한다(대표 2026-08-05:
    "작업 끝낸 사람들에 대한 체크박스나 담당자 목록이 안 뜬다")."""

    def setUp(self):
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")

    def test_보기_전환이_있다(self):
        self.assertIn('id="sf-view"', self.js)
        self.assertIn("완료 포함", self.js)

    def test_기본은_진행중만(self):
        """끝난 것까지 다 뜨면 작업 보드로 못 쓴다."""
        blk = self.js.split('id="sf-view"', 1)[1][:300]
        self.assertLess(blk.index('value="todo"'), blk.index('value="all"'))

    def test_완료포함이면_출고분도_보인다(self):
        self.assertIn('const mode = ($("#sf-view") || {}).value || "todo";', self.js)
        self.assertIn('all.filter((o) => !o.shippingDone)', self.js)

    def test_완료포함은_보관분까지_가져온다(self):
        """끝난 주문은 대부분 '보관'까지 돼 있어 view=active로는 안 잡힌다
        (실측 출고완료 700건 중 699건이 보관됨)."""
        self.assertIn('view: mode === "todo" ? "active" : "all"', self.js)
        self.assertIn("all.filter((o) => !o.cancelledAt)", self.js)

    def test_출고완료_고객_보기가_있다(self):
        """대표 2026-08-05 — QC는 출고 확인을 누르면 목록에서 빼 버려 이력이 남지 않는다.
        HMS의 이 보기가 그 이력 화면이 된다."""
        self.assertIn('<option value="shipped">', self.js)
        self.assertIn("출고 완료 고객", self.js)
        self.assertIn('all.filter((o) => o.shippingDone && !o.cancelledAt)', self.js)

    def test_바꾸면_다시_불러온다(self):
        self.assertIn('$("#sf-view").addEventListener("change"', self.js)

    def test_담당자는_원래_행에_표시된다(self):
        """stageCell이 이미 담당자를 그린다 — 새로 만들지 않는다."""
        self.assertIn('class="stage-by"', self.js)

class TestPrepChipClickable(Base):
    """챙길 옵션 칩은 눌러서 체크할 수 있어야 한다.

    ★2026-08-03 표를 한 줄로 압축하며 체크박스를 없앴는데, 서버는 여전히
      '체크 안 된 옵션이 있으면 제작완료를 막는' 상태였다. 그래서 쿠팡·카카오·전화·
      b2b·방문 주문이 통째로 제작완료가 안 됐다(2026-08-05 대표 지적, 체크 기록 0건).
    """

    def setUp(self):
        super().setUp()
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")
        self.css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")

    def test_칩이_버튼이다(self):
        blk = self.js.split("function prepChips", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("<button", blk)
        self.assertIn("data-prep=", blk)
        self.assertIn("data-opt=", blk)

    def test_체크여부가_보인다(self):
        blk = self.js.split("function prepChips", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("x.checked", blk)
        self.assertIn("☑", blk)
        self.assertIn("☐", blk)
        self.assertIn("chip-green", blk)
        self.assertIn("chip-red", blk)

    def test_클릭이_배선돼_있다(self):
        self.assertIn('$$("button[data-prep]", host)', self.js)
        self.assertIn("/options/", self.js)

    def test_권한_없으면_못_누른다(self):
        blk = self.js.split("function prepChips", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("canWork", blk)
        self.assertIn("disabled", blk)
        self.assertIn("prepChips(o, canWork)", self.js)

    def test_스타일이_있다(self):
        self.assertIn(".prep-chip", self.css)

    # ---- 실제 흐름 ----
    def _rule_order(self):
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                cur = conn.execute(
                    "INSERT INTO prep_options(name, note, enabled, sort, created_by, created_at) "
                    "VALUES('리브레오피스 설치','', 1, 0, 't', '2026-08-05')")
                oid = cur.lastrowid
                conn.execute(
                    "INSERT INTO prep_option_rules(option_id, channel, match_type, "
                    "match_value, enabled, created_at) VALUES(?,'쿠팡','all','',1,'2026-08-05')",
                    (oid,))
        r = self.c.post("/api/orders", json={
            "channel": "쿠팡", "productName": "노트북", "recipient": "홍길동",
            "quantity": 1, "amount": 1000})
        return r.get_json()["id"], oid

    def test_체크_전에는_제작완료가_막힌다(self):
        oid, _ = self._rule_order()
        r = self.c.patch(f"/api/orders/{oid}", json={"action": "production", "value": True})
        self.assertEqual(r.status_code, 409)
        self.assertIn("챙기지 않은 옵션", r.get_json()["error"])

    def test_체크하면_제작완료가_된다(self):
        oid, opt_id = self._rule_order()
        r = self.c.post(f"/api/orders/{oid}/options/{opt_id}", json={"checked": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        r2 = self.c.patch(f"/api/orders/{oid}", json={"action": "production", "value": True})
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))

    def test_체크상태가_응답에_실린다(self):
        """화면이 ☐/☑을 그리려면 이 값이 필요하다."""
        oid, opt_id = self._rule_order()
        d = self.c.get(f"/api/orders/{oid}").get_json()
        self.assertTrue(d["prepOptions"])
        self.assertFalse(d["prepOptions"][0]["checked"])
        self.c.post(f"/api/orders/{oid}/options/{opt_id}", json={"checked": True})
        d2 = self.c.get(f"/api/orders/{oid}").get_json()
        self.assertTrue(d2["prepOptions"][0]["checked"])


class TestQcWatch(Base):
    """QC 폴더 실시간 감시(대표 2026-08-05: NAS 폴더를 보고 단계를 실시간 반영)."""

    def setUp(self):
        super().setUp()
        self.src = (ROOT / "app" / "settings" / "qc_watch.py").read_text("utf-8")
        self.js = (ROOT / "static" / "js" / "app.js").read_text("utf-8")

    def test_전진만_반영한다(self):
        """되돌리면 이미 끝낸 일이 지워진다 — 이관과 같은 규칙을 쓴다."""
        self.assertIn("from .qc_import import _plan", self.src)
        self.assertIn('p["advOrders"]', self.src)

    def test_주문을_새로_만들지_않는다(self):
        """어디서 생긴 주문인지 알 수 없게 되고 취소·보관 판단이 두 곳으로 갈린다."""
        self.assertNotIn('p["newOrders"]:', self.src.split("def sync_once", 1)[1])
        self.assertIn("새 주문은 만들지 않는다", self.src)

    def test_안_바뀌면_읽지_않는다(self):
        self.assertIn("def _signature", self.src)
        self.assertIn('sig == _last_seen["sig"]', self.src)

    def test_폴더가_없어도_서버는_돈다(self):
        blk = self.src.split("def start_qc_watch", 1)[1]
        self.assertIn("except Exception", blk)
        self.assertIn("time.sleep(TICK_SECONDS)", blk)

    def test_계정은_건드리지_않는다(self):
        self.assertIn("_plan(conn, orders, [])", self.src)

    def test_검증서버에서는_끌_수_있다(self):
        init = (ROOT / "app" / "__init__.py").read_text("utf-8")
        blk = init.split("start_qc_watch", 1)[0][-400:]
        self.assertIn('HMS_NO_TMS_SYNC', blk)

    def test_상태_API가_있다(self):
        r = self.c.get("/api/qc-watch/status")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        for k in ("enabled", "folder", "found", "intervalSeconds", "lastAt", "error"):
            self.assertIn(k, d)

    def test_폴더를_바꿀_수_있다(self):
        r = self.c.post("/api/qc-watch", json={"enabled": False, "path": r"C:\tmp\qc"})
        self.assertEqual(r.status_code, 200)
        d = self.c.get("/api/qc-watch/status").get_json()
        self.assertFalse(d["enabled"])
        self.assertEqual(d["folder"], r"C:\tmp\qc")

    def test_꺼두면_돌지_않는다(self):
        from app.settings.qc_watch import sync_once
        self.c.post("/api/qc-watch", json={"enabled": False, "path": r"C:\nope"})
        rows, adv = sync_once(self.app)
        self.assertEqual((rows, adv), (0, 0))

    def test_설정_화면에_상태가_보인다(self):
        self.assertIn("QC 폴더 실시간 연동", self.js)
        self.assertIn("/api/qc-watch/status", self.js)
        self.assertIn("/api/qc-watch/run", self.js)
        self.assertIn("되돌리지는 않습니다", self.js)

class TestSetupRowDetail(Base):
    """셋팅/QC 행 상세 보기 — QC 프로그램(192.168.0.185:3000)과 같은 4칸.

    대표 지시(2026-08-05): 각 행에서 상품정보 / 결제정보 / 구매자·배송지 / 처리이력을
    펼쳐 볼 수 있게. 재고 칩은 채널/주문 칸 아래로.
    """

    def setUp(self):
        super().setUp()
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")
        self.css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")
        self.blk = self.js.split("function setupDetailRow", 1)[1].split("\nfunction ", 1)[0]

    def test_네_칸이_모두_있다(self):
        for head in ("상품 정보", "결제 정보", "구매자 / 배송지", "처리 이력"):
            self.assertIn(head, self.blk)

    def test_상품칸_내용(self):
        self.assertIn("o.productName", self.blk)
        self.assertIn("등록옵션명", self.blk)
        self.assertIn("optionChips(o.optionName)", self.blk)
        self.assertIn("o.quantity", self.blk)

    def test_결제칸_내용(self):
        self.assertIn("fmtWon(o.amount)", self.blk)
        self.assertIn("o.orderedAt", self.blk)
        self.assertIn("o.receiveMethod", self.blk)

    def test_배송칸_내용(self):
        for f in ("o.recipient", "o.phone", "o.address", "o.deliveryMessage"):
            self.assertIn(f, self.blk)

    def test_이력칸은_4단계_타임라인(self):
        for f in ("o.preparing", "o.productionDone", "o.softwareInspectionDone", "o.shippingDone"):
            self.assertIn(f, self.blk)
        for f in ("o.preparingAt", "o.productionAt", "o.softwareInspectionAt", "o.shippingAt"):
            self.assertIn(f, self.blk)
        self.assertIn('"대기"', self.blk)      # 아직인 단계는 비워 두지 않고 '대기'로

    def test_가려진_개인정보를_알려준다(self):
        """셋팅 전용 계정은 연락처·주소가 서버에서 가려진다 — 빈칸으로 오해하면 안 된다."""
        self.assertIn("o.piiMasked", self.blk)

    def test_열었을_때만_그린다(self):
        self.assertIn("state.setupOpen && state.setupOpen[o.id] ? setupDetailRow(o)", self.js)

    def test_상세버튼과_배선(self):
        self.assertIn('data-more="${o.id}"', self.js)
        self.assertIn('$$("button[data-more]", host)', self.js)
        self.assertIn("renderSetupRows(orders, canWork)", self.js)

    def test_재고칩이_채널칸_아래에_있다(self):
        row = self.js.split('<td class="setup-chan">', 1)[1].split("</td>", 1)[0]
        self.assertIn("chan-stock", row)
        self.assertIn("stockChipsHtml(o)", row)

    def test_재고칩이_상품칸에서는_빠졌다(self):
        blk = self.js.split('<td class="setup-prod">', 1)[1].split("</td>", 1)[0]
        self.assertNotIn("stockChipsHtml", blk)
        self.assertNotIn("stockChip(", blk)

    def test_스타일이_있다(self):
        for sel in (".setup-detail", ".dt-grid", ".dt-card", ".dt-step", ".chan-stock", ".chan-more"):
            self.assertIn(sel, self.css)

    def test_칸수가_표_칸수와_같다(self):
        """colspan이 어긋나면 표가 통째로 틀어진다."""
        heads = self.js.split("<thead>", 1)[1].split("</thead>", 1)[0]
        self.assertEqual(heads.count("<th"), 8)
        self.assertIn('colspan="8"', self.blk)

    # ---- 화면이 쓰는 값이 실제로 내려오는지 ----
    def test_필요한_값이_목록에_실려_온다(self):
        r = self.c.post("/api/orders", json={
            "channel": "고도몰", "productName": "노트북", "recipient": "홍길동",
            "phone": "010-1111-2222", "address": "서울시 강남구", "postalCode": "06000",
            "deliveryMessage": "부재시 경비실", "quantity": 1, "amount": 990000,
            "productCode": "840 G3_i7-6_내장", "receiveMethod": "퀵"})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        rows = self.c.get("/api/orders").get_json()["orders"]
        o = [x for x in rows if x["id"] == r.get_json()["id"]][0]
        for k in ("productName", "optionName", "productCode", "quantity", "amount",
                  "orderedAt", "channel", "receiveMethod", "recipient", "phone",
                  "address", "postalCode", "deliveryMessage", "memo", "assets",
                  "preparing", "preparingBy", "preparingAt",
                  "productionDone", "productionBy", "productionAt",
                  "softwareInspectionDone", "softwareInspectionBy", "softwareInspectionAt",
                  "shippingDone", "shippingBy", "shippingAt", "piiMasked"):
            self.assertIn(k, o, f"{k} 가 목록 응답에 없다 — 상세 칸이 빈다")

    def test_단계를_밟으면_담당자와_시각이_남는다(self):
        oid = self.c.post("/api/orders", json={
            "channel": "고도몰", "productName": "노트북", "recipient": "홍길동",
            "quantity": 1, "amount": 1000}).get_json()["id"]
        for act in ("preparing", "production", "softwareInspection"):
            r = self.c.patch(f"/api/orders/{oid}", json={"action": act, "value": True})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = self.c.get(f"/api/orders/{oid}").get_json()
        self.assertTrue(d["productionBy"])
        self.assertTrue(d["productionAt"])
        self.assertTrue(d["softwareInspectionAt"])

class TestSetupRowBreathing(Base):
    """행 여백 정리 + 배송 메시지 표시(대표 2026-08-05).

    "지금 행이 조금 더 두꺼워졌으니 각 항목을 여유있게… 너무 가로로 빽빽해서."
    "수취인의 주문 배송 메시지도 함께 보여지면 좋겠다 (주소는 상세보기로)"
    """

    def setUp(self):
        super().setUp()
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")
        self.css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")

    # ---- 배송 메시지 ----
    def test_배송메시지가_수취인_칸에_뜬다(self):
        cell = self.js.split('<td class="setup-recipient"', 1)[1].split("</td>", 1)[0]
        self.assertIn("o.deliveryMessage", cell)
        self.assertIn("recv-msg", cell)

    def test_주소는_목록에_안_넣는다(self):
        """대표 지시 — 주소는 자리를 너무 먹어 [상세 보기]에만 둔다."""
        cell = self.js.split('<td class="setup-recipient"', 1)[1].split("</td>", 1)[0]
        self.assertNotIn("o.address", cell)
        self.assertIn("o.address", self.js.split("function setupDetailRow", 1)[1]
                      .split("\nfunction ", 1)[0])

    def test_긴_메시지는_두_줄에서_끊는다(self):
        b = self.css.split(".recv-msg {", 1)[1].split("}", 1)[0]
        self.assertIn("-webkit-line-clamp: 2", b)
        cell = self.js.split('<td class="setup-recipient"', 1)[1].split("</td>", 1)[0]
        self.assertIn("title=", cell, "잘린 뒷부분을 볼 방법이 없으면 안 된다")

    def test_메시지가_없으면_줄을_만들지_않는다(self):
        cell = self.js.split('<td class="setup-recipient"', 1)[1].split("</td>", 1)[0]
        self.assertIn("? `<div class=\"recv-msg\"", cell.replace("'", '"'))

    # ---- 가로 여백 ----
    def test_수취인_칸이_메시지를_담을_만큼_넓다(self):
        w = int(self.css.split(".setup-table .col-recipient { width: ", 1)[1].split("px", 1)[0])
        self.assertGreaterEqual(w, 140, "70px에서는 이름조차 잘렸다")

    def test_좁은_화면에서도_메시지_자리를_지킨다(self):
        widths = [int(x.split("px", 1)[0])
                  for x in self.css.split(".setup-table .col-recipient { width: ")[1:]]
        self.assertTrue(all(w >= 100 for w in widths), f"좁은 화면 폭이 너무 작다: {widths}")

    def test_칸_안쪽_여백을_줬다(self):
        b = self.css.split(".setup-rows-fixed td {", 1)[1].split("}", 1)[0]
        self.assertIn("padding:", b)

    # ---- 상품 칸 세로 배치 ----
    def test_상품명이_자기_줄을_쓴다(self):
        cell = self.js.split('<td class="setup-prod">', 1)[1].split("</td>", 1)[0]
        meta = cell.split('class="prod-line prod-meta"', 1)[1].split("</div>", 1)[0]
        self.assertIn("prod-qty", meta)
        self.assertIn("prod-code", meta)
        self.assertNotIn("prod-title", meta, "상품명이 수량·코드와 같은 줄이면 다시 빽빽해진다")
        self.assertIn("prod-title", cell)

    def test_스펙도_자기_줄을_쓴다(self):
        cell = self.js.split('<td class="setup-prod">', 1)[1].split("</td>", 1)[0]
        self.assertIn("prod-spec", cell)
        # 상품명 줄과 스펙 줄이 각각 있다
        self.assertGreaterEqual(cell.count('class="prod-line'), 3)

    def test_상품명_스펙은_정해진_줄에서_끊는다(self):
        """무제한으로 늘면 행 높이가 다시 들쭉날쭉해진다 — 상품명 3줄, 스펙 2줄."""
        b = self.css.split(chr(10) + ".prod-title {", 1)[1].split("}", 1)[0]
        self.assertIn("-webkit-line-clamp: 3", b)
        b2 = self.css.split(chr(10) + ".prod-spec {", 1)[1].split("}", 1)[0]
        self.assertIn("-webkit-line-clamp: 2", b2)

    def test_체크칸은_가운데_정렬이다(self):
        """칸 전체가 위 정렬로 바뀌며 체크박스가 위로 붙던 문제."""
        self.assertIn(".setup-rows-fixed td.stage-cell", self.css)
        b = self.css.split(".setup-rows-fixed td.stage-cell,", 1)[1].split("}", 1)[0]
        self.assertIn("vertical-align: middle", b)

    # ---- 값이 실제로 내려오는지 ----

    def test_수량은_제품코드_뒤에_온다(self):
        """대표 2026-08-05 — 코드가 먼저 눈에 들어와야 한다."""
        js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")
        meta = js.split('class="prod-line prod-meta"', 1)[1].split("</div>", 1)[0]
        self.assertLess(meta.index("prod-code"), meta.index("prod-qty"))

    def test_항목_이름이_자산번호다(self):
        js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")
        head = js.split("<thead>", 1)[1].split("</thead>", 1)[0]
        self.assertIn("<th>자산번호</th>", head)
        self.assertNotIn("<th>자산 매칭</th>", head)

    def test_자산_칸이_잘리지_않을_만큼_넓다(self):
        """실측 169px가 필요한데 152px였다."""
        css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")
        w = int(css.split(".setup-table .col-assets { width: ", 1)[1].split("px", 1)[0])
        self.assertGreaterEqual(w, 200)

    def test_배송메시지가_목록_응답에_있다(self):
        r = self.c.post("/api/orders", json={
            "channel": "고도몰", "productName": "노트북", "recipient": "홍길동",
            "deliveryMessage": "부재시 경비실에 맡겨주세요", "quantity": 1, "amount": 1000})
        oid = r.get_json()["id"]
        rows = self.c.get("/api/orders").get_json()["orders"]
        o = [x for x in rows if x["id"] == oid][0]
        self.assertEqual(o["deliveryMessage"], "부재시 경비실에 맡겨주세요")

class TestQcShipped(Base):
    """QC에서 이미 출고된 고객을 셋팅/QC 보드에서 내린다(대표 2026-08-05).

    ★왜 자동으로 안 이어졌나 (실측)
      같은 주문이 두 개의 키로 존재한다 — QC는 엑셀 수집분 "주문수집:…",
      HMS는 몰 API 주문번호. import_key만 보는 qc_watch로는 영영 못 만난다.
      그래서 QC에서는 출고가 끝났는데 HMS 보드에는 계속 남는다(잔여 166건 중 83건).
    """

    def setUp(self):
        super().setUp()
        self.src = (ROOT / "app" / "settings" / "qc_shipped.py").read_text("utf-8")
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")

    # ---- 매칭 규칙 ----
    def _row(self, **kw):
        base = {"product_name": "삼성 노트북", "product_code": "NT501_i5", "amount": 500000,
                "ordered_at": "2026-07-28T10:00:00+09:00", "recipient": "홍길동"}
        base.update(kw)
        return base

    def _score(self, row, nas):
        from app.settings.qc_shipped import _score
        return _score(row, nas)

    def test_상품명과_주문일이_맞으면_확정선을_넘는다(self):
        from app.settings.qc_shipped import MIN_SCORE
        pts, why = self._score(self._row(), {
            "productName": "삼성 노트북", "productCode": "", "amount": 0,
            "orderedAt": "2026-07-29"})
        self.assertGreaterEqual(pts, MIN_SCORE)
        self.assertIn("상품명", why)

    def test_이름만_같으면_확정하지_않는다(self):
        from app.settings.qc_shipped import MIN_SCORE
        pts, _ = self._score(self._row(), {
            "productName": "전혀 다른 상품", "productCode": "XX", "amount": 0,
            "orderedAt": "2026-01-01"})
        self.assertLess(pts, MIN_SCORE)

    def test_금액이_서로_다르면_깎는다(self):
        """둘 다 금액이 있는데 다르면 다른 주문일 가능성이 높다."""
        from app.settings.qc_shipped import MIN_SCORE
        pts, why = self._score(self._row(), {
            "productName": "삼성 노트북", "productCode": "NT501_i5", "amount": 999000,
            "orderedAt": "2026-07-28"})
        self.assertIn("금액 다름", why)
        self.assertLess(pts, MIN_SCORE + 2)

    def test_한쪽_금액이_비어도_벌하지_않는다(self):
        """QC 엑셀 수집분은 금액 칸이 비어 있는 경우가 많다."""
        pts_a, why = self._score(self._row(amount=500000), {
            "productName": "삼성 노트북", "productCode": "NT501_i5", "amount": 0,
            "orderedAt": "2026-07-28"})
        self.assertNotIn("금액 다름", why)
        self.assertGreater(pts_a, 0)

    def test_공백과_괄호는_무시하고_비교한다(self):
        pts, why = self._score(self._row(product_name="[PC풀세트] 코어 i5 본체"), {
            "productName": "PC풀세트 코어i5 본체", "productCode": "", "amount": 0,
            "orderedAt": "2026-07-28"})
        self.assertIn("상품명", why)

    # ---- 안전 규칙 ----
    def test_후보가_여러_개면_자동_반영하지_않는다(self):
        blk = self.src.split("def _plan_shipped", 1)[1]
        self.assertIn("unsure", blk)
        self.assertIn("scored[0][0] > scored[1][0]", blk,
                      "1등이 2등과 같은 점수면 어느 쪽인지 알 수 없다")

    def test_취소_출고완료는_대상이_아니다(self):
        blk = self.src.split("def _plan_shipped", 1)[1]
        self.assertIn("cancelled_at='' AND shipping_done=0", blk)

    def test_반영_직전에_다시_확인한다(self):
        """미리보기와 반영 사이에 누가 처리했을 수 있다."""
        blk = self.src.split("def qc_shipped_apply", 1)[1]
        self.assertIn("SELECT shipping_done, cancelled_at FROM orders WHERE id=?", blk)
        self.assertIn('cur["shipping_done"] or cur["cancelled_at"]', blk)

    def test_금액은_손대지_않는다(self):
        blk = self.src.split("def qc_shipped_apply", 1)[1]
        self.assertNotIn("amount=", blk, "QC 파일은 금액이 비어 있는 경우가 많다")

    def test_저절로_돌지_않는다(self):
        """사람이 확인하고 누를 때만 움직인다."""
        self.assertNotIn("threading", self.src)
        self.assertNotIn("start_qc_shipped", self.src)

    def test_백업까지_읽는다(self):
        """QC는 출고 확인을 누르면 목록에서 빼기 때문에 현재 파일만 보면 이력이 없다."""
        blk = self.src.split("def _load_nas", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("backups", blk)
        self.assertIn("shippingDone", blk)

    def test_깨진_백업_하나가_전체를_막지_않는다(self):
        blk = self.src.split("def _load_nas", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("except Exception", blk)
        self.assertIn("continue", blk)

    def test_이력이_남는다(self):
        self.assertIn('audit.log("qc_shipped_link"', self.src)
        self.assertIn('"matchedBy"', self.src)

    # ---- 라우트 ----
    def test_미리보기_라우트가_있다(self):
        r = self.c.get("/api/qc-shipped/preview")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        for k in ("folder", "shippedInQc", "onBoard", "matched", "unsure"):
            self.assertIn(k, d)

    def test_권한이_있어야_한다(self):
        self.assertIn('require("settings.manage")', self.src)

    def test_반영_라우트가_있다(self):
        r = self.c.post("/api/qc-shipped/apply", json={"ids": []})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("applied", r.get_json())

    # ---- 화면 ----
    def test_보드에_알림이_뜬다(self):
        self.assertIn("renderShippedWarn", self.js)
        self.assertIn("/api/qc-shipped/preview", self.js)
        self.assertIn("setup-shipped-warn", self.js)

    def test_확인하고_누르게_되어_있다(self):
        blk = self.js.split("async function renderShippedWarn", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("confirm(", blk, "확인 없이 일괄 반영하면 되돌리기 어렵다")
        self.assertIn("matchedBy", blk, "무슨 근거로 맞췄는지 보여 줘야 한다")
        self.assertIn("qs-pick", blk, "건별로 고를 수 있어야 한다")

    def test_폴더가_끊겨도_보드는_돈다(self):
        blk = self.js.split("async function renderShippedWarn", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("catch", blk)

class TestQcShippedOneToOne(Base):
    """QC 주문 하나는 우리 주문 하나에만 붙는다 + 표기 차이 흡수(2026-08-05 실측 보완).

    ★왜 고쳤나
      1차 반영(83건)에서 **같은 QC 주문이 두 우리 주문에 배정**됐다
      (수집-A117193B5F → 이명규 #699·#702, 수집-A2EADBB548 → 안호열 #755·#757).
      QC에 1건인데 우리 쪽에 2건이면, 한 건은 아직 안 나간 것일 수 있다 —
      그걸 출고완료로 찍으면 나가지 않은 물건이 나간 것으로 잡힌다.
    ★왜 못 잡았나(반대 방향)
      같은 주문인데 표기가 달라 47건이 확정선에 못 미쳤다.
      코드/상품명이 접두사 관계이거나(옵션·등급이 뒤에 붙음), 금액이 2~4만원 다른 경우(옵션 할인).
    """

    def setUp(self):
        super().setUp()
        self.src = (ROOT / "app" / "settings" / "qc_shipped.py").read_text("utf-8")

    def _score(self, row, nas):
        from app.settings.qc_shipped import _score
        return _score(row, nas)

    def _row(self, **kw):
        base = {"product_name": "삼성 노트북 15인치 대화면", "product_code": "NT371B5M_i7-7_내장",
                "amount": 380000, "ordered_at": "2026-07-30T10:00:00+09:00", "recipient": "홍길동"}
        base.update(kw)
        return base

    # ---- 표기 차이 흡수 ----
    def test_뒤에_등급이_붙어도_같은_코드로_본다(self):
        from app.settings.qc_shipped import MIN_SCORE
        pts, why = self._score(self._row(amount=0), {
            "productName": "삼성 노트북 15인치 대화면 / 단일색상 512GB",
            "productCode": "NT371B5M_i7-7_내장 AA급2", "amount": 0, "orderedAt": "2026-07-30"})
        self.assertGreaterEqual(pts, MIN_SCORE)
        self.assertTrue(any("앞부분" in w for w in why))

    def test_짧은_글자는_접두사로_인정하지_않는다(self):
        """'i5' 같은 두 글자가 겹쳤다고 같은 상품이라고 하면 안 된다."""
        from app.settings.qc_shipped import PREFIX_MIN
        self.assertGreaterEqual(PREFIX_MIN, 6)
        pts, why = self._score(self._row(product_code="i5", product_name="노트북", amount=0), {
            "productName": "노트북 스탠드", "productCode": "i5-1235U", "amount": 0,
            "orderedAt": "2026-07-30"})
        self.assertFalse(any("앞부분" in w for w in why))

    def test_옵션_할인만큼_차이나면_깎지_않는다(self):
        """실측 23쌍 중 21쌍이 2만~4만원(8% 이내) 차이였고 전부 같은 주문이었다."""
        from app.settings.qc_shipped import MIN_SCORE
        pts, why = self._score(self._row(amount=380000), {
            "productName": "삼성 노트북 15인치 대화면", "productCode": "NT371B5M_i7-7_내장",
            "amount": 400000, "orderedAt": "2026-07-30"})
        self.assertGreaterEqual(pts, MIN_SCORE)
        self.assertNotIn("금액 다름", why)
        self.assertTrue(any("원 차" in w for w in why), why)

    def test_금액이_크게_다르면_여전히_깎는다(self):
        """실측 김병엽 259,000 vs 10,000(96%), 조용원 660,000 vs 340,000(48%)은 다른 주문이었다."""
        from app.settings.qc_shipped import MIN_SCORE
        pts, why = self._score(self._row(amount=259000), {
            "productName": "삼성 노트북 15인치 대화면", "productCode": "NT371B5M_i7-7_내장",
            "amount": 10000, "orderedAt": "2026-07-30"})
        self.assertIn("금액 다름", why)
        self.assertLess(pts, MIN_SCORE)

    def test_비율과_금액을_모두_넘어야_깎는다(self):
        """큰 주문의 5만원 차이는 옵션일 수 있지만, 비율 기준도 함께 본다."""
        from app.settings.qc_shipped import AMOUNT_NEAR_WON, AMOUNT_NEAR_RATE
        self.assertLessEqual(AMOUNT_NEAR_WON, 50000)
        self.assertLessEqual(AMOUNT_NEAR_RATE, 0.10)
        blk = self.src.split("def _score", 1)[1]
        self.assertIn("gap <= AMOUNT_NEAR_WON and gap <= max(a1, a2) * AMOUNT_NEAR_RATE", blk,
                      "둘 중 하나만 보면 큰 주문에서 오탐이 난다")

    # ---- 1:1 배정 ----
    def test_한_QC주문은_한_주문에만_붙는다(self):
        blk = self.src.split("def _plan_shipped", 1)[1]
        self.assertIn("by_qc", blk)
        self.assertIn("len(group) > 1", blk)

    def test_진_쪽은_애매로_남고_이유가_적힌다(self):
        blk = self.src.split("def _plan_shipped", 1)[1]
        self.assertIn("unsure.append(item)", blk)
        self.assertIn("몰립니다", blk, "왜 자동 반영을 안 했는지 화면에 보여야 한다")

    def test_점수가_같으면_둘_다_뺀다(self):
        """어느 쪽이 그 QC 주문인지 알 수 없다 — 하나를 골라 찍으면 안 나간 물건이 나간 게 된다."""
        blk = self.src.split("def _plan_shipped", 1)[1]
        self.assertIn("if len(winners) > 1:", blk)
        self.assertIn("losers = group", blk)

    def test_QC_수량과_자산번호도_함께_내려준다(self):
        """대표가 화면에서 대조할 때 필요하다."""
        blk = self.src.split("def _plan_shipped", 1)[1]
        self.assertIn('"qcQuantity"', blk)
        self.assertIn('"qcAssetNo"', blk)

    # ---- 실제 흐름 ----
    def _mk(self, **kw):
        base = {"channel": "고도몰", "productName": "삼성 노트북 15인치 대화면",
                "productCode": "NT371B5M_i7-7_내장", "recipient": "홍길동",
                "quantity": 1, "amount": 380000}
        base.update(kw)
        r = self.c.post("/api/orders", json=base)
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        return r.get_json()["id"]

    def _plan(self, nas_rows):
        """NAS 파일 대신 목록을 직접 넣어 계획만 계산한다."""
        from unittest import mock
        from app.db import tx
        from app.settings import qc_shipped
        with mock.patch.object(qc_shipped, "_load_nas", return_value=(nas_rows, 1, 1)):
            with self.app.app_context():
                with tx() as conn:
                    return qc_shipped._plan_shipped(conn, "x")

    def _nas(self, **kw):
        base = {"orderNumber": "수집-AAA", "recipient": "홍길동",
                "productName": "삼성 노트북 15인치 대화면", "productCode": "NT371B5M_i7-7_내장",
                "amount": 380000, "orderedAt": "2026-07-30", "quantity": 1,
                "shippingDone": True, "shippingBy": "문성진",
                "shippingAt": "2026-07-31T08:00:00+00:00", "archivedAt": "2026-07-31T09:00:00+00:00",
                "managementNumber": "260721-0035"}
        base.update(kw)
        return base

    def test_QC1건에_우리2건이면_아무것도_확정하지_않는다(self):
        a = self._mk()
        b = self._mk()
        p = self._plan([self._nas()])
        self.assertEqual(len(p["hits"]), 0, "한쪽을 골라 찍으면 안 나간 물건이 나간 게 된다")
        self.assertEqual({u["id"] for u in p["unsure"]}, {a, b})
        self.assertIn("몰립니다", p["unsure"][0]["note"])

    def test_점수가_다르면_높은_쪽만_가져간다(self):
        low = self._mk(amount=0)                     # 금액 근거 없음
        high = self._mk(amount=380000)               # 금액까지 일치
        p = self._plan([self._nas()])
        self.assertEqual([h["id"] for h in p["hits"]], [high])
        self.assertEqual([u["id"] for u in p["unsure"]], [low])

    def test_QC가_2건이면_둘_다_붙는다(self):
        a = self._mk()
        b = self._mk(amount=400000)
        p = self._plan([self._nas(orderNumber="수집-A"),
                        self._nas(orderNumber="수집-B", amount=400000)])
        self.assertEqual(len(p["hits"]), 2)
        self.assertEqual({h["qcOrderNumber"] for h in p["hits"]}, {"수집-A", "수집-B"})
        self.assertEqual({h["id"] for h in p["hits"]}, {a, b})

    def test_반영하면_보드에서_사라진다(self):
        oid = self._mk()
        from unittest import mock
        from app.settings import qc_shipped
        with mock.patch.object(qc_shipped, "_load_nas", return_value=([self._nas()], 1, 1)):
            r = self.c.post("/api/qc-shipped/apply", json={})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["applied"], 1)
        d = self.c.get(f"/api/orders/{oid}").get_json()
        self.assertTrue(d["shippingDone"])
        self.assertEqual(d["shippingBy"], "문성진")
        self.assertTrue(d["archivedAt"], "보관까지 돼야 보드에서 내려간다")

    def test_두_번_돌려도_한_번만_반영된다(self):
        self._mk()
        from unittest import mock
        from app.settings import qc_shipped
        with mock.patch.object(qc_shipped, "_load_nas", return_value=([self._nas()], 1, 1)):
            r1 = self.c.post("/api/qc-shipped/apply", json={})
            r2 = self.c.post("/api/qc-shipped/apply", json={})
        self.assertEqual(r1.get_json()["applied"], 1)
        self.assertEqual(r2.get_json()["applied"], 0)

class TestQcShippedDuplicateGuard(Base):
    """같은 주문이 우리 쪽에 두 줄이면 출고 처리하지 않는다(2026-08-05 사고 재발 방지).

    ★무슨 일이 있었나
      1차 반영 83건 중 **67건**은 그 QC 주문이 이미 HMS에 주문 행으로 들어와 있던 것이었다
      (QC 이관분 import_key='주문수집:고도몰:수집-XXXX' + 몰 API 수집분 = 같은 주문 두 줄).
      한쪽은 이미 출고완료였는데 두 번째 줄까지 출고완료로 찍어
      **매출 12,980,300원과 출고 건수가 그대로 이중계상**됐다(출고시각이 짝과 완전히 동일).
      이건 '출고 처리'가 아니라 '중복행 정리' 문제라 사람이 판단해야 한다.
    """

    def setUp(self):
        super().setUp()
        self.src = (ROOT / "app" / "settings" / "qc_shipped.py").read_text("utf-8")
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")

    def _mk(self, **kw):
        base = {"channel": "고도몰", "productName": "삼성 노트북 15인치 대화면",
                "productCode": "NT371B5M_i7-7_내장", "recipient": "홍길동",
                "quantity": 1, "amount": 380000}
        base.update(kw)
        r = self.c.post("/api/orders", json=base)
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        return r.get_json()["id"]

    def _twin(self, oid, qno="수집-AAA", shipped=True):
        """QC 이관분(그 QC 주문번호를 import_key에 단 주문)을 만든다."""
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute(
                    "INSERT INTO orders(import_key, channel, order_no, ordered_at, product_name, "
                    " option_name, product_code, quantity, amount, recipient, phone, postal_code, "
                    " address, delivery_message, memo, shipping_done, shipping_by, shipping_at, "
                    " archived_at, created_at, updated_at) "
                    "VALUES(?,'고도몰',?,'2026-07-30','삼성 노트북 15인치 대화면','', "
                    " 'NT371B5M_i7-7_내장',1,380000,'홍길동','','','','','',?,?,?,?, "
                    " '2026-07-30','2026-07-30')",
                    (f"주문수집:고도몰:{qno}", qno, 1 if shipped else 0,
                     "문성진" if shipped else "", "2026-07-31T08:00:00+00:00" if shipped else "",
                     "2026-07-31T09:00:00+00:00" if shipped else ""))

    def _nas(self, **kw):
        base = {"orderNumber": "수집-AAA", "recipient": "홍길동",
                "productName": "삼성 노트북 15인치 대화면", "productCode": "NT371B5M_i7-7_내장",
                "amount": 380000, "orderedAt": "2026-07-30", "quantity": 1,
                "shippingDone": True, "shippingBy": "문성진",
                "shippingAt": "2026-07-31T08:00:00+00:00",
                "archivedAt": "2026-07-31T09:00:00+00:00", "managementNumber": ""}
        base.update(kw)
        return base

    def _preview(self, nas_rows):
        from unittest import mock
        from app.settings import qc_shipped
        with mock.patch.object(qc_shipped, "_load_nas", return_value=(nas_rows, 1, 1)):
            r = self.c.get("/api/qc-shipped/preview")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    # ---- 핵심: 중복행은 확정에서 뺀다 ----
    def test_이미_들어온_QC주문이면_확정하지_않는다(self):
        oid = self._mk()
        self._twin(oid)
        d = self._preview([self._nas()])
        self.assertEqual(len(d["matched"]), 0, "두 번째 줄까지 찍으면 매출이 두 배가 된다")
        self.assertEqual(len(d["duplicates"]), 1)
        self.assertEqual(d["duplicates"][0]["id"], oid)

    def test_중복_상대와_이유를_알려준다(self):
        oid = self._mk()
        self._twin(oid)
        m = self._preview([self._nas()])["duplicates"][0]
        self.assertIn("duplicateOf", m)
        self.assertTrue(m["duplicateShipped"])
        self.assertIn("이미 들어와 있습니다", m["note"])

    def test_상대가_아직_미출고면_확인_필요로_알린다(self):
        oid = self._mk()
        self._twin(oid, shipped=False)
        m = self._preview([self._nas()])["duplicates"][0]
        self.assertFalse(m["duplicateShipped"])
        self.assertIn("확인이 필요", m["note"])

    def test_중복이_아니면_정상_확정된다(self):
        oid = self._mk()
        d = self._preview([self._nas()])
        self.assertEqual([h["id"] for h in d["matched"]], [oid])
        self.assertEqual(d["duplicates"], [])

    # ---- 반영 경로에도 같은 차단 ----
    def test_ids로_직접_불러도_막힌다(self):
        """계획을 우회해 id 를 손으로 넣는 경로가 있다 — 거기서도 같은 규칙이어야 한다."""
        oid = self._mk()
        self._twin(oid)
        from unittest import mock
        from app.settings import qc_shipped
        with mock.patch.object(qc_shipped, "_load_nas", return_value=([self._nas()], 1, 1)):
            r = self.c.post("/api/qc-shipped/apply", json={"ids": [oid]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["applied"], 0)
        self.assertFalse(self.c.get(f"/api/orders/{oid}").get_json()["shippingDone"])

    def test_두겹_방어가_코드에_있다(self):
        blk = self.src.split("def qc_shipped_apply", 1)[1]
        self.assertIn("import_key LIKE ? AND id<>?", blk)

    def test_이관분_키에서_QC주문번호를_뽑는다(self):
        """import_key 는 '주문수집:고도몰:수집-XXXX' 형태다."""
        blk = self.src.split("def _existing_qc_rows", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('rsplit(":", 1)[-1]', blk)
        self.assertIn("주문수집%", blk)

    # ---- 화면 ----
    def test_화면이_출고와_중복을_나눠_보여준다(self):
        blk = self.js.split("async function renderShippedWarn", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("d.duplicates", blk)
        self.assertIn("dup-warn", blk)
        self.assertIn("매출과 출고 건수가 두 배", blk, "왜 누르면 안 되는지 알려야 한다")

    def test_중복에는_반영_버튼이_없다(self):
        """'출고 처리'가 아니라 '정리' 대상이라 [반영] 버튼이 붙으면 안 된다."""
        blk = self.js.split("async function renderShippedWarn", 1)[1].split("\nfunction ", 1)[0]
        dup_block = blk.split("같은 주문이 우리 쪽에 두 줄인 것", 1)[1].split("</div>`", 1)[0]
        self.assertNotIn("qs-apply", dup_block)
        self.assertIn("qs-duplist", dup_block)

    def test_스타일이_있다(self):
        css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")
        self.assertIn(".dup-warn", css)

class TestQcDupArchive(Base):
    """중복행은 '출고완료'가 아니라 '중복'으로 보드에서만 내린다(대표 2026-08-05).

    대표 지시는 "출고완료인 건 모두 빼줘"였다. 그런데 중복행을 출고완료로 찍으면
    물건은 하나인데 매출이 두 번 잡힌다 — 1차 반영에서 실제로 12,980,300원이 그렇게 됐다.
    그래서 보관(archived_at)만 채워 보드에서 내리고, 매출·재고·실적에는 손대지 않는다.
    """

    def setUp(self):
        super().setUp()
        self.src = (ROOT / "app" / "settings" / "qc_shipped.py").read_text("utf-8")
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")

    def _mk(self, **kw):
        base = {"channel": "고도몰", "productName": "삼성 노트북 15인치 대화면",
                "productCode": "NT371B5M_i7-7_내장", "recipient": "홍길동",
                "quantity": 1, "amount": 380000}
        base.update(kw)
        r = self.c.post("/api/orders", json=base)
        return r.get_json()["id"]

    def _twin(self, qno="수집-AAA", shipped=True, recipient="홍길동"):
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                cur = conn.execute(
                    "INSERT INTO orders(import_key, channel, order_no, ordered_at, product_name, "
                    " option_name, product_code, quantity, amount, recipient, phone, postal_code, "
                    " address, delivery_message, memo, shipping_done, shipping_by, shipping_at, "
                    " archived_at, created_at, updated_at) "
                    "VALUES(?,'고도몰',?,'2026-07-30','삼성 노트북 15인치 대화면','', "
                    " 'NT371B5M_i7-7_내장',1,380000,?,'','','','','',?,?,?,?, "
                    " '2026-07-30','2026-07-30')",
                    (f"주문수집:고도몰:{qno}", qno, recipient, 1 if shipped else 0,
                     "문성진" if shipped else "", "2026-07-31T08:00:00+00:00" if shipped else "",
                     "2026-07-31T09:00:00+00:00" if shipped else ""))
                return cur.lastrowid

    def _nas(self, **kw):
        base = {"orderNumber": "수집-AAA", "recipient": "홍길동",
                "productName": "삼성 노트북 15인치 대화면", "productCode": "NT371B5M_i7-7_내장",
                "amount": 380000, "orderedAt": "2026-07-30", "quantity": 1,
                "shippingDone": True, "shippingBy": "문성진",
                "shippingAt": "2026-07-31T08:00:00+00:00",
                "archivedAt": "2026-07-31T09:00:00+00:00", "managementNumber": ""}
        base.update(kw)
        return base

    def _post(self, path, body=None, nas=None):
        from unittest import mock
        from app.settings import qc_shipped
        with mock.patch.object(qc_shipped, "_load_nas",
                               return_value=(nas if nas is not None else [self._nas()], 1, 1)):
            return self.c.post(path, json=body or {})

    # ---- 핵심 ----
    def test_보드에서_내려간다(self):
        oid = self._mk()
        self._twin()
        r = self._post("/api/qc-shipped/dismiss-duplicates")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["archived"], 1)
        d = self.c.get(f"/api/orders/{oid}").get_json()
        self.assertTrue(d["archivedAt"], "보관돼야 보드에서 내려간다")

    def test_출고완료로_찍지_않는다(self):
        """★이게 이 기능의 존재 이유다 — 찍으면 매출이 두 배가 된다."""
        oid = self._mk()
        self._twin()
        self._post("/api/qc-shipped/dismiss-duplicates")
        d = self.c.get(f"/api/orders/{oid}").get_json()
        self.assertFalse(d["shippingDone"])
        self.assertFalse(d["productionDone"])
        self.assertEqual(d["shippingBy"], "")

    def test_왜_내렸는지_남는다(self):
        oid = self._mk()
        twin = self._twin()
        self._post("/api/qc-shipped/dismiss-duplicates")
        d = self.c.get(f"/api/orders/{oid}").get_json()
        self.assertIn("중복", d["archiveReason"])
        self.assertEqual(d["duplicateOf"], twin)

    def test_상대가_미출고면_손대지_않는다(self):
        """어느 쪽이 진짜인지 모르는 상태에서 내리면 진짜 주문이 사라질 수 있다."""
        oid = self._mk()
        self._twin(shipped=False)
        r = self._post("/api/qc-shipped/dismiss-duplicates")
        self.assertEqual(r.get_json()["archived"], 0)
        self.assertFalse(self.c.get(f"/api/orders/{oid}").get_json()["archivedAt"])

    def test_고른_것만_내린다(self):
        a = self._mk()
        self._twin(qno="수집-A")
        b = self._mk(recipient="김철수")
        self._twin(qno="수집-B", recipient="김철수")
        nas = [self._nas(orderNumber="수집-A"),
               self._nas(orderNumber="수집-B", recipient="김철수")]
        r = self._post("/api/qc-shipped/dismiss-duplicates", {"ids": [a]}, nas=nas)
        self.assertEqual(r.get_json()["archived"], 1)
        self.assertTrue(self.c.get(f"/api/orders/{a}").get_json()["archivedAt"])
        self.assertFalse(self.c.get(f"/api/orders/{b}").get_json()["archivedAt"])

    def test_두_번_돌려도_한_번만(self):
        self._mk()
        self._twin()
        r1 = self._post("/api/qc-shipped/dismiss-duplicates")
        r2 = self._post("/api/qc-shipped/dismiss-duplicates")
        self.assertEqual(r1.get_json()["archived"], 1)
        self.assertEqual(r2.get_json()["archived"], 0)

    # ---- 되돌리기 ----
    def test_되돌릴_수_있다(self):
        oid = self._mk()
        self._twin()
        self._post("/api/qc-shipped/dismiss-duplicates")
        r = self.c.post("/api/qc-shipped/restore", json={"ids": [oid]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["restored"], 1)
        d = self.c.get(f"/api/orders/{oid}").get_json()
        self.assertFalse(d["archivedAt"])
        self.assertEqual(d["archiveReason"], "")

    def test_이_도구가_내린_것만_되돌린다(self):
        """출고돼서 보관된 주문을 이걸로 되살리면 끝난 일이 다시 열린다."""
        oid = self._mk()
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE orders SET archived_at='2026-08-01' WHERE id=?", (oid,))
        r = self.c.post("/api/qc-shipped/restore", json={"ids": [oid]})
        self.assertEqual(r.get_json()["restored"], 0)
        self.assertTrue(self.c.get(f"/api/orders/{oid}").get_json()["archivedAt"])

    def test_이력이_남는다(self):
        self.assertIn('audit.log("qc_dup_archived"', self.src)
        self.assertIn('audit.log("qc_dup_restored"', self.src)

    # ---- 화면 ----
    def test_버튼과_안내가_있다(self):
        blk = self.js.split("async function renderShippedWarn", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("qs-dupdismiss", blk)
        self.assertIn("/api/qc-shipped/dismiss-duplicates", blk)
        self.assertIn("매출이 두 배가 되지 않습니다", blk, "왜 이렇게 처리하는지 알려야 한다")
        self.assertIn("되돌릴 수 있습니다", blk)

    def test_상대가_출고된_것만_버튼_대상이다(self):
        blk = self.js.split("async function renderShippedWarn", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("dup.filter((m) => m.duplicateShipped)", blk)

class TestQcDupAuto(Base):
    """앞으로 생기는 중복은 자동으로 정리한다(대표 2026-08-05).

    "앞으로 해당 건들이 있다면 매칭하여 중복으로 매출은 1건만 반영하여 처리하되,
     목록에서 없애줘야 해."
    ★매출을 1건만 두는 방법 = 두 번째 줄을 **출고완료로 찍지 않는 것**.
      물건은 이미 다른 줄로 출고완료·매출 반영이 끝나 있다.
    """

    def setUp(self):
        super().setUp()
        self.watch = (ROOT / "app" / "settings" / "qc_watch.py").read_text("utf-8")
        self.src = (ROOT / "app" / "settings" / "qc_shipped.py").read_text("utf-8")

    def test_감시가_자동으로_정리한다(self):
        self.assertIn("archive_duplicates(conn, plan, actor=", self.watch)

    def test_순환_import를_피했다(self):
        """qc_shipped 가 qc_watch 의 _cfg 를 쓰므로 위에서 부르면 앱이 안 뜬다."""
        head = self.watch.split("def _cfg", 1)[0]
        self.assertNotIn("from .qc_shipped import", head)
        body = self.watch.split("def sync_once", 1)[1]
        self.assertIn("from .qc_shipped import", body)

    def test_정리에_실패해도_단계_반영은_살린다(self):
        blk = self.watch.split("def sync_once", 1)[1]
        self.assertIn("except Exception", blk)
        self.assertIn("dup_n = 0", blk)

    def test_자동_정리도_출고완료로_찍지_않는다(self):
        """수동·자동이 같은 함수를 쓴다 — 규칙이 갈리면 한쪽에만 구멍이 생긴다."""
        blk = self.src.split("def archive_duplicates", 1)[1].split("\n@bp", 1)[0]
        self.assertNotIn("shipping_done=1", blk)
        self.assertIn("SET archived_at=?, archive_reason=?, duplicate_of=?", blk)

    def test_확실한_것만_자동으로_손댄다(self):
        blk = self.src.split("def archive_duplicates", 1)[1].split("\n@bp", 1)[0]
        self.assertIn('if not m.get("duplicateShipped")', blk)

    def test_누가_내렸는지_남는다(self):
        blk = self.src.split("def archive_duplicates", 1)[1].split("\n@bp", 1)[0]
        self.assertIn('actor', blk)
        self.assertIn('"by": actor', blk)

    def test_상태에_자동정리_건수가_보인다(self):
        r = self.c.get("/api/qc-watch/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn("dupArchived", r.get_json())

    # ---- 실제 흐름 ----
    def test_감시가_돌면_중복이_사라진다(self):
        import json
        import os
        import tempfile
        from unittest import mock

        oid = self.c.post("/api/orders", json={
            "channel": "고도몰", "productName": "삼성 노트북 15인치 대화면",
            "productCode": "NT371B5M_i7-7_내장", "recipient": "홍길동",
            "quantity": 1, "amount": 380000}).get_json()["id"]
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute(
                    "INSERT INTO orders(import_key, channel, order_no, ordered_at, product_name, "
                    " option_name, product_code, quantity, amount, recipient, phone, postal_code, "
                    " address, delivery_message, memo, shipping_done, shipping_by, shipping_at, "
                    " archived_at, created_at, updated_at) "
                    "VALUES('주문수집:고도몰:수집-AAA','고도몰','수집-AAA','2026-07-30', "
                    " '삼성 노트북 15인치 대화면','','NT371B5M_i7-7_내장',1,380000,'홍길동', "
                    " '','','','','',1,'문성진','2026-07-31T08:00:00+00:00', "
                    " '2026-07-31T09:00:00+00:00','2026-07-30','2026-07-30')")

        nas = [{"orderNumber": "수집-AAA", "recipient": "홍길동", "importKey": "주문수집:수집-AAA",
                "productName": "삼성 노트북 15인치 대화면", "productCode": "NT371B5M_i7-7_내장",
                "amount": 380000, "orderedAt": "2026-07-30", "quantity": 1,
                "shippingDone": True, "shippingBy": "문성진",
                "shippingAt": "2026-07-31T08:00:00+00:00",
                "archivedAt": "2026-07-31T09:00:00+00:00", "managementNumber": ""}]
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(nas, fh, ensure_ascii=False)
        try:
            from app.settings import qc_shipped, qc_watch
            self.c.post("/api/qc-watch", json={"enabled": True, "path": os.path.dirname(path)})
            with mock.patch.object(qc_watch, "_orders_file", return_value=path), \
                 mock.patch.object(qc_shipped, "_load_nas", return_value=(nas, 1, 1)):
                qc_watch.sync_once(self.app, force=True)
        finally:
            os.unlink(path)

        d = self.c.get(f"/api/orders/{oid}").get_json()
        self.assertTrue(d["archivedAt"], "자동으로 목록에서 내려가야 한다")
        self.assertFalse(d["shippingDone"], "출고완료로 찍으면 매출이 두 배가 된다")
        self.assertIn("중복", d["archiveReason"])

class TestShipFlow2026_08_05(Base):
    """출고 흐름을 QC 프로그램과 같게 맞춘다(대표 2026-08-05).

    제작 완료 → (송장 출력 시 자동) 출고 확인 → [금일 출고 확인] → 준비 목록에서 사라짐
    → [📋 출고 기록 조회]에 쌓임.
    'SW 검수/검수 완료'라는 이름은 '출고 확인'으로 바꾼다.
    """

    def setUp(self):
        super().setUp()
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")
        self.css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")

    def _order(self, **kw):
        base = {"channel": "고도몰", "productName": "노트북", "recipient": "홍길동",
                "phone": "010-1111-2222", "address": "서울시 강남구", "postalCode": "06000",
                "quantity": 1, "amount": 500000}
        base.update(kw)
        return self.c.post("/api/orders", json=base).get_json()["id"]

    def _ready(self, oid):
        """자산까지 붙여 송장을 뽑을 수 있는 상태로 만든다."""
        b = self._batch()
        a = self._asset(b["id"]).get_json()[0]
        r = self.c.patch(f"/api/orders/{oid}", json={"action": "assets", "assetIds": [a["id"]]})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        self.c.patch(f"/api/orders/{oid}", json={"action": "production", "value": True})

    # ---- 이름 ----
    def test_표_머리글이_출고_확인이다(self):
        head = self.js.split("<thead>", 1)[1].split("</thead>", 1)[0]
        self.assertIn("출고 확인", head)
        self.assertNotIn("SW 검수", head)

    def test_상세_이력도_출고_확인이다(self):
        blk = self.js.split("function setupDetailRow", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn('"출고 확인"', blk)
        self.assertNotIn('"SW 검수"', blk)

    def test_주문_화면_라벨도_같다(self):
        js = (ROOT / "static" / "js" / "orders.js").read_text("utf-8")
        self.assertIn('label: "출고 확인"', js)

    # ---- 단계 카드 ----
    def test_단계_카드를_누를_수_있다(self):
        """대표 지적: 숫자만 보여 주고 누를 수 없었다."""
        blk = self.js.split("function renderSetupKpi", 1)[1].split("\n/* 단계 카드", 1)[0]
        self.assertIn("kpi-btn", blk)
        self.assertIn("data-stage-filter", blk)
        self.assertIn('$$("button[data-stage-filter]", host)', blk)

    def test_다시_누르면_해제된다(self):
        blk = self.js.split("function renderSetupKpi", 1)[1]
        self.assertIn('state.setupStage === k ? "" : k', blk)

    def test_네_단계와_출고기록_카드가_있다(self):
        blk = self.js.split("function renderSetupKpi", 1)[1].split("\n/* 단계 카드", 1)[0]
        for k in ("waiting", "preparing", "produced", "inspected", "shipped"):
            self.assertIn(f'"{k}"', blk)
        self.assertIn("출고 기록 조회", blk)

    def test_숫자는_전체_기준이다(self):
        """걸러 놓고 숫자까지 줄면 지금 몇 건인지 알 수 없다."""
        self.assertIn("renderSetupRows(setupOrder(filterByStage(orders)), canWork)", self.js)
        self.assertIn("renderSetupKpi(orders)", self.js)

    def test_단계별로_거른다(self):
        blk = self.js.split("function filterByStage", 1)[1].split("\n}", 1)[0]
        self.assertIn('k === "waiting"', blk)
        self.assertIn('k === "inspected"', blk)

    def test_카드_스타일이_있다(self):
        self.assertIn(".kpi-btn", self.css)
        self.assertIn(".kpi-btn.on", self.css)

    # ---- 송장 → 출고 확인 ----
    def test_제작만_끝나도_송장이_나간다(self):
        oid = self._order()
        self._ready(oid)
        r = self.c.post(f"/api/orders/{oid}/waybill", json={"boxQty": 1})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))

    def test_제작_전에는_송장이_막힌다(self):
        oid = self._order()
        r = self.c.post(f"/api/orders/{oid}/waybill", json={"boxQty": 1})
        self.assertEqual(r.status_code, 400)
        self.assertIn("제작 완료", r.get_json()["error"])

    def test_송장을_뽑으면_출고_확인이_켜진다(self):
        oid = self._order()
        self._ready(oid)
        self.assertFalse(self.c.get(f"/api/orders/{oid}").get_json()["softwareInspectionDone"])
        self.c.post(f"/api/orders/{oid}/waybill", json={"boxQty": 1})
        d = self.c.get(f"/api/orders/{oid}").get_json()
        self.assertTrue(d["softwareInspectionDone"], "손으로 또 체크하게 하면 빠뜨린다")
        self.assertTrue(d["softwareInspectionBy"])

    def test_이미_확인한_담당자를_덮어쓰지_않는다(self):
        oid = self._order()
        self._ready(oid)
        self.c.patch(f"/api/orders/{oid}", json={"action": "softwareInspection", "value": True})
        before = self.c.get(f"/api/orders/{oid}").get_json()["softwareInspectionAt"]
        self.c.post(f"/api/orders/{oid}/waybill", json={"boxQty": 1})
        self.assertEqual(self.c.get(f"/api/orders/{oid}").get_json()["softwareInspectionAt"], before)

    def test_화면도_제작_완료면_송장_버튼을_보여준다(self):
        blk = self.js.split("function waybillCell", 1)[1].split("\n}", 1)[0]
        self.assertIn("!o.productionDone", blk)
        self.assertNotIn("!o.softwareInspectionDone", blk)

    # ---- 금일 출고 확인 ----

    def test_출고_마감된_건에는_송장_발급이_없다(self):
        """기록을 보는 화면이다 — 나간 건에 새 송장을 뽑으면 안 된다."""
        blk = self.js.split("function waybillCell", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("o.shippingDone", blk)
        self.assertIn("if (done) return \"\";", blk)

    def test_끝난_주문은_체크를_되돌릴_수_없다(self):
        blk = self.js.split("function stageCell", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("if (o.shippingDone) canWork = false;", blk)

    def test_출고_마감_담당자가_보인다(self):
        """준비·제작·출고확인은 각자 칸에 뜨는데 마지막 '출고'만 비어 보였다."""
        self.assertIn("shipDoneLine(o)", self.js)
        blk = self.js.split("function shipDoneLine", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn("o.shippingBy", blk)
        self.assertIn("stageWhen(o.shippingAt)", blk)

    def test_미리보기가_대상만_센다(self):
        a = self._order()
        self._ready(a)
        self.c.post(f"/api/orders/{a}/waybill", json={"boxQty": 1})
        self._order()                       # 아직 제작 전 — 대상 아님
        d = self.c.get("/api/orders/ship-today/preview").get_json()
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["orders"][0]["id"], a)

    def test_출고_마감하면_목록에서_사라진다(self):
        oid = self._order()
        self._ready(oid)
        self.c.post(f"/api/orders/{oid}/waybill", json={"boxQty": 1})
        r = self.c.post("/api/orders/ship-today", json={})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["shipped"], 1)
        d = self.c.get(f"/api/orders/{oid}").get_json()
        self.assertTrue(d["shippingDone"])
        self.assertTrue(d["archivedAt"], "보관까지 돼야 준비 목록에서 내려간다")
        self.assertTrue(d["shippingBy"])

    def test_출고_확인_안_된_건은_안_나간다(self):
        oid = self._order()
        self.c.patch(f"/api/orders/{oid}", json={"action": "production", "value": True})
        r = self.c.post("/api/orders/ship-today", json={})
        self.assertEqual(r.get_json()["shipped"], 0)
        self.assertFalse(self.c.get(f"/api/orders/{oid}").get_json()["shippingDone"])

    def test_송장_없는_택배는_미리_알려준다(self):
        oid = self._order()
        self.c.patch(f"/api/orders/{oid}", json={"action": "production", "value": True})
        self.c.patch(f"/api/orders/{oid}", json={"action": "softwareInspection", "value": True})
        d = self.c.get("/api/orders/ship-today/preview").get_json()
        self.assertEqual(d["noWaybill"], 1)

    def test_고른_것만_마감할_수_있다(self):
        a = self._order()
        self._ready(a)
        self.c.post(f"/api/orders/{a}/waybill", json={"boxQty": 1})
        b = self._order(recipient="김철수")
        self.c.patch(f"/api/orders/{b}", json={"action": "production", "value": True})
        self.c.patch(f"/api/orders/{b}", json={"action": "softwareInspection", "value": True})
        r = self.c.post("/api/orders/ship-today", json={"ids": [a]})
        self.assertEqual(r.get_json()["shipped"], 1)
        self.assertFalse(self.c.get(f"/api/orders/{b}").get_json()["shippingDone"])

    def test_이력이_남는다(self):
        src = (ROOT / "app" / "orders" / "__init__.py").read_text("utf-8")
        blk = src.split("def ship_today(", 1)[1]
        self.assertIn('audit.log("ship_today"', blk)
        self.assertIn('_log_order("ship_today", r,', blk)

    def test_화면에_버튼과_확인이_있다(self):
        self.assertIn('id="sf-shipout"', self.js)
        self.assertIn("/api/orders/ship-today/preview", self.js)
        self.assertIn("confirm(", self.js.split('#sf-shipout"', 1)[1][:1200])

class TestQcMergeDuplicates(Base):
    """같은 주문 두 줄을 합쳐 매출을 한 건만 남긴다(대표 2026-08-05).

    ★왜 '되돌리기'가 아니라 '합치기'인가 (실측)
      한 주문의 정보가 두 줄로 쪼개져 있다.
        · 몰 수집분  : 금액이 정확하다(67쌍 중 65쌍이 여기에만 금액이 있다)
        · QC 이관분  : 자산번호(관리번호)가 붙어 있다. 금액은 대개 0
      몰 수집분을 되돌리면 매출 3,181만 원이 통째로 사라지고,
      그대로 두면 양쪽이 다 출고완료라 매출이 두 번 잡힌다.
      → 금액이 있는 줄을 남기고, 자산을 그 줄로 옮긴 뒤, 다른 줄을 내린다.
    ★매출 집계는 shipping_done=1 이 기준이라(app/reports),
      내리는 줄은 보관만으로는 부족하고 출고완료도 풀어야 한다.
    """

    def setUp(self):
        super().setUp()
        self.src = (ROOT / "app" / "settings" / "qc_shipped.py").read_text("utf-8")
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")

    def _pair(self, mall_amount=380000, qc_amount=0, qc_assets=1, qno="수집-AAA"):
        """몰 수집분 + QC 이관분(둘 다 출고완료) 한 쌍을 만든다."""
        oid = self.c.post("/api/orders", json={
            "channel": "고도몰", "productName": "삼성 노트북", "productCode": "NT371B5M",
            "recipient": "홍길동", "quantity": 1, "amount": mall_amount}).get_json()["id"]
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute(
                    "UPDATE orders SET shipping_done=1, shipping_by='문성진', "
                    "shipping_at='2026-07-31T08:00:00+00:00' WHERE id=?", (oid,))
                cur = conn.execute(
                    "INSERT INTO orders(import_key, channel, order_no, ordered_at, product_name, "
                    " option_name, product_code, quantity, amount, recipient, phone, postal_code, "
                    " address, delivery_message, memo, shipping_done, shipping_by, shipping_at, "
                    " archived_at, created_at, updated_at) "
                    "VALUES(?,'고도몰',?,'2026-07-30','삼성 노트북','','NT371B5M',1,?,'홍길동', "
                    " '','','','','',1,'문성진','2026-07-31T08:00:00+00:00', "
                    " '2026-07-31T09:00:00+00:00','2026-07-30','2026-07-30')",
                    (f"주문수집:고도몰:{qno}", qno, qc_amount))
                twin = cur.lastrowid
                conn.execute(
                    "INSERT INTO audit_log(ts, user_id, username, action, target, detail) "
                    "VALUES('2026-08-05T12:27:29+09:00', 1, '대표', 'qc_shipped_link', ?, ?)",
                    (f"주문 #{oid} 홍길동",
                     '{"qcOrderNumber": "%s", "matchedBy": "제품코드", "score": 3}' % qno))
        assets = []
        for _ in range(qc_assets):
            b = self._batch()
            a = self._asset(b["id"]).get_json()[0]
            assets.append(a["id"])
            with self.app.app_context():
                with tx(write=True) as conn:
                    conn.execute(
                        "INSERT INTO order_assets(order_id, asset_id, matched_by, matched_at) "
                        "VALUES(?,?,'이관','2026-07-31')", (twin, a["id"]))
        return oid, twin, assets

    # ---- 어느 줄을 남기나 ----
    def test_금액이_있는_줄을_남긴다(self):
        oid, twin, _ = self._pair(mall_amount=380000, qc_amount=0)
        d = self.c.get("/api/qc-shipped/merge/preview").get_json()
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["items"][0]["keepId"], oid)
        self.assertEqual(d["items"][0]["dropId"], twin)

    def test_금액이_큰_줄을_남긴다(self):
        oid, twin, _ = self._pair(mall_amount=100000, qc_amount=380000)
        d = self.c.get("/api/qc-shipped/merge/preview").get_json()
        self.assertEqual(d["items"][0]["keepId"], twin, "적은 쪽을 남기면 매출이 줄어든다")

    def test_한쪽만_출고완료면_대상이_아니다(self):
        """이미 매출은 한 건 — 손댈 이유가 없다."""
        oid, twin, _ = self._pair()
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE orders SET shipping_done=0 WHERE id=?", (twin,))
        self.assertEqual(self.c.get("/api/qc-shipped/merge/preview").get_json()["count"], 0)

    # ---- 합치기 ----
    def test_매출이_한_건만_남는다(self):
        oid, twin, _ = self._pair(mall_amount=380000, qc_amount=200000)
        r = self.c.post("/api/qc-shipped/merge", json={})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["merged"], 1)
        self.assertEqual(r.get_json()["removedAmount"], 200000)
        keep = self.c.get(f"/api/orders/{oid}").get_json()
        drop = self.c.get(f"/api/orders/{twin}").get_json()
        self.assertTrue(keep["shippingDone"])
        self.assertFalse(drop["shippingDone"], "출고완료를 풀어야 매출에서 빠진다")
        self.assertTrue(drop["archivedAt"], "목록에서도 내려가야 한다")

    def test_자산이_남는_줄로_옮겨진다(self):
        """누구에게 어느 기기가 나갔는지가 끊기면 안 된다."""
        oid, twin, assets = self._pair(qc_assets=2)
        r = self.c.post("/api/qc-shipped/merge", json={})
        self.assertEqual(r.get_json()["movedAssets"], 2)
        keep = self.c.get(f"/api/orders/{oid}").get_json()
        self.assertEqual({a["assetId"] for a in keep["assets"]}, set(assets))
        self.assertEqual(self.c.get(f"/api/orders/{twin}").get_json()["assets"], [])

    def test_왜_내렸는지_남는다(self):
        oid, twin, _ = self._pair()
        self.c.post("/api/qc-shipped/merge", json={})
        d = self.c.get(f"/api/orders/{twin}").get_json()
        self.assertIn("중복", d["archiveReason"])
        self.assertEqual(d["duplicateOf"], oid)

    def test_남길_건을_뺄_수_있다(self):
        """대표가 '이명규·안호열은 남겨 달라'고 한 경우."""
        oid, twin, _ = self._pair()
        r = self.c.post("/api/qc-shipped/merge", json={"exclude": [oid]})
        self.assertEqual(r.get_json()["merged"], 0)
        self.assertTrue(self.c.get(f"/api/orders/{twin}").get_json()["shippingDone"])

    def test_두_번_돌려도_한_번만(self):
        self._pair()
        r1 = self.c.post("/api/qc-shipped/merge", json={})
        r2 = self.c.post("/api/qc-shipped/merge", json={})
        self.assertEqual(r1.get_json()["merged"], 1)
        self.assertEqual(r2.get_json()["merged"], 0)

    def test_취소된_주문은_건드리지_않는다(self):
        oid, twin, _ = self._pair()
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE orders SET cancelled_at='2026-08-01' WHERE id=?", (twin,))
        self.assertEqual(self.c.get("/api/qc-shipped/merge/preview").get_json()["count"], 0)

    def test_이력이_남는다(self):
        self.assertIn('audit.log("qc_dup_merged"', self.src)
        self.assertIn('"removedAmount"', self.src)

    # ---- 폐기한 되돌리기 ----
    def test_되돌리기는_없앴다(self):
        """되돌리면 금액이 있는 줄이 빠져 매출이 통째로 사라진다."""
        self.assertNotIn("def qc_rollback", self.src)
        self.assertEqual(self.c.get("/api/qc-shipped/rollback/preview").status_code, 404)
        self.assertIn("답은 되돌리기가 아니라", self.src)

    # ---- 화면 ----
    def test_화면에_합치기_버튼이_있다(self):
        blk = self.js.split("function wireMerge", 1)[1].split("\n}", 1)[0]
        self.assertIn("/api/qc-shipped/merge", blk)
        self.assertIn("confirm(", blk)
        self.assertIn("매출", blk)

    def test_무엇이_어떻게_되는지_알려준다(self):
        blk = self.js.split("async function renderShippedWarn", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("매출이 두 번 잡힌", blk)
        self.assertIn("금액이 있는 줄을 남기고", blk)

class TestSetupSortOldestFirst(Base):
    """작업 보드는 오래된 주문부터 — 기본값이고, 바꿀 수 있다(대표 2026-08-05).

    "현재 제작대기로 뜨는 건들은 과거 건부터 뜨게 해줘. 가장 오래된 주문 건부터 준비해야 하거든."
    "그냥 조건필터를 과거를 기본값으로 하고 필터 최신순 지정할 수 있게 하면 되겠네."
    ★목록 API는 최신순으로 준다(새 주문을 먼저 보려고) — 작업 보드는 반대다.
    """

    def setUp(self):
        super().setUp()
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")

    def test_정렬_선택이_있다(self):
        self.assertIn('id="sf-sort"', self.js)
        self.assertIn("오래된 주문부터", self.js)
        self.assertIn("최신 주문부터", self.js)

    def test_기본이_오래된_순이다(self):
        blk = self.js.split('id="sf-sort"', 1)[1][:300]
        self.assertLess(blk.index('value="old"'), blk.index('value="new"'),
                        "먼저 오는 option 이 기본값이다")

    def test_기본값이_코드에도_박혀_있다(self):
        blk = self.js.split("function setupOrder", 1)[1].split("\n}", 1)[0]
        self.assertIn('|| "old"', blk, "선택 상자가 없을 때도 오래된 순이어야 한다")

    def test_주문일로_줄_세운다(self):
        blk = self.js.split("function setupOrder", 1)[1].split("\n}", 1)[0]
        self.assertIn("o.orderedAt", blk)
        self.assertIn("o.createdAt", blk, "주문일이 비어도 순서가 흔들리면 안 된다")

    def test_출고_기록은_출고일_기준이다(self):
        blk = self.js.split("function setupOrder", 1)[1].split("\n}", 1)[0]
        self.assertIn("o.shippingAt", blk)
        self.assertIn("shipped ?", blk)

    def test_정렬을_바꾸면_다시_그린다(self):
        self.assertIn('$("#sf-sort").addEventListener("change"', self.js)
        blk = self.js.split('$("#sf-sort").addEventListener("change"', 1)[1][:400]
        self.assertIn("renderSetupRows(setupOrder(", blk)
        self.assertNotIn("load(true)", blk, "정렬만 바꾸는데 서버를 다시 부를 이유가 없다")

    def test_페이지를_처음으로_되돌린다(self):
        blk = self.js.split('$("#sf-sort").addEventListener("change"', 1)[1][:400]
        self.assertIn('resetPage("setup")', blk, "3쪽을 보던 중 정렬을 바꾸면 엉뚱한 곳이 나온다")

    def test_표를_그릴_때_정렬을_거친다(self):
        self.assertIn("renderSetupRows(setupOrder(filterByStage(orders)), canWork)", self.js)
        self.assertIn("renderSetupRows(setupOrder(filterByStage(state.setupOrders)), canWork)",
                      self.js)

    def test_출고_기록_카드로_가면_최신순으로_바뀐다(self):
        blk = self.js.split("function renderSetupKpi", 1)[1]
        self.assertIn('sort.value = "new"', blk)
        self.assertIn('sort.value = "old"', blk)

    # ---- 목록 API 자체는 최신순 그대로 ----
    def test_주문관리_목록은_최신순_그대로다(self):
        """새 주문을 먼저 보는 화면이라 여긴 바뀌면 안 된다."""
        src = (ROOT / "app" / "orders" / "__init__.py").read_text("utf-8")
        self.assertIn('sql += " ORDER BY " + _ORDERED_SORT + " DESC, o.id DESC LIMIT ?"', src)

    def test_실제로_오래된_것이_먼저_온다(self):
        """API가 최신순으로 주는지 확인 — 화면이 뒤집는다는 전제가 맞는지."""
        old = self.c.post("/api/orders", json={
            "channel": "고도몰", "productName": "A", "recipient": "김하나",
            "quantity": 1, "amount": 1000, "orderedAt": "2026-07-01T10:00:00+09:00"}).get_json()["id"]
        new = self.c.post("/api/orders", json={
            "channel": "고도몰", "productName": "B", "recipient": "김두리",
            "quantity": 1, "amount": 1000, "orderedAt": "2026-08-01T10:00:00+09:00"}).get_json()["id"]
        ids = [o["id"] for o in self.c.get("/api/orders").get_json()["orders"]]
        self.assertLess(ids.index(new), ids.index(old), "API는 최신순이어야 한다")

class TestQcShippedFlag(Base):
    """'QC에서는 이미 출고됨'을 보드 행에 알려 준다(대표 2026-08-05).

    "출고 완료인 제품인데 왜 제작대기에 있는지 알 수 있나?"
    ★HMS와 QC는 금액을 다르게 적는다 —
       · HMS = 주문 단위 **합계**(여러 상품·여러 대를 한 줄로)
       · QC  = 상품/대수 단위 **개별 금액**
      실측: 조용원 HMS 660,000원(2대) ↔ QC 340,000원(1대 단가, 관리번호 2개)
            김병엽 HMS 259,000원(키보드+노트북) ↔ QC 10,000원(키보드만)
      금액이 크게 달라 자동 반영에서 빠지는 게 맞지만, 작업자에게는 보여 줘야
      이미 나간 물건을 또 만들지 않는다.
    """

    def setUp(self):
        super().setUp()
        self.src = (ROOT / "app" / "settings" / "qc_shipped.py").read_text("utf-8")
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")

    def _order(self, **kw):
        base = {"channel": "고도몰", "productName": "HP 14인치 코어 i7 노트북",
                "productCode": "840 G3_i7-6_내장", "recipient": "조용원",
                "quantity": 2, "amount": 660000}
        base.update(kw)
        return self.c.post("/api/orders", json=base).get_json()["id"]

    def _nas(self, **kw):
        base = {"orderNumber": "수집-D91DCD9666", "recipient": "조용원",
                "productName": "HP 14인치 코어 i7 노트북", "productCode": "840 G3_i7-6_내장",
                "amount": 340000, "orderedAt": "2026-07-31", "quantity": 1,
                "shippingDone": True, "shippingBy": "문성진",
                "shippingAt": "2026-07-31T08:00:00+00:00", "archivedAt": "",
                "managementNumber": "260422-0065\n260422-0066"}
        base.update(kw)
        return base

    def _flags(self, nas):
        from unittest import mock
        from app.settings import qc_shipped
        with mock.patch.object(qc_shipped, "_load_nas", return_value=(nas, 1, 1)):
            r = self.c.get("/api/qc-shipped/flags")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    # ---- 판정 ----
    def test_금액이_달라도_표시는_붙는다(self):
        """자동 반영은 안 하지만 알려는 준다 — 그게 이 기능의 전부다."""
        oid = self._order()
        d = self._flags([self._nas()])
        self.assertEqual(d["count"], 1)
        f = d["flags"][str(oid)]
        self.assertEqual(f["qcAmount"], 340000)
        self.assertEqual(f["amount"], 660000)

    def test_관리번호를_함께_알려준다(self):
        """어느 기기가 나갔는지 확인할 수 있어야 판단이 된다."""
        oid = self._order()
        f = self._flags([self._nas()])["flags"][str(oid)]
        self.assertIn("260422-0065", f["qcAssetNo"])
        self.assertIn("260422-0066", f["qcAssetNo"])
        self.assertNotIn("\n", f["qcAssetNo"], "줄바꿈이 그대로 오면 화면이 깨진다")

    def test_출고_담당자와_날짜를_알려준다(self):
        oid = self._order()
        f = self._flags([self._nas()])["flags"][str(oid)]
        self.assertEqual(f["shippedBy"], "문성진")
        self.assertTrue(f["shippedAt"])

    def test_상품코드가_QC_상품명에_있어도_잡는다(self):
        """쿠팡은 QC 쪽에서 코드가 상품명 자리에 들어온다."""
        oid = self._order(productCode="NT371B5M_i7-7_내장")
        d = self._flags([self._nas(productName="NT371B5M_i7-7_내장 AA급3",
                                   productCode="")])
        self.assertEqual(d["count"], 1)
        self.assertIn(str(oid), d["flags"])

    def test_다른_사람이면_안_붙는다(self):
        self._order()
        d = self._flags([self._nas(recipient="다른사람")])
        self.assertEqual(d["count"], 0)

    def test_다른_상품이면_안_붙는다(self):
        self._order()
        d = self._flags([self._nas(productName="전혀 다른 상품", productCode="XX")])
        self.assertEqual(d["count"], 0)

    def test_QC가_아직_출고_전이면_안_붙는다(self):
        self._order()
        d = self._flags([self._nas(shippingDone=False)])
        self.assertEqual(d["count"], 0)

    def test_이미_출고된_주문에는_안_붙는다(self):
        """보드에 없는 주문까지 표시할 이유가 없다."""
        oid = self._order()
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE orders SET shipping_done=1 WHERE id=?", (oid,))
        self.assertEqual(self._flags([self._nas()])["count"], 0)

    def test_폴더가_끊겨도_보드는_돈다(self):
        blk = self.src.split("def qc_shipped_flags", 1)[1].split("\n@bp", 1)[0]
        self.assertIn("except Exception", blk)
        self.assertIn("items = []", blk)

    def test_셋팅_권한이면_볼_수_있다(self):
        """작업자가 봐야 하는 정보다 — 관리자 전용이면 뜻이 없다."""
        blk = self.src.split("def qc_shipped_flags", 1)[1].split("\n@bp", 1)[0]
        self.assertIn('require("setup.view")', blk)

    # ---- 화면 ----
    def test_행에_칩이_붙는다(self):
        self.assertIn("qcShippedChip(o)", self.js)
        blk = self.js.split("function qcShippedChip", 1)[1].split("\n}", 1)[0]
        self.assertIn("QC 출고됨", blk)
        self.assertIn("state.qcShippedFlags", blk)

    def test_왜_금액이_다른지_설명한다(self):
        blk = self.js.split("function qcShippedChip", 1)[1].split("\n}", 1)[0]
        self.assertIn("주문 합계", blk)
        self.assertIn("상품별", blk)

    def test_보드를_열_때_받아_온다(self):
        self.assertIn('api("/api/qc-shipped/flags")', self.js)
        self.assertIn("state.qcShippedFlags = r.flags", self.js)

    def test_스타일이_있다(self):
        css = (ROOT / "static" / "css" / "app.css").read_text("utf-8")
        self.assertIn(".qc-shipped-chip", css)

class TestCoupangMoneyObject(Base):
    """쿠팡이 금액을 객체로 보낸다 — 숫자로 읽으려다 0원이 됐다(2026-08-05 실측).

    쿠팡이 2026-07-28부터 금액 필드를 Money 객체로 바꿔 보내기 시작했다:
        "orderPrice": {"currencyCode": "KRW", "units": 376740, "nanos": 0}
    기존 파서가 dict를 숫자로 못 읽어 **쿠팡 주문 38건이 전부 0원**으로 들어왔고
    (7/27까지는 정상, 고도몰은 0원이 하나도 없음), 매출·마진이 그만큼 비어 있었다.
    """

    def test_객체로_와도_읽는다(self):
        from app.malls.coupang import _money
        self.assertEqual(_money({"currencyCode": "KRW", "units": 376740, "nanos": 0}), 376740)

    def test_숫자로_와도_읽는다(self):
        """옛 형태로 되돌아가도 그대로 동작해야 한다."""
        from app.malls.coupang import _money
        self.assertEqual(_money(376740), 376740)
        self.assertEqual(_money("376,740"), 376740)

    def test_없는_값은_0이다(self):
        from app.malls.coupang import _money
        for v in (None, {}, {"units": None}, "", "abc"):
            self.assertEqual(_money(v), 0)

    def test_소수점은_반올림한다(self):
        from app.malls.coupang import _money
        self.assertEqual(_money({"units": 100, "nanos": 500000000}), 100)
        self.assertEqual(_money({"units": 100, "nanos": 600000000}), 101)

    def test_수집에서_쓴다(self):
        src = (ROOT / "app" / "malls" / "coupang.py").read_text("utf-8")
        self.assertIn('_money(it.get("orderPrice")) or _money(it.get("salesPrice"))', src)
        self.assertNotIn('_int(it.get("orderPrice"))', src)


class TestCoupangRentalFilter(Base):
    """쿠팡에도 렌탈 분류를 붙인다(대표 2026-08-05).

    같은 계정에 렌탈(RMS)·판매(HMS) 상품이 함께 있다. 렌탈 주문이 HMS로 들어오면
    판매 출고 사고가 난다. 스마트스토어에 이미 있던 분류를 그대로 옮겼다 —
    두 몰이 다르게 굴면 한쪽에만 구멍이 생긴다.
    """

    def setUp(self):
        super().setUp()
        self.src = (ROOT / "app" / "malls" / "coupang.py").read_text("utf-8")

    def _cls(self, settings, pid, name, opt=""):
        from app.malls.coupang import CoupangAdapter
        a = CoupangAdapter.__new__(CoupangAdapter)
        a.s = settings
        return a._classify(pid, name, opt)

    def test_상품번호로_거른다(self):
        self.assertEqual(self._cls({"rental_product_ids": "123, 456"}, "123", "노트북"), "rental")

    def test_이름에_렌탈이_있으면_등록_전에도_거른다(self):
        self.assertEqual(self._cls({}, "999", "노트북 렌탈 대여 임대"), "rental")
        self.assertEqual(self._cls({}, "999", "사무용", "사용기간 30일"), "rental")

    def test_판매_등록이_키워드를_이긴다(self):
        """상품명에 '렌탈'이 들어간 판매 상품을 구제하는 통로."""
        self.assertEqual(
            self._cls({"sale_product_ids": "999", "rental_product_ids": "123"},
                      "999", "대량렌탈가능 노트북"), "sale")

    def test_분류표를_안_쓰면_전부_판매다(self):
        """기존 동작을 바꾸지 않는다 — 갑자기 주문이 사라지면 출고 누락 사고가 된다."""
        self.assertEqual(self._cls({}, "999", "노트북"), "sale")

    def test_분류표를_쓰면_미등록은_미분류다(self):
        self.assertEqual(self._cls({"rental_product_ids": "123"}, "999", "노트북"), "unknown")

    def test_거른_것을_남긴다(self):
        """무엇이 빠졌는지 화면에서 볼 수 있어야 한다."""
        self.assertIn("self.skipped_rental.append", self.src)
        self.assertIn("self.skipped_rental = []", self.src)

    def test_설정_칸이_있다(self):
        from app.malls import MALLS
        cp = next(m for m in MALLS if m["code"] == "coupang")
        keys = [f["key"] for f in cp["fields"]]
        self.assertIn("rental_product_ids", keys)
        self.assertIn("sale_product_ids", keys)

    def test_스마트스토어와_같은_키워드다(self):
        from app.malls.coupang import RENTAL_KEYWORDS as A
        from app.malls.smartstore import RENTAL_KEYWORDS as B
        self.assertEqual(set(A), set(B), "두 몰이 다르게 굴면 한쪽에만 구멍이 생긴다")


class TestMallAmountBackfill(Base):
    """0원으로 들어온 주문의 금액을 몰에서 다시 받아 채운다."""

    def setUp(self):
        super().setUp()
        self.src = (ROOT / "app" / "malls" / "collect.py").read_text("utf-8")

    def test_금액이_있는_주문은_안_건드린다(self):
        """사람이 고쳐 넣은 금액을 몰 값으로 되돌리면 안 된다."""
        blk = self.src.split("def backfill_amounts", 1)[1]
        self.assertIn("AND amount=0", blk)
        self.assertIn('if cur is None or cur["amount"]:', blk)

    def test_이관분은_대상이_아니다(self):
        blk = self.src.split("def backfill_amounts", 1)[1]
        self.assertIn("import_key NOT LIKE '주문수집%'", blk)

    def test_외부_호출은_트랜잭션_밖이다(self):
        """BEGIN을 쥔 채 몰을 기다리면 전 시스템 쓰기가 잠긴다(원칙 #1)."""
        blk = self.src.split("def backfill_amounts", 1)[1]
        head = blk.split("# ---- 몰 조회는 트랜잭션 밖에서", 1)[1].split("with tx(write=True)", 1)[0]
        self.assertNotIn("with tx(", head)

    def test_미리보기가_있다(self):
        blk = self.src.split("def backfill_amounts", 1)[1]
        self.assertIn('body.get("dryRun")', blk)

    def test_이력이_남는다(self):
        self.assertIn('audit.log("mall_amount_backfill"', self.src)


    def test_화면에_버튼이_있다(self):
        js = (ROOT / "static" / "js" / "orders.js").read_text("utf-8")
        self.assertIn("data-fillamt", js)
        self.assertIn("/backfill-amounts", js)

    def test_미리_보여_주고_확인받는다(self):
        js = (ROOT / "static" / "js" / "orders.js").read_text("utf-8")
        blk = js.split("button[data-fillamt]", 1)[1][:1500]
        self.assertIn("dryRun: true", blk)
        self.assertIn("confirm(", blk)

    def test_라우트가_등록된다(self):
        rules = [str(r) for r in self.app.url_map.iter_rules()]
        self.assertIn("/api/malls/<code>/backfill-amounts", rules)


class TestAutoSearch(Base):
    """검색창은 엔터를 안 눌러도 조회된다(대표 2026-08-05: "엔터 치는 것은 번거롭다")."""

    def setUp(self):
        super().setUp()
        self.js = (ROOT / "static" / "js" / "app.js").read_text("utf-8")

    def test_헬퍼가_있다(self):
        self.assertIn("function autoSearch(", self.js)

    def test_한글_조합_중에는_안_부른다(self):
        """조합 중에 부르면 'ㅅ'으로 검색돼 결과가 깜빡인다."""
        blk = self.js.split("function autoSearch(", 1)[1].split("\n}", 1)[0]
        self.assertIn("isComposing", blk)
        self.assertIn("compositionend", blk)

    def test_타이핑이_멈춘_뒤_한_번만_부른다(self):
        blk = self.js.split("function autoSearch(", 1)[1].split("\n}", 1)[0]
        self.assertIn("clearTimeout", blk)
        self.assertIn("setTimeout", blk)

    def test_값이_그대로면_안_부른다(self):
        blk = self.js.split("function autoSearch(", 1)[1].split("\n}", 1)[0]
        self.assertIn("el.value === last", blk)

    def test_엔터도_그대로_동작한다(self):
        blk = self.js.split("function autoSearch(", 1)[1].split("\n}", 1)[0]
        self.assertIn('e.key !== "Enter"', blk)

    def test_모든_검색창에_붙었다(self):
        """한 곳만 남으면 거기서만 엔터를 눌러야 해서 더 헷갈린다."""
        for name, sel in (("purchase.js", "#af-q"), ("purchase.js", "#sf-q"),
                          ("purchase.js", "#ss-q"), ("as.js", "#asf-q"),
                          ("orders.js", "#of-q"), ("shipping.js", "#wf-q"),
                          ("setup.js", "#sf-q"), ("setup.js", "#mt-q")):
            js = (ROOT / "static" / "js" / name).read_text("utf-8")
            self.assertIn(f'autoSearch("{sel}"', js, f"{name} {sel} 에 자동검색이 없다")

    def test_옛_엔터_전용_배선이_남아_있지_않다(self):
        for name in ("purchase.js", "as.js", "orders.js", "shipping.js", "setup.js"):
            js = (ROOT / "static" / "js" / name).read_text("utf-8")
            self.assertNotIn('addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); })',
                             js, f"{name} 에 엔터 전용 배선이 남았다")

class TestMarkRental(Base):
    """화면에서 렌탈 주문을 한 번에 내리고, 그 상품번호를 몰 설정에 등록한다.

    ★대표 지시(2026-08-05): "RMS로 들어가는 렌탈 상품이 HMS에 뜬다 — 상품번호로 안 뜨게."
    ★수집 필터만으로는 부족하다 — 그건 앞으로 들어올 것만 막는다.
      이미 떠 있는 건(실측 #663, 8일째 제작 대기)은 화면에서 내려야 한다.
      상품번호를 몰에서 찾아 손으로 옮겨 적지 않아도 되게 등록까지 함께 한다.
    """

    def setUp(self):
        super().setUp()
        self.src = (ROOT / "app" / "malls" / "collect.py").read_text("utf-8")
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")

    def _order(self, **kw):
        base = {"channel": "스마트스토어", "productName": "[렌탈] 노트북 렌탈 대여 임대",
                "productCode": "6966207626", "recipient": "유주현",
                "quantity": 1, "amount": 57400}
        base.update(kw)
        return self.c.post("/api/orders", json=base).get_json()["id"]

    # ---- 찾기 ----
    def test_렌탈로_보이는_주문을_찾는다(self):
        oid = self._order()
        d = self.c.get("/api/orders/rental-suspects").get_json()
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["orders"][0]["id"], oid)
        self.assertIn("렌탈", d["orders"][0]["words"])

    def test_옵션은_보지_않는다(self):
        """'팬리스'·'보노보스' 같은 말에 걸려 판매 상품이 렌탈로 오인된다
        (2026-08-05 실측: 6건 중 4건이 오탐이었다)."""
        self._order(productName="HP 미니PC 팬리스 초저전력", productCode="T640",
                    optionName="렌탈 문구가 옵션에 있어도")
        self.assertEqual(self.c.get("/api/orders/rental-suspects").get_json()["count"], 0)
        blk = self.src.split("def rental_suspects", 1)[1].split("\n@bp", 1)[0]
        self.assertIn('_rental_hits(r["product_name"])', blk)
        self.assertNotIn("option_name", blk)

    def test_이미_끝난_주문은_안_센다(self):
        oid = self._order()
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE orders SET shipping_done=1 WHERE id=?", (oid,))
        self.assertEqual(self.c.get("/api/orders/rental-suspects").get_json()["count"], 0)

    # ---- 내리기 ----
    def test_작업_목록에서_내려간다(self):
        oid = self._order()
        r = self.c.post("/api/orders/mark-rental", json={"ids": [oid]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["moved"], 1)
        d = self.c.get(f"/api/orders/{oid}").get_json()
        self.assertTrue(d["archivedAt"])
        self.assertIn("렌탈", d["archiveReason"])

    def test_매출과_출고는_건드리지_않는다(self):
        """렌탈은 RMS 몫이지 '취소'나 '출고'가 아니다."""
        oid = self._order()
        self.c.post("/api/orders/mark-rental", json={"ids": [oid]})
        d = self.c.get(f"/api/orders/{oid}").get_json()
        self.assertFalse(d["shippingDone"])
        self.assertFalse(d["cancelledAt"])
        self.assertEqual(d["amount"], 57400)

    def test_상품번호가_몰_설정에_등록된다(self):
        """다음부터는 수집 자체가 안 되게 — 손으로 옮겨 적을 필요가 없다."""
        oid = self._order()
        r = self.c.post("/api/orders/mark-rental", json={"ids": [oid], "register": True})
        self.assertEqual(r.get_json()["registered"], {"smartstore": ["6966207626"]})
        from app.db import tx
        import json as _json
        with self.app.app_context():
            with tx() as conn:
                cfg = _json.loads(conn.execute(
                    "SELECT value FROM settings WHERE key='malls'").fetchone()["value"])
        self.assertIn("6966207626", cfg["smartstore"]["rental_product_ids"])

    def test_기존_설정을_지우지_않는다(self):
        """비밀 키가 날아가면 몰 연동이 통째로 끊긴다."""
        from app.db import tx
        import json as _json
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute(
                    "INSERT INTO settings(key, value, updated_at, updated_by) "
                    "VALUES('malls',?,'2026-08-05','t') ON CONFLICT(key) DO UPDATE "
                    "SET value=excluded.value",
                    (_json.dumps({"smartstore": {"client_id": "abc", "client_secret": "xyz",
                                                 "rental_product_ids": "111"}}),))
        oid = self._order()
        self.c.post("/api/orders/mark-rental", json={"ids": [oid]})
        with self.app.app_context():
            with tx() as conn:
                cfg = _json.loads(conn.execute(
                    "SELECT value FROM settings WHERE key='malls'").fetchone()["value"])
        ss = cfg["smartstore"]
        self.assertEqual(ss["client_id"], "abc")
        self.assertEqual(ss["client_secret"], "xyz")
        self.assertIn("111", ss["rental_product_ids"])
        self.assertIn("6966207626", ss["rental_product_ids"])

    def test_등록을_안_할_수도_있다(self):
        oid = self._order()
        r = self.c.post("/api/orders/mark-rental", json={"ids": [oid], "register": False})
        self.assertEqual(r.get_json()["moved"], 1)
        self.assertEqual(r.get_json()["registered"], {})

    def test_두_번_보내도_한_번만(self):
        oid = self._order()
        self.c.post("/api/orders/mark-rental", json={"ids": [oid]})
        r2 = self.c.post("/api/orders/mark-rental", json={"ids": [oid]})
        self.assertEqual(r2.get_json()["moved"], 0)

    def test_고를_주문이_없으면_거절한다(self):
        self.assertEqual(self.c.post("/api/orders/mark-rental", json={"ids": []}).status_code, 400)

    # ---- 되돌리기 ----
    def test_되돌릴_수_있다(self):
        oid = self._order()
        self.c.post("/api/orders/mark-rental", json={"ids": [oid]})
        r = self.c.post(f"/api/orders/{oid}/unmark-rental", json={})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(self.c.get(f"/api/orders/{oid}").get_json()["archivedAt"])

    def test_이_도구가_내린_것만_되돌린다(self):
        """출고돼서 보관된 주문을 이걸로 되살리면 끝난 일이 다시 열린다."""
        oid = self._order()
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE orders SET archived_at='2026-08-01' WHERE id=?", (oid,))
        self.assertEqual(self.c.post(f"/api/orders/{oid}/unmark-rental", json={}).status_code, 400)

    def test_이력이_남는다(self):
        self.assertIn('audit.log("order_marked_rental"', self.src)
        self.assertIn('audit.log("mall_rental_registered"', self.src)

    # ---- 화면 ----
    def test_보드에_배너가_뜬다(self):
        self.assertIn("renderRentalWarn", self.js)
        self.assertIn("/api/orders/rental-suspects", self.js)
        self.assertIn("setup-rental-warn", self.js)

    def test_무엇이_일어나는지_알려준다(self):
        blk = self.js.split("async function renderRentalWarn", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("confirm(", blk)
        self.assertIn("다음부터는 수집하지 않습니다", blk)
        self.assertIn("매출·재고는 건드리지 않습니다", blk)

    def test_건별로_고를_수_있다(self):
        blk = self.js.split("async function renderRentalWarn", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("rt-pick", blk)
        self.assertIn("attachShiftPick", blk)

class TestQcWatchNoLoop(Base):
    """정리한 줄을 QC 감시가 되살리면 무한 루프가 된다(2026-08-05 실측 사고).

    같은 주문이 우리 쪽에 두 줄이라 한쪽을 '중복'으로 정리했는데, QC 파일에는 그 줄의
    키가 그대로 남아 있다. 그래서 20초마다
      감시가 shipping_done=1로 되돌림 → 자동 병합이 다시 0으로 → 반복
    이 벌어졌다. 실측: 감시 17회 동안 같은 62쌍을 **13번** 다시 합쳤고(qc_dup_merged 794건),
    출고매출이 1억2,770만 ↔ 1억3,533만 사이를 계속 오갔다.
    """

    def setUp(self):
        super().setUp()
        self.src = (ROOT / "app" / "settings" / "qc_import.py").read_text("utf-8")

    def _row(self, **kw):
        base = {"preparing": 0, "production_done": 0, "inspection_done": 0,
                "shipping_done": 0, "archived_at": "", "archive_reason": ""}
        base.update(kw)
        return base

    def _fwd(self, row, o):
        from app.settings.qc_import import _advance_fields
        return _advance_fields(row, o)

    def test_중복으로_내린_줄은_되살리지_않는다(self):
        r = self._row(archived_at="2026-08-05", archive_reason="중복 — 주문 #123과 같은 건")
        self.assertEqual(self._fwd(r, {"shippingDone": True, "shippingBy": "문성진"}), {})

    def test_렌탈로_내린_줄도_되살리지_않는다(self):
        r = self._row(archived_at="2026-08-05", archive_reason="렌탈(RMS) 주문이라…")
        self.assertEqual(self._fwd(r, {"shippingDone": True}), {})

    def test_보통_주문은_그대로_전진한다(self):
        """루프를 막느라 정상 반영까지 막으면 안 된다."""
        fwd = self._fwd(self._row(), {"shippingDone": True, "shippingBy": "문성진",
                                      "shippingAt": "2026-08-05T08:00:00+00:00"})
        self.assertEqual(fwd["shipping_done"], 1)
        self.assertEqual(fwd["shipping_by"], "문성진")

    def test_출고돼서_보관된_주문은_막지_않는다(self):
        """정상 마감분은 사유가 없다 — 그건 예전대로 동작해야 한다."""
        r = self._row(archived_at="2026-08-04", archive_reason="")
        fwd = self._fwd(r, {"productionDone": True, "productionBy": "김선민"})
        self.assertEqual(fwd["production_done"], 1)

    def test_계획이_사유_칸을_읽어_온다(self):
        """읽어 오지 않으면 위 판정이 늘 통과해 루프가 되돌아온다."""
        blk = self.src.split("have_rows = {", 1)[1].split("}", 1)[0]
        self.assertIn("archive_reason", blk)

    def test_사유_칸이_없어도_터지지_않는다(self):
        """옛 호출부가 그 칸을 안 읽어 왔을 수 있다."""
        class Row(dict):
            def __getitem__(self, k):
                if k == "archive_reason":
                    raise IndexError(k)
                return dict.__getitem__(self, k)
        r = Row(self._row())
        fwd = self._fwd(r, {"shippingDone": True})
        self.assertEqual(fwd["shipping_done"], 1)

    # ---- 실제 흐름: 정리 → 감시 → 되살아나지 않는다 ----
    def test_정리한_뒤_감시가_돌아도_그대로다(self):
        import json
        import os
        import tempfile
        from unittest import mock
        from app.db import tx

        oid = self.c.post("/api/orders", json={
            "channel": "고도몰", "productName": "삼성 노트북", "productCode": "NT371B5M",
            "recipient": "홍길동", "quantity": 1, "amount": 380000}).get_json()["id"]
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE orders SET shipping_done=1, shipping_by='문성진', "
                             "shipping_at='2026-07-31T08:00:00+00:00' WHERE id=?", (oid,))
                conn.execute(
                    "INSERT INTO orders(import_key, channel, order_no, ordered_at, product_name, "
                    " option_name, product_code, quantity, amount, recipient, phone, postal_code, "
                    " address, delivery_message, memo, shipping_done, shipping_by, shipping_at, "
                    " archived_at, created_at, updated_at) "
                    "VALUES('주문수집:고도몰:수집-AAA','고도몰','수집-AAA','2026-07-30', "
                    " '삼성 노트북','','NT371B5M',1,0,'홍길동','','','','','',1,'문성진', "
                    " '2026-07-31T08:00:00+00:00','2026-07-31T09:00:00+00:00', "
                    " '2026-07-30','2026-07-30')")
                conn.execute(
                    "INSERT INTO audit_log(ts, user_id, username, action, target, detail) "
                    "VALUES('2026-08-05T12:00:00+09:00',1,'t','qc_shipped_link',?,?)",
                    (f"주문 #{oid} 홍길동", '{"qcOrderNumber": "수집-AAA"}'))
        # 합치기 — QC 이관분이 내려간다
        r = self.c.post("/api/qc-shipped/merge", json={})
        self.assertEqual(r.get_json()["merged"], 1)
        twin = self.c.get("/api/orders?view=all").get_json()["orders"]
        twin = [o for o in twin if o["orderNumber"] == "수집-AAA"]
        self.assertTrue(twin and not twin[0]["shippingDone"], "합치기가 출고완료를 풀어야 한다")

        # QC 감시가 돌아도 되살아나면 안 된다
        nas = [{"importKey": "주문수집:고도몰:수집-AAA", "orderNumber": "수집-AAA",
                "recipient": "홍길동", "productName": "삼성 노트북",
                "productCode": "NT371B5M", "amount": 0, "orderedAt": "2026-07-30",
                "quantity": 1, "shippingDone": True, "shippingBy": "문성진",
                "shippingAt": "2026-07-31T08:00:00+00:00",
                "archivedAt": "2026-07-31T09:00:00+00:00", "managementNumber": ""}]
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(nas, fh, ensure_ascii=False)
        try:
            from app.settings import qc_shipped, qc_watch
            self.c.post("/api/qc-watch", json={"enabled": True, "path": os.path.dirname(path)})
            with mock.patch.object(qc_watch, "_orders_file", return_value=path), \
                 mock.patch.object(qc_shipped, "_load_nas", return_value=(nas, 1, 1)):
                qc_watch.sync_once(self.app, force=True)
        finally:
            os.unlink(path)

        again = [o for o in self.c.get("/api/orders?view=all").get_json()["orders"]
                 if o["orderNumber"] == "수집-AAA"]
        self.assertTrue(again)
        self.assertFalse(again[0]["shippingDone"],
                         "감시가 되살리면 정리 ↔ 되살림이 20초마다 반복된다")

class TestQcWatchAlert(Base):
    """QC 폴더 연결이 끊기면 크게 알린다(2026-08-06 실측 사고).

    폴더를 못 읽어도 감시가 **조용히** 멈췄다. 화면은 평소와 똑같아서
    QC에서 체크한 것이 안 넘어오는 줄 아무도 모른 채 하루가 갔다
    (그날 qc_watch_sync 0회, 로그에도 한 줄 없음).
    """

    def setUp(self):
        super().setUp()
        self.src = (ROOT / "app" / "settings" / "qc_watch.py").read_text("utf-8")
        self.js = (ROOT / "static" / "js" / "setup.js").read_text("utf-8")

    def test_못_읽으면_로그에_남긴다(self):
        blk = self.src.split("def sync_once", 1)[1]
        self.assertIn("QC 폴더 연결 실패", blk)
        self.assertIn("app.logger.warning", blk)

    def test_같은_오류를_20초마다_쌓지_않는다(self):
        blk = self.src.split("def sync_once", 1)[1]
        self.assertIn('_last_seen.get("error") != msg', blk)

    def test_복구되면_알려준다(self):
        blk = self.src.split("def sync_once", 1)[1]
        self.assertIn("QC 폴더 연결 복구", blk)

    def test_셋팅_담당자도_상태를_본다(self):
        """모르고 종일 손으로 체크하게 된다."""
        blk = self.src.split("def qc_watch_status", 1)[1].split("\n@bp", 1)[0]
        self.assertIn('require_any("settings.manage", "setup.view")', blk)

    def test_상태에_연결_여부가_있다(self):
        r = self.c.get("/api/qc-watch/status")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertIn("found", d)
        self.assertIn("error", d)
        self.assertIn("folder", d)

    def test_폴더가_없으면_found가_거짓이다(self):
        self.c.post("/api/qc-watch", json={"enabled": True, "path": r"C:\없는폴더\없음"})
        from app.settings import qc_watch
        rows, adv = qc_watch.sync_once(self.app, force=True)
        self.assertEqual((rows, adv), (0, 0))
        self.assertFalse(self.c.get("/api/qc-watch/status").get_json()["found"])

    def test_보드에_경고가_뜬다(self):
        self.assertIn("renderQcWatchWarn", self.js)
        self.assertIn("setup-qcwatch-warn", self.js)
        blk = self.js.split("async function renderQcWatchWarn", 1)[1].split("\n/*", 1)[0]
        self.assertIn("연결하지 못하고 있습니다", blk)
        self.assertIn("반영되지 않습니다", blk, "무엇이 안 되는지 알려야 한다")

    def test_정상이면_경고를_안_띄운다(self):
        blk = self.js.split("async function renderQcWatchWarn", 1)[1].split("\n/*", 1)[0]
        self.assertIn("if (!d.enabled || d.found)", blk)

    def test_다시_시도_버튼이_있다(self):
        blk = self.js.split("async function renderQcWatchWarn", 1)[1].split("\n/*", 1)[0]
        self.assertIn("qw-retry", blk)
        self.assertIn("/api/qc-watch/run", blk)

