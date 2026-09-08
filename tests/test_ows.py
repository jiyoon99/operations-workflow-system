"""OWS Phase 0 테스트 — 인증/전역 게이트/권한/사용자 관리/카테고리/감사로그/백업/설정 마스킹."""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth as auth_mod  # noqa: E402
from app import create_app  # noqa: E402
from app.settings import _MASK as MASK  # noqa: E402

ADMIN_PW = "admin-pass-1"
USER_PW = "user-pass-12"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-test-"))
        self.db_path = self.tmp / "test.db"
        self.app = create_app(db_path=self.db_path)
        self.app.testing = True
        self.client = self.app.test_client()
        auth_mod._login_failures.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def setup_admin(self):
        r = self.client.post("/api/auth/setup", json={
            "username": "admin", "displayName": "대표", "password": ADMIN_PW,
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def create_user(self, username="worker1", perms=None, category_ids=None,
                    all_categories=False, is_admin=False):
        r = self.client.post("/api/users", json={
            "username": username, "displayName": username, "password": USER_PW,
            "perms": perms or [], "categoryIds": category_ids or [],
            "allCategories": all_categories, "isAdmin": is_admin,
        })
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()

    def login_as(self, username, password=USER_PW):
        c = self.app.test_client()
        r = c.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return c

    def db(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn


class TestAuth(Base):
    def test_bootstrap_setup_flow(self):
        r = self.client.get("/api/auth/bootstrap")
        self.assertTrue(r.get_json()["needsSetup"])
        payload = self.setup_admin()
        self.assertTrue(payload["isAdmin"])
        self.assertFalse(self.client.get("/api/auth/bootstrap").get_json()["needsSetup"])
        # setup은 1회만
        r = self.client.post("/api/auth/setup", json={
            "username": "again", "password": "whatever-123",
        })
        self.assertEqual(r.status_code, 409)
        # 세션 유지 확인
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.get_json()["username"], "admin")

    def test_global_gate_blocks_unauth(self):
        self.setup_admin()
        anon = self.app.test_client()
        for path in ("/api/users", "/api/categories", "/api/audit", "/api/settings", "/api/backups"):
            r = anon.get(path)
            self.assertEqual(r.status_code, 401, path)
        health = anon.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.get_json()["ok"])
        self.assertTrue(health.get_json()["database"])

    def test_login_lockout(self):
        self.setup_admin()
        anon = self.app.test_client()
        for _ in range(5):
            r = anon.post("/api/auth/login", json={"username": "admin", "password": "wrong-pass"})
            self.assertEqual(r.status_code, 401)
        r = anon.post("/api/auth/login", json={"username": "admin", "password": "wrong-pass"})
        self.assertEqual(r.status_code, 429)
        # 차단은 올바른 비밀번호도 막는다
        r = anon.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
        self.assertEqual(r.status_code, 429)

    def test_session_persists_across_app_restart(self):
        """세션은 DB 영속 — 서버 재시작(새 앱 인스턴스) 후에도 로그인 유지."""
        self.setup_admin()
        cookie = self.client.get_cookie("ows_session")
        self.assertIsNotNone(cookie)
        app2 = create_app(db_path=self.db_path)
        app2.testing = True
        c2 = app2.test_client()
        c2.set_cookie("ows_session", cookie.value)
        r = c2.get("/api/auth/me")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["username"], "admin")

    def test_change_password(self):
        self.setup_admin()
        r = self.client.patch("/api/auth/password", json={
            "currentPassword": "wrong", "newPassword": "new-pass-123",
        })
        self.assertEqual(r.status_code, 403)
        r = self.client.patch("/api/auth/password", json={
            "currentPassword": ADMIN_PW, "newPassword": "new-pass-123",
        })
        self.assertEqual(r.status_code, 200)
        c = self.login_as("admin", "new-pass-123")
        self.assertEqual(c.get("/api/auth/me").status_code, 200)


