"""A/S 관리 — 접수 → 회수 → 수리 → 반송.

자산번호(관리번호)와 연결해 그 제품의 A/S 이력이 자산 타임라인에도 남게 한다.
수리비는 자산 원가에 반영할지 선택할 수 있다(회사 부담이면 자산 수리비로 기록).
"""
import json
import re
from datetime import timedelta

from flask import Blueprint, abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require, require_any
from ..db import get_db, tx
from ..purchase import ASSET_STATUSES, asset_event, split_vat
from ..settings import _int_or_400

bp = Blueprint("asvc", __name__, url_prefix="/api")

AS_TYPES = {"repair": "수리", "exchange": "교환", "refund": "환불", "inspect": "점검"}
AS_STATUSES = {
    "received": "접수",
    "collecting": "회수 중",
    # ★'입고 완료'(2026-09-07 대표 개편) — 물건이 우리 손에 있는데 아직 수리를 시작하지 않은
    #   상태. 예전에는 회수 입고가 곧바로 '수리 중'이 되어, 도착만 했는지 손을 댔는지를
    #   구분할 수 없었다. 회수 택배 배달완료(CJ 91)·[입고 처리]·방문 접수가 여기로 온다.
    "arrived": "입고 완료",
    "repairing": "수리 중",
    "done": "수리 완료",
    "returned": "반송 완료",
    "cancelled": "취소",
}
CHARGE = {"company": "무상(회사 부담)", "customer": "유상(고객 청구)"}
# 접수 경로(2026-08-31 대표) — 택배는 회수 예약이 필요하고, 방문은 고객이 직접 들고 온다.
INTAKE = {"parcel": "택배", "visit": "방문"}
# 돌려줄 방법(2026-09-03 대표) — 택배로 보내거나, 고객이 찾으러 온다
RETURN_METHODS = {"parcel": "택배 발송", "visit": "방문 수령"}
OPEN_STATUSES = ("received", "collecting", "arrived", "repairing", "done")


def payment_pending(row):
    """유상인데 아직 결제 도장이 없다 — **금액을 아직 안 적었어도 참**이다.

    ★잠금·보드 칸은 이 값으로 판정한다. 금액(unpaid_bill)으로 판정하면 안 된다 —
      '무상 → 유상'으로 바꾸고 금액을 아직 안 적은 건이 [💰 결제 전]을 건너뛰고
      [📦 발송 전]으로 가서, 돈을 못 받은 채 송장이 나간다
      (2026-09-08 대표 신고: AS-260908-01 — 수리 완료 뒤에 유상으로 바꿨더니 발송 전으로 갔다).
      금액 0원인 유상은 '아직 청구액을 안 적은 것'이지 '받을 돈이 없는 것'이 아니다.
      정말 받을 게 없으면 비용 부담을 무상으로 되돌리는 것이 맞다."""
    k = row.keys()
    if row["charge_to"] != "customer":
        return False
    return not bool((row["paid_at"] if "paid_at" in k else "") or "")


def unpaid_bill(row):
    """이 건에서 아직 못 받은 청구액 — 유상 미결제면 그 금액(수리비+추가비용), 아니면 0.

    ★'얼마인가'를 묻는 값이라 **화면 표시·합계 전용**이다. '막을까 말까'는 payment_pending
      으로 묻는다(금액을 안 적은 유상 건에서 0이 되어 잠금이 풀렸다 — 위 주석 참고)."""
    k = row.keys()
    if not payment_pending(row):
        return 0
    bill = (row["cost"] or 0) + ((row["extra_charge"] if "extra_charge" in k else 0) or 0)
    return bill if bill > 0 else 0


def payment_block_reason(row, what):
    """유상 미결제라 `what`(‘송장을 발급할’ 같은 말)을 막아야 하면 사유 문장, 아니면 None.

    ★금액을 아직 안 적은 유상 건은 안내를 달리한다 — 같은 문구를 쓰면 "청구 0원 미결제"라는
      말이 안 되는 문장이 나가고, 담당자가 무엇을 해야 풀리는지 알 수 없다."""
    if not payment_pending(row):
        return None
    owed = unpaid_bill(row)
    if owed:
        return (f"유상 건은 결제 확인 후에 {what} 수 있습니다"
                f"(청구 {owed:,}원 미결제 — [💰 결제 확인]부터).")
    return (f"유상 건인데 청구 금액이 아직 없습니다 — 🧾 수리 내역에 금액을 적고 "
            f"[💰 결제 확인]을 한 뒤에 {what} 수 있습니다. "
            "받을 돈이 없는 건이라면 비용 부담을 무상으로 되돌려 주세요.")


# 회수 구성품 기본 목록(2026-09-08 대표 예시 "본체, 충전기" / "본체, 충전기, 키스킨, 가방").
# 저장한 적이 없을 때만 쓴다 — 한 번 저장하면 빈 목록도 그대로 존중한다(일부러 비운 것).
DEFAULT_INTAKE_PRESETS = ["본체", "충전기", "키스킨", "가방", "마우스", "파우치", "박스"]


def _intake_presets(conn):
    v = _kv_json(conn, "as_intake_presets")
    if not isinstance(v, list):
        return list(DEFAULT_INTAKE_PRESETS)
    return [str(x) for x in v]


def clean_intake_items(value):
    """회수 품목 한 줄로 정리 — "본체, 충전기, 키스킨, 가방"(2026-09-08 대표).

    쉼표·줄바꿈·가운뎃점 중 무엇으로 적어도 ', ' 하나로 맞춘다 — 문자에 그대로 실리는
    값이라 표기가 흔들리면 고객이 받는 문장이 매번 달라진다. 빈 칸은 지운다."""
    s = str(value or "").replace("\r", "\n")
    parts = [p.strip(" \t·") for p in re.split(r"[,\n]+", s)]
    return ", ".join(p for p in parts if p)[:200]


# 증상 분류(2026-08-31 대표 저녁 — "대분류/소분류/사유"). 대분류=장비 종류,
# 소분류=성격. 목록이 아직 저장 전이면 대표가 예시한 기본값으로 시작한다.
DEFAULT_SYM_CATS = ["데스크탑", "노트북", "태블릿"]
DEFAULT_SYM_SUBS = ["H/W", "S/W", "OS", "택배파손"]


def _symptom_cats(conn):
    """증상 분류 목록(KV as_symptom_cats). 저장한 적 없으면 기본값 —
    저장한 뒤에는 빈 목록도 그대로 존중한다(일부러 지운 것)."""
    v = _kv_json(conn, "as_symptom_cats")
    if not isinstance(v, dict):
        return {"cats": list(DEFAULT_SYM_CATS), "subs": list(DEFAULT_SYM_SUBS)}
    return {"cats": [str(x) for x in (v.get("cats") or [])],
            "subs": [str(x) for x in (v.get("subs") or [])]}


def _clean_sym(value, label):
    s = (str(value or "")).strip()
    if len(s) > 30:
        abort(400, description=f"{label} 값이 너무 깁니다(30자 이내).")
    return s


def _next_ticket_no(conn):
    """AS-YYMMDD-NN. ★자리 수를 넘겨도 다음 번호를 계속 찾는다(2026-09-02 감사).

    예전에는 GLOB 가 두 자리만 훑어서, 하루 99건을 넘기면 100번째가
    'AS-260902-100' 으로 저장된 뒤 그 값을 다음 계산에서 못 봤다 →
    같은 100번을 또 내려다 유일 제약에 걸려 '저장이 안 된다'가 됐다.
    두 자리 이상도 함께 세고, 최소 두 자리로 채운다.
    """
    prefix = f"AS-{config.now().strftime('%y%m%d')}-"
    row = conn.execute(
        "SELECT MAX(CAST(substr(ticket_no, 11) AS INTEGER)) AS m FROM as_tickets "
        "WHERE ticket_no GLOB ? AND substr(ticket_no, 11) NOT GLOB '*[^0-9]*'",
        (prefix + "[0-9][0-9]*",)).fetchone()
    return f"{prefix}{(row['m'] or 0) + 1:02d}"


def _ticket_payload(r):
    k = r.keys()
    return {
        "id": r["id"], "ticketNo": r["ticket_no"],
        # 재발 — 이 기계/이 고객의 '다른' A/S 건수(목록에도 상세에도 뜬다)
        "repeatAsset": (r["repeat_asset"] if "repeat_asset" in k else 0),
        "repeatCust": (r["repeat_cust"] if "repeat_cust" in k else 0),
        "orderId": r["order_id"], "assetId": r["asset_id"],
        # 자산 연결이 있으면 자산의 번호가 진실이고, 없으면 수기값(2026-08-31 대표 —
        # 접수 후 적을 칸). 모델명은 수기값이 화면 표기를 덮을 수 있다(자산은 안 고친다).
        "returnMethod": (r["return_method"] if "return_method" in k else "parcel"),
        "assetNo": (r["asset_no"] if "asset_no" in r.keys() else None)
                   or (r["manual_asset_no"] if "manual_asset_no" in r.keys() else ""),
        "model": (r["manual_model"] if "manual_model" in r.keys() else "")
                 or (r["model"] if "model" in r.keys() else None),
        "customer": r["customer"], "phone": r["phone"], "address": r["address"],
        "postalCode": r["postal_code"] if "postal_code" in r.keys() else "",
        "intake": r["intake"] if "intake" in r.keys() else "parcel",
        "intakeLabel": INTAKE.get(r["intake"] if "intake" in r.keys() else "parcel", ""),
        "symptomCat": r["symptom_cat"] if "symptom_cat" in r.keys() else "",
        "symptomSub": r["symptom_sub"] if "symptom_sub" in r.keys() else "",
        "channel": r["channel"], "symptom": r["symptom"],
        # 회수 품목(2026-09-08 대표) — 고객에게서 함께 받은 물건. 접수부터 발송까지 따라다닌다.
        "intakeItems": (r["intake_items"] if "intake_items" in k else "") or "",
        "asType": r["as_type"], "asTypeLabel": AS_TYPES.get(r["as_type"], r["as_type"]),
        "status": r["status"], "statusLabel": AS_STATUSES.get(r["status"], r["status"]),
        "cost": r["cost"], "chargeTo": r["charge_to"], "chargeLabel": CHARGE.get(r["charge_to"], r["charge_to"]),
        "exchangeProductCode": r["exchange_product_code"] if "exchange_product_code" in r.keys() else "",
        "extraCharge": (r["extra_charge"] if "extra_charge" in r.keys() else 0) or 0,
        "extraNote": r["extra_note"] if "extra_note" in r.keys() else "",
        # 결제 확인(2026-09-03 대표) — billTotal 이 "받을 돈"(수리비 + 추가비용),
        # paidAt 이 비어 있으면 아직 안 받은 것이다.
        "billTotal": (r["cost"] or 0) + ((r["extra_charge"] if "extra_charge" in k else 0) or 0),
        "paidAt": (r["paid_at"] if "paid_at" in k else "") or "",
        "paidAmount": (r["paid_amount"] if "paid_amount" in k else 0) or 0,
        "paidMethod": (r["paid_method"] if "paid_method" in k else "") or "",
        "result": r["result"], "receivedAt": r["received_at"], "closedAt": r["closed_at"],
        "assignee": r["assignee"], "createdBy": r["created_by"], "createdAt": r["created_at"],
    }


# ★재발 표시(2026-09-02 대표) — 같은 기계 / 같은 고객이 몇 번째로 들어왔는지.
#   불량 모델을 찾아내려면 '이 건이 처음인가'를 목록에서 바로 봐야 한다.
#   자산은 asset_id(연결분) 또는 수기 자산번호로, 고객은 전화번호 숫자만 비교한다
#   (하이픈 유무로 같은 사람이 갈리면 안 된다 — 주문 '같은 고객' 배지와 같은 기준).
#   취소 건은 세지 않는다. 쿼리 1번에 붙는 상관 서브쿼리다(N+1 금지).
_REPEAT_ASSET = (
    "(SELECT COUNT(*) FROM as_tickets r WHERE r.id <> t.id AND r.status <> 'cancelled' AND ("
    "  (t.asset_id IS NOT NULL AND r.asset_id = t.asset_id)"
    "  OR (COALESCE(t.asset_id,0)=0 AND TRIM(COALESCE(t.manual_asset_no,'')) <> ''"
    "      AND TRIM(COALESCE(r.manual_asset_no,'')) = TRIM(COALESCE(t.manual_asset_no,'')))"
    "))")
