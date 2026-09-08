"""엑셀 임포터 테스트 — 업무관리 주문 워크플로 원본(test/test_importers.py) 이식본.

새 모듈 경로(app.importers)로 임포트하며, 이식 시 추가한
channel 숫자 오염 방어 테스트 1건을 포함한다.
"""
import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.importers import find_libreoffice_command, import_workbook, read_first_sheet, write_xlsx  # noqa: E402

COLLECTED_SAMPLE = Path(os.getenv("COLLECTED_SAMPLE_FILE", "tests/fixtures/collected-orders.xlsx"))


class ImporterTests(unittest.TestCase):
    def test_imports_kakao_order(self):
        content = write_xlsx(
            ["결제번호", "채널상품번호", "상품명", "수령인명", "수량", "주문일시"],
            [["3371403104", "K-PRODUCT", "카카오 상품", "홍길동", "1", "2026-06-11 10:00:00"]],
        )
        orders = import_workbook(content, "kakao.xlsx")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["channel"], "카카오")
        self.assertEqual(orders[0]["orderNumber"], "3371403104")

    def test_imports_coupang_orders(self):
        content = write_xlsx(
            ["주문번호", "묶음배송번호", "등록상품명", "수취인이름", "구매수(수량)", "주문일"],
            [
                ["COUPANG-1", "BUNDLE-1", "쿠팡 상품 1", "정영희", "1", "2026-06-11 11:00:00"],
                ["COUPANG-2", "BUNDLE-2", "쿠팡 상품 2", "김고객", "1", "2026-06-11 12:00:00"],
                ["COUPANG-3", "BUNDLE-3", "쿠팡 상품 3", "이고객", "1", "2026-06-11 13:00:00"],
            ],
        )
        orders = import_workbook(content, "coupang.xlsx")
        self.assertEqual(len(orders), 3)
        self.assertEqual(orders[0]["channel"], "쿠팡")
        self.assertEqual(orders[0]["recipient"], "정영희")

    def test_exported_workbook_can_be_read(self):
        content = write_xlsx(["주문번호", "담당자"], [["100", "홍길동"]])
        self.assertEqual(read_first_sheet(content), [{"주문번호": "100", "담당자": "홍길동"}])

    def test_imports_legacy_xls_workbook(self):
        if os.getenv("RUN_LIBREOFFICE_TESTS") != "1":
            self.skipTest("RUN_LIBREOFFICE_TESTS=1일 때 실행합니다.")
        libreoffice = find_libreoffice_command()
        if not libreoffice:
            self.skipTest("LibreOffice가 없습니다.")
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            xlsx = temporary / "legacy-source.xlsx"
            xlsx.write_bytes(write_xlsx(
                ["주문번호", "상품명", "수령인", "수량"],
                [["XLS-1", "구형 엑셀 상품", "홍길동", "2"]],
            ))
            result = subprocess.run(
                [libreoffice, "--headless", "--convert-to", "xls", "--outdir", directory, str(xlsx)],
                check=False,
                capture_output=True,
                timeout=30,
            )
            xls = temporary / "legacy-source.xls"
            if result.returncode != 0 or not xls.exists():
                self.skipTest("LibreOffice에서 .xls 테스트 파일을 만들지 못했습니다.")
            orders = import_workbook(xls.read_bytes(), xls.name)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["orderNumber"], "XLS-1")
        self.assertEqual(orders[0]["quantity"], 2)

    def test_imports_html_workbook_with_xls_extension(self):
        content = """<html><body><table><tr>
        <td>주문 번호</td><td>주문일자</td><td>상품주문번호</td><td>상품코드</td>
        <td>자체상품코드</td><td>상품명</td><td>옵션정보</td><td>상품수량</td><td>판매가</td>
        <td>수취인 이름</td><td>수취인 핸드폰 번호</td><td>수취인 주소</td><td>수취인 나머지 주소</td>
        </tr><tr><td>260615052855</td><td>2026-06-15 05:28:36</td><td>106928</td><td>1000000493</td>
        <td>NT371B5M</td><td>테스트 노트북</td><td>A급</td><td>2</td><td>390000.00</td>
        <td>홍길동</td><td>010-1234-5678</td><td>서울시</td><td>101호</td></tr></table></body></html>""".encode()
        orders = import_workbook(content, "shop-export.xls")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["channel"], "고도몰")
        self.assertEqual(orders[0]["orderNumber"], "260615052855")
        self.assertEqual(orders[0]["quantity"], 2)
        self.assertEqual(orders[0]["address"], "서울시 101호")

    def test_groups_godomall_additions_into_main_order(self):
        content = """<html><body><table><tr>
        <td>주문 번호</td><td>주문일자</td><td>상품주문번호</td><td>상품코드</td><td>자체상품코드</td>
        <td>상품명</td><td>옵션정보</td><td>상품수량</td><td>판매가</td><td>총 결제 금액</td>
        <td>수취인 이름</td><td>수취인 핸드폰 번호</td><td>수취인 주소</td>
        </tr><tr><td>ORDER-1</td><td>2026-06-15</td><td>1</td><td>P1</td><td>MAIN</td>
        <td>기본 노트북</td><td>A급</td><td>1</td><td>390000</td><td>460000</td>
        <td>홍길동</td><td>010-1234-5678</td><td>서울시</td></tr>
        <tr><td>ORDER-1</td><td>2026-06-15</td><td>2</td><td>P2</td><td></td>
        <td>[추가]메모리 업그레이드</td><td></td><td>1</td><td>70000</td><td>460000</td>
        <td>홍길동</td><td>010-1234-5678</td><td>서울시</td></tr></table></body></html>""".encode()
        orders = import_workbook(content, "shop-export.xls")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["productName"], "기본 노트북")
        self.assertEqual(orders[0]["optionName"], "A급 / [추가]메모리 업그레이드")
        self.assertEqual(orders[0]["amount"], 460000)

    def test_imports_generic_order_columns(self):
        content = write_xlsx(
            ["주문번호", "주문일시", "상품명", "옵션명", "수량", "수령인", "연락처", "주소", "배송메시지"],
            [["MANUAL-1", "2026-06-12 12:00", "테스트 상품", "검정", "2", "홍길동", "010-1234-5678", "서울시", "문 앞"]],
        )
        orders = import_workbook(content, "other-channel.xlsx")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["orderNumber"], "MANUAL-1")
        self.assertEqual(orders[0]["recipient"], "홍길동")
        self.assertEqual(orders[0]["quantity"], 2)

    def test_collected_order_uses_excel_order_number(self):
        content = write_xlsx(
            ["플랫폼", "주문번호", "주문일시", "상품명 + 옵션명", "등록옵션명", "수량", "총 상품결제금액", "수취인 이름"],
            [["쿠팡", "CP-ORDER-1", "2026-06-12 12:00", "노트북 / 기본", "기본", "1", "390000", "홍길동"]],
        )
        orders = import_workbook(content, "collected.xlsx")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["channel"], "쿠팡")
        self.assertEqual(orders[0]["orderNumber"], "CP-ORDER-1")
        self.assertEqual(orders[0]["importKey"], "주문수집:쿠팡:CP-ORDER-1")

    def test_collected_order_replaces_numeric_channel_with_fallback(self):
        # 이식 시 수정한 결함: 플랫폼 칸에 금액 같은 숫자 오염값이 들어오면
        # channel에 그대로 실리던 문제 — 전부 숫자면 빈 문자열로 대체돼 "기타" 폴백이 적용된다.
        content = write_xlsx(
            ["플랫폼", "주문번호", "주문일시", "상품명 + 옵션명", "등록옵션명", "수량", "총 상품결제금액", "수취인 이름"],
            [["390,000", "CP-ORDER-9", "2026-06-12 12:00", "노트북 / 기본", "기본", "1", "390000", "홍길동"]],
        )
        orders = import_workbook(content, "collected.xlsx")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["channel"], "기타")
        self.assertEqual(orders[0]["importKey"], "주문수집:기타:CP-ORDER-9")

    def test_collected_order_groups_repeated_order_number_rows(self):
        content = write_xlsx(
            ["플랫폼", "주문번호", "주문일시", "상품명 + 옵션명", "등록옵션명", "수량", "총 상품결제금액", "수취인 이름"],
            [
                ["쿠팡", "CP-ORDER-1", "2026-06-12 12:00", "노트북 / 기본", "기본", "1", "390000", "홍길동"],
                ["쿠팡", "CP-ORDER-1", "2026-06-12 12:00", "무선마우스", "", "1", "0", "홍길동"],
            ],
        )
        orders = import_workbook(content, "collected.xlsx")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["orderNumber"], "CP-ORDER-1")
        self.assertIn("무선마우스", orders[0]["optionName"])

    def test_collected_order_combines_repeated_same_product_into_quantity(self):
        product = "LG전자 15.6인치 인텔 i5 초가성비 노트북 사무용 영상시청용 우측숫자키패드 탑재 윈도우11"
        content = write_xlsx(
            ["플랫폼", "주문번호", "주문일시", "상품명 + 옵션명", "등록옵션명", "수량", "총 상품결제금액", "수취인 이름"],
            [
                ["쿠팡", "CP-ORDER-2", "2026-06-12 12:00", product, "기본", "1", "390000", "나다은"],
                ["쿠팡", "CP-ORDER-2", "2026-06-12 12:00", product, "", "1", "390000", "나다은"],
            ],
        )
        orders = import_workbook(content, "collected.xlsx")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["productName"], product)
        self.assertEqual(orders[0]["quantity"], 2)
        self.assertEqual(orders[0]["optionName"], "기본")

    def test_collected_order_counts_different_notebooks_and_keeps_both_product_lines(self):
        samsung = "삼성전자 노트북7 프로 지포스 MX110 인텔 i7 9세대 15.6인치 대화면 FHD 지문인식 우측넘버패드 탑재 윈도우11\n제품등급선택 (필수): S급 외관 / S급 배터리"
        lg = "LG전자 15.6인치 인텔 i5 초가성비 노트북 사무용 영상시청용 우측숫자키패드 탑재 윈도우11"
        content = write_xlsx(
            ["플랫폼", "주문번호", "주문일시", "상품명 + 옵션명", "등록옵션명", "수량", "총 상품결제금액", "수취인 이름"],
            [
                ["쿠팡", "CP-ORDER-3", "2026-06-12 12:00", samsung, "S급", "1", "390000", "나다은"],
                ["쿠팡", "CP-ORDER-3", "2026-06-12 12:00", lg, "", "1", "390000", "나다은"],
            ],
        )
        orders = import_workbook(content, "collected.xlsx")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["productName"], samsung)
        self.assertEqual(orders[0]["quantity"], 2)
        self.assertIn(lg, orders[0]["optionName"])

    def test_collected_order_groups_repeated_recipient_rows_without_order_number(self):
        content = write_xlsx(
            ["플랫폼", "주문일시", "상품명 + 옵션명", "등록옵션명", "수량", "총 상품결제금액", "수취인 이름", "연락처", "주소"],
            [
                ["쿠팡", "2026-06-12 12:00", "노트북 / 기본", "기본", "1", "390000", "홍길동", "010-1111-2222", "서울시 강남구"],
                ["쿠팡", "2026-06-12 12:00", "무선마우스", "", "1", "0", "홍길동", "010-1111-2222", "서울시 강남구"],
            ],
        )
        orders = import_workbook(content, "collected.xlsx")
        self.assertEqual(len(orders), 1)
        self.assertTrue(orders[0]["orderNumber"].startswith("수집-"))
        self.assertIn("무선마우스", orders[0]["optionName"])

    def test_imports_orders_with_alternate_date_and_address_headers(self):
        content = write_xlsx(
            ["주문번호", "주문일자", "상품명", "옵션명", "수량", "수령인", "배송지 주소", "상세주소"],
            [["ALT-1", "2026-06-16 09:30", "테스트 상품", "기본", "1", "홍길동", "서울특별시 중구", "세종대로 1"]],
        )
        orders = import_workbook(content, "alt-channel.xlsx")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["orderedAt"], "2026-06-16 09:30")
        self.assertEqual(orders[0]["address"], "서울특별시 중구 세종대로 1")

    def test_imports_collected_order_workbook(self):
        if os.getenv("RUN_EXTERNAL_SAMPLE_TESTS") != "1":
            self.skipTest("RUN_EXTERNAL_SAMPLE_TESTS=1일 때 실행합니다.")
        if not COLLECTED_SAMPLE.exists():
            self.skipTest("주문수집 샘플 파일이 없습니다.")
        orders = import_workbook(COLLECTED_SAMPLE.read_bytes(), COLLECTED_SAMPLE.name)
        self.assertEqual(len(orders), 32)
        self.assertEqual(orders[0]["channel"], "쿠팡")
        self.assertEqual(orders[0]["amount"], 390000)
        self.assertEqual(orders[4]["channel"], "고도몰")
        self.assertIn("무선키마세트", orders[4]["optionName"])

    def test_source_zip_contains_two_excel_files(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("kakao.xlsx", write_xlsx(["주문번호"], [["1"]]))
            archive.writestr("coupang.xlsx", write_xlsx(["주문번호"], [["2"]]))
        with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
            excel_names = [name for name in archive.namelist() if name.endswith(".xlsx")]
        self.assertEqual(len(excel_names), 2)


class TemuExcelTests(unittest.TestCase):
    """테무 주문 엑셀(2026-09-01 실파일로 확정) — 다른 몰과 달라 함정이 많다.

    실측 확인: 헤더가 6행(위 5줄은 발송 주의 안내문) / '수령인 이름' 열이 두 개(뒤는 빈 칸) /
    주소가 서양식으로 쪼개짐 / 전화가 '+82 010 …' / 판매자 SKU가 비어 올 수 있음.
    """

    HEADERS = ["주문 ID", "주문 상태", "상품 주문 ID", "주문 상품 상태", "제품 이름",
               "선택 사항", "제공 sku", "SKU ID", "구매 수량", "발송할 수량",
               "수령인 이름", "수령인 이름", "수령인 성", "수령인 전화번호",
               "배송 주소 1", "배송 도시", "배송 주",
               "배송 우편번호(다음 우편번호로 발송해야 합니다.)", "구매 날짜",
               "할인 후 기본 가격 총액", "기본 가격 총액", "소매 가격(세금 제외)",
               "판매자 할인", "Temu 할인"]

    def _row(self, **kw):
        base = {
            "주문 ID": "PO-185-0501", "주문 상태": "미발송", "상품 주문 ID": "185-0501",
            "주문 상품 상태": "미발송", "제품 이름": "삼성 리퍼노트북 NT371B5L",
            "선택 사항": "Single Color", "제공 sku": "", "SKU ID": "130992207601233",
            "구매 수량": "1", "발송할 수량": "1",
            "수령인 이름": "정재현", "수령인 성": "", "수령인 전화번호": "+82 010 1234 5678",
            "배송 주소 1": "노해로70길32", "배송 도시": "도봉구", "배송 주": "서울특별시",
            "배송 우편번호(다음 우편번호로 발송해야 합니다.)": "01446",
            "구매 날짜": "2026년 9월 1일 07:54 KST(UTC+9)",
            "할인 후 기본 가격 총액": "260,610원", "기본 가격 총액": "260,610원",
            "소매 가격(세금 제외)": "280,156원", "판매자 할인": "0원", "Temu 할인": "16,809원",
        }
        base.update(kw)
        # 중복 헤더('수령인 이름' 2개)를 실제 파일처럼 재현한다 — 뒤 칸은 빈 값
        return [base["주문 ID"], base["주문 상태"], base["상품 주문 ID"], base["주문 상품 상태"],
                base["제품 이름"], base["선택 사항"], base["제공 sku"], base["SKU ID"],
                base["구매 수량"], base["발송할 수량"], base["수령인 이름"], "",
                base["수령인 성"], base["수령인 전화번호"], base["배송 주소 1"],
                base["배송 도시"], base["배송 주"],
                base["배송 우편번호(다음 우편번호로 발송해야 합니다.)"], base["구매 날짜"],
                base["할인 후 기본 가격 총액"], base["기본 가격 총액"],
                base["소매 가격(세금 제외)"], base["판매자 할인"], base["Temu 할인"]]

    def _orders(self, *rows):
        content = write_xlsx(self.HEADERS, list(rows) or [self._row()])
        return import_workbook(content, "temu.xlsx")

    def test_테무_양식을_알아본다(self):
        orders = self._orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["channel"], "테무")
        self.assertEqual(orders[0]["orderNumber"], "PO-185-0501")
        self.assertEqual(orders[0]["importKey"], "테무:PO-185-0501")

    def test_중복된_수령인_이름_열이_이름을_지우지_않는다(self):
        """실파일은 '수령인 이름' 열이 두 개고 뒤가 빈 칸이다 — 단순 dict 변환이면
        뒤의 빈 칸이 이름을 덮어 수취인이 사라진다(송장 발행 불가)."""
        self.assertEqual(self._orders()[0]["recipient"], "정재현")

    def test_전화번호가_국내표기로_바뀐다(self):
        """테무는 국가번호 뒤에도 국내 앞자리 0을 남긴다('+82 010 …') — 0을 덧붙이면 안 된다."""
        self.assertEqual(self._orders()[0]["phone"], "010-1234-5678")
        # 국가번호 뒤 0이 없는 표기도 같은 결과여야 한다
        got = self._orders(self._row(**{"수령인 전화번호": "+82 10 1234 5678"}))
        self.assertEqual(got[0]["phone"], "010-1234-5678")

    def test_주소를_한국_순서로_합친다(self):
        self.assertEqual(self._orders()[0]["address"], "서울특별시 도봉구 노해로70길32")
        self.assertEqual(self._orders()[0]["postalCode"], "01446")

    def test_주문일시를_변환한다(self):
        self.assertEqual(self._orders()[0]["orderedAt"], "2026-09-01 07:54")

    def test_매출은_할인후_기본가에_정산_가산율을_더한_값이다(self):
        """★두 규칙이 겹쳐 있다.

        ① 기준가: 소매가(280,156)·Temu 할인은 테무가 소비자에게 매긴 값이라 우리 매출이
           아니다. 판매자 할인만 우리 몫에서 빠진다(롯데온 '업체 분담' 기준과 같은 결).
        ② 가산율: 그 기준가는 **부가세가 빠진 값**이다(대표 확정 2026-09-07 — "테무는
           부가세 미포함가로 주문수집이 되고, 정산은 결제금액 + 10.75% 다").
           OWS의 다른 몰 금액은 전부 소비자가 낸 부가세 포함 총액이라, 여기서 올려놓지
           않으면 테무 매출만 실입금보다 계속 낮게 잡힌다.
        ★기대값을 원값(260,610)으로 되돌리지 말 것 — 매출·마진·부가세가 전부 어긋난다.
        """
        self.assertEqual(self._orders()[0]["amount"], 288626)   # 260,610 × 1.1075
        got = self._orders(self._row(**{"할인 후 기본 가격 총액": "", "판매자 할인": "10,000원"}))
        self.assertEqual(got[0]["amount"], 277551, "판매자 할인이 안 빠졌다")  # 250,610 기준

    def test_정산_보정의_근거를_메모로_남긴다(self):
        """금액만 바꿔 놓고 근거를 안 남기면 나중에 몰 화면과 왜 다른지 아무도 설명하지
        못한다. 목록에서 📌 메모로 바로 대조된다."""
        memo = self._orders()[0]["memo"]
        self.assertIn("260,610", memo, "몰이 준 원값이 안 남았다")
        self.assertIn("288,626", memo)
        self.assertIn("10.75", memo)

    def test_정산액은_버림도_짝수반올림도_아닌_사사오입이다(self):
        """★float 로 계산하면 두 군데서 배신한다: 260610*1.1075 이 288625.57499999995 로
        나오고, 정확히 .5 로 떨어지는 값에서 round() 는 짝수 반올림을 한다(288,614).
        세무 관행은 사사오입(288,615)이라 정수식으로 잰다."""
        got = self._orders(self._row(**{"할인 후 기본 가격 총액": "260,600원"}))
        self.assertEqual(got[0]["amount"], 288615, "짝수 반올림(288,614)으로 새면 안 된다")
        got = self._orders(self._row(**{"할인 후 기본 가격 총액": "100원"}))
        self.assertEqual(got[0]["amount"], 111)     # 110.75 → 111 (버림이면 110)

    def test_정산_보정은_주문_합계에_한_번만_건다(self):
        """행마다 걸면 행별 반올림 오차가 쌓여 2행이면 1원, 4행이면 2원이 테무 입금액과
        어긋난다. 테무도 주문 단위로 입금하므로 대조 축을 주문에 맞춘다."""
        got = self._orders(
            self._row(),
            self._row(**{"상품 주문 ID": "185-0502"}),
        )
        self.assertEqual(got[0]["amount"], 577251,
                         "행별로 곱했다(577,252) — 합계에 한 번만 걸어야 한다")

    def test_금액이_0이면_보정도_메모도_없다(self):
        """0원 주문에 도장만 찍혀 있으면 '보정된 줄 알고' 아무도 다시 안 본다."""
        got = self._orders(self._row(**{"할인 후 기본 가격 총액": "0원", "기본 가격 총액": "0원"}))
        self.assertEqual(got[0]["amount"], 0)
        self.assertEqual(got[0]["memo"], "")

    def test_테무_내부번호는_제품코드로_쓰지_않는다(self):
        """'SKU ID'(130992207601233)는 테무 내부번호다 — 제품코드 칸에 넣으면 고도몰
        스펙·자산 재고 매칭을 막는다(롯데온 LO번호와 같은 함정)."""
        o = self._orders()[0]
        self.assertEqual(o["productCode"], "", "내부번호가 제품코드 칸에 들어갔다")
        self.assertEqual(o["mallProductId"], "130992207601233", "내부번호 보존 축이 비었다")
        self.assertEqual(o["mallItemId"], "185-0501")

    def test_판매자_SKU가_있으면_제품코드로_쓴다(self):
        got = self._orders(self._row(**{"제공 sku": "NT371B5L_i3-6_내장 AS"}))
        self.assertEqual(got[0]["productCode"], "NT371B5L_i3-6_내장 AS")

    def test_기본_옵션값은_옵션으로_남기지_않는다(self):
        """'Single Color'는 옵션 없는 상품에 테무가 붙이는 기본값이다."""
        self.assertEqual(self._orders()[0]["optionName"], "")
        got = self._orders(self._row(**{"선택 사항": "RAM 16GB"}))
        self.assertEqual(got[0]["optionName"], "RAM 16GB")

    def test_취소건과_전량취소는_수집하지_않는다(self):
        """파일에는 정상 주문과 취소 주문이 섞여 온다 — 정상 건만 들어와야 한다."""
        got = self._orders(
            self._row(),
            self._row(**{"주문 ID": "PO-185-0502", "주문 상품 상태": "취소됨"}),
            self._row(**{"주문 ID": "PO-185-0503", "발송할 수량": "0"}),
        )
        self.assertEqual([o["orderNumber"] for o in got], ["PO-185-0501"])

    def test_부분취소_주문의_남은_상품은_살린다(self):
        """★행 제외 근거는 '주문 상품 상태'(상품 단위)만 쓴다 — 주문 단위 칸에 '부분 취소'가
        찍혔다고 아직 보내야 하는 상품행까지 버리면 출고 누락이 된다."""
        got = self._orders(
            self._row(**{"주문 상태": "부분 취소", "주문 상품 상태": "취소됨",
                         "발송할 수량": "0", "제품 이름": "취소된 상품"}),
            self._row(**{"주문 상태": "부분 취소", "주문 상품 상태": "미발송",
                         "상품 주문 ID": "185-0502", "제품 이름": "보내야 할 노트북"}),
        )
        self.assertEqual(len(got), 1, "부분취소 주문이 통째로 사라졌다")
        self.assertEqual(got[0]["productName"], "보내야 할 노트북")

    def test_전부_취소면_이유를_알려준다(self):
        """전부 걸러졌을 때 '양식이 안 맞다'로 오해하게 두면 안 된다."""
        with self.assertRaises(ValueError) as cm:
            self._orders(self._row(**{"주문 상품 상태": "취소됨"}))
        self.assertIn("취소", str(cm.exception))

    def test_액세서리가_노트북_수량을_부풀리지_않는다(self):
        """노트북 1대 + 가방 1개를 '노트북 2대'로 만들면 그 수량이 송장까지 나간다."""
        got = self._orders(
            self._row(**{"제품 이름": "삼성 리퍼 노트북 NT371B5L"}),
            self._row(**{"상품 주문 ID": "185-0502", "제품 이름": "노트북 가방",
                         "선택 사항": "검정", "할인 후 기본 가격 총액": "10,000원"}),
        )
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["quantity"], 1, "액세서리가 대표 상품 수량에 더해졌다")
        # 270,610(=260,610+10,000) × 1.1075 — 합산 뒤 정산 보정 한 번
        self.assertEqual(got[0]["amount"], 299701, "금액은 주문 전체를 합산해야 한다")
        self.assertIn("노트북 가방", got[0]["optionName"])

    def test_모르는_날짜표기는_가짜날짜를_만들지_않는다(self):
        """영문 표기를 억지로 읽어 '2026-07-54' 같은 없는 날짜를 만들면 그 주문은
        어느 달 조회에도 안 잡히는 유령이 된다."""
        got = self._orders(self._row(**{"구매 날짜": "Sep 1, 2026 07:54 KST(UTC+9)"}))
        self.assertNotEqual(got[0]["orderedAt"], "2026-07-54")
        self.assertIn("Sep 1, 2026", got[0]["orderedAt"], "못 읽으면 원문을 보존해야 한다")

    def test_오후_표기를_12시간_틀리지_않게_읽는다(self):
        got = self._orders(self._row(**{"구매 날짜": "2026년 9월 1일 오후 7:54 KST(UTC+9)"}))
        self.assertEqual(got[0]["orderedAt"], "2026-09-01 19:54")
        got = self._orders(self._row(**{"구매 날짜": "2026년 9월 1일 오전 12:30 KST"}))
        self.assertEqual(got[0]["orderedAt"], "2026-09-01 00:30")

    def test_같은_주문의_여러_상품행을_한_건으로_묶는다(self):
        """같은 상품을 2대 사면 수량 2, 금액은 주문 전체 합계."""
        got = self._orders(
            self._row(),
            self._row(**{"상품 주문 ID": "185-0502", "할인 후 기본 가격 총액": "260,610원"}),
        )
        self.assertEqual(len(got), 1, "같은 주문 ID가 두 건으로 갈렸다")
        self.assertEqual(got[0]["quantity"], 2)
        # 521,220 × 1.1075 = 577,250.85 → 577,251 (행별로 곱하면 577,252)
        self.assertEqual(got[0]["amount"], 577251)


