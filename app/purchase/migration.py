"""TMS 자산 데이터 엑셀 이관.

TMS 매입내역/재고를 엑셀로 내보낸 뒤 OWS로 일괄 등록한다.
- 관리번호(YYMMDD-NNNN)를 그대로 가져오며, 이미 있으면 건너뛴다(재실행 안전)
- 등급 목록에 없는 값(수리·A/S·불량·도색대기 등 TMS의 작업상태)은 자산 상태로 변환
- 미리보기(dry-run)로 먼저 확인한 뒤 실행하는 흐름
"""
import json
import sqlite3

from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import tx
from ..importers import read_first_sheet
from . import AVAILABLE_STATUSES, GRADES, SPEC_FIELDS, asset_event, bp, tier_for_grade

# 엑셀 헤더 → 내부 필드. TMS 컬럼명을 우선하되 흔한 변형도 받아준다.
HEADER_MAP = {
    "관리번호": "asset_no", "자산번호": "asset_no",
    "대분류": "category", "카테고리": "category",
    "중분류": "subcategory",             # ★카테고리 판정 축(2026-09-03, A5) — 창구 행·엑셀 모두 이 칸이 있다
    "브랜드": "maker", "제조사": "maker",
    "모델명": "model", "모델": "model",
    "시리얼번호": "serial", "시리얼": "serial", "s/n": "serial",
    "매입가": "purchase_price", "매입단가": "purchase_price",
    "판매가": "sale_price",
    "등급": "grade",
    "보관위치": "location",
    "cpu": "cpu",
    "그래픽": "gpu", "gpu": "gpu",
    "ram": "ram", "메모리": "ram",
    "ssd": "ssd", "저장장치": "ssd",
    "인치": "inch",
    "배터리효율": "battery", "배터리": "battery",
    "충전기유무": "charger", "충전기": "charger",
    "매입상세비고": "notes", "비고": "notes", "특이사항": "notes",
    # ★TMS 재고상세에서 사람이 손으로 고치는 칸(2026-09-03, 계획서 ②(g)) — 창구 재고내역이 싣는다(엑셀엔 없다)
    "재고비고": "stock_note",
    "수리비": "tms_repair", "부품비": "tms_parts",           # → 자산 수리 기록(asset_repairs, 출처 TMS연동)
    "재고상태": "tms_status", "상태": "tms_status",
    "매입일": "purchase_date", "가입고일": "purchase_date",
    "거래처명": "supplier", "매입처명": "supplier", "거래처": "supplier",
    "매입전표": "slip_no", "전표번호": "slip_no",
    # 판매현황 전용 — '이 자산이 누구에게 나갔는지'를 자산 이력에 남기기 위한 것
    "수령자성함": "buyer", "수령자": "buyer",
    "판매채널": "sale_channel",
    "판매일": "sale_date",
    "판매전표": "sale_slip",
}

# TMS가 등급 목록에 섞어 둔 작업상태 → OWS 자산 상태
GRADE_TO_STATUS = {
    "수리": "repair", "a/s": "as", "as수리": "as",
    "불량": "defective", "도색대기": "painting", "폐기": "scrapped",
    "시트지대기": "painting",          # 외관 작업 대기 — 도색대기와 같은 성격
}
# 재고상태 컬럼 값 → OWS 자산 상태
#
# ★앞의 절반은 추측이었고, 아래 '실제값' 블록이 2026-07-30 내보내기 14,969건에서
#   확인한 TMS의 진짜 값이다. 이게 없으면 전부 기본값(입고)으로 들어가
#   이미 팔린 노트북 12,261대가 판매 가능 재고로 잡힌다.
TMS_STATUS_MAP = {
    # --- TMS 실제값 (재고내역/매입현황 전수 확인) ---
    # ★TMS는 '입고'와 '판매가능'을 구분하지 않고 '매입' 하나로 쓴다.
    #   그대로 in_stock으로 넣으면 OWS 재고현황에서 전부 '작업 중'으로 잡혀
    #   "지금 팔 수 있는 재고 0대"로 표시된다(2026-07-30 이관 검증에서 확인).
    #   TMS의 '매입'은 실제로는 판매 대기 재고이므로 ready로 본다.
    "매입": "ready",          # 사서 들여놓고 판매 대기 중인 재고
    "판매": "shipped",        # 이미 팔려 나간 것 = 재고 아님
    "렌탈": "shipped",        # 렌탈로 나가 있는 것 = 우리 손에 없다(렌탈은 RMS가 관리)
    # 돌아온 물건은 검수가 필요하므로 '입고'로 둔다 — 확인 후 판매가능으로 올린다.
    "반납": "in_stock",       # 렌탈에서 돌아온 것
    "반입": "in_stock",       # 판매 반품으로 돌아온 것
    "판매취소": "in_stock",   # 판매가 취소돼 재고로 복귀
    # --- 그 밖에 들어올 수 있는 표기 ---
    "재고": "ready", "판매가능": "ready", "정상": "ready",
    "입고": "in_stock", "가입고": "in_stock",
    "정비중": "refurbishing", "수리": "repair", "a/s": "as",
    "불량": "defective", "도색대기": "painting",
    "판매완료": "shipped", "출고": "shipped", "출고완료": "shipped",
    "폐기": "scrapped",
}

# ★기존 자산 상태 자동 갱신 (2026-08-07 대표 결정: "판매인지 폐기인지 이런
#   세부사항들이 계속 변경이 돼" — 처음 등록 때 한 번만 반영하던 것을 따라가게).
#
#   TMS가 확실히 아는 사실은 '물건이 나갔다(판매·렌탈·출고)/버렸다(폐기)'뿐이다.
#   그래서 그 방향(정방향)만 자동으로 따라간다.
#   - 출발점은 작업·재고 상태일 때만. reserved(주문매칭)·returning(회수중)은
#     주문 흐름이 주인이고(update_asset 의 가드와 같은 원칙), cancelled(매입취소)·
#     returned(거래처반품)는 전표 회계 상태라 여기서 건드리면 장부가 틀어진다.
#   - 역방향(반입·판매취소·반납 → 재고 복귀)은 자동 반영하지 않는다. 검수 없이
#     재고가 되살아나면 주문 매칭이 유령 재고를 잡는다 — 알림만 남겨 사람이 본다.
#   - 작업 상태끼리(수리→판매가능 등)도 옮기지 않는다 — 그건 OWS 쪽 업무다.
# ★사업부 자동 유지(2026-08-12 대표 규칙) — TMS 재고상태가 렌탈/반납이면 렌탈 사업부다.
#   TMS 내보내기가 2시간마다 도니 여기서 계속 맞춰 주면 같은 사고가 재발하지 않는다.
#   ★예외 잠금 자산은 절대 건드리지 않는다. '매입'으로 돌아가도 판매로 되돌리지 않는다 —
#     렌탈→판매 길은 이관 커밋 하나뿐이어야 원장이 한 벌로 유지된다.
TMS_RENTAL_STATUSES = ("렌탈", "반납")

AUTO_STATUS_TO = ("shipped", "scrapped")
AUTO_STATUS_FROM = ("in_stock", "refurbishing", "repair", "as", "painting",
                    "defective", "ready")