_REPEAT_CUST = (
    "(SELECT COUNT(*) FROM as_tickets r WHERE r.id <> t.id AND r.status <> 'cancelled' "
    " AND LENGTH(REPLACE(REPLACE(REPLACE(COALESCE(t.phone,''),'-',''),' ',''),'+','')) >= 9 "
    " AND REPLACE(REPLACE(REPLACE(COALESCE(r.phone,''),'-',''),' ',''),'+','') "
    "   = REPLACE(REPLACE(REPLACE(COALESCE(t.phone,''),'-',''),' ',''),'+',''))")

_SELECT = ("SELECT t.*, a.asset_no, a.model, "
           + _REPEAT_ASSET + " AS repeat_asset, "
           + _REPEAT_CUST + " AS repeat_cust "
           "FROM as_tickets t "
           "LEFT JOIN assets a ON a.id = t.asset_id WHERE 1=1")


@bp.get("/as-meta")
def as_meta():
    require_any("as.view", "as.manage")
    return jsonify({
        "types": [{"code": k, "label": v} for k, v in AS_TYPES.items()],
        "statuses": [{"code": k, "label": v} for k, v in AS_STATUSES.items()],
        "charges": [{"code": k, "label": v} for k, v in CHARGE.items()],
        "intakes": [{"code": k, "label": v} for k, v in INTAKE.items()],
        "returnMethods": [{"code": k, "label": v} for k, v in RETURN_METHODS.items()],
        # 증상 분류(2026-08-31 대표 저녁) — 대분류/소분류, A/S ▸ ⚙ 설정에서 관리
        "symptomCats": _symptom_cats(get_db())["cats"],
        "symptomSubs": _symptom_cats(get_db())["subs"],
        # 회수 구성품(2026-09-08 대표 "구성품은 우리가 직접 추가도 할 수 있게 ＋ 버튼")
        "intakeItemPresets": _intake_presets(get_db()),
    })


