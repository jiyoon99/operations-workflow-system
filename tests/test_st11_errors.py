"""11번가 오류 안내 — 'IP 미등록'이 '서버 오류'로 뭉개지면 원인을 영영 못 찾는다.

2026-07-29 실전 확인: 11번가는 IP가 등록돼 있지 않으면 HTTP 500에
<AuthMessage><resultCode>-500</resultCode><resultMessage>[IP]인증된 IP가 아닙니다…
를 EUC-KR로 담아 준다. 이걸 '잠시 후 다시 시도'로 안내하면 아무리 기다려도 안 된다.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.malls.base import MallError  # noqa: E402
from app.malls.st11 import St11Adapter  # noqa: E402


class FakeResp:
    def __init__(self, status, xml, encoding="euc-kr"):
        self.status_code = status
        self.content = xml.encode(encoding)


class TestSt11ErrorMessages(unittest.TestCase):
    def setUp(self):
        self.a = St11Adapter({"api_key": "K" * 32})

    def test_ip_rejection_shows_real_reason(self):
        xml = ('<?xml version="1.0" encoding="EUC-KR" standalone="yes"?>'
               "<AuthMessage><resultCode>-500</resultCode>"
               "<resultMessage>[121.173.87.51]인증된 IP가 아닙니다. "
               "등록된 IP 정보를 확인하십시오.</resultMessage></AuthMessage>")
        with self.assertRaises(MallError) as cm:
            self.a._check_http(FakeResp(500, xml))
        msg = str(cm.exception)
        self.assertIn("인증된 IP가 아닙니다", msg, "실제 원인이 안내에서 사라졌다")
        self.assertIn("Open API 관리", msg, "무엇을 해야 하는지 알려주지 않는다")
        self.assertNotIn("잠시 후 다시", msg, "기다려도 해결되지 않는 문제다")

    def test_plain_server_error_still_generic(self):
        with self.assertRaises(MallError) as cm:
            self.a._check_http(FakeResp(500, "Internal Server Error", "utf-8"))
        self.assertIn("잠시 후 다시", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