# ★카테고리 = TMS 중분류 축(2026-09-03, A5 재고 카테고리 정합 — 대표 확정 전 기본값, 되돌릴 수 있게 구현).
#   실측(연동 사본 15,644행): 대분류는 'PC' 한 덩어리(14,830)라 판정 축이 못 되고 자유 입력(모니터/노트북/데스크탑)도
#   섞여 있다. 중분류는 8종 어휘를 안 벗어난다 → 중분류로 판정한다. 이름 대응은 이 상수 하나뿐(예외는 버즈→웨어러블).
#   화면(purchase.js SUBCAT_TO_CATEGORY)에 미러가 있다 — 바꿀 때 같이.
TMS_SUBCAT_TO_CATEGORY = {
    "노트북": "노트북", "데스크탑": "데스크탑", "태블릿": "태블릿", "모니터": "모니터",
    "미니PC": "미니PC", "일체형PC": "일체형PC", "주변기기": "주변기기", "버즈": "웨어러블",
}
# 중분류가 비고 모델 마스터에도 없을 때 대분류 자유 입력값으로 판정(③). 'PC'는 여기 없다 —
# 중분류 없이 'PC'만 오는 옛 엑셀·수기 행은 기본값(categories sort 0 = 노트북)으로 간다(결정 ③).
TMS_MAINCAT_TO_CATEGORY = {"노트북": "노트북", "데스크탑": "데스크탑", "모니터": "모니터",
                           "태블릿": "태블릿", "웨어러블": "웨어러블"}
# 개명 전 DB(설정 ▸ 카테고리에서 도로 'PC'로 바꾼 경우 포함)에서도 돌게 — 새 이름이 없으면 옛 이름을 같은 자리로 받는다.
CATEGORY_FALLBACK_NAMES = {"데스크탑": "PC", "일체형PC": "올인원"}
_norm_key = None


def category_ids(conn):
    """카테고리 이름 → id(사용 중인 것만). '데스크탑'이 없으면 'PC', '일체형PC'가 없으면 '올인원'을 같은 자리로 받는다."""
    cats = {r["name"]: r["id"] for r in conn.execute(
        "SELECT id, name FROM categories WHERE enabled=1").fetchall()}
    for new, old in CATEGORY_FALLBACK_NAMES.items():
        if new not in cats and old in cats:
            cats[new] = cats[old]
    return cats


def category_names(conn):
    """id → 이름(비활성 포함 — 이력·미리보기에 옛 이름도 찍어야 한다)."""
    return {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM categories").fetchall()}


def model_master_map(conn):
    """모델 마스터 정규화 이름 → {category, subcategory}. 중분류가 빈 행(②)을 채운다(삭제 표시 행도 분류 정보는 유효)."""
    try:
        rows = conn.execute("SELECT norm_name, category, subcategory FROM models").fetchall()
    except sqlite3.OperationalError:                 # models 표가 아직 없는 옛 DB
        return {}
    return {r["norm_name"]: {"category": r["category"], "subcategory": r["subcategory"]} for r in rows}


def model_key(name):
    """모델명 정규화 키 — masters.norm_key 와 같은 규칙(순환 import 라 지연 로딩)."""
    global _norm_key
    if _norm_key is None:
        from .masters import norm_key
        _norm_key = norm_key
    return _norm_key(name)


def category_decision(cats, subcat="", maincat="", master=None):
    """카테고리 판정 — (id, 근거) 또는 (None, None). None 을 기본값으로 바꿀지는 호출부가 정한다(신규만).

    순서: ① 중분류 → TMS_SUBCAT_TO_CATEGORY  ② 빈값이면 모델 마스터 중분류  ③ 대분류(자유 입력값·마스터 대분류)
    ④ 못 정하면 None. cats 는 category_ids(conn). master 는 models 행(또는 model_master_map 값)이나 None.
    """
    m_sub = str(master["subcategory"] or "").strip() if master is not None else ""
    m_main = str(master["category"] or "").strip() if master is not None else ""
    for value, label, table in ((str(subcat or "").strip(), "TMS 중분류", TMS_SUBCAT_TO_CATEGORY),
                                (m_sub, "모델 마스터 중분류", TMS_SUBCAT_TO_CATEGORY),
                                (str(maincat or "").strip(), "TMS 대분류", TMS_MAINCAT_TO_CATEGORY),
                                (m_main, "모델 마스터 대분류", TMS_MAINCAT_TO_CATEGORY)):
        name = table.get(value) if value else None
        cid = cats.get(name) if name else None
        if cid:
            return cid, f"{label} {value}"
    return None, None


def set_shadow_category(conn, asset_id, category_id):
    """tms_shadow.category = '자동(연동·재판정)이 마지막으로 정한 카테고리'. 다른 키는 그대로, 깨진 그림자는 새로 만든다."""
    row = conn.execute("SELECT tms_shadow FROM assets WHERE id=?", (asset_id,)).fetchone()
    try:
        shadow = json.loads(row["tms_shadow"]) if row and row["tms_shadow"] else {}
    except (ValueError, TypeError):
        shadow = {}
    if not isinstance(shadow, dict):
        shadow = {}
    shadow["category"] = category_id
    conn.execute("UPDATE assets SET tms_shadow=? WHERE id=?",
                 (json.dumps(shadow, ensure_ascii=False), asset_id))


def _norm_header(h):
    return str(h or "").strip().lower().replace(" ", "")


def _row_to_asset(row):
    """엑셀 한 행 → 자산 dict. 알 수 없는 컬럼은 무시한다."""
    out = {}
    for raw_key, val in row.items():
        key = HEADER_MAP.get(_norm_header(raw_key)) or HEADER_MAP.get(str(raw_key or "").strip())
        if key:
            out[key] = str(val or "").strip()
    return out


_SERIAL_BAD = __import__("re").compile(r"[가-힣]")


def _clean_serial(raw):
    """시리얼 칸에 들어온 값이 진짜 시리얼인지 가린다. (시리얼, 메모로_옮길_말) 반환.

    ★TMS에서는 시리얼 칸을 메모장처럼 쓴 흔적이 있다
      ('액정불량-메티스예정', '수리중', '2025.10.15 매입' 등 11건).
      이런 걸 시리얼로 넣으면 중복 검사·자산 검색이 통째로 흐트러진다.
      버리지는 않고 특이사항으로 옮겨 둔다.
    """
    v = (raw or "").strip()
    if not v:
        return "", ""
    if _SERIAL_BAD.search(v) or len(v) < 5:
        return "", f"[이관] 시리얼칸 원본: {v}"
    return v, ""


def _to_date(v):
    """엑셀 날짜를 YYYY-MM-DD로. 숫자(시리얼 날짜)와 문자열을 모두 받는다.

    ★엑셀은 날짜를 '1899-12-30부터 며칠'이라는 숫자로 저장한다. 그대로 넣으면
      매입일이 '46175.56'처럼 보인다(2026-07-30 이관 검증에서 실제 발생).
    """
    t = str(v or "").strip()
    if not t:
        return ""
    try:
        n = float(t)
        if 1 < n < 100000:
            import datetime
            return (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(n))).isoformat()
    except (TypeError, ValueError):
        pass
    t = t.replace(".", "-").replace("/", "-").strip("-")
    return t[:10]


