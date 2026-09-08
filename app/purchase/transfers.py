"""사업부 자산 이관 — 렌탈(RMS) ↔ 판매(OWS) 양방향 커밋.

배경
  예시 운영사 = 렌탈 사업부(RMS, 240 PC:5000) + 판매 사업부(OWS, 185 PC:5100).
  매입은 매입팀 한 곳이 공통으로 하고 매입 시스템이 OWS에 붙어 있어,
  자산 소유 이력의 단일 원장을 OWS에 둔다.
  공통 키는 TMS 관리번호(YYMMDD-NNNN) = assets.asset_no.

흐름 — 2단계 커밋
  ① 제안  POST /api/transfers                → 가드 검사 + commit_id 발급 (pending)
                                                ★아무것도 안 바뀐다. 차단 사유를 먼저 보여주는 미리보기.
  ② 커밋  POST /api/transfers/<cid>/commit   → OWS 측 적용 + 스냅샷 저장 (committed)
  ③ 수신  POST /api/transfers/<cid>/ack      → RMS가 자기 쪽 반영을 마쳤다고 알림 (done)

원칙
  - commit_id 기준 멱등. 같은 커밋을 두 번 적용해도 결과가 같다.
  - 검증을 전부 끝낸 뒤에 쓰기를 시작한다(반쪽 반영 금지, 원칙 #1).
  - 한쪽만 적용되고 끊긴 커밋은 committed로 남아 화면에 '미완료 이관'으로 뜬다.
  - snapshot이 없으면 되돌릴 수 없다 — 커밋 시점에 반드시 남긴다.

RMS 창구
  /api/bridge/transfers/… 는 같은 처리를 공유 시크릿(X-Bridge-Token)으로 연다.
  RMS 자산관리 ▸ [자산번호로 판매]가 이 창구를 쓴다.
"""
import json
import os
import re
import sqlite3

from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import DIVISION_RENTAL, DIVISION_SALE, get_db, tx
from . import asset_event, bp

DIRECTIONS = {
    "rental->sale": (DIVISION_RENTAL, DIVISION_SALE),
    "sale->rental": (DIVISION_SALE, DIVISION_RENTAL),
}

# 렌탈에서 돌아온 물건이 OWS에서 갖게 될 상태.
#   status=in_stock : 검수 대기. 바로 판매가능(ready)으로 올리지 않는다 —
#                     검수 안 한 물건이 몰에 올라가는 사고를 막는다.
#   tier=실재고     : "셋팅·시트지·간단보수 후 판매 가능". 고객이 쓰다 돌아온 물건은
#                     기본값 '가용'(바로 판매 가능)일 수 없다.
#   stock_listed=0  : 몰 노출은 사람이 확인하고 켠다.
_ARRIVE_STATUS = "in_stock"
_ARRIVE_TIER = "실재고"

# 이미 나가 버린 물건은 넘길 수 없다 — 실물 위치를 먼저 확인해야 한다.
_BLOCK_STATUS = ("shipped", "scrapped")

# ★대표 지시(2026-08-24): "매입중복, 판매기록 충돌만 제외하고 나머지만 이관될 수 있게."
#   → 데이터 정리를 이유로 막는 것은 그 두 가지뿐이다. 나머지는 경고로 알리고 넘긴다.
#
#   특히 status='shipped'는 대부분 진짜 판매 출고가 아니다. 7/30 TMS 이관 때
#   재고상태 '렌탈'이 shipped로 매핑돼 들어온 흔적이다(migration.py TMS_STATUS_MAP).
#   실측(2026-08-24): 렌탈 자산 중 shipped 1,020대 가운데 742대만 RMS가 "지금 나가 있다"고
#   하고, 278대는 창고에 있는데 상태만 shipped다. 그 278대를 막을 이유가 없다.
#
#   그래서 '실물이 지금 고객에게 있는가'는 OWS의 status가 아니라 RMS 상태로 판정한다.
#   (RMS 화면에서 시작한 이관은 RMS가 대여중/예약을 이미 걸러 보낸다 — 여기는 OWS에서
#    시작한 이관까지 같은 기준으로 막기 위한 것이다.)
_RMS_OUT_STATUS = ("rented", "holding")


def _rms_says_out(rms, asset_no):
    """RMS가 이 관리번호를 '지금 고객에게 나가 있다'고 보는가.

    RMS에 기록이 없거나 여러 건이면 판정하지 않는다(False) — 그건 다른 가드가 본다.
    """
    lst = rms.get((asset_no or "").strip().upper()) or []
    return len(lst) == 1 and (lst[0].get("status") or "").strip() in _RMS_OUT_STATUS

# ★확정 안 한 제안의 유효시간(분).
#   미리보기만 하고 창을 닫거나 네트워크가 끊기면 pending 제안이 남는데, 그게 영원히
#   그 자산을 붙잡으면 "왜 이 자산은 이관이 안 되지"가 된다(2026-08-13 검증에서 실제 발생 —
#   전날 테스트의 pending 하나가 자산을 계속 막고 있었다).
#   화면은 닫을 때 reject를 부르지만, 브라우저가 죽으면 그것도 못 부른다. 그래서 시간으로도 푼다.
#   ★커밋된 것(committed)은 만료되지 않는다 — 그건 실제로 적용된 이관이다.
PENDING_TTL_MIN = 30


def _pending_cutoff():
    from datetime import timedelta
    return (config.now() - timedelta(minutes=PENDING_TTL_MIN)).isoformat(timespec="seconds")


# ────────────────────────────── RMS 대조 (2026-08-12 정합성 실측에서 나온 가드)
#
# 실측: 겹치는 자산 1,344대 중 양쪽 다 시리얼이 있는 건 571건(42%)뿐이고 그중 8건이 어긋난다.
#   예) 250114-0124  RMS 12484M2  vs  OWS 91KY7H2   ← 아예 다른 기계
#       250224-0002  RMS 시리얼 칸에 관리번호가 들어가 있음
# 관리번호 하나에 실물 두 대가 얽혀 있을 수 있다는 뜻이다. 그대로 넘기면
# RMS가 보낸 노트북과 OWS가 받은 노트북이 서로 다른 물건이 된다 → 차단한다.
#
# 모델명은 165건이 다른데 대부분 표기 차이('Dell 5500' vs 'LATITUDE 5500')라
# 차단하지 않고 미리보기에 나란히 보여 사람이 판단하게 한다.
RMS_DB_PATH = os.getenv("RMS_DB", r"E:\rental-system\rental_system.db")


_TMS_NO = re.compile(r"^\d{6}-\d{4}$")


def _norm_sn(v):
    return re.sub(r"[\s\-]", "", str(v or "")).upper()


def _norm_model(v):
    return re.sub(r"[\s\-_/]", "", str(v or "")).upper()


def _rms_from_request():
    """이관 요청에 실려 온 RMS 쪽 사실 — {관리번호대문자: [자산]} 형태로.

    ★OWS는 185에서 돌고 RMS DB는 240에 있어 파일로 읽을 수 없다(2026-08-14 확인).
      그래서 RMS가 이관을 요청할 때 자기 시리얼·모델·상태를 함께 보낸다.
      이게 없으면 '같은 번호에 실물 두 대' 차단이 조용히 빠진다 — 가장 중요한 가드다.
    """
    try:
        body = request.get_json(silent=True) or {}
    except RuntimeError:            # 요청 밖(스크립트)에서 부른 경우
        return {}
    out = {}
    for a in (body.get("rmsAssets") or []):
        no = str(a.get("assetNo") or "").strip()
        if not no:
            continue
        out.setdefault(no.upper(), []).append({
            "mgmt_no": no,
            "serial_number": a.get("serial") or "",
            "model_name": a.get("model") or "",
            "status": a.get("status") or "",
        })
    return out


