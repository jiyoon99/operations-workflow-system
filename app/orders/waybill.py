"""송장 발급/출력 — QC 완료 후 포장 전 '선출력' (2026-07-28 대표 지시).

- 송장 상품명 = [채널] 제품코드/상품명 · 매칭 자산번호 · 추가옵션
  → 출고팀이 라벨만 보고 구성품·자산 대조 검증 가능
- 운송장 레이아웃은 동결(cj2_waybill_pdf 바이트 복사본) — 상품명은 데이터로만 구성
- CJ 미설정/미무장(armed=False)이면 테스트 발행(가짜 999 송장번호 + [테스트발행] 표기)
  → API 키 없이도 출력 흐름을 검증할 수 있다 (원칙 #3 시뮬레이션 기본)
"""
import io
import json
import re

from flask import Response, abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require, require_any
from ..cj import (_cj2_label, _cj_label_offset, cj2_addr_refine, cj2_new_invoice,
                  cj2_reg_book, cj2_track, cj2_waybill_pdf)
from ..db import get_db, tx
from ..prep import options_for_order as prep_options_for_order
from ..prep import unchecked_for_order as prep_unchecked_for_order
from . import _get_order_or_404, _order_scope_clause, bp


# ★송장을 뽑을 수 있는 단계 — 한 곳에서만 정한다(미리보기·실발행이 어긋나면 안 된다).
#   2026-08-05 제작 완료부터 → 2026-09-03 출고 확인부터(대표: "제작대기~SW 검수완료에서는
#   송장이 나가면 안 된다") → **2026-09-04 대표 정정: "SW 검수완료 탭에서 송장 일괄 출력이
#   가능해야 함"**. 검수까지 끝나면 실물이 확정되니 그 자리에서 송장을 뽑아 붙이고 내보낸다.
#   제작 대기~제작 완료는 여전히 막는다 — 아직 어떤 기계가 나갈지 확정되지 않은 단계다.
WAYBILL_STAGE_MSG = ("SW 검수가 끝난 주문만 송장을 출력할 수 있습니다. "
                     "제작과 SW 검수를 먼저 마치세요.")


def _waybill_stage_ok(row):
    return bool(row["inspection_done"] or row["shipping_done"])


def _cj_settings(conn):
    row = conn.execute("SELECT value FROM settings WHERE key='cj'").fetchone()
    cfg = {}
    if row:
        try:
            cfg = json.loads(row["value"]) or {}
        except ValueError:
            cfg = {}
    cfg.setdefault("env", "dev")
    cfg.setdefault("armed", False)
    cfg.setdefault("sender", {})
    cfg.setdefault("label", {})
    # 운임구분·박스타입 — RMS 대표 지정값(2026-07-28: 신용 03 / 극소 01)과 동일 기준.
    # ★예전 OWS는 client.py 기본값(박스 02 소)이 그대로 나가 RMS와 다른 값을 보냈다.
    cfg.setdefault("frt_dv", "03")
    cfg.setdefault("box_type", "01")
    # 집화 휴무일 규칙(2026-09-07 대표) — 실제 값은 cj.calendar.pickup_settings 가 읽는다
    cfg.setdefault("pickup", {})
    return cfg


def check_pickup_date(cfg, pickup_date):
    """수거 희망일이 CJ 쉬는 날이면 400 으로 막는다(회수 예약 3경로 공통 관문).

    ★화면에서도 막지만 서버가 마지막 선이다 — 옛 화면·API 우회·달력을 연 채 자정을 넘긴
      경우가 전부 여기서 걸린다. 기사가 안 오는 날로 예약하면 고객이 하루를 헛기다린다.
    """
    from ..cj.calendar import next_pickup_day, pickup_block_reason
    reason = pickup_block_reason(pickup_date, cfg)
    if reason:
        abort(400, description=f"{reason} 가장 이른 예약 가능일은 {next_pickup_day(cfg)} 입니다.")


def _is_real(cfg):
    """실발행 상태인가 — 고객코드는 구명칭(customerCode)도 인정한다.

    ★cust_id 만 보면 함정이 생긴다: 연결 테스트(_cj2_cust는 둘 다 인정)는 성공하는데
      발급은 조용히 전부 999 테스트 발행이 된다(RMS 이관 설정이 구명칭인 경우).
    """
    from ..cj.client import _cj2_cust
    return bool(cfg.get("env") == "prod" and cfg.get("armed")
                and _cj2_cust(cfg) and (cfg.get("biz_reg_num") or "").strip())


def _next_wid(conn):
    """WB-YYYYMMDD-NN. MAX 기준 — 중간 행이 삭제돼도 기존 ID와 충돌하지 않는다."""
    today = config.today_str()
    row = conn.execute(
        "SELECT MAX(CAST(substr(wid, 13) AS INTEGER)) AS m FROM waybills WHERE wid GLOB ?",
        (f"WB-{today}-*",)).fetchone()
    return f"WB-{today}-{(row['m'] or 0) + 1:02d}"


def _next_test_invoice(conn):
    """테스트 송장번호 — 999 접두. 삭제/취소가 있어도 겹치지 않도록 최대값 기준."""
    row = conn.execute(
        "SELECT MAX(CAST(substr(invoice_no, 4) AS INTEGER)) AS m FROM waybills "
        "WHERE invoice_no GLOB '999[0-9]*'").fetchone()
    return "999" + f"{(row['m'] or 0) + 1:09d}"


def _safe_label_text(s):
    """라벨 텍스트에서 'x숫자' 패턴을 제거한다.

    동결 렌더러가 item_summary의 [xX]\\s*(\\d+)를 전부 더해 수량 칸에 인쇄하므로,
    'RTX3060'·'8Gx2' 같은 모델/옵션 표기가 수량으로 오인된다. 렌더러는 수정 금지이므로
    데이터 쪽에서 x를 전각(×)으로 바꿔 오집계를 막는다(사람이 읽기에는 동일).
    """
    return re.sub(r"[xX](?=\s*\d)", "×", str(s or ""))


_LABEL_BUDGET = 118          # label.py가 상품명 칸을 120자에서 자른다. 여유 2자.


def _field(row, key, default=""):
    """sqlite3.Row와 dict 어느 쪽으로 와도 안전하게 값을 꺼낸다."""
    try:
        v = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


# ★한 송장에 실물이 최대 3대까지 들어간다(대표 2026-08-24: "우리 한 송장 기준
#   최대 3대까지만 들어가거든"). 그보다 많이 적으면 종이와 상자가 안 맞는다.
LABEL_MAX_ASSETS = 3


def _asset_text(asset_nos, room=999, cap=LABEL_MAX_ASSETS):
    """자산번호 — 한 송장에 들어가는 대수까지만. 나머지는 '외 N대'로 개수만 알린다.

    ★번호를 중간에서 자르면 안 된다('260729-0001,2607'처럼 반쪽 번호가 찍히면
      포장 대조가 오히려 위험해진다). 온전한 번호만 적고 나머지는 개수로 알린다.
    ★같은 번호가 두 번 들어오면 한 번만 적는다 — 종이에 같은 번호가 두 번 찍히면
      포장 담당이 두 대인 줄 안다(대표 2026-08-24: "중복되지 않게").
    ★대수 상한이 있어도 총 대수는 숨기지 않는다 — '외 N대'로 남겨야 담당자가
      상자를 더 찾아봐야 하는지 안다.
    """
    seen, uniq = set(), []
    for no in (asset_nos or []):
        k = str(no or "").strip()
        if k and k not in seen:
            seen.add(k)
            uniq.append(k)
    if not uniq:
        return ""
    total = len(uniq)
    try:
        limit = max(1, int(cap or LABEL_MAX_ASSETS))
    except (TypeError, ValueError):
        limit = LABEL_MAX_ASSETS
    kept = []
    for i, no in enumerate(uniq[:limit]):
        rest = total - i - 1
        tail = f" 외 {rest}대" if rest else ""
        cand = "자산 " + ",".join(kept + [no]) + tail
        if len(cand) <= room:
            kept.append(no)
        else:
            break
    if not kept:
        return f"자산 {total}대"
    rest = total - len(kept)
    return "자산 " + ",".join(kept) + (f" 외 {rest}대" if rest else "")


def customer_parcel_seq(conn, row):
    """같은 고객에게 함께 나가는 주문 중 이 주문이 몇 번째인가 → (순번, 전체).

    한 사람이 6건을 주문하면 상자도 6개다. 송장에 1/6·2/6이 찍혀 있어야
    포장·배송·고객 모두 '다 왔는지' 확인할 수 있다(대표 요청 2026-07-29).

    묶는 기준은 전화번호(숫자만) — 고객 이력·같은 고객 표시와 같은 잣대다.
    ★아직 배송완료되지 않은 건만 센다. 이미 발급한 송장의 번호가 나중에 흔들리지 않도록
      '송장 발급 여부'는 조건에 넣지 않는다(넣으면 한 건 낼 때마다 분모가 줄어든다).
    묶을 게 없으면 (1, 1) — 그 경우 순번을 인쇄하지 않는다.
    """
    phone = re.sub(r"\D", "", row["phone"] or "")
    if len(phone) < 9:
        return 1, 1
    expr = ("REPLACE(REPLACE(REPLACE(REPLACE(phone,'-',''),' ',''),'(',''),')','')")
    rows = conn.execute(
        f"SELECT id FROM orders WHERE {expr} = ? AND cancelled_at='' AND archived_at='' "
        "AND delivered_at='' ORDER BY id", (phone,)).fetchall()
    ids = [r["id"] for r in rows]
    if len(ids) < 2 or row["id"] not in ids:
        return 1, 1
    return ids.index(row["id"]) + 1, len(ids)


