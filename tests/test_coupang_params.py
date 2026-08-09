"""쿠팡 v5 조회 파라미터 — 실제 API가 받아들이는 형식을 고정한다(외부 호출 없음).

2026-07-29 실전 확인: 쿠팡 v5 ordersheets는 조회 날짜에 시간대를 붙인
'yyyy-MM-dd+09:00' 형식만 받는다. 날짜만 보내거나 시간을 붙인 ISO 형식은
400 "startTime/endTime not valid, please follow format(ISO standard): yyyy-MM-dd+0X:00"
으로 거부당한다. 네 가지 형식을 실제로 호출해 확인한 결과를 여기에 못 박아 둔다.
"""
import re
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.malls.coupang import KST_OFFSET, CoupangAdapter  # noqa: E402

SETTINGS = {"vendor_id": "A00123456", "access_key": "AK" * 18,
            "secret_key": "SK" * 20, "wing_id": "wing01"}


class TestCoupangQueryFormat(unittest.TestCase):
    def setUp(self):
        self.adapter = CoupangAdapter(dict(SETTINGS))
        self.calls = []
        # 실제로 쿠팡에 나가지 않게 호출을 가로챈다 — 보낸 파라미터만 확인한다
        self.adapter._call = lambda method, path, params=None, body=None: (
            self.calls.append({"method": method, "path": path, "params": params or {}})
            or {"data": [], "nextToken": ""})
        self.adapter.pace = lambda: None

    def test_dates_carry_timezone_offset(self):
        until = datetime(2026, 7, 29, 15, 30)
        self.adapter.collect_orders(until - timedelta(days=1), until)
        self.assertTrue(self.calls, "쿠팡을 한 번도 조회하지 않았다")
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2}\+09:00$")
        for call in self.calls:
            for key in ("createdAtFrom", "createdAtTo"):
                value = call["params"].get(key, "")
                self.assertRegex(
                    value, pattern,
                    f"{key}={value!r} — 쿠팡 v5는 'yyyy-MM-dd+09:00'만 받는다")

    def test_offset_constant_is_kst(self):
        self.assertEqual(KST_OFFSET, "+09:00")

    def test_no_time_component_in_dates(self):
        """시간을 붙인 ISO(2026-07-28T00:00:00+09:00)도 쿠팡이 거부한다."""
        until = datetime(2026, 7, 29, 15, 30)
        self.adapter.collect_orders(until - timedelta(days=1), until)
        for call in self.calls:
            for key in ("createdAtFrom", "createdAtTo"):
                self.assertNotIn("T", call["params"].get(key, ""),
                                 "날짜에 시간(T…)을 붙이면 쿠팡이 400을 낸다")

    def test_vendor_id_goes_into_path(self):
        until = datetime(2026, 7, 29, 15, 30)
        self.adapter.collect_orders(until - timedelta(days=1), until)
        self.assertIn(SETTINGS["vendor_id"], self.calls[0]["path"])


if __name__ == "__main__":
    unittest.main()
