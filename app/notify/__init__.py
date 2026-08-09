"""고객 안내 문자(SMS/LMS) — A/S 단계별 자동 발송.

RMS(아서렌탈)에서 검증된 비즈고 OMNI V2 어댑터를 그대로 가져오되, HMS 규율에 맞춘다.

안전 규칙 (실제 고객 휴대폰으로 나가는 기능이라 가장 보수적으로 만든다)
- **기본은 시뮬레이션.** API 키·발신번호가 있고 [실발송 켜기]까지 해야 실제로 나간다
  (CJ 실발행 무장과 같은 이중 스위치).
- **검증 서버에서는 절대 안 나간다.** 운영 DB가 아닐 때는 무조건 시뮬 — 라이브 DB를 복사해
  다른 포트로 띄우면 설정(=API 키)도 함께 복사되므로, 이 가드가 없으면 검증하다 실제로 발송된다.
- **같은 단계 문자는 한 번만.** sms_log의 UNIQUE 제약으로 보장한다(동시 발송에도 안전).
- 발송(외부 호출)은 반드시 트랜잭션 밖에서 한다(원칙 #1).
- 번호가 없거나 형식이 이상하면 보내지 않고 사유를 남긴다.
"""
import json
import os
import re
from pathlib import Path
from urllib.request import Request, urlopen

from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require, require_any
from ..db import get_db, tx
from ..settings import bp

DEFAULT_BASE_URL = "https://mars.ibapi.kr/api/comm"
SMS_BYTE_LIMIT = 90            # 이보다 길면 LMS로 나간다(비즈고 규격)

# A/S 단계별 기본 문구. 대표가 설정에서 고칠 수 있다.
# 톤: 렌탈 연체 '독촉'이 아니라 수리 '안내'다.
# ★기본 문구는 모두 90바이트 이내(단문 SMS)로 맞췄다.
#   90바이트를 넘으면 장문(LMS)이 되어 건당 요금이 3배가량 든다.
#   대표가 문구를 늘리면 자동으로 LMS로 나가며, 설정 화면의 [미리보기]가 그걸 알려 준다.
EVENTS = [
    {
        "code": "received", "label": "접수 완료",
        "when": "A/S를 접수했을 때",
        "default": "[하프전자] {고객명}님 A/S 접수 완료\n{접수번호} · {증상}\n진행되면 안내드립니다.",
        "on": True,
    },
    {
        "code": "collecting", "label": "회수 예약",
        "when": "택배 회수를 예약했을 때",
        "default": "[하프전자] {고객명}님 A/S 회수 예약 완료\n{접수번호}\n"
                   "기사 방문 시 제품을 전달해 주세요.",
        "on": True,
    },
    {
        "code": "done", "label": "수리 완료",
        "when": "수리를 마쳤을 때",
        "default": "[하프전자] {고객명}님 A/S 수리 완료\n{접수번호} · {처리내용}\n곧 발송해 드립니다.",
        "on": True,
    },
    {
        "code": "returned", "label": "반송 발송",
        "when": "반송 송장을 발급했을 때",
        "default": "[하프전자] {고객명}님 제품을 발송했습니다.\nCJ {송장번호}\n{접수번호} 감사합니다.",
        "on": True,
    },
]
EVENT_CODES = {e["code"] for e in EVENTS}


# ---------------------------------------------------------------- 설정

def sms_settings(conn):
    row = conn.execute("SELECT value FROM settings WHERE key='sms'").fetchone()
    cfg = {}
    if row:
        try:
            cfg = json.loads(row["value"]) or {}
        except ValueError:
            cfg = {}
    cfg.setdefault("provider", "bizgo")
    cfg.setdefault("baseUrl", DEFAULT_BASE_URL)
    cfg.setdefault("armed", False)       # ★실발송 스위치 — 켜야 실제로 나간다
    cfg.setdefault("events", {})         # {code: {"on": bool, "text": str}}
    return cfg


def event_config(cfg, code):
    base = next((e for e in EVENTS if e["code"] == code), None)
    if base is None:
        return None
    saved = (cfg.get("events") or {}).get(code) or {}
    return {
        "code": code,
        "label": base["label"],
        "on": bool(saved.get("on", base["on"])),
        "text": (saved.get("text") or base["default"]).strip(),
    }


