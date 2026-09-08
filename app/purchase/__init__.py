"""매입/자산 관리 — 거래처, 입고배치, 자산(자산번호 기준 이력관리), 수리비.

핵심 요구(2026-07-28 대표):
- 자산번호 자동 발번 = YYYYMMDD + 당일 순번 4자리 (예: 202607280001)
- 단, 기존 TMS 자산번호 이관을 위해 수동 지정/수정 가능 (UNIQUE 유지, 변경 이력 기록)
- 자산번호 기준 이력관리(asset_events 타임라인)가 최우선
"""
import json
import re

from flask import Blueprint, abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import can, require, require_any
from ..db import DIVISION_RENTAL, DIVISION_SALE, DIVISIONS, get_db, sale_only, tx
from ..settings import _int_or_400, _money_or_400


def queue_asset_cost(conn, asset_id, *, reason=""):
    """자산 원가(사람이 넣은 수리비)가 바뀌었다 — TMS 재고에 되돌려 넣을 큐에 올린다.

    ★얇은 껍데기인 이유: tms_push 가 이 모듈을 다시 import 하므로 위쪽에서 바로 import 하면
      순환이 된다. 그리고 큐 적재가 실패해도 자산 수리 기록 저장 자체는 절대 막지 않는다 —
      TMS 반영이 안 된 것은 화면에서 보이지만, 수리비를 못 넣는 것은 업무가 멈추는 일이다.
    """
    try:
        from .tms_push import queue_asset_cost as _q
        return _q(conn, asset_id, reason=reason)
    except Exception:                                        # noqa: BLE001
        return None

bp = Blueprint("purchase", __name__, url_prefix="/api")

# 자산 상태. reserved/shipped/returning은 주문 흐름이 관리 — 수동 변경 불가.
# TMS는 '수리·A/S·불량·도색대기'를 등급 목록에 섞어 두었지만, 재고 집계 정확도를 위해
# OWS는 품질등급(GRADES)과 작업상태(여기)를 분리한다(2026-07-28 대표 결정).
ASSET_STATUSES = {
    "in_stock": "입고",
    "refurbishing": "정비중",
    "repair": "수리",
    "as": "A/S",
    "painting": "도색대기",
    "defective": "불량",
    "ready": "판매가능",
    "reserved": "주문매칭",
    "shipped": "출고완료",
    "returning": "회수중",
    "scrapped": "폐기",
    # 전표가 취소되면 그 전표로 들어온 자산도 함께 무효가 된다(재고에서 빠진다).
    "cancelled": "매입취소",
    # 거래처로 되돌려 보낸 물건 — 우리 재고가 아니다.
    "returned": "거래처반품",
}
# 매칭 가능한(재고로 잡히는) 상태
AVAILABLE_STATUSES = ("in_stock", "refurbishing", "ready")

# ★판매불가 사유가 되는 상태(대표 2026-08-07: "판매 불가능이 된다면 수리, 폐기, A/S 등").
#   폐기·출고완료·취소·반품은 '죽은' 자산이라 여기 안 들어간다 — 손볼 대상이 아니다.
UNSELLABLE_STATUSES = ("repair", "as", "painting", "defective")

# ★판매 가능 자산 = 상태가 맞고 + '실물을 받은' 것.
#   가입고(V) 전표로 등록한 자산은 아직 물건이 안 들어온 상태라 재고로 세면 안 된다.
#   이걸 빠뜨리면 안 받은 노트북이 셋팅 화면에서 배정된다(2026-07-30 E2E에서 확인).
#   schema.sql에 'received … 가입고 자산은 0으로 시작'이라고 의도가 적혀 있었으나
#   실제 코드가 한 번도 0을 넣지 않아 죽은 필드였다.
RECEIVED_SQL = " AND a.received = 1"
MANUAL_STATUSES = {"in_stock", "refurbishing", "repair", "as", "painting",
                   "defective", "ready", "scrapped"}

# ─────────────────────────────────────────────────────────── 사람이 고르는 상태
# ★대표 2026-08-24: 상태 하나로 정한다. 재고구분(가용/실재고)과 보수체크는 상태와
#   같은 말을 두 번 하고 있었다(실측: 가재고 0대·보수체크 0대·판매불가 0대).
#
#   판매가능   → 재고로 잡히고 출고된다
#   A/S        → 고객 물건이라 재고가 아니다
#   수리       → 손볼 게 있다. ★재고에 안 잡히고 출고도 막힌다(AVAILABLE_STATUSES 밖)
#                하위 항목(시트지·도색·짜깁기…)을 자산마다 적는다 — assets.tier_tasks 재사용
#   불량·부품용 → 팔 물건이 아니다
#
# ★DB 값은 그대로 둔다. in_stock(입고)·refurbishing(정비중)·painting(도색대기)은
#   TMS 이관이 만드는 값이라 없앨 수 없다 — 화면에서 같은 그룹으로 접어 보여 준다.
STATUS_CHOICES = [
    {"code": "ready",     "label": "판매가능",
     "help": "바로 팔 수 있는 재고입니다. 셋팅·간단 보수가 끝난 것도 여기입니다."},
    {"code": "as",        "label": "A/S",
     "help": "고객에게서 들어온 A/S 물건 — 우리 재고가 아닙니다."},
    {"code": "repair",    "label": "수리",
     "help": "손볼 게 있는 물건. 재고에 안 잡히고 출고도 막힙니다. 무엇을 손볼지 아래에 적으세요."},
    {"code": "defective", "label": "불량 · 부품용",
     "help": "팔 수 없는 물건 — 부품을 빼 쓰거나 폐기 대상입니다."},
]
# 옛 상태 → 화면에서 묶어 보여 줄 상태. 저장하면 대표값으로 굳는다.
STATUS_GROUP = {
    "in_stock": "ready", "refurbishing": "ready", "ready": "ready",
    "painting": "repair", "repair": "repair",
    "as": "as", "defective": "defective",
}
# 수리 하위 항목 기본 후보(대표 2026-08-24). 자유 입력도 된다.
REPAIR_ITEMS = ["시트지", "도색", "짜깁기", "액정", "배터리", "키보드", "힌지", "간단보수"]

# 품질등급 — TMS 목록에서 작업상태를 뺀 것
GRADES = ["미정", "NU", "S+A", "S+S", "SS", "SA", "AS", "AA", "SB", "AB", "A급", "B급", "C급"]

# 재고 3단계(2026-08-04 대표 지시, 2026-08-08 이름·정의 확정: 양품 → 가용)
#   가용   = 셋팅완료·촬영용 등 — 바로 판매 가능
#   실재고 = 셋팅·시트지 또는 1~2일 내 간단한 보수 후 바로 판매 가능(재고로 친다)
#            ★무엇을 보수해야 하는지·하는 중인지를 자산마다 지정한다(tier_tasks)
#   가재고 = 짜집기·불용 — 판매 가능 재고로 잡지 않는다. ★출고 금지.
#            매칭(자산번호 등록)까지는 되지만, 매입에서 가용/실재고로 바꾸기 전에는
#            출고 확인·송장 발급이 막힌다.
TIERS = ("가용", "실재고", "가재고")
# 화면에 그대로 띄우는 설명 — 구분을 모르는 담당자가 있을 수 있어 문구를 한곳에서 관리한다
# (대표 지시 2026-08-04). 여기만 고치면 매입·셋팅 어디서든 같은 말이 나온다.
TIER_HELP = {
    "가용": "셋팅완료·촬영용 등 — 바로 판매 가능한 제품",
    "실재고": "셋팅·시트지 또는 1~2일 내 간단한 보수 후 바로 판매 가능한 제품",
    "가재고": "짜집기·불용 — 판매 가능 재고로 잡지 않는 제품",
}

# ★실재고·가재고의 보수 체크 기본 항목(2026-08-08 대표 요청: "어떤걸 보수해야하는지,
#   하는중인지까지 하나씩 모두 지정 가능"). '기타' 항목은 화면에서 직접 추가한다.
TIER_TASK_PRESETS = {
    "실재고": ("셋팅", "시트지", "도색", "간단보수"),
    "가재고": ("짜집기", "불용", "부품대기"),
}
TIER_TASK_STATES = ("todo", "doing", "done")     # 해야 함 / 작업 중 / 완료


def norm_tier(t):
    """구명칭 호환 — 2026-08-08 전에 저장·전송된 '양품'은 '가용'으로 읽는다.

    갱신 안 된 브라우저(옛 화면 JS)가 보내는 값도 여기서 받아 준다.
    """
    t = (t or "").strip()
    return "가용" if t == "양품" else t


def tier_for_grade(grade):
    """등급만 있고 재고구분이 없을 때 어느 칸으로 볼지 — 대표 기준(2026-08-04).

    등급이 매겨졌다는 건 검수가 끝났다는 뜻이라 가용으로 본다.
    아직 '미정'이면 손볼 데가 있는지 모르는 상태라 실재고로 둔다.
    가재고는 사람이 직접 옮기는 칸이라 여기서 자동으로 정하지 않는다.
    """
    g = (grade or "").strip()
    return "실재고" if (not g or g == "미정") else "가용"

# 매입 전표 단계
STAGES = {"provisional": "가입고", "purchased": "매입"}
PURCHASE_TYPES = ["일반매입", "위탁매입", "반품매입", "기타"]


# ---------------------------------------------------------------- helpers

def asset_event(conn, asset_id, action, detail=None):
    """자산 이력 기록 — 모든 자산 변경은 이 함수를 거친다."""
    actor = g.user["display_name"] if g.get("user") else ""
    conn.execute(
        "INSERT INTO asset_events(asset_id, ts, action, actor, detail) VALUES(?,?,?,?,?)",
        (asset_id, config.now_iso(), action, actor,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None),
    )


def scope_clause(alias="a"):
    """카테고리 스코프 — 스코프 밖 데이터는 서버 쿼리 레벨에서 제외(원칙)."""
    user = g.user
    if user["is_admin"] or user["all_categories"]:
        return "", []
    ids = [r["category_id"] for r in get_db().execute(
        "SELECT category_id FROM user_categories WHERE user_id=?", (user["id"],)).fetchall()]
    if not ids:
        return f" AND {alias}.category_id = -1", []  # 스코프 없음 → 아무것도 안 보임
    ph = ",".join("?" * len(ids))
    return f" AND {alias}.category_id IN ({ph})", ids


def _check_asset_scope(conn, category_id):
    """쓰기 시 스코프 검증 — 자기 카테고리 밖 자산은 만들/수정할 수 없다."""
    user = g.user
    if user["is_admin"] or user["all_categories"]:
        return
    ids = {r["category_id"] for r in conn.execute(
        "SELECT category_id FROM user_categories WHERE user_id=?", (user["id"],)).fetchall()}
    if category_id not in ids:
        abort(403, description="담당 카테고리가 아닌 자산은 등록/수정할 수 없습니다.")


# ★★TMS와 번호대를 나눈다 (대표 결정 2026-07-31).
#
# 두 시스템을 함께 쓰는 동안 규칙이 같으면 같은 번호가 서로 다른 물건에 붙는다.
# 실제로 확인했다: OWS가 새 매입을 등록하면 P260731-001 / 260731-0001이 나오는데,
# 같은 날 TMS에서 등록해도 똑같은 번호가 나온다. 그러면 다음 이관 때 OWS가
# '이미 있는 물건'으로 보고 건너뛰어 TMS 실물이 조용히 누락된다
# (시리얼이 양쪽에 있으면 오류로 막히지만 TMS 시리얼 채움률이 13%뿐이다).
#
# 형식·자릿수는 그대로 두고 시작 번호만 나눈다 — 라벨·서류가 지금과 똑같이 보이고,
# 번호만 봐도 어느 시스템에서 만든 것인지 알 수 있다.
#   자산: TMS 0001~4999 / OWS 5000~9999   (TMS 하루 최대 실적 323번)
#   전표: TMS 001~499   / OWS 500~999     (TMS 하루 최대 실적 29번)
# TMS를 접으면 아래 두 값을 0으로 되돌리면 된다.
OWS_ASSET_SEQ_START = 5000
OWS_SLIP_SEQ_START = 500


def next_asset_no(conn, today=None):
    """관리번호 = YYMMDD-NNNN (TMS와 같은 형식, 번호대만 OWS 몫을 쓴다). today 를 주면 그날 기준(시험용).

    ★재사용 금지(2026-09-03, A4): 그날 마지막 순번을 asset_no_counters 에 남긴다 — 번호를 바꾸거나 넘겨줘
      자산에서 비워진 번호도 다시 나가지 않는다(A/S 문서번호 채번과 같은 원칙).
    ★채번 보류: 그날 연동 사본에 OWS 대역 번호가 살아 있으면 409 — 판정은 tx 밖에서 numbering.precheck_* 가
      해 두고(창구 호출은 트랜잭션 안 금지) 여기서는 그 결과(g.asset_no_check.hold)만 본다.
    """
    hold = (g.get("asset_no_check") or {}).get("hold")
    if hold:
        abort(409, description=hold)
    prefix = (today or config.now()).strftime("%y%m%d")
    row = conn.execute(
        "SELECT MAX(CAST(substr(asset_no, 8) AS INTEGER)) AS m FROM assets "
        "WHERE asset_no GLOB ? AND CAST(substr(asset_no, 8) AS INTEGER) >= ?",
        (prefix + "-[0-9][0-9][0-9][0-9]", OWS_ASSET_SEQ_START),
    ).fetchone()
    last = conn.execute("SELECT last_seq FROM asset_no_counters WHERE day=?", (prefix,)).fetchone()
    # 오늘 OWS가 쓴 번호가 없으면 우리 몫의 첫 번호부터 시작한다. 지금 자산에 남은 최대값과
    # 그날 발급 순번 중 큰 쪽 다음 — 비워진 번호는 건너뛴다.
    seq = max(row["m"] or 0, last["last_seq"] if last else 0, OWS_ASSET_SEQ_START - 1) + 1
    if seq > 9999:
        abort(400, description="당일 관리번호 발번 한도(9999)를 초과했습니다.")
    conn.execute(
        "INSERT INTO asset_no_counters(day, last_seq, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(day) DO UPDATE SET last_seq=excluded.last_seq, updated_at=excluded.updated_at",
        (prefix, seq, config.now_iso()))
    return f"{prefix}-{seq:04d}"


def next_slip_no(conn, stage):
    """전표번호 = V/P + YYMMDD-NNN (가입고 V / 매입 P). 번호대는 OWS 몫."""
    head = "V" if stage == "provisional" else "P"
    prefix = f"{head}{config.now().strftime('%y%m%d')}-"
    row = conn.execute(
        "SELECT MAX(CAST(substr(slip_no, 9) AS INTEGER)) AS m FROM purchase_batches "
        "WHERE slip_no GLOB ? AND CAST(substr(slip_no, 9) AS INTEGER) >= ?",
        (prefix + "[0-9][0-9][0-9]", OWS_SLIP_SEQ_START),
    ).fetchone()
    seq = (row["m"] + 1) if row["m"] else OWS_SLIP_SEQ_START
    if seq > 999:
        abort(400, description="당일 전표번호 발번 한도(999)를 초과했습니다.")
    return f"{prefix}{seq:03d}"


SPEC_FIELDS = ("cpu", "gpu", "ram", "ssd", "inch", "battery", "charger", "location")


