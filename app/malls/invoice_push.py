"""송장번호를 쇼핑몰에 되쏘기(발송처리).

지금까지는 송장을 뽑고 나서 대표가 쇼핑몰 관리자에 들어가 번호를 일일이 입력해야 했다.
어댑터마다 upload_invoice는 이미 구현돼 있었지만 아무도 부르지 않아 죽은 코드였다.

안전 원칙
- **기본은 꺼짐**. 설정에서 몰별로 켜야 실제로 나간다(CJ 실발행과 같은 방식).
- 몰 호출은 트랜잭션 밖에서. 결과만 짧은 쓰기로 남긴다.
- 실패해도 출고는 그대로 두고 사유를 남긴다 → 재전송 목록에서 다시 보낸다.
- 채널 이름으로 몰 코드를 찾는다(주문에 저장된 값은 어댑터 name이다).
"""
import json

from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import get_db, tx
from ..settings import bp
from .base import MallError, get_adapter
from .collect import _mall_settings

# 몰이 요구하는 택배사 코드(CJ). 몰마다 코드 체계가 달라 설정에서 받는다.
COURIER_KEY = "cj_courier_code"


def _adapter_by_channel(conn, channel):
    """주문 채널 이름 → (몰코드, 어댑터, 설정). 못 찾으면 (None, None, 사유).

    주문에 저장된 channel은 어댑터의 name이므로 그것으로 맞춘다
    (설정 화면의 표시이름 '고도몰5'가 아니라 '고도몰').
    """
    name = (channel or "").strip()
    for code, s in (_mall_settings(conn) or {}).items():
        if not isinstance(s, dict):
            continue
        adapter, _why = get_adapter(code, s)
        if adapter is not None and adapter.name == name:
            return code, adapter, s
    return None, None, f"'{name}' 채널에 연결된 쇼핑몰 설정이 없습니다(설정 > API 관리에서 확인)."


def push_one(conn_settings, order_row):
    """한 건 전송. (성공여부, 메시지) — 몰 호출이라 트랜잭션 밖에서 부른다."""
    code, adapter, s = conn_settings
    if adapter is None:
        return False, s
    if not s.get("push_invoice"):
        return False, f"{adapter.name}은(는) 설정에서 [송장번호 자동 전송]이 꺼져 있습니다."
    invoice = (order_row["tracking_no"] or "").strip()
    if not invoice:
        return False, "송장번호가 없습니다."
    # ★테스트 발행(999…)은 진짜 송장이 아니다. 몰에 올리면 고객에게 엉터리 배송안내가 나간다.
    if invoice.startswith("999"):
        return False, "테스트 발행 송장이라 쇼핑몰에 전송하지 않았습니다(실제 CJ 발행 후 전송됩니다)."
    order_no = (order_row["order_no"] or "").strip()
    if not order_no:
        return False, "몰 주문번호가 없어 전송할 수 없습니다(수기 주문)."
    try:
        adapter.upload_invoice(order_no, invoice, courier_code=(s.get(COURIER_KEY) or "").strip())
    except MallError as e:
        return False, str(e)
    except NotImplementedError:
        return False, f"{adapter.name}은(는) 송장 전송을 아직 지원하지 않습니다."
    except Exception as e:                                        # noqa: BLE001
        return False, f"{adapter.name} 전송 중 오류: {e}"
    return True, f"{adapter.name}에 송장번호를 보냈습니다."


def push_for_order(app, oid):
    """주문 한 건의 송장번호를 몰에 보낸다. 결과를 주문에 기록하고 (ok, msg)를 준다."""
    with tx() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if row is None:
            return False, "주문을 찾을 수 없습니다."
        if row["mall_sent_at"]:
            return True, "이미 전송된 주문입니다."
        target = _adapter_by_channel(conn, row["channel"])
        snapshot = dict(row)

    ok, msg = push_one(target, snapshot)                          # ← 트랜잭션 밖(몰 호출)

    with tx(write=True) as conn:
        if ok:
            conn.execute("UPDATE orders SET mall_sent_at=?, mall_send_error='' WHERE id=?",
                         (config.now_iso(), oid))
        else:
            conn.execute("UPDATE orders SET mall_send_error=? WHERE id=?", (msg[:300], oid))
    return ok, msg


@bp.get("/orders/invoice-push/pending")
def invoice_push_pending():
    """아직 몰에 안 올라간 송장 — 재전송 목록."""
    require("orders.ship")
    rows = get_db().execute(
        "SELECT id, channel, order_no, recipient, tracking_no, mall_send_error, shipping_at "
        "FROM orders WHERE cancelled_at='' AND tracking_no != '' AND mall_sent_at='' "
        "ORDER BY shipping_at DESC LIMIT 200").fetchall()
    return jsonify([
        {"id": r["id"], "channel": r["channel"], "orderNumber": r["order_no"],
         "recipient": r["recipient"], "invoiceNo": r["tracking_no"],
         "error": r["mall_send_error"], "shippedAt": (r["shipping_at"] or "")[:10]}
        for r in rows])


@bp.post("/orders/invoice-push")
def invoice_push():
    """선택한 주문의 송장번호를 몰에 보낸다(건별 성공/실패를 그대로 돌려준다)."""
    require("orders.ship")
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        abort(400, description="보낼 주문을 선택하세요.")
    if len(ids) > 100:
        abort(400, description="한 번에 100건까지 보낼 수 있습니다.")
    from flask import current_app
    done, failed = [], []
    for oid in ids:
        ok, msg = push_for_order(current_app, int(oid))
        (done if ok else failed).append({"id": int(oid), "message": msg})
    with tx(write=True):
        audit.log("invoice_pushed", target=f"{len(done)}건",
                  detail={"ok": len(done), "failed": len(failed),
                          "실패": [f"#{f['id']}: {f['message']}" for f in failed[:30]]})
    return jsonify({"ok": len(done), "done": done, "failed": failed})
