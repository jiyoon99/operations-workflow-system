"""관리번호 규칙(2026-09-03, A4) — OWS 채번(5000~)과 손입력 번호 검증을 한곳에서.

배경(대표 방침 2026-09-02 "모든 데이터는 OWS·RMS에서 직접 등록·관리", 설계 09§1-2 · 11 H-2·M-1):
  형식은 TMS와 같은 YYMMDD-NNNN 이고 번호대만 나눈다 — TMS 0001~4999 / OWS 5000~9999(OWS_ASSET_SEQ_START).
  이 불변식은 TMS 쪽에서 강제되지 않는다(TMS 일련부는 손입력이 된다). 그래서 OWS가 두 가지를 지킨다.

  ① 손입력 검증 — validate_manual_no(): 사람이 관리번호를 타이핑하는 경로(자산 등록·번호 변경·번호 바로잡기)
     · TMS 대역(<5000): 연동 창구 mgmt-check 로 사본에 있는 번호만 받는다.
         없음 → 400 "TMS 자산 원장에 없는 번호입니다 (오타 확인)" / 삭제됨 → 400 / 중복 → 400
         검증불가(사본이 낡음)·창구 미설정·창구 오류 → 허용하되 asset_events '번호 미확인' 으로 남긴다
         (막지 않는다 — 등록을 창구 가용성에 묶지 않는다. 설계 (C)안 "사본·TMS 가용성과 무관").
     · OWS 대역(≥5000): 사본에 있으면 400 "TMS에 이미 있는 번호입니다 — 번호대 충돌", 없으면 통과.
       통과한 번호는 그날 채번 순번(asset_no_counters)에 반영 — 자동 채번이 같은 번호를 다시 내지 않는다.
     · 형식이 아닌 값(옛 'TMS-0001'류)은 예전 그대로(길이·UNIQUE 검사만).
  ② 채번 보류 — band_conflict(): 그날 사본에 OWS 대역 번호가 살아 있으면(TMS 에 누가 5000 이상을 손입력)
     자동 채번을 409 "번호대 충돌 가능 — 연동 사본 확인 필요" 로 멈춘다. 사본을 못 읽는 상황(미설정·낡음·오류)은
     보류하지 않는다 — 단정 못 하는 것은 응답의 verified=false 로만 알린다.

★외부 호출(창구 GET)은 tx(write=True) 안에서 하지 않는다(CLAUDE.md 절대 규칙 7).
  → 라우트가 트랜잭션에 들어가기 전에 precheck_*() 가 번호 목록을 '한 번에' 물어 g.asset_no_check 에 두고,
    트랜잭션 안의 validate_manual_no() / next_asset_no() 는 그 결과만 본다.
"""
import os
import re

from flask import abort, current_app, g, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import get_db, tx
from . import OWS_ASSET_SEQ_START, asset_event, bp, next_asset_no, tms_link

NO_RE = re.compile(r"^(\d{6})-(\d{4})$")
# (연결, 응답) 초 — 등록 화면이 창구 장애에 3분(Client 기본)씩 매달리지 않게 짧게 잡는다
DATALINK_TIMEOUT = (5, 20)
FACTS_PAGE = 2000
CHECK_CHUNK = 500                # 창구 mgmt-check 한 번에 묻는 최대 개수(창구 한도)
HOLD_MESSAGE = "번호대 충돌 가능 — 연동 사본 확인 필요"
EVENT_UNVERIFIED = "번호 미확인"


def parse_no(no):
    """'YYMMDD-NNNN' → (YYMMDD, 일련) / 형식이 아니면 None."""
    m = NO_RE.match(str(no or "").strip())
    return (m.group(1), int(m.group(2))) if m else None


def is_ows_band(seq):
    return seq >= OWS_ASSET_SEQ_START


# ---------------------------------------------------------------- 창구 조회(tx 밖에서만)
def _client():
    """연동 창구 클라이언트 — 설정이 없으면 None(검증은 건너뛰고 '미설정'으로 기록).

    ★시험 중에는 절대 실제 창구를 부르지 않는다(2026-09-03): 185 에서 전체 시험을 돌리면
      운영 설정(data\\datalink.json)을 읽어 시험용 가짜 번호를 '없음'으로 거부해
      TestNumberConflict·TestTmsReimportAfterFix 등이 무더기로 깨졌다. 가짜 창구를 쓰는
      시험은 이 함수를 직접 몽키패치하므로 여기서 막아도 그 시험들은 그대로 돈다.
    """
    if current_app and current_app.config.get("TESTING") and not os.getenv("OWS_DATALINK_URL"):
        return None      # 시험은 가짜 창구를 환경변수로 지정한다 — 그때만 창구를 쓴다
    cfg = tms_link.load_config()
    if not cfg["enabled"]:
        return None
    return tms_link.Client(cfg["url"], cfg["token"])


