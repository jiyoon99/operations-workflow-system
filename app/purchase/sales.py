"""TMS 판매 전표 이관 — 판매내역(전표 마스터) + 판매미수금관리(입금) 합치기.

★왜 별도 표인가
  OWS의 orders는 '고객 주문 1건'이다. TMS 판매전표는 그게 아니라 '하루치 채널별 묶음'이라
  S260804-001 하나에 방문구매 26대가 들어 있다. 층위가 달라서 orders에 넣으면
  주문 건수가 통째로 틀어진다. 자산별 내역은 이미 asset_events의 '판매'가 갖고 있고,
  이 표는 그 전표의 금액·정산(부가세·수수료·배송비·입금)을 맡는다.

★TMS는 읽기 전용이다(대표 지시). 여기서 하는 일은 내려받은 엑셀을 OWS에 넣는 것뿐이다.

★사람이 OWS에서 고친 값은 덮어쓰지 않는다 — 매입 이관과 같은 규칙.
  고친 값이 다음 이관에 되돌아가면 고칠 이유가 없어진다(아래 ows_edited_at 규칙).

★안 고친 연동 전표는 TMS 값을 그대로 따라간다(2026-09-03 — 창구 동시 마감 준비, 계획서 §2 대사 행).
  예전에는 '빈 칸만' 채웠다. 그런데 TMS 전표는 하루 종일 라인이 붙으면서 헤더(수량·판매금액·
  판매채널·명세 합계)가 계속 바뀌는데, 2분 틱이 입력 도중의 전표를 먼저 보고 그 값을 굳혀
  2026-08-12 이후 전표 16건이 TMS와 달랐다(S260831-001: OWS 6대/9,042,000 vs TMS 42대/15,017,400).
  아무도 OWS에서 손대지 않은 연동 전표는 TMS가 정본이므로 값이 달라지면(빈 값이 되는 것 포함)
  그대로 따라간다. 창구의 판매내역·판매미수금관리 두 화면은 같은 칸에 같은 값을 준다(실측 충돌 0건).

★입금 4칸(납부확인·입금일시·입금액·입금확인)은 OWS에서 고친 연동 전표라도 TMS를 따라간다 —
  TMS가 원본이고 OWS에는 편집 화면이 없다. 빈 칸 채우기만 하면 부분입금 뒤 추가 입금이
  옛값에 영영 굳는다(2026-08-09 확인).

★입금확인은 창구(JSON)가 'True'/'False' 로 준다(엑셀은 1/0) — 숫자 변환만 하면 'True'가 0이 되어
  완납 전표가 미수금으로 잡힌다(2026-09-03 실측: S260831-001·S260903-001). _to_flag_int 가 받아 준다.

★OWS 등록 창구(2026-09-02 대표 방침 "모든 데이터는 OWS·RMS에서 직접 등록·관리").
  전표·명세를 OWS에서 직접 만들고 고친다(sale_entry.py). 그래서 보호 규칙이 하나 더 붙는다:
  - source='ows' 전표(OWS가 만든 전표, 번호대 S…-500~) : 연동 행이 와도 아무것도 안 덮는다.
    입금 4칸도 OWS가 원본이다.
  - ows_edited_at 이 찍힌 연동 전표(TMS 전표를 OWS에서 고친 것) : 편집 가능 칸은 안 덮고,
    입금 4칸만 계속 TMS를 따라간다(그 전표의 입금은 아직 TMS가 원본이라서).
  - ows_edited_at 이 찍힌 명세(tms_sales 행) : 값을 안 덮는다(asset_id 연결만).
  - OWS 전표의 살아 있는 라인에 있는 자산이 TMS 판매현황에 다른 전표로 오면 새 명세를
    만들지 않는다 — 같은 기계가 두 전표에 팔린 것으로 잡혀 매출이 이중이 된다.
"""
from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require, require_any
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
# ★TMS가 원본인 입금 칸 — 빈 칸 채우기가 아니라 최신값을 따라간다(위 머리주석 참조)
PAID_SYNC_COLS = ("paid_status", "paid_at", "paid_amount", "paid_confirmed")
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


# ---------------------------------------------------------------- 자산 단위 판매
#   (2026-08-25 대표) 판매현황.xlsx = 자산 한 대가 한 줄. TMS의 자산별 마진을 OWS 로.
#   매칭 축은 자산번호 — 주문은 지어내지 않는다(매출 이중 방지). TMS 정본이라 정정도 따라온다.

