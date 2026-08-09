"""송장 발급/출력 — QC 완료 후 포장 전 '선출력' (2026-07-28 대표 지시).

- 송장 상품명 = [채널] 제품코드/상품명 · 매칭 자산번호 · 추가옵션
  → 출고팀이 라벨만 보고 구성품·자산 대조 검증 가능
- 운송장 레이아웃은 동결(cj2_waybill_pdf 바이트 복사본) — 상품명은 데이터로만 구성
- CJ 미설정/미무장(armed=False)이면 테스트 발행(가짜 999 송장번호 + [테스트발행] 표기)
  → API 키 없이도 출력 흐름을 검증할 수 있다 (원칙 #3 시뮬레이션 기본)
"""
import io
import json
import re

from flask import Response, abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require, require_any
from ..cj import _cj2_label, _cj_label_offset, cj2_addr_refine, cj2_new_invoice, cj2_reg_book, cj2_waybill_pdf
from ..db import get_db, tx
from ..prep import options_for_order as prep_options_for_order
from ..prep import unchecked_for_order as prep_unchecked_for_order
from . import _get_order_or_404, _order_scope_clause, bp


def _cj_settings(conn):
    row = conn.execute("SELECT value FROM settings WHERE key='cj'").fetchone()
    cfg = {}
    if row:
        try:
            cfg = json.loads(row["value"]) or {}
        except ValueError:
            cfg = {}
    cfg.setdefault("env", "dev")
    cfg.setdefault("armed", False)
    cfg.setdefault("sender", {})
    cfg.setdefault("label", {})
    return cfg


def _is_real(cfg):
    return bool(cfg.get("env") == "prod" and cfg.get("armed")
                and (cfg.get("cust_id") or "").strip() and (cfg.get("biz_reg_num") or "").strip())


def _next_wid(conn):
    """WB-YYYYMMDD-NN. MAX 기준 — 중간 행이 삭제돼도 기존 ID와 충돌하지 않는다."""
    today = config.today_str()
    row = conn.execute(
        "SELECT MAX(CAST(substr(wid, 13) AS INTEGER)) AS m FROM waybills WHERE wid GLOB ?",
        (f"WB-{today}-*",)).fetchone()
    return f"WB-{today}-{(row['m'] or 0) + 1:02d}"


def _next_test_invoice(conn):
    """테스트 송장번호 — 999 접두. 삭제/취소가 있어도 겹치지 않도록 최대값 기준."""
    row = conn.execute(
        "SELECT MAX(CAST(substr(invoice_no, 4) AS INTEGER)) AS m FROM waybills "
        "WHERE invoice_no GLOB '999[0-9]*'").fetchone()
    return "999" + f"{(row['m'] or 0) + 1:09d}"


def _safe_label_text(s):
    """라벨 텍스트에서 'x숫자' 패턴을 제거한다.

    동결 렌더러가 item_summary의 [xX]\\s*(\\d+)를 전부 더해 수량 칸에 인쇄하므로,
    'RTX3060'·'8Gx2' 같은 모델/옵션 표기가 수량으로 오인된다. 렌더러는 수정 금지이므로
    데이터 쪽에서 x를 전각(×)으로 바꿔 오집계를 막는다(사람이 읽기에는 동일).
    """
    return re.sub(r"[xX](?=\s*\d)", "×", str(s or ""))


_LABEL_BUDGET = 118          # label.py가 상품명 칸을 120자에서 자른다. 여유 2자.


def _field(row, key, default=""):
    """sqlite3.Row와 dict 어느 쪽으로 와도 안전하게 값을 꺼낸다."""
    try:
        v = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


def _asset_text(asset_nos, room=999):
    """자산번호 — 여러 대면 전부. 자리가 모자라면 '외 N대'로 넘긴다.

    ★번호를 중간에서 자르면 안 된다('260729-0001,2607'처럼 반쪽 번호가 찍히면
      포장 대조가 오히려 위험해진다). 온전한 번호만 적고 나머지는 개수로 알린다.
    """
    if not asset_nos:
        return ""
    kept = []
    for i, no in enumerate(asset_nos):
        rest = len(asset_nos) - i - 1
        tail = f" 외 {rest}대" if rest else ""
        cand = "자산 " + ",".join(kept + [no]) + tail
        if len(cand) <= room:
            kept.append(no)
        else:
            break
    if not kept:
        return f"자산 {len(asset_nos)}대"
    rest = len(asset_nos) - len(kept)
    return "자산 " + ",".join(kept) + (f" 외 {rest}대" if rest else "")


