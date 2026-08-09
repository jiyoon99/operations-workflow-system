"""셋팅 실적 — 누가 몇 대를 셋팅했나(일/주/월/분기/연도 + 캘린더).

대표 정의(2026-07-29): "제품을 준비해서 **검수완료까지 끝낸 것**을 한 대 셋팅한 것으로 본다."
그래서 검수완료(inspection_done)된 주문만 세고, 날짜는 검수를 마친 시각을 쓴다.

대수 세는 법: 매칭된 자산 수를 우선한다(한 주문에 노트북 2대면 2대).
자산을 아직 안 붙였으면 주문 수량으로 센다 — 실물은 이미 만들었는데 0대로 세면 실적이 사라진다.

실적 주인: 제작 완료를 누른 사람(= 실제로 만든 사람). 검수만 다른 사람이 했으면
그 사람 몫은 'inspected'로 따로 보여 준다(둘을 더하면 이중계상이 된다).
"""
import calendar as _cal
from datetime import date, datetime, timedelta

from flask import abort, jsonify, request

from ..auth.perms import require_any
from ..db import get_db
from . import bp

PERIODS = ("day", "week", "month", "quarter", "year")


def _parse_date(text, default=None):
    t = (text or "").strip()[:10]
    if not t:
        return default or date.today()
    try:
        return datetime.strptime(t, "%Y-%m-%d").date()
    except ValueError:
        abort(400, description="날짜 형식은 2026-07-29 처럼 넣어 주세요.")


def period_range(kind, anchor):
    """기간 종류 + 기준일 → (시작일, 끝일, 사람이 읽는 이름)."""
    if kind == "day":
        return anchor, anchor, f"{anchor.year}년 {anchor.month}월 {anchor.day}일"
    if kind == "week":
        start = anchor - timedelta(days=anchor.weekday())      # 월요일 시작
        end = start + timedelta(days=6)
        return start, end, f"{start.month}/{start.day}~{end.month}/{end.day} 주"
    if kind == "month":
        start = anchor.replace(day=1)
        end = anchor.replace(day=_cal.monthrange(anchor.year, anchor.month)[1])
        return start, end, f"{anchor.year}년 {anchor.month}월"
    if kind == "quarter":
        q = (anchor.month - 1) // 3 + 1
        start = date(anchor.year, 3 * q - 2, 1)
        last_month = 3 * q
        end = date(anchor.year, last_month, _cal.monthrange(anchor.year, last_month)[1])
        return start, end, f"{anchor.year}년 {q}분기"
    start = date(anchor.year, 1, 1)
    return start, date(anchor.year, 12, 31), f"{anchor.year}년"


def _prev_anchor(kind, start):
    """직전 같은 길이의 기간(비교용)."""
    if kind == "day":
        return start - timedelta(days=1)
    if kind == "week":
        return start - timedelta(days=7)
    if kind == "month":
        return (start.replace(day=1) - timedelta(days=1))
    if kind == "quarter":
        # ★직전 '분기'의 시작일. -62일 같은 어림수를 쓰면 한 분기를 건너뛴다
        #   (3분기를 2분기가 아니라 1분기와 비교해 증감 판단이 통째로 틀렸다).
        q = (start.month - 1) // 3          # 0~3
        return (start.replace(year=start.year - 1, month=10, day=1) if q == 0
                else start.replace(month=q * 3 - 2, day=1))
    return start.replace(year=start.year - 1)


def _rows(conn, start, end):
    """검수완료된 주문 + 그 대수 — 기간 안의 것만."""
    return conn.execute(
        """
        SELECT o.id, o.production_by, o.inspection_by, o.quantity,
               SUBSTR(o.inspection_at, 1, 10) AS day,
               (SELECT COUNT(*) FROM order_assets oa WHERE oa.order_id = o.id) AS asset_cnt
        FROM orders o
        WHERE o.inspection_done = 1 AND o.cancelled_at = ''
          AND SUBSTR(o.inspection_at, 1, 10) BETWEEN ? AND ?
        """, (start.isoformat(), end.isoformat())).fetchall()