def _asset_payload(row, extra=None):
    out = {
        "id": row["id"],
        "assetNo": row["asset_no"],
        "batchId": row["batch_id"],
        "categoryId": row["category_id"],
        "maker": row["maker"],
        "model": row["model"],
        "serial": row["serial"],
        "grade": row["grade"],
        "purchasePrice": row["purchase_price"],
        "salePrice": row["sale_price"],
        "status": row["status"],
        "statusLabel": ASSET_STATUSES.get(row["status"], row["status"]),
        "notes": row["notes"],
        "stockNote": row["stock_note"] if "stock_note" in row.keys() else "",   # 재고비고(TMS 재고상세, 2026-09-03)
        "createdBy": row["created_by"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    for f in SPEC_FIELDS:
        out[f] = row[f]
    out["received"] = bool(row["received"])
    out["receivedAt"] = row["received_at"]
    out["receivedBy"] = row["received_by"]
    out["tier"] = norm_tier(row["tier"])            # 가용/실재고/가재고 (구 '양품'은 가용으로)
    # 보수 체크 — 무엇을 보수해야 하는지/하는 중인지(실재고·가재고용, 2026-08-08 대표)
    try:
        out["tierTasks"] = (json.loads(row["tier_tasks"])
                            if ("tier_tasks" in row.keys() and row["tier_tasks"])
                            else {"items": [], "note": ""})
    except (ValueError, TypeError):
        out["tierTasks"] = {"items": [], "note": ""}
    out["productCode"] = row["product_code"]        # 쇼핑몰 재고의 축(대표 수기 입력)
    # 사업부 귀속 — 목록 배지·상세 이관이력이 이 값을 본다
    out["division"] = row["division"]
    out["divisionSince"] = row["division_since"]
    out["divisionBy"] = row["division_by"]
    out["divisionRef"] = row["division_ref"]
    out["divisionNote"] = row["division_note"]
    out["divisionLocked"] = bool(row["division_locked"])
    # 이관 보류 사유 — 쿼리가 hold_kinds를 함께 뽑아 준 경우에만 채워진다
    try:
        out["holdKinds"] = [k for k in (row["hold_kinds"] or "").split(",") if k]
    except (IndexError, KeyError):
        out["holdKinds"] = []
    # 이관 보류 해제(2026-09-01) — 사람이 "이 사유는 틀렸다"고 판정한 기록
    out["holdOverride"] = [k for k in (row["hold_override"] or "").split(",") if k]
    out["holdOverrideNote"] = row["hold_override_note"]
    out["holdOverrideBy"] = row["hold_override_by"]
    out["holdOverrideAt"] = row["hold_override_at"]
    # RMS 실제 상태 — 쿼리가 함께 뽑아 준 경우에만 채워진다(전표 상세).
    for key, col in (("rmsStatus", "rms_status"), ("rmsRenter", "rms_renter")):
        try:
            out[key] = row[col] or ""
        except (IndexError, KeyError):
            out[key] = ""
    # RMS에 실물 기록이 있나 — 렌탈 귀속인데 없으면 렌탈팀이 운용할 수 없다.
    #   None = 판정 안 함(판매 귀속이거나 RMS 사본을 아직 못 받음).
    try:
        out["inRms"] = None if row["in_rms"] is None else bool(row["in_rms"])
    except (IndexError, KeyError):
        out["inRms"] = None
    out["stockListed"] = bool(row["stock_listed"])  # 재고반영 여부(기본 꺼짐)
    out["stockListedBy"] = row["stock_listed_by"]
    # TMS 자동반영 잠금 — 번호를 바로잡아 'TMS 쪽이 틀렸다'고 판정한 자산(2026-08-18)
    if "tms_lock" in row.keys():
        out["tmsLock"] = bool(row["tms_lock"])
        out["tmsLockNote"] = row["tms_lock_note"]
    if "tms_deleted_at" in row.keys():        # TMS에서 삭제된 자산 표시(연동 창구, 2026-09-02)
        out["tmsDeletedAt"] = row["tms_deleted_at"]
        out["tmsDeletedNote"] = row["tms_deleted_note"]
    if extra:
        out.update(extra)
    # 금액 열람 권한(2026-08-27 대표) — 없으면 서버에서 가린다(화면만 가리면 API로 샌다)
    if not can("purchase.money"):
        out["purchasePrice"] = None
        out["salePrice"] = None
        out["moneyMasked"] = True
    return out


def _repair_book():
    """수리 단가표(kind='repair')를 화면이 쓸 형태로 — 이름과 단가.

    ★대표 2026-08-24: "수리단가표 참고하여 금액 자동입력되게". 화면이 단가를 짐작하지
      않게 서버가 표를 그대로 내려 준다. 표에 없는 항목은 자유 입력이고 단가 0이다.
    """
    rows = get_db().execute(
        "SELECT name, price, COALESCE(kind,'part') AS kind FROM parts "
        "WHERE COALESCE(kind,'part') IN ('repair','paint') AND enabled=1 "
        "ORDER BY CASE COALESCE(kind,'part') WHEN 'repair' THEN 0 ELSE 1 END, grp, sort, name"
    ).fetchall()
    if rows:
        return [{"name": r["name"], "price": r["price"] or 0, "kind": r["kind"]}
                for r in rows]
    # 표가 비어 있어도 화면이 죽지 않게 — 기본 후보만(단가는 모른다)
    return [{"name": n, "price": 0, "kind": "repair"} for n in REPAIR_ITEMS]


@bp.get("/purchase-meta")
def purchase_meta():
    """매입 화면이 쓰는 선택지 모음(등급/상태/단계/매입구분)."""
    require("purchase.view")
    return jsonify({
        "grades": GRADES,
        "statuses": [{"code": k, "label": v} for k, v in ASSET_STATUSES.items()],
        "manualStatuses": sorted(MANUAL_STATUSES),
        # ★사람이 고르는 상태는 이 넷뿐(2026-08-24). 나머지는 흐름이 자동으로 정한다.
        "statusChoices": STATUS_CHOICES,
        "statusGroup": STATUS_GROUP,
        "repairItems": _repair_book(),
        "availableStatuses": list(AVAILABLE_STATUSES),
        "stages": [{"code": k, "label": v} for k, v in STAGES.items()],
        "purchaseTypes": PURCHASE_TYPES,
        "tiers": list(TIERS),
        "tierHelp": TIER_HELP,
        # 보수 체크 — 재고 구분별 기본 항목과 상태(해야함/작업중/완료)
        "tierTaskPresets": {k: list(v) for k, v in TIER_TASK_PRESETS.items()},
        "tierTaskStates": list(TIER_TASK_STATES),
        # ★'재고반영'은 사내 재고와 무관하다 — 쇼핑몰에 재고 수를 밀어넣을 때만 쓰는
        #   전송 대상 표시다(대표 2026-08-17: "재고 구분으로 이미 재고가 정해지는데
        #   재고반영 버튼이 왜 필요하냐"). 몰 재고 연동을 켠 몰이 하나도 없으면
        #   화면에서 관련 버튼·칸을 통째로 감춘다 — 눌러도 아무 일이 안 나는 버튼이라서다.
        "stockSyncOn": _stock_sync_on(),
    })


def _stock_sync_on():
    """쇼핑몰 재고 연동을 켠 몰이 하나라도 있나(설정 stock_sync)."""
    row = get_db().execute("SELECT value FROM settings WHERE key='stock_sync'").fetchone()
    if not row:
        return False
    try:
        data = json.loads(row["value"]) or {}
    except ValueError:
        return False
    # 켜짐 판정은 stock_sync._is_on 과 같게 — 명시적 True 하나뿐(실수로 켜지는 길을 막는다)
    return any((v or {}).get("enabled") is True for v in data.values() if isinstance(v, dict))


_TIER_TASK_LABELS = {"todo": "해야함", "doing": "작업중", "done": "완료"}


def _clean_tier_tasks(raw):
    """보수 체크 입력 검증 → 저장용 JSON 문자열(비었으면 ""). 잘못된 형식은 400.

    {"items":[{"name":"시트지","state":"doing"},...], "note":"기타 메모"} 꼴만 받는다.
    항목 이름은 30자·20개까지, 상태는 todo/doing/done만. 같은 이름은 한 번만.
    """
    if not isinstance(raw, dict):
        abort(400, description="보수 체크 형식이 잘못됐습니다.")
    items_in = raw.get("items") or []
    if not isinstance(items_in, list) or len(items_in) > 20:
        abort(400, description="보수 항목은 20개까지 넣을 수 있습니다.")
    items, seen = [], set()
    for it in items_in:
        if not isinstance(it, dict):
            abort(400, description="보수 항목 형식이 잘못됐습니다.")
        name = str(it.get("name") or "").strip()[:30]
        if not name:
            continue
        state = str(it.get("state") or "todo").strip()
        if state not in TIER_TASK_STATES:
            abort(400, description="보수 상태는 해야함/작업중/완료만 됩니다.")
        if name in seen:
            continue
        seen.add(name)
        items.append({"name": name, "state": state})
    note = str(raw.get("note") or "").strip()[:500]
    if not items and not note:
        return ""
    return json.dumps({"items": items, "note": note}, ensure_ascii=False)


def _tier_tasks_summary(packed):
    """이력·화면용 한 줄 요약: '시트지(작업중) · 셋팅(완료) · 메모'"""
    if not packed:
        return "없음"
    try:
        data = json.loads(packed)
    except (ValueError, TypeError):
        return "없음"
    parts = ["%s(%s)" % (i["name"], _TIER_TASK_LABELS.get(i["state"], i["state"]))
             for i in data.get("items", [])]
    if (data.get("note") or "").strip():
        parts.append("메모")
    return " · ".join(parts) or "없음"


def _unlist(conn, aid, reason):
    """재고반영을 끈다 — 판매 불가능해진 자산이 몰 재고에 세어지면 안 된다(유령 재고).

    2026-08-09 대표 승인 정합성 개선: 상태가 판매가능 축(AVAILABLE_STATUSES)을
    벗어나는 순간 여기서 함께 꺼진다(수동 변경·일괄 변경·TMS 자동갱신 모두).
    """
    n = conn.execute(
        "UPDATE assets SET stock_listed=0, stock_listed_at='', stock_listed_by='' "
        "WHERE id=? AND stock_listed=1", (aid,)).rowcount
    if n:
        asset_event(conn, aid, "재고해제", {"사유": reason})
    return bool(n)


def _get_asset_or_404(conn, asset_id):
    # ★RMS 사본을 함께 붙인다 — 렌탈 자산은 OWS status 로 실상을 말할 수 없다
    #   (대표 2026-09-01: "상태가 판매가능인데 어떤 의미가 있는지 모르겠어").
    row = conn.execute(
        "SELECT a.*, ri.status AS rms_status, ri.renter AS rms_renter FROM assets a "
        "LEFT JOIN rms_inventory ri ON ri.asset_no = a.asset_no COLLATE NOCASE "
        "WHERE a.id=?", (asset_id,)).fetchone()
    if row is None:
        abort(404, description="자산을 찾을 수 없습니다.")
    return row


# ---------------------------------------------------------------- suppliers

@bp.get("/suppliers")
def list_suppliers():
    require("purchase.view")
    rows = get_db().execute(
        "SELECT s.*, "
        " (SELECT COUNT(*) FROM purchase_batches b WHERE b.supplier_id = s.id) AS batch_count, "
        " (SELECT COALESCE(SUM(b.total_amount),0) FROM purchase_batches b WHERE b.supplier_id = s.id) AS total_amount "
        "FROM suppliers s ORDER BY s.name"
    ).fetchall()
    return jsonify([
        {"id": r["id"], "name": r["name"], "contact": r["contact"], "phone": r["phone"],
         "memo": r["memo"], "enabled": bool(r["enabled"]),
         "batchCount": r["batch_count"], "totalAmount": r["total_amount"]}
        for r in rows
    ])


@bp.post("/suppliers")
def create_supplier():
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        abort(400, description="거래처 이름을 입력하세요.")
    with tx(write=True) as conn:
        if conn.execute("SELECT id FROM suppliers WHERE name=?", (name,)).fetchone():
            abort(409, description="이미 존재하는 거래처입니다.")
        cur = conn.execute(
            "INSERT INTO suppliers(name, contact, phone, memo, created_at) VALUES(?,?,?,?,?)",
            (name, (body.get("contact") or "").strip(), (body.get("phone") or "").strip(),
             (body.get("memo") or "").strip(), config.now_iso()),
        )
        audit.log("supplier_created", target=name)
        sid = cur.lastrowid
    return jsonify({"id": sid, "name": name}), 201


@bp.patch("/suppliers/<int:sid>")
def update_supplier(sid):
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
        if row is None:
            abort(404, description="거래처를 찾을 수 없습니다.")
        changes = {}
        if "name" in body:
            name = (body.get("name") or "").strip()
            if not name:
                abort(400, description="거래처 이름을 입력하세요.")
            dup = conn.execute("SELECT id FROM suppliers WHERE name=? AND id != ?", (name, sid)).fetchone()
            if dup:
                abort(409, description="이미 존재하는 거래처 이름입니다.")
            conn.execute("UPDATE suppliers SET name=? WHERE id=?", (name, sid))
            changes["name"] = name
        for field, col in (("contact", "contact"), ("phone", "phone"), ("memo", "memo")):
            if field in body:
                conn.execute(f"UPDATE suppliers SET {col}=? WHERE id=?",
                             ((body.get(field) or "").strip(), sid))
                changes[field] = (body.get(field) or "").strip()
        if "enabled" in body:
            conn.execute("UPDATE suppliers SET enabled=? WHERE id=?", (1 if body["enabled"] else 0, sid))
            changes["enabled"] = bool(body["enabled"])
        if not changes:
            abort(400, description="변경할 항목이 없습니다.")
        audit.log("supplier_updated", target=row["name"], detail=changes)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- parts (부품 단가표)
#
# 대표 요청(2026-08-10): "RAM·SSD 없이 들어오는 노트북에 부품을 추가하는데 단가가 매일
# 바뀐다 — 매번 금액을 입력할 수 없다." → 여기 '오늘 단가'만 갱신해 두면, 자산에 부품을
# 추가할 때 서버가 이 표에서 단가를 읽어 수리비(asset_repairs)로 넣는다.
# ★그 순간 단가가 자산에 동결 스냅샷되므로 나중에 표를 고쳐도 과거 원가는 안 바뀐다.
# ★price 는 부가세 포함 총액 — add_repair 의 split_vat 이 공급가/부가세를 알아서 가른다.

PART_CATEGORIES = ("ram", "ssd", "")

# 옵션 '구분별 연결'의 구분 축(2026-08-13 대표) — 램 세대 + 저장장치 4종.
# code_specs(제품코드 모델 속성)의 값과 같은 문자열을 쓴다 — 비교가 곧 판별이다.
# ★바꾸면 app.js PART_GEN_AXES 미러와 collect.STORAGE_TYPES도 같이.
PART_GEN_AXES = ("DDR3", "DDR4", "DDR5", "LPDDR3", "LPDDR4", "LPDDR5",
                 "M.2 NVMe", "M.2 SATA", "2.5 SSD", "2.5 HDD")


def resolve_option_part(conn, opt_row, order_row):
    """옵션에 연결된 부품을 확정한다 — (parts 행 또는 None, 보류 사유).

    구분별 연결(part_map)이 있으면 주문 제품코드의 스펙(code_specs)으로 고른다:
    "16GB 추가" × 코드가 DDR5 → D5 16G 부품. ★스펙을 모르면 추측하지 않고
    보류한다 — 틀린 단가가 조용히 들어가는 것보다 안 들어가고 알리는 게 낫다.
    part_map이 없으면 기존 단일 연결(part_id) 그대로다.
    """
    raw = (opt_row["part_map"] or "").strip()
    entries = []
    if raw:
        try:
            entries = [e for e in (json.loads(raw) or []) if isinstance(e, dict)]
        except ValueError:
            entries = []
    if not entries:
        if opt_row["part_id"]:
            return (conn.execute("SELECT * FROM parts WHERE id=? AND enabled=1",
                                 (opt_row["part_id"],)).fetchone(), "")
        return None, ""
    # ★후보가 하나뿐이면 제품코드 없이도 확정한다(대표 2026-08-25 "제품코드가 없어도
    #   차감될 수 있게") — 고를 게 하나면 추측이 아니다. 둘 이상일 때만 스펙이 필요하다.
    live = []
    for ent in entries:
        p_ = conn.execute("SELECT * FROM parts WHERE id=? AND enabled=1",
                          (ent.get("partId"),)).fetchone()
        if p_ is not None:
            live.append((ent, p_))
    if len(live) == 1:
        return live[0][1], ""
    code = (order_row["product_code"] or "").strip()
    if not code:
        return None, ("주문에 제품코드가 없어 구분(DDR4/DDR5 등)을 판별할 수 없습니다 — "
                      "자동 기입 보류. 상세 보기의 [🔩 램/SSD 차감]으로 직접 차감하거나, "
                      "제품코드를 넣으면 자동으로 이어집니다.")
    spec = conn.execute("SELECT * FROM code_specs WHERE code=?", (code,)).fetchone()
    gens = set()
    if spec:
        if spec["ram_gen"]:
            gens.add(spec["ram_gen"])
        if spec["storage_type"]:
            gens.add(spec["storage_type"])
    for ent in entries:
        if ent.get("gen") in gens:
            p = conn.execute("SELECT * FROM parts WHERE id=? AND enabled=1",
                             (ent.get("partId"),)).fetchone()
            if p:
                return p, ""
            return None, (f"'{ent.get('gen')}' 구분에 연결된 부품이 단가표에 없습니다"
                          "(꺼짐/삭제) — 원가 기입 보류.")
    have = ", ".join(sorted(gens)) or "미확인"
    return None, (f"제품코드 '{code}'의 스펙({have})에 맞는 구분 연결이 없어 원가 기입을 "
                  "보류했습니다 — 제품코드 칸의 [스펙 불러오기]로 세대를 채우거나 "
                  "설정 ▸ 제공 옵션의 구분별 연결을 확인하세요.")


@bp.get("/parts")
def list_parts():
    require("purchase.view")
    # ★부품/수리를 나눠 본다(2026-08-24). kind 를 안 주면 예전처럼 전부 — 옛 화면이 죽지 않게.
    kind = (request.args.get("kind") or "").strip().lower()
    where, args = "", []
    if kind in ("part", "repair", "paint"):
        where = " WHERE COALESCE(p.kind,'part') = ?"
        args = [kind]
    rows = get_db().execute(
        # ★'이름 x수량' 기록도 센다 — 삭제 확인창(_part_used_count)과 같은 기준이어야
        #   "사용 흔적 없음"이라고 안심하고 지우는 일이 없다(2026-08-14 검토).
        "SELECT p.*, "
        " (SELECT COUNT(*) FROM asset_repairs r "
        "    WHERE r.parts = p.name OR r.parts LIKE p.name || ' x%') AS used_count "
        "FROM parts p" + where +
        " ORDER BY p.enabled DESC, p.grp, p.sort, p.category, p.name", args).fetchall()
    return jsonify([
        {"id": r["id"], "name": r["name"], "category": r["category"], "price": r["price"],
         "kind": (r["kind"] if "kind" in r.keys() else "part") or "part",
         "group": r["grp"], "enabled": bool(r["enabled"]), "usedCount": r["used_count"],
         "updatedAt": r["updated_at"], "updatedBy": r["updated_by"]}
        for r in rows
    ])


@bp.post("/parts")
def create_part():
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name or len(name) > 60:
        abort(400, description="부품 이름을 1~60자로 입력하세요. (스펙 표기와 맞추면 좋습니다 — 예: D4 8G, NVMe 512G)")
    category = (body.get("category") or "").strip().lower()
    if category not in PART_CATEGORIES:
        abort(400, description="구분은 ram / ssd / 기타(빈값) 중 하나여야 합니다.")
    price = _money_or_400(body.get("price") or 0, "단가")
    kind = (body.get("kind") or "part").strip().lower()
    if kind not in ("part", "repair", "paint"):
        abort(400, description="구분은 part(부품)/repair(수리)/paint(도색·시트지)만 됩니다.")
    grp = (body.get("group") or "").strip()
    if len(grp) > 30:
        abort(400, description="대제목(그룹)은 30자 이내여야 합니다.")
    if not grp and category == "ram":
        grp = "RAM"                       # 램은 물어볼 것 없이 RAM 묶음이다
    ts = config.now_iso()
    with tx(write=True) as conn:
        if conn.execute("SELECT id FROM parts WHERE name=?", (name,)).fetchone():
            abort(409, description="이미 있는 부품입니다. 단가는 그 줄에서 고치세요.")
        cur = conn.execute(
            "INSERT INTO parts(name, category, grp, kind, price, created_at, created_by, "
            "updated_at, updated_by) VALUES(?,?,?,?,?,?,?,?,?)",
            (name, category, grp, kind, price, ts, g.user["display_name"], ts,
             g.user["display_name"]))
        audit.log("part_created", target=name,
                  detail={"category": category, "price": price, "group": grp, "kind": kind})
        pid = cur.lastrowid
    return jsonify({"id": pid, "name": name}), 201


@bp.patch("/parts/<int:pid>")
def update_part(pid):
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    ts = config.now_iso()
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM parts WHERE id=?", (pid,)).fetchone()
        if row is None:
            abort(404, description="부품을 찾을 수 없습니다.")
        changes = {}
        if "price" in body:
            price = _money_or_400(body.get("price") or 0, "단가")
            if price != row["price"]:
                conn.execute("UPDATE parts SET price=? WHERE id=?", (price, pid))
                # 단가 변경 이력은 감사로그가 정본 — {from, to} 로 남긴다
                audit.log("part_price_updated", target=row["name"],
                          detail={"from": row["price"], "to": price})
                changes["price"] = price
        if "name" in body:
            name = (body.get("name") or "").strip()
            if not name or len(name) > 60:
                abort(400, description="부품 이름을 1~60자로 입력하세요.")
            if name != row["name"]:
                if conn.execute("SELECT id FROM parts WHERE name=? AND id != ?", (name, pid)).fetchone():
                    abort(409, description="이미 있는 부품 이름입니다.")
                conn.execute("UPDATE parts SET name=? WHERE id=?", (name, pid))
                changes["name"] = name
        if "category" in body:
            category = (body.get("category") or "").strip().lower()
            if category not in PART_CATEGORIES:
                abort(400, description="구분은 ram / ssd / 기타(빈값) 중 하나여야 합니다.")
            conn.execute("UPDATE parts SET category=? WHERE id=?", (category, pid))
            changes["category"] = category
        if "group" in body:
            grp = (body.get("group") or "").strip()
            if len(grp) > 30:
                abort(400, description="대제목(그룹)은 30자 이내여야 합니다.")
            conn.execute("UPDATE parts SET grp=? WHERE id=?", (grp, pid))
            changes["group"] = grp
        if "enabled" in body:
            conn.execute("UPDATE parts SET enabled=? WHERE id=?", (1 if body["enabled"] else 0, pid))
            changes["enabled"] = bool(body["enabled"])
        if not changes:
            abort(400, description="변경할 항목이 없습니다.")
        conn.execute("UPDATE parts SET updated_at=?, updated_by=? WHERE id=?",
                     (ts, g.user["display_name"], pid))
        if "price" not in changes:
            audit.log("part_updated", target=row["name"], detail=changes)
    return jsonify({"ok": True})


def _part_used_count(conn, name):
    """이 부품이 자산에 들어간 횟수 — 목록과 삭제 확인창이 같은 숫자를 말하게 한 곳에서.

    ★수량이 2 이상이면 기록이 '이름 x2'로 남는다(_apply_part_to_asset). 이름만
      비교하면 그런 기록이 빠져 '사용 흔적 없음'처럼 보인다(2026-08-14 검토).
    """
    return conn.execute(
        "SELECT COUNT(*) AS c FROM asset_repairs WHERE parts = ? OR parts LIKE ?",
        (name, name + " x%")).fetchone()["c"]


def part_dependents(conn, row):
    """이 부품을 '지우거나 끄면' 조용히 망가지는 곳 — 사람이 읽을 수 있는 사유 목록.

    두 갈래가 있다(2026-08-14 검토에서 확정):
      ① id로 연결 — 제공 옵션의 단일 연결(part_id)·구분별 연결(part_map JSON).
         ★LIKE로 찾으면 안 된다: partId 1 검사가 partId 11에도 걸려(접두 일치)
           엉뚱한 부품이 안 지워진다. JSON을 파싱해 정수로 비교한다.
      ② 이름으로 연결 — 기준사양 채우기·판매 구성 칩은 'D4 8G' 같은 부품 '이름'을
         단가표에서 찾아 쓴다(_live_part). 지우거나 끄면 그 축의 원가가 조용히 0이 되고
         화면엔 '보류' 사유만 뜬다. 이름이 규약(D4/M.2/2.5)과 맞으면 알려 준다.
    """
    reasons = []
    for o in conn.execute("SELECT id, name, part_id, part_map FROM prep_options").fetchall():
        if o["part_id"] == row["id"]:
            reasons.append(f"제공 옵션 「{o['name']}」의 부품 연결")
            continue
        raw = (o["part_map"] or "").strip()
        if not raw:
            continue
        try:
            entries = json.loads(raw) or []
        except ValueError:
            continue
        for e in entries:
            if isinstance(e, dict) and str(e.get("partId")) == str(row["id"]):
                reasons.append(f"제공 옵션 「{o['name']}」의 구분별 연결({e.get('gen')})")
                break
    # 이름으로 찾아 쓰는 자리 — 규격 이름이면 기준사양/판매구성이 이 이름을 부른다
    name = (row["name"] or "").strip()
    if re.match(r"^(D[345]\s+\d+G|M\.2 (NVMe|SATA) |2\.5 (SSD|HDD) )", name):
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM code_specs WHERE "
            "  (ram_gen != '' AND ram_gb > 0) OR (storage_type != '' AND storage_cap != '')"
        ).fetchone()["c"]
        if n:
            reasons.append(f"기준사양 채우기·판매 구성 칩이 이름으로 찾는 규격 "
                           f"(기준사양이 등록된 제품코드 {n}개)")
    return reasons


@bp.delete("/parts/<int:pid>")
def delete_part(pid):
    """부품을 단가표에서 지운다(대표 2026-08-14: "삭제기능 없어서 추가").

    ★과거 원가는 안전하다 — 자산에 붙은 부품비(asset_repairs)는 그때 단가를 복사해
      둔 '스냅샷'이라 단가표에서 지워도 금액·이력이 그대로다.
    ★막는 경우: 제공 옵션에 연결돼 있으면 지우지 않는다. 지우면 그 옵션 체크가
      조용히 아무 일도 안 하게 되어(원가 미기입) 나중에 순이익이 틀어진다.
      어느 옵션인지 알려 주고, 연결을 먼저 풀게 한다.
    """
    require("purchase.edit")
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM parts WHERE id=?", (pid,)).fetchone()
        if row is None:
            abort(404, description="부품을 찾을 수 없습니다.")
        deps = part_dependents(conn, row)
        opt_deps = [d for d in deps if d.startswith("제공 옵션")]
        if opt_deps:
            abort(400, description="지울 수 없습니다 — " + ", ".join(opt_deps[:5])
                                   + " — 설정 ▸ 제공 옵션에서 연결을 먼저 푸세요.")
        # 이름으로 찾아 쓰는 자리는 막지는 않되(연결을 풀 방법이 없다) 강제로 알린다
        if deps and not (request.get_json(silent=True) or {}).get("force"):
            abort(409, description="이 이름을 자동으로 찾아 쓰는 곳이 있습니다: "
                                   + " · ".join(deps)
                                   + " — 지우면 그 자리의 원가가 조용히 0이 됩니다. "
                                     "정말 지우려면 다시 한 번 확인해 주세요.")
        used = _part_used_count(conn, row["name"])
        conn.execute("DELETE FROM parts WHERE id=?", (pid,))
        audit.log("part_deleted", target=row["name"],
                  detail={"price": row["price"], "group": row["grp"], "사용이력": used})
    return jsonify({"ok": True, "usedCount": used})


def _apply_part_to_asset(conn, asset_row, part, repair_date, actor,
                         qty=1, order_id=None, prep_option_id=None, remove=False,
                         dedup=True):
    """부품을 자산 하나에 붙이거나(장착) 뺀다(회수) — 오늘 단가로 수리 기록 + 스펙 칸 갱신.

    ★스펙 칸(ram/ssd)도 같이 고친다. 부품을 꽂았는데 스펙이 'X'로 남으면
      재고 그룹핑과 주문 자산매칭이 거짓말을 한다(살아있는 자산의 38%가 ssd='X').
      빈칸/'X' → 부품명으로 대체, 이미 값이 있으면 ' + ' 병기(기존 표기 관례).

    ★회수(remove=True, 2026-08-13 대표 "다운그레이드 차액은 순수익에 더해야"):
      원가에 '음수'로 기록한다 — 16G를 빼고 8G를 꽂으면 (−16G단가 +8G단가)로
      차액만큼 원가가 줄어 순수익이 그만큼 커진다(대표 공식 A=16G−8G와 동일).
      스펙 칸에서는 그 부품명을 하나 지운다(' + ' 병기면 하나만, 마지막이면 'X').
      빼낸 부품은 '부품 재고'로 돌아온다(+1) — 장착은 재고에서 나간다(−1).
      부품 재고 원장은 part_stock_moves, 현재고 = SUM(qty) (2026-08-25 대표).

    ★옵션 자동 기입(order_id+prep_option_id)일 때는 같은 조합이 이미 있으면 아무것도
      안 한다(멱등). 매칭→해제→재매칭을 반복해도 원가가 중복 계상되지 않는 장치다.
      스펙 갱신 전 값은 spec_prev(JSON)로 남겨 되돌릴 수 있게 한다.
    """
    # ★dedup=False는 '한 옵션이 회수+장착 두 줄'을 넣는 판매 구성 칩 전용 —
    #   호출자가 (자산,주문,옵션) 중복을 미리 확인했을 때만 끈다.
    if dedup and order_id is not None and prep_option_id is not None:
        dup = conn.execute(
            "SELECT id FROM asset_repairs WHERE asset_id=? AND order_id=? AND prep_option_id=?",
            (asset_row["id"], order_id, prep_option_id)).fetchone()
        if dup:
            return 0
    qty = max(1, int(qty or 1))
    cost = (part["price"] * qty) * (-1 if remove else 1)
    net, vat = split_vat(cost)
    label = part["name"] if qty == 1 else f"{part['name']} x{qty}"
    spec_change = None
    if part["category"] in ("ram", "ssd"):
        col = part["category"]
        cur = (asset_row[col] or "").strip()
        if remove:
            pieces = [s.strip() for s in cur.split(" + ") if s.strip()]
            if part["name"] in pieces:
                pieces.remove(part["name"])       # 같은 게 두 개 병기돼 있으면 하나만 뺀다
                new = " + ".join(pieces) or "X"   # 마지막 하나를 뺐으면 '없음' 표기
            else:
                new = cur                          # 스펙에 없는 부품 회수 — 원가만 기록
        else:
            new = part["name"] if (not cur or cur.upper() == "X") else f"{cur} + {part['name']}"
        if new != cur:
            spec_change = (col, cur, new)
    cur = conn.execute(
        "INSERT INTO asset_repairs(asset_id, repair_date, description, cost, vat, net, parts, "
        "order_id, prep_option_id, spec_prev, created_by, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (asset_row["id"], repair_date,
         f"부품 {'회수' if remove else '추가'}: {label}", cost, vat, net, label,
         order_id, prep_option_id,
         json.dumps({"col": spec_change[0], "from": spec_change[1], "to": spec_change[2]},
                    ensure_ascii=False) if spec_change else "",
         actor, config.now_iso()))
    # ── 부품 수량 이동(2026-08-25 대표) — 장착이면 재고에서 −qty, 회수면 +qty.
    #    repair_id 로 원가 행과 짝: 원가를 되돌리는 모든 경로가 이 행을 지워 재고도 복원된다.
    #    단가표의 '물건'(kind=part)만 — 수리·도색은 공임이라 수량이 없다.
    if ((part["kind"] if "kind" in part.keys() else "part") or "part") == "part":
        conn.execute(
            "INSERT INTO part_stock_moves(part_id, qty, unit_cost, repair_id, order_id, "
            "asset_id, reason, move_date, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (part["id"], qty if remove else -qty, part["price"] or 0, cur.lastrowid,
             order_id, asset_row["id"], "return" if remove else "use",
             repair_date, actor, config.now_iso()))
    if spec_change:
        conn.execute(f"UPDATE assets SET {spec_change[0]}=?, updated_at=? WHERE id=?",
                     (spec_change[2], config.now_iso(), asset_row["id"]))
    queue_asset_cost(conn, asset_row["id"], reason=f"부품 {'회수' if remove else '추가'}: {label}"[:60])
    asset_event(conn, asset_row["id"], "부품회수" if remove else "부품추가",
                {"부품": label, "cost": cost, "공급가": net, "부가세": vat,
                 **({"출처": f"옵션 자동(주문 #{order_id})"} if order_id else {}),
                 **({"스펙": f"{spec_change[0]}: {spec_change[1] or '(없음)'} → {spec_change[2]}"}
                    if spec_change else {})})
    return cost


def revert_option_parts(conn, order_id, asset_id=None, option_id=None, actor=""):
    """옵션 자동 기입분을 되돌린다 — 체크 해제·매칭 해제·주문 취소에서 부른다.

    ★수기로 넣은 부품·수리는 절대 안 건드린다(order_id+prep_option_id 가 있는 행만).
    ★스펙은 '우리가 바꾼 그대로면' 원래 값으로 되돌린다 — 그 사이 사람이 고쳤으면 둔다.
    ★회수 입고에서는 부르지 않는다: 돌아온 기계엔 부품이 물리적으로 꽂혀 있다.
    """
    sql = ("SELECT * FROM asset_repairs WHERE order_id=? AND prep_option_id IS NOT NULL")
    params = [order_id]
    if asset_id is not None:
        sql += " AND asset_id=?"
        params.append(asset_id)
    if option_id is not None:
        sql += " AND prep_option_id=?"
        params.append(option_id)
    # ★역순으로 되돌린다 — 판매 구성 칩은 '회수→장착' 두 줄이 한 짝이라, 넣은 순서
    #   그대로 되돌리면 장착 흔적이 남아 스펙이 'X'로 굳는다(장착 취소가 먼저여야 한다).
    sql += " ORDER BY id DESC"
    reverted = 0
    for r in conn.execute(sql, params).fetchall():
        conn.execute("DELETE FROM asset_repairs WHERE id=?", (r["id"],))
        conn.execute("DELETE FROM part_stock_moves WHERE repair_id=?", (r["id"],))
        if r["spec_prev"]:
            try:
                sp = json.loads(r["spec_prev"])
                cur = conn.execute(f"SELECT {sp['col']} AS v FROM assets WHERE id=?",
                                   (r["asset_id"],)).fetchone()
                if cur and (cur["v"] or "").strip() == sp["to"]:
                    conn.execute(f"UPDATE assets SET {sp['col']}=?, updated_at=? WHERE id=?",
                                 (sp["from"], config.now_iso(), r["asset_id"]))
            except (ValueError, KeyError):
                pass
        queue_asset_cost(conn, r["asset_id"], reason="부품 되돌리기(옵션 해제)")
        asset_event(conn, r["asset_id"], "부품회수",
                    {"부품": r["parts"], "cost": r["cost"],
                     "사유": "옵션 체크 해제/매칭 해제", "주문": order_id})
        reverted += 1
    return reverted


def apply_pending_part_uses(conn, order_row, asset_ids, actor):
    """자산 매칭 순간, '매칭 전 수동 차감'(자산 없는 자리표 이동)을 자산 원가로 이관한다.

    (2026-08-26 대표 "제품코드를 입력하지 못한 경우에도 쓸 수 있게") 재고는 자리표가
    이미 움직였으므로, ★자리표를 지우고 _apply_part_to_asset 으로 다시 기록한다 —
    순변화 0(이중 차감 없음). 단가는 차감한 날 값(unit_cost)으로 동결해 이관한다.
    여러 대가 한 번에 붙으면 첫 자산에 몰아 기입한다(수동 차감은 어차피 대상 하나).
    """
    if not asset_ids:
        return 0
    rows = conn.execute(
        "SELECT * FROM part_stock_moves WHERE order_id=? AND asset_id IS NULL "
        "AND reason IN ('use','return') AND repair_id IS NULL",
        (order_row["id"],)).fetchall()
    if not rows:
        return 0
    asset = conn.execute("SELECT * FROM assets WHERE id=?", (asset_ids[0],)).fetchone()
    if asset is None:
        return 0
    today = config.now().strftime("%Y-%m-%d")
    n = 0
    for m in rows:
        part = conn.execute("SELECT * FROM parts WHERE id=?", (m["part_id"],)).fetchone()
        if part is None:
            continue
        # 차감한 날의 단가로 동결 — 그 사이 단가표가 바뀌었어도 원가가 흔들리면 안 된다
        frozen = dict(part)
        if m["unit_cost"]:
            frozen["price"] = m["unit_cost"]
        conn.execute("DELETE FROM part_stock_moves WHERE id=?", (m["id"],))
        _apply_part_to_asset(conn, asset, frozen, m["move_date"] or today, actor,
                             qty=abs(m["qty"]), order_id=order_row["id"],
                             remove=(m["reason"] == "return"))
        asset = conn.execute("SELECT * FROM assets WHERE id=?", (asset["id"],)).fetchone()
        n += 1
    return n


def apply_checked_option_parts(conn, order_row, asset_ids, actor):
    """이 주문에서 '체크된' 챙길옵션 중 부품이 연결된 것들을 자산들에 기입한다.

    매칭 시점 훅 — 체크가 먼저 됐고 자산이 나중에 붙는 순서를 받친다.
    (반대 순서는 prep.toggle_check 가 받친다. 어느 쪽이 먼저든 결과가 같다.)
    단가 0원·부품 꺼짐은 조용히 건너뛴다 — 여기서 400을 내면 스캔 매칭 자체가 막힌다.
    """
    opts = conn.execute(
        "SELECT po.* FROM order_option_checks oc "
        "JOIN prep_options po ON po.id = oc.option_id "
        "WHERE oc.order_id = ? AND (po.part_id IS NOT NULL "
        "      OR TRIM(COALESCE(po.part_map,'')) != '' "
        "      OR TRIM(COALESCE(po.kind,'')) != '')",
        (order_row["id"],)).fetchall()
    if not opts:
        return 0
    today = config.now().strftime("%Y-%m-%d")
    applied = 0
    # 옵션마다 부품을 먼저 확정한다 — 구분별 연결이면 제품코드 스펙(D4/D5)으로 고르고,
    # 판별이 안 되면 그 옵션만 조용히 건너뛴다(매칭 흐름을 막으면 안 되는 자리).
    # 판매 구성 칩(kind)은 자산마다 실물이 달라 자산 루프 안에서 그때 계산한다.
    resolved = []
    for o in opts:
        if (o["kind"] or "").strip():
            resolved.append(("config", o, None))
            continue
        part, _warn = resolve_option_part(conn, o, order_row)
        if part is not None and part["price"]:
            resolved.append(("part", o, part))
    for aid in asset_ids:
        a = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
        if a is None:
            continue
        for tag, o, part in resolved:
            if tag == "part":
                applied += 1 if _apply_part_to_asset(
                    conn, a, part, today, actor, qty=o["part_qty"] or 1,
                    order_id=order_row["id"], prep_option_id=o["id"]) else 0
                a = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
                continue
            # 판매 구성 — (자산,주문,옵션) 중복을 먼저 보고, 회수+장착 짝을 넣는다
            if conn.execute(
                    "SELECT 1 FROM asset_repairs WHERE asset_id=? AND order_id=? "
                    "AND prep_option_id=?", (aid, order_row["id"], o["id"])).fetchone():
                continue
            actions, _why = sold_config_actions(conn, a, order_row, o["kind"])
            if not actions:
                continue
            for p_, rm in actions:
                _apply_part_to_asset(conn, a, p_, today, actor,
                                     order_id=order_row["id"], prep_option_id=o["id"],
                                     remove=rm, dedup=False)
                a = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            applied += 1
    return applied


# ---------------------------------------------------------------- 기준사양 부족분(매입)
#
# 대표 승인(2026-08-14): "SSD·RAM 없는 제품이 들어왔을 때 제품코드를 넣었다면, 코드
# 스펙(DDR4 8GB·M.2 SATA 256GB) 기준으로 부품 단가표 단가를 적용" — 버튼+확인창,
# 적용 즉시 스펙 반영(매입 직후 꽂는 흐름, 매입 담당자가 최초 진행), 상위 장착이
# 들어온 경우 다운그레이드 회수도 제안에 포함.
# ★멱등 장치: 적용하면 자산 스펙이 기준과 같아져 다음 계산에서 부족분이 0이 된다 —
#   두 번 눌러도 중복 기입이 없다. ★전표 매입금액에는 절대 안 섞인다(불변식 유지).

_GB_SIZES = (2, 4, 6, 8, 12, 16, 24, 32, 64)


def _ram_total_gb(text):
    """자산 램 표기의 총 GB — 'D4 8G'=8, '8GB'=8, 'D4 8G + D4 8G'=16, 'X'/빈값=0."""
    t = (text or "").strip().upper()
    if not t or t == "X":
        return 0
    total = 0
    for m in re.finditer(r"(\d{1,3})\s*G", t):
        v = int(m.group(1))
        if v in _GB_SIZES:
            total += v
    return total


def _storage_cap_of(text):
    """저장장치 표기의 용량 라벨('256G'/'1TB') — 부품 이름 규약과 동일."""
    t = (text or "").strip().upper()
    if not t or t == "X":
        return ""
    m = re.search(r"([12])\s*TB", t)
    if m:
        return m.group(1) + "TB"
    for m2 in re.finditer(r"(\d{3,4})\s*G", t):
        if int(m2.group(1)) >= 100:
            return m2.group(1) + "G"
    return ""


def _live_part(conn, name):
    return conn.execute("SELECT * FROM parts WHERE name=? AND enabled=1",
                        (name,)).fetchone()


def _ram_axis(conn, a, gen, target):
    """램 축의 작업 목록 — 목표 용량(target GB)에 맞추기 위한 (장착/회수, 경고)."""
    actions, warns = [], []
    if not (target and (gen or "").startswith("DDR")):
        return actions, warns
    gnum = gen[3]
    cur = _ram_total_gb(a["ram"])
    if cur != target:
        tgt = _live_part(conn, f"D{gnum} {target}G")
        if tgt is None or not tgt["price"]:
            warns.append(f"단가표 'D{gnum} {target}G' 없음/단가 0 — 램 보류")
        elif cur == 0:
            actions.append((tgt, False))
        else:
            # 다른 용량이 꽂혀 있음 — 회수+장착(다운그레이드 포함, 대표 승인)
            rm = _live_part(conn, f"D{gnum} {cur}G")
            if rm is None or not rm["price"]:
                warns.append(f"실물 램 {a['ram']}({cur}G)에 맞는 단가표 부품이 없어 교체 보류")
            else:
                actions.append((rm, True))
                actions.append((tgt, False))
    return actions, warns


def _storage_axis(conn, a, stype, scap):
    """저장장치 축 — 목표(방식+용량 라벨)에 맞추기 위한 (장착/회수, 경고).
    용량이 같으면 방식 표기가 달라도 통과(실물 표기가 방식을 생략하는 경우가 많다)."""
    actions, warns = [], []
    if not (stype and scap):
        return actions, warns
    cur_txt = (a["ssd"] or "").strip()
    cur_cap = _storage_cap_of(cur_txt)
    tgt_name = f"{stype} {scap}"
    if not cur_cap and cur_txt and cur_txt.upper() != "X":
        warns.append(f"실물 저장장치 표기 '{cur_txt}'에서 용량을 못 읽어 보류")
    elif cur_cap != scap:
        tgt = _live_part(conn, tgt_name)
        if tgt is None or not tgt["price"]:
            warns.append(f"단가표 '{tgt_name}' 없음/단가 0 — 저장장치 보류")
        elif not cur_cap:
            actions.append((tgt, False))
        else:
            rm = (_live_part(conn, cur_txt)
                  or _live_part(conn, f"{stype} {cur_cap}"))
            if rm is None or not rm["price"]:
                warns.append(f"실물 저장장치 '{cur_txt}'에 맞는 단가표 부품이 없어 교체 보류")
            else:
                actions.append((rm, True))
                actions.append((tgt, False))
    return actions, warns


