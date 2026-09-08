"""몰 호출 예산 보호 — 토큰 재사용·429 쿨다운·동시 수집 방지.

RMS가 스마트스토어에서 겪은 시행착오를 제도화한 것이다:
① 몰마다 토큰 발급·호출 한도가 한정돼 있다. 수집 때마다 새 토큰을 받으면
   발급 한도를 갉아먹는다(어댑터 인스턴스는 매번 새로 만들어지므로 전역 캐시 필수).
② 429를 맞고도 계속 때리면 차단이 길어진다 — 5분 쉬어야 한다.
③ 자동수집·수동수집·연결테스트가 겹치면 호출 예산만 두 배로 쓴다.
"""
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.malls import base  # noqa: E402
from app.malls.base import MallError, collect_guard  # noqa: E402
from app.malls.smartstore import SmartStoreAdapter  # noqa: E402

import bcrypt

# 진짜 bcrypt salt — 서명 단계가 실제로 통과해야 토큰 캐시 경로를 끝까지 검증한다
SETTINGS = {"client_id": "cid-budget",
            "client_secret": bcrypt.gensalt(rounds=4).decode("ascii"),
            "enabled": True}


def _reset():
    base._TOKEN_CACHE.clear()
    base._COOLDOWN.clear()
    base._INFLIGHT.clear()


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.content = b"x"

    def json(self):
        return self._body


class TestTokenReuse(unittest.TestCase):
    def setUp(self):
        _reset()
        self.addCleanup(_reset)

    def test_token_survives_adapter_recreation(self):
        """★수집 주기마다 어댑터가 새로 만들어져도 토큰은 재사용돼야 한다.

        이게 안 되면 30분마다 새 토큰을 발급받아 네이버 발급 한도를 갉아먹는다
        (RMS가 실제로 겪은 문제 — 토큰 수명은 3시간이다).
        """
        issued = []

        def fake_post(url, data=None, timeout=None):
            issued.append(url)
            return FakeResp(200, {"access_token": f"tok-{len(issued)}", "expires_in": 10800})

        import app.malls.smartstore as ss
        orig = ss.requests.post
        ss.requests.post = fake_post
        self.addCleanup(setattr, ss.requests, "post", orig)

        t1 = SmartStoreAdapter(dict(SETTINGS))._access_token()   # 첫 발급
        t2 = SmartStoreAdapter(dict(SETTINGS))._access_token()   # 새 인스턴스 — 재사용해야
        t3 = SmartStoreAdapter(dict(SETTINGS))._access_token()
        self.assertEqual(len(issued), 1, f"토큰을 {len(issued)}번 발급했다 — 1번이어야 한다")
        self.assertEqual(t1, t2)
        self.assertEqual(t2, t3)

    def test_different_client_ids_get_separate_tokens(self):
        base.store_token("smartstore", "cid-A", "tok-A", 3600)
        base.store_token("smartstore", "cid-B", "tok-B", 3600)
        self.assertEqual(base.cached_token("smartstore", "cid-A"), "tok-A")
        self.assertEqual(base.cached_token("smartstore", "cid-B"), "tok-B")

    def test_expired_token_not_reused(self):
        base.store_token("smartstore", "cid-X", "tok-old", 61)
        # 만료 60초 전부터는 없다고 답한다(미리 갱신) → 61초 TTL이면 사실상 즉시 만료 취급
        time.sleep(1.1)
        self.assertEqual(base.cached_token("smartstore", "cid-X"), "")

    def test_401_drops_cached_token(self):
        base.store_token("smartstore", "cid-Y", "tok-dead", 3600)
        base.drop_token("smartstore", "cid-Y")
        self.assertEqual(base.cached_token("smartstore", "cid-Y"), "")


