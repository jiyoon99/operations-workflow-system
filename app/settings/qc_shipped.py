"""QC 프로그램에서 이미 출고된 고객을 OWS 셋팅/QC 보드에서 내린다.

대표 지시(2026-08-05):
  "QC 시스템은 [금일 출고 확인 엑셀]을 누르면 작업목록에서 다 빠지고 출고처리가 된다.
   그러다 보니 출고가 끝난 제품 이력이 없고 OWS 셋팅/QC 라인에 다 남아 있다.
   \\\\127.0.0.1\\order-data 를 확인해 출고된 고객들을 OWS에 그대로 반영해 달라."

★왜 자동으로 안 이어졌나 (2026-08-05 실측)
  같은 주문이 **두 개의 키**로 존재한다.
    · QC 프로그램 : 엑셀 수집분 → importKey "주문수집:4CDDFF703D" / 주문번호 "수집-…"
    · OWS         : 몰 API 수집분 → 그 몰의 주문번호
  qc_watch는 import_key가 같은 것만 잇는다. 키가 다르니 영영 못 만나고,
  QC에서는 출고가 끝났는데 OWS 보드에는 계속 남는다(잔여 166건 중 46건이 이 경우).

★매칭 규칙 — 사람이 눈으로 확인할 수 있게 근거를 함께 남긴다
  수령인은 반드시 같아야 하고, 그 위에 상품명·상품코드·금액·주문일로 점수를 매긴다.
  후보가 **정확히 하나**이고 점수가 기준 이상일 때만 '확정'으로 올린다.
  둘 이상이면 '애매'로 따로 빼고 절대 자동 반영하지 않는다 —
  엉뚱한 주문을 출고완료로 만들면 나가지 않은 물건이 나간 것으로 잡힌다.

★안전 규칙
  - **전진만** 반영한다. 이미 출고·취소된 주문은 건드리지 않는다.
  - 자동으로 돌지 않는다. 대표가 화면에서 [대조] → [반영]을 눌러야 한다.
  - 금액은 손대지 않는다. QC 파일은 금액이 비어 있는 경우가 많다(엑셀 수집분).
"""
import glob
import json
import os
import re
import sqlite3
from datetime import date, datetime

from flask import abort, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import tx
from . import bp
from .qc_import import _s
from .qc_watch import _cfg, _orders_file, qc_live

# 근거 점수 — 이 값 이상이어야 '확정 후보'로 올린다.
#   상품명 일치(2) + 주문일 근접(1) = 3 이 가장 흔한 조합이다.
MIN_SCORE = 3
NEAR_DAYS = 14

# ★한쪽이 다른 쪽으로 시작하면 같은 상품으로 본다(2026-08-05 실측).
#   같은 주문인데 표기가 다르다 —
#     OWS 코드 'NT371B5M_i7-7_내장'  ⊂  QC 코드 'NT371B5M_i7-7_내장 AA급2'
#     OWS 이름 'NT371B5M_i7-7_내장 AA급2' ⊂ QC 이름 'NT371B5M_i7-7_내장 AA급2 / 단일색상 …'
#   짧은 글자가 우연히 겹치는 것을 막으려고 최소 길이를 둔다.
PREFIX_MIN = 8

# ★금액이 조금 다른 것은 옵션 할인·추가금이다. 실측 23쌍 중 21쌍이 2만~4만원(전부 8% 이내)이었고,
#   48%·96% 차이 나는 2쌍만 실제로 다른 주문이었다. 이 선 안쪽은 감점하지 않는다.
AMOUNT_NEAR_WON = 50000
AMOUNT_NEAR_RATE = 0.10


def _norm(v):
    """비교용 정규화 — 공백·괄호는 몰마다 달라서 무시한다."""
    return re.sub(r"[\s\[\]()]+", "", str(v or "")).lower()


def _same_text(a, b):
    """같은 상품/코드인가 — "eq"(완전) / "prefix"(한쪽이 다른 쪽의 앞부분) / ""(아님)."""
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return ""
    if x == y:
        return "eq"
    if min(len(x), len(y)) >= PREFIX_MIN and (x.startswith(y) or y.startswith(x)):
        return "prefix"
    return ""


def _date(v):
    t = str(v or "")[:10].replace(".", "-")
    try:
        return date.fromisoformat(t)
    except ValueError:
        return None