def base_spec_gap(conn, a):
    """자산 하나의 기준사양 부족분 — ({"actions":[(부품행, 회수여부)], "warns":[…]}, "")
    또는 (None, 계산 불가 사유). 추측하지 않는다 — 못 맞추면 보류하고 이유를 남긴다."""
    # ★이미 주문에 매칭됐거나 나간 자산은 손대지 않는다(2026-08-14 검토).
    #   고객 옵션으로 16G를 꽂아 둔 자산에 '기준 8G'를 맞추겠다고 회수하면,
    #   판 물건의 원가가 사후에 바뀌고 실물과 스펙도 어긋난다.
    if a["status"] in ("reserved", "shipped", "returning", "as", "scrapped",
                       "cancelled", "returned"):
        return None, f"{ASSET_STATUSES.get(a['status'], a['status'])} 자산은 기준사양을 " \
                     "채우지 않습니다(주문에 매칭·출고된 뒤에는 원가가 바뀌면 안 됩니다)"
    code = (a["product_code"] or "").strip()
    if not code:
        return None, "제품코드 없음"
    spec = conn.execute("SELECT * FROM code_specs WHERE code=?", (code,)).fetchone()
    if spec is None:
        return None, "코드 스펙 미확인 — 코드칸 🧬 [몰에서 불러오기]로 먼저 채우세요"
    a1, w1 = _ram_axis(conn, a, spec["ram_gen"], spec["ram_gb"] or 0)
    a2, w2 = _storage_axis(conn, a, spec["storage_type"], spec["storage_cap"])
    return {"actions": a1 + a2, "warns": w1 + w2}, ""


def sold_config_actions(conn, asset_row, order_row, kind):
    """'판매 구성' 칩(쿠팡 — 제목이 옵션표)의 작업 목록.

    주문 제목·옵션 텍스트에서 판매 구성을 읽어(parse_code_caps), 그 축(램/저장장치)을
    자산 실물 스펙에 맞춰 회수/장착한다. 세대·방식 판별은 제품코드 스펙이 정본.
    반환 (actions, 경고문) 또는 (None, 보류 사유) — 추측 금지 원칙 그대로.
    """
    from ..malls.collect import parse_code_caps
    code = (order_row["product_code"] or "").strip()
    if not code:
        return None, "주문에 제품코드가 없어 판매 구성 기입을 보류했습니다."
    spec = conn.execute("SELECT * FROM code_specs WHERE code=?", (code,)).fetchone()
    if spec is None:
        return None, "코드 스펙 미확인 — 코드칸 🧬 [몰에서 불러오기]로 먼저 채우세요."
    text = " ".join(x for x in (order_row["product_name"], order_row["option_name"]) if x)
    sold_gb, sold_cap = parse_code_caps(text)
    if kind == "config_ram":
        if not sold_gb:
            return None, "제목에서 판매 램 용량을 못 읽어 보류했습니다."
        actions, warns = _ram_axis(conn, asset_row, spec["ram_gen"], sold_gb)
    else:
        if not sold_cap:
            return None, "제목에서 판매 저장 용량을 못 읽어 보류했습니다."
        actions, warns = _storage_axis(conn, asset_row, spec["storage_type"], sold_cap)
    if warns and not actions:
        return None, " · ".join(warns)
    return actions, (" · ".join(warns) if warns else "")


@bp.get("/purchase-batches/<int:bid>/base-spec-gaps")
def batch_base_spec_gaps(bid):
    """전표 자산 전부의 부족분 미리보기 — 확인창이 이걸 그대로 보여준다."""
    require("purchase.view")
    conn = get_db()
    if conn.execute("SELECT id FROM purchase_batches WHERE id=?", (bid,)).fetchone() is None:
        abort(404, description="전표를 찾을 수 없습니다.")
    rows = conn.execute(
        "SELECT * FROM assets WHERE batch_id=? AND status NOT IN ('cancelled','returned') "
        "ORDER BY asset_no", (bid,)).fetchall()
    out, grand = [], 0
    for a in rows:
        item = {"assetId": a["id"], "assetNo": a["asset_no"],
                "code": a["product_code"] or "", "ram": a["ram"] or "",
                "ssd": a["ssd"] or ""}
        gap, why = base_spec_gap(conn, a)
        if gap is None:
            item["reason"] = why
        else:
            item["actions"] = [
                {"partId": p["id"], "name": p["name"],
                 "price": p["price"] * (-1 if remove else 1), "remove": remove}
                for p, remove in gap["actions"]]
            item["warns"] = gap["warns"]
            item["total"] = sum(x["price"] for x in item["actions"])
            grand += item["total"]
        out.append(item)
    return jsonify({"assets": out, "total": grand})


@bp.post("/assets/base-spec-apply")
def base_spec_apply():
    """확인창에서 체크한 자산들에 부족분을 실제 기입 — ★적용 직전에 다시 계산한다.

    미리보기 후 누군가 자산을 고쳤을 수 있다. 지금 상태 기준으로 다시 계산해 적용하면
    낡은 미리보기가 그대로 들어가는 사고도, 두 번 눌러 중복되는 사고도 없다
    (적용하면 스펙이 기준과 같아져 다음 계산이 0이 된다).
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        abort(400, description="적용할 자산을 선택하세요.")
    if len(ids) > 500:
        abort(400, description="한 번에 500대까지 처리할 수 있습니다.")
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        abort(400, description="자산 선택이 올바르지 않습니다.")
    today = config.now().strftime("%Y-%m-%d")
    results, total = [], 0
    with tx(write=True) as conn:
        for aid in ids:
            row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            if row is None:
                results.append({"assetId": aid, "reason": "자산을 찾을 수 없습니다."})
                continue
            _check_asset_scope(conn, row["category_id"])
            gap, why = base_spec_gap(conn, row)
            if gap is None:
                results.append({"assetId": aid, "assetNo": row["asset_no"], "reason": why})
                continue
            cost = 0
            for part, remove in gap["actions"]:
                cost += _apply_part_to_asset(conn, row, part, today,
                                             g.user["display_name"], remove=remove)
                row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            total += cost
            results.append({"assetId": aid, "assetNo": row["asset_no"],
                            "applied": len(gap["actions"]), "cost": cost,
                            "warns": gap["warns"]})
        audit.log("base_spec_applied", target=f"{len(ids)}대",
                  detail={"totalCost": total,
                          "applied": sum(1 for r in results if r.get("applied"))})
    return jsonify({"results": results, "totalCost": total})


@bp.post("/assets/bulk-repairs")
def bulk_repairs():
    """선택한 자산들에 같은 부품(들)을 한 번에 — 단가는 서버가 단가표에서 읽는다.

    ★금액을 클라이언트가 보내지 않는다. 화면마다 다른 값이 들어오는 것을 막고,
      '오늘 단가'가 정확히 한 곳(parts 표)에서만 나오게 하기 위해서다.
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    part_ids = body.get("partIds") or []
    if not isinstance(ids, list) or not ids:
        abort(400, description="자산을 선택하세요.")
    if not isinstance(part_ids, list) or not part_ids:
        abort(400, description="부품을 선택하세요.")
    if len(ids) > 500:
        abort(400, description="한 번에 500대까지 처리할 수 있습니다.")
    try:
        ids = [int(x) for x in ids]
        # 항목: 숫자(장착, 하위호환) 또는 {id, qty, remove} — remove=회수(음수 원가)
        items = [(int(p["id"]), max(1, int(p.get("qty") or 1)), bool(p.get("remove")))
                 if isinstance(p, dict) else (int(p), 1, False)
                 for p in part_ids]
    except (TypeError, ValueError, KeyError):
        abort(400, description="선택 값이 올바르지 않습니다.")
    repair_date = (body.get("repairDate") or "").strip() or config.now().strftime("%Y-%m-%d")

    ok, failed, total_cost = [], [], 0
    with tx(write=True) as conn:
        parts_rows = [(conn.execute("SELECT * FROM parts WHERE id=? AND enabled=1",
                                    (pid,)).fetchone(), qty, remove)
                      for pid, qty, remove in items]
        if any(p is None for p, _q, _r in parts_rows):
            abort(400, description="선택한 부품 중 없는(또는 꺼진) 것이 있습니다 — 단가표를 확인하세요.")
        zero = [p["name"] for p, _q, _r in parts_rows if not p["price"]]
        if zero:
            abort(400, description=f"단가가 0원인 부품이 있습니다: {', '.join(zero)} — "
                                   "기준정보 ▸ 부품 단가표에서 오늘 단가를 먼저 넣으세요.")
        for aid in ids:
            row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            if row is None:
                failed.append({"id": aid, "reason": "자산을 찾을 수 없습니다."})
                continue
            _check_asset_scope(conn, row["category_id"])
            for p, qty, remove in parts_rows:
                total_cost += _apply_part_to_asset(conn, row, p, repair_date,
                                                   g.user["display_name"],
                                                   qty=qty, remove=remove)
                # 같은 tx 안에서 스펙이 바뀌었을 수 있다 — 다음 부품은 최신 행으로
                row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            ok.append(aid)
        audit.log("repairs_bulk_added",
                  target=f"{len(ok)}대 × 부품 {len(parts_rows)}종",
                  detail={"parts": [f"{'−' if r else ''}{p['name']}" for p, _q, r in parts_rows],
                          "totalCost": total_cost, "failed": len(failed)})
    return jsonify({"ok": len(ok), "failed": failed, "totalCost": total_cost,
                    "perAsset": sum(p["price"] * q * (-1 if r else 1)
                                    for p, q, r in parts_rows)})


# ---------------------------------------------------------------- batches

def _batch_payload(r):
    out = {
        "id": r["id"], "slipNo": r["slip_no"], "stage": r["stage"],
        "stageLabel": STAGES.get(r["stage"], r["stage"]),
        "supplierId": r["supplier_id"], "supplierName": r["supplier_name"],
        # ★별칭 거래처면 전표에 적힌 원문 표기(대표 이름과 다를 때만, 2026-09-03 masters.py) — 화면이 '(원 표기: …)'로 곁들인다
        "supplierOrigName": (r["supplier_orig_name"] if "supplier_orig_name" in r.keys()
                             and r["supplier_orig_name"] != r["supplier_name"] else None),
        "purchaseDate": r["purchase_date"], "totalAmount": r["total_amount"], "memo": r["memo"],
        "purchaseType": r["purchase_type"], "channel": r["channel"],
        "receiveMethod": r["receive_method"], "address": r["address"],
        "vat": r["vat"], "fee": r["fee"], "shippingFee": r["shipping_fee"],
        "shippingCod": bool(r["shipping_cod"]), "trackingNo": r["tracking_no"],
        "paid": bool(r["paid"]), "paidAmount": r["paid_amount"], "paidAt": r["paid_at"],
        "paidMemo": r["paid_memo"],
        "unpaidAmount": max(0, (r["total_amount"] or 0) - (r["paid_amount"] or 0)),
        "confirmedAt": r["confirmed_at"], "confirmedBy": r["confirmed_by"],
        "assetCount": r["asset_count"], "assignedAmount": r["assigned_amount"],
        "rentalCount": (r["rental_count"] if "rental_count" in r.keys() else 0),
        "pendingCount": r["pending_count"],       # 아직 입고확인 안 된 자산 수
        "cancelledAt": r["cancelled_at"], "cancelledBy": r["cancelled_by"],
        "cancelReason": r["cancel_reason"],
        "returnedAt": r["returned_at"], "returnReason": r["return_reason"],
        "createdBy": r["created_by"], "createdAt": r["created_at"],
    }
    # 금액 열람 권한(2026-08-27 대표) — 없으면 전표 금액을 서버에서 가린다
    if not can("purchase.money"):
        for k in ("totalAmount", "assignedAmount", "paidAmount", "unpaidAmount",
                  "vat", "fee", "shippingFee"):
            out[k] = None
        out["moneyMasked"] = True
    return out


_BATCH_SELECT = (
    # ★별칭 거래처(suppliers.alias_of)는 대표 이름으로 보인다(2026-09-03, masters.py) — 원본 표기는 supplier_orig_name
    "SELECT b.*, COALESCE(rep.name, s.name) AS supplier_name, s.name AS supplier_orig_name, "
    " (SELECT COUNT(*) FROM assets a WHERE a.batch_id = b.id) AS asset_count, "
    " (SELECT COALESCE(SUM(a.purchase_price),0) FROM assets a WHERE a.batch_id = b.id) AS assigned_amount, "
    " (SELECT COUNT(*) FROM assets a WHERE a.batch_id = b.id AND a.received = 0) AS pending_count, "
    # ★렌탈로 나간 대수 — 매입은 사업부와 무관하게 전량을 세지만(공통 앞단),
    #   "이 전표는 렌탈로 갔다"가 안 보이면 담당자가 자산 목록에서 찾다가 놓친다
    #   (자산 목록 기본 필터가 판매 사업부라서다). 2026-08-24 대표 지적 P260805-001.
    " (SELECT COUNT(*) FROM assets a WHERE a.batch_id = b.id AND a.division = 'rental') AS rental_count "
    "FROM purchase_batches b LEFT JOIN suppliers s ON s.id = b.supplier_id "
    "LEFT JOIN suppliers rep ON rep.id = s.alias_of WHERE 1=1"
)


@bp.get("/purchase-batches")
def list_batches():
    require("purchase.view")
    sql = _BATCH_SELECT
    params = []
    if request.args.get("stage"):
        sql += " AND b.stage = ?"
        params.append(request.args["stage"])
    if request.args.get("supplierId"):
        sql += " AND b.supplier_id = ?"
        params.append(_int_or_400(request.args["supplierId"], "거래처 ID"))
    if request.args.get("unpaid") == "1":
        sql += " AND b.paid = 0 AND b.stage = 'purchased'"
    # 취소 전표는 기본으로 감춘다 — 목록이 취소분으로 지저분해지면 진행 중인 일을 놓친다.
    # TMS도 '취소포함' 체크를 꺼 둔 상태가 기본이다.
    if request.args.get("includeCancelled") != "1":
        sql += " AND b.cancelled_at = ''"
    q = (request.args.get("q") or "").strip()
    if q:
        sql += " AND (b.slip_no LIKE ? OR s.name LIKE ? OR rep.name LIKE ? OR b.memo LIKE ? OR b.tracking_no LIKE ?)"
        params.extend(["%" + q + "%"] * 5)
    sql += " ORDER BY b.purchase_date DESC, b.id DESC LIMIT 300"
    rows = get_db().execute(sql, params).fetchall()
    return jsonify([_batch_payload(r) for r in rows])


@bp.get("/purchase-batches/<int:bid>")
def batch_detail(bid):
    require("purchase.view")
    conn = get_db()
    row = conn.execute(_BATCH_SELECT + " AND b.id = ?", (bid,)).fetchone()
    if row is None:
        abort(404, description="전표를 찾을 수 없습니다.")
    clause, params = scope_clause("a")
    # ★렌탈 자산은 OWS 상태(shipped=출고완료 / ready=판매가능)로는 실상을 알 수 없다.
    #   shipped 는 TMS 이관 흔적이고 ready 는 아예 틀린 말이다(대표 2026-09-01).
    #   RMS가 밀어 넣어 둔 사본에서 진짜 상태와 임차인을 함께 가져온다.
    assets = conn.execute(
        "SELECT a.*, c.name AS category_name, "
        " ri.status AS rms_status, ri.renter AS rms_renter, "
        " (SELECT COALESCE(SUM(r.cost),0) FROM asset_repairs r WHERE r.asset_id=a.id) AS repair_cost "
        "FROM assets a "
        "LEFT JOIN rms_inventory ri ON ri.asset_no = a.asset_no COLLATE NOCASE "
        "LEFT JOIN categories c ON c.id = a.category_id WHERE a.batch_id = ?" + clause +
        " ORDER BY a.asset_no", [bid] + params
    ).fetchall()
    out = _batch_payload(row)
    # ★매입취소된 자산은 본 표에서 빼고 따로 준다(대표 2026-08-24 자산 단위 취소).
    #   섞어 두면 '이 전표에 몇 대'가 실제 재고와 어긋나 보인다. 되돌릴 수 있어야 하므로
    #   숨기지는 않고, 사유와 함께 접힌 목록으로 내려보낸다.
    def _cancel_reason(aid):
        r = conn.execute(
            "SELECT detail FROM asset_events WHERE asset_id=? AND action='매입취소' "
            "ORDER BY id DESC LIMIT 1", (aid,)).fetchone()
        if not r or not r["detail"]:
            return ""
        try:
            return (json.loads(r["detail"]) or {}).get("사유", "")
        except (ValueError, TypeError):
            return ""

    live = [a for a in assets if a["status"] != "cancelled"]
    dead = [a for a in assets if a["status"] == "cancelled"]
    # 판매 요약(2026-08-27 대표 "실제로 판매됐는데 어디로 판매되었는지가 안 보여") —
    #   TMS 판매 원장을 자산번호로 붙여 전표 상세에서 바로 보이게 한다.
    sales_by_no = {}
    nos = [a["asset_no"] for a in live]
    if nos:
        marks = ",".join("?" * len(nos))
        for t in conn.execute(
                f"SELECT asset_no, channel, customer, sale_date, slip_no FROM tms_sales "
                f"WHERE asset_no IN ({marks}) AND stage NOT LIKE '%취소%' "
                f"ORDER BY sale_date", nos).fetchall():
            sales_by_no[t["asset_no"]] = {          # 같은 번호가 여러 번이면 최신이 남는다
                "channel": t["channel"], "customer": t["customer"],
                "date": (t["sale_date"] or "")[:10], "slipNo": t["slip_no"]}
    out["assets"] = [_asset_payload(a, {"categoryName": a["category_name"],
                                        "repairCost": a["repair_cost"] or 0,
                                        "sale": sales_by_no.get(a["asset_no"])}) for a in live]
    out["cancelledAssets"] = [
        _asset_payload(a, {"categoryName": a["category_name"],
                           "repairCost": a["repair_cost"] or 0,
                           "cancelReason": _cancel_reason(a["id"])}) for a in dead]
    # ★총원가 = 매입가 합 + 부품·수리비 합. 전표 금액(total_amount)과는 별개다 —
    #   전표 금액은 '거래처에 준 돈'이라 부품비가 섞이면 대조(amountGap)가 깨진다.
    #   그래서 표시용 합계로만 준다(대표 2026-08-10: 부품값이 전표에도 보여야 한다).
    out["repairTotal"] = (sum(a["repair_cost"] or 0 for a in live)
                          if can("purchase.money") else None)
    if not can("purchase.money"):
        for a_ in out["assets"]:
            a_["repairCost"] = None
    out["costTotal"] = ((out.get("assignedAmount") or 0) + out["repairTotal"]
                        if out["repairTotal"] is not None else None)   # 금액 가림이면 총원가도 가림
    return jsonify(out)


def _batch_fields_from_body(body, conn, stage=None):
    """전표 공통 필드 파싱. (컬럼, 값) 목록과 감사용 dict를 돌려준다.

    stage를 넘기면 그 단계에 맞는 규칙을 적용한다(매입 전표는 거래처를 비울 수 없다).
    """
    sets, changes = [], {}

    def put(col, val, label=None):
        sets.append((col, val))
        changes[label or col] = val

    # ★거래처를 '이름'으로도 받는다(2026-08-24 대표: "cpu, 램처럼 입력하고 자동완성").
    #   화면이 자동완성 입력이라 이름이 오고, 없는 이름은 여기서 새로 만든다 —
    #   TMS 이관이 거래처를 이름으로 만들던 것과 같은 규칙이다.
    #   대소문자·공백만 다른 이름은 같은 곳으로 본다(두 곳으로 갈리면 집계를 못 믿는다).
    if "supplierName" in body and "supplierId" not in body:
        name = (body.get("supplierName") or "").strip()
        if not name:
            body = dict(body)
            body["supplierId"] = ""            # 아래 빈값 규칙(가입고 허용·매입 필수)을 그대로 탄다
        else:
            # ★별칭(alias_of)·부 신원(supplier_tms_links)도 알아본다(2026-09-03) — 대표 행에 붙는다. 자동 병합은 없다.
            hit = masters.resolve_supplier_name(conn, name)
            if hit is None:
                cur = conn.execute(
                    "INSERT INTO suppliers(name, contact, phone, memo, created_at) "
                    "VALUES(?,?,?,?,?)", (name, "", "", "", config.now_iso()))
                hit = cur.lastrowid
                changes["거래처 신규"] = name
            body = dict(body)
            body["supplierId"] = hit
    if "supplierId" in body:
        sid = body.get("supplierId")
        if sid in (None, ""):
            # ★매입 확정된 전표에서 거래처를 비우면 매입가·매입일의 근거가 사라진다.
            #   생성·확정 때는 거래처를 필수로 받으면서 수정만 열어 두면 규칙이 어긋난다.
            if stage == "purchased":
                abort(400, description="매입 전표의 거래처는 비울 수 없습니다.")
            put("supplier_id", None, "거래처")
        else:
            sid = _int_or_400(sid, "거래처 ID")
            if conn.execute("SELECT id FROM suppliers WHERE id=?", (sid,)).fetchone() is None:
                abort(400, description="존재하지 않는 거래처입니다.")
            put("supplier_id", sid, "거래처")
    if "purchaseDate" in body:
        v = (body.get("purchaseDate") or "").strip()
        if not v:
            abort(400, description="일자를 입력하세요.")
        put("purchase_date", v, "일자")
    # 신청자(requester)는 받지 않는다 — 등록한 사람이 곧 신청자이므로 created_by로 기록된다.
    for key, col, label in (
        ("memo", "memo", "메모"), ("purchaseType", "purchase_type", "매입구분"),
        ("channel", "channel", "매입방법"), ("receiveMethod", "receive_method", "수령방식"),
        ("address", "address", "주소"), ("trackingNo", "tracking_no", "송장번호"),
    ):
        if key in body:
            put(col, (body.get(key) or "").strip(), label)
    for key, col, label in (
        ("totalAmount", "total_amount", "매입금액"), ("vat", "vat", "부가세"),
        ("fee", "fee", "수수료"), ("shippingFee", "shipping_fee", "배송비"),
    ):
        if key in body:
            put(col, _money_or_400(body[key] or 0, label), label)
    if "shippingCod" in body:
        put("shipping_cod", 1 if body["shippingCod"] else 0, "착불")
    if "paid" in body:
        put("paid", 1 if body["paid"] else 0, "납부확인")
    if "paidAmount" in body:
        put("paid_amount", _money_or_400(body["paidAmount"] or 0, "지급액"), "지급액")
    if "paidAt" in body:
        put("paid_at", (body.get("paidAt") or "").strip(), "지급일")
    if "paidMemo" in body:
        put("paid_memo", (body.get("paidMemo") or "").strip(), "지급메모")
    return sets, changes


@bp.post("/purchase-batches")
def create_batch():
    """전표 생성. stage='provisional'이면 가입고, 아니면 매입."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    stage = (body.get("stage") or "purchased").strip()
    if stage not in STAGES:
        abort(400, description="알 수 없는 단계입니다.")
    purchase_date = (body.get("purchaseDate") or "").strip()
    if not purchase_date:
        abort(400, description="일자를 입력하세요.")
    # ★줄 전체의 손입력 관리번호를 연동 창구에 한 번에 묻는다 — tx 밖(2026-09-03, A4)
    numbering.precheck_bodies(body.get("assets") or [])
    with tx(write=True) as conn:
        sets, changes = _batch_fields_from_body(body, conn)
        if stage == "purchased" and not any(c == "supplier_id" and v for c, v in sets):
            abort(400, description="매입 전표에는 거래처가 필요합니다.")
        ts = config.now_iso()
        slip_no = next_slip_no(conn, stage)
        cols = ["slip_no", "stage", "purchase_date", "created_by", "created_at", "updated_at"]
        vals = [slip_no, stage, purchase_date, g.user["display_name"], ts, ts]
        for col, val in sets:
            if col != "purchase_date":
                cols.append(col)
                vals.append(val)
        conn.execute(
            f"INSERT INTO purchase_batches({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
        bid = conn.execute("SELECT id FROM purchase_batches WHERE slip_no=?", (slip_no,)).fetchone()["id"]

        # ★전표와 자산을 한 번에 저장한다(TMS의 [상세추가] → 저장과 같은 흐름).
        #   전표당 평균 32대, 최대 366대라 따로 넣으면 중간에 끊겼을 때 반쪽 전표가 남는다.
        #   한 트랜잭션이므로 자산 한 줄이라도 잘못되면 전표까지 통째로 취소된다.
        items = body.get("assets")
        created = []
        if items:
            if not isinstance(items, list):
                abort(400, description="assets는 목록이어야 합니다.")
            if len(items) > 200:
                abort(400, description="한 번에 등록할 수 있는 줄은 200개까지입니다.")
            received = 0 if stage == "provisional" else 1
            for n, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    abort(400, description=f"{n}번째 줄이 올바르지 않습니다.")
                created.extend(_add_assets(conn, item, bid, received, label=f"{n}번째 줄: "))

        audit.log("batch_created", target=slip_no,
                  detail={"stage": stage, "assets": len(created), **changes})
    return jsonify({"id": bid, "slipNo": slip_no, "stage": stage,
                    "assets": created, "assetCount": len(created)}), 201


@bp.patch("/purchase-batches/<int:bid>")
def update_batch(bid):
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM purchase_batches WHERE id=?", (bid,)).fetchone()
        if row is None:
            abort(404, description="전표를 찾을 수 없습니다.")
        sets, changes = _batch_fields_from_body(body, conn, stage=row["stage"])
        if not sets:
            abort(400, description="변경할 항목이 없습니다.")
        for col, val in sets:
            conn.execute(f"UPDATE purchase_batches SET {col}=? WHERE id=?", (val, bid))
        conn.execute("UPDATE purchase_batches SET updated_at=? WHERE id=?", (config.now_iso(), bid))
        audit.log("batch_updated", target=row["slip_no"] or str(bid), detail=changes)
    return jsonify({"ok": True})


# 취소해도 되는 자산 상태 — 아직 우리 손 안에 있는 것만.
# 주문에 매칭됐거나 출고·회수 중인 물건이 걸려 있으면 전표를 무를 수 없다.
# ★'returned'(거래처반품)도 포함한다. 빠져 있으면 반품 처리한 전표를 취소하려 할 때
#   "주문에 나갔거나 회수 중인 자산이 있어"라는 엉뚱한 문구가 떠서
#   담당자가 주문 화면에서 매칭을 풀려고 헛수고를 한다(실제 원인은 자기가 한 반품).
_CANCELABLE_ASSET = ("in_stock", "refurbishing", "ready", "repair", "as", "painting",
                     "defective", "returned")


def _batch_or_404(conn, bid):
    row = conn.execute("SELECT * FROM purchase_batches WHERE id=?", (bid,)).fetchone()
    if row is None:
        abort(404, description="전표를 찾을 수 없습니다.")
    return row


def _blocking_assets(conn, bid):
    """전표를 무를 수 없게 만드는 자산들 — 왜 안 되는지 사람 말로 돌려준다."""
    rows = conn.execute(
        "SELECT asset_no, status FROM assets WHERE batch_id=? AND status NOT IN "
        f"({','.join('?' * len(_CANCELABLE_ASSET))})",
        [bid] + list(_CANCELABLE_ASSET)).fetchall()
    return [f"{r['asset_no']}({ASSET_STATUSES.get(r['status'], r['status'])})" for r in rows]


@bp.post("/purchase-batches/<int:bid>/cancel")
def cancel_batch(bid):
    """전표 취소 — 지우지 않고 '취소'로 표시한다.

    ★삭제가 아니라 취소인 이유: 전표번호는 이미 소비됐고, 없던 일로 만들면
      번호가 왜 비었는지 나중에 아무도 설명할 수 없다. TMS도 '매입취소'로 남긴다
      (465건 중 6건, 결번은 그대로 둔다).
    딸린 자산은 함께 '매입취소' 상태가 되어 재고에서 빠진다.
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "").strip()
    with tx(write=True) as conn:
        row = _batch_or_404(conn, bid)
        if row["cancelled_at"]:
            abort(400, description="이미 취소된 전표입니다.")
        blocking = _blocking_assets(conn, bid)
        if blocking:
            abort(400, description=(
                "주문에 나갔거나 회수 중인 자산이 있어 취소할 수 없습니다: "
                + ", ".join(blocking[:5])
                + (f" 외 {len(blocking) - 5}대" if len(blocking) > 5 else "")))
        ts = config.now_iso()
        actor = g.user["display_name"]
        assets = conn.execute("SELECT id, asset_no, status FROM assets WHERE batch_id=?",
                              (bid,)).fetchall()
        for a in assets:
            # ★취소 직전 상태를 이력에 남긴다 — 되돌릴 때 그대로 복원하기 위해서다.
            #   이게 없으면 판매가능·불량·도색대기가 전부 '입고'로 뭉개져
            #   셋팅을 다 끝낸 자산을 손으로 다시 올려야 한다.
            conn.execute("UPDATE assets SET status='cancelled', updated_at=? WHERE id=?",
                         (ts, a["id"]))
            asset_event(conn, a["id"], "매입취소",
                        {"전표": row["slip_no"], "사유": reason, "이전상태": a["status"]})
        # ★부품매입 전표(2026-08-25) — 딸린 재고 입고를 상쇄 이동으로 되돌린다.
        #   원장 행을 지우지 않고 음수 보정을 넣는다(왜 줄었는지 내역에 남아야 한다).
        #   ★취소↔해제 반복 안전: '이 전표의 이동 순합을 0으로'가 목표라, 지금 순합만큼만 뺀다.
        part_moves = conn.execute(
            "SELECT part_id, COALESCE(SUM(qty),0) AS net FROM part_stock_moves "
            "WHERE batch_id=? GROUP BY part_id HAVING net != 0", (bid,)).fetchall()
        for m in part_moves:
            conn.execute(
                "INSERT INTO part_stock_moves(part_id, qty, batch_id, reason, move_date, "
                "memo, created_by, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (m["part_id"], -m["net"], bid, "adjust", ts[:10],
                 f"전표 취소({row['slip_no']})", actor, ts))
        conn.execute(
            "UPDATE purchase_batches SET cancelled_at=?, cancelled_by=?, cancel_reason=?, "
            "updated_at=? WHERE id=?", (ts, actor, reason, ts, bid))
        audit.log("batch_cancelled", target=row["slip_no"],
                  detail={"assets": len(assets), "reason": reason,
                          **({"부품이동상쇄": len(part_moves)} if part_moves else {})})
    return jsonify({"ok": True, "cancelledAssets": len(assets)})