# ---------------------------------------------------------------- 운송장 문구 템플릿
#   (2026-08-24 대표 지시) "어떤 키워드를 어떻게 활용할건지 우리가 설정할 수 있게 해줘.
#    [배송메모], [자산번호], [옵션명] 이런식으로 순서를 바꿀 수도 있을거고."
#
#   ★사용자가 정하는 것은 '무엇을 어떤 순서로' 뿐이다. 잘림 규칙은 코드가 계속 쥔다 —
#     자산번호는 반쪽이 찍히면 대조가 불가능하고(_asset_text), 칸은 120/60자로 고정이다.
#   ★자동 접두([테스트발행]·[리뷰어]·[1/6]·[박스 N개])는 상황에 따라 붙는 표시라
#     템플릿 대상이 아니다 — 항상 맨 앞에 그대로 붙는다.
#   ★수량(x1)은 첫 항목에 자동으로 붙는다. CJ 접수 payload(GDS_QTY)와 같은 값이라
#     여기서 떼면 CJ에 보내는 수량이 1로 굳는다 — 일부러 토큰으로 열지 않았다.

# 토큰 → (칸, 글자수 상한, 설명). 상한은 '이 조각이 가져갈 수 있는 최대'다.
LABEL_TOKENS = {
    "쇼핑몰":   ("both", 14,  "주문이 들어온 몰 — [고도몰]"),
    "제품코드": ("both", 40,  "제품코드. 없으면 상품명으로 대체한다"),
    "상품명":   ("both", 999, "몰 상품 제목 — 남는 자리를 전부 쓴다"),
    "자산번호": ("both", 999, "자산 260628-0015 — 번호를 중간에서 자르지 않는다"),
    "옵션명":   ("both", 46,  "챙긴 옵션 + 몰이 보낸 옵션(중복 제거)"),
    "챙긴옵션": ("both", 46,  "우리가 챙기는 옵션만 — 몰 옵션은 빼고"),
    "등급":     ("both", 12,  "요구 등급(A급 등)"),
    "주문번호": ("both", 26,  "몰 주문번호"),
    "수취인":   ("both", 20,  "받는 분 이름"),
    "배송메모": ("remark", 60, "고객이 남긴 배송 요청 — 기사가 보는 문구"),
    # ★상품명 칸 전용(2026-08-24 대표: "줄바꿈 기능도 문구로"). 배송메세지 칸은 CJ 에
    #   REMARK_1 로도 나가서 줄바꿈을 실을 수 없다 — 저장 때 거부한다.
    "줄바꿈":   ("item", 0,   "여기서 줄을 바꿉니다 — 뒤 내용이 새 줄에 찍힙니다"),
}
DEFAULT_ITEM_TMPL = "[쇼핑몰] [제품코드] / [자산번호] / [상품명] / [옵션명]"
DEFAULT_REMARK_TMPL = "[배송메모] / [챙긴옵션]"
_TOKEN_RE = re.compile(r"\[([^\[\]]+)\]")


def label_tmpl_of(cfg, which):
    """설정에서 템플릿을 꺼낸다. 비어 있으면 지금까지의 문구와 똑같은 기본값."""
    t = ((cfg or {}).get("label_tmpl") or {}).get(which)
    t = (t or "").strip()
    if which == "item":
        return t or DEFAULT_ITEM_TMPL
    return t or DEFAULT_REMARK_TMPL


def check_label_tmpl(tmpl, which):
    """모르는 키워드를 쓰면 저장 단계에서 알려 준다 — 종이에 빈칸으로 나가기 전에."""
    bad = [w for w in _TOKEN_RE.findall(tmpl or "")
           if w not in LABEL_TOKENS
           or (LABEL_TOKENS[w][0] == "remark" and which != "remark")
           or (LABEL_TOKENS[w][0] == "item" and which != "item")]
    return bad


def _token_value(name, ctx):
    """토큰 하나의 값. 자산번호는 자리를 받아야 하므로 여기서 처리하지 않는다."""
    if name == "쇼핑몰":
        return f"[{ctx['channel']}]"
    if name == "제품코드":
        return ctx["code"] or ctx["name"]        # 코드가 없으면 상품명이 그 자리를 맡는다
    if name == "상품명":
        return ctx["name"] if ctx["code"] else ""   # 코드 자리에 이미 상품명이 갔으면 생략
    if name == "옵션명":
        return ctx["opt_text"]
    if name == "챙긴옵션":
        return ctx["prep_text"]
    if name == "등급":
        return ctx["grade"]
    if name == "주문번호":
        return ctx["order_no"]
    if name == "수취인":
        return ctx["recipient"]
    if name == "배송메모":
        return ctx["memo"]
    return ""


def _render_slot(raw, ctx, room):
    """슬롯 하나(키워드+글자 섞임)를 문자열로. 상한은 슬롯이 쓴 토큰 중 가장 큰 값."""
    cap = 0
    out, pos = [], 0
    for m in _TOKEN_RE.finditer(raw):
        out.append(raw[pos:m.start()])
        name = m.group(1)
        pos = m.end()
        if name not in LABEL_TOKENS:
            continue                              # 모르는 키워드는 조용히 비운다
        cap = max(cap, LABEL_TOKENS[name][1])
        if name == "자산번호":
            # ★번호를 중간에서 자르지 않는다 — 남은 자리를 알려 주고 온전한 것만 담는다
            out.append(_asset_text(ctx["asset_nos"], room))
        else:
            out.append(_safe_label_text(str(_token_value(name, ctx) or "")))
    out.append(raw[pos:])
    text = " ".join(x.strip() for x in "".join(out).split()).strip(" /,")
    return text, (cap or 999)


def _compose_items(row, asset_nos, simulated, prep_names=(), seq=(1, 1), box_qty=1, tmpl=None):
    """송장 상품명 칸 — 셋팅·QC·포장이 같은 종이를 보고 대조한다(대표 지시 2026-07-29).

    무엇을 어떤 순서로 담을지는 설정 ▸ CJ대한통운 ▸ 운송장 문구가 정한다
    (기본값 = DEFAULT_ITEM_TMPL, 지금까지의 문구와 같다).

    ★칸이 120자뿐이다. 앞에 적은 키워드가 먼저 자리를 가져가고, 자리가 모자라면
      뒤가 빠진다 — 그래서 순서가 곧 우선순위다. 자산번호만은 반쪽으로 찍히면
      대조가 불가능해서 온전한 것만 담고 나머지는 '외 N대'로 넘긴다.
    """
    channel = _safe_label_text((row["channel"] or "수기").strip())
    code = _safe_label_text((row["product_code"] or "").strip())
    name = _safe_label_text((row["product_name"] or "").strip())
    qty = row["quantity"] or 1

    # 옵션 = 우리가 챙기는 제공 옵션 + 몰이 보내준 옵션(중복은 제거)
    opt_bits = []
    for t in list(prep_names) + [(row["option_name"] or "").strip()]:
        t = _safe_label_text((t or "").strip())
        if t and t not in opt_bits:
            opt_bits.append(t)
    ctx = {
        "channel": channel, "code": code, "name": name or "상품", "qty": qty,
        "asset_nos": asset_nos or [],
        "opt_text": ("옵션 " + ", ".join(opt_bits)) if opt_bits else "",
        "prep_text": ", ".join(x for x in prep_names if (x or "").strip()),
        "grade": _safe_label_text(_order_grade(row)),
        "order_no": _safe_label_text(str(_field(row, "order_no") or "").strip()),
        "recipient": _safe_label_text(str(_field(row, "recipient") or "").strip()),
        "memo": _safe_label_text((_field(row, "delivery_message") or "").strip()),
    }

    # ── 자동 접두 — 상황에 따라 붙는 표시라 템플릿 대상이 아니다(항상 맨 앞)
    #   ★순서는 [테스트발행] [박스 N개] [n/m] [리뷰어] 로 고정이다 — 예전 코드가 앞에
    #     하나씩 덧붙여(prepend) 만든 순서이고, 시험이 그 순서를 계약으로 잡고 있다.
    #     바꾸면 포장 담당이 종이에서 찾던 자리가 달라진다.
    prefix = ""
    if simulated:
        prefix += "[테스트발행] "
    try:
        _bq = int(box_qty or 1)
    except (TypeError, ValueError):
        _bq = 1
    if _bq > 1:
        # ★CJ 라벨 레이아웃은 동결이라 좌표를 못 늘린다 — 상품명 칸 맨 앞에 실어 보낸다
        #   (대표: "2박스 이상 나가는 경우도 있어" 2026-07-30).
        prefix += f"[박스 {_bq}개] "
    if seq and seq[1] > 1:
        prefix += f"[{seq[0]}/{seq[1]}] "
    if _field(row, "is_review"):
        prefix += "[리뷰어] "

    budget = _LABEL_BUDGET - len(f" x{qty}")      # label.py가 첫 항목에 x수량을 붙인다
    items, used = [], len(prefix)

    pending_br = False          # 조각이 '[줄바꿈]'뿐이면 다음 조각을 새 줄로
    for raw in (tmpl or DEFAULT_ITEM_TMPL).split("/"):
        # ★[줄바꿈](2026-08-24) — 이 조각을 새 줄에서 시작한다. 실제 줄바꿈 여부는
        #   설정(cj.label.lineBreak)이 정한다 — 자산번호 자동 줄바꿈과 같은 스위치다.
        br_here = "[줄바꿈]" in raw
        raw = raw.replace("[줄바꿈]", "")
        if not raw.strip():
            if br_here:
                pending_br = True
            continue
        sep = 3 if items else 0                   # label.py가 ' / '로 잇는다
        room = budget - used - sep
        if room < 8:                              # 남은 자리가 너무 적으면 넣지 않는다
            break
        text, cap = _render_slot(raw, ctx, room)
        if not text:
            continue
        t = text[:min(cap, room)]
        first = not items
        # ★자산번호 조각은 줄을 바꿔 그 줄에만 찍는다(대표 2026-08-24).
        #   포장 담당이 번호만 훑어 대조할 수 있어야 한다. 실제 줄바꿈 여부는
        #   설정(cj.label.lineBreak)이 정하고, label.py 가 그때 판단한다.
        cell = {"name": (prefix + t) if first else t}
        if first:
            cell["qty"] = qty
        if (br_here or pending_br or "[자산번호]" in raw) and not first:
            cell["br"] = True
        pending_br = False
        items.append(cell)
        used += len(t) + sep
    if not items:                                 # 템플릿을 다 비워 놨을 때의 최후 보루
        items = [{"name": (prefix + (code or name or "상품"))[:budget], "qty": qty}]
    return items



