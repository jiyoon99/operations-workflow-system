"""CJ 설정 가져오기, 견본 PDF, 주소정제 및 접수·취소 연동 도구.

공개본의 계약 식별값과 발송인 정보는 모두 예시 값이다.
실제 연동에는 사용자의 계약 설정이 필요하며, 가져오기는 실발행 무장을 켜지 않는다.
왕복 시험은 실제 접수 후 취소를 요청하므로 계약 설정과 무장 확인 후 사용한다.
취소에 실패하면 후속 처리가 가능하도록 송장번호를 반환한다.
"""
import json
import re
from datetime import datetime

from flask import Response, abort, jsonify, request

from .. import audit, config
from ..auth.perms import require, require_any
from ..db import tx
from ..settings import bp
from .client import _cj2_cust, cj2_addr_refine, cj2_new_invoice, cj2_reg_book
from .label import _cj2_sample_label, join_items
from .waybill_pdf import _cj_label_offset, cj2_waybill_pdf

# 공개용 예시 설정. 저장한 뒤에는 설정 화면의 값이 적용된다.
IDENTITY_PRESETS = {
    # 판매 업무용 발송인 예시.
    "operations": {
        "sender": {"name": "업무관리", "tel": "02-0000-0000", "zip": "00000",
                   "addr": "서울특별시 예시구 예시로 1 8층",
                   "addr_detail": "101호 업무관리"},
        "pickup": {"name": "업무관리", "tel": "02-0000-0000", "zip": "00000",
                   "addr": "서울특별시 예시구 예시로 1",
                   "addr_detail": "예시 업무센터 8층 101호 업무관리 앞"},
    },
    # 기존 렌탈 시스템과 구분하는 발송인 예시.
    "rms": {
        "sender": {"name": "예시 운영사/예시 렌탈사", "tel": "0000-0000", "zip": "00000",
                   "addr": "서울특별시 예시구 예시로 1 8층",
                   "addr_detail": "101호 예시 운영사/예시 렌탈사"},
        "pickup": {"name": "예시 렌탈사", "tel": "010-0000-0000", "zip": "00000",
                   "addr": "서울특별시 예시구 예시로 1",
                   "addr_detail": "예시 업무센터 8층 101호 예시 렌탈사 매니저 앞"},
    },
}

RMS_CJ_PRESET = {
    "cust_id": "00000000",
    "biz_reg_num": "0000000000",
    "env": "prod",
    # 접수 기본값 — RMS 대표 지정(2026-07-28): 운임 신용(03)·박스 극소(01)
    "frt_dv": "03",
    "box_type": "01",
    # 보내는분·회수지 표기(2026-08-13 대표): OWS는 판매 사업부라 '업무관리' 명의,
    # 전화는 02-0000-0000 — 주소·계약(고객코드/사업자번호)은 RMS와 공용 그대로.
    "sender": {
        "name": "업무관리",
        "tel": "02-0000-0000",
        "zip": "00000",
        "addr": "서울특별시 예시구 예시로 1 8층",
        "addr_detail": "101호 업무관리",
    },
    "pickup": {
        "name": "업무관리",
        "tel": "02-0000-0000",
        "zip": "00000",
        "addr": "서울특별시 예시구 예시로 1",
        "addr_detail": "예시 업무센터 8층 101호 업무관리 앞",
    },
}


def _load_cj(conn):
    row = conn.execute("SELECT value FROM settings WHERE key='cj'").fetchone()
    try:
        return json.loads(row["value"]) if row else {}
    except ValueError:
        return {}


def _ready_for_live(cfg):
    """실발행 가능 상태인가 — waybill._is_real 과 같은 기준(고객코드는 구명칭도 인정)."""
    return bool(cfg.get("env") == "prod" and cfg.get("armed")
                and _cj2_cust(cfg) and (cfg.get("biz_reg_num") or "").strip())