def customer_parcel_seq(conn, row):
    """같은 고객에게 함께 나가는 주문 중 이 주문이 몇 번째인가 → (순번, 전체).

    한 사람이 6건을 주문하면 상자도 6개다. 송장에 1/6·2/6이 찍혀 있어야
    포장·배송·고객 모두 '다 왔는지' 확인할 수 있다(대표 요청 2026-07-29).

    묶는 기준은 전화번호(숫자만) — 고객 이력·같은 고객 표시와 같은 잣대다.
    ★아직 배송완료되지 않은 건만 센다. 이미 발급한 송장의 번호가 나중에 흔들리지 않도록
      '송장 발급 여부'는 조건에 넣지 않는다(넣으면 한 건 낼 때마다 분모가 줄어든다).
    묶을 게 없으면 (1, 1) — 그 경우 순번을 인쇄하지 않는다.
    """
    phone = re.sub(r"\D", "", row["phone"] or "")
    if len(phone) < 9:
        return 1, 1
    expr = ("REPLACE(REPLACE(REPLACE(REPLACE(phone,'-',''),' ',''),'(',''),')','')")
    rows = conn.execute(
        f"SELECT id FROM orders WHERE {expr} = ? AND cancelled_at='' AND archived_at='' "
        "AND delivered_at='' ORDER BY id", (phone,)).fetchall()
    ids = [r["id"] for r in rows]
    if len(ids) < 2 or row["id"] not in ids:
        return 1, 1
    return ids.index(row["id"]) + 1, len(ids)


def _compose_items(row, asset_nos, simulated, prep_names=(), seq=(1, 1), box_qty=1):
    """송장 상품명 칸 — 셋팅·QC·포장이 같은 종이를 보고 대조한다(대표 지시 2026-07-29).

    담는 것: 쇼핑몰 / 상품명·자체상품코드(둘 다 있으면 둘 다) / 옵션 / 수량 / 자산번호(전부)

    ★상품코드가 있어도 상품명(제목)을 버리지 않는다. 몰마다 어느 쪽이 '진짜 모델'인지
      달라서, 코드만 찍으면 무슨 물건인지 모르는 몰이 생긴다.

    칸이 120자뿐이라 잘릴 때를 대비해 우선순위를 정해 둔다.
      ①쇼핑몰+코드(식별) ②자산번호(포장 대조의 핵심) ③옵션(누락 방지가 이 기능의 목적)
      ④상품명(가장 길고, 코드가 있으면 보조 정보)
    수량은 label.py가 첫 항목에 'x수량'으로 붙이므로 그 자리를 미리 빼 둔다.
    """
    channel = _safe_label_text((row["channel"] or "수기").strip())
    code = _safe_label_text((row["product_code"] or "").strip())
    name = _safe_label_text((row["product_name"] or "").strip())
    qty = row["quantity"] or 1

    head = f"[{channel}] " + (code or name or "상품")
    # 리뷰어 출고는 송장에도 표시한다 — 포장 담당이 일반 주문과 구분해 다뤄야 한다
    if _field(row, "is_review"):
        head = "[리뷰어] " + head
    # 같은 고객에게 여러 건이 나가면 1/6·2/6을 맨 앞에 — 몇 개 중 몇 번째 상자인지 바로 보이게
    if seq and seq[1] > 1:
        head = f"[{seq[0]}/{seq[1]}] " + head
    # ★한 송장으로 여러 상자가 나가는 경우(대표: "2박스 이상 나가는 경우도 있어" 2026-07-30).
    #   CJ 라벨 레이아웃은 동결이라 좌표를 못 늘린다 — 상품명 칸 맨 앞에 실어 보낸다.
    #   기사·포장 담당이 몇 상자인지 종이만 보고 알 수 있어야 분실이 잡힌다.
    try:
        _bq = int(box_qty or 1)
    except (TypeError, ValueError):
        _bq = 1
    if _bq > 1:
        head = f"[박스 {_bq}개] " + head
    if simulated:
        head = "[테스트발행] " + head

    # 옵션 = 우리가 챙기는 제공 옵션 + 몰이 보내준 옵션(중복은 제거)
    opt_bits = []
    for t in list(prep_names) + [(row["option_name"] or "").strip()]:
        t = _safe_label_text((t or "").strip())
        if t and t not in opt_bits:
            opt_bits.append(t)
    opt_text = ("옵션 " + ", ".join(opt_bits)) if opt_bits else ""

    budget = _LABEL_BUDGET - len(f" x{qty}")
    items, used = [], 0

    def add(text, cap, with_qty=False):
        nonlocal used
        if not text:
            return
        sep = 3 if items else 0                       # label.py가 ' / '로 잇는다
        room = budget - used - sep
        if room < 8:                                  # 남은 자리가 너무 적으면 넣지 않는다
            return
        t = text[:min(cap, room)]
        items.append({"name": t, "qty": qty} if with_qty else {"name": t})
        used += len(t) + sep

    # 순서 = 잘릴 때 남길 우선순위.
    # ★자산번호를 상품명보다 먼저 넣는다 — 상품명은 앞부분만 봐도 무슨 물건인지 알지만,
    #   자산번호는 반쪽만 찍히면 대조가 불가능하다(잘림 방지는 _asset_text가 맡는다).
    #   상품명은 남는 자리를 전부 쓴다(대표 지시 2026-07-29: 고도몰 자체상품코드 + 기준몰 상품명).
    add(head, 40, with_qty=True)                       # ① 쇼핑몰 + 자체상품코드
    if asset_nos:                                      # ② 자산번호(포장 대조) — 온전하게
        room = budget - used - (3 if items else 0)
        add(_safe_label_text(_asset_text(asset_nos, room)), room)
    if code and name:                                  # ③ 상품명 — 남는 자리를 다 쓴다
        add(name, 999)                                 #    (코드가 없으면 head가 이미 상품명)
    add(opt_text, 46)                                  # ④ 옵션(배송메세지 칸에도 실린다)
    return items