def _order_grade(row):
    """주문의 요구 등급 — 몰이 상품코드·제목 뒤에 붙여 보내는 'AA급3' 꼬리표에서 뽑는다.

    ★orders에 등급 칸은 없다. 셋팅 화면(product_info.sku_of)이 떼어 내는 것과
      같은 정규식을 쓴다 — 두 화면이 서로 다른 등급을 말하면 대조가 무의미해진다.
    """
    from .product_info import _RE_GRADE_TAIL, _RE_GRADE_TAIL_BARE
    for raw in (_field(row, "product_code"), _field(row, "product_name")):
        m = _RE_GRADE_TAIL.search(str(raw or ""))
        if m:
            return m.group(0).strip()
        # '급' 자 없이 맨끝에 붙는 꼴("…내장 AA")도 등급이다 — 등급 글자만 돌려준다
        #   (뒤 숫자는 용량 장식이라 제외). ★sku_of 와 같은 게이트를 태운다(적대 리뷰
        #   #3·#8): "/" 앞 조각 + 코드 모양(밑줄 2개)일 때만 — 자유 텍스트 상품명
        #   ("…무상 AS")을 등급으로 오인하거나, "코드 AA / 옵션" 꼴에서 끝앵커가
        #   옵션 텍스트에 막혀 못 읽는 두 가지를 함께 막는다(2026-09-01).
        seg = str(raw or "").split("/")[0].strip()
        if seg.count("_") == 2:
            m = _RE_GRADE_TAIL_BARE.search(seg)
            if m:
                return m.group(1).strip()
    return ""


def _remark_row(row, asset_nos):
    """배송메세지 칸에서도 [자산번호]를 쓸 수 있게 자산번호를 행에 실어 준다."""
    out = dict(row)
    out["_asset_nos"] = list(asset_nos or [])
    return out


def _compose_remark(row, prep_names=(), tmpl=None):
    """⑰배송메세지 칸(60자) — 무엇을 담을지는 설정이 정한다.

    ★기본값은 [배송메모] / [옵션명] — 고객 요청을 먼저 살린다.
      '부재시 경비실'처럼 기사에게 필요한 요청을 우리 문구로 덮으면 배송 사고가 난다
      (626건 중 280건이 이런 요청을 달고 들어온다).
    ★이 칸은 CJ REMARK_1 = 기사가 보는 배송메시지다. 내부 식별용 문구를 앞에 두면
      기사가 요청사항으로 읽는다 — 순서를 바꿀 때 그 점을 알고 바꿔야 한다.
    """
    names = [t.strip() for t in prep_names if (t or "").strip()]
    opt_bits = []
    for t in names + [(_field(row, "option_name") or "").strip()]:
        t = _safe_label_text((t or "").strip())
        if t and t not in opt_bits:
            opt_bits.append(t)
    ctx = {
        "channel": _safe_label_text((_field(row, "channel") or "수기").strip()),
        "code": _safe_label_text((_field(row, "product_code") or "").strip()),
        "name": _safe_label_text((_field(row, "product_name") or "").strip()) or "상품",
        "qty": _field(row, "quantity") or 1,
        "asset_nos": list(_field(row, "_asset_nos") or []),
        "opt_text": "", "prep_text": "", "grade": _safe_label_text(_order_grade(row)),
        "order_no": _safe_label_text(str(_field(row, "order_no") or "")),
        "recipient": _safe_label_text(str(_field(row, "recipient") or "")),
        "memo": (_field(row, "delivery_message") or "").strip(),
    }
    out, used = [], 0
    for raw in (tmpl or DEFAULT_REMARK_TMPL).split("/"):
        if not raw.strip():
            continue
        sep = 3 if out else 0
        room = 60 - used - sep
        if room < 8:                    # 앞 조각이 다 썼다 — 뒤는 넣지 않는다
            break
        if "[챙긴옵션]" in raw or "[옵션명]" in raw:
            # 옵션은 글자 중간에서 끊지 않는다 — 온전한 것만 적고 나머지는 '외 N건'
            pool = names if "[챙긴옵션]" in raw else opt_bits
            text = _fit_names(pool, room) if pool else ""
        else:
            text, cap = _render_slot(raw, ctx, room)
            text = text[:min(cap, room)]
        if not text:
            continue
        out.append(text)
        used += len(text) + sep
    return " / ".join(out)[:60]



def _fit_names(names, room):
    """자리에 맞게 옵션 이름을 담는다 — 글자 중간에서 끊지 않고 '외 N건'으로 넘긴다.

    '리브레오피스, 램 16G 업'처럼 반쯤 잘린 문구는 작업자가 무엇인지 알 수 없어
    안 쓴 것만 못하다. 온전히 들어가는 항목만 적고 나머지 개수를 알려 준다.
    """
    kept = []
    for name in names:
        left = len(names) - len(kept) - 1
        tail = f" 외 {left}건" if left else ""
        cand = ", ".join(kept + [name]) + tail
        if len(cand) <= room:
            kept.append(name)
        else:
            break
    if not kept:
        return f"옵션 {len(names)}건"[:room]
    left = len(names) - len(kept)
    return ", ".join(kept) + (f" 외 {left}건" if left else "")


def label_layout_of(cfg):
    """운송장 상품명 칸 인쇄 방식 — 문제가 나면 화면에서 되돌린다(대표 2026-08-24).

    ★lineBreak=False, lines=2 로 두면 2026-08-24 이전과 글자까지 똑같아진다.
      코드를 되돌리지 않고 설정만으로 원복할 수 있어야 한다.
    """
    lab = (cfg or {}).get("label") or {}
    try:
        lines = max(1, min(int(lab.get("lines") or 4), 4))
    except (TypeError, ValueError):
        lines = 4
    return {"line_break": lab.get("lineBreak") is not False, "item_lines": lines}


def _sender(cfg):
    s = cfg.get("sender") or {}
    if (s.get("name") or "").strip():
        return {"name": s.get("name", ""), "tel": s.get("tel", ""),
                "addr": s.get("addr", ""), "addr_detail": s.get("addr_detail", ""), "zip": s.get("zip", "")}
    return {"name": "업무관리", "tel": "", "addr": "(설정 > API 관리에서 출고지를 입력하세요)", "addr_detail": ""}


def _pickup(cfg):
    """회수지(반품 받는 곳). 따로 설정하지 않았으면 출고지를 쓴다."""
    p = cfg.get("pickup") or {}
    if (p.get("name") or "").strip():
        return {"name": p.get("name", ""), "tel": p.get("tel", ""),
                "addr": p.get("addr", ""), "addr_detail": p.get("addr_detail", ""), "zip": p.get("zip", "")}
    return _sender(cfg)


@bp.get("/orders/<int:oid>/waybill-preview")
def waybill_preview(oid):
    """발급 전에 송장에 '무엇이 찍힐지' 보여준다 — CJ를 부르지 않는다.

    셋팅·QC가 검수 완료 후 상품명 칸(쇼핑몰/상품/옵션/수량/자산번호)과
    배송메세지 칸(고객 요청+제공 옵션)을 눈으로 확인하고 발급하게 한다(대표 요청 2026-07-29).
    """
    require_any("orders.ship", "waybills.manage", "orders.work")
    conn = get_db()
    row = _get_order_or_404(conn, oid)
    assets = conn.execute(
        "SELECT a.asset_no FROM order_assets oa JOIN assets a ON a.id=oa.asset_id "
        "WHERE oa.order_id=? ORDER BY a.asset_no", (oid,)).fetchall()
    asset_nos = [a["asset_no"] for a in assets]
    prep_names = [o["name"] for o in prep_options_for_order(conn, row)]
    unchecked = [o["name"] for o in prep_options_for_order(conn, row) if not o["checked"]]
    cfg = _cj_settings(conn)
    parcel_seq = customer_parcel_seq(conn, row)
    try:
        pv_box = max(1, min(int(request.args.get("boxQty") or 1), 10))
    except (TypeError, ValueError):
        pv_box = 1
    items = _compose_items(dict(row), asset_nos, simulated=not _is_real(cfg),
                           prep_names=prep_names, seq=parcel_seq, box_qty=pv_box,
                           tmpl=label_tmpl_of(cfg, "item"))
    summary = " / ".join(
        f"{it.get('name') or ''}{' x' + str(it.get('qty')) if it.get('qty') else ''}".strip()
        for it in items if it.get("name"))[:120]
    blockers = []
    if not _waybill_stage_ok(row):
        blockers.append(WAYBILL_STAGE_MSG)
    if not asset_nos and not row["is_review"]:
        blockers.append("자산번호를 먼저 매칭하세요(구성품 대조용).")
    if not (row["recipient"] or "").strip() or not (row["address"] or "").strip():
        blockers.append("수취인·주소가 비어 있습니다.")
    if unchecked:
        blockers.append("아직 챙기지 않은 옵션: " + ", ".join(unchecked[:3]))
    dup = conn.execute(
        "SELECT wid, invoice_no, status FROM waybills WHERE order_id=? AND type='forward' "
        "AND status IN ('issued','test','pending') LIMIT 1", (oid,)).fetchone()
    return jsonify({
        "itemSummary": summary,                       # ⑯상품명 칸 (120자)
        "remark": _compose_remark(_remark_row(dict(row), asset_nos), prep_names,
                                  tmpl=label_tmpl_of(cfg, "remark")),   # ⑰배송메세지 칸 (60자)
        "itemTmpl": label_tmpl_of(cfg, "item"),
        "remarkTmpl": label_tmpl_of(cfg, "remark"),
        "recipient": row["recipient"], "address": row["address"],
        "assetNos": asset_nos, "prepOptions": prep_names,
        "quantity": row["quantity"], "boxQty": pv_box,
        "isReview": bool(row["is_review"]), "reviewNote": row["review_note"],
        "parcelSeq": parcel_seq[0], "parcelTotal": parcel_seq[1],
        "real": _is_real(cfg),                        # False면 테스트 발행(999 송장)
        "blockers": blockers,
        "existing": ({"wid": dup["wid"], "invoiceNo": dup["invoice_no"],
                      "status": dup["status"]} if dup else None),
    })


