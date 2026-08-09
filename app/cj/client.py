"""CJ대한통운 택배 표준 API V3.9.4 (계약 화주) — 정식 스펙 클라이언트.

rental-system(app.py)에서 추출 이식. Flask 비의존 순수 함수 — cj 설정은 dict 인자로 받는다.
  출고(RegBook RCPT_DV=01) / 회수·반품(02) / 예약(미래 접수일) / 취소(CnclBook)
  토큰: ReqOneDayToken(CUST_ID+BIZ_REG_NUM, 24h) · 채번: ReqInvcNo · 주소정제: ReqAddrRfnSm · 추적: ReqOneGdsTrc
  ⚠ env=dev(개발) 기본 — 실제 출고 안 됨. 운영 전환은 cj['env']='prod'.

원본의 log() 호출만 표준 logging으로 치환. 아래 로직은 전부 실사고/CJ 개발팀 회신 기반이므로 수정 금지:
  · cj2_call: HTTP 401 시 토큰 강제 재발급 1회 재시도
  · _cj2_phone: 02/050X/선두 0 없는 대표번호('000') 분할 특례 (2026-07-27 CJ 개발팀 지적 반영)
  · cj2_reg_book: 반품(RCPT_DV=02) INVC_NO 강제 공란 하드가드 (반품 송장은 SM기사 출력 시 CJ 채번)
  · _cj2_party: 우편번호 미보유 시 '000000' 폴백 (빈값=Oracle NULL → RegBook ORA-01400)
"""
import logging
import re
import time
from datetime import datetime

import requests as http

logger = logging.getLogger(__name__)

CJ2_HOSTS = {
    'dev':  'https://dxapi-dev.cjlogistics.com:5054',
    'prod': 'https://dxapi.cjlogistics.com:5052',
}
_cj2_token_cache: dict = {}   # {cust_id: {token, expires_at}}

def _cj2_cust(cj: dict) -> str:
    """고객사 코드 — 신규 cust_id 우선, 없으면 기존 customerCode 재사용."""
    return (cj.get('cust_id') or cj.get('customerCode') or '').strip()

def _cj2_base(cj: dict) -> str:
    return CJ2_HOSTS.get((cj.get('env') or 'dev').strip().lower(), CJ2_HOSTS['dev'])

def _cj2_phone(s: str):
    """전화번호 → (NO1, NO2, NO3). 02=지역2자리, 그 외 0XX=3자리.
    선두 0이 없는 대표번호(1566-1674 등)는 CJ 규격상 NO1='000' — 000-1566-1674.
    (2026-07-27 CJ 개발팀 지적: '1566-1674'가 156-61674-로 잘못 분할되던 버그 정정)"""
    d = re.sub(r'[^0-9]', '', s or '')
    if not d:
        return ('', '', '')
    if d.startswith('02'):
        a, rest = d[:2], d[2:]
    elif re.match(r'^050[0-9]', d) and len(d) >= 12:
        a, rest = d[:4], d[4:]   # 050X 안심(가상)번호는 국번이 4자리 — 0507-1234-5678
    elif d.startswith('0'):
        a, rest = d[:3], d[3:]
    else:
        a, rest = '000', d   # 지역번호 없는 전국대표번호(15XX/16XX/18XX 등)
    if len(rest) >= 8:
        return (a, rest[:4], rest[4:8])
    if len(rest) == 7:
        return (a, rest[:3], rest[3:])
    return (a, rest, '') if rest else (a, '', '')

def cj2_token(cj: dict, force: bool = False) -> str:
    """ReqOneDayToken — CUST_ID + BIZ_REG_NUM 으로 1일 토큰 발급(24h, 30분전 갱신)."""
    cust = _cj2_cust(cj)
    biz  = re.sub(r'[^0-9]', '', cj.get('biz_reg_num') or '')
    if not cust or not biz:
        raise Exception('CJ 고객사코드(CUST_ID)/사업자번호(BIZ_REG_NUM) 미설정 — 설정 > CJ대한통운에서 입력하세요')
    now = time.time()
    c = _cj2_token_cache.get(cust)
    if c and not force and c.get('expires_at', 0) > now + 1800:
        return c['token']
    url = _cj2_base(cj) + '/ReqOneDayToken'
    r = http.post(url, headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                  json={'DATA': {'CUST_ID': cust, 'BIZ_REG_NUM': biz}}, timeout=15)
    rj = r.json() if (r.text or '').strip().startswith('{') else {}
    if (rj.get('RESULT_CD') or '').upper().startswith('S'):
        data = rj.get('DATA') or {}
        tok = (data.get('TOKEN_NUM') or '').strip()
        exp = (data.get('TOKEN_EXPRTN_DTM') or '').strip()
        exp_ts = now + 23 * 3600
        try:
            if len(exp) == 14:
                exp_ts = datetime.strptime(exp, '%Y%m%d%H%M%S').timestamp()
        except Exception:
            pass
        if tok:
            _cj2_token_cache[cust] = {'token': tok, 'expires_at': exp_ts}
            logger.info(f"CJ 1Day 토큰 발급 ({'운영' if cj.get('env')=='prod' else '개발'}) 만료 {exp}")
            return tok
    raise Exception(f"토큰 발급 실패 [{rj.get('RESULT_CD')}] {rj.get('RESULT_DETAIL') or (r.text or '')[:200]}")