def _compose_remark(row, prep_names=()):
    """⑰배송메세지 칸(60자) — 고객 요청을 먼저 살린다.

    ★'부재시 경비실'처럼 기사에게 필요한 요청을 우리 문구로 덮으면 배송 사고가 난다
      (626건 중 280건이 이런 요청을 달고 들어온다). 남는 자리에만 옵션을 덧붙인다.
      옵션 전체는 위 상품명 칸에 이미 들어가므로 여기서 잘려도 정보가 사라지지 않는다.
    """
    msg = (row["delivery_message"] or "").strip()
    names = [t.strip() for t in prep_names if (t or "").strip()]
    if not names:
        return msg[:60]
    if not msg:
        return _fit_names(names, 60)
    room = 60 - len(msg) - 3
    if room < 8:
        return msg[:60]          # 고객 요청이 길면 그것만 — 옵션은 상품명 칸에 이미 있다
    return f"{msg} / {_fit_names(names, room)}"


def _fit_names(names, room):
    """자리에 맞게 옵션 이름을 담는다 — 글자 중간에서 끊지 않고 '외 N건'으로 넘긴다.

    '리브레오피스, 램 16G 업'처럼 반쯤 잘린 문구는 작업자가 무엇인지 알 수 없어
    안 쓴 것만 못하다. 온전히 들어가는 항목만 적고 나머지 개수를 알려 준다.
    """
    kept = []
    for name in names:
        left = len(names) - len(kept) - 1
        tail = f" 외 {left}건" if left else ""
        cand = ", ".join(kept + [name]) + tail
        if len(cand) <= room:
            kept.append(name)
        else:
            break
    if not kept:
        return f"옵션 {len(names)}건"[:room]
    left = len(names) - len(kept)
    return ", ".join(kept) + (f" 외 {left}건" if left else "")


def _sender(cfg):
    s = cfg.get("sender") or {}
    if (s.get("name") or "").strip():
        return {"name": s.get("name", ""), "tel": s.get("tel", ""),
                "addr": s.get("addr", ""), "addr_detail": s.get("addr_detail", ""), "zip": s.get("zip", "")}
    return {"name": "하프북", "tel": "", "addr": "(설정 > API 관리에서 출고지를 입력하세요)", "addr_detail": ""}


def _pickup(cfg):
    """회수지(반품 받는 곳). 따로 설정하지 않았으면 출고지를 쓴다."""
    p = cfg.get("pickup") or {}
    if (p.get("name") or "").strip():
        return {"name": p.get("name", ""), "tel": p.get("tel", ""),
                "addr": p.get("addr", ""), "addr_detail": p.get("addr_detail", ""), "zip": p.get("zip", "")}
    return _sender(cfg)


