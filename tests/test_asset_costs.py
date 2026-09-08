"""매입 자산 편집 보완(2026-09-03, 계획서 ②(g)) — 재고비고 칸 + TMS 수리비·부품비 → 자산 수리 기록(출처 TMS연동) + 제조원가.

연동 창구 재고내역 행(수리비·부품비·재고비고 포함)을 migration._prepare/apply_rows 에 직접 넣어 검증한다(네트워크·운영 DB 무접촉).
"""
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
from app.purchase import migration  # noqa: E402

PW = "admin-pass-1"


def stock_row(no, repair=0, parts=0, note="", **extra):
    """창구 재고내역 한 행(필요한 칸만)."""
    return {"#": 1, "관리번호": no, "회사ID": "예시 운영사", "매입구분": "일반매입", "재고상태": "매입", "대분류": "PC",
            "중분류": "노트북", "브랜드": "삼성", "모델명": "NT951XCJ", "매입처명": "테스트거래처", "매입일": "2026-09-01",
            "매입가": 300000, "등급": "A", "수리비": repair, "부품비": parts, "제조원가": 300000 + repair + parts,
            "재고비고": note, **extra}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-costs-"))
        self.db_path = self.tmp / "t.db"
        self.app = create_app(db_path=self.db_path)
        self.app.testing = True
        self.c = self.app.test_client()
        auth_mod._login_failures.clear()
        self.c.post("/api/auth/setup", json={"username": "admin", "displayName": "대표", "password": PW})
        os.environ.pop("OWS_DATALINK_URL", None)
        os.environ.pop("OWS_DATALINK_TOKEN", None)

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

    def sheet(self, rows):
        with self.app.app_context():
            from app.db import tx
            with tx(write=True) as conn:
                ready, dup, errors, updates, locked = migration._prepare(conn, rows, fill_blanks=True)
                res = migration.apply_rows(conn, ready, updates, actor="연동")
                return res, errors

    def asset(self, no):
        return self.sql("SELECT * FROM assets WHERE asset_no=?", no)[0]

    def repairs(self, aid):
        return self.sql("SELECT description, cost, net, vat, created_by FROM asset_repairs WHERE asset_id=? ORDER BY id", aid)

    def detail(self, aid):
        return self.c.get(f"/api/assets/{aid}").get_json()


