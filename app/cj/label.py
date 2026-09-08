"""CJ 운송장 라벨 데이터 — rental-system(app.py)에서 추출 이식.

_cj2_label 이 라벨 dict 스키마의 정본(cj2_waybill_pdf 입력 계약).
렌탈 도메인 의존부는 파라미터화 — 수취인(sender/receiver)·품목(items)을 dict/list 인자로 받는다.
(_cj2_sample_label 은 원본이 설정에서 _cj2_sender()를 읽던 부분을 sender 인자로 대체.)
"""
import re
from datetime import datetime


def join_items(items: list, line_break: bool = True) -> str:
    """품목 조각을 상품명 칸 한 덩이로 — 한 곳에서만 만든다.

    ★항목에 br=True 가 있으면 그 앞에서 줄을 바꾼다(대표 2026-08-24: 자산번호를 그 줄에만).
      렌더러의 wrap_mm 이 줄바꿈 문자를 살린다.
    ★line_break=False 면 2026-08-24 이전과 똑같이 ' / '로만 잇는다 — 설정에서 되돌리는 길.
    ★견본 운송장도 이 함수를 쓴다. 두 곳에서 따로 만들면 종이와 화면이 어긋난다.
    """
    parts, out = [], ''
    for it in (items or []):
        if not it.get('name'):
            continue
        one = f"{it.get('name')}{(' x' + str(it.get('qty')) if it.get('qty') else '')}".strip()
        if line_break and it.get('br') and out:
            parts.append(out)
            out = one
        else:
            out = f"{out} / {one}" if out else one
    if out:
        parts.append(out)
    return (chr(10) if line_break else ' / ').join(parts)[:120]


def _cj2_label(invc: str, rcpt_ymd: str, sender: dict, receiver: dict, items: list,
               refine: dict, frt_dv: str = '03', kind: str = 'ship', remark: str = '',
               default_item: str = '렌탈 장비', line_break: bool = True,
               item_lines: int = 4) -> dict:
    """운송장 자체출력용 라벨 데이터(1.5인치 표준양식 항목). 주소정제 응답 + 발/착/상품 조합."""
    refine = refine or {}
    clsf = (refine.get('CLSFCD') or '').strip()
    sub  = (refine.get('SUBCLSFCD') or '').strip()
    brannm = (refine.get('CLLDLVBRANNM') or '').strip()
    nick   = (refine.get('CLLDLVEMPNICKNM') or '').strip()
    item_summary = join_items(items, line_break)
    return {
        'invoice_no': invc, 'rcpt_ymd': re.sub(r'[^0-9]', '', rcpt_ymd or '') or datetime.now().strftime('%Y%m%d'),
        'kind': kind,
        'clsfcd': clsf, 'subclsfcd': sub, 'clsf_full': clsf + (('-' + sub) if sub else ''),
        'clsfaddr': (refine.get('CLSFADDR') or '').strip(),
        'bran': brannm + (('-' + nick) if nick else ''),
        'p2pcd': (refine.get('P2PCD') or '') or '',
        'sender': sender, 'receiver': receiver, 'item_summary': item_summary or default_item,
        'frt_dv': frt_dv, 'frt_dv_nm': {'01': '선불', '02': '착불', '03': '신용'}.get(frt_dv, ''),
        # ★상품명 칸 줄수 — 설정에서 되돌릴 수 있게 데이터로 넘긴다(2026-08-24)
        'item_lines': item_lines,
        'remark': (remark or '')[:60],
    }


# 자체출력 테스트용 샘플 라벨 (API 없이 레이아웃·인쇄 검수)
def _cj2_sample_label(sender: dict = None) -> dict:
    """원본은 설정(cj.sender)에서 보내는분을 읽었음 — OWS에서는 sender dict 인자(없으면 안내 문구)."""
    snd = dict(sender or {})
    return {
        'invoice_no': '650000000033', 'rcpt_ymd': datetime.now().strftime('%Y%m%d'), 'kind': 'ship',
        'clsfcd': '5D32', 'subclsfcd': '1g', 'clsf_full': '5D32-1g',
        'clsfaddr': '서울 강남구 테헤란로', 'bran': '강남대로점-A12', 'p2pcd': 'P12',
        'frt_dv': '03', 'frt_dv_nm': '신용', 'item_summary': '사무용 노트북 x2 / 모니터 x1',
        'remark': '부재시 경비실(테스트 출력 샘플)',
        'receiver': {'name': '홍길동', 'tel': '010-1234-5678',
                     'addr': '서울특별시 강남구 테헤란로 152', 'addr_detail': '강남파이낸스센터 10층 1001호'},
        'sender': snd if snd.get('name') else
                  {'name': '업무관리', 'tel': '02-0000-0000', 'addr': '(설정 > CJ대한통운에서 출고지 입력)', 'addr_detail': ''},
    }