@bp.get("/orders/<int:oid>/waybill-preview")
def waybill_preview(oid):
    """발급 전에 송장에 '무엇이 찍힐지' 보여준다 — CJ를 부르지 않는다.

    셋팅·QC가 검수 완료 후 상품명 칸(쇼핑몰/상품/옵션/수량/자산번호)과
    배송메세지 칸(고객 요청+제공 옵션)을 눈으로 확인하고 발급하게 한다(대표 요청 2026-07-29).
    """
    require_any("orders.ship", "waybills.manage", "orders.work")
    conn = get_db()
    row = _get_order_or_404(conn, oid)
    assets = conn.execute(
        "SELECT a.asset_no FROM order_assets oa JOIN assets a ON a.id=oa.asset_id "
        "WHERE oa.order_id=? ORDER BY a.asset_no", (oid,)).fetchall()
    asset_nos = [a["asset_no"] for a in assets]
    prep_names = [o["name"] for o in prep_options_for_order(conn, row)]
    unchecked = [o["name"] for o in prep_options_for_order(conn, row) if not o["checked"]]
    cfg = _cj_settings(conn)
    parcel_seq = customer_parcel_seq(conn, row)
    try:
        pv_box = max(1, min(int(request.args.get("boxQty") or 1), 10))
    except (TypeError, ValueError):
        pv_box = 1
    items = _compose_items(dict(row), asset_nos, simulated=not _is_real(cfg),
                           prep_names=prep_names, seq=parcel_seq, box_qty=pv_box)
    summary = " / ".join(
        f"{it.get('name') or ''}{' x' + str(it.get('qty')) if it.get('qty') else ''}".strip()
        for it in items if it.get("name"))[:120]
    blockers = []
    if not (row["production_done"] and row["inspection_done"]):
        blockers.append("제작 완료·SW 검수가 끝나야 발급할 수 있습니다.")
    if not asset_nos and not row["is_review"]:
        blockers.append("자산번호를 먼저 매칭하세요(구성품 대조용).")
    if not (row["recipient"] or "").strip() or not (row["address"] or "").strip():
        blockers.append("수취인·주소가 비어 있습니다.")
    if unchecked:
        blockers.append("아직 챙기지 않은 옵션: " + ", ".join(unchecked[:3]))
    dup = conn.execute(
        "SELECT wid, invoice_no, status FROM waybills WHERE order_id=? AND type='forward' "
        "AND status IN ('issued','test','pending') LIMIT 1", (oid,)).fetchone()
    return jsonify({
        "itemSummary": summary,                       # ⑯상품명 칸 (120자)
        "remark": _compose_remark(dict(row), prep_names),   # ⑰배송메세지 칸 (60자)
        "recipient": row["recipient"], "address": row["address"],
        "assetNos": asset_nos, "prepOptions": prep_names,
        "quantity": row["quantity"], "boxQty": pv_box,
        "isReview": bool(row["is_review"]), "reviewNote": row["review_note"],
        "parcelSeq": parcel_seq[0], "parcelTotal": parcel_seq[1],
        "real": _is_real(cfg),                        # False면 테스트 발행(999 송장)
        "blockers": blockers,
        "existing": ({"wid": dup["wid"], "invoiceNo": dup["invoice_no"],
                      "status": dup["status"]} if dup else None),
    })


