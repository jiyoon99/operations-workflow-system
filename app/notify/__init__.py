"""고객 안내 문자(SMS/LMS) — A/S 단계별 자동 발송.

RMS(예시 렌탈사)에서 검증된 비즈고 OMNI V2 어댑터를 그대로 가져오되, OWS 규율에 맞춘다.

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
        "default": "[운영팀] {고객명}님 A/S 접수 완료\n{접수번호} · {증상}\n진행되면 안내드립니다.",
        "on": True,
    },
    {
        "code": "collecting", "label": "회수 예약",
        "when": "택배 회수를 예약했을 때",
        "default": "[운영팀] {고객명}님 A/S 회수 예약 완료\n{접수번호}\n"
                   "기사 방문 시 제품을 전달해 주세요.",
        "on": True,
    },
    # ★단계별 안내 4종(2026-09-07 대표) — 진행 상황 보드의 각 칸에서 [💬 문자] 한 번으로 보낸다.
    #   "고객이 지금 어디쯤인지 물어보는 전화가 단계마다 온다"가 이유라, 문구는 '무슨 일이
    #   있었고 다음이 무엇인지'만 짧게 적는다(전부 90바이트 이내 = 단문 요금).
    {
        "code": "collected", "label": "입고 완료",
        "when": "회수한 제품이 우리에게 도착했을 때 — [📥 입고 완료] 칸",
        # ★{품목안내} = "받은 품목: 본체, 충전기"(안 적었으면 통째로 빠진다). 2026-09-08 대표
        #   "고객들과 의견차이가 발생하지 않도록" — 맡은 물건을 받은 즉시 문자로 확인해 준다.
        "default": "[운영팀] {고객명}님 제품이 입고되었습니다.\n{품목안내}\n{접수번호}",
        "on": True,
    },
    {
        "code": "repairing", "label": "수리 진행",
        "when": "수리를 시작했을 때 — [🔧 수리 중] 칸",
        "default": "[운영팀] {고객명}님 A/S 수리 진행 중입니다.\n{접수번호}\n완료되면 안내드립니다.",
        "on": True,
    },
    {
        "code": "payment", "label": "결제 안내",
        "when": "유상 수리비 입금을 요청할 때 — [💰 결제 전] 칸",
        # ★이 한 건만 장문(LMS)이다 — 금액과 계좌번호가 들어가면 90바이트를 넘길 수밖에 없다.
        #   빼면 고객이 어디로 얼마를 보낼지 몰라 전화가 온다. 요금보다 그게 비싸다.
        "default": "[운영팀] {고객명}님 A/S 비용 안내\n{비용안내}\n{계좌번호}\n입금 후 발송됩니다.",
        "on": True,
    },
    {
        "code": "shipping_today", "label": "발송 예정",
        "when": "오늘 보낼 때 — [📦 발송 전] 칸",
        # 돌려보낼 때도 같은 목록을 확인해 준다 — 받을 때와 보낼 때가 맞아야 분실 시비가 없다
        "default": "[운영팀] {고객명}님 제품을 금일 발송합니다.\n{품목안내}\n{접수번호}",
        "on": True,
    },
    {
        "code": "done", "label": "수리 완료",
        "when": "수리를 마쳤을 때",
        "default": "[운영팀] {고객명}님 A/S 수리 완료\n{접수번호} · {처리내용}\n곧 발송해 드립니다.",
        "on": True,
    },
    {
        "code": "returned", "label": "반송 발송",
        "when": "반송 송장을 발급했을 때",
        "default": "[운영팀] {고객명}님 제품을 발송했습니다.\nCJ {송장번호}\n{접수번호} 감사합니다.",
        "on": True,
    },
]
EVENT_CODES = {e["code"] for e in EVENTS}

# 자동 발송 시점(2026-08-31 대표 — "어떤 커밋이 발생했을 때 보낼 것인지 지정").
# 기본 4종은 시점이 고정이고, 직접 만든 양식은 여기서 시점을 고르거나 '수동 전용'으로 둔다.
TRIGGERS = {
    "received": "A/S를 접수했을 때",
    "collecting": "택배 회수를 예약했을 때",
    "collected": "회수한 제품이 입고됐을 때",
    "done": "수리를 마쳤을 때",
    "returned": "반송 송장을 발급했을 때",
    "invoiced": "청구내역서를 발행했을 때",
    "": "자동 발송 없음 — 상세 화면에서 수동으로만",
}
# ★자동으로 나가지 않는 안내 — 사람이 보드에서 [💬 문자]를 눌러야 나간다(2026-09-07 대표
#   "개별 고객 탭에서 SMS 문자 발송 기능 추가"). 그래서 위 TRIGGERS(자동 발송 시점 목록)에
#   일부러 넣지 않았다 — 목록에 넣으면 직접 만든 양식이 '절대 안 오는 시점'을 고를 수 있어
#   대표가 켜 놓고 왜 안 나가는지 못 찾는다.
#   특히 결제·발송예정은 '언제 보낼지'가 담당자 판단이다(금액이 정해지기 전에 청구가 나가면 안 된다).
MANUAL_ONLY_EVENTS = ("repairing", "payment", "shipping_today")

# 문구에 쓸 수 있는 자리(매크로) — 화면 도움말과 미리보기가 같은 목록을 쓴다
SMS_VARS = ["고객명", "접수번호", "증상", "증상분류", "처리내용", "송장번호", "관리번호", "모델명",
            "회수품목", "품목안내", "수리비", "추가비용", "비용안내", "계좌번호", "문의처"]


def custom_templates(cfg):
    """직접 만든 문자 양식 목록(2026-08-31 대표 — 비용/현금영수증/입금 안내 등)."""
    out = []
    for t in (cfg.get("custom") or []):
        if not isinstance(t, dict):
            continue
        if not (t.get("id") and str(t.get("label") or "").strip()):
            continue
        out.append({"id": str(t["id"]), "label": str(t["label"]).strip(),
                    "trigger": t.get("trigger") if t.get("trigger") in TRIGGERS else "",
                    "subject": str(t.get("subject") or "").strip(),
                    "text": str(t.get("text") or "").strip(),
                    "on": bool(t.get("on", True))})
    return out


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
    cfg.setdefault("events", {})         # {code: {"on": bool, "text": str, "subject": str}}
    cfg.setdefault("custom", [])         # 직접 만든 양식 [{id, label, trigger, subject, text, on}]
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
        "subject": str(saved.get("subject") or "").strip(),   # LMS 제목(비우면 기본)
    }


def is_production_server(db_path):
    """이 프로세스가 '진짜 운영 서버'인가.

    ★db.py의 _is_live_db는 여기 쓰면 안 된다 — 그건 열린 DB를 config.DB_PATH와 비교하는데,
      config.DB_PATH 자체가 OWS_DB 환경변수에서 나온다. 검증 서버를 OWS_DB=사본으로 띄우면
      '자기 자신과 같다'가 되어 항상 참이 된다. 설정(API 키)도 DB에 함께 복사되므로
      그대로 두면 검증하다가 진짜 고객에게 문자가 나간다(2026-07-29 워크플로 지적).
      그래서 환경변수를 타지 않는 값만으로 판정한다.
    """
    if os.getenv("OWS_SMS_BLOCK"):          # 검증용 킬스위치(반대 방향 '허용' 변수는 만들지 않는다)
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
    out = re.sub(r"\{[^}]*\}", "", out)                   # 안 채워진 자리는 지운다
    # ★값이 비어 통째로 사라진 줄은 지운다(2026-09-08) — {품목안내}처럼 '있을 때만 한 줄'인
    #   자리를 쓰면, 안 적은 건에서 빈 줄이 남아 문자 가운데가 뻥 뚫린 채 나간다.
    #   줄바꿈도 1바이트라 요금(90바이트 단문 경계)에도 불리하다.
    return "\n".join(ln.rstrip() for ln in out.split("\n") if ln.strip()).strip()


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
        flow = {"mms": {"from": sender, "title": (subject or "예시 운영사 A/S 안내")[:40], "text": text}}
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
    msg_key, codes = "", []
    try:
        j = json.loads(raw)
        auth_code = str(((j.get("common") or {}).get("authCode") or "")).strip().upper()
        data_code = str(((j.get("data") or {}).get("code") or "")).strip().upper()
        if auth_code:
            codes.append(f"auth={auth_code}")
        if data_code:
            codes.append(f"data={data_code}")
        if auth_code and auth_code != "A000":
            ok = False
        if data_code and data_code not in ("A000", "0000", "0", "SUCCESS"):
            ok = False
        # ★수신번호별 결과가 여기 들어 있다 — 비즈고가 접수는 했는데 그 번호로 못 보낸
        #   경우(발신번호 미등록 등)가 destinations 안에서만 드러난다(2026-09-03).
        dests = (((j.get("data") or {}).get("data") or {}).get("destinations") or [])
        for d in dests if isinstance(dests, list) else []:
            if not isinstance(d, dict):
                continue
            dc = str(d.get("code") or "").strip().upper()
            if dc:
                codes.append(f"수신={dc}({d.get('result') or ''})")
            if dc and dc not in ("A000", "0000", "0", "SUCCESS"):
                ok = False
            if d.get("msgKey"):
                msg_key = str(d["msgKey"])
    except ValueError:
        pass
    return {"ok": ok, "msgType": msg_type, "detail": raw[:300],
            "msgKey": msg_key, "codes": " ".join(codes), "httpStatus": status}


def _ticket_sms_vars(conn, t, cfg):
    """문구 치환 값 한 벌 — 발송(notify_ticket)과 발송 전 미리보기가 같은 것을 쓴다.
    (따로 만들면 미리보기가 실제 발송 내용과 어긋나 거짓말을 한다)"""
    wb = conn.execute(
        "SELECT invoice_no FROM waybills WHERE as_ticket_id=? AND type='forward' "
        "AND status IN ('issued','test') ORDER BY created_at DESC LIMIT 1", (t["id"],)).fetchone()
    asset_no = ""
    model = ""
    if t["asset_id"]:
        a = conn.execute("SELECT asset_no, model FROM assets WHERE id=?", (t["asset_id"],)).fetchone()
        asset_no = (a["asset_no"] if a else "") or ""
        model = (a["model"] if a else "") or ""
    # 자산 연결이 없으면 접수 화면에서 수기로 적은 번호/모델(2026-08-31)
    asset_no = asset_no or (t["manual_asset_no"] if "manual_asset_no" in t.keys() else "")
    model = (t["manual_model"] if "manual_model" in t.keys() else "") or model
    cost = int(t["cost"] or 0)
    # 추가비용(교환 차액 등, 2026-08-31) — 수리비 부담 구분과 무관하게 고객 청구 몫
    extra = int((t["extra_charge"] if "extra_charge" in t.keys() else 0) or 0)
    if t["charge_to"] == "customer" and cost:
        cost_note = (f"수리비 {cost:,}원과 추가비용 {extra:,}원, 합계 {cost + extra:,}원이 청구됩니다."
                     if extra else f"수리비 {cost:,}원이 청구됩니다.")
    elif t["charge_to"] == "company":
        cost_note = (f"수리비는 무상이며, 추가비용 {extra:,}원이 청구됩니다." if extra
                     else "무상으로 처리되었습니다.")
    elif extra:
        cost_note = f"추가비용 {extra:,}원이 청구됩니다."
    else:
        cost_note = ""
    # {계좌번호} = 설정 ▸ 운영 설정 ▸ A/S 문서의 수리비 입금 계좌(입금 안내 문자용)
    bank = ""
    crow = conn.execute("SELECT value FROM settings WHERE key='as_company_info'").fetchone()
    if crow:
        try:
            bank = (json.loads(crow["value"]) or {}).get("bank") or ""
        except ValueError:
            bank = ""
    # 증상 분류(2026-08-31 저녁) — "노트북 · H/W" 꼴, 비어 있으면 빈칸
    sym_cls = " · ".join(x for x in (
        (t["symptom_cat"] if "symptom_cat" in t.keys() else ""),
        (t["symptom_sub"] if "symptom_sub" in t.keys() else "")) if x)
    items_in = (t["intake_items"] if "intake_items" in t.keys() else "") or ""
    return {
        "고객명": t["customer"], "접수번호": t["ticket_no"],
        "증상": t["symptom"], "증상분류": sym_cls, "처리내용": t["result"],
        "송장번호": (wb["invoice_no"] if wb else "") or "", "관리번호": asset_no,
        "모델명": model,
        # 회수 품목(2026-09-08 대표 "SMS 문자에도 적용해야 고객들과 의견차이가 발생하지 않는다")
        # ★두 벌을 준다 — {회수품목}은 값만("본체, 충전기"), {품목안내}는 완성된 한 줄이다
        #   ("받은 품목: 본체, 충전기" / 안 적었으면 빈 문자열). 기본 문구는 {품목안내}를 쓴다:
        #   값만 넣으면 품목을 안 적은 건에 "받은 품목: " 라벨만 덩그러니 나간다({비용안내}와 같은 방식).
        # ★라벨은 '품목: ' 네 글자다 — '받은 품목: '으로 하면 품목 4개("본체, 충전기, 키스킨,
        #   가방")에서 93바이트가 되어 장문(요금 3배)으로 넘어간다. 4바이트 차이로 갈린다.
        "회수품목": items_in,
        "품목안내": f"품목: {items_in}" if items_in else "",
        "비용안내": cost_note, "수리비": f"{cost:,}원" if cost else "",
        "추가비용": f"{extra:,}원" if extra else "",
        "계좌번호": bank,
        "문의처": (cfg.get("contact") or "").strip(),
    }


def _resolve_template(cfg, code):
    """code → (양식 dict|None, 오류메시지). 기본 4종은 event_config, custom:<id>는 직접 양식."""
    if code in EVENT_CODES:
        return event_config(cfg, code), ""
    if code.startswith("custom:"):
        t = next((x for x in custom_templates(cfg) if "custom:" + x["id"] == code), None)
        if t is None:
            return None, "없는 문자 양식입니다(삭제됐을 수 있습니다)."
        return {"code": code, "label": t["label"], "on": t["on"],
                "text": t["text"], "subject": t["subject"]}, ""
    return None, "알 수 없는 문자 종류입니다."


def notify_ticket(app, tid, code, actor="", force=False, allow_quiet=False,
                  text_override="", subject_override="", manual=False):
    """A/S 한 건에 대해 문자를 보낸다. (ok, message) 반환.

    text_override — 상세 화면에서 치환된 문구를 고쳐 보낸 경우(2026-08-31 대표).
      고친 문구에도 {매크로}를 쓸 수 있게 한 번 더 치환해 준다.
    manual — 사람이 화면에서 [보내기]를 누른 발송. [자동 발송] 체크는 '자동으로 나갈지'만
      정하므로 수동 발송은 막지 않는다(2026-09-08). 화면이 계속 그렇게 안내해 왔는데
      (설정 "…A/S 화면에서 손으로는 보낼 수 있습니다", 문자 양식 "'수동 전용'이면 상세의
      [📑 기타 양식…]으로 보냅니다") 실제로는 막혀 있었다.
      ★force 로 뭉뚱그리면 안 된다 — force 는 '이미 보낸 것을 한 번 더'라는 뜻이라,
        섞으면 수동 발송 한 번이 중복 발송 확인을 건너뛴다.
    ★이 함수는 트랜잭션 밖에서 불려야 한다(내부에서 짧은 tx를 따로 연다).
    """
    if code not in EVENT_CODES and not code.startswith("custom:") \
            and not code.startswith("manual"):
        return False, "알 수 없는 문자 종류입니다."

    with tx() as conn:
        t = conn.execute("SELECT * FROM as_tickets WHERE id=?", (tid,)).fetchone()
        if t is None:
            return False, "A/S 건을 찾을 수 없습니다."
        # ★취소된 건에는 자동·수동 어느 쪽으로도 보내지 않는다(사내 사정이 대부분이라 혼란만 준다)
        if t["status"] == "cancelled":
            return False, "취소된 A/S 건에는 안내를 보내지 않습니다."
        cfg = sms_settings(conn)
        ev, err = _resolve_template(cfg, code)
        if err and not code.startswith("manual"):
            return False, err
        if ev and not ev["on"] and not force and not manual:
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
        vars_ = _ticket_sms_vars(conn, t, cfg)
        # 오늘 이 시스템이 실제로 보낸 건수(하루 상한 — 사고가 나도 피해를 묶는 마지막 선)
        today_sent = conn.execute(
            "SELECT COUNT(*) AS c FROM sms_log WHERE status='sent' AND substr(sent_at,1,10)=?",
            (config.now().strftime("%Y-%m-%d"),)).fetchone()["c"]

    # 내용이 비어 있으면 보내지 않는다 — '송장번호 (없음)' 같은 문자가 나가면 문의만 늘어난다
    if code == "returned" and not vars_["송장번호"].strip():
        return False, "송장번호가 아직 없어 발송 안내를 보내지 않았습니다."
    if code == "done" and not (ticket["result"] or "").strip():
        return False, "처리 내용을 먼저 입력해 주세요(문자에 들어갑니다)."
    # ★결제 안내(2026-09-07 대표 "계좌번호와 함께 결제하라는 내용") — 금액이나 계좌가 비면
    #   "  원을 로 입금해 주세요" 같은 문자가 나가 문의만 늘어난다. 둘 다 있어야 보낸다.
    if code == "payment":
        if ticket["charge_to"] != "customer":
            return False, "무상 처리 건이라 결제 안내를 보내지 않았습니다."
        if not vars_["비용안내"].strip():
            return False, "청구 금액이 없습니다. 🧾 수리 내역에 금액을 적은 뒤 보내 주세요."
        if not vars_["계좌번호"].strip():
            return False, ("입금 계좌가 비어 있습니다 — 설정 ▸ 운영 설정 ▸ A/S 문서에서 "
                           "계좌번호를 먼저 입력해 주세요.")

    phone = _norm_phone(ticket["phone"])
    # 유상인데 금액이 0이면 '청구 안내'가 금액 없이 나간다 — 그건 막는다
    cost = int(ticket["cost"] or 0)
    if code == "done" and ticket["charge_to"] == "customer" and cost <= 0:
        return False, "유상 처리인데 수리비가 0원입니다. 금액을 입력한 뒤 보내 주세요."
    template = (text_override or "").strip() \
        or (ev["text"] if ev else (cfg.get("manualText") or ""))
    text = render_text(template, vars_)
    if not text:
        return False, "보낼 문구가 비어 있습니다."
    subject = (subject_override or "").strip() or (ev.get("subject") if ev else "") or None

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

    res = send_sms(cfg, phone, text, subject=subject)    # ← 트랜잭션 밖
    if res["ok"]:
        # ★성공해도 비즈고 응답을 남긴다(2026-09-03) — 메시지키가 있으면 비즈고 콘솔에서
        #   그 건을 조회할 수 있고, "보냈다는데 안 왔다"를 추적할 수 있다.
        _record("sent", f"{res.get('codes') or ''} msgKey={res.get('msgKey') or '-'}",
                res["msgType"])
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


def notify_trigger(app, tid, trigger):
    """한 사건(trigger)에 걸린 문자를 전부 시도 — 기본 양식 + 그 시점을 고른 직접 양식들.

    (ok, message) 반환 — 하나라도 나갔으면 ok. 훅 자리에서는 notify_async 대신 이걸 쓴다.
    """
    results = []
    if trigger in EVENT_CODES:
        results.append(notify_async(app, tid, trigger))
    try:
        with tx() as conn:
            customs = custom_templates(sms_settings(conn))
    except Exception:                                    # noqa: BLE001
        customs = []
    for t in customs:
        if t["trigger"] == trigger and t["on"]:
            results.append(notify_async(app, tid, "custom:" + t["id"]))
    if not results:
        return False, ""
    ok = any(r[0] for r in results)
    msgs = [r[1] for r in results if r[1]]
    return ok, " / ".join(msgs[:3])


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
        "vars": SMS_VARS,
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
    if code not in EVENT_CODES and not code.startswith("custom:"):
        abort(400, description="보낼 안내 종류를 고르세요.")
    from flask import current_app
    # force = '이미 보낸 것을 한 번 더', allowQuiet = '야간에도' — 서로 다른 확인이다
    # text/subject = 발송 전에 화면에서 고친 문구(2026-08-31 대표) — 이력에는 고친 그대로 남는다
    ok, msg = notify_ticket(current_app, tid, code, manual=True,
                            force=bool(body.get("force")),
                            allow_quiet=bool(body.get("allowQuiet")),
                            text_override=str(body.get("text") or "")[:2000],
                            subject_override=str(body.get("subject") or "")[:40])
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

    text = f"[운영팀] 문자 설정 시험입니다. 이 메시지가 보이면 정상입니다. ({config.now().strftime('%H:%M')})"
    res = send_sms(cfg, to, text)                    # ← 트랜잭션 밖
    with tx(write=True) as c2:
        c2.execute(
            "INSERT INTO sms_log(ticket_id, event, phone, text, msg_type, status, reason, "
            "sent_by, sent_at) VALUES(NULL,'test',?,?,?,?,?,?,?)",
            (to, text, res["msgType"], "sent" if res["ok"] else "failed",
             # ★성공해도 비즈고 응답을 남긴다(2026-09-03) — 예전에는 성공이면 빈 값이라
             #   "보냈다는데 문자가 안 온다"를 나중에 확인할 방법이 없었다.
             #   메시지키가 있으면 비즈고 콘솔에서 그 건을 조회할 수 있다.
             (f"{res.get('codes') or ''} msgKey={res.get('msgKey') or '-'}"
              if res["ok"] else res["detail"])[:300],
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
    return jsonify({"ok": res["ok"], "msgKey": res.get("msgKey") or "",
                    "codes": res.get("codes") or "",
                    "message": ("시험 문자를 보냈습니다. 휴대폰을 확인해 주세요."
                                + (f" (비즈고 접수번호 {res['msgKey']})" if res.get("msgKey") else "")
                                + " — 1~2분 안에 안 오면 비즈고 콘솔에서 발신번호 사전등록과"
                                  " 발송서버 IP 등록을 확인해 주세요.")
                    if res["ok"] else f"발송 실패: {res['detail'][:150]}"})


@bp.post("/sms/preview")
def sms_preview():
    """문구 미리보기 — 실제로 보내지 않고 길이·종류만 확인한다."""
    require_any("as.manage", "settings.manage")   # A/S ▸ 문자 양식 탭도 쓴다(2026-08-31)
    body = request.get_json(silent=True) or {}
    text = render_text(body.get("text") or "", {
        "고객명": "홍길동", "접수번호": "AS-260729-01", "증상": "액정 세로줄",
        "처리내용": "액정 교체", "송장번호": "123456789012", "관리번호": "260729-0001",
    })
    n = _byte_len(text)
    return jsonify({"text": text, "bytes": n,
                    "msgType": "LMS" if n > SMS_BYTE_LIMIT else "SMS",
                    "note": "90바이트(한글 45자)를 넘으면 장문(LMS)으로 나갑니다."})


# ------------------------------------------------- 문자 양식 탭(2026-08-31 대표)
#   "A/S 안에 문자 보내기 양식 탭을 만들어서 관리" — 설정 권한이 아니라 A/S 담당이 만진다.
#   API 키·발신번호 등 발송 설정은 여기서 안 건드린다(설정 ▸ 문자에 그대로).

@bp.get("/as-sms-templates")
def as_sms_templates():
    require_any("as.manage", "settings.manage")
    conn = get_db()
    cfg = sms_settings(conn)
    from flask import current_app
    live, why = is_live_sending(cfg, current_app.config["DB_PATH"])
    return jsonify({
        "live": live, "reason": why, "armed": bool(cfg.get("armed")),
        "events": [dict(event_config(cfg, e["code"]), when=e["when"], default=e["default"])
                   for e in EVENTS],
        "custom": custom_templates(cfg),
        "triggers": [{"code": k, "label": v} for k, v in TRIGGERS.items()],
        "vars": SMS_VARS,
    })


@bp.post("/as-sms-templates")
def save_as_sms_templates():
    """기본 양식(문구·on/off·제목)과 직접 만든 양식을 저장한다.

    ★설정 키 'sms' 안에 API 키 같은 비밀이 함께 산다 — 통째로 갈아끼우지 말고
      events/custom 두 칸만 바꿔 병합한다(마스크 사고 2026-08 교훈과 같은 원칙).
    """
    require_any("as.manage", "settings.manage")
    body = request.get_json(silent=True) or {}
    events = body.get("events")
    custom = body.get("custom")
    if events is None and custom is None:
        abort(400, description="바꿀 내용이 없습니다.")
    with tx(write=True) as conn:
        cfg = dict(sms_settings(conn))
        if events is not None:
            if not isinstance(events, dict):
                abort(400, description="events 형식이 올바르지 않습니다.")
            cur = dict(cfg.get("events") or {})
            for code, v in events.items():
                if code not in EVENT_CODES or not isinstance(v, dict):
                    abort(400, description=f"알 수 없는 기본 양식입니다: {code}")
                text = str(v.get("text") or "").strip()
                if not text:
                    abort(400, description="기본 양식의 문구는 비울 수 없습니다.")
                if len(text) > 1000:
                    abort(400, description="문구가 너무 깁니다(1,000자 이내).")
                cur[code] = {"on": bool(v.get("on")), "text": text,
                             "subject": str(v.get("subject") or "").strip()[:40]}
            cfg["events"] = cur
        if custom is not None:
            if not isinstance(custom, list):
                abort(400, description="custom 형식이 올바르지 않습니다.")
            if len(custom) > 20:
                abort(400, description="직접 만든 양식은 20개까지입니다.")
            used = {str(t.get("id")) for t in custom
                    if isinstance(t, dict) and t.get("id")}
            out, seq = [], 0
            for t in custom:
                if not isinstance(t, dict):
                    abort(400, description="양식 형식이 올바르지 않습니다.")
                label = str(t.get("label") or "").strip()
                text = str(t.get("text") or "").strip()
                if not label or not text:
                    abort(400, description="양식 이름과 문구는 필수입니다.")
                if len(label) > 30 or len(text) > 1000:
                    abort(400, description="이름 30자·문구 1,000자 이내로 해주세요.")
                trig = t.get("trigger") or ""
                if trig not in TRIGGERS:
                    abort(400, description=f"알 수 없는 발송 시점입니다: {trig}")
                cid = str(t.get("id") or "").strip()
                if not cid:
                    seq += 1
                    while f"c{seq}" in used:
                        seq += 1
                    cid = f"c{seq}"
                    used.add(cid)
                out.append({"id": cid, "label": label, "trigger": trig,
                            "subject": str(t.get("subject") or "").strip()[:40],
                            "text": text, "on": bool(t.get("on", True))})
            cfg["custom"] = out
        conn.execute(
            "INSERT INTO settings(key, value, updated_at, updated_by) VALUES('sms',?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (json.dumps(cfg, ensure_ascii=False), config.now_iso(), g.user["display_name"]))
        audit.log("as_sms_templates", target="문자 양식",
                  detail={"custom": len(cfg.get("custom") or [])})
    return jsonify({"ok": True})


@bp.get("/as-tickets/<int:tid>/sms-preview")
def ticket_sms_preview(tid):
    """발송 전 미리보기(2026-08-31 대표) — 이 접수 건의 실제 값으로 치환된 문구를 준다.

    화면이 이 문구를 편집칸에 담아 주고, 고친 그대로 보낼 수 있다(매크로도 다시 치환됨).
    """
    require("as.manage")
    code = (request.args.get("event") or "").strip()
    conn = get_db()
    t = conn.execute("SELECT * FROM as_tickets WHERE id=?", (tid,)).fetchone()
    if t is None:
        abort(404, description="A/S 건을 찾을 수 없습니다.")
    cfg = sms_settings(conn)
    ev, err = _resolve_template(cfg, code)
    if ev is None:
        abort(400, description=err or "보낼 안내 종류를 고르세요.")
    vars_ = _ticket_sms_vars(conn, t, cfg)
    text = render_text(ev["text"], vars_)
    sent = conn.execute(
        "SELECT COUNT(*) AS c FROM sms_log WHERE ticket_id=? AND (event=? OR event LIKE ?) "
        "AND status IN ('sent','simulated')", (tid, code, code + "#%")).fetchone()["c"]
    n = _byte_len(text)
    return jsonify({"code": code, "label": ev["label"], "on": ev["on"],
                    "text": text, "subject": ev.get("subject") or "",
                    "phone": t["phone"], "bytes": n,
                    "msgType": "LMS" if n > SMS_BYTE_LIMIT else "SMS",
                    "alreadySent": sent})
