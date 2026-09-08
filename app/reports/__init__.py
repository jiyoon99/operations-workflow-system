"""리포트 — 매입/판매/마진, 재고 회전, 담당자 실적.

원가 = 자산 매입가 + 그 자산의 수리비 합계(A/S 무상 건 포함).
마진 = 주문 금액 − 매칭된 자산들의 원가.
"""
from flask import Blueprint, abort, g, jsonify, request

from .. import config
from ..auth.perms import require, require_any
from ..db import get_db, sale_only
from ..purchase import scope_clause

bp = Blueprint("reports", __name__, url_prefix="/api")


def _period():
    """?from=YYYY-MM-DD&to=YYYY-MM-DD (기본: 이번 달)."""
    today = config.now()
    frm = (request.args.get("from") or today.replace(day=1).strftime("%Y-%m-%d")).strip()
    to = (request.args.get("to") or today.strftime("%Y-%m-%d")).strip()
    # ★거꾸로 넣으면 조용히 0원이 나온다 — '매출이 없다'와 '날짜를 잘못 넣었다'가
    #   화면에서 구분되지 않으므로 서버에서 막고 이유를 말해 준다.
    if frm and to and frm > to:
        abort(400, description=f"시작일({frm})이 종료일({to})보다 늦습니다. 날짜를 바꿔 주세요.")
    return frm, to


def _to_end(to):
    """종료일 상한. shipping_at은 '2026-07-28T18:30:00+09:00' 형태라
    'T23:59:59'로 자르면 타임존 오프셋(+09:00) 때문에 마지막 1초가 빠진다.
    다음 날 0시 직전까지 포함하도록 넉넉히 잡는다."""
    return to + "T99"


# 회수 완료된 주문은 매출·원가에서 뺀다(물건이 돌아왔으므로 판매가 아니다)
_NOT_RETURNED = (
    " AND NOT EXISTS (SELECT 1 FROM waybills w WHERE w.order_id = o.id "
    " AND w.type='recall' AND w.status='delivered')"
)

# 한 자산이 살아 있는 출고 주문 몇 건에 걸려 있는가.
# 회수했다가 고쳐서 다시 판 노트북은 주문이 둘이 되는데, 수리비를 양쪽에 통째로 붙이면
# 같은 돈이 두 번 빠져 그 주문의 마진이 실제보다 낮게 나온다. 이 수로 나눠 담는다.
_ASSET_ORDER_COUNT = (
    "(SELECT COUNT(*) FROM order_assets oa2 JOIN orders o2 ON o2.id = oa2.order_id "
    " WHERE oa2.asset_id = {alias}.id AND o2.shipping_done=1 AND o2.cancelled_at='' "
    " AND NOT EXISTS (SELECT 1 FROM waybills w2 WHERE w2.order_id = o2.id "
    "                 AND w2.type='recall' AND w2.status='delivered'))"
)