def _rms_index(nos):
    """관리번호 → RMS 자산(살아있는 것만). RMS DB를 못 읽으면 빈 dict — 이관은 계속된다.

    ★읽기 전용으로만 연다. RMS DB에 쓰는 일은 절대 없다.
    """
    out = {}
    want = {n.upper() for n in nos}
    try:
        conn = sqlite3.connect(f"file:{RMS_DB_PATH}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return out
    try:
        for _k, d in conn.execute("SELECT key, data FROM assets"):
            try:
                j = json.loads(d)
            except ValueError:
                continue
            if j.get("deleted"):
                continue
            no = (j.get("mgmt_no") or "").strip()
            if no and no.upper() in want:
                out.setdefault(no.upper(), []).append(j)
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return out


def _rms_from_copy(conn, nos):
    """RMS가 밀어 넣어 둔 사본(rms_inventory)으로 대조한다 — 파일을 못 읽을 때의 대체.

    ★OWS는 185에서 돌고 RMS DB는 240에 있어 파일로는 못 읽는다. 그래서 OWS 화면에서
      시작한 이관은 시리얼 대조가 통째로 빠진 채 진행되고 있었다(2026-09-03 대표 신고:
      "RMS 자산 데이터를 읽을 수 없습니다 … 이렇게 뜨고 이관완료라고 뜨는데 맞아?").
      맞지 않다 — 가장 중요한 가드가 조용히 빠진 것이다.
    ★사본은 RMS가 화면을 열 때마다 갱신된다. 오래됐을 수 있으므로 '언제 받은 사본인지'를
      경고에 실어 사람이 판단하게 한다(_rms_copy_age).
    """
    want = {n.upper() for n in nos}
    out = {}
    try:
        rows = conn.execute(
            "SELECT asset_no, status, serial, model FROM rms_inventory").fetchall()
    except sqlite3.Error:
        return {}
    for r in rows:
        no = (r["asset_no"] or "").strip()
        if no and no.upper() in want:
            out.setdefault(no.upper(), []).append({
                "mgmt_no": no,
                "serial_number": r["serial"] or "",
                "model_name": r["model"] or "",
                "status": r["status"] or "",
                "_from_copy": True,
            })
    return out


def _rms_copy_age(conn):
    """RMS 사본을 언제 받았나(ISO). 한 번도 못 받았으면 빈 문자열."""
    try:
        row = conn.execute("SELECT MAX(synced_at) AS at FROM rms_inventory").fetchone()
    except sqlite3.Error:
        return ""
    return (row["at"] if row else "") or ""


def _norm_nos(raw):
    """붙여넣기(줄바꿈·쉼표·공백 섞임)를 관리번호 목록으로. 중복은 한 번만."""
    if isinstance(raw, str):
        raw = re.split(r"[\s,;]+", raw)
    seen, out = set(), []
    for x in (raw or []):
        n = str(x or "").strip()
        if n and n.upper() not in seen:
            seen.add(n.upper())
            out.append(n)
    return out


def _next_commit_id(conn):
    """TRF-YYMMDD-NNNN. 번호는 재사용하지 않는다(MAX+1)."""
    prefix = f"TRF-{config.now().strftime('%y%m%d')}-"
    row = conn.execute(
        "SELECT MAX(CAST(substr(commit_id, ?) AS INTEGER)) AS m FROM asset_transfers "
        "WHERE commit_id LIKE ?", (len(prefix) + 1, prefix + "%")).fetchone()
    return f"{prefix}{(row['m'] or 0) + 1:04d}"


def _inspect(conn, nos, src, dst, exclude_cid=None):  # noqa: C901
    """가드 검사. 쓰기 없음.

    result: ok(이관 가능) / noop(이미 그쪽) / blocked(사유 있음) / unmatched(OWS에 없음)

    exclude_cid: 커밋 직전 재검증에서 '자기 자신'을 이중 이관으로 세지 않기 위한 것.
      ★이게 없으면 모든 커밋이 자기 제안을 중복으로 보고 전부 차단된다
        (2026-08-04 검증에서 실제로 잡은 결함).
    """
    out = []
    # ★RMS가 보내 준 값이 있으면 그걸 쓴다(OWS가 185에서 돌면 RMS DB 파일을 못 읽는다).
    #   없을 때만 같은 PC에 있다고 보고 파일을 읽는다 — 개발·단독 실행용 보조 경로다.
    rms = _rms_from_request() or _rms_index(nos) or _rms_from_copy(conn, nos)
    for no in nos:
        rows = conn.execute(
            "SELECT * FROM assets WHERE asset_no = ? COLLATE NOCASE", (no,)).fetchall()
        if not rows:
            # ★RMS 자체 채번(2026-NNNN 등 56건)은 TMS 번호가 아니라 공통키가 없다.
            #   '없는 번호'라고만 하면 담당자가 오타인 줄 알고 계속 다시 친다.
            note = ("TMS 형식(YYMMDD-NNNN)이 아닌 자체 채번 번호입니다 — 자산번호 관리 이전에 "
                    "나간 제품이라 반납 시 채번한 뒤 이관하세요."
                    if not _TMS_NO.match(no) else "OWS에 없는 관리번호입니다.")
            out.append({"assetNo": no, "result": "unmatched", "assetId": None, "note": note})
            continue
        # ★같은 관리번호가 둘 이상이면 어느 실물인지 특정할 수 없다.
        #   자동으로 고르면 엉뚱한 물건이 넘어간다 — 사람이 번호를 고친 뒤에 처리한다.
        # ★관리번호는 UNIQUE라 여기 걸릴 일이 없다(구조적 보장). 방어로만 남긴다 —
        #   실제 매입 중복은 아래 시리얼 쌍둥이 검사에서 걸린다.
        if len(rows) > 1:
            out.append({"assetNo": no, "result": "blocked", "assetId": None,
                        "note": f"관리번호가 {len(rows)}건 중복 등록돼 있습니다. 번호를 정리한 뒤 이관하세요."})
            continue
        a = rows[0]
        # ★미리보기에도 제품을 함께 — 번호만으로는 무엇을 넘기는지 대조할 수 없다
        #   (2026-09-03 대표: "어떤 제품인지 자산번호랑 함께 쭉 나열해줬으면").
        item = {"assetNo": a["asset_no"], "assetId": a["id"], "model": a["model"],
                "serial": a["serial"], "status": a["status"], "division": a["division"],
                "maker": a["maker"] or "", "grade": a["grade"] or "",
                "spec": " / ".join(x for x in (a["cpu"], a["ram"], a["ssd"]) if x),
                "purchasePrice": a["purchase_price"] or 0, "tier": a["tier"] or "",
                "prevStatus": a["status"], "prevTier": a["tier"]}

        if a["division"] == dst:
            item.update(result="noop",
                        note=f"이미 {'렌탈' if dst == DIVISION_RENTAL else '판매'} 사업부입니다.")
        elif a["division"] != src:
            item.update(result="blocked", note=f"현재 사업부가 {a['division']}라 이 방향으로 넘길 수 없습니다.")
        elif a["division_locked"]:
            item.update(result="blocked",
                        note=f"예외 잠금 자산입니다({(a['division_note'] or '')[:60]}). 잠금 해제 후 이관하세요.")
        elif _blocking_commit(conn, a["asset_no"], exclude_cid):
            item.update(result="blocked",
                        note=f"이미 이관 {_blocking_commit(conn, a['asset_no'], exclude_cid)}에 "
                             "포함돼 있습니다.")
        elif _data_holds(conn, a):
            # ★'기록이 틀렸을 수 있는' 보류 — 매입중복·판매기록.
            #   ★한 자산에 둘 다 걸릴 수 있다. 하나만 알려 주면 사람이 풀고 또 막혀서
            #     같은 버튼을 두 번 누르게 된다(대표 2026-09-01: "3단계는 번거로워").
            #     그래서 걸린 사유를 전부 모아서 한 번에 알려 준다.
            holds = _data_holds(conn, a)
            notes = []
            if "dup_buy" in holds:
                tw = _serial_twin(conn, a["id"], a["serial"])
                notes.append(
                    f"같은 시리얼({a['serial']})이 {tw['asset_no']}"
                    f"({'렌탈' if tw['division'] == DIVISION_RENTAL else '판매'}·{tw['status']})"
                    "에도 등록돼 있습니다 — 매입 중복입니다.")
            if "sold_rec" in holds:
                _kind, why = _sale_record(conn, a["id"])
                notes.append(f"OWS에 판매 기록이 있습니다 ({why}) — 실제로 팔린 것인지 "
                             "번호가 잘못 붙은 것인지 확인하세요.")
            item.update(result="blocked", overridable=",".join(holds),
                        note=" / ".join(notes)
                             + " 기록이 틀렸다면 [보류 해제]로 풀 수 있습니다.")
        elif a["status"] == "scrapped":
            # 폐기는 어느 방향이든 넘기지 않는다 — 넘길 실물이 없다.
            item.update(result="blocked", note="폐기(scrapped) 자산입니다 — 넘길 실물이 없습니다.")
        elif a["status"] == "shipped" and dst == DIVISION_RENTAL:
            # 판매→렌탈에서 shipped는 진짜 '팔려 나간' 것이다. 그건 막는다.
            item.update(result="blocked",
                        note="이미 판매 출고(shipped)된 자산입니다 — 실물이 어디 있는지 확인한 뒤 처리하세요.")
        elif dst == DIVISION_SALE and _rms_says_out(rms, a["asset_no"]):
            # ★렌탈→판매인데 RMS가 '지금 고객에게 나가 있다'고 한다 — 반납 후에 넘긴다.
            #   OWS의 status가 아니라 RMS 상태로 판정한다(위 _RMS_OUT_STATUS 주석 참조).
            item.update(result="blocked",
                        note="RMS에서 대여중/예약 상태입니다 — 반납 후 이관하세요.")
        elif dst == DIVISION_RENTAL and a["stock_listed"]:
            item.update(result="blocked", note="쇼핑몰 재고로 올라가 있습니다. 재고반영을 먼저 해제하세요.")
        elif dst == DIVISION_RENTAL and conn.execute(
                "SELECT o.order_no FROM order_assets oa JOIN orders o ON o.id = oa.order_id "
                "WHERE oa.asset_id = ? AND o.cancelled_at = '' LIMIT 1", (a["id"],)).fetchone():
            held = conn.execute(
                "SELECT o.order_no FROM order_assets oa JOIN orders o ON o.id = oa.order_id "
                "WHERE oa.asset_id = ? AND o.cancelled_at = '' LIMIT 1", (a["id"],)).fetchone()
            item.update(result="blocked",
                        note=f"판매 주문 {held['order_no']}에 매칭돼 있습니다. 매칭을 먼저 푸세요.")
        else:
            # ★RMS 대조 — 같은 관리번호가 정말 같은 실물인지 본다
            rlist = rms.get(a["asset_no"].upper(), [])
            if len(rlist) > 1:
                item.update(result="blocked",
                            note=f"RMS에 같은 관리번호가 {len(rlist)}건 등록돼 있습니다. "
                                 "RMS에서 번호를 정리한 뒤 이관하세요.")
                out.append(item)
                continue
            warn = []
            if rlist:
                r = rlist[0]
                rsn, hsn = _norm_sn(r.get("serial_number")), _norm_sn(a["serial"])
                if rsn and hsn and rsn != hsn:
                    # ★2026-08-24 대표 지시로 차단 → 경고로 내렸다(막는 건 매입중복·판매기록뿐).
                    #   그래도 조용히 넘기지는 않는다 — 실물이 두 대 얽혀 있을 수 있는 건이라
                    #   미리보기에서 '확인이 필요한 건'으로 크게 보여 준다. 실측 4대.
                    warn.append(f"★시리얼 다름 — RMS {r.get('serial_number')} / OWS {a['serial']}"
                                " (같은 관리번호에 실물이 두 대 얽혀 있을 수 있습니다)")
                rm, hm = _norm_model(r.get("model_name")), _norm_model(a["model"])
                if rm and hm and rm != hm and rm not in hm and hm not in rm:
                    warn.append(f"모델명 다름(RMS {r.get('model_name')} / OWS {a['model']})")
                if not rsn and not hsn:
                    warn.append("양쪽 다 시리얼 없음 — 실물 대조 불가")
                item["rmsStatus"] = r.get("status") or ""
            elif dst == DIVISION_SALE:
                # 렌탈→판매인데 RMS에 그 번호가 없다. 임자를 확인할 수 없다.
                warn.append("RMS에 이 관리번호가 없습니다")
            # ★풀어 준 건은 조용히 넘어가면 안 된다 — 누가 왜 풀었는지 확정 전에 보여 준다.
            if (a["hold_override"] or "").strip():
                warn.append(_override_note(a))
            if rlist and rlist[0].get("_from_copy"):
                # ★파일이 아니라 사본으로 대조했다 — 언제 받은 사본인지 밝힌다.
                age = _rms_copy_age(conn)[:16].replace("T", " ")
                warn.append(f"RMS 사본으로 대조함({age or '시각 불명'}) — 파일을 직접 읽지 못했습니다")
            if a["status"] == "shipped":
                # 여기까지 왔다는 건 RMS가 '나가 있다'고 하지 않았다는 뜻이다.
                # TMS 이관 흔적으로 상태만 shipped인 건이라 넘기되, 한 번 보라고 알린다.
                warn.append("OWS 상태가 출고(shipped)입니다 — TMS 이관 때 렌탈 출고가 그렇게 "
                            "들어온 건이라 실물이 창고에 있는지 한 번 확인하세요")
            item.update(result="ok", note=" / ".join(warn), warn=bool(warn))
        out.append(item)
    return out


def _serial_twin(conn, asset_id, serial):
    """같은 시리얼을 쓰는 다른 OWS 자산의 관리번호. 없으면 None.

    ★같은 실물이 두 번 매입 등록된 경우다(실측 40종 86대). 그중 하나만 넘기면
      나머지 한 벌이 남아 재고가 부풀고, 어느 쪽이 진짜인지 알 수 없게 된다.
      관리번호는 UNIQUE라 중복이 안 생기지만 시리얼은 막는 장치가 없다.
    """
    sn = _norm_sn(serial)
    if len(sn) < 5:                      # 너무 짧으면 시리얼로 안 본다(빈칸·'X' 등)
        return None
    row = conn.execute(
        "SELECT asset_no, division, status FROM assets "
        "WHERE id <> ? AND REPLACE(REPLACE(UPPER(serial),' ',''),'-','') = ? LIMIT 1",
        (asset_id, sn)).fetchone()
    return row


def _sale_record(conn, asset_id):
    """이 자산에 남아 있는 '팔렸다'는 기록. (종류, 설명) 또는 None.

    ★RMS는 렌탈이라는데 OWS에는 판매전표가 붙어 있는 경우다(실측 28대).
      상태(shipped)로만 걸러지던 것을 기록 자체로 막는다 — 반납돼서 in_stock으로
      돌아온 물건은 상태로는 안 걸린다(실측 1대가 그렇게 통과했다).
    """
    ev = conn.execute(
        "SELECT detail FROM asset_events WHERE asset_id=? AND action='판매' "
        "ORDER BY id DESC LIMIT 1", (asset_id,)).fetchone()
    if ev:
        try:
            d = json.loads(ev["detail"] or "{}")
        except ValueError:
            d = {}
        slip = d.get("판매전표") or d.get("판매일") or ""
        who = d.get("수령자") or ""
        return ("slip", f"판매전표 {slip}{(' · ' + who) if who else ''}")
    od = conn.execute(
        "SELECT o.order_no FROM order_assets oa JOIN orders o ON o.id = oa.order_id "
        "WHERE oa.asset_id = ? AND o.cancelled_at = '' LIMIT 1", (asset_id,)).fetchone()
    if od:
        return ("order", f"판매 주문 {od['order_no']}")
    return None


# 사람이 풀 수 있는 보류 사유 — '기록이 틀렸을 수 있는' 것만이다.
#   dup_buy  : 같은 시리얼이 두 번 등록됨(매입 중복)
#   sold_rec : OWS에 판매전표·주문 기록이 있음
# 대여중·폐기·진행중 커밋·예외잠금은 여기 없다 — 기록이 틀린 게 아니라 지금 그런 상태다.
OVERRIDABLE = ("dup_buy", "sold_rec")


def _data_holds(conn, a):
    """이 자산에 걸린 '기록이 틀렸을 수 있는' 보류 사유 전부(사람이 푼 것은 뺀다).

    ★하나만 돌려주면 화면이 한 번에 못 푼다 — 풀고 나서 또 막히기 때문이다.
    """
    out = []
    if (not _hold_overridden(a, "dup_buy")
            and _serial_twin(conn, a["id"], a["serial"]) is not None):
        out.append("dup_buy")
    if not _hold_overridden(a, "sold_rec") and _sale_record(conn, a["id"]) is not None:
        out.append("sold_rec")
    return out


def _hold_overridden(row, kind):
    """이 자산의 그 보류 사유를 사람이 풀어 뒀나."""
    try:
        cur = row["hold_override"] or ""
    except (IndexError, KeyError):
        return False
    return kind in [x.strip() for x in cur.split(",") if x.strip()]


def _override_note(row):
    """미리보기에 붙일 '누가·언제·왜 풀었는지'. 조용히 넘어가면 안 된다."""
    try:
        who = row["hold_override_by"] or ""
        at = (row["hold_override_at"] or "")[:10]
        why = row["hold_override_note"] or ""
    except (IndexError, KeyError):
        return ""
    return f"★보류 해제됨({who}{(' ' + at) if at else ''}) — {why}"


def _blocking_commit(conn, asset_no, exclude_cid=None):
    """이 자산을 붙잡고 있는 다른 커밋 ID. 없으면 None.

    committed는 항상 막고, pending은 최근 것만 막는다(오래된 제안은 만료로 본다).
    """
    row = conn.execute(
        "SELECT t.commit_id FROM asset_transfer_items i "
        "JOIN asset_transfers t ON t.commit_id = i.commit_id "
        "WHERE i.asset_no = ? AND i.result = 'ok' AND t.commit_id <> ? "
        "  AND (t.state = 'committed' "
        "       OR (t.state = 'pending' AND t.requested_at >= ?)) LIMIT 1",
        (asset_no, exclude_cid or "", _pending_cutoff())).fetchone()
    return row["commit_id"] if row else None


def _expire_stale_pendings(conn):
    """유효시간이 지난 제안을 정리한다 — 목록에 유령 pending이 쌓이지 않게."""
    return conn.execute(
        "UPDATE asset_transfers SET state='expired' "
        "WHERE state='pending' AND requested_at < ?", (_pending_cutoff(),)).rowcount


def _summary(items):
    s = {"ok": 0, "blocked": 0, "unmatched": 0, "noop": 0}
    for i in items:
        s[i["result"]] = s.get(i["result"], 0) + 1
    return s


def _items_of(conn, cid):
    """이관 항목. ★자산의 모델·시리얼·스펙을 함께 붙인다(2026-09-03 대표).

    "RMS 내에서 자산이관되면, 팝업창으로 떠서 어떤 제품인지 자산번호랑 함께 쭉
     나열해줬으면 좋겠어 — 대조해보고 RMS로 넘겨야 해."
    번호만 있으면 무엇을 받는지 알 수 없어 대조 자체가 불가능하다.
    """
    return [{"assetNo": r["asset_no"], "assetId": r["asset_id"], "result": r["result"],
             "note": r["block_note"], "prevDivision": r["prev_division"],
             "prevStatus": r["prev_status"],
             "maker": r["maker"] or "", "model": r["model"] or "",
             "serial": r["serial"] or "", "grade": r["grade"] or "",
             "spec": " / ".join(x for x in (r["cpu"], r["ram"], r["ssd"]) if x),
             "purchasePrice": r["purchase_price"] or 0,
             "status": r["status"] or "", "tier": r["tier"] or ""}
            for r in conn.execute(
                "SELECT i.*, a.maker, a.model, a.serial, a.grade, a.cpu, a.ram, a.ssd, "
                "       a.purchase_price, a.status, a.tier "
                "FROM asset_transfer_items i "
                "LEFT JOIN assets a ON a.asset_no = i.asset_no COLLATE NOCASE "
                "WHERE i.commit_id=? ORDER BY i.asset_no", (cid,))]


def _payload(t, items=None):
    out = {
        "commitId": t["commit_id"], "direction": t["direction"], "state": t["state"],
        "assetCount": t["asset_count"], "reason": t["reason"], "source": t["source"],
        "requestedBy": t["requested_by"], "requestedAt": t["requested_at"],
        "committedBy": t["committed_by"], "committedAt": t["committed_at"],
        "owsAckedAt": t["ows_acked_at"], "rmsAckedAt": t["rms_acked_at"],
        "rolledBackAt": t["rolled_back_at"], "rolledBackBy": t["rolled_back_by"],
    }
    if items is not None:
        out["items"] = items
        out["summary"] = _summary(items)
    return out


# ─────────────────────────────────────────────────────────── 공통 처리(화면·창구 공유)

def _do_propose(body, source):
    direction = (body.get("direction") or "").strip()
    if direction not in DIRECTIONS:
        abort(400, description="direction은 rental->sale 또는 sale->rental 이어야 합니다.")
    nos = _norm_nos(body.get("assetNos"))
    if not nos:
        abort(400, description="관리번호를 입력하세요.")
    if len(nos) > 2000:
        abort(400, description="한 번에 2,000건까지만 처리합니다.")
    reason = (body.get("reason") or "").strip()[:500]
    src, dst = DIRECTIONS[direction]
    with tx(write=True) as conn:
        _expire_stale_pendings(conn)      # 묵은 제안부터 정리하고 시작한다
        items = _inspect(conn, nos, src, dst)
        cid = _next_commit_id(conn)
        ts = config.now_iso()
        conn.execute(
            "INSERT INTO asset_transfers(commit_id, direction, state, asset_count, reason, "
            " source, requested_by, requested_at, created_at) VALUES(?,?,'pending',?,?,?,?,?,?)",
            (cid, direction, sum(1 for i in items if i["result"] == "ok"), reason,
             source, g.user["display_name"], ts, ts))
        for i in items:
            conn.execute(
                "INSERT INTO asset_transfer_items(commit_id, asset_no, asset_id, result, "
                " block_note, prev_division, prev_status) VALUES(?,?,?,?,?,?,?)",
                (cid, i["assetNo"], i.get("assetId"), i["result"], i.get("note", ""),
                 i.get("division", ""), i.get("prevStatus", "")))
        # ★걸러진 자산을 이력에 남긴다(대표 2026-08-14 "정리되지 않은 것들 함께 남겨두면").
        #   제안은 화면을 닫으면 사라지지만, 자산에 붙은 이력은 남아서 나중에 정리할 때 근거가 된다.
        #   같은 사유가 반복 쌓이지 않게 직전 기록과 다를 때만 남긴다.
        for i in items:
            if i["result"] != "blocked" or not i.get("assetId"):
                continue
            prev = conn.execute(
                "SELECT detail FROM asset_events WHERE asset_id=? AND action='이관 보류' "
                "ORDER BY id DESC LIMIT 1", (i["assetId"],)).fetchone()
            note = i.get("note", "")
            if prev:
                try:
                    if (json.loads(prev["detail"] or "{}") or {}).get("사유") == note:
                        continue
                except ValueError:
                    pass
            asset_event(conn, i["assetId"], "이관 보류",
                        {"커밋": cid, "방향": direction, "사유": note})
        audit.log("transfer_propose", target=cid,
                  detail={"direction": direction, "source": source, **_summary(items)})
        t = conn.execute("SELECT * FROM asset_transfers WHERE commit_id=?", (cid,)).fetchone()
        return _payload(t, items)


def _do_commit(cid):
    with tx(write=True) as conn:
        t = conn.execute("SELECT * FROM asset_transfers WHERE commit_id=?", (cid,)).fetchone()
        if t is None:
            abort(404, description="이관 커밋을 찾을 수 없습니다.")
        if t["state"] in ("committed", "done"):
            return _payload(t, _items_of(conn, cid)), 200        # 멱등
        if t["state"] == "expired":
            abort(400, description=f"제안이 만료됐습니다({PENDING_TTL_MIN}분 경과). 다시 조회해 주세요.")
        if t["state"] != "pending":
            abort(400, description=f"{t['state']} 상태라 커밋할 수 없습니다.")

        src, dst = DIRECTIONS[t["direction"]]
        rows = conn.execute(
            "SELECT * FROM asset_transfer_items WHERE commit_id=? AND result='ok'", (cid,)).fetchall()

        # 1) 재검증 — 제안 이후 상황이 바뀌었을 수 있다(다른 사람이 몰에 올렸다든지)
        recheck = _inspect(conn, [r["asset_no"] for r in rows], src, dst, exclude_cid=cid)
        bad = [i for i in recheck if i["result"] != "ok"]
        if bad:
            for i in bad:
                conn.execute(
                    "UPDATE asset_transfer_items SET result=?, block_note=? "
                    "WHERE commit_id=? AND asset_no=?",
                    (i["result"], i.get("note", ""), cid, i["assetNo"]))
                if i.get("assetId"):
                    asset_event(conn, i["assetId"], "이관 보류",
                                {"커밋": cid, "시점": "커밋 직전 재검증",
                                 "사유": i.get("note", "")})
            conn.execute("UPDATE asset_transfers SET asset_count=? WHERE commit_id=?",
                         (sum(1 for i in recheck if i["result"] == "ok"), cid))
            t2 = conn.execute("SELECT * FROM asset_transfers WHERE commit_id=?", (cid,)).fetchone()
            return {**_payload(t2, _items_of(conn, cid)), "recheckBlocked": len(bad),
                    "message": f"제안 이후 상황이 바뀐 자산 {len(bad)}건이 있어 커밋을 멈췄습니다. "
                               "차단 사유를 확인하고 다시 커밋하세요."}, 409

        # 2) 적용 — 여기서부터 쓰기
        ts, actor = config.now_iso(), g.user["display_name"]
        snapshot = []
        for i in recheck:
            aid = i["assetId"]
            snapshot.append({"assetNo": i["assetNo"], "assetId": aid,
                             "division": i["division"], "status": i["prevStatus"],
                             "tier": i.get("prevTier", "")})
            if dst == DIVISION_SALE:
                # 렌탈 → 판매: 고객이 쓰다 돌아온 물건이다. 검수 전이므로 바로 팔 수 있는 상태로 두지 않는다.
                conn.execute(
                    "UPDATE assets SET division=?, division_since=?, division_by=?, division_ref=?, "
                    " status=?, tier=?, stock_listed=0, updated_at=? WHERE id=?",
                    (dst, ts, actor, cid, _ARRIVE_STATUS, _ARRIVE_TIER, ts, aid))
            else:
                # 판매 → 렌탈: 상태는 그대로 둔다. division만으로 판매재고에서 빠진다.
                conn.execute(
                    "UPDATE assets SET division=?, division_since=?, division_by=?, division_ref=?, "
                    " stock_listed=0, updated_at=? WHERE id=?",
                    (dst, ts, actor, cid, ts, aid))
            asset_event(conn, aid, "사업부 이관", {
                "커밋": cid, "방향": t["direction"], "이전": i["division"], "이후": dst,
                "사유": t["reason"]})

        conn.execute(
            "UPDATE asset_transfers SET state='committed', asset_count=?, committed_by=?, "
            " committed_at=?, ows_acked_at=?, snapshot=? WHERE commit_id=?",
            (len(recheck), actor, ts, ts, json.dumps(snapshot, ensure_ascii=False), cid))
        audit.log("transfer_commit", target=cid,
                  detail={"direction": t["direction"], "count": len(recheck)})
        t2 = conn.execute("SELECT * FROM asset_transfers WHERE commit_id=?", (cid,)).fetchone()
        return _payload(t2, _items_of(conn, cid)), 200


def _do_ack(cid):
    with tx(write=True) as conn:
        t = conn.execute("SELECT * FROM asset_transfers WHERE commit_id=?", (cid,)).fetchone()
        if t is None:
            abort(404, description="이관 커밋을 찾을 수 없습니다.")
        if t["state"] == "done":
            return _payload(t, _items_of(conn, cid))             # 멱등
        if t["state"] != "committed":
            abort(400, description=f"{t['state']} 상태는 수신 확인할 수 없습니다.")
        conn.execute("UPDATE asset_transfers SET state='done', rms_acked_at=? WHERE commit_id=?",
                     (config.now_iso(), cid))
        audit.log("transfer_ack", target=cid)
        t2 = conn.execute("SELECT * FROM asset_transfers WHERE commit_id=?", (cid,)).fetchone()
        return _payload(t2, _items_of(conn, cid))


def _do_reject(cid):
    """커밋하지 않은 제안을 버린다.

    ★이게 없으면 미리보기만 하고 닫은 제안이 그 자산을 영원히 붙잡아
      다음 이관에서 '이미 이관 TRF-…에 포함돼 있습니다'로 계속 막힌다
      (2026-08-04 검증에서 잡은 결함).
    """
    with tx(write=True) as conn:
        t = conn.execute("SELECT * FROM asset_transfers WHERE commit_id=?", (cid,)).fetchone()
        if t is None:
            abort(404, description="이관 커밋을 찾을 수 없습니다.")
        if t["state"] == "rejected":
            return _payload(t, _items_of(conn, cid))             # 멱등
        if t["state"] != "pending":
            abort(400, description=f"{t['state']} 상태는 취소할 수 없습니다. 적용된 이관은 되돌리기를 쓰세요.")
        conn.execute("UPDATE asset_transfers SET state='rejected' WHERE commit_id=?", (cid,))
        audit.log("transfer_reject", target=cid)
        t2 = conn.execute("SELECT * FROM asset_transfers WHERE commit_id=?", (cid,)).fetchone()
        return _payload(t2, _items_of(conn, cid))


# ────────────────────────────────────────────────────────────── OWS 화면용 라우트

@bp.post("/transfers")
def create_transfer():
    require("purchase.edit")
    return jsonify(_do_propose(request.get_json(silent=True) or {}, "ows"))


@bp.post("/transfers/<cid>/commit")
def commit_transfer(cid):
    require("purchase.edit")
    payload, code = _do_commit(cid)
    return jsonify(payload), code


@bp.post("/transfers/<cid>/reject")
def reject_transfer(cid):
    require("purchase.edit")
    return jsonify(_do_reject(cid))


@bp.post("/transfers/<cid>/ack")
def ack_transfer(cid):
    require("purchase.edit")
    return jsonify(_do_ack(cid))


@bp.post("/transfers/<cid>/rollback")
def rollback_transfer(cid):
    """커밋을 스냅샷 기준으로 되돌린다. RMS가 이미 받은 뒤에는 막는다."""
    require("purchase.edit")
    with tx(write=True) as conn:
        t = conn.execute("SELECT * FROM asset_transfers WHERE commit_id=?", (cid,)).fetchone()
        if t is None:
            abort(404, description="이관 커밋을 찾을 수 없습니다.")
        if t["state"] == "rolled_back":
            return jsonify(_payload(t, _items_of(conn, cid)))    # 멱등
        if t["state"] != "committed":
            abort(400, description=f"{t['state']} 상태는 되돌릴 수 없습니다.")
        if t["rms_acked_at"]:
            abort(400, description="RMS가 이미 받은 이관입니다. 반대 방향 이관으로 처리하세요.")
        try:
            snap = json.loads(t["snapshot"] or "[]")
        except ValueError:
            abort(400, description="스냅샷이 손상돼 되돌릴 수 없습니다.")
        ts, actor = config.now_iso(), g.user["display_name"]
        for s in snap:
            conn.execute(
                "UPDATE assets SET division=?, status=?, tier=?, division_since='', "
                " division_by='', division_ref='', updated_at=? WHERE id=?",
                (s["division"], s["status"], s.get("tier") or "가용", ts, s["assetId"]))
            asset_event(conn, s["assetId"], "사업부 이관 취소",
                        {"커밋": cid, "복원": f"{s['division']}/{s['status']}"})
        conn.execute(
            "UPDATE asset_transfers SET state='rolled_back', rolled_back_at=?, rolled_back_by=? "
            "WHERE commit_id=?", (ts, actor, cid))
        audit.log("transfer_rollback", target=cid, detail={"count": len(snap)})
        t2 = conn.execute("SELECT * FROM asset_transfers WHERE commit_id=?", (cid,)).fetchone()
        return jsonify(_payload(t2, _items_of(conn, cid)))


@bp.get("/transfers")
def list_transfers():
    require("purchase.view")
    conn = get_db()
    sql, params = "SELECT * FROM asset_transfers WHERE 1=1", []
    if request.args.get("state"):
        sql += " AND state = ?"
        params.append(request.args["state"])
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT 200", params).fetchall()
    # ★미완료(커밋했는데 RMS가 아직 안 받아간 것) 건수 — 화면 배너가 이 값만 본다
    stuck = conn.execute(
        "SELECT COUNT(*) AS c FROM asset_transfers WHERE state='committed' AND rms_acked_at=''"
    ).fetchone()["c"]
    return jsonify({"transfers": [_payload(t) for t in rows], "stuck": stuck})


@bp.get("/transfers/<cid>")
def transfer_detail(cid):
    require("purchase.view")
    conn = get_db()
    t = conn.execute("SELECT * FROM asset_transfers WHERE commit_id=?", (cid,)).fetchone()
    if t is None:
        abort(404, description="이관 커밋을 찾을 수 없습니다.")
    return jsonify(_payload(t, _items_of(conn, cid)))


@bp.get("/transfers/peer-status")
def peer_status():
    """상대 시스템(RMS)을 읽을 수 있나 — 이관 화면이 열릴 때 먼저 확인한다.

    RMS 쪽 /api/ows/ping 과 짝을 이룬다(양쪽 화면 규격 통일, 2026-08-13 대표 지시).
    OWS는 RMS DB를 파일로 직접 읽으므로 '연결'은 곧 그 파일을 읽을 수 있는지다.
    """
    require("purchase.view")
    try:
        conn = sqlite3.connect(f"file:{RMS_DB_PATH}?mode=ro", uri=True, timeout=3)
        n = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        conn.close()
        return jsonify({"ok": True, "system": "RMS", "assets": n, "path": RMS_DB_PATH})
    except sqlite3.Error as e:
        # ★파일을 못 읽는 건 정상이다 — OWS는 185, RMS DB는 240에 있다.
        #   그때는 RMS가 밀어 넣어 둔 사본으로 대조한다. 그러니 '위험'이 아니라
        #   '사본으로 대조 중'이라고 말해야 맞다(2026-09-03 대표 신고로 드러난 오해).
        conn = get_db()
        n = 0
        try:
            n = conn.execute("SELECT COUNT(*) FROM rms_inventory").fetchone()[0]
        except sqlite3.Error:
            n = 0
        if n:
            at = _rms_copy_age(conn)[:16].replace("T", " ")
            return jsonify({
                "ok": True, "system": "RMS", "assets": n, "source": "copy",
                "syncedAt": at, "path": RMS_DB_PATH,
                "note": f"RMS 사본으로 대조합니다({at or '시각 불명'} 기준 {n:,}대). "
                        "RMS 화면을 한 번 열면 사본이 갱신됩니다."})
        return jsonify({"ok": False, "system": "RMS", "path": RMS_DB_PATH, "source": "none",
                        "error": f"렌탈 시스템(RMS) 자산을 읽을 수도, 사본도 없습니다 "
                                 f"({e.__class__.__name__}). 시리얼 대조 없이 넘기면 "
                                 "같은 번호에 실물 두 대가 얽혀 있어도 못 걸러냅니다 — "
                                 "RMS 화면을 한 번 열어 사본을 만든 뒤 진행하세요."})


@bp.get("/assets/rms-mismatch")
def rms_mismatch():
    """RMS와 OWS가 같은 관리번호를 두고 서로 다른 말을 하는 자산 목록.

    2026-08-12 정합성 실측에서 드러난 것들이다. 이관 전에 사람이 정리해야 한다.
      serial   : 시리얼이 다름 — 실물이 두 대 얽혀 있을 수 있다(★이관 차단 대상)
      model    : 모델명이 다름 — 대부분 표기 차이지만 14Z960/14Z970처럼 진짜 다른 것도 있다
      status   : RMS는 창고에 있다는데 OWS는 나갔다고 하는 등 상태가 모순
      no_serial: 양쪽 다 시리얼이 없어 대조할 근거 자체가 없다
      rms_only : RMS에만 있는 관리번호(자체 채번 포함) — 공통키가 없다
      dup_buy  : ★같은 시리얼이 OWS에 두 번 등록됨 — 매입 중복(이관 차단)
      sold_rec : ★렌탈 귀속인데 OWS에 판매전표/주문 기록이 있음(이관 차단)
    """
    require("purchase.view")
    conn = get_db()
    ows = {r["asset_no"]: r for r in conn.execute(
        "SELECT id, asset_no, serial, model, status, division FROM assets")}
    rms = _rms_index(list(ows))          # 겹치는 것만 읽는다
    all_rms = _rms_index_all()
    out = []
    # ★매입 중복(같은 시리얼 두 번)과 판매기록 충돌은 RMS 대조와 무관하게 잡아야 한다.
    #   렌탈 귀속 자산이 이 상태면 OWS로 넘기면 안 되는 것들이다.
    sn_map = {}
    for r in conn.execute("SELECT id, asset_no, serial, division, status FROM assets"):
        sn = _norm_sn(r["serial"])
        if len(sn) >= 5:
            sn_map.setdefault(sn, []).append(dict(r))
    dup_rows = {}
    for sn, lst in sn_map.items():
        if len(lst) > 1:
            for x in lst:
                dup_rows[x["asset_no"]] = [y for y in lst if y["asset_no"] != x["asset_no"]]
    sold_ids = {r["asset_id"] for r in conn.execute(
        "SELECT DISTINCT asset_id FROM asset_events WHERE action='판매'")}
    ordered_ids = {r["asset_id"] for r in conn.execute(
        "SELECT DISTINCT oa.asset_id FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
        "WHERE o.cancelled_at=''")}

    for no, a in ows.items():
        extra = []
        if no in dup_rows:
            extra.append("dup_buy")
        if a["division"] == DIVISION_RENTAL and (a["id"] in sold_ids or a["id"] in ordered_ids):
            extra.append("sold_rec")
        rlist = rms.get(no.upper())
        if (not rlist or len(rlist) > 1) and extra:
            # RMS 대조는 못 하지만 이관을 막아야 하는 것 — 목록에는 올린다
            out.append({"assetNo": no, "kinds": extra, "division": a["division"],
                        "rmsSerial": "", "owsSerial": a["serial"],
                        "rmsModel": "", "owsModel": a["model"],
                        "rmsStatus": "", "owsStatus": a["status"], "rmsRenter": "",
                        "twin": (dup_rows.get(no) or [{}])[0].get("asset_no", "")})
            continue
        if not rlist or len(rlist) > 1:
            continue
        r = rlist[0]
        rsn, hsn = _norm_sn(r.get("serial_number")), _norm_sn(a["serial"])
        rm, hm = _norm_model(r.get("model_name")), _norm_model(a["model"])
        kinds = []
        if rsn and hsn and rsn != hsn:
            kinds.append("serial")
        elif not rsn and not hsn:
            kinds.append("no_serial")
        if rm and hm and rm != hm and rm not in hm and hm not in rm:
            kinds.append("model")
        # 상태 모순 — RMS가 '창고에 있다'는데 OWS는 '나갔다', 또는 그 반대
        rst, hst = (r.get("status") or ""), a["status"]
        if rst == "available" and hst in ("shipped", "scrapped"):
            kinds.append("status")
        elif rst == "rented" and hst in ("in_stock", "ready", "refurbishing"):
            kinds.append("status")
        kinds += extra
        if kinds:
            out.append({
                "assetNo": no, "kinds": kinds, "division": a["division"],
                "twin": (dup_rows.get(no) or [{}])[0].get("asset_no", ""),
                "rmsSerial": r.get("serial_number") or "", "owsSerial": a["serial"],
                "rmsModel": r.get("model_name") or "", "owsModel": a["model"],
                "rmsStatus": rst, "owsStatus": hst,
                "rmsRenter": r.get("current_renter") or "",
            })
    rms_only = sorted(k for k in all_rms if k not in {n.upper() for n in ows})
    counts = {}
    for o in out:
        for k in o["kinds"]:
            counts[k] = counts.get(k, 0) + 1
    counts["rms_only"] = len(rms_only)
    # 이관을 막는 것(시리얼 불일치·매입중복·판매기록)을 맨 위로 — 먼저 정리해야 할 것들이다
    _pri = {"serial": 0, "dup_buy": 1, "sold_rec": 2}
    out.sort(key=lambda x: (min([_pri.get(k, 9) for k in x["kinds"]]), x["assetNo"]))
    return jsonify({"items": out[:500], "total": len(out), "counts": counts,
                    "rmsOnly": rms_only[:200]})


def _rms_index_all():
    """RMS 전체(살아있는 것) — 'RMS에만 있는 번호'를 세기 위한 것."""
    out = {}
    try:
        conn = sqlite3.connect(f"file:{RMS_DB_PATH}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return out
    try:
        for _k, d in conn.execute("SELECT key, data FROM assets"):
            try:
                j = json.loads(d)
            except ValueError:
                continue
            if j.get("deleted"):
                continue
            no = (j.get("mgmt_no") or "").strip()
            if no:
                out.setdefault(no.upper(), []).append(j)
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return out


@bp.post("/assets/division-exception")
def set_division_exception():
    """사람이 판단한 귀속을 고정한다 — 자동 판정·TMS 재이관이 뒤집지 못하게.

    2026-08-04: TMS 관리번호 오배정으로 판매전표가 잘못 붙은 13건을 이 방식으로 처리했다.
    판매전표 기록은 지우지 않고 사유에 원문을 남긴다(매출 대조 때 추적돼야 한다).
    """
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    nos = _norm_nos(body.get("assetNos"))
    division = (body.get("division") or "").strip()
    note = (body.get("note") or "").strip()[:500]
    unlock = bool(body.get("unlock"))
    if not nos:
        abort(400, description="관리번호를 입력하세요.")
    if not unlock and division not in (DIVISION_SALE, DIVISION_RENTAL):
        abort(400, description="division은 sale 또는 rental 이어야 합니다.")
    done, unmatched = [], []
    with tx(write=True) as conn:
        ts, actor = config.now_iso(), g.user["display_name"]
        for no in nos:
            a = conn.execute("SELECT * FROM assets WHERE asset_no=? COLLATE NOCASE", (no,)).fetchone()
            if a is None:
                unmatched.append(no)
                continue
            if unlock:
                conn.execute("UPDATE assets SET division_locked=0, updated_at=? WHERE id=?",
                             (ts, a["id"]))
                asset_event(conn, a["id"], "사업부 예외 잠금 해제", {"사유": note})
            else:
                conn.execute(
                    "UPDATE assets SET division=?, division_locked=1, division_note=?, "
                    " division_since=?, division_by=?, updated_at=? WHERE id=?",
                    (division, note, ts, actor, ts, a["id"]))
                asset_event(conn, a["id"], "사업부 예외 등록",
                            {"사업부": division, "사유": note, "이전": a["division"]})
            done.append(no)
        audit.log("division_exception", target=f"{len(done)}건",
                  detail={"division": division, "unlock": unlock, "note": note[:100]})
    return jsonify({"done": done, "doneCount": len(done), "unmatched": unmatched})


@bp.post("/bridge/assets/hold-override")
def bridge_hold_override():
    """RMS 이관 창에서 바로 보류를 풀 수 있게(대표 2026-09-01: "3단계는 번거로워").

    세션판과 같은 처리다 — 자격만 공유 시크릿으로 받는다.
    """
    return jsonify(_do_hold_override(request.get_json(silent=True) or {}))


@bp.post("/assets/hold-override")
def set_hold_override():
    """이관 보류 사유를 사람이 푼다(대표 2026-09-01, 260731-0032).

    "TMS에서 원래 반입처리가 되어야 하는데 담당자가 깜빡했다.
     TMS 반입처리 후 바로 적용이 안 되니 임의로 풀 수 있는 방법이 있나?"

    판매 기록 가드는 asset_events 의 '판매' 사건을 본다 — 지난 기록이라 지울 수도 없고
    주문을 취소해도 안 사라진다. 반품·오등록이면 그 자산은 영영 이관 불가로 남는다.

    ★푸는 것은 자산 한 대 · 사유 하나다. 전체를 여는 스위치가 아니다.
      (대표 2026-08-14: "정리 안 된 자산은 절대 안 넘긴다 · 강제 이관 우회로 없음")
    ★왜 푸는지(note)는 필수다 — 근거 없이 푼 기록은 나중에 아무도 못 되짚는다.
    ★풀어도 조용히 넘어가지 않는다 — 이관 미리보기에 누가·왜 풀었는지 경고로 뜬다.
    body: {assetNos:[...], kinds:["sold_rec","dup_buy"], note:"...", undo?:bool}
    """
    require("purchase.edit")
    return jsonify(_do_hold_override(request.get_json(silent=True) or {}))


def _do_hold_override(body):
    nos = _norm_nos(body.get("assetNos"))
    undo = bool(body.get("undo"))
    note = (body.get("note") or "").strip()[:300]
    kinds = [str(k).strip() for k in (body.get("kinds") or []) if str(k).strip()]
    if not nos:
        abort(400, description="관리번호를 입력하세요.")
    bad = [k for k in kinds if k not in OVERRIDABLE]
    if bad:
        abort(400, description=f"풀 수 없는 사유입니다: {', '.join(bad)}. "
                               "매입중복(dup_buy)·판매기록(sold_rec)만 풀 수 있습니다.")
    if not undo:
        if not kinds:
            abort(400, description="풀 사유를 고르세요(매입중복·판매기록).")
        if not note:
            abort(400, description="왜 푸는지 사유를 적어 주세요 — 나중에 근거가 됩니다.")

    done, unmatched = [], []
    with tx(write=True) as conn:
        ts, actor = config.now_iso(), g.user["display_name"]
        for no in nos:
            a = conn.execute("SELECT * FROM assets WHERE asset_no=? COLLATE NOCASE",
                             (no,)).fetchone()
            if a is None:
                unmatched.append(no)
                continue
            if undo:
                conn.execute(
                    "UPDATE assets SET hold_override='', hold_override_note='', "
                    " hold_override_by=?, hold_override_at=?, updated_at=? WHERE id=?",
                    (actor, ts, ts, a["id"]))
                asset_event(conn, a["id"], "이관 보류 해제 취소", {"사유": note})
            else:
                cur = [x for x in (a["hold_override"] or "").split(",") if x]
                for k in kinds:
                    if k not in cur:
                        cur.append(k)
                conn.execute(
                    "UPDATE assets SET hold_override=?, hold_override_note=?, "
                    " hold_override_by=?, hold_override_at=?, updated_at=? WHERE id=?",
                    (",".join(cur), note, actor, ts, ts, a["id"]))
                asset_event(conn, a["id"], "이관 보류 해제",
                            {"사유항목": ", ".join(kinds), "사유": note})
            done.append(no)
        audit.log("hold_override", target=f"{len(done)}건",
                  detail={"kinds": kinds, "undo": undo, "note": note[:120]})
    return {"done": done, "doneCount": len(done), "unmatched": unmatched}


# ────────────────────────────────────────────────────── RMS 창구 (공유 시크릿)
#
# RMS 자산관리 ▸ [자산번호로 판매]가 부른다. 사람 세션이 없으므로
# app/__init__.py의 전역 게이트가 X-Bridge-Token으로 판정한 뒤 여기로 들어온다.
# 권한 검사(require)는 하지 않는다 — 토큰 자체가 자격이다.

@bp.get("/bridge/ping")
def bridge_ping():
    """RMS가 '지금 OWS가 살아 있나'를 확인하는 자리. 토큰이 맞아야 200."""
    return jsonify({"ok": True, "system": "OWS", "at": config.now_iso()})


@bp.post("/bridge/transfers")
def bridge_propose():
    return jsonify(_do_propose(request.get_json(silent=True) or {}, "rms"))


@bp.post("/bridge/transfers/<cid>/commit")
def bridge_commit(cid):
    payload, code = _do_commit(cid)
    return jsonify(payload), code


@bp.post("/bridge/transfers/<cid>/reject")
def bridge_reject(cid):
    return jsonify(_do_reject(cid))


@bp.post("/bridge/transfers/<cid>/ack")
def bridge_ack(cid):
    return jsonify(_do_ack(cid))


@bp.get("/bridge/transfers/pending")
def bridge_pending():
    """RMS가 아직 자기 쪽에 반영하지 않은 커밋 — OWS→RMS 방향이 여기로 흘러간다."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM asset_transfers WHERE state='committed' AND rms_acked_at='' "
        "ORDER BY id").fetchall()
    return jsonify([_payload(t, [i for i in _items_of(conn, t["commit_id"])
                                 if i["result"] == "ok"]) for t in rows])


# ────────────────────────────────────── RMS 재고 사본 · '렌탈인데 RMS에 없는 자산'
#
# 배경(2026-08-24 대표 지적, P260805-001)
#   매입은 두 사업부의 공통 앞단이다. 한 자산에 대해 세 가지가 각각 맞아야 한다:
#     ① 매입 전표에 남는다(사업부와 무관)  ② OWS에서 렌탈로 표시된다
#     ③ RMS에 자산이 있다 — 렌탈팀이 실제로 운용하려면
#   ③이 빠진 것이 실측 229대 있었다. 이관 창구로는 못 넘긴다 — 이미 division='rental'이라
#   판매→렌탈 이관이 noop으로 끝난다. 그래서 '사업부를 바꾸는' 일이 아니라
#   'RMS에 없는 것을 만들어 주는' 별도 경로가 필요하다.
#
# 왜 RMS가 목록을 밀어 넣나
#   OWS(185)는 RMS DB(240)를 읽을 수 없다. 비교하려면 둘 중 하나는 상대 목록을 알아야 하는데,
#   RMS가 자기 관리번호만 보내면(1,400여 개 · 20KB 남짓) OWS가 걸러 돌려주는 쪽이
#   응답이 작고, 매입 화면 필터도 같은 사본을 쓸 수 있다.

def _rms_inventory_synced_at(conn):
    row = conn.execute("SELECT MAX(synced_at) AS at FROM rms_inventory").fetchone()
    return (row["at"] if row else "") or ""


def _rental_missing_rows(conn):
    """OWS가 렌탈로 들고 있는데 RMS 사본에는 없는 자산.

    ★사본이 비어 있으면 빈 목록을 돌려준다 — '한 번도 안 받아봤다'와
      '정말 RMS에 없다'를 섞으면 멀쩡한 자산을 없는 것으로 만든다.
    """
    if not _rms_inventory_synced_at(conn):
        return []
    return conn.execute(
        "SELECT a.id, a.asset_no, a.maker, a.model, a.serial, a.status, a.tier, "
        "       a.grade, a.cpu, a.ram, a.ssd, a.inch, a.purchase_price, "
        "       b.slip_no, b.purchase_date, COALESCE(rep.name, s.name) AS supplier_name "
        "FROM assets a "
        "LEFT JOIN purchase_batches b ON b.id = a.batch_id "
        "LEFT JOIN suppliers s ON s.id = b.supplier_id "
        "LEFT JOIN suppliers rep ON rep.id = s.alias_of "
        "WHERE a.division = ? AND TRIM(COALESCE(a.asset_no,'')) <> '' "
        "  AND NOT EXISTS (SELECT 1 FROM rms_inventory r "
        "                  WHERE r.asset_no = a.asset_no COLLATE NOCASE) "
        "ORDER BY a.asset_no", (DIVISION_RENTAL,)).fetchall()


def _missing_payload(r):
    return {
        "assetNo": r["asset_no"], "assetId": r["id"],
        "maker": r["maker"] or "", "model": r["model"] or "",
        "serial": r["serial"] or "", "status": r["status"], "tier": r["tier"] or "",
        "grade": r["grade"] or "", "cpu": r["cpu"] or "", "ram": r["ram"] or "",
        "ssd": r["ssd"] or "", "inch": r["inch"] or "",
        "purchasePrice": r["purchase_price"] or 0,
        "slipNo": r["slip_no"] or "", "purchaseDate": r["purchase_date"] or "",
        "supplier": r["supplier_name"] or "",
    }


@bp.post("/bridge/assets/rms-sync")
def bridge_rms_sync():
    """RMS가 자기 관리번호 목록을 밀어 넣고, '렌탈인데 RMS에 없는 자산'을 받아 간다.

    body: {"assets": [{"assetNo": "...", "status": "..."} ...]}  (또는 {"nos": [...]})
    ★목록을 통째로 갈아 끼운다 — RMS에서 지운 자산이 사본에 남으면 안 된다.
    ★비어 있는 목록은 거절한다. 사고로 빈 배열이 오면 전 자산이 'RMS에 없음'이 돼 버린다.
    """
    body = request.get_json(silent=True) or {}
    raw = body.get("assets")
    pairs = []
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, dict):
                no = str(x.get("assetNo") or "").strip()
                if no:
                    pairs.append((no, str(x.get("status") or "").strip(),
                                  str(x.get("renter") or "").strip()[:60],
                                  str(x.get("serial") or "").strip()[:80],
                                  str(x.get("model") or "").strip()[:120]))
    if not pairs:
        for x in (body.get("nos") or []):
            no = str(x or "").strip()
            if no:
                pairs.append((no, "", "", "", ""))
    if not pairs:
        abort(400, description="RMS 자산 목록이 비어 있습니다 — 사본을 갈아 끼우지 않았습니다.")

    ts = config.now_iso()
    with tx(write=True) as conn:
        conn.execute("DELETE FROM rms_inventory")
        conn.executemany(
            "INSERT OR REPLACE INTO rms_inventory"
            "(asset_no, status, renter, serial, model, synced_at) VALUES(?,?,?,?,?,?)",
            [(no, st, rn, sn, md, ts) for no, st, rn, sn, md in pairs])
    conn = get_db()
    rows = _rental_missing_rows(conn)
    return jsonify({"syncedAt": ts, "rmsCount": len(pairs),
                    "missingCount": len(rows),
                    "missing": [_missing_payload(r) for r in rows]})


@bp.get("/bridge/assets/rental-missing")
def bridge_rental_missing():
    """마지막으로 받아 둔 RMS 사본 기준으로 '렌탈인데 RMS에 없는 자산'만 다시 본다."""
    conn = get_db()
    rows = _rental_missing_rows(conn)
    return jsonify({"syncedAt": _rms_inventory_synced_at(conn),
                    "missingCount": len(rows),
                    "missing": [_missing_payload(r) for r in rows]})


@bp.post("/bridge/assets/rental-missing/ack")
def bridge_rental_missing_ack():
    """RMS가 그 자산들을 자기 쪽에 만들었다고 알려 온다 — 사본에 즉시 반영한다.

    다음 동기화까지 기다리면 매입 화면이 한동안 옛 숫자를 보여 준다.
    ★자산 자체는 건드리지 않는다. division은 이미 rental이고 바뀔 이유가 없다.
    """
    body = request.get_json(silent=True) or {}
    nos = [str(x or "").strip() for x in (body.get("assetNos") or []) if str(x or "").strip()]
    if not nos:
        abort(400, description="자산번호가 없습니다.")
    ts = config.now_iso()
    with tx(write=True) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO rms_inventory"
            "(asset_no, status, renter, serial, model, synced_at) VALUES(?,?,?,?,?,?)",
            [(n, "", "", "", "", ts) for n in nos])
        for n in nos:
            a = conn.execute("SELECT id FROM assets WHERE asset_no=? COLLATE NOCASE",
                             (n,)).fetchone()
            if a is not None:
                asset_event(conn, a["id"], "RMS 자산 생성",
                            {"사유": "렌탈 귀속인데 RMS에 없어 내려보냄"})
        audit.log("rms_asset_created", target=f"{len(nos)}건")
    return jsonify({"ok": True, "count": len(nos), "syncedAt": ts})


# ─────────────────────────────────────────────── 이관 내역 (자산 한 대 = 한 줄)
#
# 대표 요청(2026-08-28): "RMS든 OWS든 서로 왔다갔다하는 자산 목록 정도는 볼 수 있어야
# 되지 않을까?" — 원장은 처음부터 있었는데 보여 주는 화면이 없었다.
#
# ★원장은 OWS 한 곳이다(설계 원칙). RMS 화면도 이 창구를 그대로 읽어 같은 목록을 띄운다 —
#   양쪽에 각자 기록을 두면 언젠가 서로 다른 말을 하게 된다.
#
# 두 종류를 한 줄로 섞어 시간순으로 보여 준다. '자산이 어디로 갔나'를 보려는 것이지
# 커밋 단위 장부를 보려는 게 아니다.
#   ① 사업부 이관   — asset_transfers/items (렌탈↔판매, 되돌린 것 포함)
#   ② RMS 내려받기 — 렌탈 귀속인데 RMS에 없어 실물 레코드만 만든 것(사업부는 그대로)

_HISTORY_STATES = ("committed", "done", "rolled_back")


def _history_rows(conn, direction="", q="", limit=300):
    like = f"%{q.strip()}%" if q and q.strip() else ""
    out = []

    sql = (
        "SELECT i.asset_no, i.result, i.block_note, i.prev_division, "
        "       t.commit_id, t.direction, t.state, t.reason, t.source, "
        "       t.requested_by, t.requested_at, t.committed_by, t.committed_at, "
        "       t.rolled_back_at, t.rms_acked_at, "
        "       a.model, a.serial, a.division "
        "FROM asset_transfer_items i "
        "JOIN asset_transfers t ON t.commit_id = i.commit_id "
        "LEFT JOIN assets a ON a.asset_no = i.asset_no COLLATE NOCASE "
        f"WHERE i.result = 'ok' AND t.state IN ({','.join('?' * len(_HISTORY_STATES))})")
    params = list(_HISTORY_STATES)
    if direction in DIRECTIONS:
        sql += " AND t.direction = ?"
        params.append(direction)
    if like:
        sql += " AND (i.asset_no LIKE ? OR a.model LIKE ? OR t.commit_id LIKE ?)"
        params += [like, like, like]
    for r in conn.execute(sql + " ORDER BY t.id DESC LIMIT ?", params + [limit]):
        rolled = r["state"] == "rolled_back"
        out.append({
            "kind": "transfer",
            "assetNo": r["asset_no"], "model": r["model"] or "", "serial": r["serial"] or "",
            "at": r["rolled_back_at"] or r["committed_at"] or r["requested_at"],
            "direction": r["direction"],
            "commitId": r["commit_id"], "state": r["state"],
            "by": r["committed_by"] or r["requested_by"] or "",
            "source": r["source"], "reason": r["reason"] or "",
            "note": r["block_note"] or "",
            # ★RMS가 아직 안 받아간 커밋은 '반쪽'이다 — 목록에서 바로 보여야 한다.
            "pending": bool(r["state"] == "committed" and not r["rms_acked_at"]),
            "rolledBack": rolled,
            "nowDivision": r["division"] or "",
        })

    # ② RMS 내려받기 — 사업부는 안 바뀌고 RMS에 실물 레코드만 생긴 건
    if direction in ("", "ows->rms"):
        sql2 = ("SELECT e.ts, e.actor, e.detail, a.asset_no, a.model, a.serial, a.division "
                "FROM asset_events e JOIN assets a ON a.id = e.asset_id "
                "WHERE e.action = 'RMS 자산 생성'")
        p2 = []
        if like:
            sql2 += " AND (a.asset_no LIKE ? OR a.model LIKE ?)"
            p2 += [like, like]
        for r in conn.execute(sql2 + " ORDER BY e.id DESC LIMIT ?", p2 + [limit]):
            out.append({
                "kind": "rms_sync",
                "assetNo": r["asset_no"], "model": r["model"] or "", "serial": r["serial"] or "",
                "at": r["ts"], "direction": "ows->rms",
                "commitId": "", "state": "done", "by": r["actor"] or "",
                "source": "rms", "reason": "렌탈 귀속인데 RMS에 없어 내려보냄",
                "note": "", "pending": False, "rolledBack": False,
                "nowDivision": r["division"] or "",
            })

    out.sort(key=lambda x: x["at"] or "", reverse=True)
    return out[:limit]


def _history_payload(conn, direction="", q="", limit=300):
    rows = _history_rows(conn, direction, q, limit)
    counts = {}
    for r in rows:
        counts[r["direction"]] = counts.get(r["direction"], 0) + 1
    return {
        "rows": rows, "count": len(rows), "byDirection": counts,
        # 반쪽 이관(OWS는 적용됐는데 RMS가 안 받아간 것) — 있으면 화면이 경고를 띄운다
        "pending": sum(1 for r in rows if r["pending"]),
        "rolledBack": sum(1 for r in rows if r["rolledBack"]),
    }


def _history_args():
    return ((request.args.get("direction") or "").strip(),
            (request.args.get("q") or "").strip(),
            max(1, min(1000, int(request.args.get("limit") or 300))))


@bp.get("/transfers/history")
def transfers_history():
    require("purchase.view")
    d, q, n = _history_args()
    return jsonify(_history_payload(get_db(), d, q, n))


@bp.get("/bridge/transfers/history")
def bridge_transfers_history():
    """RMS 화면이 같은 목록을 띄우기 위해 읽는 자리 — 원장은 OWS 한 곳뿐이다."""
    d, q, n = _history_args()
    return jsonify(_history_payload(get_db(), d, q, n))