def _to_int(v):
    try:
        return int(float(str(v).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def _sale_info(a):
    """판매현황 행에서 '누구에게 언제 얼마에 나갔는지'를 뽑는다.

    ★TMS 판매현황에는 수령자성함이 13,032건 들어 있는데 지금까지 이관하지 않아,
      출고완료 자산 13,128대 중 12,636대가 '어느 고객에게 나갔는지 모름' 상태였다
      (대표 요청 2026-07-31: 자산번호로 고객까지 이력관리가 돼야 한다).
      OWS 주문을 새로 만들지는 않는다 — 지나간 판매는 자산 이력으로 남기는 게 맞다.
    """
    buyer = (a.get("buyer") or "").strip()
    channel = (a.get("sale_channel") or "").strip()
    slip = (a.get("sale_slip") or "").strip()
    if not (buyer or slip):
        return None
    return {"buyer": buyer, "channel": channel, "slip": slip,
            "date": _to_date(a.get("sale_date")), "price": _to_int(a.get("sale_price"))}


def _resolve_grade_status(asset):
    """등급/재고상태를 OWS 체계로 변환. (등급, 상태, 메모추가) 반환."""
    raw_grade = (asset.get("grade") or "").strip()
    note = ""
    status = "in_stock"

    tms_status = (asset.get("tms_status") or "").strip().lower()
    if tms_status and tms_status in TMS_STATUS_MAP:
        status = TMS_STATUS_MAP[tms_status]

    has_status = bool(tms_status and tms_status in TMS_STATUS_MAP)
    grade = raw_grade if raw_grade in GRADES else ""
    if not grade and raw_grade:
        mapped = GRADE_TO_STATUS.get(raw_grade.lower())
        if mapped:
            # ★재고상태가 이미 말해 준 게 있으면 그쪽이 우선이다.
            #   '판매' 완료된 물건의 등급 칸에 '수리'가 남아 있는 경우가 많은데
            #   그걸 상태로 삼으면 이미 팔린 노트북이 '수리중'으로 되살아난다.
            if not has_status:
                status = mapped
            grade = "미정"
            note = f"[이관] 원본 등급: {raw_grade}"
        else:
            grade = "미정"
            note = f"[이관] 원본 등급: {raw_grade}"
    if not grade:
        grade = "미정"
    return grade, status, note


def _parse_upload():
    files = request.files.getlist("files")
    if not files:
        abort(400, description="엑셀 파일을 선택하세요.")
    rows = []
    for f in files:
        content = f.read()
        if len(content) > 20 * 1024 * 1024:
            abort(400, description=f"파일이 너무 큽니다(20MB 초과): {f.filename}")
        try:
            rows.extend(read_first_sheet(content, f.filename or "upload"))
        except Exception:
            abort(400, description=f"엑셀을 해석하지 못했습니다: {f.filename}")
    return rows


# 갱신 모드에서 '비어 있으면 채운다'로 볼 필드.
# ★핵심은 매입가다 — TMS 이관 초기에 관리번호만 선점된 자산이 492건 있고,
#   지금까지는 '이미 있으니 건너뛰기'뿐이라 매입가가 영영 0원으로 남아 마진이 100%로 보고됐다
#   (2026-07-30 감사 확인). 값을 덮어쓰지는 않고 '빈 칸만' 채운다.
# 한 파일에서 이보다 많은 정정이 나오면 통째로 보류 — 깨진 엑셀 방어(2026-08-25)
CORRECTION_LIMIT = 200

FILLABLE_NUM = ("purchase_price", "sale_price")
FILLABLE_TXT = ("maker", "model", "serial", "location", "cpu", "gpu", "ram",
                "ssd", "inch", "battery", "charger", "stock_note")

# ★TMS 수리비·부품비 → 자산 수리 기록(2026-09-03). 출처 'TMS연동' 행만 만들고 고친다 — 사람이 넣은 수리·부품 기록은 불가침.
#   TMS 제조원가는 97.8%가 매입가+수리비+부품비 자동값이라 저장하지 않는다(OWS 총원가 = 매입가 + 수리 기록 합이 같은 뜻).
TMS_COST_ACTOR = "TMS연동"
TMS_COST_DESC = {"tms_repair": "수리비(TMS)", "tms_parts": "부품비(TMS)"}


def tms_costs_of(a):
    """행에 수리비·부품비 칸이 있을 때만 {키: 원}. 엑셀 경로(칸 없음)는 None — 아무것도 안 한다."""
    if not any(k in a for k in TMS_COST_DESC):
        return None
    return {k: _to_int(a.get(k)) for k in TMS_COST_DESC}


def sync_tms_costs(conn, asset_id, costs, repair_date, ts):
    """자산 하나의 TMS 수리비·부품비를 asset_repairs(출처 TMS연동)에 맞춘다. 바뀐 항목 수를 돌려준다.

    ★★되돌아오는 고리 차단(2026-09-08) — OWS가 TMS로 되돌려 쓰기 시작하면서 생긴 함정이다.
      OWS A/S 수리비 75,000 → TMS 재고.수리비 75,000 → 트리거가 변경 기록 → 사본 반영 →
      여기서 "TMS에 수리비 75,000이 있네" 하며 'TMS연동' 수리 기록을 또 만든다
      → 자산 원가가 사람 기록 75,000 + TMS연동 75,000 = 150,000. 화면 어디에서도 안 걸린다.
      그래서 **OWS가 밀어 넣어 둔 몫을 뺀 것**만 TMS 고유의 값으로 본다(tms_push.tms_own_repair).
      TMS에서 사람이 더 올렸으면 그 차액만 TMS연동 행으로 남아 정확히 맞는다.
    """
    if not costs:
        return 0
    from . import split_vat
    from .tms_push import tms_own_repair
    n = 0
    for key, value in costs.items():
        desc = TMS_COST_DESC[key]
        value = int(value or 0)
        if key == "tms_repair":          # ★키는 'tms_repair' 다 — '수리비'는 화면 표기다
            value = tms_own_repair(conn, asset_id, value)
        row = conn.execute("SELECT id, cost FROM asset_repairs WHERE asset_id=? AND created_by=? AND description=?",
                           (asset_id, TMS_COST_ACTOR, desc)).fetchone()
        if row is None:
            if value <= 0:
                continue
            net, vat = split_vat(value)
            conn.execute("INSERT INTO asset_repairs(asset_id, repair_date, description, cost, net, vat, created_by, created_at) "
                         "VALUES(?,?,?,?,?,?,?,?)", (asset_id, repair_date or ts[:10], desc, value, net, vat, TMS_COST_ACTOR, ts))
            asset_event(conn, asset_id, "수리", {"내용": desc, "비용": value, "출처": "TMS"})
        elif int(row["cost"] or 0) != value:
            if value <= 0:
                conn.execute("DELETE FROM asset_repairs WHERE id=?", (row["id"],))
                asset_event(conn, asset_id, "수리삭제", {"내용": desc, "비용": {"from": row["cost"], "to": 0}, "출처": "TMS"})
            else:
                net, vat = split_vat(value)
                conn.execute("UPDATE asset_repairs SET cost=?, net=?, vat=? WHERE id=?", (value, net, vat, row["id"]))
                asset_event(conn, asset_id, "수리", {"내용": desc, "비용": {"from": row["cost"], "to": value}, "출처": "TMS"})
        else:
            continue
        n += 1
    return n


def _allowed_categories(conn):
    """이관도 카테고리 스코프를 지켜야 한다 — 담당 밖 분류에 대량 생성을 막는다.

    ★자동 반영(백그라운드 스레드)에는 로그인 사용자가 없다. g.user를 그냥 읽으면
      AttributeError로 자산 파일 반영이 통째로 실패한다(2026-07-30 실제로 발생).
      사람이 아닌 시스템 작업이므로 스코프 제한을 두지 않는다.
    """
    user = g.get("user")
    if user is None:
        return None                      # 자동 반영 — 사람 단위 스코프가 없다
    if user["is_admin"] or user["all_categories"]:
        return None                      # 제한 없음
    return {r["category_id"] for r in conn.execute(
        "SELECT category_id FROM user_categories WHERE user_id=?", (user["id"],)).fetchall()}


def _prepare(conn, rows, fill_blanks=False):
    """행들을 검사해 (등록대상, 중복, 오류, 갱신대상) 으로 나눈다.

    fill_blanks=True면 이미 있는 관리번호를 '건너뛰기' 대신 '빈 칸 채우기' 대상으로 돌린다.
    """
    # ★카테고리는 TMS 중분류로 정한다(2026-09-03, A5): category_decision — ① 중분류 ② 모델 마스터 ③ 대분류 ④ 기본값(sort 0)
    cats = category_ids(conn)
    masters_map = model_master_map(conn)
    default_cat = conn.execute(
        "SELECT id FROM categories WHERE enabled=1 ORDER BY sort, id LIMIT 1").fetchone()
    exist_rows = {r["asset_no"]: r for r in conn.execute(
        "SELECT id, asset_no, serial, model, purchase_price, sale_price, maker, location, "
        "       cpu, gpu, ram, ssd, inch, battery, charger, stock_note, status, tms_lock, "
        "       division, division_locked, tms_shadow, category_id "
        "FROM assets").fetchall()}
    # ★OWS가 번호를 바로잡은 적이 있는 번호 — TMS는 잘못된 값을 계속 보낸다.
    #   비워진 옛 번호로 유령 자산이 새로 생기는 길을 여기서 끊는다(2026-08-18).
    fixed_nos = {r["asset_no"] for r in conn.execute(
        "SELECT DISTINCT asset_no FROM tms_number_fixes").fetchall()}
    allowed = _allowed_categories(conn)
    # ★시리얼 → 이미 그 시리얼을 쓰는 자산. 화면 등록은 409로 막는데 이관만 무방비였다.
    #   실물 한 대가 자산 두 대로 잡혀 재고 수와 자산가치가 부풀려진다(2026-07-31 감사: 40건 86대).
    #   다만 이관을 통째로 막지는 않는다 — TMS 원본이 이미 중복인 경우가 있어
    #   막아 버리면 나머지 정상 행까지 못 들어온다. 경고로 올리고 그 행만 건너뛴다.
    serial_owner = {}
    for r in conn.execute("SELECT asset_no, TRIM(serial) AS s FROM assets WHERE TRIM(serial)<>''"):
        serial_owner.setdefault(r["s"].upper(), r["asset_no"])
    # ★TMS 수리비·부품비 현재값(출처 TMS연동 행) — 바뀐 자산만 갱신 대상에 넣는다(2026-09-03)
    desc_to_key = {v: k for k, v in TMS_COST_DESC.items()}
    tms_cost_rows = {}
    for r in conn.execute("SELECT asset_id, description, cost FROM asset_repairs WHERE created_by=?", (TMS_COST_ACTOR,)):
        if r["description"] in desc_to_key:
            tms_cost_rows.setdefault(r["asset_id"], {})[desc_to_key[r["description"]]] = int(r["cost"] or 0)

    ready, dup, errors, updates, locked = [], [], [], [], []
    seen = set()
    for i, raw in enumerate(rows, start=2):   # 2행부터가 데이터(1행 헤더)
        a = _row_to_asset(raw)
        asset_no = (a.get("asset_no") or "").strip()
        if not asset_no:
            if any(a.get(k) for k in ("model", "serial", "maker")):
                errors.append(f"{i}행: 관리번호가 비어 있습니다.")
            continue
        # ★쉼표·공백이 섞인 값은 관리번호가 아니다. 주문 표에는 자산번호 칸에
        #   "HB-001, HB-002"처럼 여러 개가 한 칸에 들어 있어, 그대로 받으면
        #   그 통짜 문자열이 자산 한 대로 등록된다(재고 대수가 틀어진다).
        if "," in asset_no or " " in asset_no:
            errors.append(f"{i}행: 관리번호 형식이 아닙니다 — {asset_no!r} "
                          "(한 칸에 여러 개가 들어 있으면 자산 표가 아닙니다)")
            continue
        if asset_no in seen:
            dup.append(asset_no)
            continue
        cur = exist_rows.get(asset_no)
        if cur is not None:
            seen.add(asset_no)
            # ★잠긴 자산 — 사람이 "TMS 쪽 기록이 틀렸다"고 판정한 것이다.
            #   값 채우기도, 상태 갱신도, 판매 이력도 반영하지 않는다.
            #   (하나라도 반영하면 2시간 뒤 원상복구된다 — 바로잡은 의미가 없어진다)
            if cur["tms_lock"]:
                locked.append(asset_no)
                continue
            # ★번호는 같은데 시리얼이 명백히 다르면 '같은 물건 재이관'이 아니라
            #   '다른 실물이 같은 번호를 쓰는' 사고다. 조용히 건너뛰면 실물이 통째로 누락된다.
            new_sn, _ = _clean_serial(a.get("serial"))
            old_sn = (cur["serial"] or "").strip()
            if new_sn and old_sn and new_sn.upper() != old_sn.upper():
                errors.append(
                    f"{i}행: 관리번호 {asset_no}는 이미 다른 기기에 쓰이고 있습니다"
                    f"(기존 시리얼 {old_sn} / 엑셀 {new_sn}). 번호를 확인하세요.")
                continue
            if not fill_blanks:
                dup.append(asset_no)
                continue
            patch = {}
            for f in FILLABLE_NUM:
                v = _to_int(a.get(f))
                if v and not (cur[f] or 0):
                    patch[f] = v
            for f in FILLABLE_TXT:
                v = (a.get(f) or "").strip()
                if f == "serial":
                    v, _ = _clean_serial(v)      # 메모가 시리얼로 새어 들어가지 않게
                    # 남의 시리얼을 빈 칸에 채워 넣으면 중복이 생긴다
                    if v and serial_owner.get(v.upper(), asset_no) != asset_no:
                        errors.append(
                            f"{i}행: 시리얼 {v}은(는) 이미 자산 "
                            f"{serial_owner[v.upper()]}에 등록돼 있어 채우지 않았습니다.")
                        continue
                if v and not (cur[f] or "").strip():
                    patch[f] = v
            # ★TMS 정정 감지 — 3방향 대조(2026-08-25). 그림자(지난 수집 때 TMS 값)와
            #   지금 OWS 값이 같을 때만 새 TMS 값을 따라간다. 다르면 사람이 고친 것 — 보호.
            #   시리얼은 제외(위 번호충돌 가드가 먼저다), TMS가 비운 칸은 지우지 않는다.
            try:
                shadow = json.loads(cur["tms_shadow"]) if cur["tms_shadow"] else {}
            except (ValueError, TypeError):
                shadow = {}
            corrections = {}
            new_shadow = {}
            for f in FILLABLE_NUM + FILLABLE_TXT:
                if f in FILLABLE_NUM:
                    tv = _to_int(a.get(f))
                    hv = int(cur[f] or 0)
                    try:
                        sv = int(shadow.get(f) or 0)
                    except (ValueError, TypeError):
                        sv = 0
                else:
                    tv = (a.get(f) or "").strip()
                    if f == "serial":
                        tv, _ = _clean_serial(tv)
                    hv = (cur[f] or "").strip()
                    sv = str(shadow.get(f) or "").strip()
                if tv:
                    new_shadow[f] = tv
                if f == "serial" or not tv or f in patch or not hv or hv == tv:
                    continue
                if sv and hv == sv:
                    corrections[f] = tv
            # ★카테고리도 3방향(2026-09-03, A5) — 그림자 category 는 '자동(연동·재판정)이 마지막으로 정한 값'이다.
            #   그 값이 지금 값과 같을 때만 새 TMS 판정을 따라가고, 다르면 사람이 고친 것 — 보호(그림자도 안 건드린다).
            #   키가 없는 옛 자산은 여기서 안 바꾼다 — 매입 ▸ 자산 ▸ [🗂 분류 재판정](reclass.py)이 사람 확인 아래 한 번에 맞춘다.
            category_change = {}
            if "category" in shadow:
                new_shadow["category"] = shadow["category"]     # 통째로 다시 쓰는 그림자에서 키가 빠지지 않게
                if str(shadow["category"]) == str(cur["category_id"]):
                    tgt, basis = category_decision(
                        cats, a.get("subcategory"), a.get("category"),
                        masters_map.get(model_key(a.get("model") or cur["model"])))
                    if tgt and tgt != cur["category_id"]:
                        category_change = {"category_to": tgt, "category_from": cur["category_id"],
                                           "category_basis": basis}
            shadow_changed = new_shadow != shadow
            meta = {"supplier": (a.get("supplier") or "").strip(),
                    "slip_no": (a.get("slip_no") or "").strip(),
                    "purchase_date": _to_date(a.get("purchase_date")),
                    "sale": _sale_info(a)}
            # ★상태 자동 갱신 — 정방향(나감/폐기)만. AUTO_STATUS_* 주석 참조.
            extra = {}
            tms_raw = (a.get("tms_status") or "").strip()
            mapped = TMS_STATUS_MAP.get(tms_raw.lower())
            cur_st = (cur["status"] or "").strip()
            if mapped and mapped != cur_st:
                if mapped in AUTO_STATUS_TO and cur_st in AUTO_STATUS_FROM:
                    extra = {"status_to": mapped, "status_from": cur_st,
                             "tms_status": tms_raw}
                elif mapped in ("in_stock", "ready") and cur_st in ("shipped", "scrapped"):
                    # 나갔던 물건이 돌아왔다는 표시 — 자동 복귀는 위험해서 알림만
                    extra = {"status_notice": tms_raw, "status_from": cur_st}
            # ★사업부 — TMS가 렌탈/반납이라고 하면 렌탈 귀속으로 맞춘다(잠금 자산 제외)
            if (tms_raw in TMS_RENTAL_STATUSES and not cur["division_locked"]
                    and (cur["division"] or "sale") != "rental"):
                extra["division_to"] = "rental"
                extra["division_from"] = cur["division"] or "sale"
            costs = tms_costs_of(a)
            have = tms_cost_rows.get(cur["id"], {})
            cost_changes = ({k: v for k, v in costs.items() if v != have.get(k, 0)} if costs else {})
            if patch or extra or corrections or shadow_changed or meta["sale"] or category_change or cost_changes:
                updates.append({"id": cur["id"], "asset_no": asset_no,
                                "patch": patch, "meta": meta,
                                "corrections": corrections,
                                "shadow": new_shadow if shadow_changed else None,
                                "prev": {f: (int(cur[f] or 0) if f in FILLABLE_NUM
                                             else (cur[f] or "")) for f in corrections},
                                "tms_costs": cost_changes,
                                **extra, **category_change})
            else:
                dup.append(asset_no)
            continue
        # ★한 번이라도 바로잡은 번호는 다시 만들지 않는다(2026-08-18).
        #   번호를 넘겨준 뒤 옛 자리가 비면, TMS는 그 번호를 '처음 보는 자산'으로 여겨
        #   유령을 새로 만든다 — 바로잡기 전 상태로 조용히 되돌아가는 길이다.
        if asset_no in fixed_nos:
            locked.append(asset_no)
            continue
        seen.add(asset_no)
        cat_name = (a.get("subcategory") or a.get("category") or "").strip()
        cat_id, _cat_basis = category_decision(cats, a.get("subcategory"), a.get("category"),
                                               masters_map.get(model_key(a.get("model"))))
        if cat_id is None:
            cat_id = default_cat["id"] if default_cat else None      # ④ 기본값 — categories sort 0(노트북)
        if cat_id is None:
            errors.append(f"{i}행: 카테고리를 정할 수 없습니다({cat_name}).")
            continue
        if allowed is not None and cat_id not in allowed:
            errors.append(f"{i}행: 담당 카테고리가 아닙니다({cat_name or '미지정'}).")
            continue
        grade, status, note = _resolve_grade_status(a)
        serial, serial_note = _clean_serial(a.get("serial"))
        if serial:
            owner = serial_owner.get(serial.upper())
            if owner:
                errors.append(
                    f"{i}행: 시리얼 {serial}은(는) 이미 자산 {owner}에 등록돼 있습니다"
                    f" (관리번호 {asset_no}). 같은 기기가 두 번 들어오지 않게 확인하세요.")
                continue
            serial_owner[serial.upper()] = asset_no      # 같은 파일 안 중복도 잡는다
        notes = " ".join(x for x in [(a.get("notes") or "").strip(), note, serial_note] if x)
        ready.append({
            # 그림자 — 이번 TMS 값 그대로. 다음 수집부터 3방향 대조가 돈다(2026-08-25)
            "tms_shadow": json.dumps(
                {**{f: (_to_int(a.get(f)) if f in FILLABLE_NUM
                        else (a.get(f) or "").strip())
                    for f in FILLABLE_NUM + FILLABLE_TXT
                    if (_to_int(a.get(f)) if f in FILLABLE_NUM else (a.get(f) or "").strip())},
                 # 카테고리 그림자 = 자동이 정한 값. 다음 수집부터 카테고리도 3방향 대조가 돈다(2026-09-03, A5)
                 "category": cat_id},
                ensure_ascii=False),
            # ★거래처·전표·매입일을 함께 들고 간다 — 이게 없으면 자산만 둥둥 떠서
            #   '어디서 언제 얼마에 샀는지'를 화면에서 되짚을 수 없다(2026-07-30 확인:
            #   TMS 거래처 2,130곳인데 OWS는 0곳이었다).
            "supplier": (a.get("supplier") or "").strip(),
            "slip_no": (a.get("slip_no") or "").strip(),
            "purchase_date": _to_date(a.get("purchase_date")),
            "sale": _sale_info(a),          # 신규 자산에도 판매 이력을 함께 남긴다
            "asset_no": asset_no, "category_id": cat_id, "grade": grade, "status": status,
            "maker": a.get("maker", ""), "model": a.get("model", ""), "serial": serial,
            "purchase_price": _to_int(a.get("purchase_price")),
            "sale_price": _to_int(a.get("sale_price")),
            "notes": notes,
            "stock_note": (a.get("stock_note") or "").strip(),
            "tms_costs": {k: v for k, v in (tms_costs_of(a) or {}).items() if v > 0},
            **{f: a.get(f, "") for f in SPEC_FIELDS if f != "location"},
            "location": a.get("location", ""),
        })
    return ready, dup, errors, updates, locked


def _fill_flag():
    """빈 칸 채우기 모드 여부. 폼/쿼리 어느 쪽으로 와도 받는다."""
    v = (request.form.get("fillBlanks") or request.args.get("fillBlanks") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _reject_sale_slip_sheet(rows):
    """판매 전표 파일을 자산 이관에 올리면 조용히 아무 일도 안 일어난다.

    ★관리번호가 없어 '오류 0건 / 등록 0건'으로 끝나는데, 화면은 성공처럼 보인다
      (2026-08-04 감사: 421건 올리고 성공한 줄 알고 넘어감). 어디로 가야 하는지 알려 준다.
    """
    from .sales import is_sale_slip_sheet
    if is_sale_slip_sheet(rows):
        abort(400, description="이건 판매 전표 파일(판매내역·판매미수금관리)입니다. "
                               "매입 → 💰 판매 전표 탭에서 올려 주세요.")


@bp.post("/assets/migrate/preview")
def migrate_preview():
    """저장 없이 결과만 계산."""
    require("purchase.edit")
    rows = _parse_upload()
    _reject_sale_slip_sheet(rows)
    fill = _fill_flag()
    with tx() as conn:
        ready, dup, errors, updates, locked = _prepare(conn, rows, fill_blanks=fill)
    fill_sum = sum(u["patch"].get("purchase_price", 0) for u in updates)
    return jsonify({
        "parsed": len(rows), "toCreate": len(ready), "duplicates": len(dup),
        # 번호를 바로잡아 잠근 자산 — TMS 값을 일부러 안 받는다
        "locked": len(locked), "lockedSample": locked[:10],
        "errors": errors[:20], "errorCount": len(errors),
        "fillBlanks": fill,
        "toUpdate": len(updates),
        # 이번에 채워질 매입가 합계 — 실행 전에 규모를 눈으로 확인하게 한다
        "fillPurchaseSum": fill_sum,
        "updateSample": [
            {"assetNo": u["asset_no"],
             "fields": ", ".join(u["patch"].keys()),
             "purchasePrice": u["patch"].get("purchase_price")}
            for u in updates[:20]
        ],
        "sample": [
            {"assetNo": a["asset_no"], "maker": a["maker"], "model": a["model"],
             "grade": a["grade"], "status": a["status"],
             "spec": " / ".join(x for x in (a["cpu"], a["ram"], a["ssd"]) if x)}
            for a in ready[:20]
        ],
    })


def apply_rows(conn, ready, updates, actor=None):
    """_prepare가 고른 행을 실제로 넣는다. 화면 이관과 자동반영이 함께 쓴다.

    거래처 → 전표 → 자산 순으로 만들고, 이미 있는 자산은 빈 칸만 채운다.
    """
    ts = config.now_iso()
    actor = actor or (g.user["display_name"] if g.get("user") else "이관")
    # ★거래처와 전표를 먼저 만들어 자산에 물려 준다.
    #   TMS 엑셀에 매입처명·매입전표·매입일이 들어 있으므로 그대로 재현한다.
    # ★별칭 행(alias_of)은 대표 거래처 id 로 — TMS 매입현황의 거래처명이 별칭 표기여도 전표가 대표 행에 붙는다(2026-09-03)
    sup_ids = {r["name"]: (r["alias_of"] or r["id"]) for r in conn.execute("SELECT id, name, alias_of FROM suppliers")}
    batch_ids = {r["slip_no"]: r["id"] for r in conn.execute(
        "SELECT id, slip_no FROM purchase_batches WHERE slip_no<>''")}
    new_sup = new_batch = 0
    # ★이 실행에서 '새로 만든' 전표만 담는다. 아래 매입금액 채우기가 이 집합만 보게 해서
    #   무관한 기존 전표의 금액을 건드리지 않는다(대조 안전장치 D2).
    #   ★2026-08-04: 이 변수 정의가 빠진 채 batch_id_for가 참조해 NameError로
    #     자동반영이 통째로 죽어 있었다(재고항목현황 반영 실패).
    created_batches = set()

    def supplier_id(name):
        nonlocal new_sup
        name = (name or "").strip()
        if not name:
            return None
        if name not in sup_ids:
            # 표기 차이(공백·대소문자)·별칭·부 신원 이름은 대표 거래처에 붙인다(masters.resolve_supplier_name) — 새로 만들지 않는다
            from .masters import resolve_supplier_name   # 순환 import 회피(masters 가 migration 을 지연 import 한다)
            hit = resolve_supplier_name(conn, name)
            if hit is not None:
                sup_ids[name] = hit
                return hit
            cur = conn.execute(
                "INSERT INTO suppliers(name, contact, phone, memo, created_at) "
                "VALUES(?,'','','[TMS 이관]',?)", (name, ts))
            sup_ids[name] = cur.lastrowid
            new_sup += 1
        return sup_ids[name]

    def batch_id_for(a):
        """전표번호가 있으면 그 전표에, 없으면 (거래처+매입일)로 하나 만들어 묶는다."""
        nonlocal new_batch
        slip = a.get("slip_no") or ""
        sup = a.get("supplier") or ""
        date = a.get("purchase_date") or ""
        if not slip and not sup:
            return None
        key = slip or f"TMS-{sup}-{date}"
        if key not in batch_ids:
            cur = conn.execute(
                "INSERT INTO purchase_batches(slip_no, stage, supplier_id, purchase_date, "
                "total_amount, memo, created_by, created_at, updated_at) "
                "VALUES(?,'purchased',?,?,0,'[TMS 이관]',?,?,?)",
                (key, supplier_id(sup), date or ts[:10], actor, ts, ts))
            batch_ids[key] = cur.lastrowid
            created_batches.add(cur.lastrowid)
            new_batch += 1
        return batch_ids[key]

    def log_sale(asset_id, sale):
        """'누구에게 나갔는지'를 자산 이력에 남긴다. 같은 판매를 두 번 적지 않는다.

        ★자산번호로 고객까지 되짚을 수 있어야 한다(대표 요청 2026-07-31).
          지나간 판매는 OWS 주문을 새로 만들지 않고 자산 이력으로 남긴다 —
          없던 주문을 지어내면 매출·정산이 이중으로 잡힌다.
        """
        if not sale:
            return 0
        key = sale.get("slip") or sale.get("date") or ""
        if key and conn.execute(
                "SELECT 1 FROM asset_events WHERE asset_id=? AND action='판매' "
                "AND detail LIKE ? LIMIT 1", (asset_id, f"%{key}%")).fetchone():
            return 0
        asset_event(conn, asset_id, "판매", {
            "판매일": sale.get("date", ""), "수령자": sale.get("buyer", ""),
            "채널": sale.get("channel", ""), "판매전표": sale.get("slip", ""),
            "판매가": sale.get("price", 0),
        })
        return 1

    sales_logged = 0

    cols = ["asset_no", "category_id", "grade", "status", "maker", "model", "serial",
            "purchase_price", "sale_price", "notes", "stock_note", "location", "cpu", "gpu", "ram",
            "ssd", "inch", "battery", "charger", "batch_id", "tier", "tms_shadow", "division",
            "created_by", "created_at", "updated_at"]
    costs_synced = 0
    for a in ready:
        a["batch_id"] = batch_id_for(a)
        # 등급이 매겨진 건 가용, '미정'은 실재고 — 가재고는 사람이 직접 옮긴다(대표 기준 2026-08-04)
        a["tier"] = tier_for_grade(a.get("grade"))
        # ★새로 들어오는 자산도 처음부터 올바른 사업부로 — 나중에 백필할 일을 만들지 않는다
        a["division"] = ("rental" if (a.get("tms_status") or "").strip() in TMS_RENTAL_STATUSES
                         else "sale")
        vals = [a.get(c, "") if c != "batch_id" else a["batch_id"] for c in cols[:-3]] + [actor, ts, ts]
        cur = conn.execute(
            f"INSERT INTO assets({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
        aid_new = cur.lastrowid
        asset_event(conn, aid_new, "TMS이관",
                    {"관리번호": a["asset_no"], "등급": a["grade"], "상태": a["status"],
                     "거래처": a.get("supplier", ""), "전표": a.get("slip_no", "")})
        sales_logged += log_sale(aid_new, a.get("sale"))
        costs_synced += 1 if sync_tms_costs(conn, aid_new, a.get("tms_costs"), a.get("purchase_date"), ts) else 0
    # ★빈 칸 채우기 — 값을 덮어쓰지 않는다(이미 있는 값은 사람이 넣은 것일 수 있다).
    filled = 0
    status_updated = 0
    status_reverts = 0
    division_updated = 0
    category_updated = 0
    cat_names = None                      # 이름표는 첫 분류갱신 때 한 번만 읽는다
    status_alerts = []
    # ★정정 폭주 방어 — 파일 하나가 CORRECTION_LIMIT 을 넘는 정정을 물고 오면
    #   깨진 엑셀일 가능성이 높다. 통째로 보류하고 화면(비고)으로 알린다.
    total_corr = sum(len(u.get("corrections") or {}) for u in updates)
    corr_frozen = total_corr > CORRECTION_LIMIT
    corrected = 0
    for u in updates:
        # ★TMS 수리비·부품비 추종(2026-09-03) — 출처 TMS연동 행만. 사람 기록·전표 금액·매입가는 무접촉
        if u.get("tms_costs"):
            costs_synced += 1 if sync_tms_costs(conn, u["id"], u["tms_costs"], (u.get("meta") or {}).get("purchase_date"), ts) else 0
        if u["patch"]:
            setc = ", ".join(f"{k}=?" for k in u["patch"])
            conn.execute(f"UPDATE assets SET {setc}, updated_at=? WHERE id=?",
                         list(u["patch"].values()) + [ts, u["id"]])
            asset_event(conn, u["id"], "TMS이관-보완", u["patch"])
            filled += 1
        # TMS 정정(3방향 대조 통과분) — 사람이 안 건드린 값만 여기 온다
        corr = u.get("corrections") or {}
        if corr and not corr_frozen:
            setc = ", ".join(f"{k}=?" for k in corr)
            conn.execute(f"UPDATE assets SET {setc}, updated_at=? WHERE id=?",
                         list(corr.values()) + [ts, u["id"]])
            asset_event(conn, u["id"], "TMS정정",
                        {f: {"from": (u.get("prev") or {}).get(f, ""), "to": v}
                         for f, v in corr.items()})
            corrected += 1
        # 그림자 갱신 — 정정을 보류했으면 그림자도 옛것으로 남겨 다음에 다시 판정하게 한다
        if u.get("shadow") is not None and not (corr and corr_frozen):
            conn.execute("UPDATE assets SET tms_shadow=? WHERE id=?",
                         (json.dumps(u["shadow"], ensure_ascii=False), u["id"]))
        # ★카테고리 추종(2026-09-03, A5) — _prepare 가 '그림자==현재값'으로 확인한 것만 온다.
        #   읽었을 때의 값일 때만 바꾼다(경합 방지). 바꾸면 그림자 category 도 새 값으로.
        if u.get("category_to"):
            n = conn.execute(
                "UPDATE assets SET category_id=?, updated_at=? WHERE id=? AND category_id=?",
                (u["category_to"], ts, u["id"], u.get("category_from"))).rowcount
            if n:
                if cat_names is None:
                    cat_names = category_names(conn)
                asset_event(conn, u["id"], "분류갱신",
                            {"from": cat_names.get(u.get("category_from"), u.get("category_from")),
                             "to": cat_names.get(u["category_to"], u["category_to"]),
                             "근거": u.get("category_basis", ""), "작업자": actor})
                set_shadow_category(conn, u["id"], u["category_to"])
                category_updated += 1
        # ★상태 자동 갱신 — _prepare 가 정방향(나감/폐기)으로 판정한 것만 온다.
        #   읽은 뒤 사람이 상태를 바꿨을 수 있으니 '그때 그 상태일 때만' 바꾼다(경합 방지).
        if u.get("status_to"):
            n = conn.execute(
                "UPDATE assets SET status=?, updated_at=? WHERE id=? AND status=?",
                (u["status_to"], ts, u["id"], u.get("status_from", ""))).rowcount
            if n:
                asset_event(conn, u["id"], "TMS상태갱신",
                            {"from": u.get("status_from", ""), "to": u["status_to"],
                             "TMS표기": u.get("tms_status", ""), "작업자": actor})
                status_updated += 1
                # ★나간/폐기된 자산이 몰 재고에 계속 세어지면 안 된다(유령 재고) —
                #   재고반영을 함께 끈다(2026-08-09 대표 승인 정합성 개선).
                m = conn.execute(
                    "UPDATE assets SET stock_listed=0, stock_listed_at='', stock_listed_by='' "
                    "WHERE id=? AND stock_listed=1", (u["id"],)).rowcount
                if m:
                    asset_event(conn, u["id"], "재고해제",
                                {"사유": "TMS 상태갱신(%s)" % u["status_to"]})
        # ★사업부 갱신 — 잠금 자산은 건드리지 않고, 읽었을 때의 값일 때만 바꾼다(경합 방지)
        if u.get("division_to"):
            n = conn.execute(
                "UPDATE assets SET division=?, division_since=?, division_by=?, "
                " division_ref='TMS-AUTO', updated_at=? "
                "WHERE id=? AND division=? AND division_locked=0",
                (u["division_to"], ts, "TMS 자동", ts, u["id"], u.get("division_from", "sale"))
            ).rowcount
            if n:
                asset_event(conn, u["id"], "사업부 자동판정",
                            {"from": u.get("division_from", ""), "to": u["division_to"],
                             "근거": "TMS 재고상태 " + (u.get("tms_status") or "렌탈/반납")})
                division_updated += 1
                if conn.execute(
                        "UPDATE assets SET stock_listed=0, stock_listed_at='', stock_listed_by='' "
                        "WHERE id=? AND stock_listed=1", (u["id"],)).rowcount:
                    asset_event(conn, u["id"], "재고해제", {"사유": "렌탈 사업부 귀속"})
        if u.get("status_notice"):
            # 나갔던 물건의 재고 복귀 표시 — 자동 반영하지 않고 알림만 남긴다
            status_reverts += 1
            if len(status_alerts) < 30:
                status_alerts.append(
                    "%s: TMS가 %r(재고 복귀)로 표시 — 현재 %s, 자동 반영 안 함(확인 필요)"
                    % (u["asset_no"], u["status_notice"], u.get("status_from", "")))
            # ★복귀후보 대기열(2026-08-09 대표 승인): 화면에서 원클릭 처리할 수 있게
            #   자산 이력에 남긴다. 열려 있거나(복귀후보) 무시 처리된(복귀후보종결)
            #   자산에는 또 쌓지 않는다 — TMS가 계속 '반입'이라고 말해도 2시간마다
            #   재알림되면 무시가 무의미해진다. '재고복귀'로 실제 복귀한 뒤
            #   다시 나갔다 돌아온 경우에만 새로 연다.
            last = conn.execute(
                "SELECT action FROM asset_events WHERE asset_id=? AND action IN "
                "('복귀후보','복귀후보종결','재고복귀') ORDER BY id DESC LIMIT 1",
                (u["id"],)).fetchone()
            if not last or last["action"] == "재고복귀":
                asset_event(conn, u["id"], "복귀후보",
                            {"TMS표기": u.get("status_notice", ""),
                             "당시상태": u.get("status_from", "")})
        # 판매 이력은 값 변경과 별개로 남긴다 — '누구에게 나갔는지'는 기록이지 수정이 아니다
        sales_logged += log_sale(u["id"], (u.get("meta") or {}).get("sale"))
    # 전표가 안 붙은 기존 자산에 거래처·전표를 이어 준다
    linked = 0
    for u in updates:
        if not u.get("meta"):
            continue
        cur_b = conn.execute("SELECT batch_id FROM assets WHERE id=?", (u["id"],)).fetchone()
        if cur_b and cur_b["batch_id"]:
            continue
        bid2 = batch_id_for(u["meta"])
        if bid2:
            conn.execute("UPDATE assets SET batch_id=?, updated_at=? WHERE id=?",
                         (bid2, ts, u["id"]))
            linked += 1
    # ★이관으로 만든 전표는 매입금액이 비어 있다(TMS 엑셀에 전표 총액 칸이 없다).
    #   그대로 두면 '전표 0원 vs 자산합 3,500만원'이 되어 화면이 476건 전부를
    #   불일치로 경고한다(2026-07-30 대표 지적). 자산 합계로 채워 준다.
    #   ★기존 전표까지 훑으면 사람이 0원으로 둔 전표를 이관이 멋대로 채운다 —
    #     이번 실행에서 만든 전표만 본다.
    for bid in created_batches:
        row = conn.execute(
            "SELECT b.total_amount, COALESCE(SUM(a.purchase_price),0) AS s "
            "FROM purchase_batches b LEFT JOIN assets a ON a.batch_id=b.id "
            "WHERE b.id=?", (bid,)).fetchone()
        if row and not (row["total_amount"] or 0) and row["s"]:
            conn.execute("UPDATE purchase_batches SET total_amount=?, updated_at=? WHERE id=?",
                         (row["s"], ts, bid))

    return {"created": len(ready), "updated": filled, "linked": linked,
            "corrected": corrected,                    # TMS 정정을 따라간 자산 수
            "correctionsFrozen": total_corr if corr_frozen else 0,
            "newSuppliers": new_sup, "newBatches": new_batch,
            "salesLogged": sales_logged,
            "costsSynced": costs_synced,              # TMS 수리비·부품비를 수리 기록에 맞춘 자산 수(2026-09-03)
            "statusUpdated": status_updated,
            "divisionUpdated": division_updated,          # TMS 따라 상태를 바꾼 자산 수
            "categoryUpdated": category_updated,          # TMS 중분류를 따라 카테고리를 바꾼 자산 수(2026-09-03)
            "statusReverts": status_reverts,          # 재고 복귀 후보(자동 반영 안 함)
            "statusAlerts": status_alerts}


@bp.post("/assets/migrate")
def migrate_run():
    require("purchase.edit")
    rows = _parse_upload()
    _reject_sale_slip_sheet(rows)
    fill = _fill_flag()
    with tx(write=True) as conn:
        ready, dup, errors, updates, locked = _prepare(conn, rows, fill_blanks=fill)
        res = apply_rows(conn, ready, updates)
        from .sales import is_sale_asset_sheet, upsert_asset_sales
        if is_sale_asset_sheet(rows):
            res["saleAssets"] = upsert_asset_sales(conn, rows, actor=g.user["display_name"])
        audit.log("assets_migrated", target=f"{res['created']}대",
                  detail={**res, "duplicates": len(dup), "errors": len(errors),
                          "locked": len(locked)})
    return jsonify({
        "parsed": len(rows), "created": res["created"], "updated": res["updated"],
        "linkedToBatch": res["linked"], "newSuppliers": res["newSuppliers"],
        "newBatches": res["newBatches"],
        "duplicates": len(dup), "fillBlanks": fill,
        "locked": len(locked), "lockedSample": locked[:10],
        "errors": errors[:20], "errorCount": len(errors),
    })


@bp.get("/assets/migrate/template")
def migrate_template():
    """이관용 엑셀 양식(헤더만) 내려받기."""
    require("purchase.edit")
    from flask import Response

    from ..importers import write_xlsx
    headers = ["관리번호", "대분류", "중분류", "브랜드", "모델명", "시리얼번호", "매입가", "판매가", "등급",
               "보관위치", "CPU", "그래픽", "RAM", "SSD", "인치", "배터리효율", "충전기유무",
               "재고상태", "매입상세비고"]
    sample = [["260719-0027", "PC", "노트북", "LENOVO", "L470", "", "77000", "150000", "AA", "A-1",
               "Intel Core i5-7300U", "Intel HD Graphics 620", "D4 8G", "2.5\" 256G", "14",
               "O", "X", "재고", ""]]
    content = write_xlsx(headers, sample)
    return Response(content, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=ows-asset-migration-template.xlsx"})
