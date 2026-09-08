"""CJ 표준운송장(123×100mm) PDF 렌더러 — rental-system(app.py)에서 추출 이식.

★★★ cj2_waybill_pdf 는 동결(FROZEN) 레이아웃 — 원본에서 바이트 단위 그대로 복사. ★★★
★ 좌표·폰트·밴드·바코드 배치를 절대 수정하지 말 것(대표 지시 2026-07-15).
★ 위치 이슈는 코드 수정이 아니라 정렬도구 보정값(ox/oy/sc/kx/ky/rot/media/rdir)으로만 대응.

의존: reportlab(+말군고딕/나눔고딕 시스템 폰트) — 외부 이미지 의존성 없음.
실물 CJ 라벨지에 양식이 인쇄돼 있어 '변동 데이터만' 얹는 오버레이 방식.
_cj_label_offset 만 원본(Flask request 의존)과 달리 offsets dict 를 받는 순수 함수로 재작성
(클램프 범위·기본값은 원본 유지).
"""
import os
import re

from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

_PDF_FONT = 'KOR'
_PDF_FONT_REGISTERED = False
def _register_font_once():
    global _PDF_FONT_REGISTERED
    if _PDF_FONT_REGISTERED: return
    candidates = [
        r'C:\Windows\Fonts\malgun.ttf',
        r'C:\Windows\Fonts\NanumGothic.ttf',
        r'C:\Windows\Fonts\gulim.ttc',
        r'/usr/share/fonts/truetype/nanum/NanumGothic.ttf',
        r'/Library/Fonts/AppleGothic.ttf',
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                pdfmetrics.registerFont(TTFont(_PDF_FONT, p))
                _PDF_FONT_REGISTERED = True
                break
            except Exception:
                continue
    if not _PDF_FONT_REGISTERED:
        raise Exception('한글 PDF 폰트를 찾지 못함 (malgun.ttf 등)')
    # 볼드(KORB) — 큰 글자(분류코드/약칭/점소)용. 없으면 _cj_font_bold()가 KOR로 대체.
    for bp in [r'C:\Windows\Fonts\malgunbd.ttf', r'C:\Windows\Fonts\NanumGothicBold.ttf',
               r'/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf']:
        if os.path.exists(bp):
            try: pdfmetrics.registerFont(TTFont('KORB', bp)); break
            except Exception: continue

def _cj_font_bold():
    """볼드 폰트명 — 등록됐으면 'KORB', 아니면 일반 'KOR'."""
    try:
        return 'KORB' if 'KORB' in pdfmetrics.getRegisteredFontNames() else _PDF_FONT
    except Exception:
        return _PDF_FONT

# ── 운송장 PDF (자체출력 / 표준운송장 123×100mm) ──────────────────────
# CJ 2025.10 개인정보 마스킹 규칙: 받는/보내는분 성함(짝수자리), 전화 뒤4자리
def _cj_mask_name(nm: str) -> str:
    core = re.sub(r'\s+', '', nm or '')   # 공백은 글자 수에서 제외
    if len(core) <= 1:
        return core
    is_kor = any('가' <= ch0 <= '힣' for ch0 in core)
    ch = list(core)
    if (not is_kor) and len(core) >= 5:   # 국문外 5자+: 앞 4자 유지 + 이후 전부 마스킹 (ABCDE→ABCD*, ABCDEFG→ABCD***)
        for i in range(4, len(ch)): ch[i] = '*'
    else:                                  # 국문/4자이하: 2·4…번째 글자 마스킹 (홍길동→홍*동, 홍길동홍→홍*동*)
        for i in range(1, len(ch), 2): ch[i] = '*'
    return ''.join(ch)

def _cj_mask_phone(tel: str) -> str:
    d = re.sub(r'[^0-9]', '', tel or '')
    if len(d) < 8:
        return tel or ''
    if d.startswith('02'):
        return f"{d[:2]}-{d[2:-4]}-****"
    if len(d) == 11:
        return f"{d[:3]}-{d[3:7]}-****"
    if len(d) == 10:
        return f"{d[:3]}-{d[3:6]}-****"
    return d[:-4] + '****'

def _cj_fmt_invc(invc: str) -> str:
    d = re.sub(r'[^0-9]', '', invc or '')
    if len(d) == 12:
        return f"{d[:4]}-{d[4:8]}-{d[8:]}"
    if len(d) == 10:
        return f"{d[:3]}-{d[3:6]}-{d[6:]}"
    return invc or ''

#   표준운송장(123×100mm) 데이터 좌표 — CJ 인쇄 표준라벨지에 '변동 데이터만' 얹는 오버레이.
#   값은 mm(좌상단 기준 top-left). 실 라벨지 정렬은 ox/oy(mm) 보정으로 미세조정.
# (구 데이터-only 좌표표 _CJ_WB 는 공식 양식 '전체출력'(cj2_waybill_pdf 내부 드로잉)으로 대체됨)

def _cj_wrap(s: str, width: int, maxlines: int = 2) -> list:
    """라벨용 줄바꿈 — 가능한 공백 경계에서 끊고, 안 되면 강제로 자른다."""
    s = (s or '').strip(); out = []
    while s and len(out) < maxlines:
        if len(s) <= width:
            out.append(s); break
        cut = s.rfind(' ', 0, width + 1)
        if cut < int(width * 0.55): cut = width
        out.append(s[:cut].rstrip()); s = s[cut:].lstrip()
    return out

def cj2_waybill_pdf(label: dict, mask: bool = True, ox: float = 0.0, oy: float = 0.0, sc: float = 1.0,
                    kx: float = 1.0, ky: float = 1.0, rot: bool = True,
                    media_w: float = 100.0, media_l: float = 123.0, rot_dir: int = 90) -> bytes:
    """CJ 표준운송장(123×100mm) — 공식 '표준운송장 가이드(1.5인치, 251105)' 필드 사양 기반, 데이터 전용 출력.

    ★★★ 레이아웃 동결(FROZEN) — 2026-07-03 확정(423b0d9, XP-DT108B 100×123 회전 출력 검수 완료). ★★★
    ★ 좌표·폰트·밴드·바코드 배치를 절대 수정하지 말 것(대표 지시 2026-07-15). 이 함수가 전 시스템 유일한
    ★ 운송장 출력 렌더러다(모든 화면이 /api/cj2/label/<wid>/pdf 로 수렴). 위치 이슈는 코드 수정이 아니라
    ★ 정렬도구 보정값(ox/oy/sc/kx/ky/rot/media)으로만 대응한다.

    실물 CJ 라벨지에 양식(테두리·필드명·안내문)이 이미 인쇄돼 있으므로 변동 데이터만 얹는다.
    밴드(상단 기준 mm): 0-10 송장헤더 / 10-25 분류 / 25-45 받는분 / 45-52 보내는분·운임 / 52-85 상품 / 85-100 점소·바코드
    필드·폰트(가이드 표, 전부 Bold — Noto Sans KR Bold 대응 KORB):
      ①운송장번호 12pt ②접수일자 8pt ③출력매수 8pt ④재출력여부 8pt
      ⑤분류코드바코드 CODE128  ⑥분류코드 대분류36 + SUB1 53 + SUB2 36pt
      ⑦받는분 성명+전화 10pt  ⑧운송장번호바코드 CODE128C — 밴드2 세로('g' 침범 주의) + 하단 가로
      ⑨받는분주소 9pt ⑩주소약칭 24pt ⑪보내는분 성명+전화 7pt
      ⑫수량 10pt ⑬운임 10pt ⑭운임구분 10pt ⑮보내는분주소 8pt
      ⑯상품명 9pt ⑰배송메세지 8pt ⑱배달점소-별칭 18pt ⑲권내(P2P) 30pt
    마스킹(가이드 별첨): 일반=받는분 해제·보내는분 마스킹 / 반품=반대. 이름=2·4번째 글자, 전화=끝 4자리.
    보정 3종:
      ox/oy(mm) = 전체 평행이동.
      sc        = 전체 배율(글자 포함 확대) — 드라이버가 축소 인쇄할 때 역보정용. 100%↑는 페이지 넘침(하단 잘림) 주의.
      kx/ky     = 간격 스트레치 — ★폰트 크기는 그대로, 좌표(간격)만 좌상단 기준 가로/세로 배율.
                  실물 라벨 칸보다 가로가 좁게 나올 때 kx>1로 오른쪽으로 벌린다(세로 영향 없음 → 하단 안 잘림)."""
    from reportlab.graphics.barcode import code128
    import io as _io

    _register_font_once()
    F = _PDF_FONT
    FB = _cj_font_bold()

    W, H = 123 * mm, 100 * mm
    OX, OY = ox * mm, oy * mm
    buf = _io.BytesIO()
    # rot=True(기본): 라벨프린터 세로급지 — 페이지를 '드라이버에 등록된 용지'(media_w×media_l, 기본 100×123)로
    #   만들고 내용을 90° 회전해 얹는다. 용지가 라벨보다 작으면(예: 대한통운 PC의 76×130 고정 설정)
    #   비율 유지로 자동 축소해 통째로 넣는다(잘림 없음, 드라이버 무변경). 내용은 급지 시작(페이지 상단) 정렬.
    if rot:
        _mw, _ml = media_w * mm, media_l * mm
        _fit = min(_mw / H, _ml / W, 1.0)   # H=라벨 폭 100mm, W=라벨 길이 123mm
        c = Canvas(buf, pagesize=(_mw, _ml))
        # 회전 방향: +90(기본·원래 방향) / -90(반대) — 정렬도구 '회전 방향'으로 선택
        if int(rot_dir) >= 0:
            c.translate(_mw, _ml - _fit * W)
            if abs(_fit - 1.0) > 0.001:
                c.scale(_fit, _fit)
            c.rotate(90)
        else:
            c.translate(0, _ml)
            if abs(_fit - 1.0) > 0.001:
                c.scale(_fit, _fit)
            c.rotate(-90)
    else:
        c = Canvas(buf, pagesize=(W, H))
    if sc and abs(sc - 1.0) > 0.001:
        # 배율 보정 — 좌상단 기준 확대/축소(프린터 드라이버가 축소 인쇄하는 경우 역보정)
        c.translate(0, H); c.scale(sc, sc); c.translate(0, -H)

    def Y(t):  # t = 라벨 상단에서부터 mm (베이스라인). ky=세로 간격 스트레치(글자 크기 불변)
        return H - t * ky * mm + OY

    def X(x):  # kx=가로 간격 스트레치(글자 크기 불변) — 좌표만 우측으로 비례 이동
        return x * kx * mm + OX

    def txt(x, t, s, size, font=None, right=False, center=False):
        if s is None or str(s) == '':
            return
        fnt = font or FB
        c.setFont(fnt, size)   # 가이드: 전 필드 Bold
        c.setFillColorRGB(0, 0, 0)
        s = str(s)
        # 우측·하단 안전 클램프 — 간격 스트레치(kx/ky)로 벌려도 페이지 밖으로 잘리지 않게 앵커 보정
        _R = W - 1 * mm
        _yy = max(Y(t), 2.2 * mm)   # 하단: 베이스라인이 페이지 바닥 아래로 못 내려가게(잘림 방지)
        try:
            _w = c.stringWidth(s, fnt, size)
        except Exception:
            _w = 0
        if right:
            c.drawRightString(min(X(x), _R), _yy, s)
        elif center:
            c.drawCentredString(min(X(x), _R - _w / 2), _yy, s)
        else:
            c.drawString(min(X(x), _R - _w), _yy, s)

    def fit_txt(x, t, s, size, max_w_mm, font=None, min_size=5.5, right=False, center=False):
        """지정 폭(max_w_mm)을 넘으면 글자 크기를 줄여 출력 — 옆 칸/바코드 침범 방지."""
        s = str(s or '')
        if not s:
            return
        fs = float(size)
        fnt = font or FB
        try:
            while fs > min_size and c.stringWidth(s, fnt, fs) > max_w_mm * mm:
                fs -= 0.5
        except Exception:
            pass
        txt(x, t, s, fs, font=fnt, right=right, center=center)

    def bar(x, t, value, h, bw):
        """가로 바코드 — (x,t)=좌상단 mm, h=높이 mm, bw=바 굵기(pt). quiet zone 제거(라벨 칸 정밀 배치)."""
        value = re.sub(r'\s+', '', str(value or ''))
        if not value:
            return 0
        try:
            obj = code128.Code128(value, barHeight=h * mm, barWidth=bw, quiet=0)
            _bx = min(X(x), W - 1 * mm - obj.width)   # 우측 안전 클램프(kx 스트레치 시 바코드 잘림 방지)
            _by = max(Y(t + h), 1 * mm)               # 하단 안전 클램프(ky 스트레치 시 바코드 잘림 방지)
            obj.drawOn(c, max(0, _bx), _by)
            return obj.width / mm   # 실제 폭(mm) 반환
        except Exception:
            return 0

    def wrap_mm(s, size, widths, maxlines=2, font=None):
        """실제 렌더 폭(mm) 기준 자동 줄바꿈 — 줄마다 허용 폭 다르게(widths[i], 부족하면 마지막 값 반복).
        바코드 등 우측 요소와 부딪히기 전에 다음 줄로 넘긴다. 공백 경계 우선, 안 되면 강제 절단.

        ★줄바꿈 문자는 '여기서 줄을 바꿔라'는 뜻으로 살린다(대표 2026-08-24 승인: 자산번호를
          그 줄에만 찍고 싶다). 예전에는 공백 정리와 함께 뭉개서 줄바꿈이 불가능했다.
          ★좌표는 하나도 안 건드린다 — 줄 시작 위치·줄간격·글자크기 그대로다.
        """
        parts = [re.sub(r'[^\S\n]+', ' ', x).strip()
                 for x in str(s or '').replace('\r', '').split('\n')]
        parts = [x for x in parts if x]
        if len(parts) > 1:
            out = []
            for i, part in enumerate(parts):
                if len(out) >= maxlines:
                    break
                # 남은 줄을 나눠 쓴다 — 앞 조각이 다 먹으면 뒤가 통째로 사라진다
                room = max(1, maxlines - len(out) - (len(parts) - i - 1))
                out.extend(wrap_mm(part, size, widths[len(out):] or widths, room, font))
            return out[:maxlines]
        s = re.sub(r'\s+', ' ', str(s or '')).strip()
        fnt = font or FB
        out = []
        while s and len(out) < maxlines:
            w_mm = widths[min(len(out), len(widths) - 1)]
            n = len(s)
            try:
                while n > 1 and c.stringWidth(s[:n], fnt, size) > w_mm * mm:
                    n -= 1
            except Exception:
                n = min(len(s), 40)
            if n < len(s):
                cut = s.rfind(' ', 0, n + 1)
                if cut >= int(n * 0.5):
                    n = cut
            out.append(s[:n].rstrip())
            s = s[n:].lstrip()
        return out

    def fmt_date(v):
        d = re.sub(r'[^0-9]', '', str(v or ''))
        if len(d) >= 8:
            return f"{d[:4]}.{d[4:6]}.{d[6:8]}"   # 샘플 표기: 2022.10.07
        return str(v or '')

    rcv = label.get('receiver') or {}
    snd = label.get('sender') or {}
    invc = label.get('invoice_no') or ''
    digits = re.sub(r'[^0-9]', '', invc)
    is_return = (label.get('kind') in ('return', 'exchange_return'))

    # 마스킹(가이드 별첨): 예약구분 일반=받는분 해제·보내는분 마스킹 / 반품=반대
    mask_rcv = mask and is_return
    mask_snd = mask and (not is_return)
    rname = _cj_mask_name(rcv.get('name', '')) if mask_rcv else (rcv.get('name', '') or '')
    rtel = _cj_mask_phone(rcv.get('tel', '')) if mask_rcv else (rcv.get('tel', '') or '')
    raddr = (str(rcv.get('addr', '')) + ' ' + str(rcv.get('addr_detail', ''))).strip()
    sname = _cj_mask_name(snd.get('name', '')) if mask_snd else (snd.get('name', '') or '')
    stel = _cj_mask_phone(snd.get('tel', '')) if mask_snd else (snd.get('tel', '') or '')
    saddr = (str(snd.get('addr', '')) + ' ' + str(snd.get('addr_detail', ''))).strip()

    qmatch = re.findall(r'[xX]\s*(\d+)', label.get('item_summary') or '')
    qty = sum(int(q) for q in qmatch) if qmatch else (label.get('box_qty') or 1)

    # ── 밴드1 (0~10): ①운송장번호 ②접수일자 ③출력매수 ④재출력여부 ──
    txt(17, 7.4, _cj_fmt_invc(invc) or '(채번 전)', 12)
    txt(74, 7.4, fmt_date(label.get('rcpt_ymd', '')), 8, center=True)
    txt(92, 7.4, label.get('print_seq') or '1/1', 8, center=True)
    txt(111, 7.4, '재출력' if label.get('reprint') else '신규', 8, center=True)

    # ── 밴드2 (10~25): ⑤분류바코드 ⑥분류코드 ⑧상단 운송장바코드 ⑲권내(P2P) ──
    bw_clsf = 0.0
    if label.get('clsfcd'):
        bw_clsf = bar(3.5, 11.5, label['clsfcd'], 12, 0.95)   # ⑤ 좌측 폭 약 30mm (실폭 반환)
    clsf = label.get('clsf_full') or label.get('clsfcd') or ''
    if '-' in clsf:
        big, sub = clsf.split('-', 1)
    else:
        big, sub = clsf, ''
    # ⑥ 샘플(4W44-4g): 첫 글자 36pt+밑줄 → 나머지 3자리 53pt → '-'+SUB 36pt. 'g' 꼬리가 우측 상단바코드 침범 금지(82mm 캡)
    cx = max(35.0, 3.5 + bw_clsf + 2.0)   # 분류바코드 실폭 오른쪽 +2mm
    b1, b2 = big[:1], big[1:]
    txt(cx, 23.0, b1, 36)
    try:
        w1 = c.stringWidth(b1, FB, 36) / mm
    except Exception:
        w1 = 3.2
    c.setLineWidth(1.1); c.setStrokeColorRGB(0, 0, 0)
    c.line(X(cx), Y(24.4), X(cx + w1), Y(24.4))   # 첫 글자 밑줄(샘플 표기)
    cx += w1
    if b2:
        txt(cx, 23.0, b2, 53)
        try:
            cx += c.stringWidth(b2, FB, 53) / mm
        except Exception:
            cx += len(b2) * 4.7
    if sub:
        fit_txt(cx + 0.5, 23.0, '-' + sub, 36, max(4.0, 82.0 - cx), min_size=20)
    if digits:
        bar(85, 25.5, digits, 5.5, 0.95)   # ⑧ 상단 운송장바코드(가로, 받는분 라인 우측 — 'g' 침범 주의 지점)
    fit_txt(113.5, 23.0, (label.get('p2pcd') or ''), 30, 15, min_size=18, center=True)   # ⑲ 권내 P0~P50

    # ── 밴드3 (25~45): ⑦⑨⑩ 전부 동일 왼쪽 정렬(x=7, 세로 '받는분' 탭 옆) ──
    fit_txt(7, 28.0, (rname + ('  ' + rtel if rtel else '')).strip(), 10, 76, min_size=7)   # ⑦ 성명+전화(우측 바코드 앞까지)
    # ⑨ 주소 — 첫 줄은 우측 상단 운송장바코드(x85~) 앞(76mm)에서 자동 줄바꿈, 둘째 줄은 바코드 아래라 전체 폭(112mm)
    for i, ln in enumerate(wrap_mm(raddr, 9, [76, 112], 2)):
        txt(7, 31.5 + i * 3.4, ln, 9)
    fit_txt(7, 43.4, (label.get('clsfaddr') or ''), 24, 112, min_size=16)                   # ⑩ 주소약칭(대형, 주소와 겹침 방지)

    # ── 밴드4 (45~52): ⑪보내는분(x=7) ⑮주소 ⑫수량 ⑬운임 ⑭운임구분 ──
    fit_txt(7, 48.0, (sname + ('  ' + stel if stel else '')).strip(), 7, 70, min_size=5.5)
    fit_txt(7, 51.2, saddr, 8, 70, min_size=5.5)
    txt(90, 50.6, str(qty), 10, center=True)
    txt(105, 50.6, str(label.get('frt') or 0), 10, center=True)
    txt(118, 50.6, (label.get('frt_dv_nm') or ''), 10, center=True)

    # ── 밴드5 (52~85): ⑯상품명(맨 왼쪽, 라벨 폭 내 자동 줄바꿈) ──
    #   ★줄수는 설정이 정한다(label['item_lines'], 기본 4 — 2026-08-24 대표 승인).
    #     좌표는 그대로다: 55.4 + i*4.0 이라 4줄이면 67.4mm. 이 칸은 85mm까지고
    #     아래 ⑰배송메세지는 87.2mm라 부딪히지 않는다.
    #     ★인쇄가 어긋나면 설정에서 2로 되돌리면 예전과 똑같아진다(코드 수정 없이).
    try:
        _lines = max(1, min(int(label.get('item_lines') or 4), 4))
    except (TypeError, ValueError):
        _lines = 4
    for i, ln in enumerate(wrap_mm(label.get('item_summary') or '', 9, [115], _lines)):
        txt(4, 55.4 + i * 4.0, ln, 9)

    # ── 밴드6 (85~100): ⑰배송메세지 ⑱배달점소-별칭(맨 아래) ⑧하단 운송장바코드+번호 ──
    if label.get('remark'):
        # 하단 우측 운송장바코드(x78~) 침범 전 자동 줄바꿈 — 최대 3줄
        for i, ln in enumerate(wrap_mm(str(label.get('remark')), 8, [70], 3)):
            txt(4, 87.2 + i * 3.3, ln, 8)                                                   # ⑰
    fit_txt(7, 96.8, (label.get('bran') or ''), 18, 64, min_size=12)                        # ⑱
    if digits:
        # ⑧ 하단 운송장바코드 + 번호 — 번호를 바코드에 '상대 배치'(항상 3.2mm 아래)해 겹침 원천 차단
        #    (가이드: 바코드 리딩에 이슈 없도록. 세로간격(ky)을 올려도 블록 전체가 페이지 안에 들어오게 top 자동 산출)
        _bh = 7.5
        _bt = min(85.0, (98.0 / max(ky, 0.01)) - (_bh + 3.6))
        bar(78, _bt, digits, _bh, 0.95)
        txt(95, _bt + _bh + 3.2, _cj_fmt_invc(invc), 8, center=True)

    c.showPage()
    c.save()
    return buf.getvalue()

def _cj_label_offset(offsets: dict = None):
    """표준라벨 정렬 보정 — 원본(rental-system)은 Flask request.args+설정을 읽었으나,
    OWS에서는 순수 함수: offsets dict(키 ox/oy/sc/kx/ky/rot/media/rdir, 'label_' 접두 키도 허용)를 받는다.
    클램프 범위·기본값은 원본 유지. sc=배율 보정 — 프린터 드라이버가 축소 인쇄할 때 역보정(예: 70%로 나오면 sc=143).
    반환: (ox, oy, sc, kx, ky, rot, media_w, media_l, rot_dir) — cj2_waybill_pdf 파라미터 순서."""
    o = offsets or {}
    def _get(k, d):
        v = o.get(k, o.get('label_' + k, d))
        return d if v is None else v
    def _f(k, d=0):
        try: return float(_get(k, d) or d)
        except Exception: return float(d)
    sc = max(50.0, min(200.0, _f('sc', 100))) / 100.0
    kx = max(80.0, min(130.0, _f('kx', 100))) / 100.0   # 가로 간격 스트레치(폰트 불변) — 기본 100(인쇄는 반드시 '실제 크기 100%')
    ky = max(80.0, min(130.0, _f('ky', 100))) / 100.0   # 세로 간격 스트레치(폰트 불변) — 밴드 치수는 공식 규격이라 기본 100
    rot = str(_get('rot', 1)).strip() not in ('0', 'false', 'False')   # 기본 ON — 프린터에 100×123 스톡 등록해 1:1 풀사이즈 인쇄(확정 방식)
    media = str(_get('media', '100x123')).strip().lower()
    rdir = -90 if str(_get('rdir', '90')).strip() in ('-90', 'ccw') else 90
    try:
        mw, ml = (float(v) for v in media.replace('×', 'x').split('x')[:2])
        mw = max(50.0, min(130.0, mw)); ml = max(80.0, min(200.0, ml))
    except Exception:
        mw, ml = 76.0, 130.0
    return _f('ox'), _f('oy'), sc, kx, ky, rot, mw, ml, rdir