@bp.get("/waybills/mode")
def waybill_mode():
    """지금 송장을 뽑으면 진짜 접수인가, 테스트 발행인가.

    ★셋팅 보드의 🧾 버튼은 두 경우에 똑같이 생겼다. 화면에 표시가 없으면
      담당자가 테스트인 줄 알고 진짜 접수를 하거나(기사가 온다), 진짜인 줄 알고
      999 번호를 붙여 내보낸다(추적이 끊긴다).
    """
    require_any("orders.ship", "waybills.manage", "orders.work", "orders.view")
    cfg = _cj_settings(get_db())
    sender = (cfg.get("sender") or {})
    return jsonify({
        "real": _is_real(cfg),
        "env": cfg.get("env") or "dev",
        "armed": bool(cfg.get("armed")),
        "custId": bool(_cj2_cust_ok(cfg)),
        "bizReg": bool((cfg.get("biz_reg_num") or "").strip()),
        "senderReady": bool((sender.get("name") or "").strip()
                            and (sender.get("addr") or "").strip()),
        "maxAssets": LABEL_MAX_ASSETS,
    })


def _cj2_cust_ok(cfg):
    from ..cj.client import _cj2_cust
    return (_cj2_cust(cfg) or "").strip()


@bp.post("/orders/<int:oid>/waybill")
def issue_waybill(oid):
    """송장 발급.

    ★CJ 네트워크 호출은 반드시 트랜잭션 '밖'에서 한다 — BEGIN IMMEDIATE를 쥔 채
      외부 API를 기다리면 그동안 전 시스템의 쓰기가 잠긴다(원칙 #1).
      1) 짧은 쓰기 tx: 검증 + 예약행(pending) 선점 → 중복 발급도 여기서 차단
      2) tx 밖: CJ 채번/정제/접수
      3) 짧은 쓰기 tx: 결과 확정(실패 시 예약행 삭제)
    """
    require_any("orders.ship", "waybills.manage")
    body = request.get_json(silent=True) or {}
    # ★boxQty를 안 보내면 그 주문의 지난 송장 상자수를 물려받는다.
    #   재발급은 '같은 짐을 다시 부치는' 것이라 상자 수가 달라지면 안 된다
    #   (2026-08-24: 발급 UI를 셋팅으로 모으면서 배송 화면의 상자수 입력칸이 없어졌다 —
    #    그때 조용히 1로 굳던 것을 여기서 막는다).
    if body.get("boxQty") in (None, ""):
        # ★waybills 의 PK 는 wid(TEXT) — id 칸이 없다. wid 는 WB-YYYYMMDD-NN 이라
        #   created_at 과 함께 정렬하면 '가장 최근 송장'이 정확히 잡힌다.
        prev = get_db().execute(
            "SELECT box_qty FROM waybills WHERE order_id=? AND type='forward' "
            "ORDER BY created_at DESC, wid DESC LIMIT 1", (oid,)).fetchone()
        box_qty = max(1, min(int((prev["box_qty"] if prev else 1) or 1), 10))
    else:
        box_qty = max(1, min(int(body.get("boxQty") or 1), 10))
    # ★다매(RMS 이식, 대표 2026-08-10) — 한 주문을 여러 '송장'으로 나눠 보낸다.
    #   박스수(boxQty)와 다르다: 박스수=한 송장에 상자 N개(합포장, 번호 1개),
    #   송장수(waybillQty)=송장 N장(번호 N개, 상자마다 따로 추적). 나눠 보내면 N장을 쓴다.
    wb_qty = max(1, min(int(body.get("waybillQty") or 1), 10))
    if wb_qty > 1:
        box_qty = 1                       # 다매는 송장마다 상자 1개 — RMS 실운영 방식

    # ---- 1) 검증 + 선점
    with tx(write=True) as conn:
        row = _get_order_or_404(conn, oid)
        if row["cancelled_at"]:
            abort(400, description="취소된 주문입니다.")
        if row["archived_at"]:
            abort(400, description="보관된 주문입니다.")
        if not _waybill_stage_ok(row):
            abort(400, description=WAYBILL_STAGE_MSG)
        dup = conn.execute(
            "SELECT wid, invoice_no FROM waybills WHERE order_id=? AND type='forward' "
            "AND status IN ('issued','test','pending') LIMIT 1",
            (oid,)).fetchone()
        if dup:
            abort(409, description=f"이미 발행된 송장이 있습니다({dup['wid']} / {dup['invoice_no'] or '발급 중'}). "
                                   "재발행하려면 기존 송장을 먼저 취소하세요.")
        assets = conn.execute(
            "SELECT a.asset_no, a.tier FROM order_assets oa JOIN assets a ON a.id=oa.asset_id "
            "WHERE oa.order_id=? ORDER BY a.asset_no", (oid,)).fetchall()
        asset_nos = [a["asset_no"] for a in assets]
        # ★리뷰어 출고는 제품 없이 빈 박스만 나가는 경우가 있어 자산 매칭을 요구하지 않는다
        if not asset_nos and not row["is_review"]:
            abort(400, description="자산번호 매칭 후 송장을 출력하세요. (구성품 대조 검증용)")
        # ★가재고 출고 금지(2026-08-04) — 출고 확인만 막으면 송장 발급으로 우회된다.
        #   두 관문 모두에서 같은 규칙을 지켜야 수리 안 끝난 물건이 못 나간다.
        prov = [a["asset_no"] for a in assets if a["tier"] == "가재고"]
        if prov:
            abort(400, description=(
                f"가재고 자산({', '.join(prov)})이 매칭돼 있어 송장을 발급할 수 없습니다. "
                "매입 화면에서 가용 또는 실재고로 바꾼 뒤 발급하세요."))
        if not (row["recipient"] or "").strip() or not (row["address"] or "").strip():
            abort(400, description="수취인과 주소를 먼저 입력하세요. "
                                   "(주문관리 ▸ 해당 주문 ▸ [상세]에서 배송지를 채울 수 있습니다)")
        # ★미리보기는 '챙길 옵션'이 남으면 발급을 잠그는데 여기서 안 막으면 규칙이 어긋난다.
        #   옵션 규칙을 나중에 추가하면 이미 제작 완료된 주문에서 실제로 벌어진다.
        missing_opts = prep_unchecked_for_order(conn, row)
        if missing_opts:
            abort(400, description="아직 챙기지 않은 옵션이 있습니다: "
                                   + ", ".join(missing_opts[:4])
                                   + " — 셋팅 화면에서 체크한 뒤 발급하세요.")

        cfg = _cj_settings(conn)
        real = _is_real(cfg)
        sender = _sender(cfg)
        if real:
            # 전화 필수 가드 — CJ 게이트웨이는 'S'를 줘도 코어에서 조용히 실패(실사고 기반 규칙)
            if not (row["phone"] or "").strip():
                abort(400, description="실발행에는 수취인 전화번호가 필수입니다.")
            if not (sender.get("name") or "").strip() or not (sender.get("addr") or "").strip() \
                    or not (sender.get("tel") or "").strip():
                abort(400, description="실발행 전에 설정 > 배송/CJ대한통운에서 출고지(보내는 분) 정보를 입력하세요.")
        today = config.today_str()
        receiver = {"name": row["recipient"], "tel": row["phone"],
                    "addr": row["address"], "addr_detail": "", "zip": row["postal_code"]}
        base_use_no = f"HB{oid}-{config.now().strftime('%H%M%S')}"
        ts = config.now_iso()
        # 송장마다 행을 미리 선점한다 — 2번째부터 접수키에 -B2, -B3 …(RMS와 동일 규칙,
        # 취소 PK가 접수키라 송장마다 달라야 개별 취소가 된다)
        wids, use_nos, invoices = [], [], []
        for i in range(wb_qty):
            w_i = _next_wid(conn)
            u_i = base_use_no if i == 0 else f"{base_use_no}-B{i + 1}"
            inv_i = "" if real else _next_test_invoice(conn)
            conn.execute(
                "INSERT INTO waybills(wid, order_id, type, cj_kind, invoice_no, status, recipient, phone, "
                "postal_code, address, items, cust_use_no, cj_rcpt_ymd, box_qty, created_by, created_at, updated_at) "
                "VALUES(?,?,'forward','ship',?,'pending',?,?,?,?,'',?,?,?,?,?,?)",
                (w_i, oid, inv_i, row["recipient"], row["phone"], row["postal_code"], row["address"],
                 u_i, today if real else "", box_qty, g.user["display_name"], ts, ts),
            )
            wids.append(w_i)
            use_nos.append(u_i)
            invoices.append(inv_i)
        wid, cust_use_no, invoice = wids[0], use_nos[0], invoices[0]
        order_snapshot = dict(row)
        # 우리가 챙기는 옵션도 송장에 찍는다 — 포장 담당이 한 번 더 대조한다
        prep_names = [o["name"] for o in prep_options_for_order(conn, row)]
        parcel_seq = customer_parcel_seq(conn, row)   # 같은 고객 1/6·2/6 표기

    # ---- 2) CJ 호출 (트랜잭션 밖 — DB 락을 쥐지 않는다)
    refine = None
    responses = [None] * wb_qty
    item_tmpl = label_tmpl_of(cfg, "item")

    # ★송장 한 장에는 실물이 LABEL_MAX_ASSETS(3)대까지 들어간다. 다매로 여러 장을 낼 때
    #   모든 장에 같은 번호를 찍으면 어느 상자에 어느 기계가 들었는지 알 수 없다 —
    #   3대씩 나눠 싣는다. 나눌 만큼 많지 않으면(3대 이하) 예전처럼 전부 같이 찍는다.
    def _nos_for(i):
        if wb_qty < 2 or len(asset_nos) <= LABEL_MAX_ASSETS:
            return asset_nos
        lo = i * LABEL_MAX_ASSETS
        return asset_nos[lo:lo + LABEL_MAX_ASSETS] or asset_nos[-LABEL_MAX_ASSETS:]

    def _items_for(i):
        return _compose_items(order_snapshot, _nos_for(i), simulated=not real,
                              box_qty=box_qty, prep_names=prep_names, seq=parcel_seq,
                              tmpl=item_tmpl)

    items = _items_for(0)               # CJ 접수 payload·라벨의 기준(1장짜리면 이게 전부)
    remark = _compose_remark(_remark_row(order_snapshot, asset_nos), prep_names,
                             tmpl=label_tmpl_of(cfg, "remark"))
    registered = []                     # 접수까지 끝난 것 — 실패 시 이것만 되돌린다
    try:
        if real:
            refine = cj2_addr_refine(cfg, order_snapshot["address"])
            for i in range(wb_qty):
                inv = cj2_new_invoice(cfg)
                if not inv:
                    raise RuntimeError("CJ 운송장 번호 채번에 실패했습니다.")
                invoices[i] = inv
                res = cj2_reg_book(
                    cfg, kind="ship", sender=sender, receiver=receiver, items=_items_for(i),
                    cust_use_no=use_nos[i], rcpt_ymd=today, invc_no=inv, box_qty=box_qty,
                    box_type=cfg.get("box_type") or "01", frt_dv=cfg.get("frt_dv") or "03",
                    remark=remark,
                )
                if not res.get("ok"):
                    raise RuntimeError(f"CJ 접수 실패({i + 1}/{wb_qty}): "
                                       f"{res.get('detail') or res.get('result_cd')}")
                responses[i] = res
                registered.append(i)
            invoice = invoices[0]
    except Exception as e:
        # ★부분 실패 정리 — 이미 접수된 앞 송장들은 취소를 시도한다(안 하면 CJ에
        #   유령 예약이 남아 기사가 온다). 취소 실패는 로그에 남기고 계속 진행.
        cancel_failed = []
        for i in registered:
            try:
                cj2_reg_book(cfg, kind="ship", sender=sender, receiver=receiver, items=[],
                             cust_use_no=use_nos[i], rcpt_ymd=today, invc_no=invoices[i],
                             cancel=True)
            except Exception:                                    # noqa: BLE001
                cancel_failed.append(invoices[i])
        with tx(write=True) as conn:  # 선점 해제 — 재시도 가능하게
            for w_i in wids:
                conn.execute("DELETE FROM waybills WHERE wid=? AND status='pending'", (w_i,))
            audit.log("waybill_failed", target=f"{wid} #{oid}",
                      detail={"error": str(e), **({"cancelFailed": cancel_failed}
                                                  if cancel_failed else {})})
        msg = str(e)
        if cancel_failed:
            msg += f" ★이미 접수된 송장 취소도 실패: {', '.join(cancel_failed)} — CJ에 취소 요청 필요"
        abort(502, description=msg)

    # ---- 3) 결과 확정
    with tx(write=True) as conn:
        ts = config.now_iso()
        for i in range(wb_qty):
            label = _cj2_label(invoices[i], today, sender, receiver, _items_for(i), refine,
                               remark=remark, default_item="업무관리 상품",
                               **label_layout_of(cfg))
            if wb_qty > 1:                # 몇 번째 상자인지 라벨에 찍는다(값만 — 레이아웃 무접촉)
                label["item_summary"] = (f"[{i + 1}/{wb_qty}] " + label["item_summary"])[:120]
            conn.execute(
                "UPDATE waybills SET invoice_no=?, status=?, items=?, label=?, cj_response=?, updated_at=? WHERE wid=?",
                (invoices[i], "issued" if real else "test", label["item_summary"],
                 json.dumps(label, ensure_ascii=False),
                 json.dumps(responses[i], ensure_ascii=False) if responses[i] else None,
                 ts, wids[i]),
            )
        invoice = invoices[0]
        # ★단계는 건드리지 않는다(2026-09-03) — 출고 확인이 이미 끝난 뒤에만 여기 오므로
        #   송장이 단계를 앞당길 이유가 없다. 택배사·송장번호만 적는다.
        conn.execute(
            "UPDATE orders SET courier=?, tracking_no=?, updated_at=? WHERE id=?",
            ("CJ대한통운", invoice, ts, oid))
        audit.log("waybill_issued", target=f"{wid} #{oid} {order_snapshot['recipient']}",
                  detail={"invoiceNo": invoice, "simulated": not real, "assets": asset_nos,
                          "boxQty": box_qty,
                          **({"waybillQty": wb_qty, "invoiceNos": invoices} if wb_qty > 1 else {})})
    # ★발급 즉시 몰에 송장번호를 올린다(2026-09-08 대표 "송장 뽑았는데 고도몰에 왜 안 들어가 있지").
    #   대표가 몰 관리자에서 손으로 넣던 시점이 바로 이 순간이다. 몰 호출은 트랜잭션 밖에서, 실패해도
    #   발급은 그대로(10분 스윕이 다시 보내고 '몰 전송 대기' 목록에도 남는다). 몰 설정의
    #   [송장 발급 즉시 전송]을 끄면 예전처럼 출고 확인 때 보낸다. 테스트 발행(999…)은 절대 안 나간다.
    mall_push = None
    if not str(invoice or "").startswith("999"):
        from flask import current_app
        from ..malls.invoice_push import push_after_issue
        try:
            res = push_after_issue(current_app, oid)
            if res is not None:
                mall_push = {"ok": bool(res[0]), "message": res[1]}
        except Exception as e:                                    # noqa: BLE001
            current_app.logger.exception("송장 몰 전송(발급 즉시) 실패 | order=%s", oid)
            mall_push = {"ok": False, "message": str(e)}
    return jsonify({"wid": wid, "invoiceNo": invoice, "simulated": not real,
                    "wids": wids, "invoiceNos": invoices, "qty": wb_qty,
                    "mallPush": mall_push}), 201