@bp.post("/cj/import-rms")
def cj_import_rms():
    """RMS의 CJ 설정을 그대로 가져온다 — 정상 설정 저장 경로(감사 기록 포함).

    ★armed(실발행 무장)는 가져오지 않는다. RMS에선 켜져 있지만, 가져오기 한 번에
      실발행까지 켜지면 시험 발급인 줄 알고 누른 송장이 진짜 접수된다.
      화면의 [실발행 무장] 체크는 사람이 눈으로 보고 켠다.
    """
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    ident = (body.get("identity") or "operations").strip()
    if ident not in IDENTITY_PRESETS:
        abort(400, description="identity는 operations 또는 rms 만 됩니다.")
    preset = {**RMS_CJ_PRESET, **IDENTITY_PRESETS[ident]}
    with tx(write=True) as conn:
        old = _load_cj(conn)
        merged = dict(old)
        for k, v in preset.items():
            if isinstance(v, dict):
                merged[k] = {**(old.get(k) or {}), **v}
            else:
                merged[k] = v
        merged.pop("customerCode", None)          # 구명칭 정리 — cust_id 로 통일
        merged.setdefault("armed", bool(old.get("armed")))
        from flask import g
        conn.execute(
            "INSERT INTO settings(key, value, updated_at, updated_by) VALUES('cj',?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (json.dumps(merged, ensure_ascii=False), config.now_iso(),
             g.user["display_name"] if g.get("user") else ""))
        audit.log("cj_import_rms", target="RMS→OWS CJ 설정 이식",
                  detail={"env": merged.get("env"), "custId": merged.get("cust_id"),
                          "identity": ident, "armedKept": bool(merged.get("armed"))})
    return jsonify({
        "ok": True,
        "custId": merged.get("cust_id"), "env": merged.get("env"),
        "armed": bool(merged.get("armed")),
        "senderName": (merged.get("sender") or {}).get("name", ""),
        "pickupName": (merged.get("pickup") or {}).get("name", ""),
        "identity": ident,
        "message": "RMS 값을 가져왔습니다. [연결 테스트]로 확인한 뒤, 실발행하려면 "
                   "[실발행 무장]을 직접 켜고 저장하세요.",
    })


# ---------------------------------------------------------------- 운송장 문구 템플릿
#   (2026-08-24 대표 지시) 어떤 키워드를 어떤 순서로 찍을지 화면에서 정한다.


@bp.get("/cj/label-tokens")
def cj_label_tokens():
    """쓸 수 있는 키워드 목록 + 지금 저장된 템플릿 + 샘플 미리보기."""
    require("settings.manage")
    from ..orders.waybill import (DEFAULT_ITEM_TMPL, DEFAULT_REMARK_TMPL,
                                  LABEL_TOKENS, label_tmpl_of)
    with tx() as conn:
        cfg = _load_cj(conn)
    return jsonify({
        "tokens": [{"name": k, "scope": v[0], "cap": v[1], "help": v[2]}
                   for k, v in LABEL_TOKENS.items()],
        "item": label_tmpl_of(cfg, "item"),
        "remark": label_tmpl_of(cfg, "remark"),
        "defaults": {"item": DEFAULT_ITEM_TMPL, "remark": DEFAULT_REMARK_TMPL},
        "limits": {"item": 120, "remark": 60},
    })


# 미리보기용 가짜 주문 — 실제 주문을 건드리지 않고 문구만 확인한다.
_SAMPLE_ORDER = {
    "channel": "고도몰", "product_code": "14-CK1007TU_i5-8_내장 AA급3",
    "product_name": "HP 인텔 i5 14인치 Full HD IPS 노트북", "quantity": 1,
    "option_name": "램 16G 업그레이드", "order_no": "20260824-0001234",
    "recipient": "홍길동", "delivery_message": "부재시 경비실에 맡겨주세요",
    "is_review": 0,
}
_SAMPLE_ASSETS = ["260628-0015", "260628-0016"]
_SAMPLE_PREP = ["리브레오피스", "정품 충전기"]


@bp.post("/cj/label-preview")
def cj_label_preview():
    """저장 전에 종이에 뭐가 찍힐지 — 샘플 주문으로 두 칸을 렌더한다.

    ★CJ를 부르지 않고, 주문/자산도 건드리지 않는다. 순수 문구 계산이다.
    """
    require("settings.manage")
    from ..orders.waybill import (_compose_items, _compose_remark, _remark_row,
                                  check_label_tmpl, label_tmpl_of)
    body = request.get_json(silent=True) or {}
    # ★안 보내면 '저장된 문구'로 본다 — 기본값으로 물러나면 견본 운송장과 화면이 어긋난다
    #   (2026-08-24: 견본은 저장본을 쓰는데 미리보기만 기본값을 써서 서로 달랐다).
    with tx() as conn:
        cfg = _load_cj(conn)
    item_t = (body.get("item") or "").strip() or label_tmpl_of(cfg, "item")
    remark_t = (body.get("remark") or "").strip() or label_tmpl_of(cfg, "remark")
    bad = {"item": check_label_tmpl(item_t, "item"),
           "remark": check_label_tmpl(remark_t, "remark")}
    from ..orders.waybill import label_layout_of
    items = _compose_items(dict(_SAMPLE_ORDER), _SAMPLE_ASSETS, simulated=False,
                           prep_names=_SAMPLE_PREP, tmpl=item_t)
    # ★견본 PDF 와 같은 조합기를 쓴다 — [줄바꿈]이 화면 글자 미리보기에도 보여야 한다
    summary = join_items(items, label_layout_of(cfg)["line_break"])
    remark = _compose_remark(_remark_row(dict(_SAMPLE_ORDER), _SAMPLE_ASSETS),
                             _SAMPLE_PREP, tmpl=remark_t)
    return jsonify({
        "itemSummary": summary, "itemLen": len(summary),
        "remark": remark, "remarkLen": len(remark),
        "unknown": bad,
        "sample": {"assetNos": _SAMPLE_ASSETS, "prepOptions": _SAMPLE_PREP,
                   **{k: v for k, v in _SAMPLE_ORDER.items() if k != "is_review"}},
    })


