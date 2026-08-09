"""스마트스토어 어댑터 — 외부 호출 없이 파싱·병합·필터·발송 규칙을 고정한다.

RMS 실운영에서 확인된 함정(응답 구조 3종, 분리주문, 상태 enum 400)을 그대로 재현한다.
"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.malls.base import MallError, get_adapter  # noqa: E402
from app.malls.smartstore import SmartStoreAdapter  # noqa: E402

SETTINGS = {"client_id": "cid123", "client_secret": "$2a$04$abcdefghijklmnopqrstuv",
            "enabled": True}


def item(po_id, order_id, status="PAYED", name="삼성 노트북", qty=1,
         goods=450000, deliv=3000, option="", orderer="김하나"):
    return {
        "productOrderId": po_id,
        "content": {
            "order": {"orderId": order_id, "ordererName": orderer,
                      "ordererTel": "010-1111-2222", "paymentDate": "2026-07-29T10:00:00"},
            "productOrder": {
                "productOrderId": po_id, "productOrderStatus": status,
                "productName": name, "productId": "P100", "quantity": qty,
                "totalPaymentAmount": goods, "deliveryFeeAmount": deliv,
                "productOption": option,
                "shippingAddress": {"name": "김하나", "tel1": "010-1111-2222",
                                    "zipCode": "06000", "baseAddress": "서울시 강남구",
                                    "detailedAddress": "1층"},
                "shippingMemo": "부재시 경비실",
            },
        },
    }


class Harness(SmartStoreAdapter):
    """네트워크를 막고 준비된 응답을 돌려주는 시험용 어댑터."""

    def __init__(self, settings, pages):
        super().__init__(settings)
        self.pages = list(pages)          # _call 1회당 하나씩 소비
        self.calls = []

    def _call(self, method, path, params=None, body=None):
        self.calls.append({"method": method, "path": path,
                           "params": params or {}, "body": body})
        return self.pages.pop(0) if self.pages else {"data": {"contents": []}}

    def pace(self):
        pass


class TestSmartStore(unittest.TestCase):
    def collect(self, pages):
        ad = Harness(dict(SETTINGS), pages)
        until = datetime(2026, 7, 29, 12, 0)
        return ad, ad.collect_orders(until - timedelta(days=1), until)

    def test_registered_and_ready(self):
        adapter, why = get_adapter("smartstore", dict(SETTINGS))
        self.assertIsNotNone(adapter, why)
        self.assertEqual(adapter.name, "스마트스토어")

    def test_parses_standard_v1_shape(self):
        _ad, orders = self.collect([{"data": {"contents": [item("PO1", "ORD1")]}}])
        self.assertEqual(len(orders), 1)
        o = orders[0]
        self.assertEqual(o["orderNumber"], "ORD1")
        self.assertEqual(o["amount"], 453000)          # 상품 450,000 + 택배 3,000
        self.assertEqual(o["recipient"], "김하나")
        self.assertEqual(o["postalCode"], "06000")
        self.assertEqual(o["address"], "서울시 강남구 1층")
        self.assertEqual(o["deliveryMessage"], "부재시 경비실")
        self.assertEqual(o["orderedAt"], "2026-07-29")

    def test_accepts_alternate_response_shapes(self):
        """응답이 {data:[…]}나 {contents:[…]}로 와도 받아야 한다(버전에 따라 달랐다)."""
        for shape in ({"data": [item("PO1", "ORD1")]},
                      {"contents": [item("PO1", "ORD1")]}):
            _ad, orders = self.collect([shape])
            self.assertEqual(len(orders), 1, f"구조 {list(shape)}를 파싱하지 못했다")

    def test_no_status_filter_in_request(self):
        """★상태 필터를 보내면 400(PRODUCT_PREPARE 미지원 enum) — 보내지 않아야 한다."""
        ad, _orders = self.collect([{"data": {"contents": []}}])
        for c in ad.calls:
            self.assertNotIn("productOrderStatuses", c["params"])

    def test_filters_to_new_orders_locally(self):
        """배송중·완료·취소는 우리 쪽에서 걸러낸다."""
        _ad, orders = self.collect([{"data": {"contents": [
            item("PO1", "ORD1", status="PAYED"),
            item("PO2", "ORD2", status="PRODUCT_PREPARE"),   # 발주확인도 신규다
            item("PO3", "ORD3", status="DELIVERING"),
            item("PO4", "ORD4", status="CANCELED"),
        ]}}])
        self.assertEqual(sorted(o["orderNumber"] for o in orders), ["ORD1", "ORD2"])

    def test_merges_split_product_orders(self):
        """★한 주문이 상품 수만큼 쪼개져 온다 — 주문 단위 1건으로 합쳐야 한다."""
        _ad, orders = self.collect([{"data": {"contents": [
            item("PO1", "ORD1", name="삼성 노트북", goods=450000, deliv=3000),
            item("PO2", "ORD1", name="노트북 가방", goods=20000, deliv=0),
        ]}}])
        self.assertEqual(len(orders), 1, "분리주문이 두 건으로 늘어났다")
        o = orders[0]
        self.assertIn("삼성 노트북", o["productName"])
        self.assertIn("노트북 가방", o["productName"])
        self.assertEqual(o["amount"], 473000)
        self.assertEqual(o["quantity"], 2)

    def test_duplicate_product_order_not_double_counted(self):
        """구간 경계에서 같은 상품주문이 다시 와도 금액이 두 번 잡히면 안 된다."""
        dup = item("PO1", "ORD1")
        _ad, orders = self.collect([{"data": {"contents": [dup, dup]}}])
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["amount"], 453000)

    def test_query_dates_carry_kst_offset(self):
        ad, _orders = self.collect([{"data": {"contents": []}}])
        for c in ad.calls:
            for key in ("from", "to"):
                self.assertTrue(c["params"][key].endswith("+09:00"),
                                f"{key}에 시간대가 없다: {c['params'][key]}")
                self.assertIn(".000", c["params"][key])

    def test_dispatch_resolves_product_orders_and_uses_cj(self):
        """발송처리는 주문번호 → 상품주문 ID 조회 → 전체 발송, 기본 택배사 CJGLS."""
        ad = Harness(dict(SETTINGS), [
            {"data": ["PO1", "PO2"]},                      # product-order-ids 조회
            {"data": {"successProductOrderIds": ["PO1", "PO2"]}},
        ])
        ad.upload_invoice("ORD1", "123456789012")
        dispatch = ad.calls[1]
        self.assertIn("dispatch", dispatch["path"])
        rows = dispatch["body"]["dispatchProductOrders"]
        self.assertEqual([r["productOrderId"] for r in rows], ["PO1", "PO2"])
        for r in rows:
            self.assertEqual(r["deliveryCompanyCode"], "CJGLS")
            self.assertEqual(r["trackingNumber"], "123456789012")

    def test_dispatch_partial_failure_raises(self):
        ad = Harness(dict(SETTINGS), [
            {"data": ["PO1"]},
            {"data": {"failProductOrderInfos": [{"productOrderId": "PO1",
                                                 "message": "이미 발송처리됨"}]}},
        ])
        with self.assertRaises(MallError) as ctx:
            ad.upload_invoice("ORD1", "123456789012")
        self.assertIn("이미 발송처리됨", str(ctx.exception))

    def test_empty_invoice_refused(self):
        ad = Harness(dict(SETTINGS), [])
        with self.assertRaises(MallError):
            ad.upload_invoice("ORD1", "")


if __name__ == "__main__":
    unittest.main()


class TestRentalFilter(unittest.TestCase):
    """렌탈/판매 분류 — 같은 스토어의 렌탈(RMS) 주문이 HMS로 새면 판매 출고 사고가 난다.

    RMS의 분류 원칙(화이트리스트 + 미분류는 버리지 않고 표시)의 거울상.
    """

    def collect(self, pages, extra_settings=None):
        s = dict(SETTINGS)
        s.update(extra_settings or {})
        ad = Harness(s, pages)
        until = datetime(2026, 7, 29, 12, 0)
        return ad, ad.collect_orders(until - timedelta(days=1), until)

    def page(self, *items):
        return [{"data": {"contents": list(items)}}]

    def _item(self, po_id, order_id, name, pid="P100", option=""):
        it = item(po_id, order_id, name=name, option=option)
        it["content"]["productOrder"]["productId"] = pid
        return it

    def test_registered_rental_product_excluded(self):
        ad, orders = self.collect(
            self.page(self._item("PO1", "ORD1", "그램 노트북", pid="R777"),
                      self._item("PO2", "ORD2", "판매 노트북", pid="S111")),
            {"rental_product_ids": "R777", "sale_product_ids": "S111"})
        self.assertEqual([o["orderNumber"] for o in orders], ["ORD2"],
                         "렌탈 등록 상품 주문이 HMS로 들어왔다")
        self.assertEqual(len(ad.skipped_rental), 1)
        self.assertIn("그램 노트북", ad.skipped_rental[0])
        self.assertIn("R777", ad.skipped_rental[0])

    def test_rental_keyword_excluded_before_registration(self):
        """상품번호를 등록하기 전에도 '렌탈·사용기간' 상품명은 자동 제외돼야 한다."""
        _ad, orders = self.collect(self.page(
            self._item("PO1", "ORD1", "LG 그램 렌탈 30일"),
            self._item("PO2", "ORD2", "노트북 판매 상품", option="사용기간 추가: 30일"),
            self._item("PO3", "ORD3", "삼성 리퍼 노트북 판매")))
        self.assertEqual([o["orderNumber"] for o in orders], ["ORD3"])

    def test_sale_whitelist_overrides_keyword(self):
        """상품명에 '렌탈'이 있어도 판매 등록 상품이면 수집돼야 한다(등록이 키워드를 이긴다)."""
        _ad, orders = self.collect(
            self.page(self._item("PO1", "ORD1", "렌탈 반납품 특가 판매", pid="S222")),
            {"sale_product_ids": "S222"})
        self.assertEqual(len(orders), 1)

    def test_unknown_kept_with_visible_tag(self):
        """분류표를 쓰는데 미등록인 상품 — 버리면 출고 누락 사고, 반드시 수집+표시."""
        _ad, orders = self.collect(
            self.page(self._item("PO1", "ORD1", "새 판매 상품", pid="NEW9")),
            {"rental_product_ids": "R777"})
        self.assertEqual(len(orders), 1, "미등록 상품 주문이 사라졌다")
        self.assertIn("분류 미등록", orders[0]["memo"])
        self.assertIn("NEW9", orders[0]["memo"])

    def test_no_lists_means_no_tagging(self):
        """분류표를 아예 안 쓰면(둘 다 빈 값) 기존처럼 조용히 수집한다."""
        _ad, orders = self.collect(self.page(self._item("PO1", "ORD1", "일반 판매 상품")))
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["memo"], "")

    def test_mixed_order_keeps_only_sale_part(self):
        """한 주문에 렌탈·판매 상품이 섞이면 판매 부분만 수집한다."""
        _ad, orders = self.collect(
            self.page(self._item("PO1", "ORD1", "노트북 렌탈 30일", pid="R777"),
                      self._item("PO2", "ORD1", "노트북 가방", pid="S111")),
            {"rental_product_ids": "R777", "sale_product_ids": "S111"})
        self.assertEqual(len(orders), 1)
        self.assertNotIn("렌탈", orders[0]["productName"])
        self.assertEqual(orders[0]["amount"], 453000, "렌탈 상품 금액이 합산됐다")