def _load_nas(base):
    """현재 orders.json + 백업본을 모두 읽어 '한 번이라도 출고된 주문'을 모은다.

    ★백업까지 보는 이유: QC는 출고 확인을 누르면 목록에서 빼 버린다.
      현재 파일만 보면 지난 출고 이력이 통째로 사라진다(대표가 말한 "이력이 없다").
    """
    seen = {}
    files = []
    cur = _orders_file(base)
    if cur:
        files.append(cur)
    files += sorted(glob.glob(os.path.join(base, "backups", "orders.json.*.bak")))
    files += sorted(glob.glob(os.path.join(base, "data", "backups", "orders.json.*.bak")))
    read = 0
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                rows = json.load(fh)
        except Exception:                                        # noqa: BLE001
            continue                      # 깨진 백업 하나가 전체를 막으면 안 된다
        if not isinstance(rows, list):
            continue
        read += 1
        for r in rows:
            k = _s(r.get("importKey")) or f"qc-{_s(r.get('id'))}"
            if not k:
                continue
            # 같은 주문이 여러 백업에 있다 — '출고된 판'을 우선으로 남긴다
            if k not in seen or (r.get("shippingDone") and not seen[k].get("shippingDone")):
                seen[k] = r
    return [r for r in seen.values() if r.get("shippingDone")], read, len(files)


def _score(row, n):
    """OWS 주문 row 와 QC 주문 n 이 같은 건일 근거를 점수와 말로 돌려준다."""
    pts, why = 0, []
    for col, key, label in (("product_name", "productName", "상품명"),
                            ("product_code", "productCode", "제품코드")):
        kind = _same_text(row[col], n.get(key))
        if kind == "eq":
            pts += 2
            why.append(label)
        elif kind == "prefix":
            pts += 2
            why.append(label + "(앞부분)")
    a1, a2 = int(row["amount"] or 0), int(n.get("amount") or 0)
    if a1 and a2:
        gap = abs(a1 - a2)
        if gap == 0:
            pts += 2
            why.append("금액")
        elif gap <= AMOUNT_NEAR_WON and gap <= max(a1, a2) * AMOUNT_NEAR_RATE:
            why.append(f"금액 {gap:,}원 차")     # 옵션 할인 — 깎지도 더하지도 않는다
        else:
            pts -= 3          # 금액이 크게 다르면 다른 주문일 가능성이 높다
            why.append("금액 다름")
    d1, d2 = _date(row["ordered_at"]), _date(n.get("orderedAt"))
    if d1 and d2 and abs((d1 - d2).days) <= NEAR_DAYS:
        pts += 1
        why.append("주문일")
    return pts, why


def _existing_qc_rows(conn):
    """QC 주문번호 → 이미 OWS에 들어와 있는 주문 행.

    ★QC 이관(설정 ▸ 데이터 이관)으로 들어온 주문은 import_key가
      '주문수집:고도몰:수집-A117193B5F' 처럼 QC 주문번호를 끝에 달고 있다.
      같은 주문이 몰 API로도 한 번 더 들어와 있으면 **OWS에 같은 주문이 두 줄**이 된다.
    """
    out = {}
    for r in conn.execute(
            "SELECT id, import_key, order_no, recipient, amount, shipping_done, archived_at "
            "FROM orders WHERE import_key LIKE '주문수집%'"):
        qno = r["import_key"].rsplit(":", 1)[-1].strip()
        if qno:
            out.setdefault(qno, []).append(r)
    return out