@bp.put("/as-intake-presets")
def put_intake_presets():
    """회수 구성품 목록 저장(2026-09-08 대표 — 접수 화면의 ＋ 버튼이 여기에 넣는다).

    자주 같이 들어오는 물건을 눌러서 담는 용도다. 목록에서 지워도 이미 접수된 건에
    적힌 품목은 그대로 남는다(증상 분류와 같은 규칙 — 과거 기록은 안 건드린다).
    """
    require("as.manage")
    body = request.get_json(silent=True) or {}
    items = body.get("items")
    if not isinstance(items, list):
        abort(400, description="구성품 목록이 필요합니다.")
    clean = []
    for x in items:
        s = str(x or "").strip()
        if not s or s in clean:
            continue
        if len(s) > 20:
            abort(400, description=f"구성품 이름이 너무 깁니다(20자 이내): {s[:20]}…")
        if "," in s:
            abort(400, description="구성품 이름에 쉼표는 쓸 수 없습니다(품목 구분자입니다).")
        clean.append(s)
    if len(clean) > 40:
        abort(400, description="구성품은 40개까지입니다.")
    with tx(write=True) as conn:
        conn.execute(
            "INSERT INTO settings(key, value, updated_at, updated_by) "
            "VALUES('as_intake_presets',?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (json.dumps(clean, ensure_ascii=False), config.now_iso(), g.user["display_name"]))
        audit.log("as_intake_presets", target="회수 구성품", detail={"n": len(clean)})
    return jsonify({"ok": True, "items": clean})


@bp.put("/as-symptom-cats")
def put_symptom_cats():
    """증상 분류 저장(2026-08-31 대표 저녁 — "대분류/소분류 직접 추가할 수 있도록").

    A/S ▸ ⚙ 설정 ▸ 🏷 증상 분류에서 고친다(설정 메뉴가 아니라 A/S 담당 자리).
    항목을 지워도 이미 접수된 건의 분류 기록은 남는다(과거 조사 자료를 지우지 않는다).
    """
    require("as.manage")
    body = request.get_json(silent=True) or {}
    out = {}
    for key, label in (("cats", "대분류"), ("subs", "소분류")):
        items = body.get(key)
        if not isinstance(items, list):
            abort(400, description=f"{label} 목록이 필요합니다.")
        clean = []
        for x in items:
            s = str(x or "").strip()
            if not s or s in clean:
                continue
            if len(s) > 30:
                abort(400, description=f"{label} 항목이 너무 깁니다(30자 이내): {s[:30]}…")
            clean.append(s)
        if len(clean) > 30:
            abort(400, description=f"{label}는 30개까지입니다.")
        out[key] = clean
    with tx(write=True) as conn:
        conn.execute(
            "INSERT INTO settings(key, value, updated_at, updated_by) "
            "VALUES('as_symptom_cats',?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (json.dumps(out, ensure_ascii=False), config.now_iso(), g.user["display_name"]))
        audit.log("as_symptom_cats", target="증상 분류",
                  detail={k: len(v) for k, v in out.items()})
    return jsonify({"ok": True, **out})


# 처리 기한 — 접수 후 이 날수를 넘겨 아직 안 끝난 건은 '늦어지는 건'으로 본다.
# 설정에서 바꿀 수 있게 KV 로 두되, 없으면 이 값을 쓴다(대표 2026-09-02).
AS_DUE_DAYS_DEFAULT = 7


def _as_due_days(conn):
    row = conn.execute("SELECT value FROM settings WHERE key='as_due_days'").fetchone()
    try:
        n = int(str(row["value"]).strip()) if row else AS_DUE_DAYS_DEFAULT
    except (TypeError, ValueError):
        n = AS_DUE_DAYS_DEFAULT
    return max(1, min(365, n))


@bp.get("/as-stats")
def as_stats():
    """A/S 현황 통계 — 기간별 접수·분류 분포·처리 소요일·유상무상·원가/수익.

    ★돈 규칙(대표 2026-09-02 확정)
      · 수입 = 유상(charge_to='customer') 건의 수리비 + 추가비용. 판매 매출과 합치지 않고
        '나란히 따로' 본다(매출/실적 화면도 같은 방식).
      · 원가 = 수리내역 줄에 동결된 부품 원가 합(as_ticket_items.cost). 자산 원가에는
        얹지 않는다 — 이미 팔린 자산의 지난 마진을 소급해 흔들지 않기 위해서다.
      · 무상(company) 건의 수리비는 '회사 부담'으로 따로 센다.
    기간은 접수일(received_at) 기준이다 — '이 기간에 몇 건 들어왔나'가 현장 감각과 맞다.
    """
    require_any("as.view", "as.manage")
    frm = (request.args.get("from") or "").strip()
    to = (request.args.get("to") or "").strip()
    if not (frm and to):
        abort(400, description="조회 기간(from, to)이 필요합니다.")
    conn = get_db()
    rng = (frm, to + "￿")           # received_at 은 ISO 문자열이라 끝을 열어 준다

    base = ("FROM as_tickets t WHERE t.received_at BETWEEN ? AND ? "
            "AND t.status != 'cancelled'")
    tot = conn.execute(
        "SELECT COUNT(*) AS cnt, "
        "  COALESCE(SUM(CASE WHEN t.charge_to='customer' THEN t.cost + "
        "    COALESCE(t.extra_charge,0) ELSE 0 END),0) AS income, "
        "  COALESCE(SUM(CASE WHEN t.charge_to='company' THEN t.cost ELSE 0 END),0) AS company_cost, "
        "  SUM(CASE WHEN t.charge_to='customer' THEN 1 ELSE 0 END) AS paid_cnt, "
        "  SUM(CASE WHEN t.status IN ('returned','done') THEN 1 ELSE 0 END) AS closed_cnt "
        + base, rng).fetchone()
    part_cost = conn.execute(
        "SELECT COALESCE(SUM(i.cost),0) AS c FROM as_ticket_items i "
        "JOIN as_tickets t ON t.id = i.ticket_id WHERE t.received_at BETWEEN ? AND ? "
        "AND t.status != 'cancelled'", rng).fetchone()["c"]

    def _group(col):
        rows = conn.execute(
            f"SELECT COALESCE(NULLIF(TRIM(t.{col}),''),'(미분류)') AS k, COUNT(*) AS n "
            + base + f" GROUP BY k ORDER BY n DESC, k", rng).fetchall()
        return [{"key": r["k"], "count": r["n"]} for r in rows]

    # 처리 소요일 — 끝난 건만(접수 → 종료). 평균과 함께 '가장 오래 걸린 건'도 준다.
    days = conn.execute(
        "SELECT t.ticket_no, t.customer, "
        "  CAST(julianday(substr(t.closed_at,1,10)) - julianday(substr(t.received_at,1,10)) "
        "       AS INTEGER) AS d " + base + " AND t.closed_at != '' ORDER BY d DESC", rng).fetchall()
    vals = [r["d"] for r in days if r["d"] is not None and r["d"] >= 0]
    avg = round(sum(vals) / len(vals), 1) if vals else 0
    mid = sorted(vals)[len(vals) // 2] if vals else 0

    # 늦어지는 건 — 아직 안 끝났는데 기한을 넘긴 것
    due = _as_due_days(conn)
    late = conn.execute(
        "SELECT t.ticket_no, t.customer, t.status, t.received_at, "
        "  CAST(julianday('now','localtime') - julianday(substr(t.received_at,1,10)) "
        "       AS INTEGER) AS d FROM as_tickets t WHERE t.closed_at='' "
        "AND t.status NOT IN ('returned','cancelled') "
        "AND julianday('now','localtime') - julianday(substr(t.received_at,1,10)) >= ? "
        "ORDER BY d DESC LIMIT 50", (due,)).fetchall()

    cnt = tot["cnt"] or 0
    return jsonify({
        "from": frm, "to": to,
        "tickets": cnt,
        "closed": tot["closed_cnt"] or 0,
        "paid": tot["paid_cnt"] or 0,
        "free": cnt - (tot["paid_cnt"] or 0),
        "paidRate": round((tot["paid_cnt"] or 0) * 100.0 / cnt, 1) if cnt else 0,
        # 돈 — 판매 매출과 합치지 않는다
        "income": tot["income"] or 0,
        "partCost": part_cost or 0,
        "profit": (tot["income"] or 0) - (part_cost or 0),
        "companyCost": tot["company_cost"] or 0,
        # 분포
        "byCat": _group("symptom_cat"),
        "bySub": _group("symptom_sub"),
        "byType": _group("as_type"),
        # 소요일
        "leadAvg": avg, "leadMid": mid, "leadCount": len(vals),
        "slowest": [{"ticketNo": r["ticket_no"], "customer": r["customer"], "days": r["d"]}
                    for r in days[:5] if r["d"] is not None],
        # 기한 초과(기간과 무관한 '지금' 현황)
        "dueDays": due,
        "late": [{"ticketNo": r["ticket_no"], "customer": r["customer"],
                  "status": r["status"], "days": r["d"]} for r in late],
    })


# ───────────── 📋 진행 보드(2026-09-03 대표 "A/S 담당자가 현황 파악이 되도록") ─────────────
# 대표가 물은 네 가지를 그대로 칸으로 만든다 —
#   ① 회수 신청한 택배가 배송완료됐는지 ② 접수해서 진행 중인지
#   ③ 수리가 끝났는데 결제 전인지     ④ 결제는 됐는데 발송 전인지.
# ★상태(status) 하나로는 답이 안 나온다. "회수 중"은 택배가 오는 중일 수도 있고,
#   이미 도착했는데 아무도 입고를 안 누른 것일 수도 있다(CJ 추적 데몬은 송장만
#   배송완료로 바꾸고 접수 건 상태는 그대로 둔다 — orders/recall.py _sync_one).
#   그래서 여기서는 접수 상태 + 회수 송장 단계 + 반송 송장 단계 + 결제 도장을
#   합쳐서 한 칸을 정한다. 화면은 이 칸 이름만 보고 그린다.
# ★칸 이름·흐름은 2026-09-07 대표 개편 그대로:
#   접수 → 회수 중 → 입고 완료 → 수리 중 → 결제 전 → 발송 전/방문 수령 → 택배 출고 → 종료
#   · '회수 예약 대기' → '접수'(회수 일정 설정·정상 접수 확인·접수 삭제가 여기서)
#   · '반송 중' → '택배 출고'
#   · 회수 택배가 배달완료면 자동으로 입고 완료(orders/recall.recall_arrived), 방문 접수는 처음부터
#   · 결제 전은 유상만 — 결제 확인 뒤에야 송장 출력·방문 수령이 열린다(payment_pending 잠금).
#     ★금액을 아직 안 적은 유상 건도 결제 전이다(2026-09-08 대표 "유상 바뀌면 무조건 결제 전으로")
#   · 택배 출고가 배달완료면 자동 종료 — 종료는 쌓이기만 하고 검색으로 본다
AS_BOARD_STAGES = [
    ("intake_wait", "📝 접수", "택배 접수 — 회수 일정을 잡아 주세요(방문 접수는 바로 입고 완료)."),
    ("collecting", "🚚 회수 중", "회수 예약 완료 — 택배가 오는 중. 배달완료가 찍히면 저절로 입고 완료로 갑니다."),
    ("arrived", "📥 입고 완료", "물건이 우리 손에 있습니다 — [수리 시작]을 눌러 주세요."),
    ("repairing", "🔧 수리 중", "작업이 진행 중입니다 — 수리 내역을 적고, 유상이면 결제 확인까지."),
    ("pay_wait", "💰 결제 전", "수리는 끝났고 유상인데 입금 확인 전 — 확인해야 송장·방문 수령이 열립니다."),
    ("ship_wait", "📦 발송 전", "돌려보낼 준비가 끝났습니다 — 송장을 출력하세요."),
    ("visit_wait", "🧍 방문 수령 대기", "고객이 직접 찾으러 오기로 한 건 — 넘겨주면 [인도 완료]."),
    ("returning", "🚚 택배 출고", "송장이 나갔습니다 — 고객에게 가는 중. 배달완료가 찍히면 저절로 종료됩니다."),
    ("closed", "✅ 종료", "끝난 건입니다 — 쌓여 있고, 검색으로 찾아 내역을 봅니다."),
]
AS_BOARD_ORDER = [s[0] for s in AS_BOARD_STAGES]

# 회수/반송 송장은 접수 건마다 최신 1건만 붙인다(취소분 제외). 상관 서브쿼리로 id를
# 고른 뒤 조인해서 N+1 을 피한다 — 목록 쿼리 한 번으로 끝난다.
# 수리 내역 요약(항목 이름 4개까지·줄 수)도 같은 쿼리에 싣는다 — [수리 중] 칸에서
# 팝업을 안 열고도 무엇을 고치고 있는지 보이게(2026-09-07 대표 "수리 관련 내역 상세").
_BOARD_SELECT = (
    "SELECT t.*, a.asset_no, a.model, "
    + _REPEAT_ASSET + " AS repeat_asset, " + _REPEAT_CUST + " AS repeat_cust, "
    "  CAST(julianday('now','localtime') - julianday(substr(t.received_at,1,10)) "
    "       AS INTEGER) AS age_days, "
    "  rw.wid AS rw_wid, rw.status AS rw_status, rw.invoice_no AS rw_invoice, "
    "  rw.cj_stage_nm AS rw_stage, rw.cj_stage_at AS rw_stage_at, "
    "  rw.cj_rcpt_ymd AS rw_rcpt, rw.scheduled_date AS rw_sched, "
    "  fw.wid AS fw_wid, fw.status AS fw_status, fw.invoice_no AS fw_invoice, "
    "  fw.cj_stage_nm AS fw_stage, fw.cj_stage_at AS fw_stage_at, "
    "  (SELECT GROUP_CONCAT(x.name, ' · ') FROM (SELECT i.name FROM as_ticket_items i "
    "     WHERE i.ticket_id = t.id ORDER BY i.sort, i.id LIMIT 4) x) AS items_summary, "
    "  (SELECT COUNT(*) FROM as_ticket_items i WHERE i.ticket_id = t.id) AS items_count "
    "FROM as_tickets t "
    "LEFT JOIN assets a ON a.id = t.asset_id "
    "LEFT JOIN waybills rw ON rw.wid = (SELECT w.wid FROM waybills w "
    "  WHERE w.as_ticket_id = t.id AND w.type='recall' AND w.status <> 'canceled' "
    "  ORDER BY w.created_at DESC, w.wid DESC LIMIT 1) "
    "LEFT JOIN waybills fw ON fw.wid = (SELECT w.wid FROM waybills w "
    "  WHERE w.as_ticket_id = t.id AND w.type='forward' AND w.status <> 'canceled' "
    "  ORDER BY w.created_at DESC, w.wid DESC LIMIT 1) "
    "WHERE 1=1")


def _board_stage(r):
    """이 접수 건이 지금 어느 칸에 있는지 — 위 AS_BOARD_STAGES 의 code 를 돌려준다."""
    k = r.keys()
    st = r["status"]
    if st in ("returned", "cancelled"):
        return "closed"
    if st == "done":
        # 송장이 나갔으면 결제 여부와 무관하게 이미 고객에게 가는 중이다
        if r["fw_wid"]:
            return "returning"
        # ★유상이면 금액을 아직 안 적었어도 결제 전이다(2026-09-08 대표
        #   "유상 바뀌면 무조건 결제 전으로") — 금액으로 판정하면 발송 전으로 새어 나간다
        if payment_pending(r):
            return "pay_wait"
        if (r["return_method"] if "return_method" in k else "parcel") == "visit":
            return "visit_wait"
        return "ship_wait"
    if st == "repairing":
        return "repairing"
    if st == "arrived":
        return "arrived"
    # 접수·회수 중 — 택배가 어디까지 왔는지가 답이다.
    # (개편 뒤에는 배달완료가 접수 건을 'arrived'로 바꾸지만, 그 전에 들어온 건과
    #  추적이 못 본 건을 위해 송장 기준 판정도 남긴다)
    if r["rw_wid"]:
        return "arrived" if r["rw_status"] == "delivered" else "collecting"
    if (r["intake"] if "intake" in k else "parcel") == "visit":
        return "arrived"          # 방문 접수 = 물건이 이미 우리 손에 있다
    return "collecting" if st == "collecting" else "intake_wait"


@bp.get("/as-board")
def as_board():
    """진행 보드 — 접수 상태·택배 단계·결제 도장을 합쳐 '지금 무엇을 해야 하나'로 묶는다.

    view=open(기본): 아직 안 끝난 건만. view=all: 종료(반송 완료)까지(취소 제외).
    view=closed: 끝난 건만(반송 완료 + 취소·삭제) — [✅ 종료] 칸. 쌓여만 있으니
    q(접수번호/고객/연락처/관리번호/증상)로 찾는다(2026-09-07 대표 "검색이나 내역 확인 가능하게").
    """
    require_any("as.view", "as.manage")
    conn = get_db()
    view = request.args.get("view", "open")
    params = []
    if view == "closed":
        sql = _BOARD_SELECT + " AND t.status IN ('returned','cancelled')"
    else:
        sql = _BOARD_SELECT + " AND t.status <> 'cancelled'"
        if view != "all":
            sql += " AND t.status <> 'returned'"
    q = (request.args.get("q") or "").strip()
    if q:
        sql += (" AND (t.ticket_no LIKE ? OR t.customer LIKE ? OR t.phone LIKE ? "
                "OR t.symptom LIKE ? OR a.asset_no LIKE ? OR t.manual_asset_no LIKE ?)")
        params.extend(["%" + q + "%"] * 6)
    if view == "closed":
        sql += " ORDER BY t.closed_at DESC, t.id DESC LIMIT 300"
    else:
        sql += " ORDER BY t.received_at DESC, t.id DESC LIMIT 500"
    rows = conn.execute(sql, params).fetchall()
    due = _as_due_days(conn)

    out = [_board_row(r, due) for r in rows]
    order = {c: i for i, c in enumerate(AS_BOARD_ORDER)}
    # 칸 순서(흐름 순) → 그 안에서는 오래 묵은 것부터. 담당자가 위에서부터 치우면 된다.
    # 종료 칸은 최근에 끝난 것이 위(쌓이는 순서의 역순).
    if view == "closed":
        out.sort(key=lambda x: x["closedAt"] or "", reverse=True)
    else:
        out.sort(key=lambda x: (order.get(x["stage"], 99), -x["ageDays"]))
    counts = {c: 0 for c in AS_BOARD_ORDER}
    for d in out:
        counts[d["stage"]] = counts.get(d["stage"], 0) + 1
    # 종료 칸 숫자는 보기와 무관하게 '끝난 건 전체'다 — 카드를 눌러야 그 목록이 열린다
    if view != "closed":
        counts["closed"] = conn.execute(
            "SELECT COUNT(*) AS n FROM as_tickets WHERE status IN ('returned','cancelled')"
        ).fetchone()["n"]
    return jsonify({
        "dueDays": due,
        "stages": [{"code": c, "label": lb, "hint": h, "count": counts.get(c, 0)}
                   for c, lb, h in AS_BOARD_STAGES],
        "rows": out,
        "late": sum(1 for d in out if d["late"]),
        "view": view, "q": q,
    })


def _board_row(r, due):
    """보드 한 줄 — 접수 건 + 지금 칸 + 회수/반송 송장 상태 + 수리 내역 요약."""
    k = r.keys()
    stage = _board_stage(r)
    age = r["age_days"] if r["age_days"] is not None else 0
    d = _ticket_payload(r)
    rcpt = (r["rw_rcpt"] if "rw_rcpt" in k else "") or ""
    d.update({
        "stage": stage,
        "ageDays": age,
        # 늦은 건 = 아직 안 끝났는데 기한(설정 as_due_days)을 넘긴 것
        "late": bool(stage not in ("closed",) and age >= due),
        "recallWid": r["rw_wid"] or "",
        "recallInvoiceNo": r["rw_invoice"] or "",
        "recallStage": r["rw_stage"] or "",
        "recallStageAt": r["rw_stage_at"] or "",
        "recallDone": r["rw_status"] == "delivered",
        # 회수 접수 확인(2026-09-07 대표 "회수 접수 시 정상 접수 확인") — CJ 가 접수를 받았는지
        # (issued=운영 접수 성공 / test=시험 접수), 접수일, 수거 예정일
        "recallBooked": (r["rw_status"] or "") in ("issued", "test", "delivered"),
        "recallTest": (r["rw_status"] or "") == "test",
        "recallRcptDate": f"{rcpt[:4]}-{rcpt[4:6]}-{rcpt[6:8]}" if len(rcpt) == 8 else rcpt,
        "recallScheduled": (r["rw_sched"] if "rw_sched" in k else "") or "",
        "returnWid": r["fw_wid"] or "",
        "returnInvoiceNo": r["fw_invoice"] or "",
        "returnStage": r["fw_stage"] or "",
        "returnStageAt": r["fw_stage_at"] or "",
        "returnDone": r["fw_status"] == "delivered",
        "itemsSummary": (r["items_summary"] if "items_summary" in k else "") or "",
        "itemsCount": (r["items_count"] if "items_count" in k else 0) or 0,
        "unpaid": unpaid_bill(r),
        "cancelled": r["status"] == "cancelled",
    })
    return d


# ───────────── 🏠 A/S 대시보드(2026-09-07 대표 "접수탭을 A/S 대시보드로") ─────────────
@bp.get("/as-dashboard")
def as_dashboard():
    """첫 화면 — 숫자 몇 개와 '지금 할 일', 최근 움직임, 늦어지는 건.

    진행 보드와 같은 산식(_board_stage)으로 세므로 두 화면의 숫자가 어긋나지 않는다
    (2026-09-03 대표 "같은 숫자를 두 곳에서 세면 반드시 어긋난다" — 그래서 한 함수로 센다).
    """
    require_any("as.view", "as.manage")
    conn = get_db()
    due = _as_due_days(conn)
    rows = conn.execute(_BOARD_SELECT + " AND t.status NOT IN ('returned','cancelled') "
                        "ORDER BY t.received_at DESC, t.id DESC LIMIT 500").fetchall()
    board = [_board_row(r, due) for r in rows]
    counts = {c: 0 for c in AS_BOARD_ORDER}
    unpaid_total = 0
    for d in board:
        counts[d["stage"]] = counts.get(d["stage"], 0) + 1
        if d["stage"] == "pay_wait":
            unpaid_total += d["unpaid"]
    today = config.today_str()
    if len(today) == 8:                                  # YYYYMMDD → YYYY-MM-DD
        today = f"{today[:4]}-{today[4:6]}-{today[6:8]}"
    week_ago = (config.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    today_received = conn.execute(
        "SELECT COUNT(*) AS n FROM as_tickets WHERE substr(received_at,1,10)=? "
        "AND status <> 'cancelled'", (today,)).fetchone()["n"]
    week_closed = conn.execute(
        "SELECT COUNT(*) AS n FROM as_tickets WHERE status='returned' AND substr(closed_at,1,10) >= ?",
        (week_ago,)).fetchone()["n"]
    late_rows = sorted([d for d in board if d["late"]], key=lambda x: -x["ageDays"])[:6]
    recent = [
        {"ts": e["ts"], "action": e["action"], "actor": e["actor"],
         "ticketId": e["ticket_id"], "ticketNo": e["ticket_no"], "customer": e["customer"],
         "detail": json.loads(e["detail"]) if e["detail"] else None}
        for e in conn.execute(
            "SELECT e.ts, e.action, e.actor, e.ticket_id, e.detail, t.ticket_no, t.customer "
            "FROM as_events e JOIN as_tickets t ON t.id = e.ticket_id "
            "ORDER BY e.id DESC LIMIT 12").fetchall()]
    return jsonify({
        "dueDays": due,
        "open": len(board),
        "late": len([d for d in board if d["late"]]),
        "todayReceived": today_received,
        "weekClosed": week_closed,
        "unpaidTotal": unpaid_total,
        "unpaidCount": counts.get("pay_wait", 0),
        "stages": [{"code": c, "label": lb, "hint": h, "count": counts.get(c, 0)}
                   for c, lb, h in AS_BOARD_STAGES if c != "closed"],
        "lateRows": [{"id": d["id"], "ticketNo": d["ticketNo"], "customer": d["customer"],
                      "stage": d["stage"], "ageDays": d["ageDays"]} for d in late_rows],
        "recent": recent,
    })


@bp.delete("/as-tickets/<int:tid>")
def delete_ticket(tid):
    """접수 삭제(2026-09-07 대표 "접수 고객 삭제 기능") — 잘못 넣었거나 고객이 무른 건.

    ★진짜로 지우지 않는다. 접수 문자가 이미 그 접수번호로 고객에게 나갔을 수 있고, 번호를
      되쓰면 다른 고객이 같은 번호를 받는다(문서번호·자산번호와 같은 '번호 재사용 금지').
      상태를 '취소'로 바꾸고 사유를 이력에 남긴다 — 진행 보드에서 사라지고, 종료 칸
      검색과 대시보드 찾기에서만 '취소'로 보인다. 되살리려면 상세에서 상태를 '접수'로.
    ★접수 단계에서만 — 회수 예약이 살아 있으면 먼저 취소해야 하고(기사가 방문한다),
      돈을 받았거나 문서를 발행한 건은 삭제가 아니라 정상 종료·취소로 다뤄야 한다.
    """
    require("as.manage")
    body = request.get_json(silent=True) or {}
    reason = (str(body.get("reason") or "")).strip()[:80]
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM as_tickets WHERE id=?", (tid,)).fetchone()
        if row is None:
            abort(404, description="A/S 건을 찾을 수 없습니다.")
        if row["status"] == "cancelled":
            abort(400, description="이미 취소(삭제)된 건입니다.")
        # 회수 예약이 걸린 건은 '어느 단계냐'보다 '기사가 온다'가 먼저다 — 취소부터 안내한다
        live = conn.execute(
            "SELECT wid, type FROM waybills WHERE as_ticket_id=? AND status NOT IN ('canceled','failed') "
            "LIMIT 1", (tid,)).fetchone()
        if live:
            abort(409, description=f"송장 {live['wid']}이(가) 살아 있습니다 — "
                                   f"{'회수 예약을 먼저 취소' if live['type'] == 'recall' else '송장을 먼저 취소'}하세요.")
        if row["status"] not in ("received", "arrived"):
            abort(400, description=f"'{AS_STATUSES.get(row['status'], row['status'])}' 단계는 삭제할 수 없습니다 "
                                   "— 접수 단계에서만 지웁니다. 진행 중인 건은 상태를 '취소'로 바꾸세요.")
        if (row["paid_at"] if "paid_at" in row.keys() else ""):
            abort(409, description="결제 확인이 기록된 건은 삭제할 수 없습니다 — 결제 확인을 해제한 뒤 취소하세요.")
        if conn.execute("SELECT 1 FROM as_documents WHERE ticket_id=? AND deleted_at='' LIMIT 1",
                        (tid,)).fetchone():
            abort(409, description="발행한 수리내역서·청구내역서가 있는 건은 삭제할 수 없습니다 — 상태를 '취소'로 바꾸세요.")
        ts = config.now_iso()
        # 수리 내역에 부품이 걸려 있었다면 재고 소진을 되돌린다(취소 건이 부품을 먹으면 안 된다)
        reverted = _revert_ticket_parts(conn, tid)
        conn.execute("UPDATE as_tickets SET status='cancelled', closed_at=?, updated_at=? WHERE id=?",
                     (ts, ts, tid))
        _as_event(conn, tid, "접수삭제", {"사유": reason or "-",
                                        **({"부품복구": reverted} if reverted else {})})
        if row["asset_id"]:
            asset_event(conn, row["asset_id"], "A/S접수삭제", {"ticketNo": row["ticket_no"], "사유": reason})
        audit.log("as_deleted", target=f"{row['ticket_no']} {row['customer']}",
                  detail={"reason": reason, "status": row["status"]})
    return jsonify({"ok": True, "ticketNo": row["ticket_no"], "status": "cancelled"})


@bp.post("/as-tickets/<int:tid>/recall-check")
def recall_check(tid):
    """[🔄 CJ 확인](2026-09-07 대표 "회수 접수 시 정상 접수 확인") — 이 건의 회수 예약이
    CJ 에 정상 접수됐는지, 집화됐는지(송장번호), 어디까지 왔는지를 지금 물어본다.

    A/S 담당자 권한으로 되는 유일한 CJ 조회다(배송/송장 화면의 수집 버튼은 배송 권한).
    접수일 하루만 물으므로 호출은 1번. 읽기 전용 조회(SND_YN=N)라 예약이 생기지 않는다.
    회수 91 이면 데몬과 같은 관문으로 '입고 완료'까지 간다.
    """
    require("as.manage")
    from ..orders.recall import sweep_recall_invoices
    from ..orders.waybill import _cj_settings, _is_real
    conn = get_db()
    t = conn.execute("SELECT * FROM as_tickets WHERE id=?", (tid,)).fetchone()
    if t is None:
        abort(404, description="A/S 건을 찾을 수 없습니다.")
    w = conn.execute(
        "SELECT * FROM waybills WHERE as_ticket_id=? AND type='recall' AND status <> 'canceled' "
        "ORDER BY created_at DESC, wid DESC LIMIT 1", (tid,)).fetchone()
    if w is None:
        abort(400, description="이 건에는 회수 예약이 없습니다.")
    cfg = _cj_settings(conn)
    checked = None
    if w["status"] == "test":
        checked = {"simulated": True}
    elif w["status"] != "delivered":
        if not _is_real(cfg):
            abort(400, description="CJ 운영 설정(운영+실발행 무장)이 있어야 확인할 수 있습니다.")
        rcpt = (w["cj_rcpt_ymd"] or "").strip() or config.now().strftime("%Y%m%d")
        checked = sweep_recall_invoices(cfg, [rcpt])
    w2 = conn.execute("SELECT * FROM waybills WHERE wid=?", (w["wid"],)).fetchone()
    t2 = conn.execute("SELECT status FROM as_tickets WHERE id=?", (tid,)).fetchone()
    return jsonify({
        "wid": w2["wid"], "booked": w2["status"] in ("issued", "test", "delivered"),
        "simulated": w2["status"] == "test",
        "invoiceNo": w2["invoice_no"] or "", "stage": w2["cj_stage_nm"] or "",
        "stageAt": w2["cj_stage_at"] or "", "delivered": w2["status"] == "delivered",
        "ticketStatus": t2["status"], "checked": checked,
    })


@bp.post("/as-tickets/<int:tid>/payment")
def set_payment(tid):
    """입금 확인 도장(2026-09-03 대표 "진행완료되어 결제전인지").

    {"paid": true, "amount": 123000, "method": "계좌이체", "date": "2026-09-03"}
    금액을 안 주면 그 시점의 청구 합계(수리비+추가비용)를 굳힌다. paid:false 면 도장 해제.
    ★영수/세금계산서 발행과는 별개다 — 여기서는 "받았는가"만 기록한다.
    """
    require("as.manage")
    body = request.get_json(silent=True) or {}
    want = bool(body.get("paid", True))
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM as_tickets WHERE id=?", (tid,)).fetchone()
        if row is None:
            abort(404, description="A/S 건을 찾을 수 없습니다.")
        bill = (row["cost"] or 0) + (row["extra_charge"] or 0)
        if not want:
            conn.execute("UPDATE as_tickets SET paid_at='', paid_amount=0, paid_method='', "
                         "updated_at=? WHERE id=?", (config.now_iso(), tid))
            _as_event(conn, tid, "결제확인해제", {"이전금액": row["paid_amount"] or 0})
            return jsonify({"ok": True, "paidAt": "", "paidAmount": 0, "paidMethod": ""})
        try:
            amount = int(body.get("amount") if body.get("amount") not in (None, "") else bill)
        except (TypeError, ValueError):
            abort(400, description="받은 금액이 숫자가 아닙니다.")
        if amount < 0:
            abort(400, description="받은 금액은 0원 이상이어야 합니다.")
        when = (body.get("date") or "").strip() or config.now_iso()[:10]
        method = (body.get("method") or "").strip()[:20]
        conn.execute("UPDATE as_tickets SET paid_at=?, paid_amount=?, paid_method=?, "
                     "updated_at=? WHERE id=?",
                     (when, amount, method, config.now_iso(), tid))
        _as_event(conn, tid, "결제확인",
                  {"금액": amount, "청구합계": bill, "수단": method or "-", "날짜": when})
    return jsonify({"ok": True, "paidAt": when, "paidAmount": amount, "paidMethod": method})

@bp.get("/as-tickets")
def list_tickets():
    require_any("as.view", "as.manage")
    sql = _SELECT
    params = []
    view = request.args.get("view", "open")
    if view == "open":
        ph = ",".join("?" * len(OPEN_STATUSES))
        sql += f" AND t.status IN ({ph})"
        params.extend(OPEN_STATUSES)
    elif view == "closed":
        sql += " AND t.status IN ('returned','cancelled')"
    if request.args.get("status"):
        sql += " AND t.status = ?"
        params.append(request.args["status"])
    # 증상 분류 필터(2026-08-31 저녁) — 대분류/소분류 정확 일치(조사용)
    if request.args.get("cat"):
        sql += " AND t.symptom_cat = ?"
        params.append(request.args["cat"])
    if request.args.get("sub"):
        sql += " AND t.symptom_sub = ?"
        params.append(request.args["sub"])
    q = (request.args.get("q") or "").strip()
    if q:
        sql += (" AND (t.ticket_no LIKE ? OR t.customer LIKE ? OR t.phone LIKE ? "
                "OR t.symptom LIKE ? OR a.asset_no LIKE ?)")
        params.extend(["%" + q + "%"] * 5)
    sql += " ORDER BY t.id DESC LIMIT 300"
    rows = get_db().execute(sql, params).fetchall()
    return jsonify([_ticket_payload(r) for r in rows])


@bp.get("/as-tickets/<int:tid>")
def ticket_detail(tid):
    require_any("as.view", "as.manage")
    conn = get_db()
    row = conn.execute(_SELECT + " AND t.id = ?", (tid,)).fetchone()
    if row is None:
        abort(404, description="A/S 건을 찾을 수 없습니다.")
    events = conn.execute(
        "SELECT * FROM as_events WHERE ticket_id=? ORDER BY id DESC LIMIT 100", (tid,)).fetchall()
    out = _ticket_payload(row)
    # 진행 중인 회수 예약이 있으면 화면이 [회수 예약] 버튼 대신 그 사실을 보여 준다
    # ★취소되지 않은 회수 건이 있으면(입고 완료 포함) 회수 버튼을 다시 보여주면 안 된다.
    #   이미 받아 와 수리 중인데 버튼이 살아나면, 눌러서 기사가 고객 집으로 또 간다.
    wb = conn.execute(
        "SELECT wid, status, invoice_no, cj_stage_nm, cj_stage_at FROM waybills "
        "WHERE as_ticket_id=? AND type='recall' "
        "AND status != 'canceled' ORDER BY created_at DESC LIMIT 1", (tid,)).fetchone()
    out["recallWid"] = wb["wid"] if wb else ""
    out["recallDone"] = bool(wb and wb["status"] == "delivered")
    # 택배 실시간 이력(2026-08-31 대표) — 3시간 데몬·수동 새로고침이 채우는 CJ 단계를 그대로 노출
    out["recallInvoiceNo"] = (wb["invoice_no"] if wb else "") or ""
    out["recallStage"] = (wb["cj_stage_nm"] if wb else "") or ""
    out["recallStageAt"] = (wb["cj_stage_at"] if wb else "") or ""
    rb = conn.execute(
        "SELECT wid, invoice_no, cj_stage_nm, cj_stage_at FROM waybills "
        "WHERE as_ticket_id=? AND type='forward' "
        "AND status IN ('issued','test','pending') ORDER BY created_at DESC LIMIT 1", (tid,)).fetchone()
    out["returnWid"] = rb["wid"] if rb else ""
    out["returnInvoiceNo"] = rb["invoice_no"] if rb else ""
    out["returnStage"] = (rb["cj_stage_nm"] if rb else "") or ""
    out["returnStageAt"] = (rb["cj_stage_at"] if rb else "") or ""
    out["events"] = [
        {"ts": e["ts"], "action": e["action"], "actor": e["actor"],
         "detail": json.loads(e["detail"]) if e["detail"] else None}
        for e in events
    ]
    return jsonify(out)


def _notify(tid, code):
    """A/S 단계 안내 문자 — 실패해도 본 작업은 그대로 둔다.

    ★notify_trigger 라 기본 양식만이 아니라 그 시점을 고른 '직접 만든 양식'도 함께 나간다
      (2026-08-31 대표 — 발송 시점 지정).
    """
    from flask import current_app

    from ..notify import notify_trigger
    ok, msg = notify_trigger(current_app, tid, code)
    return {"ok": ok, "message": msg}


def _as_event(conn, tid, action, detail=None):
    conn.execute(
        "INSERT INTO as_events(ticket_id, ts, action, actor, detail) VALUES(?,?,?,?,?)",
        (tid, config.now_iso(), action, g.user["display_name"],
         json.dumps(detail, ensure_ascii=False) if detail is not None else None))


@bp.post("/as-tickets")
def create_ticket():
    require("as.manage")
    body = request.get_json(silent=True) or {}
    customer = (body.get("customer") or "").strip()
    symptom = (body.get("symptom") or "").strip()
    if not customer or not symptom:
        abort(400, description="고객명과 증상은 필수입니다.")
    as_type = (body.get("asType") or "repair").strip()
    if as_type not in AS_TYPES:
        abort(400, description="알 수 없는 A/S 유형입니다.")
    charge_to = (body.get("chargeTo") or "company").strip()
    if charge_to not in CHARGE:
        abort(400, description="알 수 없는 비용 부담 구분입니다.")
    intake_items = clean_intake_items(body.get("intakeItems"))
    intake = (body.get("intake") or "parcel").strip()
    if intake not in INTAKE:
        abort(400, description="알 수 없는 접수 경로입니다(택배/방문).")
    asset_id = body.get("assetId")
    order_id = body.get("orderId")
    ts = config.now_iso()
    with tx(write=True) as conn:
        if asset_id not in (None, ""):
            asset_id = _int_or_400(asset_id, "자산 ID")
            if conn.execute("SELECT id FROM assets WHERE id=?", (asset_id,)).fetchone() is None:
                abort(400, description="존재하지 않는 자산입니다.")
        else:
            asset_id = None
        if order_id not in (None, ""):
            order_id = _int_or_400(order_id, "주문 ID")
        else:
            order_id = None
        ticket_no = _next_ticket_no(conn)
        sym_cat = _clean_sym(body.get("symptomCat"), "대분류")
        sym_sub = _clean_sym(body.get("symptomSub"), "소분류")
        # 수기 자산번호/모델명 — 자산이 연결됐으면 자산 것이 진실이라 수기값은 비운다
        manual_no = "" if asset_id else (str(body.get("assetNo") or "")).strip()[:40]
        manual_model = "" if asset_id else (str(body.get("model") or "")).strip()[:80]
        # ★방문 접수는 고객이 물건을 들고 왔다 — 회수할 것이 없으니 바로 '입고 완료'에 선다
        #   (2026-09-07 대표 "방문으로 접수한 경우 바로 입고완료로"). 택배는 '접수'에서
        #   회수 일정을 잡는다.
        status = "arrived" if intake == "visit" else "received"
        cur = conn.execute(
            "INSERT INTO as_tickets(ticket_no, order_id, asset_id, customer, phone, address, "
            "postal_code, intake, symptom_cat, symptom_sub, manual_asset_no, manual_model, channel, "
            "symptom, intake_items, as_type, status, charge_to, received_at, assignee, created_by, "
            "created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket_no, order_id, asset_id, customer, (body.get("phone") or "").strip(),
             (body.get("address") or "").strip(), (body.get("postalCode") or "").strip(),
             intake, sym_cat, sym_sub, manual_no, manual_model, (body.get("channel") or "").strip(),
             symptom, intake_items, as_type, status, charge_to,
             (body.get("receivedAt") or "").strip() or config.now().strftime("%Y-%m-%d"),
             (body.get("assignee") or "").strip() or g.user["display_name"],
             g.user["display_name"], ts, ts))
        tid = cur.lastrowid
        _as_event(conn, tid, "접수",
                  {"유형": AS_TYPES[as_type], "경로": INTAKE[intake],
                   **({"분류": " / ".join(x for x in (sym_cat, sym_sub) if x)}
                      if (sym_cat or sym_sub) else {}),
                   **({"상태": AS_STATUSES[status]} if status != "received" else {}),
                   **({"회수품목": intake_items} if intake_items else {}),
                   "증상": symptom})
        if asset_id:
            asset_event(conn, asset_id, "A/S접수", {"ticketNo": ticket_no, "증상": symptom})
        audit.log("as_created", target=f"{ticket_no} {customer}", detail={"type": as_type})
    # 문자는 트랜잭션 밖에서 — 외부 호출이 DB 잠금을 쥐면 안 된다(원칙 #1)
    sms = _notify(tid, "received")
    return jsonify({"id": tid, "ticketNo": ticket_no, "sms": sms}), 201


@bp.patch("/as-tickets/<int:tid>")
def update_ticket(tid):
    require("as.manage")
    body = request.get_json(silent=True) or {}
    new_status = ""
    notify_code = ""
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM as_tickets WHERE id=?", (tid,)).fetchone()
        if row is None:
            abort(404, description="A/S 건을 찾을 수 없습니다.")
        changes = {}
        for key, col, label in (
            ("customer", "customer", "고객명"), ("phone", "phone", "연락처"),
            ("address", "address", "주소"), ("postalCode", "postal_code", "우편번호"),
            ("channel", "channel", "채널"),
            ("symptom", "symptom", "증상"), ("result", "result", "처리내용"),
            ("assignee", "assignee", "담당자"), ("receivedAt", "received_at", "접수일"),
        ):
            if key in body:
                v = (body.get(key) or "").strip()
                if v != row[col]:
                    conn.execute(f"UPDATE as_tickets SET {col}=? WHERE id=?", (v, tid))
                    changes[label] = v
        if "asType" in body:
            v = (body.get("asType") or "").strip()
            if v not in AS_TYPES:
                abort(400, description="알 수 없는 A/S 유형입니다.")
            if v != row["as_type"]:            # 같은 값 저장이 유령 '수정' 이력을 만들지 않게
                conn.execute("UPDATE as_tickets SET as_type=? WHERE id=?", (v, tid))
                changes["유형"] = AS_TYPES[v]
        if "chargeTo" in body:
            v = (body.get("chargeTo") or "").strip()
            if v not in CHARGE:
                abort(400, description="알 수 없는 비용 부담 구분입니다.")
            if v != row["charge_to"]:
                conn.execute("UPDATE as_tickets SET charge_to=? WHERE id=?", (v, tid))
                changes["비용부담"] = CHARGE[v]
        if "intake" in body:
            v = (body.get("intake") or "").strip()
            if v not in INTAKE:
                abort(400, description="알 수 없는 접수 경로입니다(택배/방문).")
            if v != row["intake"]:
                conn.execute("UPDATE as_tickets SET intake=? WHERE id=?", (v, tid))
                changes["접수경로"] = INTAKE[v]
                # 접수 단계에서 경로를 바꾸면 자리도 따라간다 — 방문이면 물건이 이미 있으니
                # '입고 완료', 다시 택배로 돌리면 회수 일정을 잡아야 하니 '접수'.
                # (회수 예약이 걸린 뒤라면 상태는 송장이 정한다 — 건드리지 않는다)
                if v == "visit" and row["status"] == "received" and "status" not in body:
                    conn.execute("UPDATE as_tickets SET status='arrived' WHERE id=?", (tid,))
                    changes["상태"] = AS_STATUSES["arrived"]
                    _as_event(conn, tid, "상태변경", {"from": AS_STATUSES["received"],
                                                   "to": AS_STATUSES["arrived"], "사유": "방문 접수"})
                elif v == "parcel" and row["status"] == "arrived" and "status" not in body \
                        and conn.execute(
                            "SELECT 1 FROM waybills WHERE as_ticket_id=? AND type='recall' "
                            "AND status <> 'canceled'", (tid,)).fetchone() is None:
                    conn.execute("UPDATE as_tickets SET status='received' WHERE id=?", (tid,))
                    changes["상태"] = AS_STATUSES["received"]
                    _as_event(conn, tid, "상태변경", {"from": AS_STATUSES["arrived"],
                                                   "to": AS_STATUSES["received"], "사유": "택배 접수로 전환"})
        if "returnMethod" in body:
            v = (body.get("returnMethod") or "").strip()
            if v not in RETURN_METHODS:
                abort(400, description="알 수 없는 수령 방법입니다(택배 발송/방문 수령).")
            cur = row["return_method"] if "return_method" in row.keys() else "parcel"
            if v != cur:
                conn.execute("UPDATE as_tickets SET return_method=? WHERE id=?", (v, tid))
                changes["수령방법"] = RETURN_METHODS[v]
        # 증상 분류(2026-08-31 저녁) — 목록에 없는 값도 저장한다(옛 분류를 지워도 기록 유지)
        if "symptomCat" in body:
            v = _clean_sym(body.get("symptomCat"), "대분류")
            if v != (row["symptom_cat"] if "symptom_cat" in row.keys() else ""):
                conn.execute("UPDATE as_tickets SET symptom_cat=? WHERE id=?", (v, tid))
                changes["대분류"] = v or "(비움)"
        if "symptomSub" in body:
            v = _clean_sym(body.get("symptomSub"), "소분류")
            if v != (row["symptom_sub"] if "symptom_sub" in row.keys() else ""):
                conn.execute("UPDATE as_tickets SET symptom_sub=? WHERE id=?", (v, tid))
                changes["소분류"] = v or "(비움)"
        # 자산번호/모델명(2026-08-31 대표 — 접수 후 적을 칸이 없었다).
        # 번호가 우리 자산과 정확히 일치하면 그 자산에 연결(원가 반영·자산 이력이 살아난다),
        # 아니면 수기값으로만 남긴다. assets 테이블은 어떤 경우에도 고치지 않는다.
        asset_id_now = row["asset_id"]
        if "assetNo" in body:
            v = (str(body.get("assetNo") or "")).strip()[:40]
            linked_no = ""
            if asset_id_now:
                a = conn.execute("SELECT asset_no FROM assets WHERE id=?", (asset_id_now,)).fetchone()
                linked_no = (a["asset_no"] if a else "") or ""
            effective = linked_no or (row["manual_asset_no"] if "manual_asset_no" in row.keys() else "")
            if v != effective:
                # 중복 번호 허용 정책(2026-08-31)이라 여러 개면 최신 자산을 택한다
                hit = conn.execute(
                    "SELECT id FROM assets WHERE asset_no=? ORDER BY id DESC LIMIT 1",
                    (v,)).fetchone() if v else None
                if hit:
                    conn.execute("UPDATE as_tickets SET asset_id=?, manual_asset_no='' WHERE id=?",
                                 (hit["id"], tid))
                    asset_id_now = hit["id"]
                    asset_event(conn, hit["id"], "A/S연결", {"ticketNo": row["ticket_no"]})
                    changes["자산번호"] = f"{v} (자산 연결)"
                else:
                    conn.execute("UPDATE as_tickets SET asset_id=NULL, manual_asset_no=? WHERE id=?",
                                 (v, tid))
                    asset_id_now = None
                    changes["자산번호"] = (f"{v} (수기 — 일치하는 자산 없음)") if v else "(비움)"
        if "model" in body:
            v = (str(body.get("model") or "")).strip()[:80]
            linked_model = ""
            if asset_id_now:
                a = conn.execute("SELECT model FROM assets WHERE id=?", (asset_id_now,)).fetchone()
                linked_model = (a["model"] if a else "") or ""
            cur_manual = row["manual_model"] if "manual_model" in row.keys() else ""
            if v != (cur_manual or linked_model):
                # 자산에 적힌 모델명과 같아지면 수기값을 지워 자산 것을 따라간다
                conn.execute("UPDATE as_tickets SET manual_model=? WHERE id=?",
                             ("" if v == linked_model else v, tid))
                changes["모델명"] = v or "(비움)"
        # 교환 제품코드·추가비용(2026-08-31 대표 — 교환 차액 등, 수리비와 별개 청구)
        if "exchangeProductCode" in body:
            v = (str(body.get("exchangeProductCode") or "")).strip()[:60]
            if v != (row["exchange_product_code"] if "exchange_product_code" in row.keys() else ""):
                conn.execute("UPDATE as_tickets SET exchange_product_code=? WHERE id=?", (v, tid))
                changes["교환제품코드"] = v or "(비움)"
        # 회수 품목(2026-09-08 대표) — 접수 때 못 적었거나 나중에 하나 더 받은 경우가 있어
        # 어느 단계에서든 고칠 수 있다. 바뀌면 이력에 전값이 남는다(분실 시비의 근거).
        if "intakeItems" in body:
            v = clean_intake_items(body.get("intakeItems"))
            was = (row["intake_items"] if "intake_items" in row.keys() else "") or ""
            if v != was:
                conn.execute("UPDATE as_tickets SET intake_items=? WHERE id=?", (v, tid))
                changes["회수품목"] = f"{was or '(없음)'} → {v or '(비움)'}"
        if "extraCharge" in body:
            v = _int_or_400(body.get("extraCharge") or 0, "추가비용")
            if v < 0:
                abort(400, description="추가비용은 0 이상이어야 합니다.")
            if v != ((row["extra_charge"] if "extra_charge" in row.keys() else 0) or 0):
                conn.execute("UPDATE as_tickets SET extra_charge=? WHERE id=?", (v, tid))
                changes["추가비용"] = v
        if "extraNote" in body:
            v = (str(body.get("extraNote") or "")).strip()[:80]
            if v != (row["extra_note"] if "extra_note" in row.keys() else ""):
                conn.execute("UPDATE as_tickets SET extra_note=? WHERE id=?", (v, tid))
                changes["추가비용사유"] = v or "(비움)"
        if "cost" in body:
            v = _int_or_400(body["cost"], "수리비")
            conn.execute("UPDATE as_tickets SET cost=? WHERE id=?", (v, tid))
            changes["수리비"] = v
        if "status" in body:
            v = (body.get("status") or "").strip()
            if v not in AS_STATUSES:
                abort(400, description="알 수 없는 상태입니다.")
            if v != row["status"]:
                # ★유상 건은 돈을 받아야 끝난다(2026-09-07 대표) — 방문 인도·종료 모두.
                #   화면 버튼도 막지만 상태 셀렉트로 우회해도 여기서 걸린다.
                if v == "returned":
                    why = payment_block_reason(row, "종료(반송 완료)할")
                    if why:
                        abort(400, description=why)
                new_status = v          # 트랜잭션 밖에서 안내 문자를 보낼지 판단할 값
                closed = config.now_iso() if v in ("returned", "cancelled") else ""
                conn.execute("UPDATE as_tickets SET status=?, closed_at=? WHERE id=?", (v, closed, tid))
                _as_event(conn, tid, "상태변경",
                          {"from": AS_STATUSES.get(row["status"]), "to": AS_STATUSES[v]})
                changes["상태"] = AS_STATUSES[v]
                if asset_id_now:
                    asset_event(conn, asset_id_now, "A/S" + AS_STATUSES[v],
                                {"ticketNo": row["ticket_no"]})
                    # 고객에게 돌려보냈으면 우리 재고가 아니다 — 재고 수량에서 빼야 한다.
                    # (안 그러면 남의 물건이 계속 보유 재고로 잡혀 재고·자산가치가 부풀려진다)
                    # ★'회수중'도 포함한다 — 물건을 받아 고친 뒤 입고 처리를 건너뛰고 바로
                    #   반송하는 경우가 실무에서 흔한데, 빼면 자산이 회수중에 갇혀 재고로 남는다.
                    if v == "returned":
                        conn.execute(
                            "UPDATE assets SET status='shipped', updated_at=? WHERE id=? "
                            "AND status NOT IN ('shipped','scrapped')",
                            (config.now_iso(), asset_id_now))
        if not changes:
            abort(400, description="변경할 항목이 없습니다.")
        conn.execute("UPDATE as_tickets SET updated_at=? WHERE id=?", (config.now_iso(), tid))
        if set(changes) - {"상태"}:
            _as_event(conn, tid, "수정", {k: v for k, v in changes.items() if k != "상태"})
        audit.log("as_updated", target=row["ticket_no"], detail=changes)
        notify_code = new_status if new_status in ("done",) else ""
    # 문자는 트랜잭션 밖에서 보낸다
    sms = _notify(tid, notify_code) if notify_code else None
    return jsonify({"ok": True, "sms": sms})


@bp.post("/as-tickets/<int:tid>/to-asset-repair")
def push_cost_to_asset(tid):
    """A/S 수리비를 해당 자산의 수리 내역으로 옮겨 원가에 반영한다(회사 부담일 때)."""
    require("as.manage")
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM as_tickets WHERE id=?", (tid,)).fetchone()
        if row is None:
            abort(404, description="A/S 건을 찾을 수 없습니다.")
        if not row["asset_id"]:
            abort(400, description="자산이 연결되지 않은 A/S 건입니다.")
        if row["cost"] <= 0:
            abort(400, description="수리비가 0원입니다.")
        if row["charge_to"] != "company":
            abort(400, description="고객 청구(유상) 건은 자산 원가에 반영하지 않습니다.")
        # ★수리비를 고쳐 적었으면 다시 반영할 수 있어야 한다.
        #   전에는 '이미 반영했습니다'로 막혀, 틀린 금액이 자산 원가에 굳었다.
        dup = conn.execute(
            "SELECT id, cost FROM asset_repairs WHERE asset_id=? AND description LIKE ?",
            (row["asset_id"], f"%{row['ticket_no']}%")).fetchone()
        desc = f"[{row['ticket_no']}] {row['result'] or row['symptom']}"
        if dup:
            if dup["cost"] == row["cost"]:
                abort(409, description="이미 같은 금액으로 반영돼 있습니다.")
            conn.execute(
                "UPDATE asset_repairs SET cost=?, description=?, repair_date=? WHERE id=?",
                (row["cost"], desc, config.now().strftime("%Y-%m-%d"), dup["id"]))
            from ..purchase import queue_asset_cost
            queue_asset_cost(conn, row["asset_id"], reason=f"A/S {row['ticket_no']} 수리비 정정")
            asset_event(conn, row["asset_id"], "수리비정정",
                        {"ticketNo": row["ticket_no"], "from": dup["cost"], "to": row["cost"]})
            _as_event(conn, tid, "자산원가정정", {"from": dup["cost"], "to": row["cost"]})
            audit.log("as_cost_to_asset", target=row["ticket_no"],
                      detail={"from": dup["cost"], "to": row["cost"]})
            return jsonify({"ok": True, "updated": True})
        conn.execute(
            "INSERT INTO asset_repairs(asset_id, repair_date, description, cost, created_by, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (row["asset_id"], config.now().strftime("%Y-%m-%d"), desc, row["cost"],
             g.user["display_name"], config.now_iso()))
        from ..purchase import queue_asset_cost
        queue_asset_cost(conn, row["asset_id"], reason=f"A/S {row['ticket_no']} 수리비")
        asset_event(conn, row["asset_id"], "수리",
                    {"ticketNo": row["ticket_no"], "cost": row["cost"], "출처": "A/S"})
        _as_event(conn, tid, "자산원가반영", {"cost": row["cost"]})
        audit.log("as_cost_to_asset", target=row["ticket_no"], detail={"cost": row["cost"]})
    return jsonify({"ok": True})


@bp.get("/as-tickets/asset-search")
def as_asset_search():
    """A/S 접수 시 자산 찾기 — 출고된 자산도 검색된다(고객이 쓰던 물건이므로)."""
    require("as.manage")
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    # ★최근 것부터 준다. 관리번호 오름차순이던 시절엔 같은 모델이 1,000대를 넘으면
    #   '가장 오래된 20대'만 나와 최근에 판매된 물건 — A/S가 실제로 들어오는 그 물건 —
    #   에는 구조적으로 도달할 수 없었다(2026-08-07 검증). 관리번호가 날짜 기반이라
    #   내림차순이 곧 최근 매입/판매 순이다.
    # ★자동완성(2026-08-31 대표): 관리번호/시리얼/모델만이 아니라 그 물건을 산 고객의
    #   이름·연락처로도 걸린다 — "홍길동 노트북 왔어요"에서 바로 기계를 찾는다.
    like = "%" + q + "%"
    digits = "".join(c for c in q if c.isdigit())
    phone_like = "%" + digits + "%" if len(digits) >= 4 else None
    rows = get_db().execute(
        "SELECT a.id, a.asset_no, a.maker, a.model, a.status, "
        " (SELECT o.id FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
        "  WHERE oa.asset_id=a.id ORDER BY oa.matched_at DESC LIMIT 1) AS order_id, "
        " (SELECT o.recipient FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
        "  WHERE oa.asset_id=a.id ORDER BY oa.matched_at DESC LIMIT 1) AS recipient, "
        " (SELECT o.phone FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
        "  WHERE oa.asset_id=a.id ORDER BY oa.matched_at DESC LIMIT 1) AS phone, "
        " (SELECT o.address FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
        "  WHERE oa.asset_id=a.id ORDER BY oa.matched_at DESC LIMIT 1) AS address, "
        " (SELECT o.postal_code FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
        "  WHERE oa.asset_id=a.id ORDER BY oa.matched_at DESC LIMIT 1) AS postal_code "
        "FROM assets a WHERE a.asset_no LIKE ? OR a.serial LIKE ? OR a.model LIKE ? "
        "OR EXISTS (SELECT 1 FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
        "  WHERE oa.asset_id=a.id AND (o.recipient LIKE ? OR (? IS NOT NULL "
        "  AND replace(replace(o.phone,'-',''),' ','') LIKE ?))) "
        "ORDER BY a.asset_no DESC LIMIT 20",
        (like, like, like, like, phone_like, phone_like or "")).fetchall()
    return jsonify([
        # ★한글 이름을 서버가 함께 준다 — 화면이 매입 메뉴 데이터를 미리 받아 뒀는지에
        #   기대면, 매입 권한이 없는 A/S 담당에게는 'shipped' 같은 영어가 그대로 보인다.
        {"assetId": r["id"], "assetNo": r["asset_no"], "maker": r["maker"], "model": r["model"],
         "status": r["status"], "statusLabel": ASSET_STATUSES.get(r["status"], r["status"]),
         "orderId": r["order_id"], "recipient": r["recipient"],
         "phone": r["phone"] or "", "address": r["address"] or "",
         "postalCode": r["postal_code"] or ""}
        for r in rows
    ])


@bp.get("/as-tickets/customer-search")
def as_customer_search():
    """고객명·연락처 자동완성(2026-08-31 대표) — 주문 고객과 과거 A/S 고객에서 찾는다.

    고르면 화면이 고객명/연락처/주소/우편번호를 한 번에 채운다. 같은 사람이 여러 번
    주문했으면 최신 것 하나만 준다(이름+전화 기준).
    """
    require("as.manage")
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    like = "%" + q + "%"
    digits = "".join(c for c in q if c.isdigit())
    phone_like = "%" + digits + "%" if len(digits) >= 4 else None
    conn = get_db()
    rows = conn.execute(
        "SELECT recipient AS name, phone, address, postal_code, created_at, '주문' AS src "
        "FROM orders WHERE cancelled_at='' AND recipient != '' AND (recipient LIKE ? "
        "OR (? IS NOT NULL AND replace(replace(phone,'-',''),' ','') LIKE ?)) "
        "ORDER BY id DESC LIMIT 30", (like, phone_like, phone_like or "")).fetchall()
    as_rows = conn.execute(
        "SELECT customer AS name, phone, address, postal_code, created_at, 'A/S' AS src "
        "FROM as_tickets WHERE customer != '' AND (customer LIKE ? "
        "OR (? IS NOT NULL AND replace(replace(phone,'-',''),' ','') LIKE ?)) "
        "ORDER BY id DESC LIMIT 30", (like, phone_like, phone_like or "")).fetchall()
    out, seen = [], set()
    for r in list(rows) + list(as_rows):
        key = (r["name"], "".join(c for c in (r["phone"] or "") if c.isdigit()))
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": r["name"], "phone": r["phone"] or "",
                    "address": r["address"] or "", "postalCode": r["postal_code"] or "",
                    "source": r["src"], "lastAt": (r["created_at"] or "")[:10]})
    out.sort(key=lambda x: x["lastAt"], reverse=True)
    return jsonify(out[:15])