@bp.post("/orders/<int:oid>/waybill")
def issue_waybill(oid):
    """송장 발급.

    ★CJ 네트워크 호출은 반드시 트랜잭션 '밖'에서 한다 — BEGIN IMMEDIATE를 쥔 채
      외부 API를 기다리면 그동안 전 시스템의 쓰기가 잠긴다(원칙 #1).
      1) 짧은 쓰기 tx: 검증 + 예약행(pending) 선점 → 중복 발급도 여기서 차단
      2) tx 밖: CJ 채번/정제/접수
      3) 짧은 쓰기 tx: 결과 확정(실패 시 예약행 삭제)
    """
    require_any("orders.ship", "waybills.manage")
    body = request.get_json(silent=True) or {}
    box_qty = max(1, min(int(body.get("boxQty") or 1), 10))

    # ---- 1) 검증 + 선점
    with tx(write=True) as conn:
        row = _get_order_or_404(conn, oid)
        if row["cancelled_at"]:
            abort(400, description="취소된 주문입니다.")
        if row["archived_at"]:
            abort(400, description="보관된 주문입니다.")
        # ★대표 결정(2026-08-05): "제작 완료된 상태에서 송장을 뽑으면 자동으로 출고 확인으로 간다."
        #   예전에는 검수까지 끝나야 송장을 뽑을 수 있었는데, 실제 작업은
        #   제작 → 송장 출력 → 포장 순서라 검수 체크가 송장을 막는 걸림돌이었다.
        #   대신 송장이 나가면 그 자리에서 '출고 확인'을 켠다(아래 3단계).
        if not row["production_done"]:
            abort(400, description="제작 완료된 주문만 송장을 출력할 수 있습니다.")
        dup = conn.execute(
            "SELECT wid, invoice_no FROM waybills WHERE order_id=? AND type='forward' "
            "AND status IN ('issued','test','pending') LIMIT 1",
            (oid,)).fetchone()
        if dup:
            abort(409, description=f"이미 발행된 송장이 있습니다({dup['wid']} / {dup['invoice_no'] or '발급 중'}). "
                                   "재발행하려면 기존 송장을 먼저 취소하세요.")
        assets = conn.execute(
            "SELECT a.asset_no, a.tier FROM order_assets oa JOIN assets a ON a.id=oa.asset_id "
            "WHERE oa.order_id=? ORDER BY a.asset_no", (oid,)).fetchall()
        asset_nos = [a["asset_no"] for a in assets]
        # ★리뷰어 출고는 제품 없이 빈 박스만 나가는 경우가 있어 자산 매칭을 요구하지 않는다
        if not asset_nos and not row["is_review"]:
            abort(400, description="자산번호 매칭 후 송장을 출력하세요. (구성품 대조 검증용)")
        # ★가재고 출고 금지(2026-08-04) — 출고 확인만 막으면 송장 발급으로 우회된다.
        #   두 관문 모두에서 같은 규칙을 지켜야 수리 안 끝난 물건이 못 나간다.
        prov = [a["asset_no"] for a in assets if a["tier"] == "가재고"]
        if prov:
            abort(400, description=(
                f"가재고 자산({', '.join(prov)})이 매칭돼 있어 송장을 발급할 수 없습니다. "
                "매입 화면에서 양품 또는 실재고로 바꾼 뒤 발급하세요."))
        if not (row["recipient"] or "").strip() or not (row["address"] or "").strip():
            abort(400, description="수취인과 주소를 먼저 입력하세요. "
                                   "(주문관리 ▸ 해당 주문 ▸ [상세]에서 배송지를 채울 수 있습니다)")
        # ★미리보기는 '챙길 옵션'이 남으면 발급을 잠그는데 여기서 안 막으면 규칙이 어긋난다.
        #   옵션 규칙을 나중에 추가하면 이미 제작 완료된 주문에서 실제로 벌어진다.
        missing_opts = prep_unchecked_for_order(conn, row)
        if missing_opts:
            abort(400, description="아직 챙기지 않은 옵션이 있습니다: "
                                   + ", ".join(missing_opts[:4])
                                   + " — 셋팅 화면에서 체크한 뒤 발급하세요.")

        cfg = _cj_settings(conn)
        real = _is_real(cfg)
        sender = _sender(cfg)
        if real:
            # 전화 필수 가드 — CJ 게이트웨이는 'S'를 줘도 코어에서 조용히 실패(실사고 기반 규칙)
            if not (row["phone"] or "").strip():
                abort(400, description="실발행에는 수취인 전화번호가 필수입니다.")
            if not (sender.get("name") or "").strip() or not (sender.get("addr") or "").strip() \
                    or not (sender.get("tel") or "").strip():
                abort(400, description="실발행 전에 설정 > 배송/CJ대한통운에서 출고지(보내는 분) 정보를 입력하세요.")
        today = config.today_str()
        receiver = {"name": row["recipient"], "tel": row["phone"],
                    "addr": row["address"], "addr_detail": "", "zip": row["postal_code"]}
        cust_use_no = f"HB{oid}-{config.now().strftime('%H%M%S')}"
        wid = _next_wid(conn)
        ts = config.now_iso()
        invoice = "" if real else _next_test_invoice(conn)
        conn.execute(
            "INSERT INTO waybills(wid, order_id, type, cj_kind, invoice_no, status, recipient, phone, "
            "postal_code, address, items, cust_use_no, cj_rcpt_ymd, box_qty, created_by, created_at, updated_at) "
            "VALUES(?,?,'forward','ship',?,'pending',?,?,?,?,'',?,?,?,?,?,?)",
            (wid, oid, invoice, row["recipient"], row["phone"], row["postal_code"], row["address"],
             cust_use_no, today if real else "", box_qty, g.user["display_name"], ts, ts),
        )
        order_snapshot = dict(row)
        # 우리가 챙기는 옵션도 송장에 찍는다 — 포장 담당이 한 번 더 대조한다
        prep_names = [o["name"] for o in prep_options_for_order(conn, row)]
        parcel_seq = customer_parcel_seq(conn, row)   # 같은 고객 1/6·2/6 표기

    # ---- 2) CJ 호출 (트랜잭션 밖 — DB 락을 쥐지 않는다)
    refine = None
    cj_response = None
    items = _compose_items(order_snapshot, asset_nos, simulated=not real, box_qty=box_qty,
                           prep_names=prep_names, seq=parcel_seq)
    remark = _compose_remark(order_snapshot, prep_names)
    try:
        if real:
            invoice = cj2_new_invoice(cfg)
            if not invoice:
                raise RuntimeError("CJ 운송장 번호 채번에 실패했습니다.")
            refine = cj2_addr_refine(cfg, order_snapshot["address"])
            cj_response = cj2_reg_book(
                cfg, kind="ship", sender=sender, receiver=receiver, items=items,
                cust_use_no=cust_use_no, rcpt_ymd=today, invc_no=invoice, box_qty=box_qty,
                remark=remark,
            )
            if not cj_response.get("ok"):
                raise RuntimeError(f"CJ 접수 실패: {cj_response.get('detail') or cj_response.get('result_cd')}")
    except Exception as e:
        with tx(write=True) as conn:  # 선점 해제 — 재시도 가능하게
            conn.execute("DELETE FROM waybills WHERE wid=? AND status='pending'", (wid,))
            audit.log("waybill_failed", target=f"{wid} #{oid}", detail={"error": str(e)})
        abort(502, description=str(e))

    # ---- 3) 결과 확정
    label = _cj2_label(invoice, today, sender, receiver, items, refine,
                       remark=remark, default_item="하프북 상품")
    with tx(write=True) as conn:
        ts = config.now_iso()
        conn.execute(
            "UPDATE waybills SET invoice_no=?, status=?, items=?, label=?, cj_response=?, updated_at=? WHERE wid=?",
            (invoice, "issued" if real else "test", label["item_summary"],
             json.dumps(label, ensure_ascii=False),
             json.dumps(cj_response, ensure_ascii=False) if cj_response else None, ts, wid),
        )
        # ★송장이 나가면 '출고 확인'을 자동으로 켠다(대표 2026-08-05).
        #   손으로 한 번 더 체크하게 하면 빠뜨리고, 그러면 [금일 출고 확인]에서 누락된다.
        #   이미 켜져 있으면 담당자를 덮어쓰지 않는다 — 먼저 확인한 사람이 담당자다.
        conn.execute(
            "UPDATE orders SET courier=?, tracking_no=?, inspection_done=1, "
            "  inspection_by=CASE WHEN inspection_by='' THEN ? ELSE inspection_by END, "
            "  inspection_at=CASE WHEN inspection_at='' THEN ? ELSE inspection_at END, "
            "  updated_at=? WHERE id=?",
            ("CJ대한통운", invoice, g.user["display_name"], ts, ts, oid))
        audit.log("waybill_issued", target=f"{wid} #{oid} {order_snapshot['recipient']}",
                  detail={"invoiceNo": invoice, "simulated": not real, "assets": asset_nos,
                          "boxQty": box_qty, "autoInspection": True})
    return jsonify({"wid": wid, "invoiceNo": invoice, "simulated": not real}), 201