class EsmExcelTests(unittest.TestCase):
    """ESM PLUS '발송관리' 엑셀(옥션·G마켓) 수기 업로드.

    ★이 양식은 숫자·날짜를 엑셀 원시값으로 준다. 우리 리더는 <v> 태그를 그대로 읽으므로
      주문번호가 '2.569834423E9'(지수표기), 주문일이 '46266.8388…'(일련번호)로 들어온다.
      실파일(발송관리.xlsx, 2026-09-04)에서 확인한 값 그대로 시험한다.
    """

    HEADERS = ["판매아이디", "주문번호", "주문상태", "상품번호", "상품명",
               "택배사명(발송방법)", "송장번호", "수령인명", "구매자명", "수량",
               "판매단가", "판매금액", "판매자 관리코드", "수령인 휴대폰",
               "우편번호", "주소", "배송시 요구사항", "주문일(결제확인전)",
               "결제일", "주문일", "정산예정금액", "서비스이용료"]

    def _row(self, **kw):
        base = {
            "판매아이디": "옥션(qpsgj260)", "주문번호": "2.569834423E9",
            "주문상태": "발송처리필요", "상품번호": "F648513503",
            "상품명": "삼성 본체 윈도우11 가정용 컴퓨터 DB400T3 데스크탑",
            "택배사명(발송방법)": "CJ택배", "송장번호": "",
            "수령인명": "정성훈", "구매자명": "정성훈", "수량": "1.0",
            "판매단가": "190000.0", "판매금액": "190000.0",
            "판매자 관리코드": "DB400T3_i5-4_내장",
            "수령인 휴대폰": "010-3852-3968", "우편번호": "51769",
            "주소": "경상남도 창원시 마산합포구 월영남1길 15 (진달래맨션)",
            "배송시 요구사항": "", "주문일(결제확인전)": "46266.83888888889",
            "결제일": "46266.83888888889", "주문일": "46267.370833333334",
            "정산예정금액": "176700.0", "서비스이용료": "13300.0",
        }
        base.update(kw)
        return [base[h] for h in self.HEADERS]

    def _orders(self, *rows):
        content = write_xlsx(self.HEADERS, list(rows) or [self._row()])
        return import_workbook(content, "발송관리.xlsx")

    def test_ESM_양식을_알아본다(self):
        """★'수령인명' 열이 있어 카카오 분기가 먼저 삼킬 수 있다 — 앞에서 잡아야 한다."""
        orders = self._orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["channel"], "ESM(옥션)",
                         "카카오로 잘못 갔거나 채널 표기가 API 수집분과 다르다")

    def test_지수표기_주문번호를_되돌린다(self):
        """엑셀이 숫자로 저장한 주문번호가 '2.569834423E9'로 온다.
        그대로 두면 API 수집분(ESM:2569834423)과 다른 주문이 되어 두 번 들어온다."""
        o = self._orders()[0]
        self.assertEqual(o["orderNumber"], "2569834423")
        self.assertEqual(o["importKey"], "ESM:2569834423",
                         "API 수집분과 키가 달라 중복으로 들어온다")
        # '2569834423.0' 형태로 와도 같아야 한다
        got = self._orders(self._row(**{"주문번호": "2569834423.0"}))
        self.assertEqual(got[0]["orderNumber"], "2569834423")
        # 애초에 문자열 주문번호면 그대로 둔다
        got = self._orders(self._row(**{"주문번호": "A-2026-0001"}))
        self.assertEqual(got[0]["orderNumber"], "A-2026-0001")

    def test_엑셀_일련번호_날짜를_되돌린다(self):
        self.assertEqual(self._orders()[0]["orderedAt"], "2026-09-01 20:08:00")
        # 이미 사람이 읽는 형태면 건드리지 않는다
        got = self._orders(self._row(**{"주문일(결제확인전)": "2026-09-01 20:08:00"}))
        self.assertEqual(got[0]["orderedAt"], "2026-09-01 20:08:00")

    def test_자체코드는_판매자_관리코드에서_온다(self):
        """'상품번호'(F648513503)는 ESM 내부번호다 — 제품코드 칸에 넣으면 고도몰
        스펙·자산 재고 매칭이 통째로 어긋난다(롯데온 LO번호와 같은 함정)."""
        o = self._orders()[0]
        self.assertEqual(o["productCode"], "DB400T3_i5-4_내장")
        self.assertEqual(o["mallProductId"], "F648513503", "내부번호 보존 축이 비었다")

    def test_수량과_금액이_숫자로_들어온다(self):
        o = self._orders()[0]
        self.assertEqual(o["quantity"], 1)
        self.assertEqual(o["amount"], 190000,
                         "매출은 판매금액이다 — 서비스이용료(수수료)는 매출에서 빼지 않는다")

    def test_발송완료_취소는_안_가져온다(self):
        """고객이 취소한 상품을 꺼내 포장하면 안 된다."""
        with self.assertRaises(ValueError) as e:
            self._orders(self._row(**{"주문상태": "발송완료"}))
        self.assertIn("건너뛴", str(e.exception))
        with self.assertRaises(ValueError):
            self._orders(self._row(**{"주문상태": "취소완료"}))

    def test_수령인이_비면_구매자명을_쓴다(self):
        got = self._orders(self._row(**{"수령인명": ""}))
        self.assertEqual(got[0]["recipient"], "정성훈")


