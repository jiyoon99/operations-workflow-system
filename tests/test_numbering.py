"""관리번호 규칙(2026-09-03, A4) + 자산 수정 전값 기록(A7).

  - 손입력 번호: 가짜 창구 verdict 별 거부/허용(없음·삭제됨·중복 → 400, ok → 통과, 검증불가·미설정 → 허용 + '번호 미확인')
  - 번호대: TMS 대역(<5000)은 사본에 있어야, OWS 대역(≥5000)은 사본에 없어야 한다
  - 채번: 5000부터 순번, 비워진 번호 재사용 금지, 오늘 사본에 5000번대가 살아 있으면 409 보류
  - 브릿지 POST /api/bridge/next-asset-no: 토큰 없음/틀림 401, 맞으면 200
  - A7: 두 칸 수정 → 이벤트 1건에 두 칸의 전후값, 같은 값 저장 → 기록 없음, 일괄 수정도 전후값
★임시 DB만 쓴다 — 운영·dev DB(data/*.db)에는 절대 붙지 않는다. 창구는 가짜 클라이언트(네트워크 무접촉).
"""
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OWS_NO_TMS_SYNC"] = "1"

from app import auth as auth_mod  # noqa: E402
from app import config, create_app  # noqa: E402
from app.purchase import OWS_ASSET_SEQ_START as START  # noqa: E402
from app.purchase import next_asset_no, numbering, tms_link  # noqa: E402

PW = "admin-pass-1"
USER_PW = "user-pass-12"


