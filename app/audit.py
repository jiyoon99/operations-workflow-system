"""감사로그 — 모든 쓰기 작업의 누가/언제/무엇을 append-only 기록 (원칙 #7).

호출 측이 tx() 안에서 부르면 같은 트랜잭션에 묶이고, 밖에서 부르면 즉시 기록된다.
"""
import json

from flask import g

from . import config
from .db import get_db


def log(action, target=None, detail=None):
    user = g.get("user")
    get_db().execute(
        "INSERT INTO audit_log(ts, user_id, username, action, target, detail) VALUES(?,?,?,?,?,?)",
        (
            config.now_iso(),
            user["id"] if user is not None else None,
            user["display_name"] if user is not None else None,
            action,
            target,
            json.dumps(detail, ensure_ascii=False) if detail is not None else None,
        ),
    )
