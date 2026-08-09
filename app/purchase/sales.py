"""TMS 판매 전표 이관 — 판매내역(전표 마스터) + 판매미수금관리(입금) 합치기.

★왜 별도 표인가
  HMS의 orders는 '고객 주문 1건'이다. TMS 판매전표는 그게 아니라 '하루치 채널별 묶음'이라
  S260804-001 하나에 방문구매 26대가 들어 있다. 층위가 달라서 orders에 넣으면
  주문 건수가 통째로 틀어진다. 자산별 내역은 이미 asset_events의 '판매'가 갖고 있고,
  이 표는 그 전표의 금액·정산(부가세·수수료·배송비·입금)을 맡는다.

★TMS는 읽기 전용이다(대표 지시). 여기서 하는 일은 내려받은 엑셀을 HMS에 넣는 것뿐이다.

★값을 덮어쓰지 않는다 — 매입 이관과 같은 규칙.
  이미 있는 전표는 '빈 칸만' 채운다. 사람이 HMS에서 고친 값이 다음 이관에 되돌아가면
  고칠 이유가 없어진다.
"""
from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import tx
from ..importers import read_first_sheet
from . import bp
from .migration import _to_date, _to_int

# 판매내역(전표 마스터) 칸 → DB 컬럼
MASTER_MAP = {
    "판매채널": "channel", "판매처명": "customer", "판매메모": "memo",
    "판매일": "sale_date", "출고일": "ship_date", "반입일": "return_date",
    "송장번호": "waybill", "매입금액": "purchase_amount",
    "부가세": "vat", "수수료": "fee", "착불": "cod", "배송비": "shipping",
    "진행상태": "stage",
    # ★헤더 값과 명세 합계를 둘 다 넣는다 — 하나만 고르면 이관에서 값이 사라진다
    "수량": "head_qty", "판매수량": "qty",
    "판매금액": "sale_amount", "판매가": "item_sale_sum",
    "판매차이금액": "diff_amount",
    "순이익액": "profit", "순이익": "item_profit",
}
# 판매미수금관리(입금) 칸 → DB 컬럼. 거래처명은 마스터의 판매처명과 같은 자리다.
PAID_MAP = {
    "판매채널": "channel", "거래처명": "customer", "판매일": "sale_date",
    "출고일": "ship_date", "납부확인": "paid_status", "매입금액": "purchase_amount",
    "부가세": "vat", "수수료": "fee", "판매금액": "sale_amount", "착불": "cod",
    "배송비": "shipping", "순이익액": "profit", "입금일시": "paid_at",
    "입금액": "paid_amount", "입금확인": "paid_confirmed",
}
DATE_COLS = ("sale_date", "ship_date", "return_date", "paid_at")
INT_COLS = ("qty", "head_qty", "purchase_amount", "sale_amount", "item_sale_sum",
            "diff_amount", "vat", "fee", "cod", "shipping", "profit",
            "item_profit", "paid_amount", "paid_confirmed")
ALL_COLS = tuple(sorted(set(MASTER_MAP.values()) | set(PAID_MAP.values())))


def is_sale_slip_sheet(rows):
    """판매 전표 표인가 — 판매전표 칸이 있고 관리번호 칸이 없어야 한다.

    ★관리번호가 있으면 판매'현황'(자산별)이라 자산 이관이 가져가야 한다.
      여기서 삼키면 자산 13,302건이 전표 421건으로 뭉개진다.
    """
    if not rows:
        return False
    keys = {str(k).strip() for k in rows[0].keys()}
    return "판매전표" in keys and "관리번호" not in keys


def _row_to_fields(raw, mapping):
    out = {}
    for src, col in mapping.items():
        if src not in raw:
            continue
        v = raw[src]
        if col in DATE_COLS:
            out[col] = _to_date(v)
        elif col in INT_COLS:
            out[col] = _to_int(v)
        else:
            out[col] = str(v or "").strip()
    return out


