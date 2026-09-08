"""공용 마스터 — 모델 마스터·거래처 마스터(2026-09-02 대표 방침 "모든 데이터는 OWS·RMS에서 직접 등록·관리").

  - 모델: 연동 upsert(키ID)·페이징·이름 충돌 시 OWS 행에 연결·사람이 고친 행 보호·삭제 표시/복구·정규화 자동완성·
          중복 거부·브릿지 인증·자산 등록/수정 보완(막지 않고 경고)
  - 거래처: 키ID upsert(코드는 키가 아님)·이름 같은 OWS 행 연결·같은 이름 두 신원 비병합(대표 지정·분리)·
          다른 구분 같은 이름·보호·목록 이탈 표시·alias 대표 지정·판매처 자동완성·옛 응답 모양 유지
  - tms_link.sync_once 가 마스터 연동을 부르고, 창구 오류에도 틱이 산다
★임시 DB만 쓴다 — 운영·dev DB(data/*.db)에는 절대 붙지 않는다. 창구는 가짜 클라이언트다.
"""
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
from app import create_app  # noqa: E402
from app.purchase import masters, tms_link  # noqa: E402

PW = "admin-pass-1"
USER_PW = "user-pass-12"
SYNCED = "2026-09-02 18:10:12"


def tms_model(key, name, brand="SAMSUNG", cat="PC", sub="노트북", **kw):
    row = {"키ID": key, "모델명": name, "대분류": cat, "중분류": sub, "브랜드": brand, "펫네임": "",
           "모델사양": "", "모델메모": "", "사용여부": 1, "cdt": "2026-09-01 10:00:00",
           "mdt": "2026-09-01 10:00:00", "_synced_at": SYNCED, "_deleted_at": None}
    row.update(kw)
    return row


def tms_cat(key, cat, sub, **kw):
    row = {"키ID": key, "대분류": cat, "중분류": sub, "기본": 0, "사용여부": 1,
           "_synced_at": SYNCED, "_deleted_at": None}
    row.update(kw)
    return row


def tms_partner(key, name, kind="매입", code=None, **kw):
    row = {"키ID": key, "부서": None, "거래처구분": kind, "거래처코드": name if code is None else code,
           "거래처명": name, "주소": "", "상세주소": "", "우편번호": None, "전화번호": "", "FAX": None,
           "사업자등록번호": None, "거래처메모": None, "대표": "", "이메일": None, "사용여부": True,
           "cdt": "2026-09-01 10:00:00", "mdt": "2026-09-01 10:00:00"}
    row.update(kw)
    return row


class FakeClient:
    """창구 흉내 — /tables 는 사본 표 페이징·since·삭제 포함 규칙을, /partners 는 실시간 SELECT 를 흉내 낸다."""
    models = []
    categories = []
    partners = []
    calls = []
    fail_tables = False

    def __init__(self, url="", token=""):
        pass

    def get(self, path, **params):
        FakeClient.calls.append((path, params))
        if path == "/status":
            return {"ok": True, "status": {"freshness": {"stale": False, "age_sec": 5}}}
        if path == "/screens":
            name = params["names"]
            return {"ok": True, "server_time": "2026-09-02 20:00:00",
                    "screens": {name: {"headers": [], "count": 0, "items": []}}}
        if path in ("/deleted", "/restores"):
            return {"ok": True, "items": []}
        if path.startswith("/tables/"):
            if FakeClient.fail_tables:
                raise RuntimeError("연동 창구 오류 /tables: 500")
            table = path.split("/tables/", 1)[1]
            rows = FakeClient.models if table == masters.MODEL_TABLE else FakeClient.categories
            since = params.get("since")
            if since:
                rows = [r for r in rows if str(r.get("_synced_at") or "") >= since]
            if not params.get("include_deleted"):
                rows = [r for r in rows if not r.get("_deleted_at")]
            rows = sorted(rows, key=lambda r: r["키ID"])
            after = params.get("after_key")
            if after is not None:
                rows = [r for r in rows if r["키ID"] > int(after)]
            limit = int(params.get("limit") or 500)
            page = rows[:limit]
            nxt = page[-1]["키ID"] if len(rows) > limit else None
            return {"ok": True, "table": table, "count": len(page), "next_after_key": nxt, "items": page}
        if path == "/partners":
            kinds = (params.get("kinds") or "").split(",")
            items = [p for p in FakeClient.partners if p["거래처구분"] in kinds]
            return {"ok": True, "kinds": kinds, "count": len(items), "items": items}
        raise AssertionError(path)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-masters-"))
        self.db_path = self.tmp / "t.db"
        self.app = create_app(db_path=self.db_path)
        self.app.testing = True
        self.c = self.app.test_client()
        auth_mod._login_failures.clear()
        self.c.post("/api/auth/setup", json={"username": "admin", "displayName": "대표", "password": PW})
        cats = self.c.get("/api/categories").get_json()
        self.cat = cats[0]["id"]                       # 'PC'
        self.cat_name = cats[0]["name"]
        FakeClient.models, FakeClient.categories, FakeClient.partners = [], [], []
        FakeClient.calls, FakeClient.fail_tables = [], False
        self.cli = FakeClient()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sql(self, q, *args):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(q, args).fetchall()]
            conn.commit()
            return rows
        finally:
            conn.close()

    def sync_models(self, since=None):
        return masters.sync_models(self.app, self.cli, since)

    def sync_partners(self):
        return masters.sync_partners(self.app, self.cli)

    def worker(self, perms, username="worker1"):
        r = self.c.post("/api/users", json={
            "username": username, "displayName": username, "password": USER_PW,
            "perms": perms, "categoryIds": [], "allCategories": True, "isAdmin": False})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        self.assertEqual(c.post("/api/auth/login", json={"username": username, "password": USER_PW}).status_code, 200)
        return c