@bp.get("/cj/sample-pdf")
def cj_sample_pdf():
    """견본 운송장 PDF — CJ 호출 없이 라벨 레이아웃·프린터 정렬을 확인한다.

    ★저장한 운송장 문구가 그대로 찍혀야 한다(대표 2026-08-24: "문구 수정하고 저장
      누르면 견본 운송장에도 떠야하지 않을까? 안뜨네"). 예전에는 견본이 고정 문구
      ('사무용 노트북 x2 / 모니터 x1')를 찍어서, 문구를 고쳐도 종이로 확인할 길이 없었다.
    """
    require("settings.manage")
    from ..orders.waybill import (_compose_items, _compose_remark, _remark_row,
                                  label_layout_of, label_tmpl_of)
    with tx() as conn:
        cfg = _load_cj(conn)
    # ★고치는 중인 문구를 그대로 받아 본다(대표 2026-08-24: "견본운송장 자체를 운송장 문구
    #   이쪽 레이아웃에 실시간으로 반영"). 안 주면 저장된 문구.
    item_t = (request.args.get("item") or "").strip() or label_tmpl_of(cfg, "item")
    remark_t = (request.args.get("remark") or "").strip() or label_tmpl_of(cfg, "remark")
    label = _cj2_sample_label(cfg.get("sender") or {})
    lay = label_layout_of(cfg)
    label["item_lines"] = lay["item_lines"]      # 종이가 설정을 그대로 따른다
    # 미리보기와 같은 샘플 주문으로, 같은 문구 계산을 거친다 — 화면과 종이가 같아야 한다
    items = _compose_items(dict(_SAMPLE_ORDER), _SAMPLE_ASSETS, simulated=False,
                           prep_names=_SAMPLE_PREP, tmpl=item_t)
    # ★조합은 join_items 한 곳에서만 한다 — 실제 발급 라벨과 글자까지 같아야 한다
    label["item_summary"] = join_items(items, lay["line_break"])
    label["remark"] = _compose_remark(_remark_row(dict(_SAMPLE_ORDER), _SAMPLE_ASSETS),
                                      _SAMPLE_PREP, tmpl=remark_t)
    # ★?rot=0 → 화면용 바로선 견본(123×100 가로). 기본(rot 생략)은 프린터 방향 그대로 —
    #   라벨프린터(XP-DT108B)는 세로급지라 90° 눕혀 보내야 종이에 바로 나온다(동결 검수 방식).
    #   화면 미리보기가 그걸 그대로 띄우면 누워 보인다(대표 2026-08-24: "옆으로 누워있어서
    #   보기가 어렵네") — 보는 각도만 다르고 내용·좌표는 동일하다.
    upright = (request.args.get("rot") or "").strip() in ("0", "false")
    off = _cj_label_offset(cfg.get("label") or {})
    pdf = (cj2_waybill_pdf(label, True, *off[:5], rot=False) if upright
           else cj2_waybill_pdf(label, True, *off))
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": "inline; filename=cj-sample.pdf"})


@bp.post("/cj/addr-test")
def cj_addr_test():
    """주소정제(ReqAddrRfnSm) 왕복 시험 — 조회성 호출, 배송 예약이 생기지 않는다."""
    require("settings.manage")
    with tx() as conn:
        cfg = _load_cj(conn)
    if not (_cj2_cust(cfg) and (cfg.get("biz_reg_num") or "").strip()):
        abort(400, description="고객코드와 사업자등록번호를 먼저 저장하세요.")
    body = request.get_json(silent=True) or {}
    addr = (body.get("address") or "").strip() or (cfg.get("sender") or {}).get("addr", "")
    if not addr:
        abort(400, description="정제할 주소가 없습니다 — 보내는분 주소를 먼저 저장하세요.")
    refined = cj2_addr_refine(cfg, addr)
    if not refined:
        abort(502, description="주소정제 호출이 실패했습니다. [연결 테스트]로 토큰부터 확인하세요.")
    audit.log("cj_addr_test", target=addr[:80])
    return jsonify({"ok": True, "address": addr,
                    "clsfcd": refined.get("CLSFCD", ""),
                    "clsfaddr": refined.get("CLSFADDR", ""),
                    "bran": refined.get("CLLDLVBRANNM", ""),
                    "message": "주소정제 성공 — 분류코드가 나오면 운송장 라우팅이 됩니다."})


