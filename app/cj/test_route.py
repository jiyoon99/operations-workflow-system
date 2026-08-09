"""CJ대한통운 연결 테스트 — 토큰 발급만 확인한다.

토큰 발급(ReqOneDayToken)은 조회성 호출이라 배송 예약이 생기지 않는다.
실접수(RegBook)는 절대 부르지 않는다 — 실물이 움직이는 부수효과가 있기 때문.
"""
import json

from flask import abort, jsonify

from ..auth.perms import require
from ..db import get_db
from ..settings import bp
from .client import CJ2_HOSTS, _cj2_base, _cj2_cust, cj2_token


@bp.post("/cj/test")
def cj_test():
    require("settings.manage")
    row = get_db().execute("SELECT value FROM settings WHERE key='cj'").fetchone()
    cfg = {}
    if row:
        try:
            cfg = json.loads(row["value"]) or {}
        except ValueError:
            cfg = {}
    cust = (_cj2_cust(cfg) or "").strip()
    biz = (cfg.get("biz_reg_num") or "").strip()
    if not cust or not biz:
        abort(400, description="고객코드와 사업자등록번호를 먼저 입력하고 저장하세요.")

    env = cfg.get("env", "dev")
    try:
        token = cj2_token(cfg, force=True)
    except Exception as e:
        abort(502, description=f"CJ 연결 실패: {e}")
    if not token:
        abort(502, description="토큰을 발급받지 못했습니다. 고객코드·사업자등록번호를 확인하세요.")
    return jsonify({
        "ok": True,
        "env": env,
        "host": _cj2_base(cfg),
        "custId": cust,
        "armed": bool(cfg.get("armed")),
        "message": f"연결 성공 — {'운영' if env == 'prod' else '개발'} 서버에서 토큰을 발급받았습니다."
                   + ("" if cfg.get("armed") and env == "prod"
                      else " (실발행하려면 환경=운영 + 실발행 무장을 켜세요. 지금은 테스트 발행만 됩니다.)"),
    })
