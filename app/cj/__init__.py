"""CJ대한통운 택배 연동 패키지 (표준 API V3.9.4) — rental-system에서 추출 이식.

client      : API 클라이언트 (토큰/채번/주소정제/추적/접수·취소) — Flask 비의존, cj 설정 dict 인자
waybill_pdf : 표준운송장(123×100mm) PDF 렌더러 — ★레이아웃 동결(FROZEN), 수정 금지
label       : 라벨 데이터 스키마 생산자 + 샘플 라벨

⚠ CJ API 실호출 주의 — cj2_token/cj2_call/cj2_new_invoice/cj2_reg_book/cj2_track 은 네트워크 요청.
  env='prod' 는 실배송 접수. 검증은 격리 프로브(모킹)로만.
"""
from .client import (
    CJ2_HOSTS,
    _cj2_token_cache,
    _cj2_base,
    _cj2_cust,
    _cj2_party,
    _cj2_phone,
    cj2_addr_refine,
    cj2_call,
    cj2_mss_track,
    cj2_new_invoice,
    cj2_reg_book,
    cj2_token,
    cj2_track,
)
from .label import _cj2_label, _cj2_sample_label, join_items
from .waybill_pdf import (
    _cj_fmt_invc,
    _cj_font_bold,
    _cj_label_offset,
    _cj_mask_name,
    _cj_mask_phone,
    _cj_wrap,
    _register_font_once,
    cj2_waybill_pdf,
)

__all__ = [
    'CJ2_HOSTS', '_cj2_token_cache', '_cj2_base', '_cj2_cust', '_cj2_party', '_cj2_phone',
    'cj2_addr_refine', 'cj2_call', 'cj2_new_invoice', 'cj2_reg_book', 'cj2_token', 'cj2_track',
    '_cj2_label', '_cj2_sample_label', 'join_items',
    '_cj_fmt_invc', '_cj_font_bold', '_cj_label_offset', '_cj_mask_name', '_cj_mask_phone',
    '_cj_wrap', '_register_font_once', 'cj2_waybill_pdf',
]
