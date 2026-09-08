"""판매 전표 직접 등록·수정 — OWS가 판매 전표의 등록 창구가 된다(2026-09-02 대표 방침).

지금까지 전표 단위 판매(전표 + 자산별 판매가·옵션가·정산·입금)는 TMS 화면에 입력하고
OWS는 연동으로 받아 sale_slips(전표)·tms_sales(자산별 명세)에 읽기 전용으로 쌓기만 했다.
이제 같은 두 표를 원장으로 쓰되 OWS에서 직접 만들고 고친다. 설계 근거는 docs/SALE_ENTRY.md.

요점
  - 번호대: S{YYMMDD}-{NNN}, 일련 500부터. TMS 001~499와 나뉜다(매입 전표 next_slip_no 와 같은 규칙).
  - 자산 상태: 라인 추가 = 판매 확정. TMS의 '판매'가 자산을 shipped 로 옮기던 규칙
    (migration.AUTO_STATUS_TO/FROM)과 똑같이 작업·재고 상태(AUTO_STATUS_FROM)에서만 shipped 로 옮기고
    '판매' 이력 + 몰 재고 전송 해제(_unlist)까지 한다. reserved/returning(주문 흐름)·살아 있는 주문이
    잡은 자산·렌탈 귀속·매입취소/거래처반품/폐기·가입고는 거부한다.
  - 반입·취소: 자동 복귀 없음(검수 없이 재고가 되살아나면 유령 재고). '복귀후보' 이력만 남겨
    매입 ▸ 자산 ▸ ↩ 복귀 후보에서 사람이 복귀한다. restock=true 를 명시하면 기존 복귀 규칙 그대로
    (in_stock·실재고·received=1·'재고복귀' 이력) 즉시 복귀한다.
  - 물리 삭제 금지: 라인 제거 = stage '판매취소' + 상세취소일, 전표 취소 = stage '판매취소' + 취소일·사유.
  - 되돌리기(2026-09-03): 전표 취소 되돌리기 / 라인 반입 취소 / 라인 판매취소 되돌리기 — OWS에서 한
    취소·반입만(ows_edited_at·cancel_date 표시). 연동 전표의 취소·반입은 TMS 값이 정본이라 409.
    자산은 판매 확정 상태로 되돌리되, 그 사이 다른 전표·주문이 잡았으면 409(전체 롤백).
  - ★순이익 산식 = TMS 판매상세 실측(2026-09-03, docs/SALE_ENTRY.md §1-6). 옵션가는 매출이 아니라
    '부품 원가'다: 부품옵션합계 = 업1+업2+기타구성+충전기 − 탈거(회수). 총원가 = 자산 원가 + 옵션 원가 + 포장료.
    판매부가세 = 판매가×10%, 매입부가세 = 총원가×10%, 실부가세 = 둘의 차, 판매수수료 = 판매가×수수율(기본 3%),
    순이익 = 판매가 − 총원가 − 판매수수료 − 실부가세.
  - 합계 자동: OWS 전표(source='ows')는 살아 있는 라인에서 수량·명세합계·매입금액·순이익을 다시 센다.
    연동 전표(TMS)는 헤더 값을 TMS가 정한 것이라 다시 세지 않는다(고친 칸만 저장).
  - 모든 쓰기: audit.log + asset_events(자산 라인 변경) + ows_edited_at/by.
"""
import math

from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require, require_any
from ..db import DIVISION_RENTAL, tx
from . import (ASSET_STATUSES, AVAILABLE_STATUSES, OWS_SLIP_SEQ_START, _unlist, asset_event, bp)
from .migration import AUTO_STATUS_FROM, _to_date
from .sales import DEAD_STAGES

# ---------------------------------------------------------------- 칸 정의
# 헤더(전표) — 화면 키 → 컬럼
HEAD_TEXT = {"channel": "channel", "customer": "customer", "memo": "memo", "waybill": "waybill"}
HEAD_DATE = {"saleDate": "sale_date", "shipDate": "ship_date", "returnDate": "return_date"}
HEAD_MONEY = {"vat": "vat", "fee": "fee", "shipping": "shipping", "cod": "cod"}
HEAD_PAID = {"paidStatus": "paid_status", "paidAt": "paid_at",
             "paidAmount": "paid_amount", "paidConfirmed": "paid_confirmed"}
HEAD_LABEL = {"vat": "부가세", "fee": "수수료", "shipping": "배송비", "cod": "착불",
              "paidAmount": "입금액", "saleAmount": "판매금액"}
# 라인(자산별 명세)
LINE_PRICE = {"salePrice": "sale_price"}                                  # 0 이상 — 그 자산의 매출 전부
# 옵션 원가(TMS 판매상세 칸). ★매출이 아니라 원가다 — 판매가에 이미 포함된 구성의 부품값.
#   탈거가는 '회수 원가'(양수 = 원가에서 뺀다, TMS 관례). 음수는 반대(원가에 더함).
LINE_OPTION = {"upgrade1Price": "upgrade1_price", "upgrade2Price": "upgrade2_price",
               "removalPrice": "removal_price", "extraPrice": "extra_price",
               "chargerPrice": "charger_price", "packingFee": "packing_fee"}   # 음수 허용
# 정산 칸 — 안 보내면 규칙으로 계산(판매가×10% / 판매가×수수율), 보내면 그 값(면세·특약 등)
LINE_SETTLE = {"saleVat": "sale_vat", "saleFee": "sale_fee"}
LINE_TEXT = {"upgrade1Item": "upgrade1_item", "upgrade2Item": "upgrade2_item",
             "removalItem": "removal_item", "extraItems": "extra_items",
             "recipient": "customer", "memo": "memo"}
LINE_LABEL = {"salePrice": "판매가", "upgrade1Price": "업그레이드1가", "upgrade2Price": "업그레이드2가",
              "removalPrice": "탈거가", "extraPrice": "기타구성가", "chargerPrice": "충전기가",
              "packingFee": "포장료", "saleVat": "판매부가세", "saleFee": "판매수수료", "feeRate": "판매수수율"}
# 옵션 원가에 '더하는' 칸 — 탈거가(removal_price)는 뺀다. 화면 SALE_OPTION_COST_KEYS 와 같은 목록(시험 고정).
LINE_OPTION_COST_COLS = ("upgrade1_price", "upgrade2_price", "extra_price", "charger_price")
# TMS 실측 상수(2026-09-03, 판매상세 7,703행): 판매부가세 = 판매가×10%(99.8%), 매입부가세 = 총원가×10%(97.0%),
#   판매수수율 3%(95%) 아니면 0%. 화면(purchase.js SALE_VAT_RATE/SALE_FEE_RATE)과 같은 값이어야 한다.
SALE_VAT_RATE = 10
SALE_FEE_RATE_DEFAULT = 3.0
LIVE_STAGE = "판매"
# 연동 전표 헤더의 상태를 사람이 바꿀 때 허용하는 값(OWS 전표는 라인에서 자동으로 정해진다)
TMS_HEAD_STAGES = ("판매", "판매완료", "반입")


def is_ows_slip(row):
    """OWS가 만든 전표인가. 그 외('TMS이관'·'tms' 등)는 전부 연동 전표로 본다."""
    return (row["source"] or "") == "ows"


def _ows_touched(row):
    """OWS가 만들었거나 고친 전표인가 — 되돌리기 대상의 1차 조건."""
    return is_ows_slip(row) or bool(row["ows_edited_at"])


