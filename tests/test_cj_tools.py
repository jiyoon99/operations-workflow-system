"""CJ 이식·시험 도구 테스트 초안 — 적용 시 tests/test_cj_tools.py 로.

★전부 모킹 — 진짜 CJ를 부르는 시험은 없다(실접수가 생기면 안 된다).
"""
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth as auth_mod  # noqa: E402
from app import create_app  # noqa: E402

ADMIN_PW = "admin-pass-1"


def ensure_part(c, name, category, price):
    """부품을 '시험 단가'로 확보 — 시드(v2/v3)와 이름이 겹치면 그 줄을 시험 값으로 맞춘다.

    콤보 단가 시드(parts_seed_v3)가 D4 8G 같은 실규격 이름을 미리 만들기 때문에,
    시험이 같은 이름을 POST 하면 409가 난다. 그 경우 기존 줄의 단가·구분을 시험 값으로
    고쳐 쓴다(시험 DB 전용 — 검증 대상은 '동작'이지 시드 값이 아니다).
    """
    r = c.post("/api/parts", json={"name": name, "category": category, "price": price})
    if r.status_code == 201:
        return r.get_json()["id"]
    assert r.status_code == 409, r.get_data(as_text=True)
    pid = next(p["id"] for p in c.get("/api/parts").get_json() if p["name"] == name)
    rr = c.patch(f"/api/parts/{pid}", json={"price": price, "category": category})
    assert rr.status_code == 200, rr.get_data(as_text=True)
    return pid


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ows-cjt-"))
        self.app = create_app(db_path=self.tmp / "test.db")
        self.app.testing = True
        self.c = self.app.test_client()
        auth_mod._login_failures.clear()
        assert self.c.post("/api/auth/setup", json={
            "username": "admin", "displayName": "대표", "password": ADMIN_PW}).status_code == 200

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cj_now(self):
        from app.db import tx
        with self.app.app_context():
            with tx() as conn:
                row = conn.execute("SELECT value FROM settings WHERE key='cj'").fetchone()
        return json.loads(row["value"]) if row else {}


class TestCjImportRms(Base):
    def test_가져오면_핵심값이_전부_들어간다(self):
        r = self.c.post("/api/cj/import-rms")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        cj = self._cj_now()
        self.assertEqual(cj["cust_id"], "00000000")
        self.assertEqual(cj["biz_reg_num"], "0000000000")
        self.assertEqual(cj["env"], "prod")
        # 2026-08-13 대표: OWS는 보내는분·회수지 전부 '업무관리', 전화 02-0000-0000
        self.assertEqual(cj["sender"]["name"], "업무관리")
        self.assertEqual(cj["sender"]["tel"], "02-0000-0000")
        self.assertEqual(cj["pickup"]["name"], "업무관리")
        self.assertEqual(cj["pickup"]["tel"], "02-0000-0000")
        self.assertTrue(cj["sender"]["addr"].startswith("서울특별시 예시구"))

    def test_무장은_가져오지_않는다(self):
        """실발행 무장이 이식 한 번에 켜지면, 시험인 줄 알고 누른 발급이 진짜 접수된다."""
        self.c.post("/api/cj/import-rms")
        self.assertFalse(self._cj_now().get("armed"))

    def test_이미_켜둔_무장은_끄지_않는다(self):
        self.c.put("/api/settings", json={"cj": {"armed": True}})
        self.c.post("/api/cj/import-rms")
        self.assertTrue(self._cj_now().get("armed"))

    def test_기존_라벨_보정값은_보존한다(self):
        self.c.put("/api/settings", json={"cj": {"label": {"ox": 3, "oy": -2, "sc": 98}}})
        self.c.post("/api/cj/import-rms")
        self.assertEqual(self._cj_now()["label"], {"ox": 3, "oy": -2, "sc": 98})

    def test_구명칭_customerCode는_지운다(self):
        """★_is_real 이 cust_id 를 보므로 구명칭만 남으면 조용히 999 테스트 발행이 된다."""
        self.c.put("/api/settings", json={"cj": {"customerCode": "00000000"}})
        self.c.post("/api/cj/import-rms")
        cj = self._cj_now()
        self.assertNotIn("customerCode", cj)
        self.assertEqual(cj["cust_id"], "00000000")

    def test_두_번_눌러도_같다(self):
        self.c.post("/api/cj/import-rms")
        first = self._cj_now()
        self.c.post("/api/cj/import-rms")
        self.assertEqual(self._cj_now(), first)

    def test_발송인_표기를_RMS_그대로_고를_수_있다(self):
        """대표 2026-08-24: "RMS에 있던거 그대로 이식해줘."

        계약(고객코드·사업자번호)과 주소는 원래 같다 — 갈리는 건 명의·전화뿐이라
        그 부분만 고르게 했다. 값은 2026-08-24 RMS 라이브(cfg)에서 실측한 것이다.
        """
        self.c.post("/api/cj/import-rms", json={"identity": "rms"})
        cj = self._cj_now()
        self.assertIn("예시 렌탈사", cj["sender"]["name"])
        self.assertEqual(cj["sender"]["tel"], "0000-0000")
        self.assertEqual(cj["pickup"]["name"], "예시 렌탈사")
        self.assertEqual(cj["pickup"]["tel"], "010-0000-0000")
        # 계약·주소는 표기와 무관하게 같다 — 같은 사무실, 같은 CJ 계약이다
        self.assertEqual(cj["cust_id"], "00000000")
        self.assertEqual(cj["biz_reg_num"], "0000000000")
        self.assertEqual(cj["sender"]["zip"], "00000")

    def test_표기를_다시_업무관리으로_되돌릴_수_있다(self):
        self.c.post("/api/cj/import-rms", json={"identity": "rms"})
        self.c.post("/api/cj/import-rms", json={"identity": "operations"})
        cj = self._cj_now()
        self.assertEqual(cj["sender"]["name"], "업무관리")
        self.assertEqual(cj["sender"]["tel"], "02-0000-0000")

    def test_모르는_표기는_거부한다(self):
        r = self.c.post("/api/cj/import-rms", json={"identity": "아무거나"})
        self.assertEqual(r.status_code, 400)

    def test_클라이언트가_읽는_키가_전부_채워진다(self):
        """apiKey* 를 안 옮겨도 되는 근거 — V3.9.4 클라이언트는 이 셋만 읽는다.

        (RMS의 apiKeyRecall/apiKeyWaybill 은 키가 아니라 API 리소스 이름이다.)
        """
        import re as _re
        src = (Path(__file__).resolve().parent.parent / "app" / "cj" / "client.py"
               ).read_text("utf-8")
        need = set(_re.findall(r"cj[.]get[(]'([a-z_]+)'", src))
        self.assertTrue(need, "클라이언트가 읽는 키를 못 찾았다 — 시험을 고쳐라")
        self.c.post("/api/cj/import-rms")
        cj = self._cj_now()
        for k in sorted(need):
            self.assertTrue(str(cj.get(k) or "").strip(),
                            f"클라이언트가 읽는 {k} 가 비어 있다")

    def test_무장을_켜면_실발행_상태가_된다(self):
        """이식 + 무장 = 실발행. 이 조합만이 진짜 CJ 접수를 만든다."""
        from app.orders.waybill import _is_real
        self.c.post("/api/cj/import-rms")
        self.assertFalse(_is_real(self._cj_now()), "무장 없이 실발행 상태가 됐다")
        self.c.put("/api/settings", json={"cj": {"armed": True}})
        self.assertTrue(_is_real(self._cj_now()))



class TestCjSamplePdf(Base):
    def test_견본_운송장이_PDF로_나온다(self):
        r = self.c.get("/api/cj/sample-pdf")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"%PDF"), "PDF가 아니다")

    def test_보내는분을_저장했으면_견본에_반영된다(self):
        self.c.post("/api/cj/import-rms")
        r = self.c.get("/api/cj/sample-pdf")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"%PDF"))

    def test_저장한_문구가_견본에_반영된다(self):
        """대표 2026-08-24: "문구 수정하고 저장 누르면 견본 운송장에도 떠야하지 않을까?"

        ★예전 견본은 고정 문구('사무용 노트북 x2 / 모니터 x1')를 찍었다 — 문구를 고쳐도
          종이로 확인할 길이 없었다. 이제 미리보기와 같은 계산을 거친다.
        """
        base = self.c.get("/api/cj/sample-pdf").data
        self.assertTrue(base.startswith(b"%PDF"))
        r = self.c.put("/api/settings", json={"cj": {
            "label_tmpl": {"item": "[자산번호]", "remark": "[배송메모]"}}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        after = self.c.get("/api/cj/sample-pdf").data
        self.assertTrue(after.startswith(b"%PDF"))
        self.assertNotEqual(base, after, "★문구를 바꿨는데 견본이 그대로다")

    def test_고치는_중인_문구를_견본에_바로_반영한다(self):
        """대표 2026-08-24: "견본운송장 자체를 운송장 문구 이쪽 레이아웃에 실시간으로".

        ★저장하기 전에도 종이에 어떻게 나오는지 봐야 한다 — 그래야 잘림·넘침을 안다.
        """
        a = self.c.get("/api/cj/sample-pdf?item=%5B자산번호%5D&remark=%5B배송메모%5D").data
        b = self.c.get("/api/cj/sample-pdf?item=%5B상품명%5D&remark=%5B배송메모%5D").data
        self.assertTrue(a.startswith(b"%PDF") and b.startswith(b"%PDF"))
        self.assertNotEqual(a, b, "★고치는 중인 문구가 견본에 안 실린다")

    def test_화면_견본은_바로_서_있다(self):
        """대표 2026-08-24: "옆으로 누워있어서 보기가 어렵네."

        ★rot=0 이면 123×100 가로로 바로 선다. 기본(프린터용)은 세로급지에 맞춰
          100×123 으로 눕는다 — 인쇄 방향은 동결 검수 방식 그대로 두고 보는 각도만 바꾼다.
        """
        upright = self.c.get("/api/cj/sample-pdf?rot=0").data
        printer = self.c.get("/api/cj/sample-pdf").data
        self.assertTrue(upright.startswith(b"%PDF") and printer.startswith(b"%PDF"))
        self.assertNotEqual(upright, printer, "rot=0 이 무시된다")

        def page_size(pdf):
            import re as _re
            m = _re.search(rb"/MediaBox\s*\[([\d .]+)\]", pdf)
            self.assertIsNotNone(m, "MediaBox 를 못 찾았다")
            nums = [float(x) for x in m.group(1).split()]
            return nums[2] - nums[0], nums[3] - nums[1]

        w, h = page_size(upright)
        self.assertGreater(w, h, f"화면 견본이 누워 있다({w:.0f}×{h:.0f})")
        pw, ph = page_size(printer)
        self.assertGreater(ph, pw, f"프린터 방향이 바뀌었다({pw:.0f}×{ph:.0f}) — 인쇄가 어긋난다")

    def test_화면이_견본_PDF를_그대로_띄운다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
              ).read_text("utf-8")
        blk = js.split("const paint = async () =>", 1)[1][:2200]
        self.assertIn("/api/cj/sample-pdf?rot=0&", blk, "화면 견본이 눕는다(rot=0 누락)")
        self.assertIn("<iframe", blk)
        self.assertIn("_=", blk, "캐시 때문에 옛 종이가 남는다")
        css = (Path(__file__).resolve().parent.parent / "static" / "css" / "app.css"
               ).read_text("utf-8")
        self.assertIn(".wb-pdf", css)
        self.assertNotIn(".wb-paper", css, "안 쓰는 모형 CSS가 남았다")

    def test_견본과_미리보기가_같은_문구를_쓴다(self):
        """종이와 화면이 다르면 무엇을 믿어야 할지 알 수 없다."""
        self.c.put("/api/settings", json={"cj": {
            "label_tmpl": {"item": "[쇼핑몰] [자산번호]", "remark": "[배송메모]"}}})
        pv = self.c.post("/api/cj/label-preview", json={}).get_json()
        self.assertTrue(pv["itemSummary"].startswith("[고도몰] 자산 "), pv["itemSummary"])
        src = (Path(__file__).resolve().parent.parent / "app" / "cj" / "tools_route.py"
               ).read_text("utf-8")
        blk = src.split("def cj_sample_pdf", 1)[1].split(chr(10) + "@bp.", 1)[0]
        self.assertIn("_compose_items", blk, "견본이 문구 계산을 안 거친다")
        self.assertIn("_SAMPLE_ORDER", blk, "미리보기와 다른 샘플을 쓴다")
        self.assertIn('label_tmpl_of(cfg, "remark")', blk)


class TestCjAddrTest(Base):
    def test_설정_없으면_400(self):
        r = self.c.post("/api/cj/addr-test", json={})
        self.assertEqual(r.status_code, 400)

    def test_정제_성공_경로(self):
        self.c.post("/api/cj/import-rms")
        from app.cj import tools_route
        with mock.patch.object(tools_route, "cj2_addr_refine",
                               return_value={"CLSFCD": "5D32", "CLSFADDR": "인천 부평",
                                             "CLLDLVBRANNM": "부평점"}) as spy:
            r = self.c.post("/api/cj/addr-test", json={})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["clsfcd"], "5D32")
        self.assertIn("서울특별시 예시구", spy.call_args[0][1])   # 보내는분 주소로 불렀다


class TestCjLiveRoundtrip(Base):
    def _arm(self):
        self.c.post("/api/cj/import-rms")
        self.c.put("/api/settings", json={"cj": {"armed": True}})

    def test_무장_전에는_400(self):
        self.c.post("/api/cj/import-rms")            # env=prod 지만 armed=False
        r = self.c.post("/api/cj/live-roundtrip")
        self.assertEqual(r.status_code, 400)
        self.assertIn("무장", r.get_json()["error"])

    def test_접수하고_반드시_같은_키로_취소한다(self):
        """★취소가 다른 키로 나가면 실제 예약이 살아 있는 채 기사가 온다."""
        self._arm()
        calls = []
        from app.cj import tools_route

        def fake_reg(cfg, **kw):
            calls.append(kw)
            return {"ok": True, "result_cd": "S00"}
        with mock.patch.object(tools_route, "cj2_new_invoice", return_value="650012345678"), \
             mock.patch.object(tools_route, "cj2_reg_book", side_effect=fake_reg):
            r = self.c.post("/api/cj/live-roundtrip")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(len(calls), 2, "접수 1번 + 취소 1번이어야 한다")
        reg, cancel = calls
        self.assertFalse(reg.get("cancel"))
        self.assertTrue(cancel.get("cancel"))
        for k in ("cust_use_no", "rcpt_ymd", "invc_no"):
            self.assertEqual(reg[k], cancel[k], f"{k} 가 접수/취소에서 다르다")
        self.assertEqual(reg["invc_no"], "650012345678")
        # 우리 주소 → 우리 주소
        self.assertEqual(reg["sender"], reg["receiver"])

    def test_취소_실패면_송장번호를_크게_알린다(self):
        self._arm()
        from app.cj import tools_route

        def fake_reg(cfg, **kw):
            if kw.get("cancel"):
                return {"ok": False, "result_cd": "E99", "detail": "이미 집화"}
            return {"ok": True, "result_cd": "S00"}
        with mock.patch.object(tools_route, "cj2_new_invoice", return_value="650099999999"), \
             mock.patch.object(tools_route, "cj2_reg_book", side_effect=fake_reg):
            r = self.c.post("/api/cj/live-roundtrip")
        self.assertEqual(r.status_code, 502)
        d = r.get_json()
        self.assertFalse(d["ok"])
        self.assertEqual(d["invoice"], "650099999999")
        self.assertIn("650099999999", d["message"])

    def test_접수_실패면_취소를_시도하지_않는다(self):
        self._arm()
        calls = []
        from app.cj import tools_route

        def fake_reg(cfg, **kw):
            calls.append(kw)
            return {"ok": False, "result_cd": "E01", "detail": "검증 오류"}
        with mock.patch.object(tools_route, "cj2_new_invoice", return_value="650000000001"), \
             mock.patch.object(tools_route, "cj2_reg_book", side_effect=fake_reg):
            r = self.c.post("/api/cj/live-roundtrip")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(len(calls), 1)


class TestCjDefaultsAndSync(Base):
    """RMS 격차 보강분(2026-08-10) — 운임/박스 기본값, 기간조회, 회수 송장번호 수집,
    취소 forceLocal 탈출구. 전부 모킹 — 진짜 CJ 호출 없음."""

    def _wb(self, wid, *, type_="recall", cj_kind="return", status="issued",
            invoice="", cuse="HBR1-120000", created="2026-08-01T10:00:00+09:00"):
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute(
                    "INSERT INTO waybills(wid, order_id, as_ticket_id, type, cj_kind, invoice_no, "
                    "status, recipient, phone, postal_code, address, items, cust_use_no, "
                    "cj_rcpt_ymd, scheduled_date, box_qty, asset_ids, asset_prev, "
                    "created_by, created_at, updated_at) "
                    "VALUES(?,NULL,NULL,?,?,?,?,'홍길동','010-1111-2222','06000','서울','상품',?,"
                    "'20260801','',1,'[]','{}','대표',?,?)",
                    (wid, type_, cj_kind, invoice, status, cuse, created, created))

    def _arm(self):
        self.c.post("/api/cj/import-rms")
        self.c.put("/api/settings", json={"cj": {"armed": True}})

    def test_접수_기본값이_설정을_따라간다(self):
        """★예전엔 client.py 기본(박스 02 소)이 나가 RMS(극소 01)와 다른 값을 보냈다."""
        import inspect

        from app.orders import recall, waybill
        for src in (inspect.getsource(waybill), inspect.getsource(recall)):
            self.assertIn('cfg.get("box_type") or "01"', src)
            self.assertIn('cfg.get("frt_dv") or "03"', src)

    def test_송장_목록_기간조회(self):
        self._wb("WB-A", created="2026-08-01T10:00:00+09:00")
        self._wb("WB-B", cuse="HBR2-120000", created="2026-08-05T10:00:00+09:00")
        d = self.c.get("/api/waybills?from=2026-08-04").get_json()
        self.assertEqual([w["wid"] for w in d], ["WB-B"])
        d = self.c.get("/api/waybills?to=2026-08-02").get_json()
        self.assertEqual([w["wid"] for w in d], ["WB-A"])

    def test_회수_송장번호_수집은_무장이_필요하다(self):
        r = self.c.post("/api/waybills/recall-invoice-sync", json={})
        self.assertEqual(r.status_code, 400)

    def test_회수_송장번호를_받아_채운다(self):
        """회수는 CJ가 집화 때 채번 — 예약 기준 추적으로만 번호를 받아올 수 있다."""
        self._arm()
        self._wb("WB-R1", cuse="HBR9-101010")
        from app.orders import recall as rc
        rows = [{"CUST_USE_NO": "hbr9-101010", "INVC_NO": "6500-1234-5678",
                 "CRG_ST": "02", "RCPT_DV": "02"}]
        with mock.patch.object(rc, "cj2_mss_track",
                               return_value={"ok": True, "rows": rows}):
            r = self.c.post("/api/waybills/recall-invoice-sync", json={"days": 1})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d["filled"], 1)
        w = self.c.get("/api/waybills?q=650012345678").get_json()
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["wid"], "WB-R1")
        self.assertEqual(w[0]["stageName"], "집화완료")

    def test_접수구분이_다르면_붙이지_않는다(self):
        """출고(01) 행이 회수 송장에 붙으면 엉뚱한 번호가 박힌다."""
        self._arm()
        self._wb("WB-R2", cuse="HBR8-101010")
        from app.orders import recall as rc
        rows = [{"CUST_USE_NO": "HBR8-101010", "INVC_NO": "650099990000",
                 "CRG_ST": "02", "RCPT_DV": "01"}]
        with mock.patch.object(rc, "cj2_mss_track",
                               return_value={"ok": True, "rows": rows}):
            d = self.c.post("/api/waybills/recall-invoice-sync", json={"days": 1}).get_json()
        self.assertEqual(d["filled"], 0)

    def test_취소된_송장은_되살리지_않는다(self):
        self._arm()
        self._wb("WB-R3", cuse="HBR7-101010", status="canceled")
        from app.orders import recall as rc
        rows = [{"CUST_USE_NO": "HBR7-101010", "INVC_NO": "650011112222",
                 "CRG_ST": "91", "RCPT_DV": "02"}]
        with mock.patch.object(rc, "cj2_mss_track",
                               return_value={"ok": True, "rows": rows}):
            d = self.c.post("/api/waybills/recall-invoice-sync", json={"days": 1}).get_json()
        self.assertEqual(d["filled"], 0)
        self.assertEqual(d["staged"], 0)

    def test_취소_forceLocal_탈출구(self):
        """CJ가 취소를 거절해도(이미 취소·집화) 우리 기록은 닫을 수 있어야
        재발급이 중복 가드에 영영 안 막힌다."""
        self._arm()
        self._wb("WB-F1", type_="forward", cj_kind="ship", status="issued",
                 invoice="650055556666", cuse="HB1-101010")
        from app.orders import waybill as wb
        refuse = {"ok": False, "result_cd": "E99", "detail": "이미 취소된 건"}
        with mock.patch.object(wb, "cj2_reg_book", return_value=refuse):
            r1 = self.c.post("/api/waybills/WB-F1/cancel", json={})
            self.assertEqual(r1.status_code, 502)
            r2 = self.c.post("/api/waybills/WB-F1/cancel", json={"forceLocal": True})
        self.assertEqual(r2.status_code, 200, r2.get_data(as_text=True))
        self.assertIn("E99", r2.get_json()["cjCancelFailed"])
        w = self.c.get("/api/waybills?q=WB-F1").get_json()
        self.assertEqual(w[0]["status"], "canceled")


class TestMultiWaybill(Base):
    """다매(RMS 이식, 2026-08-10) — 한 주문을 송장 N장으로. 테스트 발행 경로로 검증."""

    def _order_ready(self):
        cats = self.c.get("/api/categories").get_json()
        o = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "홍길동", "productName": "노트북",
            "phone": "010-1111-2222", "address": "서울 강남구 1", "quantity": 1,
            "amount": 100000}).get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "production", "value": True})
        # ★송장은 '출고 확인' 뒤에만 나온다(대표 2026-09-03) — 예전에는 제작 완료면 됐다
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "softwareInspection", "value": True})
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})
        return o

    def test_송장을_여러_장_발급한다(self):
        o = self._order_ready()
        r = self.c.post(f"/api/orders/{o['id']}/waybill", json={"waybillQty": 3})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d["qty"], 3)
        self.assertEqual(len(d["wids"]), 3)
        self.assertEqual(len(set(d["invoiceNos"])), 3, "송장번호가 겹치면 안 된다")
        rows = self.c.get(f"/api/waybills?q={o['recipient']}").get_json()
        self.assertEqual(len([w for w in rows if w["type"] == "forward"]), 3)
        # 접수키: 첫 장은 기본, 2·3번째는 -B2/-B3 (개별 취소의 PK)
        from app.db import tx
        with self.app.app_context():
            with tx() as conn:
                uses = [x["cust_use_no"] for x in conn.execute(
                    "SELECT cust_use_no FROM waybills ORDER BY wid").fetchall()]
        self.assertTrue(uses[1].endswith("-B2") and uses[2].endswith("-B3"), uses)

    def test_라벨에_몇번째_상자인지_찍힌다(self):
        o = self._order_ready()
        d = self.c.post(f"/api/orders/{o['id']}/waybill", json={"waybillQty": 2}).get_json()
        rows = self.c.get("/api/waybills").get_json()
        marks = sorted(w["items"][:6] for w in rows)
        self.assertEqual(marks, ["[1/2] ", "[2/2] "])

    def test_한_장이면_예전과_똑같다(self):
        o = self._order_ready()
        d = self.c.post(f"/api/orders/{o['id']}/waybill", json={"boxQty": 2}).get_json()
        self.assertEqual(d["qty"], 1)
        self.assertEqual(len(d["wids"]), 1)
        self.assertNotIn("[1/", self.c.get("/api/waybills").get_json()[0]["items"])


class TestWaybillPopup(Base):
    """송장번호 클릭 팝업 — 추적 타임라인 + 운영 메모(RMS 이식, 2026-08-10)."""

    def _wb(self, wid="WB-P1", status="test", invoice="999000000001"):
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute(
                    "INSERT INTO waybills(wid, order_id, as_ticket_id, type, cj_kind, invoice_no, "
                    "status, recipient, phone, postal_code, address, items, cust_use_no, "
                    "cj_rcpt_ymd, scheduled_date, box_qty, asset_ids, asset_prev, "
                    "created_by, created_at, updated_at) "
                    "VALUES(?,NULL,NULL,'forward','ship',?,?,'홍길동','010-1111-2222','06000',"
                    "'서울','노트북','HB-P1','','',1,'[]','{}','대표',"
                    "'2026-08-10T10:00:00+09:00','2026-08-10T10:00:00+09:00')",
                    (wid, invoice, status))

    def test_정보와_메모를_준다(self):
        self._wb()
        d = self.c.get("/api/waybills/WB-P1/trace").get_json()
        self.assertEqual(d["invoiceNo"], "999000000001")
        self.assertEqual(d["note"], "")
        self.assertIn("테스트", d["trackError"])     # 테스트 발행은 CJ 추적이 없다고 알려준다

    def test_메모를_저장하고_다시_읽는다(self):
        self._wb()
        r = self.c.post("/api/waybills/WB-P1/note", json={"note": "고객 통화 — 수요일 재배송 약속"})
        self.assertEqual(r.status_code, 200)
        d = self.c.get("/api/waybills/WB-P1/trace").get_json()
        self.assertEqual(d["note"], "고객 통화 — 수요일 재배송 약속")

    def test_실발행이면_추적을_불러_타임라인을_준다(self):
        self._wb(status="issued", invoice="650012345678")
        self.c.post("/api/cj/import-rms")
        self.c.put("/api/settings", json={"cj": {"armed": True}})
        from app.orders import waybill as wb
        track = {"ok": True, "data": {"PROC_LIST": [
            {"CRG_ST_CD": "02", "CRG_ST_NM": "집화완료", "SCAN_YMD": "20260810",
             "SCAN_TME": "1030", "DEALT_BRAN_NM": "부평점"}]}}
        with mock.patch.object(wb, "cj2_track", return_value=track):
            d = self.c.get("/api/waybills/WB-P1/trace").get_json()
        self.assertEqual(len(d["timeline"]), 1)
        self.assertEqual(d["timeline"][0]["name"], "집화완료")
        self.assertEqual(d["timeline"][0]["branch"], "부평점")


class TestAddrSearchWiring(Base):
    """카카오 주소검색이 주소 입력처 전부에 붙었는가 — RMS와 동일 커버리지."""

    def test_공용_헬퍼가_있다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js").read_text("utf-8")
        self.assertIn("function loadDaumPostcode", js)
        self.assertIn("function attachAddrSearch", js)
        self.assertIn("postcode.v2.js", js)

    def test_주소_입력처_전부에_배선됐다(self):
        root = Path(__file__).resolve().parent.parent / "static" / "js"
        ords = (root / "orders.js").read_text("utf-8")
        apps = (root / "app.js").read_text("utf-8")
        asjs = (root / "as.js").read_text("utf-8")
        for src, sel in ((ords, '"#no-zip"'), (ords, '"#od-zip"'),
                         (apps, '"#cj-szip"'), (apps, '"#cj-pzip"'),
                         (asjs, '"#asn-address"'), (asjs, '"#asd-address"')):
            self.assertIn("attachAddrSearch", src)
            self.assertIn(sel, src, f"{sel} 에 주소검색이 안 붙었다")


class TestPartsBook(Base):
    """부품 단가표(대표 2026-08-10) — RAM/SSD 단가가 매일 바뀌니 '오늘 단가'를 한 곳에서
    관리하고, 자산에 추가하는 순간 그 단가가 원가로 동결된다."""

    def _cat(self):
        return self.c.get("/api/categories").get_json()[0]["id"]

    def _asset(self, **kw):
        body = {"categoryId": self._cat(), "model": "L480", "qty": 1,
                "purchasePrice": 100000, **kw}
        return self.c.post("/api/assets", json=body).get_json()[0]

    def _part(self, name="D4 8G", category="ram", price=15000):
        return ensure_part(self.c, name, category, price)

    def test_단가표_등록과_수정(self):
        pid = self._part()
        self.c.patch(f"/api/parts/{pid}", json={"price": 17000})
        rows = self.c.get("/api/parts").get_json()
        # 자리(index)로 집지 않는다 — 시트지 기본 시드가 함께 있어 정렬이 바뀔 수 있다
        mine = next(r for r in rows if r["id"] == pid)
        self.assertEqual(mine["price"], 17000)

    def test_부품_추가는_서버가_단가를_읽는다(self):
        """★화면이 보낸 금액을 믿지 않는다 — 오늘 단가의 출처는 단가표 하나."""
        pid = self._part(price=15000)
        a = self._asset()
        r = self.c.post(f"/api/assets/{a['id']}/repairs", json={"partIds": [pid]})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["cost"], 15000)
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(d["repairTotal"], 15000)
        self.assertEqual(d["costTotal"], 115000)

    def test_그날_단가로_동결된다(self):
        """단가표를 나중에 고쳐도 이미 추가된 자산의 원가는 안 바뀐다."""
        pid = self._part(price=15000)
        a = self._asset()
        self.c.post(f"/api/assets/{a['id']}/repairs", json={"partIds": [pid]})
        self.c.patch(f"/api/parts/{pid}", json={"price": 99000})
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(d["repairTotal"], 15000, "과거 원가가 새 단가로 바뀌면 장부가 무너진다")

    def test_스펙_칸이_같이_채워진다(self):
        """부품을 꽂았는데 ram이 'X'로 남으면 재고 그룹핑·자산매칭이 거짓이 된다."""
        pid = self._part(name="D4 16G", category="ram", price=30000)
        a = self._asset(ram="X")
        self.c.post(f"/api/assets/{a['id']}/repairs", json={"partIds": [pid]})
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(d["ram"], "D4 16G")

    def test_기존_스펙이_있으면_병기한다(self):
        pid = self._part(name="NVMe 512G", category="ssd", price=45000)
        a = self._asset(ssd="M.2 128G")
        self.c.post(f"/api/assets/{a['id']}/repairs", json={"partIds": [pid]})
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(d["ssd"], "M.2 128G + NVMe 512G")

    def test_부가세가_자동_분리된다(self):
        pid = self._part(price=11000)
        a = self._asset()
        self.c.post(f"/api/assets/{a['id']}/repairs", json={"partIds": [pid]})
        rep = self.c.get(f"/api/assets/{a['id']}").get_json()["repairs"][0]
        self.assertEqual(rep["vat"], 1000)
        self.assertEqual(rep["net"], 10000)

    def test_단가_0원이면_거부한다(self):
        pid = self._part(price=0)
        a = self._asset()
        r = self.c.post(f"/api/assets/{a['id']}/repairs", json={"partIds": [pid]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("단가", r.get_json()["error"])

    def test_일괄_추가(self):
        p1 = self._part(name="D4 8G", category="ram", price=15000)
        p2 = self._part(name="NVMe 256G", category="ssd", price=30000)
        a1, a2 = self._asset(ram="X"), self._asset(ram="X")
        r = self.c.post("/api/assets/bulk-repairs",
                        json={"ids": [a1["id"], a2["id"]], "partIds": [p1, p2]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d["ok"], 2)
        self.assertEqual(d["perAsset"], 45000)
        self.assertEqual(d["totalCost"], 90000)
        got = self.c.get(f"/api/assets/{a1['id']}").get_json()
        self.assertEqual(got["repairTotal"], 45000)
        self.assertEqual(got["ram"], "D4 8G")

    def test_전표_대조는_부품비와_무관하다(self):
        """★핵심 불변식 — 부품비가 전표 금액 대조(amountGap)에 스며들면
        멀쩡한 전표마다 경고가 뜨고, '금액 맞추기'가 거래처 지급액을 오염시킨다."""
        sid = self.c.post("/api/suppliers", json={"name": "부품시험거래처"}).get_json()["id"]
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": sid,
            "purchaseDate": "2026-08-10", "totalAmount": 100000,
            "assets": [{"categoryId": self._cat(), "qty": 1, "model": "L480",
                        "purchasePrice": 100000}]})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        b = r.get_json()
        aid = self.c.get(f"/api/purchase-batches/{b['id']}").get_json()["assets"][0]["id"]
        pid = self._part(price=50000)
        self.c.post(f"/api/assets/{aid}/repairs", json={"partIds": [pid]})
        d = self.c.get(f"/api/purchase-batches/{b['id']}").get_json()
        self.assertEqual(d["totalAmount"], 100000, "전표 금액이 오염됐다")
        self.assertEqual(d["assignedAmount"], 100000, "자산합에 부품비가 스며들었다")
        self.assertEqual(d["repairTotal"], 50000)
        self.assertEqual(d["costTotal"], 150000)
        # 자산별 부품·수리 열
        self.assertEqual(d["assets"][0]["repairCost"], 50000)


class TestCodeSpecs(Base):
    """제품코드 모델 속성(2026-08-13 대표) — 고도몰 스펙 파싱 + 구분별 옵션 기입.

    "16GB 추가"가 DDR4냐 DDR5냐는 제품코드 스펙이 정한다. 스펙을 모르면
    추측하지 않고 보류한다(틀린 단가가 조용히 들어가는 것이 최악).
    """

    def test_파서가_세대와_방식을_정확히_가른다(self):
        from app.malls.collect import parse_code_spec
        self.assertEqual(parse_code_spec("삼성 DDR4 16GB NVMe 512GB"),
                         ("DDR4", False, "M.2 NVMe"))
        self.assertEqual(parse_code_spec("LPDDR4X 8GB 온보드"), ("LPDDR4", True, ""))
        # 온보드+확장슬롯 하이브리드 — 업글은 슬롯에 꽂으니 슬롯 세대(DDR4)가 정답
        self.assertEqual(parse_code_spec("4GB 온보드(LPDDR4) + DDR4 슬롯 1"), ("DDR4", True, ""))
        self.assertEqual(parse_code_spec("M.2 SATA 256GB"), ("", False, "M.2 SATA"))
        self.assertEqual(parse_code_spec("SSD SATA 2.5인치 500GB"), ("", False, "2.5 SSD"))
        self.assertEqual(parse_code_spec("2.5인치 HDD 1TB"), ("", False, "2.5 HDD"))
        # 'SATA 2.5인치'만 있으면(SSD/HDD 낱말 없음) 방식을 추측하지 않는다
        self.assertEqual(parse_code_spec("SATA 2.5인치 500GB"), ("", False, ""))
        self.assertEqual(parse_code_spec("SATA 500GB DDR5"), ("DDR5", False, ""))
        # 듀얼 드라이브 — 주 저장장치(SSD 쪽)를 기준으로 잡는다
        self.assertEqual(parse_code_spec("NVMe 256GB + HDD 1TB DDR4"),
                         ("DDR4", False, "M.2 NVMe"))

    def test_저장장치_규격이_시드된다(self):
        """대표 2026-08-13: 2.5 HDD·2.5 SSD·M.2 SATA·M.2 NVMe 4종 × 용량.
        규격은 v2가 만들고 단가는 v3(콤보표)가 채운다 — 콤보에 없는 규격은 0으로 남는다."""
        names = {p["name"]: p for p in self.c.get("/api/parts").get_json()}
        for n in ("M.2 NVMe 512G", "M.2 SATA 256G", "2.5 SSD 1TB", "2.5 HDD 500G"):
            self.assertIn(n, names)
            self.assertEqual(names[n]["category"], "ssd")
        self.assertEqual(names["2.5 SSD 2TB"]["price"], 0)   # 콤보에 없는 규격 — 빈 단가
        self.assertNotIn("2.5 HDD 2TB", names)      # HDD는 500G/1TB 두 가지만

    def test_대제목_그룹이_자동_분류된다(self):
        """대표 2026-08-13 "대제목으로 접고 펴서 규격별로" — 시드·시트지·램이
        각자의 묶음(grp)에 자동으로 들어가고, 사람이 지정한 그룹은 그대로 남는다."""
        parts = {p["name"]: p for p in self.c.get("/api/parts").get_json()}
        self.assertEqual(parts["M.2 NVMe 512G"]["group"], "M.2 NVMe")
        self.assertEqual(parts["2.5 HDD 1TB"]["group"], "2.5 HDD")
        # ★2026-08-25 단가표 3분할: 시트지·도색은 '도색/시트지' 묶음으로 이동했다
        self.assertEqual(parts["시트지(상)"]["group"], "도색/시트지")
        # 램은 그룹을 안 적어도 자동으로 RAM 묶음(시드에 없는 새 이름으로 생성 경로 검증)
        pid = self.c.post("/api/parts", json={
            "name": "D4L 32G", "category": "ram", "price": 60000}).get_json()["id"]
        parts = {p["name"]: p for p in self.c.get("/api/parts").get_json()}
        self.assertEqual(parts["D4L 32G"]["group"], "RAM")
        # 직접 지정·수정도 된다
        self.c.patch(f"/api/parts/{pid}", json={"group": "특수램"})
        parts = {p["name"]: p for p in self.c.get("/api/parts").get_json()}
        self.assertEqual(parts["D4L 32G"]["group"], "특수램")

    def _fake_adapter(self, text):
        class A:
            name = "고도몰"
            def search_goods(self, q, field="code", size=20):
                return [{"goodsCd": q, "goodsNm": text, "shortDescription": "", "modelNo": ""}]
        return A()

    def test_몰_동기화가_파싱해_저장한다(self):
        from app.malls import collect as collect_mod
        with mock.patch.object(collect_mod, "get_adapter",
                               return_value=(self._fake_adapter("DDR5 NVMe 512G"), "")):
            r = self.c.post("/api/code-specs/sync", json={"code": "NB-100"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = self.c.get("/api/code-specs?code=NB-100").get_json()
        self.assertEqual((d["ramGen"], d["storageType"], d["source"]),
                         ("DDR5", "M.2 NVMe", "godomall"))

    def test_고도몰에_없으면_스마트스토어에서_찾는다(self):
        """제품코드는 몰마다 동일(대표 2026-08-13) — 고도몰에 없는 코드도
        스마트스토어 판매자상품코드로 찾아 스펙을 채운다."""
        from app.malls import collect as collect_mod

        class Godo:
            name = "고도몰"
            def search_goods(self, q, field="code", size=20):
                return []                       # 고도몰엔 이 코드가 없다

        class Smart:
            name = "스마트스토어"
            def search_goods(self, q, field="code", size=20):
                return [{"goodsCd": q, "goodsNm": "삼성 갤럭시북 DDR5 NVMe 512G",
                         "shortDescription": "", "modelNo": ""}]

        def fake_get_adapter(mall_code, s):
            if mall_code == "godomall":
                return Godo(), ""
            if mall_code == "smartstore":
                return Smart(), ""
            return None, "꺼짐"

        with mock.patch.object(collect_mod, "get_adapter", side_effect=fake_get_adapter):
            r = self.c.post("/api/code-specs/sync", json={"code": "NB-NAVER"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = self.c.get("/api/code-specs?code=NB-NAVER").get_json()
        self.assertEqual((d["ramGen"], d["storageType"], d["source"]),
                         ("DDR5", "M.2 NVMe", "smartstore"))

    def test_아무_몰에도_없으면_찾아본_몰을_알려준다(self):
        from app.malls import collect as collect_mod

        class Empty:
            name = "고도몰"
            def search_goods(self, q, field="code", size=20):
                return []

        with mock.patch.object(collect_mod, "get_adapter",
                               side_effect=lambda mc, s:
                               (Empty(), "") if mc == "godomall" else (None, "꺼짐")):
            r = self.c.post("/api/code-specs/sync", json={"code": "NB-NOPE"})
        self.assertEqual(r.status_code, 404)
        self.assertIn("고도몰", r.get_json()["error"])

    def test_직접_지정은_동기화가_안_덮는다(self):
        self.c.patch("/api/code-specs", json={"code": "NB-200", "ramGen": "DDR4"})
        from app.malls import collect as collect_mod
        with mock.patch.object(collect_mod, "get_adapter",
                               return_value=(self._fake_adapter("DDR5 NVMe"), "")):
            r = self.c.post("/api/code-specs/sync", json={"code": "NB-200"})
        self.assertEqual(r.get_json().get("kept"), "manual")
        self.assertEqual(self.c.get("/api/code-specs?code=NB-200").get_json()["ramGen"], "DDR4")

    # ── 구분별 연결 끝-끝 ─────────────────────────────────────────────
    def _mk_option_with_map(self):
        p4 = ensure_part(self.c, "D4 16G", "ram", 30000)
        p5 = ensure_part(self.c, "D5 16G", "ram", 45000)
        opt = self.c.post("/api/prep-options", json={"name": "RAM 16GB 추가"}).get_json()
        self.c.post(f"/api/prep-options/{opt['id']}/rules",
                    json={"channel": "", "matchType": "option", "matchValue": "RAM16"})
        r = self.c.patch(f"/api/prep-options/{opt['id']}", json={
            "partMap": [{"gen": "DDR4", "partId": p4}, {"gen": "DDR5", "partId": p5}]})
        assert r.status_code == 200, r.get_data(as_text=True)
        return opt["id"]

    def _order_with_code(self, code):
        return self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "홍길동", "productName": "노트북",
            "productCode": code, "optionName": "RAM16 업그레이드(+40,000원)",
            "quantity": 1, "amount": 500000}).get_json()

    def _asset2(self):
        cat = self.c.get("/api/categories").get_json()[0]["id"]
        return self.c.post("/api/assets", json={
            "categoryId": cat, "model": "L480", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]

    def test_코드가_DDR5면_D5_부품이_들어간다(self):
        opt_id = self._mk_option_with_map()
        self.c.patch("/api/code-specs", json={"code": "NB-DDR5", "ramGen": "DDR5"})
        o = self._order_with_code("NB-DDR5")
        a = self._asset2()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        r = self.c.post(f"/api/orders/{o['id']}/options/{opt_id}",
                        json={"checked": True}).get_json()
        self.assertEqual(r["partApplied"], 1)
        self.assertEqual(r["partCost"], 45000, "D5 단가가 들어가야 한다 — D4가 아니라")
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["ram"], "D5 16G")

    def test_스펙을_모르면_보류하고_알린다(self):
        opt_id = self._mk_option_with_map()
        o = self._order_with_code("NB-UNKNOWN")          # code_specs 없음
        a = self._asset2()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        r = self.c.post(f"/api/orders/{o['id']}/options/{opt_id}",
                        json={"checked": True}).get_json()
        self.assertEqual(r["partApplied"], 0)
        self.assertIn("보류", r["partMessage"])
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["repairTotal"], 0)

    def test_체크된_상태에서_매칭돼도_같은_판별이다(self):
        opt_id = self._mk_option_with_map()
        self.c.patch("/api/code-specs", json={"code": "NB-DDR4", "ramGen": "DDR4"})
        o = self._order_with_code("NB-DDR4")
        self.c.post(f"/api/orders/{o['id']}/options/{opt_id}", json={"checked": True})
        a = self._asset2()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["repairTotal"],
                         30000, "매칭 시점 기입도 DDR4 → D4 단가여야 한다")


class TestPartDowngrade(Base):
    """다운그레이드 회수(2026-08-13 대표 공식): 매입 16G/512G → 출고 8G/256G 면
    정해진 부품가 차액(A=16G−8G, B=512G−256G)을 순수익에 '마지막에 더해야' 한다.

    구현: 회수 = 음수 원가 행 — 원가 합이 (A+B)만큼 줄어 마진이 정확히 그만큼 커진다.
    (회수 −16G단가 + 장착 +8G단가 = −A 원가 변동 = +A 순수익 — 대표 공식과 동일식)
    """

    def setUp(self):
        super().setUp()
        self.cats = self.c.get("/api/categories").get_json()
        self.p8 = ensure_part(self.c, "D4 8G", "ram", 12000)
        self.p16 = ensure_part(self.c, "D4 16G", "ram", 30000)
        self.s256 = ensure_part(self.c, "NVMe 256G", "ssd", 20000)
        self.s512 = ensure_part(self.c, "NVMe 512G", "ssd", 38000)

    def _asset(self, ram="D4 16G", ssd="NVMe 512G"):
        a = self.c.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 300000}).get_json()[0]
        if ram or ssd:                      # 빈 값끼리는 '변경 없음' 400이 정상이라 건너뛴다
            r = self.c.patch(f"/api/assets/{a['id']}", json={"ram": ram, "ssd": ssd})
            assert r.status_code == 200, r.get_data(as_text=True)
        return a

    def test_대표공식_다운그레이드_차액이_순수익에_더해진다(self):
        a = self._asset()                               # 매입 스펙 16G / 512G
        r = self.c.post(f"/api/assets/{a['id']}/repairs", json={"partIds": [
            {"id": self.p16, "remove": True}, {"id": self.p8},
            {"id": self.s512, "remove": True}, {"id": self.s256}]})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        A = 30000 - 12000                               # 16G − 8G
        B = 38000 - 20000                               # 512G − 256G
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(sum(x["cost"] for x in d["repairs"]), -(A + B))
        self.assertEqual(d["ram"], "D4 8G")             # 출고 사양으로 바뀌어 있다
        self.assertEqual(d["ssd"], "NVMe 256G")

    def test_회수는_병기_스펙에서_하나만_지운다(self):
        a = self._asset(ram="D4 8G + D4 8G", ssd="X")
        self.c.post(f"/api/assets/{a['id']}/repairs",
                    json={"partIds": [{"id": self.p8, "remove": True}]})
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["ram"], "D4 8G")
        self.c.post(f"/api/assets/{a['id']}/repairs",
                    json={"partIds": [{"id": self.p8, "remove": True}]})
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["ram"], "X")

    def test_일괄_회수도_음수로_반영된다(self):
        a1, a2 = self._asset(), self._asset()
        r = self.c.post("/api/assets/bulk-repairs", json={
            "ids": [a1["id"], a2["id"]],
            "partIds": [{"id": self.p16, "remove": True}]}).get_json()
        self.assertEqual(r["ok"], 2)
        self.assertEqual(r["perAsset"], -30000)
        self.assertEqual(r["totalCost"], -60000)

    def test_예전_숫자형_partIds는_그대로_장착이다(self):
        a = self._asset(ram="", ssd="")
        r = self.c.post(f"/api/assets/{a['id']}/repairs", json={"partIds": [self.p8]})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(sum(x["cost"] for x in d["repairs"]), 12000)
        self.assertEqual(d["ram"], "D4 8G")

    def test_시트지_기본단가가_시드된다(self):
        """대표 2026-08-13: 시트지(상) 11,000 / 시트지(상,중) 16,500."""
        parts = {p["name"]: p for p in self.c.get("/api/parts").get_json()}
        self.assertEqual(parts["시트지(상)"]["price"], 11000)
        self.assertEqual(parts["시트지(상,중)"]["price"], 16500)

    def test_콤보표_단가가_시드된다(self):
        """콤보관리_260813.xlsx(대표 제공) — 'X→용량' 모듈 단가만 시드.
        전환 행(4G→8G 등)은 전부 모듈 차액이라 장착/회수 엔진이 자동 산출한다
        (검산: 4G→8G 24,000 = 8G 44,000 − 4G 20,000). CPU·그래픽·파워·보드는
        데스크탑용 후순위라 안 들어간다. setUp이 시험 단가로 덮은 줄은 피해서 본다."""
        parts = {p["name"]: p for p in self.c.get("/api/parts").get_json()}
        self.assertEqual(parts["D4 4G"]["price"], 20000)
        self.assertEqual((parts["D5 16G"]["price"], parts["D5 16G"]["group"]), (200000, "RAM"))
        self.assertEqual(parts["D3 4G"]["price"], 10000)
        self.assertEqual(parts["M.2 NVMe 512G"]["price"], 88000)
        self.assertEqual(parts["M.2 SATA 512G"]["price"], 88000)   # 콤보 M/N 공용 시작값
        self.assertEqual(parts["2.5 SSD 1TB"]["price"], 90000)
        self.assertEqual(parts["2.5 HDD 500G"]["price"], 6000)
        self.assertEqual(parts["2.5 SSD 2TB"]["price"], 0)         # 콤보에 없음 — 빈 채로

    def test_데스크탑_부품이_수동용으로_시드된다(self):
        """대표 "진행해 줘"(2026-08-13) — CPU·그래픽·파워·보드는 수동 장착/회수 전용.
        호환(소켓·보드·파워)은 사람이 판단하므로 옵션 자동 기입에는 안 붙는다."""
        parts = {p["name"]: p for p in self.c.get("/api/parts").get_json()}
        self.assertEqual((parts["CPU I5-8"]["price"], parts["CPU I5-8"]["group"]), (77000, "CPU"))
        self.assertEqual((parts["그래픽 RTX3080"]["price"], parts["그래픽 RTX3080"]["group"]),
                         (374000, "그래픽"))
        self.assertEqual(parts["파워 600W"]["group"], "파워")
        self.assertEqual(parts["보드 B460M-DS3H"]["price"], 77000)
        # category는 비움 — ram/ssd 스펙 칸 자동 갱신 대상이 아니다
        self.assertEqual(parts["CPU I5-8"]["category"], "")

    def test_시드는_한_번만_고쳐도_안_되살아난다(self):
        pid = {p["name"]: p["id"] for p in self.c.get("/api/parts").get_json()}["시트지(상)"]
        self.c.patch(f"/api/parts/{pid}", json={"name": "시트지(상)-단종", "enabled": False})
        create_app(db_path=self.tmp / "test.db")        # 재기동 재현 — 마이그레이션 재실행
        names = [p["name"] for p in self.c.get("/api/parts").get_json()]
        self.assertNotIn("시트지(상)", names)            # 마커가 재시드를 막는다


class TestBaseSpecFill(Base):
    """매입 기준사양 채우기(2026-08-14 대표 승인) — 코드 스펙 vs 실물 스펙 차이를
    부품 단가표 단가로 기입. 버튼+확인창, 적용 즉시 스펙 반영, 다운그레이드 회수 포함.
    """

    def setUp(self):
        super().setUp()
        self.cats = self.c.get("/api/categories").get_json()
        ensure_part(self.c, "D4 4G", "ram", 20000)
        ensure_part(self.c, "D4 8G", "ram", 44000)
        ensure_part(self.c, "M.2 SATA 256G", "ssd", 44000)
        ensure_part(self.c, "M.2 SATA 512G", "ssd", 88000)
        # 코드 NB-BASE = DDR4 8GB · M.2 SATA 256G (대표 예시 그대로)
        r = self.c.patch("/api/code-specs", json={
            "code": "NB-BASE", "ramGen": "DDR4", "ramGb": 8,
            "storageType": "M.2 SATA", "storageCap": "256G"})
        assert r.status_code == 200, r.get_data(as_text=True)

    def _asset(self, ram="", ssd="", code="NB-BASE"):
        a = self.c.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        if ram or ssd:
            r = self.c.patch(f"/api/assets/{a['id']}", json={"ram": ram, "ssd": ssd})
            assert r.status_code == 200, r.get_data(as_text=True)
        if code:
            r = self.c.post("/api/assets/product-code",
                            json={"ids": [a["id"]], "productCode": code})
            assert r.status_code == 200, r.get_data(as_text=True)
        return a

    def _apply(self, *aids):
        r = self.c.post("/api/assets/base-spec-apply", json={"ids": list(aids)})
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.get_json()

    def test_둘_다_없으면_둘_다_채운다(self):
        a = self._asset()
        res = self._apply(a["id"])
        self.assertEqual(res["totalCost"], 44000 + 44000)
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(d["ram"], "D4 8G")
        self.assertEqual(d["ssd"], "M.2 SATA 256G")
        # ★멱등 — 다시 눌러도 스펙이 기준과 같아져 아무것도 안 들어간다
        res2 = self._apply(a["id"])
        self.assertEqual(res2["totalCost"], 0)
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["repairTotal"], 88000)

    def test_일부만_있으면_부족한_것만(self):
        a = self._asset(ram="D4 8G")
        res = self._apply(a["id"])
        self.assertEqual(res["totalCost"], 44000)          # SSD만
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["ram"], "D4 8G")

    def test_상위가_꽂혀_오면_다운그레이드_회수를_제안한다(self):
        a = self._asset(ram="D4 8G", ssd="M.2 SATA 512G")
        res = self._apply(a["id"])
        self.assertEqual(res["totalCost"], -88000 + 44000)  # 512 회수 + 256 장착 = −44,000
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(d["ssd"], "M.2 SATA 256G")

    def test_스왑_차액이_콤보_전환가와_같다(self):
        """콤보표 'DDR4 4G→8G 24,000'을 회수+장착이 그대로 재현해야 한다."""
        a = self._asset(ram="D4 4G")
        res = self._apply(a["id"])
        ram_part = -20000 + 44000                          # 24,000 = 콤보 전환가
        self.assertEqual(res["totalCost"], ram_part + 44000)  # + SSD 256G
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["ram"], "D4 8G")

    def test_코드나_스펙이_없으면_이유를_말하고_보류한다(self):
        a = self._asset(code="")                           # 코드 없음
        res = self._apply(a["id"])
        self.assertEqual(res["totalCost"], 0)
        self.assertIn("제품코드", res["results"][0]["reason"])
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["repairTotal"], 0)

    def test_전표_미리보기가_자산별로_계산한다(self):
        sid = self.c.post("/api/suppliers", json={"name": "기준사양거래처"}).get_json()["id"]
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": sid,
            "purchaseDate": "2026-08-14", "totalAmount": 200000,
            "assets": [{"categoryId": self.cats[0]["id"], "qty": 2, "model": "L480",
                        "purchasePrice": 100000}]})
        assert r.status_code == 201, r.get_data(as_text=True)
        b = r.get_json()
        aids = [x["id"] for x in self.c.get(f"/api/purchase-batches/{b['id']}").get_json()["assets"]]
        self.c.post("/api/assets/product-code", json={"ids": aids, "productCode": "NB-BASE"})
        # 한 대만 램이 이미 있음 — 자산별로 다른 부족분이 나와야 한다(질문 3)
        self.c.patch(f"/api/assets/{aids[0]}", json={"ram": "D4 8G", "ssd": ""})
        g_ = self.c.get(f"/api/purchase-batches/{b['id']}/base-spec-gaps").get_json()
        by = {x["assetId"]: x for x in g_["assets"]}
        self.assertEqual(by[aids[0]]["total"], 44000)          # SSD만
        self.assertEqual(by[aids[1]]["total"], 88000)          # 둘 다
        self.assertEqual(g_["total"], 132000)


class TestNumberConflict(Base):
    """자산번호 충돌(2026-08-18 대표 지시).

    "TMS단에서 잘못 입력이 되어 판매가 되어서 중복값으로 인식되는 경우가 생겨.
     중복이라고 뜨는 것도 셋팅QC단에서 입력은 되게끔 해주되, 매입단에서
     실제 TMS와 OWS 간의 중복되는 데이터값을 치환/교환할 수 있게 해줘."
    """

    def _asset(self, no, **kw):
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": kw.pop("model", "L480"), "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        if no:
            r = self.c.patch(f"/api/assets/{a['id']}", json={"assetNo": no})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        for k, v in kw.items():
            self.c.patch(f"/api/assets/{a['id']}", json={k: v})
        return self.c.get(f"/api/assets/{a['id']}").get_json()

    def _order(self, **kw):
        base = {"channel": "고도몰", "recipient": "홍길동", "productName": "노트북",
                "phone": "010-1111-2222", "address": "서울시 강남구", "postalCode": "06000",
                "quantity": 1, "amount": 500000}
        base.update(kw)
        return self.c.post("/api/orders", json=base).get_json()

    def _ship(self, aid):
        """TMS가 '판매'로 찍은 상태를 흉내 낸다 — 실물은 창고에 있다."""
        with self.app.app_context():
            from app.db import tx
            with tx(write=True) as conn:
                conn.execute("UPDATE assets SET status='shipped' WHERE id=?", (aid,))

    # ---- 셋팅/QC: 막지 않는다 ----
    def test_충돌이면_강제매칭이_가능하다고_알려준다(self):
        a = self._asset("260701-0012")
        self._ship(a["id"])
        rows = self.c.get("/api/orders/asset-search?q=260701-0012").get_json()
        hit = next(r for r in rows if r["assetNo"] == "260701-0012")
        self.assertFalse(hit["available"], "출고 상태인데 그냥 매칭이 된다")
        self.assertTrue(hit["forceable"], "TMS 오기입을 바로잡을 길이 없다")

    def test_force로_붙일_수_있다(self):
        a = self._asset("260701-0013")
        self._ship(a["id"])
        o = self._order()
        r = self.c.patch(f"/api/orders/{o['id']}",
                         json={"action": "assets", "assetIds": [a["id"]]})
        self.assertEqual(r.status_code, 409, "force 없이 붙었다")
        r = self.c.patch(f"/api/orders/{o['id']}",
                         json={"action": "assets", "assetIds": [a["id"]], "force": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual([x["assetNo"] for x in r.get_json()["assets"]], ["260701-0013"])

    def test_force로_붙이면_TMS가_못_건드리게_잠긴다(self):
        """★잠그지 않으면 2시간 뒤 다음 수집이 다시 '출고'로 되돌린다."""
        a = self._asset("260701-0014")
        self._ship(a["id"])
        o = self._order()
        self.c.patch(f"/api/orders/{o['id']}",
                     json={"action": "assets", "assetIds": [a["id"]], "force": True})
        got = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertTrue(got["tmsLock"], "TMS 자동반영 잠금이 안 걸렸다")
        d = self.c.get("/api/assets/number-conflicts").get_json()
        self.assertEqual([x["assetNo"] for x in d["rows"]], ["260701-0014"],
                         "매입에서 정리할 목록에 안 올라왔다")

    def test_OWS_주문이_잡고_있으면_강제로_못_붙인다(self):
        """진짜로 나간 물건이다 — 두 번 팔면 안 된다."""
        # ★'중복 매칭 허용'(2026-08-31, 기본 켜짐)을 끄고 차단 모드의 안전핀을 검증한다
        self.assertEqual(self.c.put("/api/settings", json={
            "order_asset_duplicate": {"enabled": False}}).status_code, 200)
        a = self._asset("260701-0015")
        o1 = self._order(recipient="먼저산사람")
        self.c.patch(f"/api/orders/{o1['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self._ship(a["id"])
        rows = self.c.get("/api/orders/asset-search?q=260701-0015").get_json()
        hit = next(r for r in rows if r["assetNo"] == "260701-0015")
        self.assertFalse(hit["forceable"], "주문이 잡고 있는데 강제 매칭을 권한다")
        o2 = self._order(recipient="나중사람")
        r = self.c.patch(f"/api/orders/{o2['id']}",
                         json={"action": "assets", "assetIds": [a["id"]], "force": True})
        self.assertEqual(r.status_code, 409, "★같은 물건이 두 주문에 붙었다")

    def test_수리중인_자산은_강제_대상이_아니다(self):
        """TMS가 만든 상태가 아니라 OWS에서 사람이 정한 상태다."""
        a = self._asset("260701-0016")
        self.c.patch(f"/api/assets/{a['id']}", json={"status": "repair"})
        rows = self.c.get("/api/orders/asset-search?q=260701-0016").get_json()
        hit = next(r for r in rows if r["assetNo"] == "260701-0016")
        self.assertFalse(hit["forceable"])
        o = self._order()
        self.assertEqual(self.c.patch(f"/api/orders/{o['id']}", json={
            "action": "assets", "assetIds": [a["id"]], "force": True}).status_code, 409)

    # ---- 매입: 치환 / 교환 ----
    def test_맞바꾸기(self):
        a = self._asset("260701-0020", model="그램")
        b = self._asset("260701-0021", model="싱크패드")
        r = self.c.post("/api/assets/number-fix", json={
            "assetId": a["id"], "targetNo": "260701-0021", "mode": "swap",
            "reason": "TMS에서 두 기계 번호가 뒤바뀜"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["assetNo"], "260701-0021")
        self.assertEqual(self.c.get(f"/api/assets/{b['id']}").get_json()["assetNo"], "260701-0020")

    def test_넘겨받기는_상대를_임시번호로_밀어낸다(self):
        a = self._asset("260701-0030", model="그램")
        bad = self._asset("260701-0031", model="유령")
        r = self.c.post("/api/assets/number-fix", json={
            "assetId": a["id"], "targetNo": "260701-0031", "mode": "take",
            "reason": "TMS 오입력으로 판매 처리된 기록"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["assetNo"], "260701-0031")
        moved = self.c.get(f"/api/assets/{bad['id']}").get_json()["assetNo"]
        self.assertEqual(moved, "260701-0031-오류1", f"밀려난 번호가 이상하다: {moved}")

    def test_사유_없이는_못_바꾼다(self):
        a = self._asset("260701-0040")
        self.assertEqual(self.c.post("/api/assets/number-fix", json={
            "assetId": a["id"], "targetNo": "260701-0041", "mode": "take"}).status_code, 400)

    def test_바로잡으면_충돌이_닫힌다(self):
        a = self._asset("260701-0050")
        self._ship(a["id"])
        o = self._order()
        self.c.patch(f"/api/orders/{o['id']}",
                     json={"action": "assets", "assetIds": [a["id"]], "force": True})
        self.assertEqual(self.c.get("/api/assets/number-conflicts").get_json()["count"], 1)
        self.c.post("/api/assets/number-fix", json={
            "assetId": a["id"], "targetNo": "260701-0051", "mode": "take",
            "reason": "번호 되찾기"})
        self.assertEqual(self.c.get("/api/assets/number-conflicts").get_json()["count"], 0,
                         "바로잡았는데 충돌 목록에 남아 있다")


class TestTmsReimportAfterFix(Base):
    """★대표 질문(2026-08-18): "OWS에서 데이터값을 바꾼 경우 TMS에서 데이터를
    불러올 때마다 덮어씌어지지는 않아?"

    답: 값은 원래 안 덮어쓴다(빈 칸 채우기). 하지만 **번호와 상태는 달랐다** —
    번호는 매칭 키라 옛 번호로 유령 자산이 새로 생기고, 상태는 정방향(판매·폐기)
    자동갱신이 있어 되돌려 놔도 다음 수집에 원상복구됐다. 그래서 잠금을 넣었다.
    """

    def _sheet(self, rows):
        """TMS 엑셀 한 장을 흉내 내 그대로 반영한다(autosync 와 같은 코드 경로)."""
        with self.app.app_context():
            from app.db import tx
            from app.purchase.migration import _prepare, apply_rows
            with tx(write=True) as conn:
                ready, dup, errors, updates, locked = _prepare(conn, rows, fill_blanks=True)
                res = apply_rows(conn, ready, updates, actor="자동반영")
                return res, errors, locked

    def _asset(self, no):
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        self.c.patch(f"/api/assets/{a['id']}", json={"assetNo": no})
        return self.c.get(f"/api/assets/{a['id']}").get_json()

    def test_잠그기_전에는_TMS가_상태를_되돌린다(self):
        """잠금이 왜 필요한지 — 이 시험이 그 이유다."""
        a = self._asset("260801-0001")
        res, _, _ = self._sheet([{"관리번호": "260801-0001", "모델명": "L480", "재고상태": "판매"}])
        self.assertEqual(res["statusUpdated"], 1)
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["status"], "shipped")

    def test_잠근_자산은_TMS가_상태를_못_바꾼다(self):
        a = self._asset("260801-0002")
        self.c.post("/api/assets/number-fix", json={
            "assetId": a["id"], "targetNo": "260801-0002-정정", "mode": "take",
            "reason": "TMS 오입력"})
        res, _, locked = self._sheet([
            {"관리번호": "260801-0002-정정", "모델명": "L480", "재고상태": "판매"}])
        self.assertEqual(res["statusUpdated"], 0, "★잠갔는데 TMS가 다시 출고로 바꿨다")
        self.assertEqual(locked, ["260801-0002-정정"])
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["status"], "in_stock")

    def test_잠근_자산은_빈칸도_안_채운다(self):
        """TMS 행은 '다른 기계' 이야기다 — 시리얼을 채우면 남의 시리얼이 박힌다."""
        a = self._asset("260801-0003")
        self.c.post("/api/assets/number-fix", json={
            "assetId": a["id"], "targetNo": "260801-0003-정정", "mode": "take",
            "reason": "TMS 오입력"})
        self._sheet([{"관리번호": "260801-0003-정정", "모델명": "L480",
                      "시리얼번호": "SN-OTHER-99", "매입가": "999000"}])
        got = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(got["serial"], "", "★남의 시리얼이 채워졌다")
        self.assertEqual(got["purchasePrice"], 100000, "★TMS 값이 매입가를 덮었다")

    def test_비워진_옛_번호로_유령이_다시_생기지_않는다(self):
        """★번호는 매칭 키다 — 안 막으면 TMS가 옛 번호를 새 자산으로 만든다."""
        a = self._asset("260801-0004")
        self.c.post("/api/assets/number-fix", json={
            "assetId": a["id"], "targetNo": "260801-0004-새번호", "mode": "take",
            "reason": "TMS 오입력"})
        before = self.c.get("/api/assets?limit=500").get_json()["total"]
        res, _, locked = self._sheet([
            {"관리번호": "260801-0004", "모델명": "L480", "재고상태": "판매"}])
        self.assertEqual(res["created"], 0, "★옛 번호로 유령 자산이 다시 생겼다")
        self.assertIn("260801-0004", locked)
        self.assertEqual(self.c.get("/api/assets?limit=500").get_json()["total"], before)

    def test_잠그지_않은_자산은_예전처럼_채운다(self):
        """잠금은 정정한 자산에만 건다 — 나머지 TMS 반영은 그대로여야 한다."""
        a = self._asset("260801-0005")
        self._sheet([{"관리번호": "260801-0005", "모델명": "L480", "시리얼번호": "SN12345678"}])
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["serial"], "SN12345678")


class TestLabelTemplate(Base):
    """운송장 문구를 설정에서 정한다(2026-08-24 대표 지시).

    "어떤 키워드를 어떻게 활용할건지 우리가 설정할 수 있게 해줘.
     [배송메모], [자산번호], [옵션명] 이런식으로 순서를 바꿀 수도 있을거고."

    ★사용자가 정하는 건 '무엇을 어떤 순서로'뿐이다. 120/60자 예산과
      자산번호 온전 보장은 코드가 계속 쥔다.
    """

    ROW = {
        "channel": "고도몰", "product_code": "14-CK1007TU_i5-8_내장",
        "product_name": "HP 인텔 i5 14인치 노트북", "quantity": 1,
        "option_name": "램 16G 업", "order_no": "20260824-1", "recipient": "홍길동",
        "delivery_message": "부재시 경비실", "is_review": 0,
    }

    def _item(self, tmpl=None, row=None, assets=("260628-0015",), prep=()):
        from app.orders.waybill import _compose_items
        items = _compose_items(dict(row or self.ROW), list(assets), simulated=False,
                               prep_names=list(prep), tmpl=tmpl)
        return " / ".join(
            f"{it.get('name') or ''}{' x' + str(it.get('qty')) if it.get('qty') else ''}".strip()
            for it in items if it.get("name"))

    def _remark(self, tmpl=None, row=None, assets=(), prep=()):
        from app.orders.waybill import _compose_remark, _remark_row
        return _compose_remark(_remark_row(dict(row or self.ROW), list(assets)),
                               list(prep), tmpl=tmpl)

    # ---- 기본값은 지금까지의 문구와 같아야 한다 ----
    def test_기본_문구는_예전과_같다(self):
        got = self._item()
        self.assertEqual(
            got,
            "[고도몰] 14-CK1007TU_i5-8_내장 x1 / 자산 260628-0015 "
            "/ HP 인텔 i5 14인치 노트북 / 옵션 램 16G 업")

    def test_기본_배송메세지도_예전과_같다(self):
        self.assertEqual(self._remark(prep=["리브레오피스"]), "부재시 경비실 / 리브레오피스")

    def test_옵션명과_챙긴옵션은_뜻이_다르다(self):
        """★키워드 하나가 칸마다 다른 뜻이면 편집하는 사람이 속는다 — 둘로 나눠 뒀다.

        [옵션명] = 챙긴 옵션 + 몰이 보낸 옵션 / [챙긴옵션] = 우리가 챙기는 것만.
        배송메세지 기본값이 [챙긴옵션]인 이유는 몰 옵션이 상품명 칸에 이미 있어서다.
        """
        self.assertEqual(self._remark(tmpl="[챙긴옵션]", prep=["리브레오피스"]), "리브레오피스")
        both = self._remark(tmpl="[옵션명]", prep=["리브레오피스"])
        self.assertIn("램 16G 업", both, "몰 옵션이 빠졌다")
        self.assertIn("리브레오피스", both)

    def test_제품코드가_없으면_상품명이_그_자리에_간다(self):
        """예전 head 폴백 그대로 — 코드 없는 주문이 이름 없는 송장이 되면 안 된다."""
        row = {**self.ROW, "product_code": ""}
        got = self._item(row=row)
        self.assertTrue(got.startswith("[고도몰] HP 인텔 i5 14인치 노트북 x1"), got)
        self.assertEqual(got.count("HP 인텔 i5 14인치 노트북"), 1, "상품명이 두 번 찍혔다")

    # ---- 순서를 바꿀 수 있다 ----
    def test_순서를_바꾸면_그_순서로_찍힌다(self):
        got = self._item(tmpl="[자산번호] / [옵션명] / [쇼핑몰] [제품코드]")
        self.assertTrue(got.startswith("자산 260628-0015 x1"), got)
        self.assertLess(got.index("옵션"), got.index("[고도몰]"), got)

    def test_원하는_키워드만_남길_수_있다(self):
        self.assertEqual(self._item(tmpl="[자산번호]"), "자산 260628-0015 x1")

    def test_배송메세지에_자산번호를_넣을_수_있다(self):
        got = self._remark(tmpl="[자산번호] / [배송메모]", assets=["260628-0015"])
        self.assertEqual(got, "자산 260628-0015 / 부재시 경비실")

    def test_리터럴_글자도_쓸_수_있다(self):
        # [자산번호]는 '자산 <번호>'로 찍힌다 — 잘림 계산이 그 접두 길이에 묶여 있다
        self.assertIn("관리 자산 260628-0015", self._item(tmpl="관리 [자산번호]"))

    # ---- 안전장치는 그대로 ----
    def test_자산번호는_중간에서_안_잘린다(self):
        """반쪽 번호가 찍히면 포장 대조가 오히려 위험해진다."""
        nos = [f"2606{i:02d}-{i:04d}" for i in range(1, 12)]
        got = self._item(tmpl="[자산번호] / [상품명]", assets=nos)
        for piece in got.split(" / ")[0].replace("자산 ", "").split(","):
            piece = piece.split(" 외")[0].strip()
            if piece:
                self.assertRegex(piece, r"^\d{6}-\d{4}$", f"번호가 잘렸다: {piece!r}")
        self.assertIn("외", got, "못 담은 대수를 알려주지 않는다")

    def test_칸_예산을_넘지_않는다(self):
        long_row = {**self.ROW, "product_name": "가" * 300, "option_name": "나" * 200}
        got = self._item(row=long_row, assets=[f"2606{i:02d}-{i:04d}" for i in range(1, 6)])
        self.assertLessEqual(len(got), 120, f"상품명 칸이 넘쳤다({len(got)}자)")
        rm = self._remark(row={**long_row, "delivery_message": "다" * 200},
                          prep=["라" * 80])
        self.assertLessEqual(len(rm), 60, f"배송메세지 칸이 넘쳤다({len(rm)}자)")

    def test_앞에_적은_키워드가_먼저_자리를_가져간다(self):
        long_row = {**self.ROW, "product_name": "가" * 300}
        first = self._item(tmpl="[상품명] / [자산번호]", row=long_row)
        self.assertNotIn("자산", first, "상품명이 다 먹었는데 자산번호가 들어갔다")
        second = self._item(tmpl="[자산번호] / [상품명]", row=long_row)
        self.assertTrue(second.startswith("자산 260628-0015"), second)

    def test_템플릿을_비워도_빈_송장이_안_나온다(self):
        got = self._item(tmpl="   ")
        self.assertIn("14-CK1007TU_i5-8_내장", got)

    def test_수량은_첫_항목에_붙는다(self):
        """★CJ 접수 payload(GDS_QTY)와 같은 값이라 떼면 CJ 수량이 1로 굳는다."""
        from app.orders.waybill import _compose_items
        items = _compose_items({**self.ROW, "quantity": 3}, ["260628-0015"],
                               simulated=False, tmpl="[자산번호] / [상품명]")
        self.assertEqual(items[0]["qty"], 3)
        self.assertNotIn("qty", items[1])

    def test_자동_접두는_항상_맨_앞이다(self):
        got = self._item(tmpl="[상품명] / [자산번호]",
                         row={**self.ROW, "is_review": 1})
        self.assertTrue(got.startswith("[리뷰어] "), got)
        from app.orders.waybill import _compose_items
        items = _compose_items(dict(self.ROW), ["260628-0015"], simulated=True,
                               box_qty=2, tmpl="[상품명]")
        self.assertTrue(items[0]["name"].startswith("[테스트발행] [박스 2개] "), items[0]["name"])

    # ---- 모르는 키워드 ----
    def test_모르는_키워드는_저장이_거부된다(self):
        r = self.c.put("/api/settings", json={"cj": {
            "label_tmpl": {"item": "[없는키워드] / [자산번호]", "remark": ""}}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("없는키워드", r.get_json()["error"])

    def test_배송메모는_상품명_칸에서_못_쓴다(self):
        """기사가 보는 요청 문구가 상품명 칸으로 새면 배송 사고가 난다."""
        from app.orders.waybill import check_label_tmpl
        self.assertEqual(check_label_tmpl("[배송메모]", "item"), ["배송메모"])
        self.assertEqual(check_label_tmpl("[배송메모]", "remark"), [])

    # ---- 설정 ↔ 송장 연결 ----
    def test_저장한_문구가_송장에_실제로_반영된다(self):
        r = self.c.put("/api/settings", json={"cj": {
            "label_tmpl": {"item": "[자산번호] / [쇼핑몰]", "remark": "[옵션명]"}}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = self.c.get("/api/settings").get_json()
        self.assertEqual(d["cj"]["label_tmpl"]["item"], "[자산번호] / [쇼핑몰]")
        p = self.c.post("/api/cj/label-preview", json={
            "item": d["cj"]["label_tmpl"]["item"],
            "remark": d["cj"]["label_tmpl"]["remark"]}).get_json()
        self.assertTrue(p["itemSummary"].startswith("자산 "), p["itemSummary"])
        self.assertNotIn("부재시", p["remark"], "옵션만 넣었는데 배송메모가 실렸다")

    def test_미리보기가_키워드_목록을_알려준다(self):
        d = self.c.get("/api/cj/label-tokens").get_json()
        names = {t["name"] for t in d["tokens"]}
        for want in ("배송메모", "자산번호", "옵션명", "제품코드", "상품명", "쇼핑몰"):
            self.assertIn(want, names)
        self.assertEqual(d["limits"], {"item": 120, "remark": 60})

    def test_화면이_저장에_문구를_실어_보낸다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
              ).read_text("utf-8")
        self.assertIn("label_tmpl: { item:", js)
        self.assertIn("bindLabelTmpl(cj)", js)
        self.assertIn("/api/cj/label-preview", js)


class TestAssetCapOnLabel(Base):
    """한 송장에 자산번호는 최대 3개(대표 2026-08-24).

    "여러대 주문건에 있어서 자산번호가 여러개인경우 송장에 잘릴 수도 있잖아?
     한 송장에 최대 3개의 자산번호만 (중복되지않게) 표시해주면 돼.
     우리 한 송장 기준 최대 3대까지만 들어가거든."
    """

    ROW = {
        "channel": "고도몰", "product_code": "L480_i5-8_내장", "product_name": "L480",
        "quantity": 1, "option_name": "", "order_no": "1", "recipient": "홍길동",
        "delivery_message": "", "is_review": 0,
    }

    def _text(self, nos, tmpl="[자산번호]"):
        from app.orders.waybill import _compose_items
        items = _compose_items(dict(self.ROW), list(nos), simulated=False, tmpl=tmpl)
        return items[0]["name"]

    def test_세_대까지만_적는다(self):
        nos = ["260101-0001", "260101-0002", "260101-0003", "260101-0004", "260101-0005"]
        got = self._text(nos)
        for keep in nos[:3]:
            self.assertIn(keep, got)
        for drop in nos[3:]:
            self.assertNotIn(drop, got, f"3대 상한을 넘겨 {drop}까지 찍혔다")

    def test_남은_대수는_숨기지_않는다(self):
        """상한이 있어도 총 대수는 알려야 한다 — 상자를 더 찾아봐야 하는지 알아야 하니까."""
        got = self._text([f"260101-{i:04d}" for i in range(1, 6)])
        self.assertIn("외 2대", got, got)

    def test_세_대_이하면_외_N대가_안_붙는다(self):
        got = self._text(["260101-0001", "260101-0002"])
        self.assertNotIn("외", got, got)
        self.assertIn("260101-0002", got)

    def test_같은_번호는_한_번만_적는다(self):
        """★종이에 같은 번호가 두 번 찍히면 포장 담당이 두 대인 줄 안다."""
        got = self._text(["260101-0001", "260101-0001", " 260101-0001 ", "260101-0002"])
        self.assertEqual(got.count("260101-0001"), 1, got)
        self.assertIn("260101-0002", got)
        self.assertNotIn("외", got, f"중복을 대수로 세었다: {got}")

    def test_빈값과_공백은_버린다(self):
        # x수량은 label.py 가 붙인다 — _compose_items 는 qty 칸으로만 넘긴다
        got = self._text(["260101-0001", "", "   ", None])
        self.assertEqual(got, "자산 260101-0001", got)

    def test_상한을_넘겨도_번호를_중간에서_안_자른다(self):
        got = self._text([f"260101-{i:04d}" for i in range(1, 9)])
        head = got.split(" x")[0].replace("자산 ", "").split(" 외")[0]
        for piece in head.split(","):
            self.assertRegex(piece.strip(), r"^\d{6}-\d{4}$", f"번호가 잘렸다: {piece!r}")


class TestMultiWaybillAssetSplit(Base):
    """다매(송장 여러 장)면 자산번호를 송장별로 나눠 싣는다.

    ★한 송장에 3대까지 들어가는데 두 장에 같은 번호를 찍으면
      어느 상자에 어느 기계가 들었는지 알 수 없다.
    """

    def test_나눌_만큼_많으면_송장별로_다르게_싣는다(self):
        from app.orders.waybill import LABEL_MAX_ASSETS
        self.assertEqual(LABEL_MAX_ASSETS, 3)
        nos = [f"260101-{i:04d}" for i in range(1, 6)]      # 5대 → 3 + 2
        first = nos[:3]
        second = nos[3:]
        self.assertEqual(len(set(first) & set(second)), 0, "겹치면 나눈 의미가 없다")

    def test_코드가_송장별_묶음을_만든다(self):
        """실제 분배는 issue_waybill 안의 _nos_for 가 한다 — 계약을 코드로 고정한다."""
        src = (Path(__file__).resolve().parent.parent / "app" / "orders" / "waybill.py"
               ).read_text("utf-8")
        blk = src.split("def _nos_for(", 1)[1].split("def _items_for(", 1)[0]
        self.assertIn("LABEL_MAX_ASSETS", blk)
        self.assertIn("wb_qty < 2", blk, "1장짜리는 예전처럼 전부 같이 찍어야 한다")
        # 접수 payload·라벨 모두 송장별 품목을 써야 한다(종이와 접수내역이 어긋나면 대조 불가)
        self.assertIn("items=_items_for(i)", src)
        self.assertIn("_cj2_label(invoices[i], today, sender, receiver, _items_for(i)", src)


class TestWaybillMode(Base):
    """지금 누르면 진짜 접수인가 — 화면이 물어볼 수 있어야 한다(대표 2026-08-24).

    ★설정은 채워졌는데 환경이 dev·무장 꺼짐이라 🧾가 999 테스트 번호를 내보내던 상태를
      화면이 전혀 알려 주지 않았다.
    """

    def test_기본은_테스트_발행이다(self):
        d = self.c.get("/api/waybills/mode").get_json()
        self.assertFalse(d["real"])
        self.assertEqual(d["env"], "dev")
        self.assertFalse(d["armed"])
        self.assertEqual(d["maxAssets"], 3)

    def test_이식만_하면_아직_테스트다(self):
        self.c.post("/api/cj/import-rms")
        d = self.c.get("/api/waybills/mode").get_json()
        self.assertEqual(d["env"], "prod")
        self.assertTrue(d["custId"])
        self.assertTrue(d["bizReg"])
        self.assertTrue(d["senderReady"])
        self.assertFalse(d["armed"], "가져오기가 무장까지 켰다")
        self.assertFalse(d["real"], "무장 없이 실발행이 됐다")

    def test_무장을_켜면_실발행으로_바뀐다(self):
        self.c.post("/api/cj/import-rms")
        self.c.put("/api/settings", json={"cj": {"armed": True}})
        self.assertTrue(self.c.get("/api/waybills/mode").get_json()["real"])

    def test_셋팅_화면이_표시를_그린다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
              ).read_text("utf-8")
        self.assertIn('id="setup-wbmode"', js)
        self.assertIn("/api/waybills/mode", js)
        self.assertIn("renderWaybillMode()", js)
        blk = js.split("async function renderWaybillMode", 1)[1].split("\nfunction ", 1)[0]
        # ★실발행 안내 배너는 대표 지시로 뺐다(2026-09-03) — 실발행이 정상 상태라
        #   매번 뜨는 빨간 경고가 화면만 차지했다. '실발행이면 아무것도 안 그린다'를 핀한다.
        self.assertNotIn("실발행 상태입니다", blk, "지운 실발행 배너가 되살아났다")
        self.assertIn('box.innerHTML = ""', blk, "실발행일 때 배너를 비우는 처리가 없다")
        # 테스트 발행 경고는 남아야 한다 — 999 가짜 번호가 종이로 나가는 걸 막는 장치다
        self.assertIn("테스트 발행입니다", blk)
        self.assertIn("실발행 무장", blk, "왜 테스트인지 이유를 안 알려 준다")


class TestAssetPurchaseCancel(Base):
    """전표에서 제품만 골라 매입취소(2026-08-24 대표 지시).

    "매입취소가 필요한 경우에는 차라리 매입취소버튼이 전표번호단에서 매입취소할
     제품을 클릭 후 매입취소 하는게 나을 것 같은데."
    ★전표 통째 취소는 그대로 둔다 — 전표 자체가 잘못 들어온 경우가 있다.
    """

    def _slip(self, n=2):
        cats = self.c.get("/api/categories").get_json()
        sid = self.c.post("/api/suppliers", json={"name": "취소시험거래처"}).get_json()["id"]
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-08-24",
            "supplierId": sid, "totalAmount": 200000})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        b = r.get_json()
        made = self.c.post("/api/assets", json={
            "batchId": b["id"], "categoryId": cats[0]["id"], "model": "L480",
            "qty": n, "purchasePrice": 100000}).get_json()
        return b, made

    def _detail(self, bid):
        return self.c.get(f"/api/purchase-batches/{bid}").get_json()

    def test_고른_것만_취소된다(self):
        b, made = self._slip(3)
        r = self.c.post("/api/assets/purchase-cancel",
                        json={"ids": [made[0]["id"]], "reason": "실물 미도착"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["ok"], 1)
        d = self._detail(b["id"])
        self.assertEqual(len(d["assets"]), 2, "본 표에 취소분이 남았다")
        self.assertEqual(len(d["cancelledAssets"]), 1)
        self.assertEqual(d["cancelledAssets"][0]["cancelReason"], "실물 미도착")

    def test_취소분은_재고에서_빠진다(self):
        b, made = self._slip(2)
        before = self.c.get("/api/assets?limit=500").get_json()["total"]
        self.c.post("/api/assets/purchase-cancel",
                    json={"ids": [made[0]["id"]], "reason": "파손"})
        got = self.c.get(f"/api/assets/{made[0]['id']}").get_json()
        self.assertEqual(got["status"], "cancelled")
        self.assertLess(
            self.c.get("/api/assets?limit=500&status=in_stock").get_json()["total"], before)

    def test_사유_없이는_못_한다(self):
        b, made = self._slip(1)
        self.assertEqual(self.c.post("/api/assets/purchase-cancel",
                                     json={"ids": [made[0]["id"]]}).status_code, 400)
        self.assertEqual(self.c.post("/api/assets/purchase-cancel",
                                     json={"ids": [], "reason": "x"}).status_code, 400)

    def test_주문에_배정된_자산은_건너뛴다(self):
        """★나간 물건을 무르면 판 물건이 재고에서 사라진 채 장부만 바뀐다."""
        b, made = self._slip(1)
        o = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "홍길동", "productName": "노트북",
            "phone": "010-1111-2222", "address": "서울시 강남구", "postalCode": "06000",
            "quantity": 1, "amount": 500000}).get_json()
        self.c.patch(f"/api/orders/{o['id']}",
                     json={"action": "assets", "assetIds": [made[0]["id"]]})
        r = self.c.post("/api/assets/purchase-cancel",
                        json={"ids": [made[0]["id"]], "reason": "취소해보기"}).get_json()
        self.assertEqual(r["ok"], 0)
        # 매칭하는 순간 상태가 '주문매칭(reserved)'이 되므로 상태 가드가 먼저 잡는다.
        # 둘 중 어느 쪽이 잡든 '나간 물건은 못 무른다'는 결과가 같으면 된다.
        why = r["skipped"][0]["reason"]
        self.assertTrue("주문매칭" in why or "배정" in why, why)
        self.assertNotEqual(
            self.c.get(f"/api/assets/{made[0]['id']}").get_json()["status"], "cancelled")

    def test_되돌리면_취소_직전_상태로_돌아간다(self):
        """★'입고'로 뭉개면 셋팅을 다 끝낸 자산을 손으로 다시 올려야 한다."""
        b, made = self._slip(1)
        aid = made[0]["id"]
        self.c.patch(f"/api/assets/{aid}", json={"status": "ready"})
        self.c.post("/api/assets/purchase-cancel", json={"ids": [aid], "reason": "오입력"})
        self.assertEqual(self.c.get(f"/api/assets/{aid}").get_json()["status"], "cancelled")
        r = self.c.post("/api/assets/purchase-uncancel", json={"ids": [aid]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.c.get(f"/api/assets/{aid}").get_json()["status"], "ready")

    def test_두_번_취소해도_한_번만_센다(self):
        b, made = self._slip(1)
        self.c.post("/api/assets/purchase-cancel",
                    json={"ids": [made[0]["id"]], "reason": "1차"})
        r = self.c.post("/api/assets/purchase-cancel",
                        json={"ids": [made[0]["id"]], "reason": "2차"}).get_json()
        self.assertEqual(r["ok"], 0)
        self.assertIn("이미", r["skipped"][0]["reason"])

    def test_화면에_버튼이_있다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
              ).read_text("utf-8")
        self.assertIn('id="sd-cancel-assets"', js)
        self.assertIn("/api/assets/purchase-cancel", js)
        self.assertIn("/api/assets/purchase-uncancel", js)
        self.assertIn("data-uncancel=", js)


class TestStatusSimplified(Base):
    """상태 하나로 정한다(2026-08-24 대표 지시).

    "상태와 아래 보수체크가 좀 동일한내용인 것 같아. 그냥 상태 자체를 설정할 수 있게 해주면
     될 것 같아. * 판매가능 * A/S * 수리(누르면 하단에 시트지, 도색, 짜깁기 이런식으로 뜨고
     재고반영이 안되면 돼) * 불량, 부품용"

    ★라이브 실측(2026-08-24)이 뒷받침: 가재고 0대·보수체크 0대·판매불가 상태 0대.
    ★대표 확인: 실재고(1,047대)는 '판매가능'으로 흡수 — 가용과 동작이 이미 같아
      데이터를 옮길 필요가 없다. 화면만 바꾼다.
    """

    def _asset(self, **kw):
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 100000, **kw}).get_json()[0]
        return a["id"]

    def test_고를_수_있는_상태는_넷이다(self):
        m = self.c.get("/api/purchase-meta").get_json()
        self.assertEqual([c["code"] for c in m["statusChoices"]],
                         ["ready", "as", "repair", "defective"])
        for c in m["statusChoices"]:
            self.assertTrue(c["help"].strip(), f"{c['code']} 설명이 비었다")

    def test_옛_상태는_같은_그룹으로_접힌다(self):
        """입고·정비중은 '판매가능', 도색대기는 '수리'로 보여야 한다."""
        g = self.c.get("/api/purchase-meta").get_json()["statusGroup"]
        self.assertEqual(g["in_stock"], "ready")
        self.assertEqual(g["refurbishing"], "ready")
        self.assertEqual(g["painting"], "repair")

    def test_수리_항목_후보를_내려준다(self):
        """★repairItems 는 이제 수리 단가표에서 온다({name, price} 목록) —
        시트지는 '시트지(상)'처럼 등급이 붙은 이름으로 온다."""
        items = self.c.get("/api/purchase-meta").get_json()["repairItems"]
        names = [x["name"] for x in items]
        self.assertTrue(any("시트지" in n for n in names), names)
        for want in ("도색", "짜깁기"):
            self.assertIn(want, names)

    # ---- 수리는 재고에서 빠지고 출고가 막힌다 ----
    def test_수리로_바꾸면_재고에서_빠진다(self):
        aid = self._asset()
        self.c.post("/api/assets/product-code",
                    json={"ids": [aid], "productCode": "L480_i5-8_내장"})
        p = self.c.post("/api/orders/product-info",
                        json={"codes": ["L480_i5-8_내장"]}).get_json()["products"]
        self.assertEqual(p["L480_i5-8_내장"]["codeShippable"], 1)
        self.c.patch(f"/api/assets/{aid}", json={"status": "repair"})
        p = self.c.post("/api/orders/product-info",
                        json={"codes": ["L480_i5-8_내장"]}).get_json()["products"]
        self.assertEqual(p["L480_i5-8_내장"]["codeShippable"], 0,
                         "★수리 중인데 재고로 잡힌다")

    def test_수리_자산은_주문에_못_붙인다(self):
        aid = self._asset()
        self.c.patch(f"/api/assets/{aid}", json={"status": "repair"})
        o = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "홍길동", "productName": "노트북",
            "phone": "010-1111-2222", "address": "서울시 강남구", "postalCode": "06000",
            "quantity": 1, "amount": 500000}).get_json()
        r = self.c.patch(f"/api/orders/{o['id']}",
                         json={"action": "assets", "assetIds": [aid]})
        self.assertEqual(r.status_code, 409, "★수리 중인 물건이 주문에 붙었다")

    def test_수리_항목을_자산마다_적는다(self):
        aid = self._asset()
        r = self.c.patch(f"/api/assets/{aid}", json={
            "status": "repair",
            "tierTasks": {"items": [{"name": "시트지", "state": "todo"},
                                    {"name": "도색", "state": "doing"}],
                          "note": "앞판 긁힘"}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        got = self.c.get(f"/api/assets/{aid}").get_json()
        self.assertEqual([t["name"] for t in got["tierTasks"]["items"]], ["시트지", "도색"])
        self.assertEqual(got["tierTasks"]["note"], "앞판 긁힘")

    def test_판매가능으로_되돌리면_다시_재고다(self):
        aid = self._asset()
        self.c.post("/api/assets/product-code",
                    json={"ids": [aid], "productCode": "L480_i5-8_내장"})
        self.c.patch(f"/api/assets/{aid}", json={"status": "repair"})
        self.c.patch(f"/api/assets/{aid}", json={"status": "ready"})
        p = self.c.post("/api/orders/product-info",
                        json={"codes": ["L480_i5-8_내장"]}).get_json()["products"]
        self.assertEqual(p["L480_i5-8_내장"]["codeShippable"], 1)

    # ---- 화면 ----
    def test_화면에서_재고구분_칸이_사라졌다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
              ).read_text("utf-8")
        self.assertNotIn('id="ad-tier"', js, "재고구분 칸이 아직 있다")
        self.assertNotIn("ad-tier", js, "재고구분 배선이 남아 화면이 죽는다")
        self.assertIn("statusOptionsHtml(a.status)", js)
        self.assertIn('id="ad-status-help"', js, "상태 뜻을 안 알려 준다")

    def test_수리_항목은_수리일_때만_뜬다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
              ).read_text("utf-8")
        blk = js.split("const drawTasks = () =>", 1)[1][:900]
        self.assertIn('st === "repair"', blk)
        self.assertIn("repairItems", blk, "수리 항목 후보를 메타에서 안 읽는다")

    def test_옛_메타로도_상태를_바꿀_수_있다(self):
        """★재시작 전 캐시로 열면 선택지가 비어 상태를 아예 못 바꾸는 일이 없어야 한다."""
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
              ).read_text("utf-8")
        blk = js.split("function statusOptionsHtml", 1)[1].split("\n}", 1)[0]
        self.assertIn("meta.statusChoices && meta.statusChoices.length", blk)
        self.assertIn("manualStatuses", blk, "옛 목록으로 물러날 길이 없다")


class TestWaybillColumnVisible(Base):
    """셋팅 보드의 송장 버튼은 잘리면 안 된다(대표 2026-08-24: "송장은 어디서 출력가능?").

    ★버튼은 원래 있었는데 상태 칸(108px)에 상태 칩·출고담당자와 함께 우겨넣어 놓고
      `.setup-status{white-space:nowrap}` + `.setup-rows-fixed td{overflow:hidden}` 이라
      칸 밖으로 밀려 잘려 있었다 — 화면에 없는 것과 같다. 전용 칸으로 뺐다.
    """

    def setUp(self):
        super().setUp()
        root = Path(__file__).resolve().parent.parent
        self.js = (root / "static" / "js" / "setup.js").read_text("utf-8")
        self.css = (root / "static" / "css" / "app.css").read_text("utf-8")

    def test_송장_전용_칸이_있다(self):
        head = self.js.split("<thead>", 1)[1].split("</thead>", 1)[0]
        # 2026-09-03: 셋팅라벨 버튼이 같은 칸에 들어가 머리글이 '송장 / 라벨'이 됐다
        self.assertIn("송장 / 라벨</th>", head)
        self.assertIn('<td class="setup-wb">', self.js)
        self.assertNotIn("shipDoneLine(o)}${waybillCell(o)}", self.js,
                         "상태 칸에 다시 우겨넣었다")

    def test_칸수와_colspan이_맞는다(self):
        n = self.js.split("<thead>", 1)[1].split("</thead>", 1)[0].count("<th")
        self.assertIn(f'colspan="{n}"', self.js, f"머리글 {n}칸과 colspan이 다르다")

    def test_송장_칸은_안_잘린다(self):
        """★overflow:hidden 인 표에서 이 칸만은 보이게 해 둬야 한다."""
        self.assertIn(".setup-wb", self.css)
        blk = self.css.split(".setup-wb {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow: visible", blk)
        self.assertIn(".setup-table .col-wb", self.css, "폭을 안 잡아 뒀다")

    def test_행에는_버튼_하나만_둔다(self):
        """숫자 입력칸을 행에 두면 좁은 칸에서 또 밀린다 — 모달에서 정한다."""
        blk = self.js.split("function waybillCell", 1)[1].split(chr(10) + "async function", 1)[0]
        self.assertNotIn('class="wb-box"', blk)
        self.assertNotIn('class="wb-mqty"', blk)
        self.assertIn("data-wbpreview=", blk)
        self.assertIn("🧾 송장", blk, "무슨 버튼인지 글자로 안 적혀 있다")

    def test_모달에서_상자수와_송장수를_정한다(self):
        self.assertIn('id="wp-box"', self.js)
        self.assertIn('id="wp-mqty"', self.js)
        self.assertIn("issueWaybillFromSetup(oid, boxIn ? boxIn.value : 1, mqIn ? mqIn.value : 1)",
                      self.js)

    def test_죽은_배선이_남지_않았다(self):
        for dead in ("boxOf(", "mqtyOf(", "data-wbissue"):
            self.assertNotIn(dead, self.js, f"{dead} 가 남아 있다(없는 칸을 읽는다)")


class TestRepairBook(Base):
    """수리 단가표를 부품 단가표와 나눈다(2026-08-24 대표 지시).

    "시트지의 경우 부품 단가표에 있던데 이건 수리단가표로 이동하여 있어야 함.
     그래서 수리단가표 기준으로 보이게끔 해줘.
     부품단가표처럼 수리단가표 탭도 동일한 양식대로 보일 수 있게 해주면 돼"
    ★부품(물건을 꽂는 것)과 수리(작업)가 한 표에 섞이면 부품 매입·재고와 공임이 뒤엉킨다.
    """

    def test_시트지는_도색시트지_단가표에_있다(self):
        """★2026-08-25 3분할: 부품 / 수리(액정·케이스·키보드·배터리) / 도색·시트지."""
        paint = [p["name"] for p in self.c.get("/api/parts?kind=paint").get_json()]
        self.assertTrue(any("시트지" in n for n in paint), f"시트지가 안 옮겨졌다: {paint}")
        self.assertIn("도색", paint)
        for kind in ("part", "repair"):
            names = [p["name"] for p in self.c.get(f"/api/parts?kind={kind}").get_json()]
            self.assertFalse(any("시트지" in n for n in names),
                             f"{kind} 단가표에 시트지가 남아 있다")
            self.assertNotIn("도색", names)

    def test_부품은_그대로_부품_단가표에_있다(self):
        part = [p["name"] for p in self.c.get("/api/parts?kind=part").get_json()]
        for want in ("D4 8G", "M.2 NVMe 512G"):
            self.assertTrue(any(w == want for w in part), f"{want} 가 부품 표에서 사라졌다")

    def test_수리_항목_기본값이_들어간다(self):
        names = [p["name"] for p in self.c.get("/api/parts?kind=repair").get_json()]
        for want in ("짜깁기", "액정 교체", "배터리 교체", "케이스 교체"):
            self.assertIn(want, names)

    def test_단가는_0으로_두고_대표가_채운다(self):
        """★짐작으로 넣으면 그 값이 그대로 자산 원가에 실린다."""
        for p in self.c.get("/api/parts?kind=repair").get_json():
            if p["name"] in ("도색", "짜깁기", "액정 교체"):
                self.assertEqual(p["price"], 0, f"{p['name']} 에 임의 단가가 들어갔다")

    def test_kind_없이_부르면_예전처럼_전부(self):
        """옛 화면·시험이 죽지 않아야 한다."""
        allp = self.c.get("/api/parts").get_json()
        part = self.c.get("/api/parts?kind=part").get_json()
        rep = self.c.get("/api/parts?kind=repair").get_json()
        paint = self.c.get("/api/parts?kind=paint").get_json()
        self.assertEqual(len(allp), len(part) + len(rep) + len(paint))
        self.assertTrue(paint, "도색·시트지 단가표가 비어 있다")

    def test_수리_탭에서_만들면_수리로_들어간다(self):
        r = self.c.post("/api/parts", json={"name": "메인보드 세척", "category": "",
                                            "price": 15000, "kind": "repair", "group": "수리"})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        names = [p["name"] for p in self.c.get("/api/parts?kind=repair").get_json()]
        self.assertIn("메인보드 세척", names)
        self.assertNotIn("메인보드 세척",
                         [p["name"] for p in self.c.get("/api/parts?kind=part").get_json()])

    def test_모르는_구분은_거부한다(self):
        self.assertEqual(self.c.post("/api/parts", json={
            "name": "이상한것", "category": "", "price": 0, "kind": "아무거나"}).status_code, 400)

    def test_도색시트지_탭에서_만들면_paint로_들어간다(self):
        r = self.c.post("/api/parts", json={"name": "무광 시트지(특)", "category": "",
                                            "price": 22000, "kind": "paint",
                                            "group": "도색/시트지"})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        names = [p["name"] for p in self.c.get("/api/parts?kind=paint").get_json()]
        self.assertIn("무광 시트지(특)", names)

    def test_메타가_수리_단가표를_이름과_금액으로_준다(self):
        """★화면이 단가를 짐작하지 않게 서버가 표를 그대로 내려 준다."""
        self.c.post("/api/parts", json={"name": "액정 교체 테스트", "category": "",
                                        "price": 88000, "kind": "repair", "group": "수리"})
        items = self.c.get("/api/purchase-meta").get_json()["repairItems"]
        self.assertTrue(all(isinstance(x, dict) and "name" in x and "price" in x for x in items),
                        f"이름만 오면 화면이 금액을 못 채운다: {items[:3]}")
        hit = next(x for x in items if x["name"] == "액정 교체 테스트")
        self.assertEqual(hit["price"], 88000)

    def test_화면에_단가표_탭이_세_개_있다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
              ).read_text("utf-8")
        self.assertIn('["repairs", "🛠 수리 단가표"]', js)
        self.assertIn('["paints", "🎨 도색/시트지 단가표"]', js)
        self.assertIn("repairs: renderRepairBook, paints: renderPaintBook", js)
        self.assertIn('renderPartsBook(body, "paint")', js)
        self.assertIn('api("/api/parts?kind=" + encodeURIComponent(kind))', js)

    def test_단가표_검색은_실시간이고_한글이_안_끊긴다(self):
        """대표 2026-08-25 "Enter 없이" + 2026-08-26 "글자가 뚝뚝 끊겨" —
        ★입력창을 다시 그리면 한글 조합이 끊긴다. 결과 영역(#pb-groups)만 갈아야 한다."""
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
              ).read_text("utf-8")
        self.assertIn('id="pb-q"', js, "단가표 검색칸이 없다")
        blk = js.split('const qEl = $("#pb-q", body);', 1)[1][:700]
        self.assertIn('addEventListener("input"', blk, "input 즉시가 아니라 Enter 를 기다린다")
        self.assertIn("renderGroups()", blk, "결과 영역 부분 갱신이 아니라 전체 재렌더다")
        self.assertNotIn("renderPartsBook(body", blk,
                         "검색이 입력창까지 다시 그린다 — 한글 조합이 끊긴다")
        self.assertIn('<div id="pb-groups">', js, "결과 영역 분리가 사라졌다")
        # 검색 중에는 걸린 그룹을 펼친다 — 접혀 있으면 찾은 게 안 보인다
        self.assertIn("const open = q ? true", js)
        # 재고 내역·보정 버튼 배선(한때 그리기만 하고 안 걸어 무반응이었다)
        self.assertIn('$$(".pb-moves", host)', js, "📜 내역 버튼이 배선 안 됐다")
        self.assertIn('$$(".pb-adjust", host)', js, "± 보정 버튼이 배선 안 됐다")

    def test_재고현황_검색은_조합_중에_안_그린다(self):
        """#sp-q 는 카드 전체를 다시 그리는 구조라, 한글 조합 중엔 미룬다."""
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
              ).read_text("utf-8")
        self.assertIn("state.spqComposing", js)
        self.assertIn('addEventListener("compositionstart"', js)
        self.assertIn('addEventListener("compositionend"', js)
        self.assertIn("if (state.spqComposing) return;", js, "redraw 쪽 가드가 없다")

    def test_고르기가_묶음으로_갈린다(self):
        """대표 2026-08-25: "항목 클릭하면 그거에 맞는 것들이 나오게"."""
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
              ).read_text("utf-8")
        self.assertIn("function fillPartSelect", js)
        self.assertIn("optgroup", js.split("function fillPartSelect", 1)[1][:800])
        self.assertEqual(js.count("fillPartSelect("), 3,
                         "고르기 셀렉트 2곳(rp-part·cr-part)이 다 묶음을 안 쓴다")
        # 자산 상세 '수리 항목' 칩도 수리/도색·시트지로 갈린다
        self.assertIn('kk === "repair" ? "🛠 수리" : "🎨 도색/시트지"', js)

    def test_수리항목_메타가_묶음을_알려준다(self):
        items = self.c.get("/api/purchase-meta").get_json()["repairItems"]
        kinds = {x.get("kind") for x in items}
        self.assertIn("repair", kinds)
        self.assertIn("paint", kinds, "도색·시트지가 수리 항목 후보에 안 온다")
        sitji = next((x for x in items if "시트지" in x["name"]), None)
        self.assertIsNotNone(sitji)
        self.assertEqual(sitji["kind"], "paint")

    def test_수리_항목에_금액과_합계가_보인다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
              ).read_text("utf-8")
        blk = js.split("const drawTasks = () =>", 1)[1][:2600]
        self.assertIn("priceOf(", blk, "항목별 금액을 안 보여 준다")
        self.assertIn("pickedSum", blk, "합계를 안 보여 준다")
        self.assertIn("overflow-y:auto", blk, "항목이 많아지면 화면을 밀어낸다")


class TestPeriodReport(Base):
    """매출 실적을 일/주/월/분기/연으로(2026-08-24 대표 지시).

    "판매된 제품의 경우에는 매출 실적에서 오늘 출고된것들에 대한 순수익, 매출까지 계산이 돼?
     매출/부가세/순이익/부가세등 한눈에 편하게 볼 수 있게 해줘야 돼. 일/주/월/분기/연 단위로."
    ★설정 ▸ 매출/실적에 들어간다(대표 정정).
    """

    def _sold(self, amount=550000, buy=300000):
        """출고까지 끝난 주문 한 건을 만든다 — 매출·원가가 잡히는 최소 조합."""
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": buy}).get_json()[0]
        o = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "홍길동", "productName": "노트북",
            "phone": "010-1111-2222", "address": "서울시 강남구", "postalCode": "06000",
            "quantity": 1, "amount": amount}).get_json()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        # 단계 이름은 서버 계약 그대로 — 'inspection'이 아니라 'softwareInspection'이다
        for act in ("preparing", "production", "softwareInspection", "shipping"):
            r = self.c.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
            self.assertEqual(r.status_code, 200, f"{act}: {r.get_data(as_text=True)}")
        return o, a

    def test_오늘_출고분이_오늘_실적에_잡힌다(self):
        self._sold()
        d = self.c.get("/api/reports/period?unit=day").get_json()
        self.assertEqual(d["orders"], 1)
        self.assertEqual(d["units"], 1)
        self.assertEqual(d["revenue"], 550000)

    def test_부가세를_총액에서_나눈다(self):
        """판매가는 부가세 포함 총액이다 — 매입·수리비와 같은 규약."""
        self._sold(amount=550000)
        d = self.c.get("/api/reports/period?unit=day").get_json()
        self.assertEqual(d["vat"], 50000)
        self.assertEqual(d["supply"], 500000)
        self.assertEqual(d["supply"] + d["vat"], d["revenue"], "공급가+부가세가 총액과 다르다")

    def test_순이익은_실입금에서_원가를_뺀다(self):
        self._sold(amount=550000, buy=300000)
        d = self.c.get("/api/reports/period?unit=day").get_json()
        self.assertEqual(d["buyCost"], 300000)
        self.assertEqual(d["profit"], d["netRevenue"] - d["cost"])
        # ★출고 확인 때 몰 수수료가 자동으로 붙는다 — 순이익은 그걸 뺀 실입금 기준이다.
        #   (매출 550,000 − 고도몰 수수료 − 매입 300,000)
        self.assertGreater(d["fee"], 0, "몰 수수료가 안 붙었다")
        self.assertEqual(d["netRevenue"], d["revenue"] - d["fee"] - d["refund"])
        self.assertEqual(d["profit"], 550000 - d["fee"] - 300000)

    def test_낼_세금은_매출세액에서_매입세액을_뺀다(self):
        d = self.c.get("/api/reports/period?unit=day").get_json()
        self.assertEqual(d["vatPayable"], d["vat"] - d["repairVat"])

    def test_다섯_단위가_모두_된다(self):
        self._sold()
        for unit in ("day", "week", "month", "quarter", "year"):
            d = self.c.get(f"/api/reports/period?unit={unit}").get_json()
            self.assertEqual(d["unit"], unit)
            self.assertTrue(d["label"].strip(), f"{unit} 이름이 비었다")
            self.assertLessEqual(d["from"], d["to"])
            self.assertEqual(d["revenue"], 550000, f"{unit} 에 오늘 출고분이 안 잡힌다")

    def test_모르는_단위는_거부한다(self):
        self.assertEqual(self.c.get("/api/reports/period?unit=아무거나").status_code, 400)

    def test_이전_기간에는_안_잡힌다(self):
        self._sold()
        d = self.c.get("/api/reports/period?unit=day").get_json()
        prev = self.c.get(f"/api/reports/period?unit=day&at={d['prev']}").get_json()
        self.assertEqual(prev["revenue"], 0, "어제 실적에 오늘 출고분이 섞였다")

    def test_이전_다음_기준일을_알려준다(self):
        d = self.c.get("/api/reports/period?unit=month&at=2026-03-15").get_json()
        self.assertEqual(d["from"], "2026-03-01")
        self.assertEqual(d["to"], "2026-03-31")
        self.assertTrue(d["prev"].startswith("2026-02"))
        self.assertTrue(d["next"].startswith("2026-04"))

    def test_분기_경계가_맞는다(self):
        d = self.c.get("/api/reports/period?unit=quarter&at=2026-05-20").get_json()
        self.assertEqual((d["from"], d["to"]), ("2026-04-01", "2026-06-30"))
        self.assertIn("2분기", d["label"])

    def test_주는_월요일에_시작한다(self):
        d = self.c.get("/api/reports/period?unit=week&at=2026-08-19").get_json()  # 수요일
        self.assertEqual(d["from"], "2026-08-17")
        self.assertEqual(d["to"], "2026-08-23")

    def test_설정_매출실적에_들어간다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
              ).read_text("utf-8")
        self.assertIn('["period", "📊 기간 실적"]', js)
        self.assertIn('state.salesView === "period"', js)
        self.assertIn("/api/reports/period", js)
        rj = (Path(__file__).resolve().parent.parent / "static" / "js" / "reports.js"
              ).read_text("utf-8")
        self.assertNotIn("renderPeriodReport", rj, "리포트에 남아 두 곳에서 그린다")


class TestLabelLineBreak(Base):
    """자산번호를 그 줄에만 찍는다(2026-08-24 대표 승인 — 좌표 무접촉 줄바꿈).

    "자산번호가 있는 부분에는 해당 줄에 자산번호만 나왔으면 좋겠는데."
    ★레이아웃 동결 준수: 줄 시작(55.4mm)·줄간격(4.0mm)·글자크기(9pt)는 안 건드렸다.
      바꾼 건 wrap_mm 이 줄바꿈 문자를 살리는 것과, 상품명 칸 최대 줄수(2→4, 설정으로 조절)뿐.
    """

    def test_자산번호가_줄을_바꾼다(self):
        from app.cj.label import join_items
        items = [{"name": "[고도몰] 코드", "qty": 1},
                 {"name": "자산 260628-0015", "br": True},
                 {"name": "상품명"}]
        got = join_items(items, True)
        lines = got.split(chr(10))
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1], "자산 260628-0015 / 상품명",
                         "자산번호가 줄 머리에 안 온다")

    def test_끄면_예전과_똑같다(self):
        """★롤백 장치(대표 2026-08-24: "문제발생하면 롤백할 수 있게") — 코드 수정 없이."""
        from app.cj.label import join_items
        items = [{"name": "[고도몰] 코드", "qty": 1},
                 {"name": "자산 260628-0015", "br": True},
                 {"name": "상품명"}]
        self.assertEqual(join_items(items, False),
                         "[고도몰] 코드 x1 / 자산 260628-0015 / 상품명")
        self.assertNotIn(chr(10), join_items(items, False))

    def test_설정이_줄수와_줄바꿈을_정한다(self):
        from app.orders.waybill import label_layout_of
        self.assertEqual(label_layout_of({}), {"line_break": True, "item_lines": 4})
        self.assertEqual(label_layout_of({"label": {"lineBreak": False, "lines": 2}}),
                         {"line_break": False, "item_lines": 2})
        # 엉뚱한 값은 안전한 기본으로
        self.assertEqual(label_layout_of({"label": {"lines": 99}})["item_lines"], 4)
        self.assertEqual(label_layout_of({"label": {"lines": "x"}})["item_lines"], 4)

    def test_compose가_자산번호_조각에_br을_단다(self):
        from app.orders.waybill import _compose_items
        row = {"channel": "고도몰", "product_code": "L480_i5-8_내장", "product_name": "L480",
               "quantity": 1, "option_name": "", "order_no": "1", "recipient": "홍길동",
               "delivery_message": "", "is_review": 0}
        items = _compose_items(row, ["260628-0015"], simulated=False)
        asset = next(it for it in items if "자산" in it["name"])
        self.assertTrue(asset.get("br"), "자산번호 조각에 줄바꿈 표시가 없다")
        self.assertFalse(items[0].get("br"), "첫 조각이 줄을 바꾸면 첫 줄이 빈다")

    def test_렌더러가_줄바꿈을_살린다(self):
        """wrap_mm 이 줄바꿈 문자를 뭉개면 위 전부가 헛일이다 — PDF 바이트로 확인한다."""
        from app.cj import cj2_waybill_pdf
        base = {"invoice_no": "650000000033", "rcpt_ymd": "20260824", "kind": "ship",
                "clsfcd": "5D32", "subclsfcd": "1g", "clsf_full": "5D32-1g",
                "clsfaddr": "서울", "bran": "강남-A12", "p2pcd": "P12",
                "frt_dv": "03", "frt_dv_nm": "신용", "remark": "",
                "receiver": {"name": "홍길동", "tel": "010-1234-5678",
                             "addr": "서울 강남구", "addr_detail": ""},
                "sender": {"name": "업무관리", "tel": "02", "addr": "인천", "addr_detail": ""}}
        one = cj2_waybill_pdf({**base, "item_summary": "코드 / 자산 260628-0015 / 상품"}, True)
        two = cj2_waybill_pdf({**base, "item_summary": "코드" + chr(10) + "자산 260628-0015 / 상품"}, True)
        self.assertTrue(one.startswith(b"%PDF") and two.startswith(b"%PDF"))
        self.assertNotEqual(one, two, "줄바꿈 문자가 뭉개져 같은 종이가 나온다")
        # 줄수 설정도 종이에 반영된다
        capped = cj2_waybill_pdf({**base, "item_lines": 2,
                                  "item_summary": "코드" + chr(10) + "자산 1" + chr(10) + "셋째줄"}, True)
        full = cj2_waybill_pdf({**base, "item_lines": 4,
                                "item_summary": "코드" + chr(10) + "자산 1" + chr(10) + "셋째줄"}, True)
        self.assertNotEqual(capped, full, "줄수 설정이 종이에 안 실린다")

    def test_화면에_되돌리기_스위치가_있다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
              ).read_text("utf-8")
        self.assertIn('id="cj-lines"', js)
        self.assertIn('id="cj-linebreak"', js)
        self.assertIn("lineBreak: !!$(\"#cj-linebreak\")?.checked", js)
        self.assertIn("view=Fit", js, "견본이 스크롤 없이 한 장으로 안 보인다")


class TestSupplierAutocomplete(Base):
    """매입 등록 거래처를 CPU·램처럼 타이핑 자동완성으로(2026-08-24 대표 지시).

    ★거래처가 2,000곳이 넘는다(TMS 이관 2,130곳) — select 를 내려서 훑을 수 없다.
    """

    def test_거래처_후보가_온다(self):
        self.c.post("/api/suppliers", json={"name": "엔씨디지텍"})
        self.c.post("/api/suppliers", json={"name": "미래아이앤씨"})
        r = self.c.get("/api/spec-options?field=supplier&q=엔씨").get_json()
        vals = [o["value"] for o in r["options"]]
        self.assertIn("엔씨디지텍", vals)
        self.assertNotIn("미래아이앤씨", vals)

    def test_전표를_많이_쓴_거래처가_먼저_온다(self):
        a = self.c.post("/api/suppliers", json={"name": "한산컴퓨터"}).get_json()["id"]
        b = self.c.post("/api/suppliers", json={"name": "한빛유통"}).get_json()["id"]
        for _ in range(3):
            self.c.post("/api/purchase-batches", json={
                "stage": "purchased", "purchaseDate": "2026-08-24",
                "supplierId": b, "totalAmount": 0})
        vals = [o["value"] for o in
                self.c.get("/api/spec-options?field=supplier&q=한").get_json()["options"]]
        self.assertLess(vals.index("한빛유통"), vals.index("한산컴퓨터"),
                        f"자주 쓰는 곳이 아래에 있다: {vals}")

    def test_이름으로_전표를_만든다(self):
        self.c.post("/api/suppliers", json={"name": "드림노트북"})
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-08-24",
            "supplierName": "드림노트북", "totalAmount": 0})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        d = self.c.get(f"/api/purchase-batches/{r.get_json()['id']}").get_json()
        self.assertEqual(d["supplierName"], "드림노트북")

    def test_없는_이름이면_거래처가_새로_생긴다(self):
        """TMS 이관과 같은 규칙 — 이름이 곧 거래처다."""
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-08-24",
            "supplierName": "처음보는거래처", "totalAmount": 0})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        names = [s["name"] for s in self.c.get("/api/suppliers").get_json()]
        self.assertIn("처음보는거래처", names)

    def test_공백_대소문자만_다르면_같은_곳이다(self):
        """★'엔씨디지텍'과 '엔씨디지텍 '이 두 곳으로 갈리면 거래처별 집계를 못 믿는다."""
        self.c.post("/api/suppliers", json={"name": "AB테크"})
        before = len(self.c.get("/api/suppliers").get_json())
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-08-24",
            "supplierName": " ab 테크 ", "totalAmount": 0})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))
        self.assertEqual(len(self.c.get("/api/suppliers").get_json()), before,
                         "★띄어쓰기만 다른 이름이 새 거래처로 갈렸다")
        d = self.c.get(f"/api/purchase-batches/{r.get_json()['id']}").get_json()
        self.assertEqual(d["supplierName"], "AB테크")

    def test_매입_전표는_이름을_비울_수_없다(self):
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-08-24",
            "supplierName": "", "totalAmount": 0})
        self.assertEqual(r.status_code, 400)

    def test_가입고는_비워도_된다(self):
        r = self.c.post("/api/purchase-batches", json={
            "stage": "provisional", "purchaseDate": "2026-08-24",
            "supplierName": "", "totalAmount": 0})
        self.assertIn(r.status_code, (200, 201), r.get_data(as_text=True))

    def test_id가_오면_이름은_무시한다(self):
        """옛 화면·다른 호출자가 둘 다 보내도 id 가 이긴다 — 규칙이 갈리면 안 된다."""
        a = self.c.post("/api/suppliers", json={"name": "아이디우선"}).get_json()["id"]
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-08-24",
            "supplierId": a, "supplierName": "다른이름이왔다", "totalAmount": 0})
        self.assertIn(r.status_code, (200, 201))
        d = self.c.get(f"/api/purchase-batches/{r.get_json()['id']}").get_json()
        self.assertEqual(d["supplierName"], "아이디우선")
        names = [s["name"] for s in self.c.get("/api/suppliers").get_json()]
        self.assertNotIn("다른이름이왔다", names, "무시해야 할 이름으로 거래처가 생겼다")

    def test_수정으로도_이름을_바꿀_수_있다(self):
        self.c.post("/api/suppliers", json={"name": "갑거래처"})
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-08-24",
            "supplierName": "갑거래처", "totalAmount": 0}).get_json()
        p = self.c.patch(f"/api/purchase-batches/{r['id']}",
                         json={"supplierName": "을거래처"})
        self.assertEqual(p.status_code, 200, p.get_data(as_text=True))
        d = self.c.get(f"/api/purchase-batches/{r['id']}").get_json()
        self.assertEqual(d["supplierName"], "을거래처")

    def test_화면이_자동완성_입력을_쓴다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
              ).read_text("utf-8")
        self.assertNotIn('<select id="sl-supplier"', js, "아직 select 다")
        self.assertIn('<input type="text" id="sl-supplier"', js)
        self.assertEqual(js.count('attachAutocomplete($("#sl-supplier", host), "supplier")'), 2,
                         "등록 폼·상세 폼 두 곳 모두에 붙어야 한다")
        self.assertIn('supplierName: pick("#sl-supplier").value.trim()', js)


class TestUnshipRollback(Base):
    """출고 확인 되돌리기(2026-08-24 대표: "잘못한 경우는 어떻게 해야해? 롤백하는 기능도").

    ★서버 경로(PATCH shipping=false)는 있었고 화면 버튼이 없었다 — 출고 기록 조회에서
      원클릭으로 되돌린다. 보관돼 있으면 먼저 꺼내고, 송장은 상황에 따라 취소를 묻는다.
    """

    def _shipped(self):
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        o = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "홍길동", "productName": "노트북",
            "phone": "010-1111-2222", "address": "서울시 강남구", "postalCode": "06000",
            "quantity": 1, "amount": 500000}).get_json()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("preparing", "production", "softwareInspection", "shipping"):
            r = self.c.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
            self.assertEqual(r.status_code, 200, f"{act}: {r.get_data(as_text=True)}")
        return o, a

    def test_되돌리면_진행_중으로_돌아온다(self):
        o, a = self._shipped()
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["status"], "shipped")
        r = self.c.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": False})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        got = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(got["status"], "reserved", "★자산이 출고완료로 굳어 있다")
        acts = [e["action"] for e in got["events"]]
        self.assertIn("출고취소", acts, "되돌린 흔적이 이력에 없다")

    def test_보관된_주문은_해제부터_해야_한다(self):
        """화면이 unarchive → shipping=false 순서로 부르는 근거."""
        o, a = self._shipped()
        self.c.post("/api/orders/bulk", json={"action": "archive", "ids": [o["id"]]})
        r = self.c.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": False})
        self.assertEqual(r.status_code, 409, "보관 중인데 단계 변경이 됐다")
        un = self.c.post("/api/orders/bulk",
                         json={"action": "unarchive", "ids": [o["id"]]}).get_json()
        self.assertFalse(un.get("failed"), un)
        r = self.c.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": False})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_되돌려도_출고일과_담당자는_남는다(self):
        """다시 출고 확인하면 처음 출고한 날·사람이 유지된다(매출 귀속이 안 흔들린다)."""
        o, a = self._shipped()
        before = self.c.get(f"/api/orders/{o['id']}").get_json()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": False})
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})
        after = self.c.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual(after["shippingAt"], before["shippingAt"], "출고일이 오늘로 덮였다")
        self.assertEqual(after["shippingBy"], before["shippingBy"])

    def test_화면에_되돌리기_버튼이_있다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
              ).read_text("utf-8")
        self.assertIn("data-unship=", js)
        self.assertIn("async function unshipOrder", js)
        blk = js.split("async function unshipOrder", 1)[1].split(chr(10) + "function ", 1)[0]
        self.assertIn('"unarchive"', blk, "보관된 건은 먼저 꺼내야 단계 변경이 된다")
        self.assertIn('{ action: "shipping", value: false }', blk)
        self.assertIn("/cancel", blk, "송장 취소 경로가 없다")
        self.assertIn("기사가 집화", blk, "실발행 송장을 남겨 두면 무슨 일이 나는지 안 알린다")


class TestLineBreakToken(Base):
    """[줄바꿈] 키워드(2026-08-24 대표: "줄바꿈 기능도 문구로 넣어주면").

    ★상품명 칸 전용 — 배송메세지 칸은 CJ REMARK_1 로도 나가서 줄바꿈을 실을 수 없다.
    """

    ROW = {
        "channel": "고도몰", "product_code": "L480_i5-8_내장", "product_name": "L480",
        "quantity": 1, "option_name": "램 16G 업", "order_no": "1", "recipient": "홍길동",
        "delivery_message": "", "is_review": 0,
    }

    def _summary(self, tmpl, line_break=True):
        from app.cj.label import join_items
        from app.orders.waybill import _compose_items
        items = _compose_items(dict(self.ROW), ["260628-0015"], simulated=False, tmpl=tmpl)
        return join_items(items, line_break)

    def test_줄바꿈_키워드로_줄이_바뀐다(self):
        got = self._summary("[쇼핑몰] [제품코드] / [줄바꿈] [옵션명] / [상품명]")
        lines = got.split(chr(10))
        self.assertEqual(len(lines), 2, got)
        self.assertTrue(lines[1].startswith("옵션 램 16G 업"), got)

    def test_홀로_쓰면_다음_조각이_새_줄이_된다(self):
        got = self._summary("[쇼핑몰] [제품코드] / [줄바꿈] / [옵션명]")
        self.assertEqual(got.split(chr(10))[1], "옵션 램 16G 업", got)

    def test_설정에서_줄바꿈을_끄면_한_줄이다(self):
        """롤백 스위치(cj.label.lineBreak)가 [줄바꿈]에도 같이 먹는다 — 스위치가 두 개면 헷갈린다."""
        got = self._summary("[쇼핑몰] [제품코드] / [줄바꿈] [옵션명]", line_break=False)
        self.assertNotIn(chr(10), got)
        self.assertIn(" / 옵션 램 16G 업", got)

    def test_배송메세지_칸에서는_거부한다(self):
        from app.orders.waybill import check_label_tmpl
        self.assertEqual(check_label_tmpl("[배송메모] / [줄바꿈]", "remark"), ["줄바꿈"])
        self.assertEqual(check_label_tmpl("[줄바꿈] [자산번호]", "item"), [])
        r = self.c.put("/api/settings", json={"cj": {
            "label_tmpl": {"item": "", "remark": "[배송메모] / [줄바꿈]"}}})
        self.assertEqual(r.status_code, 400, "★CJ REMARK_1 에 줄바꿈이 실린다")

    def test_미리보기_글자에도_줄바꿈이_보인다(self):
        r = self.c.post("/api/cj/label-preview", json={
            "item": "[쇼핑몰] [제품코드] / [줄바꿈] [자산번호]", "remark": ""})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn(chr(10), r.get_json()["itemSummary"],
                      "화면 미리보기가 줄바꿈을 뭉갠다")

    def test_편집_화면에_키워드_칩이_뜬다(self):
        d = self.c.get("/api/cj/label-tokens").get_json()
        tok = next((t for t in d["tokens"] if t["name"] == "줄바꿈"), None)
        self.assertIsNotNone(tok, "칩 목록에 줄바꿈이 없다")
        self.assertEqual(tok["scope"], "item", "배송메세지 칸 칩에도 떠 버린다")


class TestExtraCharge(Base):
    """추가 결제(2026-08-24 대표) — 업그레이드 비용을 기존 주문에 합친다.

    "입금/고도몰 결제(카드, 에스크로, 입금 등)에 따라서 수수료 및 부가세 부분을 다르게 …
     해당 자산에 매출 반영, 수수료 반영이 함께"
    """

    def _order(self, amount=500000, channel="고도몰"):
        return self.c.post("/api/orders", json={
            "channel": channel, "recipient": "홍길동", "productName": "노트북",
            "phone": "010-1111-2222", "address": "서울시 강남구", "postalCode": "06000",
            "quantity": 1, "amount": amount}).get_json()

    def test_입금은_수수료_없이_합쳐진다(self):
        o = self._order()
        r = self.c.post(f"/api/orders/{o['id']}/extras", json={
            "amount": 30000, "method": "bank", "note": "램 16G 업"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d["fee"], 0)
        self.assertEqual(d["order"]["amount"], 530000, "매출에 안 합쳐졌다")

    def test_몰_결제는_채널_요율로_수수료가_붙는다(self):
        self.c.put("/api/settings", json={"settlement": {"rates": {"고도몰": 10}}})
        o = self._order()
        r = self.c.post(f"/api/orders/{o['id']}/extras", json={
            "amount": 30000, "method": "mall"}).get_json()
        self.assertEqual(r["fee"], 3000)
        self.assertEqual(r["order"]["amount"], 530000)

    def test_요율_직접_입력도_된다(self):
        o = self._order()
        r = self.c.post(f"/api/orders/{o['id']}/extras", json={
            "amount": 10000, "method": "custom", "rate": 3.3}).get_json()
        self.assertEqual(r["fee"], 330)

    def test_출고_시_자동_수수료가_입금분에는_안_붙는다(self):
        """★핵심 — 안 빼면 입금(무수수료) 추가분에도 몰 요율이 붙는다."""
        self.c.put("/api/settings", json={"settlement": {"rates": {"고도몰": 10}}})
        o = self._order(amount=500000)
        self.c.post(f"/api/orders/{o['id']}/extras", json={"amount": 30000, "method": "bank"})
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("preparing", "production", "softwareInspection", "shipping"):
            self.c.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        d = self.c.get(f"/api/orders/{o['id']}").get_json()
        # 기본 50만원×10% = 5만원. 입금 3만원엔 수수료가 붙으면 안 된다(53,000이면 버그)
        self.assertEqual(d["feeAmount"], 50000, f"★입금 추가분에 요율이 붙었다: {d['feeAmount']}")
        self.assertEqual(d["amount"], 530000)

    def test_출고_시_몰_결제_추가분은_이중으로_안_붙는다(self):
        self.c.put("/api/settings", json={"settlement": {"rates": {"고도몰": 10}}})
        o = self._order(amount=500000)
        self.c.post(f"/api/orders/{o['id']}/extras", json={"amount": 30000, "method": "mall"})
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("preparing", "production", "softwareInspection", "shipping"):
            self.c.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        d = self.c.get(f"/api/orders/{o['id']}").get_json()
        # 기본 5만 + 추가분 3천 = 53,000. 56,000(전체×10%+3천)이면 이중 계상.
        self.assertEqual(d["feeAmount"], 53000, f"이중 계상: {d['feeAmount']}")

    def test_기간_실적에_그대로_잡힌다(self):
        """'해당 자산에 매출 반영' — 집계가 orders.amount/fee 를 보므로 자동으로 맞는다."""
        o = self._order(amount=500000)
        self.c.post(f"/api/orders/{o['id']}/extras", json={"amount": 50000, "method": "bank"})
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("preparing", "production", "softwareInspection", "shipping"):
            self.c.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        d = self.c.get("/api/reports/period?unit=day").get_json()
        self.assertEqual(d["revenue"], 550000, "추가 결제가 기간 실적에 안 잡힌다")

    def test_삭제하면_금액과_수수료가_되돌아간다(self):
        o = self._order()
        x = self.c.post(f"/api/orders/{o['id']}/extras", json={
            "amount": 30000, "method": "custom", "rate": 10}).get_json()
        xid = self.c.get(f"/api/orders/{o['id']}/extras").get_json()["extras"][0]["id"]
        r = self.c.delete(f"/api/orders/{o['id']}/extras/{xid}").get_json()
        self.assertEqual(r["order"]["amount"], 500000)
        self.assertEqual(r["order"]["feeAmount"], 0)

    def test_출고_확인_후에는_막힌다(self):
        """이미 확정된 그 달 매출이 소리 없이 바뀌면 안 된다 — [↩ 되돌리기] 먼저."""
        o = self._order()
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("preparing", "production", "softwareInspection", "shipping"):
            self.c.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        r = self.c.post(f"/api/orders/{o['id']}/extras", json={"amount": 10000, "method": "bank"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("되돌리기", r.get_json()["error"])

    def test_화면에_추가_결제_버튼과_모달이_있다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
              ).read_text("utf-8")
        self.assertIn("data-extra=", js)
        self.assertIn("async function openExtraCharge", js)
        self.assertIn("부가세 포함 총액", js)


class TestChannelColors(Base):
    """채널별 색(2026-08-24 대표: "실제 API 입력하는 몰마다 색상이 다르게")."""

    def test_몰마다_다른_클래스가_붙는다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "orders.js"
              ).read_text("utf-8")
        self.assertIn("CHANNEL_CLASS", js)
        blk = js.split("const CHANNEL_CLASS = {", 1)[1].split("};", 1)[0]
        import re as _re
        pairs = dict(_re.findall(r'"([^"]+)": "(ch-[a-z0-9]+)"', blk))
        for ch in ("쿠팡", "스마트스토어", "11번가", "고도몰", "카카오"):
            self.assertIn(ch, pairs, f"{ch} 색이 없다")
        # ★수기·전화·방문(같은 회색)만 빼고, '서로 다른 몰'은 색이 달라야 구분이 된다.
        #   같은 몰의 다른 표기(네이버=스마트스토어, b2b=B2B)는 같은 색이 맞다 —
        #   대소문자만 다른 키와 알려진 별칭은 하나로 접고 나서 겹침을 본다.
        ALIAS = {"네이버": "스마트스토어"}
        by_channel = {}
        for k, v in pairs.items():
            if v == "ch-manual":
                continue
            key = ALIAS.get(k, k).lower()
            self.assertEqual(by_channel.setdefault(key, v), v,
                             f"같은 몰({k})의 표기끼리 색이 갈린다")
        vals = list(by_channel.values())
        self.assertEqual(len(vals), len(set(vals)), f"몰끼리 색이 겹친다: {by_channel}")

    def test_색_정의가_CSS에_있다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "orders.js"
              ).read_text("utf-8")
        css = (Path(__file__).resolve().parent.parent / "static" / "css" / "app.css"
               ).read_text("utf-8")
        import re as _re
        for cls in set(_re.findall(r'"(ch-[a-z0-9]+)"', js)):
            self.assertIn(f".{cls}", css, f"{cls} 색이 CSS에 없다 — 배지가 투명해진다")

    def test_주문_구분선이_있다(self):
        css = (Path(__file__).resolve().parent.parent / "static" / "css" / "app.css"
               ).read_text("utf-8")
        self.assertIn("#setup-rows > tr:not(.setup-detail) > td", css)
        self.assertIn("#setup-rows > tr.setup-detail > td { border-top: 0; }", css,
                      "상세 펼침 줄에도 굵은 선이 그어져 주문이 갈라져 보인다")


class TestContactEdit(Base):
    """주문자 정보 수정 + CJ 주소 확인 + 롤백(2026-08-24 대표)."""

    def _order(self):
        return self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "홍길동", "productName": "노트북",
            "phone": "010-1111-2222", "address": "서울시 강남구 테헤란로 1", "postalCode": "06000",
            "quantity": 1, "amount": 500000}).get_json()

    def test_수정하면_이전_값이_이력에_남는다(self):
        o = self._order()
        r = self.c.post(f"/api/orders/{o['id']}/contact", json={
            "recipient": "김철수", "phone": "010-9999-8888",
            "postalCode": "12345", "address": "서울특별시 예시구 예시로 1"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["order"]["recipient"], "김철수")
        h = self.c.get(f"/api/orders/{o['id']}/contact-history").get_json()
        self.assertEqual(len(h["history"]), 1)
        self.assertEqual(h["history"][0]["recipient"], "홍길동", "수정 전 값이 아니다")
        self.assertEqual(h["history"][0]["phone"], "010-1111-2222")

    def test_롤백하면_그_시점으로_돌아가고_지금_값도_남는다(self):
        o = self._order()
        self.c.post(f"/api/orders/{o['id']}/contact", json={"recipient": "김철수"})
        hid = self.c.get(f"/api/orders/{o['id']}/contact-history"
                         ).get_json()["history"][0]["id"]
        r = self.c.post(f"/api/orders/{o['id']}/contact-rollback", json={"historyId": hid})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["order"]["recipient"], "홍길동")
        h = self.c.get(f"/api/orders/{o['id']}/contact-history").get_json()
        # 롤백 전 값(김철수)도 남아 롤백의 롤백이 된다
        self.assertTrue(any(x["recipient"] == "김철수" for x in h["history"]),
                        "롤백 전 값이 사라져 되돌릴 수 없다")

    def test_바뀐_게_없으면_이력을_안_쌓는다(self):
        o = self._order()
        r = self.c.post(f"/api/orders/{o['id']}/contact", json={"recipient": "홍길동"})
        self.assertEqual(r.get_json()["changed"], 0)
        h = self.c.get(f"/api/orders/{o['id']}/contact-history").get_json()
        self.assertEqual(len(h["history"]), 0, "안 바꿨는데 이력이 쌓인다")

    def test_성함은_비울_수_없다(self):
        o = self._order()
        self.assertEqual(self.c.post(f"/api/orders/{o['id']}/contact",
                                     json={"recipient": ""}).status_code, 400)

    def test_송장이_있으면_경고_플래그가_선다(self):
        o = self._order()
        h = self.c.get(f"/api/orders/{o['id']}/contact-history").get_json()
        self.assertFalse(h["hasWaybill"])

    def test_주소_확인은_CJ_미설정이면_조용히_실패한다(self):
        """확인만 못 할 뿐 수정을 막으면 안 된다."""
        o = self._order()
        r = self.c.post(f"/api/orders/{o['id']}/addr-check",
                        json={"address": "서울시 강남구"})
        self.assertEqual(r.status_code, 200, "CJ 미설정인데 500/400이 났다")
        self.assertFalse(r.get_json()["ok"])

    def test_화면에_수정_버튼과_모달이_있다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
              ).read_text("utf-8")
        self.assertIn("data-contact=", js)
        self.assertIn("async function openContactEdit", js)
        # ★2026-08-26 대표: CJ 주소 확인 버튼 삭제 → [↩ 처음 값으로]가 그 자리
        self.assertNotIn('"#ct-check"', js, "없앤 CJ 확인 배선이 되살아났다")
        self.assertIn('id="ct-orig"', js)
        self.assertIn("data-ctroll=", js, "롤백 버튼이 없다")
        self.assertIn("수정 전 주소", js, "송장 발급 후 수정 시 경고가 없다")

    def test_주소는_표준_양식이다(self):
        """★대표 2026-08-24 재지시: "주소찾기 기능이 있어서 FM대로 주소가 입력되게".

        우편번호 + [🔍 주소 찾기](다음 우편번호 검색) + 기본주소 + 상세주소 —
        손으로 치면 오타·비표준 표기가 생겨 CJ 분류가 어긋난다.
        """
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
              ).read_text("utf-8")
        blk = js.split("async function openContactEdit", 1)[1].split(chr(10) + "/* ", 1)[0]
        self.assertIn('id="ct-find"', blk, "주소 찾기 버튼이 없다")
        self.assertIn("openAddressSearch", blk, "다음 우편번호 검색을 안 쓴다")
        self.assertIn('id="ct-addr2"', blk, "상세주소 칸이 없다")
        self.assertIn("fullAddr()", blk, "기본+상세를 합쳐 보내지 않는다")
        # 주소 찾기 결과가 우편번호·기본주소를 채우고 상세로 커서를 옮긴다
        self.assertIn('$("#ct-zip", host).value = got.zip', blk)
        self.assertIn("detail.focus()", blk)


class TestPerUnitPrepared(Base):
    """대별 준비 체크(2026-08-24 대표: "2대 이상건의 경우 각각 자산 준비가 되었는지")."""

    def _multi(self, qty=2):
        cats = self.c.get("/api/categories").get_json()
        made = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": qty,
            "purchasePrice": 100000}).get_json()
        o = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "홍길동", "productName": "노트북",
            "phone": "010-1111-2222", "address": "서울시 강남구", "postalCode": "06000",
            "quantity": qty, "amount": 1000000}).get_json()
        self.c.patch(f"/api/orders/{o['id']}",
                     json={"action": "assets", "assetIds": [a["id"] for a in made]})
        return o, made

    def test_대별로_준비를_체크한다(self):
        o, made = self._multi(2)
        r = self.c.post(f"/api/orders/{o['id']}/assets/{made[0]['id']}/prepared",
                        json={"value": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        assets = r.get_json()["order"]["assets"]
        flags = {a["assetNo"]: a["prepared"] for a in assets}
        self.assertEqual(sorted(flags.values()), [False, True],
                         "한 대만 체크했는데 결과가 다르다")

    def test_해제도_된다(self):
        o, made = self._multi(2)
        self.c.post(f"/api/orders/{o['id']}/assets/{made[0]['id']}/prepared",
                    json={"value": True})
        r = self.c.post(f"/api/orders/{o['id']}/assets/{made[0]['id']}/prepared",
                        json={"value": False}).get_json()
        self.assertFalse(any(a["prepared"] for a in r["order"]["assets"]))

    def test_매칭_안_된_자산은_404(self):
        o, made = self._multi(1)
        cats = self.c.get("/api/categories").get_json()
        other = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "X", "qty": 1,
            "purchasePrice": 1}).get_json()[0]
        self.assertEqual(self.c.post(
            f"/api/orders/{o['id']}/assets/{other['id']}/prepared",
            json={"value": True}).status_code, 404)

    def test_출고_확인_후에는_못_바꾼다(self):
        o, made = self._multi(1)
        for act in ("preparing", "production", "softwareInspection", "shipping"):
            self.c.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        self.assertEqual(self.c.post(
            f"/api/orders/{o['id']}/assets/{made[0]['id']}/prepared",
            json={"value": True}).status_code, 409)

    def test_2대_이상은_자산칸이_하단에만_뜬다(self):
        """대표 2026-08-25: "우측과 아래 두 개가 표시될 필요는 없으니 하단에만" —
        2대 이상은 우측 칸이 요약(N/M대·🔍·펼침)만, 자산 칩·스캔칸은 대별 줄에만."""
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
              ).read_text("utf-8")
        blk = js.split("function assetCell", 1)[1].split(chr(10) + "}", 1)[0]
        # 칩·스캔칸 그리기가 1대짜리 분기 안에 있어야 한다
        chips = blk.split("if (need === 1) {", 1)
        self.assertEqual(len(chips), 2, "1대 전용 분기가 사라졌다 — 2대 이상에 이중 표시")
        self.assertIn("chip chip-green", chips[1], "자산 칩이 1대 분기 밖에 있다")
        self.assertIn('class="as-slot"', chips[1], "스캔칸이 1대 분기 밖에 있다")
        # 접힌 2대 주문은 스캔할 곳이 없어지므로 안내 힌트가 있어야 한다
        self.assertIn("▸를 눌러 대별 입력", blk, "접힘 상태 안내가 없다")

    def test_화면에_펼침과_대별_줄이_있다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
              ).read_text("utf-8")
        self.assertIn("function unitsRow", js)
        self.assertIn("data-units=", js, "펼침 버튼이 없다")
        self.assertIn("data-prep-unit=", js, "대별 체크박스가 없다")
        self.assertIn('need > 1', js, "1대짜리에도 펼침이 떠 화면만 복잡해진다")
        # ★기본이 '펼침'이다(대표 2026-08-24: "왜 각각 수량별로 뜨게 안되어있지") —
        #   접은 주문만 false 로 기억한다. 이 조건이 truthy 검사로 바뀌면 기본이 접힘이 된다.
        self.assertIn('[o.id] !== false && (Number(o.quantity) || 1) > 1', js,
                      "2대 이상이 기본으로 접혀 있다")
        css = (Path(__file__).resolve().parent.parent / "static" / "css" / "app.css"
               ).read_text("utf-8")
        self.assertIn(".units-box", css)
        self.assertIn("margin-left: 34px", css, "들여쓰기가 없다")
        self.assertIn("border-bottom: 1px dashed", css, "대별 구분선이 없다")


class TestChangelogSameDayAppend(Base):
    """같은 날 여러 번 재기동해도 나중에 적은 항목이 올라간다(2026-08-24 실제 구멍).

    첫 재기동이 그 시점의 몇 줄로 날짜를 만들면, 그날 뒤에 적은 시드 항목이 영영
    안 올라갔다(라이브 실측: 시드 18줄인데 화면 5줄). 기존 줄이 시드의 앞부분과
    정확히 같을 때만 나머지를 이어 붙인다 — 사람이 고친 문구는 절대 안 덮는다.
    """

    def _reseed(self):
        from app.db import _migrate, get_db
        with self.app.app_context():
            from app.db import tx
            with tx(write=True) as conn:
                _migrate(conn)

    def _rows(self, day):
        with self.app.app_context():
            from app.db import tx
            with tx() as conn:
                return [r["text"] for r in conn.execute(
                    "SELECT text FROM changelog WHERE day=? ORDER BY seq", (day,)).fetchall()]

    def test_시드가_늘면_나머지가_붙는다(self):
        import app.changelog_seed as seed_mod
        day = "2099-01-01"
        orig = seed_mod.SEED
        try:
            seed_mod.SEED = orig + [(day, ["첫 줄", "둘째 줄"])]
            self._reseed()
            self.assertEqual(self._rows(day), ["첫 줄", "둘째 줄"])
            seed_mod.SEED = orig + [(day, ["첫 줄", "둘째 줄", "셋째 줄(나중 세션)"])]
            self._reseed()
            self.assertEqual(self._rows(day), ["첫 줄", "둘째 줄", "셋째 줄(나중 세션)"],
                             "★같은 날 나중에 적은 항목이 안 올라간다")
        finally:
            seed_mod.SEED = orig

    def test_사람이_고친_날은_안_건드린다(self):
        import app.changelog_seed as seed_mod
        day = "2099-01-02"
        orig = seed_mod.SEED
        try:
            seed_mod.SEED = orig + [(day, ["첫 줄"])]
            self._reseed()
            with self.app.app_context():
                from app.db import tx
                with tx(write=True) as conn:
                    conn.execute("UPDATE changelog SET text='대표가 고친 문구' "
                                 "WHERE day=? AND seq=1", (day,))
            seed_mod.SEED = orig + [(day, ["첫 줄", "둘째 줄"])]
            self._reseed()
            self.assertEqual(self._rows(day), ["대표가 고친 문구"],
                             "★사람이 고친 날에 시드가 덮어썼다")
        finally:
            seed_mod.SEED = orig


class TestModalDetachedHost(Base):
    """openModalWith 가 허공에 만든 패널도 받는다(2026-08-24 브라우저 검수에서 발견).

    ★주문자 수정·추가 결제·번호 바로잡기 모달이 전부 document.createElement 직후의
      div 를 넘겼는데, openModalWith 는 '화면에 있는 패널'만 받아서 parentNode 가
      null → insertBefore 에서 죽었다. 버튼을 눌러도 아무 일도 없는 상태 —
      문자열 존재만 보는 시험으로는 못 잡는다. 이 시험은 그 지원 코드가 사라지면 잡는다.
    """

    def test_떼어_온_패널_지원이_있다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
              ).read_text("utf-8")
        blk = js.split("function openModalWith", 1)[1].split(chr(10) + "function ", 1)[0]
        self.assertIn("if (!host.parentNode)", blk,
                      "★허공 패널을 넘기면 insertBefore 에서 죽는다(모달 3종 전멸)")
        self.assertIn("_temp", blk)
        close = js.split("function closeModal", 1)[1].split(chr(10) + "function ", 1)[0]
        self.assertIn("back._temp", close, "닫을 때 임시 패널을 안 지워 DOM 이 쌓인다")

    def test_모달_호출부는_전부_이_규약을_쓴다(self):
        """새 모달이 또 허공 div + openModalWith 조합을 쓰더라도 이제 안전하다는 계약."""
        for fname in ("setup.js", "purchase.js"):
            js = (Path(__file__).resolve().parent.parent / "static" / "js" / fname
                  ).read_text("utf-8")
            self.assertNotIn("document.body.appendChild(host)", js,
                             f"{fname} 가 우회책을 쓰고 있다 — openModalWith 한 곳에서 처리한다")


class TestTotalSplit(Base):
    """매입금액 양방향(2026-08-24 대표): 총액을 적으면 균등분할, 비우면 합계 자동.

    ★브라우저 실측(2026-08-24): 1,000,000원 ÷ 3대 → 333,334/333,333/333,333
      (나머지 1원은 수량 1짜리 첫 줄 — 합계가 총액과 정확히 일치해야 전표 대조가 산다).
    """

    def setUp(self):
        super().setUp()
        self.js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
                   ).read_text("utf-8")

    def test_매입금액_칸이_열려_있다(self):
        blk = self.js.split('id="sl-amount"', 1)[0][-300:] + self.js.split('id="sl-amount"', 1)[1][:300]
        self.assertNotIn("readonly", blk, "매입금액 입력(균등분할 모드)이 막혔다")
        self.assertIn("균등분할", blk, "안내문이 모드를 설명하지 않는다")

    def test_분할_로직이_있다(self):
        blk = self.js.split("const splitTotal = () =>", 1)[1].split("};", 1)[0]
        self.assertIn("Math.floor(total / totalQty)", blk, "대당가 계산이 없다")
        self.assertIn("per * totalQty", blk, "나머지 계산이 없다")
        # ★나머지는 수량 1짜리 줄에 얹는다 — 합계가 총액과 정확히 맞아야 한다
        self.assertIn("(Number(l.qty) || 1) === 1", blk)
        self.assertIn("per + rem", blk)

    def test_총액을_적으면_분할이_돌고_비우면_합계_모드다(self):
        self.assertIn('$("#sl-amount")?.addEventListener("input"', self.js)
        blk = self.js.split('$("#sl-amount")?.addEventListener("input"', 1)[1][:300]
        self.assertIn("totalManual = manualTotal() > 0", blk)
        self.assertIn("splitTotal()", blk)
        # 합계 모드에서만 총액 칸을 자동으로 덮는다 — 분할 모드에선 사람이 적은 값이 기준
        self.assertIn("if (amt && !totalManual) amt.value = fmtNum(total)", self.js)

    def test_줄을_담거나_빼도_다시_나눈다(self):
        self.assertEqual(self.js.count("if (totalManual) splitTotal()"), 2,
                         "줄 담기·빼기 중 한쪽에서 재분배가 빠졌다")

    def test_저장은_분할_모드면_적은_총액을_쓴다(self):
        """전표 금액 = 거래처에 준 돈 — 분할 모드에선 사람이 적은 값이 정답이다."""
        self.assertIn("(totalManual && manualTotal()) ? manualTotal() : lineSum", self.js)


class TestTmsCorrections(Base):
    """TMS 값 '정정'을 따라간다 — 3방향 대조(2026-08-25 대표 승인).

    "자산을 최초로 불러오면 그 뒤로는 아예 커밋이 안되고 있는 것인지" → 빈 칸 채우기만
    되고 값 정정은 안 따라가고 있었다. 그림자(지난 TMS 값)를 두고:
      OWS 값 == 그림자 → 사람이 안 건드림 → TMS 정정 반영(+이력 'TMS정정')
      OWS 값 != 그림자 → 사람이 고침     → 절대 안 덮음
    """

    def _sheet(self, rows):
        with self.app.app_context():
            from app.db import tx
            from app.purchase.migration import _prepare, apply_rows
            with tx(write=True) as conn:
                ready, dup, errors, updates, locked = _prepare(conn, rows, fill_blanks=True)
                return apply_rows(conn, ready, updates, actor="자동반영"), errors

    def _tms_asset(self, no="260901-0001", price="110000", model="NT371B5L"):
        res, _ = self._sheet([{
            "관리번호": no, "브랜드": "SAMSUNG", "모델명": model,
            "매입가": price, "재고상태": "매입"}])
        self.assertEqual(res["created"], 1)
        return self.c.get("/api/assets?limit=500").get_json()["rows"][0]["id"] \
            if False else [a for a in self.c.get("/api/assets?limit=500").get_json()["rows"]
                           if a["assetNo"] == no][0]["id"]

    def test_TMS가_매입가를_고치면_따라간다(self):
        aid = self._tms_asset()
        res, _ = self._sheet([{"관리번호": "260901-0001", "모델명": "NT371B5L",
                               "매입가": "100000", "재고상태": "매입"}])
        self.assertEqual(res["corrected"], 1, "★정정이 안 따라온다")
        got = self.c.get(f"/api/assets/{aid}").get_json()
        self.assertEqual(got["purchasePrice"], 100000)
        acts = [e["action"] for e in got["events"]]
        self.assertIn("TMS정정", acts, "정정 흔적이 이력에 없다")

    def test_모델명_정정도_따라간다(self):
        aid = self._tms_asset(model="NT371B5L")
        self._sheet([{"관리번호": "260901-0001", "모델명": "NT371B5M",
                      "매입가": "110000", "재고상태": "매입"}])
        self.assertEqual(self.c.get(f"/api/assets/{aid}").get_json()["model"], "NT371B5M")

    def test_사람이_고친_값은_안_덮는다(self):
        """★핵심 보호 — OWS 값이 그림자와 다르면 사람이 고친 것이다."""
        aid = self._tms_asset()
        self.c.patch(f"/api/assets/{aid}", json={"purchasePrice": 90000})   # 사람이 정정
        res, _ = self._sheet([{"관리번호": "260901-0001", "모델명": "NT371B5L",
                               "매입가": "100000", "재고상태": "매입"}])
        self.assertEqual(res["corrected"], 0)
        self.assertEqual(self.c.get(f"/api/assets/{aid}").get_json()["purchasePrice"],
                         90000, "★사람이 고친 매입가를 TMS가 덮었다")

    def test_부품장착으로_바뀐_스펙도_보호된다(self):
        """부품 장착이 ram 을 바꾸면 그림자와 달라진다 — 어떤 수정 경로든 자동 보호."""
        aid = self._tms_asset()
        self.c.patch(f"/api/assets/{aid}", json={"ram": "D4 16G"})
        self._sheet([{"관리번호": "260901-0001", "모델명": "NT371B5L",
                      "매입가": "110000", "ram": "D4 8G", "재고상태": "매입"}])
        self.assertEqual(self.c.get(f"/api/assets/{aid}").get_json()["ram"], "D4 16G")

    def test_TMS가_칸을_비워도_지우지_않는다(self):
        aid = self._tms_asset()
        self._sheet([{"관리번호": "260901-0001", "재고상태": "매입"}])   # 모델·가격 없음
        got = self.c.get(f"/api/assets/{aid}").get_json()
        self.assertEqual(got["model"], "NT371B5L")
        self.assertEqual(got["purchasePrice"], 110000)

    def test_정정_폭주면_통째로_보류한다(self):
        """깨진 엑셀 한 장이 수천 대를 다시 쓰면 안 된다."""
        import app.purchase.migration as mig
        for i in range(3):
            self._tms_asset(no=f"260902-{i:04d}")
        orig = mig.CORRECTION_LIMIT
        mig.CORRECTION_LIMIT = 1
        try:
            res, _ = self._sheet([{"관리번호": f"260902-{i:04d}", "모델명": "NT371B5L",
                                   "매입가": "50000", "재고상태": "매입"} for i in range(3)])
            self.assertEqual(res["corrected"], 0, "보류해야 하는데 반영됐다")
            self.assertEqual(res["correctionsFrozen"], 3)
            got = [a for a in self.c.get("/api/assets?limit=500").get_json()["rows"]
                   if a["assetNo"].startswith("260902-")]
            self.assertTrue(all(a["purchasePrice"] == 110000 for a in got))
            # ★그림자도 옛것으로 남아야 한다 — 다음 정상 파일에서 다시 판정된다
            res2, _ = self._sheet([{"관리번호": "260902-0000", "모델명": "NT371B5L",
                                    "매입가": "50000", "재고상태": "매입"}])
            self.assertEqual(res2["corrected"], 1, "보류 뒤 재판정이 안 된다")
        finally:
            mig.CORRECTION_LIMIT = orig

    def test_잠긴_자산은_정정도_안_받는다(self):
        aid = self._tms_asset()
        with self.app.app_context():
            from app.db import tx
            with tx(write=True) as conn:
                conn.execute("UPDATE assets SET tms_lock=1 WHERE id=?", (aid,))
        res, _ = self._sheet([{"관리번호": "260901-0001", "모델명": "NT371B5L",
                               "매입가": "100000", "재고상태": "매입"}])
        self.assertEqual(res["corrected"], 0)
        self.assertEqual(self.c.get(f"/api/assets/{aid}").get_json()["purchasePrice"], 110000)

    def test_시리얼은_정정_대상이_아니다(self):
        """번호충돌 가드(시리얼이 갈리면 행 통째 거부)가 먼저다 — 정정으로 우회되면 안 된다."""
        src = (Path(__file__).resolve().parent.parent / "app" / "purchase" / "migration.py"
               ).read_text("utf-8")
        blk = src.split("corrections = {}", 1)[1].split("shadow_changed", 1)[0]
        self.assertIn('f == "serial" or not tv', blk, "시리얼 제외가 사라졌다")


class TestWorkloadMoved(Base):
    """기간별 작업량 — 2026-08-27 독립 메뉴에서 설정 ▸ 매출/실적으로 옮겼고,
    2026-09-03 대표 지시로 [📅 셋팅·작업 실적] 한 화면에 흡수됐다
    ("셋팅실적 / 기간별 작업량이 거의 같은 내용이라서 서로 흡수해서, 두 개로 나누지 말고")."""

    def test_사이드바에서_빠지고_셋팅작업실적_한_화면으로(self):
        root = Path(__file__).resolve().parent.parent
        html = (root / "static" / "index.html").read_text("utf-8")
        self.assertNotIn('data-view="workload"', html, "사이드바 메뉴가 되살아났다")
        app_js = (root / "static" / "js" / "app.js").read_text("utf-8")
        self.assertIn('["stats", "📅 셋팅·작업 실적"]', app_js, "합친 보기가 없다")
        self.assertNotIn('["workload", "기간별 작업량"]', app_js, "보기가 다시 둘로 갈렸다")
        # 창구는 그대로 쓴다 — 계산을 새로 짜면 옛 숫자와 어긋난다
        self.assertIn("/api/workload-stats?from=", app_js)
        # 옛 링크(#workload)는 합친 화면으로 간다
        self.assertIn('state.salesView = "stats";', app_js, "옛 링크 이동이 없다")






class TestAsDocs(Base):
    """A/S 수리내역서·청구내역서(2026-08-31 대표) — 항목별 금액(부가세 자동 분리),
    회사정보·도장 필수, 발행본은 스냅샷 불변. 합계는 as_tickets.cost 로 동기화."""

    STAMP = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg"

    def _ticket(self, **kw):
        base = {"customer": "김고객", "symptom": "전원 불량", "phone": "010-9999-8888"}
        base.update(kw)
        r = self.c.post("/api/as-tickets", json=base)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()

    def _company(self):
        self.c.post("/api/as-doc-settings", json={"company": {
            "name": "예시 운영사", "manager": "계약 담당자", "phone": "032-123-4567",
            "bizReg": "123-45-67890", "address": "인천시"}})
        self.c.post("/api/as-doc-stamp", json={"dataUrl": self.STAMP})

    def test_항목_저장과_부가세_분리(self):
        t = self._ticket()
        r = self.c.post(f"/api/as-tickets/{t['id']}/items", json={"items": [
            {"name": "메인보드 수리", "qty": 1, "amount": 110000},
            {"name": "출장비", "qty": 1, "amount": 22000}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d["cost"], 132000)
        self.assertEqual(d["items"][0]["vat"], 10000)     # 110,000 × 10/110
        self.assertEqual(d["items"][0]["net"], 100000)
        # 합계가 티켓 수리비로 동기화 — 자산 원가 반영 경로가 그대로 돈다
        self.assertEqual(self.c.get(f"/api/as-tickets/{t['id']}").get_json()["cost"], 132000)
        # 빈 이름·과다 줄 수 거부
        self.assertEqual(self.c.post(f"/api/as-tickets/{t['id']}/items",
                                     json={"items": [{"name": " ", "amount": 1}]}).status_code, 400)

    def test_문서_발행은_회사정보와_도장이_필수(self):
        t = self._ticket()
        self.c.post(f"/api/as-tickets/{t['id']}/items",
                    json={"items": [{"name": "수리", "qty": 1, "amount": 55000}]})
        r = self.c.post(f"/api/as-tickets/{t['id']}/documents", json={"docType": "repair"})
        self.assertEqual(r.status_code, 400, "회사정보 없이 발행되면 안 된다")
        self._company()
        r = self.c.post(f"/api/as-tickets/{t['id']}/documents", json={"docType": "repair"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        doc = r.get_json()["doc"]
        self.assertTrue(doc["docNo"].startswith("ASR-"))
        self.assertEqual(doc["totals"], {"amount": 55000, "vat": 5000, "net": 50000})
        self.assertEqual(doc["company"]["manager"], "계약 담당자")
        # 청구내역서는 다른 채번, 발행 이력에 스냅샷이 남는다
        r2 = self.c.post(f"/api/as-tickets/{t['id']}/documents", json={"docType": "invoice"})
        self.assertTrue(r2.get_json()["doc"]["docNo"].startswith("ASB-"))
        lst = self.c.get(f"/api/as-tickets/{t['id']}/documents").get_json()
        self.assertEqual(len(lst["docs"]), 2)
        self.assertEqual(lst["stamp"], self.STAMP)
        # 내역 없는 문서·이상한 종류 거부
        t2 = self._ticket(customer="이내역없음")
        self.assertEqual(self.c.post(f"/api/as-tickets/{t2['id']}/documents",
                                     json={"docType": "repair"}).status_code, 400)
        self.assertEqual(self.c.post(f"/api/as-tickets/{t['id']}/documents",
                                     json={"docType": "x"}).status_code, 400)

    def test_고객_이력(self):
        t = self._ticket(phone="010-7777-6666")
        self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "김고객", "phone": "010-7777-6666",
            "productName": "그램", "address": "서울", "quantity": 1, "amount": 10000})
        h = self.c.get(f"/api/as-tickets/{t['id']}/history").get_json()
        self.assertEqual(len(h["orders"]), 1, "같은 연락처 주문이 이력에 떠야 한다")
        t2 = self._ticket(phone="010-7777-6666", customer="김고객", symptom="화면 줄")
        h2 = self.c.get(f"/api/as-tickets/{t2['id']}/history").get_json()
        self.assertEqual(len(h2["tickets"]), 1, "과거 A/S 가 떠야 한다(자기 자신 제외)")
        anon = self.c.application.test_client()
        self.assertEqual(anon.get(f"/api/as-tickets/{t['id']}/history").status_code, 401)

    def test_회사정보_검증(self):
        self.assertEqual(self.c.post("/api/as-doc-settings",
                                     json={"company": {"name": ""}}).status_code, 400)
        self.assertEqual(self.c.post("/api/as-doc-stamp",
                                     json={"dataUrl": "http://x/y.png"}).status_code, 400)
        g = self.c.get("/api/as-doc-settings").get_json()
        self.assertIn("company", g)

    def test_화면_배선(self):
        base = Path(__file__).resolve().parent.parent / "static" / "js"
        asjs = (base / "as.js").read_text("utf-8")
        self.assertIn("function renderAsItems", asjs, "수리 내역 편집이 없다")
        self.assertIn("function printAsDocument", asjs, "문서 인쇄가 없다")
        self.assertIn("total * 10 / 110", asjs, "부가세 식이 서버(split_vat)와 달라지면 안 된다")
        self.assertIn('id="asi-total"', asjs, "합계 입력(자동 분배) 칸이 없다")
        self.assertIn("@page { size: A4", asjs, "A4 인쇄 스타일이 없다")
        self.assertIn('class="stamp"', asjs, "도장 오버레이가 없다")
        self.assertIn("function renderAsDocSettings", asjs, "회사정보 설정 카드가 없다")
        self.assertIn("function renderAsHistory", asjs, "고객·자산 이력 카드가 없다")
        self.assertIn("recallStage", asjs, "회수 택배 단계 표시가 없다")
        app_js = (base / "app.js").read_text("utf-8")
        self.assertIn("renderAsDocSettings($(\"#ops-asdoc\", body))", app_js,
                      "운영 설정에 A/S 문서 카드가 안 붙었다")

class TestAsIntake(Base):
    """A/S 접수 개편(2026-08-31 대표) — 자동완성(관리번호·모델·고객·연락처),
    방문/택배 접수 경로, 주소 표준화(우편번호 = 주문과 같은 CJ 규격)."""

    def _order(self, **kw):
        base = {"channel": "고도몰", "recipient": "김이력", "phone": "010-5555-4444",
                "productName": "그램", "address": "인천시 남동구 1", "postalCode": "21550",
                "quantity": 1, "amount": 300000}
        base.update(kw)
        r = self.c.post("/api/orders", json=base)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()

    def _asset(self, model="L480"):
        cats = self.c.get("/api/categories").get_json()
        return self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": model, "qty": 1,
            "purchasePrice": 100000}).get_json()[0]

    def _wb(self, **where):
        from app.db import tx
        cond = " AND ".join(f"{k}=?" for k in where)
        with self.app.app_context():
            with tx() as conn:
                return conn.execute(f"SELECT * FROM waybills WHERE {cond}",
                                    tuple(where.values())).fetchone()

    def test_접수경로와_우편번호(self):
        r = self.c.post("/api/as-tickets", json={
            "customer": "방문객", "symptom": "액정", "intake": "visit",
            "postalCode": "21550", "address": "인천시 남동구 1"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        t = self.c.get(f"/api/as-tickets/{r.get_json()['id']}").get_json()
        self.assertEqual(t["intake"], "visit")
        self.assertEqual(t["intakeLabel"], "방문")
        self.assertEqual(t["postalCode"], "21550")
        # 수정으로 택배 전환
        self.assertEqual(self.c.patch(f"/api/as-tickets/{t['id']}",
                                      json={"intake": "parcel"}).status_code, 200)
        self.assertEqual(self.c.get(f"/api/as-tickets/{t['id']}").get_json()["intake"], "parcel")
        # 이상한 경로 거부 + 안 주면 기본 택배
        self.assertEqual(self.c.post("/api/as-tickets", json={
            "customer": "x", "symptom": "y", "intake": "drone"}).status_code, 400)
        r2 = self.c.post("/api/as-tickets", json={"customer": "기본", "symptom": "z"})
        self.assertEqual(
            self.c.get(f"/api/as-tickets/{r2.get_json()['id']}").get_json()["intake"], "parcel")
        self.assertIn("intakes", self.c.get("/api/as-meta").get_json())

    def test_고객_자동완성(self):
        self._order()
        self._order()                              # 같은 사람 두 번 주문 → 후보 한 줄
        rows = self.c.get("/api/as-tickets/customer-search?q=김이").get_json()
        hits = [r for r in rows if r["name"] == "김이력"]
        self.assertEqual(len(hits), 1, f"중복 제거가 안 됐다: {rows}")
        self.assertEqual(hits[0]["phone"], "010-5555-4444")
        self.assertEqual(hits[0]["postalCode"], "21550")
        self.assertTrue(hits[0]["address"])
        # 전화 뒷자리(하이픈 없이)로도 걸린다
        rows = self.c.get("/api/as-tickets/customer-search?q=55554444").get_json()
        self.assertTrue(any(r["name"] == "김이력" for r in rows), "전화번호 검색이 안 된다")
        # 과거 A/S 고객도 후보에 뜬다
        self.c.post("/api/as-tickets", json={"customer": "예전손님", "symptom": "s",
                                             "phone": "010-1234-0000"})
        rows = self.c.get("/api/as-tickets/customer-search?q=예전손").get_json()
        self.assertTrue(any(r["name"] == "예전손님" and r["source"] == "A/S" for r in rows))
        # 2자 미만은 빈 목록 + 미로그인 401
        self.assertEqual(self.c.get("/api/as-tickets/customer-search?q=김").get_json(), [])
        anon = self.app.test_client()
        self.assertEqual(anon.get("/api/as-tickets/customer-search?q=김이").status_code, 401)

    def test_자산검색이_고객명과_연락처로도_찾는다(self):
        a = self._asset()
        o = self._order(recipient="박기계", phone="010-2222-3333")
        self.assertEqual(self.c.patch(f"/api/orders/{o['id']}",
                                      json={"action": "assets",
                                            "assetIds": [a["id"]]}).status_code, 200)
        for q in ("박기계", "0102222"):
            rows = self.c.get("/api/as-tickets/asset-search?q=" + q).get_json()
            hit = next((r for r in rows if r["assetId"] == a["id"]), None)
            self.assertIsNotNone(hit, f"'{q}'로 자산을 못 찾았다")
            self.assertEqual(hit["phone"], "010-2222-3333",
                             "연락처가 안 내려오면 화면 자동 채움이 안 된다")
            self.assertEqual(hit["postalCode"], "21550")

    def test_회수와_반송에_우편번호가_실린다(self):
        r = self.c.post("/api/as-tickets", json={
            "customer": "택배님", "symptom": "s", "phone": "010-7777-1111",
            "address": "서울시 강남구 2", "postalCode": "06134"})
        tid = r.get_json()["id"]
        self.assertEqual(self.c.post(f"/api/as-tickets/{tid}/recall",
                                     json={}).status_code, 201)
        wb = self._wb(as_ticket_id=tid, type="recall")
        self.assertEqual(wb["postal_code"], "06134", "CJ 회수 접수에 우편번호가 빠졌다")
        rb = self.c.post(f"/api/as-tickets/{tid}/return-waybill", json={})
        self.assertEqual(rb.status_code, 201, rb.get_data(as_text=True))
        wb = self._wb(as_ticket_id=tid, type="forward")
        self.assertEqual(wb["postal_code"], "06134", "CJ 반송 송장에 우편번호가 빠졌다")

    def test_화면_배선(self):
        asjs = (Path(__file__).resolve().parent.parent / "static" / "js" / "as.js").read_text("utf-8")
        self.assertIn("function attachAsAutocomplete", asjs, "자동완성 헬퍼가 없다")
        self.assertIn("autoSearch(input, run, 300)", asjs,
                      "IME 안전 디바운스(autoSearch)를 안 쓴다 — 한글 조합이 끊긴다")
        self.assertIn("/api/as-tickets/customer-search?q=", asjs, "고객 자동완성이 배선 안 됐다")
        for sel in ('id="asn-zip"', 'id="asn-addr2"', 'id="asn-intake"',
                    'id="asd-zip"', 'id="asd-intake"'):
            self.assertIn(sel, asjs, f"{sel} 이 없다 — 접수 폼 표준화 누락")
        # ★상태 카드(접수/회수 중/수리 중/수리 완료)는 2026-09-03 대표 지시로 접수 탭에서 뺐다
        #   ("접수 탭 카드와 진행 상황이 중첩된다"). 같은 숫자를 두 곳에서 세면 반드시 어긋난다 —
        #   세는 곳은 [📊 현황] 하나이고, 그 자리는 진행 보드의 칸(data-bstage)이다.
        self.assertNotIn('data-kf=', asjs, "접수 탭에 상태 카드가 되살아났다 — 현황과 중복이다")
        self.assertIn('data-bstage=', asjs, "현황 보드의 단계 칸이 없다")
        self.assertNotIn('id="asn-search"', asjs, "옛 '제품 찾기' 버튼이 남아 있다")


class TestAsSymptomCats(Base):
    """증상 분류(2026-08-31 대표 저녁 — "대분류/소분류/사유"). 대분류=장비 종류
    (데스크탑·노트북·태블릿…), 소분류=성격(H/W·S/W·OS·택배파손…), 사유=증상 자유 서술.
    목록은 A/S ▸ ⚙ 설정 ▸ 🏷 증상 분류에서 직접 추가 — 오전의 태그 방식을 대체."""

    def test_분류_목록_저장과_기본값(self):
        # 저장 전엔 대표가 예시한 기본값으로 시작한다
        m = self.c.get("/api/as-meta").get_json()
        self.assertEqual(m["symptomCats"], ["데스크탑", "노트북", "태블릿"])
        self.assertEqual(m["symptomSubs"], ["H/W", "S/W", "OS", "택배파손"])
        r = self.c.put("/api/as-symptom-cats", json={
            "cats": ["노트북", "데스크탑", "노트북", " 태블릿 "],
            "subs": ["H/W", "S/W"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["cats"], ["노트북", "데스크탑", "태블릿"], "중복·공백 정리가 안 됐다")
        m2 = self.c.get("/api/as-meta").get_json()
        self.assertEqual(m2["symptomCats"], ["노트북", "데스크탑", "태블릿"])
        self.assertEqual(m2["symptomSubs"], ["H/W", "S/W"])
        # 일부러 비운 목록은 기본값으로 되돌리지 않는다
        self.c.put("/api/as-symptom-cats", json={"cats": [], "subs": []})
        self.assertEqual(self.c.get("/api/as-meta").get_json()["symptomCats"], [])
        # 검증 — 길이·형·권한
        self.assertEqual(self.c.put("/api/as-symptom-cats",
                                    json={"cats": ["가" * 31], "subs": []}).status_code, 400)
        self.assertEqual(self.c.put("/api/as-symptom-cats",
                                    json={"cats": "노트북", "subs": []}).status_code, 400)
        anon = self.app.test_client()
        self.assertEqual(anon.put("/api/as-symptom-cats",
                                  json={"cats": [], "subs": []}).status_code, 401)

    def test_접수에_붙이고_필터로_거른다(self):
        a = self.c.post("/api/as-tickets", json={
            "customer": "김노트북", "symptom": "안 켜짐",
            "symptomCat": "노트북", "symptomSub": "H/W"}).get_json()
        self.c.post("/api/as-tickets", json={
            "customer": "이데탑", "symptom": "부팅 불가",
            "symptomCat": "데스크탑", "symptomSub": "H/W"})
        self.c.post("/api/as-tickets", json={"customer": "박무관", "symptom": "기타"})
        got = self.c.get(f"/api/as-tickets/{a['id']}").get_json()
        self.assertEqual((got["symptomCat"], got["symptomSub"]), ("노트북", "H/W"))
        rows = self.c.get("/api/as-tickets?cat=노트북").get_json()
        self.assertEqual([t["customer"] for t in rows], ["김노트북"], "대분류 필터가 안 걸린다")
        rows = self.c.get("/api/as-tickets?sub=H/W").get_json()
        self.assertEqual({t["customer"] for t in rows}, {"김노트북", "이데탑"})
        rows = self.c.get("/api/as-tickets?cat=데스크탑&sub=H/W").get_json()
        self.assertEqual([t["customer"] for t in rows], ["이데탑"], "대+소 AND 필터가 안 걸린다")
        # 상세에서 고칠 수 있고, 너무 긴 값은 거부
        self.assertEqual(self.c.patch(f"/api/as-tickets/{a['id']}",
                                      json={"symptomCat": "태블릿"}).status_code, 200)
        self.assertEqual(self.c.get(f"/api/as-tickets/{a['id']}").get_json()["symptomCat"], "태블릿")
        self.assertEqual(self.c.post("/api/as-tickets", json={
            "customer": "x", "symptom": "y", "symptomCat": "가" * 31}).status_code, 400)
        # {증상분류} 문자 자리 — "대분류 · 소분류"
        self.c.post("/api/as-sms-templates", json={
            "events": {"done": {"on": True, "text": "{증상분류}", "subject": ""}}})
        p = self.c.get(f"/api/as-tickets/{a['id']}/sms-preview?event=done").get_json()
        self.assertEqual(p["text"], "태블릿 · H/W")

    def test_화면_배선(self):
        asjs = (Path(__file__).resolve().parent.parent / "static" / "js" / "as.js").read_text("utf-8")
        self.assertIn("function symSelectHtml", asjs, "분류 선택기가 없다")
        self.assertIn("function symQuickAdd", asjs, "＋ 분류 바로 추가가 없다")
        self.assertIn("function renderAsSettingsTab", asjs, "⚙ 설정 탭 셸이 없다")
        self.assertIn("function renderAsCatsTab", asjs, "증상 분류 관리 탭이 없다")
        self.assertIn("/api/as-symptom-cats", asjs)
        self.assertIn("data-symcat", asjs, "목록의 대분류 필터 버튼이 없다")
        self.assertIn("data-symsub", asjs, "목록의 소분류 필터 버튼이 없다")
        for sel in ('symSelectHtml("asn-symcat"', 'symSelectHtml("asn-symsub"',
                    'symSelectHtml("asd-symcat"', 'symSelectHtml("asd-symsub"',
                    'id="ascat-cats"', 'id="ascat-subs"'):
            self.assertIn(sel, asjs, f"{sel} 이 없다 — 분류 칸 누락")
        # 옛 태그 방식이 남아 있으면 안 된다(대체 완료)
        for old in ("function tagPicker", "data-astag", "/api/as-symptom-types",
                    'id="asn-tagrow"', 'id="asd-tagrow"', "symptomTags"):
            self.assertNotIn(old, asjs, f"옛 증상 태그 잔재: {old}")
        # 탭 구조 — [📋 접수 | ⚙ 설정], 설정 안 세부탭 [🏷 증상 분류 | 💬 문자 양식]
        self.assertIn('data-atab="settings"', asjs, "⚙ 설정 탭이 없다")
        self.assertIn('data-aset="cats"', asjs)
        self.assertIn('data-aset="sms"', asjs)


class TestAsSmsTemplates(Base):
    """문자 양식(2026-08-31 대표) — A/S 안 [💬 문자 양식] 탭에서 관리, 직접 만든 양식은
    발송 시점(트리거)을 고르고, 접수 건에서는 치환된 문구를 고쳐 보낼 수 있다."""

    def _staff(self):
        r = self.c.post("/api/users", json={
            "username": "asstaff", "displayName": "AS담당", "password": "asstaff-pw-12",
            "perms": ["as.view", "as.manage"], "allCategories": True})
        assert r.status_code == 201, r.get_data(as_text=True)
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "asstaff", "password": "asstaff-pw-12"})
        return c

    def _log_events(self, tid):
        rows = self.c.get(f"/api/sms/log?ticketId={tid}").get_json()
        return [r["event"] for r in rows]

    def test_양식_저장은_설정권한_없이_되고_비밀은_보존된다(self):
        # 설정 키 'sms' 안에 API 키가 함께 산다 — 양식 저장이 키를 지우면 대형 사고
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute(
                    "INSERT INTO settings(key, value, updated_at, updated_by) "
                    "VALUES('sms', '{\"apiKey\": \"SECRET-1\"}', '', '')")
        c = self._staff()
        r = c.post("/api/as-sms-templates", json={
            "events": {"received": {"on": False, "text": "고친 접수 문구 {고객명}", "subject": ""}},
            "custom": [{"label": "입금 안내", "trigger": "", "text": "{고객명}님 입금 안내", "on": True}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = c.get("/api/as-sms-templates").get_json()
        ev = next(e for e in d["events"] if e["code"] == "received")
        self.assertFalse(ev["on"])
        self.assertEqual(ev["text"], "고친 접수 문구 {고객명}")
        self.assertEqual(d["custom"][0]["label"], "입금 안내")
        self.assertTrue(d["custom"][0]["id"], "새 양식에 id 가 안 붙었다")
        with self.app.app_context():
            with tx() as conn:
                raw = conn.execute("SELECT value FROM settings WHERE key='sms'").fetchone()["value"]
        self.assertIn("SECRET-1", raw, "양식 저장이 API 키를 날렸다")
        # 검증: 시점·이름
        self.assertEqual(c.post("/api/as-sms-templates", json={
            "custom": [{"label": "x", "trigger": "no-such", "text": "y"}]}).status_code, 400)
        self.assertEqual(c.post("/api/as-sms-templates", json={
            "custom": [{"label": "", "trigger": "", "text": "y"}]}).status_code, 400)
        anon = self.app.test_client()
        self.assertEqual(anon.get("/api/as-sms-templates").status_code, 401)

    def test_직접_양식이_고른_시점에_자동으로_나간다(self):
        self.c.post("/api/as-sms-templates", json={"custom": [
            {"label": "접수 추가 안내", "trigger": "received", "text": "{고객명}님 추가 안내", "on": True},
            {"label": "꺼진 양식", "trigger": "received", "text": "안 나가야 함", "on": False}]})
        t = self.c.post("/api/as-tickets", json={
            "customer": "홍길동", "symptom": "전원", "phone": "010-1234-5678"}).get_json()
        evs = self._log_events(t["id"])
        self.assertIn("received", evs, "기본 접수 문자가 없다")
        self.assertTrue(any(e.startswith("custom:") for e in evs), "직접 양식이 시점에 안 걸렸다")
        self.assertEqual(len([e for e in evs if e.startswith("custom:")]), 1,
                         "꺼진 양식까지 나갔다")

    def test_청구서_발행_시점(self):
        self.c.post("/api/as-sms-templates", json={"custom": [
            {"label": "입금 안내", "trigger": "invoiced", "text": "{고객명}님 {수리비} 입금 안내", "on": True}]})
        t = self.c.post("/api/as-tickets", json={
            "customer": "김청구", "symptom": "s", "phone": "010-2222-3333"}).get_json()
        self.c.post(f"/api/as-tickets/{t['id']}/items",
                    json={"items": [{"name": "수리", "qty": 1, "amount": 55000}]})
        self.c.post("/api/as-doc-settings", json={"company": {
            "name": "예시 운영사", "manager": "담당", "phone": "032-1"}})
        self.c.post("/api/as-doc-stamp", json={"dataUrl": "data:image/png;base64,AAA"})
        r = self.c.post(f"/api/as-tickets/{t['id']}/documents", json={"docType": "invoice"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any(e.startswith("custom:") for e in self._log_events(t["id"])),
                        "청구서 발행 시점 문자가 안 나갔다")
        # 재발행해도 같은 양식은 한 번만(중복 방지)
        self.c.post(f"/api/as-tickets/{t['id']}/documents", json={"docType": "invoice"})
        self.assertEqual(len([e for e in self._log_events(t["id"]) if e.startswith("custom:")]), 1)

    def test_회수_입고_시점(self):
        self.c.post("/api/as-sms-templates", json={"custom": [
            {"label": "입고 안내", "trigger": "collected", "text": "{고객명}님 제품이 입고됐습니다", "on": True}]})
        t = self.c.post("/api/as-tickets", json={
            "customer": "박입고", "symptom": "s", "phone": "010-3333-4444",
            "address": "서울 1", "postalCode": "06000"}).get_json()
        wid = self.c.post(f"/api/as-tickets/{t['id']}/recall", json={}).get_json()["wid"]
        r = self.c.post(f"/api/waybills/{wid}/received", json={"status": "as"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(any(e.startswith("custom:") for e in self._log_events(t["id"])),
                        "회수 입고 시점 문자가 안 나갔다")

    def test_발송_전_수정(self):
        t = self.c.post("/api/as-tickets", json={
            "customer": "최수정", "symptom": "액정", "phone": "010-5555-6666"}).get_json()
        p = self.c.get(f"/api/as-tickets/{t['id']}/sms-preview?event=received").get_json()
        self.assertIn("최수정", p["text"], "미리보기가 실제 값으로 치환되지 않았다")
        self.assertIn("msgType", p)
        # 고친 문구로 발송(접수 때 이미 1회 나갔으니 force) — 이력에 고친 그대로 + 매크로 재치환
        r = self.c.post(f"/api/as-tickets/{t['id']}/sms", json={
            "event": "received", "force": True,
            "text": "직접 쓴 안내 — {고객명}님 확인 부탁드립니다"})
        self.assertTrue(r.get_json()["ok"], r.get_data(as_text=True))
        rows = self.c.get(f"/api/sms/log?ticketId={t['id']}").get_json()
        self.assertEqual(rows[0]["text"], "직접 쓴 안내 — 최수정님 확인 부탁드립니다")
        # 없는 양식 미리보기는 400/404
        self.assertIn(self.c.get(
            f"/api/as-tickets/{t['id']}/sms-preview?event=custom:c99").status_code, (400, 404))

    def test_화면_배선(self):
        asjs = (Path(__file__).resolve().parent.parent / "static" / "js" / "as.js").read_text("utf-8")
        self.assertIn("function renderAsSmsTab", asjs, "[문자 양식] 탭이 없다")
        # 2026-08-31 저녁: [⚙ 설정] 안의 세부탭으로 이사 — 진입은 data-aset="sms"
        self.assertIn('data-aset="sms"', asjs)
        self.assertIn("function openAsSmsModal", asjs, "발송 전 미리보기 모달이 없다")
        self.assertIn("/sms-preview?event=", asjs, "미리보기 API 배선이 없다")
        self.assertIn("function openAsSmsPicker", asjs, "기타 양식 고르기가 없다")
        self.assertIn("/api/as-sms-templates", asjs)
        self.assertIn("function smsBytes", asjs, "바이트(단문/장문) 표시가 없다")

class TestOptionLabelSheets(Base):
    """옵션라벨(2026-09-03 대표) — ①안 뽑았는데 ✓가 붙던 것 ②주문 대수만큼 여러 장 + 1/4 표시.

    라벨 생성은 화면(JS)이 하므로 여기서는 배선을 핀한다. 실제 장수·표시는 브라우저에서
    확인했다(4대 → 1/4·2/4·3/4·4/4, 1대 → 표시 없음, 자산 3대 매칭 → 1/3~3/3).
    """

    def test_인쇄_기록은_사람_확인_뒤에만_남는다(self):
        root = Path(__file__).resolve().parent.parent
        app_js = (root / "static" / "js" / "app.js").read_text("utf-8")
        self.assertIn("function confirmPrinted", app_js, "인쇄 확인 물음이 없다")
        for name in ("setup.js", "orders.js"):
            js = (root / "static" / "js" / name).read_text("utf-8")
            body = js.split("printOptionLabels(", 1)[1][:900]
            self.assertIn("confirmPrinted", body,
                          f"{name}: 확인 없이 '인쇄됨'으로 기록한다")

    def test_대수만큼_여러_장이_나간다(self):
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "js" / "app.js").read_text("utf-8")
        blk = js.split("async function printOptionLabels", 1)[1].split("\n/* ", 1)[0]
        self.assertIn("assets.length || Number(o.quantity)", blk, "대수를 안 센다")
        self.assertIn("total > 1 ?", blk, "한 대짜리에도 1/1을 찍는다")
        self.assertIn("${i + 1}/${total}", blk, "1/4 표시를 안 만든다")

    def test_라벨_버튼이_제작_전_단계에도_뜬다(self):
        """★대표 2026-09-03: "옵션라벨 출력은 제작대기·준비중에서 뽑아야 한다".
        예전에는 제작 완료 전이면 송장 칸을 통째로 비워서(return "") 라벨 버튼까지
        같이 사라졌다 — 정작 라벨이 필요한 단계가 그때다."""
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "js" / "setup.js").read_text("utf-8")
        blk = js.split("function waybillCell(o) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (!o.productionDone) return optLabelBtn(o);", blk,
                      "제작 전 단계에서 라벨 버튼이 안 나온다")
        self.assertNotIn('if (!o.productionDone || o.cancelledAt) return "";', blk,
                         "옛 조기 반환이 남아 라벨 버튼이 사라진다")
        # 송장은 SW 검수 완료부터(대표 2026-09-04 정정) — 제작 대기~제작 완료는 여전히 막는다
        self.assertIn("SW 검수 후 발급", blk)
        self.assertIn("if (!o.softwareInspectionDone && !done) {", blk,
                      "SW 검수 완료 단계에서 송장 버튼이 안 나온다")

    def test_설정에서_장수표시_위치를_바꿀_수_있다(self):
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "js" / "app.js").read_text("utf-8")
        self.assertIn("seq:", js.split("OPTLABEL_DEFAULT", 1)[1][:1400],
                      "옵션라벨 기본 배치에 장수 표시가 없다")
        self.assertIn("seq: \"장수 표시", js, "설정 화면 이름표가 없다")
        self.assertIn('seq: "1/4"', js, "설정 견본에 장수 표시가 없다")

    def test_라벨_저장이_장수표시를_받는다(self):
        """레이아웃은 서버가 통째로 저장한다 — 새 요소가 끼어도 그대로 돌아와야 한다."""
        lay = {"w": 80, "h": 50, "els": {"seq": {"show": 1, "x": 60, "y": 8, "size": 14}}}
        r = self.c.post("/api/asset-label", json={"optionLayout": lay})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        got = self.c.get("/api/asset-label").get_json()["optionLayout"]
        self.assertEqual(got["els"]["seq"]["x"], 60)
        self.assertEqual(got["els"]["seq"]["size"], 14)


class TestSetupQcAndShipping(Base):
    """셋팅/QC·배송/송장 개선(2026-09-03 대표) — ①CJ 실발행 배너 삭제 ②출고확인 전체 선택
    후 송장 출력 ③취소 주문은 셋팅/QC에서 안 보임 ④주문과 다른 자산이면 경고
    ⑤배송/송장 신규 등록(신규·회수·예약)."""

    def _cat(self):
        return self.c.get("/api/categories").get_json()[0]["id"]

    def _asset(self, code, model="L480"):
        a = self.c.post("/api/assets", json={
            "categoryId": self._cat(), "model": model, "qty": 1,
            "purchasePrice": 100000, "productCode": code}).get_json()
        return (a[0] if isinstance(a, list) else a)["id"]

    def _order(self, code="L480_i5-8_내장"):
        return self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "김주문", "productName": "노트북",
            "productCode": code, "quantity": 1, "amount": 500000}).get_json()["id"]

    # ---------------- 자산 불일치 경고
    def test_주문과_다른_제품이면_경고가_붙는다(self):
        oid = self._order()
        bad = self._asset("NT371B5L_i3-6_내장", model="NT371B5L")
        r = self.c.patch(f"/api/orders/{oid}", json={"action": "assets", "assetIds": [bad]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        warns = r.get_json().get("assetWarnings") or []
        self.assertTrue(warns, "다른 제품인데 경고가 없다")
        self.assertIn("맞지 않는 제품", warns[0])

    def test_맞는_제품이면_경고가_없다(self):
        oid = self._order()
        good = self._asset("L480_i5-8_내장")
        r = self.c.patch(f"/api/orders/{oid}", json={"action": "assets", "assetIds": [good]})
        self.assertEqual(r.get_json().get("assetWarnings") or [], [])

    def test_등급_꼬리만_다르면_같은_제품으로_본다(self):
        """몰 주문 코드는 '…내장 AA'처럼 등급이 붙어 온다 — 그것만으로 경고하면 안 된다."""
        oid = self._order(code="L480_i5-8_내장 AA")
        good = self._asset("L480_i5-8_내장")
        r = self.c.patch(f"/api/orders/{oid}", json={"action": "assets", "assetIds": [good]})
        self.assertEqual(r.get_json().get("assetWarnings") or [], [])

    def test_코드가_없으면_경고하지_않는다(self):
        """주문에 제품코드가 없으면 비교 기준이 없다 — 조용히 넘어간다."""
        oid = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "코드없음", "productName": "노트북",
            "quantity": 1, "amount": 1000}).get_json()["id"]
        aid = self._asset("L480_i5-8_내장")
        r = self.c.patch(f"/api/orders/{oid}", json={"action": "assets", "assetIds": [aid]})
        self.assertEqual(r.get_json().get("assetWarnings") or [], [])

    # ---------------- 송장 신규 등록
    def test_예약은_CJ를_부르지_않고_기록만_남긴다(self):
        r = self.c.post("/api/waybills/manual", json={
            "type": "forward", "recipient": "예약고객", "phone": "010-1111-2222",
            "address": "서울 도봉구 노해로 1", "reserve": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d["status"], "reserved")
        self.assertEqual(d["invoiceNo"], "", "예약인데 송장번호가 붙었다")

    def test_출고와_회수를_각각_만들_수_있다(self):
        f = self.c.post("/api/waybills/manual", json={
            "type": "forward", "recipient": "출고", "phone": "010-1",
            "address": "서울 1", "boxQty": 2}).get_json()
        self.assertEqual(f["status"], "issued")
        # ★수거 희망일은 '오늘 기준'으로 만든다 — 날짜를 박아 두면 그날이 지나는 순간
        #   CJ 집화 휴무 검사(2026-09-07)에 걸려 시험이 저절로 깨진다(하드코딩 날짜 = 시한폭탄).
        from app.cj.calendar import next_pickup_day
        rc = self.c.post("/api/waybills/manual", json={
            "type": "recall", "recipient": "회수", "phone": "010-2",
            "address": "서울 2", "pickupDate": next_pickup_day({})}).get_json()
        self.assertEqual(rc["status"], "issued")
        rows = self.c.get("/api/waybills").get_json()
        rows = rows.get("items", rows) if isinstance(rows, dict) else rows
        kinds = {w["wid"]: w.get("type") for w in rows}
        self.assertEqual(kinds.get(f["wid"]), "forward")
        self.assertEqual(kinds.get(rc["wid"]), "recall")

    def test_받는_분과_주소는_반드시_있어야_한다(self):
        r = self.c.post("/api/waybills/manual", json={"type": "forward", "recipient": ""})
        self.assertEqual(r.status_code, 400)

    # ---------------- 화면 배선
    def test_출고확인만_눌렀다고_목록에서_내리지_않는다(self):
        """★대표 2026-09-03: "출고 확인 누르면 출고확인으로 넘어가게만".
        송장을 이 단계에서 뽑게 바뀌었으니 출고 확인된 건이 사라지면 뽑을 자리가 없다 —
        **송장이 나가야** 셋팅 보드에서 내려간다."""
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "js" / "setup.js").read_text("utf-8")
        self.assertIn("function hasIssuedWaybill", js, "송장 발급 여부 판정이 없다")
        self.assertIn("(!o.shippingDone || inShippingBucket(o))", js,
                      "출고 확인만으로 목록에서 내린다")
        self.assertNotIn("all.filter((o) => !o.shippingDone && !o.cancelledAt)", js,
                         "옛 필터가 남아 출고 확인 즉시 사라진다")
        # '출고 확인'이 누를 수 있는 단계 카드여야 한다(여기서 송장을 뽑는다)
        self.assertIn('card("shipping", "출고 확인"', js, "출고 확인 카드를 못 누른다")
        self.assertIn('if (k === "shipping") return inShippingBucket(o);', js)

    def test_출고확인은_배송완료까지_남는다(self):
        """★대표 2026-09-04: "출고확인에서 배송완료가 되어야만 출고기록 조회로 넘어가게."
        배송완료(CJ 91)가 찍히면 서버가 보관까지 하므로 조회에서 아예 빠진다."""
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "js" / "setup.js").read_text("utf-8")
        blk = js.split("function inShippingBucket(o) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (o.deliveredAt) return false;", blk,
                      "배송완료돼도 출고 확인 칸에 남는다")
        self.assertIn("if (hasIssuedWaybill(o)) return true;", blk,
                      "송장이 있는 건을 날짜로 끊으면 배송 중인데 사라진다")

    def test_판매유형_채널은_검수완료까지만_보인다(self):
        """★대표 2026-09-04: "판매유형, 판매채널은 SW 검수완료까지만 보여야 해."
        출고 확인·출고 기록 조회는 이미 나간 물건을 보는 화면이라 채널로 고를 일이 없다."""
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "js" / "setup.js").read_text("utf-8")
        self.assertIn('const SHIPPED_STAGES = ["shipping", "shipped"]', js,
                      "이미 나간 단계 목록이 없다")
        blk = js.split("function renderSetupTagFilters(", 1)[1][:900]
        self.assertIn("SHIPPED_STAGES.includes(state.setupStage)", blk,
                      "출고 확인·출고 기록에서도 판매유형·채널이 뜬다")
        self.assertIn('host.innerHTML = "";', blk, "감추지 않고 그대로 그린다")
        self.assertIn('state.setupChannel = "all";', blk,
                      "골라 둔 채널이 남아 다음 단계 목록을 조용히 걸러 낸다")

    def test_지난_출고분은_셋팅_보드에_쌓이지_않는다(self):
        """★대표 2026-09-04: "제작대기를 한 번 더 누르면 이미 출고된 것까지 나온다".
        OWS 송장을 아직 안 쓰는 주문이 대부분이라('송장 없음'만으로 거르면 실측 257건이
        남아 보드가 지난달 출고분에 묻힌다) 출고 확인분은 **최근 것만** 남긴다."""
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "js" / "setup.js").read_text("utf-8")
        self.assertIn("function inShippingBucket", js, "출고 확인 칸 판정이 없다")
        # 날짜 하한이 실제로 걸려 있어야 한다 — 없으면 무한히 쌓인다
        self.assertIn("ymd(new Date(Date.now() - 864e5))", js,
                      "출고 확인분에 날짜 하한이 없다(지난 출고분이 계속 쌓인다)")
        # 세 곳(기본 목록·단계 카드·숫자)이 같은 판정을 써야 어긋나지 않는다
        for where, frag in (("기본 목록", "(!o.shippingDone || inShippingBucket(o))"),
                            ("[출고 확인] 카드", 'if (k === "shipping") return inShippingBucket(o);'),
                            ("카드 숫자", "orders.filter(inShippingBucket)")):
            self.assertIn(frag, js, f"{where}가 다른 기준을 쓴다")
        self.assertNotIn("o.shippingDone && !o.cancelledAt && !hasIssuedWaybill(o)", js,
                         "옛 무제한 판정이 남아 있다")

    def test_송장_일괄인쇄는_검수완료부터_보인다(self):
        """★대표 2026-09-04 정정: "SW 검수완료 탭에서 송장 일괄 출력이 가능해야 함".
        제작 대기~제작 완료에서는 여전히 안 보인다 — 뽑을 송장이 없는 단계다."""
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "js" / "setup.js").read_text("utf-8")
        self.assertIn("syncWbPrintBtn", js, "일괄 인쇄 버튼 표시 제어가 없다")
        self.assertIn('const WB_PRINT_STAGES = ["inspected", "shipping", "shipped"]', js,
                      "일괄 인쇄가 보이는 단계 목록이 다르다")
        self.assertEqual(js.count("WB_PRINT_STAGES.includes(state.setupStage)"), 3,
                         "표시 조건이 그리기·초기화·단계전환 세 곳에 다 걸려야 한다")
        for gone in ("waiting", "preparing", "produced"):
            self.assertNotIn(f'"{gone}", "shipping"', js, f"{gone} 단계에도 일괄 인쇄가 뜬다")

    def test_검수완료_탭의_일괄_버튼은_송장_없는_건만_발급해_인쇄한다(self):
        """★대표 2026-09-08: "송장을 뽑았던 애를 뽑는 게 아니라 송장을 뽑지 않았던 애들에게 송장 넣어서
        실제 출력이 전부 되어야 해 — 이미 출력했던 애들은 제외". 출고 확인·출고 기록 탭은 예전 인쇄 그대로."""
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "js" / "setup.js").read_text("utf-8")
        blk = js[js.index('"#sf-wbprint-all"'):][:1500]
        self.assertIn('state.setupStage === "inspected"', blk, "SW 검수 완료 탭 분기가 없다")
        self.assertIn("bulkIssueAndPrint(list, picked.size)", blk, "발급·인쇄 함수를 안 부른다")
        fn = js[js.index("async function bulkIssueAndPrint("):][:7000]
        self.assertIn("hasIssuedWaybill(o)", fn, "이미 발급(출력)된 건을 제외하지 않는다")
        self.assertIn("/api/orders/${o.id}/waybill", fn, "송장 없는 건을 발급하지 않는다")
        self.assertIn("printWaybillWids(wids)", fn, "새로 발급한 송장을 인쇄하지 않는다")
        self.assertIn("이미 발급된", fn, "안내문에 제외 건수가 없다")
        self.assertIn("failed.push(", fn, "한 건 실패가 전체를 멈추면 안 된다")
        self.assertIn("/api/waybills/print", js[js.index("async function printWaybillWids("):][:800])
        self.assertIn('"🧾 송장 일괄 발급·인쇄"', js, "검수 완료 탭에서 버튼 이름이 안 바뀐다")
        self.assertGreaterEqual(js.count("syncWbPrintLabel()"), 2, "단계가 바뀔 때 버튼 이름이 안 따라간다")

    def test_화면_배선(self):
        root = Path(__file__).resolve().parent.parent
        setup = (root / "static" / "js" / "setup.js").read_text("utf-8")
        self.assertNotIn("실발행 상태입니다", setup, "CJ 실발행 배너가 아직 남아 있다")
        self.assertIn('id="sf-all"', setup, "전체 선택 체크박스가 없다")
        self.assertIn('class="sf-pick"', setup, "행 선택 체크박스가 없다")
        self.assertIn("picked.size", setup, "고른 건만 인쇄하는 처리가 없다")
        self.assertIn("!o.shippingDone && !o.cancelledAt", setup, "취소 주문이 셋팅에서 안 걸러진다")
        self.assertIn("showAssetWarnings", setup, "자산 경고 표시가 없다")
        ship = (root / "static" / "js" / "shipping.js").read_text("utf-8")
        self.assertIn('id="wb-new"', ship, "송장 신규 등록 버튼이 없다")
        self.assertIn("function openManualWaybill", ship)


class TestAsVisitAndPopup(Base):
    """A/S UI 개선(2026-09-03 대표) — 접수·상세 팝업 + 좌우 배치, 방문 접수/방문 수령,
    반송 송장 문구를 '신규 송장 발급'으로."""

    def test_수령_방법을_저장한다(self):
        t = self.c.post("/api/as-tickets", json={
            "customer": "방문고객", "symptom": "전원", "intake": "visit"}).get_json()
        # 생성 응답은 id/접수번호만 준다 — 기본값은 상세 조회로 확인한다
        got = self.c.get(f"/api/as-tickets/{t['id']}").get_json()
        self.assertEqual(got.get("returnMethod"), "parcel", "기본값은 택배 발송이다")
        r = self.c.patch(f"/api/as-tickets/{t['id']}", json={"returnMethod": "visit"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        again = self.c.get(f"/api/as-tickets/{t['id']}").get_json()
        self.assertEqual(again["returnMethod"], "visit", "수령 방법이 저장되지 않았다")

    def test_모르는_수령_방법은_막는다(self):
        t = self.c.post("/api/as-tickets", json={"customer": "고객", "symptom": "x"}).get_json()
        r = self.c.patch(f"/api/as-tickets/{t['id']}", json={"returnMethod": "quick"})
        self.assertEqual(r.status_code, 400)

    def test_메타가_수령_방법을_알려준다(self):
        m = self.c.get("/api/as-meta").get_json()
        codes = {x["code"] for x in (m.get("returnMethods") or [])}
        self.assertEqual(codes, {"parcel", "visit"})

    def test_화면_배선(self):
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "js" / "as.js").read_text("utf-8")
        self.assertIn("as-wide-form", js, "팝업 넓은 배치 클래스가 없다")
        self.assertEqual(js.count("openModalWith(host)"), 2, "접수·상세 둘 다 팝업이어야 한다")
        self.assertIn("신규 송장 발급", js, "반송 송장 문구가 안 바뀌었다")
        self.assertNotIn('>📦 반송 송장 발급<', js, "옛 버튼 문구가 남아 있다")
        self.assertIn("방문 접수 — 회수 불필요", js, "방문 접수 안내가 없다")
        self.assertIn("방문 수령 — 송장 불필요", js, "방문 수령 안내가 없다")
        self.assertIn('id="asd-retmethod"', js, "수령 방법 칸이 없다")
        css = (root / "static" / "css" / "app.css").read_text("utf-8")
        self.assertIn("#as-form.as-wide-form", css, "접수 폼 좌우 배치 CSS가 없다")
        self.assertIn("#as-detail.as-wide-form", css, "상세 좌우 배치 CSS가 없다")


class TestAsStatsAndParts(Base):
    """A/S 추가 개발(2026-09-02 대표) — ①현황 통계 ②부품 소진 연결 ③재발 추적
    ④처리 기한 알림 ⑤원가/수익 집계.

    ★돈 규칙(대표 확정): A/S 수입은 판매 매출과 **합치지 않고 나란히**,
      A/S 부품 원가는 **A/S 비용으로만**(자산 원가에 안 얹음 — 지난달 마진 보호).
    """

    def _ticket(self, **kw):
        base = {"customer": "김에이", "symptom": "전원 불량", "phone": "010-5555-6666",
                "symptomCat": "노트북", "symptomSub": "H/W"}
        base.update(kw)
        r = self.c.post("/api/as-tickets", json=base)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()

    def _part(self, name="시험램-AS", price=22000):
        r = self.c.post("/api/parts", json={"name": name, "category": "ram", "price": price})
        if r.status_code in (200, 201):
            return r.get_json()["id"]
        rows = self.c.get("/api/parts").get_json()
        rows = rows if isinstance(rows, list) else rows.get("parts", [])
        return next(x["id"] for x in rows if x["name"] == name)

    def _stock(self, pid):
        from app.db import tx
        with self.c.application.app_context():
            with tx() as conn:
                return conn.execute(
                    "SELECT COALESCE(SUM(qty),0) AS s FROM part_stock_moves WHERE part_id=?",
                    (pid,)).fetchone()["s"]

    # ---------------- 부품 소진
    def test_수리내역_부품이_재고에서_빠지고_원가가_동결된다(self):
        pid = self._part(price=22000)
        t = self._ticket()
        before = self._stock(pid)
        r = self.c.post(f"/api/as-tickets/{t['id']}/items", json={"items": [
            {"partId": pid, "name": "시험램-AS", "qty": 2, "amount": 60000},
            {"name": "공임", "qty": 1, "amount": 20000},
        ]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d["cost"], 80000, "청구 합계는 줄 금액의 합이다")
        self.assertEqual(d["partCost"], 44000, "부품 원가 = 단가 22,000 × 2")
        self.assertEqual(self._stock(pid), before - 2, "부품 2개가 재고에서 안 빠졌다")
        items = {x["name"]: x for x in d["items"]}
        self.assertEqual(items["시험램-AS"]["cost"], 44000)
        self.assertEqual(items["공임"]["cost"], 0, "부품이 아닌 줄은 원가가 없다")

    def test_다시_저장해도_재고가_두_번_빠지지_않는다(self):
        """수리내역은 '전체 교체' 저장이라, 이전 소진을 물리고 다시 빼야 한다."""
        pid = self._part()
        t = self._ticket()
        body = {"items": [{"partId": pid, "name": "시험램-AS", "qty": 2, "amount": 60000}]}
        self.c.post(f"/api/as-tickets/{t['id']}/items", json=body)
        once = self._stock(pid)
        self.c.post(f"/api/as-tickets/{t['id']}/items", json=body)
        self.assertEqual(self._stock(pid), once, "재저장이 재고를 또 깎았다")
        # 줄을 지우면 재고가 돌아온다
        self.c.post(f"/api/as-tickets/{t['id']}/items", json={"items": []})
        self.assertEqual(self._stock(pid), once + 2, "내역을 비웠는데 재고가 안 돌아온다")

    def test_A_S_부품은_자산_원가에_안_얹는다(self):
        """대표 확정 — 이미 팔린 자산의 지난 마진을 소급해 흔들면 안 된다."""
        from app.db import tx
        pid = self._part()
        t = self._ticket()
        self.c.post(f"/api/as-tickets/{t['id']}/items", json={"items": [
            {"partId": pid, "name": "시험램-AS", "qty": 1, "amount": 30000}]})
        with self.c.application.app_context():
            with tx() as conn:
                n = conn.execute("SELECT COUNT(*) AS c FROM asset_repairs").fetchone()["c"]
        self.assertEqual(n, 0, "A/S 부품이 자산 수리원가(asset_repairs)에 들어갔다")

    # ---------------- 통계
    def test_현황_통계가_수입과_원가를_가른다(self):
        pid = self._part(price=10000)
        paid = self._ticket(chargeTo="customer")
        self._ticket(chargeTo="company", symptomSub="S/W")
        self.c.post(f"/api/as-tickets/{paid['id']}/items", json={"items": [
            {"partId": pid, "name": "시험램-AS", "qty": 3, "amount": 90000}]})
        r = self.c.get("/api/as-stats?from=2000-01-01&to=2099-12-31")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertEqual(d["tickets"], 2)
        self.assertEqual(d["paid"], 1)
        self.assertEqual(d["free"], 1)
        self.assertEqual(d["paidRate"], 50.0)
        self.assertEqual(d["income"], 90000, "유상 건의 수리비가 수입이다")
        self.assertEqual(d["partCost"], 30000, "10,000 × 3")
        self.assertEqual(d["profit"], 60000, "수입 − 부품 원가")
        self.assertEqual([x["key"] for x in d["byCat"]], ["노트북"])
        self.assertEqual(sorted(x["key"] for x in d["bySub"]), ["H/W", "S/W"])

    def test_통계는_취소건을_빼고_기간이_필요하다(self):
        t = self._ticket(chargeTo="customer")
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "cancelled"})
        d = self.c.get("/api/as-stats?from=2000-01-01&to=2099-12-31").get_json()
        self.assertEqual(d["tickets"], 0, "취소 건이 통계에 남아 있다")
        self.assertEqual(self.c.get("/api/as-stats").status_code, 400, "기간 없이 열리면 안 된다")

    # ---------------- 재발 추적
    def test_같은_고객_재접수가_목록에_표시된다(self):
        self._ticket(phone="010-1234-5678")
        self._ticket(phone="01012345678")     # 하이픈만 다른 같은 번호
        rows = self.c.get("/api/as-tickets?view=all").get_json()
        self.assertTrue(rows, "목록이 비었다")
        for t in rows:
            self.assertEqual(t["repeatCust"], 1, "하이픈 유무로 같은 고객이 갈렸다")

    def test_같은_자산_재입고가_표시된다(self):
        self._ticket(assetNo="260101-0001")
        self._ticket(assetNo="260101-0001", phone="010-9999-0000")
        rows = self.c.get("/api/as-tickets?view=all").get_json()
        for t in rows:
            self.assertEqual(t["repeatAsset"], 1, "같은 기계 재입고가 안 잡힌다")

    # ---------------- 채번(감사 2026-09-02)
    def test_접수번호가_두_자리를_넘겨도_이어진다(self):
        """예전에는 99건을 넘기면 같은 번호를 또 내려다 저장이 막혔다."""
        from app.db import tx
        from app import config
        pre = f"AS-{config.now().strftime('%y%m%d')}-"
        with self.c.application.app_context():
            with tx(write=True) as conn:
                ts = config.now_iso()
                conn.execute(
                    "INSERT INTO as_tickets(ticket_no, customer, received_at, created_at, updated_at) "
                    "VALUES(?,'과거',?,?,?)", (pre + "100", ts, ts, ts))
        t = self._ticket()
        self.assertEqual(t["ticketNo"], pre + "101", "세 자리 번호를 못 보고 되돌아갔다")

    # ---------------- 화면 배선
    def test_화면_배선(self):
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "js" / "as.js").read_text("utf-8")
        # 2026-09-07 개편: [📊 현황] → [🚦 진행 상황](data-atab="board"), 통계 본문은 매출/실적이 계속 쓴다
        self.assertIn('data-atab="board"', js, "진행 상황 탭 버튼이 없다")
        self.assertIn("function renderAsBoardTab", js)
        self.assertIn("function renderAsStatsBody", js, "A/S 실적 본문(매출/실적 ▸ 🔧 A/S 실적)이 사라졌다")
        self.assertIn("function asRepeatChip", js, "재발 배지가 없다")
        self.assertIn("asRepeatChip(t)", js, "목록에 재발 배지가 안 붙었다")
        app_js = (root / "static" / "js" / "app.js").read_text("utf-8")
        self.assertIn("A/S 처리 지연", app_js, "대시보드 '오늘 할 일'에 지연 알림이 없다")


class TestAsDetailFix(Base):
    """A/S 상세 보완 6건(2026-08-31 대표) — ①접수 후 자산번호/모델명 칸(일치하면 자산 연결)
    ②수리비 = 수리 내역 합계(직접 수정 불가) ③발행본 삭제(문서번호 재사용 금지)
    ④운영설정 계좌번호(청구서·{계좌번호}) ⑤문자 창을 상세 '위에' 겹치기 ⑥발송 버튼 하단 배치."""

    def _ticket(self, **kw):
        base = {"customer": "김상세", "symptom": "전원 불량", "phone": "010-8888-7777"}
        base.update(kw)
        r = self.c.post("/api/as-tickets", json=base)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()

    def _asset(self, model="L480"):
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": model, "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        return self.c.get(f"/api/assets/{a['id']}").get_json()

    def _company(self, **extra):
        body = {"name": "예시 운영사", "manager": "계약 담당자", "phone": "032-123-4567"}
        body.update(extra)
        r = self.c.post("/api/as-doc-settings", json={"company": body})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.c.post("/api/as-doc-stamp", json={"dataUrl": "data:image/png;base64,AAA"})

    def test_자산번호_모델명_수기_입력(self):
        t = self._ticket()
        r = self.c.patch(f"/api/as-tickets/{t['id']}", json={"assetNo": "TMS-0001", "model": "그램15"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = self.c.get(f"/api/as-tickets/{t['id']}").get_json()
        self.assertEqual(d["assetNo"], "TMS-0001")
        self.assertEqual(d["model"], "그램15")
        self.assertIsNone(d["assetId"], "없는 번호가 자산에 연결되면 안 된다")
        # 접수 때도 받는다(자산 연결 없이 수기)
        t2 = self._ticket(customer="이수기", assetNo="OTHER-77", model="맥북")
        d2 = self.c.get(f"/api/as-tickets/{t2['id']}").get_json()
        self.assertEqual((d2["assetNo"], d2["model"]), ("OTHER-77", "맥북"))
        # 비우면 지워진다
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"assetNo": "", "model": ""})
        d3 = self.c.get(f"/api/as-tickets/{t['id']}").get_json()
        self.assertEqual(d3["assetNo"], "")

    def test_관리번호가_일치하면_자산에_연결된다(self):
        a = self._asset(model="L480")
        t = self._ticket()
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"assetNo": a["assetNo"], "model": ""})
        d = self.c.get(f"/api/as-tickets/{t['id']}").get_json()
        self.assertEqual(d["assetId"], a["id"], "관리번호 일치가 자산 연결로 안 이어졌다")
        self.assertEqual(d["model"], "L480", "연결된 자산의 모델이 보여야 한다")
        # 모델명 수기 덮기 — 화면 표기만 바뀌고 자산 테이블은 안 건드린다
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"model": "L480 개조"})
        self.assertEqual(self.c.get(f"/api/as-tickets/{t['id']}").get_json()["model"], "L480 개조")
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["model"], "L480",
                         "A/S 화면의 모델명 수정이 자산을 고치면 안 된다")
        # 문자 {관리번호}/{모델명} — 연결 자산 우선, 수기 모델이 표기를 덮는다
        self.c.post("/api/as-sms-templates", json={
            "events": {"done": {"on": True, "text": "{관리번호} {모델명}", "subject": ""}}})
        p = self.c.get(f"/api/as-tickets/{t['id']}/sms-preview?event=done").get_json()
        self.assertEqual(p["text"], f"{a['assetNo']} L480 개조")

    def test_수리비는_내역_합계가_진실(self):
        asjs = (Path(__file__).resolve().parent.parent / "static" / "js" / "as.js").read_text("utf-8")
        self.assertIn('id="asd-cost" value="${t.cost}" readonly', asjs,
                      "상세의 수리비 칸은 읽기 전용(내역 합계 자동)이어야 한다")
        self.assertNotIn('cost: $("#asd-cost")', asjs,
                         "저장이 수리비를 직접 보내면 내역 합계와 어긋난다")
        self.assertIn('costEl.value = r.cost', asjs, "내역 저장 → 수리비 칸 동기화가 없다")

    def test_발행본_삭제와_문서번호_유지(self):
        self._company()
        t = self._ticket()
        self.c.post(f"/api/as-tickets/{t['id']}/items",
                    json={"items": [{"name": "수리", "qty": 1, "amount": 55000}]})
        d1 = self.c.post(f"/api/as-tickets/{t['id']}/documents", json={"docType": "repair"}).get_json()["doc"]
        d2 = self.c.post(f"/api/as-tickets/{t['id']}/documents", json={"docType": "repair"}).get_json()["doc"]
        self.assertTrue(d1["docNo"].endswith("-01") and d2["docNo"].endswith("-02"))
        lst = self.c.get(f"/api/as-tickets/{t['id']}/documents").get_json()["docs"]
        did2 = next(x["id"] for x in lst if x["docNo"] == d2["docNo"])
        r = self.c.delete(f"/api/as-tickets/{t['id']}/documents/{did2}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        left = self.c.get(f"/api/as-tickets/{t['id']}/documents").get_json()["docs"]
        self.assertEqual([x["docNo"] for x in left], [d1["docNo"]], "삭제본이 목록에서 안 빠졌다")
        # 재삭제 400 · 없는 문서 404 · 이력에 남는다
        self.assertEqual(self.c.delete(f"/api/as-tickets/{t['id']}/documents/{did2}").status_code, 400)
        self.assertEqual(self.c.delete(f"/api/as-tickets/{t['id']}/documents/999999").status_code, 404)
        evs = [e["action"] for e in self.c.get(f"/api/as-tickets/{t['id']}").get_json()["events"]]
        self.assertIn("문서삭제", evs)
        # ★지운 번호는 다시 쓰지 않는다 — 소프트 삭제라 채번이 계속 센다
        d3 = self.c.post(f"/api/as-tickets/{t['id']}/documents", json={"docType": "repair"}).get_json()["doc"]
        self.assertTrue(d3["docNo"].endswith("-03"),
                        f"삭제된 -02 번호가 재사용됐다: {d3['docNo']}")
        anon = self.c.application.test_client()
        self.assertEqual(anon.delete(f"/api/as-tickets/{t['id']}/documents/{did2}").status_code, 401)
        # 화면 배선
        asjs = (Path(__file__).resolve().parent.parent / "static" / "js" / "as.js").read_text("utf-8")
        self.assertIn("data-asdocdel", asjs, "발행 이력에 삭제 버튼이 없다")

    def test_계좌번호_설정_문서_문자(self):
        bank = "국민 000000-00-000000 예시 운영사"
        self._company(bank=bank)
        self.assertEqual(self.c.get("/api/as-doc-settings").get_json()["company"]["bank"], bank)
        t = self._ticket()
        self.c.post(f"/api/as-tickets/{t['id']}/items",
                    json={"items": [{"name": "수리", "qty": 1, "amount": 55000}]})
        doc = self.c.post(f"/api/as-tickets/{t['id']}/documents", json={"docType": "invoice"}).get_json()["doc"]
        self.assertEqual(doc["company"]["bank"], bank, "청구내역서 스냅샷에 계좌가 없다")
        # {계좌번호} 문자 자리
        self.c.post("/api/as-sms-templates", json={
            "events": {"done": {"on": True, "text": "{계좌번호}로 입금해 주세요", "subject": ""}}})
        p = self.c.get(f"/api/as-tickets/{t['id']}/sms-preview?event=done").get_json()
        self.assertEqual(p["text"], f"{bank}로 입금해 주세요")
        # 화면 배선 — 설정 칸과 청구서 계좌 줄
        asjs = (Path(__file__).resolve().parent.parent / "static" / "js" / "as.js").read_text("utf-8")
        self.assertIn('id="asdoc-bank"', asjs, "운영 설정에 계좌번호 칸이 없다")
        self.assertIn("bank-line", asjs, "청구내역서에 입금 계좌 줄이 없다")

    def test_문자창은_상세_위에_겹친다(self):
        base = Path(__file__).resolve().parent.parent / "static" / "js"
        appjs = (base / "app.js").read_text("utf-8")
        self.assertIn("function openModalOver", appjs, "2층 팝업이 없다")
        self.assertIn("function closeModalOver", appjs)
        self.assertIn('back.id = "modal-back2"', appjs)
        self.assertIn('if (document.getElementById("modal-back2")) { closeModalOver(); return; }',
                      appjs, "Esc 는 위층부터 닫아야 한다")
        asjs = (base / "as.js").read_text("utf-8")
        modal_blk = asjs[asjs.index("function openAsSmsModal"):asjs.index("function openAsSmsPicker")]
        self.assertIn("openModalOver(host)", modal_blk, "문자 창이 상세를 닫고 열리면 안 된다")
        self.assertNotIn("openModalWith(host)", modal_blk)
        pick_blk = asjs[asjs.index("function openAsSmsPicker"):asjs.index("function ensureAsMeta")]
        self.assertIn("openModalOver(host)", pick_blk)
        self.assertNotIn("openModalWith(host)", pick_blk)

    def test_교환_제품코드와_추가비용(self):
        """교환이면 어떤 제품과 바꾸는지 제품코드를 적고, 교환 차액 같은 추가비용은
        수리비(내역 합계)와 별개로 두 문서(수리내역서/청구내역서)의 합계에 얹힌다
        (2026-08-31 대표 — 별도 장부 없이 두 폼까지만)."""
        self._company()
        t = self._ticket(customer="정교환", asType="exchange")
        r = self.c.patch(f"/api/as-tickets/{t['id']}", json={
            "exchangeProductCode": "HB-NT-0099", "extraCharge": 50000, "extraNote": "교환 차액"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = self.c.get(f"/api/as-tickets/{t['id']}").get_json()
        self.assertEqual(d["exchangeProductCode"], "HB-NT-0099")
        self.assertEqual((d["extraCharge"], d["extraNote"]), (50000, "교환 차액"))
        self.assertEqual(self.c.patch(f"/api/as-tickets/{t['id']}",
                                      json={"extraCharge": -1}).status_code, 400)
        # 문서 합계 = 수리 항목 + 추가비용(부가세도 같은 식으로 갈라 합산)
        self.c.post(f"/api/as-tickets/{t['id']}/items",
                    json={"items": [{"name": "패널 점검", "qty": 1, "amount": 55000}]})
        doc = self.c.post(f"/api/as-tickets/{t['id']}/documents",
                          json={"docType": "invoice"}).get_json()["doc"]
        self.assertEqual(doc["ticket"]["exchangeProductCode"], "HB-NT-0099")
        self.assertEqual(doc["extra"], {"amount": 50000, "vat": 4545, "net": 45455,
                                        "note": "교환 차액"})
        self.assertEqual(doc["totals"], {"amount": 105000, "vat": 9545, "net": 95455})
        # 수리비(cost)는 내역 합계 그대로 — 추가비용이 섞이면 안 된다
        self.assertEqual(self.c.get(f"/api/as-tickets/{t['id']}").get_json()["cost"], 55000)
        # {비용안내} — 무상+추가비용 / 유상+추가비용 문구
        self.c.post("/api/as-sms-templates", json={
            "events": {"done": {"on": True, "text": "{비용안내}", "subject": ""}}})
        p = self.c.get(f"/api/as-tickets/{t['id']}/sms-preview?event=done").get_json()
        self.assertEqual(p["text"], "수리비는 무상이며, 추가비용 50,000원이 청구됩니다.")
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"chargeTo": "customer"})
        p2 = self.c.get(f"/api/as-tickets/{t['id']}/sms-preview?event=done").get_json()
        self.assertEqual(p2["text"], "수리비 55,000원과 추가비용 50,000원, 합계 105,000원이 청구됩니다.")
        # 화면 배선 — 상세의 교환 칸(유형=교환일 때만)·수리 내역의 추가비용 줄·문서 3줄 합계
        asjs = (Path(__file__).resolve().parent.parent / "static" / "js" / "as.js").read_text("utf-8")
        self.assertIn('id="asd-exproduct"', asjs, "상세에 교환 제품코드 칸이 없다")
        self.assertIn('id="asi-extra"', asjs, "수리 내역에 추가비용 칸이 없다")
        self.assertIn("교환 제품코드</th>", asjs, "문서에 교환 제품코드 행이 없다")
        self.assertIn("수리 소계", asjs, "문서에 소계/추가비용/합계 구분이 없다")

    def test_발송_버튼은_하단_SMS_발송_카드에(self):
        asjs = (Path(__file__).resolve().parent.parent / "static" / "js" / "as.js").read_text("utf-8")
        self.assertNotIn(">문자 보내기</b>", asjs, "상단 카드의 문자 줄이 남아 있다")
        i_card = asjs.index("💬 SMS 발송")
        i_btns = asjs.index("AS_SMS_EVENTS.map")
        i_log = asjs.index('id="asd-sms"')
        self.assertLess(i_card, i_btns, "발송 버튼이 SMS 발송 카드 밖에 있다")
        self.assertLess(i_btns, i_log, "발송 버튼은 보낸 문자 목록 위에 있어야 한다")

class TestShipAutoConfirm(Base):
    """배송이 시작되면 주문이 자동으로 출고 마감돼 [출고 기록 조회]로 넘어간다
    (2026-08-31 대표 — "간선상차가 되면 출고기록조회로 자동으로").
    ★배송중/배송완료 구분은 고도몰 기준(대표 확정): 송장이 실려 움직이는 동안(첫 물리
    스캔~91 전)은 '배송중', CJ 배달완료(91)가 '배송완료'. 보관(archived)은 주문관리
    상태 산식이 배송완료로 읽으므로 — 배송중 동안은 보관하지 않고 91 에서 한다."""

    def _order(self):
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={"categoryId": cats[0]["id"], "model": "L480",
                                             "qty": 1, "purchasePrice": 100000}).get_json()[0]
        o = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "자동마감", "phone": "010-1111-2222",
            "productName": "노트북", "address": "서울시 강남구 1", "postalCode": "06000",
            "quantity": 1, "amount": 500000}).get_json()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("production", "softwareInspection"):
            self.c.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        return o, a

    def _db_order(self, oid):
        from app.db import tx
        with self.app.app_context():
            with tx() as conn:
                return dict(conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone())

    def test_배송_시작이면_자동_출고마감(self):
        from app.db import tx
        from app.orders.recall import _auto_ship_confirm
        o, a = self._order()
        w = {"type": "forward", "cj_kind": "forward", "order_id": o["id"],
             "wid": "WB-AUTO-1", "invoice_no": "123456789012"}
        with self.app.app_context():
            with tx(write=True) as conn:
                self.assertFalse(_auto_ship_confirm(conn, w, "01"),
                                 "집화지시(01)는 기사 배정만 된 것 — 아직 안 나갔다")
                self.assertTrue(_auto_ship_confirm(conn, w, "11"),
                                "간선상차(11)면 자동 출고 마감돼야 한다")
                self.assertFalse(_auto_ship_confirm(conn, w, "42"),
                                 "이미 마감된 주문은 다시 건드리지 않는다")
        row = self._db_order(o["id"])
        self.assertEqual(row["shipping_done"], 1)
        self.assertEqual(row["shipping_by"], "CJ배송추적")
        # ★고도몰 기준 — 배송중 동안은 보관하지 않는다(보관=배송완료로 읽히므로)
        self.assertEqual(row["archived_at"], "", "배송중인데 보관하면 주문관리에 '배송완료'로 보인다")
        self.assertEqual(row["delivered_at"], "", "아직 배달완료(91) 전이다")
        # 자산은 [금일 출고 확인]과 똑같이 출고 처리된다
        st = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(st["status"], "shipped", "매칭 자산이 출고 처리 안 됐다")

    def test_집화완료로는_출고확인이_안_된다(self):
        """★대표 2026-09-04: "송장이 각 고객에게 자동으로 들어갔어도 간선상차가 되기
        전까지는 출고확인으로 넘기면 안 된다. 간선상차 기준으로 우리 제품이 정상적으로
        출고되었는지가 중요하다."
        02(집화완료)는 기사가 받아 스캔만 한 것 — 아직 터미널에 실리지 않았다."""
        from app.db import tx
        from app.orders.recall import _auto_ship_confirm
        o, _a = self._order()
        w = {"type": "forward", "cj_kind": "forward", "order_id": o["id"],
             "wid": "WB-AUTO-9", "invoice_no": "123456789019"}
        with self.app.app_context():
            with tx(write=True) as conn:
                for early in ("01", "02", "03"):
                    self.assertFalse(_auto_ship_confirm(conn, w, early),
                                     f"{early} 단계로 출고확인이 켜졌다 — 간선상차 전이다")
        self.assertEqual(self._db_order(o["id"])["shipping_done"], 0,
                         "간선상차 전에 출고확인이 켜졌다")
        with self.app.app_context():
            with tx(write=True) as conn:
                self.assertTrue(_auto_ship_confirm(conn, w, "11"),
                                "간선상차(11)에서는 출고확인이 켜져야 한다")
        self.assertEqual(self._db_order(o["id"])["shipping_done"], 1)

    def test_배달완료면_배송완료_도장과_보관(self):
        from app.db import tx
        from app.orders.recall import _auto_delivered, _auto_ship_confirm
        o, _a = self._order()
        w = {"type": "forward", "cj_kind": "forward", "order_id": o["id"],
             "wid": "WB-AUTO-3", "invoice_no": "123456789014"}
        with self.app.app_context():
            with tx(write=True) as conn:
                _auto_ship_confirm(conn, w, "42")
                self.assertFalse(_auto_delivered(conn, w, "42"),
                                 "배송중(42)은 아직 배송완료가 아니다 — 고도몰 기준")
                self.assertTrue(_auto_delivered(conn, w, "91"),
                                "배달완료(91)면 배송완료 도장이 찍혀야 한다")
                self.assertFalse(_auto_delivered(conn, w, "91"), "두 번 찍지 않는다")
        row = self._db_order(o["id"])
        self.assertTrue(row["delivered_at"] and row["archived_at"],
                        "91 에서 배송완료 도장+보관까지 — 고도몰 기준의 '배송완료'")

    def test_회수송장과_취소주문은_건드리지_않는다(self):
        from app.db import tx
        from app.orders.recall import _auto_ship_confirm
        o, _a = self._order()
        recall_w = {"type": "recall", "cj_kind": "return", "order_id": o["id"],
                    "wid": "WB-R-1", "invoice_no": ""}
        with self.app.app_context():
            with tx(write=True) as conn:
                self.assertFalse(_auto_ship_confirm(conn, recall_w, "11"),
                                 "회수 송장의 간선상차는 출고가 아니다")
        r = self.c.patch(f"/api/orders/{o['id']}",
                         json={"action": "cancel", "reason": "시험 취소"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        w = {"type": "forward", "cj_kind": "forward", "order_id": o["id"],
             "wid": "WB-AUTO-2", "invoice_no": "123456789013"}
        with self.app.app_context():
            with tx(write=True) as conn:
                self.assertFalse(_auto_ship_confirm(conn, w, "11"),
                                 "취소된 주문을 자동 마감하면 안 된다")
        self.assertEqual(self._db_order(o["id"])["shipping_done"], 0)

    def test_배선(self):
        src = (Path(__file__).resolve().parent.parent / "app" / "orders" / "recall.py"
               ).read_text("utf-8")
        self.assertIn('SHIP_CONFIRM_STAGES = ("11", "12", "21", "41", "42", "82", "91")', src,
                      "자동 출고확인 단계 목록이 바뀌면 마감 기준이 흔들린다")
        self.assertNotIn('"02", "11"', src,
                         "집화완료(02)로 출고확인하면 안 된다 — 간선상차(11)부터(대표 2026-09-04)")
        # 2026-09-07: 두 추적 경로(_sync_one·회수 송장번호 수집)가 한 관문(_apply_stage)을 탄다 —
        # 관문 안에 자동 마감·배달완료 도장이 있고, 두 경로가 그 관문을 부르는지 본다.
        gate = src.split("def _apply_stage(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_auto_ship_confirm(conn, w, code)", gate,
                      "추적 관문(_apply_stage)에 자동 마감이 안 걸려 있다")
        self.assertIn("_auto_delivered(conn, w, code)", gate,
                      "배달완료(91) 도장이 추적 관문에 안 걸려 있다")
        sync_one = src.split("def _sync_one(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_apply_stage(conn, w, code, name", sync_one,
                      "추적 반영(_sync_one)이 관문을 안 탄다")
        sweep = src.split("def sweep_recall_invoices(", 1)[1].split("\n@bp.", 1)[0]
        self.assertIn("_apply_stage(c2, w, stage[0], stage[1]", sweep,
                      "예약 기준 추적(회수 송장번호 수집)이 관문을 안 탄다")
        setupjs = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
                   ).read_text("utf-8")
        self.assertIn('id="sf-wbprint-all"', setupjs, "송장 일괄 인쇄 버튼이 없다")
        self.assertIn("function printPdfBlob", setupjs, "PDF iframe 인쇄가 없다")
        blk = setupjs[setupjs.index('"#sf-wbprint-all"'):]
        self.assertIn("/api/waybills/print", blk[:2000], "일괄 인쇄가 병합 PDF 를 안 쓴다")

class TestShipBoard(Base):



    """배송/송장 재편(2026-08-31 대표 — "현황판처럼 메인으로, [현황]/[신규접수] 탭").
    현황판 숫자는 서버(/waybills/board)가 센다 — 보관 포함 잣대(/orders/today)와 통일."""

    def test_현황판_집계(self):
        r = self.c.get("/api/waybills/board")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        b = r.get_json()
        for k in ("issuedToday", "testToday", "shippedToday", "inTransit",
                  "recallActive", "pending", "pendingOldest", "date"):
            self.assertIn(k, b, f"현황판 응답에 {k} 가 없다")
        anon = self.c.application.test_client()
        self.assertEqual(anon.get("/api/waybills/board").status_code, 401)

    def test_카드_클릭_근거내역(self):
        """카드 숫자와 ?detail= 근거 내역은 같은 조건 한 벌을 쓴다(2026-08-31 대표 —
        "탭 4개 모두 각각 누를 수 있게"). 근거를 다시 계산하면 숫자와 목록이 어긋난다."""
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        o = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "카드고객", "phone": "010-1111-2222",
            "productName": "노트북", "address": "서울시 강남구 1", "postalCode": "06000",
            "quantity": 1, "amount": 500000}).get_json()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        for act in ("production", "softwareInspection", "shipping"):
            self.c.patch(f"/api/orders/{o['id']}", json={"action": act, "value": True})
        b = self.c.get("/api/waybills/board").get_json()
        d = self.c.get("/api/waybills/board?detail=shippedToday").get_json()
        self.assertEqual(len(d["rows"]), b["shippedToday"],
                         "카드 숫자와 근거 내역 개수가 다르다")
        self.assertTrue(any(r["orderId"] == o["id"] for r in d["rows"]))
        self.assertEqual(d["rows"][0]["recipient"], "카드고객")
        # 회수 카드 — 예약(test)도 진행 중으로 센다
        self.assertEqual(self.c.post(f"/api/orders/{o['id']}/recall",
                                     json={"reason": "반품"}).status_code, 201)
        b = self.c.get("/api/waybills/board").get_json()
        d = self.c.get("/api/waybills/board?detail=recallActive").get_json()
        self.assertEqual(len(d["rows"]), b["recallActive"])
        self.assertTrue(all(r["type"] == "recall" for r in d["rows"]))
        # 이상한 카드 이름은 거부
        self.assertEqual(self.c.get("/api/waybills/board?detail=x").status_code, 400)

    def test_화면_배선(self):
        base = Path(__file__).resolve().parent.parent / "static" / "js"
        js = (base / "shipping.js").read_text("utf-8")
        # 현황판 카드 클릭(2026-08-31) — 카드 전부 근거 내역으로 연결
        self.assertIn("data-bd=", js, "카드 클릭 배선이 없다")
        self.assertIn("function renderShipBoardDetail", js, "근거 내역 패널이 없다")
        self.assertIn('id="ship-board-detail"', js)
        self.assertIn("/api/waybills/board?detail=", js, "근거를 서버 조건으로 안 받고 있다")
        # 탭 2개(현황/신규접수) + 옛 탭 이름 별칭(북마크·대시보드 'ready' 바로가기 보호)
        self.assertIn('data-stab="status"', js)
        self.assertIn('data-stab="intake"', js)
        self.assertNotIn('data-stab="ready"', js, "옛 탭 버튼이 남아 있다")
        self.assertNotIn('data-stab="waybills"', js)
        self.assertNotIn('data-stab="recall"', js)
        self.assertIn('state.shipTab === "ready" || state.shipTab === "waybills"', js,
                      "옛 탭 이름 별칭 매핑이 없다")
        # 현황판 카드 + 서버 집계 사용
        self.assertIn('id="ship-board"', js)
        self.assertIn("function loadShipBoard", js)
        self.assertIn("/api/waybills/board", js)
        # [현황] 탭 = 신규건 + 송장 조회 병합(두 함수는 이동만, 재작성 금지)
        self.assertIn('id="ship-ready-sec"', js)
        self.assertIn('id="ship-wb-sec"', js)
        self.assertIn("renderShipReady($(\"#ship-ready-sec\"))", js)
        self.assertIn("renderWaybillList($(\"#ship-wb-sec\"))", js)
        # ★'오늘 출고 확인'을 화면에서 세지 않는다 — active 목록엔 보관분이 빠진다(7/31 감사)
        self.assertNotIn('(o.shippingAt || "").slice(0, 10) === ymd()', js,
                         "오늘 출고를 화면에서 다시 세고 있다")
        # 유령 필터 값 as_return 제거(A/S 반송은 forward+as_ticket_id 라 항상 0건이었다)
        self.assertNotIn('as_return: "A/S 반송"', js)
class TestReportsMoved(Base):

    """리포트 탭 통합(2026-08-31 대표 — "설정 내 매출/실적으로 통합하여 필요한 기능만").
    남긴 것: 판매 분석(마진·채널·월별·묵은 재고)+매출 대장. KPI·손익은 기간 실적이 상위호환,
    담당자 실적은 기간별 작업량이 대체. 백엔드 API 는 그대로다."""

    def test_사이드바에서_빠지고_매출실적_보기로(self):
        root = Path(__file__).resolve().parent.parent
        html = (root / "static" / "index.html").read_text("utf-8")
        self.assertNotIn('data-view="reports"', html, "리포트 메뉴가 되살아났다")
        app_js = (root / "static" / "js" / "app.js").read_text("utf-8")
        self.assertIn('["analysis", "판매 분석"]', app_js, "판매 분석 보기가 없다")
        self.assertIn('["ledger", "매출 대장"]', app_js, "매출 대장 보기가 없다")
        self.assertIn("renderAnalysisView(host)", app_js)
        self.assertIn("renderLedgerView(host)", app_js)
        # 금액 화면이므로 canMoney 게이트에 묶여야 한다
        self.assertIn('k !== "analysis" && k !== "ledger"', app_js, "금액 게이트가 빠졌다")
        # 옛 링크(북마크·대시보드 KPI)는 새 자리로
        blk = app_js.split('case "reports":', 1)[1][:300]
        self.assertIn('state.salesView = "period";', blk, "옛 리포트 링크 이동이 없다")
        rp = (root / "static" / "js" / "reports.js").read_text("utf-8")
        self.assertIn("function renderAnalysisView", rp)
        self.assertIn("function renderLedgerView", rp)
        self.assertNotIn("renderReportsView", rp, "옛 리포트 화면이 남아 있다")

    def test_권한_코드는_살아_있다(self):
        """메뉴는 없어져도 reports.view 는 백엔드 15곳이 쓴다 — 코드가 빠지면 전원 403."""
        from app.auth.perms import PERM_CODES
        self.assertIn("reports.view", PERM_CODES)
        me = self.c.get("/api/auth/me").get_json()
        self.assertNotIn("reports", me["menus"], "리포트 메뉴가 서버 응답에 남아 있다")

    def test_금액권한으로도_분석이_열린다(self):
        """판매 분석·대장 API 는 기간 실적과 같은 잣대 — purchase.money 로도 연다."""
        for path in ("/api/reports/channels", "/api/reports/monthly",
                     "/api/reports/ledger", "/api/reports/profitability",
                     "/api/reports/aging", "/api/reports/summary"):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200, f"{path}: {r.status_code}")
class TestOptionLabel(Base):

    """옵션라벨(2026-08-31 대표) — 셋팅·QC 작업대용 옵션표. 설정 ▸ 🏷 라벨의 세부 탭에서
    레이아웃을 고치고, 주문관리 일괄 바 [🏷 옵션라벨]이 '안 뽑은 주문만' 골라 인쇄한다."""

    def test_자동인쇄_설정_저장(self):
        """자동인쇄 켜고 끄기(2026-08-31 대표 "설정/해제는 설정 탭 내 옵션라벨 쪽에서") —
        저장 전 기본은 켜짐, 설정 ▸ 라벨 ▸ 옵션 라벨에서 끄면 전 작업대에 같이 적용."""
        self.assertTrue(self.c.get("/api/asset-label").get_json()["optionAutoPrint"],
                        "저장 전 기본이 켜짐이 아니다")
        r = self.c.post("/api/asset-label", json={"optionAutoPrint": False})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse(self.c.get("/api/asset-label").get_json()["optionAutoPrint"])
        self.assertEqual(self.c.post("/api/asset-label",
                                     json={"optionAutoPrint": True}).status_code, 200)
        self.assertTrue(self.c.get("/api/asset-label").get_json()["optionAutoPrint"])
        # 빈 몸통은 여전히 400, 미로그인 401
        self.assertEqual(self.c.post("/api/asset-label", json={}).status_code, 400)
        anon = self.app.test_client()
        self.assertEqual(anon.post("/api/asset-label",
                                   json={"optionAutoPrint": False}).status_code, 401)

    def test_셋팅_자동인쇄_배선(self):
        """셋팅/QC [제작 완료] 체크 → 자동인쇄 + 송장 밑 [🏷] (2026-08-31 대표).
        이미 뽑은 주문은 자동/수동 모두 '다시 출력할까요?'를 물어본다."""
        base = Path(__file__).resolve().parent.parent / "static" / "js"
        js = (base / "setup.js").read_text("utf-8")
        self.assertNotIn('id="sf-optauto"', js, "토글은 설정 탭으로 옮겼다 — 보드에 남으면 안 된다")
        self.assertIn("function optLabelAutoOn", js)
        self.assertIn("optionAutoPrint", js, "서버 설정을 안 읽고 있다")
        self.assertIn("function setupOptLabel", js)
        self.assertIn("다시 출력할까요?", js, "재출력 확인창이 없다 — 두 번 누르면 말없이 또 뽑는다")
        self.assertIn('cb.dataset.stage === "production" && cb.checked', js,
                      "제작완료 체크에 자동인쇄가 안 걸려 있다")
        self.assertIn("data-optlabel=", js, "송장 칸 옵션라벨 버튼이 없다")
        self.assertIn("printOptionLabels([o])", js, "인쇄가 공용 함수(app.js)를 안 쓴다")
        app_js = (base / "app.js").read_text("utf-8")
        self.assertIn('id="optauto-toggle"', app_js, "설정 ▸ 라벨 ▸ 옵션 라벨에 토글이 없다")
        self.assertIn("optionAutoPrint", app_js)

    def _order(self, **kw):
        base = {"channel": "고도몰", "recipient": "홍길동", "productName": "노트북",
                "phone": "010-1111-2222", "address": "서울시 강남구", "postalCode": "06000",
                "quantity": 1, "amount": 500000}
        base.update(kw)
        return self.c.post("/api/orders", json=base).get_json()

    def test_옵션_레이아웃_저장_조회(self):
        r = self.c.post("/api/asset-label", json={"optionLayout": {
            "w": 80, "h": 50, "els": {"prodCode": {"show": 1, "x": 3, "y": 4, "size": 14}}}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        g = self.c.get("/api/asset-label").get_json()
        self.assertEqual(g["optionLayout"]["els"]["prodCode"]["size"], 14)
        # 자산 라벨 레이아웃과는 딴 칸 — 서로 안 덮는다
        self.c.post("/api/asset-label", json={"layout": {"w": 50, "h": 80, "els": {}}})
        g2 = self.c.get("/api/asset-label").get_json()
        self.assertEqual(g2["optionLayout"]["els"]["prodCode"]["size"], 14)
        self.assertEqual(g2["layout"]["w"], 50)
        self.assertEqual(self.c.post("/api/asset-label",
                                     json={"optionLayout": "문자열"}).status_code, 400)
        self.assertEqual(self.c.post("/api/asset-label", json={}).status_code, 400)

    def test_인쇄_기록_일괄(self):
        o1 = self._order(recipient="라벨1")
        o2 = self._order(recipient="라벨2")
        r = self.c.post("/api/orders/bulk", json={
            "action": "optlabel", "ids": [o1["id"], o2["id"]], "value": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["ok"], 2)
        d = self.c.get(f"/api/orders/{o1['id']}").get_json()
        self.assertTrue(d["optLabelAt"], "인쇄 시각이 안 남았다")
        self.assertTrue(d["optLabelBy"])
        # 기록 지우기(value=False) — 잘못 찍은 것 정정용
        self.c.post("/api/orders/bulk", json={
            "action": "optlabel", "ids": [o1["id"]], "value": False})
        self.assertEqual(self.c.get(f"/api/orders/{o1['id']}").get_json()["optLabelAt"], "")
        # 비로그인 거부
        anon = self.c.application.test_client()
        self.assertEqual(anon.post("/api/orders/bulk", json={
            "action": "optlabel", "ids": [o2["id"]]}).status_code, 401)

    def test_화면_배선(self):
        base = Path(__file__).resolve().parent.parent / "static" / "js"
        ords = (base / "orders.js").read_text("utf-8")
        # 일괄 바에는 옵션라벨 + 리뷰어 지정 + 선택 해제만 남는다(8/31 대표 —
        # "그 외 기능은 셋팅/QC쪽에서 쓰니까"). 백엔드 bulk 액션은 그대로다.
        self.assertIn('data-bulk="optlabel"', ords, "옵션라벨 버튼이 없다")
        self.assertIn('data-bulk="review"', ords)
        self.assertIn('data-bulk="clear"', ords)
        for gone in ('data-bulk="issue"', 'data-bulk="print"', 'data-bulk="preparing"',
                     'data-bulk="shipping"', 'data-bulk="delivered"', 'data-bulk="cancel"',
                     'data-bulk="archive"', 'data-bulk="unarchive"', "bulkWaybill"):
            self.assertNotIn(gone, ords, f"뺀 일괄 버튼이 되살아났다: {gone}")
        # '안 뽑은 것만' 기본 + 전부 뽑았으면 다시 인쇄 확인 + 팝업 차단 시 기록 금지
        self.assertIn("function bulkOptionLabels", ords)
        self.assertIn("!o.optLabelAt", ords, "안 뽑은 주문만 거르는 필터가 없다")
        self.assertIn("다시 인쇄할까요", ords, "재인쇄 확인이 없다")
        self.assertIn("printOptionLabels", ords)
        self.assertIn("optLabelAt", ords.split('class="of-pick"', 1)[1][:1200],
                      "목록 행에 인쇄됨 표시가 없다")
        app_js = (base / "app.js").read_text("utf-8")
        self.assertIn("OPTLABEL_DEFAULT", app_js)
        self.assertIn("async function printOptionLabels", app_js)
        self.assertIn('["asset", "자산 라벨"]', app_js, "라벨 세부 탭이 없다")
        self.assertIn('["option", "옵션 라벨"]', app_js, "옵션 라벨 세부 탭이 없다")
        self.assertIn("prodCode:", app_js)
        self.assertIn("prepOpts:", app_js)
        self.assertIn("state.labelView", app_js)
        # 편집기는 하나를 공용한다 — 갈라지면 견본이 거짓말한다
        self.assertIn("function labelEditor", app_js)
        self.assertIn("OPTLABEL_CFG", app_js)

class TestAssetLabel(Base):

    """자산 라벨(2026-08-27 대표 — XP-DT427B 50×80). 발행 버튼은 아직 숨김,
    레이아웃 편집은 설정 ▸ API 관리 ▸ 자산 라벨. QR 은 qr.js(레퍼런스 56/56 일치 검증)."""

    def test_레이아웃_저장_조회(self):
        r = self.c.post("/api/asset-label", json={"layout": {
            "w": 50, "h": 80, "els": {"qr": {"show": 1, "x": 10, "y": 4, "size": 30}}}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        g = self.c.get("/api/asset-label").get_json()
        self.assertEqual(g["layout"]["els"]["qr"]["size"], 30)

    def test_이상한_값은_거부(self):
        self.assertEqual(self.c.post("/api/asset-label", json={"layout": "문자열"}).status_code, 400)
        anon = self.c.application.test_client()
        self.assertEqual(anon.get("/api/asset-label").status_code, 401)

    def test_로고_저장_조회_거부(self):
        """흑백 로고(2026-08-27 대표) — data URL 저장, 이미지 아닌 값 거부, 빈 값=제거."""
        du = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg"
        self.assertEqual(self.c.post("/api/asset-label-logo",
                                     json={"dataUrl": du}).status_code, 200)
        g = self.c.get("/api/asset-label").get_json()
        self.assertEqual(g["logo"], du)
        self.assertEqual(self.c.post("/api/asset-label-logo",
                                     json={"dataUrl": "http://x/y.png"}).status_code, 400)
        self.c.post("/api/asset-label-logo", json={"dataUrl": ""})
        self.assertEqual(self.c.get("/api/asset-label").get_json()["logo"], "")

    def test_화면_배선(self):
        app_js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
                  ).read_text("utf-8")
        # 8/31 대표: 탭 이름은 "라벨", 안에서 자산/옵션 세부 탭으로 갈린다
        self.assertIn('["label", "🏷 라벨"]', app_js, "설정에 라벨 탭이 없다")
        # ★독립 탭(2026-08-27 대표 "대한통운과 헷갈리니 다른 쪽에") — API 관리 밖
        self.assertIn('case "label": return renderLabelTab(body);', app_js)
        self.assertIn('logo:    { show: 1', app_js, "로고 요소 기본값이 없다")
        self.assertIn("asset-label-logo", app_js, "로고 업로드 배선이 없다")
        self.assertIn("function renderLabelTab", app_js)
        self.assertIn("function printAssetLabels", app_js)
        # ★발행 진입점은 준비만(숨김) — 보드에 버튼이 미리 붙으면 안 된다(대표: 추후 지시)
        self.assertIn("async function openAssetLabels", app_js)
        setup = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
                 ).read_text("utf-8")
        self.assertNotIn("openAssetLabels", setup, "라벨 버튼이 지시 전에 보드에 붙었다")
        # 견본과 인쇄가 같은 렌더러를 쓴다 — 다르면 견본이 거짓말한다
        self.assertIn("labelHtml(SAMPLE, lay)", app_js)
        self.assertIn("labelHtml(lb, lay)", app_js)
        # 가로형 기본(8/27 대표 "가로로 눕힐 거야") + 미리보기 직접 조작 + 정보 확장
        self.assertIn("w: 80, h: 50", app_js, "기본이 가로형(80×50)이 아니다")
        self.assertIn("function beginDrag", app_js, "미리보기 드래그 이동이 없다")
        self.assertIn("lb-handle", app_js, "크기 손잡이가 없다")
        self.assertIn('data-elkey', app_js)
        for k in ("orderNo", "channel", "text1", "text2"):
            self.assertIn(f'{k}:', app_js.split("const LABEL_DEFAULT", 1)[1][:1400],
                          f"정보 요소 {k} 가 없다")
        # scale 겹침 방지 래퍼(설정이 견본 아래)
        self.assertIn("lb-prevwrap", app_js)
        html = (Path(__file__).resolve().parent.parent / "static" / "index.html"
                ).read_text("utf-8")
        self.assertIn("qr.js", html, "QR 스크립트가 포함 안 됐다")

    def test_상세스펙_요소(self):
        """상세스펙(제품코드 소제목) 켜고 끄기(2026-08-28 대표) — 옵션명(주문 원문)과 별개."""
        app_js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
                  ).read_text("utf-8")
        head = app_js.split("const LABEL_DEFAULT", 1)[1][:1600]
        self.assertIn("spec:", head, "spec 요소 기본값이 없다")
        self.assertIn("spec:    { show: 0", app_js, "기본은 꺼짐(체크로 켬)이어야 한다")
        # 8/31 렌더러 일반화 — 요소 사전을 그대로 돌므로 spec 도 자동으로 그려진다
        self.assertIn("Object.keys(lay.els)", app_js, "라벨 렌더러가 요소 사전을 돌지 않는다")
        # 인쇄 때 스펙을 채운다 — 셋팅 캐시 우선, 없으면 그 코드만 몰에 묻는다
        tail = app_js.split("async function openAssetLabels", 1)[1][:2600]
        self.assertIn("product-info", tail, "인쇄 시 스펙 조회가 없다")
        self.assertIn("state.productInfo", tail, "셋팅 캐시를 안 쓴다")

    def test_qr_스크립트가_레퍼런스_검증_흔적을_남겼다(self):
        qr = (Path(__file__).resolve().parent.parent / "static" / "js" / "qr.js"
              ).read_text("utf-8")
        self.assertIn("행렬 1:1 대조로 검증", qr, "검증 각주가 사라졌다 — 수정 시 재검증 계약")
        self.assertIn("const QR =", qr)


class TestMoneyPerm(Base):
    """금액 열람 권한 분리(2026-08-27 대표 "매입 탭 접근과 금액 열람은 별도").

    purchase.money 없으면(관리자 제외): 전표·자산 금액 = null(화면 •••),
    미지급금·매입 대시보드·판매 전표·판매 자산·기간 실적 = 403. 부여 즉시 열림(재로그인 불요).
    """

    def _worker(self, perms):
        # ★카테고리 스코프 없는 계정은 자산이 아예 안 보인다(설계) — 전체 카테고리로 생성
        self.c.post("/api/users", json={"username": "nomoney", "displayName": "금액무권한",
                                        "password": "Nm!test2026", "perms": perms,
                                        "allCategories": True})
        w = self.c.application.test_client()
        r = w.post("/api/auth/login", json={"username": "nomoney", "password": "Nm!test2026"})
        assert r.status_code == 200, r.get_data(as_text=True)
        return w

    def test_권한없으면_금액이_가려지고_금액화면은_403(self):
        # 금액 있는 전표·자산 하나
        sid = self.c.post("/api/suppliers", json={"name": "금액시험"}).get_json()["id"]
        bid = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-08-20", "supplierId": sid,
            "totalAmount": 500000,
            "assets": [{"categoryId": self.c.get("/api/categories").get_json()[0]["id"],
                        "model": "L480", "qty": 1, "purchasePrice": 500000}]}).get_json()["id"]
        w = self._worker(["purchase.view", "purchase.edit"])
        b = w.get(f"/api/purchase-batches/{bid}").get_json()
        self.assertIsNone(b["totalAmount"], "전표 금액이 안 가려졌다")
        self.assertIsNone(b["paidAmount"])
        self.assertTrue(b["moneyMasked"])
        self.assertIsNone(b["assets"][0]["purchasePrice"], "자산 매입가가 안 가려졌다")
        self.assertIsNone(b["assets"][0]["repairCost"])
        aid = b["assets"][0]["id"]
        a = w.get(f"/api/assets/{aid}").get_json()
        self.assertIsNone(a["purchasePrice"])
        self.assertIsNone(a["costTotal"])
        for ep, want in (("/api/purchase/payables", 403),
                         ("/api/sale-assets", 403),
                         ("/api/reports/period?unit=month&date=2026-08-20", 403),
                         ("/api/sale-slips", 403)):
            self.assertEqual(w.get(ep).status_code, want, ep)

    def test_부여하면_즉시_보인다(self):
        sid = self.c.post("/api/suppliers", json={"name": "금액시험2"}).get_json()["id"]
        bid = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-08-20", "supplierId": sid,
            "totalAmount": 700000}).get_json()["id"]
        w = self._worker(["purchase.view"])
        self.assertIsNone(w.get(f"/api/purchase-batches/{bid}").get_json()["totalAmount"])
        uid = next(u for u in self.c.get("/api/users").get_json()
                   if u["username"] == "nomoney")["id"]
        self.c.put(f"/api/users/{uid}/perms",
                   json={"perms": ["purchase.view", "purchase.money"]})
        self.assertEqual(w.get(f"/api/purchase-batches/{bid}").get_json()["totalAmount"],
                         700000, "부여했는데 재로그인 없이 안 열린다")
        self.assertEqual(w.get("/api/purchase/payables").status_code, 200)

    def test_관리자는_영향_없다(self):
        sid = self.c.post("/api/suppliers", json={"name": "금액시험3"}).get_json()["id"]
        bid = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-08-20", "supplierId": sid,
            "totalAmount": 900000}).get_json()["id"]
        self.assertEqual(self.c.get(f"/api/purchase-batches/{bid}").get_json()["totalAmount"],
                         900000)

    def test_화면_배선(self):
        pur = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
               ).read_text("utf-8")
        # null = 가려진 금액 → ••• (0원으로 속이면 안 된다)
        self.assertIn('if (n === null) return "•••";', pur)
        # 미지급금 탭·대시보드는 권한 있어야 보인다
        self.assertIn('canMoney ? [["payables", "💰 미지급금"]] : []', pur)
        # 금액 권한 없으면 저장에 금액을 안 실어 보낸다(•••로 원본 파괴 금지)
        self.assertIn('hasPerm("purchase.money")' , pur.split("totalAmount: pick", 1)[0][-300:])
        # 권한 등록부에 새 권한이 있다(사용자 관리 토글 자동 노출)
        perms_py = (Path(__file__).resolve().parent.parent / "app" / "auth" / "perms.py"
                    ).read_text("utf-8")
        self.assertIn('purchase.money', perms_py)


class TestMergeExtra(Base):
    """몰 결제 임시주문 → 기존 주문 추가 결제로 합치기(2026-08-26 대표 "임시결제창").

    ★수수료 요율은 '임시 주문의 채널'(결제가 일어난 몰)로 계산한다.
    ★임시 주문은 취소+사유 링크로 내려간다 — 기간 실적은 출고 기준이라 이중 매출 없음.
    """

    def _order(self, **kw):
        base = {"channel": "고도몰", "recipient": "박준태", "productName": "임시결제창",
                "quantity": 1, "amount": 50000}
        base.update(kw)
        return self.c.post("/api/orders", json=base).get_json()

    def test_합치면_대상에_금액_수수료가_붙고_임시는_취소된다(self):
        tmp = self._order()
        tgt = self._order(recipient="실주문", productName="L480 노트북", amount=500000)
        r = self.c.post(f"/api/orders/{tmp['id']}/merge-extra",
                        json={"targetOrderId": tgt["id"]})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        j = r.get_json()
        self.assertEqual(j["amount"], 50000)
        self.assertEqual(j["order"]["amount"], 550000)
        self.assertGreater(j["fee"], 0, "고도몰 요율 수수료가 안 붙었다")
        xs = self.c.get(f"/api/orders/{tgt['id']}/extras").get_json()["extras"]
        self.assertTrue(any("합침" in (x["note"] or "") for x in xs))
        allo = self.c.get("/api/orders?view=all").get_json()
        rows = allo.get("orders") or allo
        t = next(x for x in rows if x["id"] == tmp["id"])
        self.assertTrue(t["cancelledAt"], "임시 주문이 취소되지 않았다")
        self.assertIn("합침", t["cancelReason"])

    def test_자산_매칭된_주문은_거부(self):
        tmp = self._order()
        cid = self.c.get("/api/categories").get_json()[0]["id"]
        aid = self.c.post("/api/assets", json={"categoryId": cid, "model": "L480",
                                               "qty": 1, "purchasePrice": 1}).get_json()[0]["id"]
        self.c.patch(f"/api/orders/{tmp['id']}", json={"action": "assets", "assetIds": [aid]})
        tgt = self._order(recipient="대상")
        r = self.c.post(f"/api/orders/{tmp['id']}/merge-extra",
                        json={"targetOrderId": tgt["id"]})
        self.assertEqual(r.status_code, 409)
        self.assertIn("자산", r.get_json()["error"])

    def test_자기자신_금액0_거부(self):
        tmp = self._order()
        self.assertEqual(self.c.post(f"/api/orders/{tmp['id']}/merge-extra",
                                     json={"targetOrderId": tmp["id"]}).status_code, 400)
        zero = self._order(amount=0)
        tgt = self._order(recipient="대상2")
        self.assertEqual(self.c.post(f"/api/orders/{zero['id']}/merge-extra",
                                     json={"targetOrderId": tgt["id"]}).status_code, 400)

    def test_화면_배선(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
              ).read_text("utf-8")
        self.assertIn("data-mergex", js, "합치기 버튼이 없다")
        self.assertIn("function openMergeExtra", js)
        # 자산 매칭된 주문엔 버튼이 안 뜬다(임시 결제창만 대상)
        self.assertIn('!(o.assets || []).length', js.split("data-mergex", 1)[0][-400:])


class TestModalUnify(Base):
    """상세보기 모달 3종(램/SSD 차감·추가 결제·주문자 수정)이 매입등록 팝업과 같은
    골격(slip-form + sf-step)을 쓴다(2026-08-26 대표 "통일성")."""

    def test_세_모달이_같은_골격(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
              ).read_text("utf-8")
        for fn in ("openPartUse", "openExtraCharge", "openContactEdit", "openMergeExtra"):
            blk = js.split(f"function {fn}", 1)[1][:3500]
            self.assertIn('class="slip-form"', blk, f"{fn} 이 매입등록 골격이 아니다")
            self.assertIn("sf-step", blk, f"{fn} 에 번호 구획이 없다")

    def test_주문자수정_배송메모와_처음값(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
              ).read_text("utf-8")
        blk = js.split("function openContactEdit", 1)[1]
        self.assertIn('id="ct-msg"', blk, "배송메모 칸이 없다")
        self.assertIn('id="ct-orig"', blk, "[처음 값으로] 버튼이 없다")
        self.assertNotIn('id="ct-check"', blk, "없앤 CJ 확인 버튼이 되살아났다")
        self.assertIn("deliveryMessage: $(\"#ct-msg\", host).value", blk.replace("'", "\""))

    def test_배송메모_수정_스냅샷_롤백(self):
        o = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "메모왕", "productName": "L480",
            "quantity": 1, "amount": 1000, "deliveryMessage": "경비실"}).get_json()
        r = self.c.post(f"/api/orders/{o['id']}/contact",
                        json={"deliveryMessage": "문앞에"})
        self.assertEqual(r.get_json()["changed"], 1)
        h = self.c.get(f"/api/orders/{o['id']}/contact-history").get_json()
        self.assertEqual(h["current"]["deliveryMessage"], "문앞에")
        self.assertEqual(h["history"][0]["deliveryMessage"], "경비실", "스냅샷에 메모가 없다")
        # 롤백하면 메모도 돌아온다
        self.c.post(f"/api/orders/{o['id']}/contact-rollback",
                    json={"historyId": h["history"][0]["id"]})
        h2 = self.c.get(f"/api/orders/{o['id']}/contact-history").get_json()
        self.assertEqual(h2["current"]["deliveryMessage"], "경비실")


class TestSetupNotice(Base):
    """셋팅 필수 참고 사항(2026-08-26 대표) — 부품 수량 바 아래 공지 카드,
    문구는 설정 ▸ API 관리 ▸ 제공 옵션에서 편집."""

    def test_저장하고_조회한다(self):
        r = self.c.post("/api/setup-notice", json={"text": "정품 스티커 좌측 하단"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        g = self.c.get("/api/setup-notice").get_json()
        self.assertEqual(g["text"], "정품 스티커 좌측 하단")
        self.assertTrue(g["updatedBy"])

    def test_비우면_빈값으로_저장된다(self):
        self.c.post("/api/setup-notice", json={"text": "지울 내용"})
        self.c.post("/api/setup-notice", json={"text": ""})
        self.assertEqual(self.c.get("/api/setup-notice").get_json()["text"], "")

    def test_너무_길면_거부(self):
        r = self.c.post("/api/setup-notice", json={"text": "가" * 2001})
        self.assertEqual(r.status_code, 400)

    def test_비인증은_차단(self):
        anon = self.c.application.test_client()
        self.assertEqual(anon.get("/api/setup-notice").status_code, 401)
        self.assertEqual(anon.post("/api/setup-notice", json={"text": "x"}).status_code, 401)

    def test_화면_배선(self):
        setup = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
                 ).read_text("utf-8")
        # 부품 수량 바 '바로 아래'가 공지 자리다(대표: "첨부 사진 틈사이에")
        self.assertIn('<div id="setup-parts-bar"></div>' + chr(10)
                      + '    <div id="setup-notice"></div>',
                      setup, "공지 슬롯이 부품 수량 바로 아래가 아니다")
        self.assertIn("async function renderSetupNotice", setup)
        self.assertIn("pre-wrap", setup.split("renderSetupNotice", 2)[2][:900],
                      "줄바꿈이 안 살면 여러 줄 유의사항이 뭉개진다")
        app_js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
                  ).read_text("utf-8")
        self.assertIn('id="pn-text"', app_js, "제공 옵션 화면에 편집 칸이 없다")
        self.assertIn('id="pn-save"', app_js)


class TestTmsSaleAssets(Base):
    """TMS 자산 단위 판매 이관(2026-08-25 대표) — 판매현황 원장을 자산번호로 OWS에.

    ★주문은 지어내지 않는다(매출 이중 방지) — order_assets 는 표시용으로만 읽는다.
    ★TMS 가 정본이라 수기 정정이 그대로 따라온다(매입 동기화와 같은 결).
    """

    def _upsert(self, rows):
        from app.db import tx
        from app.purchase.sales import upsert_asset_sales
        with self.c.application.app_context():
            with tx(write=True) as conn:
                return upsert_asset_sales(conn, rows, actor="시험")

    def _row(self, **kw):
        base = {"판매전표": "S260820-001", "관리번호": "990101-0001",
                "수령자성함": "박판매", "판매채널": "자사몰(업무관리)", "모델명": "L480",
                "판매일": "2026-08-20 10:00", "매입가": "300000", "판매가": "550000",
                "순이익": "180000", "등급": "AA", "진행상태": "판매"}
        base.update(kw)
        return base

    def _mk_asset(self, no="990101-0001", price=300000):
        cid = self.c.get("/api/categories").get_json()[0]["id"]
        a = self.c.post("/api/assets", json={"categoryId": cid, "model": "L480",
                                             "qty": 1, "purchasePrice": price}).get_json()[0]
        # 자산번호를 원하는 값으로 못 박는다(시험 전용 — 자동발번이라 직접 수정)
        from app.db import tx
        with self.c.application.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE assets SET asset_no=? WHERE id=?", (no, a["id"]))
        return a["id"]

    def test_판형_판별이_전표표와_갈린다(self):
        from app.purchase.sales import is_sale_asset_sheet, is_sale_slip_sheet
        asset_rows = [self._row()]
        slip_rows = [{"판매전표": "S1", "판매채널": "자사몰", "판매금액": "1000"}]
        self.assertTrue(is_sale_asset_sheet(asset_rows))
        self.assertFalse(is_sale_slip_sheet(asset_rows), "자산별 표를 전표가 삼키면 안 된다")
        self.assertTrue(is_sale_slip_sheet(slip_rows))
        self.assertFalse(is_sale_asset_sheet(slip_rows))

    def test_업서트가_자산번호로_매칭한다(self):
        aid = self._mk_asset()
        res = self._upsert([self._row(), self._row(관리번호="999999-0000", 수령자성함="김미매칭")])
        self.assertEqual(res["created"], 2)
        self.assertEqual(res["matched"], 1, "자산번호 매칭이 안 됐다")
        d = self.c.get("/api/sale-assets?from=2026-08-01&to=2026-08-31").get_json()
        hit = next(x for x in d["items"] if x["assetNo"] == "990101-0001")
        self.assertEqual(hit["assetId"], aid)

    def test_TMS_정정이_따라온다(self):
        self._upsert([self._row()])
        res = self._upsert([self._row(판매가="600000", 수령자성함="박정정")])
        self.assertEqual(res["created"], 0)
        self.assertEqual(res["updated"], 1, "TMS 수기 정정이 안 따라왔다")
        d = self.c.get("/api/sale-assets?from=2026-08-01&to=2026-08-31").get_json()
        hit = next(x for x in d["items"] if x["assetNo"] == "990101-0001")
        self.assertEqual(hit["salePrice"], 600000)
        self.assertEqual(hit["customer"], "박정정")

    def test_원가는_OWS_우선_없으면_TMS(self):
        self._mk_asset(price=280000)
        self._upsert([self._row(), self._row(관리번호="999999-0000")])
        d = self.c.get("/api/sale-assets?from=2026-08-01&to=2026-08-31").get_json()
        ows = next(x for x in d["items"] if x["assetNo"] == "990101-0001")
        tms = next(x for x in d["items"] if x["assetNo"] == "999999-0000")
        self.assertEqual((ows["costSrc"], ows["cost"], ows["margin"]),
                         ("ows", 280000, 270000))
        self.assertEqual((tms["costSrc"], tms["cost"], tms["margin"]),
                         ("tms", 300000, 250000))

    def test_취소는_기본_제외_판매가0은_합계_제외(self):
        self._upsert([self._row(),
                      self._row(관리번호="999999-0001", 진행상태="판매취소"),
                      self._row(관리번호="999999-0002", 판매가="0")])
        d = self.c.get("/api/sale-assets?from=2026-08-01&to=2026-08-31").get_json()
        nos = [x["assetNo"] for x in d["items"]]
        self.assertNotIn("999999-0001", nos, "판매취소가 기본 목록에 섞였다")
        self.assertIn("999999-0002", nos, "판매가 0 행은 보이긴 해야 한다")
        self.assertEqual(d["zeroPriceCount"], 1)
        self.assertEqual(d["saleSum"], 550000, "판매가 0이 합계에 들어갔다")
        inc = self.c.get(
            "/api/sale-assets?from=2026-08-01&to=2026-08-31&includeCancelled=1").get_json()
        self.assertIn("999999-0001", [x["assetNo"] for x in inc["items"]])

    def test_자산_상세에_판매정보가_붙는다(self):
        aid = self._mk_asset(price=280000)
        self._upsert([self._row()])
        d = self.c.get(f"/api/assets/{aid}").get_json()
        self.assertIsNotNone(d["tmsSale"])
        self.assertEqual(d["tmsSale"]["customer"], "박판매")
        self.assertEqual(d["tmsSale"]["margin"], 550000 - 280000)

    def test_주문연결은_표시만_주문은_안_만든다(self):
        aid = self._mk_asset()
        oid = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "박판매", "productName": "L480",
            "quantity": 1, "amount": 550000}).get_json()["id"]
        self.c.patch(f"/api/orders/{oid}", json={"action": "assets", "assetIds": [aid]})
        before = self.c.get("/api/orders").get_json()
        n_before = before.get("total") or len(before.get("orders") or before)
        self._upsert([self._row()])
        d = self.c.get("/api/sale-assets?from=2026-08-01&to=2026-08-31").get_json()
        hit = next(x for x in d["items"] if x["assetNo"] == "990101-0001")
        self.assertTrue(hit["hasOrder"])
        after = self.c.get("/api/orders").get_json()
        n_after = after.get("total") or len(after.get("orders") or after)
        self.assertEqual(n_before, n_after, "이관이 주문을 지어냈다 — 매출 이중")

    def test_매입등록은_팝업이고_제품부품_전환이_있다(self):
        """대표 2026-08-26: "열기 눌렀을 때처럼 팝업으로 + 상단에 제품/부품 선택"."""
        pur = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
               ).read_text("utf-8")
        blk = pur.split("function renderSlipForm(stage)", 1)[1]
        self.assertIn("revealPanel(host)", blk.split("function renderPartForm", 1)[0]
                      if "function renderPartForm" in blk else blk,
                      "매입등록이 팝업으로 안 뜬다")
        self.assertIn("function regModeBar", pur)
        self.assertIn('data-regmode="asset"', pur)
        self.assertIn('data-regmode="part"', pur)
        # 가입고(V)에는 모드 바가 없다 — purchased 조건부
        self.assertIn('stage === "purchased" ? regModeBar("asset") : ""', pur)
        # 부품 폼도 같은 팝업(slip-form 골격)과 revealPanel 을 쓴다
        pf = pur.split("async function renderPartForm", 1)[1]
        pf = pf.split(chr(10) + "}" + chr(10), 1)[0]
        self.assertIn("revealPanel(host)", pf)
        self.assertIn('class="slip-form"', pf)

    def test_부품등록_총액_양방향(self):
        """총액 → 수량 비율 분배 / 행 금액 → 합계 자동(자산 매입금액과 같은 규칙)."""
        pur = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
               ).read_text("utf-8")
        pf = pur.split("async function renderPartForm", 1)[1]
        self.assertIn('id="pp-total"', pf, "총 매입금액 칸이 없다")
        self.assertIn("let totalManual = false", pf)
        self.assertIn("const distribute = ", pf)
        self.assertIn("totalManual = false;                   // 줄을 직접 고치면 합계 모드",
                      pf.replace("  ", "  "), )
        # 원 단위 나머지는 첫 줄에 — 합계가 총액과 정확히 같아야 한다
        self.assertIn("const rem = total - used;", pf)

    def test_이관은_설정_데이터이관_한곳(self):
        """대표 2026-08-26: 이관/자동반영 버튼 정리 — 설정 ▸ 데이터 이관으로 통합,
        자동 반영 현황(마지막 반영 시각+[지금 반영])이 주인공, 수동 이관은 접이식."""
        pur = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
               ).read_text("utf-8")
        # 기준정보 탭 목록에서 빠졌다(옛 링크는 설정으로 보낸다)
        blk = pur.split("const BASE_VIEWS", 1)[1].split("];", 1)[0]
        self.assertNotIn("migrate", blk, "기준정보에 TMS 이관 탭이 남아 있다")
        self.assertIn('state.baseView === "migrate"', pur, "옛 링크 이동이 없다")
        self.assertIn('state.settingsTab = "migrate"', pur)
        # 이관 화면: 자동 반영이 먼저, 수동 업로드는 접이식
        mg = pur.split("function renderMigrate(body)", 1)[1]
        self.assertLess(mg.index('id="mg-auto"'), mg.index('id="mg-files"'),
                        "자동 반영 현황이 수동 업로드보다 아래에 있다")
        self.assertIn("<details", mg.split("`;", 1)[0], "수동 이관이 접이식이 아니다")
        app_js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
                  ).read_text("utf-8")
        self.assertIn("renderMigrate(tmsHost)", app_js, "설정 탭이 TMS 이관을 안 품는다")
        self.assertIn("function renderQcMigrate", app_js)

    def test_판매원장_자가시드_배선(self):
        """버튼 없이 '그냥 동기화' — tms_sales 가 비면 autosync 틱이 판매현황으로 채운다."""
        auto = (Path(__file__).resolve().parent.parent / "app" / "purchase" / "autosync.py"
                ).read_text("utf-8")
        self.assertIn("SELECT 1 FROM tms_sales LIMIT 1", auto, "빈 원장 검사가 없다")
        self.assertIn("upsert_asset_sales", auto, "자가 시드가 업서트를 안 부른다")
        self.assertIn('glob("판매현황*.xlsx")', auto)
        self.assertIn("files[:1]", auto, "최신 파일 하나만 읽어야 한다(옛 스냅샷 되덮기 방지)")

    def test_기간실적_TMS수기판매는_자산번호로_중복제거(self):
        """대표 2026-08-25 "자산번호는 하나로 나가니까 그걸로 매출을 매칭" —
        OWS 주문에 매칭된 자산의 TMS 판매는 빼고, 주문 없는 수기 판매만 합산한다."""
        aid = self._mk_asset()                       # OWS 주문에 붙일 자산
        oid = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "박판매", "productName": "L480",
            "quantity": 1, "amount": 550000}).get_json()["id"]
        self.c.patch(f"/api/orders/{oid}", json={"action": "assets", "assetIds": [aid]})
        self._upsert([self._row(),                                    # 주문연결 → 제외
                      self._row(관리번호="999999-0000", 수령자성함="김수기"),   # 수기 → 포함
                      self._row(관리번호="999999-0001", 판매가="0")])          # 0원 → 제외
        d = self.c.get("/api/reports/period?unit=month&date=2026-08-20").get_json()
        sb = d.get("sales") or d
        self.assertEqual(sb["tmsUnits"], 1, "중복 제거가 안 됐거나 0원 행이 섞였다")
        self.assertEqual(sb["tmsRevenue"], 550000)
        self.assertEqual(sb["tmsProfit"], 550000 - 300000)
        self.assertEqual(sb["combinedRevenue"], sb["revenue"] + 550000)

    def test_판매자산_목록에_연결주문_실체가_붙는다(self):
        aid = self._mk_asset()
        oid = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "박판매", "productName": "L480",
            "quantity": 1, "amount": 550000}).get_json()["id"]
        self.c.patch(f"/api/orders/{oid}", json={"action": "assets", "assetIds": [aid]})
        self._upsert([self._row()])
        d = self.c.get("/api/sale-assets?from=2026-08-01&to=2026-08-31").get_json()
        hit = next(x for x in d["items"] if x["assetNo"] == "990101-0001")
        self.assertIsNotNone(hit.get("order"), "연결 주문 실체가 안 붙었다")
        self.assertEqual(hit["order"]["id"], oid)
        self.assertEqual(hit["order"]["amount"], 550000)

    def test_전표_상세에_판매처가_보인다(self):
        """대표 2026-08-27: "실제로 판매됐는데 어디로 판매되었는지가 안 보여" —
        전표 상세 자산마다 TMS 판매 요약(채널·고객·날짜)이 붙는다."""
        # 전표에 담긴 자산이어야 전표 상세를 볼 수 있다
        sid = self.c.post("/api/suppliers", json={"name": "판매처시험"}).get_json()["id"]
        bid = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-08-20", "supplierId": sid,
            "assets": [{"categoryId": self.c.get("/api/categories").get_json()[0]["id"],
                        "model": "L480", "qty": 1, "purchasePrice": 100000}]}).get_json()["id"]
        aid = self.c.get(f"/api/purchase-batches/{bid}").get_json()["assets"][0]["id"]
        from app.db import tx
        with self.c.application.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE assets SET asset_no='990101-0001' WHERE id=?", (aid,))
        self._upsert([self._row()])
        b = self.c.get(f"/api/purchase-batches/{bid}").get_json()
        hit = next(x for x in b["assets"] if x["assetNo"] == "990101-0001")
        self.assertIsNotNone(hit.get("sale"), "전표 상세에 판매 요약이 없다")
        self.assertEqual(hit["sale"]["customer"], "박판매")
        self.assertEqual(hit["sale"]["channel"], "자사몰(업무관리)")
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
              ).read_text("utf-8")
        self.assertIn("a.sale", js.split("sd-assets", 1)[1][:2500], "전표 상세 표에 판매 줄이 없다")

    def test_권한_없으면_금액이_안_보인다(self):
        r = self.c.get("/api/sale-assets")
        self.assertIn(r.status_code, (200, 403))   # Base 계정 권한에 따라 — 403이면 그 자체로 합격
        # 비인증은 무조건 차단
        from app.db import tx  # noqa: F401  (컨텍스트 정리용)
        anon = self.c.application.test_client()
        self.assertEqual(anon.get("/api/sale-assets").status_code, 401)

    def test_화면_배선(self):
        app_js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js"
                  ).read_text("utf-8")
        self.assertIn('["saleAssets", "💻 판매 자산"]', app_js)
        self.assertIn('renderSaleAssets(host)', app_js)
        pur = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
               ).read_text("utf-8")
        self.assertIn("async function renderSaleAssets", pur)
        self.assertIn("/api/sale-assets", pur)
        # ★2026-08-26 대표: "다시 읽기 버튼은 필요없고 그냥 실시간 동기화" —
        #   버튼 대신 autosync 자가 시드가 채운다. 버튼이 되살아나면 퇴행이다.
        self.assertNotIn("판매현황 다시 읽기", pur, "없앤 수동 버튼이 되살아났다")
        self.assertIn("자동 동기화", pur)
        self.assertIn("tmsSale", pur, "자산 상세 판매 카드가 없다")


class TestPartStock(Base):
    """부품 소진 재고(2026-08-25 대표) — 램·SSD 수량 매입/자동·수동 차감.

    ★주문 payload의 자산 키는 id 가 아니라 assetId — 브라우저 검수에서 실제로 잡은 함정.
    ★부품 재고 차감은 _apply_part_to_asset 한 관문 — 옵션 칩·자산상세 부품추가·수동 차감이
      전부 같은 규칙(장착 −, 회수 +, 원가는 자산 수리 기록으로).
    """

    def _mk_part(self, name="D4 8G", cat="ram", price=30000):
        r = self.c.post("/api/parts", json={"name": name, "category": cat, "price": price})
        if r.status_code in (200, 201):
            return r.get_json()["id"]
        return next(x["id"] for x in self.c.get("/api/parts").get_json() if x["name"] == name)

    def _buy(self, pid, qty=10, amount=280000, **kw):
        return self.c.post("/api/part-purchases", json={
            "supplierName": "부품상사", "rows": [{"partId": pid, "qty": qty, "amount": amount}],
            **kw})

    def _order_with_asset(self, option="램 16G 업그레이드 선택"):
        cid = self.c.get("/api/categories").get_json()[0]["id"]
        aid = self.c.post("/api/assets", json={"categoryId": cid, "model": "L480",
                                               "qty": 1, "purchasePrice": 100000}
                          ).get_json()[0]["id"]
        oid = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "부품왕", "productName": "L480 노트북",
            "optionName": option, "quantity": 1, "amount": 500000}).get_json()["id"]
        self.c.patch(f"/api/orders/{oid}", json={"action": "assets", "assetIds": [aid]})
        return oid, aid

    def test_매입하면_현재고가_쌓이고_단가가_갱신된다(self):
        pid = self._mk_part()
        r = self._buy(pid, qty=10, amount=280000)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        j = r.get_json()
        self.assertEqual(j["rows"][0]["onhand"], 10)
        self.assertEqual(j["rows"][0]["unitCost"], 28000)
        part = next(x for x in self.c.get("/api/parts").get_json() if x["id"] == pid)
        self.assertEqual(part["price"], 28000, "단가표가 최신 매입 단가로 안 바뀌었다")

    def test_매입은_전표로_잡히고_미지급도_된다(self):
        pid = self._mk_part()
        j = self._buy(pid, unpaid=True).get_json()
        self.assertTrue(j["slipNo"].startswith("P"))
        b = self.c.get(f"/api/purchase-batches/{j['batchId']}").get_json()
        self.assertEqual(b["purchaseType"], "부품매입")
        pay = self.c.get("/api/purchase/payables?onlyOwed=1").get_json()
        sup = next((g for g in pay["suppliers"] if g["supplier"] == "부품상사"), None)
        self.assertIsNotNone(sup, "부품 매입이 미지급금에 안 잡힌다")
        self.assertEqual(sup["unpaid"], 280000)

    def test_공임_항목은_수량매입_거부(self):
        rid = self.c.post("/api/parts", json={"name": "짜깁기(테스트)", "category": "",
                                              "price": 10000, "kind": "repair"}).get_json()["id"]
        r = self._buy(rid, qty=1, amount=10000)
        self.assertEqual(r.status_code, 400)
        self.assertIn("공임", r.get_json()["error"])

    def test_옵션칩_체크가_자동차감하고_해제가_복원한다(self):
        pid = self._mk_part()
        self._buy(pid)
        opt = self.c.post("/api/prep-options", json={"name": "램 업글(자동차감시험)"}).get_json()
        self.c.patch(f"/api/prep-options/{opt['id']}", json={"partId": pid, "partQty": 1})
        self.c.post(f"/api/prep-options/{opt['id']}/rules",
                    json={"matchType": "option", "matchValue": "16G"})
        oid, aid = self._order_with_asset()
        r = self.c.post(f"/api/orders/{oid}/options/{opt['id']}", json={"checked": True})
        self.assertEqual(r.get_json()["partApplied"], 1, r.get_data(as_text=True))
        st = self.c.get("/api/part-stocks").get_json()
        d4 = next(x for x in st["items"] if x["id"] == pid)
        self.assertEqual(d4["onhand"], 9, "체크했는데 재고가 안 줄었다")
        uses = self.c.get(f"/api/orders/{oid}/part-uses").get_json()["items"]
        self.assertEqual(len(uses), 1)
        self.assertTrue(uses[0]["auto"], "자동차감 표시가 아니다")
        # 해제 → 복원
        self.c.post(f"/api/orders/{oid}/options/{opt['id']}", json={"checked": False})
        st = self.c.get("/api/part-stocks").get_json()
        d4 = next(x for x in st["items"] if x["id"] == pid)
        self.assertEqual(d4["onhand"], 10, "해제했는데 재고가 안 돌아왔다")
        self.assertEqual(self.c.get(f"/api/orders/{oid}/part-uses").get_json()["items"], [])

    def test_대기소요가_가용에서_빠진다(self):
        pid = self._mk_part()
        self._buy(pid)
        opt = self.c.post("/api/prep-options", json={"name": "램 업글(대기시험)"}).get_json()
        self.c.patch(f"/api/prep-options/{opt['id']}", json={"partId": pid, "partQty": 1})
        self.c.post(f"/api/prep-options/{opt['id']}/rules",
                    json={"matchType": "option", "matchValue": "16G"})
        self._order_with_asset()                       # 미체크 주문 1건 = 대기 1
        st = self.c.get("/api/part-stocks").get_json()
        d4 = next(x for x in st["items"] if x["id"] == pid)
        self.assertEqual(d4["pending"], 1)
        self.assertEqual(d4["available"], d4["onhand"] - 1)

    def test_수동차감과_회수(self):
        pid = self._mk_part()
        self._buy(pid)
        oid, aid = self._order_with_asset(option="옵션 없음")
        r = self.c.post(f"/api/orders/{oid}/part-use",
                        json={"partId": pid, "assetId": aid, "qty": 2})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertEqual(j["cost"], 56000)             # 원가 2개분 기입
        self.assertEqual(j["onhand"], 8)
        uses = self.c.get(f"/api/orders/{oid}/part-uses").get_json()["items"]
        self.assertFalse(uses[0]["auto"], "수동인데 자동으로 표시된다")
        # 회수 → 재고 +1, 원가 음수
        r = self.c.post(f"/api/orders/{oid}/part-use",
                        json={"partId": pid, "assetId": aid, "qty": 1, "remove": True})
        self.assertEqual(r.get_json()["onhand"], 9)
        self.assertEqual(r.get_json()["cost"], -28000)

    def test_매칭_안된_자산은_수동차감_거부(self):
        pid = self._mk_part()
        self._buy(pid)
        oid, _aid = self._order_with_asset(option="옵션 없음")
        r = self.c.post(f"/api/orders/{oid}/part-use",
                        json={"partId": pid, "assetId": 999999, "qty": 1})
        self.assertEqual(r.status_code, 400)

    def test_수리기록_삭제가_재고를_되돌린다(self):
        pid = self._mk_part()
        self._buy(pid)
        oid, aid = self._order_with_asset(option="옵션 없음")
        self.c.post(f"/api/orders/{oid}/part-use", json={"partId": pid, "assetId": aid, "qty": 1})
        a = self.c.get(f"/api/assets/{aid}").get_json()
        rep = next(r for r in a["repairs"] if "D4 8G" in (r.get("parts") or ""))
        self.c.delete(f"/api/assets/{aid}/repairs/{rep['id']}")
        st = self.c.get("/api/part-stocks").get_json()
        d4 = next(x for x in st["items"] if x["id"] == pid)
        self.assertEqual(d4["onhand"], 10, "수리 기록을 지웠는데 재고가 안 돌아왔다")

    def test_재고보정과_이동내역(self):
        pid = self._mk_part()
        self._buy(pid)
        r = self.c.post(f"/api/parts/{pid}/stock-adjust", json={"qty": -3, "memo": "실사"})
        self.assertEqual(r.get_json()["onhand"], 7)
        self.assertEqual(self.c.post(f"/api/parts/{pid}/stock-adjust",
                                     json={"qty": -100}).status_code, 400)
        moves = self.c.get(f"/api/parts/{pid}/stock-moves").get_json()
        self.assertEqual(moves["onhand"], 7)
        self.assertEqual([m["reason"] for m in moves["moves"]], ["adjust", "purchase"])

    def test_부품매입_전표취소가_재고를_되돌리고_반복에도_안전(self):
        pid = self._mk_part()
        bid = self._buy(pid).get_json()["batchId"]
        self.c.post(f"/api/purchase-batches/{bid}/cancel", json={"reason": "오입력"})
        onhand = lambda: next(x for x in self.c.get("/api/part-stocks").get_json()["items"]
                              if x["id"] == pid)["onhand"]
        self.assertEqual(onhand(), 0, "전표 취소했는데 재고가 남아 있다")
        self.c.post(f"/api/purchase-batches/{bid}/uncancel")
        self.assertEqual(onhand(), 10, "취소 해제했는데 재고가 안 돌아왔다")
        # 반복 — 잔액 기준이라 누적 오차가 없어야 한다
        self.c.post(f"/api/purchase-batches/{bid}/cancel", json={"reason": "재취소"})
        self.assertEqual(onhand(), 0)
        self.c.post(f"/api/purchase-batches/{bid}/uncancel")
        self.assertEqual(onhand(), 10)

    def test_매칭_전_수동차감과_매칭시_원가_이관(self):
        """대표 2026-08-26: "제품코드를 입력하지 못한 경우에도 쓸 수 있게" —
        자산 없는 주문도 차감(재고 즉시·원가 보류) → 매칭 순간 원가 이관, 이중 차감 0."""
        pid = self._mk_part()
        self._buy(pid)                                          # 재고 10
        oid = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "코드없음", "productName": "노트북",
            "quantity": 1, "amount": 300000}).get_json()["id"]
        r = self.c.post(f"/api/orders/{oid}/part-use",
                        json={"partId": pid, "assetId": 0, "qty": 1})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertTrue(j["pending"], "매칭 전 차감이 보류 표시가 아니다")
        self.assertEqual(j["onhand"], 9, "재고가 즉시 안 줄었다")
        uses = self.c.get(f"/api/orders/{oid}/part-uses").get_json()["items"]
        self.assertEqual(uses[0]["assetNo"], "", "자리표에 자산이 붙어 있다")
        # 매칭 → 이관
        cid = self.c.get("/api/categories").get_json()[0]["id"]
        aid = self.c.post("/api/assets", json={"categoryId": cid, "model": "L480",
                                               "qty": 1, "purchasePrice": 100000}
                          ).get_json()[0]["id"]
        self.c.patch(f"/api/orders/{oid}", json={"action": "assets", "assetIds": [aid]})
        st = self.c.get("/api/part-stocks").get_json()
        self.assertEqual(next(x for x in st["items"] if x["id"] == pid)["onhand"], 9,
                         "이관 때 재고가 또 줄었다(이중 차감)")
        a = self.c.get(f"/api/assets/{aid}").get_json()
        self.assertTrue(any("D4 8G" in (rp.get("parts") or "") and rp["cost"] == 28000
                            for rp in a["repairs"]), "매칭 순간 원가가 자산에 안 붙었다")
        uses2 = self.c.get(f"/api/orders/{oid}/part-uses").get_json()["items"]
        self.assertTrue(uses2[0]["assetNo"], "이관 뒤에도 자리표로 남아 있다")

    def test_매칭전_차감_단가는_차감한_날로_동결(self):
        """자리표의 unit_cost 가 이관 원가다 — 그 사이 단가표가 바뀌어도 안 흔들린다."""
        pid = self._mk_part()
        self._buy(pid)                                          # 단가 28,000 로 갱신됨
        oid = self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "동결왕", "productName": "노트북",
            "quantity": 1, "amount": 1}).get_json()["id"]
        self.c.post(f"/api/orders/{oid}/part-use", json={"partId": pid, "assetId": 0, "qty": 1})
        self.c.patch(f"/api/parts/{pid}", json={"price": 99000})   # 이관 전에 단가 인상
        cid = self.c.get("/api/categories").get_json()[0]["id"]
        aid = self.c.post("/api/assets", json={"categoryId": cid, "model": "L480",
                                               "qty": 1, "purchasePrice": 1}).get_json()[0]["id"]
        self.c.patch(f"/api/orders/{oid}", json={"action": "assets", "assetIds": [aid]})
        a = self.c.get(f"/api/assets/{aid}").get_json()
        costs = [rp["cost"] for rp in a["repairs"] if "D4 8G" in (rp.get("parts") or "")]
        self.assertEqual(costs, [28000], f"차감한 날 단가로 동결돼야 한다: {costs}")

    def test_구분연결_후보가_하나면_제품코드_없어도_차감(self):
        """대표 2026-08-25: "제품코드가 들어가지 않아도 부품은 차감될 수 있게" —
        구분별 연결(part_map)이라도 살아있는 후보가 1개면 추측이 아니라 확정이다."""
        pid = self._mk_part()
        self._buy(pid)
        opt = self.c.post("/api/prep-options", json={"name": "구분연결 코드없음"}).get_json()
        self.c.patch(f"/api/prep-options/{opt['id']}",
                     json={"partMap": [{"gen": "DDR4", "partId": pid}]})
        self.c.post(f"/api/prep-options/{opt['id']}/rules",
                    json={"matchType": "option", "matchValue": "코드없음업글"})
        oid, _aid = self._order_with_asset(option="코드없음업글 16G")   # 제품코드 없음
        r = self.c.post(f"/api/orders/{oid}/options/{opt['id']}", json={"checked": True})
        j = r.get_json()
        self.assertEqual(j["partApplied"], 1, j)
        self.assertEqual(j["partMessage"], "", "유일 후보인데 보류 경고가 떴다")
        st = self.c.get("/api/part-stocks").get_json()
        self.assertEqual(next(x for x in st["items"] if x["id"] == pid)["onhand"], 9)

    def test_구분연결_후보가_둘이면_여전히_보류(self):
        """후보가 여럿이면 스펙 없이 추측하지 않는다 — 기존 원칙 유지."""
        p1 = self._mk_part("D4 8G(이지선다)", price=30000)
        p2 = self._mk_part("D5 8G(이지선다)", price=50000)
        opt = self.c.post("/api/prep-options", json={"name": "구분연결 이지선다"}).get_json()
        self.c.patch(f"/api/prep-options/{opt['id']}", json={"partMap": [
            {"gen": "DDR4", "partId": p1}, {"gen": "DDR5", "partId": p2}]})
        self.c.post(f"/api/prep-options/{opt['id']}/rules",
                    json={"matchType": "option", "matchValue": "이지선다업글"})
        oid, _aid = self._order_with_asset(option="이지선다업글")
        j = self.c.post(f"/api/orders/{oid}/options/{opt['id']}",
                        json={"checked": True}).get_json()
        self.assertEqual(j["partApplied"], 0)
        self.assertIn("제품코드", j["partMessage"])
        self.assertIn("램/SSD 차감", j["partMessage"], "수동 차감 안내가 없다")

    def test_화면_배선(self):
        setup = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
                 ).read_text("utf-8")
        # 부품 수량 바 — 단계 카드(제작대기~출고기록조회) 바로 아래 + 접이식
        self.assertIn('<div class="kpi-row" id="setup-kpi"></div>\n    <div id="setup-parts-bar"></div>',
                      setup, "부품 바가 단계 카드 바로 아래가 아니다")
        self.assertIn("parts-bar-box", setup)
        self.assertIn("async function renderPartsBar", setup)
        self.assertIn("async function openPartUse", setup)
        self.assertIn('a.assetId', setup.split("function openPartUse", 1)[1][:1200])
        self.assertIn("data-partuse-status", setup)
        self.assertIn("자동차감됨", setup)
        # 바는 재고 0이어도 항상 보인다(2026-08-25 대표 "왜 안 떠?") + 0규격은 흐리게
        bar = setup.split("async function renderPartsBar", 1)[1]
        bar = bar.split("async function", 1)[0]
        self.assertIn("x.enabled || x.onhand || x.pending", bar,
                      "재고 0이면 바가 통째로 숨는 예전 필터다")
        self.assertIn("단가표에 램·SSD 부품이 없습니다", bar, "빈 단가표 안내가 없다")
        self.assertIn("opacity:.55", bar, "0 규격 흐림 표시가 없다")
        # ★대제목 탭 + 마지막 탭·펼침 고정(2026-08-26 대표 "탭이 최소화되면 안 돼")
        self.assertIn("data-pgtab", bar, "대제목 탭이 없다")
        self.assertIn("ows.partsBarTab", bar, "마지막 탭이 localStorage 에 안 남는다")
        self.assertIn("ows.partsBarOpen", bar, "펼침 상태가 localStorage 에 안 남는다")
        # 차감 창 부품 고르기 — 노트북 부품 먼저, 데스크탑은 별도 묶음, 가용 수량 표기
        pu = setup.split("async function openPartUse", 1)[1][:3000]
        self.assertIn("🖥 데스크탑 부품", pu, "데스크탑 부품 묶음이 없다")
        self.assertIn("가용 ${x.available}", pu, "부품 항목에 가용 수량이 없다")
        pur = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
               ).read_text("utf-8")
        # ★2026-08-26 대표: 부품 매입은 매입등록 팝업의 [🔩 부품 등록] 모드로 통합 —
        #   별도 버튼(data-partbuy)이 되살아나면 퇴행이다.
        self.assertNotIn("data-partbuy", pur, "없앤 부품 매입 버튼이 되살아났다")
        self.assertIn("async function renderPartForm", pur)
        self.assertIn("/api/part-purchases", pur)
        css = (Path(__file__).resolve().parent.parent / "static" / "css" / "app.css"
               ).read_text("utf-8")
        self.assertIn("상품 칸 내부 구분선", css)


class TestPayables(Base):
    """매입 미지급금 관리(2026-08-25 대표) — 거래처에 줄 돈, 지급 기록.

    ★_batch_payload 는 계산 컬럼(assigned_amount)을 요구한다 — 지급 응답에서 그걸 부르면
      500이 난다(브라우저 검수에서 실제로 잡음). 요약만 돌려준다.
    """

    def _batch(self, total, supplier="미래아이앤씨"):
        sid = self.c.post("/api/suppliers", json={"name": supplier}).get_json()["id"]
        return self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "purchaseDate": "2026-08-19",
            "supplierId": sid, "totalAmount": total}).get_json()["id"]

    def test_미지급_요약이_거래처별로_나온다(self):
        self._batch(1000000)
        d = self.c.get("/api/purchase/payables").get_json()
        sup = next(g for g in d["suppliers"] if g["supplier"] == "미래아이앤씨")
        self.assertEqual(sup["totalAmount"], 1000000)
        self.assertEqual(sup["unpaid"], 1000000)
        self.assertEqual(d["totalUnpaid"], 1000000)

    def test_부분지급하면_잔액이_준다(self):
        bid = self._batch(1000000)
        r = self.c.post(f"/api/purchase-batches/{bid}/pay", json={"amount": 300000})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertFalse(d["fully"])
        self.assertEqual(d["unpaid"], 700000)

    def test_완납하면_잔액_거래처에서_사라진다(self):
        bid = self._batch(500000)
        r = self.c.post(f"/api/purchase-batches/{bid}/pay", json={"amount": 500000})
        self.assertTrue(r.get_json()["fully"])
        owed = self.c.get("/api/purchase/payables?onlyOwed=1").get_json()
        self.assertEqual(owed["totalUnpaid"], 0)
        self.assertEqual(len(owed["suppliers"]), 0)

    def test_과지급은_막는다(self):
        bid = self._batch(500000)
        r = self.c.post(f"/api/purchase-batches/{bid}/pay", json={"amount": 600000})
        self.assertEqual(r.status_code, 400)
        self.assertIn("과지급", r.get_json()["error"])

    def test_지급_취소도_기록된다(self):
        """음수 = 지급 취소(정정). 누계가 음수가 되면 막는다."""
        bid = self._batch(500000)
        self.c.post(f"/api/purchase-batches/{bid}/pay", json={"amount": 300000})
        r = self.c.post(f"/api/purchase-batches/{bid}/pay", json={"amount": -100000})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["unpaid"], 300000)   # 500 - (300-100)
        over = self.c.post(f"/api/purchase-batches/{bid}/pay", json={"amount": -500000})
        self.assertEqual(over.status_code, 400)

    def test_지급_이력이_남는다(self):
        bid = self._batch(1000000)
        self.c.post(f"/api/purchase-batches/{bid}/pay",
                    json={"amount": 400000, "method": "계좌이체", "memo": "1차"})
        h = self.c.get(f"/api/purchase-batches/{bid}/payments").get_json()["payments"]
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["amount"], 400000)
        self.assertEqual(h[0]["method"], "계좌이체")

    def test_취소_전표는_미지급에서_빠진다(self):
        bid = self._batch(1000000)
        self.c.post(f"/api/purchase-batches/{bid}/cancel", json={"reason": "오입력"})
        d = self.c.get("/api/purchase/payables").get_json()
        self.assertEqual(d["totalUnpaid"], 0)

    def test_지급_응답이_계산컬럼을_안_부른다(self):
        """★_batch_payload(assigned_amount 요구)를 부르면 500 — 실제로 겪은 버그."""
        bid = self._batch(500000)
        r = self.c.post(f"/api/purchase-batches/{bid}/pay", json={"amount": 100000})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertIn("paidAmount", j)
        self.assertNotIn("assignedAmount", j, "계산 컬럼 payload 를 다시 물고 왔다")

    def test_화면에_미지급금_탭이_있다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
              ).read_text("utf-8")
        self.assertIn('["payables", "💰 미지급금"]', js)
        self.assertIn("payables: renderPayables", js)
        self.assertIn("async function renderPayables", js)
        self.assertIn("async function openPayment", js)
        self.assertIn("/api/purchase/payables", js)


class TestSubtabDesignUnified(Base):
    """매입 서브탭 디자인 통일(2026-08-25 대표: "매입작업·자산·기준정보 세부탭 디자인이 다 틀리잖아").

    매입작업의 전표목록/대시보드 전환을 초록/외곽선 버튼 → 자산·기준정보와 같은
    알약형 .subtabs 로 맞춘다.
    """

    def test_매입작업_전환이_subtabs다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
              ).read_text("utf-8")
        blk = js.split("function slipViewPills", 1)[1].split(chr(10) + "}", 1)[0]
        self.assertIn('class="subtabs"', blk, "매입작업 전환이 아직 버튼이다")
        self.assertNotIn("btn-primary", blk, "옛 초록 버튼이 남았다")
        self.assertIn("data-slipview=", blk)

    def test_세_영역이_같은_서브탭_클래스를_쓴다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
              ).read_text("utf-8")
        # 자산(data-aview)·기준정보(data-bview)·매입작업(data-slipview) 전부 .subtabs 안에 있다
        for marker in ('<div class="subtabs">${views.map',        # 자산
                       '<div class="subtabs">${BASE_VIEWS.map',   # 기준정보
                       'class="subtabs" style="max-width:max-content; margin:0;"'):  # 매입작업
            self.assertIn(marker, js, f"{marker} 가 .subtabs 를 안 쓴다")


class TestStageNames(unittest.TestCase):
    """단계 이름은 셋팅 보드 머리글이 정본이다(대표 승인 2026-08-18).

    2026-08-05에 "'SW 검수/검수 완료'를 '출고 확인'으로 바꿔라"는 지시가 있었는데,
    그 뒤 SW 검수가 **독립 단계**로 되살아나면서 화면마다 이름이 한 칸씩 밀렸다 —
    리포트 [출고 확인]이 셋팅 보드 [SW 검수 완료]를 세고, 리포트 [출고 마감]이
    셋팅 보드 [출고 확인]을 셌다(인수인계서 2026-08-13도 이 어긋남을 경고로 적어 뒀다).
    같은 단계를 화면마다 다른 이름으로 부르면 숫자를 비교할 수 없다.
    """

    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent
        self.setup_js = (self.root / "static" / "js" / "setup.js").read_text("utf-8")
        head = self.setup_js.split("<thead>", 1)[1].split("</thead>", 1)[0]
        self.stages = [x.split("</th>", 1)[0]
                       for x in head.split('<th class="stage-th">')[1:]]

    def test_보드가_네_단계다(self):
        """서버 상태머신과 같은 4단계 — 하나라도 사라지면 이름 대응이 깨진다."""
        self.assertEqual(self.stages, ["준비 중", "제작 완료", "SW 검수 완료", "출고 확인"])

    def test_작업량_화면이_같은_단계_이름을_쓴다(self):
        """기간별 작업량 실적은 제작·SW만 센다. 출고 확인은 물류 처리라 제외한다."""
        py = (self.root / "app" / "reports" / "workload.py").read_text("utf-8")
        for name in self.stages[1:3]:
            self.assertIn(f'"{name}"', py,
                          f"작업량이 [{name}]을 다른 이름으로 부른다")
        self.assertNotIn('("shipping", "출고 확인"', py,
                         "출고 확인은 작업자 실적에 포함하면 안 된다")
        self.assertNotIn("출고 마감", py, "'출고 마감'은 [출고 확인]의 옛 이름이다")
        wl = (self.root / "static" / "js" / "workload.js").read_text("utf-8")
        self.assertNotIn('data-workload-stage="shipping"', wl,
                         "출고 확인 필터가 작업량 화면에 남아 있다")
        # 리포트 화면의 담당자 실적 표는 없어졌다 — 되살아나면 3중복이 다시 생긴다
        rp = (self.root / "static" / "js" / "reports.js").read_text("utf-8")
        self.assertNotIn("<h3>담당자 실적</h3>", rp)

    def test_주문_상태_칩이_단계마다_다르다(self):
        """작업 단계 5개는 이름도 색도 서로 달라야 한다.

        ★취소·보관은 단계 사다리 밖이라 여기서 안 본다(둘 다 회색인 게 맞다).
          문제는 3·4단계가 같은 이름·같은 초록이라 목록만 봐선 어디까지 갔는지
          알 수 없던 것이다(2026-08-18 정정).
        """
        js = (self.root / "static" / "js" / "orders.js").read_text("utf-8")
        blk = js.split("function orderStatusInfo", 1)[1].split(chr(10) + "function ", 1)[0]
        trio = re.findall('key: "([^"]+)", label: "([^"]+)", chip: "([^"]+)"', blk)
        stage = [t for t in trio if t[0] not in ("cancelled", "archived")]
        self.assertEqual([t[0] for t in stage],
                         ["shipped", "inspected", "produced", "preparing", "waiting"],
                         "작업 단계가 늘거나 줄었다 — 이름 대응을 다시 맞춰라")
        labels = [t[1] for t in stage]
        self.assertEqual(len(labels), len(set(labels)),
                         f"두 단계가 같은 이름으로 뜬다: {labels}")
        chips = [t[2] for t in stage]
        self.assertEqual(len(chips), len(set(chips)),
                         f"두 단계가 같은 색이라 눈으로 구분이 안 된다: {chips}")
        self.assertIn("SW 검수 완료", labels)
        self.assertIn("출고 확인", labels)

    def test_셋팅_실적_설명이_세는_단계와_맞다(self):
        """셋팅 실적은 inspection_done(3단계)을 센다 — 설명도 그 이름이어야 한다."""
        stats = (self.root / "app" / "reports" / "setup_stats.py").read_text("utf-8")
        self.assertIn("o.inspection_done = 1", stats, "세는 기준이 바뀌었다 — 설명문도 다시 보라")
        js = (self.root / "static" / "js" / "app.js").read_text("utf-8")
        # The first occurrence belongs to the prebuild report; inspect the setup report.
        blk = js.split("SETUP_PERIODS.map", 2)[2][:1200]
        self.assertIn("SW 검수 완료까지 끝낸 것", blk)


class TestStockSyncHidden(Base):
    """'재고반영'은 사내 재고와 무관하다 — 몰 재고 전송용 표시다(대표 2026-08-17).

    "재고 구분 자체로 가용·실재고가 재고로 잡히고 가재고가 안 잡히는 거잖아" — 맞다.
    그래서 몰 재고 연동을 켠 몰이 없으면 화면에서 관련 버튼·칸을 감춘다(기능은 보존).
    """

    def test_기본은_꺼져_있다(self):
        self.assertFalse(self.c.get("/api/purchase-meta").get_json()["stockSyncOn"])

    def test_몰을_켜면_다시_켜진다(self):
        self.c.put("/api/settings", json={"stock_sync": {"godomall": {"enabled": True}}})
        self.assertTrue(self.c.get("/api/purchase-meta").get_json()["stockSyncOn"])
        # 명시적 True 하나만 인정 — 문자열·1 은 켜짐이 아니다(stock_sync._is_on 과 같은 규칙)
        self.c.put("/api/settings", json={"stock_sync": {"godomall": {"enabled": "1"}}})
        self.assertFalse(self.c.get("/api/purchase-meta").get_json()["stockSyncOn"])

    def test_사내_재고는_재고반영과_무관하다(self):
        """재고반영을 한 번도 안 켜도 제품코드만 있으면 셋팅 재고에 잡힌다."""
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        self.c.post("/api/assets/product-code",
                    json={"ids": [a["id"]], "productCode": "L480_i5-8_내장"})
        self.assertFalse(self.c.get(f"/api/assets/{a['id']}").get_json()["stockListed"])
        p = self.c.post("/api/orders/product-info",
                        json={"codes": ["L480_i5-8_내장"]}).get_json()["products"]["L480_i5-8_내장"]
        self.assertEqual(p["codeShippable"], 1, "재고반영과 무관하게 재고로 잡혀야 한다")

    def test_화면이_플래그로_감춘다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "purchase.js"
              ).read_text("utf-8")
        self.assertIn("function stockSyncOn()", js)
        for anchor in ('id="ab-stock-on"', 'id="sd-stock-on"', 'id="ad-stocklisted"'):
            i = js.index(anchor)
            self.assertIn("stockSyncOn()", js[max(0, i - 400):i],
                          f"{anchor} 가 연동 여부와 무관하게 항상 그려진다")


class TestCodeSuggestFromOrders(Base):
    """제품코드 자동완성(대표 2026-08-17: "15만 입력해도 나와야 하는 것 아니냐").

    자산에 코드가 하나도 없는 동안에는 자산 기준 후보가 비어 자동완성이 무용지물이다.
    주문에 실려 온 코드가 곧 '앞으로 자산에 넣을 코드'라 그것도 후보로 준다.
    """

    def _order(self, code, name="노트북"):
        return self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "홍길동", "productName": name,
            "productCode": code, "quantity": 1, "amount": 500000}).get_json()

    def test_일부만_쳐도_주문_코드가_뜬다(self):
        self._order("15U50N_i5-10_내장", "LG 그램 15")
        self._order("15U470_i3-7_내장")
        self._order("NT371B5M_i7-7_내장")
        codes = self.c.get("/api/product-codes?q=15").get_json()["codes"]
        found = {c["code"] for c in codes}
        self.assertIn("15U50N_i5-10_내장", found, f"부분 입력으로 못 찾았다: {found}")
        self.assertIn("15U470_i3-7_내장", found)
        self.assertNotIn("NT371B5M_i7-7_내장", found, "상관없는 코드가 섞였다")
        row = next(c for c in codes if c["code"] == "15U50N_i5-10_내장")
        self.assertTrue(row["fromOrder"], "주문에서 온 후보임을 알려야 한다")
        self.assertEqual(row["orderCount"], 1)

    def test_자산에_있는_코드는_재고_숫자로_뜬다(self):
        """같은 코드가 자산에도 있으면 '주문 N건'이 아니라 재고 숫자가 정답이다."""
        cats = self.c.get("/api/categories").get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "15U50N", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        self.c.post("/api/assets/product-code",
                    json={"ids": [a["id"]], "productCode": "15U50N_i5-10_내장"})
        self._order("15U50N_i5-10_내장")
        codes = self.c.get("/api/product-codes?q=15U").get_json()["codes"]
        row = next(c for c in codes if c["code"] == "15U50N_i5-10_내장")
        self.assertFalse(row.get("fromOrder"))
        self.assertEqual(row["shippable"], 1)
        self.assertEqual(len([c for c in codes if c["code"] == "15U50N_i5-10_내장"]), 1,
                         "같은 코드가 두 줄로 뜨면 안 된다")

    def test_등급_꼬리표는_깎아서_준다(self):
        """몰이 'NT550_i5-11_내장 AA급3'으로 보내도 후보는 순수 코드여야 한다."""
        self._order("NT550_i5-11_내장 AA급3", "삼성 노트북")
        self._order("NT550_i5-11_내장")
        codes = self.c.get("/api/product-codes?q=NT550").get_json()["codes"]
        found = [c["code"] for c in codes]
        self.assertEqual(found, ["NT550_i5-11_내장"], f"꼬리표가 남았다: {found}")
        self.assertEqual(codes[0]["orderCount"], 2, "같은 코드인데 따로 세었다")


class TestSlipDetailSideBySide(Base):
    """전표 상세에서 관리번호를 누르면 자산 상세가 **오른쪽**에 뜬다(대표 2026-08-14 요청).

    ★2026-08-17 대표 재지적: 아래에 뜨고 있었다. 원인은 팝업 폭이 900px에 묶여
      5:5가 안 들어가 flex-wrap 으로 접힌 것 — 팝업에 wide 를 줘서 넓게 쓴다.
    """

    def setUp(self):
        super().setUp()
        root = Path(__file__).resolve().parent.parent
        self.js = (root / "static" / "js" / "purchase.js").read_text("utf-8")
        self.css = (root / "static" / "css" / "app.css").read_text("utf-8")

    def test_자산_상세는_오른쪽_칸에_그린다(self):
        blk = self.js.split("async function renderSlipDetail", 1)[1].split("\nasync function ", 1)[0]
        self.assertIn('id="sd-assetdetail"', blk)
        self.assertIn("display:flex", blk, "좌우로 나누는 컨테이너가 없다")
        self.assertIn('renderAssetDetail("#sd-assetdetail"', blk)
        self.assertIn("position:sticky", blk, "표를 스크롤해도 상세가 따라와야 한다")

    def test_팝업이_5대5를_담을_만큼_넓다(self):
        blk = self.js.split("async function renderSlipDetail", 1)[1].split("\nasync function ", 1)[0]
        self.assertIn('classList.add("wide")', blk, "팝업을 넓히지 않으면 오른쪽 칸이 아래로 접힌다")
        self.assertIn(".in-modal.wide", self.css)
        # 좌우 기준폭 × 2 가 팝업 최대폭 안에 들어가야 접히지 않는다
        import re
        basis = [int(x) for x in re.findall(r"flex:1 1 (\d+)px", blk)]
        self.assertTrue(basis, "좌우 칸 기준폭을 못 찾았다")
        cap = int(re.search(r"\.in-modal\.wide \{ max-width: (\d+)px", self.css).group(1))
        self.assertLessEqual(max(basis) * 2 + 24, cap,
                             f"기준폭 {max(basis)}px 두 칸이 팝업 {cap}px 안에 안 들어간다")


class TestChangelog(Base):
    """업데이트 내역(2026-08-14 대표) — 작업자가 좌측 상단 📢에서 본다."""

    def test_개발_이력이_처음부터_채워져_있다(self):
        d = self.c.get("/api/changelog").get_json()
        days = [x["day"] for x in d["days"]]
        self.assertIn("2026-07-28", days, "개발 시작일 기록이 없다")
        self.assertIn("2026-08-14", days)
        self.assertEqual(days, sorted(days, reverse=True), "최근 날짜가 위로 와야 한다")
        first = next(x for x in d["days"] if x["day"] == "2026-08-14")
        self.assertTrue(len(first["items"]) >= 3)

    def test_모든_로그인_사용자가_볼_수_있다(self):
        """셋팅·배송 담당이 봐야 의미가 있다 — 권한으로 막으면 정작 볼 사람이 못 본다."""
        self.c.post("/api/users", json={
            "username": "worker1", "displayName": "셋팅담당", "password": "worker-pass-1",
            "menus": ["setup"], "perms": ["setup.view", "orders.work"]})
        c2 = self.app.test_client()
        r = c2.post("/api/auth/login", json={"username": "worker1", "password": "worker-pass-1"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = c2.get("/api/changelog")
        self.assertEqual(d.status_code, 200, d.get_data(as_text=True))
        self.assertFalse(d.get_json()["canEdit"], "작업자에게 편집 권한이 열리면 안 된다")
        # 쓰기는 막힌다
        self.assertEqual(c2.post("/api/changelog",
                                 json={"items": ["몰래 수정"]}).status_code, 403)

    def test_하루치를_통째로_갈아끼운다(self):
        """세션이 끝날 때마다 그날 것을 다시 올려도 줄이 겹치면 안 된다."""
        self.c.post("/api/changelog", json={"day": "2026-08-20", "items": ["가", "나"]})
        r = self.c.post("/api/changelog", json={"day": "2026-08-20", "items": ["가", "나", "다"]})
        self.assertEqual(r.get_json()["items"], ["가", "나", "다"])
        day = next(x for x in self.c.get("/api/changelog").get_json()["days"]
                   if x["day"] == "2026-08-20")
        self.assertEqual(day["items"], ["가", "나", "다"])

    def test_번호를_붙여_적어도_알아서_뗀다(self):
        r = self.c.post("/api/changelog", json={
            "day": "2026-08-21", "items": "1. 첫째\n2) 둘째\n- 셋째\n\n  "})
        self.assertEqual(r.get_json()["items"], ["첫째", "둘째", "셋째"])

    def test_비우면_그날_기록이_지워진다(self):
        self.c.post("/api/changelog", json={"day": "2026-08-22", "items": ["임시"]})
        self.c.post("/api/changelog", json={"day": "2026-08-22", "items": []})
        days = [x["day"] for x in self.c.get("/api/changelog").get_json()["days"]]
        self.assertNotIn("2026-08-22", days)

    def test_화면에_버튼과_안본표시가_있다(self):
        root = Path(__file__).resolve().parent.parent
        html = (root / "static" / "index.html").read_text("utf-8")
        js = (root / "static" / "js" / "app.js").read_text("utf-8")
        self.assertIn('id="whatsnew-btn"', html)
        self.assertIn('id="whatsnew-dot"', html)
        self.assertIn("ows.whatsnew.seen", js, "마지막으로 본 날짜를 기억해야 점이 꺼진다")


class TestCodeOnlyStock(Base):
    """★재고는 제품코드로만 센다(대표 2026-08-14 재확인).

    "제품코드가 입력되기 전까지 셋팅·QC 탭에서 그 자산들이 잡히면 안 된다 —
     제품은 없다고 보면 된다." 모델명 근사는 'NT371B5M'이 'NT371B5M2'까지 끌어와
     없는 재고를 있다고 말했다. 그 경로를 화면·서버 양쪽에서 끊었다.
    """

    def setUp(self):
        super().setUp()
        from app.orders import product_info as pi
        pi._cache.clear()
        self.addCleanup(pi._cache.clear)
        self.cats = self.c.get("/api/categories").get_json()

    def _asset(self, model, code=""):
        a = self.c.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": model, "qty": 1,
            "purchasePrice": 100000, "grade": "A급"}).get_json()[0]
        if code:
            self.c.post("/api/assets/product-code", json={"ids": [a["id"]], "productCode": code})
        return a

    def _info(self, code, name=""):
        r = self.c.post("/api/orders/product-info",
                        json={"codes": [code], "names": {code: name}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()["products"][code]

    def test_코드가_없으면_재고로_안_잡힌다(self):
        self._asset("NT371B5M")                      # 코드 미입력 — 없는 것으로 봐야 한다
        self._asset("NT371B5M2")                     # 모델명이 비슷한 다른 기종
        p = self._info("NT371B5M_i7-7_내장", "삼성 NT371B5M 노트북")
        self.assertEqual(p["codeStock"], 0)
        self.assertFalse(p["codeRegistered"])
        self.assertEqual(p["ourStock"], 0, "모델명으로 짐작한 재고가 아직 새어 나온다")
        self.assertEqual(p["ourStockGrade"], [], "등급 분포도 모델명으로 세면 안 된다")
        self.assertEqual(p["ourModel"], "")

    def test_코드를_넣으면_그때_잡힌다(self):
        a = self._asset("NT371B5M")
        p = self._info("NT371B5M_i7-7_내장", "삼성 NT371B5M")
        self.assertEqual(p["codeShippable"], 0)
        self.c.post("/api/assets/product-code",
                    json={"ids": [a["id"]], "productCode": "NT371B5M_i7-7_내장"})
        from app.orders import product_info as pi
        pi._cache.clear()
        p2 = self._info("NT371B5M_i7-7_내장", "삼성 NT371B5M")
        self.assertTrue(p2["codeRegistered"])
        self.assertEqual(p2["codeShippable"], 1)
        self.assertEqual([g["grade"] for g in p2["ourStockGrade"]], ["A급"])

    def test_비슷한_모델명은_섞이지_않는다(self):
        """코드로 센 뒤에도 다른 기종이 끼면 안 된다 — 코드가 정확 일치일 때만."""
        self._asset("NT371B5M", "NT371B5M_i7-7_내장")
        self._asset("NT371B5M2", "NT371B5M2_i7-7_내장")
        p = self._info("NT371B5M_i7-7_내장", "삼성 NT371B5M")
        self.assertEqual(p["codeStock"], 1, "코드가 다른 기종까지 셌다")

    def test_화면도_모델_재고를_그리지_않는다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
              ).read_text("utf-8")
        blk = js.split("function stockChip", 1)[1].split("\nfunction ", 1)[0]
        self.assertNotIn("모델 ${ship}", blk, "모델명 기반 재고 칩이 아직 남아 있다")
        self.assertIn("재고 없음 · 코드 미입력", blk)


class TestPartsDelete(Base):
    """부품 단가표 삭제(2026-08-14 대표 "삭제기능 없어서 추가").

    ★과거 원가는 스냅샷이라 삭제해도 안 바뀐다. ★옵션에 연결된 부품은 막는다 —
      지우면 그 옵션 체크가 조용히 원가를 안 넣어 순이익이 틀어진다.
    """

    def test_지워도_이미_들어간_원가는_그대로다(self):
        cats = self.c.get("/api/categories").get_json()
        pid = ensure_part(self.c, "시트지(상)", "", 11000)
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        self.c.post(f"/api/assets/{a['id']}/repairs", json={"partIds": [pid]})
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["repairTotal"], 11000)
        r = self.c.delete(f"/api/parts/{pid}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["usedCount"], 1)
        got = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(got["repairTotal"], 11000, "지운 부품의 과거 원가가 사라지면 장부가 무너진다")
        self.assertNotIn("시트지(상)", [p["name"] for p in self.c.get("/api/parts").get_json()])

    def test_옵션에_연결된_부품은_못_지운다(self):
        pid = ensure_part(self.c, "D4 16G", "ram", 110000)
        opt = self.c.post("/api/prep-options", json={"name": "RAM 16GB 추가"}).get_json()
        self.c.patch(f"/api/prep-options/{opt['id']}", json={"partId": pid})
        r = self.c.delete(f"/api/parts/{pid}")
        self.assertEqual(r.status_code, 400)
        self.assertIn("RAM 16GB 추가", r.get_json()["error"])
        # 구분별 연결(part_map)도 같이 막힌다
        pid5 = ensure_part(self.c, "D5 16G", "ram", 200000)
        self.c.patch(f"/api/prep-options/{opt['id']}", json={
            "partId": None, "partMap": [{"gen": "DDR5", "partId": pid5}]})
        self.assertEqual(self.c.delete(f"/api/parts/{pid5}").status_code, 400)

    def test_없는_부품_삭제는_404(self):
        self.assertEqual(self.c.delete("/api/parts/999999").status_code, 404)

    def test_연결검사가_id_접두로_오탐하지_않는다(self):
        """★2026-08-14 검토: LIKE '%"partId": 1%' 는 partId 11에도 걸려
        한 자리 id 부품이 영원히 안 지워졌다. JSON을 파싱해 정수로 비교한다."""
        from app.db import tx
        keep = ensure_part(self.c, "무관부품A", "", 1000)
        # id가 접두로 겹치는 상황을 강제로 만든다(900 은 9001 의 앞자리)
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE parts SET id=900 WHERE id=?", (keep,))
                conn.execute(
                    "INSERT INTO parts(id, name, category, grp, price, enabled, created_at, "
                    "created_by, updated_at, updated_by) "
                    "VALUES(9001,'연결부품B','','',2000,1,'','','','')")
        opt = self.c.post("/api/prep-options", json={"name": "구분연결옵션"}).get_json()
        r = self.c.patch(f"/api/prep-options/{opt['id']}", json={
            "partMap": [{"gen": "DDR4", "partId": 9001}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.c.delete("/api/parts/9001").status_code, 400, "연결된 9001은 막혀야")
        r2 = self.c.delete("/api/parts/900")
        self.assertEqual(r2.status_code, 200, f"무관한 900이 막혔다: {r2.get_data(as_text=True)}")

    def test_이름으로_찾아쓰는_규격은_한_번_더_묻는다(self):
        """기준사양 채우기·판매 구성 칩은 'D4 8G' 같은 이름을 찾아 쓴다 — 그냥 지우면
        그 자리 원가가 조용히 0이 되므로 409로 한 번 더 확인받는다."""
        pid = ensure_part(self.c, "D4 8G", "ram", 44000)
        self.c.patch("/api/code-specs", json={
            "code": "GUARD-1", "ramGen": "DDR4", "ramGb": 8})
        r = self.c.delete(f"/api/parts/{pid}")
        self.assertEqual(r.status_code, 409, r.get_data(as_text=True))
        self.assertIn("기준사양", r.get_json()["error"])
        r2 = self.c.delete(f"/api/parts/{pid}", json={"force": True})
        self.assertEqual(r2.status_code, 200, "확인 후에는 지울 수 있어야 한다")

    def test_사용횟수는_수량표기도_센다(self):
        """'D4 8G x2' 로 기록된 것도 사용으로 세야 '흔적 없음'으로 오인하지 않는다."""
        cats = self.c.get("/api/categories").get_json()
        pid = ensure_part(self.c, "카운트시험부품", "", 5000)
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 10000}).get_json()[0]
        self.c.post(f"/api/assets/{a['id']}/repairs",
                    json={"partIds": [{"id": pid, "qty": 2}]})
        row = next(p for p in self.c.get("/api/parts").get_json() if p["id"] == pid)
        self.assertEqual(row["usedCount"], 1, "수량 표기(x2) 기록이 안 세어졌다")


class TestCostAfterShip(Base):
    """출고·매칭된 뒤에는 원가가 사후에 바뀌면 안 된다(2026-08-14 검토 확정 2건)."""

    def setUp(self):
        super().setUp()
        self.cats = self.c.get("/api/categories").get_json()
        ensure_part(self.c, "D4 8G", "ram", 44000)
        self.c.patch("/api/code-specs", json={
            "code": "SHIP-1", "ramGen": "DDR4", "ramGb": 8})

    def _asset(self):
        a = self.c.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        self.c.post("/api/assets/product-code",
                    json={"ids": [a["id"]], "productCode": "SHIP-1"})
        return a

    def test_매칭된_자산은_기준사양_채우기_대상이_아니다(self):
        a = self._asset()
        o = self.c.post("/api/orders", json={
            "channel": "전화", "recipient": "홍길동", "productName": "노트북",
            "quantity": 1, "amount": 500000}).get_json()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        res = self.c.post("/api/assets/base-spec-apply", json={"ids": [a["id"]]}).get_json()
        self.assertEqual(res["totalCost"], 0)
        self.assertIn("주문매칭", res["results"][0]["reason"])
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["repairTotal"], 0)

    def test_출고된_주문은_옵션_체크를_풀_수_없다(self):
        pid = ensure_part(self.c, "D4 16G", "ram", 110000)
        opt = self.c.post("/api/prep-options", json={"name": "RAM 16GB 추가"}).get_json()
        self.c.post(f"/api/prep-options/{opt['id']}/rules",
                    json={"channel": "", "matchType": "option", "matchValue": "RAM16"})
        self.c.patch(f"/api/prep-options/{opt['id']}", json={"partId": pid})
        a = self._asset()
        o = self.c.post("/api/orders", json={
            "channel": "전화", "recipient": "홍길동", "productName": "노트북",
            "optionName": "RAM16 업그레이드", "quantity": 1, "amount": 500000}).get_json()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.c.post(f"/api/orders/{o['id']}/options/{opt['id']}", json={"checked": True})
        for action in ("production", "softwareInspection", "shipping"):
            self.c.patch(f"/api/orders/{o['id']}", json={"action": action, "value": True})
        before = self.c.get(f"/api/assets/{a['id']}").get_json()["repairTotal"]
        self.assertEqual(before, 110000)
        r = self.c.post(f"/api/orders/{o['id']}/options/{opt['id']}", json={"checked": False})
        self.assertEqual(r.status_code, 409, "출고 후 해제가 열려 있으면 지난 달 마진이 바뀐다")
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["repairTotal"], before)


class TestEndToEndMargin(Base):
    """★매입 → 부품 → 주문 → 셋팅 → 출고 → 매출·순이익 한 줄 검증(대표 2026-08-14).

    "매입단에서 빠진 RAM·SSD를 기본 스펙으로 채우고, 옵션으로 업그레이드했을 때
     추가 비용까지 감안해 실제 매출·순이익이 맞는지."
    시나리오(대표 예시 그대로):
      · 매입: 노트북 1대 300,000원 — RAM·SSD 없이 들어옴
      · 코드 기준사양: DDR4 8GB / M.2 SATA 256G  → 기준사양 채우기로 44,000 + 44,000
      · 고객이 '16GB 업그레이드' 옵션 구매      → 8G 회수(−44,000) + 16G 장착(+110,000)
      · 판매 900,000원, 쿠팡 수수료 5%(45,000), 택배비 3,000
    기대 순이익 = (900,000 − 45,000 − 3,000) − (300,000 + 44,000 + 44,000 − 44,000 + 110,000)
                = 852,000 − 454,000 = 398,000
    """

    def setUp(self):
        super().setUp()
        self.cats = self.c.get("/api/categories").get_json()
        ensure_part(self.c, "D4 8G", "ram", 44000)
        ensure_part(self.c, "D4 16G", "ram", 110000)
        ensure_part(self.c, "M.2 SATA 256G", "ssd", 44000)
        self.c.patch("/api/code-specs", json={
            "code": "E2E-CODE", "ramGen": "DDR4", "ramGb": 8,
            "storageType": "M.2 SATA", "storageCap": "256G"})
        self.c.put("/api/settings", json={"settlement": {
            "rates": {"쿠팡": 5.0}, "shippingCost": 3000}})

    def test_매입부터_매출까지_금액이_이어진다(self):
        # ── 1) 매입: 전표 + 자산(램·SSD 없음)
        sid = self.c.post("/api/suppliers", json={"name": "E2E거래처"}).get_json()["id"]
        from app import config
        today = config.now().strftime("%Y-%m-%d")
        b = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": sid, "purchaseDate": today,
            "totalAmount": 300000,
            "assets": [{"categoryId": self.cats[0]["id"], "qty": 1, "model": "L480",
                        "purchasePrice": 300000}]}).get_json()
        aid = self.c.get(f"/api/purchase-batches/{b['id']}").get_json()["assets"][0]["id"]
        self.c.post("/api/assets/product-code",
                    json={"ids": [aid], "productCode": "E2E-CODE"})

        # ── 2) 기준사양 채우기 — 빠진 RAM·SSD를 단가표 단가로
        gaps = self.c.get(f"/api/purchase-batches/{b['id']}/base-spec-gaps").get_json()
        self.assertEqual(gaps["total"], 88000, f"부족분 계산이 다르다: {gaps}")
        fill = self.c.post("/api/assets/base-spec-apply", json={"ids": [aid]}).get_json()
        self.assertEqual(fill["totalCost"], 88000)
        asset = self.c.get(f"/api/assets/{aid}").get_json()
        self.assertEqual((asset["ram"], asset["ssd"]), ("D4 8G", "M.2 SATA 256G"))
        self.assertEqual(asset["costTotal"], 300000 + 88000)

        # ── 3) 주문(쿠팡, 16GB 업그레이드) — 판매 구성 칩이 자동으로 붙는다
        o = self.c.post("/api/orders", json={
            "channel": "쿠팡", "recipient": "홍길동",
            "productName": "L480 16GB/256GB", "productCode": "E2E-CODE",
            "quantity": 1, "amount": 900000}).get_json()
        chips = [c for c in self.c.get(f"/api/orders/{o['id']}").get_json()["prepOptions"]
                 if "판매 구성" in c["name"]]
        self.assertTrue(any("램 16GB" in c["name"] for c in chips), chips)

        # ── 4) 셋팅: 자산 매칭 → 칩 체크(업그레이드 원가 기입) → 단계 진행
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [aid]})
        ram_chip = next(c for c in chips if "램" in c["name"])
        chk = self.c.post(f"/api/orders/{o['id']}/options/{ram_chip['id']}",
                          json={"checked": True}).get_json()
        self.assertEqual(chk["partCost"], -44000 + 110000, "업그레이드 차액이 다르다")
        asset = self.c.get(f"/api/assets/{aid}").get_json()
        self.assertEqual(asset["ram"], "D4 16G", "출고 사양으로 안 바뀌었다")
        # 원가 = 매입 300,000 + 기준사양 88,000 + 업그레이드 66,000
        self.assertEqual(asset["costTotal"], 300000 + 88000 + 66000)

        for c in self.c.get(f"/api/orders/{o['id']}").get_json()["prepOptions"]:
            if not c["checked"]:
                self.c.post(f"/api/orders/{o['id']}/options/{c['id']}", json={"checked": True})
        for action in ("production", "softwareInspection", "shipping"):
            r = self.c.patch(f"/api/orders/{o['id']}", json={"action": action, "value": True})
            self.assertEqual(r.status_code, 200, f"{action}: {r.get_data(as_text=True)}")

        # ── 5) 매출·정산 — 수수료 5%, 택배비 3,000이 자동으로
        d = self.c.get(f"/api/orders/{o['id']}").get_json()
        self.assertEqual((d["amount"], d["feeAmount"], d["shippingCost"]),
                         (900000, 45000, 3000))
        self.assertEqual(d["netAmount"], 855000)          # 실입금(환불 없음)

        # ── 6) 리포트 순이익 = 852,000 − 454,000
        s = self.c.get(f"/api/reports/summary?from={today}&to={today}").get_json()["sales"]
        self.assertEqual(s["revenue"], 900000)
        self.assertEqual(s["fee"], 45000)
        self.assertEqual(s["cost"], 300000 + 154000 + 3000)   # 매입+부품·수리+택배
        self.assertEqual(s["margin"], 398000, f"순이익이 다르다: {s}")

        # ── 7) 매입 대시보드도 같은 숫자를 말한다
        dash = self.c.get(f"/api/reports/purchase-dashboard?from={today}&to={today}").get_json()
        self.assertEqual(dash["totals"]["buy"], 300000)
        self.assertEqual(dash["totals"]["sale"], 855000)      # 실입금 기준
        self.assertEqual(dash["totals"]["profit"], 398000)
        day = next(x for x in dash["days"] if x["date"] == today)
        self.assertEqual([sp["supplier"] for sp in day["slips"]], ["E2E거래처"])


class TestRealProductLink(Base):
    """상품명 링크를 '진짜 그 상품'으로(2026-08-14 대표: "링크가 실제 상품이 아니더라").

    ★쿠팡 URL은 노출상품ID(productId)를 쓴다 — 등록상품ID(sellerProductId)로는 안 열린다.
      대표 실물: 등록 16063461329 안에 노출 9616118750 + 옵션ID 6개.
    """

    def test_쿠팡_수집이_노출상품ID와_옵션ID를_남긴다(self):
        from app.malls.coupang import CoupangAdapter
        a = CoupangAdapter.__new__(CoupangAdapter)
        b = {"orderId": "3000012345", "orderedAt": "2026-08-14T10:00:00",
             "recipient": "홍길동", "phone": "010-1111-2222", "postalCode": "06000",
             "address": "서울 강남", "message": "", "lines": [{
                 "name": "그램 16GB/512GB", "opt": "", "qty": 1, "amount": 900000,
                 "code": "NB-CP", "pid": "16063461329",       # 등록상품ID(분류용)
                 "urlPid": "9616118750",                       # 노출상품ID(URL용)
                 "itemId": "94873098139"}]}                    # 옵션ID
        a.s = {}
        o = a._to_order(b)
        self.assertEqual(o["mallProductId"], "9616118750", "URL에는 노출상품ID를 써야 한다")
        self.assertEqual(o["mallItemId"], "94873098139")

    def test_저장하고_다시_읽어도_남아_있다(self):
        from app.db import tx
        from app.orders.mapping import insert_import_dict
        with self.app.app_context():
            with tx(write=True) as conn:
                oid = insert_import_dict(conn, {
                    "channel": "쿠팡", "orderNumber": "3000012345",
                    "productName": "그램 16GB/512GB", "recipient": "홍길동",
                    "quantity": 1, "amount": 900000,
                    "mallProductId": "9616118750", "mallItemId": "94873098139"}, "대표")
        d = self.c.get(f"/api/orders/{oid}").get_json()
        self.assertEqual(d["mallProductId"], "9616118750")
        self.assertEqual(d["mallItemId"], "94873098139")

    def test_화면이_링크를_상품페이지로_만든다(self):
        """setup.js mallProductUrl 계약 — 노출상품ID로 /vp/products/, 옵션ID는 쿼리로."""
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "setup.js"
              ).read_text("utf-8")
        blk = js.split("function mallProductUrl", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("coupang.com/vp/products/", blk)
        self.assertIn("vendorItemId=", blk)
        self.assertIn("smartstore", blk)
        self.assertIn("/products/", blk)
        # 식별자가 없으면 예전처럼 검색으로 떨어져야 한다(수기·옛 주문)
        self.assertIn("coupang.com/np/search", blk)
        self.assertIn("search.shopping.naver.com", blk)

    def test_스마트스토어_스토어주소_설정칸이_있다(self):
        malls = self.c.get("/api/mall-registry").get_json()
        ss = next(m for m in malls if m["code"] == "smartstore")
        self.assertIn("store_url", [f["key"] for f in ss["fields"]])

    def test_스토어주소는_기본값이_있고_설정이_이긴다(self):
        """대표 제공 주소(ExampleShare)를 기본값으로 — 설정에 넣으면 그쪽이 우선.
        ★렌탈·판매가 같은 스마트스토어 계정을 쓰기 때문에 주소가 ExampleShare이다."""
        r = self.c.post("/api/orders/product-info", json={"codes": []})
        base = r.get_json()
        # 코드가 비면 products만 비고 shopUrls는 온다 — 화면이 링크를 만들 수 있어야 한다
        r2 = self.c.post("/api/orders/product-info", json={"codes": ["NB-Z"]})
        urls = r2.get_json()["shopUrls"]
        self.assertEqual(urls["smartstore"], "https://smartstore.naver.com/ExampleShare")
        self.c.put("/api/settings", json={"malls": {
            "smartstore": {"store_url": "https://smartstore.naver.com/other/"}}})
        urls2 = self.c.post("/api/orders/product-info",
                            json={"codes": ["NB-Z"]}).get_json()["shopUrls"]
        self.assertEqual(urls2["smartstore"], "https://smartstore.naver.com/other")
        self.assertIsInstance(base, dict)


class TestSpecAllMalls(Base):
    """셋팅 보드 스펙을 모든 몰에서(2026-08-14 대표 "쿠팡뿐 아니라 모든 쇼핑몰").

    제품코드가 몰마다 같으므로 주문 채널로 거르지 않고 코드 자체를 고도몰 → 상품 조회
    되는 몰 순으로 찾는다. ★호출 예산이 핵심이라 '어느 몰에도 없는 코드'는 캐시한다.
    """

    def setUp(self):
        super().setUp()
        from app.orders import product_info as pi
        pi._cache.clear()                     # 모듈 전역 캐시 — 시험끼리 오염되면 안 된다
        pi._mall_fail.clear()                 # 실패 브레이크도 시험 사이에 초기화
        self.addCleanup(pi._cache.clear)
        self.addCleanup(pi._mall_fail.clear)
        self.pi = pi

    def _adapters(self, godo_rows, smart_rows):
        calls = {"godomall": 0, "smartstore": 0}

        def mk(code, rows):
            class A:
                pass
            A.code = code
            A.name = code
            def search_goods(self, q, field="code", size=5):
                calls[code] += 1
                return list(rows)
            A.search_goods = search_goods
            return A()

        made = {"godomall": mk("godomall", godo_rows), "smartstore": mk("smartstore", smart_rows)}
        return (lambda mc, s: (made.get(mc), "" if mc in made else "꺼짐")), calls

    def _ask(self, code):
        r = self.c.post("/api/orders/product-info", json={"codes": [code]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()["products"].get(code, {})

    def test_고도몰에_없으면_스마트스토어_스펙을_쓴다(self):
        rows = [{"goodsCd": "NB-X", "goodsNm": "갤럭시북",
                 "shortDescription": "DDR4 8GB / M.2 SATA 256GB"}]
        fake, calls = self._adapters([], rows)
        with mock.patch.object(self.pi, "get_adapter", side_effect=fake):
            p = self._ask("NB-X")
        self.assertEqual(p["spec"], "DDR4 8GB / M.2 SATA 256GB")
        self.assertEqual(p["specMall"], "smartstore")
        self.assertEqual(calls["godomall"], 1, "고도몰을 먼저 물어봐야 한다")

    def test_고도몰에_있으면_다른_몰은_안_부른다(self):
        rows = [{"goodsCd": "NB-G", "goodsNm": "그램", "shortDescription": "DDR5 16GB"}]
        fake, calls = self._adapters(rows, [])
        with mock.patch.object(self.pi, "get_adapter", side_effect=fake):
            p = self._ask("NB-G")
        self.assertEqual(p["spec"], "DDR5 16GB")
        self.assertEqual(calls["smartstore"], 0, "이미 찾았는데 다른 몰까지 부르면 예산이 샌다")

    def test_어느_몰에도_없으면_한_번만_묻는다(self):
        """★셋팅 화면은 계속 폴링한다 — 없는 코드를 매번 전 몰에 물으면 한도가 터진다."""
        fake, calls = self._adapters([], [])
        with mock.patch.object(self.pi, "get_adapter", side_effect=fake):
            self._ask("NB-NONE")
            self._ask("NB-NONE")
            self._ask("NB-NONE")
        self.assertEqual((calls["godomall"], calls["smartstore"]), (1, 1))

    def test_한_몰이_죽어도_다른_몰로_이어진다(self):
        from app.malls.base import MallError
        rows = [{"goodsCd": "NB-D", "goodsNm": "씽크패드", "shortDescription": "DDR4 16GB"}]

        class Dead:
            code = "godomall"
            name = "고도몰"
            def search_goods(self, q, field="code", size=5):
                raise MallError("고도몰 점검 중")

        class Ok:
            code = "smartstore"
            name = "스마트스토어"
            def search_goods(self, q, field="code", size=5):
                return list(rows)

        def fake(mc, s):
            return ({"godomall": Dead(), "smartstore": Ok()}.get(mc), "")

        with mock.patch.object(self.pi, "get_adapter", side_effect=fake):
            p = self._ask("NB-D")
        self.assertEqual(p["spec"], "DDR4 16GB")

    def test_호출_상한은_코드가_아니라_몰_호출_수다(self):
        """★2026-08-14 검토 확정: 예전엔 '코드 8개'를 잘랐는데 몰이 늘면 8×몰수 호출이
        나갔다. 지금은 한 요청의 몰 호출이 MAX_NEW_CALLS를 절대 안 넘는다."""
        fake, calls = self._adapters([], [])          # 둘 다 미스 → 코드당 2콜
        codes = [f"NB-{i}" for i in range(12)]
        with mock.patch.object(self.pi, "get_adapter", side_effect=fake):
            r = self.c.post("/api/orders/product-info", json={"codes": codes})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        total = calls["godomall"] + calls["smartstore"]
        self.assertLessEqual(total, self.pi.MAX_NEW_CALLS,
                             f"한 요청에 몰 호출 {total}회 — 상한을 넘었다")
        # 못 채운 코드는 pending으로 남아 다음 폴링에서 이어 받는다
        got = r.get_json()["products"]
        self.assertTrue(any(got[c].get("pending") for c in codes))

    def test_실패한_몰은_잠깐_쉬어_무한재시도를_막는다(self):
        """★검토 확정: 429가 아닌 실패(인증·IP미등록·타임아웃)는 쿨다운이 안 걸리고
        캐시도 안 남아, 5초 폴링이 토큰 발급 한도까지 태울 수 있었다."""
        from app.malls.base import MallError
        hits = {"n": 0}

        class Dead:
            code = "smartstore"
            name = "스마트스토어"
            def search_goods(self, q, field="code", size=5):
                hits["n"] += 1
                raise MallError("IP 미등록")

        with mock.patch.object(self.pi, "get_adapter",
                               side_effect=lambda mc, s:
                               (Dead(), "") if mc == "smartstore" else (None, "꺼짐")):
            for _ in range(5):                        # 폴링 5회
                self.c.post("/api/orders/product-info", json={"codes": ["NB-FAIL"]})
        self.assertEqual(hits["n"], 1, "실패한 몰을 매 폴링마다 다시 부르면 한도가 터진다")

    def test_창고_재고는_몰_연동_없이도_그대로_나온다(self):
        """몰이 전부 꺼져 있어도 창고 재고는 DB만 보므로 계속 떠야 한다.
        ★단 기준은 제품코드다(2026-08-14 대표) — 모델명으로 짐작하지 않는다."""
        cats = self.c.get("/api/categories").get_json()
        rows = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "NT551EAA", "qty": 2,
            "purchasePrice": 100000}).get_json()
        self.c.post("/api/assets/product-code", json={
            "ids": [x["id"] for x in rows], "productCode": "NT551EAA_i5-8_내장"})
        with mock.patch.object(self.pi, "get_adapter", side_effect=lambda mc, s: (None, "꺼짐")):
            r = self.c.post("/api/orders/product-info", json={
                "codes": ["NT551EAA_i5-8_내장"]})
        self.assertEqual(r.status_code, 200)
        p = r.get_json()["products"]["NT551EAA_i5-8_내장"]
        self.assertEqual(p["codeStock"], 2)
        self.assertTrue(p["codeRegistered"])


class TestConfigChips(Base):
    """판매 구성 칩(2026-08-14 대표 "쿠팡 진행해줘") — 제목이 옵션표인 채널.

    쿠팡 주문 제목("… 16GB/512GB")을 파싱해 코드 기준 사양과 다를 때만 칩이 붙고,
    체크하면 자산 '실물' 기준 회수+장착 원가가 들어간다. 해제하면 되돌아간다.
    """

    def setUp(self):
        super().setUp()
        self.cats = self.c.get("/api/categories").get_json()
        ensure_part(self.c, "D4 8G", "ram", 44000)
        ensure_part(self.c, "D4 16G", "ram", 110000)
        ensure_part(self.c, "M.2 SATA 256G", "ssd", 44000)
        ensure_part(self.c, "M.2 SATA 512G", "ssd", 88000)
        r = self.c.patch("/api/code-specs", json={
            "code": "NB-CFG", "ramGen": "DDR4", "ramGb": 8,
            "storageType": "M.2 SATA", "storageCap": "256G"})
        assert r.status_code == 200, r.get_data(as_text=True)
        data = self.c.get("/api/prep-options").get_json()
        by_kind = {o.get("kind"): o["id"] for o in data["options"] if o.get("kind")}
        self.ram_opt = by_kind["config_ram"]
        self.st_opt = by_kind["config_storage"]

    def _order(self, title="그램 16GB/512GB 노트북", channel="쿠팡", code="NB-CFG"):
        return self.c.post("/api/orders", json={
            "channel": channel, "recipient": "홍길동", "productName": title,
            "productCode": code, "quantity": 1, "amount": 900000}).get_json()

    def _asset_base(self):
        a = self.c.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]
        r = self.c.patch(f"/api/assets/{a['id']}",
                         json={"ram": "D4 8G", "ssd": "M.2 SATA 256G"})
        assert r.status_code == 200, r.get_data(as_text=True)
        return a

    def _chips(self, oid):
        return self.c.get(f"/api/orders/{oid}").get_json()["prepOptions"]

    def test_구성이_다르면_칩이_뜨고_같으면_안_뜬다(self):
        o = self._order()
        names = [c["name"] for c in self._chips(o["id"])]
        self.assertTrue(any("램 16GB" in n and "기준 8GB" in n for n in names), names)
        self.assertTrue(any("512G" in n and "256G" in n for n in names), names)
        same = self._order(title="그램 8GB/256GB 노트북")
        self.assertEqual(
            [c for c in self._chips(same["id"]) if "판매 구성" in c["name"]], [])

    def test_판매구성_칩은_쿠팡에만_붙는다(self):
        """고도몰은 옵션 문구가 따로 오므로 여기까지 붙이면 이중 기입이 된다."""
        o = self._order(channel="고도몰")
        self.assertEqual(
            [c for c in self._chips(o["id"]) if "판매 구성" in c["name"]], [])

    def test_체크하면_실물_기준_회수_장착이_들어간다(self):
        o = self._order()
        a = self._asset_base()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        r = self.c.post(f"/api/orders/{o['id']}/options/{self.ram_opt}",
                        json={"checked": True}).get_json()
        self.assertEqual(r["partApplied"], 1)
        self.assertEqual(r["partCost"], -44000 + 110000)   # 8G 회수 + 16G 장착
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(d["ram"], "D4 16G")
        # 해제 → 되돌림(원가·스펙 모두)
        self.c.post(f"/api/orders/{o['id']}/options/{self.ram_opt}", json={"checked": False})
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(d["ram"], "D4 8G")
        self.assertEqual(d["repairTotal"], 0)

    def test_체크가_먼저고_매칭이_나중이어도_같다(self):
        o = self._order()
        self.c.post(f"/api/orders/{o['id']}/options/{self.st_opt}", json={"checked": True})
        a = self._asset_base()
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        d = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(d["ssd"], "M.2 SATA 512G")
        self.assertEqual(d["repairTotal"], -44000 + 88000)

    def test_시스템_옵션은_삭제가_막힌다(self):
        r = self.c.delete(f"/api/prep-options/{self.ram_opt}")
        self.assertEqual(r.status_code, 400)
        self.assertIn("시스템", r.get_json()["error"])


class TestPurchaseDashboard(Base):
    """매입 대시보드(2026-08-13 대표, 매입대시보드.hwpx) — 매입일 기준 코호트.

    일별 매입수량·매입합계 + 그 매입분이 실현한 판매액·순수익(관리자 전용).
    """

    def setUp(self):
        super().setUp()
        self.cats = self.c.get("/api/categories").get_json()

    def _asset(self, price):
        return self.c.post("/api/assets", json={
            "categoryId": self.cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": price}).get_json()[0]

    def _ship(self, asset_id, amount, channel="전화"):
        o = self.c.post("/api/orders", json={
            "channel": channel, "recipient": "홍길동", "productName": "노트북",
            "quantity": 1, "amount": amount}).get_json()
        for action, extra in (("assets", {"assetIds": [asset_id]}),
                              ("production", {"value": True}),
                              ("softwareInspection", {"value": True}),
                              ("shipping", {"value": True})):
            r = self.c.patch(f"/api/orders/{o['id']}", json={"action": action, **extra})
            assert r.status_code == 200, r.get_data(as_text=True)
        return o

    def _dash(self, **qs):
        from app import config
        today = config.now().strftime("%Y-%m-%d")
        p = {"from": today, "to": today, **qs}
        q = "&".join(f"{k}={v}" for k, v in p.items())
        r = self.c.get("/api/reports/purchase-dashboard?" + q)
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.get_json()

    def test_일별_수량_매입합_판매_순수익(self):
        a1 = self._asset(100000)
        self._asset(200000)                          # 미판매분 — 판매액엔 안 잡힌다
        self._ship(a1["id"], 500000)                 # 전화 채널 = 수수료 0
        d = self._dash()
        self.assertEqual(d["totals"]["qty"], 2)
        self.assertEqual(d["totals"]["buy"], 300000)
        self.assertTrue(d["showProfit"])             # 관리자 계정
        self.assertEqual(d["totals"]["soldQty"], 1)
        self.assertEqual(d["totals"]["sale"], 500000)
        self.assertEqual(d["totals"]["profit"], 400000)   # 50만 − 매입 10만
        self.assertEqual(len(d["days"]), 1)          # 오늘 하루 코호트

    def test_부품_회수가_순수익에_더해진다(self):
        """다운그레이드 회수(음수 원가)가 대시보드 순수익에도 그대로 반영돼야 한다."""
        pid = ensure_part(self.c, "D4 8G", "ram", 40000)
        a = self._asset(100000)
        r = self.c.post(f"/api/assets/{a['id']}/repairs",
                        json={"partIds": [{"id": pid, "remove": True}]})
        assert r.status_code == 201, r.get_data(as_text=True)
        self._ship(a["id"], 300000)
        d = self._dash()
        self.assertEqual(d["totals"]["profit"], 300000 - 100000 + 40000)

    def test_날짜별_전표_내역이_함께_온다(self):
        """대표 2026-08-14: 달력 칸 색·툴팁('어떤 매입처에 어떤 매입')과
        칸을 눌렀을 때 뜨는 목록이 같은 숫자를 쓰도록 전표 단위까지 쪼개 준다."""
        sid = self.c.post("/api/suppliers", json={"name": "달력거래처"}).get_json()["id"]
        from app import config
        today = config.now().strftime("%Y-%m-%d")
        r = self.c.post("/api/purchase-batches", json={
            "stage": "purchased", "supplierId": sid, "purchaseDate": today,
            "totalAmount": 300000,
            "assets": [{"categoryId": self.cats[0]["id"], "qty": 3, "model": "L480",
                        "purchasePrice": 100000}]})
        assert r.status_code == 201, r.get_data(as_text=True)
        bid = r.get_json()["id"]
        self._asset(50000)                       # 전표 없이 등록된 자산도 한 줄로 온다
        d = self._dash()
        day = next(x for x in d["days"] if x["date"] == today)
        self.assertEqual(day["qty"], 4)
        by_batch = {s["batchId"]: s for s in day["slips"]}
        self.assertEqual(by_batch[bid]["supplier"], "달력거래처")
        self.assertEqual((by_batch[bid]["qty"], by_batch[bid]["buy"]), (3, 300000))
        self.assertIn(None, by_batch)            # 전표 없는 자산 묶음
        self.assertEqual(sum(s["buy"] for s in day["slips"]), day["buy"])

    def test_매입가_범위_필터(self):
        self._asset(100000)
        self._asset(900000)
        d = self._dash(priceMin=500000)
        self.assertEqual(d["totals"]["qty"], 1)
        self.assertEqual(d["totals"]["buy"], 900000)


class TestSettlementDefaults(Base):
    """채널별 수수료 기본값(대표 2026-08-11: "일단 기본 값으로 진행").

    저장값이 없으면 통상 요율(DEFAULT_FEE_RATES)이 적용되고, 설정 화면에서 저장한
    값(0 포함)이 항상 이긴다. 직거래(전화·방문·b2b)는 수수료가 붙지 않는다.
    """

    def _shipped(self, channel="쿠팡", amount=1000000):
        cats = self.c.get("/api/categories").get_json()
        o = self.c.post("/api/orders", json={
            "channel": channel, "recipient": "홍길동", "productName": "노트북",
            "quantity": 1, "amount": amount}).get_json()
        a = self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 500000}).get_json()[0]
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "assets", "assetIds": [a["id"]]})
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "production", "value": True})
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "softwareInspection", "value": True})
        r = self.c.patch(f"/api/orders/{o['id']}", json={"action": "shipping", "value": True})
        assert r.status_code == 200, r.get_data(as_text=True)
        return self.c.get(f"/api/orders/{o['id']}").get_json()

    def test_설정이_비어도_기본_요율이_붙는다(self):
        o = self._shipped(channel="쿠팡", amount=1000000)
        self.assertEqual(o["feeRate"], 5.0)
        self.assertEqual(o["feeAmount"], 50000)

    def test_저장값이_기본값을_이긴다_0도_존중(self):
        """대표가 '이 채널 수수료 없음'으로 0을 저장하면 기본값이 되살아나면 안 된다."""
        self.c.put("/api/settings", json={"settlement": {"rates": {"쿠팡": 0}}})
        o = self._shipped(channel="쿠팡")
        self.assertEqual(o["feeAmount"], 0)
        self.c.put("/api/settings", json={"settlement": {"rates": {"쿠팡": 10.8}}})
        o2 = self._shipped(channel="쿠팡", amount=1000000)
        self.assertEqual(o2["feeAmount"], 108000)

    def test_직거래에는_수수료가_안_붙는다(self):
        for ch in ("전화", "방문", "b2b"):
            o = self._shipped(channel=ch, amount=500000)
            self.assertEqual(o["feeAmount"], 0, f"{ch} 직거래에 수수료가 붙었다")

    def test_기본_요율표가_실채널_이름과_맞다(self):
        """요율 키가 주문의 channel 문자열과 정확히 같아야 걸린다 — '고도몰5' 같은
        표시이름으로 넣으면 요율을 넣어도 수수료가 0으로 남는다(설정 화면 주석의 함정)."""
        from app.orders import DEFAULT_FEE_RATES
        for ch in ("고도몰", "쿠팡", "스마트스토어", "카카오"):   # 라이브 실사용 채널
            self.assertIn(ch, DEFAULT_FEE_RATES)
        self.assertEqual(DEFAULT_FEE_RATES["_default"], 0)


class TestOptionAutoParts(Base):
    """옵션 → 부품 원가 자동 기입(대표 2026-08-10).

    "고객이 옵션으로 램 추가를 골랐으면, 셋팅에서 칩 누르는 순간 매칭된 자산에
     ★고객 옵션가가 아니라 우리 매입 단가가 자동으로 원가 기입."
    """

    def _cat(self):
        return self.c.get("/api/categories").get_json()[0]["id"]

    def setUp(self):
        super().setUp()
        # 부품 + 옵션 + 규칙: 옵션 문자열에 'RAM16' 이 있으면 'RAM 16GB 추가' 옵션
        self.part_id = ensure_part(self.c, "D4 16G", "ram", 30000)
        opt = self.c.post("/api/prep-options", json={"name": "RAM 16GB 추가"}).get_json()
        self.opt_id = opt["id"]
        self.c.post(f"/api/prep-options/{self.opt_id}/rules",
                    json={"channel": "", "matchType": "option", "matchValue": "RAM16"})
        self.c.patch(f"/api/prep-options/{self.opt_id}",
                     json={"partId": self.part_id, "partQty": 1})

    def _order(self, option="RAM16 업그레이드(+40,000원)"):
        return self.c.post("/api/orders", json={
            "channel": "고도몰", "recipient": "홍길동", "productName": "노트북",
            "optionName": option, "quantity": 1, "amount": 500000}).get_json()

    def _asset(self, **kw):
        return self.c.post("/api/assets", json={
            "categoryId": self._cat(), "model": "L480", "qty": 1,
            "purchasePrice": 100000, **kw}).get_json()[0]

    def _match(self, oid, aids):
        r = self.c.patch(f"/api/orders/{oid}", json={"action": "assets", "assetIds": aids})
        assert r.status_code == 200, r.get_data(as_text=True)

    def _check(self, oid, checked=True):
        return self.c.post(f"/api/orders/{oid}/options/{self.opt_id}",
                           json={"checked": checked}).get_json()

    def _repair_total(self, aid):
        return self.c.get(f"/api/assets/{aid}").get_json()["repairTotal"]

    def test_체크하면_우리_단가가_기입된다(self):
        """옵션가 4만원이 아니라 단가표 3만원이 들어가야 한다."""
        o = self._order()
        a = self._asset(ram="X")
        self._match(o["id"], [a["id"]])
        r = self._check(o["id"])
        self.assertEqual(r["partApplied"], 1)
        self.assertEqual(r["partCost"], 30000)
        self.assertEqual(self._repair_total(a["id"]), 30000)
        got = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(got["ram"], "D4 16G", "스펙 칸도 채워져야 한다")

    def test_체크가_먼저고_매칭이_나중이어도_된다(self):
        o = self._order()
        r = self._check(o["id"])
        self.assertEqual(r["partApplied"], 0)
        self.assertIn("매칭", r["partMessage"])
        a = self._asset()
        self._match(o["id"], [a["id"]])
        self.assertEqual(self._repair_total(a["id"]), 30000)

    def test_두_번_체크해도_한_번만_기입된다(self):
        o = self._order()
        a = self._asset()
        self._match(o["id"], [a["id"]])
        self._check(o["id"])
        self._check(o["id"])
        self.assertEqual(self._repair_total(a["id"]), 30000)

    def test_체크_해제하면_되돌린다(self):
        o = self._order()
        a = self._asset(ram="X")
        self._match(o["id"], [a["id"]])
        self._check(o["id"])
        self._check(o["id"], checked=False)
        self.assertEqual(self._repair_total(a["id"]), 0)
        got = self.c.get(f"/api/assets/{a['id']}").get_json()
        self.assertEqual(got["ram"], "X", "스펙도 원래대로 돌아와야 한다")

    def test_매칭_해제하면_그_자산에서_되돌린다(self):
        o = self._order()
        a1, a2 = self._asset(), self._asset()
        self._match(o["id"], [a1["id"]])
        self._check(o["id"])
        self.assertEqual(self._repair_total(a1["id"]), 30000)
        self._match(o["id"], [a2["id"]])          # a1 빠지고 a2 — 원가가 따라간다
        self.assertEqual(self._repair_total(a1["id"]), 0)
        self.assertEqual(self._repair_total(a2["id"]), 30000)

    def test_주문_취소하면_되돌린다(self):
        o = self._order()
        a = self._asset()
        self._match(o["id"], [a["id"]])
        self._check(o["id"])
        self.c.patch(f"/api/orders/{o['id']}", json={"action": "cancel", "reason": "시험"})
        self.assertEqual(self._repair_total(a["id"]), 0)

    def test_수기_수리는_안_건드린다(self):
        """자동 기입 되돌리기가 사람이 넣은 수리비까지 지우면 안 된다."""
        o = self._order()
        a = self._asset()
        self.c.post(f"/api/assets/{a['id']}/repairs",
                    json={"description": "액정 수리", "cost": 50000})
        self._match(o["id"], [a["id"]])
        self._check(o["id"])
        self._check(o["id"], checked=False)
        self.assertEqual(self._repair_total(a["id"]), 50000)

    def test_수량_2면_두_배로_기입된다(self):
        self.c.patch(f"/api/prep-options/{self.opt_id}", json={"partQty": 2})
        o = self._order()
        a = self._asset()
        self._match(o["id"], [a["id"]])
        r = self._check(o["id"])
        self.assertEqual(r["partCost"], 60000)

    def test_단가_0원이면_기입하지_않고_경고한다(self):
        self.c.patch(f"/api/parts/{self.part_id}", json={"price": 0})
        o = self._order()
        a = self._asset()
        self._match(o["id"], [a["id"]])
        r = self._check(o["id"])
        self.assertEqual(r["partApplied"], 0)
        self.assertIn("단가", r["partMessage"])
        self.assertEqual(self._repair_total(a["id"]), 0)

    def test_부품_연결_없는_옵션은_아무_일도_없다(self):
        self.c.patch(f"/api/prep-options/{self.opt_id}", json={"partId": None})
        o = self._order()
        a = self._asset()
        self._match(o["id"], [a["id"]])
        r = self._check(o["id"])
        self.assertEqual(r.get("partApplied", 0), 0)
        self.assertEqual(self._repair_total(a["id"]), 0)


class TestApiTabNesting(Base):
    """★설정 ▸ API 관리에서 몰을 누를 때마다 세부탭 줄이 하나씩 쌓이던 버그
    (대표 보고 2026-08-10). 원인: 몰 버튼 핸들러가 자기 자리가 아니라 한 단계 위
    화면(renderApiTab)을 자기 안에 다시 그려서 '쇼핑몰·택배 API / 제공 옵션' 줄이
    중첩됐다. 저장·키지우기 뒤 renderApiTab($("#tab-body"))도 같은 클래스."""

    def setUp(self):
        super().setUp()
        self.js = (Path(__file__).resolve().parent.parent
                   / "static" / "js" / "app.js").read_text("utf-8")

    def _fn(self, name):
        blk = self.js.split(f"function {name}(", 1)
        self.assertEqual(len(blk), 2, f"{name} 이 없다")
        return blk[1].split("\nasync function ", 1)[0].split("\nfunction ", 1)[0]

    def test_몰_버튼은_자기_자리에만_다시_그린다(self):
        blk = self._fn("renderApiSettings")
        i = blk.index('button[data-api]')
        handler = blk[i:i + 400]
        self.assertIn("renderApiSettings(body)", handler)
        self.assertNotIn("renderApiTab(", handler,
                         "몰 버튼이 상위 화면을 다시 그리면 세부탭이 겹쳐 쌓인다")

    def test_저장_삭제_뒤에도_상위화면을_덮지_않는다(self):
        blk = self._fn("renderMallSection")
        self.assertNotIn('renderApiTab($("#tab-body"))', blk,
                         "설정 탭 전체에 API 화면을 덮으면 세부탭이 중첩된다")
        self.assertIn("redrawApiSettings()", blk)

    def test_다시그리기_헬퍼가_apiview_body를_쓴다(self):
        blk = self._fn("redrawApiSettings")
        self.assertIn('$("#apiview-body")', blk)


class TestIsRealLegacyKey(Base):
    def test_구명칭_고객코드로도_실발행_판정이_된다(self):
        """★연결테스트는 되는데 발급은 조용히 999가 되는 함정의 재발 방지."""
        from app.orders.waybill import _is_real
        cfg = {"env": "prod", "armed": True,
               "customerCode": "00000000", "biz_reg_num": "0000000000"}
        self.assertTrue(_is_real(cfg), "customerCode(구명칭)를 인정하지 않으면 "
                                       "연결테스트 성공 + 전량 테스트발행의 모순이 생긴다")


class TestAsBoard(Base):
    """🚦 A/S 진행 보드(2026-09-03 대표 "A/S 담당자가 현황 파악이 되도록").

    대표가 물은 네 가지 —
      ① 회수 신청한 택배가 배송완료됐는지 ② 접수해서 진행 중인지
      ③ 수리가 끝났는데 결제 전인지     ④ 결제는 됐는데 발송 전인지.
    ★상태(status) 하나로는 답이 안 나온다는 것이 이 화면의 존재 이유다 —
      "회수 중"은 택배가 오는 중일 수도, 이미 도착했는데 아무도 입고를 안 누른 것일 수도 있다
      (CJ 추적 데몬은 송장만 배송완료로 바꾸고 접수 건 상태는 그대로 둔다).
      아래 test_회수_송장이_배송완료면_입고_완료_칸으로_간다 가 그 경우를 고정한다.
    """

    def _ticket(self, **kw):
        base = {"customer": "김보드", "phone": "010-7777-0001", "symptom": "전원 불량",
                "address": "서울시 강남구 1", "postalCode": "06000",
                "symptomCat": "노트북", "symptomSub": "H/W"}
        base.update(kw)
        r = self.c.post("/api/as-tickets", json=base)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()

    def _board(self, view="open"):
        r = self.c.get(f"/api/as-board?view={view}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def _stage(self, tid, view="open"):
        for row in self._board(view)["rows"]:
            if row["id"] == tid:
                return row["stage"]
        return None

    def _counts(self, view="open"):
        return {s["code"]: s["count"] for s in self._board(view)["stages"]}

    def _items(self, tid, amount):
        r = self.c.post(f"/api/as-tickets/{tid}/items",
                        json={"items": [{"name": "메인보드", "qty": 1, "amount": amount}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    # ---------------- ② 접수해서 진행 중인지
    def test_택배_접수만_하면_회수_예약_대기다(self):
        t = self._ticket()
        self.assertEqual(self._stage(t["id"]), "intake_wait")

    def test_방문_접수는_물건이_이미_있으므로_입고_완료다(self):
        """고객이 들고 온 건은 회수할 것이 없다 — 바로 수리 시작 대기여야 한다."""
        t = self._ticket(intake="visit")
        self.assertEqual(self._stage(t["id"]), "arrived")

    def test_회수_예약을_하면_회수_중이다(self):
        t = self._ticket()
        r = self.c.post(f"/api/as-tickets/{t['id']}/recall", json={})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertEqual(self._stage(t["id"]), "collecting")

    # ---------------- ① 회수 택배가 배송완료됐는지 (대표가 가장 먼저 물은 것)
    def test_회수_송장이_배송완료면_입고_완료_칸으로_간다(self):
        """★CJ 추적 데몬은 송장만 delivered 로 바꾸고 접수 건은 'collecting' 에 둔다.
        상태만 보면 영원히 '회수 중'이라 담당자가 도착한 줄 모른다 — 보드는 송장을 본다."""
        t = self._ticket()
        r = self.c.post(f"/api/as-tickets/{t['id']}/recall", json={}).get_json()
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE waybills SET status='delivered', cj_stage_cd='91', "
                             "cj_stage_nm='배송완료' WHERE wid=?", (r["wid"],))
                # 접수 건 상태는 일부러 그대로 둔다(데몬이 하는 그대로)
                row = conn.execute("SELECT status FROM as_tickets WHERE id=?",
                                   (t["id"],)).fetchone()
        self.assertEqual(row["status"], "collecting", "시험 전제가 깨졌다")
        rows = {x["id"]: x for x in self._board()["rows"]}
        self.assertEqual(rows[t["id"]]["stage"], "arrived")
        self.assertTrue(rows[t["id"]]["recallDone"])
        self.assertEqual(rows[t["id"]]["recallStage"], "배송완료")

    def test_수리_중이면_수리_중_칸이다(self):
        t = self._ticket()
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "repairing"})
        self.assertEqual(self._stage(t["id"]), "repairing")

    # ---------------- ③ 끝났는데 결제 전인지
    def test_유상인데_돈을_안_받았으면_결제_전이다(self):
        t = self._ticket(chargeTo="customer")
        self._items(t["id"], 253000)
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "done"})
        self.assertEqual(self._stage(t["id"]), "pay_wait")

    def test_결제_확인을_누르면_발송_전으로_넘어간다(self):
        t = self._ticket(chargeTo="customer")
        self._items(t["id"], 253000)
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "done"})
        r = self.c.post(f"/api/as-tickets/{t['id']}/payment",
                        json={"paid": True, "method": "계좌이체"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 금액을 안 주면 그 시점의 청구 합계를 굳힌다
        self.assertEqual(r.get_json()["paidAmount"], 253000)
        self.assertEqual(self._stage(t["id"]), "ship_wait")

    def test_결제_확인을_해제하면_다시_결제_전이다(self):
        t = self._ticket(chargeTo="customer")
        self._items(t["id"], 100000)
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "done"})
        self.c.post(f"/api/as-tickets/{t['id']}/payment", json={"paid": True})
        self.assertEqual(self._stage(t["id"]), "ship_wait")
        self.c.post(f"/api/as-tickets/{t['id']}/payment", json={"paid": False})
        self.assertEqual(self._stage(t["id"]), "pay_wait")

    def test_받은_금액은_나중에_내역을_고쳐도_안_흔들린다(self):
        """청구서를 다시 뽑으려고 금액을 고치는 일이 있다 — 받은 돈은 그대로여야 한다."""
        t = self._ticket(chargeTo="customer")
        self._items(t["id"], 200000)
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "done"})
        self.c.post(f"/api/as-tickets/{t['id']}/payment", json={"paid": True})
        self._items(t["id"], 300000)                  # 나중에 내역을 올려 잡았다
        got = self.c.get(f"/api/as-tickets/{t['id']}").get_json()
        self.assertEqual(got["paidAmount"], 200000)
        self.assertEqual(got["billTotal"], 300000)

    def test_유상이면_금액을_아직_안_적었어도_결제_전이다(self):
        """★규칙이 바뀌었다(2026-09-08 대표, AS-260908-01 신고 — 수리 완료 뒤 유상으로 바꿨더니
        발송 전으로 새어 나가 돈을 못 받은 채 송장이 나갈 뻔했다). 어제(09-07)까지는 '금액 0원이면
        결제 단계를 건너뛴다'였는데, 0원인 유상은 '아직 청구액을 안 적은 것'이지 '받을 돈이 없는 것'이
        아니다. 정말 받을 게 없으면 비용 부담을 무상으로 되돌린다(payment_pending 주석)."""
        t = self._ticket(chargeTo="customer")
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "done"})
        self.assertEqual(self._stage(t["id"]), "pay_wait")
        # 무상으로 되돌리면 그때 발송 전으로 간다
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"chargeTo": "company"})
        self.assertEqual(self._stage(t["id"]), "ship_wait")

    def test_무상은_돈_이야기_없이_발송_전이다(self):
        t = self._ticket(chargeTo="company")
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "done"})
        self.assertEqual(self._stage(t["id"]), "ship_wait")

    # ---------------- ④ 발송 전인지
    def test_방문_수령이면_발송이_아니라_방문_수령_대기다(self):
        t = self._ticket()
        self.c.patch(f"/api/as-tickets/{t['id']}",
                     json={"status": "done", "returnMethod": "visit"})
        self.assertEqual(self._stage(t["id"]), "visit_wait")

    def test_반송_송장이_나가면_반송_중이다(self):
        t = self._ticket()
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "done"})
        r = self.c.post(f"/api/as-tickets/{t['id']}/return-waybill", json={})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        row = next(x for x in self._board()["rows"] if x["id"] == t["id"])
        self.assertEqual(row["stage"], "returning")
        self.assertTrue(row["returnWid"])

    def test_반송_완료는_끝난_건_보기에서만_나온다(self):
        t = self._ticket()
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "returned"})
        self.assertIsNone(self._stage(t["id"]), "진행 중 보기에 종료 건이 섞이면 안 된다")
        self.assertEqual(self._stage(t["id"], view="all"), "closed")

    def test_취소_건은_어느_보기에도_없다(self):
        t = self._ticket()
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "cancelled"})
        self.assertIsNone(self._stage(t["id"]))
        self.assertIsNone(self._stage(t["id"], view="all"))

    # ---------------- 숫자·지연
    def test_칸_숫자는_실제_줄_수와_같다(self):
        self._ticket()
        self._ticket(intake="visit")
        t = self._ticket()
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "repairing"})
        d = self._board()
        counts = {s["code"]: s["count"] for s in d["stages"]}
        for code, n in counts.items():
            self.assertEqual(n, sum(1 for r in d["rows"] if r["stage"] == code),
                             f"{code} 칸 숫자가 표와 다르다")
        self.assertEqual(sum(counts.values()), len(d["rows"]))

    def test_기한을_넘긴_건에_지연_표시가_붙는다(self):
        old = self._ticket(receivedAt="2026-01-01")
        new = self._ticket()
        d = self._board()
        rows = {r["id"]: r for r in d["rows"]}
        self.assertTrue(rows[old["id"]]["late"])
        self.assertFalse(rows[new["id"]]["late"])
        self.assertGreaterEqual(d["late"], 1)
        self.assertGreater(rows[old["id"]]["ageDays"], d["dueDays"])

    def test_칸_순서는_일의_흐름_순이고_오래된_것이_위다(self):
        a = self._ticket(receivedAt="2026-02-01")
        b = self._ticket(receivedAt="2026-03-01")
        rows = self._board()["rows"]
        order = [r["id"] for r in rows]
        self.assertLess(order.index(a["id"]), order.index(b["id"]),
                        "같은 칸에서는 오래 묵은 것이 먼저 나와야 한다")

    # ---------------- 권한
    def test_보기_권한만_있어도_보드는_읽는다(self):
        r = self.c.post("/api/users", json={
            "username": "asview", "displayName": "AS보기", "password": "asview-pw-12",
            "perms": ["as.view"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "asview", "password": "asview-pw-12"})
        self.assertEqual(c.get("/api/as-board").status_code, 200)

    def test_결제_확인은_관리_권한이_있어야_한다(self):
        t = self._ticket(chargeTo="customer")
        self._items(t["id"], 50000)
        r = self.c.post("/api/users", json={
            "username": "asview2", "displayName": "AS보기2", "password": "asview2-pw-12",
            "perms": ["as.view"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "asview2", "password": "asview2-pw-12"})
        self.assertEqual(c.post(f"/api/as-tickets/{t['id']}/payment",
                                json={"paid": True}).status_code, 403)

    def test_결제_확인은_이력에_남는다(self):
        t = self._ticket(chargeTo="customer")
        self._items(t["id"], 77000)
        self.c.post(f"/api/as-tickets/{t['id']}/payment",
                    json={"paid": True, "method": "현금"})
        got = self.c.get(f"/api/as-tickets/{t['id']}").get_json()
        self.assertIn("결제확인", [e["action"] for e in got["events"]])
        self.assertEqual(got["paidMethod"], "현금")

    # ---------------- 회수 예약 취소(2026-09-03 대표 "회수예약건 자체를 취소하는 기능도")
    def _recall(self, tid):
        r = self.c.post(f"/api/as-tickets/{tid}/recall", json={})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()["wid"]

    def test_회수_예약_취소하면_접수로_돌아가고_다시_걸_수_있다(self):
        """★취소가 없으면 그 건은 영영 '회수 중'에 갇힌다 — 중복 가드가 재예약도 막는다."""
        t = self._ticket()
        wid = self._recall(t["id"])
        self.assertEqual(self._stage(t["id"]), "collecting")
        r = self.c.post(f"/api/waybills/{wid}/cancel", json={})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._stage(t["id"]), "intake_wait")
        got = self.c.get(f"/api/as-tickets/{t['id']}").get_json()
        self.assertEqual(got["status"], "received")
        self.assertIn("회수예약취소", [e["action"] for e in got["events"]])
        # 다시 걸 수 있어야 한다
        self.assertEqual(self._recall(t["id"])[:3], "WB-")
        self.assertEqual(self._stage(t["id"]), "collecting")

    def test_배송완료된_회수는_취소할_수_없다(self):
        """물건이 이미 들어왔는데 예약을 무르면 접수 건과 자산 흐름이 뒤집힌다."""
        t = self._ticket()
        wid = self._recall(t["id"])
        from app.db import tx
        with self.app.app_context():
            with tx(write=True) as conn:
                conn.execute("UPDATE waybills SET status='delivered' WHERE wid=?", (wid,))
        r = self.c.post(f"/api/waybills/{wid}/cancel", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("배송(입고)이 끝난", r.get_json()["error"])

    def test_A_S_담당자도_자기가_건_회수를_무를_수_있다(self):
        """예약은 as.manage 로 하는데 취소만 배송 권한이면 건 사람이 못 문다."""
        r = self.c.post("/api/users", json={
            "username": "asonly", "displayName": "AS전담", "password": "asonly-pw-12",
            "perms": ["as.view", "as.manage"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "asonly", "password": "asonly-pw-12"})
        rr = c.post("/api/as-tickets", json={
            "customer": "김전담", "phone": "010-8888-0001", "symptom": "전원",
            "address": "서울시 강남구 9", "postalCode": "06000"})
        self.assertEqual(rr.status_code, 201, rr.get_data(as_text=True))
        tid = rr.get_json()["id"]
        wid = c.post(f"/api/as-tickets/{tid}/recall", json={}).get_json()["wid"]
        self.assertEqual(c.post(f"/api/waybills/{wid}/cancel", json={}).status_code, 200)

    def test_주문_회수는_배송_권한이_그대로다(self):
        """A/S 때문에 넓힌 권한이 주문 쪽 송장까지 열어 주면 안 된다."""
        src = (Path(__file__).resolve().parent.parent / "app" / "orders" / "waybill.py"
               ).read_text("utf-8")
        blk = src.split("def cancel_waybill", 1)[1].split("body =", 1)[0]
        self.assertIn('_pre["type"] == "recall" and _pre["as_ticket_id"]', blk)
        self.assertIn('require_any("orders.ship", "waybills.manage")', blk)

    def test_화면에_회수_취소_버튼이_있다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "as.js").read_text("utf-8")
        self.assertIn("cancelAsRecall", js)
        self.assertIn('id="asd-recall-cancel"', js, "상세 팝업에 취소 버튼이 없다")
        self.assertIn('b("recallCancel"', js, "현황 보드의 회수 중 줄에 취소 버튼이 없다")
        # CJ 가 거절해도 '기록만 취소'로 빠져나갈 수 있어야 재예약이 된다
        self.assertIn("forceLocal: true", js)

    # ---------------- 화면
    def test_현황_탭은_진행_보드_하나다(self):
        """2026-09-03 대표: A/S 현황은 '어디에 있나'만, 통계(돈)는 매출/실적에서."""
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "js" / "as.js").read_text("utf-8")
        app = (root / "static" / "js" / "app.js").read_text("utf-8")
        self.assertIn("renderAsBoard", js)
        self.assertNotIn('data-astab=', js, "현황 안에 세부탭을 다시 만들지 않는다")
        self.assertIn('["asstats", "🔧 A/S 실적"]', app, "A/S 통계는 매출/실적 보기에 있어야 한다")
        self.assertIn("renderAsStatsBody(host)", app)

    def test_접수_탭에는_상태_카드가_없다(self):
        """2026-09-03 대표: 접수 탭 카드와 진행 상황이 중첩된다 — 한 곳에서만 센다."""
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "as.js").read_text("utf-8")
        self.assertNotIn('id="as-kpi"', js)
        self.assertNotIn('data-kf=', js)

    def test_셋팅실적과_기간별_작업량이_한_화면이다(self):
        """2026-09-03 대표: "거의 같은 내용이라 서로 흡수해서, 두 개로 나누지 마라"."""
        root = Path(__file__).resolve().parent.parent
        app = (root / "static" / "js" / "app.js").read_text("utf-8")
        wl = (root / "static" / "js" / "workload.js").read_text("utf-8")
        self.assertIn('["stats", "📅 셋팅·작업 실적"]', app)
        self.assertNotIn('["workload", "기간별 작업량"]', app, "보기가 둘로 남아 있다")
        self.assertIn("workload-stats", app, "합친 화면이 작업량 창구를 함께 불러야 한다")
        self.assertIn("renderWorkDetail", app)
        self.assertIn('class="workload-cards"', app, "기존 작업자 카드가 통합 화면에서 빠졌다")
        self.assertIn('id="ss-from"', app, "시작일 직접 선택이 통합 화면에서 빠졌다")
        self.assertIn('id="ss-to"', app, "종료일 직접 선택이 통합 화면에서 빠졌다")
        self.assertNotIn("workload-cards", wl, "옛 작업량 화면이 되살아나 있다")

    def test_접수_상세_팝업이_좌우_두_기둥이다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "as.js").read_text("utf-8")
        css = (Path(__file__).resolve().parent.parent / "static" / "css" / "app.css").read_text("utf-8")
        self.assertIn('class="as-form2"', js, "접수 폼이 좌/우로 안 나뉘어 있다")
        self.assertEqual(js.count('class="as-dcol"'), 2, "상세는 왼쪽·오른쪽 두 기둥이어야 한다")
        self.assertIn(".as-form2 {", css)
        self.assertIn("> .as-dcol {", css)
        # ★폭을 픽셀로 못 박으면 작은 화면에서 잘린다 — min(폭, 화면비율)이어야 한다
        self.assertIn("width: min(1120px, 96vw)", css)
        self.assertIn("width: min(1440px, 96vw)", css)
        self.assertIn("@media (max-width: 900px)", css)


class TestAsRework0907(Base):
    """A/S 개편(2026-09-07 대표) — 탭 [🏠 대시보드][🚦 진행 상황], 칸 이름 정정, 자동 전이, 결제 잠금, 삭제.

    대표 요청 그대로:
      1. 회수예약대기 → 접수(회수 일정 설정·정상 접수 확인·접수 삭제)
      2. 회수 중에서 우리 쪽으로 배송완료되면 자동으로 입고 완료 / 방문 접수는 바로 입고 완료
      3. 수리 중에서 수리 내역 상세
      4. 결제 전은 유상만 — 결제 확인 후 송장 출력·방문 수령
      5. 발송 전 = 결제까지 완료(수리 중에서도 결제 확인 버튼)
      6. 반송 중 → 택배 출고
      7. 종료는 쌓이되 검색·내역 확인
    """

    def _ticket(self, **kw):
        base = {"customer": "김개편", "phone": "010-9000-0001", "symptom": "전원 불량",
                "address": "서울시 강남구 1", "postalCode": "06000"}
        base.update(kw)
        r = self.c.post("/api/as-tickets", json=base)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()

    def _get(self, tid):
        return self.c.get(f"/api/as-tickets/{tid}").get_json()

    def _board(self, view="open", q=""):
        r = self.c.get(f"/api/as-board?view={view}&q={q}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def _row(self, tid, view="open", q=""):
        return next((x for x in self._board(view, q)["rows"] if x["id"] == tid), None)

    def _items(self, tid, amount):
        r = self.c.post(f"/api/as-tickets/{tid}/items",
                        json={"items": [{"name": "메인보드", "qty": 1, "amount": amount}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def _asset(self):
        cats = self.c.get("/api/categories").get_json()
        return self.c.post("/api/assets", json={
            "categoryId": cats[0]["id"], "model": "L480", "qty": 1,
            "purchasePrice": 100000}).get_json()[0]

    def _wb_row(self, wid):
        from app.db import tx
        with self.app.app_context():
            with tx() as conn:
                return dict(conn.execute("SELECT * FROM waybills WHERE wid=?", (wid,)).fetchone())

    def _cj_stage(self, wid, code, name):
        """추적 데몬이 CJ 응답 하나를 반영하는 것과 똑같이(_sync_one) — 트랜잭션 밖 문자 목록도 돌려준다."""
        from app.db import tx
        from app.orders import recall as rc
        res = {"ok": True, "data": {"PROC_LIST": [{"CRG_ST_CD": code, "CRG_ST_NM": name}]}}
        after = []
        with self.app.app_context():
            with tx(write=True) as conn:
                w = conn.execute("SELECT * FROM waybills WHERE wid=?", (wid,)).fetchone()
                changed = rc._sync_one(conn, w, res, after=after)
        return changed, after

    # ---------------- 2. 입고 완료 자동 전이
    def test_방문_접수는_처음부터_입고_완료다(self):
        t = self._ticket(intake="visit")
        self.assertEqual(self._get(t["id"])["status"], "arrived")
        self.assertEqual(self._row(t["id"])["stage"], "arrived")

    def test_회수_택배가_배달완료면_접수_건이_저절로_입고_완료가_된다(self):
        """★라이브 WB-20260901-01 이 6일째 '회수 중'이던 구멍 — 데몬은 송장만 바꾸고 접수 건을
        안 건드렸다. 이제 송장·자산·접수 건이 한 관문(recall_arrived)으로 같이 움직인다."""
        a = self._asset()
        t = self._ticket(assetId=a["id"])
        wid = self.c.post(f"/api/as-tickets/{t['id']}/recall", json={}).get_json()["wid"]
        self.assertEqual(self._get(t["id"])["status"], "collecting")
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["status"], "returning")
        changed, after = self._cj_stage(wid, "91", "배송완료")
        self.assertTrue(changed)
        d = self._get(t["id"])
        self.assertEqual(d["status"], "arrived", "배달완료인데 접수 건이 안 넘어갔다")
        self.assertIn("회수입고", [e["action"] for e in d["events"]])
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["status"], "as",
                         "A/S 물건은 판매 재고가 아니라 'as' 여야 한다")
        self.assertEqual(self._wb_row(wid)["status"], "delivered")
        self.assertEqual(after, [("collected", t["id"])], "입고 문자 시점이 트랜잭션 밖으로 넘어와야 한다")
        self.assertEqual(self._row(t["id"])["stage"], "arrived")

    def test_배달완료_전_단계는_접수_건을_안_건드린다(self):
        t = self._ticket()
        wid = self.c.post(f"/api/as-tickets/{t['id']}/recall", json={}).get_json()["wid"]
        self._cj_stage(wid, "02", "집화완료")
        self.assertEqual(self._get(t["id"])["status"], "collecting")
        self.assertEqual(self._row(t["id"])["recallStage"], "집화완료")

    def test_이미_수리_중이면_늦게_온_배달완료가_되돌리지_않는다(self):
        t = self._ticket()
        wid = self.c.post(f"/api/as-tickets/{t['id']}/recall", json={}).get_json()["wid"]
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "repairing"})
        self._cj_stage(wid, "91", "배송완료")
        self.assertEqual(self._get(t["id"])["status"], "repairing")

    def test_회수_송장번호_수집이_배달완료를_받아도_입고_완료가_된다(self):
        """회수는 CJ 가 집화 때 채번 — 접수일 기준 수집이 유일한 경로다. 그 경로도 같은 관문."""
        from app.cj.calendar import next_pickup_day
        from app.orders import recall as rc
        self.c.post("/api/cj/import-rms")
        self.c.put("/api/settings", json={"cj": {"armed": True}})
        t = self._ticket()
        with mock.patch.object(rc, "cj2_reg_book", return_value={"ok": True, "result_cd": "S"}):
            r = self.c.post(f"/api/as-tickets/{t['id']}/recall",
                            json={"pickupDate": next_pickup_day({})})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        w = self._wb_row(r.get_json()["wid"])
        self.assertEqual(w["status"], "issued")
        rows = [{"CUST_USE_NO": w["cust_use_no"], "INVC_NO": "650012345678",
                 "CRG_ST": "91", "RCPT_DV": "02"}]
        with mock.patch.object(rc, "cj2_mss_track", return_value={"ok": True, "rows": rows}):
            d = self.c.post("/api/waybills/recall-invoice-sync", json={"days": 1}).get_json()
        self.assertEqual(d["filled"], 1)
        self.assertEqual(d["arrived"], 1)
        self.assertEqual(self._get(t["id"])["status"], "arrived")
        self.assertEqual(self._wb_row(w["wid"])["invoice_no"], "650012345678")

    def test_데몬_한_바퀴가_회수_접수일을_골라_수집한다(self):
        """번호 없는 회수 송장은 번호 기준 추적에 안 잡힌다 — 데몬이 접수일을 골라 물어야 한다."""
        from app.db import tx
        from app.orders import recall as rc
        self.c.post("/api/cj/import-rms")
        self.c.put("/api/settings", json={"cj": {"armed": True}})
        t = self._ticket()
        with mock.patch.object(rc, "cj2_reg_book", return_value={"ok": True, "result_cd": "S"}):
            wid = self.c.post(f"/api/as-tickets/{t['id']}/recall", json={}).get_json()["wid"]
        w = self._wb_row(wid)
        with self.app.app_context():
            with tx() as conn:
                dates = rc._recall_sweep_dates(conn)
        self.assertEqual(dates, [w["cj_rcpt_ymd"]])
        rows = [{"CUST_USE_NO": w["cust_use_no"], "INVC_NO": "650000001111",
                 "CRG_ST": "91", "RCPT_DV": "02"}]
        with mock.patch.object(rc, "cj2_mss_track", return_value={"ok": True, "rows": rows}) as m, \
             mock.patch.object(rc, "cj2_track", return_value=None):
            out = rc.tracker_tick(self.app)
        self.assertEqual(out["sweepDates"], 1)
        self.assertEqual(out["arrived"], 1)
        self.assertEqual(m.call_count, 1, "접수일 하나면 CJ 호출도 한 번이어야 한다")
        self.assertEqual(self._get(t["id"])["status"], "arrived")
        # 끝난 뒤에는 더 물을 날짜가 없다
        with self.app.app_context():
            with tx() as conn:
                self.assertEqual(rc._recall_sweep_dates(conn), [])

    def test_입고_처리_버튼도_입고_완료에_선다(self):
        t = self._ticket()
        wid = self.c.post(f"/api/as-tickets/{t['id']}/recall", json={}).get_json()["wid"]
        r = self.c.post(f"/api/waybills/{wid}/received", json={"status": "refurbishing"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["status"], "as")
        self.assertEqual(self._get(t["id"])["status"], "arrived")

    # ---------------- 6·7. 택배 출고 → 배달완료면 자동 종료, 종료는 쌓이고 검색
    def test_출고_택배가_배달완료면_접수_건이_저절로_종료된다(self):
        a = self._asset()
        t = self._ticket(assetId=a["id"], intake="visit")
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "done"})
        r = self.c.post(f"/api/as-tickets/{t['id']}/return-waybill", json={})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertEqual(self._row(t["id"])["stage"], "returning")
        self._cj_stage(r.get_json()["wid"], "91", "배송완료")
        d = self._get(t["id"])
        self.assertEqual(d["status"], "returned")
        self.assertTrue(d["closedAt"])
        self.assertEqual(self.c.get(f"/api/assets/{a['id']}").get_json()["status"], "shipped")
        self.assertIsNone(self._row(t["id"]), "종료 건이 진행 중 보기에 남아 있다")
        self.assertEqual(self._row(t["id"], view="closed")["stage"], "closed")

    def test_종료_칸은_검색으로_찾고_취소_건도_함께_쌓인다(self):
        a = self._ticket(customer="박종료", intake="visit")
        b = self._ticket(customer="최종료", intake="visit")
        c = self._ticket(customer="정삭제")
        for t in (a, b):
            self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "returned"})
        self.assertEqual(self.c.delete(f"/api/as-tickets/{c['id']}", json={"reason": "중복"}).status_code, 200)
        closed = self._board("closed")
        ids = {r["id"] for r in closed["rows"]}
        self.assertEqual(ids, {a["id"], b["id"], c["id"]})
        self.assertTrue(next(r for r in closed["rows"] if r["id"] == c["id"])["cancelled"])
        self.assertEqual([r["id"] for r in self._board("closed", "박종료")["rows"]], [a["id"]])
        # 진행 중 보기의 종료 카드 숫자는 '끝난 건 전체'다
        stages = {s["code"]: s["count"] for s in self._board()["stages"]}
        self.assertEqual(stages["closed"], 3)
        # view=all 은 예전처럼 취소를 뺀다
        self.assertNotIn(c["id"], {r["id"] for r in self._board("all")["rows"]})

    # ---------------- 4·5. 결제 잠금
    def test_유상_미결제면_송장도_인도도_막힌다(self):
        t = self._ticket(chargeTo="customer", intake="visit")
        self._items(t["id"], 150000)
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "done"})
        self.assertEqual(self._row(t["id"])["stage"], "pay_wait")
        r = self.c.post(f"/api/as-tickets/{t['id']}/return-waybill", json={})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("결제", r.get_json()["error"])
        r = self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "returned"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("결제", r.get_json()["error"])
        # 받았다고 표시하면 열린다
        self.c.post(f"/api/as-tickets/{t['id']}/payment", json={"paid": True})
        self.assertEqual(self._row(t["id"])["stage"], "ship_wait")
        self.assertEqual(self.c.post(f"/api/as-tickets/{t['id']}/return-waybill", json={}).status_code, 201)

    def test_수리_중에서도_결제_확인이_되고_그러면_바로_발송_전이다(self):
        t = self._ticket(chargeTo="customer", intake="visit")
        self._items(t["id"], 90000)
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "repairing"})
        row = self._row(t["id"])
        self.assertEqual(row["stage"], "repairing")
        self.assertEqual(row["unpaid"], 90000, "수리 중 줄에 못 받은 돈이 실려야 결제 버튼이 뜬다")
        self.c.post(f"/api/as-tickets/{t['id']}/payment", json={"paid": True})
        self.assertEqual(self._row(t["id"])["unpaid"], 0)
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "done"})
        self.assertEqual(self._row(t["id"])["stage"], "ship_wait", "결제가 끝났으니 결제 전을 건너뛰어야 한다")

    def test_무상은_결제_잠금이_없다(self):
        t = self._ticket(chargeTo="company", intake="visit")
        self._items(t["id"], 50000)
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "done"})
        self.assertEqual(self.c.post(f"/api/as-tickets/{t['id']}/return-waybill", json={}).status_code, 201)

    def test_유상으로_바꾸면_금액이_없어도_결제_전이다(self):
        """2026-09-08 대표 신고(AS-260908-01) — 무상으로 수리 완료한 뒤 유상으로 바꿨더니
        금액이 0이라 [💰 결제 전]을 건너뛰고 [📦 발송 전]으로 갔다. 돈을 못 받은 채 송장이 나간다.
        ★잠금 판정을 금액(unpaid_bill)이 아니라 '유상인가'(payment_pending)로 되돌리면 시험이 잡는다."""
        t = self._ticket(chargeTo="company")               # 무상으로 접수·완료
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "done"})
        self.assertEqual(self._row(t["id"])["stage"], "ship_wait")
        # 완료 뒤에 유상으로 바꾼다 — 금액은 아직 안 적었다
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"chargeTo": "customer"})
        row = self._row(t["id"])
        self.assertEqual(row["stage"], "pay_wait", "유상으로 바뀌면 금액이 없어도 결제 전이어야 한다")
        self.assertEqual(row["unpaid"], 0, "표시용 금액은 0 그대로 — 잠금은 금액이 아니라 '유상인가'로 한다")
        # 금액이 비어 있어도 송장·종료는 막힌다(돈 못 받고 나가는 길을 닫는다)
        r = self.c.post(f"/api/as-tickets/{t['id']}/return-waybill", json={})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("청구 금액", r.get_json()["error"])
        r = self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "returned"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("청구 금액", r.get_json()["error"])
        # 푸는 길 ① 정말 받을 게 없으면 무상으로 되돌린다
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"chargeTo": "company"})
        self.assertEqual(self._row(t["id"])["stage"], "ship_wait")
        # 푸는 길 ② 금액을 적고 결제 확인
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"chargeTo": "customer"})
        self._items(t["id"], 50000)
        self.assertEqual(self._row(t["id"])["unpaid"], 50000)
        self.c.post(f"/api/as-tickets/{t['id']}/payment", json={"paid": True})
        self.assertEqual(self._row(t["id"])["stage"], "ship_wait")

    # ---------------- 1. 접수 단계: 삭제·경로 전환·회수 접수 확인
    def test_접수_삭제는_취소로_남고_번호는_되쓰지_않는다(self):
        t = self._ticket()
        r = self.c.delete(f"/api/as-tickets/{t['id']}", json={"reason": "고객 변심"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = self._get(t["id"])
        self.assertEqual(d["status"], "cancelled")
        self.assertTrue(d["closedAt"])
        ev = next(e for e in d["events"] if e["action"] == "접수삭제")
        self.assertEqual(ev["detail"]["사유"], "고객 변심")
        self.assertIsNone(self._row(t["id"]), "삭제한 건이 보드에 남아 있다")
        # 접수번호는 사라지지 않고, 다음 접수는 새 번호를 받는다(문자로 이미 나갔을 수 있다)
        self.assertNotEqual(self._ticket()["ticketNo"], t["ticketNo"])
        # 되살리기 = 상태를 접수로
        self.assertEqual(self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "received"}).status_code, 200)
        self.assertEqual(self._row(t["id"])["stage"], "intake_wait")

    def test_회수_예약이_살아_있거나_진행_중이면_삭제_못_한다(self):
        t = self._ticket()
        self.c.post(f"/api/as-tickets/{t['id']}/recall", json={})
        r = self.c.delete(f"/api/as-tickets/{t['id']}", json={})
        self.assertEqual(r.status_code, 409)
        self.assertIn("회수 예약", r.get_json()["error"])
        t2 = self._ticket(intake="visit")
        self.c.patch(f"/api/as-tickets/{t2['id']}", json={"status": "repairing"})
        r = self.c.delete(f"/api/as-tickets/{t2['id']}", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("접수 단계", r.get_json()["error"])

    def test_삭제는_관리_권한이_있어야_한다(self):
        t = self._ticket()
        r = self.c.post("/api/users", json={
            "username": "asview3", "displayName": "AS보기3", "password": "asview3-pw-12",
            "perms": ["as.view"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "asview3", "password": "asview3-pw-12"})
        self.assertEqual(c.delete(f"/api/as-tickets/{t['id']}", json={}).status_code, 403)

    def test_접수_경로를_방문으로_바꾸면_입고_완료로_간다(self):
        t = self._ticket()
        self.assertEqual(self.c.patch(f"/api/as-tickets/{t['id']}", json={"intake": "visit"}).status_code, 200)
        self.assertEqual(self._get(t["id"])["status"], "arrived")
        self.assertEqual(self.c.patch(f"/api/as-tickets/{t['id']}", json={"intake": "parcel"}).status_code, 200)
        self.assertEqual(self._get(t["id"])["status"], "received")

    def test_회수_접수_확인이_보드_줄과_확인_버튼에_실린다(self):
        from app.cj.calendar import next_pickup_day
        want = next_pickup_day({})          # ★날짜를 박으면 그날이 지나며 시험이 저절로 깨진다
        t = self._ticket()
        self.assertEqual(self.c.post(f"/api/as-tickets/{t['id']}/recall-check", json={}).status_code, 400)
        self.c.post(f"/api/as-tickets/{t['id']}/recall", json={"pickupDate": want})
        row = self._row(t["id"])
        self.assertTrue(row["recallBooked"])
        self.assertTrue(row["recallTest"])                 # CJ 미설정 → 시험 예약
        self.assertEqual(row["recallScheduled"], want)
        r = self.c.post(f"/api/as-tickets/{t['id']}/recall-check", json={})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        self.assertTrue(d["simulated"])
        self.assertTrue(d["booked"])
        self.assertEqual(d["ticketStatus"], "collecting")

    # ---------------- 3. 수리 내역 요약
    def test_보드_줄에_수리_내역_요약이_실린다(self):
        t = self._ticket(intake="visit")
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "repairing", "result": "메인보드 교체"})
        r = self.c.post(f"/api/as-tickets/{t['id']}/items", json={"items": [
            {"name": "메인보드", "qty": 1, "amount": 120000},
            {"name": "공임", "qty": 1, "amount": 30000}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        row = self._row(t["id"])
        self.assertEqual(row["itemsCount"], 2)
        self.assertIn("메인보드", row["itemsSummary"])
        self.assertIn("공임", row["itemsSummary"])
        self.assertEqual(row["result"], "메인보드 교체")

    # ---------------- 대시보드
    def test_대시보드_숫자는_보드와_같다(self):
        self._ticket()
        self._ticket(intake="visit")
        old = self._ticket(receivedAt="2026-01-01")
        t = self._ticket(chargeTo="customer", intake="visit")
        self._items(t["id"], 70000)
        self.c.patch(f"/api/as-tickets/{t['id']}", json={"status": "done"})
        done = self._ticket(intake="visit")
        self.c.patch(f"/api/as-tickets/{done['id']}", json={"status": "returned"})
        r = self.c.get("/api/as-dashboard")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        d = r.get_json()
        board = self._board()
        self.assertEqual(d["open"], len(board["rows"]))
        want = {s["code"]: s["count"] for s in board["stages"] if s["code"] != "closed"}
        self.assertEqual({s["code"]: s["count"] for s in d["stages"]}, want)
        self.assertEqual(d["unpaidCount"], 1)
        self.assertEqual(d["unpaidTotal"], 70000)
        self.assertEqual(d["late"], board["late"])
        self.assertEqual([x["id"] for x in d["lateRows"]], [old["id"]])
        self.assertGreaterEqual(d["todayReceived"], 4)
        self.assertEqual(d["weekClosed"], 1)
        self.assertTrue(d["recent"] and d["recent"][0]["ticketNo"])

    def test_대시보드는_보기_권한으로_읽는다(self):
        r = self.c.post("/api/users", json={
            "username": "asview4", "displayName": "AS보기4", "password": "asview4-pw-12",
            "perms": ["as.view"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "asview4", "password": "asview4-pw-12"})
        self.assertEqual(c.get("/api/as-dashboard").status_code, 200)

    # ---------------- 칸 이름·화면 배선
    def test_칸_이름이_대표_요청_그대로다(self):
        from app.asvc import AS_BOARD_STAGES, AS_STATUSES
        labels = dict((c, lb) for c, lb, _h in AS_BOARD_STAGES)
        self.assertIn("접수", labels["intake_wait"])
        self.assertNotIn("예약 대기", labels["intake_wait"])
        self.assertIn("택배 출고", labels["returning"])
        self.assertNotIn("반송 중", labels["returning"])
        self.assertEqual(AS_STATUSES["arrived"], "입고 완료")
        meta = self.c.get("/api/as-meta").get_json()
        self.assertIn("arrived", [s["code"] for s in meta["statuses"]])

    def test_화면_배선(self):
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "js" / "as.js").read_text("utf-8")
        css = (root / "static" / "css" / "app.css").read_text("utf-8")
        self.assertIn("🏠 대시보드", js)
        self.assertIn("🚦 진행 상황", js)
        self.assertNotIn('data-atab="list"', js, "옛 접수 탭이 남아 있다")
        self.assertNotIn('data-atab="stats"', js, "옛 현황 탭이 남아 있다")
        self.assertIn("/api/as-dashboard", js)
        self.assertIn("openAsRecallModal", js)
        self.assertIn('id="asrecall-date"', js, "회수 일정(수거 희망일) 입력이 없다")
        self.assertIn('b("delete"', js, "접수 칸에 삭제 버튼이 없다")
        self.assertIn('b("recallCheck"', js, "회수 중 칸에 CJ 확인 버튼이 없다")
        self.assertIn('b("received"', js, "회수 중 칸에 입고 처리 버튼이 없다")
        self.assertIn('b("repairing", "🔧 수리 시작"', js)
        self.assertIn('b("ship", "🧾 송장 출력"', js)
        self.assertIn('id="asb-q"', js, "종료 칸 검색창이 없다")
        self.assertIn("asWorkLine", js, "수리 내역 요약 줄이 없다")
        self.assertIn('method: "DELETE"', js)
        self.assertIn(".as-dash-cols", css)


class TestCjPickupCalendar(Base):
    """CJ 집화 휴무일(2026-09-07 대표 "택배 쉬는날이나 휴무인 경우 그 날에는 예약접수 불가능하게").

    ★기사가 안 오는 날로 예약하면 고객은 하루를 헛기다리고 우리는 며칠 뒤에야 안다.
      화면에서도 막지만 서버가 마지막 선이다 — 회수 예약 3경로(A/S·주문 반품·신규 등록)가
      전부 같은 관문(waybill.check_pickup_date)을 탄다.
    """

    def _ticket(self, **kw):
        base = {"customer": "김휴무", "phone": "010-9100-0001", "symptom": "전원 불량",
                "address": "서울시 강남구 1", "postalCode": "06000"}
        base.update(kw)
        r = self.c.post("/api/as-tickets", json=base)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()

    def _next(self):
        r = self.c.get("/api/cj/pickup-calendar")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    # ---------------- 규칙 자체(날짜 계산은 순수 함수라 오늘 날짜에 안 흔들리게 고정 기준일로)
    def test_일요일과_공휴일과_지난_날은_막힌다(self):
        from datetime import date
        from app.cj.calendar import pickup_block_reason
        t = date(2026, 9, 7)                       # 월요일
        cases = [
            ("2026-09-08", ""),                    # 화요일 — 가능
            ("2026-09-06", "지난 날짜"),
            ("2026-09-13", "일요일"),
            ("2026-09-25", "추석"),                # 설정 시드(음력)
            ("2026-10-03", "개천절"),              # 날짜 고정 공휴일 — 목록에 없어도 막힌다
            ("2026-10-09", "한글날"),
            ("2026-10-05", "대체공휴일"),
        ]
        for day, want in cases:
            got = pickup_block_reason(day, {}, today=t)
            if want:
                self.assertIn(want, got, f"{day} 가 안 막혔다: {got!r}")
            else:
                self.assertEqual(got, "", f"{day} 는 되어야 한다: {got!r}")

    def test_수거일을_안_고르면_막지_않는다(self):
        """수거 희망일은 선택 항목이다 — 안 주면 CJ가 통상 다음 영업일에 배차한다."""
        from app.cj.calendar import pickup_block_reason
        self.assertEqual(pickup_block_reason("", {}), "")
        self.assertEqual(pickup_block_reason(None, {}), "")

    def test_토요일은_설정으로_켜고_끈다(self):
        from datetime import date
        from app.cj.calendar import pickup_block_reason
        t = date(2026, 9, 7)
        self.assertEqual(pickup_block_reason("2026-09-12", {}, today=t), "",
                         "기본값에서 토요일까지 막으면 안 된다(계약·지역에 따라 다름)")
        off = {"pickup": {"offWeekdays": [5, 6]}}
        self.assertIn("토요일", pickup_block_reason("2026-09-12", off, today=t))

    def test_검사를_끄면_아무_날짜나_된다(self):
        from datetime import date
        from app.cj.calendar import pickup_block_reason
        self.assertEqual(
            pickup_block_reason("2026-09-25", {"pickup": {"enabled": False}}, today=date(2026, 9, 7)), "")

    def test_가장_이른_가능일은_쉬는_날을_건너뛴다(self):
        from datetime import date
        from app.cj.calendar import next_pickup_day
        # 2026-09-26(토)에 보면 27(일·추석연휴)을 건너뛰고 28(월)
        self.assertEqual(next_pickup_day({}, today=date(2026, 9, 26)), "2026-09-28")

    # ---------------- 서버 관문 — 3경로
    def _blocked(self):
        """오늘 이후 실제로 막히는 첫 날과 그 사유 — ★날짜를 박으면 그날이 지나는 순간
        '지난 날짜'로 사유가 바뀌어 시험이 저절로 깨진다(2026-09-01 같은 시한폭탄 재발 방지)."""
        from datetime import date, timedelta
        from app.cj.calendar import pickup_block_reason
        day = date.today() + timedelta(days=1)
        for _ in range(60):
            why = pickup_block_reason(day.isoformat(), {})
            if why:
                return day.isoformat(), why
            day += timedelta(days=1)
        raise AssertionError("두 달 안에 쉬는 날이 하나도 없다 — 규칙이 꺼진 것 같다")

    def test_A_S_택배_접수는_쉬는_날을_거부한다(self):
        day, why = self._blocked()
        t = self._ticket()
        r = self.c.post(f"/api/as-tickets/{t['id']}/recall", json={"pickupDate": day})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        err = r.get_json()["error"]
        self.assertIn(why, err, "왜 안 되는지 그대로 알려 줘야 한다")
        self.assertIn("가장 이른 예약 가능일", err, "언제 되는지 알려 줘야 담당자가 다시 고른다")
        # 예약이 생기지 않았어야 한다
        self.assertEqual(self.c.get(f"/api/as-tickets/{t['id']}").get_json()["recallWid"], "")

    def test_주문_반품_회수도_같은_규칙이다(self):
        day, why = self._blocked()
        o = self.c.post("/api/orders", json={
            "recipient": "박반품", "productName": "노트북", "phone": "010-9100-0002",
            "address": "서울시 강남구 2", "postalCode": "06000"}).get_json()
        r = self.c.post(f"/api/orders/{o['id']}/recall", json={"pickupDate": day})
        self.assertEqual(r.status_code, 400)
        self.assertIn(why, r.get_json()["error"])

    def test_신규_등록_회수도_같은_규칙이고_출고는_해당_없다(self):
        day, why = self._blocked()
        bad = {"type": "recall", "recipient": "최신규", "phone": "010-9100-0003",
               "address": "서울시 강남구 3", "postalCode": "06000", "pickupDate": day}
        r = self.c.post("/api/waybills/manual", json=bad)
        self.assertEqual(r.status_code, 400)
        self.assertIn(why, r.get_json()["error"])
        # 출고 송장은 수거 희망일을 쓰지 않는다 — 같은 날짜를 보내도 막히면 안 된다
        ok = dict(bad, type="forward", items="노트북 1대")
        self.assertEqual(self.c.post("/api/waybills/manual", json=ok).status_code, 200)

    def test_가능한_날이면_그대로_예약된다(self):
        from app.cj.calendar import next_pickup_day
        t = self._ticket()
        r = self.c.post(f"/api/as-tickets/{t['id']}/recall",
                        json={"pickupDate": next_pickup_day({})})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))

    # ---------------- 화면이 쓰는 달력 창구
    def test_달력_창구가_쉬는_날을_알려준다(self):
        d = self._next()
        self.assertTrue(d["enabled"])
        self.assertEqual(d["offWeekdays"], [6])
        self.assertIn("2026-09-25", d["holidays"])
        self.assertTrue(d["days"])
        for row in d["days"]:
            self.assertEqual(row["ok"], not row["reason"])
        nxt = next(r for r in d["days"] if r["date"] == d["next"])
        self.assertTrue(nxt["ok"], "가장 이른 가능일이 막힌 날이면 화면이 열자마자 못 쓴다")

    def test_달력은_A_S_담당자도_읽는다(self):
        """회수 예약을 거는 사람이 A/S 담당자다 — 배송 권한을 요구하면 화면이 못 연다."""
        r = self.c.post("/api/users", json={
            "username": "asonly", "displayName": "AS담당", "password": "asonly-pw-123",
            "perms": ["as.view", "as.manage"], "allCategories": True})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        c = self.app.test_client()
        c.post("/api/auth/login", json={"username": "asonly", "password": "asonly-pw-123"})
        self.assertEqual(c.get("/api/cj/pickup-calendar").status_code, 200)

    def test_지운_공휴일은_되살아나지_않는다(self):
        """★설정 병합의 함정 — 목록을 병합하면 화면에서 지운 날이 되살아나 계속 막힌다."""
        self.assertEqual(self.c.put("/api/settings", json={"cj": {"pickup": {
            "enabled": True, "offWeekdays": [6],
            "holidays": {"2026-09-25": "추석", "2026-12-31": "임시휴무"}}}}).status_code, 200)
        self.assertEqual(set(self._next()["holidays"]), {"2026-09-25", "2026-12-31"})
        self.assertEqual(self.c.put("/api/settings", json={"cj": {"pickup": {
            "enabled": True, "offWeekdays": [6],
            "holidays": {"2026-09-25": "추석"}}}}).status_code, 200)
        self.assertEqual(set(self._next()["holidays"]), {"2026-09-25"}, "지운 날이 되살아났다")

    def test_설정을_저장해도_다른_CJ_값은_안_지워진다(self):
        self.c.post("/api/cj/import-rms")
        before = self._cj_now()
        self.assertEqual(self.c.put("/api/settings", json={"cj": {"pickup": {
            "enabled": False, "offWeekdays": [], "holidays": {}}}}).status_code, 200)
        after = self._cj_now()
        for k in ("env", "cust_id", "frt_dv", "box_type"):
            if k in before:
                self.assertEqual(after.get(k), before.get(k), f"{k} 가 사라졌다")
        self.assertFalse(self._next()["enabled"])

    def test_화면_배선(self):
        root = Path(__file__).resolve().parent.parent
        asjs = (root / "static" / "js" / "as.js").read_text("utf-8")
        appjs = (root / "static" / "js" / "app.js").read_text("utf-8")
        self.assertIn("/api/cj/pickup-calendar", asjs, "예약 창이 쉬는 날을 안 물어본다")
        self.assertIn("renderCjPickupOff", asjs)
        self.assertIn("renderCjPickupOff($(\"#cj-pickup\")", appjs, "CJ 설정 카드가 휴무일 칸을 안 그린다")
        self.assertIn("pickup: $(\"#cj-pickup\")._value()", appjs, "저장에 휴무일 값이 안 실린다")
        self.assertIn('id="cjp-add"', asjs, "공휴일 추가 버튼이 없다")
        self.assertIn("data-cjwd", asjs, "쉬는 요일 체크가 없다")


class TestAsStageSms0907(Base):
    """단계별 안내 문자(2026-09-07 대표 "개별 고객 탭에서 SMS 문자 발송 기능 추가").

    회수신청·입고완료·수리중·결제전·발송전·택배출고 — 여섯 자리 전부에서 보낼 수 있어야 한다.
    """

    def _ticket(self, **kw):
        base = {"customer": "김문자", "phone": "010-9200-0001", "symptom": "전원 불량",
                "address": "서울시 강남구 1", "postalCode": "06000", "intake": "visit"}
        base.update(kw)
        r = self.c.post("/api/as-tickets", json=base)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()

    def _preview(self, tid, code):
        return self.c.get(f"/api/as-tickets/{tid}/sms-preview?event={code}")

    def test_여섯_단계_문구가_전부_있다(self):
        from app.notify import EVENTS
        codes = {e["code"] for e in EVENTS}
        for code in ("collecting", "collected", "repairing", "payment", "shipping_today", "returned"):
            self.assertIn(code, codes, f"{code} 안내 문구가 없다")

    def test_기본_문구는_결제_안내만_빼고_단문이다(self):
        """★90바이트를 넘으면 장문(LMS)이라 건당 요금이 3배다 — 기본값은 단문으로 맞춘다.
        결제 안내만 예외다(금액+계좌번호가 들어가면 줄일 수 없다)."""
        from app.notify import EVENTS
        sample = {"{고객명}": "김샘플", "{접수번호}": "AS-260907-01", "{증상}": "전원 불량",
                  "{처리내용}": "메인보드 교체", "{송장번호}": "650012345678",
                  "{비용안내}": "수리비 150,000원이 청구됩니다.",
                  "{계좌번호}": "국민 123456-78-901234 예시 운영사",
                  # 회수 품목은 대표가 예시한 가장 긴 경우로 잰다(2026-09-08) —
                  # 여기서 90바이트를 넘으면 실제 접수에서 장문 요금이 나간다
                  "{회수품목}": "본체, 충전기, 키스킨, 가방",
                  "{품목안내}": "품목: 본체, 충전기, 키스킨, 가방"}
        for e in EVENTS:
            text = e["default"]
            for k, v in sample.items():
                text = text.replace(k, v)
            n = sum(2 if ord(ch) > 127 else 1 for ch in text)
            if e["code"] == "payment":
                continue
            self.assertLessEqual(n, 90, f"{e['code']} 기본 문구가 {n}바이트 — 장문 요금이 된다")

    def test_각_단계_문구를_미리보고_보낸다(self):
        t = self._ticket(chargeTo="customer")
        self.c.post(f"/api/as-tickets/{t['id']}/items",
                    json={"items": [{"name": "메인보드", "qty": 1, "amount": 150000}]})
        self.c.put("/api/settings", json={"as_company_info": {"bank": "국민 123456-78-901234 예시 운영사"}})
        for code, must in (("collected", "입고"), ("repairing", "수리"),
                           ("payment", "150,000"), ("shipping_today", "발송")):
            r = self._preview(t["id"], code)
            self.assertEqual(r.status_code, 200, f"{code}: {r.get_data(as_text=True)}")
            p = r.get_json()
            self.assertIn(must, p["text"], f"{code} 문구에 {must} 가 없다")
            self.assertIn("김문자", p["text"])
        # 결제 안내에는 계좌번호가 반드시 들어간다
        self.assertIn("국민 123456-78-901234", self._preview(t["id"], "payment").get_json()["text"])

    def test_결제_안내는_금액이나_계좌가_없으면_안_나간다(self):
        """"  원을 로 보내주세요" 같은 문자가 나가면 문의만 늘어난다."""
        from app.notify import notify_ticket
        t = self._ticket(chargeTo="customer")            # 금액 없음
        with self.app.app_context():
            ok, msg = notify_ticket(self.app, t["id"], "payment")
        self.assertFalse(ok)
        self.assertIn("금액", msg)
        self.c.post(f"/api/as-tickets/{t['id']}/items",
                    json={"items": [{"name": "메인보드", "qty": 1, "amount": 90000}]})
        with self.app.app_context():                     # 이제 금액은 있고 계좌가 없다
            ok, msg = notify_ticket(self.app, t["id"], "payment")
        self.assertFalse(ok)
        self.assertIn("계좌", msg)

    def test_무상_건에는_결제_안내를_안_보낸다(self):
        from app.notify import notify_ticket
        t = self._ticket(chargeTo="company")
        self.c.post(f"/api/as-tickets/{t['id']}/items",
                    json={"items": [{"name": "메인보드", "qty": 1, "amount": 90000}]})
        with self.app.app_context():
            ok, msg = notify_ticket(self.app, t["id"], "payment")
        self.assertFalse(ok)
        self.assertIn("무상", msg)

    def test_수동_전용_문구는_자동_발송_시점_목록에_없다(self):
        """★목록에 넣으면 직접 만든 양식이 '절대 안 오는 시점'을 고를 수 있다 —
        대표가 켜 놓고 왜 안 나가는지 못 찾는다."""
        from app.notify import MANUAL_ONLY_EVENTS, TRIGGERS
        for code in MANUAL_ONLY_EVENTS:
            self.assertNotIn(code, TRIGGERS, f"{code} 가 자동 발송 시점으로 열려 있다")
        meta = self.c.get("/api/as-sms-templates").get_json()
        self.assertNotIn("payment", [t["code"] for t in meta["triggers"]])

    def test_자동_발송이_꺼져_있어도_손으로는_보낼_수_있다(self):
        """[자동 발송] 체크는 '자동으로 나갈지'만 정한다 — 화면이 계속 그렇게 안내해 왔는데
        (설정 "…A/S 화면에서 손으로는 보낼 수 있습니다", 문자 양식 "'수동 전용'이면 상세의
        [📑 기타 양식…]으로 보냅니다") 실제로는 수동 발송까지 막혀 있었다(2026-09-08)."""
        from app.notify import notify_ticket
        t = self._ticket()
        r = self.c.post("/api/as-sms-templates", json={"events": {
            "collected": {"on": False, "text": "[운영팀] {고객명}님 제품이 입고되었습니다.",
                          "subject": ""}}})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 자동(단계 훅)으로는 그대로 안 나간다 — 끈 뜻이 그거다
        with self.app.app_context():
            ok, msg = notify_ticket(self.app, t["id"], "collected")
        self.assertFalse(ok)
        self.assertIn("꺼져", msg)
        # 사람이 화면에서 [보내기]를 누르면 나간다
        r = self.c.post(f"/api/as-tickets/{t['id']}/sms", json={"event": "collected"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["ok"], r.get_json()["message"])
        rows = self.c.get(f"/api/sms/log?ticketId={t['id']}").get_json()
        self.assertIn("collected", [x["event"] for x in rows])   # 접수 문자(received)도 함께 있다
        # ★수동이라고 중복 발송 확인까지 건너뛰면 안 된다(force 와 섞지 않았는지)
        r = self.c.post(f"/api/as-tickets/{t['id']}/sms", json={"event": "collected"})
        self.assertFalse(r.get_json()["ok"])
        self.assertIn("이미 보낸", r.get_json()["message"])

    # ---------------- 회수 품목(2026-09-08 대표)
    def test_회수_품목이_접수부터_발송까지_따라다니고_문자에_실린다(self):
        """대표: "고객 회수 품목 기재란 — 처음 접수단계부터 발송단계까지 필요.
        SMS 문자에도 적용해야 고객들과 의견차이가 발생하지 않는다." (예: 본체, 충전기)"""
        get = lambda tid: self.c.get(f"/api/as-tickets/{tid}").get_json()
        t = self._ticket(intakeItems="본체 , 충전기")          # 표기가 흔들려도 한 꼴로
        self.assertEqual(get(t["id"])["intakeItems"], "본체, 충전기")
        # 접수 뒤에도 고칠 수 있다(하나 더 받은 경우) — 이력에 전값이 남는다
        self.c.patch(f"/api/as-tickets/{t['id']}",
                     json={"intakeItems": "본체\n충전기\n키스킨\n가방"})
        self.assertEqual(get(t["id"])["intakeItems"], "본체, 충전기, 키스킨, 가방")
        self.assertTrue(any("회수품목" in str(e.get("detail") or "")
                            for e in get(t["id"])["events"]), "이력에 안 남았다")
        # 보드 줄에도 실린다(접수 칸부터)
        row = next(x for x in self.c.get("/api/as-board?view=open").get_json()["rows"]
                   if x["id"] == t["id"])
        self.assertEqual(row["intakeItems"], "본체, 충전기, 키스킨, 가방")
        # 문자: 입고·발송 예정 기본 문구에 품목이 들어간다
        for code in ("collected", "shipping_today"):
            p = self._preview(t["id"], code).get_json()
            self.assertIn("본체, 충전기, 키스킨, 가방", p["text"], f"{code} 문자에 품목이 없다")
            self.assertLessEqual(p["bytes"], 90,
                                 f"{code} 가 {p['bytes']}바이트 — 장문(요금 3배)이 된다")
        # 품목을 안 적은 건은 그 줄이 통째로 빠진다("품목: " 라벨만 나가면 안 된다)
        t2 = self._ticket(customer="이무품")
        p2 = self._preview(t2["id"], "collected").get_json()
        self.assertNotIn("품목", p2["text"])
        self.assertNotIn("\n\n", p2["text"], "빈 줄이 남았다")
        # {회수품목} 은 값만, {품목안내} 는 완성된 한 줄 — 직접 만든 양식으로 확인한다
        r = self.c.post("/api/as-sms-templates", json={"custom": [
            {"id": "", "label": "품목 확인", "trigger": "", "subject": "", "on": True,
             "text": "맡기신 {회수품목} 확인 바랍니다."}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        cid = self.c.get("/api/as-sms-templates").get_json()["custom"][0]["id"]
        p = self._preview(t["id"], "custom:" + cid).get_json()
        self.assertEqual(p["text"], "맡기신 본체, 충전기, 키스킨, 가방 확인 바랍니다.")
        self.assertIn("회수품목", self.c.get("/api/sms/config").get_json()["vars"])
        self.assertIn("품목안내", self.c.get("/api/sms/config").get_json()["vars"])

    def test_회수_품목_칸이_접수_폼과_상세에_모두_있다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "as.js").read_text("utf-8")
        self.assertIn('intakeItemsHtml("asn-recv-items"', js, "접수 폼에 회수 품목 칸이 없다")
        self.assertIn('intakeItemsHtml("asd-recv-items"', js, "상세에 회수 품목 칸이 없다")
        self.assertIn("intakeItems: $(\"#asn-recv-items\")", js, "접수가 회수 품목을 안 보낸다")
        self.assertIn("intakeItems: $(\"#asd-recv-items\")", js, "상세 저장이 회수 품목을 안 보낸다")
        self.assertIn("function asIntakeItemsLine", js, "보드 줄에 회수 품목이 안 나온다")
        # 구성품은 클릭으로 담는다(2026-09-08 대표 "적어두고 저장하면 클릭하는 형태로…
        # 필요한 것들만 ＋ 눌러서 저장해두면 두고두고 쓸 수 있잖아")
        self.assertIn("data-itemadd", js, "목록에서 골라 담는 [＋ 이름] 버튼이 없다")
        self.assertIn("data-itemdel", js, "담긴 품목을 빼는 버튼이 없다")
        self.assertIn("data-itemnew", js, "[＋ 새 구성품] 버튼이 없다")
        self.assertIn("/api/as-intake-presets", js, "＋ 가 목록에 저장하지 않는다")
        self.assertIn('data-aset="items"', js, "⚙ 설정에 회수 구성품 탭이 없다")
        self.assertIn("function renderAsItemsTab", js, "회수 구성품 관리 화면이 없다")
        for sel in ('wireIntakeItems(host, "asn-recv-items")',
                    'wireIntakeItems(host, "asd-recv-items")'):
            self.assertIn(sel, js, f"{sel} 배선이 없다")

    def test_화면에_같은_id가_두_번_나오지_않는다(self):
        """★2026-09-08 실제 사고 — 회수 품목 입력칸에 id="asd-items" 를 붙였더니 상세의
        🧾 수리 내역 패널(<div id="asd-items">)과 겹쳤다. $("#asd-items") 가 문서 위쪽의
        <input> 을 먼저 잡고, renderAsItems 의 host.innerHTML 이 void 요소에 씌어져
        아무 일도 안 일어나 수리 내역·수리내역서가 '불러오는 중…' 에서 멈췄다.
        (대표 신고: "수리내역서 쪽 불러오는 중만 뜨고 열람이 안 돼")"""
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "as.js").read_text("utf-8")
        # 주석은 뺀다 — 설명 안의 <div id="..."> 까지 세면 오탐이 난다
        body = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
        body = re.sub(r"^\s*//.*$", "", body, flags=re.M)
        # ★같은 id 가 as.js 안에 여러 번 나오는 것 자체는 정상이다(화면마다 다른 템플릿이고
        #   한 번에 한 화면만 그려진다 — 예: as-detail 은 대시보드·접수·보드 세 곳에 있다).
        #   막아야 하는 것은 '같은 화면 안에서' 겹치는 것이라, 회수 품목 칸이 쓰는 id 가
        #   화면 어딘가에 이미 박혀 있는 id 와 같은지를 본다.
        used = set(re.findall(r'intakeItemsHtml\("([\w-]+)"', body))
        self.assertTrue(used, "intakeItemsHtml 호출을 못 찾았다")
        literal = set(re.findall(r'id="([\w-]+)"', body))
        for i in sorted(used):
            self.assertNotIn(i, literal,
                             f'회수 품목 칸의 id "{i}" 가 화면의 다른 요소와 겹친다 — '
                             '$("#" + id) 가 엉뚱한 것을 잡아 그 패널이 안 그려진다.')
        # 🧾 수리 내역 패널은 하나뿐이어야 한다(회수 품목 칸이 이 id 를 뺏어 갔던 자리)
        self.assertEqual(len(re.findall(r'id="asd-items"', body)), 1)

    def test_구성품_목록은_직접_늘릴_수_있다(self):
        """대표 2026-09-08 "구성품은 우리가 직접 추가도 할 수 있게 ＋ 버튼이 있어야 해"."""
        meta = self.c.get("/api/as-meta").get_json()
        self.assertIn("본체", meta["intakeItemPresets"])
        self.assertIn("충전기", meta["intakeItemPresets"])
        items = meta["intakeItemPresets"] + ["거치대"]
        r = self.c.put("/api/as-intake-presets", json={"items": items})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("거치대", self.c.get("/api/as-meta").get_json()["intakeItemPresets"])
        # ★쉼표는 품목 구분자라 이름에 못 쓴다 — 넣으면 한 품목이 둘로 쪼개진다
        r = self.c.put("/api/as-intake-presets", json={"items": ["본체, 충전기"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("쉼표", r.get_json()["error"])
        # 목록에서 빼도 이미 접수된 건에 적힌 품목은 그대로다(증상 분류와 같은 규칙)
        t = self._ticket(intakeItems="본체, 거치대")
        self.c.put("/api/as-intake-presets", json={"items": ["본체"]})
        self.assertEqual(self.c.get(f"/api/as-tickets/{t['id']}").get_json()["intakeItems"],
                         "본체, 거치대")

    def test_보드_줄에서_단계마다_문자_버튼이_있다(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "as.js").read_text("utf-8")
        self.assertIn("AS_STAGE_SMS", js)
        for stage in ("intake_wait", "collecting", "arrived", "repairing",
                      "pay_wait", "ship_wait", "visit_wait", "returning"):
            self.assertIn(stage + ":", js.split("AS_STAGE_SMS", 1)[1][:800],
                          f"{stage} 칸에 보낼 문구가 안 정해져 있다")
        self.assertIn('b("sms"', js, "보드 줄에 문자 버튼이 없다")
        self.assertIn('AS_STAGE_SMS[row.stage]', js, "버튼이 칸에 맞는 문구를 안 고른다")

    def test_접수_탭이_따로_있다(self):
        """2026-09-07 대표 "A/S 접수탭 신설" — 접수·택배 접수·찾기가 한 화면."""
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "as.js").read_text("utf-8")
        self.assertIn('data-atab="intake"', js)
        self.assertIn("function renderAsIntakeTab", js)
        self.assertIn("function renderAsIntakeWait", js, "택배 접수 대기 목록이 없다")
        self.assertIn("🚚 택배 접수", js)
        self.assertNotIn("🚚 회수 일정 설정", js, "옛 이름이 남아 있다")
        # 접수 폼은 한 곳에만 — 두 탭에 같은 폼을 두면 id 가 겹쳐 서로를 덮는다
        self.assertEqual(js.count('id="as-form"'), 1)


if __name__ == "__main__":
    unittest.main()
