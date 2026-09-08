"""OWS → TMS 되돌려 쓰기 큐(2026-09-08 대표 확정 "앞으로는 tms와 ows 그대로 연동해서 쓸거야").

지금까지는 한 방향이었다 — TMS에서 읽어 OWS를 채우기만 했다. 그래서 OWS에서 넣은 A/S 수리비가
TMS 재고내역에는 영영 안 보였다(대표가 260424-0020 으로 물은 그 건).

흐름
    OWS에서 값이 바뀐다 → 여기 큐(tms_outbox)에 '반영해야 할 것' 한 줄
    → 연동 틱(tms_link.sync_once)이 창구로 밀어 넣는다(POST /api/v1/tms-write)
    → 창구(data-bridge/tms_writer.py)가 TMS를 고친다(화이트리스트·키ID·로우버전 잠금·무장 스위치)
    → 결과를 받아 큐를 닫는다. 실패·충돌은 큐에 남아 다음 틱에 다시 간다.

★큐를 두는 이유: 창구가 죽어 있거나 무장 전이어도 **바뀐 사실이 사라지지 않는다**. 켜는 순간
  밀린 것부터 순서대로 나간다. 화면에서 무엇이 밀려 있는지 볼 수 있어야 사람이 믿을 수 있다.

★되돌아오는 고리(이중 계상) 차단 — 이 파일의 존재 이유 절반이다:
  OWS가 TMS 수리비를 고치면 TMS 트리거가 HIS변경요약에 남기고, 사본이 그걸 반영하고,
  OWS가 다시 "TMS에 수리비가 생겼네" 하며 자기 원가에 또 더한다(자산 원가 2배).
  그래서 **OWS가 밀어 넣은 금액(pushed)을 기억**해 두고, TMS 값에서 그만큼 뺀 것만
  'TMS가 원래 갖고 있던 몫'으로 본다(migration.sync_tms_costs 가 이 값을 쓴다).
"""
from __future__ import annotations

import json

from .. import audit, config


class TmsWriteConflict(RuntimeError):
    """그 사이 TMS 쪽이 먼저 바뀌었다 — 덮어쓰지 않는다."""


class TmsWriteRefused(RuntimeError):
    """창구가 규칙상 거부했다(무장 전·화이트리스트 밖) — 재시도해도 같다."""


# 큐에 담기는 종류 — 창구 tms_writer.WRITABLE 의 kind 와 같은 이름이어야 한다
KIND_ASSET_COST = "asset_cost"

OPEN_STATUSES = ("queued", "failed", "conflict")
MAX_ATTEMPTS = 8            # 이만큼 실패하면 자동 재시도를 멈춘다(사람이 화면에서 다시 건다)