@bp.get("/reports/summary")
def summary():
    """기간 요약 — 매입/출고/마진/재고 한눈에."""
    require_any("reports.view", "settings.manage", "purchase.money")
    frm, to = _period()
    conn = get_db()
    to_end = _to_end(to)

    purchase = conn.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(total_amount),0) AS amount "
        "FROM purchase_batches WHERE stage='purchased' AND cancelled_at='' "
        "AND purchase_date BETWEEN ? AND ?",
        (frm, to)).fetchone()
    purchased_assets = conn.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(a.purchase_price),0) AS amount FROM assets a "
        "JOIN purchase_batches b ON b.id = a.batch_id "
        "WHERE b.stage='purchased' AND b.cancelled_at='' "
        "AND b.purchase_date BETWEEN ? AND ?", (frm, to)).fetchone()

    shipped = conn.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount),0) AS amount, "
        "       COALESCE(SUM(fee_amount),0) AS fee, "
        "       COALESCE(SUM(refund_amount),0) AS refund, "
        "       COALESCE(SUM(shipping_cost),0) AS ship "
        "FROM orders o "
        "WHERE shipping_done=1 AND cancelled_at='' AND shipping_at BETWEEN ? AND ?" + _NOT_RETURNED,
        (frm, to_end)).fetchone()
    # 출고 주문에 매칭된 자산 원가 — ★주문×자산 단위로 합산한다.
    # 자산 기준 DISTINCT로 세면 같은 자산이 회수 후 재판매됐을 때 원가가 한 번만 잡혀
    # 마진이 부풀려진다(2026-07-28 리뷰 확인).
    cost = conn.execute(
        "SELECT COALESCE(SUM(a.purchase_price),0) AS buy FROM order_assets oa "
        "JOIN orders o ON o.id = oa.order_id JOIN assets a ON a.id = oa.asset_id "
        "WHERE o.shipping_done=1 AND o.cancelled_at='' AND o.shipping_at BETWEEN ? AND ?" + _NOT_RETURNED,
        (frm, to_end)).fetchone()
    # ★수리비는 총액(부가세 포함)이다. 원가로는 총액을 쓰되, 부가세는 따로 뽑아 둔다 —
    #   매입세액 공제 대상이라 매출 부가세와 나란히 봐야 실제로 낼 세금이 나온다
    #   (대표 2026-08-04). 옛 기록은 vat가 0이라 총액에서 역산해 채운다.
    repair = conn.execute(
        "SELECT COALESCE(SUM(r.cost * 1.0 / MAX(1, " + _ASSET_ORDER_COUNT.format(alias="a") + ")),0) AS repair, "
        "COALESCE(SUM((CASE WHEN r.vat > 0 THEN r.vat ELSE ROUND(r.cost * 10.0 / 110) END)"
        " * 1.0 / MAX(1, " + _ASSET_ORDER_COUNT.format(alias="a") + ")),0) AS repair_vat "
        "FROM asset_repairs r "
        "JOIN order_assets oa ON oa.asset_id = r.asset_id "
        "JOIN assets a ON a.id = r.asset_id "
        "JOIN orders o ON o.id = oa.order_id "
        "WHERE o.shipping_done=1 AND o.cancelled_at='' AND o.shipping_at BETWEEN ? AND ?" + _NOT_RETURNED,
        (frm, to_end)).fetchone()

    revenue = shipped["amount"] or 0
    # 실입금 = 판매가 − 쇼핑몰/PG 수수료 − 환불. 마진은 이 돈에서 원가를 뺀 것이어야 한다.
    fee = shipped["fee"] or 0
    refund = shipped["refund"] or 0
    net_revenue = revenue - fee - refund
    repair_vat = round(repair["repair_vat"] or 0)
    cost = {"buy": cost["buy"] or 0, "repair": round(repair["repair"] or 0),
            "shipping": shipped["ship"] or 0}
    total_cost = cost["buy"] + cost["repair"] + cost["shipping"]
    scope, sparams = scope_clause("a")
    # ★'보유 재고'는 매입 화면과 같은 기준으로 센다.
    #   예전엔 shipped/scrapped만 뺐더니 매입취소·거래처반품·아직 안 받은 가입고분까지
    #   창고에 있는 재산으로 잡혀, 대시보드 7대 / 재무 7대 / 매입 3대로 화면마다 달랐다.
    stock = conn.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(a.purchase_price),0) AS amount FROM assets a "
        "WHERE a.status NOT IN ('shipped','scrapped','cancelled','returned') "
        "AND a.received = 1" + sale_only("a") + scope, sparams).fetchone()
    # ★A/S 돈(대표 2026-09-02 확정): 유상 A/S 수입은 판매 매출과 **합치지 않고 나란히** 본다.
    #   부품 원가는 A/S 비용으로만 잡는다(자산 원가에 안 얹음 — 지난달 마진을 흔들지 않으려고).
    as_stats = conn.execute(
        "SELECT COUNT(*) AS cnt, "
        "  COALESCE(SUM(CASE WHEN charge_to='company' THEN cost ELSE 0 END),0) AS company_cost, "
        "  COALESCE(SUM(CASE WHEN charge_to='customer' THEN cost + COALESCE(extra_charge,0) "
        "    ELSE 0 END),0) AS income "
        "FROM as_tickets WHERE received_at BETWEEN ? AND ? AND status != 'cancelled'",
        (frm, to)).fetchone()
    as_part_cost = conn.execute(
        "SELECT COALESCE(SUM(i.cost),0) AS c FROM as_ticket_items i "
        "JOIN as_tickets t ON t.id = i.ticket_id "
        "WHERE t.received_at BETWEEN ? AND ? AND t.status != 'cancelled'", (frm, to)).fetchone()["c"]

    # 원가가 비어 있으면 마진이 실제보다 높게 나온다 — 얼마나 못 믿을 숫자인지 함께 알려준다
    gaps = conn.execute(
        "SELECT COUNT(*) AS units, SUM(CASE WHEN a.purchase_price=0 THEN 1 ELSE 0 END) AS no_buy "
        "FROM order_assets oa JOIN orders o ON o.id=oa.order_id JOIN assets a ON a.id=oa.asset_id "
        "WHERE o.shipping_done=1 AND o.cancelled_at='' AND o.shipping_at BETWEEN ? AND ?" + _NOT_RETURNED,
        (frm, to_end)).fetchone()
    no_asset = conn.execute(
        "SELECT COUNT(*) AS c FROM orders o WHERE o.shipping_done=1 AND o.cancelled_at='' "
        "AND o.shipping_at BETWEEN ? AND ? AND NOT EXISTS "
        "(SELECT 1 FROM order_assets oa WHERE oa.order_id=o.id)" + _NOT_RETURNED,
        (frm, to_end)).fetchone()["c"]
    no_amount = conn.execute(
        "SELECT COUNT(*) AS c FROM orders o WHERE o.shipping_done=1 AND o.cancelled_at='' "
        "AND o.amount=0 AND o.shipping_at BETWEEN ? AND ?" + _NOT_RETURNED,
        (frm, to_end)).fetchone()["c"]

    # ★TMS 전표 매출(2026-08-09 대표 승인) — 몰 주문(orders)만 집계하면 방문구매·B2B
    #   묶음 판매가 통째로 빠진다. 같은 채널명이 겹칠 수 있어 몰 매출과 '합산하지 않고'
    #   나란히 보여만 준다(이중계상 방지).
    slip = conn.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(qty),0) AS qty, "
        "  COALESCE(SUM(sale_amount),0) AS amount, COALESCE(SUM(profit),0) AS profit "
        "FROM sale_slips WHERE stage NOT IN ('판매취소','반입') "
        "AND sale_date BETWEEN ? AND ?", (frm, to)).fetchone()

    return jsonify({
        "from": frm, "to": to,
        "purchase": {"slips": purchase["cnt"], "amount": purchase["amount"],
                     "assets": purchased_assets["cnt"], "assetAmount": purchased_assets["amount"]},
        "slipSales": {"slips": slip["cnt"], "qty": slip["qty"],
                      "amount": slip["amount"], "profit": slip["profit"]},
        "sales": {"orders": shipped["cnt"], "revenue": revenue,
                  "fee": fee, "refund": refund, "netRevenue": net_revenue,
                  "cost": total_cost, "buyCost": cost["buy"], "repairCost": cost["repair"],
                  # 수리비에 포함된 부가세(매입세액) — 매출 부가세에서 뺄 수 있는 금액
                  "repairVat": repair_vat, "repairNet": cost["repair"] - repair_vat,
                  "shippingCost": cost["shipping"],
                  "margin": net_revenue - total_cost,
                  "marginRate": round((net_revenue - total_cost) / revenue * 100, 1) if revenue else 0},
        "stock": {"assets": stock["cnt"], "amount": stock["amount"]},
        "as": {"tickets": as_stats["cnt"], "companyCost": as_stats["company_cost"],
               # 유상 A/S 수입 / 나간 부품 원가 / 남는 돈 — 판매 매출과 별도 줄
               "income": as_stats["income"], "partCost": as_part_cost,
               "profit": (as_stats["income"] or 0) - (as_part_cost or 0)},
        # 이 숫자들이 크면 위의 마진은 실제보다 부풀려진 값이다
        "dataGaps": {"units": gaps["units"] or 0, "noBuyPrice": gaps["no_buy"] or 0,
                     "ordersWithoutAsset": no_asset, "ordersWithoutAmount": no_amount},
    })


