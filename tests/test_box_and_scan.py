"""박스 수량 · 자산 스캔 안내 — 2026-07-30 대표 지적 2건에 대한 회귀 방지.

  ① "2박스 이상 나가는 경우도 있어"
     → 한 송장으로 여러 상자가 나가면 종이에 그 사실이 찍혀야 한다.
       CJ 라벨 좌표는 동결이라 상품명 칸(데이터)에 실어 보낸다.

  ② "자산번호를 적었는데 매칭이 안 된다"
     → 번호는 맞는데 이미 출고됐거나 다른 주문에 잡힌 경우가 있다.
       매칭 가능한 것만 돌려주면 담당자는 '번호를 잘못 쳤나' 하고 같은 번호를 계속 다시 찍는다.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth as auth_mod  # noqa: E402
from app import create_app  # noqa: E402
from app.orders.waybill import _compose_items  # noqa: E402

ADMIN_PW = "admin-pass-1"


def _row(**kw):
    base = {"channel": "고도몰", "product_code": "L480_i5-8_내장",
            "product_name": "레노버 씽크패드 L480", "option_name": "",
            "quantity": 1, "is_review": 0, "delivery_message": ""}
    base.update(kw)
    return base


class TestBoxQtyOnLabel(unittest.TestCase):
    """박스 수는 '데이터'로만 싣는다 — waybill_pdf 레이아웃은 절대 건드리지 않는다."""

    def test_한_박스면_아무것도_안_붙는다(self):
        items = _compose_items(_row(), ["260628-001"], simulated=False, box_qty=1)
        self.assertNotIn("박스", items[0]["name"])

    def test_두_박스_이상이면_상품명_칸에_찍힌다(self):
        items = _compose_items(_row(), ["260628-001"], simulated=False, box_qty=2)
        self.assertIn("[박스 2개]", items[0]["name"])

    def test_박스수가_비었거나_이상해도_터지지_않는다(self):
        for bad in (None, "", "abc", 0, -3):
            items = _compose_items(_row(), ["260628-001"], simulated=False, box_qty=bad)
            self.assertNotIn("박스", items[0]["name"], f"box_qty={bad!r}")

    def test_테스트발행_다중건_리뷰어와_함께_붙어도_순서가_유지된다(self):
        items = _compose_items(_row(is_review=1), ["260628-001"], simulated=True,
                               seq=(2, 6), box_qty=3)
        self.assertTrue(items[0]["name"].startswith("[테스트발행] [박스 3개] [2/6] [리뷰어] "),
                        items[0]["name"])

    def test_박스표시가_붙어도_라벨_길이를_넘지_않는다(self):
        items = _compose_items(_row(product_name="가" * 200), ["260628-001"] * 10,
                               simulated=True, seq=(3, 9), box_qty=10)
        joined = " / ".join(i["name"] for i in items)
        self.assertLessEqual(len(joined), 120, joined)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-box-"))
        self.app = create_app(db_path=self.tmp / "test.db")
        self.app.testing = True
        self.client = self.app.test_client()
        auth_mod._login_failures.clear()
        self.client.post("/api/auth/setup", json={
            "username": "admin", "displayName": "대표", "password": ADMIN_PW})
        self.cats = self.client.get("/api/categories").get_json()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _asset(self, model="L480"):
        return self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": model,
            "purchasePrice": 200000, "qty": 1}).get_json()[0]


class TestAssetScanReason(Base):
    def test_판매_가능한_자산은_사유가_비어_있다(self):
        a = self._asset()
        rows = self.client.get(f"/api/orders/asset-search?q={a['assetNo']}").get_json()
        hit = next(x for x in rows if x["assetNo"] == a["assetNo"])
        self.assertTrue(hit["available"])
        self.assertEqual(hit["reason"], "")

    def test_이미_출고된_자산도_찾아_주되_이유를_알려_준다(self):
        """예전에는 아무것도 안 나와서 '없는 번호'로 보였다 — 그래서 계속 다시 찍게 됐다."""
        # ★'중복 매칭 허용'(2026-08-31, 기본 켜짐)을 끄고 차단 모드의 안전핀을 검증한다
        self.assertEqual(self.client.put("/api/settings", json={
            "order_asset_duplicate": {"enabled": False}}).status_code, 200)
        a = self._asset()
        o = self.client.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북", "phone": "010-1111-2222",
            "address": "서울시 강남구 1", "postalCode": "06000", "amount": 500000}).get_json()
        for act, val in (("assets", None), ("production", True),
                         ("softwareInspection", True), ("shipping", True)):
            body = {"action": "assets", "assetIds": [a["id"]]} if val is None \
                else {"action": act, "value": val}
            self.client.patch(f"/api/orders/{o['id']}", json=body)

        rows = self.client.get(f"/api/orders/asset-search?q={a['assetNo']}").get_json()
        hit = next((x for x in rows if x["assetNo"] == a["assetNo"]), None)
        self.assertIsNotNone(hit, "출고된 자산이 검색에서 아예 사라지면 안 된다")
        self.assertFalse(hit["available"])
        self.assertIn("출고", hit["reason"])
        # ★어느 주문에 물려 있는지까지 알려 준다.
        #   이 분기를 안 밟는 테스트만 있으면 사유 문구의 오류를 놓친다(실제로 놓쳐 500이 났다).
        self.assertIn(o["orderNumber"], hit["reason"],
                      f"어느 주문에 나갔는지 알려 줘야 한다: {hit['reason']}")

    def test_매칭_가능한_자산이_먼저_나온다(self):
        used = self._asset("USED-MODEL")
        free = self._asset("USED-MODEL")
        o = self.client.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북", "phone": "010-1111-2222",
            "address": "서울시 강남구 1", "postalCode": "06000", "amount": 500000}).get_json()
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [used["id"]]})
        for act in ("production", "softwareInspection", "shipping"):
            self.client.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})

        rows = self.client.get("/api/orders/asset-search?q=USED-MODEL").get_json()
        nos = [x["assetNo"] for x in rows]
        self.assertIn(free["assetNo"], nos)
        self.assertIn(used["assetNo"], nos)
        self.assertLess(nos.index(free["assetNo"]), nos.index(used["assetNo"]),
                        "쓸 수 있는 자산이 위에 있어야 한다")


if __name__ == "__main__":
    unittest.main()