@bp.get("/waybills")
def list_waybills():
    require_any("orders.ship", "waybills.manage", "shipping.view", "as.manage")
    # 담당 분류 밖 고객의 이름·전화·주소가 송장 목록으로 새면 안 된다
    scope, params = _order_scope_clause()
    sql = ("SELECT w.*, o.channel AS order_channel, o.product_name AS order_product "
           "FROM waybills w LEFT JOIN orders o ON o.id = w.order_id "
           "WHERE (w.order_id IS NULL OR EXISTS (SELECT 1 FROM orders o2 WHERE o2.id = w.order_id"
           + scope.replace("o.", "o2.") + "))")
    if request.args.get("type"):
        sql += " AND w.type = ?"
        params.append(request.args["type"])
    if request.args.get("status"):
        sql += " AND w.status = ?"
        params.append(request.args["status"])
    q = (request.args.get("q") or "").strip()
    if q:
        # 송장번호는 하이픈을 넣어 부르는 경우가 많다 — 숫자만 남겨서도 맞춰 본다
        digits = "".join(ch for ch in q if ch.isdigit())
        sql += " AND (w.invoice_no LIKE ? OR w.recipient LIKE ? OR w.items LIKE ? OR w.wid LIKE ?"
        params.extend(["%" + q + "%"] * 3 + ["%" + q + "%"])
        if digits and digits != q:
            sql += " OR w.invoice_no LIKE ?"
            params.append("%" + digits + "%")
        sql += ")"
    # 기간 조회 — 일/주 단위 마감 대조용(대표 2026-08-10). created_at은 ISO라 앞부분 비교로 충분.
    if request.args.get("from"):
        sql += " AND substr(w.created_at, 1, 10) >= ?"
        params.append(request.args["from"][:10])
    if request.args.get("to"):
        sql += " AND substr(w.created_at, 1, 10) <= ?"
        params.append(request.args["to"][:10])
    sql += " ORDER BY w.created_at DESC LIMIT 300"
    rows = get_db().execute(sql, params).fetchall()
    return jsonify([
        {"wid": r["wid"], "orderId": r["order_id"], "invoiceNo": r["invoice_no"], "status": r["status"],
         "type": r["type"], "recipient": r["recipient"], "phone": r["phone"], "address": r["address"],
         "items": r["items"], "boxQty": r["box_qty"], "channel": r["order_channel"],
         "stageName": r["cj_stage_nm"], "stageCode": r["cj_stage_cd"], "stageAt": r["cj_stage_at"],
         "scheduledDate": r["scheduled_date"],
         # 일괄 인쇄가 라벨 없는 건(회수 등)을 미리 거를 수 있게 알려 준다
         "hasLabel": bool(r["label"]),
         "createdBy": r["created_by"], "createdAt": r["created_at"]}
        for r in rows
    ])