def is_production_server(db_path):
    """이 프로세스가 '진짜 운영 서버'인가.

    ★db.py의 _is_live_db는 여기 쓰면 안 된다 — 그건 열린 DB를 config.DB_PATH와 비교하는데,
      config.DB_PATH 자체가 HMS_DB 환경변수에서 나온다. 검증 서버를 HMS_DB=사본으로 띄우면
      '자기 자신과 같다'가 되어 항상 참이 된다. 설정(API 키)도 DB에 함께 복사되므로
      그대로 두면 검증하다가 진짜 고객에게 문자가 나간다(2026-07-29 워크플로 지적).
      그래서 환경변수를 타지 않는 값만으로 판정한다.
    """
    if os.getenv("HMS_SMS_BLOCK"):          # 검증용 킬스위치(반대 방향 '허용' 변수는 만들지 않는다)
        return False, "이 서버는 문자 발송이 차단돼 있습니다(검증용)."
    try:
        fixed = config.LIVE_DB_PATH.resolve()   # 환경변수를 타지 않는 운영 DB 경로(config 한 곳에서 관리)
        if Path(db_path).resolve() != fixed:
            return False, "운영 데이터가 아니라 실제로 보내지 않았습니다(검증용 사본)."
    except OSError:
        return False, "데이터 경로를 확인할 수 없어 보내지 않았습니다."
    if int(config.PORT) != 5100:
        return False, f"운영 포트(5100)가 아니라 보내지 않았습니다(현재 {config.PORT})."
    return True, ""


def host_fingerprint():
    """이 PC를 가리키는 표식. 폴더를 복사해도 따라오지 않는 값이어야 한다."""
    import socket
    return socket.gethostname().strip().lower()


def is_live_sending(cfg, db_path):
    """지금 이 요청이 '실제로 문자를 보내는' 상태인가.

    하나라도 어긋나면 시뮬레이션(기록만)이다.
    """
    ok, why = is_production_server(db_path)
    if not ok:
        return False, why
    if not cfg.get("armed"):
        return False, "설정에서 [실제 발송]이 꺼져 있습니다."
    if not (cfg.get("apiKey") or "").strip():
        return False, "문자 API 키가 없습니다."
    if not _norm_phone(cfg.get("sender")):
        return False, "발신번호가 없습니다."
    # 첫 실발송 전에 대표 번호로 테스트 1건이 성공해야 고객에게 나간다
    if not (cfg.get("firstLiveConfirmedAt") or "").strip():
        return False, "먼저 설정에서 [내 번호로 시험 발송]을 한 번 성공시켜 주세요."
    # ★폴더째 복사해 다른 PC에서 띄우면 설정(API 키)까지 함께 복사돼 그 복사본도
    #   자기를 운영이라고 믿는다. 시험 발송을 성공시킨 PC의 이름을 기억해 두고,
    #   그 PC가 아니면 잠근다(PC를 옮겼다면 새 PC에서 시험 발송을 한 번 하면 풀린다).
    bound = (cfg.get("boundHost") or "").strip().lower()
    if bound and bound != host_fingerprint():
        return False, (f"이 PC({host_fingerprint()})는 문자를 보내도록 확인된 곳이 아닙니다"
                       f" — 등록된 PC: {bound}. 옮긴 것이라면 설정에서 "
                       "[내 번호로 시험 발송]을 한 번 성공시켜 주세요.")
    return True, ""


# ---------------------------------------------------------------- 발송

