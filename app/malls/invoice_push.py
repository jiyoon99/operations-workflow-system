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
import threading
import time
from datetime import timedelta

from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import get_db, tx
from ..settings import bp
from .base import MallError, get_adapter
from .collect import _mall_settings

# 몰이 요구하는 택배사 코드(CJ). 몰마다 코드 체계가 달라 설정에서 받는다.
COURIER_KEY = "cj_courier_code"

# ★보내는 시점(2026-09-08 대표 "송장 뽑았는데 고도몰에 왜 안 들어가 있지").
#   처음엔 '출고 확인' 때만 보냈다(09-04 "간선상차 전 출고 확인 금지"에 맞춰). 그런데 대표가 몰 관리자에서
#   손으로 송장을 넣던 시점은 언제나 '뽑은 직후'였다 — 몰이 배송중으로 바뀌고 고객에게 안내가 가는 것까지
#   포함해서 그게 원래 하던 일이다. 그래서 기본은 발급 즉시, 몰별 스위치로 출고 확인 때로 돌릴 수 있다.
PUSH_ON_ISSUE_KEY = "push_on_issue"

# 자동 스윕 — 발급 순간 못 보낸 건(재시작 직후·몰 장애·나중에 켠 몰)을 10분마다 다시 보낸다.
SWEEP_INTERVAL_SEC = 10 * 60
SWEEP_FIRST_DELAY_SEC = 60            # 서버가 뜨자마자 몰을 두드리지 않는다
FRESH_HOURS = 48                      # 이보다 오래된 발급분은 사람이 대기 목록에서 판단한다
MAX_TRIES = 5                         # 몰이 계속 거부하는 건을 영원히 두드리지 않는 상한(자동 스윕만)


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


def _mall_sno(order_row):
    """수집 원본(raw JSON)에 보관된 몰 주문상품 번호 — 고도몰 sno('|' 연결). 없으면 빈 문자열."""
    raw = order_row.get("raw") if isinstance(order_row, dict) else order_row["raw"]
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    return str((data or {}).get("mallSno") or "").strip() if isinstance(data, dict) else ""


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
    # ★몰 주문상품 번호(고도몰 sno) — 수집 원본에서 꺼내고, 없으면(옛 수집분) 몰에 다시 묻는다.
    #   2026-09-08 첫 실전송에서 이것 없이 보낸 고도몰 5건이 전부 거부됐다.
    sno = _mall_sno(order_row)
    if not sno and hasattr(adapter, "fetch_sno"):
        try:
            sno = adapter.fetch_sno(order_no, order_row.get("ordered_at") or "")
        except MallError as e:
            return False, f"{adapter.name} 주문상품 번호 조회 실패: {e}"
        except Exception as e:                                    # noqa: BLE001
            return False, f"{adapter.name} 주문상품 번호 조회 중 오류: {e}"
    extra = {"sno": sno} if sno else {}
    try:
        adapter.upload_invoice(order_no, invoice, courier_code=(s.get(COURIER_KEY) or "").strip(), **extra)
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


def push_on_issue_enabled(s):
    """이 몰이 '발급 즉시' 보내는지 — 칸이 없으면(옛 저장분) 켜진 것으로 본다."""
    return (s or {}).get(PUSH_ON_ISSUE_KEY, True) is not False


def _issue_target(conn, oid):
    """발급 즉시(또는 스윕이) 보낼 대상인지 — (보낼지, 안 보내는 이유).

    여기서 걸러진 건은 오류를 남기지 않는다(꺼진 몰에서 발급할 때마다 '꺼져 있습니다'가
    대기 목록에 쌓이면 진짜 실패가 묻힌다). 출고 확인 경로는 예전처럼 사유를 남긴다.
    """
    row = conn.execute("SELECT channel, tracking_no, mall_sent_at, cancelled_at FROM orders WHERE id=?",
                       (oid,)).fetchone()
    if row is None:
        return False, "주문 없음"
    invoice = (row["tracking_no"] or "").strip()
    if not invoice or invoice.startswith("999"):
        return False, "테스트 발행"
    if row["mall_sent_at"]:
        return False, "이미 전송"
    if row["cancelled_at"]:
        return False, "취소 주문"
    _code, adapter, s = _adapter_by_channel(conn, row["channel"])
    if adapter is None:
        return False, s
    if not s.get("push_invoice"):
        return False, "자동 전송 꺼짐"
    if not push_on_issue_enabled(s):
        return False, "출고 확인 때 전송"
    return True, ""