class TestMenuPermissions(Base):
    """메뉴(카테고리) 단위 접근 권한 — 대표 요구: 매입·주문만 켜면 배송/AS/셋팅은 안 보인다."""

    def test_registry_is_menu_shaped(self):
        self.setup_admin()
        reg = self.client.get("/api/perm-registry").get_json()
        self.assertIn("menus", reg)
        names = {m["menu"]: m for m in reg["menus"]}
        for expected in ("purchase", "orders", "setup", "shipping", "as", "settings"):
            self.assertIn(expected, names)
        self.assertEqual(names["purchase"]["label"], "매입")
        self.assertTrue(all("view" in m and "perms" in m for m in reg["menus"]))

    def test_menus_reflect_granted_perms(self):
        self.setup_admin()
        # 매입 + 주문만 부여
        u = self.create_user("mdpark", perms=["purchase.view", "purchase.edit", "orders.view"])
        c = self.login_as("mdpark")
        me = c.get("/api/auth/me").get_json()
        self.assertIn("purchase", me["menus"])
        self.assertIn("orders", me["menus"])
        # 셋팅/배송은 orders.view만으로도 열리면 안 된다 — 각 메뉴 전용 권한이 필요
        self.assertNotIn("as", me["menus"])
        self.assertNotIn("settings", me["menus"])
        # 실제 API도 막힌다
        self.assertEqual(c.get("/api/users").status_code, 403)
        self.assertEqual(c.get("/api/settings").status_code, 403)
        self.assertEqual(c.get("/api/assets").status_code, 200)

    def test_admin_sees_all_menus(self):
        payload = self.setup_admin()
        self.assertTrue(payload["isAdmin"])
        me = self.client.get("/api/auth/me").get_json()
        for expected in ("purchase", "orders", "setup", "shipping", "as", "settings"):
            self.assertIn(expected, me["menus"])

    def test_setup_menu_requires_work_perm(self):
        self.setup_admin()
        self.create_user("qcman", perms=["setup.view", "orders.work"])
        c = self.login_as("qcman")
        me = c.get("/api/auth/me").get_json()
        self.assertIn("setup", me["menus"])
        # 셋팅만 준 사람에게 주문관리·배송 메뉴가 열리면 안 된다
        self.assertNotIn("orders", me["menus"])
        self.assertNotIn("shipping", me["menus"])
        # 셋팅 화면은 주문 데이터를 읽어야 하므로 조회는 가능
        self.assertEqual(c.get("/api/orders").status_code, 200)

    def test_each_menu_is_independent(self):
        """주문관리 권한만 준 사용자에게 셋팅·배송이 열리면 안 된다(2026-07-28 대표 지적)."""
        self.setup_admin()
        self.create_user("jws", perms=["purchase.view", "purchase.edit", "orders.view", "orders.edit"])
        c = self.login_as("jws")
        me = c.get("/api/auth/me").get_json()
        self.assertEqual(sorted(me["menus"]), ["orders", "purchase"])
        for closed in ("setup", "shipping", "as", "settings"):
            self.assertNotIn(closed, me["menus"])
        # 배송 전용 API는 막힌다
        self.assertEqual(c.get("/api/waybills").status_code, 403)