@bp.post("/purchase-batches/<int:bid>/uncancel")
def uncancel_batch(bid):
    """취소 되돌리기 — 잘못 취소한 경우. 자산도 함께 입고 상태로 돌린다."""
    require("purchase.edit")
    with tx(write=True) as conn:
        row = _batch_or_404(conn, bid)
        if not row["cancelled_at"]:
            abort(400, description="취소된 전표가 아닙니다.")
        ts = config.now_iso()
        n = 0
        for a in conn.execute(
                "SELECT id FROM assets WHERE batch_id=? AND status='cancelled'", (bid,)).fetchall():
            # 취소할 때 적어 둔 '이전상태'로 되돌린다. 못 찾으면 입고로.
            prev = conn.execute(
                "SELECT detail FROM asset_events WHERE asset_id=? AND action='매입취소' "
                "ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()
            back = "in_stock"
            if prev and prev["detail"]:
                try:
                    back = json.loads(prev["detail"]).get("이전상태") or "in_stock"
                except (ValueError, TypeError):
                    pass
            if back not in ASSET_STATUSES or back == "cancelled":
                back = "in_stock"
            conn.execute("UPDATE assets SET status=?, updated_at=? WHERE id=?",
                         (back, ts, a["id"]))
            asset_event(conn, a["id"], "매입취소 해제",
                        {"전표": row["slip_no"], "복원상태": back})
            n += 1
        # 부품매입 재입고 — '이 전표의 이동 순합을 매입 합계로 되돌린다'(반복 안전).
        undo = conn.execute(
            "SELECT part_id, COALESCE(SUM(CASE WHEN reason='purchase' THEN qty END),0) "
            "       - COALESCE(SUM(qty),0) AS lack "
            "FROM part_stock_moves WHERE batch_id=? GROUP BY part_id HAVING lack != 0",
            (bid,)).fetchall()
        for m in undo:
            conn.execute(
                "INSERT INTO part_stock_moves(part_id, qty, batch_id, reason, move_date, "
                "memo, created_by, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (m["part_id"], m["lack"], bid, "adjust", ts[:10],
                 f"취소 해제 재입고({row['slip_no']})", g.user["display_name"], ts))
        conn.execute(
            "UPDATE purchase_batches SET cancelled_at='', cancelled_by='', cancel_reason='', "
            "updated_at=? WHERE id=?", (ts, bid))
        audit.log("batch_uncancelled", target=row["slip_no"], detail={"assets": n})
    return jsonify({"ok": True, "restoredAssets": n})


@bp.post("/purchase-batches/<int:bid>/return")
def return_batch(bid):
    """매입 반품 — 거래처로 되돌려 보냈다는 기록.

    TMS에도 '반품일' 칸이 있지만 실사용은 0건이었다(14,966건 전수 확인).
    그래도 기록할 곳은 있어야 하므로 전표 단위로 남기고, 자산은 재고에서 뺀다.
    일부만 반품하는 경우는 자산 상태를 개별로 '거래처반품'으로 바꾸면 된다.
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "").strip()
    date = (body.get("returnedAt") or "").strip() or config.now().strftime("%Y-%m-%d")
    only = body.get("assetIds")            # 일부만 반품할 때
    with tx(write=True) as conn:
        row = _batch_or_404(conn, bid)
        if row["cancelled_at"]:
            abort(400, description="취소된 전표는 반품 처리할 수 없습니다.")
        if only:
            ids = [_int_or_400(x, "자산 ID") for x in only]
            ph = ",".join("?" * len(ids))
            targets = conn.execute(
                f"SELECT id, asset_no, status FROM assets WHERE batch_id=? AND id IN ({ph})",
                [bid] + ids).fetchall()
        else:
            targets = conn.execute(
                "SELECT id, asset_no, status FROM assets WHERE batch_id=?", (bid,)).fetchall()
        bad = [f"{t['asset_no']}({ASSET_STATUSES.get(t['status'], t['status'])})"
               for t in targets if t["status"] not in _CANCELABLE_ASSET]
        if bad:
            abort(400, description="주문에 나갔거나 회수 중인 자산은 반품 처리할 수 없습니다: "
                                   + ", ".join(bad[:5]))
        ts = config.now_iso()
        for t in targets:
            conn.execute("UPDATE assets SET status='returned', updated_at=? WHERE id=?",
                         (ts, t["id"]))
            asset_event(conn, t["id"], "거래처반품",
                        {"전표": row["slip_no"], "반품일": date, "사유": reason})
        # 전체 반품일 때만 전표에 반품일을 남긴다(일부 반품은 자산 이력에만)
        if not only:
            conn.execute(
                "UPDATE purchase_batches SET returned_at=?, return_reason=?, updated_at=? WHERE id=?",
                (date, reason, ts, bid))
        audit.log("batch_returned", target=row["slip_no"],
                  detail={"assets": len(targets), "partial": bool(only), "reason": reason})
    return jsonify({"ok": True, "returnedAssets": len(targets)})


@bp.post("/purchase-batches/<int:bid>/confirm")
def confirm_batch(bid):
    """가입고 → 매입 전환(입고확인). 매입 전표번호를 새로 부여한다."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM purchase_batches WHERE id=?", (bid,)).fetchone()
        if row is None:
            abort(404, description="전표를 찾을 수 없습니다.")
        if row["stage"] != "provisional":
            abort(400, description="가입고 전표만 매입으로 확정할 수 있습니다.")
        # ★취소된 전표를 확정하면 매입번호만 하나 소비하고 전표는 취소인 채로 목록에서 사라진다
        #   (P번호 결번이 생기고 왜 비었는지 설명할 수 없다). 반품 쪽엔 이미 같은 가드가 있다.
        if row["cancelled_at"]:
            abort(400, description="취소된 전표는 매입 확정할 수 없습니다. 먼저 취소를 되돌리세요.")
        sets, changes = _batch_fields_from_body(body, conn)
        for col, val in sets:
            conn.execute(f"UPDATE purchase_batches SET {col}=? WHERE id=?", (val, bid))
        cur = conn.execute("SELECT supplier_id FROM purchase_batches WHERE id=?", (bid,)).fetchone()
        if not cur["supplier_id"]:
            abort(400, description="매입 확정에는 거래처가 필요합니다.")
        ts = config.now_iso()
        slip_no = next_slip_no(conn, "purchased")
        conn.execute(
            "UPDATE purchase_batches SET stage='purchased', slip_no=?, confirmed_at=?, confirmed_by=?, "
            "updated_at=? WHERE id=?",
            (slip_no, ts, g.user["display_name"], ts, bid))
        # 가입고 상태로 묶여 있던 자산을 '받은 것'으로 바꾸고 이력에 남긴다.
        # ★이 UPDATE가 없으면 received가 영영 0으로 남아 재고에 안 잡힌다.
        conn.execute(
            "UPDATE assets SET received=1, received_at=?, received_by=?, updated_at=? "
            "WHERE batch_id=? AND received=0",
            (ts, g.user["display_name"], ts, bid))
        for a in conn.execute("SELECT id FROM assets WHERE batch_id=?", (bid,)).fetchall():
            asset_event(conn, a["id"], "입고확인", {"slipNo": slip_no, "from": row["slip_no"]})
        audit.log("batch_confirmed", target=slip_no,
                  detail={"from": row["slip_no"], **changes})
    return jsonify({"ok": True, "slipNo": slip_no})


# ---------------------------------------------------------------- assets

def _assets_filter_sql():
    """자산 목록 조건을 SQL로 — 엑셀 내보내기가 '보고 있는 그대로' 받게 공유한다.

    예전에는 내보내기가 상태만 반영해서, 검색·등급·분류로 걸러 놓고 받아도
    전체가 통째로 나왔다(2026-07-29 전수조사 확인).
    """
    clause, params = scope_clause("a")
    # ★전표번호를 함께 준다 — 목록에서 '이 자산이 어느 전표로 들어왔는지'가 바로 보여야
    #   전표를 찾으러 다른 화면을 뒤지지 않는다(대표 요청 2026-08-04).
    #   LEFT JOIN이어야 한다. 전표에 안 묶인 이관·수기 자산이 통째로 사라지면 안 된다.
    # ★취소·반품 여부도 함께 뽑는다. 안 그러면 취소한 전표가 목록에서 살아 있는 매입으로
    #   보이고, 대표가 그 전표 기준으로 매입가·거래처·미지급을 판단한다(2026-08-04 감사).
    # ★이관 보류 사유를 목록에서 바로 보여준다 — 막히기만 하고 안 보이면 정리가 안 된다.
    _hold = (
        "TRIM(',' || "
        " CASE WHEN LENGTH(REPLACE(REPLACE(UPPER(a.serial),' ',''),'-','')) >= 5"
        "       AND EXISTS (SELECT 1 FROM assets d WHERE d.id <> a.id"
        "                   AND REPLACE(REPLACE(UPPER(d.serial),' ',''),'-','')"
        "                       = REPLACE(REPLACE(UPPER(a.serial),' ',''),'-',''))"
        "      THEN 'dup_buy' ELSE '' END"
        " || CASE WHEN a.division = 'rental' AND ("
        "        EXISTS (SELECT 1 FROM asset_events e WHERE e.asset_id = a.id AND e.action = '판매')"
        "        OR EXISTS (SELECT 1 FROM order_assets oa JOIN orders o ON o.id = oa.order_id"
        "                   WHERE oa.asset_id = a.id AND o.cancelled_at = ''))"
        "      THEN ',sold_rec' ELSE '' END"
        ", ',') AS hold_kinds"
    )
    # RMS 실물 유무 — 렌탈 귀속만 판정한다. RMS 사본이 비었으면 NULL(판정 안 함).
    _in_rms = (
        "CASE WHEN a.division <> 'rental'"
        "       OR NOT EXISTS (SELECT 1 FROM rms_inventory LIMIT 1) THEN NULL"
        "     WHEN EXISTS (SELECT 1 FROM rms_inventory r"
        "                  WHERE r.asset_no = a.asset_no COLLATE NOCASE) THEN 1"
        "     ELSE 0 END AS in_rms"
    )
    sql = ("SELECT a.*, " + _hold + ", " + _in_rms
           + ", ri.status AS rms_status, ri.renter AS rms_renter"
           + ", c.name AS category_name, b.slip_no AS slip_no, "
           "       b.cancelled_at AS slip_cancelled, b.returned_at AS slip_returned "
           "FROM assets a "
           # ★렌탈 자산의 '판매가능'은 뜻이 없는 말이다(대표 2026-09-01) — 목록에서도
           #   RMS가 보는 실제 상태를 보여줘야 한다. 전표 상세와 같은 사본을 쓴다.
           "LEFT JOIN rms_inventory ri ON ri.asset_no = a.asset_no COLLATE NOCASE "
           "LEFT JOIN categories c ON c.id = a.category_id "
           "LEFT JOIN purchase_batches b ON b.id = a.batch_id WHERE 1=1" + clause)
    if request.args.get("status"):
        sql += " AND a.status = ?"
        params.append(request.args["status"])
    if request.args.get("categoryId"):
        sql += " AND a.category_id = ?"
        params.append(_int_or_400(request.args["categoryId"], "카테고리 ID"))
    if request.args.get("batchId"):
        sql += " AND a.batch_id = ?"
        params.append(_int_or_400(request.args["batchId"], "입고 ID"))
    if request.args.get("grade"):
        sql += " AND a.grade = ?"
        params.append(request.args["grade"])
    # ★사업부는 '강제 제외'가 아니라 '고르는 필터'다 — 자산 목록에서는 렌탈 귀속분도
    #   찾아볼 수 있어야 한다. 판매재고를 세는 쪽에서만 sale_only로 강제 제외한다.
    # ★'문제 있는 자산' 필터(issue)는 렌탈 귀속에서 주로 걸린다. 사업부 기본값(판매)이
    #   같이 걸리면 늘 0건이 나온다 — 막혀 있는데 목록이 비어 "문제 없다"로 읽힌다
    #   (2026-08-24 검증에서 실제로 잡음: issue=no-rms 가 228대인데 0건으로 나왔다).
    #   issue를 지정했는데 사업부를 안 골랐으면 전체에서 찾는다.
    _issue = (request.args.get("issue") or "").strip()
    div = (request.args.get("division")
           or ("all" if _issue else DIVISION_SALE)).strip()
    if div != "all":
        if div not in DIVISIONS:
            abort(400, description="division은 sale / rental / all 중 하나여야 합니다.")
        sql += " AND a.division = ?"
        params.append(div)
    # ★정리 대기 — 이관이 막히는 자산을 한곳에 모아 본다(대표 2026-08-14).
    #   막기만 하고 목록이 없으면 그 자산들은 화면에서 사라져 영영 정리가 안 된다.
    if request.args.get("issue") == "hold":
        # ★사람이 푼 건은 '정리 대기'가 아니다 — 풀었는데도 목록에 남으면 끝이 안 난다.
        #   대신 목록 어디서든 배지로 '해제됨'이 보인다(holdOverride).
        sql += " AND TRIM(COALESCE(a.hold_override,'')) = ''"
        sql += (
            " AND ("
            "  TRIM(COALESCE(a.tms_deleted_at,'')) <> ''"           # TMS에서 삭제된 자산도 정리 대기에 모은다
            "  OR EXISTS (SELECT 1 FROM assets d WHERE d.id <> a.id"
            "          AND LENGTH(REPLACE(REPLACE(UPPER(a.serial),' ',''),'-','')) >= 5"
            "          AND REPLACE(REPLACE(UPPER(d.serial),' ',''),'-','')"
            "              = REPLACE(REPLACE(UPPER(a.serial),' ',''),'-',''))"
            "  OR (a.division = 'rental' AND ("
            "        EXISTS (SELECT 1 FROM asset_events e WHERE e.asset_id = a.id AND e.action = '판매')"
            "        OR EXISTS (SELECT 1 FROM order_assets oa JOIN orders o ON o.id = oa.order_id"
            "                   WHERE oa.asset_id = a.id AND o.cancelled_at = '')))"
            ")")
    # ★렌탈로 정해졌는데 RMS에 실물 기록이 없는 자산(대표 2026-08-24, P260805-001).
    #   매입 전표에는 남고 OWS에서도 렌탈로 보이지만, RMS에 없으면 렌탈팀이 운용할 수 없다.
    #   판정 근거는 RMS가 밀어 넣어 둔 사본(rms_inventory)이다 — 사본이 비어 있으면
    #   아무것도 걸지 않는다('한 번도 못 받았다'를 '없다'로 읽으면 안 된다).
    if request.args.get("issue") == "no-rms":
        sql += (
            " AND a.division = 'rental'"
            " AND EXISTS (SELECT 1 FROM rms_inventory LIMIT 1)"
            " AND NOT EXISTS (SELECT 1 FROM rms_inventory r"
            "                 WHERE r.asset_no = a.asset_no COLLATE NOCASE)")
    # ★재고집계 숫자를 눌러 들어오는 묶음 필터(2026-08-12 대표: "팔 수 있는 재고·작업중·
    #   보류 전부 눌러서 확인할 수 있게"). 기준은 집계(stock-by-model)와 완전히 같아야
    #   한다(STOCK_BUCKETS + 실물 받은 것만) — 어긋나면 '판매가능 12'를 눌렀는데
    #   목록이 11대가 되는 식으로 숫자를 못 믿게 된다.
    bucket = (request.args.get("bucket") or "").strip()
    if bucket:
        codes = ([c for cs in STOCK_BUCKETS.values() for c in cs]
                 if bucket == "stock" else list(STOCK_BUCKETS.get(bucket) or ()))
        if not codes:
            abort(400, description="알 수 없는 재고 묶음입니다(ready/working/held/stock).")
        sql += f" AND a.status IN ({','.join('?' * len(codes))})" + RECEIVED_SQL
        params.extend(codes)
    # 카테고리×상태 표에서 넘어올 때 — 집계는 실물 받은 것만 세므로 같은 기준을 걸어 준다
    if request.args.get("received") in ("0", "1"):
        sql += " AND a.received = ?"
        params.append(int(request.args["received"]))
    # 제품별 재고 줄에서 넘어올 때 — 모델명 LIKE(q)로는 '그램'이 다른 제조사 것까지 섞인다.
    #   빈 문자열도 조건이다(메이커 없는 모델, '(모델 미입력)') — 파라미터 존재로 가른다.
    if "maker" in request.args:
        sql += " AND TRIM(COALESCE(a.maker,'')) = ?"
        params.append(request.args["maker"].strip())
    if "model" in request.args:
        sql += " AND TRIM(COALESCE(a.model,'')) = ?"
        params.append(request.args["model"].strip())
    q = (request.args.get("q") or "").strip()
    if q:
        sql += (" AND (a.asset_no LIKE ? OR a.model LIKE ? OR a.serial LIKE ? OR a.maker LIKE ? "
                "OR a.notes LIKE ? OR a.cpu LIKE ? OR a.location LIKE ?)")
        params.extend(["%" + q + "%"] * 7)
    return sql, params


@bp.get("/assets")
def list_assets():
    """자산 목록 — 500대씩, 이어 받을 수 있다.

    ★예전엔 최신 500대에서 잘렸다(offset 없음). 판매완료가 13,129대라
      대표가 "판매완료인 제품은 이력이 아예 안 보이는 게 맞아?"라고 물은
      실체가 이 상한이었다(2026-08-07). 이력은 늘 있었다 — 도달을 못 했을 뿐.
    ★offset은 여기서만 붙인다. _assets_filter_sql 은 엑셀 내보내기와 공유라
      거기에 넣으면 내보내기가 앞 500대만 나가게 된다.
    """
    require("purchase.view")
    sql, params = _assets_filter_sql()
    conn = get_db()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM (" + sql + ")", params).fetchone()["n"]
    offset = _int_or_400(request.args.get("offset") or 0, "offset")
    if offset < 0:
        offset = 0
    sql += " ORDER BY a.id DESC LIMIT 500 OFFSET ?"
    rows = conn.execute(sql, params + [offset]).fetchall()
    return jsonify({
        "total": total, "offset": offset, "limit": 500,
        "rows": [_asset_payload(r, {
            "categoryName": r["category_name"], "slipNo": r["slip_no"] or "",
            "slipCancelled": bool(r["slip_cancelled"]),
            "slipReturned": bool(r["slip_returned"])}) for r in rows],
    })


# 제품코드를 넣어 봐야 소용없는 상태 — 다시 팔 물건이 아니다.
#   출고완료(이미 나감) · 폐기 · 매입취소 · 거래처반품
# 나머지(입고·정비중·수리·A/S·도색대기·불량·판매가능·주문매칭·회수중)는 언젠가 재고가 되므로
# 코드를 넣어 두는 게 맞다. 특히 수리·A/S는 고쳐서 파는 물건이라 빠지면 안 된다.
CODE_TARGET_EXCLUDE = ("shipped", "scrapped", "cancelled", "returned")


@bp.post("/purchase-batches/<int:bid>/sync-amount")
def sync_batch_amount(bid):
    """전표 매입금액을 그 전표에 달린 자산 매입가 합으로 맞춘다.

    ★자산을 담아 등록하면 화면이 합계를 자동으로 넣어 주지만, 나중에 자산 매입가를
      고치면 전표 총액이 그대로 남아 '금액 불일치' 경고가 뜬다(2026-08-04 대표).
      그때 이 버튼 하나로 맞춘다 — 취소·반품된 전표는 금액을 건드리지 않는다.
    """
    require("purchase.edit")
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM purchase_batches WHERE id=?", (bid,)).fetchone()
        if row is None:
            abort(404, description="전표를 찾을 수 없습니다.")
        if row["cancelled_at"]:
            abort(400, description="취소된 전표는 금액을 바꿀 수 없습니다.")
        total = conn.execute(
            "SELECT COALESCE(SUM(purchase_price),0) AS s FROM assets WHERE batch_id=?",
            (bid,)).fetchone()["s"]
        if total == (row["total_amount"] or 0):
            return jsonify({"changed": False, "totalAmount": total})
        conn.execute("UPDATE purchase_batches SET total_amount=?, updated_at=? WHERE id=?",
                     (total, config.now_iso(), bid))
        audit.log("batch_amount_synced", target=row["slip_no"] or str(bid),
                  detail={"from": row["total_amount"], "to": total})
    return jsonify({"changed": True, "totalAmount": total})


@bp.get("/product-codes")
def product_codes():
    """제품코드 자동완성 — 이미 쓰고 있는 코드를 그대로 다시 쓰게 한다.

    ★같은 물건에 코드를 조금씩 다르게 적으면(840 G3_i7-6_내장 / 840G3_i7-6_내장)
      셋팅 화면의 재고 대조가 통째로 어긋난다. 손으로 다시 타이핑하지 않게 하는 것이
      이 기능의 목적이다(대표 요청 2026-08-05: "모든 제품코드가 들어가는 곳에").

    코드마다 함께 준다 —
      · 재고 수(출고 가능 / 전체)         → 지금 팔 수 있는지 바로 보인다
      · 대표 상품명                       → 코드만 봐선 뭔지 모른다
      · 그 코드로 들어온 지난 주문의 옵션 → 수기 주문에서 옵션을 그대로 가져다 쓴다
    """
    require("purchase.view")
    q = (request.args.get("q") or "").strip()
    conn = get_db()
    clause, params = scope_clause("a")

    # ★'보유'에서 출고완료·폐기·취소·반품을 뺀다. 안 빼면 이미 나간 물건까지
    #   보유로 세어져 '출고가능 0 · 보유 20' 같은 유령 숫자가 뜬다(2026-08-07 실측
    #   — 라이브의 보유 20은 전부 매입취소분이었다).
    dead = ",".join("?" * len(CODE_TARGET_EXCLUDE))
    where = f"TRIM(a.product_code) <> '' AND a.status NOT IN ({dead})" + clause
    args = list(CODE_TARGET_EXCLUDE) + list(params)
    if q:
        where += " AND a.product_code LIKE ?"
        args.append(f"%{q}%")

    marks = ",".join("?" * len(AVAILABLE_STATUSES))
    rows = conn.execute(
        "SELECT a.product_code AS code, COUNT(*) AS total, "
        f"  SUM(CASE WHEN a.status IN ({marks}) AND a.received = 1 "
        "        AND a.tier <> '가재고' THEN 1 ELSE 0 END) AS shippable, "
        "  MAX(a.maker || ' ' || a.model) AS model "
        "FROM assets a WHERE " + where + sale_only("a") +
        " GROUP BY a.product_code ORDER BY shippable DESC, total DESC LIMIT 30",
        list(AVAILABLE_STATUSES) + args).fetchall()

    out = []
    for r in rows:
        code = r["code"]
        # 지난 주문에서 쓰던 옵션 — 몰에서 실제로 팔린 조합이라 가장 믿을 만하다
        opts = [x["option_name"] for x in conn.execute(
            "SELECT option_name, COUNT(*) n FROM orders "
            "WHERE TRIM(option_name) <> '' AND (product_code = ? OR product_name LIKE ?) "
            "GROUP BY option_name ORDER BY n DESC LIMIT 5", (code, f"%{code}%")).fetchall()]
        name = conn.execute(
            "SELECT product_name FROM orders WHERE product_code = ? "
            "AND TRIM(product_name) <> '' ORDER BY id DESC LIMIT 1", (code,)).fetchone()
        out.append({
            "code": code, "total": r["total"], "shippable": r["shippable"] or 0,
            "model": (r["model"] or "").strip(),
            "productName": (name["product_name"] if name else ""),
            "options": opts,
        })

    # ★주문에 실려 온 코드도 후보로 준다(대표 2026-08-17: "15만 쳐도 나와야 하는 것 아니냐").
    #   자산에 코드를 아직 안 넣은 동안에는 위 목록이 통째로 비어서 자동완성이 무용지물이다.
    #   주문 코드가 곧 '앞으로 자산에 넣어야 할 코드'라 여기가 가장 정확한 출처다.
    #   자산에 이미 있는 코드는 위에서 이미 들어갔으므로 건너뛴다(재고 숫자가 있는 쪽이 낫다).
    have = {x["code"] for x in out}
    oq, oargs = "TRIM(product_code) <> ''", []
    if q:
        oq += " AND product_code LIKE ?"
        oargs.append(f"%{q}%")
    # ★몰이 보내는 코드에는 등급 꼬리표(AA급3)·사은품(+한컴)이 붙어 온다. 그대로 권하면
    #   자산에 엉뚱한 코드가 박혀 재고 대조가 쪼개진다 — 셋팅 화면과 같은 규칙으로 깎는다.
    from ..orders.product_info import sku_of
    picked = {}
    for r in conn.execute(
            "SELECT product_code AS code, product_name AS nm, COUNT(*) AS n "
            f"FROM orders WHERE {oq} GROUP BY product_code, product_name "
            "ORDER BY n DESC LIMIT 120", oargs).fetchall():
        code = sku_of(r["code"], "") or (r["code"] or "").strip()
        if not code or code in have:
            continue
        # 깎고 나면 찾던 글자와 안 맞을 수 있다(꼬리표에만 걸린 경우) — 그건 후보가 아니다
        if q and q.lower() not in code.lower():
            continue
        cur = picked.setdefault(code, {"n": 0, "nm": ""})
        cur["n"] += r["n"]
        if not cur["nm"]:
            cur["nm"] = (r["nm"] or "").strip()
    for code, v in sorted(picked.items(), key=lambda kv: -kv[1]["n"]):
        if len(out) >= 40:
            break
        opts = [x["option_name"] for x in conn.execute(
            "SELECT option_name, COUNT(*) n FROM orders "
            "WHERE TRIM(option_name) <> '' AND product_code LIKE ? "
            "GROUP BY option_name ORDER BY n DESC LIMIT 5", (f"{code}%",)).fetchall()]
        out.append({
            "code": code, "total": 0, "shippable": 0, "model": "",
            "productName": v["nm"], "options": opts,
            "fromOrder": True, "orderCount": v["n"],   # 화면이 '주문 N건'으로 알려 준다
        })
    return jsonify({"codes": out})


def _unsellable_reasons(r):
    """이 자산이 왜 판매불가/미정비인지 — 화면 칩과 필터가 이 목록을 쓴다."""
    out = []
    if (r["status"] or "") in UNSELLABLE_STATUSES:
        out.append("상태:" + ASSET_STATUSES.get(r["status"], r["status"]))
    if (r["tier"] or "") == "가재고":
        out.append("가재고")
    if not r["received"]:
        out.append("미입고")
    return out


