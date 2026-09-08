"""SQLite(WAL) 접근 계층.

원칙(개발계획서 §2):
- 단일 DB 원본, 레코드 단위 upsert — "전체 읽고 전체 덮어쓰기" 금지
- 쓰기는 tx(write=True) 트랜잭션 경유(BEGIN IMMEDIATE)
- 쓰기 전 일별 백업 보장 + 보존기간 초과분 정리
"""
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from flask import current_app, g

from . import config

_backup_lock = threading.Lock()
_SCHEMA = Path(__file__).with_name("schema.sql")


def _connect(db_path):
    conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


# 기존 DB에 나중에 추가된 컬럼들 — 스키마 파일의 CREATE TABLE IF NOT EXISTS는
# 이미 있는 테이블을 바꾸지 않으므로, 여기서 ALTER TABLE로 채운다.
_ADDED_COLUMNS = {
    "assets": [
        ("sale_price", "INTEGER NOT NULL DEFAULT 0"),
        ("location", "TEXT NOT NULL DEFAULT ''"),
        ("cpu", "TEXT NOT NULL DEFAULT ''"),
        ("gpu", "TEXT NOT NULL DEFAULT ''"),
        ("ram", "TEXT NOT NULL DEFAULT ''"),
        ("ssd", "TEXT NOT NULL DEFAULT ''"),
        ("inch", "TEXT NOT NULL DEFAULT ''"),
        ("battery", "TEXT NOT NULL DEFAULT ''"),
        ("charger", "TEXT NOT NULL DEFAULT ''"),
        ("received", "INTEGER NOT NULL DEFAULT 1"),      # 입고확인 완료 여부
        ("received_at", "TEXT NOT NULL DEFAULT ''"),
        ("received_by", "TEXT NOT NULL DEFAULT ''"),
        # 제품코드 = 쇼핑몰 재고의 축(예: 840 G3_i7-6_내장). 자산번호와는 다른 개념이다 —
        # 자산번호는 개체 하나(출고의 축), 제품코드는 같은 사양 묶음(재고의 축).
        # ★대표가 직접 기입한다 — 자동으로 채우지 않는다(2026-08-04 지시).
        ("product_code", "TEXT NOT NULL DEFAULT ''"),
        # 재고반영 여부. ★기본 0 — 제품코드를 넣었다고 바로 몰 재고에 잡히면 안 된다.
        # 수리를 다녀와서 올리는 경우가 있어, 사람이 체크했을 때만 재고로 센다(2026-08-04 지시).
        ("stock_listed", "INTEGER NOT NULL DEFAULT 0"),
        ("stock_listed_at", "TEXT NOT NULL DEFAULT ''"),
        ("stock_listed_by", "TEXT NOT NULL DEFAULT ''"),
        # 재고 3단계(2026-08-04 대표, 2026-08-08 이름 확정: 양품→가용):
        # 가용=셋팅완료·촬영용 등 바로 판매 가능 / 실재고=셋팅·시트지·간단보수 후
        # 판매 가능 / 가재고=짜집기·불용 — 판매 재고로 잡지 않는다.
        #   가재고만은 매입에서 가용/실재고로 바꾸기 전까지 출고가 막힌다.
        #   (기존 '양품' 값은 아래 _migrate 의 일회성 UPDATE가 '가용'으로 바꾼다)
        # 재고비고 — TMS 재고상세의 짧은 상태 메모(2026-09-03, 창구 재고내역 → 빈 칸 채우기·3방향 정정, 자산 상세에서 편집)
        ("stock_note", "TEXT NOT NULL DEFAULT ''"),
        ("tier", "TEXT NOT NULL DEFAULT '가용'"),
        # ★보수 체크(2026-08-08 대표): 실재고·가재고 자산마다 무엇을 보수해야 하는지,
        #   하는 중인지(해야함/작업중/완료)를 항목별로 기록한다. JSON:
        #   {"items":[{"name":"시트지","state":"doing"},...],"note":"기타 메모"}
        ("tier_tasks", "TEXT NOT NULL DEFAULT ''"),
        # ★TMS 자동반영 잠금(2026-08-18). 1이면 TMS 엑셀이 이 자산을 건드리지 않는다 —
        #   값 채우기·상태 자동갱신·판매이력 전부. 번호를 바로잡은 자산에 선다.
        #   TMS 쪽 기록이 틀렸다고 사람이 판정한 자산이라, 다시 따라가면 원상복구된다.
        ("tms_lock", "INTEGER NOT NULL DEFAULT 0"),
        ("tms_lock_note", "TEXT NOT NULL DEFAULT ''"),
        # ★TMS 그림자(2026-08-25) — 지난 수집 때 TMS가 말한 값(JSON). 3방향 대조의 기준:
        #   OWS 값이 그림자와 같으면(사람이 안 건드림) TMS 정정을 따라가고, 다르면 보호한다.
        ("tms_shadow", "TEXT NOT NULL DEFAULT ''"),
        # ★사업부 귀속(2026-08-04 대표 결정, 2026-08-12 규칙 확정)
        #   렌탈 사업부(RMS) 귀속 자산은 팔 수 있는 물건이 아니다 — 판매재고에서 빠져야 한다.
        #   ★기본값 'sale' — 기존 판매 자산은 손대지 않고 그대로 정상이다.
        ("division", "TEXT NOT NULL DEFAULT 'sale'"),        # sale | rental
        ("division_since", "TEXT NOT NULL DEFAULT ''"),
        ("division_by", "TEXT NOT NULL DEFAULT ''"),
        ("division_ref", "TEXT NOT NULL DEFAULT ''"),        # 이관 커밋 ID
        ("division_note", "TEXT NOT NULL DEFAULT ''"),       # 예외 사유(무시한 판매전표 등)
        # ★예외 잠금 — 사람이 판단한 건은 자동 판정·TMS 재이관이 뒤집으면 안 된다.
        ("division_locked", "INTEGER NOT NULL DEFAULT 0"),
        # ★이관 보류 사유 해제(2026-09-01 대표) — "TMS에서 반입처리가 됐어야 하는데
        #   담당자가 깜빡했다. 임의로 풀 수 있는 방법이 있나?"
        #
        #   판매 기록(asset_events '판매')은 지난 일이라 지울 수도, 주문 취소로 풀 수도 없다.
        #   실제로는 반품·오등록이라 그 기록이 틀린 경우가 있는데, 지금은 푸는 길이 없어
        #   자산이 영영 이관 불가로 남는다.
        #   → 사람이 "이 사유는 틀렸다"고 판정한 것을 자산에 기록으로 남기고 그 가드만 푼다.
        #
        # ★풀 수 있는 것은 '기록이 틀렸을 수 있는' 두 가지뿐이다(dup_buy·sold_rec).
        #   대여중·폐기·진행중 커밋 같은 물리적·운영상 사실은 여기서 못 푼다 —
        #   그건 기록이 틀린 게 아니라 지금 그런 상태인 것이다.
        ("hold_override", "TEXT NOT NULL DEFAULT ''"),        # 'sold_rec,dup_buy'
        ("hold_override_note", "TEXT NOT NULL DEFAULT ''"),   # 왜 풀었는지(필수)
        ("hold_override_by", "TEXT NOT NULL DEFAULT ''"),
        ("hold_override_at", "TEXT NOT NULL DEFAULT ''"),
        # ★TMS 삭제 표시(2026-09-02) — 연동 창구(data-bridge)가 'TMS에서 지워졌다'고 알려 준 자산.
        #   지우지 않고 표시만 한다(사본에 원본이 보존돼 있어 되살릴 수 있다). TMS에서 복구되면 지워진다.
        ("tms_deleted_at", "TEXT NOT NULL DEFAULT ''"),
        ("tms_deleted_note", "TEXT NOT NULL DEFAULT ''"),
    ],
    "sale_slips": [
        ("source", "TEXT NOT NULL DEFAULT 'TMS이관'"),      # 옛 DB 안전망(이미 있으면 건너뜀)
        ("ows_edited_at", "TEXT NOT NULL DEFAULT ''"),
        ("ows_edited_by", "TEXT NOT NULL DEFAULT ''"),
        ("cancel_date", "TEXT NOT NULL DEFAULT ''"),        # 전표 취소일(OWS 취소 표시)
        ("cancel_reason", "TEXT NOT NULL DEFAULT ''"),
        # OWS 취소 직전 상태 — 취소 되돌리기가 원래 상태로 복원한다(TMS 판매H '이전진행상태'와 같은 뜻, 2026-09-03)
        ("prev_stage", "TEXT NOT NULL DEFAULT ''"),
    ],
    # ★거래처 마스터 확장(2026-09-02 대표 방침 "모든 데이터는 OWS·RMS에서 직접 등록·관리").
    #   TMS MST거래처의 업무 거래처(매입·판매·AS업체)를 연동 창구에서 받아 같은 표에 둔다
    #   (app/purchase/masters.py). name UNIQUE 는 그대로 — 전표 supplier_id·이름 조회 코드가
    #   '한 이름 = 한 행'을 전제로 한다. 같은 이름의 다른 TMS 신원은 supplier_tms_links 에 남긴다.
    #   ★거래처코드는 TMS에서 사실상 자유 입력(코드 '업체'를 15곳이 공유)이라 키가 아니다 — 정보 칸.
    "suppliers": [
        ("kind", "TEXT NOT NULL DEFAULT '매입'"),           # 거래처구분: 매입 | 판매 | AS업체
        ("code", "TEXT NOT NULL DEFAULT ''"),               # 거래처코드(정보용)
        ("biz_no", "TEXT NOT NULL DEFAULT ''"),             # 사업자등록번호
        ("ceo", "TEXT NOT NULL DEFAULT ''"),                # 대표
        ("fax", "TEXT NOT NULL DEFAULT ''"),
        ("email", "TEXT NOT NULL DEFAULT ''"),
        ("address", "TEXT NOT NULL DEFAULT ''"),
        ("address_detail", "TEXT NOT NULL DEFAULT ''"),
        ("zip", "TEXT NOT NULL DEFAULT ''"),
        ("dept", "TEXT NOT NULL DEFAULT ''"),               # 부서
        ("norm_name", "TEXT NOT NULL DEFAULT ''"),          # 공백 제거·대문자 — 중복 후보 판정 키
        # 기존 행(사람이 만들었거나 이름으로 생긴 행)은 전부 'ows' — 연동이 빈 칸만 채운다.
        ("source", "TEXT NOT NULL DEFAULT 'ows'"),          # tms | ows
        ("tms_key_id", "INTEGER"),                          # 대표 TMS 신원(키ID)
        ("alias_of", "INTEGER"),                            # 같은 업체의 다른 표기 → 대표 거래처 id
        ("ows_edited_at", "TEXT NOT NULL DEFAULT ''"),
        ("ows_edited_by", "TEXT NOT NULL DEFAULT ''"),
        ("tms_deleted_at", "TEXT NOT NULL DEFAULT ''"),     # TMS 업무 거래처 목록에서 빠짐(표시만)
        ("updated_at", "TEXT NOT NULL DEFAULT ''"),
    ],
    "tms_sales": [
        # TMS 판매상세의 자산별 옵션가 — 연동 창구가 '판매현황' 행에 실어 준다(sales.SALE_ASSET_MAP).
        # OWS 전표 등록 화면도 같은 칸에 적는다. sale_vat·stage·return_date 는 원래 있던 칸.
        ("upgrade1_price", "INTEGER NOT NULL DEFAULT 0"),
        ("upgrade2_price", "INTEGER NOT NULL DEFAULT 0"),
        ("removal_price", "INTEGER NOT NULL DEFAULT 0"),    # 탈거가
        ("extra_price", "INTEGER NOT NULL DEFAULT 0"),      # 기타구성가
        ("charger_price", "INTEGER NOT NULL DEFAULT 0"),
        ("packing_fee", "INTEGER NOT NULL DEFAULT 0"),      # 포장료
        ("sale_fee", "INTEGER NOT NULL DEFAULT 0"),         # 판매수수료
        ("upgrade1_item", "TEXT NOT NULL DEFAULT ''"),
        ("upgrade2_item", "TEXT NOT NULL DEFAULT ''"),
        ("removal_item", "TEXT NOT NULL DEFAULT ''"),
        ("extra_items", "TEXT NOT NULL DEFAULT ''"),        # 기타구성품
        ("cancel_date", "TEXT NOT NULL DEFAULT ''"),        # 상세취소일
        ("source", "TEXT NOT NULL DEFAULT 'tms'"),          # tms | ows
        ("ows_edited_at", "TEXT NOT NULL DEFAULT ''"),
        # 순이익 산식 TMS 대조(2026-09-03): 판매수수율(3%/0%) — 판매수수료 = 판매가×수수율. 매입부가세는 원래 있던 buy_vat.
        ("fee_rate", "REAL NOT NULL DEFAULT 0"),
        # 되돌리기용: 어떤 취소였나('slip'=전표 취소로 함께 / 'line'=라인 단독) + 취소·반입 직전 상태
        ("cancel_kind", "TEXT NOT NULL DEFAULT ''"),
        ("prev_stage", "TEXT NOT NULL DEFAULT ''"),
        # ★판매 시점 원가 스냅샷(2026-09-03 대표 "원가 기준으로 실어올 건데 … TMS 값을 그대로
        #   살려오는 게 낫겠다"). TMS 판매상세가 판매하던 그 순간의 원가를 행에 굳혀 둔 값이다.
        #   ★재고(자산)의 지금 값과 다르다 — 자산 매입가는 나중에 고쳐지고(라이브 151행 이미 다름),
        #     수리·부품비는 팔린 뒤에도 붙는다. 그래서 '지금 값으로 다시 계산'하면 지난달 장부가 바뀐다.
        #     받은 값을 그대로 얼려 두고, OWS 계산은 절대 이 칸을 덮지 않는다.
        #   ★TMS를 끊는 날 이 칸이 대조 기준이 된다 — 없으면 "OWS 숫자가 맞다"를 증명할 길이 없다.
        ("manufacture_cost", "INTEGER NOT NULL DEFAULT 0"),   # 제조원가(= 매입가+수리비+부품비, 실측 9,960/11,084)
        ("tms_repair_cost", "INTEGER NOT NULL DEFAULT 0"),    # 수리비(판매상세 칸 — 지금은 전부 0)
        ("tms_part_cost", "INTEGER NOT NULL DEFAULT 0"),      # 부품비(  〃  )
        ("net_vat", "INTEGER NOT NULL DEFAULT 0"),            # 실부가세 = 판매부가세 − 매입부가세
    ],
    "purchase_batches": [
        ("slip_no", "TEXT NOT NULL DEFAULT ''"),
        ("stage", "TEXT NOT NULL DEFAULT 'purchased'"),
        ("purchase_type", "TEXT NOT NULL DEFAULT '일반매입'"),
        ("channel", "TEXT NOT NULL DEFAULT ''"),
        ("receive_method", "TEXT NOT NULL DEFAULT '택배'"),
        ("requester", "TEXT NOT NULL DEFAULT ''"),
        ("address", "TEXT NOT NULL DEFAULT ''"),
        ("vat", "INTEGER NOT NULL DEFAULT 0"),
        ("fee", "INTEGER NOT NULL DEFAULT 0"),
        ("shipping_fee", "INTEGER NOT NULL DEFAULT 0"),
        ("shipping_cod", "INTEGER NOT NULL DEFAULT 0"),
        ("tracking_no", "TEXT NOT NULL DEFAULT ''"),
        ("paid", "INTEGER NOT NULL DEFAULT 1"),
        ("confirmed_at", "TEXT NOT NULL DEFAULT ''"),
        ("confirmed_by", "TEXT NOT NULL DEFAULT ''"),
        ("cancelled_at", "TEXT NOT NULL DEFAULT ''"),     # 전표 취소(지우지 않고 표시)
        ("cancelled_by", "TEXT NOT NULL DEFAULT ''"),
        ("cancel_reason", "TEXT NOT NULL DEFAULT ''"),
        ("returned_at", "TEXT NOT NULL DEFAULT ''"),      # 거래처 반품일
        ("return_reason", "TEXT NOT NULL DEFAULT ''"),
        ("updated_at", "TEXT NOT NULL DEFAULT ''"),
        ("paid_amount", "INTEGER NOT NULL DEFAULT 0"),   # 실제 지급액(부분지급 가능)
        ("paid_at", "TEXT NOT NULL DEFAULT ''"),         # 지급일
        ("paid_memo", "TEXT NOT NULL DEFAULT ''"),
    ],
    "orders": [
        # ★수령방식 — 택배가 아닌 건이 있다(대표 2026-08-05: "방문수령이나 퀵 발송 건도 있다").
        #   빈 값 = 택배(예전 주문 전부). 택배가 아니면 송장이 필요 없고, 셋팅·배송 화면이
        #   '송장 발급 대기'로 잡으면 안 된다.
        ("receive_method", "TEXT NOT NULL DEFAULT ''"),
        # ★왜 보관했는지 — 보드에서 내린 이유를 남긴다(대표 2026-08-05).
        #   같은 주문이 두 줄이라 내린 것과, 실제로 출고돼서 내린 것은 뜻이 전혀 다르다.
        #   중복이라 내린 건은 **출고완료로 찍지 않는다** — 찍으면 매출이 그대로 두 배가 된다.
        ("archive_reason", "TEXT NOT NULL DEFAULT ''"),
        ("duplicate_of", "INTEGER"),                      # 같은 주문인 다른 줄
        # 쇼핑몰 관점 상태 — 입금대기 구분과 배송완료(구매확정) 확정용
        ("pay_status", "TEXT NOT NULL DEFAULT 'paid'"),   # paid | unpaid(입금대기)
        ("mall_status", "TEXT NOT NULL DEFAULT ''"),      # 몰이 준 원본 상태(참고용)
        # 몰의 상품 식별자(2026-08-14 대표: 상품명 링크가 진짜 상품 페이지로 가게).
        # ★제품코드(우리 코드)와 다른 축이다 — 쿠팡 '노출상품ID', 스마트스토어 '상품번호'처럼
        #   몰이 URL에 쓰는 값. 옵션 단위 식별자는 mall_item_id(쿠팡 vendorItemId)로 따로 둔다
        #   — 제목이 옵션표인 쿠팡은 이 값이 있어야 '그 옵션'으로 바로 열린다.
        ("mall_product_id", "TEXT NOT NULL DEFAULT ''"),
        ("mall_item_id", "TEXT NOT NULL DEFAULT ''"),
        ("delivered_at", "TEXT NOT NULL DEFAULT ''"),     # 배송완료/구매확정 확인 시각
        # 정산 — 판매가가 아니라 '실제로 손에 남는 돈'으로 마진을 봐야 한다
        ("fee_amount", "INTEGER NOT NULL DEFAULT 0"),     # 쇼핑몰·PG 판매수수료
        ("fee_rate", "REAL NOT NULL DEFAULT 0"),          # 적용 요율(%) — 근거를 남긴다
        ("shipping_cost", "INTEGER NOT NULL DEFAULT 0"),  # 출고 택배비(원가)
        ("refund_amount", "INTEGER NOT NULL DEFAULT 0"),  # 환불·부분환불 금액
        ("refund_at", "TEXT NOT NULL DEFAULT ''"),
        ("refund_reason", "TEXT NOT NULL DEFAULT ''"),
        # 사람이 직접 확정한 수수료는 자동 계산·소급 적용이 덮어쓰면 안 된다
        ("fee_manual", "INTEGER NOT NULL DEFAULT 0"),
        # 리뷰어 출고 — 체험단/리뷰용. 제품 없이 빈 박스만 나가는 경우가 있어
        # 셋팅·QC가 일반 주문과 구분해서 다뤄야 한다(2026-07-29 대표 요청)
        ("is_review", "INTEGER NOT NULL DEFAULT 0"),
        ("review_note", "TEXT NOT NULL DEFAULT ''"),
        # 쇼핑몰에 송장번호를 되쏜 결과 — 실패분을 재전송 목록에서 다시 보낼 수 있게 남긴다
        ("mall_sent_at", "TEXT NOT NULL DEFAULT ''"),
        ("mall_send_error", "TEXT NOT NULL DEFAULT ''"),
        # 옵션라벨 인쇄 기록(2026-08-31 대표) — 주문관리 일괄 인쇄가 '안 뽑은 것만' 거르는 기준
        ("opt_label_at", "TEXT NOT NULL DEFAULT ''"),
        ("opt_label_by", "TEXT NOT NULL DEFAULT ''"),
    ],
    # RMS 재고 사본은 8/28에 만들어져 이미 라이브에 있다 — 칸만 뒤늦게 붙인다.
    "rms_inventory": [
        ("renter", "TEXT NOT NULL DEFAULT ''"),
        # ★시리얼 대조를 되살리기 위한 칸(2026-09-03) — OWS는 185라 240의 RMS DB 파일을
        #   못 읽는다. RMS가 사본을 밀 때 시리얼·모델을 같이 보내면 그 대조를 되살릴 수 있다.
        ("serial", "TEXT NOT NULL DEFAULT ''"),
        ("model", "TEXT NOT NULL DEFAULT ''"),
    ],
    "asset_repairs": [
        # 총액을 넣으면 공급가·부가세를 나눠 기록한다(대표 2026-08-04).
        # 세금계산서 대조와 원가 집계에 쓰인다 — 총액만 있으면 매번 손으로 나눠야 한다.
        ("vat", "INTEGER NOT NULL DEFAULT 0"),          # 부가세(총액의 1/11, 반올림)
        ("net", "INTEGER NOT NULL DEFAULT 0"),          # 공급가 = 총액 - 부가세
        ("parts", "TEXT NOT NULL DEFAULT ''"),          # 교체한 부품(SSD·RAM·배터리 …)
        # 옵션 자동 기입(대표 2026-08-10): 고객이 몰에서 '램 추가'를 골랐으면 챙길옵션 체크
        # 시점에 우리 매입 단가가 자동으로 이 표에 들어온다. 어느 주문·어느 옵션에서 왔는지
        # 를 남겨야 ①같은 조합 중복 기입 차단 ②체크 해제/매칭 해제 때 정확히 되돌린다.
        ("order_id", "INTEGER"),                        # 자동 기입의 출처 주문
        ("prep_option_id", "INTEGER"),                  # 자동 기입의 출처 챙길옵션
        ("spec_prev", "TEXT NOT NULL DEFAULT ''"),      # 스펙 갱신 전 값(JSON) — 되돌리기용
    ],
    "parts": [
        # 대제목(그룹) — 단가표를 규격별로 접고 펴기(2026-08-13 대표)
        ("grp", "TEXT NOT NULL DEFAULT ''"),
        # ★부품(물건을 꽂는 것) / 수리(작업) 를 나눈다(2026-08-24 대표).
        #   섞여 있으면 부품 매입·재고와 공임이 뒤엉킨다. 기본은 부품.
        ("kind", "TEXT NOT NULL DEFAULT 'part'"),
    ],
    "code_specs": [
        # 출고 기준 용량(2026-08-14 대표) — 매입 부족분(기준사양 채우기) 계산 축
        ("ram_gb", "INTEGER NOT NULL DEFAULT 0"),
        ("storage_cap", "TEXT NOT NULL DEFAULT ''"),
    ],
    "prep_options": [
        # 챙길옵션 ↔ 부품 연결(대표 2026-08-10): "고객이 옵션으로 추가하면 우리 매입
        # 단가가 자동 기입". 옵션에 부품을 연결해 두면 셋팅에서 칩을 누르는 순간
        # 매칭된 자산에 그날 단가가 원가로 들어간다 — 작업자는 아무것도 더 안 한다.
        ("part_id", "INTEGER"),
        ("part_qty", "INTEGER NOT NULL DEFAULT 1"),
        # 구분별 연결(2026-08-13): "16GB 추가"가 DDR4냐 DDR5냐는 제품코드 스펙이 정한다.
        # JSON [{"gen":"DDR4","partId":3},...] — 있으면 code_specs 세대로 부품을 고르고,
        # 스펙을 모르면 기입을 보류한다(추측으로 틀린 단가를 넣지 않는다).
        ("part_map", "TEXT NOT NULL DEFAULT ''"),
        # 시스템 옵션 종류(2026-08-14 쿠팡): '' = 일반, config_ram/config_storage =
        # '판매 구성' 칩 — 제목이 옵션표인 채널에서 제목 파싱으로 동적으로 붙는다.
        ("kind", "TEXT NOT NULL DEFAULT ''"),
    ],
    "order_assets": [
        ("prev_status", "TEXT NOT NULL DEFAULT 'ready'"),
        # ★대별 준비 체크(2026-08-24 대표) — 2대 이상 주문에서 '이 기계는 셋팅이 끝났나'를
        #   대마다 표시한다. 주문 단계(제작완료 등)와 별개의 세부 체크다.
        ("prepared", "INTEGER NOT NULL DEFAULT 0"),
        ("prepared_by", "TEXT NOT NULL DEFAULT ''"),
        ("prepared_at", "TEXT NOT NULL DEFAULT ''"),
    ],
    "asset_prebuilds": [
        # 출고준비 시점 사양과 주문 사용 시 사양변경 여부.
        ("built_ram_type", "TEXT NOT NULL DEFAULT ''"),
        ("built_ram_primary", "TEXT NOT NULL DEFAULT ''"),
        ("built_ram2_type", "TEXT NOT NULL DEFAULT ''"),
        ("built_ram2", "TEXT NOT NULL DEFAULT ''"),
        ("built_ram", "TEXT NOT NULL DEFAULT ''"),
        ("built_ssd_type", "TEXT NOT NULL DEFAULT ''"),
        ("built_ssd", "TEXT NOT NULL DEFAULT ''"),
        ("built_hdd", "TEXT NOT NULL DEFAULT ''"),
        ("credit_voided", "INTEGER NOT NULL DEFAULT 0"),
        ("credit_void_reason", "TEXT NOT NULL DEFAULT ''"),
        ("spec_change_required", "INTEGER NOT NULL DEFAULT 0"),
        ("spec_change_reason", "TEXT NOT NULL DEFAULT ''"),
        ("cancelled_at", "TEXT NOT NULL DEFAULT ''"),
        ("cancelled_by", "TEXT NOT NULL DEFAULT ''"),
        ("cancel_reason", "TEXT NOT NULL DEFAULT ''"),
    ],
    "order_contact_log": [
        # 배송메모도 수정·복원 대상(2026-08-26 대표)
        ("delivery_message", "TEXT NOT NULL DEFAULT ''"),
    ],
    "as_tickets": [
        # ★주소 표준화(2026-08-31 대표) — CJ 회수/반송 접수가 우편번호 없이 나가고 있었다.
        #   주문과 같은 규격(postal_code + address 합본)으로 맞춘다.
        ("postal_code", "TEXT NOT NULL DEFAULT ''"),
        # 접수 경로(2026-08-31 대표): parcel=택배(회수 예약 필요) / visit=방문(고객이 들고 옴).
        # 기존 건은 전부 택배 흐름으로 만들어졌으므로 기본값 parcel.
        ("intake", "TEXT NOT NULL DEFAULT 'parcel'"),
        # 증상 항목(2026-08-31 대표 — "나중에 A/S 종류 조사"용 분류 태그). JSON 배열.
        # 증상 서술(symptom)과 별개 — 서술은 자유, 항목은 통계·필터용.
        ("symptom_tags", "TEXT NOT NULL DEFAULT ''"),
        # 수기 자산번호/모델명(2026-08-31 대표 — 접수 후에도 적을 칸이 없었다).
        # 우리 자산과 일치하면 asset_id 로 연결되고 이 칸은 비운다 — 연결 못 하는
        # 남의 기계·타사 장비만 여기 남는다(assets 테이블은 절대 건드리지 않는다).
        ("manual_asset_no", "TEXT NOT NULL DEFAULT ''"),
        ("manual_model", "TEXT NOT NULL DEFAULT ''"),
        # 교환 처리(2026-08-31 대표) — 어떤 제품과 교환하는지 제품코드를 적는다.
        # 별도 장부 없이 접수 건과 수리내역서/청구내역서 두 폼에만 실린다(대표 확정).
        ("exchange_product_code", "TEXT NOT NULL DEFAULT ''"),
        # 추가비용(교환 차액 등) — 수리비(수리 내역 합계)와 별개로 청구에 얹힌다.
        ("extra_charge", "INTEGER NOT NULL DEFAULT 0"),
        ("extra_note", "TEXT NOT NULL DEFAULT ''"),
        # 증상 분류(2026-08-31 대표 저녁 — "대분류/소분류/사유"로 개편).
        # 대분류=장비 종류(데스크탑·노트북·태블릿…), 소분류=성격(H/W·S/W·OS·택배파손…),
        # 사유=기존 symptom 서술 그대로. 목록은 KV as_symptom_cats — 지워도 과거 기록은 남는다.
        # (오전의 symptom_tags 태그 방식을 대체 — 재시작 전이라 라이브 데이터 없음)
        ("symptom_cat", "TEXT NOT NULL DEFAULT ''"),
        ("symptom_sub", "TEXT NOT NULL DEFAULT ''"),
        # 돌려줄 방법(2026-09-03 대표 "방문 수령하시는 분들도 계신다").
        # parcel=택배로 보냄(송장 발급) / visit=고객이 찾으러 옴(송장 불필요).
        # 받을 때의 경로(intake)와 별개다 — 택배로 받아 방문 수령하는 경우가 있다.
        ("return_method", "TEXT NOT NULL DEFAULT 'parcel'"),
        # 결제 확인(2026-09-03 대표 "진행완료되어 결제전인지") — 유상 A/S의 입금 도장.
        # 금액은 확인 시점의 청구 합계(수리비+추가비용)를 그대로 굳힌다 — 나중에 내역을
        # 고쳐도 "얼마를 받았는지"는 흔들리면 안 된다. 비우면(빈 문자열) 아직 안 받은 것.
        ("paid_at", "TEXT NOT NULL DEFAULT ''"),
        ("paid_amount", "INTEGER NOT NULL DEFAULT 0"),
        ("paid_method", "TEXT NOT NULL DEFAULT ''"),
        # 회수 품목(2026-09-08 대표 "고객 회수 품목 기재란 — 접수부터 발송까지, 문자에도").
        # 고객에게서 함께 받은 물건을 적는다: "본체, 충전기" / "본체, 충전기, 키스킨, 가방".
        # ★수리 내역(as_ticket_items)과 다른 것이다 — 그쪽은 '무엇을 고치고 얼마를 받는가'이고,
        #   이 칸은 '무엇을 맡았는가'다. 돌려줄 때 이 목록으로 맞춰야 분실 시비가 안 난다.
        ("intake_items", "TEXT NOT NULL DEFAULT ''"),
    ],
    "as_ticket_items": [
        # ★A/S 부품 원가(2026-09-02 대표) — amount 는 '고객에게 청구하는 금액'이고,
        #   이 칸은 '우리가 쓴 원가'다(단가표 가격 × 수량, 저장 시점으로 동결).
        #   둘을 갈라 놔야 A/S 수익(청구 − 원가)을 셀 수 있다. 부품 아닌 줄(공임 등)은 0.
        ("cost", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "part_stock_moves": [
        # ★A/S 소진 연결고리(2026-09-02) — 판매 경로가 repair_id 로 잇는 것과 같은 역할.
        #   A/S 수리내역은 통째로 교체 저장되므로 ticket 단위로 되돌릴 수 있어야 한다.
        ("ticket_id", "INTEGER"),
    ],
    "as_documents": [
        # 발행본 삭제(2026-08-31 대표) — 잘못 발행한 내역서를 지운다. 소프트 삭제:
        # 문서번호 채번이 지운 것까지 세어 같은 번호가 다시 나가지 않는다(번호 재사용 금지).
        ("deleted_at", "TEXT NOT NULL DEFAULT ''"),
        ("deleted_by", "TEXT NOT NULL DEFAULT ''"),
    ],
    "waybills": [
        # 회수 대상 자산과 회수 전 상태 — 주문 없이 접수되는 A/S 회수도 되돌릴 수 있어야 한다
        ("asset_ids", "TEXT NOT NULL DEFAULT ''"),
        ("asset_prev", "TEXT NOT NULL DEFAULT ''"),
        ("as_ticket_id", "INTEGER"),
        # 운영 메모 — 송장번호 팝업에서 적는다(고객 통화 내용·재배송 약속 등, RMS 이식 2026-08-10)
        ("note", "TEXT NOT NULL DEFAULT ''"),
    ],
}


# 새로 추가된 컬럼을 참조하는 인덱스 — 반드시 _migrate() 이후에 만든다.
_POST_MIGRATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_batches_stage ON purchase_batches(stage)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_batches_slip ON purchase_batches(slip_no) WHERE slip_no != ''",
    # 판매재고를 세는 쿼리가 전부 division을 함께 건다 — status와 묶어 둔다.
    "CREATE INDEX IF NOT EXISTS idx_assets_division ON assets(division, status)",
    # 거래처 ↔ TMS 대표 신원은 1:1(2026-09-02 마스터 연동). 같은 키가 두 행에 붙으면 upsert 가 갈린다.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_suppliers_tms_key ON suppliers(tms_key_id) WHERE tms_key_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_suppliers_norm ON suppliers(norm_name, kind)",
]


def _rebuild_purchase_batches(conn):
    """구형 테이블의 supplier_id NOT NULL 제약을 푼다.

    가입고(V전표)는 '택배로 물건만 먼저 받고 거래처는 나중에 채우는' 단계라
    거래처가 비어 있어야 한다. 그런데 초기 테이블이 NOT NULL이라 저장이 통째로 실패했다
    (스키마 파일은 고쳤지만 CREATE TABLE IF NOT EXISTS는 기존 테이블을 바꾸지 않는다).
    SQLite에는 제약 해제가 없어 테이블을 다시 만들어 옮긴다.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='purchase_batches'").fetchone()
    if row is None:
        return
    ddl = (row["sql"] or "").replace(" ", "").replace("\n", "")
    if "supplier_idINTEGERNOTNULL" not in ddl:
        return                      # 이미 정상

    cols = [r["name"] for r in conn.execute("PRAGMA table_info(purchase_batches)").fetchall()]
    # executescript가 암묵 커밋을 하므로 여기서는 트랜잭션을 쓰지 않는다.
    # 대신 실패하면 옛 테이블을 제자리로 돌려놓아 데이터를 잃지 않게 한다.
    conn.execute("PRAGMA foreign_keys=OFF")
    # ★RENAME이 '다른 테이블의 참조까지' 따라 바꾸지 못하게 막는다.
    #   SQLite 3.25부터 ALTER TABLE ... RENAME은 그 테이블을 가리키는 다른 테이블의
    #   REFERENCES 문구도 새 이름으로 자동 수정한다. 그래서 여기서 이름을 바꾸는 순간
    #   assets.batch_id가 purchase_batches_old를 가리키게 되고, 아래에서 그 테이블을
    #   지우면 assets는 '없는 테이블'을 참조한 채 남아 자산 등록이 전부 실패한다
    #   (2026-07-29 운영에서 실제로 발생 — '서버 내부 오류'로만 보였다).
    conn.execute("PRAGMA legacy_alter_table=ON")
    renamed = False
    try:
        conn.execute("ALTER TABLE purchase_batches RENAME TO purchase_batches_old")
        renamed = True
        conn.executescript(_SCHEMA.read_text("utf-8"))          # 올바른 정의로 재생성
        new_cols = {r["name"] for r in conn.execute("PRAGMA table_info(purchase_batches)").fetchall()}
        keep = [c for c in cols if c in new_cols]
        if keep:
            conn.execute(
                f"INSERT INTO purchase_batches({','.join(keep)}) "
                f"SELECT {','.join(keep)} FROM purchase_batches_old")
        conn.execute("DROP TABLE purchase_batches_old")
    except Exception:
        if renamed:
            try:
                conn.execute("DROP TABLE IF EXISTS purchase_batches")
                conn.execute("ALTER TABLE purchase_batches_old RENAME TO purchase_batches")
            except Exception:                                    # noqa: BLE001
                pass
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate(conn):
    _rebuild_purchase_batches(conn)
    for table, columns in _ADDED_COLUMNS.items():
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, ddl in columns:
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    for sql in _POST_MIGRATE_INDEXES:
        conn.execute(sql)
    # 주문 취소 전에 선제작 사용 원복 로직이 없던 기간의 데이터 자동 정리.
    # 취소 주문에 묶인 선제작 자산은 출고 준비완료 상태로 다시 사용 가능해야 한다.
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='asset_prebuilds'").fetchone():
        # 취소 내역을 별도 보관하지 않는다. 같은 자산을 즉시 다시 선제작할 수 있어야 한다.
        conn.execute("DELETE FROM asset_prebuilds WHERE cancelled_at!=''")
        conn.execute(
            "UPDATE asset_prebuilds SET used_order_id=NULL, used_by='', used_at='', "
            "spec_change_required=0, spec_change_reason='', updated_at=? "
            "WHERE used_order_id IN (SELECT id FROM orders WHERE cancelled_at!='')",
            (config.now_iso(),))
    # 재고 구분 이름 확정(2026-08-08 대표): '양품' → '가용'. 멱등이라 매 기동마다 돌려도 안전.
    conn.execute("UPDATE assets SET tier='가용' WHERE tier='양품'")
    # 거래처 정규화 키 백필(2026-09-02 마스터 연동) — 비어 있는 행만, 멱등.
    #   파이썬 쪽 규칙(masters.norm_key: 공백 제거·대문자)과 같은 결과가 나오게 공백류만 지운다.
    conn.execute(
        "UPDATE suppliers SET norm_name = UPPER(REPLACE(REPLACE(REPLACE(name, ' ', ''), char(9), ''), char(10), '')) "
        "WHERE norm_name = '' AND name != ''")
    # 모델 분류 기본값(2026-09-02) — 연동 창구가 없는 환경(검증 서버·새 DB)에서도 모델 등록 화면의
    # 선택지가 비지 않게 TMS 분류 8종을 최초 1회 심는다. 연동이 오면 같은 (대분류, 중분류)에 키ID 가 붙는다.
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='model_categories'").fetchone():
        if not conn.execute("SELECT 1 FROM settings WHERE key='model_categories_seed_v1'").fetchone():
            ts = config.now_iso()
            for i, (cat, sub) in enumerate((("PC", "노트북"), ("PC", "데스크탑"), ("PC", "모니터"),
                                            ("PC", "일체형PC"), ("PC", "미니PC"), ("PC", "주변기기"),
                                            ("태블릿", "태블릿"), ("웨어러블", "버즈"))):
                if not conn.execute("SELECT 1 FROM model_categories WHERE category=? AND subcategory=?",
                                    (cat, sub)).fetchone():
                    conn.execute(
                        "INSERT INTO model_categories(category, subcategory, sort, enabled, source, created_at, updated_at) "
                        "VALUES(?,?,?,1,'ows',?,?)", (cat, sub, i, ts, ts))
            conn.execute(
                "INSERT INTO settings(key, value, updated_at, updated_by) "
                "VALUES('model_categories_seed_v1','1',?,?)", (ts, "기본값"))
    # 재고 카테고리 정합(2026-09-03, A5) — 자산 카테고리를 TMS 중분류 8종 축으로 맞춘다. 최초 1회(settings 마커).
    #   'PC'→'데스크탑', '올인원'→'일체형PC' 개명은 새 이름이 아직 없을 때만(설정 ▸ 카테고리에서 다시 바꿀 수 있다 — 되돌림 가능,
    #   마커가 남으므로 재기동해도 다시 안 바뀐다). 그다음 config.DEFAULT_CATEGORIES 중 없는 이름만 맨 뒤에 INSERT.
    #   기존 정렬(노트북 sort 0 = 기본값)·B급은 안 건드린다. 빈 표(새 DB)는 아래 init_db 의 기본 시드가 맡는다.
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='categories'").fetchone() \
            and conn.execute("SELECT COUNT(*) AS c FROM categories").fetchone()["c"] \
            and not conn.execute("SELECT 1 FROM settings WHERE key='categories_seed_v2'").fetchone():
        ts = config.now_iso()
        for old, new in (("PC", "데스크탑"), ("올인원", "일체형PC")):
            if not conn.execute("SELECT 1 FROM categories WHERE name=?", (new,)).fetchone():
                conn.execute("UPDATE categories SET name=? WHERE name=?", (new, old))
        max_sort = conn.execute("SELECT COALESCE(MAX(sort), -1) AS m FROM categories").fetchone()["m"]
        for name in config.DEFAULT_CATEGORIES:
            if not conn.execute("SELECT 1 FROM categories WHERE name=?", (name,)).fetchone():
                max_sort += 1
                conn.execute("INSERT INTO categories(name, sort, enabled, created_at) VALUES(?,?,1,?)",
                             (name, max_sort, ts))
        conn.execute(
            "INSERT INTO settings(key, value, updated_at, updated_by) "
            "VALUES('categories_seed_v2','1',?,?)", (ts, "기본값"))
    # 시트지 단가 시드(2026-08-13 대표: "시트지(상) 11,000 / 시트지(상,중) 16,500").
    # ★최초 1회만 — 대표가 지우거나 이름을 고친 뒤 재기동해도 되살아나면 안 되므로
    #   settings 마커로 막는다. 단가 자체는 화면(기준정보 ▸ 부품 단가표)에서 계속 고친다.
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='parts'").fetchone():
        if not conn.execute("SELECT 1 FROM settings WHERE key='parts_seed_v1'").fetchone():
            ts = config.now_iso()
            for name, price in (("시트지(상)", 11000), ("시트지(상,중)", 16500)):
                if not conn.execute("SELECT id FROM parts WHERE name=?", (name,)).fetchone():
                    conn.execute(
                        "INSERT INTO parts(name, category, price, created_at, created_by, "
                        "updated_at, updated_by) VALUES(?,?,?,?,?,?,?)",
                        (name, "", price, ts, "기본값", ts, "기본값"))
            conn.execute(
                "INSERT INTO settings(key, value, updated_at, updated_by) "
                "VALUES('parts_seed_v1','1',?,?)", (ts, "기본값"))
        # 저장장치 규격 시드(2026-08-13 대표: "2.5 HDD·2.5 SSD·M.2 SATA·M.2 NVMe 네 가지로
        # 용량 나눠서" — SSD류는 128G~2TB, HDD는 500G/1TB만, 필요하면 화면에서 추가).
        # ★단가는 0으로 심는다 — 0원이면 자동 기입이 멈추고 '단가표부터 채우라'고 알리므로
        #   값을 넣기 전에 엉뚱한 원가가 들어갈 일이 없다. 역시 최초 1회만(v2 마커).
        if not conn.execute("SELECT 1 FROM settings WHERE key='parts_seed_v2'").fetchone():
            ts = config.now_iso()
            caps = ("128G", "256G", "512G", "1TB", "2TB")
            names = ([f"M.2 NVMe {c}" for c in caps] + [f"M.2 SATA {c}" for c in caps]
                     + [f"2.5 SSD {c}" for c in caps] + ["2.5 HDD 500G", "2.5 HDD 1TB"])
            for name in names:
                if not conn.execute("SELECT id FROM parts WHERE name=?", (name,)).fetchone():
                    conn.execute(
                        "INSERT INTO parts(name, category, price, created_at, created_by, "
                        "updated_at, updated_by) VALUES(?,?,?,?,?,?,?)",
                        (name, "ssd", 0, ts, "기본값", ts, "기본값"))
            conn.execute(
                "INSERT INTO settings(key, value, updated_at, updated_by) "
                "VALUES('parts_seed_v2','1',?,?)", (ts, "기본값"))
        # 콤보 단가 시드 v3(2026-08-13 대표, 콤보관리_260813.xlsx) — TMS 콤보표의
        # 'X→용량'(없음→용량) 금액이 곧 모듈 단가다(전환 행 4G→8G 등은 전부 그 차액이라
        # 장착/회수 엔진이 자동 산출 — 검산: 4G→8G 24,000 = 44,000−20,000 ✓).
        # ★이미 단가가 들어 있는 줄은 절대 안 덮는다(0원인 줄만 채움). M.2 SATA는 콤보에
        #   'M/N' 한 줄뿐이라 NVMe와 같은 값으로 시작(대표가 화면에서 조정).
        # ★CPU·그래픽·파워·보드(데스크탑용)는 대표 지시로 후순위 — 여기 안 넣는다.
        if not conn.execute("SELECT 1 FROM settings WHERE key='parts_seed_v3'").fetchone():
            ts = config.now_iso()
            combo = {
                # RAM — DDR3/4/5 모듈 단가(콤보 X→N)
                "D3 4G": ("ram", "RAM", 10000), "D3 8G": ("ram", "RAM", 20000),
                "D3 16G": ("ram", "RAM", 40000),
                "D4 4G": ("ram", "RAM", 20000), "D4 8G": ("ram", "RAM", 44000),
                "D4 16G": ("ram", "RAM", 110000), "D4 32G": ("ram", "RAM", 220000),
                "D5 4G": ("ram", "RAM", 70000), "D5 8G": ("ram", "RAM", 100000),
                "D5 16G": ("ram", "RAM", 200000), "D5 32G": ("ram", "RAM", 400000),
                "D5 64G": ("ram", "RAM", 800000),
                # 저장장치 — 콤보 M/N(=M.2)·2.5·HDD
                "M.2 NVMe 128G": ("ssd", "M.2 NVMe", 20000),
                "M.2 NVMe 256G": ("ssd", "M.2 NVMe", 44000),
                "M.2 NVMe 512G": ("ssd", "M.2 NVMe", 88000),
                "M.2 NVMe 1TB": ("ssd", "M.2 NVMe", 160000),
                "M.2 NVMe 2TB": ("ssd", "M.2 NVMe", 320000),
                "M.2 SATA 128G": ("ssd", "M.2 SATA", 20000),
                "M.2 SATA 256G": ("ssd", "M.2 SATA", 44000),
                "M.2 SATA 512G": ("ssd", "M.2 SATA", 88000),
                "M.2 SATA 1TB": ("ssd", "M.2 SATA", 160000),
                "M.2 SATA 2TB": ("ssd", "M.2 SATA", 320000),
                "2.5 SSD 128G": ("ssd", "2.5 SSD", 15000),
                "2.5 SSD 256G": ("ssd", "2.5 SSD", 30000),
                "2.5 SSD 512G": ("ssd", "2.5 SSD", 60000),
                "2.5 SSD 1TB": ("ssd", "2.5 SSD", 90000),
                "2.5 HDD 500G": ("ssd", "2.5 HDD", 6000),
                "2.5 HDD 1TB": ("ssd", "2.5 HDD", 15000),
            }
            for name, (cat, grp, price) in combo.items():
                row = conn.execute("SELECT id, price FROM parts WHERE name=?", (name,)).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO parts(name, category, grp, price, created_at, created_by, "
                        "updated_at, updated_by) VALUES(?,?,?,?,?,?,?,?)",
                        (name, cat, grp, price, ts, "콤보표", ts, "콤보표"))
                elif not row["price"]:
                    conn.execute("UPDATE parts SET price=?, updated_at=?, updated_by=? WHERE id=?",
                                 (price, ts, "콤보표", row["id"]))
            conn.execute(
                "INSERT INTO settings(key, value, updated_at, updated_by) "
                "VALUES('parts_seed_v3','1',?,?)", (ts, "콤보표"))
        # 데스크탑 부품 시드 v4(2026-08-13 대표 "진행해 줘" — 콤보표의 CPU·그래픽·파워·보드).
        # ★자동 기입 대상이 아니다: CPU·그래픽은 소켓·보드·파워 호환을 사람이 판단해야
        #   해서, 자산 상세·전표 [부품 일괄]의 '수동' 장착/회수 전용이다.
        if not conn.execute("SELECT 1 FROM settings WHERE key='parts_seed_v4'").fetchone():
            ts = config.now_iso()
            desktop = {
                "CPU I5-2": 7700, "CPU I5-3": 15000, "CPU I7-3": 42900, "CPU I5-4": 21000,
                "CPU I5-6": 31000, "CPU I7-6": 63000, "CPU I5-7": 45100, "CPU I3-7": 25000,
                "CPU I5-8": 77000, "CPU I3-8": 22000, "CPU I7-8": 125000, "CPU I5-9": 105000,
                "CPU I7-9": 140000, "CPU I7-10": 270000,
                "그래픽 GT710": 13200, "그래픽 GTX1050": 55000, "그래픽 GTX1060": 93500,
                "그래픽 GTX1650": 110000, "그래픽 RTX2060": 199000, "그래픽 RTX2070": 198000,
                "그래픽 RTX2080": 165000, "그래픽 RTX3060TI": 250000, "그래픽 RTX3080": 374000,
                "파워 600W": 5500, "파워 800W": 20000, "파워 1000W": 30000,
                "보드 B460M-DS3H": 77000,
            }
            for name, price in desktop.items():
                grp = name.split(" ", 1)[0]            # CPU / 그래픽 / 파워 / 보드
                row = conn.execute("SELECT id, price FROM parts WHERE name=?", (name,)).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO parts(name, category, grp, price, created_at, created_by, "
                        "updated_at, updated_by) VALUES(?,?,?,?,?,?,?,?)",
                        (name, "", grp, price, ts, "콤보표", ts, "콤보표"))
                elif not row["price"]:
                    conn.execute("UPDATE parts SET price=?, updated_at=?, updated_by=? WHERE id=?",
                                 (price, ts, "콤보표", row["id"]))
            conn.execute(
                "INSERT INTO settings(key, value, updated_at, updated_by) "
                "VALUES('parts_seed_v4','1',?,?)", (ts, "콤보표"))
        # 수리 단가표 분리 v5(2026-08-24 대표: "시트지는 수리단가표로 이동하여 있어야 함").
        # ★부품은 '물건을 꽂는' 것, 수리는 '작업'이다. 단가는 0으로 두고 대표가 채운다 —
        #   짐작으로 넣으면 그 값이 그대로 자산 원가에 실린다.
        if not conn.execute("SELECT 1 FROM settings WHERE key='parts_seed_v5'").fetchone():
            ts = config.now_iso()
            conn.execute("UPDATE parts SET kind='repair' WHERE grp='시트지' OR name LIKE '시트지%'")
            for name in ("도색", "짜깁기", "액정 교체", "배터리 교체", "키보드 교체",
                         "힌지 교체", "간단보수"):
                if conn.execute("SELECT 1 FROM parts WHERE name=?", (name,)).fetchone():
                    continue
                conn.execute(
                    "INSERT INTO parts(name, category, grp, kind, price, created_at, "
                    "created_by, updated_at, updated_by) VALUES(?,?,?,'repair',0,?,?,?,?)",
                    (name, "", "수리", ts, "수리단가표", ts, "수리단가표"))
            conn.execute(
                "INSERT INTO settings(key, value, updated_at, updated_by) "
                "VALUES('parts_seed_v5','1',?,?)", (ts, "수리단가표"))
        # 단가표 3분할 v6(2026-08-25 대표): 도색·시트지를 수리에서 떼어 paint 로.
        #   부품(CPU·RAM·SSD·HDD…) / 수리(액정·케이스·키보드·배터리…) / 도색·시트지.
        if not conn.execute("SELECT 1 FROM settings WHERE key='parts_seed_v6'").fetchone():
            ts = config.now_iso()
            conn.execute("UPDATE parts SET kind='paint', grp='도색/시트지' "
                         "WHERE grp='시트지' OR name LIKE '시트지%' OR name='도색'")
            for name in ("케이스 교체",):
                if not conn.execute("SELECT 1 FROM parts WHERE name=?", (name,)).fetchone():
                    conn.execute(
                        "INSERT INTO parts(name, category, grp, kind, price, created_at, "
                        "created_by, updated_at, updated_by) VALUES(?,?,?,'repair',0,?,?,?,?)",
                        (name, "", "수리", ts, "수리단가표", ts, "수리단가표"))
            conn.execute(
                "INSERT INTO settings(key, value, updated_at, updated_by) "
                "VALUES('parts_seed_v6','1',?,?)", (ts, "단가표3분할"))
        # 대제목(그룹) 백필(2026-08-13 대표 "대제목으로 접고 펴서 규격별로") — 이름 규약으로
        # (아래 계속)
        pass
    # 업데이트 내역(2026-08-14 대표) — 개발 시작부터의 기록 + 작업한 날의 요약.
    # ★'없는 날짜만' 채운다(대표 선택 2026-08-17, 1번 방식): 클로드가 작업을 마칠 때
    #   changelog_seed.py 에 그날 항목을 적어 두면, 다음 서버 재시작에 그대로 올라온다.
    #   이미 있는 날짜는 건드리지 않으므로 대표가 화면에서 고친 문구가 덮이지 않는다.
    #   (단 화면에서 '통째로 지운' 날짜는 재시작 때 되살아난다 — 지우려면 파일에서도 뺄 것)
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='changelog'").fetchone():
        from .changelog_seed import SEED
        ts = config.now_iso()
        for day, items in SEED:
            have = [r["text"] for r in conn.execute(
                "SELECT text FROM changelog WHERE day=? ORDER BY seq", (day,)).fetchall()]
            if not have:
                for i, text in enumerate(items, 1):
                    conn.execute(
                        "INSERT INTO changelog(day, seq, text, created_at, created_by) "
                        "VALUES(?,?,?,?,?)", (day, i, text, ts, "시스템(작업이력)"))
                continue
            # ★같은 날 여러 번 재기동하는 날의 구멍(2026-08-24 실제 발생) — 첫 재기동이
            #   그 시점의 몇 줄로 날짜를 만들면, 그날 나중에 적은 항목이 영영 안 올라갔다.
            #   기존 줄이 시드의 '앞부분과 정확히 같을 때만'(= 화면에서 아무도 안 고쳤을 때만)
            #   나머지를 이어 붙인다. 한 글자라도 다르면 사람이 고친 것 — 건드리지 않는다.
            if len(items) > len(have) and have == list(items[:len(have)]):
                for i, text in enumerate(items[len(have):], len(have) + 1):
                    conn.execute(
                        "INSERT INTO changelog(day, seq, text, created_at, created_by) "
                        "VALUES(?,?,?,?,?)", (day, i, text, ts, "시스템(작업이력)"))
    # 판매 구성 칩 시스템 옵션(2026-08-14 대표 "쿠팡 진행") — 쿠팡은 옵션 없이 제목이
    # 옵션표라, 제목 파싱으로 동적으로 붙는 시스템 옵션 두 개를 시드한다.
    # 대표가 껐다 켤 수는 있고(enabled), 삭제는 라우트가 막는다(기능이 통째로 죽는다).
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='prep_options'").fetchone():
        if not conn.execute("SELECT 1 FROM settings WHERE key='prep_config_seed_v1'").fetchone():
            ts = config.now_iso()
            for name, kind in (("판매 구성(램)", "config_ram"),
                               ("판매 구성(저장장치)", "config_storage")):
                if not conn.execute("SELECT id FROM prep_options WHERE kind=?",
                                    (kind,)).fetchone():
                    conn.execute(
                        "INSERT INTO prep_options(name, note, enabled, sort, kind, "
                        "created_by, created_at) VALUES(?,?,1,900,?,?,?)",
                        (name, "제목이 옵션표인 채널(쿠팡)에서 판매 구성이 기준 사양과 "
                               "다를 때 자동으로 붙습니다. 체크하면 실물 기준 회수/장착 "
                               "원가가 기입됩니다.", kind, "기본값", ts))
            conn.execute(
                "INSERT INTO settings(key, value, updated_at, updated_by) "
                "VALUES('prep_config_seed_v1','1',?,?)", (ts, "기본값"))
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='parts'").fetchone():
        # 대제목(그룹) 백필(계속) — 이름 규약으로
        # grp가 '비어 있는' 행만 채운다. 사람이 화면에서 바꾼 그룹은 절대 안 건드린다.
        # 멱등이라 매 기동마다 돌아도 안전하고, 새로 시드된 규격도 같은 기동에서 분류된다.
        for prefix, grp in (("M.2 NVMe %", "M.2 NVMe"), ("M.2 SATA %", "M.2 SATA"),
                            ("2.5 SSD %", "2.5 SSD"), ("2.5 HDD %", "2.5 HDD"),
                            ("시트지%", "시트지")):
            conn.execute("UPDATE parts SET grp=? WHERE grp='' AND name LIKE ?", (grp, prefix))
        conn.execute("UPDATE parts SET grp='RAM' WHERE grp='' AND category='ram'")


def init_db(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA.read_text("utf-8"))
        _migrate(conn)
        row = conn.execute("SELECT COUNT(*) AS c FROM categories").fetchone()
        if row["c"] == 0:
            conn.execute("BEGIN IMMEDIATE")
            for i, name in enumerate(config.DEFAULT_CATEGORIES):
                conn.execute(
                    "INSERT INTO categories(name, sort, enabled, created_at) VALUES(?,?,1,?)",
                    (name, i, config.now_iso()),
                )
            conn.execute("COMMIT")
    finally:
        conn.close()


def get_db():
    if "db" not in g:
        g.db = _connect(current_app.config["DB_PATH"])
    return g.db


# ────────────────────────────────────────── 사업부 귀속(2026-08-12 대표 규칙 확정)
#
# 렌탈 사업부(RMS) 귀속 자산은 '팔 수 있는 물건'이 아니다. 판매재고를 세는 모든 곳에서
# 빠져야 한다 — 재고현황·몰재고·셋팅 브리핑·주문매칭·판매리포트.
#
# ★조건을 여기 한 곳에 두는 이유: 이게 필요한 쿼리가 매입·주문·몰·리포트에 흩어져 있는데
#   각자 문자열을 적으면 한 군데만 빠져도 렌탈 물건이 몰에 올라간다.
DIVISION_SALE = "sale"
DIVISION_RENTAL = "rental"
DIVISIONS = (DIVISION_SALE, DIVISION_RENTAL)


def sale_only(alias="a"):
    """판매 사업부 자산만. alias가 빈 문자열이면 별칭 없는 쿼리용으로 낸다."""
    col = f"{alias}.division" if alias else "division"
    return f" AND {col} = '{DIVISION_SALE}'"


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@contextmanager
def tx(write=False):
    """트랜잭션 경계. 쓰기는 BEGIN IMMEDIATE로 잠금을 선점한다."""
    conn = get_db()
    if write:
        ensure_daily_backup(current_app.config["DB_PATH"])
    conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- backups

def backup_dir(db_path) -> Path:
    return Path(db_path).parent / "backups"


def make_backup(db_path, target: Path):
    """sqlite 온라인 백업 API로 일관된 스냅샷 생성(tmp 후 원자적 교체).

    tmp 이름에 스레드 ID를 붙여 동시 호출이 같은 tmp를 잡는 충돌을 차단한다.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f"{target.name}.tmp-{threading.get_ident()}"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    tmp.replace(target)


def ensure_daily_backup(db_path):
    """주기 백업 — 마지막 백업이 BACKUP_INTERVAL_HOURS보다 오래됐으면 새로 만든다.

    ★하루 1회로 두면 그날 아침 백업 뒤에 벌어진 일(이관 626건 같은 큰 변화)이
      통째로 백업에 없는 상태로 하루를 보낸다(2026-07-28 실제 발생). 몇 시간 주기로 남긴다.
    """
    bdir = backup_dir(db_path)
    now = config.now().timestamp()
    stamp = config.now().strftime("%Y%m%d-%H%M%S")
    target = bdir / f"ows-{stamp}.db"
    with _backup_lock:
        if _recent_backup_exists(bdir, now):
            return
        make_backup(db_path, target)
        _cleanup_old_backups(bdir)
    mirror_backup(target, db_path)   # 2차 사본은 락 밖에서(느린 외장/NAS가 쓰기를 막지 않게)


def _recent_backup_exists(bdir, now_ts) -> bool:
    if not bdir.exists():
        return False
    cutoff = now_ts - config.BACKUP_INTERVAL_HOURS * 3600
    for f in bdir.glob("ows-*.db"):
        try:
            if f.stat().st_mtime >= cutoff:
                return True
        except OSError:
            continue
    return False


def mirror_dir():
    """2차 백업 위치 — 설정하지 않으면 None(그때는 1차만 남는다)."""
    raw = (config.BACKUP_MIRROR or "").strip()
    return Path(raw) if raw else None


def _is_live_db(db_path) -> bool:
    """미러는 '운영 DB'의 사본일 때만 의미가 있다.

    테스트·검증 서버는 임시 DB로 돌기 때문에, 이걸 안 막으면 테스트를 한 번 돌릴 때마다
    2차 백업 폴더에 쓸모없는 사본이 수천 개 쌓인다(2026-07-29 실제 발생).

    ★config.DB_PATH와 비교하면 안 된다. 그 값은 OWS_DB 환경변수에서 오기 때문에,
      라이브를 복사해 OWS_DB=<사본>으로 검증 서버를 띄우면 '자기 자신과 같다'가 되어
      항상 참이 된다. 실제로 그래서 테스트 계정이 섞인 사본이 D: 미러에 올라갔다
      (2026-07-29 ows-20260729-103002.db, 격리함). 환경변수를 타지 않는 고정 경로로 본다.
    """
    try:
        return Path(db_path).resolve() == config.LIVE_DB_PATH.resolve()
    except OSError:
        return False


def mirror_backup(src: Path, db_path=None):
    """다른 디스크/NAS에 사본 하나 더. 실패해도 본 백업은 유지된다.

    같은 디스크에만 두면 디스크 고장·랜섬웨어에 원본과 함께 사라진다.
    """
    dst_dir = mirror_dir()
    if dst_dir is None or not src.exists():
        return None
    if db_path is not None and not _is_live_db(db_path):
        return None
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        tmp = dst_dir / f"{src.name}.tmp"
        shutil.copy2(src, tmp)
        tmp.replace(dst_dir / src.name)
        _cleanup_old_backups(dst_dir)
        return dst_dir / src.name
    except OSError:
        return None


def run_manual_backup(db_path) -> Path:
    """수동 백업 — 주기 백업과 같은 락으로 직렬화(동시 실행 충돌 방지)."""
    ts = config.now().strftime("%Y%m%d-%H%M%S")
    with _backup_lock:
        target = backup_dir(db_path) / f"ows-{ts}.db"
        make_backup(db_path, target)
    mirror_backup(target, db_path)
    return target


def _cleanup_old_backups(bdir: Path):
    cutoff = config.now().timestamp() - config.BACKUP_RETENTION_DAYS * 86400
    for f in bdir.glob("ows-*.db"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def list_backups(db_path):
    bdir = backup_dir(db_path)
    out = []
    if bdir.exists():
        for f in sorted(bdir.glob("*.db"), reverse=True):
            st = f.stat()
            out.append({
                "name": f.name,
                "sizeBytes": st.st_size,
                "modifiedAt": datetime.fromtimestamp(st.st_mtime, config.KST).isoformat(timespec="seconds"),
            })
    return out