def _verdicts(cli, nos):
    """mgmt-check 일괄. 반환 {번호: verdict}. 형식 번호만 묻고, 창구가 없으면 {} , 오류면 못 받은 번호는 '검증불가'."""
    nos = [n for n in dict.fromkeys(str(x or "").strip() for x in nos) if parse_no(n)]
    if not nos or cli is None:
        return {}
    out = {}
    try:
        for i in range(0, len(nos), CHECK_CHUNK):
            chunk = nos[i:i + CHECK_CHUNK]
            data = cli.get("/mgmt-check", nos=",".join(chunk), timeout=DATALINK_TIMEOUT)
            result = data.get("result") or {}
            for n in chunk:
                out[n] = str((result.get(n) or {}).get("verdict") or "검증불가")
    except Exception:                                            # noqa: BLE001 — 창구 장애는 등록을 못 막는다
        pass
    for n in nos:
        out.setdefault(n, "검증불가")
    return out


def check_numbers(nos):
    """손입력 번호들을 창구에 한 번에 묻는다(tx 밖). 화면 미리보기·시험용 공개 함수."""
    return _verdicts(_client(), nos)


def band_conflict(cli=None, today=None):
    """그날 연동 사본에 OWS 대역(≥5000) 번호가 살아 있나 — tx 밖.

    반환 {"hold": 409 메시지|None, "verified": 사본을 실제로 봤는가, "conflicts": [...], "reason": 못 본 이유}.
    """
    day = today or config.now()
    prefix = day.strftime("%y%m%d")
    if cli is None:
        cli = _client()
    if cli is None:
        return {"hold": None, "verified": False, "conflicts": [], "reason": "연동 창구 미설정"}
    conflicts, after = [], 0
    since = day.strftime("%Y-%m-%d") + " 00:00:00"
    try:
        while True:
            data = cli.get("/asset-facts", since=since, after_key=after, limit=FACTS_PAGE,
                           timeout=DATALINK_TIMEOUT)
            if (data.get("freshness") or {}).get("stale"):
                return {"hold": None, "verified": False, "conflicts": [], "reason": "연동 사본이 낡음"}
            for it in data.get("items") or []:
                p = parse_no(it.get("관리번호"))
                if p and p[0] == prefix and is_ows_band(p[1]) and not it.get("_deleted_at"):
                    conflicts.append(str(it.get("관리번호")).strip())
            after = data.get("next_after_key")
            if not after:
                break
    except Exception as e:                                       # noqa: BLE001
        return {"hold": None, "verified": False, "conflicts": [], "reason": "창구 오류: " + str(e)[:120]}
    conflicts = sorted(set(conflicts))
    hold = None
    if conflicts:
        hold = "%s: 오늘 TMS 사본에 %s%s" % (
            HOLD_MESSAGE, ", ".join(conflicts[:5]),
            " 외 %d건" % (len(conflicts) - 5) if len(conflicts) > 5 else "")
    return {"hold": hold, "verified": True, "conflicts": conflicts, "reason": ""}


# ---------------------------------------------------------------- 라우트가 tx 전에 부르는 사전 조회
def _put(verdicts, hold, enabled):
    g.asset_no_check = {"verdicts": verdicts, "hold": hold, "enabled": enabled}


def precheck_bodies(items):
    """자산 등록 요청(1건 또는 전표 동시저장 N줄)의 관리번호를 tx 밖에서 한 번에 판정해 g 에 둔다.

    손입력 번호는 mgmt-check, 자동 채번이 필요한 줄이 하나라도 있으면 그날 번호대 충돌(채번 보류)까지 본다.
    """
    items = [it for it in (items if isinstance(items, list) else []) if isinstance(it, dict)]
    manual = [m for m in (str(it.get("assetNo") or "").strip() for it in items) if m]
    cli = _client()
    hold = None
    if cli is not None and len(manual) < len(items):
        hold = band_conflict(cli=cli)["hold"]
    _put(_verdicts(cli, manual), hold, cli is not None)


def precheck_nos(nos):
    """번호 변경·바로잡기 — 새로 들어오는 번호만 묻는다(tx 밖)."""
    cli = _client()
    _put(_verdicts(cli, nos), None, cli is not None)