@bp.get("/assets/uncoded")
def uncoded_assets():
    """판매불가·제품코드 없음 자산을 '모델별'로 묶어서 준다 — 정리 화면용.

    ★같은 모델은 대개 같은 제품코드라, 모델로 묶어 두면 한 번에 넣을 수 있다
      (대표 2026-08-04). 이미 판매된 것과 다시 안 팔 것은 빼고 보여준다.

    ★2026-08-07 대표 지시로 '판매불가 모아보기'를 겸한다:
      "판매 불가능이 된다면 수리, 폐기, A/S 등 … 판매불가능 제품만 모아놓은
       카테고리도 매입탭 안에 만들어줘야 함."
      새 화면을 만들지 않고 이 화면을 넓혔다 — 오늘 실측으로 두 집합(코드 없음
      1,959대 / 판매불가+코드없음 합집합 1,959대)이 완전히 같아서, 화면을 하나 더
      만들면 똑같은 목록이 두 벌 생긴다(★같은 걸 두 벌로 만들지 마라 — 대표 지시).
      URL을 안 바꾼 것도 일부러다 — 화면·시험 여러 곳이 이 주소를 알고 있다.

    ★판매가능/판매불가의 축(2026-08-07 결정):
      판매가능 = received=1 AND status ∈ (입고·정비중·판매가능) AND tier ≠ 가재고
      판매불가 = 살아있는 자산 중 위를 못 채운 것 (사유: 상태·가재고·미입고)
      제품코드 없음·재고반영 꺼짐은 판매불가 '사유'가 아니라 별도 할 일이다 —
      코드를 넣는 순간 판매가능이어야 하므로 섞으면 코드를 넣어도 계속 여기 남는다.
    """
    require("purchase.view")
    scope, params = scope_clause("a")
    marks = ",".join("?" * len(CODE_TARGET_EXCLUDE))
    un = ",".join("?" * len(UNSELLABLE_STATUSES))
    rows = get_db().execute(
        "SELECT a.*, c.name AS category_name FROM assets a "
        "LEFT JOIN categories c ON c.id = a.category_id "
        f"WHERE a.status NOT IN ({marks})"
        f"  AND (TRIM(a.product_code) = '' OR a.status IN ({un})"
        "       OR a.tier = '가재고' OR a.received = 0)" + sale_only("a") + scope +
        " ORDER BY a.model, a.asset_no",
        list(CODE_TARGET_EXCLUDE) + list(UNSELLABLE_STATUSES) + params).fetchall()

    groups = {}
    unsellable_total = 0
    nocode_total = 0
    for r in rows:
        key = (r["model"] or "").strip() or "(모델 없음)"
        g = groups.setdefault(key, {
            "model": key, "makers": set(), "count": 0,
            "byStatus": {}, "byGrade": {}, "byTier": {},
            "unsellable": 0, "noCode": 0, "assets": [],
        })
        g["count"] += 1
        if r["maker"]:
            g["makers"].add(r["maker"])
        for field, bucket in (("status", "byStatus"), ("grade", "byGrade"), ("tier", "byTier")):
            v = r[field] or ""
            label = ASSET_STATUSES.get(v, v) if field == "status" else (v or "미정")
            g[bucket][label] = g[bucket].get(label, 0) + 1
        reasons = _unsellable_reasons(r)
        if reasons:
            g["unsellable"] += 1
            unsellable_total += 1
        if not (r["product_code"] or "").strip():
            g["noCode"] += 1
            nocode_total += 1
        g["assets"].append(_asset_payload(r, {"categoryName": r["category_name"],
                                              "reasons": reasons}))
    out = []
    for g in groups.values():
        g["maker"] = " / ".join(sorted(g.pop("makers"))) or ""
        out.append(g)
    # 대수가 많은 모델부터 — 한 번에 많이 처리되는 것을 위로
    out.sort(key=lambda x: (-x["count"], x["model"]))
    return jsonify({"groups": out, "total": len(rows), "models": len(out),
                    # 두 숫자를 갈라 준다 — 합쳐 세면 '코드만 없는' 1,950대까지
                    # 판매불가로 보여 정반대 얘기가 된다(2026-08-07 검증 지적).
                    "unsellable": unsellable_total, "noCode": nocode_total,
                    "excluded": [ASSET_STATUSES[s] for s in CODE_TARGET_EXCLUDE]})


@bp.get("/assets/to-convert")
def assets_to_convert():
    """가용이 아닌 자산(실재고·가재고)을 카테고리별로 묶어 준다 — 전환 안내 화면용.

    ★가재고는 출고가 막혀 있어 누군가 손을 대야 나간다(2026-08-04 대표).
      어디에 몇 대가 묶여 있는지 한눈에 보여 주고, 그 자리에서 수리 내역을 적고
      가용/실재고로 올릴 수 있게 한다. 이미 나갔거나 안 팔 것은 제외한다.
    """
    require("purchase.view")
    scope, params = scope_clause("a")
    marks = ",".join("?" * len(CODE_TARGET_EXCLUDE))
    rows = get_db().execute(
        "SELECT a.*, c.name AS category_name, b.slip_no, "
        "(SELECT COALESCE(SUM(r.cost),0) FROM asset_repairs r WHERE r.asset_id=a.id) AS repair_cost, "
        "(SELECT COUNT(*) FROM asset_repairs r WHERE r.asset_id=a.id) AS repair_count "
        "FROM assets a LEFT JOIN categories c ON c.id = a.category_id "
        "LEFT JOIN purchase_batches b ON b.id = a.batch_id "
        f"WHERE a.tier <> '가용' AND a.status NOT IN ({marks})" + sale_only("a") + scope +
        " ORDER BY a.tier DESC, c.name, a.model, a.asset_no",
        list(CODE_TARGET_EXCLUDE) + params).fetchall()

    groups = {}
    for r in rows:
        key = r["category_name"] or "(분류 없음)"
        g_ = groups.setdefault(key, {"category": key, "count": 0, "byTier": {},
                                     "repairCost": 0, "assets": []})
        g_["count"] += 1
        g_["byTier"][r["tier"]] = g_["byTier"].get(r["tier"], 0) + 1
        g_["repairCost"] += r["repair_cost"] or 0
        g_["assets"].append(_asset_payload(r, {
            "categoryName": r["category_name"], "slipNo": r["slip_no"] or "",
            "repairCost": r["repair_cost"] or 0, "repairCount": r["repair_count"] or 0}))
    out = sorted(groups.values(), key=lambda x: (-x["count"], x["category"]))
    by_tier = {}
    for r in rows:
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
    return jsonify({"groups": out, "total": len(rows), "byTier": by_tier,
                    "repairCost": sum(r["repair_cost"] or 0 for r in rows),
                    "tierHelp": TIER_HELP})


@bp.get("/assets/summary")
def assets_summary():
    require("purchase.view")
    clause, params = scope_clause("a")
    rows = get_db().execute(
        # ★아직 실물을 안 받은 가입고분은 재고 요약에서 뺀다 — 이걸 세면
        #   대시보드가 매입 화면보다 큰 숫자를 말한다(2026-07-31 감사).
        "SELECT a.category_id, c.name AS category_name, a.grade, a.status, COUNT(*) AS cnt "
        "FROM assets a LEFT JOIN categories c ON c.id = a.category_id "
        "WHERE a.received = 1" + sale_only("a") + clause +
        " GROUP BY a.category_id, a.grade, a.status", params
    ).fetchall()
    return jsonify([
        {"categoryId": r["category_id"], "categoryName": r["category_name"],
         "grade": r["grade"], "status": r["status"], "count": r["cnt"]}
        for r in rows
    ])


# 재고 현황을 '한눈에' 읽기 위한 3분류 — 상태 11개를 그대로 늘어놓으면 눈에 안 들어온다.
STOCK_BUCKETS = {
    "ready": ("ready",),                                       # 지금 팔 수 있는 것
    "working": ("in_stock", "refurbishing", "repair", "painting"),   # 손보면 팔 수 있는 것
    "held": ("reserved", "as", "returning", "defective"),      # 묶여 있거나 못 파는 것
}
STOCK_GONE = ("shipped", "scrapped")                           # 재고에서 빠진 것


def _bucket_of(status):
    for name, codes in STOCK_BUCKETS.items():
        if status in codes:
            return name
    return "gone"


@bp.get("/assets/stock-by-model")
def assets_stock_by_model():
    """제품(모델)별 재고 현황 — 지금 무엇이 몇 대 남았는지 한 장에 본다.

    같은 모델이어도 스펙이 다르면 실질적으로 다른 상품이라, 모델을 큰 줄로 묶고
    스펙 조합을 그 아래 줄로 편다(화면에서 펼쳐 보게).
    """
    require("purchase.view")
    clause, params = scope_clause("a")
    rows = get_db().execute(
        "SELECT a.category_id, c.name AS category_name, "
        "       COALESCE(NULLIF(TRIM(a.maker),''),'') AS maker, "
        "       COALESCE(NULLIF(TRIM(a.model),''),'') AS model, "
        "       a.cpu, a.ram, a.ssd, a.inch, a.grade, a.status, "
        "       COUNT(*) AS cnt, "
        "       SUM(a.purchase_price) AS buy_sum, SUM(a.sale_price) AS sale_sum, "
        # ★평균은 '값이 있는 것'만 나눠야 한다. 매입가 공란(0원, TMS 이관분)까지 분모에 넣으면
        #   평균 매입가가 실제보다 낮게 나와 마진이 부풀려 보인다(판매가는 원래 이렇게 세고 있었다).
        "       SUM(CASE WHEN a.purchase_price > 0 THEN 1 ELSE 0 END) AS buy_n, "
        "       SUM(CASE WHEN a.sale_price > 0 THEN 1 ELSE 0 END) AS sale_n "
        "FROM assets a LEFT JOIN categories c ON c.id = a.category_id WHERE 1=1"
        + sale_only("a") + clause + RECEIVED_SQL +
        " GROUP BY a.category_id, maker, model, a.cpu, a.ram, a.ssd, a.inch, a.grade, a.status",
        params).fetchall()

    products = {}
    for r in rows:
        name = " ".join(x for x in (r["maker"], r["model"]) if x) or "(모델 미입력)"
        p = products.setdefault(name, {
            "product": name, "maker": r["maker"], "model": r["model"],
            "categoryId": r["category_id"], "categoryName": r["category_name"] or "미지정",
            "ready": 0, "working": 0, "held": 0, "gone": 0, "stock": 0, "total": 0,
            "grades": {}, "buySum": 0, "buyCount": 0, "buyMissing": 0,
            "saleSum": 0, "saleCount": 0,
            "variants": {},
        })
        bucket = _bucket_of(r["status"])
        n = r["cnt"]
        p[bucket] += n
        p["total"] += n
        if bucket != "gone":
            p["stock"] += n
            p["grades"][r["grade"]] = p["grades"].get(r["grade"], 0) + n
            p["buySum"] += r["buy_sum"] or 0
            p["buyCount"] += r["buy_n"] or 0     # ★매입가가 실제로 있는 대수만
            p["buyMissing"] += n - (r["buy_n"] or 0)   # 매입가 미입력 — 화면에 알려 준다
            p["saleSum"] += r["sale_sum"] or 0
            p["saleCount"] += r["sale_n"] or 0

        spec = " / ".join(x for x in (r["cpu"], r["ram"], r["ssd"], r["inch"]) if x) or "스펙 미입력"
        v = p["variants"].setdefault(spec, {"spec": spec, "ready": 0, "working": 0,
                                            "held": 0, "gone": 0, "stock": 0})
        v[bucket] += n
        if bucket != "gone":
            v["stock"] += n

    out = []
    for p in products.values():
        p["variants"] = sorted(p["variants"].values(),
                               key=lambda v: (-v["stock"], -v["ready"], v["spec"]))
        p["avgBuy"] = round(p["buySum"] / p["buyCount"]) if p["buyCount"] else 0
        p["avgSale"] = round(p["saleSum"] / p["saleCount"]) if p["saleCount"] else 0
        p["stockValue"] = p["buySum"]          # 실제 매입가 합(평균×수량은 반올림이 쌓인다)
        p["gradeList"] = sorted(p["grades"].items(), key=lambda kv: -kv[1])
        # 팔린 적 있는데 남은 게 얼마 없으면 매입 신호
        p["lowStock"] = bool(p["gone"] and p["ready"] <= 2)
        for k in ("buySum", "buyCount", "saleSum", "saleCount", "grades"):
            p.pop(k)
        out.append(p)
    # 팔 수 있는 게 많은 순 → 그다음 보유 많은 순. 재고 0(품절)은 자연히 아래로 간다.
    out.sort(key=lambda p: (-p["ready"], -p["stock"], p["product"]))

    totals = {k: sum(p[k] for p in out) for k in ("ready", "working", "held", "stock", "total")}
    totals["products"] = len(out)
    totals["soldOut"] = sum(1 for p in out if p["stock"] == 0 and p["gone"])
    totals["lowStock"] = sum(1 for p in out if p["lowStock"] and p["stock"])
    totals["stockValue"] = sum(p["stockValue"] for p in out)
    return jsonify({"products": out, "totals": totals,
                    "buckets": {"ready": "판매가능", "working": "작업중", "held": "보류"}})


def _asset_input(body):
    """자산 등록/수정 공통 입력 파싱(스펙 필드 포함)."""
    out = {}
    for key in ("maker", "model", "serial", "notes") + SPEC_FIELDS:
        if key in body:
            out[key] = (body.get(key) or "").strip()
    return out


@bp.get("/assets/model-brief")
def asset_model_brief():
    """모델 하나의 '최근 매입 단가' 요약 — 매입 입력 중 단가 판단용.

    대표 요청(2026-07-30): "재고내역과 자주 대조하는데 탭이 따로 있으니 사용성이 떨어진다."

    ★재고 숫자는 더 이상 주지 않는다(대표 지시 2026-08-07):
      "이름으로 유추하는 매입 제품 재고 안내는 아예 없애줘.
       앞으로 모든 재고 확인은 제품코드로 통일."
      모델명 LIKE 는 '갤럭시탭 S6'를 치면 'S6 Lite'까지 세는 식이라 숫자가 틀렸다.
      최근 매입 단가는 재고가 아니라 가격 판단 근거라 남긴다 — 이건 대표가
      2026-07-30에 직접 요청한 것이고, 대수를 말하지 않는다.
    """
    require("purchase.view")
    model = (request.args.get("model") or "").strip()
    maker = (request.args.get("maker") or "").strip()
    if len(model) < 2:
        return jsonify({"model": model, "recentBuys": [], "avgBuy": None,
                        "reason": "모델명을 2자 이상 입력하세요."})

    key = model.upper().replace(" ", "")
    clause, params = scope_clause("a")
    conn = get_db()
    like = "%" + key + "%"

    # 최근 매입 — 어디서 얼마에 샀는지(단가 판단 근거)
    recent = conn.execute(
        "SELECT a.purchase_price, a.grade, a.created_at, b.purchase_date, "
        "       COALESCE(rep.name, s.name) AS supplier_name, b.slip_no "
        "FROM assets a LEFT JOIN purchase_batches b ON b.id = a.batch_id "
        "LEFT JOIN suppliers s ON s.id = b.supplier_id "
        "LEFT JOIN suppliers rep ON rep.id = s.alias_of "
        "WHERE REPLACE(UPPER(a.model),' ','') LIKE ? AND a.purchase_price > 0"
        + sale_only("a") + clause +
        " ORDER BY COALESCE(b.purchase_date, a.created_at) DESC LIMIT 5",
        [like] + params).fetchall()

    prices = [r["purchase_price"] for r in recent]
    return jsonify({
        "model": model, "maker": maker,
        "avgBuy": round(sum(prices) / len(prices)) if prices else None,
        "recentBuys": [
            {"price": r["purchase_price"], "grade": r["grade"],
             "date": r["purchase_date"] or (r["created_at"] or "")[:10],
             "supplier": r["supplier_name"] or "", "slipNo": r["slip_no"] or ""}
            for r in recent
        ],
        "reason": "",
    })


def _add_assets(conn, body, batch_id, received, label=""):
    """자산 N대를 그 트랜잭션 안에서 만든다. create_assets와 전표 동시저장이 공유한다.

    ★한 전표에 자산을 따로따로 넣으면 중간에 실패했을 때 반쪽 상태가 된다.
      TMS는 [상세추가]로 쌓은 뒤 한 번에 저장하므로(전표당 평균 32대, 최대 366대)
      OWS도 같은 트랜잭션에서 처리해야 한다.
    label은 오류 메시지에 '3번째 줄'처럼 위치를 알려 주기 위한 것.
    """
    # ★모델 마스터 보완(2026-09-02, masters.py) — 마스터에 있으면 빈 브랜드·카테고리를 채우고, 없으면 막지 않고 경고만
    body, model_note = masters.complete_asset_body(conn, body)
    category_id = _int_or_400(body.get("categoryId"), "카테고리 ID")
    qty = _int_or_400(body.get("qty") or 1, "수량")
    if not 1 <= qty <= 100:
        abort(400, description=f"{label}수량은 1~100 사이여야 합니다.")
    manual_no = (body.get("assetNo") or "").strip()
    if manual_no and qty != 1:
        abort(400, description=f"{label}관리번호를 직접 지정할 때는 1개씩만 등록할 수 있습니다.")
    # ★손입력 번호 규칙(2026-09-03, A4 — numbering.py): TMS 대역(<5000)은 연동 사본에 있는 번호만,
    #   OWS 대역(≥5000)은 사본과 겹치면 거부. 창구 조회는 호출 라우트가 tx 밖에서 미리 해 뒀다(precheck_bodies).
    unverified = numbering.validate_manual_no(conn, manual_no, label) if manual_no else None
    purchase_price = _money_or_400(body.get("purchasePrice") or 0, "매입가")
    sale_price = _money_or_400(body.get("salePrice") or 0, "판매가")
    grade = (body.get("grade") or "미정").strip()
    if grade not in GRADES:
        abort(400, description=f"{label}등급은 {', '.join(GRADES)} 중 하나여야 합니다.")
    tier = norm_tier(body.get("tier")) or "가용"
    if tier not in TIERS:
        abort(400, description=f"{label}재고 구분은 {', '.join(TIERS)} 중 하나여야 합니다.")
    # 제품코드를 등록 시점에 넣을 수 있다 — 나중에 따로 채우러 다니지 않아도 된다(2026-08-04)
    product_code = (body.get("productCode") or "").strip()
    if len(product_code) > 60:
        abort(400, description=f"{label}제품코드는 60자 이내여야 합니다.")
    spec = _asset_input(body)

    if conn.execute("SELECT id FROM categories WHERE id=?", (category_id,)).fetchone() is None:
        abort(400, description=f"{label}존재하지 않는 카테고리입니다.")
    _check_asset_scope(conn, category_id)

    # ★시리얼 중복은 등록 시점에 막는다.
    #   같은 시리얼이 두 대면 어느 쪽이 진짜인지 알 수 없어 이력이 통째로 흔들린다.
    #   빈 시리얼은 정상(모니터·부속 등 시리얼이 없는 물건이 있다) — 값이 있을 때만 본다.
    serial = (spec.get("serial") or "").strip()
    if serial:
        dup = conn.execute(
            "SELECT asset_no FROM assets WHERE TRIM(serial)=? LIMIT 1", (serial,)).fetchone()
        if dup:
            abort(409, description=(
                f"{label}시리얼 {serial}은(는) 이미 자산 {dup['asset_no']}에 등록돼 있습니다."
                " 같은 기기를 두 번 등록하려는 게 아닌지 확인하세요."))
        if qty > 1:
            abort(400, description=f"{label}여러 대를 한 번에 등록할 때는 시리얼을 비워 두세요"
                                   "(개체마다 달라 나중에 각각 입력합니다).")

    created = []
    ts = config.now_iso()
    cols = ["asset_no", "batch_id", "category_id", "grade", "tier", "product_code",
            "purchase_price", "sale_price",
            "status", "received", "created_by", "created_at", "updated_at"]
    extra_cols = [k for k in spec]
    for _i in range(qty):
        asset_no = manual_no or next_asset_no(conn)
        if conn.execute("SELECT id FROM assets WHERE asset_no=?", (asset_no,)).fetchone():
            abort(409, description=f"{label}이미 존재하는 관리번호입니다: {asset_no}")
        vals = [asset_no, batch_id, category_id, grade, tier, product_code,
                purchase_price, sale_price,
                "in_stock", received, g.user["display_name"], ts, ts]
        for k in extra_cols:
            # 시리얼은 개체마다 다르므로 여러 대 등록 시에는 비운다
            vals.append("" if (k == "serial" and qty > 1) else spec[k])
        all_cols = cols + extra_cols
        cur = conn.execute(
            f"INSERT INTO assets({','.join(all_cols)}) VALUES({','.join('?' * len(all_cols))})", vals)
        aid = cur.lastrowid
        asset_event(conn, aid, "등록", {
            "관리번호": asset_no, "전표": batch_id,
            "수동지정": bool(manual_no), "매입가": purchase_price,
            "입고확인": bool(received),
        })
        if unverified:                       # 사본과 대조 못 한 손입력 번호 — 막지 않고 기록만 남긴다
            numbering.note_unverified(conn, aid, asset_no, unverified)
        created.append({"id": aid, "assetNo": asset_no, **model_note})
    return created


@bp.post("/assets")
def create_assets():
    """자산 등록. qty>1이면 같은 스펙으로 연번 발번. assetNo 지정 시(TMS 이관) qty=1만."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    batch_id = body.get("batchId")
    numbering.precheck_bodies([body])   # ★관리번호 연동 검증·채번 보류 판정은 tx 밖에서 한 번(2026-09-03, A4)
    with tx(write=True) as conn:
        received = 1
        if batch_id not in (None, ""):
            batch_id = _int_or_400(batch_id, "전표 ID")
            b = conn.execute("SELECT id, stage, cancelled_at FROM purchase_batches WHERE id=?",
                             (batch_id,)).fetchone()
            if b is None:
                abort(400, description="존재하지 않는 전표입니다.")
            # ★취소된 전표는 목록에서 숨겨져 있다. 거기에 자산을 붙이면
            #   재고에는 있는데 어느 매입 건인지 화면에서 되짚을 수 없는 유령 자산이 된다.
            if b["cancelled_at"]:
                abort(400, description="취소된 전표에는 자산을 추가할 수 없습니다."
                                       " 취소를 되돌린 뒤 추가하세요.")
            # ★가입고(V) = 아직 실물이 안 들어온 단계 → 재고로 세지 않는다
            if b["stage"] == "provisional":
                received = 0
        else:
            batch_id = None
        created = _add_assets(conn, body, batch_id, received)
        audit.log("assets_created",
                  target=created[0]["assetNo"] if len(created) == 1 else f"{len(created)}대",
                  detail={"count": len(created), "batchId": batch_id,
                          "assetNos": [c["assetNo"] for c in created][:20]})
    return jsonify(created), 201


@bp.get("/assets/<int:aid>")
def asset_detail(aid):
    require("purchase.view")
    conn = get_db()
    row = _get_asset_or_404(conn, aid)
    clause, params = scope_clause("a")
    if clause:
        ok = conn.execute(f"SELECT a.id FROM assets a WHERE a.id=? {clause}", [aid] + params).fetchone()
        if ok is None:
            abort(403, description="담당 카테고리가 아닌 자산입니다.")
    repairs = conn.execute(
        "SELECT * FROM asset_repairs WHERE asset_id=? ORDER BY repair_date DESC, id DESC", (aid,)
    ).fetchall()
    events = conn.execute(
        "SELECT * FROM asset_events WHERE asset_id=? ORDER BY id DESC LIMIT 100", (aid,)
    ).fetchall()
    orders = conn.execute(
        "SELECT o.id, o.channel, o.order_no, o.recipient, o.shipping_done, oa.matched_by, oa.matched_at "
        "FROM order_assets oa JOIN orders o ON o.id = oa.order_id WHERE oa.asset_id=? "
        "ORDER BY oa.matched_at DESC", (aid,)
    ).fetchall()
    # A/S 이력 요약 — 판매된 자산의 사후 관리(대표 2026-08-07: "판매된 제품 이력도
    # 보여야 하는데 이후 A/S 관리가 필요하니까").
    # ★고객 이름·연락처는 안 준다. 이 라우트는 purchase.view 게이트라, 티켓을 통째로
    #   붙이면 A/S 권한(as.view) 없이 고객 개인정보가 새는 문이 된다.
    try:
        as_rows = conn.execute(
            "SELECT ticket_no, status, as_type, received_at FROM as_tickets "
            "WHERE asset_id=? ORDER BY id DESC LIMIT 20", (aid,)).fetchall()
    except Exception:                                            # noqa: BLE001
        as_rows = []                          # A/S 테이블이 없는 옛 DB도 화면은 떠야 한다
    batch = None
    if row["batch_id"]:
        # ★LEFT JOIN이어야 한다. 거래처가 아직 없는 가입고(V) 전표는 INNER JOIN이면
        #   행이 통째로 사라져, 전표에 속해 있는데도 '전표 없음'으로 보인다.
        #   전표번호(slip_no)도 함께 준다 — 없으면 어느 전표인지 식별할 수 없다.
        b = conn.execute(
            "SELECT b.*, COALESCE(rep.name, s.name) AS supplier_name, s.name AS supplier_orig_name "
            "FROM purchase_batches b "
            "LEFT JOIN suppliers s ON s.id = b.supplier_id "
            "LEFT JOIN suppliers rep ON rep.id = s.alias_of WHERE b.id=?", (row["batch_id"],)
        ).fetchone()
        if b:
            batch = {"id": b["id"], "slipNo": b["slip_no"], "stage": b["stage"],
                     "stageLabel": STAGES.get(b["stage"], b["stage"]),
                     "supplierName": b["supplier_name"] or "",
                     "supplierOrigName": (b["supplier_orig_name"]
                                          if b["supplier_orig_name"] != b["supplier_name"] else None),
                     "purchaseDate": b["purchase_date"], "totalAmount": b["total_amount"],
                     # ★취소·반품을 감추면 죽은 전표를 살아있는 매입으로 읽는다
                     "cancelledAt": b["cancelled_at"], "cancelReason": b["cancel_reason"],
                     "returnedAt": b["returned_at"], "returnReason": b["return_reason"]}
    repair_total = sum(r["cost"] for r in repairs)
    # TMS 자산 단위 판매(2026-08-25) — 이 자산이 누구에게 얼마에 나갔는지 + OWS식 마진
    sale_row = conn.execute(
        "SELECT * FROM tms_sales WHERE (asset_id=? OR asset_no=?) "
        "AND stage NOT LIKE '%취소%' ORDER BY sale_date DESC LIMIT 1",
        (aid, row["asset_no"])).fetchone()
    tms_sale = None
    if sale_row is not None:
        # ★원가는 '판매하던 그 순간'의 값이어야 한다(2026-09-03 대표 승인으로 TMS 스냅샷 적재).
        #   자산의 지금 매입가·수리비로 계산하면 지난달 마진이 오늘 조용히 바뀐다
        #   (라이브에서 이미 151행이 판매 당시와 다르다). 그래서 순서는
        #   ① 판매 시점 제조원가(TMS 판매상세) → ② 자산의 지금 값 → ③ 명세의 매입가 이다.
        #   ★②·③ 순서는 예전 그대로다(판매 자산 목록 costSrc 'ows'→'tms' 와 같은 잣대) —
        #     스냅샷만 앞에 끼워 넣는다. 뒤집으면 우리 자산인데 남의 매입가로 세게 된다.
        k = sale_row.keys()
        snap = (sale_row["manufacture_cost"] if "manufacture_cost" in k else 0) or 0
        opt = 0
        if snap:
            for c in ("upgrade1_price", "upgrade2_price", "extra_price", "charger_price"):
                opt += (sale_row[c] if c in k else 0) or 0
            opt -= (sale_row["removal_price"] if "removal_price" in k else 0) or 0
            opt += (sale_row["packing_fee"] if "packing_fee" in k else 0) or 0
        cost_ows = (row["purchase_price"] or 0) + repair_total
        if snap:
            cost, cost_src = snap + opt, "snapshot"
        elif (row["purchase_price"] or 0) > 0:
            cost, cost_src = cost_ows, "asset"
        else:
            cost, cost_src = (sale_row["purchase_price"] or 0), "slip"
        tms_sale = {
            "slipNo": sale_row["slip_no"], "customer": sale_row["customer"],
            "channel": sale_row["channel"], "saleDate": (sale_row["sale_date"] or "")[:10],
            "salePrice": sale_row["sale_price"], "tmsProfit": sale_row["tms_profit"],
            "margin": (sale_row["sale_price"] or 0) - cost, "stage": sale_row["stage"],
            # 화면이 '무엇을 뺀 값인지' 말할 수 있게 원가와 출처를 함께 준다
            "cost": cost, "costSource": cost_src,
            "manufactureCost": snap, "optionCost": opt,
            "saleFee": (sale_row["sale_fee"] if "sale_fee" in k else 0) or 0,
            "netVat": (sale_row["net_vat"] if "net_vat" in k else 0) or 0,
        }
    if not can("purchase.money"):
        repair_total = None
        if tms_sale is not None:
            for k in ("salePrice", "tmsProfit", "margin", "cost", "manufactureCost",
                      "optionCost", "saleFee", "netVat"):
                tms_sale[k] = None
    return jsonify(_asset_payload(row, {
        "batch": batch,
        "tmsSale": tms_sale,
        "repairTotal": repair_total,
        "costTotal": (row["purchase_price"] + (repair_total or 0))
                     if can("purchase.money") else None,
        "repairs": [
            {"id": r["id"], "repairDate": r["repair_date"], "description": r["description"],
             "cost": r["cost"] if can("purchase.money") else None,
             "vat": r["vat"] if can("purchase.money") else None,
             "net": r["net"] if can("purchase.money") else None, "parts": r["parts"],
             "createdBy": r["created_by"]}
            for r in repairs
        ],
        "events": [
            {"ts": e["ts"], "action": e["action"], "actor": e["actor"],
             "detail": json.loads(e["detail"]) if e["detail"] else None}
            for e in events
        ],
        "orders": [
            {"orderId": o["id"], "channel": o["channel"], "orderNo": o["order_no"],
             "recipient": o["recipient"], "shipped": bool(o["shipping_done"]),
             "matchedBy": o["matched_by"], "matchedAt": o["matched_at"]}
            for o in orders
        ],
        "asTickets": [
            {"ticketNo": t["ticket_no"], "status": t["status"],
             "asType": t["as_type"], "receivedAt": t["received_at"]}
            for t in as_rows
        ],
    }))


@bp.post("/assets/product-code")
def bulk_product_code():
    """선택한 자산들에 같은 제품코드를 한 번에 기입한다 — 전표 화면의 일괄 버튼.

    ★한 전표에 여러 모델이 섞여 들어온다(2026-08-04 대표). 그래서 '전표 전체'가 아니라
      화면에서 체크한 자산에만 넣는다 — 같은 모델만 골라 일괄, 나머지는 각개 수정.
      빈 코드를 보내면 지우기다. 지워지는 자산은 재고반영도 함께 꺼진다(유령 상태 방지).
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        abort(400, description="자산을 선택하세요.")
    if len(ids) > 500:
        abort(400, description="한 번에 500대까지 처리할 수 있습니다.")
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        abort(400, description="자산 번호가 올바르지 않습니다.")
    pc = (body.get("productCode") or "").strip()
    if len(pc) > 60:
        abort(400, description="제품코드는 60자 이내여야 합니다.")
    # ★등급·재고구분도 같이 넣을 수 있다(2026-08-04 대표) — 코드만 넣고 등급을 따로
    #   돌면 같은 목록을 두 번 훑게 된다. 보내지 않은 항목은 건드리지 않는다.
    grade = body.get("grade")
    if grade is not None:
        grade = str(grade).strip()
        if grade and grade not in GRADES:
            abort(400, description=f"등급은 {', '.join(GRADES)} 중 하나여야 합니다.")
    tier = body.get("tier")
    if tier is not None:
        tier = norm_tier(tier)
        if tier and tier not in TIERS:
            abort(400, description=f"재고 구분은 {', '.join(TIERS)} 중 하나여야 합니다.")
    if not pc and not grade and not tier and "productCode" not in body:
        abort(400, description="바꿀 값을 입력하세요.")
    now = config.now_iso()
    done, skipped = [], []
    with tx(write=True) as conn:
        for aid in ids:
            row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            if row is None:
                skipped.append({"id": aid, "reason": "자산을 찾을 수 없습니다."})
                continue
            _check_asset_scope(conn, row["category_id"])
            touched = False
            if "productCode" in body and (row["product_code"] or "") != pc:
                conn.execute("UPDATE assets SET product_code=? WHERE id=?", (pc, aid))
                asset_event(conn, aid, "제품코드",
                            {"from": row["product_code"], "to": pc, "일괄": True})
                if not pc and row["stock_listed"]:
                    conn.execute("UPDATE assets SET stock_listed=0, stock_listed_at='', "
                                 "stock_listed_by='' WHERE id=?", (aid,))
                    asset_event(conn, aid, "재고해제", {"사유": "제품코드 삭제(일괄)"})
                touched = True
            if grade and grade != row["grade"]:
                conn.execute("UPDATE assets SET grade=? WHERE id=?", (grade, aid))
                asset_event(conn, aid, "등급", {"from": row["grade"], "to": grade, "일괄": True})
                touched = True
            if tier and tier != row["tier"]:
                conn.execute("UPDATE assets SET tier=? WHERE id=?", (tier, aid))
                asset_event(conn, aid, "재고구분", {"from": row["tier"], "to": tier, "일괄": True})
                touched = True
            if not touched:
                continue                       # 이미 같은 값 — 이력만 어지럽히지 않는다
            conn.execute("UPDATE assets SET updated_at=? WHERE id=?", (now, aid))
            done.append(row["asset_no"])
        audit.log("product_code_bulk", target=(pc or "(지움)"),
                  detail={"count": len(done), "skipped": len(skipped),
                          "등급": grade or "", "재고구분": tier or ""})
    return jsonify({"ok": len(done), "done": done, "skipped": skipped, "productCode": pc,
                    "grade": grade or "", "tier": tier or ""})