def cj2_call(cj: dict, resource: str, data: dict) -> dict:
    """표준 API 호출 — 토큰 헤더 + {DATA:{...TOKEN_NUM...}}. RESULT_CD 'S'/'E'. 401 시 토큰 재발급 1회 재시도."""
    def _do(tok):
        url = _cj2_base(cj) + '/' + resource
        body = {'DATA': {**data, 'TOKEN_NUM': tok}}
        return http.post(url, headers={'CJ-Gateway-APIKey': tok, 'Content-Type': 'application/json',
                                       'Accept': 'application/json'}, json=body, timeout=20)
    tok = cj2_token(cj)
    r = _do(tok)
    if r.status_code == 401:
        time.sleep(0.2)
        r = _do(cj2_token(cj, force=True))
    rj = r.json() if (r.text or '').strip().startswith('{') else {}
    ok = (rj.get('RESULT_CD') or '').upper().startswith('S')
    return {'ok': ok, 'result_cd': rj.get('RESULT_CD'), 'detail': rj.get('RESULT_DETAIL'),
            'data': rj.get('DATA'), 'http': r.status_code, 'raw': (r.text or '')[:500]}

def cj2_new_invoice(cj: dict) -> str:
    """ReqInvcNo — 운송장 번호 1건 채번(자가출력용)."""
    res = cj2_call(cj, 'ReqInvcNo', {'CLNTNUM': _cj2_cust(cj)})
    if res['ok']:
        return ((res.get('data') or {}).get('INVC_NO') or '').strip()
    raise Exception(f"운송장 채번 실패 [{res['result_cd']}] {res['detail']}")

def cj2_addr_refine(cj: dict, address: str) -> dict:
    """ReqAddrRfnSm — 주소정제(운송장 분류코드/주소약칭/배달점소). 실패해도 None 허용."""
    try:
        res = cj2_call(cj, 'ReqAddrRfnSm', {'CLNTNUM': _cj2_cust(cj),
                                            'CLNTMGMCUSTCD': _cj2_cust(cj),
                                            'ADDRESS': address or ''})
        return res.get('data') if res['ok'] else None
    except Exception:
        return None

def cj2_track(cj: dict, invc_no: str) -> dict:
    """ReqOneGdsTrc — 운송장 번호 기준 단건 배송추적."""
    return cj2_call(cj, 'ReqOneGdsTrc', {'CLNTNUM': _cj2_cust(cj), 'INVC_NO': invc_no})

def _cj2_party(p: dict, prefix: str) -> dict:
    """보내는분/받는분/주문자 블록 생성. prefix ∈ {SENDR, RCVR, ORDRR}."""
    t1, t2, t3 = _cj2_phone(p.get('tel') or p.get('phone') or '')
    return {
        f'{prefix}_NM': (p.get('name') or '')[:100],
        f'{prefix}_TEL_NO1': t1, f'{prefix}_TEL_NO2': t2, f'{prefix}_TEL_NO3': t3,
        f'{prefix}_CELL_NO1': t1, f'{prefix}_CELL_NO2': t2, f'{prefix}_CELL_NO3': t3,
        # 우편번호 미보유 시 '000000' 기본값(빈값=Oracle NULL → RegBook ORA-01400). CJ 라우팅은
        # 우편번호가 아니라 주소정제 분류코드(CLSFCD)로 하므로 0채움이 가이드 예시 표준.
        f'{prefix}_ZIP_NO': (re.sub(r'[^0-9]', '', p.get('zip') or '')[:6] or '000000'),
        f'{prefix}_ADDR': (p.get('addr') or '')[:150],
        f'{prefix}_DETAIL_ADDR': (p.get('addr_detail') or p.get('detail') or '')[:300] or '-',
    }

