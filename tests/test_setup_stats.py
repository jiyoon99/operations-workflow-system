"""셋팅 실적 — 담당자별 대수(일/주/월/분기/연도) + 날짜별 캘린더.

대표 정의(2026-07-29): "제품을 준비해서 검수완료까지 끝낸 것"을 한 대로 센다.
원본(NAS 주문워크플로)에 있던 daily-stats·캘린더를 HMS로 이식하면서,
대수 기준과 실적 주인을 명확히 못박는다.
"""
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth as auth_mod  # noqa: E402
from app import create_app  # noqa: E402

ADMIN_PW = "admin-pass-1"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hms-ss-"))
        self.db_path = self.tmp / "test.db"
        self.app = create_app(db_path=self.db_path)
        self.app.testing = True
        self.client = self.app.test_client()
        auth_mod._login_failures.clear()
        self.client.post("/api/auth/setup", json={
            "username": "admin", "displayName": "대표", "password": ADMIN_PW})
        self.cats = self.client.get("/api/categories").get_json()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _inspected(self, when, maker="정우석", checker=None, qty=1, assets=0):
        """검수완료 주문 하나 만들기 — 검수 시각을 원하는 날짜로 박는다."""
        o = self.client.post("/api/orders", json={
            "recipient": "김하나", "productName": "노트북", "quantity": qty,
            "channel": "고도몰"}).get_json()
        if assets:
            made = self.client.post("/api/assets", json={
                "categoryId": self.cats[0]["id"], "model": "L480", "qty": assets}).get_json()
            self.client.patch(f"/api/orders/{o['id']}", json={
                "action": "assets", "assetIds": [a["id"] for a in made]})
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "production", "value": True})
        self.client.patch(f"/api/orders/{o['id']}",
                          json={"action": "softwareInspection", "value": True})
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE orders SET production_by=?, inspection_by=?, inspection_at=? WHERE id=?",
            (maker, checker or maker, f"{when}T14:30:00+09:00", o["id"]))
        conn.commit()
        conn.close()
        return o

    def _stats(self, period="month", when=None):
        q = f"?period={period}" + (f"&date={when}" if when else "")
        r = self.client.get("/api/reports/setup-stats" + q)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()