@bp.get("/waybills/board")
def waybill_board():
    """배송/송장 현황판 집계(2026-08-31 대표 — "오늘 출고 송장·조회·회수·신규 접수를
    현황판처럼 메인으로"). 화면 상단 카드가 이 한 번의 호출로 다 채워진다.

    ★오늘 출고 확인은 /orders/today 와 같은 잣대(취소 제외·보관 포함) — 진행 중 목록을
      화면에서 세면 출고 직후 보관되는 운영 특성상 종일 0건으로 보인다(2026-07-31 감사).
    """
    require_any("orders.ship", "waybills.manage", "shipping.view", "as.manage")
    conn = get_db()
    today = config.now_iso()[:10]
    scope, params = _order_scope_clause()
    wb_scope = (" AND (w.order_id IS NULL OR EXISTS (SELECT 1 FROM orders o2 "
                "WHERE o2.id = w.order_id" + scope.replace("o.", "o2.") + "))")

    # ★카드 조건은 여기 한 벌뿐 — 카드 숫자와 카드 클릭 근거 내역(?detail=)이 같은
    #   잣대를 쓴다. 근거를 다시 계산하면 숫자와 목록이 어긋난다(RMS 매출 카드 원칙).
    buckets = {
        "issuedToday": ("waybills", "substr(w.created_at,1,10)=? AND w.type='forward' "
                        "AND w.status IN ('issued','delivered')" + wb_scope, [today] + params),
        "testToday": ("waybills", "substr(w.created_at,1,10)=? AND w.type='forward' "
                      "AND w.status='test'" + wb_scope, [today] + params),
        "shippedToday": ("orders", "o.cancelled_at=''" + scope
                         + " AND substr(o.shipping_at,1,10)=?", params + [today]),
        "inTransit": ("waybills", "w.type='forward' AND w.status='issued' "
                      "AND COALESCE(w.cj_stage_cd,'') != '91'" + wb_scope, list(params)),
        "recallActive": ("waybills", "w.type='recall' "
                         "AND w.status NOT IN ('delivered','canceled')" + wb_scope, list(params)),
    }

    detail = (request.args.get("detail") or "").strip()
    if detail:
        if detail not in buckets:
            abort(400, description="알 수 없는 현황 카드입니다.")
        table, where, ps = buckets[detail]
        if table == "orders":
            rows = conn.execute(
                "SELECT o.id, o.order_no, o.channel, o.recipient, o.product_name, "
                "o.shipping_by, o.shipping_at, o.archived_at FROM orders o WHERE " + where
                + " ORDER BY o.shipping_at DESC LIMIT 300", ps).fetchall()
            return jsonify({"detail": detail, "rows": [
                {"orderId": r["id"], "orderNo": r["order_no"] or "", "channel": r["channel"] or "",
                 "recipient": r["recipient"] or "", "productName": r["product_name"] or "",
                 "by": r["shipping_by"] or "", "at": r["shipping_at"] or "",
                 "archived": bool(r["archived_at"])} for r in rows]})
        rows = conn.execute(
            "SELECT w.wid, w.invoice_no, w.recipient, w.items, w.status, w.type, "
            "w.cj_stage_nm, w.scheduled_date, w.created_at FROM waybills w WHERE " + where
            + " ORDER BY w.created_at DESC LIMIT 300", ps).fetchall()
        return jsonify({"detail": detail, "rows": [
            {"wid": r["wid"], "invoiceNo": r["invoice_no"] or "", "recipient": r["recipient"] or "",
             "items": r["items"] or "", "status": r["status"], "type": r["type"],
             "stage": r["cj_stage_nm"] or "", "scheduledDate": r["scheduled_date"] or "",
             "createdAt": r["created_at"] or ""} for r in rows]})

    def count(key):
        table, where, ps = buckets[key]
        alias = "o" if table == "orders" else "w"
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} {alias} WHERE " + where, ps).fetchone()[0]

    # 발급 걸림 — pending 은 수 분이면 정상(발급 중), 오래 남아 있으면 CJ 응답 유실이다
    prow = conn.execute(
        "SELECT COUNT(*) AS c, MIN(w.created_at) AS oldest FROM waybills w "
        "WHERE w.status='pending'" + wb_scope, params).fetchone()
    return jsonify({
        "date": today,
        "issuedToday": count("issuedToday"), "testToday": count("testToday"),
        "shippedToday": count("shippedToday"),
        "inTransit": count("inTransit"), "recallActive": count("recallActive"),
        "pending": prow["c"], "pendingOldest": prow["oldest"] or "",
    })


@bp.get("/waybills/<wid>/trace")
def waybill_trace(wid):
    """송장 하나의 상세 + 실시간 추적 타임라인 — 송장번호 클릭 팝업용(RMS 이식).

    고객이 송장번호만 들고 전화했을 때 이 팝업 하나로 답한다.
    추적은 조회성 호출이라 예약이 생기지 않는다. CJ 미설정/미발행이면 정보·메모만 준다.
    """
    require_any("orders.ship", "waybills.manage", "shipping.view", "as.manage")
    conn = get_db()
    row = _waybill_in_scope(conn, wid)
    if row is None:
        abort(404, description="송장을 찾을 수 없습니다.")
    cfg = _cj_settings(conn)
    snapshot = dict(row)

    timeline, track_error = [], ""
    if snapshot["invoice_no"] and _is_real(cfg) and snapshot["status"] != "test":
        try:
            res = cj2_track(cfg, snapshot["invoice_no"])          # 조회성 — tx 밖
            data = (res or {}).get("data") or {}
            rows = data.get("PROC_LIST") or data.get("procList") or []
            if isinstance(rows, dict):
                rows = [rows]
            for p in rows:
                timeline.append({
                    "code": str(p.get("CRG_ST_CD") or p.get("crgStCd") or "").strip(),
                    "name": (p.get("CRG_ST_NM") or p.get("crgStNm") or "").strip(),
                    "date": str(p.get("SCAN_YMD") or p.get("scanYmd") or "").strip(),
                    "time": str(p.get("SCAN_TME") or p.get("SCAN_HOUR") or p.get("scanTme") or "").strip(),
                    "branch": (p.get("DEALT_BRAN_NM") or p.get("dealtBranNm") or "").strip(),
                })
            if not res or not res.get("ok"):
                track_error = str((res or {}).get("detail") or "추적 데이터가 아직 없습니다.")
        except Exception as e:                                   # noqa: BLE001
            track_error = str(e)
    elif snapshot["status"] == "test":
        track_error = "테스트 발행 송장이라 CJ 추적이 없습니다."
    elif not snapshot["invoice_no"]:
        track_error = "송장번호가 아직 없습니다(회수는 기사 집화 시 채번)."
    elif not _is_real(cfg):
        track_error = "CJ 운영 설정(운영+무장)이 있어야 실시간 추적이 됩니다."

    return jsonify({
        "wid": snapshot["wid"], "invoiceNo": snapshot["invoice_no"],
        "type": snapshot["type"], "status": snapshot["status"],
        "recipient": snapshot["recipient"], "phone": snapshot["phone"],
        "address": snapshot["address"], "items": snapshot["items"],
        "orderId": snapshot["order_id"], "boxQty": snapshot["box_qty"],
        "stageName": snapshot["cj_stage_nm"], "stageCode": snapshot["cj_stage_cd"],
        "stageAt": snapshot["cj_stage_at"],
        "createdBy": snapshot["created_by"], "createdAt": snapshot["created_at"],
        "note": snapshot["note"] or "",
        "timeline": timeline, "trackError": track_error,
    })


@bp.post("/waybills/<wid>/note")
def waybill_note(wid):
    """송장 운영 메모 저장 — 고객 통화 내용·재배송 약속 등."""
    require_any("orders.ship", "waybills.manage")
    body = request.get_json(silent=True) or {}
    note = (body.get("note") or "").strip()[:500]
    with tx(write=True) as conn:
        row = _waybill_in_scope(conn, wid)
        if row is None:
            abort(404, description="송장을 찾을 수 없습니다.")
        conn.execute("UPDATE waybills SET note=?, updated_at=? WHERE wid=?",
                     (note, config.now_iso(), wid))
        audit.log("waybill_note", target=wid, detail={"len": len(note)})
    return jsonify({"ok": True})


def _waybill_in_scope(conn, wid):
    """담당 분류 밖 주문의 송장은 없는 것으로 취급한다."""
    scope, params = _order_scope_clause()
    return conn.execute(
        "SELECT w.* FROM waybills w WHERE w.wid=? AND (w.order_id IS NULL OR EXISTS "
        "(SELECT 1 FROM orders o WHERE o.id = w.order_id" + scope + "))",
        [wid] + params).fetchone()


@bp.get("/waybills/<wid>/pdf")
def waybill_pdf(wid):
    require_any("orders.ship", "waybills.manage", "shipping.view", "as.manage")
    conn = get_db()
    row = _waybill_in_scope(conn, wid)
    if row is None or not row["label"]:
        abort(404, description="송장을 찾을 수 없습니다.")
    label = json.loads(row["label"])
    cfg = _cj_settings(conn)
    offsets = _cj_label_offset(cfg.get("label") or {})
    pdf = cj2_waybill_pdf(label, True, *offsets)
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={wid}.pdf"})