class FakeClient:
    """창구 흉내 — mgmt-check 는 verdicts 표를, asset-facts 는 facts 행을 돌려준다."""
    verdicts = {}            # 번호 → verdict(없으면 '없음')
    facts = []               # /asset-facts 행(관리번호·_deleted_at)
    stale = False
    fail = False
    calls = []

    def __init__(self, url="", token=""):
        pass

    def get(self, path, **params):
        FakeClient.calls.append((path, params))
        if FakeClient.fail:
            raise RuntimeError("연동 창구 오류 500")
        fr = {"stale": FakeClient.stale, "age_sec": 5}
        if path == "/mgmt-check":
            nos = params["nos"].split(",")
            return {"ok": True, "freshness": fr,
                    "result": {n: {"verdict": "검증불가" if FakeClient.stale else FakeClient.verdicts.get(n, "없음")}
                               for n in nos}}
        if path == "/asset-facts":
            return {"ok": True, "count": len(FakeClient.facts), "next_after_key": None,
                    "freshness": fr, "items": FakeClient.facts}
        raise AssertionError(path)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-num-"))
        self.db_path = self.tmp / "t.db"
        self.app = create_app(db_path=self.db_path)
        self.app.testing = True
        self.c = self.app.test_client()
        auth_mod._login_failures.clear()
        self.c.post("/api/auth/setup", json={"username": "admin", "displayName": "대표", "password": PW})
        cats = self.c.get("/api/categories").get_json()
        self.cat, self.cat2 = cats[0], cats[1]
        self.sid = self.c.post("/api/suppliers", json={"name": "시험거래처"}).get_json()["id"]
        self._env = {k: os.environ.get(k) for k in ("OWS_DATALINK_URL", "OWS_DATALINK_TOKEN")}
        os.environ["OWS_DATALINK_URL"], os.environ["OWS_DATALINK_TOKEN"] = "http://fake", "t"
        self._client = tms_link.Client
        tms_link.Client = FakeClient
        FakeClient.verdicts, FakeClient.facts, FakeClient.calls = {}, [], []
        FakeClient.stale = FakeClient.fail = False
        self.today = config.now().strftime("%y%m%d")

    def tearDown(self):
        tms_link.Client = self._client
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def disable_link(self):
        os.environ["OWS_DATALINK_URL"] = ""

    def register(self, **kw):
        body = {"categoryId": self.cat["id"], "qty": 1, "model": "L480", "maker": "삼성", "ram": "8GB",
                "purchasePrice": 100000}
        body.update(kw)
        return self.c.post("/api/assets", json=body)

    def sql(self, q, *args):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(q, args).fetchall()]
            conn.commit()
            return rows
        finally:
            conn.close()

    def events(self, aid):
        return [(r["action"], json.loads(r["detail"]) if r["detail"] else None) for r in self.sql(
            "SELECT action, detail FROM asset_events WHERE asset_id=? ORDER BY id", aid)]

    def checks(self):
        return [p for path, p in FakeClient.calls if path == "/mgmt-check"]

    def worker(self, perms, username="worker"):
        r = self.c.post("/api/users", json={"username": username, "displayName": username, "password": USER_PW,
                                            "perms": perms, "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        n = self.app.test_client()
        self.assertEqual(n.post("/api/auth/login", json={"username": username, "password": USER_PW}).status_code, 200)
        return n


# ══════════════════════════════════════════════════════════════ 손입력 — TMS 대역(<5000)
class TestManualTmsBand(Base):
    def test_사본에_없는_번호는_거부(self):
        r = self.register(assetNo="260901-0001")
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("TMS 자산 원장에 없는 번호입니다 (오타 확인)", r.get_json()["error"])
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM assets")[0]["c"], 0)

    def test_삭제된_번호는_거부(self):
        FakeClient.verdicts = {"260901-0002": "삭제됨"}
        r = self.register(assetNo="260901-0002")
        self.assertEqual(r.status_code, 400)
        self.assertIn("TMS에서 삭제된 번호입니다", r.get_json()["error"])

    def test_둘_이상인_번호는_거부(self):
        FakeClient.verdicts = {"260901-0003": "중복"}
        r = self.register(assetNo="260901-0003")
        self.assertEqual(r.status_code, 400)
        self.assertIn("둘 이상 있어 확인이 필요합니다", r.get_json()["error"])

    def test_사본에_있는_번호는_통과하고_미확인_기록이_없다(self):
        FakeClient.verdicts = {"260901-0004": "ok"}
        r = self.register(assetNo="260901-0004")
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        aid = r.get_json()[0]["id"]
        self.assertEqual([a for a, _ in self.events(aid)], ["등록"])

    def test_사본이_낡으면_허용하고_미확인으로_남긴다(self):
        FakeClient.stale = True
        r = self.register(assetNo="260901-0005")
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        evs = self.events(r.get_json()[0]["id"])
        self.assertEqual([a for a, _ in evs], ["등록", "번호 미확인"])
        self.assertEqual(evs[1][1]["관리번호"], "260901-0005")
        self.assertIn("검증불가", evs[1][1]["사유"])

    def test_창구_미설정이면_허용하고_미확인으로_남긴다(self):
        self.disable_link()
        r = self.register(assetNo="260901-0006")
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        evs = self.events(r.get_json()[0]["id"])
        self.assertEqual(evs[1][0], "번호 미확인")
        self.assertEqual(evs[1][1]["사유"], "연동 창구 미설정")
        self.assertEqual(FakeClient.calls, [], "창구가 없는데 뭔가 불렀다")

    def test_창구_오류도_등록을_막지_않는다(self):
        FakeClient.fail = True
        r = self.register(assetNo="260901-0007")
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertEqual(self.events(r.get_json()[0]["id"])[1][0], "번호 미확인")

    def test_형식이_아닌_번호는_예전_그대로(self):
        r = self.register(assetNo="TMS-2023-0777")
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertEqual(self.checks(), [], "형식 밖 번호를 창구에 물었다")
        self.assertEqual([a for a, _ in self.events(r.get_json()[0]["id"])], ["등록"])

    def test_전표_동시저장은_창구를_한_번만_부른다(self):
        FakeClient.verdicts = {"260901-0011": "ok", "260901-0012": "ok", "260901-0013": "ok"}
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-09-03", "supplierId": self.sid, "totalAmount": 0,
            "assets": [{"categoryId": self.cat["id"], "qty": 1, "model": "L480", "assetNo": f"260901-00{n}"}
                       for n in (11, 12, 13)]})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["assetCount"], 3)
        calls = self.checks()
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(sorted(calls[0]["nos"].split(",")), ["260901-0011", "260901-0012", "260901-0013"])

    def test_전표_동시저장에서_한_줄이_틀리면_전표째_취소된다(self):
        FakeClient.verdicts = {"260901-0021": "ok"}          # 0022 는 사본에 없음
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-09-03", "supplierId": self.sid, "totalAmount": 0,
            "assets": [{"categoryId": self.cat["id"], "qty": 1, "assetNo": "260901-0021"},
                       {"categoryId": self.cat["id"], "qty": 1, "assetNo": "260901-0022"}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("2번째 줄", r.get_json()["error"])
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM purchase_batches")[0]["c"], 0)
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM assets")[0]["c"], 0)


# ══════════════════════════════════════════════════════════════ 손입력 — OWS 대역(≥5000)
class TestManualOwsBand(Base):
    def test_사본에_있는_5000번대는_번호대_충돌로_거부(self):
        FakeClient.verdicts = {"260901-5001": "ok"}
        r = self.register(assetNo="260901-5001")
        self.assertEqual(r.status_code, 400)
        self.assertIn("번호대 충돌", r.get_json()["error"])
        FakeClient.verdicts = {"260901-5002": "삭제됨"}      # 지워졌어도 TMS 에 있었던 번호다
        self.assertEqual(self.register(assetNo="260901-5002").status_code, 400)

    def test_사본에_없는_5000번대는_통과한다(self):
        r = self.register(assetNo="260901-5003")
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertEqual([a for a, _ in self.events(r.get_json()[0]["id"])], ["등록"])

    def test_창구가_없을_때_OWS가_채번한_번호는_미확인이_아니다(self):
        """[자동 채번]으로 받은 번호는 우리 번호다 — 대조를 못 했어도 '번호 미확인'을 남기지 않는다.
        손으로 지어낸 5000번대는 남긴다(TMS 쪽 번호대 침범을 못 본 것이므로)."""
        self.disable_link()
        issued = self.c.post("/api/assets/next-no", json={}).get_json()["assetNo"]
        aid = self.register(assetNo=issued).get_json()[0]["id"]
        self.assertEqual([a for a, _ in self.events(aid)], ["등록"])
        aid2 = self.register(assetNo=f"{self.today}-5050").get_json()[0]["id"]
        self.assertEqual([a for a, _ in self.events(aid2)], ["등록", "번호 미확인"])

    def test_손으로_넣은_5000번대는_그날_순번에_반영된다(self):
        no = f"{self.today}-5007"
        aid = self.register(assetNo=no).get_json()[0]["id"]
        # 번호를 형식 밖으로 바꿔 5007 이 자산에서 비워져도 자동 채번은 그 다음부터 — 재사용 없음
        self.assertEqual(self.c.patch(f"/api/assets/{aid}", json={"assetNo": "HB-X1"}).status_code, 200)
        r = self.register()
        self.assertEqual(r.get_json()[0]["assetNo"], f"{self.today}-5008")


# ══════════════════════════════════════════════════════════════ 번호 변경·바로잡기
class TestChangeAndFix(Base):
    def test_번호_변경도_같은_규칙(self):
        aid = self.register().get_json()[0]["id"]
        r = self.c.patch(f"/api/assets/{aid}", json={"assetNo": "260901-0031"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("TMS 자산 원장에 없는 번호", r.get_json()["error"])
        FakeClient.verdicts = {"260901-0031": "ok"}
        self.assertEqual(self.c.patch(f"/api/assets/{aid}", json={"assetNo": "260901-0031"}).status_code, 200)
        self.assertIn("번호변경", [a for a, _ in self.events(aid)])
        self.assertNotIn("번호 미확인", [a for a, _ in self.events(aid)])

    def test_번호가_안_바뀌는_저장은_창구를_안_부른다(self):
        FakeClient.verdicts = {"260901-0032": "ok"}
        aid = self.register(assetNo="260901-0032").get_json()[0]["id"]
        before = len(self.checks())
        # 자산 상세 폼은 저장마다 번호를 함께 보낸다 — 그때마다 창구에 물으면 안 된다
        r = self.c.patch(f"/api/assets/{aid}", json={"assetNo": "260901-0032", "maker": "LG"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(len(self.checks()), before)

    def test_빈_번호_넘겨받기(self):
        aid = self.register().get_json()[0]["id"]
        body = {"assetId": aid, "targetNo": "260901-0041", "mode": "take", "reason": "TMS 오입력"}
        r = self.c.post("/api/assets/number-fix", json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("TMS 자산 원장에 없는 번호", r.get_json()["error"])
        FakeClient.verdicts = {"260901-0041": "ok"}
        r = self.c.post("/api/assets/number-fix", json=body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.c.get(f"/api/assets/{aid}").get_json()["assetNo"], "260901-0041")

    def test_빈_번호_넘겨받기_창구_미설정이면_미확인_기록(self):
        self.disable_link()
        aid = self.register().get_json()[0]["id"]
        r = self.c.post("/api/assets/number-fix", json={
            "assetId": aid, "targetNo": "260901-0042", "mode": "take", "reason": "TMS 오입력"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        acts = [a for a, _ in self.events(aid)]
        self.assertEqual(acts[-2:], ["번호정정", "번호 미확인"])

    def test_상대가_옮겨_갈_번호도_검증한다(self):
        FakeClient.verdicts = {"260901-0051": "ok"}
        a = self.register().get_json()[0]["id"]
        b = self.register(assetNo="260901-0051", serial="SN-B").get_json()[0]["id"]
        body = {"assetId": a, "targetNo": "260901-0051", "mode": "take", "moveToNo": "260901-0052",
                "reason": "실물이 바뀌었다"}
        r = self.c.post("/api/assets/number-fix", json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("260901-0052", r.get_json()["error"])
        FakeClient.verdicts["260901-0052"] = "ok"
        r = self.c.post("/api/assets/number-fix", json=body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.c.get(f"/api/assets/{a}").get_json()["assetNo"], "260901-0051")
        self.assertEqual(self.c.get(f"/api/assets/{b}").get_json()["assetNo"], "260901-0052")

    def test_자동_임시번호는_형식_밖이라_그대로_통과(self):
        FakeClient.verdicts = {"260901-0061": "ok"}
        a = self.register().get_json()[0]["id"]
        b = self.register(assetNo="260901-0061", serial="SN-B2").get_json()[0]["id"]
        r = self.c.post("/api/assets/number-fix", json={
            "assetId": a, "targetNo": "260901-0061", "mode": "take", "reason": "TMS 오입력"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(self.c.get(f"/api/assets/{b}").get_json()["assetNo"].startswith("260901-0061-"))
        self.assertNotIn("번호 미확인", [x for x, _ in self.events(b)])


# ══════════════════════════════════════════════════════════════ 채번
class TestIssue(Base):
    def test_순번은_5000부터_이어지고_비워진_번호는_다시_나가지_않는다(self):
        r = self.c.post("/api/assets/next-no", json={})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json(), {"assetNo": f"{self.today}-{START:04d}", "verified": True, "note": ""})
        self.assertEqual(self.c.post("/api/assets/next-no", json={}).get_json()["assetNo"], f"{self.today}-{START + 1:04d}")
        # 받은 번호로 등록 → 번호를 바꿔 비움 → 그래도 다음 번호는 5002 (재사용 금지)
        aid = self.register(assetNo=f"{self.today}-{START + 1:04d}").get_json()[0]["id"]
        self.assertEqual(self.c.patch(f"/api/assets/{aid}", json={"assetNo": "HB-OLD"}).status_code, 200)
        self.assertEqual(self.register().get_json()[0]["assetNo"], f"{self.today}-{START + 2:04d}")
        # 안 쓴 채번(5000)도 다시 나가지 않는다
        self.assertEqual(self.c.post("/api/assets/next-no", json={}).get_json()["assetNo"], f"{self.today}-{START + 3:04d}")
        self.assertEqual(self.sql("SELECT last_seq FROM asset_no_counters WHERE day=?", self.today)[0]["last_seq"], START + 3)

    def test_서비스_함수는_날짜를_받는다(self):
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                self.assertEqual(next_asset_no(conn, today=dt.date(2026, 1, 2)), f"260102-{START:04d}")
                self.assertEqual(next_asset_no(conn, today=dt.date(2026, 1, 2)), f"260102-{START + 1:04d}")
                self.assertEqual(next_asset_no(conn, today=dt.date(2026, 1, 3)), f"260103-{START:04d}")

    def test_오늘_사본에_5000번대가_살아_있으면_보류(self):
        FakeClient.facts = [{"관리번호": f"{self.today}-5003", "_deleted_at": None}]
        r = self.c.post("/api/assets/next-no", json={})
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertIn("번호대 충돌 가능 — 연동 사본 확인 필요", r.get_json()["error"])
        self.assertIn(f"{self.today}-5003", r.get_json()["error"])
        # 자동 발번 등록도 같이 멈춘다 — 손입력(TMS 번호)은 계속 된다
        self.assertEqual(self.register().status_code, 409)
        FakeClient.verdicts = {"260901-0071": "ok"}
        self.assertEqual(self.register(assetNo="260901-0071").status_code, 201)
        FakeClient.facts = []
        self.assertEqual(self.c.post("/api/assets/next-no", json={}).status_code, 200)

    def test_지워진_사본_행이나_다른_날_번호는_보류하지_않는다(self):
        FakeClient.facts = [{"관리번호": f"{self.today}-5003", "_deleted_at": "2026-09-03 09:00:00"},
                            {"관리번호": "250101-5009", "_deleted_at": None},
                            {"관리번호": f"{self.today}-0301", "_deleted_at": None}]
        self.assertEqual(self.c.post("/api/assets/next-no", json={}).status_code, 200)

    def test_사본을_못_보면_보류_대신_verified_false(self):
        FakeClient.stale = True
        d = self.c.post("/api/assets/next-no", json={}).get_json()
        self.assertFalse(d["verified"])
        self.assertIn("낡음", d["note"])
        self.disable_link()
        d = self.c.post("/api/assets/next-no", json={}).get_json()
        self.assertEqual((d["verified"], d["note"]), (False, "연동 창구 미설정"))

    def test_권한(self):
        n = self.worker(["purchase.view"])
        self.assertEqual(n.post("/api/assets/next-no", json={}).status_code, 403)
        self.assertEqual(self.app.test_client().post("/api/assets/next-no", json={}).status_code, 401)


class TestBridge(Base):
    def test_토큰_없음_틀림_401_맞으면_채번(self):
        self.assertEqual(self.c.post("/api/bridge/next-asset-no", json={}).status_code, 401)
        old = os.environ.get("OWS_BRIDGE_TOKEN")
        os.environ["OWS_BRIDGE_TOKEN"] = "numbering-test-token"
        try:
            anon = self.app.test_client()          # 사람 세션 없이 토큰만으로
            self.assertEqual(anon.post("/api/bridge/next-asset-no", json={},
                                       headers={"X-Bridge-Token": "wrong"}).status_code, 401)
            r = anon.post("/api/bridge/next-asset-no", json={"ref": "rms-return-77"},
                          headers={"X-Bridge-Token": "numbering-test-token"})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            d = r.get_json()
            self.assertEqual((d["ok"], d["assetNo"], d["verified"]), (True, f"{self.today}-{START:04d}", True))
            # 화면 채번과 같은 순번을 쓴다
            self.assertEqual(self.c.post("/api/assets/next-no", json={}).get_json()["assetNo"], f"{self.today}-{START + 1:04d}")
            FakeClient.facts = [{"관리번호": f"{self.today}-5100", "_deleted_at": None}]
            self.assertEqual(anon.post("/api/bridge/next-asset-no", json={},
                                       headers={"X-Bridge-Token": "numbering-test-token"}).status_code, 409)
        finally:
            if old is None:
                os.environ.pop("OWS_BRIDGE_TOKEN", None)
            else:
                os.environ["OWS_BRIDGE_TOKEN"] = old


# ══════════════════════════════════════════════════════════════ A7 — 자산 수정 전값 기록
class TestEditHistory(Base):
    def _mods(self, aid):
        return [d for a, d in self.events(aid) if a == "수정"]

    def test_두_칸_수정하면_이벤트_하나에_두_칸의_전후값(self):
        aid = self.register().get_json()[0]["id"]
        r = self.c.patch(f"/api/assets/{aid}", json={"maker": "LG", "ram": "16GB"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        mods = self._mods(aid)
        self.assertEqual(len(mods), 1)
        self.assertEqual(mods[0], {"maker": {"from": "삼성", "to": "LG"}, "ram": {"from": "8GB", "to": "16GB"}})

    def test_같은_값_저장은_기록이_없다(self):
        aid = self.register().get_json()[0]["id"]
        r = self.c.patch(f"/api/assets/{aid}", json={"maker": "삼성", "ram": "8GB"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._mods(aid), [])
        # 한 칸만 바뀌면 그 칸만
        self.c.patch(f"/api/assets/{aid}", json={"maker": "삼성", "ram": "32GB"})
        self.assertEqual(self._mods(aid), [{"ram": {"from": "8GB", "to": "32GB"}}])

    def test_금액_전표_카테고리도_전후값(self):
        bid = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-09-03", "supplierId": self.sid, "totalAmount": 0}).get_json()["id"]
        aid = self.register().get_json()[0]["id"]
        r = self.c.patch(f"/api/assets/{aid}", json={"purchasePrice": 150000, "batchId": bid,
                                                     "categoryId": self.cat2["id"], "location": "A-1"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = self._mods(aid)[0]
        self.assertEqual(d["매입가"], {"from": 100000, "to": 150000})
        self.assertEqual(d["전표"], {"from": None, "to": bid})
        self.assertEqual(d["카테고리"], {"from": self.cat["name"], "to": self.cat2["name"]})
        self.assertEqual(d["location"], {"from": "", "to": "A-1"})

    def test_일괄_수정도_전후값(self):
        ids = [self.register().get_json()[0]["id"] for _ in range(2)]
        r = self.c.post("/api/assets/bulk", json={"ids": ids, "location": "B-2", "grade": "SA"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        for aid in ids:
            self.assertEqual(self._mods(aid), [{"위치": {"from": "", "to": "B-2"}, "등급": {"from": "미정", "to": "SA"}}])


if __name__ == "__main__":
    unittest.main()