class TestSetupStats(Base):
    def test_counts_only_inspected(self):
        """검수완료까지 간 것만 센다 — 제작만 끝난 건 실적이 아니다."""
        today = date.today().isoformat()
        self._inspected(today)
        # 제작만 하고 검수 안 한 주문
        o = self.client.post("/api/orders", json={
            "recipient": "미검수", "productName": "노트북"}).get_json()
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "production", "value": True})
        d = self._stats("day", today)
        self.assertEqual(d["total"]["units"], 1)
        self.assertEqual(d["total"]["orders"], 1)

    def test_units_use_matched_assets(self):
        """대수는 매칭한 자산 수 기준 — 한 주문에 2대면 2대."""
        today = date.today().isoformat()
        self._inspected(today, qty=2, assets=2)
        d = self._stats("day", today)
        self.assertEqual(d["total"]["units"], 2)
        self.assertEqual(d["total"]["orders"], 1)

    def test_units_fall_back_to_quantity(self):
        """자산을 아직 안 붙였어도 실적이 0대가 되면 안 된다."""
        today = date.today().isoformat()
        self._inspected(today, qty=3, assets=0)
        self.assertEqual(self._stats("day", today)["total"]["units"], 3)

    def test_credit_goes_to_maker_not_checker(self):
        """실적 주인은 만든 사람. 검수만 한 사람은 따로 표시하고 합계에 더하지 않는다."""
        today = date.today().isoformat()
        self._inspected(today, maker="정우석", checker="김검수")
        d = self._stats("day", today)
        by = {s["name"]: s for s in d["staff"]}
        self.assertEqual(by["정우석"]["units"], 1)
        self.assertEqual(by["김검수"]["units"], 0, "검수자가 셋팅 실적을 가져갔다")
        self.assertEqual(by["김검수"]["inspected"], 1)
        self.assertEqual(d["total"]["units"], 1, "같은 제품이 두 번 세어졌다")

    def test_period_boundaries(self):
        """일/주/월/분기/연도 경계가 정확해야 한다."""
        anchor = date(2026, 7, 15)
        self._inspected("2026-07-15", maker="A")
        self._inspected("2026-07-01", maker="B")     # 같은 달, 다른 주
        self._inspected("2026-06-30", maker="C")     # 전월(같은 분기·연도)
        self._inspected("2025-12-31", maker="D")     # 전년

        self.assertEqual(self._stats("day", anchor.isoformat())["total"]["units"], 1)
        week = self._stats("week", anchor.isoformat())
        self.assertEqual(week["total"]["units"], 1)          # 7/13~7/19
        month = self._stats("month", anchor.isoformat())
        self.assertEqual(month["total"]["units"], 2)         # 7/1 + 7/15
        quarter = self._stats("quarter", anchor.isoformat())
        self.assertEqual(quarter["total"]["units"], 2)       # 3분기=7~9월 → 6/30은 2분기라 제외
        self.assertEqual(quarter["period"]["from"], "2026-07-01")
        self.assertEqual(quarter["period"]["to"], "2026-09-30")
        year = self._stats("year", anchor.isoformat())
        self.assertEqual(year["total"]["units"], 3)          # 2026년 전체(6/30 포함)

    def test_calendar_has_per_day_and_staff(self):
        self._inspected("2026-07-10", maker="정우석")
        self._inspected("2026-07-10", maker="김철수")
        self._inspected("2026-07-11", maker="정우석")
        d = self._stats("month", "2026-07-15")
        cal = {c["date"]: c for c in d["calendar"]}
        self.assertEqual(cal["2026-07-10"]["units"], 2)
        self.assertEqual(cal["2026-07-10"]["byStaff"]["정우석"], 1)
        self.assertEqual(cal["2026-07-11"]["units"], 1)

    def test_previous_period_comparison(self):
        self._inspected("2026-07-05")
        self._inspected("2026-06-05")
        self._inspected("2026-06-06")
        d = self._stats("month", "2026-07-15")
        self.assertEqual(d["total"]["units"], 1)
        self.assertEqual(d["previous"]["units"], 2)
        self.assertEqual(d["previous"]["diff"], -1)

    def test_cancelled_excluded(self):
        today = date.today().isoformat()
        o = self._inspected(today)
        self.client.patch(f"/api/orders/{o['id']}",
                          json={"action": "cancel", "reason": "고객 변심"})
        self.assertEqual(self._stats("day", today)["total"]["units"], 0)

    def test_day_detail_lists_orders(self):
        self._inspected("2026-07-10", maker="정우석", qty=2, assets=2)
        r = self.client.get("/api/reports/setup-day?date=2026-07-10")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["units"], 2)
        self.assertEqual(len(d["orders"]), 1)
        self.assertEqual(d["orders"][0]["productionBy"], "정우석")
        self.assertEqual(d["orders"][0]["units"], 2)

    def test_bad_period_refused(self):
        self.assertEqual(self.client.get("/api/reports/setup-stats?period=hour").status_code, 400)
        self.assertEqual(
            self.client.get("/api/reports/setup-stats?date=2026-13-99").status_code, 400)

    def test_missing_maker_is_labelled(self):
        """담당자 기록이 없는 옛 데이터도 실적에서 사라지면 안 된다."""
        today = date.today().isoformat()
        self._inspected(today, maker="")
        d = self._stats("day", today)
        self.assertEqual(d["total"]["units"], 1)
        self.assertEqual(d["staff"][0]["name"], "(담당자 미기록)")


if __name__ == "__main__":
    unittest.main()


class TestShippingDateStability(Base):
    """★출고 확인을 껐다 켜도 출고일·담당자가 바뀌면 안 된다.

    2026-07-29 점검 지적: 자산 매칭을 고치려고 단계를 껐다 켜는 흔한 작업만으로
    출고일이 오늘로 덮어써져, 지난달 매출이 이번 달로 넘어가고 담당자 실적도 바뀌었다.
    """

    def _shipped(self):
        o = self.client.post("/api/orders", json={
            "recipient": "홍길동", "productName": "노트북", "amount": 500000,
            "channel": "쿠팡"}).get_json()
        a = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1}).get_json()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("production", "softwareInspection", "shipping"):
            self.client.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        return o

    def test_reship_keeps_original_date_and_worker(self):
        o = self._shipped()
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE orders SET shipping_at=?, shipping_by=? WHERE id=?",
                     ("2026-06-15T10:00:00+09:00", "정우석", o["id"]))
        conn.commit(); conn.close()

        # 검수를 풀면 출고도 함께 풀린다 → 다시 체크
        self.client.patch(f"/api/orders/{o['id']}",
                          json={"action": "softwareInspection", "value": False})
        self.client.patch(f"/api/orders/{o['id']}",
                          json={"action": "softwareInspection", "value": True})
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})

        conn = sqlite3.connect(self.db_path)
        at, by = conn.execute(
            "SELECT shipping_at, shipping_by FROM orders WHERE id=?", (o["id"],)).fetchone()
        conn.close()
        self.assertTrue(at.startswith("2026-06-15"),
                        f"출고일이 오늘로 덮어써졌다: {at} — 지난달 매출이 이번 달로 넘어간다")
        self.assertEqual(by, "정우석", "출고 담당자가 재체크한 사람으로 바뀌었다")

    def test_first_shipping_records_now(self):
        """처음 출고할 때는 당연히 지금 시각이 찍혀야 한다."""
        o = self._shipped()
        conn = sqlite3.connect(self.db_path)
        at, by = conn.execute(
            "SELECT shipping_at, shipping_by FROM orders WHERE id=?", (o["id"],)).fetchone()
        conn.close()
        self.assertTrue(at, "출고일이 비어 있다")
        self.assertEqual(by, "대표")