def _plan_shipped(conn, base):
    """QC에서 출고됐는데 OWS 보드에 남아 있는 주문을 찾는다."""
    shipped, read, total = _load_nas(base)
    rows = conn.execute(
        "SELECT id, import_key, channel, order_no, recipient, amount, product_code, "
        "       product_name, ordered_at, quantity "
        "FROM orders WHERE cancelled_at='' AND shipping_done=0 AND archived_at=''"
    ).fetchall()
    existing = _existing_qc_rows(conn)

    by_name = {}
    for n in shipped:
        by_name.setdefault(_norm(n.get("recipient")), []).append(n)

    def _item(row):
        return {
            "id": row["id"], "channel": row["channel"], "orderNumber": row["order_no"],
            "recipient": row["recipient"], "amount": row["amount"],
            "productName": row["product_name"], "productCode": row["product_code"],
            "orderedAt": row["ordered_at"], "quantity": row["quantity"],
        }

    def _dup_note(qno, oid):
        """★그 QC 주문이 이미 OWS에 주문 행으로 들어와 있으면 (상대 행, 안내문)을 준다.

        같은 주문이 두 줄(QC 이관분 + 몰 API 수집분)인 것이다. 한쪽은 대개 이미 출고완료다.
        여기서 두 번째 줄까지 출고완료로 찍으면 매출·출고 건수가 그대로 두 배가 된다
        (2026-08-05 실측: 1차 반영 83건 중 67건이 이 경우, 매출 12,980,300원 이중계상.
         출고시각이 짝과 완전히 같아 같은 출고를 두 번 기록한 것이 확실했다).
        이건 '출고 처리'가 아니라 '중복행 정리' 문제라 사람이 판단해야 한다.
        """
        twin = [x for x in existing.get(qno, []) if x["id"] != oid]
        if not twin:
            return None, ""
        t = twin[0]
        return t, (f"같은 QC 주문({qno})이 우리 주문 #{t['id']}으로 이미 들어와 있습니다"
                   + (" — 그 건은 이미 출고완료입니다. 출고 처리가 아니라 중복 정리 대상입니다."
                      if t["shipping_done"] else " — 어느 쪽이 진짜인지 확인이 필요합니다."))

    # 1단계 — 주문마다 후보를 점수로 줄 세운다
    cand_of = {}
    unsure, dups = [], []
    for row in rows:
        scored = []
        for n in by_name.get(_norm(row["recipient"]), []):
            pts, why = _score(row, n)
            if pts >= MIN_SCORE:
                scored.append((pts, why, n))
        if not scored:
            continue
        scored.sort(key=lambda x: -x[0])
        # 1등이 2등보다 확실히 앞설 때만 확정 후보로 본다
        if len(scored) == 1 or scored[0][0] > scored[1][0]:
            # ★중복행 검사가 1:1 경쟁보다 **먼저**다. 상대 줄이 아직 미출고라 보드에 남아 있으면
            #   둘이 같은 QC 건을 놓고 다투게 되는데, 그건 '경쟁'이 아니라 '같은 주문 두 줄'이다.
            qno = _s(scored[0][2].get("orderNumber"))
            twin, note = _dup_note(qno, row["id"])
            if twin is not None:
                item = _item(row)
                item.update({"qcOrderNumber": qno, "duplicateOf": twin["id"],
                             "duplicateShipped": bool(twin["shipping_done"]), "note": note})
                dups.append(item)
                continue
            cand_of[row["id"]] = (row, scored[0])
        else:
            item = _item(row)
            item["candidates"] = len(scored)
            item["note"] = "근거가 같은 후보가 둘 이상입니다"
            unsure.append(item)

    # 2단계 — ★QC 주문 하나는 OWS 주문 하나에만 붙인다(1:1).
    #   이게 없으면 QC에 1건인데 OWS에 2건인 고객에서 **안 나간 물건까지 출고완료**가 된다
    #   (2026-08-05 실측: 이명규·안호열 2명 4건이 그렇게 처리됐다).
    #   같은 QC 주문을 놓고 다투면 점수가 높은 쪽만 가져가고, 나머지는 '애매'로 남긴다.
    #   점수까지 같으면 어느 쪽인지 알 수 없으므로 **둘 다** 애매로 뺀다.
    by_qc = {}
    for oid, (row, best) in cand_of.items():
        by_qc.setdefault(_s(best[2].get("orderNumber")) or id(best[2]), []).append((oid, row, best))
    hits = []
    for qno, group in by_qc.items():
        if len(group) > 1:
            top = max(g[2][0] for g in group)
            winners = [g for g in group if g[2][0] == top]
            losers = [g for g in group if g[2][0] != top]
            if len(winners) > 1:            # 동점 — 어느 쪽인지 알 수 없다
                losers = group
                winners = []
            for oid, row, best in losers:
                item = _item(row)
                item["candidates"] = 1
                item["note"] = (f"QC 주문 {qno} 하나에 우리 주문 {len(group)}건이 몰립니다 — "
                                "QC에 안 올라간 주문일 수 있어 자동 반영하지 않습니다")
                unsure.append(item)
            group = winners
        for oid, row, best in group:
            pts, why, n = best
            item = _item(row)
            qno = _s(n.get("orderNumber"))
            item.update({
                "score": pts, "matchedBy": " · ".join(why),
                "qcOrderNumber": qno,
                "qcQuantity": n.get("quantity"),
                "qcAssetNo": _s(n.get("managementNumber")),
                "shippedBy": _s(n.get("shippingBy")),
                "shippedAt": _s(n.get("shippingAt")),
                "qcArchivedAt": _s(n.get("archivedAt")),
            })
            twin, note = _dup_note(qno, oid)      # 두 겹 방어 — 1단계에서 이미 걸렀어야 한다
            if twin is not None:
                item.update({"duplicateOf": twin["id"],
                             "duplicateShipped": bool(twin["shipping_done"]), "note": note})
                dups.append(item)
                continue
            hits.append(item)
    for lst in (hits, unsure, dups):
        lst.sort(key=lambda x: x["id"])
    return {"hits": hits, "unsure": unsure, "duplicates": dups,
            "shippedCount": len(shipped),
            "filesRead": read, "filesFound": total, "boardCount": len(rows)}