@bp.post("/cj/live-roundtrip")
def cj_live_roundtrip():
    """★실발행 왕복 시험 — 우리 주소로 진짜 접수(RegBook)한 뒤 곧바로 취소(CnclBook).

    이게 성공하면 CJ 연동은 끝까지 검증된 것이다. DB에 송장 행은 만들지 않는다.
    운영(prod) + 실발행 무장 상태에서만 동작한다 — 개발/미무장이면 400.
    """
    require("settings.manage")
    with tx() as conn:
        cfg = _load_cj(conn)
    if not _ready_for_live(cfg):
        abort(400, description="운영(prod) + 실발행 무장 + 고객코드/사업자번호가 모두 있어야 "
                               "실발행 시험을 할 수 있습니다. 그 전엔 [연결 테스트]와 "
                               "[견본 운송장]으로 확인하세요.")
    sender = dict(cfg.get("sender") or {})
    if not (sender.get("name") and sender.get("addr")):
        abort(400, description="보내는분 이름·주소를 먼저 저장하세요.")

    rcpt_ymd = datetime.now().strftime("%Y%m%d")
    use_no = ("OWSTEST" + datetime.now().strftime("%m%d%H%M%S"))[:50]
    # 접수 — 우리 주소 → 우리 주소, 품목명에 시험임을 박아 둔다
    invoice = cj2_new_invoice(cfg)
    reg = cj2_reg_book(
        cfg, kind="ship", sender=sender, receiver=sender,
        items=[{"name": "[OWS 연동시험 — 즉시취소] 문서", "qty": 1}],
        cust_use_no=use_no, rcpt_ymd=rcpt_ymd, invc_no=invoice,
        remark="OWS-RMS 이식 검증용 시험 접수 — 즉시 취소됩니다")
    if not reg.get("ok"):
        audit.log("cj_live_roundtrip", target="접수 실패",
                  detail={"resultCd": reg.get("result_cd"), "detail": reg.get("detail")})
        abort(502, description=f"접수 실패 [{reg.get('result_cd')}] {reg.get('detail')}")

    cancel = cj2_reg_book(
        cfg, kind="ship", sender=sender, receiver=sender,
        items=[{"name": "[OWS 연동시험 — 즉시취소] 문서", "qty": 1}],
        cust_use_no=use_no, rcpt_ymd=rcpt_ymd, invc_no=invoice, cancel=True)
    audit.log("cj_live_roundtrip", target=f"송장 {invoice}",
              detail={"reg": reg.get("result_cd"), "cancel": cancel.get("result_cd"),
                      "cancelled": bool(cancel.get("ok"))})
    if not cancel.get("ok"):
        # ★취소 실패 = 실제 예약이 살아 있다. 숨기면 기사가 온다 — 크게 알린다.
        return jsonify({
            "ok": False, "invoice": invoice, "regResult": reg.get("result_cd"),
            "cancelResult": cancel.get("result_cd"), "cancelDetail": cancel.get("detail"),
            "message": f"⚠ 접수는 됐는데 취소가 실패했습니다. 송장번호 {invoice} — "
                       "CJ 담당(계약 담당자)에게 이 번호로 취소를 요청하세요.",
        }), 502
    return jsonify({
        "ok": True, "invoice": invoice,
        "message": f"실발행 왕복 성공 — 접수(송장 {invoice}) 후 곧바로 취소했습니다. "
                   "CJ 연동이 끝까지 검증됐습니다.",
    })


@bp.get("/cj/pickup-calendar")
def cj_pickup_calendar():
    """집화(회수) 가능일 표 — 화면이 쉬는 날을 고르지 못하게 만든다(2026-09-07 대표).

    조회만 하는 창구라 A/S 담당자도 읽는다(회수 예약을 그 사람이 건다).
    """
    require_any("as.manage", "orders.ship", "waybills.manage", "settings.manage")
    from .calendar import next_pickup_day, pickup_calendar, pickup_settings
    with tx() as conn:
        cfg = _load_cj(conn)
    try:
        days = int(request.args.get("days") or 45)
    except (TypeError, ValueError):
        days = 45
    rules = pickup_settings(cfg)
    return jsonify({
        "enabled": rules["enabled"],
        "offWeekdays": rules["offWeekdays"],
        "holidays": rules["holidays"],
        "next": next_pickup_day(cfg),
        "days": pickup_calendar(cfg, days=days, start=request.args.get("from")),
    })