class TestTmsCosts(Base):
    def test_신규_자산은_수리비_부품비가_수리_기록으로_들어오고_재고비고가_채워진다(self):
        res, errors = self.sheet([stock_row("260901-0001", repair=33100, parts=11000, note="새배터리교체")])
        self.assertEqual(errors, [])
        self.assertEqual((res["created"], res["costsSynced"]), (1, 1))
        a = self.asset("260901-0001")
        self.assertEqual(a["stock_note"], "새배터리교체")
        reps = self.repairs(a["id"])
        self.assertEqual([(r["description"], r["cost"], r["created_by"]) for r in reps],
                         [("수리비(TMS)", 33100, "TMS연동"), ("부품비(TMS)", 11000, "TMS연동")])
        self.assertEqual(reps[0]["net"] + reps[0]["vat"], 33100, "공급가+부가세 = 총액")
        d = self.detail(a["id"])
        self.assertEqual((d["repairTotal"], d["costTotal"], d["stockNote"]), (44100, 344100, "새배터리교체"))
        events = [e["action"] for e in d["events"]]
        self.assertEqual(events.count("수리"), 2)

    def test_같은_값을_다시_받아도_기록이_늘지_않고_바뀐_값만_따라간다(self):
        self.sheet([stock_row("260901-0001", repair=33100, parts=11000)])
        res, _ = self.sheet([stock_row("260901-0001", repair=33100, parts=11000)])
        self.assertEqual((res["created"], res["updated"], res["costsSynced"]), (0, 0, 0), "멱등")
        a = self.asset("260901-0001")
        self.assertEqual(len(self.repairs(a["id"])), 2)
        # 수리비가 바뀌면 그 행만 갱신, 부품비 0이면 행이 사라진다
        res, _ = self.sheet([stock_row("260901-0001", repair=50000, parts=0)])
        self.assertEqual(res["costsSynced"], 1)
        self.assertEqual([(r["description"], r["cost"]) for r in self.repairs(a["id"])], [("수리비(TMS)", 50000)])
        self.assertEqual(self.detail(a["id"])["costTotal"], 350000)

    def test_사람이_넣은_수리_기록과_매입가는_건드리지_않는다(self):
        self.sheet([stock_row("260901-0001", repair=33100, parts=0)])
        a = self.asset("260901-0001")
        r = self.c.post(f"/api/assets/{a['id']}/repairs", json={"repairDate": "2026-09-02", "description": "액정 교체", "cost": 120000})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        # 사람이 매입가를 고쳤다 — TMS 값이 와도 덮지 않는다(기존 규칙)
        self.c.patch(f"/api/assets/{a['id']}", json={"purchasePrice": 280000})
        res, _ = self.sheet([stock_row("260901-0001", repair=0, parts=5000)])
        self.assertEqual(res["costsSynced"], 1)
        reps = self.repairs(a["id"])
        self.assertEqual([(r["description"], r["cost"], r["created_by"]) for r in reps],
                         [("액정 교체", 120000, "대표"), ("부품비(TMS)", 5000, "TMS연동")])
        self.assertEqual(self.asset("260901-0001")["purchase_price"], 280000)
        self.assertEqual(self.detail(a["id"])["costTotal"], 280000 + 125000)

    def test_수리비_칸이_없는_행_엑셀_경로는_아무것도_안_한다(self):
        row = {k: v for k, v in stock_row("260901-0001").items() if k not in ("수리비", "부품비", "제조원가", "재고비고")}
        res, _ = self.sheet([row])
        self.assertEqual((res["created"], res["costsSynced"]), (1, 0))
        a = self.asset("260901-0001")
        self.assertEqual(self.repairs(a["id"]), [])
        self.assertEqual(a["stock_note"], "")
        # 이미 TMS 행이 있는 자산에 칸 없는 행이 와도 지우지 않는다
        self.sheet([stock_row("260901-0001", repair=1000)])
        self.sheet([row])
        self.assertEqual([r["cost"] for r in self.repairs(a["id"])], [1000])

    def test_잠긴_자산은_수리비도_안_따라간다(self):
        self.sheet([stock_row("260901-0001", repair=1000)])
        a = self.asset("260901-0001")
        self.sql("UPDATE assets SET tms_lock=1 WHERE id=?", a["id"])
        res, _ = self.sheet([stock_row("260901-0001", repair=99000)])
        self.assertEqual(res["costsSynced"], 0)
        self.assertEqual([r["cost"] for r in self.repairs(a["id"])], [1000])