@bp.post("/as-tickets/<int:tid>/return-waybill")
def issue_as_return_waybill(tid):
    """A/S 반송 송장 — 수리를 마친 고객 물건을 돌려보낸다(회수의 반대 방향).

    구조는 주문 송장 발급과 같다: 짧은 쓰기 tx로 선점 → 트랜잭션 밖에서 CJ 호출 → 확정.
    """
    require("as.manage")
    body = request.get_json(silent=True) or {}
    box_qty = max(1, min(int(body.get("boxQty") or 1), 10))

    with tx(write=True) as conn:
        t = conn.execute("SELECT * FROM as_tickets WHERE id=?", (tid,)).fetchone()
        if t is None:
            abort(404, description="A/S 건을 찾을 수 없습니다.")
        if t["status"] in ("returned", "cancelled"):
            abort(400, description="이미 마무리된 A/S 건입니다.")
        if not (t["customer"] or "").strip() or not (t["address"] or "").strip():
            abort(400, description="고객명과 주소를 먼저 입력하세요.")
        # ★유상 건은 돈을 받은 뒤에만 송장이 나간다(2026-09-07 대표 "결제 전은 유상처리된
        #   것들에 대해서 금액 결제 확인 후 송장 출력 및 방문수령 가능하게").
        #   진행 보드의 [💰 결제 전] 칸이 이 규칙을 화면에서 보여주고, 여기는 상세 팝업이나
        #   API 로 우회해도 막히는 서버 쪽 잠금이다. 무상은 해당 없음.
        #   ★금액이 아니라 '유상인가'로 막는다 — 청구 0원인 유상(금액 미입력)까지 막아야
        #     '무상 → 유상'으로 바꾼 건이 돈을 못 받은 채 나가지 않는다(2026-09-08 대표).
        from ..asvc import payment_block_reason
        why = payment_block_reason(t, "송장을 발급할")
        if why:
            abort(400, description=why + " (진행 상황 ▸ [💰 결제 확인])")
        dup = conn.execute(
            "SELECT wid, invoice_no FROM waybills WHERE as_ticket_id=? AND type='forward' "
            "AND status IN ('issued','test','pending') LIMIT 1", (tid,)).fetchone()
        if dup:
            abort(409, description=f"이미 반송 송장이 있습니다({dup['wid']} / {dup['invoice_no'] or '발급 중'}).")

        cfg = _cj_settings(conn)
        real = _is_real(cfg)
        sender = _sender(cfg)
        if real:
            if not (t["phone"] or "").strip():
                abort(400, description="실발행에는 고객 전화번호가 필수입니다.")
            if not (sender.get("name") or "").strip() or not (sender.get("addr") or "").strip() \
                    or not (sender.get("tel") or "").strip():
                abort(400, description="실발행 전에 설정 > API 관리에서 출고지 정보를 입력하세요.")
        asset_no = ""
        if t["asset_id"]:
            a = conn.execute("SELECT asset_no FROM assets WHERE id=?", (t["asset_id"],)).fetchone()
            asset_no = a["asset_no"] if a else ""
        today = config.today_str()
        # ★우편번호를 함께 보낸다(2026-08-31) — 주문 송장과 같은 CJ 규격.
        receiver = {"name": t["customer"], "tel": t["phone"], "addr": t["address"],
                    "addr_detail": "", "zip": t["postal_code"] or ""}
        cust_use_no = f"HBR{tid}-{config.now().strftime('%H%M%S')}"
        wid = _next_wid(conn)
        ts = config.now_iso()
        invoice = "" if real else _next_test_invoice(conn)
        conn.execute(
            "INSERT INTO waybills(wid, order_id, as_ticket_id, type, cj_kind, invoice_no, status, "
            "recipient, phone, postal_code, address, items, cust_use_no, cj_rcpt_ymd, box_qty, "
            "created_by, created_at, updated_at) "
            "VALUES(?,NULL,?,'forward','ship',?,'pending',?,?,?,?,'',?,?,?,?,?,?)",
            (wid, tid, invoice, t["customer"], t["phone"], t["postal_code"] or "", t["address"],
             cust_use_no, today if real else "", box_qty, g.user["display_name"], ts, ts))
        ticket = dict(t)

    items = [{"name": _safe_label_text(
        f"[A/S반송] {ticket['ticket_no']}"
        + (f" {asset_no}" if asset_no else "")
        + (f" · {ticket['result']}" if ticket["result"] else ""))[:60], "qty": 1}]
    refine = None
    cj_response = None
    try:
        if real:
            invoice = cj2_new_invoice(cfg)
            if not invoice:
                raise RuntimeError("CJ 운송장 번호 채번에 실패했습니다.")
            refine = cj2_addr_refine(cfg, ticket["address"])
            cj_response = cj2_reg_book(
                cfg, kind="ship", sender=sender, receiver=receiver, items=items,
                cust_use_no=cust_use_no, rcpt_ymd=today, invc_no=invoice, box_qty=box_qty,
                box_type=cfg.get("box_type") or "01", frt_dv=cfg.get("frt_dv") or "03",
                remark=(ticket["symptom"] or "A/S 반송")[:60])
            if not cj_response.get("ok"):
                raise RuntimeError(f"CJ 접수 실패: {cj_response.get('detail') or cj_response.get('result_cd')}")
    except Exception as e:
        with tx(write=True) as conn:
            conn.execute("DELETE FROM waybills WHERE wid=? AND status='pending'", (wid,))
            audit.log("as_return_failed", target=f"{wid} {ticket['ticket_no']}", detail={"error": str(e)})
        abort(502, description=str(e))

    label = _cj2_label(invoice, today, sender, receiver, items, refine,
                       remark=(ticket["symptom"] or "")[:60], default_item="A/S 반송품",
                       **label_layout_of(cfg))
    with tx(write=True) as conn:
        ts = config.now_iso()
        conn.execute(
            "UPDATE waybills SET invoice_no=?, status=?, items=?, label=?, cj_response=?, updated_at=? "
            "WHERE wid=?",
            (invoice, "issued" if real else "test", label["item_summary"],
             json.dumps(label, ensure_ascii=False),
             json.dumps(cj_response, ensure_ascii=False) if cj_response else None, ts, wid))
        # 반송 송장이 나갔으면 수리는 끝난 것으로 본다(자산은 '반송 완료' 시 재고에서 빠진다)
        conn.execute("UPDATE as_tickets SET status='done', updated_at=? WHERE id=? AND status!='done'",
                     (ts, tid))
        conn.execute(
            "INSERT INTO as_events(ticket_id, ts, action, actor, detail) VALUES(?,?,?,?,?)",
            (tid, ts, "반송송장", g.user["display_name"],
             json.dumps({"wid": wid, "invoiceNo": invoice}, ensure_ascii=False)))
        audit.log("as_return_issued", target=f"{wid} {ticket['ticket_no']}",
                  detail={"invoiceNo": invoice, "simulated": not real})
    # 발송 안내 문자(송장번호 포함) — 트랜잭션 밖에서
    from flask import current_app

    from ..notify import notify_trigger
    ok, msg = notify_trigger(current_app, tid, "returned")
    return jsonify({"wid": wid, "invoiceNo": invoice, "simulated": not real,
                    "sms": {"ok": ok, "message": msg}}), 201


@bp.post("/waybills/manual")
def issue_manual_waybill():
    """주문·A/S 없이 송장 하나를 직접 만든다 — 배송/송장 ▸ [🆕 신규 등록](RMS 이식 2026-09-03).

    실무에서 시스템에 없는 발송이 생긴다(견본 발송, 부품만 보내기, 반품 회수 등).
    지금까지는 주문이나 A/S 건이 있어야만 송장이 나왔다.

    종류
      forward : 우리가 고객에게 보낸다(출고)
      recall  : 고객에게서 받아 온다(회수·반품 — 번호는 기사가 집화할 때 CJ가 만든다)
    예약(reserve=True)이면 **CJ를 부르지 않고** 우리 기록만 남긴다(RMS '⏳ 예약'과 같다).
    나중에 배송/송장 화면에서 실제 접수로 올린다.

    ★CJ 호출은 트랜잭션 밖에서(원칙 #1) — 발급/회수 경로와 같은 3단 구조.
    """
    require_any("orders.ship", "waybills.manage")
    body = request.get_json(silent=True) or {}
    kind = (body.get("type") or "forward").strip()
    if kind not in ("forward", "recall"):
        abort(400, description="종류는 출고(forward) 또는 회수(recall)만 됩니다.")
    reserve = body.get("reserve") is True
    name = (body.get("recipient") or "").strip()
    phone = (body.get("phone") or "").strip()
    addr = (body.get("address") or "").strip()
    addr_detail = (body.get("addressDetail") or "").strip()
    zipno = (body.get("postalCode") or "").strip()
    items_text = (body.get("items") or "").strip()
    memo = (body.get("memo") or "").strip()[:60]
    pickup_date = (body.get("pickupDate") or "").strip()
    box_qty = max(1, min(int(body.get("boxQty") or 1), 10))
    if not name or not addr:
        abort(400, description="받는 분 성함과 주소를 입력하세요.")

    with tx(write=True) as conn:
        cfg = _cj_settings(conn)
        # 회수만 수거 희망일을 쓴다 — 출고 송장에는 없는 값이라 검사도 회수일 때만
        if kind == "recall":
            check_pickup_date(cfg, pickup_date)
        real = _is_real(cfg) and not reserve
        sender = _sender(cfg)
        if real:
            if not phone:
                abort(400, description="실발행에는 전화번호가 필수입니다.")
            if not (sender.get("name") or "").strip() or not (sender.get("addr") or "").strip()                     or not (sender.get("tel") or "").strip():
                abort(400, description="실발행 전에 설정 ▸ API 관리에서 출고지 정보를 입력하세요.")
        today = config.today_str()
        wid = _next_wid(conn)
        ts = config.now_iso()
        cust_use_no = f"HBM{wid[-6:]}-{config.now().strftime('%H%M%S')}"
        # 예약은 CJ를 안 부르므로 번호가 없다. 테스트 발행이면 999 번호를 붙인다.
        invoice = "" if (real or reserve) else _next_test_invoice(conn)
        status = "reserved" if reserve else "pending"
        conn.execute(
            "INSERT INTO waybills(wid, order_id, as_ticket_id, type, cj_kind, invoice_no, status, "
            "recipient, phone, postal_code, address, items, cust_use_no, cj_rcpt_ymd, box_qty, "
            "note, created_by, created_at, updated_at) "
            "VALUES(?,NULL,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wid, kind, "ship" if kind == "forward" else "recall", invoice, status,
             name, phone, zipno, " ".join(x for x in (addr, addr_detail) if x),
             items_text, cust_use_no, today if real else "", box_qty, memo,
             g.user["display_name"], ts, ts))
        audit.log("waybill_manual_created",
                  target=wid, detail={"type": kind, "reserve": reserve, "recipient": name})
    if reserve:
        # 기록만 남기고 끝 — 화면이 '예약'으로 보여주고, 나중에 실제 접수로 올린다
        return jsonify({"wid": wid, "status": "reserved", "invoiceNo": "",
                        "message": "예약으로 저장했습니다 — CJ에는 아직 접수되지 않았습니다."})

    receiver = {"name": name, "tel": phone, "addr": addr,
                "addr_detail": addr_detail, "zip": zipno}
    items = [{"name": _safe_label_text(items_text or ("회수품" if kind == "recall" else "상품"))[:60],
              "qty": box_qty}]
    refine = None
    try:
        if real:
            if kind == "forward":
                invoice = cj2_new_invoice(cfg)
                if not invoice:
                    raise RuntimeError("CJ 운송장 번호 채번에 실패했습니다.")
                refine = cj2_addr_refine(cfg, addr)
                res = cj2_reg_book(
                    cfg, kind="ship", sender=sender, receiver=receiver, items=items,
                    cust_use_no=cust_use_no, rcpt_ymd=today, invc_no=invoice, box_qty=box_qty,
                    box_type=cfg.get("box_type") or "01", frt_dv=cfg.get("frt_dv") or "03",
                    remark=memo)
            else:
                # 회수는 번호를 우리가 넣지 않는다 — 기사가 집화할 때 CJ가 만든다
                res = cj2_reg_book(
                    cfg, kind="return", sender=sender, receiver=receiver, items=items,
                    cust_use_no=cust_use_no, rcpt_ymd=today, box_qty=box_qty,
                    box_type=cfg.get("box_type") or "01", frt_dv=cfg.get("frt_dv") or "03",
                    remark=memo, colct_ymd=pickup_date)
                invoice = ""
            if not res.get("ok"):
                raise RuntimeError(f"CJ 접수 실패: {res.get('detail') or res.get('result_cd')}")
    except Exception as e:                                        # noqa: BLE001
        with tx(write=True) as conn:
            conn.execute("DELETE FROM waybills WHERE wid=? AND status='pending'", (wid,))
            audit.log("waybill_manual_failed", target=wid, detail={"error": str(e)})
        abort(502, description=str(e))

    label = None
    if kind == "forward":
        label = _cj2_label(invoice, today, sender, receiver, items, refine,
                           remark=memo, default_item="상품")
    with tx(write=True) as conn:
        conn.execute(
            "UPDATE waybills SET invoice_no=?, status='issued', label=?, updated_at=? WHERE wid=?",
            (invoice, json.dumps(label, ensure_ascii=False) if label else "",
             config.now_iso(), wid))
        audit.log("waybill_manual_issued", target=f"{wid} {invoice or '(회수)'}",
                  detail={"type": kind, "real": real})
    return jsonify({"wid": wid, "status": "issued", "invoiceNo": invoice,
                    "message": "회수 접수했습니다 — 송장번호는 기사가 집화할 때 붙습니다."
                               if kind == "recall" else "송장을 발급했습니다."})