# ─────────────────────────── 수리내역 라인 + 문서(2026-08-31 대표) ───────────────────────────
#   "수리내역서·청구내역서 — 상호/담당자/연락처/도장 필수, 부가세·부가세별도 금액,
#    합계에 입력하면 나머지 금액이 자동입력". 항목은 as_ticket_items, 발행본은 as_documents
#   스냅샷(불변). 합계는 as_tickets.cost 로 동기화 — to-asset-repair(원가 반영)가 그대로 돈다.

DOC_TYPES = {"repair": ("수리내역서", "ASR"), "invoice": ("청구내역서", "ASB")}


def _kv_json(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    try:
        return json.loads(row["value"]) if row and row["value"] else None
    except (ValueError, TypeError):
        return None


def _items_payload(conn, tid):
    rows = conn.execute(
        "SELECT * FROM as_ticket_items WHERE ticket_id=? ORDER BY sort, id", (tid,)).fetchall()
    return [{"id": r["id"], "partId": r["part_id"], "name": r["name"], "qty": r["qty"],
             "amount": r["amount"], "vat": r["vat"], "net": r["net"],
             # 원가(부품일 때만) — 화면이 '청구 − 원가 = 남는 돈'을 보여준다
             "cost": (r["cost"] if "cost" in r.keys() else 0)} for r in rows]


@bp.get("/as-tickets/<int:tid>/items")
def list_ticket_items(tid):
    require_any("as.view", "as.manage")
    conn = get_db()
    if conn.execute("SELECT id FROM as_tickets WHERE id=?", (tid,)).fetchone() is None:
        abort(404, description="A/S 건을 찾을 수 없습니다.")
    return jsonify(_items_payload(conn, tid))


def _revert_ticket_parts(conn, tid):
    """이 A/S 건이 뺀 부품 소진을 전부 되돌린다(행 제거). 되돌린 줄 수를 돌려준다.

    A/S 수리내역은 '전체 교체' 저장이라, 저장할 때마다 이전 소진을 물리고 새로 뺀다.
    판매 경로(_apply_part_to_asset 의 revert)와 같은 방식이다.
    """
    n = conn.execute("SELECT COUNT(*) AS c FROM part_stock_moves WHERE ticket_id=?",
                     (tid,)).fetchone()["c"]
    if n:
        conn.execute("DELETE FROM part_stock_moves WHERE ticket_id=?", (tid,))
    return n


def _use_part_for_ticket(conn, tid, part_id, qty):
    """A/S 로 부품 qty 개를 재고에서 뺀다 → 그 원가(동결)를 돌려준다.

    ★대표 확정(2026-09-02): A/S 로 나간 부품 원가는 **A/S 비용으로만** 잡는다.
      자산 원가(asset_repairs)에는 얹지 않는다 — 이미 팔린 자산의 지난 마진을
      소급해서 흔들면 지난달 실적 숫자가 바뀌기 때문이다.
      그래서 여기서는 part_stock_moves(재고 원장)만 건드리고 asset_repairs 는 안 만든다.
    ★재고가 모자라도 막지 않는다 — 실물은 이미 나갔는데 기록을 막으면 장부가 실물과
      더 어긋난다(판매 경로와 같은 원칙). 마이너스 재고는 부품 화면이 드러낸다.
    """
    part = conn.execute("SELECT * FROM parts WHERE id=?", (part_id,)).fetchone()
    if part is None:
        abort(400, description="수리 단가표에 없는 부품입니다.")
    qty = max(1, int(qty or 1))
    unit = int(part["price"] or 0)
    conn.execute(
        "INSERT INTO part_stock_moves(part_id, qty, unit_cost, amount, reason, move_date, "
        "ticket_id, memo, created_by, created_at) VALUES(?,?,?,?,'use',?,?,?,?,?)",
        (part_id, -qty, unit, 0, config.today_str(), tid, "A/S 소진",
         g.user["display_name"] if g.get("user") else "", config.now_iso()))
    return unit * qty


@bp.post("/as-tickets/<int:tid>/items")
def save_ticket_items(tid):
    """항목 전체 교체 저장. 각 줄 금액은 부가세 포함 총액 — split_vat 로 공급가/부가세를 갈라
    함께 저장하고, 합계를 as_tickets.cost 에 동기화한다(자산 원가 반영 경로 유지)."""
    require("as.manage")
    body = request.get_json(silent=True) or {}
    items = body.get("items")
    if not isinstance(items, list) or len(items) > 50:
        abort(400, description="항목 목록이 올바르지 않습니다(최대 50줄).")
    cleaned = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            abort(400, description="항목 형식이 올바르지 않습니다.")
        name = (str(it.get("name") or "")).strip()
        if not name:
            abort(400, description=f"{i + 1}번째 항목의 이름이 비어 있습니다.")
        qty = _int_or_400(it.get("qty", 1), "수량")
        if qty < 1:
            abort(400, description="수량은 1 이상이어야 합니다.")
        amount = _int_or_400(it.get("amount", 0), "금액")
        if amount < 0:
            abort(400, description="금액은 0 이상이어야 합니다.")
        part_id = it.get("partId")
        part_id = _int_or_400(part_id, "부품") if part_id not in (None, "") else None
        cleaned.append((part_id, name[:80], qty, amount))
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM as_tickets WHERE id=?", (tid,)).fetchone()
        if row is None:
            abort(404, description="A/S 건을 찾을 수 없습니다.")
        conn.execute("DELETE FROM as_ticket_items WHERE ticket_id=?", (tid,))
        # ★부품 재고 되돌리기 — 이 저장은 '전체 교체'라 이전 소진을 먼저 물린다.
        #   (판매 경로가 repair_id 로 되돌리는 것과 같은 방식. 되돌린 뒤 아래에서 다시 뺀다)
        used = _revert_ticket_parts(conn, tid)
        total, cost_total = 0, 0
        for sort, (part_id, name, qty, amount) in enumerate(cleaned):
            net, vat = split_vat(amount)
            total += amount
            # 부품 줄이면 단가표의 '지금 단가'로 원가를 동결하고 재고에서 뺀다.
            # 부품이 아닌 줄(공임·출장비 등)은 원가 0 — 청구만 있고 나가는 물건이 없다.
            line_cost = _use_part_for_ticket(conn, tid, part_id, qty) if part_id else 0
            cost_total += line_cost
            conn.execute(
                "INSERT INTO as_ticket_items(ticket_id, part_id, name, qty, amount, vat, net, sort, cost) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (tid, part_id, name, qty, amount, vat, net, sort, line_cost))
        if total != row["cost"]:
            conn.execute("UPDATE as_tickets SET cost=?, updated_at=? WHERE id=?",
                         (total, config.now_iso(), tid))
        detail = {"항목": len(cleaned), "합계": total}
        if cost_total or used:
            detail["부품원가"] = cost_total
        _as_event(conn, tid, "내역수정", detail)
        audit.log("as_items_saved", target=row["ticket_no"],
                  detail={"items": len(cleaned), "total": total, "partCost": cost_total})
        return jsonify({"ok": True, "cost": total, "partCost": cost_total,
                        "items": _items_payload(conn, tid)})


@bp.get("/as-doc-settings")
def get_as_doc_settings():
    """문서용 회사정보(상호/대표자/담당자/연락처/사업자번호/주소) + 도장.
    비어 있으면 CJ 설정(사업자번호·출고지)에서 초깃값을 제안한다 — 다시 칠 필요 없게."""
    require_any("as.view", "as.manage", "settings.manage")
    conn = get_db()
    company = _kv_json(conn, "as_company_info") or {}
    if not company:
        cj = _kv_json(conn, "cj") or {}
        sender = cj.get("sender") or {}
        company = {"name": "예시 운영사", "ceo": "", "manager": "",
                   "phone": sender.get("tel") or "", "bizReg": cj.get("biz_reg_num") or "",
                   "address": sender.get("addr") or "", "bank": "", "suggested": True}
    srow = conn.execute("SELECT value FROM settings WHERE key='as_doc_stamp'").fetchone()
    return jsonify({"company": company, "stamp": (srow["value"] if srow else "") or ""})


@bp.post("/as-doc-settings")
def set_as_doc_settings():
    require("settings.manage")
    company = (request.get_json(silent=True) or {}).get("company")
    if not isinstance(company, dict):
        abort(400, description="회사 정보 값이 올바르지 않습니다.")
    # bank = 수리비 입금 계좌(2026-08-31 대표) — 청구내역서와 {계좌번호} 문자 자리에 찍힌다
    keep = {k: str(company.get(k) or "").strip()[:120]
            for k in ("name", "ceo", "manager", "phone", "bizReg", "address", "bank")}
    if not keep["name"]:
        abort(400, description="상호명은 필수입니다.")
    raw = json.dumps(keep, ensure_ascii=False)
    with tx(write=True) as conn:
        conn.execute(
            "INSERT INTO settings(key, value, updated_at, updated_by) VALUES('as_company_info',?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (raw, config.now_iso(), g.user["display_name"]))
        audit.log("as_company_saved", target="A/S 문서 회사정보", detail=keep)
    return jsonify({"ok": True})


@bp.post("/as-doc-stamp")
def set_as_doc_stamp():
    """회사 도장 이미지 — data URL 저장, 빈 값 = 제거(자산 라벨 로고와 같은 방식)."""
    require("settings.manage")
    data = str((request.get_json(silent=True) or {}).get("dataUrl") or "")
    if data and not data.startswith("data:image/"):
        abort(400, description="이미지 파일이 아닙니다.")
    if len(data) > 400_000:
        abort(400, description="도장 이미지가 너무 큽니다(300KB 이내로 줄여 주세요).")
    with tx(write=True) as conn:
        conn.execute(
            "INSERT INTO settings(key, value, updated_at, updated_by) VALUES('as_doc_stamp',?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (data, config.now_iso(), g.user["display_name"]))
        audit.log("as_stamp_saved", target="A/S 문서 도장",
                  detail={"bytes": len(data), "removed": not data})
    return jsonify({"ok": True})


def _next_doc_no(conn, prefix3):
    """ASR-/ASB-YYMMDD-NN. 두 자리를 넘겨도 계속 이어진다(_next_ticket_no 와 같은 규칙).

    ★지운 발행본까지 세므로 번호가 재사용되지 않는다(as_documents 는 소프트 삭제).
    """
    prefix = f"{prefix3}-{config.now().strftime('%y%m%d')}-"
    row = conn.execute(
        "SELECT MAX(CAST(substr(doc_no, 12) AS INTEGER)) AS m FROM as_documents "
        "WHERE doc_no GLOB ? AND substr(doc_no, 12) NOT GLOB '*[^0-9]*'",
        (prefix + "[0-9][0-9]*",)).fetchone()
    return f"{prefix}{(row['m'] or 0) + 1:02d}"


@bp.post("/as-tickets/<int:tid>/documents")
def issue_document(tid):
    """수리내역서/청구내역서 발행 — 스냅샷 저장 후 그 내용을 돌려준다(화면이 인쇄).
    상호·담당자·연락처·도장은 대표 지시로 필수 — 비면 발행을 막고 설정으로 안내한다."""
    require("as.manage")
    doc_type = ((request.get_json(silent=True) or {}).get("docType") or "").strip()
    if doc_type not in DOC_TYPES:
        abort(400, description="문서 종류는 repair(수리내역서)/invoice(청구내역서)만 됩니다.")
    label, prefix3 = DOC_TYPES[doc_type]
    with tx(write=True) as conn:
        row = conn.execute(_SELECT + " AND t.id = ?", (tid,)).fetchone()
        if row is None:
            abort(404, description="A/S 건을 찾을 수 없습니다.")
        items = _items_payload(conn, tid)
        if not items:
            abort(400, description="수리 항목을 먼저 저장하세요 — 내역이 없는 문서는 만들 수 없습니다.")
        company = _kv_json(conn, "as_company_info") or {}
        if not (company.get("name") and company.get("manager") and company.get("phone")):
            abort(400, description="설정 ▸ 운영 설정 ▸ A/S 문서에서 상호·담당자·연락처를 먼저 입력하세요.")
        srow = conn.execute("SELECT value FROM settings WHERE key='as_doc_stamp'").fetchone()
        if not (srow and srow["value"]):
            abort(400, description="설정 ▸ 운영 설정 ▸ A/S 문서에서 회사 도장 이미지를 먼저 올려 주세요.")
        doc_no = _next_doc_no(conn, prefix3)
        # 추가비용(교환 차액 등, 2026-08-31 대표) — 수리 항목과 별개 줄로 청구 합계에 얹힌다
        extra = int((row["extra_charge"] if "extra_charge" in row.keys() else 0) or 0)
        extra_net, extra_vat = split_vat(extra)
        snapshot = {
            "docNo": doc_no, "docType": doc_type, "docLabel": label,
            "issuedAt": config.now_iso(), "issuedBy": g.user["display_name"],
            "company": {k: company.get(k) or "" for k in
                        ("name", "ceo", "manager", "phone", "bizReg", "address", "bank")},
            "customer": {"name": row["customer"], "phone": row["phone"], "address": row["address"]},
            "ticket": {"ticketNo": row["ticket_no"],
                       "assetNo": row["asset_no"] or row["manual_asset_no"] or "",
                       "model": row["manual_model"] or row["model"] or "", "symptom": row["symptom"],
                       "symptomCat": (row["symptom_cat"] if "symptom_cat" in row.keys() else "") or "",
                       "symptomSub": (row["symptom_sub"] if "symptom_sub" in row.keys() else "") or "",
                       "result": row["result"], "asType": AS_TYPES.get(row["as_type"], row["as_type"]),
                       "receivedAt": row["received_at"], "chargeTo": row["charge_to"],
                       "exchangeProductCode": (row["exchange_product_code"]
                                               if "exchange_product_code" in row.keys() else "") or ""},
            "items": items,
            "extra": {"amount": extra, "vat": extra_vat, "net": extra_net,
                      "note": (row["extra_note"] if "extra_note" in row.keys() else "") or ""},
            "totals": {"amount": sum(i["amount"] for i in items) + extra,
                       "vat": sum(i["vat"] for i in items) + extra_vat,
                       "net": sum(i["net"] for i in items) + extra_net},
        }
        conn.execute(
            "INSERT INTO as_documents(ticket_id, doc_type, doc_no, snapshot, issued_by, issued_at) "
            "VALUES(?,?,?,?,?,?)",
            (tid, doc_type, doc_no, json.dumps(snapshot, ensure_ascii=False),
             g.user["display_name"], config.now_iso()))
        _as_event(conn, tid, "문서발행", {"종류": label, "docNo": doc_no})
        audit.log("as_doc_issued", target=f"{row['ticket_no']} {label}", detail={"docNo": doc_no})
        stamp = srow["value"]
    # 청구내역서 발행 시점 문자(2026-08-31 대표 — 입금 안내 등을 이 시점에 걸 수 있다).
    # 트랜잭션 밖 + 실패해도 발행은 그대로. 같은 양식은 한 번만 나간다(재발행 중복 방지).
    sms = _notify(tid, "invoiced") if doc_type == "invoice" else None
    return jsonify({"ok": True, "doc": snapshot, "stamp": stamp,
                    "sms": sms if (sms and sms.get("message")) else None})


@bp.get("/as-tickets/<int:tid>/documents")
def list_documents(tid):
    """발행 이력 — 스냅샷 그대로 다시 인쇄할 수 있다(발행 후 수정과 무관한 원본)."""
    require_any("as.view", "as.manage")
    conn = get_db()
    if conn.execute("SELECT id FROM as_tickets WHERE id=?", (tid,)).fetchone() is None:
        abort(404, description="A/S 건을 찾을 수 없습니다.")
    rows = conn.execute(
        "SELECT * FROM as_documents WHERE ticket_id=? AND deleted_at='' "
        "ORDER BY id DESC LIMIT 50", (tid,)).fetchall()
    srow = conn.execute("SELECT value FROM settings WHERE key='as_doc_stamp'").fetchone()
    return jsonify({"stamp": (srow["value"] if srow else "") or "",
                    "docs": [{"id": r["id"], "docType": r["doc_type"], "docNo": r["doc_no"],
                              "issuedBy": r["issued_by"], "issuedAt": r["issued_at"],
                              "snapshot": json.loads(r["snapshot"])} for r in rows]})


@bp.delete("/as-tickets/<int:tid>/documents/<int:did>")
def delete_document(tid, did):
    """발행본 삭제(2026-08-31 대표). 소프트 삭제 — 행은 남겨 문서번호 채번이 계속 세므로
    지운 번호가 다른 문서에 다시 붙지 않는다(채번 번호 재사용 금지 원칙)."""
    require("as.manage")
    with tx(write=True) as conn:
        row = conn.execute(
            "SELECT d.*, t.ticket_no FROM as_documents d JOIN as_tickets t ON t.id=d.ticket_id "
            "WHERE d.id=? AND d.ticket_id=?", (did, tid)).fetchone()
        if row is None:
            abort(404, description="발행본을 찾을 수 없습니다.")
        if row["deleted_at"]:
            abort(400, description="이미 삭제된 발행본입니다.")
        conn.execute("UPDATE as_documents SET deleted_at=?, deleted_by=? WHERE id=?",
                     (config.now_iso(), g.user["display_name"], did))
        label = DOC_TYPES.get(row["doc_type"], (row["doc_type"], ""))[0]
        _as_event(conn, tid, "문서삭제", {"종류": label, "docNo": row["doc_no"]})
        audit.log("as_doc_deleted", target=f"{row['ticket_no']} {label}",
                  detail={"docNo": row["doc_no"]})
    return jsonify({"ok": True})


@bp.get("/as-tickets/<int:tid>/history")
def ticket_history(tid):
    """이 고객·이 기계의 과거 — 전화번호(숫자만)로 주문/A/S, 자산으로 출고·수리 이력.
    접수 화면에서 '전에 뭘 샀고 뭘 고쳤는지'가 바로 보여야 한다(대표 2026-08-31)."""
    require_any("as.view", "as.manage")
    from ..orders import _DIGITS_ONLY
    conn = get_db()
    row = conn.execute(_SELECT + " AND t.id = ?", (tid,)).fetchone()
    if row is None:
        abort(404, description="A/S 건을 찾을 수 없습니다.")
    digits = "".join(ch for ch in (row["phone"] or "") if ch.isdigit())
    orders, tickets = [], []
    if len(digits) >= 9:
        orders = [
            {"id": o["id"], "orderNumber": o["order_no"], "channel": o["channel"],
             "productName": o["product_name"], "orderedAt": (o["ordered_at"] or "")[:10],
             "shippedAt": (o["shipping_at"] or "")[:10], "cancelled": bool(o["cancelled_at"]),
             "assetNos": o["asset_nos"] or ""}
            for o in conn.execute(
                "SELECT o.id, o.order_no, o.channel, o.product_name, o.ordered_at, o.shipping_at, "
                " o.cancelled_at, (SELECT GROUP_CONCAT(a.asset_no, ', ') FROM order_assets oa "
                "  JOIN assets a ON a.id=oa.asset_id WHERE oa.order_id=o.id) AS asset_nos "
                f"FROM orders o WHERE {_DIGITS_ONLY('o.phone')} = ? ORDER BY o.id DESC LIMIT 20",
                (digits,)).fetchall()]
        tickets = [
            {"id": t["id"], "ticketNo": t["ticket_no"],
             "status": AS_STATUSES.get(t["status"], t["status"]),
             "asType": AS_TYPES.get(t["as_type"], t["as_type"]), "symptom": t["symptom"],
             "receivedAt": t["received_at"]}
            for t in conn.execute(
                f"SELECT * FROM as_tickets t WHERE {_DIGITS_ONLY('t.phone')} = ? AND t.id != ? "
                "ORDER BY t.id DESC LIMIT 20", (digits, tid)).fetchall()]
    events, repairs = [], []
    if row["asset_id"]:
        events = [
            {"ts": e["ts"], "action": e["action"],
             "detail": json.loads(e["detail"]) if e["detail"] else None}
            for e in conn.execute(
                "SELECT * FROM asset_events WHERE asset_id=? ORDER BY id DESC LIMIT 30",
                (row["asset_id"],)).fetchall()]
        repairs = [
            {"date": r["repair_date"], "description": r["description"], "cost": r["cost"]}
            for r in conn.execute(
                "SELECT * FROM asset_repairs WHERE asset_id=? ORDER BY id DESC LIMIT 20",
                (row["asset_id"],)).fetchall()]
    return jsonify({"orders": orders, "tickets": tickets,
                    "assetEvents": events, "repairs": repairs})