def _also_shipped(conn, base):
    """자동 반영 기준엔 못 미치지만 **QC에 출고 기록이 있는** 보드 주문.

    ★왜 필요한가(대표 2026-08-05: "출고 완료인 제품인데 왜 제작대기에 있는지 알 수 있나?")
      OWS와 QC는 금액을 다르게 적는다 —
        · OWS = **주문 단위 합계**(여러 상품·여러 대를 한 줄로)
        · QC  = **상품/대수 단위 개별 금액**
      예: 조용원 OWS 660,000원(2대) ↔ QC 340,000원(1대 단가, 관리번호 2개)
          김병엽 OWS 259,000원(키보드+노트북) ↔ QC 10,000원(키보드만)
      그래서 금액이 크게 달라 자동 반영에서 빠진다. 그건 안전상 맞지만,
      **작업자에게는 알려 줘야** 이미 나간 물건을 또 만들지 않는다.
    """
    shipped, _r, _t = _load_nas(base)
    rows = conn.execute(
        "SELECT id, recipient, product_name, product_code, amount, ordered_at "
        "FROM orders WHERE cancelled_at='' AND shipping_done=0 AND archived_at=''").fetchall()
    by_name = {}
    for n in shipped:
        if not n.get("shippingDone"):
            continue          # 두 겹 방어 — 아직 안 나간 건에 '출고됨'을 붙이면 안 된다
        by_name.setdefault(_norm(n.get("recipient")), []).append(n)
    out = {}
    for row in rows:
        for n in by_name.get(_norm(row["recipient"]), []):
            # 이름이 같고 상품명이나 코드가 이어지면 '같은 고객의 같은 물건'으로 본다.
            if not (_same_text(row["product_name"], n.get("productName"))
                    or _same_text(row["product_code"], n.get("productCode"))
                    or _same_text(row["product_code"], n.get("productName"))):
                continue
            out[row["id"]] = {
                "id": row["id"], "recipient": row["recipient"], "amount": row["amount"],
                "qcOrderNumber": _s(n.get("orderNumber")),
                "qcAmount": int(n.get("amount") or 0),
                "qcAssetNo": _s(n.get("managementNumber")).replace("\n", ", "),
                "shippedBy": _s(n.get("shippingBy")),
                "shippedAt": _s(n.get("shippingAt")),
            }
            break
    return list(out.values())


@bp.get("/qc-shipped/flags")
def qc_shipped_flags():
    """셋팅 보드 행에 붙일 표시 — 'QC에서는 이미 출고됨'.

    ★2026-08-07 대표 지시로 연동을 끈 뒤로는 늘 빈 값이다. 셋팅/QC를 OWS에서 직접
      쓰기 시작했으므로 남의 폴더를 들여다볼 이유가 없다. 라우트를 지우지 않은 것은
      화면이 이걸 부르다 404를 만나 깨지지 않게 하려는 것뿐이다.
    """
    require("setup.view")
    if not qc_live():
        return jsonify({"flags": {}, "count": 0, "live": False})
    with tx() as conn:
        cfg = _cfg(conn)
        try:
            items = _also_shipped(conn, cfg["path"])
        except Exception:                                        # noqa: BLE001
            items = []            # 폴더가 끊겼을 뿐 — 작업은 계속돼야 한다
    return jsonify({"flags": {str(x["id"]): x for x in items}, "count": len(items),
                    "live": True})