@bp.post("/assets/stock-listing")
def bulk_stock_listing():
    """여러 자산의 '재고반영'을 한 번에 켜거나 끈다 — 매입 화면의 일괄 버튼.

    ★수리를 다녀온 물건을 몰아서 올리는 실무 흐름(2026-08-04 대표).
      켤 때는 제품코드가 있는 것만 켜지고, 없는 것은 건너뛰어 사유와 함께 알려 준다.
      끄는 것은 제품코드와 무관하게 항상 된다(위험한 방향이 아니므로).
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        abort(400, description="자산을 선택하세요.")
    if len(ids) > 500:
        abort(400, description="한 번에 500대까지 처리할 수 있습니다.")
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        abort(400, description="자산 번호가 올바르지 않습니다.")
    on = bool(body.get("on"))
    now = config.now_iso()
    actor = g.user["display_name"]
    done, skipped = [], []
    with tx(write=True) as conn:
        for aid in ids:
            row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            if row is None:
                skipped.append({"id": aid, "reason": "자산을 찾을 수 없습니다."})
                continue
            _check_asset_scope(conn, row["category_id"])
            # ★렌탈 귀속은 팔 물건이 아니다 — 켜도 몰 재고에는 안 잡히는데(sale_only)
            #   화면만 '전송'으로 보이면 있지도 않은 재고를 판 것으로 오해한다.
            # ★제품코드 검사보다 먼저 본다 — 렌탈 자산은 대개 제품코드가 없어서,
            #   순서가 반대면 "제품코드를 넣으세요"라는 엉뚱한 안내를 받고 코드를 채운 뒤에야
            #   진짜 이유를 만나게 된다.
            if on and row["division"] == DIVISION_RENTAL:
                skipped.append({"id": aid, "assetNo": row["asset_no"],
                                "reason": "렌탈 사업부 자산은 판매 재고로 올릴 수 없습니다."})
                continue
            if on and not (row["product_code"] or "").strip():
                skipped.append({"id": aid, "assetNo": row["asset_no"],
                                "reason": "제품코드가 없어 재고반영할 수 없습니다."})
                continue
            if bool(row["stock_listed"]) == on:
                continue                       # 이미 원하는 상태 — 이력만 어지럽히지 않는다
            conn.execute(
                "UPDATE assets SET stock_listed=?, stock_listed_at=?, stock_listed_by=?, "
                "updated_at=? WHERE id=?",
                (1 if on else 0, now if on else "", actor if on else "", now, aid))
            asset_event(conn, aid, "재고반영" if on else "재고해제", {"일괄": True})
            done.append(row["asset_no"])
        audit.log("stock_listing", target=f"{'반영' if on else '해제'} {len(done)}대",
                  detail={"skipped": len(skipped)})
    return jsonify({"ok": len(done), "done": done, "skipped": skipped})


@bp.patch("/assets/<int:aid>")
def update_asset(aid):
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    numbering.precheck_update(aid, body)   # ★번호가 바뀌는 요청만 연동 창구에 묻는다 — tx 밖(2026-09-03, A4)
    with tx(write=True) as conn:
        row = _get_asset_or_404(conn, aid)
        _check_asset_scope(conn, row["category_id"])
        # ★전값 기록(2026-09-03, A7): 바뀐 칸은 {"from": 전, "to": 후} — 등급·제품코드가 쓰던 모양 그대로.
        #   창구가 OWS로 오면 이 이력이 유일한 감사 기록이다. 같은 값은 애초에 changes 에 안 들어간다.
        changes = {}
        # ★모델 마스터 보완(2026-09-02, masters.py) — 모델명·브랜드를 고칠 때 빈 브랜드를 채우고, 없으면 경고만
        body, model_note = (masters.complete_asset_body(conn, body, row)
                            if ("model" in body or "maker" in body) else (body, {}))

        if "assetNo" in body:  # TMS 이관 등 — 번호 변경은 이력에 남긴다
            new_no = (body.get("assetNo") or "").strip()
            if not new_no or len(new_no) > 30:
                abort(400, description="관리번호는 1~30자여야 합니다.")
            if new_no != row["asset_no"]:
                if conn.execute("SELECT id FROM assets WHERE asset_no=? AND id != ?", (new_no, aid)).fetchone():
                    abort(409, description="이미 존재하는 관리번호입니다.")
                unverified = numbering.validate_manual_no(conn, new_no)   # ★A4: TMS 대역은 사본 대조, OWS 대역은 충돌 검사
                conn.execute("UPDATE assets SET asset_no=? WHERE id=?", (new_no, aid))
                asset_event(conn, aid, "번호변경", {"from": row["asset_no"], "to": new_no})
                if unverified:
                    numbering.note_unverified(conn, aid, new_no, unverified)
                changes["관리번호"] = {"from": row["asset_no"], "to": new_no}

        if "batchId" in body:
            b = body.get("batchId")
            b = None if b in (None, "") else _int_or_400(b, "전표 ID")
            if b is not None and conn.execute(
                    "SELECT id FROM purchase_batches WHERE id=?", (b,)).fetchone() is None:
                abort(400, description="존재하지 않는 전표입니다.")
            if b != row["batch_id"]:
                conn.execute("UPDATE assets SET batch_id=? WHERE id=?", (b, aid))
                changes["전표"] = {"from": row["batch_id"], "to": b}

        if "categoryId" in body:
            cid = _int_or_400(body["categoryId"], "카테고리 ID")
            if conn.execute("SELECT id FROM categories WHERE id=?", (cid,)).fetchone() is None:
                abort(400, description="존재하지 않는 카테고리입니다.")
            _check_asset_scope(conn, cid)
            if cid != row["category_id"]:
                conn.execute("UPDATE assets SET category_id=? WHERE id=?", (cid, aid))
                names = {r["id"]: r["name"] for r in conn.execute(
                    "SELECT id, name FROM categories WHERE id IN (?,?)", (row["category_id"], cid))}
                changes["카테고리"] = {"from": names.get(row["category_id"], row["category_id"]),
                                    "to": names.get(cid, cid)}

        for field in ("maker", "model", "serial", "notes") + SPEC_FIELDS:
            if field in body:
                v = (body.get(field) or "").strip()
                if v != row[field]:
                    # ★수정으로도 시리얼이 겹치면 안 된다 — 등록만 막으면 우회된다
                    if field == "serial" and v:
                        dup = conn.execute(
                            "SELECT asset_no FROM assets WHERE TRIM(serial)=? AND id<>? LIMIT 1",
                            (v, aid)).fetchone()
                        if dup:
                            abort(409, description=(
                                f"시리얼 {v}은(는) 이미 자산 {dup['asset_no']}에 등록돼 있습니다."))
                    conn.execute(f"UPDATE assets SET {field}=? WHERE id=?", (v, aid))
                    changes[field] = {"from": row[field], "to": v}

        if "stockNote" in body:
            # 재고비고(2026-09-03, 계획서 ②(g)) — TMS 재고상세의 짧은 상태 메모('새배터리교체', '액정불량'). 특이사항(매입상세비고)과 다른 칸.
            v = (body.get("stockNote") or "").strip()
            if len(v) > 200:
                abort(400, description="재고비고는 200자 이내여야 합니다.")
            if v != (row["stock_note"] or ""):
                conn.execute("UPDATE assets SET stock_note=? WHERE id=?", (v, aid))
                changes["재고비고"] = {"from": row["stock_note"] or "", "to": v}

        if "grade" in body:
            grade = (body.get("grade") or "").strip()
            if grade not in GRADES:
                abort(400, description=f"등급은 {', '.join(GRADES)} 중 하나여야 합니다.")
            if grade != row["grade"]:
                conn.execute("UPDATE assets SET grade=? WHERE id=?", (grade, aid))
                changes["등급"] = {"from": row["grade"], "to": grade}

        if "tier" in body:
            tier = norm_tier(body.get("tier"))
            if tier not in TIERS:
                abort(400, description=f"재고 구분은 {', '.join(TIERS)} 중 하나여야 합니다.")
            if tier != row["tier"]:
                conn.execute("UPDATE assets SET tier=? WHERE id=?", (tier, aid))
                asset_event(conn, aid, "재고구분", {"from": row["tier"], "to": tier})
                changes["재고구분"] = {"from": row["tier"], "to": tier}

        if "tierTasks" in body:
            # 보수 체크(2026-08-08 대표): 무엇을 보수해야 하는지·하는 중인지 항목별 기록
            packed = _clean_tier_tasks(body.get("tierTasks") or {})
            if packed != (row["tier_tasks"] or ""):
                conn.execute("UPDATE assets SET tier_tasks=? WHERE id=?", (packed, aid))
                summary = _tier_tasks_summary(packed)
                asset_event(conn, aid, "보수체크", {"항목": summary})
                changes["보수체크"] = summary

        if "productCode" in body:
            # 제품코드 = 쇼핑몰 재고의 축(예: 840 G3_i7-6_내장).
            # ★대표가 직접 기입한다 — 모델·CPU에서 자동으로 만들지 않는다(2026-08-04 지시).
            pc = (body.get("productCode") or "").strip()
            if len(pc) > 60:
                abort(400, description="제품코드는 60자 이내여야 합니다.")
            if pc != row["product_code"]:
                conn.execute("UPDATE assets SET product_code=? WHERE id=?", (pc, aid))
                asset_event(conn, aid, "제품코드", {"from": row["product_code"], "to": pc})
                changes["제품코드"] = {"from": row["product_code"], "to": pc}
                # ★코드를 지우면 재고반영도 함께 끈다(2026-08-04 검증 결함 ②).
                #   켤 때는 '코드 필수'로 막는데 지우기로 우회되면, 화면엔 '반영 켜짐'인데
                #   집계에선 조용히 빠지는 유령 상태가 된다 — 직원은 몰에 올라간 줄 안다.
                if not pc and row["stock_listed"]:
                    conn.execute(
                        "UPDATE assets SET stock_listed=0, stock_listed_at='', "
                        "stock_listed_by='' WHERE id=?", (aid,))
                    asset_event(conn, aid, "재고해제", {"사유": "제품코드 삭제"})
                    changes["재고반영"] = False

        if "stockListed" in body:
            # 재고반영 — 사람이 체크했을 때만 몰 재고에 세어질 자격이 생긴다.
            # ★제품코드를 넣었다고 자동으로 켜지지 않는다. 수리를 다녀와서 올리는
            #   경우가 있어 반영 시점은 사람이 정한다(2026-08-04 지시).
            want = bool(body.get("stockListed"))
            # ★코드가 이 요청에서 지워졌으면 그 '새 값'으로 판정해야 한다.
            #   옛 값으로 판정하면 '코드 지우기 + 켜기'를 한 번에 보내 가드를 우회한다.
            effective_code = ((body.get("productCode") or "") if "productCode" in body
                              else row["product_code"]) or ""
            # ★렌탈 귀속은 판매 재고가 아니다(위 일괄 처리와 같은 이유·같은 순서).
            #   끄는 것은 막지 않는다 — 정리는 언제든 되어야 한다.
            if want and row["division"] == DIVISION_RENTAL:
                abort(400, description="렌탈 사업부 자산은 판매 재고로 올릴 수 없습니다. "
                                       "판매할 물건이면 [↔ 사업부 이관]으로 먼저 넘기세요.")
            if want and not effective_code.strip():
                abort(400, description="재고반영은 제품코드가 있어야 켤 수 있습니다.")
            if "재고반영" in changes:
                pass                                   # 코드 삭제로 이미 꺼졌다 — 중복 기록 방지
            elif want != bool(row["stock_listed"]):
                conn.execute(
                    "UPDATE assets SET stock_listed=?, stock_listed_at=?, stock_listed_by=? WHERE id=?",
                    (1 if want else 0, config.now_iso() if want else "",
                     g.user["display_name"] if want else "", aid))
                asset_event(conn, aid, "재고반영" if want else "재고해제", {})
                changes["재고반영"] = want

        for key, col, label in (("purchasePrice", "purchase_price", "매입가"),
                                ("salePrice", "sale_price", "판매가")):
            if key in body:
                # 빈 칸(''), None은 0으로 — 자산 생성·전표·단가와 같은 규칙.
                # 예전엔 여기만 규칙이 달라 빈 칸 저장이 400이 되고, 같은 요청의
                # 등급·스펙 수정까지 통째로 되돌아갔다(2026-08-14 검토).
                price = _money_or_400(body[key] or 0, label)
                if price != row[col]:
                    conn.execute(f"UPDATE assets SET {col}=? WHERE id=?", (price, aid))
                    changes[label] = {"from": row[col], "to": price}

        if "status" in body:
            status = (body.get("status") or "").strip()
            if status not in ASSET_STATUSES:
                abort(400, description="알 수 없는 상태입니다.")
            if status != row["status"]:
                # ★'회수중'을 빼먹으면 회수 입고가 스킵돼 한 대가 두 고객에게 나간다
                #   (orders/recall.py 주석이 경고하는 바로 그 경로가 매입 화면으로 열려 있었다)
                if status not in MANUAL_STATUSES or row["status"] in ("reserved", "shipped", "returning"):
                    abort(400, description="주문매칭/출고 상태는 주문 흐름에서만 변경됩니다. 먼저 매칭을 해제하세요.")
                conn.execute("UPDATE assets SET status=? WHERE id=?", (status, aid))
                asset_event(conn, aid, "상태변경", {
                    "from": ASSET_STATUSES[row["status"]], "to": ASSET_STATUSES[status]})
                changes["status"] = {"from": row["status"], "to": status}
                # 판매가능 축을 벗어났으면 재고반영도 함께 끈다(유령 재고 방지).
                # ★같은 PATCH에서 재고반영을 켜고 상태를 수리로 바꾸는 경우도 있어(자산
                #   상세 폼은 둘을 항상 같이 보낸다) 조건 없이 시도한다 — _unlist 는
                #   이미 꺼져 있으면 아무것도 안 하는 멱등 함수라 이중 기록 걱정이 없다.
                if status not in AVAILABLE_STATUSES:
                    if _unlist(conn, aid, "상태변경(%s)" % ASSET_STATUSES[status]):
                        changes["재고반영"] = False

        if not changes:
            abort(400, description="변경할 항목이 없습니다.")
        conn.execute("UPDATE assets SET updated_at=? WHERE id=?", (config.now_iso(), aid))
        if set(changes) - {"관리번호", "status", "보수체크"}:  # 전용 이벤트로 이미 기록된 것 제외
            asset_event(conn, aid, "수정",
                        {k: v for k, v in changes.items()
                         if k not in ("관리번호", "status", "보수체크")})
        audit.log("asset_updated", target=row["asset_no"], detail=changes)
    return jsonify({"ok": True, **model_note})


# ---------------------------------------------------------------- repairs

def split_vat(total):
    """총액(부가세 포함) → (공급가, 부가세). 부가세 = 총액 × 10/110, 원 단위 반올림.

    ★대표 요청(2026-08-04): 총 금액만 넣으면 원 금액과 부가세가 자동으로 나와야 한다.
      세금계산서와 맞추려면 공급가 + 부가세 = 총액이 정확히 떨어져야 하므로
      부가세를 반올림한 뒤 공급가는 '총액 - 부가세'로 되돌려 계산한다.
    """
    total = int(total or 0)
    vat = round(total * 10 / 110)
    return total - vat, vat


@bp.post("/assets/<int:aid>/repairs")
def add_repair(aid):
    require("purchase.edit")
    body = request.get_json(silent=True) or {}

    # ── 단가표 부품 추가/회수(2026-08-10, 회수는 2026-08-13): partIds 가 오면 금액은
    #    서버가 단가표에서 읽는다. 화면이 보낸 금액을 믿지 않는다 — '오늘 단가'의 출처를
    #    한 곳으로 못박는 장치다. 항목은 숫자(장착 1개, 하위호환) 또는
    #    {id, qty, remove} — remove=true 면 다운그레이드 회수(음수 원가 = 순수익 가산).
    part_ids = body.get("partIds")
    if isinstance(part_ids, list) and part_ids:
        repair_date = (body.get("repairDate") or "").strip() or config.now().strftime("%Y-%m-%d")
        try:
            items = [(int(p["id"]), max(1, int(p.get("qty") or 1)), bool(p.get("remove")))
                     if isinstance(p, dict) else (int(p), 1, False)
                     for p in part_ids]
        except (TypeError, ValueError, KeyError):
            abort(400, description="부품 선택 값이 올바르지 않습니다.")
        added = []
        with tx(write=True) as conn:
            row = _get_asset_or_404(conn, aid)
            _check_asset_scope(conn, row["category_id"])
            for pid, qty, remove in items:
                part = conn.execute("SELECT * FROM parts WHERE id=? AND enabled=1",
                                    (pid,)).fetchone()
                if part is None:
                    abort(400, description="선택한 부품이 단가표에 없습니다.")
                if not part["price"]:
                    abort(400, description=f"'{part['name']}' 단가가 0원입니다 — "
                                           "기준정보 ▸ 부품 단가표에서 오늘 단가를 먼저 넣으세요.")
                cost = _apply_part_to_asset(conn, row, part, repair_date,
                                            g.user["display_name"], qty=qty, remove=remove)
                added.append({"name": part["name"], "cost": cost, "remove": remove})
                row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            audit.log("repair_added", target=row["asset_no"],
                      detail={"parts": [a["name"] for a in added],
                              "cost": sum(a["cost"] for a in added), "출처": "단가표"})
        return jsonify({"ok": True, "added": added,
                        "cost": sum(a["cost"] for a in added)}), 201

    desc = (body.get("description") or "").strip()
    parts = (body.get("parts") or "").strip()
    if not desc and not parts:
        abort(400, description="수리 내용이나 교체 부품을 입력하세요.")
    if not desc:
        desc = f"부품 교체: {parts}"
    if len(parts) > 200:
        abort(400, description="교체 부품은 200자 이내여야 합니다.")
    cost = _money_or_400(body.get("cost") or 0, "수리비용")
    net, vat = split_vat(cost)
    repair_date = (body.get("repairDate") or "").strip() or config.now().strftime("%Y-%m-%d")
    with tx(write=True) as conn:
        row = _get_asset_or_404(conn, aid)
        _check_asset_scope(conn, row["category_id"])
        cur = conn.execute(
            "INSERT INTO asset_repairs(asset_id, repair_date, description, cost, vat, net, parts, "
            "created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (aid, repair_date, desc, cost, vat, net, parts,
             g.user["display_name"], config.now_iso()),
        )
        queue_asset_cost(conn, aid, reason=f"수리 등록: {desc}"[:60])   # → TMS 재고 수리비
        asset_event(conn, aid, "수리", {"description": desc, "cost": cost,
                                       "공급가": net, "부가세": vat,
                                       "교체부품": parts, "date": repair_date})
        # 수리했으면 재고 구분을 함께 바꿀 수 있다 — 고쳐 놓고 가재고로 남으면 출고가 막힌다
        new_tier = norm_tier(body.get("tier"))
        if new_tier:
            if new_tier not in TIERS:
                abort(400, description=f"재고 구분은 {', '.join(TIERS)} 중 하나여야 합니다.")
            if new_tier != row["tier"]:
                conn.execute("UPDATE assets SET tier=? WHERE id=?", (new_tier, aid))
                asset_event(conn, aid, "재고구분",
                            {"from": row["tier"], "to": new_tier, "사유": "수리 완료"})
        conn.execute("UPDATE assets SET updated_at=? WHERE id=?", (config.now_iso(), aid))
        audit.log("repair_added", target=row["asset_no"],
                  detail={"cost": cost, "vat": vat, "description": desc, "parts": parts})
        rid = cur.lastrowid
    return jsonify({"id": rid, "cost": cost, "net": net, "vat": vat}), 201


@bp.get("/assets/duplicates")
def asset_duplicates():
    """같은 실물이 두 번 등록된 것 찾기 — 시리얼이 같으면 거의 확실한 중복이다.

    재고 수와 자산가치가 부풀려지는 원인이라 매입 화면에서 바로 보이게 한다.
    """
    require("purchase.view")
    clause, params = scope_clause("a")
    rows = get_db().execute(
        "SELECT a.serial, a.maker, a.model, COUNT(*) AS cnt, "
        "       GROUP_CONCAT(a.asset_no) AS nos, GROUP_CONCAT(a.id) AS ids, "
        "       GROUP_CONCAT(a.status) AS statuses "
        "FROM assets a WHERE TRIM(a.serial) != ''" + clause +
        " GROUP BY UPPER(TRIM(a.serial)) HAVING cnt > 1 ORDER BY cnt DESC LIMIT 200",
        params).fetchall()
    out = []
    for r in rows:
        ids = [int(x) for x in (r["ids"] or "").split(",") if x]
        nos = (r["nos"] or "").split(",")
        sts = (r["statuses"] or "").split(",")
        out.append({
            "serial": r["serial"], "maker": r["maker"], "model": r["model"], "count": r["cnt"],
            "assets": [{"id": i, "assetNo": n, "status": s,
                        "statusLabel": ASSET_STATUSES.get(s, s)}
                       for i, n, s in zip(ids, nos, sts)],
        })
    return jsonify({"groups": out, "count": len(out)})


# ---------------------------------------------------------------- 자산 단위 매입취소
#   (2026-08-24 대표 지시) 전표를 통째로 무르지 않고, 전표 안에서 취소할 제품만 고른다.
#   ★상태 전이는 전표 취소와 똑같이 한다 — '이전상태'를 이력에 남겨 되돌릴 때 복원한다.
#     이게 없으면 셋팅을 다 끝낸 자산이 되살아날 때 '입고'로 뭉개진다.


@bp.post("/assets/purchase-cancel")
def assets_purchase_cancel():
    """고른 자산만 매입취소. body: {ids:[...], reason}

    취소된 자산은 재고·전표 금액 합계에서 빠진다(전표 상세가 cancelled 를 이미 제외한다).
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        abort(400, description="취소할 자산을 고르세요.")
    if len(ids) > 500:
        abort(400, description="한 번에 500대까지만 취소할 수 있습니다.")
    reason = (body.get("reason") or "").strip()
    if not reason:
        abort(400, description="왜 취소하는지 사유를 적어 주세요 — 나중에 이 기록만 남습니다.")
    ts = config.now_iso()
    done, skipped = [], []
    with tx(write=True) as conn:
        for raw in ids:
            aid = _int_or_400(raw, "자산 ID")
            a = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            if a is None:
                skipped.append({"id": aid, "reason": "존재하지 않는 자산"})
                continue
            _check_asset_scope(conn, a["category_id"])
            if a["status"] == "cancelled":
                skipped.append({"id": aid, "assetNo": a["asset_no"], "reason": "이미 매입취소"})
                continue
            # ★나갔거나 회수 중인 물건은 무를 수 없다 — 전표 취소와 같은 잣대다.
            if a["status"] not in _CANCELABLE_ASSET:
                skipped.append({"id": aid, "assetNo": a["asset_no"],
                                "reason": ASSET_STATUSES.get(a["status"], a["status"])
                                          + " 상태라 취소할 수 없습니다"})
                continue
            # 살아 있는 주문이 잡고 있으면 상태가 판매가능이어도 무르면 안 된다
            held = conn.execute(
                "SELECT o.id, o.recipient FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
                "WHERE oa.asset_id=? AND o.cancelled_at='' AND o.archived_at='' LIMIT 1",
                (aid,)).fetchone()
            if held:
                skipped.append({"id": aid, "assetNo": a["asset_no"],
                                "reason": f"주문 #{held['id']} {held['recipient']}에 배정됨"})
                continue
            conn.execute("UPDATE assets SET status='cancelled', updated_at=? WHERE id=?",
                         (ts, aid))
            asset_event(conn, aid, "매입취소",
                        {"사유": reason, "이전상태": a["status"], "방식": "자산 단위"})
            # 매입취소분이 몰 재고에 남으면 유령 재고가 된다
            if a["stock_listed"]:
                conn.execute("UPDATE assets SET stock_listed=0, stock_listed_at='', "
                             "stock_listed_by='' WHERE id=?", (aid,))
                asset_event(conn, aid, "재고해제", {"사유": "매입취소"})
            done.append(a["asset_no"])
        if done:
            audit.log("assets_purchase_cancelled", target=f"{len(done)}대",
                      detail={"reason": reason, "assetNos": done[:20],
                              "skipped": len(skipped)})
    return jsonify({"ok": len(done), "done": done, "skipped": skipped})


@bp.post("/assets/purchase-uncancel")
def assets_purchase_uncancel():
    """매입취소 되돌리기 — 취소 직전 상태로 복원한다. body: {ids:[...]}"""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        abort(400, description="되돌릴 자산을 고르세요.")
    ts = config.now_iso()
    done, skipped = [], []
    with tx(write=True) as conn:
        for raw in ids:
            aid = _int_or_400(raw, "자산 ID")
            a = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            if a is None or a["status"] != "cancelled":
                skipped.append({"id": aid, "reason": "매입취소 상태가 아닙니다"})
                continue
            _check_asset_scope(conn, a["category_id"])
            prev = conn.execute(
                "SELECT detail FROM asset_events WHERE asset_id=? AND action='매입취소' "
                "ORDER BY id DESC LIMIT 1", (aid,)).fetchone()
            back = "in_stock"
            if prev and prev["detail"]:
                try:
                    back = json.loads(prev["detail"]).get("이전상태") or "in_stock"
                except (ValueError, TypeError):
                    back = "in_stock"
            if back not in ASSET_STATUSES or back == "cancelled":
                back = "in_stock"
            conn.execute("UPDATE assets SET status=?, updated_at=? WHERE id=?", (back, ts, aid))
            asset_event(conn, aid, "매입취소해제", {"복원상태": back})
            done.append(a["asset_no"])
        if done:
            audit.log("assets_purchase_uncancelled", target=f"{len(done)}대",
                      detail={"assetNos": done[:20]})
    return jsonify({"ok": len(done), "done": done, "skipped": skipped})


# ---------------------------------------------------------------- 자산번호 정정
#   (2026-08-18 대표 지시) TMS에서 번호를 잘못 적어 판매로 찍힌 탓에, 실물이 멀쩡한데
#   OWS가 "이미 출고된 자산"이라며 막는 일이 생긴다. 번호를 맞바꾸거나 넘겨받아
#   바로잡되, ★TMS가 2시간마다 같은 값을 다시 보내므로 잠금(tms_lock)을 함께 건다.
#   안 걸면 다음 수집에 상태가 되돌아가고, 비워진 옛 번호로 유령 자산이 새로 생긴다.

_FIX_SUFFIX = "-오류"          # 번호를 잃는 쪽이 받을 임시 번호 꼬리표