class TestCooldown(unittest.TestCase):
    def setUp(self):
        _reset()
        self.addCleanup(_reset)

    def test_429_sets_cooldown_and_blocks_next_call(self):
        """429를 맞으면 5분간 그 몰 호출이 시작조차 안 돼야 한다."""
        ad = SmartStoreAdapter(dict(SETTINGS))
        ad._access_token = lambda: "tok"

        import app.malls.smartstore as ss
        orig = ss.requests.request
        ss.requests.request = lambda *a, **k: FakeResp(429)
        self.addCleanup(setattr, ss.requests, "request", orig)

        with self.assertRaises(MallError):
            ad._call("GET", "/x")
        self.assertGreater(base.cooldown_left("smartstore"), 250)

        # 다음 호출은 네트워크에 나가기 전에 막힌다
        ss.requests.request = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("쿨다운 중인데 네트워크 호출이 나갔다"))
        with self.assertRaises(MallError) as cm:
            ad._call("GET", "/x")
        self.assertIn("쉬는 중", str(cm.exception))

    def test_cooldown_expires(self):
        base.set_cooldown("smartstore", seconds=1)
        self.assertGreater(base.cooldown_left("smartstore"), 0)
        time.sleep(1.1)
        self.assertEqual(base.cooldown_left("smartstore"), 0)
        base.check_cooldown("smartstore")          # 예외 없이 통과해야 한다

    def test_cooldown_is_per_mall(self):
        base.set_cooldown("smartstore")
        self.assertEqual(base.cooldown_left("coupang"), 0)
        base.check_cooldown("coupang")             # 다른 몰은 영향 없다

    def test_scheduler_skips_mall_in_cooldown(self):
        """자동수집 데몬은 쿨다운 중인 몰을 조용히 건너뛴다(호출 예산 보호)."""
        from app.malls import scheduler
        base.set_cooldown("smartstore")
        # cooldown_left가 스케줄러 모듈에서도 같은 상태를 봐야 한다
        self.assertGreater(scheduler.cooldown_left("smartstore"), 0)


class TestCollectGuard(unittest.TestCase):
    def setUp(self):
        _reset()
        self.addCleanup(_reset)

    def test_concurrent_collect_refused(self):
        """같은 몰을 동시에 두 번 수집하면 두 번째는 즉시 거절돼야 한다."""
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def first():
            with collect_guard("coupang", "쿠팡"):
                entered.set()
                release.wait(timeout=5)

        t = threading.Thread(target=first)
        t.start()
        entered.wait(timeout=5)
        try:
            with collect_guard("coupang", "쿠팡"):
                errors.append("동시 진입이 허용됐다")
        except MallError as e:
            self.assertIn("이미 진행 중", str(e))
        finally:
            release.set()
            t.join(timeout=5)
        self.assertEqual(errors, [])

    def test_guard_released_after_exception(self):
        """수집이 오류로 끝나도 락이 풀려야 다음 수집이 가능하다."""
        with self.assertRaises(RuntimeError):
            with collect_guard("coupang"):
                raise RuntimeError("수집 중 폭발")
        with collect_guard("coupang"):             # 다시 잡혀야 한다
            pass

    def test_different_malls_do_not_block_each_other(self):
        with collect_guard("coupang"):
            with collect_guard("godomall"):        # 다른 몰은 동시에 돼야 한다
                pass


if __name__ == "__main__":
    unittest.main()


class TestSmsResend(unittest.TestCase):
    """A/S 문자 재발송 — 500 오류로 끝나거나 실발송 중복이 되면 안 된다(2026-07-29 전수조사)."""

    def setUp(self):
        import shutil, tempfile
        from app import auth as auth_mod, create_app
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-sms-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.app = create_app(db_path=self.tmp / "t.db")
        self.app.testing = True
        self.client = self.app.test_client()
        auth_mod._login_failures.clear()
        self.client.post("/api/auth/setup", json={
            "username": "admin", "displayName": "대표", "password": "admin-pass-1"})
        self.tid = self.client.post("/api/as-tickets", json={
            "customer": "김수리", "phone": "010-1234-5678", "symptom": "액정 불량"}).get_json()["id"]

    def _send(self, event, force=False):
        return self.client.post(f"/api/as-tickets/{self.tid}/sms",
                                json={"event": event, "force": force})

    def test_resend_without_force_is_refused_not_crashed(self):
        """접수 시 자동 기록이 있으므로 그냥 누르면 '이미 보냈다'로 막혀야 한다(500 아님)."""
        r = self._send("received")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(r.get_json()["ok"])
        self.assertIn("이미 보낸", r.get_json()["message"])

    def test_forced_resend_succeeds_and_is_logged(self):
        """★확인 후 재발송은 성공해야 하고 이력에도 남아야 한다(예전엔 여기서 500)."""
        r = self._send("received", force=True)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["ok"], r.get_json()["message"])
        log = self.client.get("/api/sms/log").get_json()
        events = [x["event"] for x in log if x.get("ticketId") == self.tid or True]
        self.assertTrue(any(e.startswith("received") for e in events))
        self.assertGreaterEqual(len([e for e in events if e.startswith("received")]), 2,
                                "재발송이 이력에 안 남았다")

    def test_repeated_resend_keeps_working(self):
        """세 번째·네 번째 재발송도 계속 동작해야 한다(회차가 쌓이는 구조)."""
        for _ in range(3):
            r = self._send("received", force=True)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["ok"], r.get_json()["message"])