@bp.get("/qc-shipped/preview")
def qc_shipped_preview():
    """무엇이 반영될지 먼저 보여 준다 — 누르기 전에 눈으로 확인한다."""
    require("settings.manage")
    with tx() as conn:
        cfg = _cfg(conn)
        plan = _plan_shipped(conn, cfg["path"])
    return jsonify({
        "folder": cfg["path"],
        "shippedInQc": plan["shippedCount"],
        "onBoard": plan["boardCount"],
        "filesRead": plan["filesRead"], "filesFound": plan["filesFound"],
        "matched": plan["hits"], "unsure": plan["unsure"],
        # 같은 주문이 우리 쪽에 두 줄인 것 — 출고 처리가 아니라 정리 대상이다
        "duplicates": plan["duplicates"],
    })


@bp.post("/qc-shipped/apply")
def qc_shipped_apply():
    """확인한 건을 출고완료로 올리고 보드에서 내린다.

    body.ids 를 주면 그것만, 없으면 확정 후보 전부.
    """
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    want = body.get("ids")
    want = {int(x) for x in want} if isinstance(want, list) else None
    ts = config.now_iso()

    with tx(write=True) as conn:
        cfg = _cfg(conn)
        plan = _plan_shipped(conn, cfg["path"])
        applied, skipped = 0, 0
        for h in plan["hits"]:
            if want is not None and h["id"] not in want:
                skipped += 1
                continue
            # ★전진만 — 다시 읽어 확인한다. 미리보기와 반영 사이에 누가 처리했을 수 있다.
            cur = conn.execute(
                "SELECT shipping_done, cancelled_at FROM orders WHERE id=?", (h["id"],)).fetchone()
            if cur is None or cur["shipping_done"] or cur["cancelled_at"]:
                skipped += 1
                continue
            # ★두 겹 방어 — 계획에서 걸렀더라도 여기서 한 번 더 본다.
            #   ids 를 손으로 넣어 부르는 경로가 있어 계획을 우회할 수 있다.
            if conn.execute("SELECT 1 FROM orders WHERE import_key LIKE ? AND id<>? LIMIT 1",
                            ("%" + h["qcOrderNumber"], h["id"])).fetchone():
                skipped += 1
                continue
            conn.execute(
                "UPDATE orders SET preparing=1, production_done=1, inspection_done=1, "
                "  shipping_done=1, "
                "  preparing_by=CASE WHEN preparing_by='' THEN ? ELSE preparing_by END, "
                "  production_by=CASE WHEN production_by='' THEN ? ELSE production_by END, "
                "  inspection_by=CASE WHEN inspection_by='' THEN ? ELSE inspection_by END, "
                "  shipping_by=?, shipping_at=?, archived_at=?, updated_at=? WHERE id=?",
                (h["shippedBy"], h["shippedBy"], h["shippedBy"], h["shippedBy"],
                 h["shippedAt"] or ts, h["qcArchivedAt"] or h["shippedAt"] or ts, ts, h["id"]))
            audit.log("qc_shipped_link", target=f"주문 #{h['id']} {h['recipient']}",
                      detail={"qcOrderNumber": h["qcOrderNumber"], "matchedBy": h["matchedBy"],
                              "score": h["score"], "shippedBy": h["shippedBy"],
                              "shippedAt": h["shippedAt"]})
            applied += 1
        if applied:
            audit.log("qc_shipped_apply", target=f"{applied}건 출고 반영",
                      detail={"applied": applied, "skipped": skipped})
    return jsonify({"ok": True, "applied": applied, "skipped": skipped,
                    "remaining": len(plan["hits"]) - applied - skipped})