# ---------------------------------------------------------------- 기간 실적(일/주/월/분기/연)
#   (2026-08-24 대표) "매출/부가세/순이익 한눈에, 일·주·월·분기·연 단위로".
#   ★계산은 /reports/summary 와 같은 코드를 쓴다 — 두 화면이 다른 숫자를 말하면 안 된다.

PERIOD_UNITS = ("day", "week", "month", "quarter", "year")


def _period_range(unit, at):
    """단위와 기준일로 (시작일, 종료일, 보여줄 이름)."""
    from datetime import date, timedelta
    y, m, d = (int(x) for x in at.split("-"))
    base = date(y, m, d)
    if unit == "day":
        return base, base, f"{base.year}년 {base.month}월 {base.day}일"
    if unit == "week":
        start = base - timedelta(days=base.weekday())      # 월요일 시작
        end = start + timedelta(days=6)
        return start, end, f"{start.month}/{start.day} ~ {end.month}/{end.day} (주)"
    if unit == "month":
        start = base.replace(day=1)
        end = (start.replace(year=start.year + 1, month=1) if start.month == 12
               else start.replace(month=start.month + 1)) - timedelta(days=1)
        return start, end, f"{start.year}년 {start.month}월"
    if unit == "quarter":
        q = (base.month - 1) // 3
        start = date(base.year, q * 3 + 1, 1)
        end = (date(base.year + 1, 1, 1) if q == 3
               else date(base.year, q * 3 + 4, 1)) - timedelta(days=1)
        return start, end, f"{base.year}년 {q + 1}분기"
    start, end = date(base.year, 1, 1), date(base.year, 12, 31)
    return start, end, f"{base.year}년"