class TestSmsGuards(Base):
    """문자 안전장치 — 야간 차단이 재발송으로 뚫리면 안 되고, 다른 PC면 잠겨야 한다.

    ★실제 발송은 send_sms를 가로채 원천 차단한다(테스트가 고객에게 문자를 보내면 안 된다).
    """

    def _force_live(self):
        """운영 판정을 통과시킨다 — 임시 DB라 그대로면 '검증용 사본'에서 막힌다."""
        import app.notify as nm
        orig = nm.is_production_server
        nm.is_production_server = lambda p: (True, "")
        self.addCleanup(setattr, nm, "is_production_server", orig)
        return nm

    def _block_real_send(self, nm):
        sent = []
        orig = nm.send_sms
        nm.send_sms = lambda *a, **k: (sent.append(a) or
                                       {"ok": True, "msgType": "SMS", "detail": ""})
        self.addCleanup(setattr, nm, "send_sms", orig)
        return sent

    def test_quiet_hours_not_bypassed_by_force(self):
        nm = self._force_live()
        sent = self._block_real_send(nm)
        # 하루 종일을 야간으로 설정해 지금이 몇 시든 재현되게 한다(0시~24시)
        self.client.put("/api/settings", json={"sms": {
            "armed": True, "apiKey": "K", "sender": "0212345678",
            "firstLiveConfirmedAt": "2026-07-29T10:00:00+09:00",
            "quietFrom": 0, "quietTo": 24}})
        t = self.client.post("/api/as-tickets", json={
            "customer": "홍길동", "phone": "010-1111-2222", "symptom": "전원 불량"}).get_json()

        r = self.client.post(f"/api/as-tickets/{t['id']}/sms",
                             json={"event": "received", "force": True})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("야간", body["message"], "재발송(force)이 야간 차단을 뚫었다")
        self.assertEqual(sent, [], "야간인데 실제로 발송됐다")

    def test_quiet_hours_can_be_allowed_explicitly(self):
        """사람이 '그래도 보낸다'고 한 번 더 확인하면 나간다."""
        nm = self._force_live()
        sent = self._block_real_send(nm)
        self.client.put("/api/settings", json={"sms": {
            "armed": True, "apiKey": "K", "sender": "0212345678",
            "firstLiveConfirmedAt": "2026-07-29T10:00:00+09:00",
            "quietFrom": 0, "quietTo": 24}})
        t = self.client.post("/api/as-tickets", json={
            "customer": "홍길동", "phone": "010-1111-2222", "symptom": "전원 불량"}).get_json()
        r = self.client.post(f"/api/as-tickets/{t['id']}/sms",
                             json={"event": "received", "force": True, "allowQuiet": True})
        self.assertTrue(r.get_json()["ok"], r.get_data(as_text=True))
        self.assertEqual(len(sent), 1)

    def test_bound_host_blocks_other_pc(self):
        """폴더째 복사해 다른 PC에서 띄우면 실발송이 잠겨야 한다."""
        from app.notify import is_live_sending
        self._force_live()
        cfg = {"armed": True, "apiKey": "K", "sender": "0212345678",
               "firstLiveConfirmedAt": "2026-07-29T10:00:00+09:00",
               "boundHost": "some-other-pc"}
        ok, why = is_live_sending(cfg, self.db_path)
        self.assertFalse(ok)
        self.assertIn("확인된 곳이 아닙니다", why)

    def test_same_host_allowed(self):
        from app.notify import host_fingerprint, is_live_sending
        self._force_live()
        cfg = {"armed": True, "apiKey": "K", "sender": "0212345678",
               "firstLiveConfirmedAt": "2026-07-29T10:00:00+09:00",
               "boundHost": host_fingerprint()}
        ok, why = is_live_sending(cfg, self.db_path)
        self.assertTrue(ok, why)

    def test_no_bound_host_is_backward_compatible(self):
        """지문을 아직 기록하지 않은 기존 설정은 그대로 동작해야 한다."""
        from app.notify import is_live_sending
        self._force_live()
        cfg = {"armed": True, "apiKey": "K", "sender": "0212345678",
               "firstLiveConfirmedAt": "2026-07-29T10:00:00+09:00"}
        ok, why = is_live_sending(cfg, self.db_path)
        self.assertTrue(ok, why)


