"""연동 창구 직접 반영(tms_link) — 가짜 창구로 반영·삭제 표시·복구·커서를 검증한다(네트워크·운영 DB 무접촉)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["OWS_NO_TMS_SYNC"] = "1"
# ★창구 주소·토큰은 setUp/tearDown 안에서만 켠다 — 모듈 수준에 두면 unittest discover 가 모든 시험 모듈을
#   먼저 import 하는 바람에 다른 모듈의 자산 등록(관리번호 검증, numbering.py)이 진짜 'http://fake' 로 나가
#   시험마다 7초씩 매달린다(2026-09-03 확인).

from app import create_app  # noqa: E402
from app.purchase import tms_link  # noqa: E402

PURCHASE_ROWS = [
    {"#": 1, "매입전표": "P260901-001", "관리번호": "260901-0001", "회사ID": "예시 운영사", "매입구분": "일반매입",
     "대분류": "PC", "모델명": "NT951XCJ", "브랜드": "삼성", "거래처명": "테스트거래처", "매입일": "2026-09-01",
     "매입가": 300000, "등급": "A", "CPU": "i5", "RAM": "16GB", "SSD": "512GB", "진행상태": "매입", "재고상태": "매입"},
    {"#": 2, "매입전표": "P260901-001", "관리번호": "260901-0002", "회사ID": "예시 운영사", "매입구분": "일반매입",
     "대분류": "PC", "모델명": "NT951XCJ", "브랜드": "삼성", "거래처명": "테스트거래처", "매입일": "2026-09-01",
     "매입가": 310000, "등급": "B", "CPU": "i7", "RAM": "16GB", "SSD": "1TB", "진행상태": "매입", "재고상태": "매입"},
]
SALE_ROWS = [
    {"#": 1, "판매전표": "S260902-001", "관리번호": "260901-0001", "수령자성함": "홍길동", "판매채널": "자사몰",
     "모델명": "NT951XCJ", "판매처명": "업무관리", "판매일": "2026-09-02", "매입가": 300000, "판매가": 450000,
     "순이익": 150000, "등급": "A", "진행상태": "판매", "매입전표": "P260901-001", "매입처명": "테스트거래처"},
]


class FakeClient:
    """창구 흉내 — 경로별 응답을 테스트가 바꿔 가며 준다."""
    stale = False
    screens = {}
    deleted = []
    restores = []
    calls = []

    def __init__(self, url, token):
        pass

    def get(self, path, **params):
        FakeClient.calls.append((path, params))
        if path == "/status":
            return {"ok": True, "status": {"freshness": {"stale": FakeClient.stale, "age_sec": 5}}}
        if path == "/screens":
            name = params["names"]
            rows = FakeClient.screens.get(name, [])
            return {"ok": True, "server_time": "2026-09-02 20:00:00",
                    "screens": {name: {"headers": list(rows[0].keys()) if rows else [], "count": len(rows), "items": rows}}}
        if path == "/deleted":
            return {"ok": True, "items": FakeClient.deleted}
        if path == "/restores":
            return {"ok": True, "items": FakeClient.restores}
        if path.startswith("/tables/") or path == "/partners":     # 마스터 연동(masters.py)은 여기선 빈 표 — tests/test_masters.py 가 다룬다
            return {"ok": True, "count": 0, "next_after_key": None, "items": []}
        raise AssertionError(path)


class TmsLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = create_app(db_path=self.tmp / "t.db")
        tms_link.STATE_FILE = self.tmp / "state.json"
        self._env = {k: os.environ.get(k) for k in ("OWS_DATALINK_URL", "OWS_DATALINK_TOKEN")}
        os.environ["OWS_DATALINK_URL"], os.environ["OWS_DATALINK_TOKEN"] = "http://fake", "t"
        self._client = tms_link.Client
        tms_link.Client = FakeClient
        FakeClient.stale, FakeClient.screens, FakeClient.deleted, FakeClient.restores, FakeClient.calls = False, {}, [], [], []

    def tearDown(self):
        tms_link.Client = self._client
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _q(self, sql, *args):
        from app.db import tx
        with self.app.app_context():
            with tx() as conn:
                return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def test_apply_creates_assets_and_sales_then_cursor(self):
        FakeClient.screens = {"매입현황": PURCHASE_ROWS, "판매현황": SALE_ROWS}
        res = tms_link.sync_once(self.app)
        self.assertEqual(res["screens"]["매입현황"]["created"], 2)
        assets = self._q("SELECT asset_no, purchase_price, grade, tms_deleted_at FROM assets ORDER BY asset_no")
        self.assertEqual([a["asset_no"] for a in assets], ["260901-0001", "260901-0002"])
        self.assertEqual(assets[0]["purchase_price"], 300000)
        self.assertEqual(self._q("SELECT COUNT(*) AS c FROM purchase_batches WHERE slip_no='P260901-001'")[0]["c"], 1)
        self.assertEqual(self._q("SELECT COUNT(*) AS c FROM tms_sales WHERE slip_no='S260902-001'")[0]["c"], 1)
        logs = self._q("SELECT filename FROM tms_sync_log")
        self.assertTrue(any(l["filename"] == "연동:매입현황" for l in logs))
        state = json.loads(tms_link.STATE_FILE.read_text("utf-8"))
        self.assertEqual(state["cursor"], "2026-09-02 20:00:00")
        # 두 번째 틱은 겹침(3분)을 둔 since 로 증분 요청한다
        tms_link.sync_once(self.app)
        since = [p.get("since") for path, p in FakeClient.calls if path == "/screens"][-1]
        self.assertEqual(since, "2026-09-02 19:57:00")
        # 같은 행을 다시 받아도 자산이 늘지 않는다(멱등)
        self.assertEqual(self._q("SELECT COUNT(*) AS c FROM assets")[0]["c"], 2)

    def test_sale_header_resync_runs_once_after_first_incremental_tick(self):
        """2026-09-03: 규칙 변경(안 고친 연동 전표는 TMS 값 추종) 전에 굳은 전표를 첫 증분 틱에 전량 한 번 다시 맞춘다."""
        head = {"#": 1, "판매전표": "S260831-001", "판매채널": "자사몰(업무관리)", "판매처명": "업무관리", "판매일": "2026-08-31",
                "수량": 6, "판매금액": 9042000, "진행상태": "판매"}
        FakeClient.screens = {"판매내역": [head]}
        tms_link.sync_once(self.app)                      # 첫 틱(전량) — 이 값으로 전표가 생긴다
        state = json.loads(tms_link.STATE_FILE.read_text("utf-8"))
        self.assertTrue(state.get("sale_head_resync"), "전량 틱은 그 자체로 맞으므로 마커만 찍는다")
        # 예전 규칙으로 굳어 있던 상태를 흉내 낸다 — 마커를 지우고 TMS 값은 바뀌었는데 증분엔 안 실려 온다
        state.pop("sale_head_resync"); tms_link.STATE_FILE.write_text(json.dumps(state), "utf-8")
        latest = dict(head, 수량=42, 판매금액=15017400)
        FakeClient.screens = {"판매내역": []}
        real_get = FakeClient.get

        def get(self_, path, **params):                    # since 없는 전량 요청에만 최신 헤더를 준다
            if path == "/screens" and params.get("since") is None and params["names"] == "판매내역":
                return {"ok": True, "server_time": "2026-09-02 20:00:00",
                        "screens": {"판매내역": {"headers": list(latest), "count": 1, "items": [latest]}}}
            return real_get(self_, path, **params)
        FakeClient.get = get
        try:
            res = tms_link.sync_once(self.app)
        finally:
            FakeClient.get = real_get
        self.assertEqual(res["saleHeadResync"]["판매내역"], {"rows": 1, "updated": 1})
        row = self._q("SELECT head_qty, sale_amount FROM sale_slips WHERE slip_no='S260831-001'")[0]
        self.assertEqual((row["head_qty"], row["sale_amount"]), (42, 15017400))
        self.assertTrue(json.loads(tms_link.STATE_FILE.read_text("utf-8"))["sale_head_resync"])
        # 세 번째 틱 — 다시 하지 않는다
        FakeClient.calls = []
        res = tms_link.sync_once(self.app)
        self.assertNotIn("saleHeadResync", res)
        self.assertFalse([p for path, p in FakeClient.calls if path == "/screens" and p.get("since") is None])


    def test_stale_mirror_applies_nothing(self):
        FakeClient.screens = {"매입현황": PURCHASE_ROWS}
        FakeClient.stale = True
        res = tms_link.sync_once(self.app)
        self.assertEqual(res, {"skipped": "stale"})
        self.assertEqual(self._q("SELECT COUNT(*) AS c FROM assets")[0]["c"], 0)

    def test_deletion_marks_and_restore_clears(self):
        FakeClient.screens = {"매입현황": PURCHASE_ROWS}
        tms_link.sync_once(self.app)
        FakeClient.screens = {}
        FakeClient.deleted = [{"키ID": 77, "관리번호": "260901-0002", "_deleted_at": "2026-09-02 19:59:00", "_restored_to": None}]
        res = tms_link.sync_once(self.app)
        self.assertEqual(res["deleted"], 1)
        a = self._q("SELECT id, tms_deleted_at, tms_deleted_note FROM assets WHERE asset_no='260901-0002'")[0]
        self.assertTrue(a["tms_deleted_at"])
        self.assertIn("사본 키ID 77", a["tms_deleted_note"])
        ev = self._q("SELECT action FROM asset_events WHERE asset_id=? ORDER BY id DESC LIMIT 1", a["id"])[0]
        self.assertEqual(ev["action"], "TMS삭제")
        self.assertEqual(self._q("SELECT COUNT(*) AS c FROM assets")[0]["c"], 2)   # 지우지 않는다
        FakeClient.deleted = []
        FakeClient.restores = [{"id": 5, "kind": "복구", "mgmt_no": "260901-0002",
                                "detail": {"변경구분": "재고", "변경타입": "삭제", "관리번호": "260901-0002", "재생성키ID": 99}}]
        res = tms_link.sync_once(self.app)
        self.assertEqual(res["restored"], 1)
        a = self._q("SELECT tms_deleted_at FROM assets WHERE asset_no='260901-0002'")[0]
        self.assertEqual(a["tms_deleted_at"], "")
        self.assertEqual(json.loads(tms_link.STATE_FILE.read_text("utf-8"))["restore_after"], 5)

    def test_disabled_without_config(self):
        os.environ["OWS_DATALINK_URL"] = ""
        try:
            self.assertEqual(tms_link.sync_once(self.app), {"skipped": "disabled"})
        finally:
            os.environ["OWS_DATALINK_URL"] = "http://fake"


if __name__ == "__main__":
    unittest.main()