class TestPermissions(Base):
    def test_feature_perm_gate(self):
        self.setup_admin()
        u = self.create_user("worker1", perms=["orders.view"])
        c = self.login_as("worker1")
        self.assertEqual(c.get("/api/users").status_code, 403)
        self.assertEqual(c.get("/api/audit").status_code, 403)
        self.assertEqual(c.get("/api/categories").status_code, 200)
        # 권한 부여 후 접근 가능
        r = self.client.put(f"/api/users/{u['id']}/perms", json={"perms": ["orders.view", "audit.view"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/api/audit").status_code, 200)

    def test_non_admin_cannot_escalate(self):
        self.setup_admin()
        self.create_user("mgr", perms=["users.manage"])
        c = self.login_as("mgr")
        # 관리자 생성 시도 → 403
        r = c.post("/api/users", json={
            "username": "evil", "password": "evil-pass-123", "isAdmin": True,
        })
        self.assertEqual(r.status_code, 403)
        # 관리자 계정 수정 시도 → 403
        r = c.patch("/api/users/1", json={"displayName": "탈취"})
        self.assertEqual(r.status_code, 403)
        r = c.put("/api/users/1/perms", json={"perms": []})
        self.assertEqual(r.status_code, 403)
        # isAdmin 플래그 지정 시도 → 403
        u = self.create_user("victim", perms=[])
        r = c.patch(f"/api/users/{u['id']}", json={"isAdmin": True})
        self.assertEqual(r.status_code, 403)

    def test_last_admin_protected(self):
        self.setup_admin()
        r = self.client.patch("/api/users/1", json={"enabled": False})
        self.assertEqual(r.status_code, 400)
        r = self.client.patch("/api/users/1", json={"isAdmin": False})
        self.assertEqual(r.status_code, 400)
        # 관리자가 2명이면 강등 가능
        self.create_user("admin2", is_admin=True)
        r = self.client.patch("/api/users/1", json={"isAdmin": False})
        self.assertEqual(r.status_code, 200)


class TestUsers(Base):
    def test_create_validation(self):
        self.setup_admin()
        r = self.client.post("/api/users", json={"username": "ab", "password": USER_PW})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/users", json={"username": "abc", "password": "short"})
        self.assertEqual(r.status_code, 400)
        self.create_user("dupuser")
        r = self.client.post("/api/users", json={"username": "DupUser", "password": USER_PW})
        self.assertEqual(r.status_code, 409)  # 대소문자 무시 중복
        r = self.client.post("/api/users", json={
            "username": "badperm", "password": USER_PW, "perms": ["no.such.perm"],
        })
        self.assertEqual(r.status_code, 400)

    def test_disable_kills_session(self):
        self.setup_admin()
        u = self.create_user("temp1")
        c = self.login_as("temp1")
        self.assertEqual(c.get("/api/auth/me").status_code, 200)
        self.client.patch(f"/api/users/{u['id']}", json={"enabled": False})
        self.assertEqual(c.get("/api/auth/me").status_code, 401)

    def test_password_reset_kills_session(self):
        self.setup_admin()
        u = self.create_user("temp2")
        c = self.login_as("temp2")
        self.client.patch(f"/api/users/{u['id']}", json={"password": "changed-pass-1"})
        self.assertEqual(c.get("/api/auth/me").status_code, 401)
        c2 = self.login_as("temp2", "changed-pass-1")
        self.assertEqual(c2.get("/api/auth/me").status_code, 200)


class TestCategories(Base):
    def test_seed_and_crud(self):
        self.setup_admin()
        cats = self.client.get("/api/categories").get_json()
        # 카테고리 = TMS 중분류 8종 축(2026-09-03, A5). 첫 항목 노트북이 sort 0 = 기본값
        self.assertEqual([c["name"] for c in cats],
                         ["노트북", "데스크탑", "태블릿", "모니터", "미니PC", "일체형PC", "주변기기", "웨어러블"])
        r = self.client.post("/api/categories", json={"name": "프린터"})
        self.assertEqual(r.status_code, 201)
        r = self.client.post("/api/categories", json={"name": "프린터"})  # 중복
        self.assertEqual(r.status_code, 409)
        r = self.client.post("/api/categories", json={"name": "태블릿"})  # 시드와 중복
        self.assertEqual(r.status_code, 409)
        cid = self.client.get("/api/categories").get_json()[-1]["id"]
        r = self.client.patch(f"/api/categories/{cid}", json={"name": "프린터/복합기", "enabled": False})
        self.assertEqual(r.status_code, 200)

    def test_user_category_scope(self):
        self.setup_admin()
        cats = self.client.get("/api/categories").get_json()
        u = self.create_user("scoped", category_ids=[cats[0]["id"]])
        got = self.client.get("/api/users").get_json()
        target = next(x for x in got if x["username"] == "scoped")
        self.assertEqual(target["categoryIds"], [cats[0]["id"]])
        # 존재하지 않는 카테고리 → 400
        r = self.client.put(f"/api/users/{u['id']}/categories",
                            json={"allCategories": False, "categoryIds": [9999]})
        self.assertEqual(r.status_code, 400)
        # 전체 접근 전환
        r = self.client.put(f"/api/users/{u['id']}/categories",
                            json={"allCategories": True, "categoryIds": []})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["allCategories"])


class TestAuditSettingsBackup(Base):
    def test_audit_written(self):
        self.setup_admin()
        rows = self.client.get("/api/audit").get_json()
        actions = [r["action"] for r in rows]
        self.assertIn("setup", actions)
        # 실패 로그인도 기록
        anon = self.app.test_client()
        anon.post("/api/auth/login", json={"username": "admin", "password": "nope-nope"})
        rows = self.client.get("/api/audit?action=login_failed").get_json()
        self.assertTrue(rows)

    def test_settings_secret_masking(self):
        self.setup_admin()
        r = self.client.put("/api/settings", json={
            "cj": {"cust_id": "00000000", "biz_reg_num": "1234567890", "env": "dev"},
        })
        self.assertEqual(r.status_code, 200)
        got = self.client.get("/api/settings").get_json()
        self.assertEqual(got["cj"]["cust_id"], "00000000")
        self.assertEqual(got["cj"]["env"], "dev")
        self.assertEqual(got["cj"]["biz_reg_num"], MASK)
        # 마스크 값을 그대로 되돌려보내면 원본 유지
        r = self.client.put("/api/settings", json={
            "cj": {"cust_id": "00000000", "biz_reg_num": MASK, "env": "prod"},
        })
        self.assertEqual(r.status_code, 200)
        row = self.db().execute("SELECT value FROM settings WHERE key='cj'").fetchone()
        stored = json.loads(row["value"])
        self.assertEqual(stored["biz_reg_num"], "1234567890")
        self.assertEqual(stored["env"], "prod")

    def test_backups(self):
        self.setup_admin()  # 쓰기 발생 → 일별 백업 생성
        bdir = self.db_path.parent / "backups"
        self.assertTrue(any(bdir.glob("ows-*.db")))
        r = self.client.post("/api/backups/run")
        self.assertEqual(r.status_code, 200)
        listed = self.client.get("/api/backups").get_json()["backups"]
        self.assertTrue(any(b["name"] == r.get_json()["name"] for b in listed))


class TestReviewFixes(Base):
    """2026-07-28 적대적 리뷰에서 확인된 결함들의 회귀 테스트."""

    def test_settings_list_secret_roundtrip(self):
        """리스트 내부 시크릿도 마스크 왕복 시 원본이 유지되어야 한다."""
        self.setup_admin()
        self.client.put("/api/settings", json={
            "malls": {"accounts": [{"name": "고도몰", "api_key": "REAL-KEY-1"},
                                    {"name": "쿠팡", "api_key": "REAL-KEY-2"}]},
        })
        got = self.client.get("/api/settings").get_json()
        self.assertEqual(got["malls"]["accounts"][0]["api_key"], MASK)
        # 마스킹된 응답을 그대로 다시 PUT (설정 화면의 표준 저장 패턴)
        r = self.client.put("/api/settings", json=got)
        self.assertEqual(r.status_code, 200)
        stored = json.loads(self.db().execute(
            "SELECT value FROM settings WHERE key='malls'").fetchone()["value"])
        self.assertEqual(stored["accounts"][0]["api_key"], "REAL-KEY-1")
        self.assertEqual(stored["accounts"][1]["api_key"], "REAL-KEY-2")

    def test_create_user_invalid_category_returns_400(self):
        self.setup_admin()
        r = self.client.post("/api/users", json={
            "username": "badcat", "password": USER_PW, "categoryIds": [9999],
        })
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/users", json={
            "username": "badcat", "password": USER_PW, "categoryIds": ["abc"],
        })
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/users", json={
            "username": "badcat", "password": USER_PW, "perms": "orders.view",
        })
        self.assertEqual(r.status_code, 400)

    def test_update_category_invalid_sort_returns_400(self):
        self.setup_admin()
        cid = self.client.get("/api/categories").get_json()[0]["id"]
        r = self.client.patch(f"/api/categories/{cid}", json={"sort": "abc"})
        self.assertEqual(r.status_code, 400)

    def test_categories_reorder_atomic(self):
        self.setup_admin()
        ids = [c["id"] for c in self.client.get("/api/categories").get_json()]
        rev = list(reversed(ids))
        r = self.client.post("/api/categories/reorder", json={"ids": rev})
        self.assertEqual(r.status_code, 200)
        got = [c["id"] for c in self.client.get("/api/categories").get_json()]
        self.assertEqual(got, rev)
        # 부분 목록/중복은 거부
        self.assertEqual(self.client.post("/api/categories/reorder",
                                          json={"ids": rev[:2]}).status_code, 400)
        self.assertEqual(self.client.post("/api/categories/reorder",
                                          json={"ids": rev[:1] * len(rev)}).status_code, 400)

    def test_change_password_invalidates_other_sessions(self):
        self.setup_admin()
        self.create_user("dual")
        c1 = self.login_as("dual")
        c2 = self.login_as("dual")
        r = c1.patch("/api/auth/password", json={
            "currentPassword": USER_PW, "newPassword": "brand-new-pw-1",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c1.get("/api/auth/me").status_code, 200)  # 본인 세션 유지
        self.assertEqual(c2.get("/api/auth/me").status_code, 401)  # 다른 세션 무효화

    def test_change_password_bruteforce_lockout(self):
        self.setup_admin()
        self.create_user("bruter")
        c = self.login_as("bruter")
        for _ in range(5):
            r = c.patch("/api/auth/password", json={
                "currentPassword": "wrong-pass!", "newPassword": "whatever-123",
            })
            self.assertEqual(r.status_code, 403)
        r = c.patch("/api/auth/password", json={
            "currentPassword": "wrong-pass!", "newPassword": "whatever-123",
        })
        self.assertEqual(r.status_code, 429)
        rows = self.client.get("/api/audit?action=password_change_failed").get_json()
        self.assertTrue(rows)


if __name__ == "__main__":
    unittest.main()