@bp.get("/waybills")
def list_waybills():
    require_any("orders.ship", "waybills.manage", "shipping.view", "as.manage")
    # 담당 분류 밖 고객의 이름·전화·주소가 송장 목록으로 새면 안 된다
    scope, params = _order_scope_clause()
    sql = ("SELECT w.*, o.channel AS order_channel, o.product_name AS order_product "
           "FROM waybills w LEFT JOIN orders o ON o.id = w.order_id "
           "WHERE (w.order_id IS NULL OR EXISTS (SELECT 1 FROM orders o2 WHERE o2.id = w.order_id"
           + scope.replace("o.", "o2.") + "))")
    if request.args.get("type"):
        sql += " AND w.type = ?"
        params.append(request.args["type"])
    if request.args.get("status"):
        sql += " AND w.status = ?"
        params.append(request.args["status"])
    q = (request.args.get("q") or "").strip()
    if q:
        sql += " AND (w.invoice_no LIKE ? OR w.recipient LIKE ? OR w.items LIKE ?)"
        params.extend(["%" + q + "%"] * 3)
    sql += " ORDER BY w.created_at DESC LIMIT 300"
    rows = get_db().execute(sql, params).fetchall()
    return jsonify([
        {"wid": r["wid"], "orderId": r["order_id"], "invoiceNo": r["invoice_no"], "status": r["status"],
         "type": r["type"], "recipient": r["recipient"], "phone": r["phone"], "address": r["address"],
         "items": r["items"], "boxQty": r["box_qty"], "channel": r["order_channel"],
         "stageName": r["cj_stage_nm"], "scheduledDate": r["scheduled_date"],
         "createdBy": r["created_by"], "createdAt": r["created_at"]}
        for r in rows
    ])


def _waybill_in_scope(conn, wid):
    """담당 분류 밖 주문의 송장은 없는 것으로 취급한다."""
    scope, params = _order_scope_clause()
    return conn.execute(
        "SELECT w.* FROM waybills w WHERE w.wid=? AND (w.order_id IS NULL OR EXISTS "
        "(SELECT 1 FROM orders o WHERE o.id = w.order_id" + scope + "))",
        [wid] + params).fetchone()


@bp.get("/waybills/<wid>/pdf")
def waybill_pdf(wid):
    require_any("orders.ship", "waybills.manage", "shipping.view", "as.manage")
    conn = get_db()
    row = _waybill_in_scope(conn, wid)
    if row is None or not row["label"]:
        abort(404, description="송장을 찾을 수 없습니다.")
    label = json.loads(row["label"])
    cfg = _cj_settings(conn)
    offsets = _cj_label_offset(cfg.get("label") or {})
    pdf = cj2_waybill_pdf(label, True, *offsets)
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={wid}.pdf"})