def _free_asset_no(conn, base):
    """base 를 바탕으로 아직 안 쓰는 번호를 만든다(base-오류1, -오류2 …)."""
    for n in range(1, 100):
        cand = f"{base}{_FIX_SUFFIX}{n}"[-30:]
        if conn.execute("SELECT id FROM assets WHERE asset_no=?", (cand,)).fetchone() is None:
            return cand
    abort(409, description=f"{base} 뒤에 붙일 임시 번호가 동났습니다. 번호를 직접 정해 주세요.")


def _no_live_waybill(conn, aid, asset_no):
    """송장이 이미 나간 자산의 번호는 바꾸지 않는다 — 라벨에 그 번호가 찍혀 나갔다."""
    wb = conn.execute(
        "SELECT w.wid, w.invoice_no FROM waybills w "
        "JOIN order_assets oa ON oa.order_id = w.order_id "
        "WHERE oa.asset_id=? AND w.type='forward' AND w.status IN ('issued','test','pending') "
        "LIMIT 1", (aid,)).fetchone()
    if wb:
        abort(409, description=(
            f"{asset_no}은(는) 송장({wb['invoice_no'] or wb['wid']})이 이미 발급된 자산입니다. "
            "라벨에 이 번호가 찍혀 나갔으니, 배송/송장 화면에서 송장을 취소한 뒤 바로잡으세요."))


def _lock_tms(conn, aid, note):
    conn.execute("UPDATE assets SET tms_lock=1, tms_lock_note=?, updated_at=? WHERE id=?",
                 (note[:200], config.now_iso(), aid))


def _record_fix(conn, asset_no, aid, kind, reason):
    conn.execute(
        "INSERT INTO tms_number_fixes(asset_no, asset_id, kind, reason, created_by, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (asset_no, aid, kind, reason[:300], g.user["username"], config.now_iso()))


def _conflict_is_open(conn, aid):
    """번호 충돌이 아직 열려 있나 — 복귀 후보와 같은 방식(최신 이벤트로 판정)."""
    r = conn.execute(
        "SELECT action FROM asset_events WHERE asset_id=? "
        "AND action IN ('번호충돌','번호충돌해결') ORDER BY id DESC LIMIT 1", (aid,)).fetchone()
    return bool(r and r["action"] == "번호충돌")


@bp.post("/assets/number-fix")
def asset_number_fix():
    """자산번호 맞바꾸기(swap) / 넘겨받기(take).

    body: {assetId, targetNo, mode:"swap"|"take", moveToNo?, reason}
      · swap — 내 번호와 상대 번호를 통째로 맞바꾼다(두 실물이 서로 뒤바뀐 경우).
      · take — 상대가 쓰던 번호를 내가 넘겨받고, 상대는 임시 번호로 물러난다
               (상대가 TMS 오입력으로 생긴 잘못된 기록인 경우).
    ★두 자산 모두 TMS 자동반영을 잠근다 — 안 잠그면 다음 수집(2시간)에 되돌아간다.
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    aid = _int_or_400(body.get("assetId"), "자산 ID")
    mode = (body.get("mode") or "take").strip()
    if mode not in ("swap", "take"):
        abort(400, description="mode는 swap(맞바꾸기) 또는 take(넘겨받기)만 됩니다.")
    target_no = (body.get("targetNo") or "").strip()
    if not target_no or len(target_no) > 30:
        abort(400, description="넘겨받을 관리번호를 1~30자로 적어 주세요.")
    reason = (body.get("reason") or "").strip()
    if not reason:
        abort(400, description="왜 바로잡는지 사유를 적어 주세요 — 나중에 이 기록만 남습니다.")
    # ★새로 들어오는 번호(빈 번호 넘겨받기·상대가 옮겨 갈 번호)의 연동 검증은 tx 밖에서(2026-09-03, A4)
    numbering.precheck_nos([target_no, (body.get("moveToNo") or "").strip()])

    with tx(write=True) as conn:
        me = _get_asset_or_404(conn, aid)
        _check_asset_scope(conn, me["category_id"])
        my_no = me["asset_no"]
        if target_no == my_no:
            abort(400, description="지금 쓰는 번호와 같습니다.")
        other = conn.execute("SELECT * FROM assets WHERE asset_no=?", (target_no,)).fetchone()
        ts = config.now_iso()

        if other is None:
            if mode == "swap":
                abort(404, description=f"{target_no}를 쓰는 자산이 없어 맞바꿀 수 없습니다.")
            _no_live_waybill(conn, aid, my_no)
            unverified = numbering.validate_manual_no(conn, target_no)   # ★A4 — OWS에 없는 번호를 새로 붙인다
            conn.execute("UPDATE assets SET asset_no=?, updated_at=? WHERE id=?",
                         (target_no, ts, aid))
            asset_event(conn, aid, "번호정정",
                        {"from": my_no, "to": target_no,
                         "방식": "넘겨받기(빈 번호)", "사유": reason})
            if unverified:
                numbering.note_unverified(conn, aid, target_no, unverified)
            _lock_tms(conn, aid, f"번호정정: {my_no} → {target_no} / {reason}")
            _record_fix(conn, target_no, aid, "take", reason)
            _record_fix(conn, my_no, None, "release", reason)
            if _conflict_is_open(conn, aid):
                asset_event(conn, aid, "번호충돌해결", {"처리": "넘겨받기", "사유": reason})
            audit.log("asset_number_fix", target=f"{my_no} → {target_no}",
                      detail={"mode": "take-empty", "reason": reason})
            return jsonify({"ok": True, "mode": "take", "myNo": target_no, "otherNo": None,
                            "message": f"{my_no} → {target_no}로 바로잡았습니다."})

        _check_asset_scope(conn, other["category_id"])
        if other["id"] == aid:
            abort(400, description="같은 자산입니다.")
        _no_live_waybill(conn, aid, my_no)
        _no_live_waybill(conn, other["id"], target_no)

        # 물러날 번호를 먼저 정한다 — 쓰기 전에 검증을 끝낸다(원칙: 부분 반영 금지)
        unverified_other = None
        if mode == "swap":
            other_new = my_no
        else:
            other_new = (body.get("moveToNo") or "").strip() or _free_asset_no(conn, target_no)
            if len(other_new) > 30:
                abort(400, description="옮겨 갈 관리번호는 30자 이내여야 합니다.")
            if other_new in (my_no, target_no):
                abort(400, description="옮겨 갈 번호가 지금 쓰는 번호와 겹칩니다.")
            if conn.execute("SELECT id FROM assets WHERE asset_no=? AND id != ?",
                            (other_new, other["id"])).fetchone():
                abort(409, description=f"{other_new}는 이미 쓰이고 있습니다. 다른 번호를 적어 주세요.")
            # ★A4 — 사람이 적은 번호만 형식에 걸린다(자동 '-오류N' 은 형식 밖이라 그대로 통과)
            unverified_other = numbering.validate_manual_no(conn, other_new)

        # ★UNIQUE(asset_no) 때문에 곧바로 맞바꿀 수 없다 — 임시 번호를 한 번 거친다.
        tmp = _free_asset_no(conn, "TMP")
        conn.execute("UPDATE assets SET asset_no=?, updated_at=? WHERE id=?", (tmp, ts, other["id"]))
        conn.execute("UPDATE assets SET asset_no=?, updated_at=? WHERE id=?", (target_no, ts, aid))
        conn.execute("UPDATE assets SET asset_no=?, updated_at=? WHERE id=?",
                     (other_new, ts, other["id"]))

        label = "맞바꾸기" if mode == "swap" else "넘겨받기"
        asset_event(conn, aid, "번호정정",
                    {"from": my_no, "to": target_no, "방식": label,
                     "상대": f"{target_no} → {other_new}", "사유": reason})
        asset_event(conn, other["id"], "번호정정",
                    {"from": target_no, "to": other_new, "방식": label,
                     "상대": f"{my_no} → {target_no}", "사유": reason})
        if unverified_other:
            numbering.note_unverified(conn, other["id"], other_new, unverified_other)
        note = f"번호정정({label}): {my_no} / {target_no} — {reason}"
        _lock_tms(conn, aid, note)
        _lock_tms(conn, other["id"], note)
        _record_fix(conn, target_no, aid, mode, reason)
        _record_fix(conn, my_no, other["id"] if mode == "swap" else None, mode, reason)
        for x in (aid, other["id"]):
            if _conflict_is_open(conn, x):
                asset_event(conn, x, "번호충돌해결", {"처리": label, "사유": reason})
        audit.log("asset_number_fix", target=f"{my_no} / {target_no}",
                  detail={"mode": mode, "reason": reason, "otherNo": other_new})
        return jsonify({"ok": True, "mode": mode, "myNo": target_no, "otherNo": other_new,
                        "message": (f"{my_no} ↔ {target_no} 맞바꿨습니다." if mode == "swap"
                                    else f"{target_no}를 넘겨받고, 쓰던 자산은 "
                                         f"{other_new}로 옮겼습니다.")})


# ---------------------------------------------------------------- 번호 충돌 대기열
#   셋팅/QC에서 "TMS가 판매로 찍었지만 실물이 여기 있다"며 강제 매칭한 자산이 쌓인다.
#   판정은 복귀 후보와 같은 방식 — 자산 이력의 최신 이벤트로 열림/닫힘을 본다.


@bp.get("/assets/number-conflicts")
def asset_number_conflicts():
    """번호 충돌 대기열 — 강제 매칭한 채 아직 안 바로잡은 자산."""
    require("purchase.view")
    clause, params = scope_clause("a")
    rows = get_db().execute(
        "SELECT a.*, c.name AS category_name, e.ts AS flagged_at, e.detail AS flag_detail "
        "FROM asset_events e "
        "JOIN (SELECT asset_id, MAX(id) AS mid FROM asset_events "
        "      WHERE action IN ('번호충돌','번호충돌해결') GROUP BY asset_id) m ON m.mid = e.id "
        "JOIN assets a ON a.id = e.asset_id "
        "LEFT JOIN categories c ON c.id = a.category_id "
        "WHERE e.action='번호충돌'" + clause + " ORDER BY e.id DESC LIMIT 300", params).fetchall()
    out = []
    for r in rows:
        try:
            d = json.loads(r["flag_detail"]) if r["flag_detail"] else {}
        except (ValueError, TypeError):
            d = {}
        out.append(_asset_payload(r, {
            "categoryName": r["category_name"], "flaggedAt": r["flagged_at"],
            "wasStatus": d.get("당시상태", ""), "orderNo": d.get("주문", ""),
            "flagBy": d.get("작업자", "")}))
    return jsonify({"rows": out, "count": len(out)})


@bp.post("/assets/<int:aid>/number-conflict/close")
def asset_number_conflict_close(aid):
    """번호를 안 바꾸고 '확인했다'로 닫는다 — TMS 쪽에서 고치기로 한 경우."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    with tx(write=True) as conn:
        row = _get_asset_or_404(conn, aid)
        _check_asset_scope(conn, row["category_id"])
        if not _conflict_is_open(conn, aid):
            abort(400, description="열려 있는 번호 충돌이 없습니다.")
        asset_event(conn, aid, "번호충돌해결",
                    {"처리": "확인만", "사유": (body.get("reason") or "").strip()[:200]})
        audit.log("asset_conflict_close", target=row["asset_no"], detail={})
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 자산 단위 판매(TMS)
#   (2026-08-25 대표) 판매현황.xlsx 원장을 자산번호로 OWS에 붙인다 — 자산별 마진.
#   OWS 원가 = 자산 매입가 + 수리비(자산이 매칭된 경우), 아니면 TMS 매입가.
#   주문은 지어내지 않는다 — order_assets 에 있으면 '주문연결' 표시만.


@bp.get("/sale-assets")
def sale_assets():
    require_any("settings.manage", "reports.view", "purchase.money")
    conn = get_db()
    frm = (request.args.get("from") or "").strip()
    to = (request.args.get("to") or "").strip()
    q = (request.args.get("q") or "").strip()
    include_cancelled = request.args.get("includeCancelled") in ("1", "true")
    where, args = [], []
    if frm:
        where.append("substr(t.sale_date,1,10) >= ?"); args.append(frm)
    if to:
        where.append("substr(t.sale_date,1,10) <= ?"); args.append(to)
    if q:
        like = f"%{q}%"
        where.append("(t.asset_no LIKE ? OR t.customer LIKE ? OR t.model LIKE ? "
                     "OR t.slip_no LIKE ? OR t.channel LIKE ?)")
        args += [like] * 5
    if not include_cancelled:
        where.append("t.stage NOT LIKE '%취소%'")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        "SELECT t.*, a.purchase_price AS ows_price, a.status AS ows_status, "
        "       (SELECT COALESCE(SUM(r.cost),0) FROM asset_repairs r WHERE r.asset_id=t.asset_id) "
        "         AS repair_sum, "
        "       EXISTS(SELECT 1 FROM order_assets oa WHERE oa.asset_id=t.asset_id) AS has_order "
        "FROM tms_sales t LEFT JOIN assets a ON a.id=t.asset_id"
        + w + " ORDER BY t.sale_date DESC, t.id DESC LIMIT 1000", args).fetchall()
    total = conn.execute("SELECT COUNT(*) AS c FROM tms_sales t" + w, args).fetchone()["c"]
    items, sale_sum, cost_sum, zero_cnt = [], 0, 0, 0
    for r in rows:
        # OWS 원가: 자산이 있고 매입가가 있으면 그걸(수리비 포함), 아니면 TMS 매입가
        if r["asset_id"] is not None and (r["ows_price"] or 0) > 0:
            cost = (r["ows_price"] or 0) + (r["repair_sum"] or 0)
            cost_src = "ows"
        else:
            cost = r["purchase_price"] or 0
            cost_src = "tms"
        margin = (r["sale_price"] or 0) - cost
        # ★TMS엔 판매가 0으로 두고 금액을 판매등록 칸에만 적은 행이 있다 — 합계에 넣으면
        #   마진이 통째로 음수로 왜곡된다. 합계에서 빼고 개수만 따로 센다(행은 그대로 보인다).
        if (r["sale_price"] or 0) > 0:
            sale_sum += r["sale_price"] or 0
            cost_sum += cost
        else:
            zero_cnt += 1
        items.append({
            "id": r["id"], "slipNo": r["slip_no"], "assetNo": r["asset_no"],
            "assetId": r["asset_id"], "customer": r["customer"], "channel": r["channel"],
            "model": r["model"], "saleDate": (r["sale_date"] or "")[:10],
            "salePrice": r["sale_price"], "cost": cost, "costSrc": cost_src,
            "margin": margin, "tmsProfit": r["tms_profit"], "grade": r["grade"],
            "stage": r["stage"], "hasOrder": bool(r["has_order"]),
        })
    # 연결된 OWS 주문 실체(대표 2026-08-25 "자산번호로 매출을 매칭") — 표시 행만 조회.
    #   자산이 여러 주문을 거쳤으면(재판매) 판매일에 가장 가까운 주문을 고른다.
    aids = [x["assetId"] for x in items if x["assetId"] is not None]
    if aids:
        marks = ",".join("?" * len(aids))
        by_asset = {}
        for r in conn.execute(
                f"SELECT oa.asset_id, o.id, o.order_no, o.amount, o.quantity, o.ordered_at "
                f"FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
                f"WHERE oa.asset_id IN ({marks}) AND o.cancelled_at=''", aids).fetchall():
            by_asset.setdefault(r["asset_id"], []).append(r)
        for x in items:
            cands = by_asset.get(x["assetId"])
            if not cands:
                continue
            sd = x["saleDate"] or ""
            best = min(cands, key=lambda r: _date_gap(str(r["ordered_at"] or "")[:10], sd))
            x["order"] = {"id": best["id"], "no": best["order_no"],
                          "amount": best["amount"], "qty": best["quantity"]}
    matched = conn.execute(
        "SELECT COUNT(*) AS c FROM tms_sales WHERE asset_id IS NOT NULL").fetchone()["c"]
    all_cnt = conn.execute("SELECT COUNT(*) AS c FROM tms_sales").fetchone()["c"]
    return jsonify({"items": items, "total": total, "shown": len(items),
                    "saleSum": sale_sum, "costSum": cost_sum, "marginSum": sale_sum - cost_sum,
                    "zeroPriceCount": zero_cnt,
                    "matchedAll": matched, "totalAll": all_cnt})


@bp.post("/sale-assets/backfill")
def sale_assets_backfill():
    """tms-export 폴더의 판매현황 엑셀을 즉시 다시 읽는다(2시간 자동 주기를 안 기다리고)."""
    require("purchase.edit")
    from ..importers import read_first_sheet
    from .autosync import EXPORT_DIR
    from .sales import is_sale_asset_sheet, upsert_asset_sales
    files = sorted(EXPORT_DIR.glob("판매현황*.xlsx"),
                   key=lambda x: x.stat().st_mtime, reverse=True) if EXPORT_DIR.exists() else []
    if not files:
        abort(404, description="tms-export 폴더에 판매현황 엑셀이 없습니다.")
    done = []
    with tx(write=True) as conn:
        # ★최신 파일 '하나만' 읽는다 — 폴더에 옛 스냅샷(판매현황260730 등)이 같이 있어서
        #   여러 개를 돌면 마지막(오래된) 파일이 최신 값을 되덮는다(e2e 실측: 172건 역행).
        for f in files:
            try:
                rows = read_first_sheet(f.read_bytes(), f.name)
            except Exception:                                        # noqa: BLE001
                continue
            if not is_sale_asset_sheet(rows):
                continue
            res = upsert_asset_sales(conn, rows, actor=g.user["display_name"])
            done.append({"file": f.name, **res})
            break
        if done:
            audit.log("sale_assets_backfill", target=done[0]["file"], detail=done[0])
    if not done:
        abort(400, description="판매현황 판형(판매전표+관리번호+수령자성함)이 아닙니다.")
    return jsonify({"ok": True, "results": done})


# ---------------------------------------------------------------- 부품 소진 재고
#   (2026-08-25 대표) 램·SSD를 수량으로 — 매입하면 쌓이고, 옵션 칩/수동 차감으로 나간다.
#   현재고 = SUM(part_stock_moves.qty). 대기 소요 = 보드의 미출고 주문 중 아직 체크
#   안 된 부품 연결 옵션의 소요량(제품코드 스펙으로 부품 확정) — 가용 = 현재고 − 대기.


def _date_gap(d1, d2):
    """YYYY-MM-DD 두 날짜의 대략적 간격(일) — 재판매 자산에서 판매일에 가까운 주문 고르기용."""
    try:
        from datetime import date
        a = date.fromisoformat(d1)
        b = date.fromisoformat(d2)
        return abs((a - b).days)
    except (ValueError, TypeError):
        return 9999


def _part_onhand(conn, pid):
    r = conn.execute("SELECT COALESCE(SUM(qty),0) AS q FROM part_stock_moves WHERE part_id=?",
                     (pid,)).fetchone()
    return r["q"] or 0


@bp.get("/part-stocks")
def part_stocks():
    """부품별 현재고/대기/가용. 셋팅 보드 상단 바와 단가표 재고 열이 쓴다."""
    require_any("purchase.view", "orders.view", "setup.view")
    conn = get_db()
    onhand = {r["part_id"]: r["q"] for r in conn.execute(
        "SELECT part_id, COALESCE(SUM(qty),0) AS q FROM part_stock_moves "
        "GROUP BY part_id").fetchall()}
    # 대기 소요 — 아직 체크 안 된 부품 연결 옵션(제품코드 스펙으로 부품 확정).
    #   판매 구성 칩은 자산 실물이 있어야 계산돼 여기선 못 센다(체크 순간 정확히 차감된다).
    pending, unresolved = {}, 0
    try:
        from ..prep import load_rules, match_option_ids
        # ★load_rules 의 옵션 행은 가벼운 컬럼(id·name·note·kind)뿐 — 부품 연결 칸은
        #   여기서 직접 읽는다. match_option_ids 는 id만 쓰므로 전체 행을 넘겨도 된다.
        _light, by_option = load_rules(conn)
        linked = conn.execute(
            "SELECT * FROM prep_options WHERE enabled=1 AND (part_id IS NOT NULL "
            "OR TRIM(COALESCE(part_map,'')) != '')").fetchall()
        if linked:
            checked = {(r["order_id"], r["option_id"]) for r in conn.execute(
                "SELECT order_id, option_id FROM order_option_checks").fetchall()}
            actives = conn.execute(
                "SELECT * FROM orders WHERE cancelled_at='' AND archived_at='' "
                "AND shipping_done=0").fetchall()
            for row in actives:
                hit = match_option_ids(linked, by_option, row)
                for opt in linked:
                    if opt["id"] not in hit or (row["id"], opt["id"]) in checked:
                        continue
                    part, _why = resolve_option_part(conn, opt, row)
                    if part is None:
                        unresolved += 1
                        continue
                    need = (opt["part_qty"] or 1) * max(1, row["quantity"] or 1)
                    pending[part["id"]] = pending.get(part["id"], 0) + need
    except Exception:                                                # noqa: BLE001
        # 대기 계산이 죽어도 현재고는 보여 준다 — 보드가 이 API에 얹혀 있다
        pending, unresolved = {}, -1
    items = []
    for r in conn.execute(
            "SELECT * FROM parts WHERE COALESCE(kind,'part')='part' "
            "ORDER BY category DESC, grp, sort, name").fetchall():
        oh = onhand.get(r["id"], 0)
        pd = pending.get(r["id"], 0)
        if not r["enabled"] and not oh and not pd:
            continue
        items.append({"id": r["id"], "name": r["name"], "category": r["category"],
                      "group": r["grp"], "price": r["price"], "enabled": bool(r["enabled"]),
                      "onhand": oh, "pending": pd, "available": oh - pd})
    return jsonify({"items": items, "pendingUnresolved": unresolved})


@bp.get("/parts/<int:pid>/stock-moves")
def part_stock_moves_list(pid):
    require("purchase.view")
    conn = get_db()
    if conn.execute("SELECT id FROM parts WHERE id=?", (pid,)).fetchone() is None:
        abort(404, description="부품을 찾을 수 없습니다.")
    rows = conn.execute(
        "SELECT m.*, COALESCE(rep.name, s.name) AS supplier_name, a.asset_no, b.slip_no "
        "FROM part_stock_moves m "
        "LEFT JOIN suppliers s ON s.id=m.supplier_id "
        "LEFT JOIN suppliers rep ON rep.id=s.alias_of "
        "LEFT JOIN assets a ON a.id=m.asset_id "
        "LEFT JOIN purchase_batches b ON b.id=m.batch_id "
        "WHERE m.part_id=? ORDER BY m.id DESC LIMIT 300", (pid,)).fetchall()
    return jsonify({"onhand": _part_onhand(conn, pid), "moves": [
        {"id": r["id"], "qty": r["qty"], "unitCost": r["unit_cost"], "amount": r["amount"],
         "reason": r["reason"], "date": r["move_date"], "supplier": r["supplier_name"] or "",
         "slipNo": r["slip_no"] or "", "assetNo": r["asset_no"] or "",
         "orderId": r["order_id"], "memo": r["memo"], "by": r["created_by"]}
        for r in rows]})


@bp.post("/parts/<int:pid>/stock-adjust")
def part_stock_adjust(pid):
    """재고 실사 보정(±). 매입도 차감도 아닌 '세어 보니 다르다'용."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    try:
        qty = int(body.get("qty") or 0)
    except (TypeError, ValueError):
        abort(400, description="수량을 숫자로 입력하세요.")
    if qty == 0:
        abort(400, description="0은 보정할 것이 없습니다.")
    with tx(write=True) as conn:
        part = conn.execute("SELECT * FROM parts WHERE id=?", (pid,)).fetchone()
        if part is None:
            abort(404, description="부품을 찾을 수 없습니다.")
        if qty < 0 and _part_onhand(conn, pid) + qty < 0:
            abort(400, description="현재고보다 많이 뺄 수 없습니다.")
        conn.execute(
            "INSERT INTO part_stock_moves(part_id, qty, reason, move_date, memo, "
            "created_by, created_at) VALUES(?,?,?,?,?,?,?)",
            (pid, qty, "adjust", config.now().strftime("%Y-%m-%d"),
             (body.get("memo") or "").strip()[:120], g.user["display_name"], config.now_iso()))
        audit.log("part_stock_adjust", target=part["name"], detail={"qty": qty})
        return jsonify({"ok": True, "onhand": _part_onhand(conn, pid)})


@bp.post("/part-purchases")
def part_purchase():
    """부품 매입등록 — 거래처·입고일·부품별 수량/금액.

    전표(purchase_type='부품매입')를 만들어 매입 실적·미지급금에 그대로 잡고,
    행마다 +입고 이동을 남기며, ★단가표 단가를 최신 매입 단가로 갱신한다
    (옵션 칩이 자산에 기입하는 '오늘 단가'의 출처가 단가표라서 — 그날 단가 원칙).
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    rows = body.get("rows")
    if not isinstance(rows, list) or not rows:
        abort(400, description="매입할 부품 줄을 넣어 주세요.")
    if len(rows) > 50:
        abort(400, description="한 번에 50줄까지입니다.")
    date = (body.get("date") or "").strip() or config.now().strftime("%Y-%m-%d")
    with tx(write=True) as conn:
        # 거래처 — id 또는 이름(없으면 생성: 자산 매입과 같은 규칙)
        sid = body.get("supplierId")
        sname = (body.get("supplierName") or "").strip()
        if not sid and sname:
            hit = conn.execute("SELECT id FROM suppliers WHERE name=?", (sname,)).fetchone()
            sid = hit["id"] if hit else conn.execute(
                "INSERT INTO suppliers(name, created_at) VALUES(?,?)",
                (sname, config.now_iso())).lastrowid
        if not sid:
            abort(400, description="거래처를 지정하세요.")
        if conn.execute("SELECT id FROM suppliers WHERE id=?", (sid,)).fetchone() is None:
            abort(400, description="거래처를 찾을 수 없습니다.")
        parsed, total = [], 0
        for n, r in enumerate(rows, start=1):
            try:
                pid = int(r.get("partId") or 0)
                qty = int(r.get("qty") or 0)
                amount = int(str(r.get("amount") or "0").replace(",", ""))
            except (TypeError, ValueError, AttributeError):
                abort(400, description=f"{n}번째 줄 값이 올바르지 않습니다.")
            part = conn.execute("SELECT * FROM parts WHERE id=?", (pid,)).fetchone()
            if part is None:
                abort(400, description=f"{n}번째 줄: 단가표에 없는 부품입니다.")
            if (part["kind"] or "part") != "part":
                abort(400, description=f"{n}번째 줄: '{part['name']}'은 공임 항목이라 "
                                       "수량 재고가 없습니다.")
            if qty <= 0:
                abort(400, description=f"{n}번째 줄: 수량은 1 이상이어야 합니다.")
            if amount < 0:
                abort(400, description=f"{n}번째 줄: 금액이 음수입니다.")
            parsed.append((part, qty, amount))
            total += amount
        ts = config.now_iso()
        slip_no = next_slip_no(conn, "purchased")
        memo_lines = ", ".join(f"{p['name']} x{q}" for p, q, _a in parsed)
        conn.execute(
            "INSERT INTO purchase_batches(slip_no, stage, supplier_id, purchase_date, "
            "total_amount, memo, purchase_type, paid, created_by, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (slip_no, "purchased", sid, date, total,
             ("📦 부품 매입: " + memo_lines)[:300], "부품매입",
             0 if body.get("unpaid") else 1, g.user["display_name"], ts, ts))
        bid = conn.execute("SELECT id FROM purchase_batches WHERE slip_no=?",
                           (slip_no,)).fetchone()["id"]
        out = []
        for part, qty, amount in parsed:
            unit = round(amount / qty) if amount else 0
            conn.execute(
                "INSERT INTO part_stock_moves(part_id, qty, unit_cost, amount, supplier_id, "
                "batch_id, reason, move_date, created_by, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (part["id"], qty, unit, amount, sid, bid, "purchase", date,
                 g.user["display_name"], ts))
            if unit > 0 and unit != part["price"]:
                conn.execute("UPDATE parts SET price=?, updated_at=?, updated_by=? WHERE id=?",
                             (unit, ts, g.user["display_name"], part["id"]))
            out.append({"partId": part["id"], "name": part["name"], "qty": qty,
                        "amount": amount, "unitCost": unit,
                        "onhand": _part_onhand(conn, part["id"])})
        audit.log("part_purchase", target=slip_no,
                  detail={"supplier": sid, "total": total,
                          "rows": [(o["name"], o["qty"]) for o in out]})
        return jsonify({"ok": True, "batchId": bid, "slipNo": slip_no,
                        "total": total, "rows": out}), 201


