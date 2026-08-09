"""스펙 입력 자동완성 후보 제공.

후보 = ①내장 카탈로그(spec_catalog.py, 2010년 이후 CPU/GPU/RAM/SSD 표기)
      + ②이미 등록된 자산에서 실제로 쓰인 값(운영하면서 자동으로 늘어난다)
카탈로그에 없는 값도 자유롭게 입력할 수 있고, 한 번 쓰면 다음부터 후보에 나온다.
"""
from flask import abort, jsonify, request

from ..auth.perms import require
from ..db import get_db
from . import bp
from .spec_catalog import CATALOG

# 자동완성을 지원하는 자산 컬럼
SUPPORTED = {"cpu", "gpu", "ram", "ssd", "inch", "battery", "charger", "location", "maker", "model"}


def _used_values(field, q, limit):
    """이미 등록된 자산에서 쓰인 값(빈도순)."""
    sql = (f"SELECT {field} AS v, COUNT(*) AS c FROM assets "
           f"WHERE {field} != ''")
    params = []
    if q:
        sql += f" AND {field} LIKE ?"
        params.append("%" + q + "%")
    sql += f" GROUP BY {field} ORDER BY c DESC, v LIMIT ?"
    params.append(limit)
    return [r["v"] for r in get_db().execute(sql, params).fetchall()]


def _match(items, q, limit):
    """부분일치 필터. 앞부분 일치를 먼저 보여준다."""
    if not q:
        return items[:limit]
    ql = q.lower()
    starts, contains = [], []
    for it in items:
        low = it.lower()
        if low.startswith(ql):
            starts.append(it)
        elif ql in low:
            contains.append(it)
        if len(starts) >= limit:
            break
    return (starts + contains)[:limit]


@bp.get("/spec-options")
def spec_options():
    """?field=cpu&q=i5 → 자동완성 후보 목록."""
    require("purchase.view")
    field = (request.args.get("field") or "").strip().lower()
    if field not in SUPPORTED:
        abort(400, description=f"자동완성을 지원하지 않는 항목입니다: {field}")
    q = (request.args.get("q") or "").strip()
    try:
        limit = min(max(int(request.args.get("limit", 30)), 1), 100)
    except ValueError:
        limit = 30

    used = _match(_used_values(field, q, limit * 2), q, limit)
    catalog = _match(CATALOG.get(field, []), q, limit * 2)

    # 이미 쓰던 값을 위로, 그 다음 카탈로그. 중복 제거(대소문자 무시)
    out, seen = [], set()
    for group, source in (("used", used), ("catalog", catalog)):
        for v in source:
            key = v.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"value": v, "source": group})
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    return jsonify({"field": field, "q": q, "options": out,
                    "catalogSize": len(CATALOG.get(field, []))})
