"""CJ대한통운 집화 가능일 — 쉬는 날에는 회수(택배) 예약을 받지 않는다.

★왜 막아야 하나(2026-09-07 대표): 예약은 우리 화면에서 그냥 들어가지만, 기사가 안 오는 날이면
  고객은 하루 종일 기다리다 헛걸음하고 우리는 그걸 며칠 뒤에야 안다. 날짜를 고르는 그 자리에서
  막는 게 유일하게 확실한 방법이다.

판정 규칙 — 셋 중 하나라도 걸리면 그 날은 예약 불가:
  ① 지난 날짜(오늘 이전)
  ② 쉬는 요일 — 기본 일요일. 토요일은 계약·지역에 따라 달라 설정에서 켜고 끈다.
  ③ 공휴일 — 날짜가 고정된 것(FIXED_HOLIDAYS)은 코드가 알고, 음력·대체공휴일은 설정에 적는다.

★음력 공휴일(설날·부처님오신날)과 대체공휴일은 해마다 날짜가 달라 코드에 못 박지 않는다.
  2026년분 중 확인된 것만 시드로 넣고(docs/CUTOVER_PLAN.md 가 확정한 추석·대체공휴일),
  나머지는 설정 ▸ API 관리 ▸ CJ ▸ 집화 휴무일에서 대표가 추가한다. 등록 안 된 공휴일은
  막히지 않으므로, 화면이 '등록된 휴무일'을 그대로 보여 준다(모르는 것을 아는 척하지 않는다).
"""
from __future__ import annotations

from datetime import date, timedelta

WEEKDAY_LABEL = ("월", "화", "수", "목", "금", "토", "일")   # date.weekday(): 월=0 … 일=6

# 날짜가 해마다 같은 국가 공휴일(양력)
FIXED_HOLIDAYS = {
    (1, 1): "신정",
    (3, 1): "삼일절",
    (5, 5): "어린이날",
    (6, 6): "현충일",
    (8, 15): "광복절",
    (10, 3): "개천절",
    (10, 9): "한글날",
    (12, 25): "성탄절",
}

# 음력·대체공휴일 시드 — 2026년분만, 근거가 있는 것만(docs/CUTOVER_PLAN.md).
# 설정에 저장된 목록이 있으면 그것이 이긴다(대표가 지운 날은 지워진 채로 둔다).
SEED_HOLIDAYS = {
    "2026-09-24": "추석 연휴",
    "2026-09-25": "추석",
    "2026-09-26": "추석 연휴",
    "2026-09-27": "추석 연휴",
    "2026-10-05": "개천절 대체공휴일",
}

DEFAULT_OFF_WEEKDAYS = (6,)          # 일요일. 토요일(5)은 대표가 설정에서 켠다.
MAX_AHEAD_DAYS = 60                  # 두 달 뒤까지만 — 그 이상은 오타로 본다


def pickup_settings(cfg):
    """CJ 설정에서 집화 휴무 규칙을 꺼낸다 — 없으면 기본값.

    {"offWeekdays": [6], "holidays": {"2026-09-25": "추석"}, "enabled": True}
    enabled=False 면 아무것도 막지 않는다(예외 상황에서 대표가 끌 수 있는 스위치).
    """
    raw = (cfg or {}).get("pickup") or {}
    try:
        off = [int(x) for x in raw.get("offWeekdays", DEFAULT_OFF_WEEKDAYS) if 0 <= int(x) <= 6]
    except (TypeError, ValueError):
        off = list(DEFAULT_OFF_WEEKDAYS)
    holidays = raw.get("holidays")
    if not isinstance(holidays, dict):
        holidays = dict(SEED_HOLIDAYS)
    return {
        "enabled": raw.get("enabled", True) is not False,
        "offWeekdays": sorted(set(off)),
        "holidays": {str(k): str(v or "공휴일") for k, v in holidays.items()},
    }


def _as_date(value):
    """'2026-09-08' → date. 못 읽으면 None(모양이 이상한 값은 호출자가 따로 막는다)."""
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def holiday_name(day, rules):
    """이 날이 공휴일이면 이름, 아니면 빈 문자열."""
    named = rules["holidays"].get(day.isoformat())
    if named:
        return named
    return FIXED_HOLIDAYS.get((day.month, day.day), "")


def pickup_block_reason(value, cfg, *, today=None):
    """이 날짜로 집화(회수)를 예약할 수 없는 이유. 예약해도 되면 빈 문자열.

    value 가 비어 있으면 빈 문자열 — 수거 희망일은 선택 항목이라 '안 고름'은 막지 않는다
    (CJ 는 날짜를 안 주면 통상 다음 영업일에 배차한다).
    """
    rules = pickup_settings(cfg)
    if not rules["enabled"]:
        return ""
    day = _as_date(value)
    if day is None:
        return "" if not str(value or "").strip() else "수거 희망일 형식이 올바르지 않습니다(YYYY-MM-DD)."
    ref = today or date.today()
    if day < ref:
        return f"{day.isoformat()}은 지난 날짜입니다 — 오늘({ref.isoformat()}) 이후로 골라 주세요."
    if (day - ref).days > MAX_AHEAD_DAYS:
        return f"{day.isoformat()}은 너무 멉니다 — {MAX_AHEAD_DAYS}일 이내로 골라 주세요."
    name = holiday_name(day, rules)
    if name:
        return f"{day.isoformat()}은 {name}이라 CJ 집화를 하지 않습니다."
    if day.weekday() in rules["offWeekdays"]:
        return f"{day.isoformat()}은 {WEEKDAY_LABEL[day.weekday()]}요일이라 CJ 집화를 하지 않습니다."
    return ""


def next_pickup_day(cfg, *, today=None, start=None):
    """예약할 수 있는 가장 이른 날 — 화면이 기본값으로 쓴다.

    ★기본값을 '내일'로 고정하면 그 내일이 일요일·공휴일일 때 열자마자 막힌 날짜가 보인다.
    """
    ref = today or date.today()
    day = _as_date(start) or (ref + timedelta(days=1))
    if day < ref:
        day = ref
    for _ in range(MAX_AHEAD_DAYS + 1):
        if not pickup_block_reason(day.isoformat(), cfg, today=ref):
            return day.isoformat()
        day += timedelta(days=1)
    return day.isoformat()          # 두 달이 전부 막히는 일은 없지만, 무한 루프는 만들지 않는다


def pickup_calendar(cfg, *, today=None, days=45, start=None):
    """오늘부터 days 일치의 가능/불가 표 — 화면이 달력에서 쉬는 날을 회색으로 만든다."""
    ref = today or date.today()
    first = _as_date(start) or ref
    out = []
    for i in range(max(1, min(int(days or 45), MAX_AHEAD_DAYS + 1))):
        day = first + timedelta(days=i)
        reason = pickup_block_reason(day.isoformat(), cfg, today=ref)
        out.append({"date": day.isoformat(), "weekday": WEEKDAY_LABEL[day.weekday()],
                    "ok": not reason, "reason": reason,
                    "holiday": holiday_name(day, pickup_settings(cfg))})
    return out