def _units(row):
    """이 주문이 몇 대인가 — 매칭 자산 수 우선, 없으면 주문 수량."""
    return int(row["asset_cnt"] or 0) or max(1, int(row["quantity"] or 1))


@bp.get("/reports/setup-stats")
def setup_stats():
    """담당자별 셋팅 실적 + 날짜별 캘린더."""
    # 설정 화면 탭으로도 열리므로 settings.manage도 받는다 — 탭은 보이는데 내용이
    # 권한 오류로 비어 있던 문제(2026-07-29 전수조사)
    require_any("reports.view", "setup.view", "orders.work", "settings.manage")
    kind = (request.args.get("period") or "month").strip()
    if kind not in PERIODS:
        abort(400, description="기간은 day/week/month/quarter/year 중 하나여야 합니다.")
    anchor = _parse_date(request.args.get("date"))
    start, end, label = period_range(kind, anchor)

    conn = get_db()
    rows = _rows(conn, start, end)

    staff, by_day, total_units = {}, {}, 0
    for r in rows:
        units = _units(r)
        total_units += units
        maker = (r["production_by"] or "").strip() or "(담당자 미기록)"
        s = staff.setdefault(maker, {"name": maker, "units": 0, "orders": 0, "inspected": 0})
        s["units"] += units
        s["orders"] += 1
        checker = (r["inspection_by"] or "").strip()
        if checker:
            c = staff.setdefault(checker,
                                 {"name": checker, "units": 0, "orders": 0, "inspected": 0})
            c["inspected"] += units
        day = r["day"] or ""
        if day:
            d = by_day.setdefault(day, {"date": day, "units": 0, "byStaff": {}})
            d["units"] += units
            d["byStaff"][maker] = d["byStaff"].get(maker, 0) + units

    # 직전 같은 기간과 비교 — 늘었는지 줄었는지 한눈에
    p_start, p_end, p_label = period_range(kind, _prev_anchor(kind, start))
    prev_units = sum(_units(r) for r in _rows(conn, p_start, p_end))

    return jsonify({
        "period": {"type": kind, "from": start.isoformat(), "to": end.isoformat(),
                   "label": label},
        "total": {"units": total_units, "orders": len(rows),
                  "staffCount": len([s for s in staff.values() if s["units"]])},
        "previous": {"label": p_label, "units": prev_units,
                     "diff": total_units - prev_units},
        "staff": sorted(staff.values(), key=lambda x: (-x["units"], -x["inspected"], x["name"])),
        "calendar": [by_day[k] for k in sorted(by_day)],
    })


@bp.get("/reports/setup-day")
def setup_day():
    """캘린더에서 하루를 눌렀을 때 — 그날 셋팅한 건 목록."""
    require_any("reports.view", "setup.view", "orders.work", "settings.manage")
    day = _parse_date(request.args.get("date"))
    conn = get_db()
    rows = conn.execute(
        """
        SELECT o.id, o.channel, o.order_no, o.product_name, o.recipient, o.quantity,
               o.production_by, o.inspection_by, o.inspection_at,
               (SELECT COUNT(*) FROM order_assets oa WHERE oa.order_id = o.id) AS asset_cnt
        FROM orders o
        WHERE o.inspection_done = 1 AND o.cancelled_at = ''
          AND SUBSTR(o.inspection_at, 1, 10) = ?
        ORDER BY o.inspection_at
        """, (day.isoformat(),)).fetchall()
    return jsonify({
        "date": day.isoformat(),
        "units": sum(_units(r) for r in rows),
        "orders": [{
            "id": r["id"], "channel": r["channel"],
            "orderNumber": r["order_no"], "productName": r["product_name"],
            "recipient": r["recipient"], "units": _units(r),
            "productionBy": r["production_by"], "inspectionBy": r["inspection_by"],
            "at": (r["inspection_at"] or "")[11:16],
        } for r in rows],
    })