def _shift(unit, at, step):
    """이전/다음 기간의 기준일 — 화면의 ◀ ▶ 버튼이 쓴다."""
    from datetime import date, timedelta
    y, m, d = (int(x) for x in at.split("-"))
    base = date(y, m, d)
    if unit == "day":
        return base + timedelta(days=step)
    if unit == "week":
        return base + timedelta(weeks=step)
    if unit == "year":
        return base.replace(year=base.year + step, day=1, month=base.month)
    months = 1 if unit == "month" else 3
    total = (base.year * 12 + base.month - 1) + step * months
    return date(total // 12, total % 12 + 1, 1)


@bp.get("/reports/period")
def period_report():
    """?unit=day|week|month|quarter|year&at=YYYY-MM-DD — 그 기간의 매출·부가세·순이익."""
    require_any("reports.view", "settings.manage", "purchase.money")
    from ..purchase import split_vat
    unit = (request.args.get("unit") or "month").strip().lower()
    if unit not in PERIOD_UNITS:
        abort(400, description="단위는 day/week/month/quarter/year 중 하나여야 합니다.")
    # ★config.today_str() 은 'YYYYMMDD'(대시 없음)라 여기 쓰면 안 된다 — 기간 파서가 거부한다
    # `date` was used by the earlier report screen; keep it as a compatible alias.
    at = (request.args.get("at") or request.args.get("date")
          or config.now().strftime("%Y-%m-%d")).strip()
    try:
        start, end, label = _period_range(unit, at)
    except (ValueError, TypeError):
        abort(400, description="기준일은 YYYY-MM-DD 형식이어야 합니다.")
    frm, to = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    data = _sales_block(frm, to)
    # ★매출 부가세 — 판매가는 부가세 포함 총액이다(매입·수리비와 같은 규약).
    supply, vat = split_vat(data["revenue"])
    data.update({
        "supply": supply, "vat": vat,
        # 낼 세금 = 매출세액 − 매입세액(수리비에 포함된 부가세)
        "vatPayable": vat - data["repairVat"],
    })
    return jsonify({
        "unit": unit, "at": at, "from": frm, "to": to, "label": label,
        "prev": _shift(unit, at, -1).strftime("%Y-%m-%d"),
        "next": _shift(unit, at, 1).strftime("%Y-%m-%d"),
        **data,
    })


def _sales_block(frm, to):
    """출고 기준 매출·원가·순이익 — /reports/summary 와 같은 잣대."""
    conn = get_db()
    to_end = _to_end(to)
    shipped = conn.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount),0) AS amount, "
        "       COALESCE(SUM(fee_amount),0) AS fee, "
        "       COALESCE(SUM(refund_amount),0) AS refund, "
        "       COALESCE(SUM(shipping_cost),0) AS ship "
        "FROM orders o WHERE shipping_done=1 AND cancelled_at='' "
        "AND shipping_at BETWEEN ? AND ?" + _NOT_RETURNED, (frm, to_end)).fetchone()
    units = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(a.purchase_price),0) AS buy FROM order_assets oa "
        "JOIN orders o ON o.id = oa.order_id JOIN assets a ON a.id = oa.asset_id "
        "WHERE o.shipping_done=1 AND o.cancelled_at='' AND o.shipping_at BETWEEN ? AND ?"
        + _NOT_RETURNED, (frm, to_end)).fetchone()
    repair = conn.execute(
        "SELECT COALESCE(SUM(r.cost * 1.0 / MAX(1, "
        + _ASSET_ORDER_COUNT.format(alias="a") + ")),0) AS repair, "
        "COALESCE(SUM((CASE WHEN r.vat > 0 THEN r.vat ELSE ROUND(r.cost * 10.0 / 110) END)"
        " * 1.0 / MAX(1, " + _ASSET_ORDER_COUNT.format(alias="a") + ")),0) AS repair_vat "
        "FROM asset_repairs r JOIN order_assets oa ON oa.asset_id = r.asset_id "
        "JOIN assets a ON a.id = r.asset_id JOIN orders o ON o.id = oa.order_id "
        "WHERE o.shipping_done=1 AND o.cancelled_at='' AND o.shipping_at BETWEEN ? AND ?"
        + _NOT_RETURNED, (frm, to_end)).fetchone()
    revenue = shipped["amount"] or 0
    fee, refund = shipped["fee"] or 0, shipped["refund"] or 0
    buy = units["buy"] or 0
    rep = round(repair["repair"] or 0)
    ship = shipped["ship"] or 0
    cost = buy + rep + ship
    net_revenue = revenue - fee - refund
    # ── TMS 수기 판매(2026-08-25 대표 "자산번호로 매출을 매칭") — 자사몰·B2B 전화 등
    #    OWS 주문이 없는 판매를 합산한다. ★자산번호가 OWS 주문에 매칭된 판매는 뺀다:
    #    자산은 한 번에 한 대만 나가므로 자산번호가 곧 중복 제거 열쇠다.
    #    판매가 0 기재 행(금액이 TMS 판매등록 칸에만 있는 행)도 뺀다 — 0을 더하면 왜곡만 된다.
    tms = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(t.sale_price),0) AS sale, "
        "  COALESCE(SUM(CASE WHEN t.asset_id IS NOT NULL AND COALESCE(a.purchase_price,0) > 0 "
        "    THEN a.purchase_price + COALESCE((SELECT SUM(r.cost) FROM asset_repairs r "
        "         WHERE r.asset_id=t.asset_id),0) "
        "    ELSE t.purchase_price END),0) AS cost "
        "FROM tms_sales t LEFT JOIN assets a ON a.id=t.asset_id "
        "WHERE substr(t.sale_date,1,10) BETWEEN ? AND ? "
        "AND t.stage NOT LIKE '%취소%' AND t.sale_price > 0 "
        "AND (t.asset_id IS NULL OR NOT EXISTS("
        "     SELECT 1 FROM order_assets oa WHERE oa.asset_id=t.asset_id))",
        (frm, to)).fetchone()
    tms_units, tms_rev, tms_cost = tms["n"] or 0, tms["sale"] or 0, tms["cost"] or 0
    return {
        "orders": shipped["cnt"], "units": units["n"] or 0,
        "revenue": revenue, "fee": fee, "refund": refund, "netRevenue": net_revenue,
        "buyCost": buy, "repairCost": rep, "repairVat": round(repair["repair_vat"] or 0),
        "shippingCost": ship, "cost": cost,
        "profit": net_revenue - cost,
        "profitRate": round((net_revenue - cost) / revenue * 100, 1) if revenue else 0,
        # TMS 수기 판매(중복 제거 후) — 화면은 이걸 별도 줄로 보여 주고 합산액도 준다
        "tmsUnits": tms_units, "tmsRevenue": tms_rev, "tmsCost": tms_cost,
        "tmsProfit": tms_rev - tms_cost,
        "combinedRevenue": revenue + tms_rev,
        "combinedProfit": (net_revenue - cost) + (tms_rev - tms_cost),
    }