def archive_duplicates(conn, plan, actor="", want=None):
    """중복행을 보드에서 내린다 — **출고완료로는 찍지 않는다**. (내린 건수, 건너뛴 건수)

    대표 지시(2026-08-05): "중복으로 매출은 1건만 반영하여 처리하되, 목록에서 없애 달라."
    ★매출을 1건만 두는 방법은 '두 번째 줄을 출고완료로 찍지 않는 것'이다.
      물건은 이미 다른 줄(QC 이관분)로 출고완료·매출 반영이 끝나 있다.
      여기서 또 찍으면 매출과 출고 건수가 그대로 두 배가 된다
      (2026-08-05 실측: 1차 반영 83건 중 67건이 그렇게 돼 12,980,300원이 이중계상).
      그래서 보관(archived_at)만 채워 목록에서 내리고, 왜 내렸는지를 남긴다.
    ★상대 줄이 아직 출고 전이면 손대지 않는다 — 어느 쪽이 진짜인지 모르는데 내리면
      진짜 주문이 조용히 사라진다.
    """
    ts = config.now_iso()
    done = skipped = 0
    for m in plan["duplicates"]:
        if want is not None and m["id"] not in want:
            skipped += 1
            continue
        if not m.get("duplicateShipped"):
            skipped += 1
            continue
        cur = conn.execute(
            "SELECT cancelled_at, archived_at FROM orders WHERE id=?", (m["id"],)).fetchone()
        if cur is None or cur["cancelled_at"] or cur["archived_at"]:
            skipped += 1
            continue
        conn.execute(
            "UPDATE orders SET archived_at=?, archive_reason=?, duplicate_of=?, updated_at=? "
            "WHERE id=?",
            (ts, f"중복 — 주문 #{m['duplicateOf']}과 같은 건(QC {m['qcOrderNumber']}). "
                 "그쪽이 이미 출고완료라 매출은 그 한 건만 남기고 목록에서 내렸습니다."
                 + (f" [{actor}]" if actor else ""),
             m["duplicateOf"], ts, m["id"]))
        audit.log("qc_dup_archived", target=f"주문 #{m['id']} {m['recipient']}",
                  detail={"duplicateOf": m["duplicateOf"], "qcOrderNumber": m["qcOrderNumber"],
                          "amount": m["amount"], "by": actor or "화면",
                          "note": "출고완료로 찍지 않고 보관만 함(매출 이중계상 방지)"})
        done += 1
    return done, skipped


@bp.post("/qc-shipped/dismiss-duplicates")
def qc_shipped_dismiss():
    """같은 주문이 두 줄인 것을 보드에서 내린다 — **출고완료로는 찍지 않는다**.

    대표 지시(2026-08-05): "출고완료인 건 모두 빼줘."
    ★그런데 중복행을 출고완료로 찍으면 안 된다.
      물건은 이미 다른 줄(QC 이관분)로 출고완료 처리돼 있다. 여기서 또 찍으면
      **매출과 출고 건수가 그대로 두 배**가 된다(1차 반영 83건 중 67건이 그렇게 됐고
      매출 12,980,300원이 이중계상됐다).
      그래서 보관(archived_at)만 채워 보드에서 내리고, 왜 내렸는지를 함께 남긴다.
      매출·재고·실적 어디에도 새 숫자를 만들지 않는다.
    """
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    want = body.get("ids")
    want = {int(x) for x in want} if isinstance(want, list) else None

    with tx(write=True) as conn:
        cfg = _cfg(conn)
        plan = _plan_shipped(conn, cfg["path"])
        done, skipped = archive_duplicates(conn, plan, want=want)
        if done:
            audit.log("qc_dup_archive_batch", target=f"{done}건 목록에서 내림",
                      detail={"archived": done, "skipped": skipped})
    return jsonify({"ok": True, "archived": done, "skipped": skipped})


@bp.post("/qc-shipped/restore")
def qc_shipped_restore():
    """중복으로 내린 것을 되돌린다 — 잘못 내렸을 때 쓴다."""
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        abort(400, description="되돌릴 주문을 고르세요.")
    ts = config.now_iso()
    with tx(write=True) as conn:
        n = 0
        for oid in [int(x) for x in ids]:
            r = conn.execute(
                "SELECT archive_reason FROM orders WHERE id=?", (oid,)).fetchone()
            if r is None or not (r["archive_reason"] or "").startswith("중복"):
                continue          # 이 도구가 내린 것만 되돌린다
            conn.execute(
                "UPDATE orders SET archived_at='', archive_reason='', duplicate_of=NULL, "
                "updated_at=? WHERE id=?", (ts, oid))
            audit.log("qc_dup_restored", target=f"주문 #{oid}")
            n += 1
    return jsonify({"ok": True, "restored": n})


# ★'되돌리기'는 만들었다가 **없앴다**(2026-08-05).
#   되돌릴 대상인 몰 수집분에 정작 금액이 있어서(67쌍 중 65쌍), 되돌리면
#   매출 3,181만 원이 통째로 사라진다. 답은 되돌리기가 아니라 아래 '합치기'다.
#   남길 줄(금액)과 내릴 줄을 정하고, 자산을 남길 줄로 옮긴다.

