"""A5 재고 카테고리 정합 — 카테고리 = TMS 중분류 축(2026-09-03).

  ① 연동 규칙(migration): 중분류 8종 대응(버즈→웨어러블)·빈값→모델 마스터 보충·대분류 폴백·기본값(노트북)·
     잠금 자산 미변경·그림자 3방향(자동이 정한 값만 따라가고 사람이 바꾼 값은 보호, 옛 자산은 무접촉)
  ② 마스터 보정(masters.complete_asset_body): 태블릿·모니터·웨어러블 모델의 카테고리를 채운다 — tests/test_masters.py 에도 1건
  ③ 재판정(reclass): 미리보기 건수·묶음 → 적용(멱등·compare-and-set·보호·잠금·출고 제외·개별 포함) → 되돌리기, 권한, 창구 없음
  ④ 시드(db._migrate categories_seed_v2): 'PC'→'데스크탑' 개명(마커 1회·새 이름 있으면 건너뜀)·없는 이름만 추가·노트북 sort 0 유지·폴백
★임시 DB만 쓴다. 창구는 가짜 클라이언트(tms_link.Client 교체, env 는 setUp/tearDown 안에서만) 또는 미설정(모델 마스터만).
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OWS_NO_TMS_SYNC"] = "1"

from app import auth as auth_mod  # noqa: E402
from app import config, create_app  # noqa: E402
from app.purchase import migration, reclass, tms_link  # noqa: E402

PW = "admin-pass-1"
USER_PW = "user-pass-12"
EIGHT = ["노트북", "데스크탑", "태블릿", "모니터", "미니PC", "일체형PC", "주변기기", "웨어러블"]
_KEY = [0]


def stock_row(no, sub="", main="PC", model="", **kw):
    _KEY[0] += 1
    row = {"키ID": _KEY[0], "관리번호": no, "대분류": main, "중분류": sub, "모델명": model, "브랜드": "SAMSUNG",
           "재고상태": "매입", "_synced_at": "2026-09-03 10:00:00", "_deleted_at": None}
    row.update(kw)
    return row


class FakeClient:
    """창구 흉내 — /tables/HB_TBL재고(페이징 규칙은 test_masters 와 같다). 자산 등록의 번호 검증 경로는 조용히 통과."""
    stock = []
    fail = False
    calls = []

    def __init__(self, url="", token=""):
        pass

    def get(self, path, **params):
        FakeClient.calls.append((path, params))
        if path.startswith("/tables/"):
            if FakeClient.fail:
                raise RuntimeError("연동 창구 오류 /tables: 500")
            table = path.split("/tables/", 1)[1]
            rows = sorted(FakeClient.stock if table == reclass.STOCK_TABLE else [], key=lambda r: r["키ID"])
            after = params.get("after_key")
            if after is not None:
                rows = [r for r in rows if r["키ID"] > int(after)]
            limit = int(params.get("limit") or 500)
            page = rows[:limit]
            nxt = page[-1]["키ID"] if len(rows) > limit else None
            return {"ok": True, "table": table, "count": len(page), "next_after_key": nxt, "items": page}
        if path == "/asset-facts":
            return {"ok": True, "items": [], "next_after_key": None, "freshness": {"stale": False}}
        if path == "/mgmt-check":
            return {"ok": True, "result": {n: {"verdict": "ok"} for n in (params.get("nos") or "").split(",")}}
        if path == "/status":
            return {"ok": True, "status": {"freshness": {"stale": False, "age_sec": 5}}}
        return {"ok": True, "items": [], "next_after_key": None}


class Base(unittest.TestCase):
    use_client = False

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-reclass-"))
        self.db_path = self.tmp / "t.db"
        self.app = create_app(db_path=self.db_path)
        self.app.testing = True
        self.c = self.app.test_client()
        auth_mod._login_failures.clear()
        self.c.post("/api/auth/setup", json={"username": "admin", "displayName": "대표", "password": PW})
        self.cats = {c["name"]: c["id"] for c in self.c.get("/api/categories").get_json()}
        self.names = {v: k for k, v in self.cats.items()}
        FakeClient.stock, FakeClient.fail, FakeClient.calls = [], False, []
        self._env = {k: os.environ.get(k) for k in ("OWS_DATALINK_URL", "OWS_DATALINK_TOKEN")}
        self._client = tms_link.Client
        if self.use_client:
            os.environ["OWS_DATALINK_URL"], os.environ["OWS_DATALINK_TOKEN"] = "http://fake", "t"
            tms_link.Client = FakeClient
        else:
            os.environ.pop("OWS_DATALINK_URL", None)
            os.environ.pop("OWS_DATALINK_TOKEN", None)

    def tearDown(self):
        tms_link.Client = self._client
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 도우미
    def sql(self, q, *args):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(q, args).fetchall()]
            conn.commit()
            return rows
        finally:
            conn.close()

    def sheet(self, rows):
        """TMS 행(엑셀·창구 공통 모양)을 autosync/tms_link 와 같은 코드 경로로 반영한다."""
        with self.app.app_context():
            from app.db import tx
            with tx(write=True) as conn:
                ready, dup, errors, updates, locked = migration._prepare(conn, rows, fill_blanks=True)
                res = migration.apply_rows(conn, ready, updates, actor="자동반영")
                return res, errors, locked

    def asset(self, no):
        row = self.sql("SELECT * FROM assets WHERE asset_no=?", no)
        self.assertTrue(row, f"자산 없음 {no}")
        return row[0]

    def cat_of(self, no):
        return self.names.get(self.asset(no)["category_id"])

    def events(self, no):
        return [dict(r) for r in self.sql(
            "SELECT e.action, e.detail FROM asset_events e JOIN assets a ON a.id=e.asset_id WHERE a.asset_no=? ORDER BY e.id", no)]

    def worker(self, perms, username="worker1"):
        r = self.c.post("/api/users", json={
            "username": username, "displayName": username, "password": USER_PW,
            "perms": perms, "categoryIds": [], "allCategories": True, "isAdmin": False})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        self.assertEqual(c.post("/api/auth/login", json={"username": username, "password": USER_PW}).status_code, 200)
        return c


# ══════════════════════════════════════════════════════════════ ① 연동 규칙
class TestCategoryDecision(Base):
    def test_시드는_TMS_중분류_8종이고_노트북이_기본값(self):
        self.assertEqual([c["name"] for c in self.c.get("/api/categories").get_json()], EIGHT)
        self.assertEqual(config.DEFAULT_CATEGORIES, EIGHT)
        self.assertEqual(self.sql("SELECT sort FROM categories WHERE name='노트북'")[0]["sort"], 0)

    def test_중분류_8종이_카테고리로_간다_버즈는_웨어러블(self):
        pairs = [("노트북", "노트북"), ("데스크탑", "데스크탑"), ("태블릿", "태블릿"), ("모니터", "모니터"),
                 ("미니PC", "미니PC"), ("일체형PC", "일체형PC"), ("주변기기", "주변기기"), ("버즈", "웨어러블")]
        rows = [{"관리번호": f"260901-{i:04d}", "대분류": "PC" if sub != "버즈" else "웨어러블", "중분류": sub,
                 "모델명": f"M{i}", "매입가": "1", "재고상태": "매입"} for i, (sub, _) in enumerate(pairs, 1)]
        res, errors, _ = self.sheet(rows)
        self.assertEqual((res["created"], errors), (8, []))
        for i, (sub, cat) in enumerate(pairs, 1):
            self.assertEqual(self.cat_of(f"260901-{i:04d}"), cat, sub)
        self.assertEqual(migration.TMS_SUBCAT_TO_CATEGORY["버즈"], "웨어러블")
        self.assertEqual(sum(1 for k, v in migration.TMS_SUBCAT_TO_CATEGORY.items() if k != v), 1, "예외는 버즈뿐")

    def test_중분류_빈값은_모델_마스터_중분류로_채운다(self):
        self.c.post("/api/models", json={"name": "SM-T505N", "brand": "SAMSUNG", "category": "태블릿", "subcategory": "태블릿"})
        self.c.post("/api/models", json={"name": "E2700MFP", "brand": "기타", "category": "PC", "subcategory": "모니터"})
        res, errors, _ = self.sheet([
            {"관리번호": "260901-0001", "대분류": "태블릿", "중분류": "", "모델명": "sm-t505n", "매입가": "1"},
            {"관리번호": "260901-0002", "대분류": "PC", "중분류": "", "모델명": "E2700MFP", "매입가": "1"}])
        self.assertEqual(res["created"], 2)
        self.assertEqual(self.cat_of("260901-0001"), "태블릿", "정규화 이름(소문자)으로도 마스터를 찾아야 한다")
        self.assertEqual(self.cat_of("260901-0002"), "모니터")

    def test_마스터에도_없으면_대분류_그래도_없으면_기본값_노트북(self):
        res, errors, _ = self.sheet([
            {"관리번호": "260901-0001", "대분류": "모니터", "모델명": "ZZ-1", "매입가": "1"},     # 대분류 자유 입력값
            {"관리번호": "260901-0002", "대분류": "웨어러블", "모델명": "ZZ-2", "매입가": "1"},
            {"관리번호": "260901-0003", "대분류": "PC", "모델명": "ZZ-3", "매입가": "1"},         # 결정 ③: PC 만 → 노트북
            {"관리번호": "260901-0004", "모델명": "ZZ-4", "매입가": "1"}])                         # 아무것도 없음 → 노트북
        self.assertEqual((res["created"], errors), (4, []))
        self.assertEqual([self.cat_of(f"260901-000{i}") for i in range(1, 5)], ["모니터", "웨어러블", "노트북", "노트북"])

    def test_중분류가_대분류보다_먼저다(self):
        self.sheet([{"관리번호": "260901-0001", "대분류": "모니터", "중분류": "노트북", "모델명": "X", "매입가": "1"}])
        self.assertEqual(self.cat_of("260901-0001"), "노트북")

    def test_신규_자산_그림자에_카테고리가_남고_TMS가_바꾸면_따라간다(self):
        self.sheet([{"관리번호": "260901-0001", "대분류": "PC", "중분류": "노트북", "모델명": "X", "매입가": "1", "재고상태": "매입"}])
        a = self.asset("260901-0001")
        self.assertEqual(json.loads(a["tms_shadow"])["category"], self.cats["노트북"])
        res, _, _ = self.sheet([{"관리번호": "260901-0001", "대분류": "PC", "중분류": "모니터", "모델명": "X", "매입가": "1", "재고상태": "매입"}])
        self.assertEqual(res["categoryUpdated"], 1)
        self.assertEqual(self.cat_of("260901-0001"), "모니터")
        ev = [e for e in self.events("260901-0001") if e["action"] == "분류갱신"]
        self.assertEqual(len(ev), 1)
        d = json.loads(ev[0]["detail"])
        self.assertEqual((d["from"], d["to"], d["근거"]), ("노트북", "모니터", "TMS 중분류 모니터"))
        self.assertEqual(json.loads(self.asset("260901-0001")["tms_shadow"])["category"], self.cats["모니터"])
        # 같은 행을 다시 보내면 아무것도 안 바뀐다(멱등)
        res, _, _ = self.sheet([{"관리번호": "260901-0001", "대분류": "PC", "중분류": "모니터", "모델명": "X", "매입가": "1", "재고상태": "매입"}])
        self.assertEqual(res["categoryUpdated"], 0)

    def test_사람이_바꾼_카테고리는_TMS가_안_덮는다(self):
        self.sheet([{"관리번호": "260901-0001", "대분류": "PC", "중분류": "노트북", "모델명": "X", "매입가": "1", "재고상태": "매입"}])
        a = self.asset("260901-0001")
        r = self.c.patch(f"/api/assets/{a['id']}", json={"categoryId": self.cats["모니터"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        res, _, _ = self.sheet([{"관리번호": "260901-0001", "대분류": "PC", "중분류": "태블릿", "모델명": "X", "매입가": "1", "재고상태": "매입"}])
        self.assertEqual(res["categoryUpdated"], 0, "★사람이 고친 카테고리를 TMS 가 덮었다")
        self.assertEqual(self.cat_of("260901-0001"), "모니터")
        # 그림자는 자동이 정했던 값(노트북) 그대로 — 다른 칸 갱신으로 그림자를 다시 써도 키가 빠지지 않는다
        self.sheet([{"관리번호": "260901-0001", "대분류": "PC", "중분류": "태블릿", "모델명": "X", "매입가": "1",
                     "CPU": "i7", "재고상태": "매입"}])
        self.assertEqual(json.loads(self.asset("260901-0001")["tms_shadow"])["category"], self.cats["노트북"])

    def test_그림자에_카테고리가_없는_옛_자산은_연동이_안_건드린다(self):
        a = self.c.post("/api/assets", json={"categoryId": self.cats["데스크탑"], "model": "X", "qty": 1}).get_json()[0]
        self.c.patch(f"/api/assets/{a['id']}", json={"assetNo": "260901-0001"})
        res, _, _ = self.sheet([{"관리번호": "260901-0001", "대분류": "PC", "중분류": "노트북", "모델명": "X", "매입가": "1", "재고상태": "매입"}])
        self.assertEqual(res["categoryUpdated"], 0)
        self.assertEqual(self.cat_of("260901-0001"), "데스크탑")
        self.assertNotIn("category", json.loads(self.asset("260901-0001")["tms_shadow"] or "{}"))

    def test_잠긴_자산은_카테고리도_안_바뀐다(self):
        self.sheet([{"관리번호": "260901-0001", "대분류": "PC", "중분류": "노트북", "모델명": "X", "매입가": "1", "재고상태": "매입"}])
        self.sql("UPDATE assets SET tms_lock=1 WHERE asset_no='260901-0001'")
        res, _, locked = self.sheet([{"관리번호": "260901-0001", "대분류": "PC", "중분류": "모니터", "모델명": "X", "매입가": "1"}])
        self.assertEqual((res["categoryUpdated"], locked), (0, ["260901-0001"]))
        self.assertEqual(self.cat_of("260901-0001"), "노트북")

    def test_개명_전_DB_폴백_데스크탑이_없으면_PC(self):
        self.c.patch(f"/api/categories/{self.cats['데스크탑']}", json={"name": "PC"})     # 대표가 되돌린 상황
        with self.app.app_context():
            from app.db import tx
            with tx() as conn:
                cats = migration.category_ids(conn)
        self.assertEqual(cats["데스크탑"], self.cats["데스크탑"])
        self.assertEqual(cats["PC"], self.cats["데스크탑"])
        self.sheet([{"관리번호": "260901-0001", "대분류": "PC", "중분류": "데스크탑", "모델명": "X", "매입가": "1"}])
        self.assertEqual(self.asset("260901-0001")["category_id"], self.cats["데스크탑"])

    def test_헤더_중분류가_subcategory로_들어온다(self):
        self.assertEqual(migration.HEADER_MAP["중분류"], "subcategory")
        self.assertEqual(migration._row_to_asset({"관리번호": "1", "중분류": " 노트북 "})["subcategory"], "노트북")


# ══════════════════════════════════════════════════════════════ ② 마스터 보정
class TestMasterCompletion(Base):
    def test_등록시_마스터_중분류로_카테고리를_채운다(self):
        self.c.post("/api/models", json={"name": "SM-T505N", "brand": "SAMSUNG", "category": "태블릿", "subcategory": "태블릿"})
        self.c.post("/api/models", json={"name": "SM-R180", "brand": "SAMSUNG", "category": "웨어러블", "subcategory": "버즈"})
        self.c.post("/api/models", json={"name": "ONLY-MAIN", "brand": "LG", "category": "모니터", "subcategory": ""})
        for model, cat in (("SM-T505N", "태블릿"), ("SM-R180", "웨어러블"), ("ONLY-MAIN", "모니터")):
            r = self.c.post("/api/assets", json={"model": model, "qty": 1})
            self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
            self.assertEqual(r.get_json()[0]["modelFilled"]["categoryId"], self.cats[cat], model)
        # 마스터가 'PC'만 알면(중분류 없음) 채우지 않고 400 — 추측하지 않는다
        self.c.post("/api/models", json={"name": "PC-ONLY", "brand": "LG", "category": "PC", "subcategory": ""})
        self.assertEqual(self.c.post("/api/assets", json={"model": "PC-ONLY", "qty": 1}).status_code, 400)


# ══════════════════════════════════════════════════════════════ ③ 재판정
class TestReclass(Base):
    use_client = True

    def seed(self):
        """옛 자산 모양 — TMS 연동으로 들어와 카테고리가 틀린 채 굳은 자산(그림자에 category 키 없음)."""
        rows = [{"관리번호": f"260801-{i:04d}", "대분류": "PC", "중분류": "노트북", "모델명": f"NT{i}", "매입가": "1", "재고상태": "매입"}
                for i in range(1, 7)]
        rows[4]["재고상태"] = "판매"                       # 0005: 출고 완료
        rows[5]["재고상태"] = "판매"                       # 0006: 출고 완료 + 잠금
        self.sheet(rows)
        # 연동 당시 규칙이 'PC'로 넣었던 상태를 재현: 카테고리를 데스크탑으로, 그림자 category 키 제거
        for r in self.sql("SELECT id, tms_shadow FROM assets"):
            sh = json.loads(r["tms_shadow"] or "{}")
            sh.pop("category", None)
            self.sql("UPDATE assets SET category_id=?, tms_shadow=? WHERE id=?", self.cats["데스크탑"], json.dumps(sh), r["id"])
        self.sql("UPDATE assets SET tms_lock=1 WHERE asset_no='260801-0006'")
        # 창구 사본: 4대는 노트북, 0002 는 태블릿(빈 중분류 → 마스터), 0003 은 이미 맞는 데스크탑
        self.c.post("/api/models", json={"name": "NT2", "brand": "SAMSUNG", "category": "태블릿", "subcategory": "태블릿"})
        FakeClient.stock = [
            stock_row("260801-0001", sub="노트북", model="NT1"),
            stock_row("260801-0002", sub="", main="태블릿", model="NT2"),
            stock_row("260801-0003", sub="데스크탑", model="NT3"),
            stock_row("260801-0004", sub="노트북", model="NT4"),
            stock_row("260801-0005", sub="노트북", model="NT5", 재고상태="판매"),
            stock_row("260801-0006", sub="노트북", model="NT6", 재고상태="판매"),
            stock_row("260801-9999", sub="노트북", model="없는자산"),          # OWS 에 없는 TMS 행 — 무시
        ]

    def preview(self, **q):
        r = self.c.get("/api/assets/reclass/preview", query_string=q)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def apply(self, **body):
        r = self.c.post("/api/assets/reclass/apply", json=body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def test_미리보기_건수와_묶음(self):
        self.seed()
        d = self.preview()
        self.assertEqual(d["basis"], "datalink")
        self.assertIsNone(d["warning"])
        self.assertEqual(d["tmsRows"], 7)
        t = d["totals"]
        self.assertEqual((t["assets"], t["inTms"], t["change"], t["same"], t["undecided"]), (6, 6, 5, 1, 0))
        self.assertEqual((t["protected"], t["locked"], t["gone"], t["willApply"]), (0, 1, 2, 4))   # 기본: 출고 포함·잠금 제외
        rows = {(c["from"], c["to"]): c for c in d["changes"]}
        self.assertEqual(set(rows), {("데스크탑", "노트북"), ("데스크탑", "태블릿")})
        nb = rows[("데스크탑", "노트북")]
        self.assertEqual((nb["count"], nb["alive"], nb["gone"], nb["lockedAlive"], nb["lockedGone"], nb["protected"], nb["willApply"]),
                         (4, 2, 1, 0, 1, 0, 3))
        self.assertEqual(nb["basis"], {"TMS 중분류 노트북": 4})
        self.assertEqual(rows[("데스크탑", "태블릿")]["basis"], {"모델 마스터 중분류 태블릿": 1})
        self.assertEqual(d["before"], {"데스크탑": 6})
        self.assertEqual(d["after"], {"데스크탑": 2, "노트북": 3, "태블릿": 1})
        self.assertEqual(d["categories"], EIGHT)
        self.assertIsNone(d["lastApply"])
        # 체크를 바꾸면 서버도 같은 계산을 내놓는다(화면은 묶음으로 바로 세고, 적용은 서버가 다시 센다)
        self.assertEqual(self.preview(includeShipped=0)["totals"]["willApply"], 3)
        self.assertEqual(self.preview(includeShipped=0, includeLocked=1)["totals"]["willApply"], 3)
        self.assertEqual(self.preview(includeLocked=1)["totals"]["willApply"], 5)
        # 저장은 없다
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM assets WHERE category_id=?", self.cats["데스크탑"])[0]["c"], 6)
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM asset_events WHERE action='분류재판정'")[0]["c"], 0)

    def test_적용은_멱등이고_이력_그림자_감사를_남긴다(self):
        self.seed()
        r = self.apply()
        self.assertEqual((r["applied"], r["skipped"]), (4, 0))
        self.assertEqual(r["changes"], [{"from": "데스크탑", "to": "노트북", "count": 3}, {"from": "데스크탑", "to": "태블릿", "count": 1}])
        self.assertEqual(r["after"], {"데스크탑": 2, "노트북": 3, "태블릿": 1})
        self.assertEqual(self.cat_of("260801-0001"), "노트북")
        self.assertEqual(self.cat_of("260801-0002"), "태블릿")
        self.assertEqual(self.cat_of("260801-0003"), "데스크탑")
        self.assertEqual(self.cat_of("260801-0005"), "노트북", "출고 완료도 기본 포함")
        self.assertEqual(self.cat_of("260801-0006"), "데스크탑", "잠금은 기본 제외")
        ev = [e for e in self.events("260801-0001") if e["action"] == "분류재판정"]
        self.assertEqual(len(ev), 1)
        d = json.loads(ev[0]["detail"])
        self.assertEqual(d, {"from": "데스크탑", "to": "노트북", "근거": "TMS 중분류 노트북",
                             "ids": [self.cats["데스크탑"], self.cats["노트북"]], "audit": r["auditId"]})
        self.assertEqual(json.loads(self.asset("260801-0001")["tms_shadow"])["category"], self.cats["노트북"])
        a = self.sql("SELECT action, target, detail FROM audit_log WHERE id=?", r["auditId"])[0]
        self.assertEqual((a["action"], a["target"]), ("assets_reclassified", "4대"))
        self.assertEqual(json.loads(a["detail"])["applied"], 4)
        # 두 번째는 0건 — 미리보기도 변경 0(잠금 1대만 남는다)
        self.assertEqual(self.apply()["applied"], 0)
        d2 = self.preview()
        self.assertEqual((d2["totals"]["change"], d2["totals"]["willApply"]), (1, 0))
        self.assertEqual(d2["lastApply"]["applied"], 0)
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM asset_events WHERE action='분류재판정'")[0]["c"], 4)
        # 재분류 뒤 연동이 와도 되돌리지 않는다(그림자 == 현재값 == TMS 판정)
        res, _, _ = self.sheet([{"관리번호": "260801-0001", "대분류": "PC", "중분류": "노트북", "모델명": "NT1", "매입가": "1", "재고상태": "매입"}])
        self.assertEqual(res["categoryUpdated"], 0)
        self.assertEqual(self.cat_of("260801-0001"), "노트북")

    def test_체크_출고_제외_잠금_포함_개별_ids(self):
        self.seed()
        r = self.apply(includeShipped=False, includeLocked=True)
        self.assertEqual(r["applied"], 3)                          # 0001·0002·0004 (0005 출고 제외, 0006 출고+잠금 제외)
        self.assertEqual(self.cat_of("260801-0006"), "데스크탑")
        r = self.apply(includeShipped=True, includeLocked=True, ids=[self.asset("260801-0006")["id"]])
        self.assertEqual(r["applied"], 1)
        self.assertEqual(self.cat_of("260801-0006"), "노트북")
        self.assertEqual(self.cat_of("260801-0005"), "데스크탑", "ids 를 주면 그 자산만")
        self.assertEqual(self.c.post("/api/assets/reclass/apply", json={"ids": "x"}).status_code, 400)

    def test_보호_자산은_기본_제외_개별_체크로만(self):
        self.seed()
        # ⓐ 카테고리 수정 이력  ⓑ 수기 등록  ⓑ OWS 번호대  ⓒ 자동 판정 뒤 사람이 바꿈
        a1 = self.asset("260801-0001")
        self.c.patch(f"/api/assets/{a1['id']}", json={"categoryId": self.cats["모니터"]})
        m = self.c.post("/api/assets", json={"categoryId": self.cats["데스크탑"], "model": "NT9", "qty": 1}).get_json()[0]
        self.c.patch(f"/api/assets/{m['id']}", json={"assetNo": "260801-0009"})
        FakeClient.stock.append(stock_row("260801-0009", sub="노트북", model="NT9"))
        self.sheet([{"관리번호": "260801-5001", "대분류": "PC", "중분류": "노트북", "모델명": "NT10", "매입가": "1"}])
        self.sql("UPDATE assets SET category_id=?, tms_shadow='{}' WHERE asset_no='260801-5001'", self.cats["데스크탑"])
        FakeClient.stock.append(stock_row("260801-5001", sub="노트북", model="NT10"))
        a4 = self.asset("260801-0004")
        self.sql("UPDATE assets SET tms_shadow=? WHERE id=?", json.dumps({"category": self.cats["노트북"]}), a4["id"])   # 자동이 노트북으로 정했는데 지금은 데스크탑
        d = self.preview()
        prot = {p["assetNo"]: p for p in d["protected"]}
        self.assertEqual(set(prot), {"260801-0001", "260801-0009", "260801-5001", "260801-0004"})
        self.assertEqual(prot["260801-0001"]["protected"], "edited")
        self.assertEqual(prot["260801-0009"]["protected"], "manual")
        self.assertEqual(prot["260801-5001"]["protected"], "band")
        self.assertEqual(prot["260801-0004"]["protected"], "shadow")
        self.assertEqual(prot["260801-0004"]["protectedLabel"], "자동 판정 뒤 사람이 바꿈")
        self.assertEqual(d["totals"]["protected"], 4)
        r = self.apply()
        self.assertEqual(r["applied"], 2)                          # 0002·0005 만
        self.assertEqual(self.cat_of("260801-0001"), "모니터")
        self.assertEqual(self.cat_of("260801-0009"), "데스크탑")
        r = self.apply(protectedIds=[a1["id"], m["id"]])
        self.assertEqual(r["applied"], 2)
        self.assertEqual((self.cat_of("260801-0001"), self.cat_of("260801-0009")), ("노트북", "노트북"))

    def test_경합_읽었을_때_값일_때만_바꾼다(self):
        self.seed()
        real = reclass._decide
        target = self.asset("260801-0001")["id"]

        def racing(conn, tms):
            out = real(conn, tms)
            conn.execute("UPDATE assets SET category_id=? WHERE id=?", (self.cats["모니터"], target))   # 판정 뒤 누가 바꿨다
            return out
        with mock.patch.object(reclass, "_decide", side_effect=racing):
            r = self.apply()
        self.assertEqual((r["applied"], r["skipped"]), (3, 1))
        self.assertEqual(self.cat_of("260801-0001"), "모니터", "★읽은 뒤 바뀐 값을 덮었다")
        self.assertEqual(len([e for e in self.events("260801-0001") if e["action"] == "분류재판정"]), 0)

    def test_되돌리기(self):
        self.seed()
        r = self.apply()
        self.assertEqual(r["applied"], 4)
        # 그 사이 사람이 하나를 다시 바꿨다 — 건너뛴다
        a2 = self.asset("260801-0002")
        self.c.patch(f"/api/assets/{a2['id']}", json={"categoryId": self.cats["모니터"]})
        # 카테고리 이름이 바뀌어도(개명) id 로 되돌린다
        self.c.patch(f"/api/categories/{self.cats['데스크탑']}", json={"name": "PC"})
        u = self.c.post("/api/assets/reclass/undo", json={"auditId": r["auditId"]})
        self.assertEqual(u.status_code, 200, u.get_data(as_text=True))
        self.assertEqual((u.get_json()["restored"], u.get_json()["skipped"]), (3, 1))
        self.assertEqual(self.asset("260801-0001")["category_id"], self.cats["데스크탑"])
        self.assertEqual(self.asset("260801-0005")["category_id"], self.cats["데스크탑"])
        self.assertEqual(self.cat_of("260801-0002"), "모니터")
        self.assertEqual(json.loads(self.asset("260801-0001")["tms_shadow"])["category"], self.cats["데스크탑"])
        ev = [e for e in self.events("260801-0001") if e["action"] == "분류재판정취소"]
        self.assertEqual(json.loads(ev[0]["detail"]), {"from": "노트북", "to": "데스크탑", "audit": r["auditId"]})
        det = json.loads(self.sql("SELECT detail FROM audit_log WHERE id=?", r["auditId"])[0]["detail"])
        self.assertEqual((det["restored"], det["undoSkipped"], det["undoneBy"]), (3, 1, "대표"))
        self.assertTrue(det["undoneAt"])
        self.assertTrue(self.sql("SELECT 1 FROM audit_log WHERE action='assets_reclass_undone'"))
        # 두 번은 안 된다, 없는 기록은 404
        self.assertEqual(self.c.post("/api/assets/reclass/undo", json={"auditId": r["auditId"]}).status_code, 409)
        self.assertEqual(self.c.post("/api/assets/reclass/undo", json={"auditId": 999999}).status_code, 404)
        self.assertEqual(self.preview()["lastApply"]["undoneAt"], det["undoneAt"])

    def test_권한(self):
        self.seed()
        v = self.worker(["purchase.view"], "viewer")
        self.assertEqual(v.get("/api/assets/reclass/preview").status_code, 200)
        self.assertEqual(v.post("/api/assets/reclass/apply", json={}).status_code, 403)
        self.assertEqual(v.post("/api/assets/reclass/undo", json={"auditId": 1}).status_code, 403)
        e = self.worker(["purchase.view", "purchase.edit"], "editor")
        self.assertEqual(e.post("/api/assets/reclass/apply", json={}).status_code, 403, "편집 권한으로는 안 된다 — 관리자만")
        n = self.worker(["orders.view"], "nobody")
        self.assertEqual(n.get("/api/assets/reclass/preview").status_code, 403)
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM asset_events WHERE action='분류재판정'")[0]["c"], 0)

    def test_창구_오류면_502_저장_없음(self):
        self.seed()
        FakeClient.fail = True
        self.assertEqual(self.c.get("/api/assets/reclass/preview").status_code, 502)
        self.assertEqual(self.c.post("/api/assets/reclass/apply", json={}).status_code, 502)
        self.assertEqual(self.sql("SELECT COUNT(*) AS c FROM assets WHERE category_id=?", self.cats["데스크탑"])[0]["c"], 6)

    def test_판정_불가는_그대로_둔다(self):
        self.seed()
        FakeClient.stock = [stock_row("260801-0001", sub="", main="PC", model="없는모델")]     # 중분류·마스터·대분류 전부 못 정함
        d = self.preview()
        # TMS 에 없는 자산은 OWS 모델명으로 마스터를 본다 — NT2 만 마스터(태블릿)에 있어 정해지고 나머지 5대는 판정 불가
        self.assertEqual(d["totals"]["undecided"], 5)
        self.assertEqual(d["undecidedSample"][0]["assetNo"], "260801-0001")
        self.assertEqual(d["totals"]["willApply"], 1)
        self.assertEqual(self.apply()["applied"], 1)
        self.assertEqual(self.cat_of("260801-0002"), "태블릿")
        self.assertEqual(self.cat_of("260801-0001"), "데스크탑", "판정 불가는 기본값으로 밀지 않는다")


class TestReclassMasterOnly(Base):
    """창구 설정이 없는 서버(검증 서버) — 모델 마스터만으로 판정하고 그 사실을 밝힌다."""

    def test_basis_master_와_경고(self):
        self.sheet([{"관리번호": "260801-0001", "대분류": "PC", "중분류": "노트북", "모델명": "SM-T505N", "매입가": "1"}])
        self.sql("UPDATE assets SET tms_shadow='{}'")
        self.c.post("/api/models", json={"name": "SM-T505N", "brand": "SAMSUNG", "category": "태블릿", "subcategory": "태블릿"})
        d = self.c.get("/api/assets/reclass/preview").get_json()
        self.assertEqual((d["basis"], d["tmsRows"]), ("master", 0))
        self.assertIn("모델 마스터만으로", d["warning"])
        self.assertEqual(d["totals"]["willApply"], 1)
        self.assertEqual(d["changes"][0]["basis"], {"모델 마스터 중분류 태블릿": 1})
        r = self.c.post("/api/assets/reclass/apply", json={}).get_json()
        self.assertEqual((r["applied"], r["basis"]), (1, "master"))
        self.assertEqual(self.cat_of("260801-0001"), "태블릿")


# ══════════════════════════════════════════════════════════════ ④ 시드·개명
class TestCategorySeed(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-catseed-"))
        self.db_path = self.tmp / "legacy.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _legacy(self, names):
        schema = (ROOT / "app" / "schema.sql").read_text("utf-8")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(schema)
        for i, name in enumerate(names):
            conn.execute("INSERT INTO categories(name, sort, enabled, created_at) VALUES(?,?,1,'2026-07-28')", (name, i))
        conn.commit()
        conn.close()

    def _names(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in conn.execute("SELECT name, sort, enabled FROM categories ORDER BY sort, id")]
        finally:
            conn.close()

    def test_라이브_모양_개명과_추가_노트북_sort0_유지_마커_1회(self):
        self._legacy(["노트북", "PC", "B급", "주변기기", "올인원"])          # 라이브 5종(실측 2026-09-03)
        create_app(db_path=self.db_path)
        got = self._names()
        self.assertEqual([g[0] for g in got], ["노트북", "데스크탑", "B급", "주변기기", "일체형PC", "태블릿", "모니터", "미니PC", "웨어러블"])
        self.assertEqual(got[0], ("노트북", 0, 1))
        self.assertEqual([g[2] for g in got], [1] * 9, "B급도 비활성화하지 않는다 — 사람 몫")
        conn = sqlite3.connect(self.db_path)
        self.assertTrue(conn.execute("SELECT 1 FROM settings WHERE key='categories_seed_v2'").fetchone())
        # 대표가 되돌렸다(데스크탑 → PC): 재기동해도 다시 안 바꾼다(마커)
        conn.execute("UPDATE categories SET name='PC' WHERE name='데스크탑'")
        conn.commit()
        conn.close()
        create_app(db_path=self.db_path)
        self.assertIn("PC", [g[0] for g in self._names()])
        self.assertNotIn("데스크탑", [g[0] for g in self._names()])
        self.assertEqual(len(self._names()), 9)

    def test_새_이름이_이미_있으면_개명하지_않는다(self):
        self._legacy(["노트북", "PC", "데스크탑"])
        create_app(db_path=self.db_path)
        names = [g[0] for g in self._names()]
        self.assertEqual(names[:3], ["노트북", "PC", "데스크탑"])
        self.assertEqual(len(names), 3 + 6, "없는 이름 6개(태블릿·모니터·미니PC·일체형PC·주변기기·웨어러블)만 붙는다")

    def test_빈_표는_기본_시드_그대로(self):
        self._legacy([])
        create_app(db_path=self.db_path)
        self.assertEqual([g[0] for g in self._names()], EIGHT)
        create_app(db_path=self.db_path)
        self.assertEqual([g[0] for g in self._names()], EIGHT)


if __name__ == "__main__":
    unittest.main()