def precheck_update(aid, body):
    """자산 수정 — 관리번호가 실제로 바뀌는 요청만 창구에 묻는다(자산 상세 폼은 저장마다 번호를 함께 보낸다)."""
    new_no = str(body.get("assetNo") or "").strip() if isinstance(body, dict) and "assetNo" in body else ""
    if new_no and parse_no(new_no):
        cur = get_db().execute("SELECT asset_no FROM assets WHERE id=?", (aid,)).fetchone()
        if cur is None or cur["asset_no"] != new_no:
            precheck_nos([new_no])
            return
    _put({}, None, None)


# ---------------------------------------------------------------- 트랜잭션 안에서 쓰는 판정
def _reserve(conn, prefix, seq):
    """OWS 대역 번호를 그날 순번에 반영 — 손으로 넣은 5000번대도 자동 채번이 다시 내지 않는다."""
    conn.execute(
        "INSERT INTO asset_no_counters(day, last_seq, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(day) DO UPDATE SET last_seq=MAX(last_seq, excluded.last_seq), "
        "updated_at=excluded.updated_at",
        (prefix, seq, config.now_iso()))


def validate_manual_no(conn, no, label=""):
    """손입력 관리번호 규칙. 형식이 아니면 None(예전 동작 그대로).

    거부는 abort(400). 통과하면 None, 사본과 대조하지 못했으면 그 사유(문자열) — 호출 측이 자산이 생긴 뒤
    note_unverified() 로 '번호 미확인' 이력을 남긴다.
    """
    p = parse_no(no)
    if p is None:
        return None
    prefix, seq = p
    chk = g.get("asset_no_check") or {}
    v = (chk.get("verdicts") or {}).get(no)
    if not is_ows_band(seq):
        if v == "없음":
            abort(400, description=f"{label}TMS 자산 원장에 없는 번호입니다 (오타 확인): {no}")
        if v == "삭제됨":
            abort(400, description=f"{label}TMS에서 삭제된 번호입니다: {no}")
        if v == "중복":
            abort(400, description=f"{label}TMS에 같은 번호 자산이 둘 이상 있어 확인이 필요합니다: {no}")
        if v == "ok":
            return None
    else:
        if v in ("ok", "중복", "삭제됨"):
            abort(400, description=f"{label}TMS에 이미 있는 번호입니다 — 번호대 충돌: {no}")
        # OWS가 오늘 채번해 준 번호([자동 채번]·브릿지)면 우리 번호다 — 대조를 못 했어도 '미확인'으로 남기지 않는다.
        last = conn.execute("SELECT last_seq FROM asset_no_counters WHERE day=?", (prefix,)).fetchone()
        issued = bool(last and last["last_seq"] >= seq)
        _reserve(conn, prefix, seq)
        if v == "없음" or issued:
            return None
    if v == "검증불가":
        return "연동 사본과 대조 못 함(검증불가 — 사본이 낡았거나 창구 오류)"
    return "연동 창구 미설정" if not chk.get("enabled") else "연동 창구 응답 없음"


def note_unverified(conn, aid, no, reason):
    """사본과 대조하지 못한 손입력 번호 — 막지 않고 이력에만 남긴다."""
    asset_event(conn, aid, EVENT_UNVERIFIED, {"관리번호": no, "사유": reason})


# ---------------------------------------------------------------- 채번 API
def issue_asset_no(ref=""):
    """채번 1건 — 창구 판정(tx 밖) → 트랜잭션에서 번호 소비. 발급한 번호는 쓰지 않아도 다시 나가지 않는다."""
    chk = band_conflict()
    if chk["hold"]:
        abort(409, description=chk["hold"])
    with tx(write=True) as conn:
        no = next_asset_no(conn)
        audit.log("asset_no_issued", target=no,
                  detail={"verified": chk["verified"], "note": chk.get("reason") or "", "ref": ref})
    return {"assetNo": no, "verified": chk["verified"], "note": chk.get("reason") or ""}


@bp.post("/assets/next-no")
def assets_next_no():
    """자산 등록 폼 [자동 채번] — {assetNo, verified, note}. 409 = 번호대 충돌 가능(사본 확인 필요)."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    return jsonify(issue_asset_no(str(body.get("ref") or "")[:100]))


@bp.post("/bridge/next-asset-no")
def bridge_next_asset_no():
    """RMS 창구(X-Bridge-Token, app/__init__.py 전역 게이트) — 반납 채번 게이트가 OWS 번호를 받아 간다.

    응답은 /assets/next-no 와 같고 ok 가 붙는다. 권한 검사는 하지 않는다 — 토큰 자체가 자격이다.
    """
    body = request.get_json(silent=True) or {}
    return jsonify({"ok": True, **issue_asset_no(str(body.get("ref") or "")[:100])})