SALE_ASSET_MAP = {
    "판매전표": "slip_no", "관리번호": "asset_no", "수령자성함": "customer",
    "판매채널": "channel", "판매처명": "seller", "모델명": "model",
    "판매상세비고": "memo", "판매일": "sale_date", "출고일": "ship_date",
    "반입일": "return_date", "매입가": "purchase_price", "판매가": "sale_price",
    "순이익": "tms_profit", "등급": "grade", "진행상태": "stage",
    "매입전표": "purchase_slip", "매입처명": "supplier_name",
    # ★자산별 옵션가(2026-09-02) — 연동 창구가 판매현황 행에 TMS 판매상세 칸을 실어 준다.
    #   헤더가 없는 행은 예전과 똑같이 동작한다(없는 칸은 건드리지 않는다).
    "업그레이드1가": "upgrade1_price", "업그레이드2가": "upgrade2_price",
    "탈거가": "removal_price", "기타구성가": "extra_price", "충전기가": "charger_price",
    "포장료": "packing_fee", "판매부가세": "sale_vat", "판매수수료": "sale_fee",
    "업그레이드1항목": "upgrade1_item", "업그레이드2항목": "upgrade2_item",
    "탈거항목": "removal_item", "기타구성품": "extra_items",
    "상세취소일": "cancel_date",
    # 순이익 산식 TMS 대조(2026-09-03) — 실부가세 = 판매부가세 − 매입부가세, 판매수수료 = 판매가 × 판매수수율
    "매입부가세": "buy_vat", "판매수수율": "fee_rate",
    # ★판매 시점 원가 스냅샷(2026-09-03 대표 승인) — 창구가 판매상세 원본 값을 그대로 실어 준다.
    #   이 칸들이 들어오기 전에는 OWS가 아는 원가가 '매입가' 하나뿐이라 TMS 순이익을 재현할 수 없었다
    #   (라이브 5,901행 중 일치 0건). 창구 실측: 이 칸이 실린 5,326행은 산식이 98.7% 맞는다.
    "제조원가": "manufacture_cost", "수리비": "tms_repair_cost", "부품비": "tms_part_cost",
    "실부가세": "net_vat",
    # 상세상태(판매상세의 행 상태)가 진행상태(전표 상태)보다 뒤에 있어 둘 다 오면 상세가 이긴다
    "상세상태": "stage",
}
_SA_DATES = {"sale_date", "ship_date", "return_date", "cancel_date"}
_SA_INTS = {"purchase_price", "sale_price", "tms_profit",
            "upgrade1_price", "upgrade2_price", "removal_price", "extra_price",
            "charger_price", "packing_fee", "sale_vat", "sale_fee", "buy_vat",
            "manufacture_cost", "tms_repair_cost", "tms_part_cost", "net_vat"}
_SA_FLOATS = {"fee_rate"}                      # 판매수수율(%) — 3.0 / 0.0


