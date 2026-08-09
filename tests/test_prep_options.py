"""제공 옵션 — 몰이 안 실어 주는 옵션을 우리가 붙여 셋팅·QC가 챙기게 한다.

실제 상황(2026-07-29 대표 요청): 고도몰은 리브레오피스·리커버리를 상품 옵션으로 걸 수 있어
주문서에 실려 오지만, 카카오쇼핑은 옵션 자체를 만들 수 없어 주문서가 비어 온다.
그대로 두면 셋팅·QC 담당자가 화면에서 볼 방법이 없어 그냥 넘어가고,
고객은 받아 보고 나서야 "리브레오피스가 없다"고 한다.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth as auth_mod  # noqa: E402
from app import create_app  # noqa: E402

ADMIN_PW = "admin-pass-1"


class TestPrepOptions(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hms-prep-"))
        self.app = create_app(db_path=self.tmp / "test.db")
        self.app.testing = True
        self.client = self.app.test_client()
        auth_mod._login_failures.clear()
        self.client.post("/api/auth/setup", json={
            "username": "admin", "displayName": "대표", "password": ADMIN_PW})
        self.cats = self.client.get("/api/categories").get_json()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- helpers

    def _option(self, name="리브레오피스 설치", note=""):
        r = self.client.post("/api/prep-options", json={"name": name, "note": note})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()

    def _rule(self, oid, **kw):
        body = {"channel": "카카오쇼핑", "matchType": "all", "matchValue": ""}
        body.update(kw)
        return self.client.post(f"/api/prep-options/{oid}/rules", json=body)

    def _order(self, **kw):
        body = {"recipient": "김하나", "productName": "삼성 노트북",
                "channel": "카카오쇼핑", "amount": 500000}
        body.update(kw)
        return self.client.post("/api/orders", json=body).get_json()

    def _detail(self, oid):
        return self.client.get(f"/api/orders/{oid}").get_json()

    # ---------------------------------------------------------------- tests

    def test_option_shows_on_matching_channel_only(self):
        opt = self._option()
        self.assertEqual(self._rule(opt["id"]).status_code, 201)
        kakao = self._order()
        godo = self._order(channel="고도몰")
        self.assertEqual([o["name"] for o in self._detail(kakao["id"])["prepOptions"]],
                         ["리브레오피스 설치"])
        self.assertEqual(self._detail(godo["id"])["prepOptions"], [],
                         "다른 몰 주문에 옵션이 붙었다")

    def test_product_condition_narrows_to_matching_orders(self):
        opt = self._option("램 16G 업그레이드")
        self.assertEqual(
            self._rule(opt["id"], matchType="code", matchValue="RAM16").status_code, 201)
        hit = self._order(productCode="KKO-RAM16-NT371")
        miss = self._order(productCode="KKO-NT371")
        self.assertEqual(len(self._detail(hit["id"])["prepOptions"]), 1)
        self.assertEqual(self._detail(miss["id"])["prepOptions"], [])

    def test_production_blocked_until_all_checked(self):
        """★이 기능의 핵심 — 다 챙기기 전에는 제작 완료로 넘어갈 수 없다."""
        o1 = self._option("리브레오피스 설치")
        o2 = self._option("리커버리 복구 영역")
        self._rule(o1["id"])
        self._rule(o2["id"])
        order = self._order()

        r = self.client.patch(f"/api/orders/{order['id']}",
                              json={"action": "production", "value": True})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertIn("챙기지 않은 옵션", r.get_json()["error"])

        self.client.post(f"/api/orders/{order['id']}/options/{o1['id']}", json={"checked": True})
        r = self.client.patch(f"/api/orders/{order['id']}",
                              json={"action": "production", "value": True})
        self.assertEqual(r.status_code, 409, "하나 남았는데 통과했다")

        self.client.post(f"/api/orders/{order['id']}/options/{o2['id']}", json={"checked": True})
        r = self.client.patch(f"/api/orders/{order['id']}",
                              json={"action": "production", "value": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_check_records_who_and_when(self):
        opt = self._option()
        self._rule(opt["id"])
        order = self._order()
        r = self.client.post(f"/api/orders/{order['id']}/options/{opt['id']}",
                             json={"checked": True})
        self.assertEqual(r.status_code, 200)
        item = self._detail(order["id"])["prepOptions"][0]
        self.assertTrue(item["checked"])
        self.assertEqual(item["checkedBy"], "대표")
        self.assertTrue(item["checkedAt"])
        # 잘못 눌렀을 때 해제도 된다
        self.client.post(f"/api/orders/{order['id']}/options/{opt['id']}", json={"checked": False})
        self.assertFalse(self._detail(order["id"])["prepOptions"][0]["checked"])

    def test_check_survives_rule_toggle(self):
        """규칙을 껐다 켜도 이미 챙긴 체크는 남아야 한다(다시 체크시키면 안 된다)."""
        opt = self._option()
        rule = self._rule(opt["id"]).get_json()["rules"][0]
        order = self._order()
        self.client.post(f"/api/orders/{order['id']}/options/{opt['id']}", json={"checked": True})
        self.client.delete(f"/api/prep-option-rules/{rule['id']}")
        self.assertEqual(self._detail(order["id"])["prepOptions"], [])
        self._rule(opt["id"])
        again = self._detail(order["id"])["prepOptions"]
        self.assertTrue(again[0]["checked"], "규칙을 되살렸더니 체크가 사라졌다")

    def test_all_malls_all_orders_rule_is_refused(self):
        """모든 쇼핑몰 + 모든 주문 = 전 주문 강제. 실수로 만들기 쉬워 막는다."""
        opt = self._option()
        r = self._rule(opt["id"], channel="", matchType="all")
        self.assertEqual(r.status_code, 400)
        self.assertIn("쇼핑몰", r.get_json()["error"])

    def test_condition_without_value_is_refused(self):
        """조건을 고르고 값을 비우면 '전부 일치'가 돼 버린다 — 그건 사고다."""
        opt = self._option()
        self.assertEqual(self._rule(opt["id"], matchType="code", matchValue="").status_code, 400)

    def test_preview_counts_before_saving(self):
        """저장 전에 몇 건에 붙는지 보여줘야 한다(규칙은 지난 주문에도 소급 적용된다)."""
        self._order()
        self._order()
        self._order(channel="고도몰")
        r = self.client.post("/api/prep-options/preview",
                             json={"channel": "카카오쇼핑", "matchType": "all", "matchValue": ""})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["total"], 2)
        self.assertEqual(r.get_json()["pending"], 2)

    def test_disabled_option_does_not_block_work(self):
        opt = self._option()
        self._rule(opt["id"])
        order = self._order()
        self.client.patch(f"/api/prep-options/{opt['id']}", json={"enabled": False})
        self.assertEqual(self._detail(order["id"])["prepOptions"], [])
        r = self.client.patch(f"/api/orders/{order['id']}",
                              json={"action": "production", "value": True})
        self.assertEqual(r.status_code, 200, "꺼 둔 옵션이 작업을 막았다")

    def test_option_prints_on_waybill(self):
        """송장에도 찍혀야 포장 담당이 대조할 수 있다."""
        opt = self._option()
        self._rule(opt["id"])
        order = self._order(phone="010-1111-2222", address="서울시 강남구 1",
                            postalCode="06000")
        a = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1}).get_json()[0]
        self.client.patch(f"/api/orders/{order['id']}",
                          json={"action": "assets", "assetIds": [a["id"]]})
        self.client.post(f"/api/orders/{order['id']}/options/{opt['id']}", json={"checked": True})
        self.client.patch(f"/api/orders/{order['id']}", json={"action": "production", "value": True})
        self.client.patch(f"/api/orders/{order['id']}",
                          json={"action": "softwareInspection", "value": True})
        r = self.client.post(f"/api/orders/{order['id']}/waybill", json={})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        items = self.client.get("/api/waybills").get_json()[0]["items"]
        self.assertIn("리브레오피스 설치", items, "송장에 제공 옵션이 안 찍혔다")
        self.assertIn(a["assetNo"], items, "송장에 자산번호가 안 찍혔다")

    def test_cancelled_order_cannot_be_checked(self):
        opt = self._option()
        self._rule(opt["id"])
        order = self._order()
        self.client.patch(f"/api/orders/{order['id']}",
                          json={"action": "cancel", "reason": "고객 변심"})
        r = self.client.post(f"/api/orders/{order['id']}/options/{opt['id']}",
                             json={"checked": True})
        self.assertEqual(r.status_code, 409)

    def test_unrelated_option_cannot_be_checked(self):
        """화면이 오래돼 사라진 옵션을 체크하려 하면 조용히 통과시키면 안 된다."""
        opt = self._option()
        order = self._order()          # 규칙이 없어 이 주문엔 붙지 않는다
        r = self.client.post(f"/api/orders/{order['id']}/options/{opt['id']}",
                             json={"checked": True})
        self.assertEqual(r.status_code, 400)

    def test_list_does_not_slow_down_with_many_orders(self):
        """목록에서 주문마다 옵션을 따로 조회하면(N+1) 실무에서 느려진다."""
        opt = self._option()
        self._rule(opt["id"])
        for _ in range(30):
            self._order()
        r = self.client.get("/api/orders?view=active")
        self.assertEqual(r.status_code, 200)
        orders = r.get_json()["orders"]
        self.assertTrue(all("prepOptions" in o for o in orders))
        self.assertTrue(any(o["prepOptions"] for o in orders))


if __name__ == "__main__":
    unittest.main()


class TestWaybillPreview(TestPrepOptions):
    """송장 미리보기 — CJ를 부르지 않고 인쇄될 내용을 보여준다(셋팅 검수완료 → 발급 흐름)."""

    def _ready_order(self):
        opt = self._option()
        self._rule(opt["id"])
        order = self._order(phone="010-1111-2222", address="서울시 강남구 1",
                            postalCode="06000", productCode="KKO-NT371")
        a = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "NT371", "qty": 1}).get_json()[0]
        self.client.patch(f"/api/orders/{order['id']}",
                          json={"action": "assets", "assetIds": [a["id"]]})
        return order, a, opt

    def test_preview_shows_label_content_without_issuing(self):
        order, a, opt = self._ready_order()
        self.client.post(f"/api/orders/{order['id']}/options/{opt['id']}", json={"checked": True})
        self.client.patch(f"/api/orders/{order['id']}", json={"action": "production", "value": True})
        self.client.patch(f"/api/orders/{order['id']}",
                          json={"action": "softwareInspection", "value": True})
        r = self.client.get(f"/api/orders/{order['id']}/waybill-preview")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        p = r.get_json()
        self.assertIn("KKO-NT371", p["itemSummary"])
        self.assertIn(a["assetNo"], p["itemSummary"])
        self.assertIn("리브레오피스 설치", p["itemSummary"])
        self.assertEqual(p["blockers"], [])
        # 미리보기는 송장을 만들지 않는다
        self.assertEqual(self.client.get("/api/waybills").get_json(), [])

    def test_preview_lists_blockers(self):
        """왜 발급이 안 되는지 이유가 그대로 보여야 한다(무반응 버튼 금지)."""
        opt = self._option()
        self._rule(opt["id"])
        order = self._order()          # 자산 미매칭·주소 없음·옵션 미체크·검수 전
        p = self.client.get(f"/api/orders/{order['id']}/waybill-preview").get_json()
        joined = " ".join(p["blockers"])
        self.assertIn("검수", joined)
        self.assertIn("자산번호", joined)
        self.assertIn("주소", joined)
        self.assertIn("리브레오피스 설치", joined)

    def test_preview_shows_existing_waybill(self):
        order, _a, opt = self._ready_order()
        self.client.post(f"/api/orders/{order['id']}/options/{opt['id']}", json={"checked": True})
        self.client.patch(f"/api/orders/{order['id']}", json={"action": "production", "value": True})
        self.client.patch(f"/api/orders/{order['id']}",
                          json={"action": "softwareInspection", "value": True})
        self.client.post(f"/api/orders/{order['id']}/waybill", json={})
        p = self.client.get(f"/api/orders/{order['id']}/waybill-preview").get_json()
        self.assertIsNotNone(p["existing"], "이미 발급된 송장을 알려주지 않는다")


class TestParcelSequence(TestPrepOptions):
    """같은 고객에게 여러 건이 나가면 송장에 1/6·2/6이 찍혀야 한다(대표 요청 2026-07-29)."""

    def _shipped_ready(self, phone, name="노트북"):
        order = self._order(recipient="김여섯", phone=phone, address="서울시 강남구 1",
                            postalCode="06000", productName=name, channel="쿠팡")
        a = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1}).get_json()[0]
        self.client.patch(f"/api/orders/{order['id']}",
                          json={"action": "assets", "assetIds": [a["id"]]})
        self.client.patch(f"/api/orders/{order['id']}", json={"action": "production", "value": True})
        self.client.patch(f"/api/orders/{order['id']}",
                          json={"action": "softwareInspection", "value": True})
        return order

    def test_sequence_printed_for_multi_order_customer(self):
        orders = [self._shipped_ready("010-6666-7777", f"노트북{i}") for i in range(3)]
        seqs = []
        for o in orders:
            p = self.client.get(f"/api/orders/{o['id']}/waybill-preview").get_json()
            seqs.append((p["parcelSeq"], p["parcelTotal"]))
            self.assertIn(f"[{p['parcelSeq']}/3]", p["itemSummary"],
                          "송장 상품명에 순번이 안 찍혔다")
        self.assertEqual(seqs, [(1, 3), (2, 3), (3, 3)])

    def test_single_order_has_no_sequence(self):
        """한 건뿐이면 1/1 같은 표기는 오히려 헷갈린다 — 찍지 않는다."""
        o = self._shipped_ready("010-8888-9999")
        p = self.client.get(f"/api/orders/{o['id']}/waybill-preview").get_json()
        self.assertEqual((p["parcelSeq"], p["parcelTotal"]), (1, 1))
        self.assertNotIn("/1]", p["itemSummary"])

    def test_sequence_stable_after_one_is_issued(self):
        """한 건을 발급해도 나머지 분모가 줄면 안 된다(1/3 → 2/2 로 흔들리면 혼란)."""
        orders = [self._shipped_ready("010-5555-4444", f"노트북{i}") for i in range(3)]
        self.client.post(f"/api/orders/{orders[0]['id']}/waybill", json={})
        p = self.client.get(f"/api/orders/{orders[2]['id']}/waybill-preview").get_json()
        self.assertEqual((p["parcelSeq"], p["parcelTotal"]), (3, 3))

    def test_other_customer_not_counted(self):
        self._shipped_ready("010-1212-3434")
        mine = self._shipped_ready("010-7878-5656")
        p = self.client.get(f"/api/orders/{mine['id']}/waybill-preview").get_json()
        self.assertEqual(p["parcelTotal"], 1, "다른 고객 주문까지 묶어 셌다")

    def test_issued_label_carries_sequence(self):
        orders = [self._shipped_ready("010-3333-2222", f"노트북{i}") for i in range(2)]
        self.client.post(f"/api/orders/{orders[1]['id']}/waybill", json={})
        items = self.client.get("/api/waybills").get_json()[0]["items"]
        self.assertIn("[2/2]", items)


class TestReviewerOrders(TestPrepOptions):
    """리뷰어 출고 — 체험단 건은 셋팅·QC가 일반 주문과 구분해 다뤄야 한다."""

    def test_bulk_mark_and_unmark(self):
        o1 = self._order(recipient="리뷰어1")
        o2 = self._order(recipient="리뷰어2")
        r = self.client.post("/api/orders/bulk", json={
            "action": "review", "ids": [o1["id"], o2["id"]], "value": True,
            "reason": "제품X 빈박스출고"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["ok"], 2)
        d = self._detail(o1["id"])
        self.assertTrue(d["isReview"])
        self.assertEqual(d["reviewNote"], "제품X 빈박스출고")
        # 해제
        self.client.post("/api/orders/bulk", json={
            "action": "review", "ids": [o1["id"]], "value": False})
        self.assertFalse(self._detail(o1["id"])["isReview"])

    def test_reviewer_waybill_without_asset(self):
        """★리뷰어는 제품 없이 빈 박스만 나가기도 한다 — 자산 매칭을 요구하면 막힌다."""
        o = self._order(recipient="리뷰어", phone="010-1111-2222",
                        address="서울시 강남구 1", postalCode="06000")
        self.client.post("/api/orders/bulk", json={
            "action": "review", "ids": [o["id"]], "value": True})
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "production", "value": True})
        self.client.patch(f"/api/orders/{o['id']}",
                          json={"action": "softwareInspection", "value": True})
        p = self.client.get(f"/api/orders/{o['id']}/waybill-preview").get_json()
        self.assertTrue(p["isReview"])
        self.assertEqual([b for b in p["blockers"] if "자산" in b], [],
                         "리뷰어인데 자산 매칭을 요구했다")
        r = self.client.post(f"/api/orders/{o['id']}/waybill", json={})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        items = self.client.get("/api/waybills").get_json()[0]["items"]
        self.assertIn("[리뷰어]", items, "송장에 리뷰어 표시가 없다")

    def test_normal_order_still_needs_asset(self):
        """일반 주문은 그대로 자산 매칭이 필수여야 한다(리뷰어 예외가 새면 안 된다)."""
        o = self._order(phone="010-1111-2222", address="서울시 강남구 1")
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "production", "value": True})
        self.client.patch(f"/api/orders/{o['id']}",
                          json={"action": "softwareInspection", "value": True})
        r = self.client.post(f"/api/orders/{o['id']}/waybill", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("자산번호", r.get_json()["error"])


class TestLabelBudget(TestPrepOptions):
    """송장 상품명 칸(120자) 배분 — 자산번호는 절대 잘리면 안 된다."""

    def test_asset_numbers_never_truncated(self):
        from app.orders.waybill import _compose_items
        row = {"channel": "고도몰", "product_code": "Z16 Gen1_R7p-6_RX6500M",
               "product_name": "S급 레노버 씽크패드 Z16 라데온RX650M 탑재 16인치 전문작업 "
                               "노트북 DDR5 16G 램 NVMe SSD 512G 저장용량 C타입충전가능",
               "option_name": "", "quantity": 3, "is_review": 0}
        nos = ["260729-0001", "260729-0002", "260729-0003"]
        items = _compose_items(row, nos, simulated=False, prep_names=["리브레오피스"], seq=(2, 6))
        summary = " / ".join(
            f"{i.get('name')}{' x' + str(i['qty']) if i.get('qty') else ''}".strip()
            for i in items if i.get("name"))
        self.assertLessEqual(len(summary), 120)
        asset_part = next((i["name"] for i in items if i["name"].startswith("자산")), "")
        # 실린 번호는 전부 온전해야 한다(반쪽 번호 금지)
        for token in asset_part.replace("자산 ", "").split(" 외 ")[0].split(","):
            self.assertIn(token.strip(), nos, f"잘린 자산번호가 찍혔다: {token}")
        self.assertIn("Z16 Gen1", summary, "자체상품코드가 빠졌다")
        self.assertIn("[2/6]", summary, "택배 순번이 빠졌다")

    def test_many_assets_summarised(self):
        from app.orders.waybill import _compose_items
        row = {"channel": "쿠팡", "product_code": "CP-1", "product_name": "노트북",
               "option_name": "", "quantity": 9, "is_review": 0}
        nos = [f"26072{i}-000{i}" for i in range(1, 10)]
        items = _compose_items(row, nos, simulated=False)
        asset_part = next((i["name"] for i in items if i["name"].startswith("자산")), "")
        self.assertIn("외", asset_part, "자산이 많은데 '외 N대' 요약이 없다")
        head, _, rest = asset_part.replace("자산 ", "").partition(" 외 ")
        for token in head.split(","):
            self.assertIn(token.strip(), nos)