@bp.post("/orders/<int:oid>/part-use")
def order_part_use(oid):
    """셋팅 상세의 수동 차감 — 이 주문의 매칭 자산에 부품을 장착(−재고 +원가)하거나
    회수(+재고 −원가)한다. 옵션 칩과 같은 관문(_apply_part_to_asset)이라 규칙이 같다."""
    require_any("orders.work", "orders.edit")
    body = request.get_json(silent=True) or {}
    remove = bool(body.get("remove"))
    try:
        pid = int(body.get("partId") or 0)
        aid = int(body.get("assetId") or 0)
        qty = max(1, int(body.get("qty") or 1))
    except (TypeError, ValueError):
        abort(400, description="부품/자산/수량 값이 올바르지 않습니다.")
    if qty > 10:
        abort(400, description="한 번에 10개까지입니다.")
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if row is None:
            abort(404, description="주문을 찾을 수 없습니다.")
        if row["cancelled_at"] or row["archived_at"]:
            abort(409, description="취소/보관된 주문입니다.")
        part = conn.execute("SELECT * FROM parts WHERE id=? AND enabled=1", (pid,)).fetchone()
        if part is None:
            abort(404, description="부품을 찾을 수 없습니다.")
        if (part["kind"] or "part") != "part":
            abort(400, description="공임 항목은 수량 차감이 없습니다 — 자산 상세의 수리 기록을 쓰세요.")
        if not part["price"]:
            abort(400, description=f"'{part['name']}' 단가가 0원입니다 — 부품 매입을 먼저 "
                                   "등록하거나 단가표에 단가를 넣으세요.")
        # ── 자산 매칭 전 차감(2026-08-26 대표 "제품코드를 입력하지 못한 경우에도") ──
        #   재고는 지금 움직이고(자리표, asset NULL), 원가는 매칭되는 순간 자동 이관된다
        #   (apply_pending_part_uses — 옵션 칩의 '매칭되면 자동 기입'과 같은 결).
        if not aid:
            if conn.execute("SELECT 1 FROM order_assets WHERE order_id=? LIMIT 1",
                            (oid,)).fetchone():
                abort(400, description="이 주문에는 이미 매칭된 자산이 있습니다 — 자산을 골라 주세요.")
            conn.execute(
                "INSERT INTO part_stock_moves(part_id, qty, unit_cost, order_id, reason, "
                "move_date, memo, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (pid, qty if remove else -qty, part["price"], oid,
                 "return" if remove else "use", config.now().strftime("%Y-%m-%d"),
                 "자산 매칭 대기 — 매칭되면 원가 자동 기입", g.user["display_name"],
                 config.now_iso()))
            audit.log("part_use_manual", target=f"주문 #{oid}(매칭 전)",
                      detail={"part": part["name"], "qty": qty, "remove": remove})
            return jsonify({"ok": True, "cost": 0, "pending": True, "name": part["name"],
                            "onhand": _part_onhand(conn, pid)})
        if conn.execute("SELECT 1 FROM order_assets WHERE order_id=? AND asset_id=?",
                        (oid, aid)).fetchone() is None:
            abort(400, description="이 주문에 매칭된 자산이 아닙니다.")
        asset = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
        if asset is None:
            abort(404, description="자산을 찾을 수 없습니다.")
        cost = _apply_part_to_asset(conn, asset, part, config.now().strftime("%Y-%m-%d"),
                                    g.user["display_name"], qty=qty, order_id=oid,
                                    remove=remove)
        audit.log("part_use_manual", target=asset["asset_no"],
                  detail={"part": part["name"], "qty": qty, "remove": remove, "order": oid})
        return jsonify({"ok": True, "cost": cost, "name": part["name"],
                        "onhand": _part_onhand(conn, pid)})


@bp.get("/orders/<int:oid>/part-uses")
def order_part_uses(oid):
    """이 주문의 부품 차감 현황 — 자동(옵션 칩)인지 수동인지 표시(대표 2026-08-25:
    "자동차감이면 자동차감됐다고 적어주고 아니면 수동차감할 수 있게")."""
    require_any("orders.view", "setup.view", "purchase.view")
    conn = get_db()
    rows = conn.execute(
        "SELECT m.qty, m.reason, p.name, a.asset_no, "
        "       (SELECT r.prep_option_id FROM asset_repairs r WHERE r.id=m.repair_id) AS opt "
        "FROM part_stock_moves m JOIN parts p ON p.id=m.part_id "
        "LEFT JOIN assets a ON a.id=m.asset_id "
        "WHERE m.order_id=? AND m.reason IN ('use','return') ORDER BY m.id", (oid,)).fetchall()
    return jsonify({"items": [
        {"name": r["name"], "qty": abs(r["qty"]), "remove": r["reason"] == "return",
         "auto": r["opt"] is not None, "assetNo": r["asset_no"] or ""} for r in rows]})


# ---------------------------------------------------------------- 매입 미지급금
#   (2026-08-25 대표) 거래처에 줄 돈 관리 — TMS의 최악 사용성을 대체한다.
#   미지급 = 매입 확정 전표 총액 − 지급 누계. 취소 전표는 뺀다.


@bp.get("/purchase/payables")
def payables():
    """거래처별 미지급 요약. ?onlyOwed=1 이면 잔액 남은 거래처만.

    반환: [{supplierId, supplier, batches, totalAmount, paidAmount, unpaid,
            oldestDate, batchList:[...]}]
    """
    require("purchase.view")
    require("purchase.money")           # 금액 화면(2026-08-27 대표 — 접근과 금액은 별도)
    conn = get_db()
    only_owed = request.args.get("onlyOwed") in ("1", "true")
    frm = (request.args.get("from") or "").strip()
    to = (request.args.get("to") or "").strip()
    where = "b.stage='purchased' AND b.cancelled_at=''"
    args = []
    if frm:
        where += " AND b.purchase_date >= ?"; args.append(frm)
    if to:
        where += " AND b.purchase_date <= ?"; args.append(to)
    rows = conn.execute(
        "SELECT b.id, b.slip_no, b.purchase_date, b.total_amount, b.paid_amount, b.paid, "
        "       b.supplier_id, COALESCE(rep.name, s.name, '(거래처 미지정)') AS supplier "
        "FROM purchase_batches b LEFT JOIN suppliers s ON s.id=b.supplier_id "
        "LEFT JOIN suppliers rep ON rep.id=s.alias_of "
        "WHERE " + where + " ORDER BY b.purchase_date, b.id", args).fetchall()
    by_sup = {}
    for r in rows:
        unpaid = max(0, (r["total_amount"] or 0) - (r["paid_amount"] or 0))
        key = r["supplier_id"] or 0
        g = by_sup.setdefault(key, {
            "supplierId": r["supplier_id"], "supplier": r["supplier"],
            "batches": 0, "totalAmount": 0, "paidAmount": 0, "unpaid": 0,
            "oldestUnpaidDate": "", "batchList": []})
        g["batches"] += 1
        g["totalAmount"] += r["total_amount"] or 0
        g["paidAmount"] += r["paid_amount"] or 0
        g["unpaid"] += unpaid
        if unpaid > 0 and not g["oldestUnpaidDate"]:
            g["oldestUnpaidDate"] = r["purchase_date"]
        g["batchList"].append({
            "id": r["id"], "slipNo": r["slip_no"], "date": r["purchase_date"],
            "total": r["total_amount"] or 0, "paid": r["paid_amount"] or 0,
            "unpaid": unpaid, "settled": bool(r["paid"]) and unpaid == 0})
    out = sorted(by_sup.values(), key=lambda x: -x["unpaid"])
    if only_owed:
        out = [g for g in out if g["unpaid"] > 0]
    return jsonify({
        "suppliers": out,
        "totalUnpaid": sum(g["unpaid"] for g in by_sup.values()),
        "totalPurchased": sum(g["totalAmount"] for g in by_sup.values()),
        "totalPaid": sum(g["paidAmount"] for g in by_sup.values()),
    })


@bp.get("/purchase-batches/<int:bid>/payments")
def batch_payments(bid):
    require("purchase.view")
    require("purchase.money")
    conn = get_db()
    _batch_or_404(conn, bid)
    rows = conn.execute(
        "SELECT * FROM purchase_payments WHERE batch_id=? ORDER BY id", (bid,)).fetchall()
    return jsonify({"payments": [
        {"id": r["id"], "amount": r["amount"], "date": r["pay_date"], "method": r["method"],
         "memo": r["memo"], "by": r["created_by"], "at": r["created_at"]} for r in rows]})


@bp.post("/purchase-batches/<int:bid>/pay")
def add_payment(bid):
    """지급 기록 — 전표에 얼마를 냈다. 누계가 총액에 닿으면 완납 표시.

    body: {amount, date?, method?, memo?}. amount 음수면 지급 취소(환불·정정)로 뺀다.
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    with tx(write=True) as conn:
        b = _batch_or_404(conn, bid)
        if b["cancelled_at"]:
            abort(400, description="취소된 전표입니다.")
        if b["stage"] != "purchased":
            abort(400, description="매입 확정된 전표에만 지급을 기록할 수 있습니다.")
        try:
            amount = int(str(body.get("amount") or "").replace(",", ""))
        except (TypeError, ValueError):
            abort(400, description="금액을 숫자로 입력하세요.")
        if amount == 0:
            abort(400, description="0원은 기록할 수 없습니다.")
        total = b["total_amount"] or 0
        cur_paid = b["paid_amount"] or 0
        new_paid = cur_paid + amount
        if new_paid < 0:
            abort(400, description="지급 누계가 음수가 됩니다 — 취소 금액이 너무 큽니다.")
        if amount > 0 and new_paid > total:
            abort(400, description=f"과지급입니다. 남은 미지급 {total - cur_paid:,}원까지만 됩니다.")
        now = config.now_iso()
        conn.execute(
            "INSERT INTO purchase_payments(batch_id, amount, pay_date, method, memo, "
            "created_by, created_at) VALUES(?,?,?,?,?,?,?)",
            (bid, amount, (body.get("date") or now[:10]).strip(),
             (body.get("method") or "").strip()[:40], (body.get("memo") or "").strip()[:120],
             g.user["display_name"], now))
        fully = new_paid >= total and total > 0
        conn.execute(
            "UPDATE purchase_batches SET paid_amount=?, paid=?, paid_at=?, updated_at=? WHERE id=?",
            (new_paid, 1 if fully else 0, now[:10] if fully else (b["paid_at"] or ""), now, bid))
        audit.log("purchase_payment", target=b["slip_no"] or f"#{bid}",
                  detail={"amount": amount, "paidTotal": new_paid, "fully": fully})
        # ★_batch_payload 는 assigned_amount 등 계산 컬럼(_BATCH_SELECT)을 요구한다 —
        #   여기선 전체 화면을 다시 그리므로(onDone) 지급 결과 요약만 돌려주면 된다.
        return jsonify({"ok": True, "fully": fully, "unpaid": max(0, total - new_paid),
                        "paidAmount": new_paid, "totalAmount": total})


# ---------------------------------------------------------------- 재고 복귀 대기열
#   (2026-08-09 대표 승인) TMS가 반입·반납·판매취소로 표시한 출고/폐기 자산을
#   사람이 검수 후 원클릭으로 되살린다. 판정은 자산 이력의 최신 이벤트로 한다:
#   복귀후보(열림) → 재고복귀(복귀 실행) 또는 복귀후보종결(무시 — 영구).


@bp.get("/assets/revert-candidates")
def revert_candidates():
    """복귀 후보 목록 — 최신 이벤트가 '복귀후보'인 출고/폐기 자산."""
    require("purchase.view")
    clause, params = scope_clause("a")
    rows = get_db().execute(
        "SELECT a.*, c.name AS category_name, e.ts AS flagged_at, e.detail AS flag_detail "
        "FROM asset_events e "
        "JOIN (SELECT asset_id, MAX(id) AS mid FROM asset_events "
        "      WHERE action IN ('복귀후보','복귀후보종결','재고복귀') GROUP BY asset_id) m "
        "  ON m.mid = e.id "
        "JOIN assets a ON a.id = e.asset_id "
        "LEFT JOIN categories c ON c.id = a.category_id "
        "WHERE e.action='복귀후보' AND a.status IN ('shipped','scrapped')" + clause +
        " ORDER BY e.id DESC LIMIT 300", params).fetchall()
    out = []
    for r in rows:
        try:
            d = json.loads(r["flag_detail"]) if r["flag_detail"] else {}
        except (ValueError, TypeError):
            d = {}
        out.append(_asset_payload(r, {
            "categoryName": r["category_name"], "flaggedAt": r["flagged_at"],
            "tmsLabel": d.get("TMS표기", ""), "wasStatus": d.get("당시상태", "")}))
    return jsonify({"rows": out, "count": len(out)})


@bp.post("/assets/<int:aid>/restock")
def asset_restock(aid):
    """복귀 후보 처리 — {"action": "restock"(검수 후 복귀) | "dismiss"(무시)}.

    출고/폐기 상태는 일반 수정(PATCH)이 일부러 막는다(한 대가 두 고객에게 나가는
    사고 방지). 돌아온 실물을 사람이 확인했을 때만 이 전용 경로로 되살린다.
    복귀하면 입고(in_stock)·실재고로 들어간다 — 재고반영은 자동으로 켜지 않는다
    (검수 후 사람이 켠다는 기존 방침 그대로).
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    act = (body.get("action") or "restock").strip()
    if act not in ("restock", "dismiss"):
        abort(400, description="action은 restock/dismiss만 됩니다.")
    with tx(write=True) as conn:
        row = _get_asset_or_404(conn, aid)
        _check_asset_scope(conn, row["category_id"])
        if act == "dismiss":
            asset_event(conn, aid, "복귀후보종결", {"처리": "무시"})
            audit.log("asset_revert_dismiss", target=row["asset_no"], detail={})
            return jsonify({"ok": True, "action": "dismiss"})
        if row["status"] not in ("shipped", "scrapped"):
            abort(400, description="출고/폐기 상태의 자산만 복귀할 수 있습니다.")
        conn.execute(
            "UPDATE assets SET status='in_stock', tier='실재고', received=1, updated_at=? "
            "WHERE id=?", (config.now_iso(), aid))
        asset_event(conn, aid, "재고복귀",
                    {"from": row["status"], "to": "in_stock", "재고구분": "실재고"})
        audit.log("asset_restock", target=row["asset_no"], detail={"from": row["status"]})
    # ★렌탈 귀속이면 복귀해도 판매 재고에는 안 잡힌다 — 그 자리에서 알려 준다(위와 같은 이유).
    return jsonify({"ok": True, "action": "restock",
                    "rentalKept": row["division"] == DIVISION_RENTAL})


@bp.post("/assets/restock")
def assets_restock_bulk():
    """자산목록에서 고른 자산을 재고로 되돌린다(대표 2026-09-01: "재고로 어디서 되돌려?").

    ↩ 복귀 후보 탭은 'TMS가 반입/반납이라 하는 것'만 모은다. TMS가 그렇게 말한 적 없는
    자산(반품인데 TMS 기록이 안 들어온 경우)은 그 목록에 안 떠서 되돌릴 길이 없었다.
    → 자산목록에서 번호로 찾아 바로 되돌린다. 처리 내용은 복귀 후보와 완전히 같다
      (같은 이벤트명 '재고복귀' — 두 경로가 다른 기록을 남기면 이력을 못 믿는다).

    ★출고 상태를 사람이 바꾸는 일이라 사유를 받는다. 왜 되돌렸는지가 남아야
      나중에 "이건 왜 재고에 있지"를 되짚을 수 있다.
    ★살아 있는 주문이 아직 이 자산을 잡고 있으면 그 주문번호를 함께 돌려준다 —
      한 대가 두 곳에 걸린 상태를 모르고 지나가면 안 된다(중복 매칭은 주문 쪽 가드가 막는다).
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    reason = (body.get("reason") or "").strip()[:300]
    if not isinstance(ids, list) or not ids:
        abort(400, description="자산을 선택하세요.")
    if len(ids) > 200:
        abort(400, description="한 번에 200대까지 처리할 수 있습니다.")
    if not reason:
        abort(400, description="왜 되돌리는지 사유를 적어 주세요 — 나중에 근거가 됩니다.")
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        abort(400, description="자산 번호가 올바르지 않습니다.")

    done, skipped, held, rental_kept = [], [], [], []
    with tx(write=True) as conn:
        ts, actor = config.now_iso(), g.user["display_name"]
        for aid in ids:
            row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            if row is None:
                skipped.append({"id": aid, "reason": "자산을 찾을 수 없습니다."})
                continue
            _check_asset_scope(conn, row["category_id"])
            if row["status"] not in ("shipped", "scrapped"):
                skipped.append({"id": aid, "assetNo": row["asset_no"],
                                "reason": f"{ASSET_STATUSES.get(row['status'], row['status'])} "
                                          "상태는 되돌릴 게 없습니다(출고/폐기만 해당)."})
                continue
            od = conn.execute(
                "SELECT o.order_no FROM order_assets oa JOIN orders o ON o.id = oa.order_id "
                "WHERE oa.asset_id = ? AND o.cancelled_at = '' LIMIT 1", (aid,)).fetchone()
            conn.execute(
                "UPDATE assets SET status='in_stock', tier='실재고', received=1, updated_at=? "
                "WHERE id=?", (ts, aid))
            asset_event(conn, aid, "재고복귀",
                        {"from": row["status"], "to": "in_stock", "재고구분": "실재고",
                         "사유": reason,
                         **({"열린주문": od["order_no"]} if od else {})})
            done.append(row["asset_no"])
            if od:
                held.append({"assetNo": row["asset_no"], "orderNo": od["order_no"]})
            # ★복귀시켰다고 판매 재고가 되는 게 아니다 — 사업부가 렌탈이면 그대로 빠진다.
            #   대표 2026-09-01: "재고복귀를 해도 재고 복귀가 안돼" — 실은 복귀는 됐는데
            #   렌탈이라 판매 재고에서 빠진 것이었다. 그 자리에서 알려 준다.
            if row["division"] == DIVISION_RENTAL:
                rental_kept.append(row["asset_no"])
        audit.log("asset_restock", target=f"{len(done)}대",
                  detail={"reason": reason[:120], "held": len(held),
                          "rental": len(rental_kept)})
    return jsonify({"ok": len(done), "done": done, "skipped": skipped,
                    "heldByOrder": held, "rentalKept": rental_kept})


# ---------------------------------------------------------------- 제품코드 정합성
#   (2026-08-09 대표 승인) 코드 채우기 캠페인에서 오타 하나가 같은 상품 재고를
#   둘로 쪼갠다 — 띄어쓰기·대소문자만 다른 코드 묶음을 찾아 병합할 수 있게 한다.


@bp.get("/assets/code-variants")
def code_variants():
    """정규화(대문자·공백 제거)하면 같은데 표기가 다른 제품코드 묶음."""
    require("purchase.view")
    clause, params = scope_clause("a")
    rows = get_db().execute(
        "SELECT a.product_code AS code, COUNT(*) AS cnt, GROUP_CONCAT(a.id) AS ids "
        "FROM assets a WHERE TRIM(a.product_code) != ''" + sale_only("a") + clause +
        " GROUP BY a.product_code", params).fetchall()
    groups = {}
    for r in rows:
        norm = (r["code"] or "").upper().replace(" ", "")
        groups.setdefault(norm, []).append({
            "code": r["code"], "count": r["cnt"],
            "ids": [int(x) for x in (r["ids"] or "").split(",") if x]})
    out = []
    for norm, variants in groups.items():
        if len(variants) < 2:
            continue
        variants.sort(key=lambda v: -v["count"])
        out.append({"norm": norm, "canonical": variants[0]["code"],
                    "variants": variants,
                    "total": sum(v["count"] for v in variants)})
    out.sort(key=lambda x: -x["total"])
    return jsonify({"groups": out[:100], "count": len(out)})


@bp.get("/assets/integrity")
def assets_integrity():
    """재고 숫자 모순 점검 — 몰에 보내는 숫자를 믿을 수 있게(2026-08-09 대표 승인).

    ghostListed  = 반영 켜짐인데 판매가능 상태가 아님(유령 — 자동해제가 있어 0이어야 정상)
    codedButOff  = 판매가능 + 코드 있음 + 반영 꺼짐(올릴 수 있는 기회)
    listedNoCode = 반영 켜짐인데 코드 없음(가드가 막지만 과거 데이터 잔재)
    unreceivedReady = 입고 확인 전인데 판매가능 상태
    """
    require("purchase.view")
    clause, params = scope_clause("a")
    conn = get_db()
    marks = ",".join("?" * len(AVAILABLE_STATUSES))
    avail = list(AVAILABLE_STATUSES)
    ghost = conn.execute(
        f"SELECT COUNT(*) AS n FROM assets a WHERE a.stock_listed=1 "
        f"AND a.status NOT IN ({marks})" + clause, avail + params).fetchone()["n"]
    coded_off = conn.execute(
        f"SELECT COUNT(*) AS n FROM assets a WHERE a.stock_listed=0 "
        f"AND TRIM(a.product_code) != '' AND a.status IN ({marks}) "
        f"AND a.tier <> '가재고'" + clause, avail + params).fetchone()["n"]
    listed_nocode = conn.execute(
        "SELECT COUNT(*) AS n FROM assets a WHERE a.stock_listed=1 "
        "AND TRIM(a.product_code) = ''" + clause, params).fetchone()["n"]
    unreceived = conn.execute(
        "SELECT COUNT(*) AS n FROM assets a WHERE a.received=0 AND a.status='ready'"
        + clause, params).fetchone()["n"]
    # 자산 매칭 없이 출고 마감된 주문 — 어느 기계가 나갔는지 모른다(백필 대상)
    shipped_unmatched = conn.execute(
        "SELECT COUNT(*) AS n FROM orders o WHERE o.shipping_done=1 AND o.cancelled_at='' "
        "AND NOT EXISTS (SELECT 1 FROM order_assets oa WHERE oa.order_id=o.id)"
    ).fetchone()["n"]
    return jsonify({"ghostListed": ghost, "codedButOff": coded_off,
                    "listedNoCode": listed_nocode, "unreceivedReady": unreceived,
                    "shippedUnmatched": shipped_unmatched})


@bp.post("/assets/bulk")
def bulk_assets():
    """여러 자산의 상태·위치·등급을 한 번에 — 단건 규칙(주문매칭/출고는 손대지 않음)을 그대로 지킨다."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        abort(400, description="처리할 자산을 선택하세요.")
    if len(ids) > 500:
        abort(400, description="한 번에 500대까지 처리할 수 있습니다.")
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        abort(400, description="자산 번호가 올바르지 않습니다.")

    status = (body.get("status") or "").strip()
    location = body.get("location")
    grade = (body.get("grade") or "").strip()
    tier = norm_tier(body.get("tier"))
    if status and status not in MANUAL_STATUSES:
        abort(400, description="여기서 지정할 수 없는 상태입니다(주문매칭/출고는 주문 흐름에서만).")
    if grade and grade not in GRADES:
        abort(400, description="알 수 없는 등급입니다.")
    if tier and tier not in TIERS:
        abort(400, description="재고구분은 가용/실재고/가재고 중 하나여야 합니다.")
    if not status and location is None and not grade and not tier:
        abort(400, description="바꿀 항목을 하나 이상 지정하세요.")

    now = config.now_iso()
    ok, failed = [], []
    with tx(write=True) as conn:
        for aid in ids:
            row = conn.execute("SELECT * FROM assets WHERE id=?", (aid,)).fetchone()
            if row is None:
                failed.append({"id": aid, "reason": "자산을 찾을 수 없습니다."})
                continue
            _check_asset_scope(conn, row["category_id"])
            changes = {}
            status_blocked = False
            if status and status != row["status"]:
                if row["status"] in ("reserved", "shipped", "returning"):
                    # ★상태만 막고 나머지(위치·등급·재고구분)는 계속 처리한다.
                    #   예전엔 여기서 자산을 통째로 건너뛰어서, 상태와 등급을 같이
                    #   지정하면 매칭된 자산은 등급도 조용히 안 바뀌었다.
                    failed.append({"id": aid, "label": row["asset_no"],
                                   "reason": "상태 변경 불가 — 주문에 매칭/출고된 자산입니다."
                                             " (다른 항목은 반영됨)"})
                    status_blocked = True
                else:
                    conn.execute("UPDATE assets SET status=? WHERE id=?", (status, aid))
                    asset_event(conn, aid, "상태변경", {
                        "from": ASSET_STATUSES.get(row["status"], row["status"]),
                        "to": ASSET_STATUSES[status], "일괄": True})
                    changes["status"] = status
                    # 판매가능 축을 벗어났으면 재고반영도 함께 끈다(유령 재고 방지)
                    if status not in AVAILABLE_STATUSES:
                        if _unlist(conn, aid, "일괄 상태변경(%s)" % ASSET_STATUSES[status]):
                            changes["재고반영"] = False
            if location is not None and str(location).strip() != row["location"]:
                conn.execute("UPDATE assets SET location=? WHERE id=?", (str(location).strip(), aid))
                changes["위치"] = {"from": row["location"], "to": str(location).strip()}   # 전값 기록(A7)
            if grade and grade != row["grade"]:
                conn.execute("UPDATE assets SET grade=? WHERE id=?", (grade, aid))
                changes["등급"] = {"from": row["grade"], "to": grade}
            if tier and tier != row["tier"]:
                # ★가재고→가용/실재고는 출고 차단이 풀리는 변경이다. 기록을 남긴다.
                conn.execute("UPDATE assets SET tier=? WHERE id=?", (tier, aid))
                asset_event(conn, aid, "재고구분", {"from": row["tier"] or "미정",
                                                  "to": tier, "일괄": True})
                changes["재고구분"] = tier
            if changes:
                conn.execute("UPDATE assets SET updated_at=? WHERE id=?", (now, aid))
                if set(changes) - {"status", "재고구분"}:
                    asset_event(conn, aid, "수정", {k: v for k, v in changes.items()
                                                  if k not in ("status", "재고구분")})
            if not status_blocked or changes:
                ok.append(aid)
        audit.log("assets_bulk", target=f"{len(ok)}대",
                  detail={"status": status or None, "location": location, "grade": grade or None,
                          "tier": tier or None, "ok": len(ok), "failed": len(failed)})
    return jsonify({"ok": len(ok), "done": ok, "failed": failed})


@bp.get("/assets/export")
def export_assets():
    """현재 조건의 자산 목록을 엑셀로 내려받기(이관 양식과 동일 컬럼)."""
    require("purchase.view")
    from flask import Response

    from ..importers import write_xlsx
    sql, params = _assets_filter_sql()          # 화면 목록과 같은 조건
    # ★예전에는 asset_no 오름차순 LIMIT 5000이라, 15,107대 중 '가장 오래된' 5,000대만
    #   나가고 최근 1년치가 통째로 빠졌다. 게다가 잘렸다는 표시가 없어 그게 전부인 줄 알았다
    #   (2026-08-04 감사). 최신순으로 바꾸고, 잘리면 파일명과 첫 줄로 알려 준다.
    total = get_db().execute(
        "SELECT COUNT(*) FROM (" + sql + ")", params).fetchone()[0]
    rows = get_db().execute(sql + " ORDER BY a.id DESC LIMIT 5000", params).fetchall()
    cut = total > len(rows)
    headers = ["관리번호", "매입전표", "대분류", "브랜드", "모델명", "시리얼번호", "매입가", "판매가", "등급",
               "보관위치", "CPU", "그래픽", "RAM", "SSD", "인치", "배터리효율", "충전기유무",
               "재고상태", "매입상세비고"]
    data = [[
        r["asset_no"], r["slip_no"] or "", r["category_name"] or "", r["maker"], r["model"], r["serial"],
        r["purchase_price"], r["sale_price"], r["grade"], r["location"], r["cpu"], r["gpu"],
        r["ram"], r["ssd"], r["inch"], r["battery"], r["charger"],
        ASSET_STATUSES.get(r["status"], r["status"]), r["notes"],
    ] for r in rows]
    if cut:
        # 파일을 열자마자 보이도록 맨 위에 한 줄 박는다 — 파일명만으로는 놓친다
        data.insert(0, [f"※ 전체 {total:,}대 중 최근 {len(rows):,}대만 담겼습니다."
                        " 조건(상태·분류·검색)을 좁혀 나눠 받으세요."] + [""] * (len(headers) - 1))
    name = (f"ows-assets-{config.today_str()}"
            + (f"-일부{len(rows)}of{total}" if cut else "") + ".xlsx")
    content = write_xlsx(headers, data)
    return Response(content, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@bp.delete("/assets/<int:aid>/repairs/<int:rid>")
def delete_repair(aid, rid):
    require("purchase.edit")
    with tx(write=True) as conn:
        row = _get_asset_or_404(conn, aid)
        _check_asset_scope(conn, row["category_id"])
        rep = conn.execute("SELECT * FROM asset_repairs WHERE id=? AND asset_id=?", (rid, aid)).fetchone()
        if rep is None:
            abort(404, description="수리 기록을 찾을 수 없습니다.")
        conn.execute("DELETE FROM asset_repairs WHERE id=?", (rid,))
        conn.execute("DELETE FROM part_stock_moves WHERE repair_id=?", (rid,))
        queue_asset_cost(conn, aid, reason=f"수리 삭제: {rep['description']}"[:60])
        asset_event(conn, aid, "수리삭제", {"description": rep["description"], "cost": rep["cost"]})
        audit.log("repair_deleted", target=row["asset_no"],
                  detail={"cost": rep["cost"], "description": rep["description"]})
    return jsonify({"ok": True})


# 서브모듈 라우트 등록 (bp 정의 이후에 import해야 함)
from . import autosync, backfill, migration, sales, spec_options, tms_link, transfers  # noqa: E402,F401
from . import sale_entry  # noqa: E402,F401  — 판매 전표 직접 등록·수정(2026-09-02)
from . import masters  # noqa: E402,F401  — 공용 모델·거래처 마스터(2026-09-02). /suppliers 세 라우트의 뷰도 거기서 교체된다
from . import numbering  # noqa: E402,F401  — 관리번호 채번(5000~)·손입력 검증(2026-09-03, A4). _add_assets/update_asset/number-fix 가 쓴다
from . import reclass  # noqa: E402,F401  — 자산 카테고리 재판정 미리보기·적용·되돌리기(2026-09-03, A5). 규칙은 migration.category_decision 한 곳
from . import cutover  # noqa: E402,F401  — 창구 동시 마감 이중입력 경보(2026-09-03). tms_link.sync_once 끝에서 틱마다 점검