def halfup(x):
    """TMS 반올림(.5 올림). 판매수수료 .5 경계 12행 전부 올림이었다(파이썬 round 는 5행만 일치)."""
    return int(math.floor(x + 0.5))


def next_sale_slip_no(conn):
    """판매 전표번호 = S + YYMMDD-NNN. 번호대는 OWS 몫(500~999) — TMS(001~499)와 안 겹친다."""
    prefix = f"S{config.now().strftime('%y%m%d')}-"
    row = conn.execute(
        "SELECT MAX(CAST(substr(slip_no, 9) AS INTEGER)) AS m FROM sale_slips "
        "WHERE slip_no GLOB ? AND CAST(substr(slip_no, 9) AS INTEGER) >= ?",
        (prefix + "[0-9][0-9][0-9]", OWS_SLIP_SEQ_START)).fetchone()
    seq = (row["m"] + 1) if row["m"] else OWS_SLIP_SEQ_START
    if seq > 999:
        abort(400, description="당일 판매 전표번호 발번 한도(999)를 초과했습니다.")
    return f"{prefix}{seq:03d}"


# ---------------------------------------------------------------- 권한·파서
def _read_gate():
    """목록(GET /sale-slips)과 같은 문턱 — 매입 접근 + 금액 열람 계열 권한."""
    require("purchase.view")
    require_any("purchase.money", "reports.view", "settings.manage")


def _write_gate():
    require("purchase.edit")
    require_any("purchase.money", "reports.view", "settings.manage")


def _actor():
    return g.user["display_name"] if g.get("user") else ""


def _money(v, what, allow_negative=False):
    """금액 칸 — '1,000'·''·None 을 받아 정수로. 음수는 옵션가(차감)에서만 허용."""
    if v is None or v == "" or v is False:
        return 0
    try:
        n = int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        abort(400, description=f"{what}이(가) 올바르지 않습니다: {v!r}")
    if n < 0 and not allow_negative:
        abort(400, description=f"{what}은(는) 0보다 작을 수 없습니다: {n}")
    return n


def _rate(v, default):
    """수수율(%) — 비우면 기본값, 0~100."""
    if v is None or v == "" or v is False:
        return float(default)
    try:
        r = float(str(v).replace("%", "").strip())
    except (TypeError, ValueError):
        abort(400, description=f"판매수수율이 올바르지 않습니다: {v!r}")
    if r < 0 or r > 100:
        abort(400, description=f"판매수수율은 0~100 사이여야 합니다: {r}")
    return r


def _head_fields(body, partial):
    """헤더 입력 → 컬럼 dict. partial=True 면 보낸 칸만."""
    out = {}
    for k, col in HEAD_TEXT.items():
        if k in body or not partial:
            out[col] = str(body.get(k) or "").strip()[:300]
    for k, col in HEAD_DATE.items():
        if k in body or not partial:
            out[col] = _to_date(body.get(k))
    for k, col in HEAD_MONEY.items():
        if k in body or not partial:
            out[col] = _money(body.get(k), HEAD_LABEL[k])
    if "paidStatus" in body or not partial:
        out["paid_status"] = str(body.get("paidStatus") or "").strip()[:20]
    if "paidAt" in body or not partial:
        out["paid_at"] = str(body.get("paidAt") or "").strip()[:19]
    if "paidAmount" in body or not partial:
        out["paid_amount"] = _money(body.get("paidAmount"), "입금액")
    if "paidConfirmed" in body or not partial:
        out["paid_confirmed"] = 1 if body.get("paidConfirmed") else 0
    return out


def _line_fields(item, partial, line=None):
    """라인 입력 → 컬럼 dict. partial=True 면 보낸 칸만(line = 지금 저장된 행).

    정산 칸(판매부가세·판매수수료)은 ★TMS 규칙으로 계산한다 — 판매가×10%, 판매가×수수율.
    보낸 값이 있으면 그 값을 쓴다(면세·특약). 판매가나 수수율이 바뀌면 안 보낸 정산 칸을 다시 센다.
    """
    out = {}
    for k, col in LINE_PRICE.items():
        if k in item or not partial:
            out[col] = _money(item.get(k), LINE_LABEL[k])
    for k, col in LINE_OPTION.items():
        if k in item or not partial:
            out[col] = _money(item.get(k), LINE_LABEL[k], allow_negative=True)
    for k, col in LINE_TEXT.items():
        if k in item or not partial:
            out[col] = str(item.get(k) or "").strip()[:200]
    if "feeRate" in item or not partial:
        out["fee_rate"] = _rate(item.get("feeRate"),
                                line["fee_rate"] if (partial and line is not None) else SALE_FEE_RATE_DEFAULT)
    price = out["sale_price"] if "sale_price" in out else int(line["sale_price"] or 0)
    rate = out["fee_rate"] if "fee_rate" in out else float(line["fee_rate"] or 0)
    if "saleVat" in item:
        out["sale_vat"] = _money(item.get("saleVat"), LINE_LABEL["saleVat"])
    elif not partial or "sale_price" in out:
        out["sale_vat"] = halfup(price * SALE_VAT_RATE / 100)
    if "saleFee" in item:
        out["sale_fee"] = _money(item.get("saleFee"), LINE_LABEL["saleFee"])
    elif not partial or "sale_price" in out or "fee_rate" in out:
        out["sale_fee"] = halfup(price * rate / 100)
    return out


# ---------------------------------------------------------------- 조회 헬퍼
def _get_slip(conn, slip_no):
    row = conn.execute("SELECT * FROM sale_slips WHERE slip_no=?", (slip_no,)).fetchone()
    if row is None:
        abort(404, description="판매 전표를 찾을 수 없습니다.")
    return row


def _live_slip(conn, slip_no):
    slip = _get_slip(conn, slip_no)
    if slip["stage"] == "판매취소":
        abort(400, description="취소된 전표는 고칠 수 없습니다.")
    return slip


def _get_line(conn, slip_no, asset_no):
    row = conn.execute("SELECT * FROM tms_sales WHERE slip_no=? AND asset_no=?",
                       (slip_no, asset_no)).fetchone()
    if row is None:
        abort(404, description="전표에 그 자산 라인이 없습니다.")
    return row


_ASSET_HOLD_SQL = (
    "SELECT a.*, "
    "  (SELECT o.order_no FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
    "   WHERE oa.asset_id=a.id AND o.cancelled_at='' LIMIT 1) AS held_order, "
    "  (SELECT COALESCE(SUM(r.cost),0) FROM asset_repairs r WHERE r.asset_id=a.id) AS repair_sum, "
    "  b.slip_no AS buy_slip, COALESCE(rep.name, s.name) AS supplier_name "
    "FROM assets a LEFT JOIN purchase_batches b ON b.id=a.batch_id "
    "LEFT JOIN suppliers s ON s.id=b.supplier_id "
    "LEFT JOIN suppliers rep ON rep.id=s.alias_of ")      # 별칭 거래처는 대표 이름(2026-09-03, masters.py)


def _asset_with_hold(conn, asset_no=None, asset_id=None):
    if asset_id is not None:
        return conn.execute(_ASSET_HOLD_SQL + "WHERE a.id=?", (asset_id,)).fetchone()
    return conn.execute(_ASSET_HOLD_SQL + "WHERE a.asset_no=?", (asset_no,)).fetchone()