def apply_sale_slips(conn, rows, actor="이관"):
    """전표를 넣거나 빈 칸을 채운다. 판매내역·판매미수금 어느 쪽이든 같은 코드를 탄다."""
    keys = {str(k).strip() for k in (rows[0].keys() if rows else ())}
    mapping = PAID_MAP if "입금확인" in keys else MASTER_MAP
    ts = config.now_iso()
    # ★dict으로 풀어 둔다. 같은 실행에서 방금 넣은 전표를 다시 만나면
    #   sqlite3.Row와 달리 아직 없는 칸을 읽어야 하는데, Row였다면 KeyError가 난다
    #   (판매내역으로 만든 전표를 판매미수금이 이어서 채우는 게 정상 흐름이다).
    exist = {}
    for r in conn.execute("SELECT * FROM sale_slips").fetchall():
        exist[r["slip_no"]] = {c: r[c] for c in ALL_COLS}

    created = updated = skipped = 0
    for raw in rows:
        slip = str(raw.get("판매전표") or "").strip()
        if not slip:
            skipped += 1
            continue
        fields = _row_to_fields(raw, mapping)
        old = exist.get(slip)
        if old is None:
            cols = ["slip_no"] + list(fields) + ["created_at", "updated_at"]
            vals = [slip] + [fields[c] for c in fields] + [ts, ts]
            conn.execute(
                f"INSERT INTO sale_slips({','.join(cols)}) "
                f"VALUES({','.join('?' * len(cols))})", vals)
            # 방금 넣은 것도 같은 파일 안에서 두 번 나오면 '있는 것'으로 봐야 한다.
            # 안 넣은 칸은 기본값으로 채워 둬야 다음 파일이 빈 칸 판정을 할 수 있다.
            exist[slip] = {c: fields.get(c, 0 if c in INT_COLS else "") for c in ALL_COLS}
            created += 1
            continue
        # ★빈 칸만 채운다 — 0이나 빈 문자열일 때만
        fill = {c: v for c, v in fields.items()
                if v not in ("", 0) and not old[c]}
        if not fill:
            skipped += 1
            continue
        conn.execute(
            "UPDATE sale_slips SET " + ", ".join(f"{c}=?" for c in fill) +
            ", updated_at=? WHERE slip_no=?",
            list(fill.values()) + [ts, slip])
        for c, v in fill.items():
            exist[slip][c] = v
        updated += 1
    return {"created": created, "updated": updated, "skipped": skipped}