@bp.post("/waybills/print")
def waybills_print():
    """여러 송장을 한 PDF로 — 프린터 대화상자를 건마다 띄우지 않게 한다.

    ★라벨 렌더러(cj2_waybill_pdf)는 손대지 않는다. 그것이 만든 PDF를 합치기만 한다.
    """
    require_any("orders.ship", "waybills.manage", "shipping.view", "as.manage")
    from pypdf import PdfReader, PdfWriter

    body = request.get_json(silent=True) or {}
    wids = [str(w).strip() for w in (body.get("wids") or []) if str(w).strip()]
    if not wids:
        abort(400, description="인쇄할 송장을 선택하세요.")
    if len(wids) > 100:
        abort(400, description="한 번에 100건까지 인쇄할 수 있습니다.")

    conn = get_db()
    cfg = _cj_settings(conn)
    offsets = _cj_label_offset(cfg.get("label") or {})
    rows = {}
    for w in wids:                       # 담당 분류 검사를 건별로 그대로 태운다
        r = _waybill_in_scope(conn, w)
        if r is not None:
            rows[w] = r
    missing = [w for w in wids if w not in rows or not rows[w]["label"]]
    if missing:
        abort(404, description=f"라벨이 없는 송장이 있습니다: {', '.join(missing[:5])}")

    writer = PdfWriter()
    for wid in wids:                       # 화면에서 고른 순서를 그대로 지킨다
        pdf = cj2_waybill_pdf(json.loads(rows[wid]["label"]), True, *offsets)
        for page in PdfReader(io.BytesIO(pdf)).pages:
            writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    audit.log("waybills_printed", target=f"{len(wids)}건", detail={"wids": wids[:50]})
    return Response(buf.getvalue(), mimetype="application/pdf", headers={
        "Content-Disposition": f"inline; filename=ows-waybills-{config.today_str()}.pdf"})


@bp.post("/waybills/<wid>/cancel")
def cancel_waybill(wid):
    """송장 취소 — CJ 호출은 트랜잭션 밖에서(발급과 동일 원칙).

    body.forceLocal=true 면 CJ 예약 취소가 거절돼도 **우리 기록만** 취소한다.
    ★CJ에서 이미 취소·집화된 건은 CnclBook이 거절되는데, 그때 여기서도 막으면
      송장이 '발행'으로 남아 중복 가드에 걸려 재발급이 영영 안 된다(RMS의
      skip_cancel과 같은 탈출구). 사용자가 확인창을 거친 경우에만 보내온다.
    """
    # ★A/S 회수 예약은 A/S 담당자가 걸었으니 A/S 담당자가 무를 수 있어야 한다
    #   (2026-09-03 대표 "회수예약건 자체를 취소하는 기능도"). 예약은 as.manage 로 하는데
    #   취소만 배송 권한을 요구하면 건 사람이 못 문다. 범위는 'A/S 건에 딸린 회수 송장'
    #   하나로 좁힌다 — 출고 송장·주문 회수는 그대로 배송 권한이다.
    _pre = get_db().execute(
        "SELECT type, as_ticket_id FROM waybills WHERE wid=?", (wid,)).fetchone()
    if _pre and _pre["type"] == "recall" and _pre["as_ticket_id"]:
        require_any("orders.ship", "waybills.manage", "as.manage")
    else:
        require_any("orders.ship", "waybills.manage")
    body = request.get_json(silent=True) or {}
    force_local = bool(body.get("forceLocal"))
    with tx() as conn:
        row = conn.execute("SELECT * FROM waybills WHERE wid=?", (wid,)).fetchone()
        if row is None:
            abort(404, description="송장을 찾을 수 없습니다.")
        if row["status"] == "canceled":
            abort(400, description="이미 취소된 송장입니다.")
        # ★이미 배송완료된 회수는 물건이 우리 손에 들어온 것이다 — 예약을 무를 대상이 아니다.
        #   그대로 취소하면 접수 건이 '접수'로 되돌아가고 자산 흐름이 뒤집힌다.
        #   (배송/송장 화면도 delivered 에는 [취소]를 안 띄운다 — 같은 규칙을 서버에서 못 박는다.)
        if row["status"] == "delivered":
            abort(400, description="이미 배송(입고)이 끝난 건이라 예약을 취소할 수 없습니다. "
                                   "잘못 들어온 물건이면 A/S 건 상태를 직접 바꾸세요.")
        cfg = _cj_settings(conn)
        snapshot = dict(row)

    cj_cancel_failed = ""
    if snapshot["status"] == "issued":
        if not _is_real(cfg):
            abort(400, description="실발행 송장 취소는 CJ 설정(운영+무장)이 필요합니다.")
        label = json.loads(snapshot["label"]) if snapshot["label"] else {}
        # ★취소 PK에는 접수구분이 들어간다 — 회수(02)를 출고(01)로 취소하면 CJ가 건을 못 찾아
        #   실제 예약이 살아있는 채로 기사가 수거하러 온다(2026-07-28 리뷰 확인).
        kind = "return" if snapshot["type"] == "recall" else "ship"
        res = cj2_reg_book(
            cfg, kind=kind, sender=label.get("sender") or {}, receiver=label.get("receiver") or {},
            items=[], cust_use_no=snapshot["cust_use_no"], rcpt_ymd=snapshot["cj_rcpt_ymd"],
            invc_no=snapshot["invoice_no"], cancel=True,
        )
        if not res.get("ok"):
            if not force_local:
                abort(502, description=f"CJ 예약 취소 실패: {res.get('detail') or res.get('result_cd')}")
            cj_cancel_failed = f"{res.get('result_cd')} {res.get('detail') or ''}".strip()

    with tx(write=True) as conn:
        ts = config.now_iso()
        conn.execute("UPDATE waybills SET status='canceled', updated_at=? WHERE wid=?", (ts, wid))
        restored = []
        if snapshot["type"] == "recall":
            # 회수 취소 — 자산이 '회수중'에 고착되지 않게 직전 상태로 되돌린다
            from .recall import _restore_from_recall
            w = conn.execute("SELECT * FROM waybills WHERE wid=?", (wid,)).fetchone()
            restored = _restore_from_recall(conn, w)
            if snapshot["as_ticket_id"]:
                conn.execute("UPDATE as_tickets SET status='received', updated_at=? "
                             "WHERE id=? AND status='collecting'", (ts, snapshot["as_ticket_id"]))
                # A/S 상세의 이력에 남는다 — 언제 누가 예약을 물렀는지 그 건에서 바로 보여야 한다
                conn.execute(
                    "INSERT INTO as_events(ticket_id, ts, action, actor, detail) VALUES(?,?,?,?,?)",
                    (snapshot["as_ticket_id"], ts, "회수예약취소",
                     (g.user or {}).get("display_name", "") if isinstance(g.user, dict)
                     else g.user["display_name"],
                     json.dumps({"wid": wid, "송장번호": snapshot["invoice_no"] or "",
                                 **({"CJ취소실패": cj_cancel_failed} if cj_cancel_failed else {})},
                                ensure_ascii=False)))
        elif snapshot["order_id"]:
            o = conn.execute("SELECT tracking_no FROM orders WHERE id=?", (snapshot["order_id"],)).fetchone()
            if o and o["tracking_no"] == snapshot["invoice_no"]:
                conn.execute("UPDATE orders SET tracking_no='', updated_at=? WHERE id=?",
                             (ts, snapshot["order_id"]))
        audit.log("waybill_canceled", target=wid,
                  detail={"invoiceNo": snapshot["invoice_no"], "type": snapshot["type"],
                          "restoredAssets": restored,
                          **({"cjCancelFailed": cj_cancel_failed} if cj_cancel_failed else {})})
    return jsonify({"ok": True, "restoredAssets": restored,
                    "cjCancelFailed": cj_cancel_failed or None})