@bp.post("/as-tickets/<int:tid>/return-waybill")
def issue_as_return_waybill(tid):
    """A/S 반송 송장 — 수리를 마친 고객 물건을 돌려보낸다(회수의 반대 방향).

    구조는 주문 송장 발급과 같다: 짧은 쓰기 tx로 선점 → 트랜잭션 밖에서 CJ 호출 → 확정.
    """
    require("as.manage")
    body = request.get_json(silent=True) or {}
    box_qty = max(1, min(int(body.get("boxQty") or 1), 10))

    with tx(write=True) as conn:
        t = conn.execute("SELECT * FROM as_tickets WHERE id=?", (tid,)).fetchone()
        if t is None:
            abort(404, description="A/S 건을 찾을 수 없습니다.")
        if t["status"] in ("returned", "cancelled"):
            abort(400, description="이미 마무리된 A/S 건입니다.")
        if not (t["customer"] or "").strip() or not (t["address"] or "").strip():
            abort(400, description="고객명과 주소를 먼저 입력하세요.")
        dup = conn.execute(
            "SELECT wid, invoice_no FROM waybills WHERE as_ticket_id=? AND type='forward' "
            "AND status IN ('issued','test','pending') LIMIT 1", (tid,)).fetchone()
        if dup:
            abort(409, description=f"이미 반송 송장이 있습니다({dup['wid']} / {dup['invoice_no'] or '발급 중'}).")

        cfg = _cj_settings(conn)
        real = _is_real(cfg)
        sender = _sender(cfg)
        if real:
            if not (t["phone"] or "").strip():
                abort(400, description="실발행에는 고객 전화번호가 필수입니다.")
            if not (sender.get("name") or "").strip() or not (sender.get("addr") or "").strip() \
                    or not (sender.get("tel") or "").strip():
                abort(400, description="실발행 전에 설정 > API 관리에서 출고지 정보를 입력하세요.")
        asset_no = ""
        if t["asset_id"]:
            a = conn.execute("SELECT asset_no FROM assets WHERE id=?", (t["asset_id"],)).fetchone()
            asset_no = a["asset_no"] if a else ""
        today = config.today_str()
        receiver = {"name": t["customer"], "tel": t["phone"], "addr": t["address"],
                    "addr_detail": "", "zip": ""}
        cust_use_no = f"HBR{tid}-{config.now().strftime('%H%M%S')}"
        wid = _next_wid(conn)
        ts = config.now_iso()
        invoice = "" if real else _next_test_invoice(conn)
        conn.execute(
            "INSERT INTO waybills(wid, order_id, as_ticket_id, type, cj_kind, invoice_no, status, "
            "recipient, phone, postal_code, address, items, cust_use_no, cj_rcpt_ymd, box_qty, "
            "created_by, created_at, updated_at) "
            "VALUES(?,NULL,?,'forward','ship',?,'pending',?,?,'',?,'',?,?,?,?,?,?)",
            (wid, tid, invoice, t["customer"], t["phone"], t["address"],
             cust_use_no, today if real else "", box_qty, g.user["display_name"], ts, ts))
        ticket = dict(t)

    items = [{"name": _safe_label_text(
        f"[A/S반송] {ticket['ticket_no']}"
        + (f" {asset_no}" if asset_no else "")
        + (f" · {ticket['result']}" if ticket["result"] else ""))[:60], "qty": 1}]
    refine = None
    cj_response = None
    try:
        if real:
            invoice = cj2_new_invoice(cfg)
            if not invoice:
                raise RuntimeError("CJ 운송장 번호 채번에 실패했습니다.")
            refine = cj2_addr_refine(cfg, ticket["address"])
            cj_response = cj2_reg_book(
                cfg, kind="ship", sender=sender, receiver=receiver, items=items,
                cust_use_no=cust_use_no, rcpt_ymd=today, invc_no=invoice, box_qty=box_qty,
                remark=(ticket["symptom"] or "A/S 반송")[:60])
            if not cj_response.get("ok"):
                raise RuntimeError(f"CJ 접수 실패: {cj_response.get('detail') or cj_response.get('result_cd')}")
    except Exception as e:
        with tx(write=True) as conn:
            conn.execute("DELETE FROM waybills WHERE wid=? AND status='pending'", (wid,))
            audit.log("as_return_failed", target=f"{wid} {ticket['ticket_no']}", detail={"error": str(e)})
        abort(502, description=str(e))

    label = _cj2_label(invoice, today, sender, receiver, items, refine,
                       remark=(ticket["symptom"] or "")[:60], default_item="A/S 반송품")
    with tx(write=True) as conn:
        ts = config.now_iso()
        conn.execute(
            "UPDATE waybills SET invoice_no=?, status=?, items=?, label=?, cj_response=?, updated_at=? "
            "WHERE wid=?",
            (invoice, "issued" if real else "test", label["item_summary"],
             json.dumps(label, ensure_ascii=False),
             json.dumps(cj_response, ensure_ascii=False) if cj_response else None, ts, wid))
        # 반송 송장이 나갔으면 수리는 끝난 것으로 본다(자산은 '반송 완료' 시 재고에서 빠진다)
        conn.execute("UPDATE as_tickets SET status='done', updated_at=? WHERE id=? AND status!='done'",
                     (ts, tid))
        conn.execute(
            "INSERT INTO as_events(ticket_id, ts, action, actor, detail) VALUES(?,?,?,?,?)",
            (tid, ts, "반송송장", g.user["display_name"],
             json.dumps({"wid": wid, "invoiceNo": invoice}, ensure_ascii=False)))
        audit.log("as_return_issued", target=f"{wid} {ticket['ticket_no']}",
                  detail={"invoiceNo": invoice, "simulated": not real})
    # 발송 안내 문자(송장번호 포함) — 트랜잭션 밖에서
    from flask import current_app

    from ..notify import notify_async
    ok, msg = notify_async(current_app, tid, "returned")
    return jsonify({"wid": wid, "invoiceNo": invoice, "simulated": not real,
                    "sms": {"ok": ok, "message": msg}}), 201