@bp.get("/sale-slips")
def sale_slips():
    """판매 전표 목록 — 기간·채널·미입금 필터."""
    require("purchase.view")
    q = (request.args.get("q") or "").strip()
    frm = (request.args.get("from") or "").strip()
    to = (request.args.get("to") or "").strip()
    channel = (request.args.get("channel") or "").strip()
    unpaid = request.args.get("unpaid") == "1"

    where, params = ["1=1"], []
    if q:
        where.append("(slip_no LIKE ? OR customer LIKE ? OR channel LIKE ?)")
        params += [f"%{q}%"] * 3
    if frm:
        where.append("sale_date >= ?")
        params.append(frm)
    if to:
        where.append("sale_date <= ?")
        params.append(to)
    if channel:
        where.append("channel = ?")
        params.append(channel)
    if unpaid:
        where.append("paid_confirmed = 0")
    sql = " AND ".join(where)

    with tx() as conn:
        # ★501개를 뽑아 '진짜 잘렸는지'를 정확히 판정한다.
        #   len(rows) >= 500 으로 보면 딱 500건일 때도 잘렸다고 거짓 경고한다.
        rows = conn.execute(
            "SELECT * FROM sale_slips WHERE " + sql +
            " ORDER BY sale_date DESC, slip_no DESC LIMIT 501", params).fetchall()
        capped = len(rows) > 500
        rows = rows[:500]
        # ★취소·반입은 매출이 아니다. 섞어 놓으면 대표가 보는 판매금액이 부풀려진다
        #   (실측 421건 중 취소 5건 1,590만 + 반입 4건 2,796만 = 4,386만원).
        agg = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(qty),0) qty, "
            "COALESCE(SUM(sale_amount),0) sale, COALESCE(SUM(purchase_amount),0) buy, "
            "COALESCE(SUM(vat),0) vat, COALESCE(SUM(fee),0) fee, "
            "COALESCE(SUM(shipping),0) ship, COALESCE(SUM(profit),0) profit "
            "FROM sale_slips WHERE (" + sql + ") AND stage NOT IN ('판매취소','반입')",
            params).fetchone()
        excl = conn.execute(
            "SELECT stage, COUNT(*) n, COALESCE(SUM(sale_amount),0) sale "
            "FROM sale_slips WHERE (" + sql + ") AND stage IN ('판매취소','반입') "
            "GROUP BY stage", params).fetchall()
        allcnt = conn.execute(
            "SELECT COUNT(*) FROM sale_slips WHERE " + sql, params).fetchone()[0]
        channels = [r["channel"] for r in conn.execute(
            "SELECT DISTINCT channel FROM sale_slips "
            "WHERE channel<>'' ORDER BY channel").fetchall()]
    return jsonify({
        "slips": [{
            "id": r["id"], "slipNo": r["slip_no"], "channel": r["channel"],
            "customer": r["customer"], "saleDate": r["sale_date"],
            "shipDate": r["ship_date"], "returnDate": r["return_date"],
            "waybill": r["waybill"], "qty": r["qty"],
            "purchaseAmount": r["purchase_amount"], "saleAmount": r["sale_amount"],
            "vat": r["vat"], "fee": r["fee"], "cod": r["cod"],
            "shipping": r["shipping"], "profit": r["profit"], "stage": r["stage"],
            "headQty": r["head_qty"], "itemSaleSum": r["item_sale_sum"],
            "diffAmount": r["diff_amount"], "itemProfit": r["item_profit"],
            "paidStatus": r["paid_status"], "paidAt": r["paid_at"],
            "paidAmount": r["paid_amount"], "paidConfirmed": bool(r["paid_confirmed"]),
            "memo": r["memo"], "source": r["source"],
        } for r in rows],
        "channels": channels,
        # total = 취소·반입을 뺀 '실제 매출'
        "total": {
            "count": agg["n"], "qty": agg["qty"], "sale": agg["sale"],
            "purchase": agg["buy"], "vat": agg["vat"], "fee": agg["fee"],
            "shipping": agg["ship"], "profit": agg["profit"],
        },
        # 뺀 것을 감추지 않고 따로 보여 준다 — 합계가 왜 다른지 대표가 알아야 한다
        "excluded": [{"stage": r["stage"], "count": r["n"], "sale": r["sale"]} for r in excl],
        "allCount": allcnt,            # 조건에 맞는 전표 수(취소·반입 포함)
        "shown": len(rows),            # 표에 실제로 그린 줄 수
        "capped": capped,
    })


@bp.post("/sale-slips/import")
def sale_slips_import():
    """판매내역 / 판매미수금관리 엑셀을 올려 전표를 채운다."""
    require("purchase.edit")
    files = request.files.getlist("files")
    if not files:
        abort(400, description="엑셀 파일을 선택하세요.")
    out = {"created": 0, "updated": 0, "skipped": 0, "files": []}
    with tx(write=True) as conn:
        for f in files:
            content = f.read()
            if len(content) > 20 * 1024 * 1024:
                abort(400, description=f"파일이 너무 큽니다(20MB 초과): {f.filename}")
            try:
                rows = read_first_sheet(content, f.filename or "upload")
            except Exception:
                abort(400, description=f"엑셀을 해석하지 못했습니다: {f.filename}")
            if not is_sale_slip_sheet(rows):
                out["files"].append({"name": f.filename, "skipped": "판매 전표 표가 아닙니다"})
                continue
            res = apply_sale_slips(conn, rows)
            out["files"].append({"name": f.filename, "rows": len(rows), **res})
            for k in ("created", "updated", "skipped"):
                out[k] += res[k]
        audit.log("sale_slips_import", detail=out)
    return jsonify(out)