class ProductCodeHintTests(unittest.TestCase):
    """제품코드가 빈 주문에 '후보'를 메모로 남긴다(대표 2026-09-01 "테무 파일은
    자체상품코드가 같이 안 나온다").

    ★코드 칸에 직접 박지 않는다 — 라이브 실증상 같은 상품명의 실제 모델이 재입고마다
      갈아끼워져(같은 listing 이 4번 로테이션) '후보 유일'이 정답을 뜻하지 않는다.
      잘못 박히면 셋팅 재고·송장 요구등급까지 그 코드를 따라가 출고 직전까지 안 걸린다.
    """

    def _conn(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE orders(product_name TEXT, product_code TEXT, "
                     "ordered_at TEXT DEFAULT '', cancelled_at TEXT DEFAULT '')")
        return conn

    def _hint(self, conn, added):
        from app.orders.importing import suggest_product_code
        return suggest_product_code(conn, added)

    def _add(self, conn, name, code, ordered_at="2026-08-01"):
        conn.execute("INSERT INTO orders(product_name, product_code, ordered_at, cancelled_at) "
                     "VALUES(?,?,?,'')", (name, code, ordered_at))

    def test_후보를_메모로_알린다(self):
        conn = self._conn()
        self._add(conn, "삼성 리퍼 NT371B5L", "NT371B5L_i3-6_내장 AS")
        orders = [{"productName": "삼성 리퍼 NT371B5L", "productCode": "", "memo": ""}]
        self.assertEqual(self._hint(conn, orders), 1)
        self.assertIn("NT371B5L_i3-6_내장 AS", orders[0]["memo"])
        self.assertEqual(orders[0]["productCode"], "",
                         "★코드 칸에 추측값을 박으면 안 된다(잘못된 자산이 출고된다)")

    def test_코드가_이미_있으면_아무것도_안_한다(self):
        conn = self._conn()
        self._add(conn, "삼성 리퍼 NT371B5L", "NT371B5L_i3-6_내장 AS")
        orders = [{"productName": "삼성 리퍼 NT371B5L", "productCode": "840 G3_i7-6_내장",
                   "memo": ""}]
        self.assertEqual(self._hint(conn, orders), 0)
        self.assertEqual(orders[0]["memo"], "")

    def test_후보가_여럿이면_전부_보여주고_경고한다(self):
        """같은 상품명에 코드가 여럿이면 '재입고로 갈아끼워졌다'는 뜻이다 —
        하나를 고르지 말고 사람이 실물을 보게 한다. 최근에 팔린 코드가 앞에 온다."""
        conn = self._conn()
        self._add(conn, "삼성 리퍼 NT371B5L", "NT371B5L_i3-6_내장 AS", "2026-07-01")
        self._add(conn, "삼성 리퍼 NT371B5L", "NT371B5J_i5-4_내장 AA", "2026-08-20")
        orders = [{"productName": "삼성 리퍼 NT371B5L", "productCode": "", "memo": ""}]
        self.assertEqual(self._hint(conn, orders), 1)
        memo = orders[0]["memo"]
        self.assertIn("NT371B5J_i5-4_내장 AA", memo)
        self.assertIn("NT371B5L_i3-6_내장 AS", memo)
        self.assertLess(memo.index("NT371B5J"), memo.index("NT371B5L"), "최근 코드가 앞이어야 한다")
        self.assertIn("여러 개", memo, "여러 후보라는 경고가 없다")

    def test_몰_내부번호는_후보로_삼지_않는다(self):
        conn = self._conn()
        self._add(conn, "삼성 리퍼 NT371B5L", "95937740368")
        orders = [{"productName": "삼성 리퍼 NT371B5L", "productCode": "", "memo": ""}]
        self.assertEqual(self._hint(conn, orders), 0)
        self.assertEqual(orders[0]["memo"], "")

    def test_기존_메모를_지우지_않는다(self):
        conn = self._conn()
        self._add(conn, "삼성 리퍼 NT371B5L", "NT371B5L_i3-6_내장 AS")
        orders = [{"productName": "삼성 리퍼 NT371B5L", "productCode": "", "memo": "선물포장"}]
        self._hint(conn, orders)
        self.assertTrue(orders[0]["memo"].startswith("선물포장"))
        self.assertIn("NT371B5L_i3-6_내장 AS", orders[0]["memo"])

    def test_중복판정_뒤에_불린다(self):
        """★코드/메모를 중복 판정 '앞'에서 건드리면 쿠팡 교차 중복키(productCode 를 신원
        축으로 쓴다)가 흔들려 같은 주문이 두 건으로 들어온다 — 배선 순서를 핀한다."""
        src = (ROOT / "app" / "orders" / "importing.py").read_text("utf-8")
        for block in ("def import_preview", "def import_orders"):
            body = src.split(block, 1)[1].split("return jsonify", 1)[0]
            self.assertLess(body.index("new_unique_orders"), body.index("suggest_product_code"),
                            f"{block}: 후보 제안이 중복 판정보다 먼저 돈다")
        self.assertNotIn("fill_missing_product_code", src, "옛 자동채움 함수가 남아 있다")


if __name__ == "__main__":
    unittest.main()