def cj2_reg_book(cj: dict, *, kind: str, sender: dict, receiver: dict, items: list,
                 cust_use_no: str, rcpt_ymd: str = '', invc_no: str = '', ori_invc_no: str = '',
                 box_type: str = '02', box_qty: int = 1, frt_dv: str = '03', remark: str = '',
                 ordrr: dict = None, cancel: bool = False, colct_ymd: str = '') -> dict:
    """RegBook/CnclBook — 출고/반품(회수)/예약/취소. kind: 'ship'(01) | 'return'(02)."""
    cust = _cj2_cust(cj)
    rcpt_dv = '01' if kind == 'ship' else '02'
    rcpt_ymd = re.sub(r'[^0-9]', '', rcpt_ymd) or datetime.now().strftime('%Y%m%d')
    prt_st = '02' if kind == 'ship' else '01'   # 출고=선출력(자가출력), 반품=미출력(CJ 출력)
    # ⚠ CJ 규칙: 반품(RCPT_DV=02) 접수는 PRT_ST=01 + INVC_NO=NULL(빈값) 이어야 한다.
    #   반품 운송장번호는 SM기사 출력 시 CJ가 생성하므로, 우리가 INVC_NO에 값을 넣으면 접수 검증 오류.
    #   원 출고송장은 ORI_INVC_NO 칸으로만 전달(INVC_NO 아님). 취소(CnclBook)는 예외로 원값 유지.
    if kind == 'return' and not cancel:
        invc_no = ''   # RegBook 반품 하드가드 — 어떤 호출자가 invc_no를 넘겨도 INVC_NO는 비움
    arr = []
    for i, it in enumerate(items or [{'GDS_NM': '렌탈 장비'}], start=1):
        arr.append({'MPCK_SEQ': i, 'GDS_CD': (it.get('code') or '')[:20],
                    'GDS_NM': (it.get('name') or '렌탈 장비')[:500], 'GDS_QTY': int(it.get('qty') or 1),
                    'UNIT_CD': '', 'UNIT_NM': '', 'GDS_AMT': int(it.get('amt') or 0)})
    data = {
        'CUST_ID': cust, 'RCPT_YMD': rcpt_ymd, 'CUST_USE_NO': (cust_use_no or '')[:50],
        'RCPT_DV': rcpt_dv, 'WORK_DV_CD': '01', 'REQ_DV_CD': '02' if cancel else '01',
        'MPCK_KEY': f"{rcpt_ymd}_{cust}_{(cust_use_no or invc_no)}"[:100],
        'CAL_DV_CD': '01', 'FRT_DV_CD': frt_dv, 'CNTR_ITEM_CD': '01',
        'BOX_TYPE_CD': box_type, 'BOX_QTY': int(box_qty or 1), 'FRT': '',
        'CUST_MGMT_DLCM_CD': cust,
        **_cj2_party(sender, 'SENDR'), **_cj2_party(receiver, 'RCVR'),
        **_cj2_party(ordrr or sender, 'ORDRR'),
        'INVC_NO': invc_no or '', 'ORI_INVC_NO': ori_invc_no or '', 'ORI_ORD_NO': '',
        # 집화 예정일(수거 희망일) — 회수(반품)에서 CJ 기사 방문 수거일 지정. 미지정이면 빈값(CJ 기본 배정).
        'COLCT_EXPCT_YMD': re.sub(r'[^0-9]', '', colct_ymd or '')[:8], 'COLCT_EXPCT_HOUR': '',
        'SHIP_EXPCT_YMD': '', 'SHIP_EXPCT_HOUR': '',
        'PRT_ST': prt_st, 'ARTICLE_AMT': sum(int(it.get('amt') or 0) for it in (items or [])),
        'REMARK_1': (remark or '')[:1000], 'REMARK_2': '', 'REMARK_3': '',
        'COD_YN': 'N', 'ETC_1': '', 'ETC_2': '', 'ETC_3': '', 'ETC_4': '', 'ETC_5': '',
        'DLV_DV': '01', 'RCPT_SERIAL': '', 'ARRAY': arr,
    }
    return cj2_call(cj, 'CnclBook' if cancel else 'RegBook', data)
