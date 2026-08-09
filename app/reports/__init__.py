"""리포트 — 매입/판매/마진, 재고 회전, 담당자 실적.

원가 = 자산 매입가 + 그 자산의 수리비 합계(A/S 무상 건 포함).
마진 = 주문 금액 − 매칭된 자산들의 원가.
"""
from flask import Blueprint, abort, jsonify, request

from .. import config
from ..auth.perms import require
from ..db import get_db
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
    require("reports.view")
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
        "AND a.received = 1" + scope, sparams).fetchone()
    as_stats = conn.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(CASE WHEN charge_to='company' THEN cost ELSE 0 END),0) AS company_cost "
        "FROM as_tickets WHERE received_at BETWEEN ? AND ?", (frm, to)).fetchone()

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

    return jsonify({
        "from": frm, "to": to,
        "purchase": {"slips": purchase["cnt"], "amount": purchase["amount"],
                     "assets": purchased_assets["cnt"], "assetAmount": purchased_assets["amount"]},
        "sales": {"orders": shipped["cnt"], "revenue": revenue,
                  "fee": fee, "refund": refund, "netRevenue": net_revenue,
                  "cost": total_cost, "buyCost": cost["buy"], "repairCost": cost["repair"],
                  # 수리비에 포함된 부가세(매입세액) — 매출 부가세에서 뺄 수 있는 금액
                  "repairVat": repair_vat, "repairNet": cost["repair"] - repair_vat,
                  "shippingCost": cost["shipping"],
                  "margin": net_revenue - total_cost,
                  "marginRate": round((net_revenue - total_cost) / revenue * 100, 1) if revenue else 0},
        "stock": {"assets": stock["cnt"], "amount": stock["amount"]},
        "as": {"tickets": as_stats["cnt"], "companyCost": as_stats["company_cost"]},
        # 이 숫자들이 크면 위의 마진은 실제보다 부풀려진 값이다
        "dataGaps": {"units": gaps["units"] or 0, "noBuyPrice": gaps["no_buy"] or 0,
                     "ordersWithoutAsset": no_asset, "ordersWithoutAmount": no_amount},
    })


@bp.get("/reports/monthly")
def monthly():
    """월별 매입·출고·마진 추이(최근 12개월)."""
    require("reports.view")
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

    months = sorted(set(purchase) | set(sales), reverse=True)[:12]
    return jsonify([
        {"month": m,
         "purchaseSlips": purchase.get(m, {}).get("slips", 0),
         "purchaseAmount": purchase.get(m, {}).get("amount", 0),
         "orders": sales.get(m, {}).get("orders", 0),
         "revenue": sales.get(m, {}).get("revenue", 0)}
        for m in months
    ])


@bp.get("/reports/channels")
def channels():
    """채널별 판매 실적."""
    require("reports.view")
    frm, to = _period()
    rows = get_db().execute(
        "SELECT channel, COUNT(*) AS orders, COALESCE(SUM(amount),0) AS revenue FROM orders o "
        "WHERE shipping_done=1 AND cancelled_at='' AND shipping_at BETWEEN ? AND ?" + _NOT_RETURNED +
        " GROUP BY channel ORDER BY revenue DESC", (frm, _to_end(to))).fetchall()
    return jsonify([{"channel": r["channel"] or "(미지정)", "orders": r["orders"],
                     "revenue": r["revenue"]} for r in rows])


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
    require("reports.view")
    conn = get_db()
    scope, sparams = scope_clause("a")
    rows = conn.execute(
        "SELECT a.id, a.asset_no, a.maker, a.model, a.grade, a.status, a.purchase_price, "
        " a.created_at, c.name AS category_name "
        "FROM assets a LEFT JOIN categories c ON c.id=a.category_id "
        "WHERE a.status NOT IN ('shipped','scrapped')" + scope +
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
    require("reports.view")
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
    require("reports.view")
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
        headers={"Content-Disposition": f"attachment; filename=hms-ledger-{frm}_{to}.xlsx"})


@bp.get("/reports/profitability")
def profitability():
    """무엇이 남는 장사인가 — 모델·등급·매입처별 마진.

    ?by=model|grade|supplier (기본 model)
    """
    require("reports.view")
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
from . import setup_stats  # noqa: E402,F401
