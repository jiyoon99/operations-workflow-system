# -*- coding: utf-8 -*-
"""업데이트 내역 — 화면 좌측 상단 📢에서 보는 '무엇이 바뀌었나'.

대표 요청(2026-08-14): "그날 작업한 것을 날짜별로 1. 2. 3. 으로 요약해 두면
실제 작업자들이 어떤 게 바뀌었는지 알 수 있다."

설계
- 읽기는 **로그인한 모두**에게 연다. 셋팅 담당·배송 담당이 봐야 의미가 있는 기능이라
  권한으로 막으면 정작 볼 사람이 못 본다(쓰기만 settings.manage).
- 하루치를 통째로 저장한다(POST에 그날 항목 전부) — 같은 날 다시 저장하면 갈아끼운다.
  세션이 끝날 때마다 그날 것을 다시 정리해 올려도 줄이 겹치지 않는다.
"""
from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import get_db, tx
from . import bp

MAX_ITEMS = 30          # 하루 항목 수 — 이보다 많으면 요약이 아니다
MAX_LEN = 300           # 한 줄 길이


@bp.get("/changelog")
def list_changelog():
    """최근 날짜부터. 로그인한 사람은 누구나 볼 수 있다(권한 검사 없음)."""
    try:
        limit = max(1, min(int(request.args.get("days") or 60), 365))
    except (TypeError, ValueError):
        limit = 60
    rows = get_db().execute(
        "SELECT day, seq, text, created_by FROM changelog "
        "WHERE day IN (SELECT day FROM changelog GROUP BY day ORDER BY day DESC LIMIT ?) "
        "ORDER BY day DESC, seq", (limit,)).fetchall()
    days, author, order = {}, {}, []
    for r in rows:
        if r["day"] not in days:
            days[r["day"]] = []
            order.append(r["day"])
        days[r["day"]].append(r["text"])
        # 그날 항목을 남긴 사람 — 화면 오른쪽에 '2건 · 대표'처럼 붙는다
        author.setdefault(r["day"], r["created_by"] or "")
    return jsonify({
        "days": [{"day": d, "items": days[d], "author": author.get(d, "")} for d in order],
        "latest": order[0] if order else "",
        "canEdit": bool(g.user["is_admin"] or "settings.manage" in g.perms),
        "today": config.now().strftime("%Y-%m-%d"),
    })


@bp.post("/changelog")
def save_changelog():
    """하루치를 통째로 저장(같은 날이면 갈아끼운다). items가 비면 그날 기록을 지운다."""
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    day = (body.get("day") or "").strip() or config.now().strftime("%Y-%m-%d")
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        abort(400, description="날짜는 2026-08-14 형식이어야 합니다.")
    items = body.get("items")
    if isinstance(items, str):        # 화면이 여러 줄 텍스트로 보내는 경우
        items = items.splitlines()
    if not isinstance(items, list):
        abort(400, description="항목 목록이 필요합니다.")
    clean = []
    for raw in items:
        t = str(raw or "").strip()
        # 사람이 '1.' '- ' 를 붙여 적어도 번호는 화면이 매기므로 떼어 낸다
        while t[:1] in ("-", "·", "*"):
            t = t[1:].strip()
        i = 0
        while i < len(t) and t[i].isdigit():
            i += 1
        if i and t[i:i + 1] in (".", ")"):
            t = t[i + 1:].strip()
        if not t:
            continue
        if len(t) > MAX_LEN:
            abort(400, description=f"한 줄이 너무 깁니다({MAX_LEN}자 이내): {t[:40]}…")
        clean.append(t)
    if len(clean) > MAX_ITEMS:
        abort(400, description=f"하루 항목은 {MAX_ITEMS}개까지입니다 — 요약해 주세요.")
    ts = config.now_iso()
    with tx(write=True) as conn:
        conn.execute("DELETE FROM changelog WHERE day=?", (day,))
        for i, t in enumerate(clean, 1):
            conn.execute(
                "INSERT INTO changelog(day, seq, text, created_at, created_by) "
                "VALUES(?,?,?,?,?)", (day, i, t, ts, g.user["display_name"]))
        audit.log("changelog_saved", target=day, detail={"항목": len(clean)})
    return jsonify({"day": day, "items": clean})
