"""엑셀 주문 임포터 패키지 (업무관리 주문 워크플로 이식본, 순수 stdlib).

- excel: 자체 XLSX 파서 / XLSX 생성 (.xls는 LibreOffice 변환 의존)
- mall_excel: 채널 자동감지 임포터 (import_workbook)
- dedupe: 다층 중복키·병합·pendingShippingUpdate

Flask/DB 의존 없음 — 모든 함수는 camelCase 키의 주문 dict를 입출력한다.
"""
from .dedupe import (
    DUPLICATE_DETAIL_FIELDS,
    SHIPPING_UPDATE_FIELDS,
    cleanup_duplicate_orders,
    coupang_cross_import_key,
    duplicate_cleanup_keys,
    duplicate_keep_sort_key,
    exact_order_content_key,
    is_synthetic_order_number,
    merge_duplicate_cleanup_details,
    merge_duplicate_order_details,
    new_unique_orders,
    normalized_address_value,
    normalized_coupang_product_identity,
    normalized_order_minute,
    normalized_order_number,
    normalized_order_value,
    normalized_phone_value,
    order_dedupe_key,
    order_duplicate_keys,
    order_fingerprint,
    shipping_update_candidate,
)
from .excel import find_libreoffice_command, read_first_sheet, write_xlsx
from .mall_excel import import_workbook

__all__ = [
    "DUPLICATE_DETAIL_FIELDS",
    "SHIPPING_UPDATE_FIELDS",
    "cleanup_duplicate_orders",
    "coupang_cross_import_key",
    "duplicate_cleanup_keys",
    "duplicate_keep_sort_key",
    "exact_order_content_key",
    "find_libreoffice_command",
    "import_workbook",
    "is_synthetic_order_number",
    "merge_duplicate_cleanup_details",
    "merge_duplicate_order_details",
    "new_unique_orders",
    "normalized_address_value",
    "normalized_coupang_product_identity",
    "normalized_order_minute",
    "normalized_order_number",
    "normalized_order_value",
    "normalized_phone_value",
    "order_dedupe_key",
    "order_duplicate_keys",
    "order_fingerprint",
    "read_first_sheet",
    "shipping_update_candidate",
    "write_xlsx",
]