@bp.post("/waybills/print")
def waybills_print():
    """여러 송장을 한 PDF로 — 프린터 대화상자를 건마다 띄우지 않게 한다.

    ★라벨 렌더러(cj2_waybill_pdf)는 손대지 않는다. 그것이 만든 PDF를 합치기만 한다.
    """
    require_any("orders.ship", "waybills.manage", "shipping.view", "as.manage")
    from pypdf import PdfReader, PdfWriter

    body = request.get_json(silent=True) or {}
    wids = [str(w).strip() for w in (body.get("wids") or []) if str(w).strip()]
    if not wids:
        abort(400, description="인쇄할 송장을 선택하세요.")
    if len(wids) > 100:
        abort(400, description="한 번에 100건까지 인쇄할 수 있습니다.")

    conn = get_db()
    cfg = _cj_settings(conn)
    offsets = _cj_label_offset(cfg.get("label") or {})
    rows = {}
    for w in wids:                       # 담당 분류 검사를 건별로 그대로 태운다
        r = _waybill_in_scope(conn, w)
        if r is not None:
            rows[w] = r
    missing = [w for w in wids if w not in rows or not rows[w]["label"]]
    if missing:
        abort(404, description=f"라벨이 없는 송장이 있습니다: {', '.join(missing[:5])}")

    writer = PdfWriter()
    for wid in wids:                       # 화면에서 고른 순서를 그대로 지킨다
        pdf = cj2_waybill_pdf(json.loads(rows[wid]["label"]), True, *offsets)
        for page in PdfReader(io.BytesIO(pdf)).pages:
            writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    audit.log("waybills_printed", target=f"{len(wids)}건", detail={"wids": wids[:50]})
    return Response(buf.getvalue(), mimetype="application/pdf", headers={
        "Content-Disposition": f"inline; filename=hms-waybills-{config.today_str()}.pdf"})


@bp.post("/waybills/<wid>/cancel")
def cancel_waybill(wid):
    """송장 취소 — CJ 호출은 트랜잭션 밖에서(발급과 동일 원칙)."""
    require_any("orders.ship", "waybills.manage")   # 권한 설명대로 취소할 수 있어야 한다
    with tx() as conn:
        row = conn.execute("SELECT * FROM waybills WHERE wid=?", (wid,)).fetchone()
        if row is None:
            abort(404, description="송장을 찾을 수 없습니다.")
        if row["status"] == "canceled":
            abort(400, description="이미 취소된 송장입니다.")
        cfg = _cj_settings(conn)
        snapshot = dict(row)

    if snapshot["status"] == "issued":
        if not _is_real(cfg):
            abort(400, description="실발행 송장 취소는 CJ 설정(운영+무장)이 필요합니다.")
        label = json.loads(snapshot["label"]) if snapshot["label"] else {}
        # ★취소 PK에는 접수구분이 들어간다 — 회수(02)를 출고(01)로 취소하면 CJ가 건을 못 찾아
        #   실제 예약이 살아있는 채로 기사가 수거하러 온다(2026-07-28 리뷰 확인).
        kind = "return" if snapshot["type"] == "recall" else "ship"
        res = cj2_reg_book(
            cfg, kind=kind, sender=label.get("sender") or {}, receiver=label.get("receiver") or {},
            items=[], cust_use_no=snapshot["cust_use_no"], rcpt_ymd=snapshot["cj_rcpt_ymd"],
            invc_no=snapshot["invoice_no"], cancel=True,
        )
        if not res.get("ok"):
            abort(502, description=f"CJ 예약 취소 실패: {res.get('detail') or res.get('result_cd')}")

    with tx(write=True) as conn:
        ts = config.now_iso()
        conn.execute("UPDATE waybills SET status='canceled', updated_at=? WHERE wid=?", (ts, wid))
        restored = []
        if snapshot["type"] == "recall":
            # 회수 취소 — 자산이 '회수중'에 고착되지 않게 직전 상태로 되돌린다
            from .recall import _restore_from_recall
            w = conn.execute("SELECT * FROM waybills WHERE wid=?", (wid,)).fetchone()
            restored = _restore_from_recall(conn, w)
            if snapshot["as_ticket_id"]:
                conn.execute("UPDATE as_tickets SET status='received', updated_at=? "
                             "WHERE id=? AND status='collecting'", (ts, snapshot["as_ticket_id"]))
        elif snapshot["order_id"]:
            o = conn.execute("SELECT tracking_no FROM orders WHERE id=?", (snapshot["order_id"],)).fetchone()
            if o and o["tracking_no"] == snapshot["invoice_no"]:
                conn.execute("UPDATE orders SET tracking_no='', updated_at=? WHERE id=?",
                             (ts, snapshot["order_id"]))
        audit.log("waybill_canceled", target=wid,
                  detail={"invoiceNo": snapshot["invoice_no"], "type": snapshot["type"],
                          "restoredAssets": restored})
    return jsonify({"ok": True, "restoredAssets": restored})