# ══════════════════════════════════════════════════════════════ 모델 연동
class TestModelSync(Base):
    def test_전량_적재_후_증분(self):
        FakeClient.categories = [tms_cat(1, "PC", "노트북"), tms_cat(4, "PC", "모니터"), tms_cat(99, "PC", "서버")]
        FakeClient.models = [tms_model(6, "NT850XAC", 모델사양="I7-8,16G,512G"), tms_model(7, "NT930X5J"),
                             tms_model(8, "LS24A400", brand="SAMSUNG", cat="모니터", sub="모니터")]
        res = self.sync_models()
        self.assertEqual((res["rows"], res["created"], res["updated"], res["attached"]), (3, 3, 0, 0))
        self.assertEqual(res["cursor"], SYNCED)
        # 분류: 기본 시드(PC/노트북·PC/모니터)에 키가 붙고, 없는 것은 새로 생긴다
        self.assertEqual((res["categories"]["attached"], res["categories"]["created"]), (2, 1))
        cat = self.sql("SELECT * FROM model_categories WHERE category='PC' AND subcategory='노트북'")[0]
        self.assertEqual((cat["tms_key_id"], cat["source"]), (1, "ows"))
        rows = self.sql("SELECT * FROM models ORDER BY name")
        self.assertEqual([r["name"] for r in rows], ["LS24A400", "NT850XAC", "NT930X5J"])
        m = [r for r in rows if r["name"] == "NT850XAC"][0]
        self.assertEqual((m["source"], m["tms_key_id"], m["norm_name"], m["brand"], m["category"], m["subcategory"], m["spec"]),
                         ("tms", 6, "NT850XAC", "SAMSUNG", "PC", "노트북", "I7-8,16G,512G"))
        # 같은 것을 다시 받아도 아무것도 안 바뀐다(멱등) — 증분(since)이면 행 자체가 안 온다
        res = self.sync_models(since="2026-09-02 18:20:00")
        self.assertEqual((res["rows"], res["created"], res["updated"]), (0, 0, 0))
        self.assertEqual(res["cursor"], SYNCED)                      # 분류 표의 _synced_at 이 커서 후보
        res = self.sync_models()
        self.assertEqual((res["created"], res["updated"], res["attached"]), (0, 0, 0))
        # 바뀐 행만 증분으로 따라온다
        FakeClient.models[1] = tms_model(7, "NT930X5J", brand="LG", _synced_at="2026-09-02 19:00:00")
        res = self.sync_models(since="2026-09-02 18:30:00")
        self.assertEqual((res["rows"], res["updated"]), (1, 1))
        self.assertEqual(self.sql("SELECT brand FROM models WHERE tms_key_id=7")[0]["brand"], "LG")
        self.assertEqual(res["cursor"], "2026-09-02 19:00:00")

    def test_페이징으로_전량을_받는다(self):
        FakeClient.models = [tms_model(i, f"M{i:04d}") for i in range(1, 1201)]
        res = self.sync_models()
        self.assertEqual(res["created"], 1200)
        pages = [p for p, q in FakeClient.calls if p == f"/tables/{masters.MODEL_TABLE}"]
        self.assertEqual(len(pages), 3)
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM models")[0]["c"], 1200)

    def test_이름이_같은_OWS_행에_키를_붙이고_빈칸만_채운다(self):
        r = self.c.post("/api/models", json={"name": "nt 850xac", "brand": "", "memo": "사람이 적음"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        FakeClient.models = [tms_model(6, "NT850XAC", brand="SAMSUNG", 모델메모="TMS메모")]
        res = self.sync_models()
        self.assertEqual((res["created"], res["attached"]), (0, 1))
        rows = self.sql("SELECT * FROM models")
        self.assertEqual(len(rows), 1)
        m = rows[0]
        self.assertEqual((m["name"], m["source"], m["tms_key_id"], m["brand"], m["memo"]),
                         ("nt 850xac", "ows", 6, "SAMSUNG", "사람이 적음"))       # 이름·메모는 사람 것, 빈 브랜드만 채움
        # 그 뒤 TMS 가 값을 바꿔도 사람 행은 안 덮는다
        FakeClient.models = [tms_model(6, "NT850XAC", brand="LG", 모델메모="바뀜")]
        res = self.sync_models()
        self.assertEqual((res["updated"], res["protected"]), (0, 1))
        m = self.sql("SELECT * FROM models")[0]
        self.assertEqual((m["brand"], m["memo"]), ("SAMSUNG", "사람이 적음"))

    def test_사람이_고친_연동행은_안_덮고_삭제표시만_따라온다(self):
        FakeClient.models = [tms_model(6, "NT850XAC", brand="SAMSUNG")]
        self.sync_models()
        mid = self.sql("SELECT id FROM models")[0]["id"]
        r = self.c.patch(f"/api/models/{mid}", json={"brand": "LG"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["owsEditedAt"])
        FakeClient.models = [tms_model(6, "NT850XAC", brand="SAMSUNG", 모델사양="바뀜", _deleted_at="2026-09-02 19:00:00")]
        res = self.sync_models()
        self.assertEqual((res["updated"], res["protected"], res["deleted"]), (0, 1, 1))
        m = self.sql("SELECT * FROM models")[0]
        self.assertEqual((m["brand"], m["spec"], m["source"]), ("LG", "", "tms"))
        self.assertTrue(m["tms_deleted_at"])
        # 목록 기본에서는 빠지고 all=1 이면 보인다. 지우지는 않는다.
        self.assertEqual(self.c.get("/api/models?q=nt850").get_json()["count"], 0)
        self.assertEqual(self.c.get("/api/models?q=nt850&all=1").get_json()["count"], 1)
        # 복구되면 표시가 지워진다
        FakeClient.models = [tms_model(6, "NT850XAC", brand="SAMSUNG")]
        res = self.sync_models()
        self.assertEqual(res["restored"], 1)
        self.assertEqual(self.sql("SELECT tms_deleted_at FROM models")[0]["tms_deleted_at"], "")

    def test_연동행은_TMS_값을_따라가고_이름충돌은_건너뛴다(self):
        FakeClient.models = [tms_model(6, "NT850XAC"), tms_model(7, "NT930X5J")]
        self.sync_models()
        FakeClient.models = [tms_model(6, "NT850XAD", brand="LG", 사용여부=0), tms_model(7, "nt 850xad")]
        res = self.sync_models()
        rows = {r["tms_key_id"]: r for r in self.sql("SELECT * FROM models")}
        self.assertEqual((rows[6]["name"], rows[6]["brand"], rows[6]["enabled"]), ("NT850XAD", "LG", 0))
        self.assertEqual(rows[7]["name"], "NT930X5J")            # 충돌하는 이름 변경은 건너뛴다
        self.assertTrue(any("충돌" in n for n in res["notes"]))
        # 처음 보는 키가 기존 TMS 행과 같은 이름이면 새로 만들지 않고 기록만
        FakeClient.models.append(tms_model(8, "NT850XAD"))
        res = self.sync_models()
        self.assertEqual((res["created"], res["skipped"]), (0, 1))
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM models")[0]["c"], 2)


# ══════════════════════════════════════════════════════════════ 모델 API
class TestModelApi(Base):
    def test_정규화_부분일치_자동완성(self):
        for name, brand in (("NT850XAC", "SAMSUNG"), ("NT 950XCJ", "SAMSUNG"), ("L480", "LENOVO"), ("XCJ-1", "기타")):
            self.assertEqual(self.c.post("/api/models", json={"name": name, "brand": brand}).status_code, 201)
        r = self.c.get("/api/models?q=nt 8").get_json()
        self.assertEqual([m["name"] for m in r["items"]], ["NT850XAC"])
        r = self.c.get("/api/models?q=NT").get_json()
        self.assertEqual([m["name"] for m in r["items"]], ["NT 950XCJ", "NT850XAC"])
        r = self.c.get("/api/models?q=xcj").get_json()       # 앞부분 일치가 먼저
        self.assertEqual([m["name"] for m in r["items"]], ["XCJ-1", "NT 950XCJ"])
        r = self.c.get("/api/models?q=lenovo").get_json()    # 브랜드로도
        self.assertEqual([m["name"] for m in r["items"]], ["L480"])
        r = self.c.get("/api/models?limit=2").get_json()
        self.assertEqual((r["count"], r["total"]), (2, 4))
        # lookup
        self.assertTrue(self.c.get("/api/models/lookup?name=nt950xcj").get_json()["found"])
        self.assertFalse(self.c.get("/api/models/lookup?name=없는모델").get_json()["found"])

    def test_중복_정규화_이름_거부(self):
        self.assertEqual(self.c.post("/api/models", json={"name": "NT850XAC"}).status_code, 201)
        r = self.c.post("/api/models", json={"name": "nt 850 xac"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("NT850XAC", r.get_json()["error"])
        self.assertEqual(self.c.post("/api/models", json={"name": ""}).status_code, 400)

    def test_수정_비활성_분류목록(self):
        a = self.c.post("/api/models", json={"name": "NT850XAC"}).get_json()["id"]
        self.c.post("/api/models", json={"name": "L480"})
        r = self.c.patch(f"/api/models/{a}", json={"brand": "SAMSUNG", "category": "PC", "subcategory": "노트북", "enabled": False})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.c.get("/api/models").get_json()["count"], 1)
        self.assertEqual(self.c.get("/api/models?all=1").get_json()["count"], 2)
        self.assertEqual(self.c.get("/api/models?all=1&category=PC").get_json()["count"], 1)
        self.assertEqual(self.c.patch(f"/api/models/{a}", json={"name": "l 480"}).status_code, 409)
        self.assertEqual(self.c.patch(f"/api/models/{a}", json={"brand": "SAMSUNG"}).status_code, 400)  # 변경 없음
        self.assertEqual(self.c.patch("/api/models/9999", json={"brand": "X"}).status_code, 404)
        cats = self.c.get("/api/model-categories").get_json()["items"]
        self.assertIn(("PC", "노트북"), [(c["category"], c["subcategory"]) for c in cats])
        self.assertEqual(len(cats), 8)
        acts = [r["action"] for r in self.sql("SELECT action FROM audit_log ORDER BY id")]
        self.assertIn("model_created", acts)
        self.assertIn("model_updated", acts)

    def test_권한(self):
        w = self.worker(["purchase.view"])
        self.assertEqual(w.get("/api/models").status_code, 200)
        self.assertEqual(w.post("/api/models", json={"name": "X1"}).status_code, 403)
        n = self.worker(["orders.view"], "worker2")
        self.assertEqual(n.get("/api/models").status_code, 403)


class TestBridgeModels(Base):
    def test_토큰_없으면_401_맞으면_후보(self):
        self.c.post("/api/models", json={"name": "NT850XAC", "brand": "SAMSUNG", "category": "PC", "subcategory": "노트북"})
        self.assertEqual(self.c.get("/api/bridge/models?q=nt").status_code, 401)
        old = os.environ.get("OWS_BRIDGE_TOKEN")
        os.environ["OWS_BRIDGE_TOKEN"] = "masters-test-token"
        try:
            self.assertEqual(self.c.get("/api/bridge/models?q=nt", headers={"X-Bridge-Token": "wrong"}).status_code, 401)
            r = self.c.get("/api/bridge/models?q=nt 850", headers={"X-Bridge-Token": "masters-test-token"})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            d = r.get_json()
            self.assertTrue(d["ok"])
            self.assertEqual(d["count"], 1)
            self.assertEqual(d["items"][0], {"name": "NT850XAC", "brand": "SAMSUNG", "category": "PC",
                                             "subcategory": "노트북", "petName": "", "spec": ""})
        finally:
            if old is None:
                os.environ.pop("OWS_BRIDGE_TOKEN", None)
            else:
                os.environ["OWS_BRIDGE_TOKEN"] = old


# ══════════════════════════════════════════════════════════════ 자산 보완
class TestAssetCompletion(Base):
    def test_등록시_브랜드와_카테고리를_빈칸만_채운다(self):
        self.c.post("/api/models", json={"name": "NT850XAC", "brand": "SAMSUNG", "category": self.cat_name, "subcategory": "노트북"})
        r = self.c.post("/api/assets", json={"categoryId": self.cat, "model": "nt850xac", "qty": 1})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        d = r.get_json()[0]
        self.assertEqual(d["modelFilled"], {"maker": "SAMSUNG"})
        self.assertEqual(d["modelMaster"]["name"], "NT850XAC")
        self.assertNotIn("modelWarning", d)
        self.assertEqual(self.sql("SELECT maker, model FROM assets WHERE id=?", d["id"])[0], {"maker": "SAMSUNG", "model": "nt850xac"})
        # 브랜드를 적었으면 그대로 둔다
        r = self.c.post("/api/assets", json={"categoryId": self.cat, "model": "NT850XAC", "maker": "삼성", "qty": 1})
        self.assertNotIn("modelFilled", r.get_json()[0])
        self.assertEqual(self.sql("SELECT maker FROM assets WHERE id=?", r.get_json()[0]["id"])[0]["maker"], "삼성")
        # 카테고리를 안 보내도 마스터 중분류(노트북)로 채워진다(예전엔 400)
        r = self.c.post("/api/assets", json={"model": "NT850XAC", "qty": 1})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertEqual(r.get_json()[0]["modelFilled"]["categoryId"], self.cat)

    def test_태블릿_모니터_모델은_중분류로_카테고리를_채운다(self):
        """카테고리 축이 TMS 중분류(2026-09-03, A5) — 예전엔 대분류 이름과 같은 카테고리만 찾아 태블릿·모니터 모델을 못 채웠다."""
        cats = {c["name"]: c["id"] for c in self.c.get("/api/categories").get_json()}
        self.c.post("/api/models", json={"name": "SM-T505N", "brand": "SAMSUNG", "category": "태블릿", "subcategory": "태블릿"})
        self.c.post("/api/models", json={"name": "S24C314EA", "brand": "SAMSUNG", "category": "모니터", "subcategory": "모니터"})
        self.c.post("/api/models", json={"name": "SM-R180", "brand": "SAMSUNG", "category": "웨어러블", "subcategory": "버즈"})
        self.c.post("/api/models", json={"name": "M710Q", "brand": "LENOVO", "category": "PC", "subcategory": "미니PC"})
        for model, cat in (("SM-T505N", "태블릿"), ("S24C314EA", "모니터"), ("SM-R180", "웨어러블"), ("M710Q", "미니PC")):
            r = self.c.post("/api/assets", json={"model": model, "qty": 1})
            self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
            self.assertEqual(r.get_json()[0]["modelFilled"]["categoryId"], cats[cat], model)
        # 사람이 고른 카테고리는 그대로
        r = self.c.post("/api/assets", json={"model": "SM-T505N", "categoryId": cats["노트북"], "qty": 1})
        self.assertNotIn("categoryId", r.get_json()[0].get("modelFilled") or {})

    def test_마스터에_없으면_막지_않고_경고만(self):
        r = self.c.post("/api/assets", json={"categoryId": self.cat, "model": "ZZZ-없는모델", "qty": 2})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        for d in r.get_json():
            self.assertIn("ZZZ-없는모델", d["modelWarning"])
            self.assertIsNone(d["modelMaster"])
        # 모델명이 없으면 아무 표시도 없다
        r = self.c.post("/api/assets", json={"categoryId": self.cat, "qty": 1})
        self.assertEqual(r.status_code, 201)
        self.assertNotIn("modelWarning", r.get_json()[0])

    def test_수정시_빈_브랜드만_채운다(self):
        self.c.post("/api/models", json={"name": "NT850XAC", "brand": "SAMSUNG"})
        aid = self.c.post("/api/assets", json={"categoryId": self.cat, "model": "L480", "maker": "LENOVO", "qty": 1}).get_json()[0]["id"]
        r = self.c.patch(f"/api/assets/{aid}", json={"model": "NT850XAC", "maker": ""})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["modelFilled"], {"maker": "SAMSUNG"})
        self.assertEqual(self.sql("SELECT maker FROM assets WHERE id=?", aid)[0]["maker"], "SAMSUNG")
        r = self.c.patch(f"/api/assets/{aid}", json={"maker": "LG"})
        self.assertNotIn("modelFilled", r.get_json())
        self.assertEqual(self.sql("SELECT maker FROM assets WHERE id=?", aid)[0]["maker"], "LG")
        r = self.c.patch(f"/api/assets/{aid}", json={"model": "없는모델"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("modelWarning", r.get_json())
        self.assertEqual(self.sql("SELECT model FROM assets WHERE id=?", aid)[0]["model"], "없는모델")   # 막지 않는다

    def test_스펙_자동완성_모델칸에_마스터_후보가_맨_위(self):
        self.c.post("/api/models", json={"name": "NT850XAC", "brand": "SAMSUNG", "category": "PC", "subcategory": "노트북"})
        self.c.post("/api/assets", json={"categoryId": self.cat, "model": "NT850XAB", "qty": 1})
        opts = self.c.get("/api/spec-options?field=model&q=nt850").get_json()["options"]
        self.assertEqual(opts[0], {"value": "NT850XAC", "source": "master", "hint": "SAMSUNG · PC/노트북"})
        self.assertIn(("NT850XAB", "used"), [(o["value"], o["source"]) for o in opts])


# ══════════════════════════════════════════════════════════════ 거래처 연동
class TestPartnerSync(Base):
    def test_키ID_기준_적재_코드는_키가_아니다(self):
        FakeClient.partners = [tms_partner(1, "매입처A", code="업체", 전화번호="02-1", 대표="대표A"),
                               tms_partner(2, "매입처B", code="업체"),
                               tms_partner(3, "판매처C", kind="판매", code="C", 사업자등록번호="123-45-67890"),
                               tms_partner(4, "AS업체D", kind="AS업체"),
                               tms_partner(5, "렌탈고객", kind="렌탈")]        # 창구가 안 주는 구분 — 안 온다
        res = self.sync_partners()
        self.assertEqual((res["rows"], res["created"], res["dups"]), (4, 4, 0))
        rows = {r["name"]: r for r in self.sql("SELECT * FROM suppliers")}
        self.assertEqual(set(rows), {"매입처A", "매입처B", "판매처C", "AS업체D"})   # 같은 코드 '업체' 두 곳이 각각 산다
        a = rows["매입처A"]
        self.assertEqual((a["kind"], a["code"], a["phone"], a["ceo"], a["source"], a["tms_key_id"], a["norm_name"]),
                         ("매입", "업체", "02-1", "대표A", "tms", 1, "매입처A"))
        self.assertEqual((rows["판매처C"]["kind"], rows["판매처C"]["biz_no"]), ("판매", "123-45-67890"))
        links = self.sql("SELECT * FROM supplier_tms_links ORDER BY tms_key_id")
        self.assertEqual([(l["tms_key_id"], l["is_primary"]) for l in links], [(1, 1), (2, 1), (3, 1), (4, 1)])
        # 다시 받아도 아무것도 안 바뀐다(갱신 시각 유지)
        before = self.sql("SELECT updated_at FROM suppliers WHERE name='매입처A'")[0]["updated_at"]
        res = self.sync_partners()
        self.assertEqual((res["created"], res["updated"], res["attached"]), (0, 0, 0))
        self.assertEqual(self.sql("SELECT updated_at FROM suppliers WHERE name='매입처A'")[0]["updated_at"], before)
        # 연동 행은 TMS 값을 따라간다(전화·구분·사용여부·이름)
        FakeClient.partners = [tms_partner(1, "매입처A2", kind="판매", code="업체", 전화번호="02-2", 사용여부=False),
                               tms_partner(2, "매입처B", code="업체"),
                               tms_partner(3, "판매처C", kind="판매", code="C", 사업자등록번호="123-45-67890"),
                               tms_partner(4, "AS업체D", kind="AS업체")]
        res = self.sync_partners()
        self.assertEqual(res["updated"], 1)
        a = self.sql("SELECT * FROM suppliers WHERE tms_key_id=1")[0]
        self.assertEqual((a["name"], a["norm_name"], a["kind"], a["phone"], a["enabled"]), ("매입처A2", "매입처A2", "판매", "02-2", 0))

    def test_이름이_같은_OWS_행에_붙이고_빈칸만_채운다(self):
        sid = self.c.post("/api/suppliers", json={"name": "테스트상사", "phone": "02-111", "contact": "김담당"}).get_json()["id"]
        FakeClient.partners = [tms_partner(10, "테스트상사", code="TS", 전화번호="02-999", 대표="박대표", 주소="서울")]
        res = self.sync_partners()
        self.assertEqual((res["created"], res["attached"], res["protected"]), (0, 1, 1))
        s = self.sql("SELECT * FROM suppliers WHERE id=?", sid)[0]
        self.assertEqual((s["source"], s["tms_key_id"], s["phone"], s["contact"], s["ceo"], s["address"], s["code"]),
                         ("ows", 10, "02-111", "김담당", "박대표", "서울", "TS"))
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM suppliers")[0]["c"], 1)
        # 공백·대소문자만 다른 이름도 같은 구분이면 그 행에 붙는다(새 행 금지)
        sid2 = self.c.post("/api/suppliers", json={"name": "AB 테크"}).get_json()["id"]
        FakeClient.partners.append(tms_partner(11, "ab테크", code="AB"))
        res = self.sync_partners()
        self.assertEqual((res["created"], res["attached"]), (0, 1))
        s2 = self.sql("SELECT * FROM suppliers WHERE id=?", sid2)[0]
        self.assertEqual((s2["name"], s2["tms_key_id"], s2["code"]), ("AB 테크", 11, "AB"))

    def test_같은_이름_두_신원은_합치지_않고_사람이_정한다(self):
        FakeClient.partners = [tms_partner(177, "HNC", code="HNC", 전화번호="02-1"),
                               tms_partner(2244, "HNC", code="HNC", 전화번호="02-2", 대표="둘째")]
        res = self.sync_partners()
        self.assertEqual((res["created"], res["dups"]), (1, 1))
        rows = self.sql("SELECT * FROM suppliers")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["tms_key_id"], rows[0]["phone"]), (177, "02-1"))
        sid = rows[0]["id"]
        lst = self.c.get("/api/suppliers").get_json()
        self.assertEqual([(l["keyId"], l["isPrimary"]) for l in lst[0]["tmsLinks"]], [(177, True), (2244, False)])
        self.assertEqual(lst[0]["tmsLinks"][1]["payload"]["대표"], "둘째")   # 부 신원의 원본 칸이 보인다
        # 대표 신원 바꾸기 — 고른 신원의 값이 바로 반영된다(연동 행이라 따라감). ows_edited 는 안 찍힌다(값을 고친 게 아니다)
        r = self.c.post(f"/api/suppliers/{sid}/tms-primary", json={"keyId": 2244})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        s = self.sql("SELECT * FROM suppliers WHERE id=?", sid)[0]
        self.assertEqual((s["tms_key_id"], s["phone"], s["ceo"], s["ows_edited_at"]), (2244, "02-2", "둘째", ""))
        self.assertEqual(self.sql("SELECT is_primary FROM supplier_tms_links WHERE tms_key_id=2244")[0]["is_primary"], 1)
        # 대표 신원은 분리할 수 없다. 부 신원은 사람이 정한 이름으로 별도 거래처가 된다
        self.assertEqual(self.c.post(f"/api/suppliers/{sid}/tms-split", json={"keyId": 2244, "name": "HNC-2"}).status_code, 400)
        r = self.c.post(f"/api/suppliers/{sid}/tms-split", json={"keyId": 177, "name": "HNC(구)"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        new = r.get_json()
        self.assertEqual((new["name"], new["tmsKeyId"], new["phone"], new["source"]), ("HNC(구)", 177, "02-1", "tms"))
        self.assertEqual(self.sql("SELECT supplier_id, is_primary FROM supplier_tms_links WHERE tms_key_id=177")[0],
                         {"supplier_id": new["id"], "is_primary": 1})
        # 이후 연동은 각자 자기 키를 따라간다
        FakeClient.partners = [tms_partner(177, "HNC", code="HNC", 전화번호="02-7"),
                               tms_partner(2244, "HNC", code="HNC", 전화번호="02-8")]
        res = self.sync_partners()
        self.assertEqual((res["created"], res["dups"], res["updated"]), (0, 0, 2))
        self.assertEqual(self.sql("SELECT phone FROM suppliers WHERE id=?", new["id"])[0]["phone"], "02-7")
        self.assertEqual(self.sql("SELECT phone FROM suppliers WHERE id=?", sid)[0]["phone"], "02-8")
        self.assertEqual(self.c.post(f"/api/suppliers/{sid}/tms-primary", json={"keyId": 177}).status_code, 400)

    def test_다른_구분_같은_이름은_한_행에_구분이_둘(self):
        FakeClient.partners = [tms_partner(5178, "개인매입", kind="판매"), tms_partner(5186, "개인매입", kind="매입"),
                               tms_partner(1, "순수매입처")]
        res = self.sync_partners()
        self.assertEqual((res["created"], res["dups"]), (2, 1))
        lst = {s["name"]: s for s in self.c.get("/api/suppliers").get_json()}
        self.assertEqual((lst["개인매입"]["kind"], lst["개인매입"]["kinds"]), ("판매", ["매입", "판매"]))
        self.assertEqual([s["name"] for s in self.c.get("/api/suppliers?kind=판매").get_json()], ["개인매입"])
        self.assertEqual(sorted(s["name"] for s in self.c.get("/api/suppliers?kind=매입").get_json()), ["개인매입", "순수매입처"])
        # 판매처 자동완성은 '판매' 구분만(부 신원 포함)
        vals = [o["value"] for o in self.c.get("/api/spec-options?field=customer&q=").get_json()["options"]]
        self.assertEqual(vals, ["개인매입"])

    def test_사람이_고친_연동행은_빈칸만_채운다(self):
        FakeClient.partners = [tms_partner(1, "매입처A", 전화번호="02-1")]
        self.sync_partners()
        sid = self.sql("SELECT id FROM suppliers")[0]["id"]
        r = self.c.patch(f"/api/suppliers/{sid}", json={"phone": "010-0000"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        FakeClient.partners = [tms_partner(1, "매입처A-개명", 전화번호="02-2", FAX="02-3", 사용여부=False)]
        res = self.sync_partners()
        self.assertEqual((res["updated"], res["protected"]), (0, 1))
        s = self.sql("SELECT * FROM suppliers WHERE id=?", sid)[0]
        self.assertEqual((s["name"], s["phone"], s["fax"], s["enabled"]), ("매입처A", "010-0000", "02-3", 1))
        self.assertTrue(s["ows_edited_at"])

    def test_목록에서_빠지면_표시만_하고_돌아오면_지운다(self):
        FakeClient.partners = [tms_partner(1, "매입처A"), tms_partner(2, "매입처B")]
        self.sync_partners()
        FakeClient.partners = [tms_partner(1, "매입처A")]
        res = self.sync_partners()
        self.assertEqual(res["removed"], 1)
        b = self.sql("SELECT * FROM suppliers WHERE tms_key_id=2")[0]
        self.assertTrue(b["tms_deleted_at"])
        self.assertTrue(self.sql("SELECT deleted_at FROM supplier_tms_links WHERE tms_key_id=2")[0]["deleted_at"])
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM suppliers")[0]["c"], 2)         # 지우지 않는다
        # 빈 응답이면 아무것도 표시하지 않는다(창구 이상으로 전부 이탈 처리되는 것 방지)
        FakeClient.partners = []
        res = self.sync_partners()
        self.assertEqual(res["removed"], 0)
        self.assertEqual(self.sql("SELECT tms_deleted_at FROM suppliers WHERE tms_key_id=1")[0]["tms_deleted_at"], "")
        FakeClient.partners = [tms_partner(1, "매입처A"), tms_partner(2, "매입처B")]
        res = self.sync_partners()
        self.assertEqual(res["restored"], 1)
        self.assertEqual(self.sql("SELECT tms_deleted_at FROM suppliers WHERE tms_key_id=2")[0]["tms_deleted_at"], "")

    def test_sync_once가_마스터를_부르고_커서를_남긴다(self):
        tms_link.STATE_FILE = self.tmp / "state.json"
        old_client = tms_link.Client
        old_env = {k: os.environ.get(k) for k in ("OWS_DATALINK_URL", "OWS_DATALINK_TOKEN")}
        os.environ["OWS_DATALINK_URL"], os.environ["OWS_DATALINK_TOKEN"] = "http://fake", "t"
        tms_link.Client = FakeClient
        try:
            FakeClient.models = [tms_model(6, "NT850XAC")]
            FakeClient.partners = [tms_partner(1, "매입처A")]
            res = tms_link.sync_once(self.app)
            self.assertEqual(res["masters"]["models"]["created"], 1)
            self.assertEqual(res["masters"]["partners"]["created"], 1)
            state = json.loads(tms_link.STATE_FILE.read_text("utf-8"))
            self.assertEqual(state["masters_cursor"], SYNCED)
            self.assertEqual(state["cursor"], "2026-09-02 20:00:00")
            # 두 번째 틱은 마스터도 3분 겹친 증분으로 받는다
            tms_link.sync_once(self.app)
            since = [q.get("since") for p, q in FakeClient.calls if p == f"/tables/{masters.MODEL_TABLE}"][-1]
            self.assertEqual(since, "2026-09-02 18:07:12")
            # 창구가 표를 못 주면 모델은 오류로 남고 거래처·화면 반영·커서는 그대로 진행된다
            FakeClient.fail_tables = True
            res = tms_link.sync_once(self.app)
            self.assertIn("error", res["masters"]["models"])
            self.assertEqual(res["masters"]["partners"]["rows"], 1)
            self.assertEqual(json.loads(tms_link.STATE_FILE.read_text("utf-8"))["masters_cursor"], SYNCED)
            self.assertEqual(self.c.get("/api/masters/status").get_json()["counts"]["modelsTms"], 1)
        finally:
            tms_link.Client = old_client
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


# ══════════════════════════════════════════════════════════════ 거래처 API
class TestSupplierApi(Base):
    def test_옛_응답_모양과_새_칸(self):
        r = self.c.post("/api/suppliers", json={"name": "테스트상사", "contact": "김", "phone": "02-1", "memo": "m"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["kind"], "매입")
        lst = self.c.get("/api/suppliers").get_json()
        self.assertIsInstance(lst, list)
        s = lst[0]
        for k in ("id", "name", "contact", "phone", "memo", "enabled", "batchCount", "totalAmount"):
            self.assertIn(k, s)
        self.assertEqual((s["kind"], s["kinds"], s["source"], s["code"], s["bizNo"], s["aliasOf"], s["dupIds"], s["tmsLinks"]),
                         ("매입", ["매입"], "ows", "", "", None, [], []))
        self.assertTrue(s["owsEditedAt"])
        self.assertEqual(self.sql("SELECT norm_name FROM suppliers")[0]["norm_name"], "테스트상사")

    def test_등록_구분_코드_사업자번호_검색(self):
        self.assertEqual(self.c.post("/api/suppliers", json={"name": "업무관리판매", "kind": "판매", "code": "HB", "bizNo": "111-22-33333"}).status_code, 201)
        self.assertEqual(self.c.post("/api/suppliers", json={"name": "수리업체", "kind": "AS업체"}).status_code, 201)
        self.assertEqual(self.c.post("/api/suppliers", json={"name": "매입처"}).status_code, 201)
        self.assertEqual(self.c.post("/api/suppliers", json={"name": "이상한구분", "kind": "렌탈"}).status_code, 400)
        self.assertEqual([s["name"] for s in self.c.get("/api/suppliers?kind=판매").get_json()], ["업무관리판매"])
        self.assertEqual([s["name"] for s in self.c.get("/api/suppliers?q=111 22").get_json()], ["업무관리판매"])
        self.assertEqual([s["name"] for s in self.c.get("/api/suppliers?q=hb").get_json()], ["업무관리판매"])
        self.assertEqual(len(self.c.get("/api/suppliers").get_json()), 3)

    def test_중복_거부(self):
        self.assertEqual(self.c.post("/api/suppliers", json={"name": "AB 테크"}).status_code, 201)
        self.assertEqual(self.c.post("/api/suppliers", json={"name": "AB 테크", "kind": "판매"}).status_code, 409)   # 이름은 한 곳에 하나
        self.assertEqual(self.c.post("/api/suppliers", json={"name": "ab테크"}).status_code, 409)              # 공백·대소문자만 다름
        self.assertEqual(self.c.post("/api/suppliers", json={"name": "ab테크", "kind": "판매"}).status_code, 201)  # 다른 구분·다른 표기는 허용

    def test_수정_새칸과_ows_edited(self):
        sid = self.c.post("/api/suppliers", json={"name": "매입처"}).get_json()["id"]
        r = self.c.patch(f"/api/suppliers/{sid}", json={"kind": "판매", "code": "C1", "bizNo": "1", "ceo": "대표", "fax": "f",
                                                        "email": "e@x", "address": "a", "addressDetail": "d", "zip": "0", "dept": "부"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        s = self.sql("SELECT * FROM suppliers WHERE id=?", sid)[0]
        self.assertEqual((s["kind"], s["code"], s["biz_no"], s["ceo"], s["fax"], s["email"], s["address"], s["address_detail"], s["zip"], s["dept"]),
                         ("판매", "C1", "1", "대표", "f", "e@x", "a", "d", "0", "부"))
        self.assertEqual(self.c.patch(f"/api/suppliers/{sid}", json={"code": "C1"}).status_code, 400)   # 변경 없음
        self.assertEqual(self.c.patch(f"/api/suppliers/{sid}", json={"kind": "없음"}).status_code, 400)
        self.assertEqual(self.c.patch(f"/api/suppliers/{sid}", json={"name": "새이름", "enabled": False}).status_code, 200)
        s = self.sql("SELECT * FROM suppliers WHERE id=?", sid)[0]
        self.assertEqual((s["name"], s["norm_name"], s["enabled"]), ("새이름", "새이름", 0))

    def test_대표_지정_alias(self):
        # OWS 등록은 공백·대소문자만 다른 이름을 거부하므로, 중복 후보는 연동으로 생긴다:
        # 키 1 이 '태화무역'(OWS 행)에 붙은 뒤 키 2 '태화 무역'이 오면 새 행이 된다(원문이 달라 name UNIQUE 를 안 건드린다)
        a = self.c.post("/api/suppliers", json={"name": "태화무역"}).get_json()["id"]
        FakeClient.partners = [tms_partner(1, "태화무역"), tms_partner(2, "태화 무역")]
        res = self.sync_partners()
        self.assertEqual((res["attached"], res["created"], res["dups"]), (1, 1, 0))
        b = self.sql("SELECT id FROM suppliers WHERE name='태화 무역'")[0]["id"]
        c = self.c.post("/api/suppliers", json={"name": "다른곳"}).get_json()["id"]
        self.c.post("/api/purchase-batches", json={"stage": "purchased", "purchaseDate": "2026-09-01", "supplierId": b, "totalAmount": 1000})
        lst = {s["id"]: s for s in self.c.get("/api/suppliers").get_json()}
        self.assertEqual((lst[a]["dupIds"], lst[b]["dupIds"], lst[c]["dupIds"]), ([b], [a], []))
        r = self.c.post(f"/api/suppliers/{a}/canonical", json={"aliasIds": [b]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        lst = {s["id"]: s for s in self.c.get("/api/suppliers").get_json()}
        self.assertEqual((lst[b]["aliasOf"], lst[b]["aliasOfName"], lst[a]["aliasCount"]), (a, "태화무역", 1))
        self.assertEqual((lst[a]["batchCount"], lst[a]["groupBatchCount"], lst[a]["groupTotalAmount"]), (0, 1, 1000))
        self.assertEqual(lst[a]["dupIds"], [])                                   # 묶이면 후보에서 빠진다
        # 전표의 거래처는 그대로다
        self.assertEqual(self.sql("SELECT supplier_id FROM purchase_batches")[0]["supplier_id"], b)
        # 매입 거래처 자동완성은 별칭을 안 보여 준다
        vals = [o["value"] for o in self.c.get("/api/spec-options?field=supplier&q=태화").get_json()["options"]]
        self.assertEqual(vals, ["태화무역"])
        # 별칭을 대표로 삼을 수 없고, 별칭 행을 다른 대표로 지정하려면 400
        self.assertEqual(self.c.patch(f"/api/suppliers/{c}", json={"aliasOf": b}).status_code, 400)
        self.assertEqual(self.c.post(f"/api/suppliers/{b}/canonical", json={"aliasIds": [c]}).status_code, 400)
        self.assertEqual(self.c.patch(f"/api/suppliers/{a}", json={"aliasOf": a}).status_code, 400)
        # 풀기
        self.assertEqual(self.c.post(f"/api/suppliers/{a}/canonical", json={"detachIds": [b]}).status_code, 200)
        self.assertIsNone(self.sql("SELECT alias_of FROM suppliers WHERE id=?", b)[0]["alias_of"])
        self.assertEqual(self.c.patch(f"/api/suppliers/{b}", json={"aliasOf": a}).status_code, 200)
        self.assertEqual(self.c.patch(f"/api/suppliers/{b}", json={"aliasOf": None}).status_code, 200)
        self.assertIsNone(self.sql("SELECT alias_of FROM suppliers WHERE id=?", b)[0]["alias_of"])
        acts = [r["action"] for r in self.sql("SELECT action FROM audit_log")]
        self.assertIn("supplier_canonical", acts)

    def _alias_pair(self):
        """'태화무역'(OWS 등록 행 a) + 연동으로 생긴 '태화 무역'(b) — test_대표_지정_alias 와 같은 만들기. 묶기(canonical)는 호출자가."""
        a = self.c.post("/api/suppliers", json={"name": "태화무역"}).get_json()["id"]
        FakeClient.partners = [tms_partner(1, "태화무역"), tms_partner(2, "태화 무역")]
        self.sync_partners()
        b = self.sql("SELECT id FROM suppliers WHERE name='태화 무역'")[0]["id"]
        return a, b

    def test_별칭은_조회_화면에서_대표_이름으로(self):
        """§8-1(2026-09-03): 전표 목록·상세·자산 상세·미지급·판매 전표 라인의 거래처가 대표 이름으로 보인다. 전표의 거래처 id 는 그대로."""
        a, b = self._alias_pair()
        bid = self.c.post("/api/purchase-batches", json={"stage": "purchased", "purchaseDate": "2026-09-01",
                                                         "supplierId": b, "totalAmount": 1000}).get_json()["id"]
        aid = self.c.post("/api/assets", json={"categoryId": self.cat, "batchId": bid, "model": "NT950XDB",
                                               "qty": 1, "purchasePrice": 1000}).get_json()[0]["id"]
        lst = {x["id"]: x for x in self.c.get("/api/purchase-batches").get_json()}
        self.assertEqual((lst[bid]["supplierName"], lst[bid]["supplierOrigName"]), ("태화 무역", None))   # 묶기 전엔 원문
        self.assertEqual(self.c.post(f"/api/suppliers/{a}/canonical", json={"aliasIds": [b]}).status_code, 200)
        lst = {x["id"]: x for x in self.c.get("/api/purchase-batches").get_json()}
        self.assertEqual((lst[bid]["supplierId"], lst[bid]["supplierName"], lst[bid]["supplierOrigName"]),
                         (b, "태화무역", "태화 무역"))
        d = self.c.get(f"/api/purchase-batches/{bid}").get_json()
        self.assertEqual((d["supplierId"], d["supplierName"], d["supplierOrigName"]), (b, "태화무역", "태화 무역"))
        ad = self.c.get(f"/api/assets/{aid}").get_json()
        self.assertEqual((ad["batch"]["supplierName"], ad["batch"]["supplierOrigName"]), ("태화무역", "태화 무역"))
        # 전표 검색은 대표 이름으로도 원문으로도 찾는다
        self.assertEqual([x["id"] for x in self.c.get("/api/purchase-batches?q=태화무역").get_json()], [bid])
        self.assertEqual([x["id"] for x in self.c.get("/api/purchase-batches?q=태화 무역").get_json()], [bid])
        # 미지급 요약(거래처 id 축은 그대로, 이름만 대표)
        pay = self.c.get("/api/purchase/payables").get_json()["suppliers"]
        self.assertEqual([(p["supplierId"], p["supplier"]) for p in pay], [(b, "태화무역")])
        # 판매 전표 라인에 적히는 매입처명도 대표 이름
        self.sql("UPDATE assets SET asset_no='260901-0001', status='ready' WHERE id=?", aid)
        r = self.c.post("/api/sale-slips", json={"channel": "방문구매", "customer": "손님", "saleDate": "2026-09-02",
                                                 "lines": [{"assetNo": "260901-0001", "salePrice": 5000}]})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertEqual(self.sql("SELECT supplier_name FROM tms_sales")[0]["supplier_name"], "태화무역")
        # 집계 규칙은 그대로 — 별칭 행의 전표는 별칭 행에 남고, 대표에는 groupBatchCount 로만 합산돼 보인다
        sup = {s["id"]: s for s in self.c.get("/api/suppliers").get_json()}
        self.assertEqual((sup[a]["batchCount"], sup[a]["groupBatchCount"], sup[b]["batchCount"]), (0, 1, 1))
        self.assertEqual(self.sql("SELECT supplier_id FROM purchase_batches WHERE id=?", bid)[0]["supplier_id"], b)

    def test_전표_거래처명이_별칭이나_부_신원이면_대표에_붙는다(self):
        """§8-3(2026-09-03): 전표 등록의 supplierName 이 별칭 표기·공백 차이·부 신원(TMS) 이름이어도 대표 행에 붙는다. 자동 병합은 없다."""
        a, b = self._alias_pair()
        self.assertEqual(self.c.post(f"/api/suppliers/{a}/canonical", json={"aliasIds": [b]}).status_code, 200)

        def batch_supplier(name):
            r = self.c.post("/api/purchase-batches", json={"stage": "purchased", "purchaseDate": "2026-09-01",
                                                           "supplierName": name, "totalAmount": 0})
            self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
            return self.sql("SELECT supplier_id FROM purchase_batches WHERE id=?", r.get_json()["id"])[0]["supplier_id"]

        self.assertEqual(batch_supplier("태화 무역"), a)          # 별칭 표기 → 대표
        self.assertEqual(batch_supplier("태 화 무 역"), a)        # 공백·대소문자만 다른 표기 → 대표(별칭 행이 아니라)
        self.assertEqual(batch_supplier("태화무역"), a)
        # 부 신원: 같은 이름의 TMS 신원 둘(키 4·5)이 'HNC' 한 행에 붙은 뒤 사람이 행 이름을 바꿔도, TMS 이름으로 찾는다
        FakeClient.partners += [tms_partner(4, "HNC"), tms_partner(5, "HNC", 전화번호="02-5")]
        res = self.sync_partners()
        self.assertEqual((res["created"], res["dups"]), (1, 1))
        h = self.sql("SELECT id FROM suppliers WHERE name='HNC'")[0]["id"]
        self.assertEqual(self.c.patch(f"/api/suppliers/{h}", json={"name": "에이치엔씨"}).status_code, 200)
        self.assertEqual(batch_supplier("hnc"), h)
        # 아무 데도 없는 이름은 예전처럼 새 거래처 — 행이 합쳐지거나 이름이 바뀐 곳은 없다
        n = batch_supplier("낯선곳")
        self.assertNotIn(n, (a, b, h))
        self.assertEqual(sorted(s["name"] for s in self.sql("SELECT name FROM suppliers")),
                         ["낯선곳", "에이치엔씨", "태화 무역", "태화무역"])

    def test_TMS_매입현황_거래처명도_별칭과_부_신원을_안다(self):
        """§8-3(2026-09-03): migration.apply_rows 가 거래처명으로 전표를 만들 때 별칭·표기 차이·부 신원 이름이 대표 행에 붙는다."""
        from app.db import tx
        from app.purchase.migration import _prepare, apply_rows
        a, b = self._alias_pair()
        self.assertEqual(self.c.post(f"/api/suppliers/{a}/canonical", json={"aliasIds": [b]}).status_code, 200)
        FakeClient.partners += [tms_partner(4, "HNC"), tms_partner(5, "HNC")]
        self.sync_partners()
        h = self.sql("SELECT id FROM suppliers WHERE name='HNC'")[0]["id"]
        self.c.patch(f"/api/suppliers/{h}", json={"name": "에이치엔씨"})
        rows = [{"관리번호": "260901-0101", "모델명": "L480", "거래처명": "태화 무역", "매입일": "2026-09-01"},
                {"관리번호": "260901-0102", "모델명": "L480", "거래처명": "태 화무역", "매입일": "2026-09-01"},
                {"관리번호": "260901-0103", "모델명": "L480", "거래처명": "HNC", "매입일": "2026-09-01"},
                {"관리번호": "260901-0104", "모델명": "L480", "거래처명": "새매입처", "매입일": "2026-09-01"}]
        with self.app.app_context():
            with tx(write=True) as conn:
                ready, dup, errors, updates, _lk = _prepare(conn, rows, fill_blanks=True)
                self.assertEqual(errors, [])
                apply_rows(conn, ready, updates, actor="자동반영")
        by_no = {r["asset_no"]: r["supplier_id"] for r in self.sql(
            "SELECT a.asset_no, b.supplier_id FROM assets a JOIN purchase_batches b ON b.id=a.batch_id")}
        self.assertEqual((by_no["260901-0101"], by_no["260901-0102"], by_no["260901-0103"]), (a, a, h))
        new = self.sql("SELECT id FROM suppliers WHERE name='새매입처'")
        self.assertEqual(len(new), 1)
        self.assertEqual(by_no["260901-0104"], new[0]["id"])
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM suppliers")[0]["c"], 4)     # 대표·별칭·에이치엔씨·새매입처

    def test_전표_자동생성_거래처는_매입_구분(self):
        r = self.c.post("/api/purchase-batches", json={"stage": "purchased", "purchaseDate": "2026-09-01",
                                                       "supplierName": "처음보는곳", "totalAmount": 0})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        s = self.sql("SELECT * FROM suppliers WHERE name='처음보는곳'")[0]
        self.assertEqual((s["kind"], s["source"], s["tms_key_id"]), ("매입", "ows", None))

    def test_권한과_라우트_교체(self):
        for ep in ("list_suppliers", "create_supplier", "update_supplier"):
            self.assertEqual(self.app.view_functions[f"purchase.{ep}"].__module__, "app.purchase.masters", ep)
        w = self.worker(["purchase.view"])
        self.assertEqual(w.get("/api/suppliers").status_code, 200)
        self.assertEqual(w.post("/api/suppliers", json={"name": "x"}).status_code, 403)
        sid = self.c.post("/api/suppliers", json={"name": "y"}).get_json()["id"]
        self.assertEqual(w.post(f"/api/suppliers/{sid}/canonical", json={"aliasIds": []}).status_code, 403)

    def test_화면_배선(self):
        js = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")
        for needle in ('["models", "💻 모델 마스터"]', "models: renderModelMaster", "async function renderModelMaster",
                       'attachModelMaster("#ar-model", "#ar-maker", "#ar-cat", "#ar-master")',
                       'attachModelMaster("#sa-model", "#sa-maker", "#sa-cat", "#sa-master")',
                       'attachModelMaster("#ad-model", "#ad-maker", null, "#ad-master")',
                       'attachAutocomplete($("#se-customer", host), "customer")', "/api/models/lookup",
                       "data-splinks", "function openSupplierTmsLinks", "function openSupplierEditor",
                       "/canonical", "/tms-primary", "/tms-split", 'id="sp-kind"', 'id="sp-bizno"', "/api/masters/sync",
                       "modelWarning"):
            self.assertIn(needle, js, needle)
        self.assertEqual(js.count('attachAutocomplete($("#se-customer", host), "customer")'), 2)   # 새 전표·상세 둘 다
        ac = (ROOT / "static" / "js" / "autocomplete.js").read_text("utf-8")
        self.assertIn('o.source === "master"', ac)


# ══════════════════════════════════════════════════════════════ 모델명 정비 캠페인(2026-09-03, §8-4)
class TestMissingModels(Base):
    def _asset(self, model, maker="", status=None, cat=None):
        a = self.c.post("/api/assets", json={"categoryId": cat or self.cat, "model": model, "maker": maker, "qty": 1}).get_json()[0]
        if status:
            self.sql("UPDATE assets SET status=? WHERE id=?", status, a["id"])
        return a["id"]

    def test_마스터에_없는_모델_목록(self):
        cats = self.c.get("/api/categories").get_json()
        other = next(c for c in cats if c["id"] != self.cat)
        self._asset("NT950XDB", "SAMSUNG")
        self._asset("nt 950xdb", "SAMSUNG")                     # 표기만 다른 같은 모델 — 한 줄로 묶인다
        self._asset("NT950XDB", "LG", cat=other["id"])          # 브랜드 추정은 최다값(SAMSUNG 2 : LG 1)
        self._asset("L480", "LENOVO")
        self._asset("L480", "", status="cancelled")             # 매입취소는 안 센다
        self._asset("X1C", "LENOVO", status="returned")         # 거래처반품은 안 센다 → 목록에 없음
        self._asset("T480", "LENOVO", status="shipped")         # 판매된 자산도 센다
        self.assertEqual(self.c.post("/api/models", json={"name": "L 480", "brand": "LENOVO"}).status_code, 201)   # 마스터에 있음(정규화)
        r = self.c.get("/api/models/missing")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d["total"], 2)
        self.assertEqual(d["liveModels"], 3)                     # NT950XDB · L480 · T480 (X1C 는 죽은 자산뿐)
        self.assertEqual([(it["name"], it["count"], it["brand"], it["spellings"]) for it in d["items"]],
                         [("NT950XDB", 3, "SAMSUNG", 2), ("T480", 1, "LENOVO", 1)])
        self.assertEqual(d["items"][0]["category"], self.cat_name)
        self.assertEqual(len(d["items"][0]["lastAt"]), 10)
        self.assertEqual(self.c.get("/api/models/missing?limit=1").get_json()["items"][0]["name"], "NT950XDB")
        self.assertEqual(self.c.get("/api/models/missing?limit=1").get_json()["total"], 2)
        # 등록 창구는 기존 POST /models — 올리면 목록에서 빠진다(같은 정규화 키의 다른 표기도 함께)
        self.assertEqual(self.c.post("/api/models", json={"name": "NT950XDB", "brand": "SAMSUNG", "category": "PC", "subcategory": "노트북"}).status_code, 201)
        self.assertEqual([it["name"] for it in self.c.get("/api/models/missing").get_json()["items"]], ["T480"])
        self.assertEqual(self.c.get("/api/models/lookup?name=nt 950xdb").get_json()["found"], True)
        # 권한: 매입 조회
        w = self.worker([])
        self.assertEqual(w.get("/api/models/missing").status_code, 403)

    def test_화면_배선(self):
        js = (ROOT / "static" / "js" / "purchase.js").read_text("utf-8")
        for needle in ("/api/models/missing", 'id="mm-missing"', "function openModelRegister", "data-mmreg",
                       'id="mmx-bulk"', "data-mmx", "const loadMissing"):
            self.assertIn(needle, js, needle)
        # 새 최상위 탭 금지 — 기준정보 서브탭 안(BASE_VIEWS)에만 있다
        self.assertNotIn('"missing"', js.split("const BASE_VIEWS = [", 1)[1].split("];", 1)[0])


if __name__ == "__main__":
    unittest.main()