def _live_line_elsewhere(conn, asset_no, exclude_id=None):
    """이 자산이 살아 있는 라인(취소·반입 아님)으로 있는 다른 전표. 전표 자체가 취소된 것은 제외.

    ★tms_sales 에는 sale_slips 에 없는 전표(판매현황만 온 것)도 있어 LEFT JOIN 으로 본다.
    """
    return conn.execute(
        "SELECT t.slip_no FROM tms_sales t LEFT JOIN sale_slips s ON s.slip_no=t.slip_no "
        "WHERE t.asset_no=? AND t.stage NOT IN (?,?) AND t.stage NOT LIKE '%취소%' "
        "AND COALESCE(s.stage,'') <> '판매취소' AND t.id <> ? "
        "ORDER BY t.id DESC LIMIT 1", (asset_no,) + DEAD_STAGES + (exclude_id or 0,)).fetchone()


def _check_asset(conn, asset_no, slip_no=None):
    """라인에 넣을 수 있는 자산인가. (자산행|None, 거부사유, 경고) 반환.

    ★거부 = 두 번 팔리거나 우리 물건이 아닌 경우. 경고 = 넣되 사람이 알아야 하는 것.
    """
    a = _asset_with_hold(conn, asset_no)
    if a is None:
        return None, "등록되지 않은 자산번호입니다.", ""
    st = a["status"] or ""
    label = ASSET_STATUSES.get(st, st)
    if (a["division"] or "sale") == DIVISION_RENTAL:
        return a, "렌탈 사업부 귀속 자산입니다 — 판매 전표에 넣을 수 없습니다(사업부 이관 후 가능).", ""
    if st in ("cancelled", "returned"):
        return a, f"{label} 상태 — 우리 재고가 아닙니다.", ""
    if st == "scrapped":
        return a, "폐기된 자산입니다 — 재고 복귀 후 등록하세요.", ""
    if st in ("reserved", "returning"):
        where = f"(주문 {a['held_order']})" if a["held_order"] else ""
        return a, f"{label} — 주문 흐름이 잡고 있는 자산입니다{where}.", ""
    if a["held_order"]:
        return a, f"OWS 주문 {a['held_order']}이(가) 잡고 있는 자산입니다 — 두 번 팔 수 없습니다.", ""
    if not a["received"]:
        return a, "아직 입고확인이 안 된 자산입니다(가입고).", ""
    # 살아 있는 라인(취소·반입 아님)에 이미 있으면 거부 — 전표 자체가 취소된 것은 제외.
    live = _live_line_elsewhere(conn, asset_no)
    if live:
        if slip_no and live["slip_no"] == slip_no:
            return a, "이미 이 전표에 들어 있습니다.", ""
        return a, (f"전표 {live['slip_no']}에 이미 판매로 기록된 자산입니다 — "
                   "그 전표에서 반입·취소하기 전에는 다시 팔 수 없습니다."), ""
    warns = []
    if a["tms_deleted_at"]:
        warns.append("TMS에서 삭제 표시된 자산입니다.")
    if st == "shipped":
        warns.append("이미 출고완료 상태입니다 — 상태는 그대로 두고 판매 기록만 남깁니다.")
    elif st not in AVAILABLE_STATUSES:
        warns.append(f"{label} 상태인데 판매로 등록합니다 — 출고완료로 바뀝니다.")
    if (a["tier"] or "") == "가재고":
        warns.append("가재고(불용·짜깁기) 자산입니다.")
    return a, "", " ".join(warns)


# ---------------------------------------------------------------- 자산 상태
def _ship_asset(conn, a, slip, price, ts):
    """라인 추가 = 판매 확정. TMS '판매' 자동 추종과 같은 집합(AUTO_STATUS_FROM)에서만 shipped 로."""
    prev = a["status"] or ""
    changed = False
    if prev in AUTO_STATUS_FROM:
        changed = bool(conn.execute(
            "UPDATE assets SET status='shipped', updated_at=? WHERE id=? AND status=?",
            (ts, a["id"], prev)).rowcount)
    asset_event(conn, a["id"], "판매", {
        "판매일": slip["sale_date"], "수령자": "", "채널": slip["channel"],
        "판매처": slip["customer"], "판매전표": slip["slip_no"], "판매가": price,
        "이전상태": prev, "상태": "shipped" if changed else prev, "출처": "OWS 전표",
    })
    _unlist(conn, a["id"], f"판매(전표 {slip['slip_no']})")
    return changed


def _release_asset(conn, asset_id, kind, slip_no, restock, reason, ts, when=""):
    """반입/판매취소 뒤 자산 처리 — 기본은 '복귀후보'만, restock 이면 기존 복귀 규칙 그대로."""
    if asset_id is None:
        return {"assetNo": "", "status": "", "restocked": False, "candidate": False, "heldOrder": ""}
    a = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if a is None:
        return {"assetNo": "", "status": "", "restocked": False, "candidate": False, "heldOrder": ""}
    label = "반입" if kind == "return" else "판매취소"
    detail = {"판매전표": slip_no, "사유": reason, "출처": "OWS 전표"}
    if kind == "return":
        detail["반입일"] = when
    asset_event(conn, asset_id, label, detail)
    held = conn.execute(
        "SELECT o.order_no FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
        "WHERE oa.asset_id=? AND o.cancelled_at='' LIMIT 1", (asset_id,)).fetchone()
    out = {"assetNo": a["asset_no"], "status": a["status"], "restocked": False,
           "candidate": False, "heldOrder": held["order_no"] if held else ""}
    # 출고/폐기 상태만 되돌릴 게 있다. 살아 있는 주문이 잡고 있으면 주문 흐름 몫이라 손대지 않는다.
    if a["status"] not in ("shipped", "scrapped") or held:
        return out
    if restock:
        conn.execute(
            "UPDATE assets SET status='in_stock', tier='실재고', received=1, updated_at=? "
            "WHERE id=? AND status=?", (ts, asset_id, a["status"]))
        asset_event(conn, asset_id, "재고복귀",
                    {"from": a["status"], "to": "in_stock", "재고구분": "실재고",
                     "사유": f"{label}(전표 {slip_no}) {reason}".strip()})
        out["restocked"], out["status"] = True, "in_stock"
        return out
    # ↩ 복귀 후보 대기열과 같은 판정 — 열려 있거나 무시된 자산에는 또 쌓지 않는다
    last = conn.execute(
        "SELECT action FROM asset_events WHERE asset_id=? AND action IN "
        "('복귀후보','복귀후보종결','재고복귀') ORDER BY id DESC LIMIT 1", (asset_id,)).fetchone()
    if not last or last["action"] == "재고복귀":
        asset_event(conn, asset_id, "복귀후보",
                    {"TMS표기": f"{label}(OWS 전표 {slip_no})", "당시상태": a["status"]})
    out["candidate"] = True
    return out


# ---------------------------------------------------------------- 되돌리기(2026-09-03)
def _restore_guard(conn, a, line):
    """되돌리려는 라인의 자산이 그 사이 다른 데 잡혔는가 — 잡혔으면 사유(409), 아니면 ''."""
    st = a["status"] or ""
    label = ASSET_STATUSES.get(st, st)
    if (a["division"] or "sale") == DIVISION_RENTAL:
        return "그 사이 렌탈 사업부로 귀속됐습니다."
    if st in ("cancelled", "returned"):
        return f"그 사이 {label} 처리돼 우리 재고가 아닙니다."
    if st in ("reserved", "returning"):
        where = f"(주문 {a['held_order']})" if a["held_order"] else ""
        return f"그 사이 주문 흐름이 잡았습니다{where}."
    if a["held_order"]:
        return f"그 사이 OWS 주문 {a['held_order']}이(가) 잡았습니다."
    other = _live_line_elsewhere(conn, line["asset_no"], exclude_id=line["id"])
    if other:
        return f"그 사이 전표 {other['slip_no']}에 다시 팔렸습니다."
    return ""