def push_after_issue(app, oid):
    """송장 발급 직후 — 몰 설정이 '발급 즉시'면 보낸다. 해당 없으면 None(출고 확인 때 보낸다)."""
    with tx() as conn:
        go, _why = _issue_target(conn, oid)
    if not go:
        return None
    return push_for_order(app, oid)


def fresh_unsent(conn, *, hours=FRESH_HOURS, limit=30):
    """최근 hours 시간 안에 실발행됐는데 아직 몰에 안 올라간 주문 — 현재 송장과 맺어진 발행본 기준."""
    since = (config.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT o.id, o.tracking_no FROM orders o "
        "JOIN waybills w ON w.order_id=o.id AND w.invoice_no=o.tracking_no "
        "WHERE o.cancelled_at='' AND o.tracking_no!='' AND o.mall_sent_at='' "
        "AND w.type='forward' AND w.status='issued' AND w.created_at>=? "
        "ORDER BY w.created_at LIMIT ?", (since, limit)).fetchall()


_tries = {}                  # (주문id, 송장번호) → 자동 스윕 실패 횟수. 메모리라 재시작하면 0부터(상한이지 장부가 아니다)
_tries_lock = threading.Lock()


def reset_sweep_tries():
    with _tries_lock:
        _tries.clear()


def sweep_pending(app, *, limit=30):
    """놓친 최근 발급분을 보낸다 — 발급 즉시 전송이 켜진 몰만.

    왜 있나: (1) 이 코드가 들어오기 전에 뽑은 송장(재시작 직후), (2) 발급 순간 몰 API가 잠깐 죽었던 건,
    (3) 자동 전송을 나중에 켠 몰. 손으로 '몰 전송 대기'에서 다시 보내지 않아도 된다.
    ★48시간 지난 건은 건드리지 않는다 — 고도몰은 송장을 받으면 주문을 '배송중'으로 되돌리므로
      이미 배송완료된 옛 주문에 보내면 상태가 뒤로 간다. 옛 건은 사람이 대기 목록에서 판단한다.
    ★몰 호출은 DB 잠금 밖에서(원칙 #1). 대상 고르기와 보내기를 분리한다.
    """
    out = {"sent": 0, "failed": 0, "skipped": 0}
    with app.app_context():
        with tx() as conn:
            targets = []
            for r in fresh_unsent(conn, limit=limit):
                go, _why = _issue_target(conn, r["id"])
                if go:
                    targets.append((r["id"], r["tracking_no"]))
                else:
                    out["skipped"] += 1
        for oid, invoice in targets:
            key = (oid, invoice)
            with _tries_lock:
                if _tries.get(key, 0) >= MAX_TRIES:
                    out["skipped"] += 1
                    continue
            try:
                ok, _msg = push_for_order(app, oid)
            except Exception:                                    # noqa: BLE001
                ok = False
                app.logger.exception("송장 몰 전송(자동 스윕) 실패 | order=%s", oid)
            with _tries_lock:
                if ok:
                    _tries.pop(key, None)
                else:
                    _tries[key] = _tries.get(key, 0) + 1
            out["sent" if ok else "failed"] += 1
    return out


def start_push_sweeper(app):
    """10분 주기 자동 스윕 데몬. 테스트·검증 서버에서는 app/__init__ 이 띄우지 않는다."""
    def loop():
        time.sleep(SWEEP_FIRST_DELAY_SEC)
        while True:
            try:
                res = sweep_pending(app)
                if res["sent"] or res["failed"]:
                    app.logger.info("송장 몰 전송 스윕 | %s", res)
            except Exception:                                    # noqa: BLE001
                app.logger.exception("송장 몰 전송 스윕 실패")
            time.sleep(SWEEP_INTERVAL_SEC)

    t = threading.Thread(target=loop, name="ows-mall-push", daemon=True)
    t.start()
    return t


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