class TestSweepFixes(Base):
    """2026-07-29 버튼 전수조사에서 나온 서버 쪽 결함들."""

    def _order(self, **kw):
        body = {"recipient": "홍길동", "productName": "노트북", "channel": "쿠팡",
                "amount": 500000}
        body.update(kw)
        return self.client.post("/api/orders", json=body).get_json()

    def test_delivered_order_can_be_archived(self):
        """★[배송완료 처리]한 주문을 보관할 수 없어 목록을 치울 방법이 없었다."""
        o = self._order()
        self.client.post("/api/orders/bulk", json={
            "action": "delivered", "ids": [o["id"]], "value": True})
        r = self.client.post("/api/orders/bulk", json={"action": "archive", "ids": [o["id"]]})
        body = r.get_json()
        self.assertEqual(body["ok"], 1, f"배송완료 주문을 보관하지 못했다: {body.get('failed')}")

    def test_in_progress_order_still_cannot_be_archived(self):
        """진행 중 주문까지 보관되면 안 된다(가드가 통째로 풀리지 않았는지)."""
        o = self._order()
        r = self.client.post("/api/orders/bulk", json={"action": "archive", "ids": [o["id"]]})
        body = r.get_json()
        self.assertEqual(body["ok"], 0)
        self.assertIn("진행 중", body["failed"][0]["reason"])

    def test_waybill_blocked_when_prep_option_unchecked(self):
        """미리보기는 막는데 발급은 통과하던 규칙 어긋남."""
        opt = self.client.post("/api/prep-options", json={"name": "리브레오피스 설치"}).get_json()
        self.client.post(f"/api/prep-options/{opt['id']}/rules",
                         json={"channel": "쿠팡", "matchType": "all", "matchValue": ""})
        o = self._order(phone="010-1111-2222", address="서울시 강남구 1", postalCode="06000")
        a = self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1}).get_json()[0]
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.client.post(f"/api/orders/{o['id']}/options/{opt['id']}", json={"checked": True})
        self.client.patch(f"/api/orders/{o['id']}", json={"action": "production", "value": True})
        self.client.patch(f"/api/orders/{o['id']}",
                          json={"action": "softwareInspection", "value": True})
        # 체크를 풀면 발급도 막혀야 한다(미리보기와 같은 규칙)
        self.client.post(f"/api/orders/{o['id']}/options/{opt['id']}", json={"checked": False})
        r = self.client.post(f"/api/orders/{o['id']}/waybill", json={})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("챙기지 않은 옵션", r.get_json()["error"])

    def test_as_recall_not_blocked_by_return_waybill(self):
        """반송 송장을 먼저 낸 A/S도 회수 예약을 걸 수 있어야 한다."""
        t = self.client.post("/api/as-tickets", json={
            "customer": "홍길동", "phone": "010-1111-2222", "symptom": "전원 불량",
            "address": "서울시 강남구 1"}).get_json()
        self.client.patch(f"/api/as-tickets/{t['id']}", json={"status": "done",
                                                             "resolution": "메인보드 교체"})
        rb = self.client.post(f"/api/as-tickets/{t['id']}/return-waybill", json={})
        self.assertEqual(rb.status_code, 201, rb.get_data(as_text=True))
        r = self.client.post(f"/api/as-tickets/{t['id']}/recall", json={})
        self.assertEqual(r.status_code, 201,
                         f"반송 송장 때문에 회수가 막혔다: {r.get_data(as_text=True)}")

    def test_asset_search_gives_korean_status(self):
        """A/S 담당(매입 권한 없음)에게 'shipped' 같은 영어가 보이면 안 된다."""
        self.client.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "SEARCHME", "qty": 1})
        rows = self.client.get("/api/as-tickets/asset-search?q=SEARCHME").get_json()
        self.assertTrue(rows)
        self.assertTrue(rows[0]["statusLabel"])
        self.assertNotEqual(rows[0]["statusLabel"], rows[0]["status"],
                            "상태가 코드 그대로 내려온다")

    def test_report_period_reversed_is_refused(self):
        """날짜를 거꾸로 넣으면 조용히 0원이 아니라 이유를 알려야 한다."""
        r = self.client.get("/api/reports/summary?from=2026-07-29&to=2026-07-01")
        self.assertEqual(r.status_code, 400)
        self.assertIn("늦습니다", r.get_json()["error"])