class TestStockNote(Base):
    def test_자산_상세에서_재고비고를_고치면_전값이_남고_연동이_덮지_않는다(self):
        self.sheet([stock_row("260901-0001", note="새배터리교체")])
        a = self.asset("260901-0001")
        r = self.c.patch(f"/api/assets/{a['id']}", json={"stockNote": "액정불량, 키보드불량"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.detail(a["id"])["stockNote"], "액정불량, 키보드불량")
        ev = [e for e in self.detail(a["id"])["events"] if e["action"] == "수정"]
        self.assertTrue(ev and ev[0]["detail"].get("재고비고") == {"from": "새배터리교체", "to": "액정불량, 키보드불량"}, ev)
        # 사람이 고친 뒤 TMS 값이 바뀌어 와도 보호(3방향: 그림자 '새배터리교체' ≠ 현재값)
        self.sheet([stock_row("260901-0001", note="배터리교체완료")])
        self.assertEqual(self.asset("260901-0001")["stock_note"], "액정불량, 키보드불량")

    def test_사람이_안_건드린_재고비고는_TMS_정정을_따라간다(self):
        self.sheet([stock_row("260901-0001", note="새배터리교체")])
        res, _ = self.sheet([stock_row("260901-0001", note="배터리교체완료")])
        self.assertEqual(res["corrected"], 1)
        self.assertEqual(self.asset("260901-0001")["stock_note"], "배터리교체완료")

    def test_빈_재고비고는_채워지고_긴_값은_400(self):
        a = self.c.post("/api/assets", json={"categoryId": self.c.get("/api/categories").get_json()[0]["id"],
                                             "qty": 1, "assetNo": "260901-0002"}).get_json()[0]
        self.assertEqual(self.detail(a["id"])["stockNote"], "")
        self.sheet([stock_row("260901-0002", note="액정불량")])
        self.assertEqual(self.detail(a["id"])["stockNote"], "액정불량")
        self.assertEqual(self.c.patch(f"/api/assets/{a['id']}", json={"stockNote": "x" * 201}).status_code, 400)


class TestCostBackfill(unittest.TestCase):
    """첫 증분 틱에 재고내역 전량을 한 번 더 받아 기존 자산의 수리비·부품비를 채운다(tms_link.sync_once)."""

    def setUp(self):
        from app.purchase import tms_link
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-costs-bf-"))
        self.app = create_app(db_path=self.tmp / "t.db")
        self.app.testing = True
        tms_link.STATE_FILE = self.tmp / "state.json"
        self._env = {k: os.environ.get(k) for k in ("OWS_DATALINK_URL", "OWS_DATALINK_TOKEN")}
        os.environ["OWS_DATALINK_URL"], os.environ["OWS_DATALINK_TOKEN"] = "http://fake", "t"
        self._client = tms_link.Client
        tests = self

        class Fake:
            calls = []
            stock = [stock_row("260901-0001", repair=33100, parts=11000, note="새배터리교체")]

            def __init__(self, url="", token=""):
                pass

            def get(self, path, **params):
                Fake.calls.append((path, params))
                if path == "/status":
                    return {"ok": True, "status": {"freshness": {"stale": False, "age_sec": 5}, "invariants": {}}}
                if path == "/screens":
                    name = params["names"]
                    rows = Fake.stock if (name == "재고내역" and params.get("since") is None) else []
                    return {"ok": True, "server_time": "2026-09-03 18:00:00",
                            "screens": {name: {"headers": list(rows[0].keys()) if rows else [], "count": len(rows), "items": rows}}}
                if path in ("/deleted", "/restores", "/changes"):
                    return {"ok": True, "items": [], "next_after": None}
                return {"ok": True, "count": 0, "next_after_key": None, "items": []}
        self.Fake = Fake
        tms_link.Client = Fake

    def tearDown(self):
        from app.purchase import tms_link
        tms_link.Client = self._client
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def q(self, sql, *args):
        from app.db import tx
        with self.app.app_context():
            with tx() as conn:
                return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def test_첫_증분_틱에_전량을_한_번_받아_채우고_다시는_안_한다(self):
        from app.purchase import tms_link
        import json as _json
        # 전량 틱(첫 가동) — 자산이 생기고 마커가 찍힌다(전량이므로 이미 맞다)
        res = tms_link.sync_once(self.app)
        self.assertNotIn("costBackfill", res)
        state = _json.loads(tms_link.STATE_FILE.read_text("utf-8"))
        self.assertTrue(state.get("cost_backfill"))
        # 마커를 지우고(= 옛 상태 파일) 증분 틱을 돌리면 재고내역 전량을 한 번 더 받아 채운다
        state.pop("cost_backfill"); tms_link._write_state(state)
        self.q("DELETE FROM asset_repairs")
        self.Fake.calls = []
        res = tms_link.sync_once(self.app)
        self.assertEqual(res["costBackfill"]["costsSynced"], 1, res)
        self.assertEqual([r["cost"] for r in self.q("SELECT cost FROM asset_repairs ORDER BY id")], [33100, 11000])
        full_calls = [p for p, prm in self.Fake.calls if p == "/screens" and prm.get("names") == "재고내역" and prm.get("since") is None]
        self.assertEqual(len(full_calls), 1)
        # 다음 틱은 전량을 다시 안 받는다
        self.Fake.calls = []
        res = tms_link.sync_once(self.app)
        self.assertNotIn("costBackfill", res)
        self.assertFalse([p for p, prm in self.Fake.calls if p == "/screens" and prm.get("names") == "재고내역" and prm.get("since") is None])


class TestTmsPushOutbox(Base):
    """OWS → TMS 되돌려 쓰기 큐(2026-09-08 대표 "앞으로는 tms와 ows 그대로 연동해서 쓸거야").

    지금까지는 한 방향이라, OWS에서 넣은 A/S 수리비가 TMS 재고내역에 영영 안 보였다
    (대표가 260424-0020 으로 물은 그 건). 이제 바뀔 때마다 큐에 쌓이고 연동 틱이 밀어 넣는다.
    ★TMS·창구에 접속하지 않는다 — 창구 응답만 가짜로 만들어 큐의 규칙을 검증한다.
    """

    def _asset(self, key_id=25996, price=242000):
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "15U480", "qty": 1,
            "purchasePrice": price}).get_json()[0]
        if key_id is not None:      # TMS에서 온 자산이라는 표시(연동이 채우는 그림자)
            self.sql("UPDATE assets SET tms_shadow=? WHERE id=?",
                     '{"키ID": %d}' % key_id, a["id"])
        return a

    def _repair(self, aid, cost, desc="액정수리"):
        r = self.c.post("/api/assets/%d/repairs" % aid,
                        json={"description": desc, "cost": cost, "repairDate": "2026-09-07"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()

    def _queue(self):
        return self.sql("SELECT * FROM tms_outbox ORDER BY id")

    def _push(self, reply=None, error=None):
        """연동 틱의 밀어넣기 — 창구 클라이언트만 가짜로 세운다."""
        from unittest import mock

        from app.purchase import tms_link
        cli = mock.MagicMock()
        if error:
            cli.post.side_effect = error
        else:
            cli.post.return_value = reply
        with self.app.app_context():
            with self.app.test_request_context():
                return tms_link.push_outbox(self.app, cli), cli

    # ---------------- 큐에 쌓이는 규칙
    def test_수리비를_넣으면_TMS에_보낼_줄이_생긴다(self):
        a = self._asset()
        self._repair(a["id"], 75000)
        q = self._queue()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["kind"], "asset_cost")
        self.assertEqual(q[0]["tms_key_id"], 25996)
        self.assertEqual(q[0]["status"], "queued")
        self.assertIn("75000", q[0]["payload"])
        self.assertIn("액정수리", q[0]["reason"])

    def test_TMS에_없는_자산은_보내지_않는다(self):
        """OWS에서 채번한 자산(5000번대)은 TMS에 그 행이 없다 — 보낼 곳이 없다."""
        a = self._asset(key_id=None)
        self._repair(a["id"], 50000)
        self.assertEqual(self._queue(), [])

    def test_잠긴_자산은_보내지_않는다(self):
        """번호 충돌로 잠근 자산(tms_lock)은 어느 쪽도 건드리지 않는다."""
        a = self._asset()
        self.sql("UPDATE assets SET tms_lock=1 WHERE id=?", a["id"])
        self._repair(a["id"], 50000)
        self.assertEqual(self._queue(), [])

    def test_여러_번_고쳐도_줄은_하나고_마지막_값이다(self):
        """★줄이 쌓이면 중간값이 순서대로 TMS에 잠깐씩 보인다 — 마지막 값만 의미가 있다."""
        a = self._asset()
        self._repair(a["id"], 75000)
        self._repair(a["id"], 30000, desc="키보드")
        q = self._queue()
        self.assertEqual(len(q), 1, "안 나간 줄이 쌓였다")
        self.assertIn("105000", q[0]["payload"])          # 합계

    def test_TMS연동_수리비는_되돌려_보내지_않는다(self):
        """★TMS에서 받아 온 값을 도로 밀어 넣으면 제자리를 맴돈다."""
        self.sheet([stock_row("260908-0001", repair=40000)])
        aid = self.asset("260908-0001")["id"]
        self.sql("UPDATE assets SET tms_shadow=? WHERE id=?", '{"키ID": 111}', aid)
        self.assertEqual(self._queue(), [], "TMS가 준 값이 되돌아 나가려 한다")
        # 사람이 더 넣으면 그 사람 몫만 나간다
        self._repair(aid, 25000)
        q = self._queue()
        self.assertEqual(len(q), 1)
        self.assertIn("25000", q[0]["payload"])

    def test_수리_기록을_지우면_줄어든_값이_나간다(self):
        a = self._asset()
        self._repair(a["id"], 75000)
        rid = self.sql("SELECT id FROM asset_repairs WHERE asset_id=? ORDER BY id DESC",
                       a["id"])[0]["id"]
        self.assertEqual(self.c.delete("/api/assets/%d/repairs/%d" % (a["id"], rid)).status_code, 200)
        q = self._queue()
        self.assertEqual(len(q), 1)
        # ★아직 TMS로 안 나간 줄이었다 → 보낼 값이 없어졌으니 그 줄을 접는다.
        #   접지 않으면 방금 지운 75,000이 그대로 TMS에 들어간다.
        self.assertEqual(q[0]["status"], "cancelled")

    def test_이미_보낸_뒤_수리를_지우면_0을_보낸다(self):
        """TMS에 75,000이 들어가 있는데 OWS에서 지웠다 — TMS도 0으로 되돌려야 한다."""
        a = self._asset()
        self._repair(a["id"], 75000)
        self._push({"ok": True, "applied": True, "actionId": "a", "before": {}, "after": {}})
        rid = self.sql("SELECT id FROM asset_repairs WHERE asset_id=? ORDER BY id DESC",
                       a["id"])[0]["id"]
        self.assertEqual(self.c.delete("/api/assets/%d/repairs/%d" % (a["id"], rid)).status_code, 200)
        q = [x for x in self._queue() if x["status"] == "queued"]
        self.assertEqual(len(q), 1)
        self.assertIn('"수리비": 0', q[0]["payload"])

    # ---------------- 밀어 넣기(창구 응답은 가짜)
    def test_반영되면_큐가_닫히고_도장이_남는다(self):
        a = self._asset()
        self._repair(a["id"], 75000)
        out, cli = self._push({"ok": True, "applied": True,
                               "actionId": "26090812000000_OWS연동_대표",
                               "before": {"수리비": 0.0}, "after": {"수리비": 75000.0}})
        self.assertEqual(out["applied"], 1)
        body = cli.post.call_args[0][1]
        self.assertEqual(body["kind"], "asset_cost")
        self.assertEqual(body["keyId"], 25996)
        self.assertEqual(body["values"], {"수리비": 75000})
        q = self._queue()[0]
        self.assertEqual(q["status"], "applied")
        self.assertEqual(q["action_id"], "26090812000000_OWS연동_대표")
        self.assertTrue(q["applied_at"])

    def test_무장_전에는_큐가_남아_켜는_순간_나간다(self):
        """★창구가 아직 무장 전이면 '거부'다 — 실패로 세어 시도 횟수를 까먹으면 안 된다."""
        from app.purchase.tms_push import TmsWriteRefused
        a = self._asset()
        self._repair(a["id"], 75000)
        out, _ = self._push(error=TmsWriteRefused("TMS 쓰기가 무장되지 않았습니다"))
        self.assertEqual(out["skipped"], 1)
        q = self._queue()[0]
        self.assertEqual(q["status"], "queued", "무장 전인데 큐가 닫혔다")
        self.assertEqual(q["attempts"], 0, "재시도 횟수를 까먹었다")
        self.assertIn("무장", q["last_error"])
        # 무장 뒤 그대로 나간다
        out, _ = self._push({"ok": True, "applied": True, "actionId": "x",
                             "before": {}, "after": {}})
        self.assertEqual(out["applied"], 1)
        self.assertEqual(self._queue()[0]["status"], "applied")

    def test_충돌이면_덮어쓰지_않고_남는다(self):
        from app.purchase.tms_push import TmsWriteConflict
        a = self._asset()
        self._repair(a["id"], 75000)
        out, _ = self._push(error=TmsWriteConflict("그 사이 TMS에서 먼저 바뀌었습니다"))
        self.assertEqual(out["conflict"], 1)
        q = self._queue()[0]
        self.assertEqual(q["status"], "conflict")
        self.assertEqual(q["attempts"], 1)

    def test_같은_값이면_반영으로_닫는다(self):
        """TMS가 이미 그 값이면 할 일이 없다 — 큐를 열어 두면 영원히 다시 시도한다."""
        a = self._asset()
        self._repair(a["id"], 75000)
        out, _ = self._push({"ok": True, "applied": False, "reason": "이미 같은 값입니다",
                             "before": {"수리비": 75000.0}, "after": {"수리비": 75000.0}})
        self.assertEqual(self._queue()[0]["status"], "applied")
        self.assertEqual(out["applied"], 1)

    # ---------------- ★되돌아오는 고리(이중 계상) 차단
    def test_밀어_넣은_값이_돌아와도_원가가_두_배가_되지_않는다(self):
        """★이 시험이 이 기능의 존재 이유다.

        OWS 수리비 75,000 → TMS 재고.수리비 75,000 → 트리거 → 사본 → OWS가 다시 읽는다.
        빼지 않으면 'TMS연동' 수리 기록이 또 생겨 자산 원가가 150,000이 된다.
        """
        self.sheet([stock_row("260908-0002")])            # TMS에서 온 자산(수리비 0)
        aid = self.asset("260908-0002")["id"]
        self.sql("UPDATE assets SET tms_shadow=? WHERE id=?", '{"키ID": 222}', aid)
        self._repair(aid, 75000)                          # 사람이 A/S 수리비를 넣는다
        self._push({"ok": True, "applied": True, "actionId": "26090812000000_OWS연동_대표",
                    "before": {"수리비": 0.0}, "after": {"수리비": 75000.0}})
        # 이제 TMS 사본이 수리비 75,000을 되돌려 준다
        self.sheet([stock_row("260908-0002", repair=75000)])
        reps = self.repairs(aid)
        self.assertEqual(sum(r["cost"] for r in reps), 75000, "원가가 두 배로 잡혔다: %r" % (reps,))
        self.assertEqual([r for r in reps if r["created_by"] == "TMS연동"], [],
                         "우리가 넣은 값이 TMS연동 기록으로 또 들어왔다")

    def test_TMS에서_더_올린_차액만_따라간다(self):
        """OWS가 75,000을 넣어 뒀는데 TMS에서 사람이 100,000으로 올렸다 → 차액 25,000만 TMS 몫."""
        self.sheet([stock_row("260908-0003")])
        aid = self.asset("260908-0003")["id"]
        self.sql("UPDATE assets SET tms_shadow=? WHERE id=?", '{"키ID": 333}', aid)
        self._repair(aid, 75000)
        self._push({"ok": True, "applied": True, "actionId": "a", "before": {}, "after": {}})
        self.sheet([stock_row("260908-0003", repair=100000)])
        reps = self.repairs(aid)
        self.assertEqual(sum(r["cost"] for r in reps), 100000, "%r" % (reps,))
        tms_rows = [r for r in reps if r["created_by"] == "TMS연동"]
        self.assertEqual([r["cost"] for r in tms_rows], [25000], "차액만 TMS 몫이어야 한다")


class TestTmsOutboxScreen(Base):
    """설정 ▸ 데이터 이관 ▸ [⤴ OWS → TMS 되돌려 쓰기] 카드(2026-09-08).

    ★무장 전에는 큐가 쌓이기만 한다 — 사람이 그걸 **화면에서** 알아야 한다.
      숫자만 맞고 '왜 안 나가는지'가 안 보이면 대표는 고장 났다고 생각한다.
    """

    def _asset_with_repair(self, cost=75000, key_id=25996):
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "15U480", "qty": 1,
            "purchasePrice": 242000}).get_json()[0]
        self.sql("UPDATE assets SET tms_shadow=? WHERE id=?", '{"키ID": %d}' % key_id, a["id"])
        self.c.post("/api/assets/%d/repairs" % a["id"],
                    json={"description": "액정수리", "cost": cost, "repairDate": "2026-09-07"})
        return a

    def _list(self, all_=False):
        r = self.c.get("/api/tms-outbox" + ("?all=1" if all_ else ""))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def test_밀린_것과_창구_상태를_함께_보여_준다(self):
        self._asset_with_repair()
        d = self._list()
        self.assertEqual(d["counts"]["open"], 1)
        self.assertEqual(d["counts"]["applied"], 0)
        self.assertEqual(len(d["items"]), 1)
        it = d["items"][0]
        self.assertEqual(it["values"], {"수리비": 75000})
        self.assertEqual(it["tmsKeyId"], 25996)
        self.assertEqual(it["statusLabel"], "보낼 차례")
        self.assertIn("액정수리", it["reason"])
        # ★시험·검증 서버(OWS_NO_TMS_SYNC=1)는 창구에 붙지 않는다 — 붙으면 라이브 TMS까지 닿는다.
        #   '막아 둔 것'과 '연결 실패'는 원인이 달라 화면 문구도 달라야 한다.
        self.assertFalse(d["gate"]["reachable"])
        self.assertIn("OWS_NO_TMS_SYNC", d["gate"]["error"])

    def test_보낸_것은_기본_목록에서_빠지고_전체_보기에만_나온다(self):
        a = self._asset_with_repair()
        self.sql("UPDATE tms_outbox SET status='applied', applied_at='2026-09-08T10:00:00+09:00' "
                 "WHERE asset_id=?", a["id"])
        self.assertEqual(len(self._list()["items"]), 0)
        d = self._list(all_=True)
        self.assertEqual(len(d["items"]), 1)
        self.assertEqual(d["counts"]["applied"], 1)
        self.assertEqual(d["counts"]["open"], 0)

    def test_여러_번_실패한_줄은_멈춤으로_센다(self):
        """★자동 재시도를 멈춘 줄은 사람이 눌러야 움직인다 — 숫자에 안 뜨면 영영 묻힌다."""
        a = self._asset_with_repair()
        self.sql("UPDATE tms_outbox SET status='failed', attempts=99 WHERE asset_id=?", a["id"])
        self.assertEqual(self._list()["counts"]["stuck"], 1)

    def test_다시_보내기는_시도_횟수를_지운다(self):
        a = self._asset_with_repair()
        self.sql("UPDATE tms_outbox SET status='failed', attempts=99, last_error='x' WHERE asset_id=?",
                 a["id"])
        rid = self.sql("SELECT id FROM tms_outbox WHERE asset_id=?", a["id"])[0]["id"]
        self.assertEqual(self.c.post("/api/tms-outbox/%d/retry" % rid, json={}).status_code, 200)
        row = self.sql("SELECT * FROM tms_outbox WHERE id=?", rid)[0]
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["last_error"], "")

    def test_접으면_안_보내고_이미_반영된_줄은_못_접는다(self):
        a = self._asset_with_repair()
        rid = self.sql("SELECT id FROM tms_outbox WHERE asset_id=?", a["id"])[0]["id"]
        self.assertEqual(self.c.post("/api/tms-outbox/%d/cancel" % rid, json={}).status_code, 200)
        self.assertEqual(self.sql("SELECT status FROM tms_outbox WHERE id=?", rid)[0]["status"],
                         "cancelled")
        # 이미 TMS에 들어간 줄은 접는 대상이 아니다(접어도 TMS가 안 되돌아간다 — 오해를 막는다)
        self.sql("UPDATE tms_outbox SET status='applied' WHERE id=?", rid)
        r = self.c.post("/api/tms-outbox/%d/cancel" % rid, json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("이미", r.get_json()["error"])

    def test_모르는_동작은_404(self):
        a = self._asset_with_repair()
        rid = self.sql("SELECT id FROM tms_outbox WHERE asset_id=?", a["id"])[0]["id"]
        self.assertEqual(self.c.post("/api/tms-outbox/%d/delete" % rid, json={}).status_code, 404)

    def test_막아_둔_서버에서는_지금_보내기가_400(self):
        """★검증 서버에서 이 버튼을 누르면 라이브 TMS가 바뀐다 — 그 길을 서버가 막는다."""
        self._asset_with_repair()
        r = self.c.post("/api/tms-outbox/push", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("OWS_NO_TMS_SYNC", r.get_json()["error"])

    def test_막아_둔_서버는_창구_클라이언트를_아예_못_만든다(self):
        """★마지막 선 — 라우트를 우회해 코드로 불러도 접속이 안 된다."""
        from app.purchase.tms_link import Client
        with self.assertRaises(RuntimeError) as e:
            Client("http://127.0.0.2:15200/api/v1", "t")
        self.assertIn("OWS_NO_TMS_SYNC", str(e.exception))

    def test_화면_배선(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js").read_text("utf-8")
        self.assertIn("function renderTmsOutbox", js)
        self.assertIn('id="mg-outbox-body"', js)
        self.assertIn("renderTmsOutbox();", js)
        self.assertIn("/api/tms-outbox", js)
        self.assertIn('data-obact="retry"', js)
        self.assertIn('data-obact="cancel"', js)
        self.assertIn('id="ob-push"', js)
        # ★무장 상태를 사람 말로 보여 준다 — 안 그러면 '고장 났다'로 오해한다
        self.assertIn("쓰기 무장 안 됨", js)
        self.assertIn("TMS_WRITE_ARMED=1", js)


if __name__ == "__main__":
    unittest.main()