# ─────────────────────── 같은 주문 두 줄 합치기(매출 1건) ───────────────────────
# ★대표 지시(2026-08-05): "중복으로 매출은 1건만 반영하여 처리하되, 목록에서 없애 달라."
#
# ★왜 '되돌리기'가 답이 아니었나 (실측)
#   한 주문의 정보가 두 줄로 **쪼개져** 있다.
#     · 몰 수집분  : 금액이 정확하다(몰 API). 67쌍 중 65쌍이 여기에만 금액이 있다.
#     · QC 이관분  : 자산번호(관리번호)가 붙어 있다. 금액은 대개 0(엑셀 수집분).
#   그래서 몰 수집분을 되돌리면 매출 3,181만 원이 통째로 사라지고,
#   그대로 두면 양쪽이 다 출고완료라 매출이 두 번 잡힌다.
#   → **금액이 있는 쪽을 남기고, 자산을 그 줄로 옮긴 뒤, 다른 줄을 내린다.**
#
# ★매출 집계는 `shipping_done=1`이 기준이다(app/reports). 그래서 내리는 줄은
#   보관만으로는 부족하고 **출고완료도 풀어야** 매출이 하나가 된다.


def _dup_pairs(conn):
    """같은 QC 주문번호를 가진 두 줄을 짝지어 준다.

    QC 이관분은 import_key가 '주문수집:고도몰:수집-XXXX' 형태다.
    짝은 그 QC 주문번호로 이번 대조가 이어 준 몰 수집분이다(duplicate_of 또는 감사 이력).
    """
    qc = {}
    for r in conn.execute(
            "SELECT id, import_key, recipient, amount, quantity, shipping_done, shipping_at, "
            "       archived_at, cancelled_at FROM orders WHERE import_key LIKE '주문수집%'"):
        qno = r["import_key"].rsplit(":", 1)[-1].strip()
        if qno:
            qc.setdefault(qno, []).append(r)

    # 몰 수집분 ← 감사 이력(대조가 이어 준 것) + duplicate_of(이미 내린 것)
    linked = {}
    for r in conn.execute(
            "SELECT target, detail FROM audit_log "
            "WHERE action IN ('qc_shipped_link','qc_dup_archived') ORDER BY id"):
        try:
            oid = int(str(r["target"]).split("#", 1)[1].split()[0])
            qno = _s(json.loads(r["detail"]).get("qcOrderNumber"))
        except (IndexError, ValueError, KeyError):
            continue
        if qno:
            linked[oid] = qno

    pairs = []
    for oid, qno in linked.items():
        mall = conn.execute(
            "SELECT id, import_key, recipient, amount, quantity, shipping_done, shipping_at, "
            "       archived_at, cancelled_at FROM orders WHERE id=?", (oid,)).fetchone()
        if mall is None or mall["cancelled_at"]:
            continue
        for twin in qc.get(qno, []):
            if twin["id"] == oid or twin["cancelled_at"]:
                continue
            pairs.append((mall, twin, qno))
    return pairs


def _merge_plan(conn):
    """양쪽 다 출고완료라 매출이 두 번 잡힌 쌍을 찾고, 어느 줄을 남길지 정한다."""
    items = []
    seen = set()
    pairs = _dup_pairs(conn)
    # ★한 QC 주문에 우리 주문이 둘 이상 붙은 것은 자동으로 손대지 않는다.
    #   QC에 1건인데 우리 쪽에 2건이면 한 건은 아직 안 나간 것일 수 있다
    #   (2026-08-05 실측: 이명규 #699·#702, 안호열 #755·#757 — 결제만 되고 자산·송장이 없다).
    #   합치면 안 나간 주문이 조용히 사라진다. 대표가 확인할 때까지 그대로 둔다.
    crowded = set()
    per_qc = {}
    for mall, twin, qno in pairs:
        per_qc.setdefault(qno, set()).add(mall["id"])
    for qno, ids in per_qc.items():
        if len(ids) > 1:
            crowded.add(qno)
    for mall, twin, qno in pairs:
        if qno in crowded:
            continue
        key = tuple(sorted((mall["id"], twin["id"])))
        if key in seen:
            continue
        seen.add(key)
        if not (mall["shipping_done"] and twin["shipping_done"]):
            continue                    # 이미 한쪽만 출고완료 = 매출은 이미 한 건
        # 남길 줄 — 금액이 있는 쪽. 둘 다 있으면 큰 쪽, 그래도 같으면 몰 수집분.
        a, b = mall, twin
        if (b["amount"] or 0) > (a["amount"] or 0):
            a, b = b, a
        keep, drop = a, b
        assets = conn.execute(
            "SELECT asset_id FROM order_assets WHERE order_id=?", (drop["id"],)).fetchall()
        have = {r["asset_id"] for r in conn.execute(
            "SELECT asset_id FROM order_assets WHERE order_id=?", (keep["id"],))}
        move = [r["asset_id"] for r in assets if r["asset_id"] not in have]
        items.append({
            "qcOrderNumber": qno,
            "keepId": keep["id"], "keepAmount": keep["amount"],
            "dropId": drop["id"], "dropAmount": drop["amount"],
            "recipient": keep["recipient"] or drop["recipient"],
            "moveAssets": move,
            # 이 줄을 내리면 매출에서 빠지는 금액
            "removedAmount": drop["amount"] or 0,
        })
    items.sort(key=lambda x: x["keepId"])
    return items


