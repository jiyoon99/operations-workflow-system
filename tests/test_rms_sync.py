"""RMS 재고 사본(rms_inventory)에 시리얼·모델을 싣는다 — OWS 화면에서 시작한 이관의 시리얼 대조 복원(2026-09-03).

배경(대표 신고): OWS(185)는 240의 RMS DB 파일을 못 읽는다. 그래서 OWS 화면에서 시작한
렌탈→판매 이관은 "RMS 자산 데이터를 읽을 수 없습니다"라고만 띄우고 시리얼 대조 없이
'이관완료'가 됐다. RMS가 사본을 밀 때 시리얼·모델을 같이 보내고(RMS app.py
ows_rental_missing/accept) OWS가 그것을 저장하면(bridge_rms_sync) `_rms_from_copy`가
그 값으로 '같은 번호에 실물 두 대'를 잡는다.

  ① POST /api/bridge/assets/rms-sync 가 serial·model 을 저장한다(옛 RMS가 안 보내면 빈칸)
  ② 렌탈→판매 미리보기가 사본 시리얼로 '★시리얼 다름'을 경고하고 사본 시각을 밝힌다
  ③ 판매→렌탈 커밋의 수신 대기 항목에 제품·시리얼·스펙·등급이 실린다(RMS 수신 팝업 대조용)
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import auth as auth_mod  # noqa: E402
from app import create_app  # noqa: E402
from app.db import tx  # noqa: E402
from app.purchase import transfers as transfers_mod  # noqa: E402

PW = "admin-pass-1"
TOKEN = "rms-sync-test-token"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-rmssync-"))
        self.app = create_app(db_path=self.tmp / "t.db")
        self.app.testing = True
        self.c = self.app.test_client()
        auth_mod._login_failures.clear()
        self.c.post("/api/auth/setup", json={
            "username": "admin", "displayName": "대표", "password": PW})
        self.cat = self.c.get("/api/categories").get_json()[0]["id"]
        self.sid = self.c.post("/api/suppliers", json={"name": "테스트상사"}).get_json()["id"]
        # ★같은 PC에 RMS DB 파일이 있으면(240) 파일 경로가 먼저 잡혀 사본 경로가 안 돈다 —
        #   185 와 같은 조건(파일 없음)으로 고정한다. 라이브 RMS DB 무접촉.
        p = mock.patch.object(transfers_mod, "RMS_DB_PATH", str(self.tmp / "no-rms.db"))
        p.start()
        self.addCleanup(p.stop)
        self._old_token = os.environ.get("OWS_BRIDGE_TOKEN")
        os.environ["OWS_BRIDGE_TOKEN"] = TOKEN
        self.bridge = self.app.test_client()      # 사람 세션 없이 토큰만으로

    def tearDown(self):
        if self._old_token is None:
            os.environ.pop("OWS_BRIDGE_TOKEN", None)
        else:
            os.environ["OWS_BRIDGE_TOKEN"] = self._old_token
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _asset(self, division="sale", serial="SN-OWS-1", model="렌탈기계", **cols):
        b = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-09-01", "totalAmount": 0,
            "supplierId": self.sid}).get_json()["id"]
        r = self.c.post("/api/assets", json={"categoryId": self.cat, "qty": 1, "batchId": b,
                                             "model": model, "serial": serial})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        aid = r.get_json()[0]["id"]
        no = self.c.get(f"/api/assets/{aid}").get_json()["assetNo"]
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE assets SET division=? WHERE id=?", (division, aid))
                for k, v in cols.items():
                    conn.execute(f"UPDATE assets SET {k}=? WHERE id=?", (v, aid))
        return aid, no

    def _sync(self, body):
        return self.bridge.post("/api/bridge/assets/rms-sync", json=body,
                                headers={"X-Bridge-Token": TOKEN})

    def _copy_row(self, no):
        with self.app.app_context():
            with tx() as conn:
                return conn.execute(
                    "SELECT status, renter, serial, model, synced_at FROM rms_inventory "
                    "WHERE asset_no=?", (no,)).fetchone()


class TestRmsSyncStoresSerial(Base):
    def test_사본에_시리얼과_모델이_저장된다(self):
        _aid, no = self._asset(division="rental")
        r = self._sync({"assets": [{"assetNo": no, "status": "available", "renter": "",
                                    "serial": "SN-RMS-9", "model": "렌탈기계"}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["rmsCount"], 1)
        row = self._copy_row(no)
        self.assertEqual((row["serial"], row["model"], row["status"]),
                         ("SN-RMS-9", "렌탈기계", "available"),
                         "RMS가 보낸 시리얼·모델이 사본에 안 남는다 — 시리얼 대조가 통째로 빠진다")
        with self.app.app_context():
            with tx() as conn:
                got = transfers_mod._rms_from_copy(conn, [no])
        self.assertEqual(got[no.upper()][0]["serial_number"], "SN-RMS-9")
        self.assertEqual(got[no.upper()][0]["model_name"], "렌탈기계")
        self.assertTrue(got[no.upper()][0]["_from_copy"])

    def test_시리얼을_안_보내는_옛_RMS도_받는다(self):
        _aid, no = self._asset(division="rental")
        r = self._sync({"assets": [{"assetNo": no, "status": "rented", "renter": "코넥"}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        row = self._copy_row(no)
        self.assertEqual((row["serial"], row["model"], row["renter"]), ("", "", "코넥"))
        # 번호만 보내는 형식(nos)도 그대로
        r = self._sync({"nos": [no]})
        self.assertEqual(r.status_code, 200)
        row = self._copy_row(no)
        self.assertEqual((row["serial"], row["model"], row["status"]), ("", "", ""))

    def test_사본_표에_칸이_있다(self):
        with self.app.app_context():
            with tx() as conn:
                cols = {r["name"] for r in conn.execute("PRAGMA table_info(rms_inventory)")}
        self.assertTrue({"serial", "model", "renter", "synced_at"} <= cols, cols)

    def test_빈_목록은_거절한다(self):
        self.assertEqual(self._sync({"assets": []}).status_code, 400)
        self.assertEqual(self._sync({"assets": [{"serial": "x"}]}).status_code, 400)


class TestCopyBasedSerialCheck(Base):
    """OWS 화면에서 시작한 렌탈→판매 이관 — 파일 대신 사본으로 대조한다."""

    def _propose(self, no):
        r = self.c.post("/api/transfers", json={"direction": "rental->sale",
                                                "assetNos": [no], "reason": "시험"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        return next(i for i in d["items"] if i["assetNo"] == no)

    def test_시리얼이_다르면_사본으로_잡는다(self):
        _aid, no = self._asset(division="rental", serial="SN-OWS-1", model="렌탈기계")
        self._sync({"assets": [{"assetNo": no, "status": "available",
                                "serial": "SN-RMS-9", "model": "렌탈기계"}]})
        item = self._propose(no)
        self.assertEqual(item["result"], "ok", item)
        self.assertIn("시리얼 다름", item["note"],
                      "사본에 시리얼이 있는데도 '같은 번호에 실물 두 대'를 못 잡는다")
        self.assertIn("SN-RMS-9", item["note"])
        self.assertIn("사본으로 대조함", item["note"], "언제 받은 사본인지 밝혀야 한다")
        self.assertTrue(item["warn"])

    def test_모델이_다르면_알려_준다(self):
        _aid, no = self._asset(division="rental", serial="SN-1", model="렌탈기계")
        self._sync({"assets": [{"assetNo": no, "status": "available",
                                "serial": "SN-1", "model": "전혀다른장비"}]})
        item = self._propose(no)
        self.assertIn("모델명 다름", item["note"], item)
        self.assertNotIn("시리얼 다름", item["note"])

    def test_같으면_경고_없이_사본_시각만_적는다(self):
        _aid, no = self._asset(division="rental", serial="SN-1", model="렌탈기계")
        self._sync({"assets": [{"assetNo": no, "status": "available",
                                "serial": "sn-1", "model": "렌탈기계"}]})    # 대소문자만 다름
        item = self._propose(no)
        self.assertEqual(item["result"], "ok", item)
        self.assertNotIn("시리얼 다름", item["note"])
        self.assertNotIn("모델명 다름", item["note"])
        self.assertIn("사본으로 대조함", item["note"])
        self.assertEqual(item["rmsStatus"], "available")

    def test_사본에_시리얼이_없으면_대조_불가를_말한다(self):
        """옛 RMS(시리얼 안 보냄) + OWS 도 시리얼 없음 — 조용히 넘기지 않는다."""
        _aid, no = self._asset(division="rental", serial="", model="렌탈기계")
        self._sync({"assets": [{"assetNo": no, "status": "available"}]})
        item = self._propose(no)
        self.assertIn("양쪽 다 시리얼 없음", item["note"], item)

    def test_RMS가_대여중이라면_막는다(self):
        _aid, no = self._asset(division="rental", serial="SN-1")
        self._sync({"assets": [{"assetNo": no, "status": "rented", "renter": "코넥",
                                "serial": "SN-1", "model": "렌탈기계"}]})
        item = self._propose(no)
        self.assertEqual(item["result"], "blocked", item)
        self.assertIn("대여중", item["note"])


class TestPendingCarriesProduct(Base):
    """판매→렌탈 커밋을 RMS 가 받기 전에 실물과 대조하도록 제품 정보를 싣는다(2026-09-03 대표).

    "RMS 내에서 자산이관되면, 팝업창으로 떠서 어떤 제품인지 자산번호랑 함께 쭉 나열해줬으면 —
     대조해보고 RMS로 넘겨야 해."
    """

    def test_수신_대기_항목에_제품_시리얼_스펙_등급이_있다(self):
        _aid, no = self._asset(division="sale", serial="SN-P-1", model="15ZB95N",
                               maker="LG", cpu="i5-1135G7", ram="16G", ssd="512G", grade="A")
        d = self.c.post("/api/transfers", json={"direction": "sale->rental",
                                                "assetNos": [no], "reason": "렌탈 전환"}).get_json()
        cid = d["commitId"]
        self.assertEqual(d["summary"]["ok"], 1, d)
        r = self.c.post(f"/api/transfers/{cid}/commit")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        pend = self.bridge.get("/api/bridge/transfers/pending",
                               headers={"X-Bridge-Token": TOKEN})
        self.assertEqual(pend.status_code, 200, pend.get_data(as_text=True))
        t = next(x for x in pend.get_json() if x["commitId"] == cid)
        self.assertEqual(t["direction"], "sale->rental")
        it = next(i for i in t["items"] if i["assetNo"] == no)
        self.assertEqual((it["maker"], it["model"], it["serial"], it["grade"]),
                         ("LG", "15ZB95N", "SN-P-1", "A"), it)
        self.assertEqual(it["spec"], "i5-1135G7 / 16G / 512G")
        self.assertEqual(it["result"], "ok")


if __name__ == "__main__":
    unittest.main()