@bp.get("/reports/monthly")
def monthly():
    """월별 매입·출고·마진 추이(최근 12개월)."""
    require_any("reports.view", "settings.manage", "purchase.money")
    conn = get_db()
    rows = conn.execute(
        "SELECT substr(purchase_date,1,7) AS ym, COUNT(*) AS slips, "
        " COALESCE(SUM(total_amount),0) AS amount FROM purchase_batches "
        "WHERE stage='purchased' AND cancelled_at='' AND purchase_date != '' "
        "GROUP BY ym ORDER BY ym DESC LIMIT 12").fetchall()
    purchase = {r["ym"]: {"slips": r["slips"], "amount": r["amount"]} for r in rows}

    rows = conn.execute(
        "SELECT substr(shipping_at,1,7) AS ym, COUNT(*) AS orders, COALESCE(SUM(amount),0) AS revenue "
        "FROM orders o WHERE shipping_done=1 AND cancelled_at='' AND shipping_at != ''" + _NOT_RETURNED +
        " GROUP BY ym ORDER BY ym DESC LIMIT 12").fetchall()
    sales = {r["ym"]: {"orders": r["orders"], "revenue": r["revenue"]} for r in rows}

    # TMS 전표 매출 — 몰 주문과 별도 집계(합산 금지, summary 주석 참조)
    rows = conn.execute(
        "SELECT substr(sale_date,1,7) AS ym, COUNT(*) AS slips, "
        " COALESCE(SUM(sale_amount),0) AS revenue FROM sale_slips "
        "WHERE stage NOT IN ('판매취소','반입') AND sale_date != '' "
        "GROUP BY ym ORDER BY ym DESC LIMIT 12").fetchall()
    slips = {r["ym"]: {"slips": r["slips"], "revenue": r["revenue"]} for r in rows}

    months = sorted(set(purchase) | set(sales) | set(slips), reverse=True)[:12]
    return jsonify([
        {"month": m,
         "purchaseSlips": purchase.get(m, {}).get("slips", 0),
         "purchaseAmount": purchase.get(m, {}).get("amount", 0),
         "orders": sales.get(m, {}).get("orders", 0),
         "revenue": sales.get(m, {}).get("revenue", 0),
         "slipCount": slips.get(m, {}).get("slips", 0),
         "slipRevenue": slips.get(m, {}).get("revenue", 0)}
        for m in months
    ])


@bp.get("/reports/channels")
def channels():
    """채널별 판매 실적."""
    require_any("reports.view", "settings.manage", "purchase.money")
    frm, to = _period()
    rows = get_db().execute(
        "SELECT channel, COUNT(*) AS orders, COALESCE(SUM(amount),0) AS revenue FROM orders o "
        "WHERE shipping_done=1 AND cancelled_at='' AND shipping_at BETWEEN ? AND ?" + _NOT_RETURNED +
        " GROUP BY channel ORDER BY revenue DESC", (frm, _to_end(to))).fetchall()
    return jsonify([{"channel": r["channel"] or "(미지정)", "orders": r["orders"],
                     "revenue": r["revenue"]} for r in rows])


@bp.get("/reports/slip-channels")
def slip_channels():
    """TMS 전표 채널별 판매 — 몰 주문 채널표와 별도(합산 금지, summary 주석 참조)."""
    require_any("reports.view", "settings.manage", "purchase.money")
    frm, to = _period()
    rows = get_db().execute(
        "SELECT channel, COUNT(*) AS slips, COALESCE(SUM(qty),0) AS qty, "
        "  COALESCE(SUM(sale_amount),0) AS revenue FROM sale_slips "
        "WHERE stage NOT IN ('판매취소','반입') AND sale_date BETWEEN ? AND ? "
        "GROUP BY channel ORDER BY revenue DESC", (frm, to)).fetchall()
    return jsonify([{"channel": r["channel"] or "(미지정)", "slips": r["slips"],
                     "qty": r["qty"], "revenue": r["revenue"]} for r in rows])


@bp.get("/reports/staff")
def staff():
    """담당자별 처리 실적 — 준비/검수/출고 건수."""
    require("reports.view")
    frm, to = _period()
    to_end = _to_end(to)
    conn = get_db()
    out = {}

    def add(name, key, n):
        if not name:
            return
        out.setdefault(name, {"name": name, "production": 0, "inspection": 0, "shipping": 0})
        out[name][key] += n

    # ★단계를 해제해도 _by/_at은 남으므로, 현재 '완료' 상태인 것만 센다
    for col, at, key, done in (("production_by", "production_at", "production", "production_done"),
                               ("inspection_by", "inspection_at", "inspection", "inspection_done"),
                               ("shipping_by", "shipping_at", "shipping", "shipping_done")):
        rows = conn.execute(
            f"SELECT {col} AS who, COUNT(*) AS c FROM orders "
            f"WHERE {col} != '' AND {done}=1 AND cancelled_at='' AND {at} BETWEEN ? AND ? "
            f"GROUP BY {col}", (frm, to_end)).fetchall()
        for r in rows:
            add(r["who"], key, r["c"])
    return jsonify(sorted(out.values(), key=lambda x: -(x["production"] + x["inspection"] + x["shipping"])))