@bp.get("/qc-shipped/merge/preview")
def qc_merge_preview():
    """양쪽 다 출고완료인 중복 쌍 — 어느 줄을 남기고 얼마가 빠지는지."""
    require("settings.manage")
    with tx() as conn:
        items = _merge_plan(conn)
    return jsonify({
        "items": items, "count": len(items),
        "removedAmount": sum(x["removedAmount"] for x in items),
        "keptAmount": sum(x["keepAmount"] or 0 for x in items),
        "movingAssets": sum(len(x["moveAssets"]) for x in items),
    })


def merge_duplicates(conn, actor="", skip=None):
    """중복 쌍을 합친다 — 매출은 한 건만 남기고 다른 줄은 목록에서 내린다.

    자동(QC 폴더 감시)과 수동(화면 버튼)이 같은 코드를 쓴다 —
    규칙이 갈리면 한쪽에만 구멍이 생긴다.
    """
    skip = skip or set()
    ts = config.now_iso()
    done = skipped = moved = 0
    removed = 0
    if True:
        for it in _merge_plan(conn):
            if it["keepId"] in skip or it["dropId"] in skip:
                skipped += 1
                continue
            # 자산을 남는 줄로 옮긴다 — 누구에게 어느 기기가 나갔는지가 끊기면 안 된다
            for aid in it["moveAssets"]:
                conn.execute("UPDATE order_assets SET order_id=? WHERE order_id=? AND asset_id=?",
                             (it["keepId"], it["dropId"], aid))
                moved += 1
            conn.execute("DELETE FROM order_assets WHERE order_id=?", (it["dropId"],))
            # ★출고완료도 함께 푼다 — 매출 집계가 shipping_done 기준이라
            #   보관만 하면 매출은 그대로 두 번 잡힌다.
            conn.execute(
                "UPDATE orders SET shipping_done=0, shipping_by='', shipping_at='', "
                "  archived_at=?, archive_reason=?, duplicate_of=?, updated_at=? WHERE id=?",
                (ts, f"중복 — 주문 #{it['keepId']}과 같은 건(QC {it['qcOrderNumber']}). "
                     "매출은 그 한 건만 남기고 이 줄은 목록에서 내렸습니다.",
                 it["keepId"], ts, it["dropId"]))
            audit.log("qc_dup_merged", target=f"주문 #{it['dropId']} → #{it['keepId']} {it['recipient']}",
                      detail={"qcOrderNumber": it["qcOrderNumber"],
                              "keptAmount": it["keepAmount"], "removedAmount": it["removedAmount"],
                              "movedAssets": it["moveAssets"], "by": actor or "화면"})
            removed += it["removedAmount"]
            done += 1
    return {"merged": done, "skipped": skipped, "movedAssets": moved, "removedAmount": removed}


@bp.post("/qc-shipped/merge")
def qc_merge():
    """중복 쌍 합치기 — body.exclude 에 넣은 주문은 그 쌍을 통째로 건너뛴다."""
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    skip = {int(x) for x in (body.get("exclude") or [])}
    with tx(write=True) as conn:
        r = merge_duplicates(conn, skip=skip)
        if r["merged"]:
            audit.log("qc_dup_merge_batch", target=f"{r['merged']}쌍 합침", detail=r)
    return jsonify({"ok": True, **r})