def ows_repair_total(conn, asset_id):
    """이 자산의 **사람이 넣은** 수리비 합계 — TMS로 밀어 넣을 값.

    ★출처 'TMS연동' 행은 뺀다. 그건 TMS에서 받아 온 값이라 도로 밀어 넣으면 제자리를 맴돈다.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(cost),0) AS c FROM asset_repairs "
        "WHERE asset_id=? AND created_by <> 'TMS연동'", (asset_id,)).fetchone()
    return int(row["c"] or 0)


def _tms_key(conn, asset_id):
    """이 자산의 TMS 키ID·관리번호 — TMS에 없는(OWS에서 만든) 자산이면 (None, 번호)."""
    a = conn.execute("SELECT asset_no, tms_shadow FROM assets WHERE id=?", (asset_id,)).fetchone()
    if a is None:
        return None, ""
    no = (a["asset_no"] or "").strip()
    try:
        shadow = json.loads(a["tms_shadow"] or "{}") or {}
    except (ValueError, TypeError):
        shadow = {}
    key = shadow.get("키ID") or shadow.get("keyId")
    try:
        key = int(key) if key not in (None, "") else None
    except (TypeError, ValueError):
        key = None
    return key, no


def queue_asset_cost(conn, asset_id, *, reason=""):
    """이 자산의 수리비를 TMS에 반영해야 하면 큐에 넣는다. 넣었으면 큐 id, 아니면 None.

    ★같은 자산의 아직 안 나간 줄이 있으면 그 줄을 최신 값으로 고친다(줄이 쌓이면 마지막만
      의미가 있는데 앞의 것들이 순서대로 나가며 중간값을 TMS에 잠깐씩 보여 준다).
    """
    key_id, asset_no = _tms_key(conn, asset_id)
    if not key_id:
        return None          # TMS에 없는 자산(OWS 채번분) — 되돌려 쓸 곳이 없다
    a = conn.execute("SELECT tms_lock FROM assets WHERE id=?", (asset_id,)).fetchone()
    if a is not None and (a["tms_lock"] if "tms_lock" in a.keys() else 0):
        return None          # 번호 충돌로 잠근 자산은 건드리지 않는다(2026-08-24 tms_lock)
    amount = ows_repair_total(conn, asset_id)
    pushed = pushed_amount(conn, asset_id)
    ts = config.now_iso()
    # 아직 안 나간 줄이 있으면 그 줄을 최신 값으로 고친다 — 줄이 쌓이면 중간값들이 순서대로
    # TMS에 잠깐씩 보이고, 마지막 것만 의미가 있다.
    dup = conn.execute(
        "SELECT id FROM tms_outbox WHERE kind=? AND asset_id=? AND status IN "
        f"({','.join('?' * len(OPEN_STATUSES))}) ORDER BY id DESC LIMIT 1",
        (KIND_ASSET_COST, asset_id, *OPEN_STATUSES)).fetchone()
    if amount == pushed:
        # TMS에 이미 그 값이 있다. ★안 나간 줄이 남아 있으면 지운다 —
        #   수리비를 넣었다 지운 경우가 여기다(75,000 보내려던 줄이 살아 있으면 그게 나간다).
        if dup:
            conn.execute("UPDATE tms_outbox SET status='cancelled', last_error='되돌려져 보낼 값이 없어짐', "
                         "reason=?, updated_at=? WHERE id=?", (reason, ts, dup["id"]))
        return None
    payload = json.dumps({"수리비": amount}, ensure_ascii=False)
    if dup:
        conn.execute("UPDATE tms_outbox SET payload=?, status='queued', attempts=0, last_error='', "
                     "reason=?, updated_at=? WHERE id=?", (payload, reason, ts, dup["id"]))
        return dup["id"]
    cur = conn.execute(
        "INSERT INTO tms_outbox(kind, asset_id, asset_no, tms_key_id, payload, reason, status, "
        "attempts, created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,'queued',0,?,?,?)",
        (KIND_ASSET_COST, asset_id, asset_no, key_id, payload, reason,
         _actor(), ts, ts))
    return cur.lastrowid


def _actor():
    from flask import g
    try:
        u = g.get("user")
    except RuntimeError:
        return "OWS"
    if u is None:
        return "OWS"
    try:
        return u["display_name"] or "OWS"
    except (KeyError, IndexError, TypeError):
        return "OWS"


def pushed_amount(conn, asset_id):
    """OWS가 TMS에 실제로 밀어 넣어 둔 수리비 — 되돌아오는 고리를 끊는 기준값."""
    row = conn.execute(
        "SELECT payload FROM tms_outbox WHERE kind=? AND asset_id=? AND status='applied' "
        "ORDER BY id DESC LIMIT 1", (KIND_ASSET_COST, asset_id)).fetchone()
    if row is None:
        return 0
    try:
        return int((json.loads(row["payload"]) or {}).get("수리비") or 0)
    except (ValueError, TypeError):
        return 0


def tms_own_repair(conn, asset_id, tms_value):
    """TMS 수리비 중 **TMS가 원래 갖고 있던 몫** = TMS 값 − OWS가 밀어 넣은 몫.

    ★이 뺄셈이 없으면 OWS가 넣은 75,000원이 TMS를 한 바퀴 돌아 'TMS연동' 수리 기록으로
      다시 들어와 자산 원가가 150,000원이 된다. migration.sync_tms_costs 가 이걸 쓴다.
    """
    return max(0, int(tms_value or 0) - pushed_amount(conn, asset_id))


def pending(conn, limit=50):
    """아직 안 나간 줄 — 오래된 것부터."""
    ph = ",".join("?" * len(OPEN_STATUSES))
    return conn.execute(
        f"SELECT o.*, a.model FROM tms_outbox o LEFT JOIN assets a ON a.id=o.asset_id "
        f"WHERE o.status IN ({ph}) AND o.attempts < ? ORDER BY o.id LIMIT ?",
        (*OPEN_STATUSES, MAX_ATTEMPTS, limit)).fetchall()


def mark(conn, row_id, status, *, error="", action_id="", before=None, after=None):
    """한 줄의 결과를 기록한다."""
    ts = config.now_iso()
    conn.execute(
        "UPDATE tms_outbox SET status=?, attempts=attempts+1, last_error=?, action_id=?, "
        "before_json=?, after_json=?, applied_at=CASE WHEN ?='applied' THEN ? ELSE applied_at END, "
        "updated_at=? WHERE id=?",
        (status, str(error or "")[:300], action_id or "",
         json.dumps(before, ensure_ascii=False) if before is not None else "",
         json.dumps(after, ensure_ascii=False) if after is not None else "",
         status, ts, ts, row_id))


def push_once(app, cli, conn, limit=30):
    """큐를 창구로 밀어 넣는다. 연동 틱이 부른다.

    ★창구 호출은 트랜잭션 밖에서 해야 하지만, 여기서는 한 건씩 짧게 쓰고 바로 커밋한다
      (호출자가 건별로 tx 를 잡아 준다 — 아래 tms_link.push_outbox 참고).
    돌려주는 값: {"sent", "applied", "conflict", "failed", "skipped"}
    """
    out = {"sent": 0, "applied": 0, "conflict": 0, "failed": 0, "skipped": 0}
    rows = pending(conn, limit)
    if not rows:
        return out
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}") or {}
        except (ValueError, TypeError):
            mark(conn, r["id"], "failed", error="보낼 값을 읽지 못했습니다")
            out["failed"] += 1
            continue
        values = dict(payload)
        try:
            res = cli.post("/tms-write", {
                "kind": r["kind"], "keyId": r["tms_key_id"], "values": values,
                "actor": r["created_by"] or "OWS",
            })
        except TmsWriteConflict as e:
            mark(conn, r["id"], "conflict", error=str(e))
            out["conflict"] += 1
            continue
        except TmsWriteRefused as e:
            # 무장 전·규칙 위반 — 다시 시도해도 같으니 시도 횟수를 올리지 않는다
            conn.execute("UPDATE tms_outbox SET status='queued', last_error=?, updated_at=? WHERE id=?",
                         (str(e)[:300], config.now_iso(), r["id"]))
            out["skipped"] += 1
            continue
        except Exception as e:                                   # noqa: BLE001
            mark(conn, r["id"], "failed", error=str(e))
            out["failed"] += 1
            continue
        out["sent"] += 1
        if res.get("applied"):
            mark(conn, r["id"], "applied", action_id=res.get("actionId") or "",
                 before=res.get("before"), after=res.get("after"))
            out["applied"] += 1
            audit.log("tms_push_applied", target=f"{r['asset_no']} {r['kind']}",
                      detail={"values": values, "actionId": res.get("actionId"),
                              "before": res.get("before")})
        else:
            # '이미 같은 값' — TMS가 그 값을 이미 갖고 있다. 밀어 넣은 것으로 친다.
            mark(conn, r["id"], "applied", action_id="", before=res.get("before"),
                 after=res.get("after"), error=res.get("reason") or "")
            out["applied"] += 1
    return out



# ---------------------------------------------------------------- 화면용 API
STATUS_LABEL = {
    "queued": "보낼 차례", "applied": "반영됨", "conflict": "TMS가 먼저 바뀜",
    "failed": "실패", "cancelled": "취소(보낼 값 없어짐)",
}


def _item(r):
    k = r.keys()
    def _j(col):
        try:
            return json.loads(r[col]) if (col in k and r[col]) else None
        except (ValueError, TypeError):
            return None
    return {
        "id": r["id"], "kind": r["kind"], "assetId": r["asset_id"], "assetNo": r["asset_no"],
        "model": (r["model"] if "model" in k else "") or "",
        "tmsKeyId": r["tms_key_id"], "values": _j("payload") or {},
        "reason": r["reason"], "status": r["status"],
        "statusLabel": STATUS_LABEL.get(r["status"], r["status"]),
        "attempts": r["attempts"], "lastError": r["last_error"], "actionId": r["action_id"],
        "before": _j("before_json"), "after": _j("after_json"),
        "appliedAt": r["applied_at"], "createdBy": r["created_by"],
        "createdAt": r["created_at"], "updatedAt": r["updated_at"],
    }


def counts(conn):
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM tms_outbox GROUP BY status").fetchall()
    out = {k: 0 for k in STATUS_LABEL}
    for r in rows:
        out[r["status"]] = r["n"]
    out["open"] = sum(out.get(s, 0) for s in OPEN_STATUSES)
    # 자동 재시도를 멈춘 것 — 사람이 봐야 움직인다
    out["stuck"] = conn.execute(
        f"SELECT COUNT(*) AS n FROM tms_outbox WHERE status IN "
        f"({','.join('?' * len(OPEN_STATUSES))}) AND attempts >= ?",
        (*OPEN_STATUSES, MAX_ATTEMPTS)).fetchone()["n"]
    return out