@bp.get("/reports/aging")
def aging():
    """재고 체류 기간 — 오래 묵은 자산을 찾는다."""
    require_any("reports.view", "settings.manage", "purchase.money")
    conn = get_db()
    scope, sparams = scope_clause("a")
    rows = conn.execute(
        "SELECT a.id, a.asset_no, a.maker, a.model, a.grade, a.status, a.purchase_price, "
        " a.created_at, c.name AS category_name "
        "FROM assets a LEFT JOIN categories c ON c.id=a.category_id "
        "WHERE a.status NOT IN ('shipped','scrapped')" + sale_only("a") + scope +
        " ORDER BY a.created_at LIMIT 100", sparams).fetchall()
    now = config.now()
    out = []
    for r in rows:
        days = 0
        try:
            from datetime import datetime
            days = (now - datetime.fromisoformat(r["created_at"])).days
        except (TypeError, ValueError):
            pass
        out.append({"assetId": r["id"], "assetNo": r["asset_no"],
                    "model": " ".join(x for x in (r["maker"], r["model"]) if x),
                    "grade": r["grade"], "status": r["status"], "categoryName": r["category_name"],
                    "purchasePrice": r["purchase_price"], "days": days})
    return jsonify(out)


def _ledger_rows(conn, frm, to_end):
    """매출 대장 원본 — 주문 한 건이 한 줄. 원가는 매칭된 자산 기준으로 붙인다."""
    return conn.execute(
        "SELECT o.id, o.ordered_at, o.shipping_at, o.channel, o.order_no, o.product_name, "
        "       o.option_name, o.quantity, o.amount, o.fee_amount, o.fee_rate, o.shipping_cost, "
        "       o.refund_amount, o.refund_reason, o.recipient, "
        "       COALESCE(ac.buy,0) AS buy_cost, COALESCE(ac.repair,0) AS repair_cost, "
        "       COALESCE(ac.nos,'') AS asset_nos "
        "FROM orders o LEFT JOIN ("
        "  SELECT oa.order_id, SUM(a.purchase_price) AS buy, "
        "         SUM(COALESCE((SELECT SUM(r.cost) FROM asset_repairs r WHERE r.asset_id=a.id),0) "
        "             * 1.0 / MAX(1, " + _ASSET_ORDER_COUNT.format(alias="a") + ")) AS repair, "
        "         GROUP_CONCAT(a.asset_no) AS nos "
        "  FROM order_assets oa JOIN assets a ON a.id=oa.asset_id GROUP BY oa.order_id"
        ") ac ON ac.order_id = o.id "
        "WHERE o.shipping_done=1 AND o.cancelled_at='' AND o.shipping_at BETWEEN ? AND ?"
        + _NOT_RETURNED + " ORDER BY o.shipping_at DESC LIMIT 5000", (frm, to_end)).fetchall()


def _ledger_line(r):
    net = (r["amount"] or 0) - (r["fee_amount"] or 0) - (r["refund_amount"] or 0)
    cost = (r["buy_cost"] or 0) + round(r["repair_cost"] or 0) + (r["shipping_cost"] or 0)
    return {
        "orderId": r["id"], "shippedAt": (r["shipping_at"] or "")[:10],
        "orderedAt": (r["ordered_at"] or "")[:10],
        "channel": r["channel"], "orderNumber": r["order_no"],
        "productName": r["product_name"], "optionName": r["option_name"],
        "assetNos": r["asset_nos"], "recipient": r["recipient"], "quantity": r["quantity"],
        "amount": r["amount"], "fee": r["fee_amount"], "feeRate": r["fee_rate"],
        "refund": r["refund_amount"], "refundReason": r["refund_reason"],
        "netAmount": net, "buyCost": r["buy_cost"], "repairCost": round(r["repair_cost"] or 0),
        "shippingCost": r["shipping_cost"], "cost": cost, "margin": net - cost,
        "marginRate": round((net - cost) / r["amount"] * 100, 1) if r["amount"] else 0,
    }


@bp.get("/reports/ledger")
def ledger():
    """매출 대장 — 한 줄에 판매가·수수료·환불·원가·마진까지. 세무·정산에 그대로 쓴다."""
    require_any("reports.view", "settings.manage", "purchase.money")
    frm, to = _period()
    rows = [_ledger_line(r) for r in _ledger_rows(get_db(), frm, _to_end(to))]
    totals = {k: sum(x[k] for x in rows) for k in
              ("amount", "fee", "refund", "netAmount", "buyCost", "repairCost",
               "shippingCost", "cost", "margin")}
    totals["orders"] = len(rows)
    return jsonify({"from": frm, "to": to, "rows": rows, "totals": totals})


