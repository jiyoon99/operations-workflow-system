"""창구 동시 마감 — 이중입력 경보(app/purchase/cutover.py). 가짜 창구로 변경 피드·원본 행·기준선을 흉내 낸다(네트워크·운영 DB 무접촉)."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["OWS_NO_TMS_SYNC"] = "1"

from app import auth as auth_mod  # noqa: E402
from app import create_app  # noqa: E402
from app.purchase import cutover, tms_link  # noqa: E402

PW = "admin-pass-1"
USER_PW = "worker-pass-1"
D = "2026-11-01"


def his(key, table, kind, tkey, at, n=1):
    return {"키ID": key, "테이블명": table, "변경구분": kind, "변경키ID": tkey, "변경키IDcs": str(tkey),
            "변경자ID": "7", "변경일시": at, "변경건수": n}


class FakeClient:
    """창구 흉내 — /changes 는 키ID 커서·limit 페이징, /tables 는 after_key 로 한 건, /status 는 his_cursor·invariants."""
    changes = []
    rows = {}                       # (table, 키ID) -> 원본 행
    his_cursor = 1000
    invariants = {"asset_no_ge_5000": 0, "slip_no_ge_500": 0, "checked_at": "2026-11-02 09:00:00"}
    calls = []
    fail_tables = False

    def __init__(self, url="", token=""):
        pass

    def get(self, path, **params):
        FakeClient.calls.append((path, params))
        if path == "/status":
            return {"ok": True, "status": {"freshness": {"stale": False, "age_sec": 5}, "his_cursor": str(FakeClient.his_cursor),
                                           "invariants": dict(FakeClient.invariants)}}
        if path == "/changes":
            after = int(params.get("after") or 0)
            limit = int(params.get("limit") or 500)
            tables = set((params.get("tables") or "").split(","))
            rows = [r for r in FakeClient.changes if r["키ID"] > after and r["테이블명"] in tables]
            page = rows[:limit]
            return {"ok": True, "count": len(page), "next_after": (page[-1]["키ID"] if len(rows) > limit else None), "items": page}
        if path.startswith("/tables/"):
            if FakeClient.fail_tables:
                raise RuntimeError("창구 오류 500")
            table = path.split("/", 2)[2]
            after_key = int(params.get("after_key") or 0)
            cands = sorted(k for (t, k) in FakeClient.rows if t == table and k > after_key)
            return {"ok": True, "items": [FakeClient.rows[(table, cands[0])]] if cands else []}
        if path == "/screens":
            name = params["names"]
            return {"ok": True, "server_time": "2026-11-02 09:00:00", "screens": {name: {"headers": [], "count": 0, "items": []}}}
        if path in ("/deleted", "/restores"):
            return {"ok": True, "items": []}
        return {"ok": True, "count": 0, "next_after_key": None, "items": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-cutover-"))
        self.db_path = self.tmp / "t.db"
        self.app = create_app(db_path=self.db_path)
        self.app.testing = True
        self.c = self.app.test_client()
        auth_mod._login_failures.clear()
        self.c.post("/api/auth/setup", json={"username": "admin", "displayName": "대표", "password": PW})
        tms_link.STATE_FILE = self.tmp / "state.json"
        self._env = {k: os.environ.get(k) for k in ("OWS_DATALINK_URL", "OWS_DATALINK_TOKEN")}
        os.environ["OWS_DATALINK_URL"], os.environ["OWS_DATALINK_TOKEN"] = "http://fake", "t"
        self._client = tms_link.Client
        tms_link.Client = FakeClient
        FakeClient.changes, FakeClient.rows, FakeClient.calls, FakeClient.fail_tables = [], {}, [], False
        FakeClient.his_cursor = 1000
        FakeClient.invariants = {"asset_no_ge_5000": 0, "slip_no_ge_500": 0, "checked_at": "2026-11-02 09:00:00"}
        cutover._last.clear()

    def tearDown(self):
        tms_link.Client = self._client
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sql(self, q, *args):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(q, args).fetchall()]
        finally:
            conn.close()

    def state(self):
        try:
            return json.loads(tms_link.STATE_FILE.read_text("utf-8"))
        except OSError:
            return {}

    def worker(self, perms, username="worker1"):
        r = self.c.post("/api/users", json={"username": username, "displayName": username, "password": USER_PW,
                                            "perms": perms, "categoryIds": [], "allCategories": True, "isAdmin": False})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        self.assertEqual(c.post("/api/auth/login", json={"username": username, "password": USER_PW}).status_code, 200)
        return c

    def set_date(self, date=D, **extra):
        r = self.c.post("/api/cutover", json={"date": date, **extra})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def scan(self):
        state = self.state()
        res = cutover.scan(self.app, FakeClient(), state)
        tms_link._write_state(state)
        return res

    def seed_feed(self):
        """마감(11-01) 전후의 변경 — 위반은 판매 1·매입 1·재고 1 뿐이어야 한다."""
        FakeClient.changes = [
            his(1001, "HB_TBL판매H", "추가", 5001, "2026-10-31 23:59:59"),       # 마감 전 — 아님
            his(1002, "HB_TBL판매H", "수정", 5001, "2026-11-01 09:00:00"),       # 수정 — 아님(입금 확인 등)
            his(1003, "HB_TBL판매H", "추가", 5002, "2026-11-01 10:00:00"),       # 위반(판매)
            his(1004, "HB_TBL매입H", "추가", 4001, "2026-11-02 11:00:00"),       # 위반(매입) — 임시매입 패턴
            his(1005, "HB_TBL재고", "추가", 31001, "2026-11-02 11:00:05"),       # 위반(재고) — 옛 전표에 붙음
            his(1006, "HB_TBL재고", "삭제", 31001, "2026-11-02 12:00:00"),       # 삭제 — 아님
            his(1007, "HB_TBL렌탈H", "추가", 900, "2026-11-02 12:30:00"),        # 대상 표 아님 — 창구가 tables= 로 걸러 준다(안 온다)
        ]
        FakeClient.rows = {
            ("HB_TBL판매H", 5002): {"키ID": 5002, "전표번호": "S261101-001", "거래처명": "업무관리판매", "수량": 3, "판매금액": 1500000.0,
                                   "판매자": "오주영"},
            ("HB_TBL매입H", 4001): {"키ID": 4001, "전표번호": "P261102-001", "거래처명": "개인", "수량": 1, "매입금액": 0.0, "매입자": "총괄관리자"},
            ("HB_TBL재고", 31001): {"키ID": 31001, "관리번호": "261102-0001", "매입전표번호": "P251202-001", "매입처명": "컴퓨존",
                                   "매입가": 25170.0, "매입자": "총괄관리자"},
        }


class TestScan(Base):
    def test_마감일이_없으면_경보를_만들지_않는다(self):
        self.seed_feed()
        res = self.scan()
        self.assertEqual(res["skipped"], "마감일 없음")
        self.assertFalse(res["active"])
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM tms_dual_entries")[0]["c"], 0)
        self.assertFalse([p for p, _ in FakeClient.calls if p == "/changes"], "마감일 없으면 변경 피드를 읽지 않는다")
        # 번호대 침범 수는 마감과 무관하게 받아 둔다
        self.assertEqual(res["invariants"]["asset_no_ge_5000"], 0)

    def test_마감일_이후_추가만_잡고_원본_행으로_채운다(self):
        self.seed_feed()
        self.set_date(D, baselineCursor=1000)
        res = self.scan()
        self.assertEqual((res["checked"], res["new"]), (6, 3))
        rows = self.sql("SELECT * FROM tms_dual_entries ORDER BY his_key")
        self.assertEqual([r["his_key"] for r in rows], [1003, 1004, 1005])
        sale, buy, stock = rows
        self.assertEqual((sale["kind"], sale["slip_no"], sale["party"], sale["qty"], sale["amount"], sale["actor"]),
                         ("판매", "S261101-001", "업무관리판매", 3, 1500000.0, "오주영"))
        self.assertEqual((buy["kind"], buy["slip_no"], buy["pattern"]), ("매입", "P261102-001", "임시매입 의심"))
        self.assertEqual((stock["kind"], stock["asset_no"], stock["slip_no"], stock["pattern"]),
                         ("재고", "261102-0001", "P251202-001", "옛 전표에 추가"))
        self.assertTrue(all(r["status"] == "open" for r in rows))
        self.assertEqual(self.state()[cutover.STATE_KEY], 1006, "커서는 마지막으로 본 HIS 키ID")

    def test_다시_점검해도_같은_변경은_한_번만_남고_커서부터_이어_읽는다(self):
        self.seed_feed()
        self.set_date(D, baselineCursor=1000)
        self.scan()
        FakeClient.calls = []
        res = self.scan()
        self.assertEqual(res["new"], 0)
        self.assertEqual([p["after"] for path, p in FakeClient.calls if path == "/changes"], [1006])
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM tms_dual_entries")[0]["c"], 3)
        # 새 위반이 오면 그것만 늘어난다
        FakeClient.changes.append(his(1008, "HB_TBL판매H", "추가", 5003, "2026-11-03 09:00:00"))
        FakeClient.rows[("HB_TBL판매H", 5003)] = {"키ID": 5003, "전표번호": "S261103-001", "거래처명": "방문", "수량": 1, "판매금액": 300000.0, "판매자": "김"}
        self.assertEqual(self.scan()["new"], 1)
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM tms_dual_entries")[0]["c"], 4)

    def test_페이지가_넘치면_next_after_로_이어_읽는다(self):
        self.set_date(D, baselineCursor=0)
        FakeClient.changes = [his(i, "HB_TBL판매H", "추가", 5000 + i, "2026-11-05 10:00:00") for i in range(1, 1201)]
        old_page = cutover.PAGE
        cutover.PAGE = 500
        try:
            res = self.scan()
        finally:
            cutover.PAGE = old_page
        self.assertEqual(res["new"], 1200)
        self.assertEqual(len([1 for p, _ in FakeClient.calls if p == "/changes"]), 3)
        self.assertEqual(self.state()[cutover.STATE_KEY], 1200)

    def test_원본_행을_못_읽어도_경보는_남는다(self):
        self.seed_feed()
        self.set_date(D, baselineCursor=1000)
        FakeClient.fail_tables = True
        res = self.scan()
        self.assertEqual(res["new"], 3)
        row = self.sql("SELECT slip_no, kind, tms_key, pattern FROM tms_dual_entries WHERE his_key=1003")[0]
        self.assertEqual((row["slip_no"], row["kind"], row["tms_key"], row["pattern"]), ("", "판매", 5002, "원본 없음"))

    def test_원본_행의_키ID가_다르면_남의_행으로_채우지_않는다(self):
        """창구는 키ID 동등 필터를 안 받는다 — after_key 로 읽은 다음 행이 다른 전표면 빈 값으로 둔다."""
        self.set_date(D, baselineCursor=0)
        FakeClient.changes = [his(1, "HB_TBL판매H", "추가", 5002, "2026-11-01 10:00:00")]
        FakeClient.rows = {("HB_TBL판매H", 5009): {"키ID": 5009, "전표번호": "S261109-001", "거래처명": "다른", "수량": 1, "판매금액": 1.0}}
        self.scan()
        self.assertEqual(self.sql("SELECT slip_no, pattern FROM tms_dual_entries")[0], {"slip_no": "", "pattern": "원본 없음"})

    def test_틱이_점검을_부르고_실패해도_틱은_산다(self):
        self.seed_feed()
        self.set_date(D, baselineCursor=1000)
        res = tms_link.sync_once(self.app)
        self.assertEqual(res["cutover"]["new"], 3)
        self.assertEqual(self.state()[cutover.STATE_KEY], 1006)
        self.assertEqual(self.state()["cursor"], "2026-11-02 09:00:00", "틱 자체의 커서도 같이 저장된다")
        # 점검이 터져도 틱은 끝까지 간다
        old = cutover.scan
        cutover.scan = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("점검 폭발"))
        try:
            res = tms_link.sync_once(self.app)
        finally:
            cutover.scan = old
        self.assertIn("점검 폭발", res["cutover"]["error"])
        self.assertEqual(self.state()["cursor"], "2026-11-02 09:00:00")


class TestRoutes(Base):
    def test_마감일_저장이_창구_기준선을_잡고_커서를_거기서_시작한다(self):
        FakeClient.his_cursor = 220573
        out = self.set_date(D)
        self.assertTrue(out["changed"])
        self.assertEqual(out["config"]["baselineCursor"], 220573)
        self.assertEqual(out["config"]["setBy"], "대표")
        self.assertEqual(self.state()[cutover.STATE_KEY], 220573)
        # 같은 날짜로 다시 저장하면 기준선은 그대로(커서 되감기 없음)
        FakeClient.his_cursor = 999999
        out = self.set_date(D, enabled=False)
        self.assertFalse(out["changed"])
        self.assertEqual(out["config"]["baselineCursor"], 220573)
        self.assertFalse(out["config"]["enabled"])
        self.assertFalse(self.c.get("/api/cutover").get_json()["active"])
        # 해제(빈 날짜)
        out = self.set_date("")
        self.assertEqual((out["config"]["date"], out["config"]["baselineCursor"]), ("", 0))

    def test_날짜_형식_검사(self):
        self.assertEqual(self.c.post("/api/cutover", json={"date": "2026/11/01"}).status_code, 400)
        self.assertEqual(self.c.post("/api/cutover", json={"date": "2026-13-01"}).status_code, 400)

    def test_창구가_죽어_있으면_기준선을_못_잡고_502(self):
        def boom(self, path, **p):
            raise RuntimeError("connection refused")
        old = FakeClient.get
        FakeClient.get = boom
        try:
            r = self.c.post("/api/cutover", json={"date": D})
        finally:
            FakeClient.get = old
        self.assertEqual(r.status_code, 502)
        self.assertEqual(self.c.get("/api/cutover").get_json()["config"]["date"], "", "저장되지 않는다")

    def test_권한(self):
        viewer = self.worker(["purchase.view"], "viewer")
        editor = self.worker(["purchase.view", "purchase.edit"], "editor")
        self.assertEqual(viewer.get("/api/cutover").status_code, 200)
        self.assertEqual(viewer.get("/api/cutover/alerts").status_code, 200)
        self.assertEqual(viewer.post("/api/cutover", json={"date": D}).status_code, 403)
        self.assertEqual(editor.post("/api/cutover", json={"date": D}).status_code, 403, "마감일은 설정 관리 권한")
        self.assertEqual(viewer.post("/api/cutover/scan", json={}).status_code, 403)
        self.assertEqual(editor.post("/api/cutover/scan", json={}).status_code, 200)
        self.assertEqual(viewer.post("/api/cutover/entries/1/status", json={"status": "ok"}).status_code, 403)

    def test_지금_점검과_목록과_판정(self):
        self.seed_feed()
        self.set_date(D, baselineCursor=1000)
        r = self.c.post("/api/cutover/scan", json={})
        self.assertEqual(r.get_json()["result"]["new"], 3)
        d = self.c.get("/api/cutover").get_json()
        self.assertEqual(d["open"], {"판매": 1, "매입": 1, "재고": 1, "total": 3})
        self.assertEqual([i["kind"] for i in d["items"]], ["재고", "매입", "판매"], "변경일시 내림차순")
        self.assertEqual(d["items"][0]["assetNo"], "261102-0001")
        self.assertEqual(d["items"][0]["statusLabel"], "확인 필요")
        eid = [i for i in d["items"] if i["kind"] == "판매"][0]["id"]
        r = self.c.post(f"/api/cutover/entries/{eid}/status", json={"status": "reentered", "note": "OWS S261101-500 으로 입력"})
        self.assertEqual(r.status_code, 200)
        it = r.get_json()["item"]
        self.assertEqual((it["status"], it["resolvedBy"], it["note"]), ("reentered", "대표", "OWS S261101-500 으로 입력"))
        self.assertEqual(self.c.get("/api/cutover/alerts").get_json()["open"], {"판매": 0, "매입": 1, "재고": 1, "total": 2})
        self.assertEqual(len(self.c.get("/api/cutover").get_json()["items"]), 2, "기본은 미처리만")
        self.assertEqual(len(self.c.get("/api/cutover?all=1").get_json()["items"]), 3)
        # 다시 열기
        r = self.c.post(f"/api/cutover/entries/{eid}/status", json={"status": "open"})
        self.assertEqual((r.get_json()["item"]["status"], r.get_json()["item"]["resolvedBy"]), ("open", ""))
        self.assertEqual(self.c.post(f"/api/cutover/entries/{eid}/status", json={"status": "weird"}).status_code, 400)
        self.assertEqual(self.c.post("/api/cutover/entries/9999/status", json={"status": "ok"}).status_code, 404)
        acts = self.sql("SELECT action FROM audit_log WHERE action LIKE 'cutover%' ORDER BY id")
        self.assertEqual([a["action"] for a in acts], ["cutover_set", "cutover_entry", "cutover_entry"])

    def test_오늘_할_일_요약에_번호대_침범이_실린다(self):
        FakeClient.invariants = {"asset_no_ge_5000": 2, "slip_no_ge_500": 1, "checked_at": "2026-11-02 09:00:00"}
        self.scan()                                              # 마감일 없이도 침범 수는 받아 둔다
        a = self.c.get("/api/cutover/alerts").get_json()
        self.assertFalse(a["active"])
        self.assertEqual(a["invariants"], {"assetNo": 2, "slipNo": 1, "checkedAt": "2026-11-02 09:00:00"})
        self.assertEqual(a["open"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