def _to_float(v):
    try:
        return float(str(v).replace(",", "").replace("%", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0
# 같은 컬럼을 두 헤더가 가리킬 수 있어(진행상태·상세상태 → stage) 중복을 걷어 낸다
_SA_COLS = tuple(dict.fromkeys(SALE_ASSET_MAP.values()))
# 살아 있는 라인 = 취소·반입이 아닌 것. 전표 합계·중복 판정이 전부 이 조건을 쓴다.
DEAD_STAGES = ("판매취소", "반입")


def is_sale_asset_sheet(rows):
    """자산 단위 판매표인가 — 판매전표+관리번호+수령자성함이 다 있어야 한다.

    (판매전표만 있으면 전표표, 관리번호만 있으면 자산표 — 이 표는 둘 다 갖고 있어
    자산 이관'과' 여기 둘 다 탄다: 자산 빈칸 채우기 + 판매 원장.)
    """
    if not rows:
        return False
    keys = {str(k).strip() for k in rows[0].keys()}
    return {"판매전표", "관리번호", "수령자성함"} <= keys


def upsert_asset_sales(conn, rows, actor="자동반영"):
    """자산 단위 판매를 넣거나 최신화한다 — (전표, 자산번호)가 열쇠, TMS 값이 정본.

    ★OWS 보호(2026-09-02): ows_edited_at 이 찍힌 명세는 값을 안 덮는다(asset_id 연결만).
      OWS 전표의 살아 있는 라인에 있는 자산이 다른 전표로 오면 새 명세를 만들지 않는다
      (같은 기계가 두 전표에 팔린 것으로 잡혀 매출이 이중이 된다). 둘 다 protected 로 센다.
    """
    ts = config.now_iso()
    exist = {}
    ows_live = set()          # OWS 전표(source='ows')의 살아 있는 라인에 있는 자산번호
    for r in conn.execute("SELECT * FROM tms_sales").fetchall():
        exist[(r["slip_no"], r["asset_no"])] = ({c: r[c] for c in _SA_COLS}
                                               | {"id": r["id"], "_ows": r["ows_edited_at"]})
        if r["source"] == "ows" and r["stage"] not in DEAD_STAGES:
            ows_live.add(r["asset_no"])
    aid_of = {r["asset_no"]: r["id"]
              for r in conn.execute("SELECT id, asset_no FROM assets").fetchall()}
    created = updated = skipped = protected = 0
    for raw in rows:
        slip = str(raw.get("판매전표") or "").strip()
        ano = str(raw.get("관리번호") or "").strip()
        if not slip or not ano:
            skipped += 1
            continue
        fields = {}
        for src, col in SALE_ASSET_MAP.items():
            if src not in raw:
                continue
            v = raw[src]
            if col in _SA_DATES:
                fields[col] = _to_date(v)
            elif col in _SA_INTS:
                fields[col] = _to_int(v)
            elif col in _SA_FLOATS:
                fields[col] = _to_float(v)
            else:
                fields[col] = str(v or "").strip()
        old = exist.get((slip, ano))
        aid = aid_of.get(ano)
        if old is None:
            if ano in ows_live:
                # OWS 전표에 이미 팔린 것으로 등록된 기계 — TMS 쪽 중복 등록은 받지 않는다
                protected += 1
                continue
            cols = list(fields) + ["asset_id", "created_at", "updated_at"]
            conn.execute(
                f"INSERT INTO tms_sales({','.join(cols)}) "
                f"VALUES({','.join('?' * len(cols))})",
                [fields[c] for c in fields] + [aid, ts, ts])
            exist[(slip, ano)] = {c: fields.get(c, 0 if c in _SA_INTS or c in _SA_FLOATS else "")
                                  for c in _SA_COLS} | {"id": None, "_ows": ""}
            created += 1
        else:
            if old.get("_ows"):
                # 사람이 OWS에서 고친 명세 — 값은 불가침, 자산 연결만 보강한다
                if aid is not None and old["id"] is not None:
                    conn.execute("UPDATE tms_sales SET asset_id=? WHERE id=? AND asset_id IS NULL",
                                 (aid, old["id"]))
                protected += 1
                continue
            changes = {c: v for c, v in fields.items()
                       if c not in ("slip_no", "asset_no") and old.get(c) != v}
            if changes and old["id"] is not None:
                sets = ", ".join(f"{c}=?" for c in changes)
                conn.execute(
                    f"UPDATE tms_sales SET {sets}, asset_id=?, updated_at=? WHERE id=?",
                    list(changes.values()) + [aid, ts, old["id"]])
                old.update(changes)
                updated += 1
            elif changes:
                # 같은 실행에서 방금 넣은 행이 다시 왔다 — 열쇠로 갱신한다
                sets = ", ".join(f"{c}=?" for c in changes)
                conn.execute(
                    f"UPDATE tms_sales SET {sets}, asset_id=?, updated_at=? "
                    "WHERE slip_no=? AND asset_no=?",
                    list(changes.values()) + [aid, ts, slip, ano])
                old.update(changes)
                updated += 1
            elif aid is not None:
                conn.execute("UPDATE tms_sales SET asset_id=? WHERE slip_no=? AND asset_no=? "
                             "AND asset_id IS NULL", (aid, slip, ano))
    matched = conn.execute(
        "SELECT COUNT(*) AS c FROM tms_sales WHERE asset_id IS NOT NULL").fetchone()["c"]
    return {"created": created, "updated": updated, "skipped": skipped, "matched": matched,
            "protected": protected}


_TRUE_WORDS = ("true", "y", "yes", "t")
_FALSE_WORDS = ("false", "n", "no", "f")


def _to_flag_int(v):
    """정수 칸 변환 — 창구 JSON 의 bit 칸('True'/'False'·bool)도 1/0 으로. 그 밖은 _to_int 그대로."""
    if isinstance(v, bool):
        return int(v)
    w = str(v or "").strip().lower()
    if w in _TRUE_WORDS:
        return 1
    if w in _FALSE_WORDS:
        return 0
    return _to_int(v)


def _row_to_fields(raw, mapping):
    out = {}
    for src, col in mapping.items():
        if src not in raw:
            continue
        v = raw[src]
        if col in DATE_COLS:
            out[col] = _to_date(v)
        elif col in INT_COLS:
            out[col] = _to_flag_int(v)
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
        exist[r["slip_no"]] = ({c: r[c] for c in ALL_COLS}
                               | {"_source": r["source"], "_ows": r["ows_edited_at"]})

    created = updated = skipped = protected = 0
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
            exist[slip] = ({c: fields.get(c, 0 if c in INT_COLS else "") for c in ALL_COLS}
                           | {"_source": "TMS이관", "_ows": ""})
            created += 1
            continue
        # ★OWS가 만든 전표(source='ows')는 연동이 손대지 않는다 — 입금 4칸까지 OWS가 원본이다.
        #   (번호대가 달라 실제로 겹칠 일은 없지만, 겹쳐도 사람 입력이 이긴다.)
        if old.get("_source") == "ows":
            protected += 1
            continue
        if old.get("_ows"):
            # 사람이 OWS에서 고친 연동 전표 — 편집 가능 칸은 불가침. 입금 4칸만 TMS를 따라간다.
            fill = {c: fields[c] for c in PAID_SYNC_COLS
                    if c in fields and fields[c] != old[c]}
            protected += 1
        else:
            # ★아무도 OWS에서 손대지 않은 연동 전표 — TMS가 정본이라 달라진 칸은 전부 따라간다(2026-09-03).
            #   행에 실려 온 칸만 본다(화면마다 칸이 달라 없는 칸은 손대지 않는다). 빈 값이 된 것도 따라간다.
            #   예전 '빈 칸만 채우기'는 입력 도중의 전표를 굳혀 헤더 수량·금액이 TMS와 달라지는 결함이 있었다(머리주석).
            fill = {c: v for c, v in fields.items() if v != old[c]}
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
    return {"created": created, "updated": updated, "skipped": skipped, "protected": protected}


@bp.get("/sale-slips")
def sale_slips():
    """판매 전표 목록 — 기간·채널·미입금 필터."""
    require("purchase.view")
    require_any("purchase.money", "reports.view", "settings.manage")
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
    # 미수금 표의 '전표 보기' — 거래처명 정확 일치. '(미지정)'은 빈 이름을 뜻한다
    # (LIKE 검색으로는 빈 문자열을 못 찾는다, 2026-08-09 검토에서 발견).
    customer_exact = request.args.get("customerExact")
    if customer_exact is not None and customer_exact != "":
        where.append("customer = ?")
        params.append("" if customer_exact == "(미지정)" else customer_exact)
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
            # OWS 등록 창구(2026-09-02) — 화면이 'OWS 등록 / OWS 수정 / TMS 연동' 배지를 그린다
            "isOws": r["source"] == "ows",
            "owsEditedAt": r["ows_edited_at"], "owsEditedBy": r["ows_edited_by"],
            "cancelDate": r["cancel_date"],
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


@bp.get("/sale-slips/receivables")
def sale_slips_receivables():
    """거래처별 미수금 잔액·연령(30/60/90일) — 취소·반입 제외(2026-08-09 대표 승인).

    판매 전표 화면 상단과 대시보드 KPI가 쓴다. 500건 캡이 있는 목록으로 합산하면
    틀리므로 반드시 서버에서 집계한다.
    """
    require("purchase.view")
    today = config.now_iso()[:10]
    with tx() as conn:
        rows = conn.execute(
            "SELECT customer, COUNT(*) AS n, "
            "  COALESCE(SUM(sale_amount - paid_amount),0) AS balance, "
            "  MIN(CASE WHEN sale_date != '' THEN sale_date END) AS oldest, "
            "  COALESCE(SUM(CASE WHEN sale_date >= date(?, '-30 day') "
            "      THEN sale_amount - paid_amount ELSE 0 END),0) AS d30, "
            "  COALESCE(SUM(CASE WHEN sale_date < date(?, '-30 day') "
            "      AND sale_date >= date(?, '-60 day') "
            "      THEN sale_amount - paid_amount ELSE 0 END),0) AS d60, "
            "  COALESCE(SUM(CASE WHEN sale_date < date(?, '-60 day') "
            "      AND sale_date >= date(?, '-90 day') "
            "      THEN sale_amount - paid_amount ELSE 0 END),0) AS d90, "
            "  COALESCE(SUM(CASE WHEN sale_date != '' AND sale_date < date(?, '-90 day') "
            "      THEN sale_amount - paid_amount ELSE 0 END),0) AS over90 "
            "FROM sale_slips "
            "WHERE paid_confirmed = 0 AND stage NOT IN ('판매취소','반입') "
            "  AND sale_amount - paid_amount > 0 "
            "GROUP BY customer ORDER BY balance DESC",
            (today,) * 6).fetchall()
    total = sum(r["balance"] for r in rows)
    return jsonify({
        "total": total, "count": len(rows),
        "customers": [{
            "customer": r["customer"] or "(미지정)", "slips": r["n"],
            "balance": r["balance"], "d30": r["d30"], "d60": r["d60"],
            "d90": r["d90"], "over90": r["over90"], "oldest": r["oldest"] or "",
        } for r in rows],
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