def _restore_asset(conn, a, kind, slip_no, price, ts):
    """취소·반입 되돌리기 뒤 자산 = 판매 확정 상태 복원(_ship_asset 과 대칭)."""
    prev = a["status"] or ""
    changed = False
    if prev in AUTO_STATUS_FROM:
        changed = bool(conn.execute(
            "UPDATE assets SET status='shipped', updated_at=? WHERE id=? AND status=?",
            (ts, a["id"], prev)).rowcount)
    asset_event(conn, a["id"], "판매취소해제" if kind == "cancel" else "반입취소", {
        "판매전표": slip_no, "판매가": price, "이전상태": prev,
        "상태": "shipped" if changed else prev, "출처": "OWS 전표",
    })
    _unlist(conn, a["id"], f"판매 복원(전표 {slip_no})")
    # 열려 있던 ↩ 복귀 후보는 닫는다 — 되팔린 게 아니라 판매가 되살아난 것이라 대기열에 남으면 안 된다
    last = conn.execute(
        "SELECT action FROM asset_events WHERE asset_id=? AND action IN "
        "('복귀후보','복귀후보종결','재고복귀') ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
    if last and last["action"] == "복귀후보":
        asset_event(conn, a["id"], "복귀후보종결", {"처리": "판매 복원", "판매전표": slip_no})
    out = {"assetNo": a["asset_no"], "status": "shipped" if changed else prev, "statusChanged": changed,
           "warning": ""}
    if prev == "scrapped":
        out["warning"] = "폐기 상태라 자산 상태는 그대로 두고 판매 기록만 되살렸습니다."
    return out


def _restore_line(conn, slip_no, line, kind, ts):
    """라인 하나를 살아 있는 상태로 — kind: 'cancel'(판매취소→) | 'return'(반입→). 자산이 잡혔으면 409."""
    a = None
    if line["asset_id"] is not None:
        a = _asset_with_hold(conn, asset_id=line["asset_id"])
        if a is not None:
            err = _restore_guard(conn, a, line)
            if err:
                abort(409, description=f"{line['asset_no']}: {err} 되돌릴 수 없습니다.")
    back = line["prev_stage"] or LIVE_STAGE
    clear = "cancel_date=''" if kind == "cancel" else "return_date=''"
    conn.execute(
        f"UPDATE tms_sales SET stage=?, prev_stage='', cancel_kind='', {clear}, ows_edited_at=?, updated_at=? "
        "WHERE id=?", (back, ts, ts, line["id"]))
    if a is None:
        return {"assetNo": line["asset_no"], "status": "", "statusChanged": False, "warning": ""}
    return _restore_asset(conn, a, kind, slip_no, int(line["sale_price"] or 0), ts)


# ---------------------------------------------------------------- 합계·기록
def _touch(conn, slip_no, ts):
    conn.execute("UPDATE sale_slips SET ows_edited_at=?, ows_edited_by=?, updated_at=? WHERE slip_no=?",
                 (ts, _actor(), ts, slip_no))


def _slip_lines(conn, slip_no):
    return conn.execute(
        "SELECT t.*, a.id AS aid, a.purchase_price AS ows_price, a.status AS a_status, "
        "  a.division AS a_division, a.maker AS a_maker, a.model AS a_model, a.grade AS a_grade, "
        "  a.tms_deleted_at AS a_deleted, "
        "  (SELECT COALESCE(SUM(r.cost),0) FROM asset_repairs r WHERE r.asset_id=t.asset_id) AS repair_sum, "
        "  (SELECT o.order_no FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
        "   WHERE oa.asset_id=t.asset_id AND o.cancelled_at='' LIMIT 1) AS held_order "
        "FROM tms_sales t LEFT JOIN assets a ON a.id=t.asset_id "
        "WHERE t.slip_no=? ORDER BY t.id", (slip_no,)).fetchall()


def _line_cost(l):
    """이 라인의 원가.

    OWS 라인 = 자산 매입가 + 수리·부품비(지금 값). 우리가 만든 전표라 지금 값이 정본이다.
    연동(TMS) 라인 = **판매 시점 제조원가**가 정본이다(2026-09-03 대표 승인으로 적재).
      ★자산의 지금 매입가로 계산하면 안 된다 — 매입가는 나중에 고쳐지고(라이브 151행 이미 다름)
        수리·부품비는 팔린 뒤에도 붙어서, 지난달 마진이 오늘 조용히 바뀐다.
      제조원가가 아직 안 들어온 옛 행은 예전처럼 명세의 매입가로 물러선다.
    """
    keys = l.keys() if hasattr(l, "keys") else l
    if (l["source"] or "") != "ows" and "manufacture_cost" in keys and (l["manufacture_cost"] or 0) > 0:
        return l["manufacture_cost"]
    if l["aid"] is not None and (l["ows_price"] or 0) > 0:
        return (l["ows_price"] or 0) + (l["repair_sum"] or 0)
    return l["purchase_price"] or 0


def _line_calc(l):
    """라인 순이익 — TMS 판매상세 산식(실측 2026-09-03).

      부품옵션합계 = 업1 + 업2 + 기타구성 + 충전기 − 탈거(회수)
      총원가       = 자산 원가(매입가+수리·부품비 ↔ TMS 제조원가) + 부품옵션합계 + 포장료
      실부가세     = 판매부가세(판매가×10%) − 매입부가세(총원가×10%)
      순이익       = 판매가 − 총원가 − 판매수수료 − 실부가세
    OWS 라인은 전부 규칙으로 센다. 연동(TMS) 라인은 TMS가 준 순이익이 정본이라 있으면 그 값을 쓰고,
    없으면(옛 행·미계산 0) 저장된 정산 칸으로 같은 식을 돌린다(profitSrc 로 구분).
    """
    ows = (l["source"] or "") == "ows"
    price = int(l["sale_price"] or 0)
    option = sum(int(l[c] or 0) for c in LINE_OPTION_COST_COLS) - int(l["removal_price"] or 0)
    packing = int(l["packing_fee"] or 0)
    asset_cost = int(_line_cost(l))
    total_cost = asset_cost + option + packing
    sale_vat = int(l["sale_vat"] or 0)
    sale_fee = int(l["sale_fee"] or 0)
    buy_vat = halfup(total_cost * SALE_VAT_RATE / 100) if ows else int(l["buy_vat"] or 0)
    net_vat = sale_vat - buy_vat
    profit, src = price - total_cost - sale_fee - net_vat, "ows"
    if not ows and int(l["tms_profit"] or 0):
        profit, src = int(l["tms_profit"]), "tms"
    return {"price": price, "option": option, "packing": packing, "asset_cost": asset_cost,
            "total_cost": total_cost, "sale_vat": sale_vat, "buy_vat": buy_vat, "net_vat": net_vat,
            "sale_fee": sale_fee, "fee_rate": float(l["fee_rate"] or 0), "profit": profit, "src": src}


def _recompute(conn, slip_no, ts):
    """OWS 전표의 헤더 합계를 살아 있는 라인에서 다시 센다. 연동 전표·취소 전표는 건드리지 않는다.

    sale_amount = 명세합계(Σ판매가) + diff_amount. diff_amount 는 사람이 판매금액을 직접 적었을 때의
    차액(할인·끝수)이라 라인이 바뀌어도 그대로 얹힌다(TMS의 '판매차이금액'과 같은 뜻).
    ★순이익액(profit) = Σ라인 순이익 − 헤더 부가세·수수료·배송비. TMS 실측: 순이익액 = Σ상세 순이익이고
      판매차이금액은 순이익액에 안 들어간다(구형 전표 96% 일치). 헤더 부가세·수수료·배송비는 TMS에서 늘 0이라
      OWS가 받으면 그대로 뺀다(0이면 TMS와 같다).
    """
    slip = _get_slip(conn, slip_no)
    if not is_ows_slip(slip) or slip["stage"] == "판매취소":
        return
    lines = _slip_lines(conn, slip_no)
    live = [l for l in lines if l["stage"] not in DEAD_STAGES]
    calcs = {l["id"]: _line_calc(l) for l in live}
    item_sum = sum(c["price"] for c in calcs.values())
    buy = sum(int(l["ows_price"] or 0) if l["aid"] is not None else int(l["purchase_price"] or 0)
              for l in live)
    item_profit = sum(c["profit"] for c in calcs.values())
    sale_amount = item_sum + int(slip["diff_amount"] or 0)
    profit = item_profit - int(slip["vat"] or 0) - int(slip["fee"] or 0) - int(slip["shipping"] or 0)
    returned = [l for l in lines if l["stage"] == "반입"]
    if lines and not live and returned:
        stage, ret = "반입", max((l["return_date"] or "") for l in returned)
    else:
        stage, ret = LIVE_STAGE, slip["return_date"] if live else ""
    conn.execute(
        "UPDATE sale_slips SET qty=?, head_qty=?, item_sale_sum=?, purchase_amount=?, sale_amount=?, "
        "profit=?, item_profit=?, stage=?, return_date=?, updated_at=? WHERE slip_no=?",
        (len(live), len(live), item_sum, buy, sale_amount, profit, item_profit, stage, ret, ts, slip_no))
    for lid, c in calcs.items():
        conn.execute("UPDATE tms_sales SET tms_profit=?, buy_vat=? WHERE id=?", (c["profit"], c["buy_vat"], lid))


def _set_manual_total(conn, slip, amount, ts):
    """판매금액을 사람이 직접 적었다 — 차액을 diff_amount 로 남긴다."""
    lines = _slip_lines(conn, slip["slip_no"])
    item_sum = sum(int(l["sale_price"] or 0) for l in lines if l["stage"] not in DEAD_STAGES)
    conn.execute("UPDATE sale_slips SET sale_amount=?, item_sale_sum=?, diff_amount=?, updated_at=? "
                 "WHERE slip_no=?", (amount, item_sum, amount - item_sum, ts, slip["slip_no"]))


def _insert_line(conn, slip, a, f, ts):
    cols = {
        "slip_no": slip["slip_no"], "asset_no": a["asset_no"], "asset_id": a["id"],
        "channel": slip["channel"], "seller": slip["customer"],
        "model": " ".join(x for x in (a["maker"], a["model"]) if x),
        "sale_date": slip["sale_date"], "ship_date": slip["ship_date"],
        "purchase_price": a["purchase_price"] or 0, "grade": a["grade"] or "",
        "stage": LIVE_STAGE, "purchase_slip": a["buy_slip"] or "",
        "supplier_name": a["supplier_name"] or "",
        "source": "ows", "ows_edited_at": ts, "created_at": ts, "updated_at": ts,
    }
    cols.update(f)
    conn.execute(
        f"INSERT INTO tms_sales({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        list(cols.values()))
    return _ship_asset(conn, a, slip, int(cols.get("sale_price") or 0), ts)


def _line_payload(l):
    c = _line_calc(l)
    dead = l["stage"] in DEAD_STAGES
    return {
        "id": l["id"], "assetNo": l["asset_no"], "assetId": l["asset_id"],
        "model": l["model"] or " ".join(x for x in (l["a_maker"], l["a_model"]) if x),
        "grade": l["grade"] or l["a_grade"] or "",
        "status": l["a_status"] or "", "statusLabel": ASSET_STATUSES.get(l["a_status"] or "", l["a_status"] or ""),
        "division": l["a_division"] or "", "tmsDeletedAt": l["a_deleted"] or "",
        "heldOrder": l["held_order"] or "",
        "recipient": l["customer"], "memo": l["memo"], "stage": l["stage"],
        "saleDate": (l["sale_date"] or "")[:10], "shipDate": (l["ship_date"] or "")[:10],
        "returnDate": l["return_date"], "cancelDate": l["cancel_date"],
        "cancelKind": l["cancel_kind"] or "", "prevStage": l["prev_stage"] or "",
        # OWS에서 한 취소·반입만 되돌릴 수 있다(연동 명세의 취소·반입은 TMS 정본). 전표 취소로 함께 취소된
        # 라인은 전표 취소 되돌리기로만.
        "owsUndoable": bool(dead and l["ows_edited_at"] and (l["stage"] == "반입" or l["cancel_kind"] != "slip")),
        "salePrice": c["price"],
        **{k: l[col] for k, col in LINE_OPTION.items()},
        **{k: l[col] for k, col in LINE_TEXT.items() if k not in ("recipient", "memo")},
        "feeRate": c["fee_rate"], "saleVat": c["sale_vat"], "buyVat": c["buy_vat"], "netVat": c["net_vat"],
        "saleFee": c["sale_fee"], "optionCost": c["option"],
        "cost": c["asset_cost"], "totalCost": c["total_cost"],
        "purchasePrice": l["purchase_price"], "repairSum": l["repair_sum"] or 0,
        "profit": c["profit"], "profitSrc": c["src"],
        "source": l["source"], "owsEditedAt": l["ows_edited_at"],
    }


def _slip_payload(conn, row):
    lines = [_line_payload(l) for l in _slip_lines(conn, row["slip_no"])]
    return {
        "id": row["id"], "slipNo": row["slip_no"], "channel": row["channel"],
        "customer": row["customer"], "memo": row["memo"], "saleDate": row["sale_date"],
        "shipDate": row["ship_date"], "returnDate": row["return_date"], "waybill": row["waybill"],
        "qty": row["qty"], "headQty": row["head_qty"], "purchaseAmount": row["purchase_amount"],
        "saleAmount": row["sale_amount"], "itemSaleSum": row["item_sale_sum"],
        "diffAmount": row["diff_amount"], "vat": row["vat"], "fee": row["fee"], "cod": row["cod"],
        "shipping": row["shipping"], "profit": row["profit"], "itemProfit": row["item_profit"],
        "stage": row["stage"], "prevStage": row["prev_stage"] or "",
        "paidStatus": row["paid_status"], "paidAt": row["paid_at"],
        "paidAmount": row["paid_amount"], "paidConfirmed": bool(row["paid_confirmed"]),
        "source": row["source"], "isOws": is_ows_slip(row),
        "owsEditedAt": row["ows_edited_at"], "owsEditedBy": row["ows_edited_by"],
        "cancelDate": row["cancel_date"], "cancelReason": row["cancel_reason"],
        # 전표 취소 되돌리기 가능 여부 — OWS에서 취소한 전표(cancel_date 표시)만
        "owsUndoable": bool(row["stage"] == "판매취소" and row["cancel_date"] and _ows_touched(row)),
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        "lines": lines,
        "liveCount": sum(1 for l in lines if l["stage"] not in DEAD_STAGES),
    }


def _add_lines(conn, slip, raw_lines, ts):
    """라인 목록 검사 → 전부 통과할 때만 넣는다(하나라도 거부되면 아무것도 안 넣는다)."""
    if not isinstance(raw_lines, list):
        abort(400, description="lines 는 목록이어야 합니다.")
    errors, warnings, rows, seen = [], [], [], set()
    for it in raw_lines:
        if not isinstance(it, dict):
            abort(400, description="라인 형식이 올바르지 않습니다.")
        ano = str(it.get("assetNo") or "").strip()
        if not ano:
            errors.append("자산번호가 빈 줄이 있습니다.")
            continue
        if ano in seen:
            errors.append(f"{ano}: 같은 자산이 두 번 들어 있습니다.")
            continue
        seen.add(ano)
        a, err, warn = _check_asset(conn, ano, slip["slip_no"])
        if err:
            errors.append(f"{ano}: {err}")
            continue
        if warn:
            warnings.append(f"{ano}: {warn}")
        rows.append((a, _line_fields(it, partial=False)))
    if errors:
        abort(400, description=" / ".join(errors))
    added = []
    for a, f in rows:
        changed = _insert_line(conn, slip, a, f, ts)
        added.append({"assetNo": a["asset_no"], "statusChanged": changed})
    return added, warnings


# ---------------------------------------------------------------- API
@bp.post("/sale-slips/check-assets")
def sale_slip_check_assets():
    """자산번호 목록의 전표 등록 가능 여부 — 새 전표 화면이 스캔·붙여넣기 직후 부른다."""
    _read_gate()
    body = request.get_json(silent=True) or {}
    nos = body.get("assetNos") or []
    slip_no = str(body.get("slipNo") or "").strip() or None
    if not isinstance(nos, list) or len(nos) > 300:
        abort(400, description="자산번호 목록(최대 300개)을 보내 주세요.")
    out = []
    with tx() as conn:
        for raw in nos:
            ano = str(raw or "").strip()
            if not ano:
                continue
            a, err, warn = _check_asset(conn, ano, slip_no)
            item = {"assetNo": ano, "ok": not err, "reason": err, "warning": warn}
            if a is not None:
                item.update({
                    "assetId": a["id"], "maker": a["maker"], "model": a["model"],
                    "grade": a["grade"], "status": a["status"],
                    "statusLabel": ASSET_STATUSES.get(a["status"], a["status"]),
                    "division": a["division"], "purchasePrice": a["purchase_price"],
                    "repairSum": a["repair_sum"] or 0, "salePrice": a["sale_price"] or 0,
                    "heldOrder": a["held_order"] or "", "tmsDeletedAt": a["tms_deleted_at"] or "",
                })
            out.append(item)
    return jsonify({"items": out})


@bp.route("/sale-slips/rounds", methods=["GET", "POST"])
def sale_slip_rounds():
    """자산별 판매 회전 이력(2026-09-03) — 판매→반입→재판매가 같은 자산의 여러 행으로 있다.

    GET ?assetNo=a,b / POST {assetNos:[…]} (최대 1,000개) →
      {assets: {자산번호: {count: 회차 수, rows: [{id, slipNo, saleDate, shipDate, returnDate, cancelDate,
                          stage, round, salePrice, customer, channel, seller, source}]}}}
    round = 취소가 아닌 행을 판매일·id 순으로 1부터 센 회차(취소 행은 null). 집계는 건드리지 않는다.
    """
    _read_gate()
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        raw = body.get("assetNos") or []
        if not isinstance(raw, list):
            abort(400, description="assetNos 는 목록이어야 합니다.")
    else:
        raw = (request.args.get("assetNo") or "").split(",")
    nos = list(dict.fromkeys(str(x or "").strip() for x in raw if str(x or "").strip()))
    if len(nos) > 1000:
        abort(400, description="자산번호는 최대 1,000개까지입니다.")
    rows = []
    with tx() as conn:
        for i in range(0, len(nos), 500):
            chunk = nos[i:i + 500]
            rows += conn.execute(
                "SELECT t.id, t.slip_no, t.asset_no, t.sale_date, t.ship_date, t.return_date, t.cancel_date, "
                "  t.stage, t.sale_price, t.customer, t.channel, t.seller, t.source, "
                "  COALESCE(s.stage,'') AS slip_stage, COALESCE(s.source,'') AS slip_source "
                "FROM tms_sales t LEFT JOIN sale_slips s ON s.slip_no=t.slip_no "
                f"WHERE t.asset_no IN ({','.join('?' * len(chunk))})", chunk).fetchall()
    by_no = {}
    for r in sorted(rows, key=lambda r: ((r["sale_date"] or ""), r["id"])):
        by_no.setdefault(r["asset_no"], []).append(r)
    out = {}
    for no, rs in by_no.items():
        n, items = 0, []
        for r in rs:
            cancelled = "취소" in (r["stage"] or "") or r["slip_stage"] == "판매취소"
            if not cancelled:
                n += 1
            items.append({
                "id": r["id"], "slipNo": r["slip_no"], "saleDate": (r["sale_date"] or "")[:10],
                "shipDate": (r["ship_date"] or "")[:10], "returnDate": r["return_date"] or "",
                "cancelDate": r["cancel_date"] or "", "stage": r["stage"] or "",
                "slipStage": r["slip_stage"], "round": None if cancelled else n,
                "salePrice": r["sale_price"], "customer": r["customer"], "channel": r["channel"],
                "seller": r["seller"], "source": r["source"] or "", "slipSource": r["slip_source"],
            })
        out[no] = {"count": n, "rows": items}
    return jsonify({"assets": out})


@bp.post("/sale-slips")
def sale_slip_create():
    """전표 생성 — 헤더 + 라인(자산번호·판매가·옵션가). 번호는 S{YYMMDD}-{500~}."""
    _write_gate()
    body = request.get_json(silent=True) or {}
    head = _head_fields(body, partial=False)
    if not head["sale_date"]:
        head["sale_date"] = config.now_iso()[:10]
    raw_lines = body.get("lines") or []
    manual = body.get("saleAmount")
    if not raw_lines and manual in (None, ""):
        abort(400, description="자산을 한 대 이상 넣거나 판매금액을 적어 주세요.")
    manual_amt = None if manual in (None, "") else _money(manual, "판매금액")
    with tx(write=True) as conn:
        ts = config.now_iso()
        slip_no = next_sale_slip_no(conn)
        cols = dict(head)
        cols.update({"slip_no": slip_no, "stage": LIVE_STAGE, "source": "ows",
                     "ows_edited_at": ts, "ows_edited_by": _actor(),
                     "created_at": ts, "updated_at": ts})
        conn.execute(
            f"INSERT INTO sale_slips({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            list(cols.values()))
        slip = _get_slip(conn, slip_no)
        added, warnings = _add_lines(conn, slip, raw_lines, ts)
        if manual_amt is not None:
            _set_manual_total(conn, slip, manual_amt, ts)
        _recompute(conn, slip_no, ts)
        audit.log("sale_slip_create", target=slip_no,
                  detail={"lines": len(added), "channel": head["channel"],
                          "customer": head["customer"], "saleDate": head["sale_date"],
                          "saleAmount": manual_amt})
        payload = _slip_payload(conn, _get_slip(conn, slip_no))
    return jsonify({"ok": True, "slipNo": slip_no, "slip": payload, "warnings": warnings,
                    "added": added}), 201


@bp.get("/sale-slips/<slip_no>")
def sale_slip_detail(slip_no):
    _read_gate()
    with tx() as conn:
        return jsonify(_slip_payload(conn, _get_slip(conn, slip_no)))


@bp.patch("/sale-slips/<slip_no>")
def sale_slip_update(slip_no):
    """헤더 수정. 연동 전표도 고칠 수 있다 — 고치는 순간 ows_edited_at 이 찍혀 연동이 안 덮는다.

    ★연동 전표의 입금 4칸은 예외 — TMS가 원본이라 연동이 계속 따라가므로 여기서 막는다
      (고쳐 봐야 다음 틱에 되돌아가 헛일이 된다).
    """
    _write_gate()
    body = request.get_json(silent=True) or {}
    with tx(write=True) as conn:
        ts = config.now_iso()
        slip = _live_slip(conn, slip_no)
        head = _head_fields(body, partial=True)
        if not is_ows_slip(slip) and set(head) & set(HEAD_PAID.values()):
            abort(400, description="TMS 연동 전표의 입금(납부확인·입금일·입금액·입금확인)은 TMS가 원본이라 "
                                   "연동이 계속 따라갑니다 — 이 전표의 입금은 TMS에서 처리해 주세요.")
        stage = str(body.get("stage") or "").strip()
        if stage:
            if is_ows_slip(slip):
                abort(400, description="OWS 전표의 상태는 라인(반입·취소)에서 자동으로 정해집니다.")
            if stage not in TMS_HEAD_STAGES:
                abort(400, description=f"상태는 {'/'.join(TMS_HEAD_STAGES)} 중 하나여야 합니다.")
            head["stage"] = stage
        manual = body.get("saleAmount")
        if not head and manual in (None, ""):
            abort(400, description="바뀐 내용이 없습니다.")
        changes = {c: [slip[c], v] for c, v in head.items() if slip[c] != v}
        if head:
            conn.execute("UPDATE sale_slips SET " + ", ".join(f"{c}=?" for c in head) +
                         " WHERE slip_no=?", list(head.values()) + [slip_no])
        if manual not in (None, ""):
            amt = _money(manual, "판매금액")
            if amt != slip["sale_amount"]:
                changes["sale_amount"] = [slip["sale_amount"], amt]
            _set_manual_total(conn, slip, amt, ts)
        # 헤더의 채널·판매처·판매일·출고일은 OWS 라인에도 같은 값으로 실린다(명세 화면·자산 이력용)
        sync = {"channel": "channel", "customer": "seller", "sale_date": "sale_date", "ship_date": "ship_date"}
        line_sets = {sync[c]: head[c] for c in sync if c in head}
        if line_sets:
            conn.execute("UPDATE tms_sales SET " + ", ".join(f"{c}=?" for c in line_sets) +
                         ", updated_at=? WHERE slip_no=? AND source='ows'",
                         list(line_sets.values()) + [ts, slip_no])
        _touch(conn, slip_no, ts)
        _recompute(conn, slip_no, ts)
        audit.log("sale_slip_update", target=slip_no, detail={"changes": changes})
        payload = _slip_payload(conn, _get_slip(conn, slip_no))
    return jsonify({"ok": True, "slip": payload, "changed": list(changes)})


@bp.post("/sale-slips/<slip_no>/lines")
def sale_slip_lines_add(slip_no):
    """라인 추가 — {lines:[{assetNo, salePrice, …}]} 또는 라인 하나를 그대로."""
    _write_gate()
    body = request.get_json(silent=True) or {}
    raw = body.get("lines") if "lines" in body else ([body] if body.get("assetNo") else [])
    if not raw:
        abort(400, description="추가할 자산이 없습니다.")
    with tx(write=True) as conn:
        ts = config.now_iso()
        slip = _live_slip(conn, slip_no)
        added, warnings = _add_lines(conn, slip, raw, ts)
        _touch(conn, slip_no, ts)
        _recompute(conn, slip_no, ts)
        audit.log("sale_slip_line_add", target=slip_no,
                  detail={"assets": [x["assetNo"] for x in added]})
        payload = _slip_payload(conn, _get_slip(conn, slip_no))
    return jsonify({"ok": True, "added": added, "warnings": warnings, "slip": payload})


@bp.patch("/sale-slips/<slip_no>/lines/<asset_no>")
def sale_slip_line_update(slip_no, asset_no):
    _write_gate()
    body = request.get_json(silent=True) or {}
    with tx(write=True) as conn:
        ts = config.now_iso()
        _live_slip(conn, slip_no)
        line = _get_line(conn, slip_no, asset_no)
        if line["stage"] in DEAD_STAGES:
            abort(400, description=f"{line['stage']} 처리된 라인은 고칠 수 없습니다.")
        f = _line_fields(body, partial=True, line=line)
        if not f:
            abort(400, description="바뀐 내용이 없습니다.")
        changes = {c: [line[c], v] for c, v in f.items() if line[c] != v}
        conn.execute("UPDATE tms_sales SET " + ", ".join(f"{c}=?" for c in f) +
                     ", ows_edited_at=?, updated_at=? WHERE id=?",
                     list(f.values()) + [ts, ts, line["id"]])
        if line["asset_id"] is not None and changes:
            asset_event(conn, line["asset_id"], "판매수정",
                        {"판매전표": slip_no, "변경": changes, "출처": "OWS 전표"})
        _touch(conn, slip_no, ts)
        _recompute(conn, slip_no, ts)
        audit.log("sale_slip_line_update", target=f"{slip_no}/{asset_no}", detail={"changes": changes})
        payload = _slip_payload(conn, _get_slip(conn, slip_no))
    return jsonify({"ok": True, "slip": payload, "changed": list(changes)})


def _body_or_args():
    body = request.get_json(silent=True) or {}
    if not body:
        body = {k: v for k, v in request.args.items()}
    return body


def _flag(v):
    return str(v).lower() in ("1", "true", "yes", "on") if not isinstance(v, bool) else v


@bp.delete("/sale-slips/<slip_no>/lines/<asset_no>")
def sale_slip_line_cancel(slip_no, asset_no):
    """라인 제거 = '판매취소' 표시(물리 삭제 금지). {reason?, restock?}"""
    _write_gate()
    body = _body_or_args()
    reason = str(body.get("reason") or "").strip()[:300]
    restock = _flag(body.get("restock", False))
    with tx(write=True) as conn:
        ts = config.now_iso()
        _live_slip(conn, slip_no)
        line = _get_line(conn, slip_no, asset_no)
        if line["stage"] in DEAD_STAGES:
            abort(400, description=f"이미 {line['stage']} 처리된 라인입니다.")
        conn.execute("UPDATE tms_sales SET stage='판매취소', prev_stage=?, cancel_kind='line', cancel_date=?, "
                     "ows_edited_at=?, updated_at=? WHERE id=?",
                     (line["stage"] or LIVE_STAGE, ts[:10], ts, ts, line["id"]))
        rel = _release_asset(conn, line["asset_id"], "cancel", slip_no, restock, reason, ts)
        _touch(conn, slip_no, ts)
        _recompute(conn, slip_no, ts)
        audit.log("sale_slip_line_cancel", target=f"{slip_no}/{asset_no}",
                  detail={"reason": reason, "restock": restock, "asset": rel})
        payload = _slip_payload(conn, _get_slip(conn, slip_no))
    return jsonify({"ok": True, "asset": rel, "slip": payload})


@bp.post("/sale-slips/<slip_no>/lines/<asset_no>/return")
def sale_slip_line_return(slip_no, asset_no):
    """반입 — stage '반입' + 반입일. 자산은 기본 '복귀후보', restock=true 면 즉시 재고복귀."""
    _write_gate()
    body = request.get_json(silent=True) or {}
    reason = str(body.get("reason") or "").strip()[:300]
    restock = _flag(body.get("restock", False))
    when = _to_date(body.get("returnDate")) or config.now_iso()[:10]
    with tx(write=True) as conn:
        ts = config.now_iso()
        _live_slip(conn, slip_no)
        line = _get_line(conn, slip_no, asset_no)
        if line["stage"] in DEAD_STAGES:
            abort(400, description=f"이미 {line['stage']} 처리된 라인입니다.")
        conn.execute("UPDATE tms_sales SET stage='반입', prev_stage=?, return_date=?, ows_edited_at=?, "
                     "updated_at=? WHERE id=?", (line["stage"] or LIVE_STAGE, when, ts, ts, line["id"]))
        rel = _release_asset(conn, line["asset_id"], "return", slip_no, restock, reason, ts, when)
        _touch(conn, slip_no, ts)
        _recompute(conn, slip_no, ts)
        audit.log("sale_slip_line_return", target=f"{slip_no}/{asset_no}",
                  detail={"returnDate": when, "reason": reason, "restock": restock, "asset": rel})
        payload = _slip_payload(conn, _get_slip(conn, slip_no))
    return jsonify({"ok": True, "asset": rel, "slip": payload})


@bp.post("/sale-slips/<slip_no>/cancel")
def sale_slip_cancel(slip_no):
    """전표 취소 표시 — 살아 있는 라인 전부 '판매취소'. 금액은 그대로 남긴다(취소분으로 따로 보인다)."""
    _write_gate()
    body = request.get_json(silent=True) or {}
    reason = str(body.get("reason") or "").strip()[:300]
    restock = _flag(body.get("restock", False))
    if not reason:
        abort(400, description="왜 취소하는지 사유를 적어 주세요 — 나중에 근거가 됩니다.")
    with tx(write=True) as conn:
        ts = config.now_iso()
        slip = _get_slip(conn, slip_no)
        if slip["stage"] == "판매취소":
            abort(400, description="이미 취소된 전표입니다.")
        conn.execute("UPDATE sale_slips SET stage='판매취소', prev_stage=?, cancel_date=?, cancel_reason=? "
                     "WHERE slip_no=?", (slip["stage"] or LIVE_STAGE, ts[:10], reason, slip_no))
        released = []
        for l in _slip_lines(conn, slip_no):
            if l["stage"] in DEAD_STAGES:
                continue
            conn.execute("UPDATE tms_sales SET stage='판매취소', prev_stage=?, cancel_kind='slip', cancel_date=?, "
                         "ows_edited_at=?, updated_at=? WHERE id=?",
                         (l["stage"] or LIVE_STAGE, ts[:10], ts, ts, l["id"]))
            released.append(_release_asset(conn, l["asset_id"], "cancel", slip_no, restock, reason, ts))
        _touch(conn, slip_no, ts)
        audit.log("sale_slip_cancel", target=slip_no,
                  detail={"reason": reason, "lines": len(released), "restock": restock})
        payload = _slip_payload(conn, _get_slip(conn, slip_no))
    return jsonify({"ok": True, "assets": released, "slip": payload})


# ---------------------------------------------------------------- 되돌리기 API(2026-09-03)
#   매입 전표의 uncancel 과 같은 관례. 대상은 OWS에서 한 취소·반입만 — 연동 전표의 취소·반입은
#   TMS 값이 정본이라 되돌리지 않는다(화면 버튼 숨김 + 여기서 409).
@bp.post("/sale-slips/<slip_no>/uncancel")
def sale_slip_uncancel(slip_no):
    """전표 취소 되돌리기 — 전표 취소로 함께 취소된 라인(cancel_kind='slip')을 살리고 자산을 판매 상태로.

    그 사이 자산이 다른 전표·주문에 잡혔으면 409, 아무것도 바꾸지 않는다(전체 롤백).
    """
    _write_gate()
    with tx(write=True) as conn:
        ts = config.now_iso()
        slip = _get_slip(conn, slip_no)
        if slip["stage"] != "판매취소":
            abort(400, description="취소된 전표가 아닙니다.")
        if not _ows_touched(slip) or not slip["cancel_date"]:
            abort(409, description="TMS에서 취소된 연동 전표입니다 — TMS 값이 정본이라 OWS에서 되돌리지 않습니다.")
        restored = []
        for l in _slip_lines(conn, slip_no):
            if l["stage"] != "판매취소":
                continue
            # 새 표시(cancel_kind) 이전에 취소된 라인은 취소일·OWS 표시로 전표 취소분을 가려낸다
            by_slip = (l["cancel_kind"] == "slip" or
                       (not l["cancel_kind"] and l["ows_edited_at"] and (l["cancel_date"] or "") == slip["cancel_date"]))
            if by_slip:
                restored.append(_restore_line(conn, slip_no, l, "cancel", ts))
        back = slip["prev_stage"] or LIVE_STAGE
        conn.execute("UPDATE sale_slips SET stage=?, prev_stage='', cancel_date='', cancel_reason='' WHERE slip_no=?",
                     (back, slip_no))
        _touch(conn, slip_no, ts)
        _recompute(conn, slip_no, ts)
        audit.log("sale_slip_uncancel", target=slip_no,
                  detail={"lines": len(restored), "assets": [x["assetNo"] for x in restored]})
        payload = _slip_payload(conn, _get_slip(conn, slip_no))
    return jsonify({"ok": True, "assets": restored, "slip": payload})


def _undo_line(slip_no, asset_no, kind):
    with tx(write=True) as conn:
        ts = config.now_iso()
        _live_slip(conn, slip_no)
        line = _get_line(conn, slip_no, asset_no)
        want = "반입" if kind == "return" else "판매취소"
        if line["stage"] != want:
            abort(400, description=f"{want} 처리된 라인이 아닙니다.")
        if not line["ows_edited_at"]:
            abort(409, description=f"TMS 연동 명세의 {want}입니다 — TMS 값이 정본이라 OWS에서 되돌리지 않습니다.")
        if kind == "cancel" and line["cancel_kind"] == "slip":
            abort(400, description="전표 취소로 함께 취소된 라인입니다 — 전표 취소 되돌리기로 살립니다.")
        rel = _restore_line(conn, slip_no, line, kind, ts)
        _touch(conn, slip_no, ts)
        _recompute(conn, slip_no, ts)
        audit.log("sale_slip_line_unreturn" if kind == "return" else "sale_slip_line_uncancel",
                  target=f"{slip_no}/{asset_no}", detail={"asset": rel})
        payload = _slip_payload(conn, _get_slip(conn, slip_no))
    return jsonify({"ok": True, "asset": rel, "slip": payload})


@bp.post("/sale-slips/<slip_no>/lines/<asset_no>/unreturn")
def sale_slip_line_unreturn(slip_no, asset_no):
    """반입 취소(반입 표시 해제) — 라인 '반입' → 반입 전 상태, 자산은 판매 확정 상태 복원."""
    _write_gate()
    return _undo_line(slip_no, asset_no, "return")


@bp.post("/sale-slips/<slip_no>/lines/<asset_no>/uncancel")
def sale_slip_line_uncancel(slip_no, asset_no):
    """라인 판매취소 되돌리기 — 라인 단독으로 취소한 것만(전표 취소분은 전표 되돌리기로)."""
    _write_gate()
    return _undo_line(slip_no, asset_no, "cancel")