def _int_or(value, default):
    """0을 유효한 값으로 받는다 — `or`를 쓰면 0시·0건을 설정할 수 없다."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _norm_phone(v):
    return re.sub(r"\D", "", str(v or ""))


def _byte_len(text):
    try:
        return len((text or "").encode("euc-kr"))
    except Exception:                                    # noqa: BLE001
        return len((text or "").encode("utf-8"))


def render_text(template, vars_):
    """{고객명} 같은 자리를 실제 값으로 바꾼다. 없는 값은 빈칸으로."""
    out = template or ""
    for k, v in (vars_ or {}).items():
        out = out.replace("{" + k + "}", str(v if v is not None else ""))
    return re.sub(r"\{[^}]*\}", "", out).strip()          # 안 채워진 자리는 지운다


def send_sms(cfg, phone, text, subject=None):
    """비즈고 OMNI V2 단건 발송. 호출자는 반드시 트랜잭션 밖에서 부른다.

    반환: {"ok": bool, "msgType": "SMS"|"LMS", "detail": str}
    """
    to = _norm_phone(phone)
    sender = _norm_phone(cfg.get("sender"))
    key = (cfg.get("apiKey") or "").strip()
    base = (cfg.get("baseUrl") or DEFAULT_BASE_URL).strip().rstrip("/")

    if _byte_len(text) > SMS_BYTE_LIMIT:
        msg_type = "LMS"
        flow = {"mms": {"from": sender, "title": (subject or "하프전자 A/S 안내")[:40], "text": text}}
    else:
        msg_type = "SMS"
        flow = {"sms": {"from": sender, "text": text}}
    body = json.dumps({"destinations": [{"to": to}], "messageFlow": [flow]},
                      ensure_ascii=False).encode("utf-8")
    req = Request(base + "/v1/send/omni", data=body, headers={
        "Content-Type": "application/json",
        "Authorization": key,          # V2 통합키는 접두어 없이 값만
    })
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except Exception as e:                               # noqa: BLE001
        return {"ok": False, "msgType": msg_type, "detail": f"발송 실패: {e}"}

    ok = status == 200
    try:
        j = json.loads(raw)
        auth_code = str(((j.get("common") or {}).get("authCode") or "")).strip().upper()
        data_code = str(((j.get("data") or {}).get("code") or "")).strip().upper()
        if auth_code and auth_code != "A000":
            ok = False
        if data_code and data_code not in ("A000", "0000", "0", "SUCCESS"):
            ok = False
    except ValueError:
        pass
    return {"ok": ok, "msgType": msg_type, "detail": raw[:300]}


def notify_ticket(app, tid, code, actor="", force=False, allow_quiet=False):
    """A/S 한 건에 대해 단계 문자를 보낸다. (ok, message) 반환.

    ★이 함수는 트랜잭션 밖에서 불려야 한다(내부에서 짧은 tx를 따로 연다).
    """
    if code not in EVENT_CODES and not code.startswith("manual"):
        return False, "알 수 없는 문자 종류입니다."

    with tx() as conn:
        t = conn.execute("SELECT * FROM as_tickets WHERE id=?", (tid,)).fetchone()
        if t is None:
            return False, "A/S 건을 찾을 수 없습니다."
        # ★취소된 건에는 자동·수동 어느 쪽으로도 보내지 않는다(사내 사정이 대부분이라 혼란만 준다)
        if t["status"] == "cancelled":
            return False, "취소된 A/S 건에는 안내를 보내지 않습니다."
        cfg = sms_settings(conn)
        ev = event_config(cfg, code) if code in EVENT_CODES else None
        if ev and not ev["on"] and not force:
            return False, f"'{ev['label']}' 안내는 꺼져 있습니다."
        sent_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM sms_log WHERE ticket_id=? AND (event=? OR event LIKE ?) "
            "AND status IN ('sent','simulated')",
            (tid, code, code + "#%")).fetchone()["c"]
        if sent_rows and not force:
            return False, "이미 보낸 안내입니다."
        # ★재발송은 같은 (건, 종류)로 또 기록할 수 없다(중복 방지 UNIQUE).
        #   회차를 붙여 남긴다 — 안 그러면 기록 단계에서 터져 화면에 '서버 내부 오류'만 뜨고,
        #   실발송 모드에서는 문자가 이미 나간 뒤라 누를 때마다 고객에게 중복 발송된다
        #   (2026-07-29 전수조사 확정).
        log_event = f"{code}#{sent_rows + 1}" if sent_rows else code
        ticket = dict(t)
        wb = conn.execute(
            "SELECT invoice_no FROM waybills WHERE as_ticket_id=? AND type='forward' "
            "AND status IN ('issued','test') ORDER BY created_at DESC LIMIT 1", (tid,)).fetchone()
        asset_no = ""
        if t["asset_id"]:
            a = conn.execute("SELECT asset_no FROM assets WHERE id=?", (t["asset_id"],)).fetchone()
            asset_no = a["asset_no"] if a else ""
        # 오늘 이 시스템이 실제로 보낸 건수(하루 상한 — 사고가 나도 피해를 묶는 마지막 선)
        today_sent = conn.execute(
            "SELECT COUNT(*) AS c FROM sms_log WHERE status='sent' AND substr(sent_at,1,10)=?",
            (config.now().strftime("%Y-%m-%d"),)).fetchone()["c"]

    # 내용이 비어 있으면 보내지 않는다 — '송장번호 (없음)' 같은 문자가 나가면 문의만 늘어난다
    if code == "returned" and not (wb and (wb["invoice_no"] or "").strip()):
        return False, "송장번호가 아직 없어 발송 안내를 보내지 않았습니다."
    if code == "done" and not (ticket["result"] or "").strip():
        return False, "처리 내용을 먼저 입력해 주세요(문자에 들어갑니다)."

    phone = _norm_phone(ticket["phone"])
    # 유상인데 금액이 0이면 '청구 안내'가 금액 없이 나간다 — 그건 막는다
    charge = ticket.get("charge_to") if isinstance(ticket, dict) else ticket["charge_to"]
    cost = int(ticket["cost"] or 0)
    if code == "done" and charge == "customer" and cost <= 0:
        return False, "유상 처리인데 수리비가 0원입니다. 금액을 입력한 뒤 보내 주세요."
    cost_note = (f"수리비 {cost:,}원이 청구됩니다." if charge == "customer" and cost
                 else "무상으로 처리되었습니다." if charge == "company" else "")
    text = render_text(ev["text"] if ev else (cfg.get("manualText") or ""), {
        "고객명": ticket["customer"], "접수번호": ticket["ticket_no"],
        "증상": ticket["symptom"], "처리내용": ticket["result"],
        "송장번호": wb["invoice_no"] if wb else "", "관리번호": asset_no,
        "비용안내": cost_note, "수리비": f"{cost:,}원" if cost else "",
        "문의처": (cfg.get("contact") or "").strip(),
    })

    def _record(status, reason="", msg_type=""):
        # 기록이 실패해도 '이미 나간 문자'를 없던 일로 만들 수는 없다 —
        # 예외를 삼키고 사용자에게는 발송 결과를 그대로 알린다.
        try:
            _record_row(status, reason, msg_type)
        except Exception:                                  # noqa: BLE001
            app.logger.exception("문자 이력 기록 실패 | ticket=%s event=%s", tid, log_event)

    def _record_row(status, reason="", msg_type=""):
        with tx(write=True) as c2:
            c2.execute(
                "INSERT INTO sms_log(ticket_id, event, phone, text, msg_type, status, reason, "
                "sent_by, sent_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (tid, log_event, ticket["phone"], text, msg_type, status, reason[:300],
                 actor or (g.user["display_name"] if g.get("user") else "시스템"), config.now_iso()))
            c2.execute(
                "INSERT INTO as_events(ticket_id, ts, action, actor, detail) VALUES(?,?,?,?,?)",
                (tid, config.now_iso(), "문자" + ("발송" if status == "sent" else
                                                 "시뮬" if status == "simulated" else "실패"),
                 actor or (g.user["display_name"] if g.get("user") else "시스템"),
                 json.dumps({"종류": code, "사유": reason} if reason else {"종류": code},
                            ensure_ascii=False)))

    if not (phone.startswith("01") and len(phone) in (10, 11)):
        # 050 안심번호·유선번호·공란은 문자를 못 받는다 — 조용히 넘기지 않고 사유를 남긴다
        why = ("안심번호(050)라 문자를 보낼 수 없습니다. 전화로 안내해 주세요."
               if phone.startswith("050") else "휴대폰 번호가 없거나 형식이 올바르지 않습니다.")
        _record("skipped", why)
        return False, why

    live, why = is_live_sending(cfg, app.config["DB_PATH"])
    if live:
        # 밤에는 보내지 않는다(고객 항의의 가장 큰 원인). 낮에 손으로 보낼 수 있다.
        hour = config.now().hour
        # ★`or 21` 로 쓰면 0시를 설정할 수 없다(0은 falsy). 명시적으로 None만 기본값 처리한다.
        q_from = _int_or(cfg.get("quietFrom"), 21)
        q_to = _int_or(cfg.get("quietTo"), 8)
        quiet = (hour >= q_from or hour < q_to) if q_from > q_to else (q_from <= hour < q_to)
        # ★force는 '이미 보낸 건을 다시'라는 뜻이지 '야간에도 보내라'가 아니다.
        #   둘을 한 스위치로 묶으면 재발송 한 번에 새벽 문자가 나간다(2026-07-29 지적).
        if quiet and not allow_quiet:
            _record("skipped", f"야간({q_from}시~{q_to}시)이라 보내지 않았습니다.")
            return False, (f"지금은 야간({q_from}시~{q_to}시)이라 보내지 않았습니다. "
                           "그래도 보내려면 화면에서 한 번 더 확인해 주세요.")
        cap = _int_or(cfg.get("dailyCap"), 30)
        if today_sent >= cap:
            _record("skipped", f"하루 발송 상한({cap}건)을 넘었습니다.")
            return False, f"오늘 발송 상한({cap}건)에 도달해 보내지 않았습니다."
    if not live:
        _record("simulated", why, "SMS")
        return True, f"문자를 실제로 보내지는 않았습니다 — {why} (내용은 이력에 남았습니다)"

    res = send_sms(cfg, phone, text)                     # ← 트랜잭션 밖
    if res["ok"]:
        _record("sent", "", res["msgType"])
        return True, f"{res['msgType']} 발송 완료"
    _record("failed", res["detail"], res["msgType"])
    return False, f"발송 실패: {res['detail'][:120]}"


def notify_async(app, tid, code):
    """상태 전이 훅에서 부르는 래퍼 — 실패해도 본 작업을 막지 않는다."""
    try:
        return notify_ticket(app, tid, code)
    except Exception:                                    # noqa: BLE001
        app.logger.exception("A/S 문자 발송 실패 | ticket=%s | %s", tid, code)
        return False, "문자 발송 중 오류가 났습니다(작업은 정상 처리되었습니다)."


# ---------------------------------------------------------------- API

@bp.get("/sms/config")
def sms_config():
    """설정 화면용 — 문구·on/off·발송 준비 상태. 키 값은 내려주지 않는다."""
    require("settings.manage")
    conn = get_db()
    cfg = sms_settings(conn)
    from flask import current_app
    live, why = is_live_sending(cfg, current_app.config["DB_PATH"])
    return jsonify({
        "provider": cfg.get("provider"),
        "hasApiKey": bool((cfg.get("apiKey") or "").strip()),
        "sender": cfg.get("sender") or "",
        "testPhone": cfg.get("testPhone") or "",
        "contact": cfg.get("contact") or "",
        "dailyCap": _int_or(cfg.get("dailyCap"), 30),
        "quietFrom": _int_or(cfg.get("quietFrom"), 21),
        "quietTo": _int_or(cfg.get("quietTo"), 8),
        "baseUrl": cfg.get("baseUrl") or DEFAULT_BASE_URL,
        "armed": bool(cfg.get("armed")),
        "confirmed": bool((cfg.get("firstLiveConfirmedAt") or "").strip()),
        "live": live,
        "reason": why,
        "events": [dict(event_config(cfg, e["code"]), when=e["when"], default=e["default"])
                   for e in EVENTS],
        "vars": ["고객명", "접수번호", "증상", "처리내용", "송장번호", "관리번호"],
    })


@bp.get("/sms/log")
def sms_log():
    """발송 이력 — A/S 건별 또는 전체."""
    require_any("as.view", "as.manage", "settings.manage")
    tid = request.args.get("ticketId", type=int)
    sql = ("SELECT s.*, t.ticket_no, t.customer FROM sms_log s "
           "LEFT JOIN as_tickets t ON t.id = s.ticket_id WHERE 1=1")
    params = []
    if tid:
        sql += " AND s.ticket_id = ?"
        params.append(tid)
    sql += " ORDER BY s.id DESC LIMIT 200"
    rows = get_db().execute(sql, params).fetchall()
    return jsonify([
        {"id": r["id"], "ticketId": r["ticket_id"], "ticketNo": r["ticket_no"],
         "customer": r["customer"], "event": r["event"], "phone": r["phone"],
         "text": r["text"], "msgType": r["msg_type"], "status": r["status"],
         "reason": r["reason"], "sentBy": r["sent_by"], "sentAt": r["sent_at"]}
        for r in rows])


@bp.post("/as-tickets/<int:tid>/sms")
def send_ticket_sms(tid):
    """A/S 건에 문자를 수동으로 보낸다(자동 발송이 꺼져 있어도 여기서 보낼 수 있다)."""
    require("as.manage")
    body = request.get_json(silent=True) or {}
    code = (body.get("event") or "").strip()
    if code not in EVENT_CODES:
        abort(400, description="보낼 안내 종류를 고르세요.")
    from flask import current_app
    # force = '이미 보낸 것을 한 번 더', allowQuiet = '야간에도' — 서로 다른 확인이다
    ok, msg = notify_ticket(current_app, tid, code,
                            force=bool(body.get("force")),
                            allow_quiet=bool(body.get("allowQuiet")))
    with tx(write=True):
        audit.log("as_sms", target=f"#{tid} {code}", detail={"ok": ok, "message": msg})
    return jsonify({"ok": ok, "message": msg})


@bp.post("/sms/test")
def sms_test():
    """★대표 본인 번호로만 시험 발송 — 이걸 한 번 성공해야 고객에게 나가기 시작한다.

    받는 번호를 요청에서 받지 않는다(오타로 모르는 사람에게 가는 사고를 원천 차단).
    설정에 저장된 번호로만 보낸다.
    """
    require("settings.manage")
    from flask import current_app
    conn = get_db()
    cfg = sms_settings(conn)
    to = _norm_phone(cfg.get("testPhone"))
    if not (to.startswith("01") and len(to) in (10, 11)):
        abort(400, description="먼저 [시험 받을 번호]에 대표님 휴대폰 번호를 저장하세요.")
    prod, why = is_production_server(current_app.config["DB_PATH"])
    if not prod:
        return jsonify({"ok": False, "message": f"{why} 시험 발송도 하지 않았습니다."})
    if not ((cfg.get("apiKey") or "").strip() and _norm_phone(cfg.get("sender"))):
        abort(400, description="API 키와 발신번호를 먼저 저장하세요.")

    text = f"[하프전자] 문자 설정 시험입니다. 이 메시지가 보이면 정상입니다. ({config.now().strftime('%H:%M')})"
    res = send_sms(cfg, to, text)                    # ← 트랜잭션 밖
    with tx(write=True) as c2:
        c2.execute(
            "INSERT INTO sms_log(ticket_id, event, phone, text, msg_type, status, reason, "
            "sent_by, sent_at) VALUES(NULL,'test',?,?,?,?,?,?,?)",
            (to, text, res["msgType"], "sent" if res["ok"] else "failed",
             "" if res["ok"] else res["detail"][:300],
             g.user["display_name"], config.now_iso()))
        if res["ok"]:
            cur = dict(sms_settings(c2))
            cur["firstLiveConfirmedAt"] = config.now_iso()
            cur["boundHost"] = host_fingerprint()      # 이 PC에서만 고객에게 나간다
            c2.execute(
                "INSERT INTO settings(key, value, updated_at, updated_by) VALUES('sms',?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (json.dumps(cur, ensure_ascii=False), config.now_iso(), g.user["display_name"]))
        audit.log("sms_test", target=to[-4:], detail={"ok": res["ok"]})
    return jsonify({"ok": res["ok"],
                    "message": "시험 문자를 보냈습니다. 휴대폰을 확인해 주세요."
                    if res["ok"] else f"발송 실패: {res['detail'][:150]}"})


@bp.post("/sms/preview")
def sms_preview():
    """문구 미리보기 — 실제로 보내지 않고 길이·종류만 확인한다."""
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    text = render_text(body.get("text") or "", {
        "고객명": "홍길동", "접수번호": "AS-260729-01", "증상": "액정 세로줄",
        "처리내용": "액정 교체", "송장번호": "123456789012", "관리번호": "260729-0001",
    })
    n = _byte_len(text)
    return jsonify({"text": text, "bytes": n,
                    "msgType": "LMS" if n > SMS_BYTE_LIMIT else "SMS",
                    "note": "90바이트(한글 45자)를 넘으면 장문(LMS)으로 나갑니다."})