@bp.get("/reports/ledger/export")
def ledger_export():
    """매출 대장 엑셀 — 세무사에게 그대로 넘길 수 있는 형태."""
    require_any("reports.view", "settings.manage", "purchase.money")
    from flask import Response

    from ..importers import write_xlsx
    frm, to = _period()
    rows = [_ledger_line(r) for r in _ledger_rows(get_db(), frm, _to_end(to))]
    headers = ["출고일", "주문일", "쇼핑몰", "주문번호", "상품명", "옵션", "자산번호", "수취인",
               "수량", "판매가", "수수료", "요율(%)", "환불", "실입금",
               "매입원가", "수리비", "택배비", "원가합", "마진", "마진율(%)"]
    data = [[
        r["shippedAt"], r["orderedAt"], r["channel"], r["orderNumber"], r["productName"],
        r["optionName"], r["assetNos"], r["recipient"], r["quantity"], r["amount"],
        r["fee"], r["feeRate"], r["refund"], r["netAmount"], r["buyCost"], r["repairCost"],
        r["shippingCost"], r["cost"], r["margin"], r["marginRate"],
    ] for r in rows]
    return Response(
        write_xlsx(headers, data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=ows-ledger-{frm}_{to}.xlsx"})


@bp.get("/reports/profitability")
def profitability():
    """무엇이 남는 장사인가 — 모델·등급·매입처별 마진.

    ?by=model|grade|supplier (기본 model)
    """
    require_any("reports.view", "settings.manage", "purchase.money")
    frm, to = _period()
    by = (request.args.get("by") or "model").strip()
    if by not in ("model", "grade", "supplier"):
        from flask import abort
        abort(400, description="집계 기준은 model / grade / supplier 중 하나입니다.")
    key_sql = {
        "model": "TRIM(COALESCE(a.maker,'') || ' ' || COALESCE(a.model,''))",
        "grade": "a.grade",
        "supplier": "COALESCE(s.name, '(거래처 미지정)')",
    }[by]
    join = (" LEFT JOIN purchase_batches b ON b.id = a.batch_id "
            " LEFT JOIN suppliers s ON s.id = b.supplier_id") if by == "supplier" else ""

    # 주문 한 건에 자산이 여럿일 수 있어, 매출은 자산 수로 나눠 배분한다(이중계상 방지)
    rows = get_db().execute(
        "SELECT " + key_sql + " AS k, COUNT(*) AS units, "
        "  SUM((o.amount - o.fee_amount - o.refund_amount) * 1.0 / cnt.n) AS net, "
        "  SUM(o.amount * 1.0 / cnt.n) AS revenue, "
        "  SUM(a.purchase_price) AS buy, "
        "  SUM(COALESCE((SELECT SUM(r.cost) FROM asset_repairs r WHERE r.asset_id=a.id),0) "
        "      * 1.0 / MAX(1, " + _ASSET_ORDER_COUNT.format(alias="a") + ")) AS repair, "
        "  SUM(o.shipping_cost * 1.0 / cnt.n) AS ship "
        "FROM order_assets oa "
        "JOIN orders o ON o.id = oa.order_id "
        "JOIN assets a ON a.id = oa.asset_id " + join +
        " JOIN (SELECT order_id, COUNT(*) AS n FROM order_assets GROUP BY order_id) cnt "
        "   ON cnt.order_id = oa.order_id "
        "WHERE o.shipping_done=1 AND o.cancelled_at='' AND o.shipping_at BETWEEN ? AND ?"
        + _NOT_RETURNED +
        " GROUP BY k ORDER BY (SUM((o.amount - o.fee_amount - o.refund_amount) * 1.0 / cnt.n) "
        "  - SUM(a.purchase_price) - SUM(COALESCE((SELECT SUM(r.cost) FROM asset_repairs r "
        "  WHERE r.asset_id=a.id),0))) DESC LIMIT 100",
        (frm, _to_end(to))).fetchall()

    out = []
    for r in rows:
        net = round(r["net"] or 0)
        cost = (r["buy"] or 0) + round(r["repair"] or 0) + round(r["ship"] or 0)
        out.append({
            "key": r["k"] or "(미입력)", "units": r["units"],
            "revenue": round(r["revenue"] or 0), "netRevenue": net,
            "buyCost": r["buy"] or 0, "repairCost": round(r["repair"] or 0),
            "shippingCost": round(r["ship"] or 0), "cost": cost,
            "margin": net - cost,
            "marginPerUnit": round((net - cost) / r["units"]) if r["units"] else 0,
            "marginRate": round((net - cost) / net * 100, 1) if net else 0,
        })
    return jsonify({"from": frm, "to": to, "by": by, "rows": out})


# 서브모듈 라우트 등록 (bp 정의 이후에 import해야 함)
@bp.get("/reports/purchase-dashboard")
def purchase_dashboard():
    """매입 대시보드(2026-08-13 대표, 매입대시보드.hwpx — 토스쇼핑 달력 참고).

    매입일 기준 '일별 코호트': 그날 매입한 자산이 몇 대·얼마이고, 그 자산들이
    지금까지 실현한 판매액·순수익이 얼마인가(그날 판 금액이 아니다 — 매입 관점).
    순수익 = 자산별 배분 실입금(주문금액−수수료−환불, 자산 수로 나눔) − 배분 택배비
             − 매입가 − 수리·부품비(회수는 음수라 자동 가산).
    ★권한 분리(대표 지정): 매입 숫자는 매입 담당(purchase.view)까지, 판매액·순수익은
      관리자 전용 — reports.view 없으면 응답에서 아예 뺀다(0으로 주면 진짜 0과 헷갈린다).
    """
    require("purchase.view")
    require("purchase.money")   # 매입 금액 화면(2026-08-27 대표)
    frm = (request.args.get("from") or "").strip()[:10]
    to = (request.args.get("to") or "").strip()[:10]
    if not frm or not to:
        abort(400, description="조회 기간(from/to)을 지정하세요.")
    if frm > to:
        abort(400, description=f"시작일({frm})이 종료일({to})보다 늦습니다.")

    clause, params = scope_clause("a")
    # 매입일 = 전표 매입일(없으면 자산 등록일). 취소·반품 전표와 매입취소·반품 자산은 제외.
    buy_date = "COALESCE(NULLIF(b.purchase_date,''), substr(a.created_at,1,10))"
    sql = (
        # ★전표 단위까지 쪼개서 준다(2026-08-14 대표) — 달력 칸의 색·툴팁('어떤 매입처에
        #   어떤 매입')과, 칸을 눌렀을 때 뜨는 그날 매입 목록이 같은 숫자를 쓰게 하기 위해서다.
        "SELECT " + buy_date + " AS d, b.id AS bid, b.slip_no AS slip_no, "
        "  COALESCE(rep.name, sp.name, '') AS supplier, COUNT(*) AS qty, "
        "  COALESCE(SUM(a.purchase_price),0) AS buy, "
        "  SUM(CASE WHEN s.revenue IS NOT NULL THEN 1 ELSE 0 END) AS sold_qty, "
        "  COALESCE(SUM(s.revenue),0) AS sale, "
        "  COALESCE(SUM(CASE WHEN s.revenue IS NOT NULL THEN "
        "    s.revenue - s.ship - a.purchase_price - COALESCE(rp.rc,0) END),0) AS profit "
        "FROM assets a "
        "LEFT JOIN purchase_batches b ON b.id = a.batch_id "
        "LEFT JOIN suppliers sp ON sp.id = b.supplier_id "
        "LEFT JOIN suppliers rep ON rep.id = sp.alias_of "   # 별칭 거래처는 대표 이름(2026-09-03)
        "LEFT JOIN (SELECT oa.asset_id, "
        "         SUM((o.amount - o.fee_amount - o.refund_amount) * 1.0 / cnt.n) AS revenue, "
        "         SUM(o.shipping_cost * 1.0 / cnt.n) AS ship "
        "       FROM order_assets oa "
        "       JOIN orders o ON o.id = oa.order_id "
        "       JOIN (SELECT order_id, COUNT(*) AS n FROM order_assets GROUP BY order_id) cnt "
        "         ON cnt.order_id = oa.order_id "
        "       WHERE o.shipping_done=1 AND o.cancelled_at=''" + _NOT_RETURNED +
        "       GROUP BY oa.asset_id) s ON s.asset_id = a.id "
        "LEFT JOIN (SELECT asset_id, SUM(cost) AS rc FROM asset_repairs "
        "           GROUP BY asset_id) rp ON rp.asset_id = a.id "
        "WHERE a.status NOT IN ('cancelled','returned') "
        "  AND (b.id IS NULL OR (b.cancelled_at='' AND b.returned_at='' "
        "                        AND b.stage='purchased')) "
        "  AND " + buy_date + " BETWEEN ? AND ?" + clause)
    args = [frm, to] + list(params)
    supplier = (request.args.get("supplierId") or "").strip()
    if supplier:
        try:
            sql += " AND b.supplier_id = ?"
            args.append(int(supplier))
        except (TypeError, ValueError):
            abort(400, description="거래처 선택이 올바르지 않습니다.")
    for key, op in (("priceMin", ">="), ("priceMax", "<=")):
        v = (request.args.get(key) or "").strip().replace(",", "")
        if v:
            try:
                sql += f" AND a.purchase_price {op} ?"
                args.append(int(v))
            except (TypeError, ValueError):
                abort(400, description="매입가 범위가 올바르지 않습니다.")
    sql += " GROUP BY d, b.id ORDER BY d, b.id"
    rows = get_db().execute(sql, args).fetchall()

    # 판매액·순수익은 관리자(리포트 권한) 전용 — 매입 담당 응답에는 키 자체가 없다
    show_money = g.user["is_admin"] or "reports.view" in g.perms
    by_day, order = {}, []
    totals = {"qty": 0, "buy": 0, "soldQty": 0, "sale": 0, "profit": 0}
    for r in rows:
        day = by_day.get(r["d"])
        if day is None:
            day = {"date": r["d"], "qty": 0, "buy": 0, "slips": []}
            if show_money:
                day.update({"soldQty": 0, "sale": 0, "profit": 0})
            by_day[r["d"]] = day
            order.append(r["d"])
        day["qty"] += r["qty"]
        day["buy"] += r["buy"]
        slip = {"batchId": r["bid"], "slipNo": r["slip_no"] or "",
                "supplier": r["supplier"] or "", "qty": r["qty"], "buy": r["buy"]}
        totals["qty"] += r["qty"]
        totals["buy"] += r["buy"]
        totals["soldQty"] += r["sold_qty"] or 0
        totals["sale"] += round(r["sale"] or 0)
        totals["profit"] += round(r["profit"] or 0)
        if show_money:
            day["soldQty"] += r["sold_qty"] or 0
            day["sale"] += round(r["sale"] or 0)
            day["profit"] += round(r["profit"] or 0)
            slip["sale"] = round(r["sale"] or 0)
            slip["profit"] = round(r["profit"] or 0)
        day["slips"].append(slip)
    if not show_money:
        for k in ("soldQty", "sale", "profit"):
            totals.pop(k)
    return jsonify({"from": frm, "to": to, "days": [by_day[d] for d in order],
                    "totals": totals, "showProfit": show_money})


from . import setup_stats  # noqa: E402,F401
from . import workload  # noqa: E402,F401
