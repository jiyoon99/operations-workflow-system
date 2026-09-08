"""주문 전 자산을 제작·SW검수까지 끝내 두는 선제작 작업판."""
import json
from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require, require_any
from ..db import DIVISION_RENTAL, get_db, tx
from ..purchase import AVAILABLE_STATUSES, asset_event
from . import bp

RENTAL_PREBUILD_SETTING = "prebuild_rental_asset"


def _allow_rental_prebuild(conn):
    row = conn.execute("SELECT value FROM settings WHERE key=?",
                       (RENTAL_PREBUILD_SETTING,)).fetchone()
    if row is None:
        return False
    try:
        value = json.loads(row["value"])
    except (TypeError, ValueError):
        return False
    return bool(value.get("enabled")) if isinstance(value, dict) else bool(value)


def _payload(conn, row):
    a = conn.execute(
        "SELECT id, asset_no, maker, model, product_code, ram, ssd, status, received, division "
        "FROM assets WHERE id=?", (row["asset_id"],)).fetchone()
    final_ram = row["built_ram"] or a["ram"] or ""
    final_ssd = row["built_ssd"] or a["ssd"] or ""
    return {
        "id": row["id"], "assetId": row["asset_id"], "assetNo": a["asset_no"],
        "maker": a["maker"], "model": a["model"], "productCode": a["product_code"],
        "ram": a["ram"], "ssd": a["ssd"], "status": a["status"],
        "mountedRamPartId": None, "mountedRamQty": 1,
        "mountedSsdPartId": None, "mountedSsdQty": 1,
        "mountedRam": "", "mountedSsd": "",
        "finalRam": final_ram, "finalSsd": final_ssd,
        "ramType": row["built_ram_type"],
        "ram1Type": row["built_ram_type"], "ram1": row["built_ram_primary"],
        "ram2Type": row["built_ram2_type"], "ram2": row["built_ram2"],
        "ssdType": row["built_ssd_type"],
        "hdd": row["built_hdd"],
        "productionDone": bool(row["production_done"]),
        "productionBy": row["production_by"], "productionAt": row["production_at"],
        "inspectionDone": bool(row["inspection_done"]),
        "inspectionBy": row["inspection_by"], "inspectionAt": row["inspection_at"],
        "ready": bool(row["ready_done"]), "readyBy": row["ready_by"],
        "readyAt": row["ready_at"], "usedOrderId": row["used_order_id"],
        "usedAt": row["used_at"], "note": row["note"],
        "builtRam": row["built_ram"], "builtSsd": row["built_ssd"],
        "specChangeRequired": bool(row["spec_change_required"]),
        "specChangeReason": row["spec_change_reason"],
        "cancelledAt": row["cancelled_at"], "cancelledBy": row["cancelled_by"],
        "cancelReason": row["cancel_reason"],
    }


def _get(conn, pid):
    row = conn.execute("SELECT * FROM asset_prebuilds WHERE id=?", (pid,)).fetchone()
    if row is None:
        abort(404, description="선제작 기록을 찾을 수 없습니다.")
    return row


@bp.get("/prebuilds")
def list_prebuilds():
    require_any("setup.view", "orders.work")
    q = (request.args.get("q") or "").strip()
    mode = (request.args.get("view") or "active").strip()
    # 취소는 보관하지 않는 정책. 구버전 잔여 행이 있어도 어떤 목록에도 노출하지 않는다.
    where, args = ["p.cancelled_at=''"], []
    if mode == "active":
        where.append("p.used_order_id IS NULL")
    elif mode == "used":
        where.append("p.used_order_id IS NOT NULL")
    if q:
        where.append("(a.asset_no LIKE ? OR a.model LIKE ? OR a.product_code LIKE ?)")
        args.extend([f"%{q}%"] * 3)
    sql = ("SELECT p.* FROM asset_prebuilds p JOIN assets a ON a.id=p.asset_id "
           + ("WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY p.used_order_id IS NOT NULL, p.id DESC LIMIT 500")
    conn = get_db()
    rows = conn.execute(sql, args).fetchall()
    return jsonify({"rows": [_payload(conn, r) for r in rows]})


@bp.post("/prebuilds")
def create_prebuild():
    require("orders.work")
    body = request.get_json(silent=True) or {}
    asset_no = (body.get("assetNo") or "").strip()
    if not asset_no:
        abort(400, description="자산번호를 입력하세요.")
    with tx(write=True) as conn:
        a = conn.execute("SELECT * FROM assets WHERE UPPER(asset_no)=UPPER(?)", (asset_no,)).fetchone()
        if a is None:
            abort(404, description=f"{asset_no}은(는) 등록되지 않은 자산번호입니다.")
        if not a["received"]:
            abort(409, description="입고확인이 안 된 자산은 선제작할 수 없습니다.")
        if a["division"] == DIVISION_RENTAL and not _allow_rental_prebuild(conn):
            abort(409, description=("렌탈 사업부 자산은 선제작할 수 없습니다. "
                                    "설정 ▸ 운영에서 '렌탈 자산 제작·셋팅 허용'을 체크하세요."))
        if a["status"] not in AVAILABLE_STATUSES:
            abort(409, description="판매 가능한 상태의 자산만 선제작할 수 있습니다.")
        held = conn.execute(
            "SELECT o.id FROM order_assets oa JOIN orders o ON o.id=oa.order_id "
            "WHERE oa.asset_id=? AND o.cancelled_at='' AND o.archived_at='' LIMIT 1",
            (a["id"],)).fetchone()
        if held:
            abort(409, description=f"이미 주문 #{held['id']}에 배정된 자산입니다.")
        old = conn.execute("SELECT * FROM asset_prebuilds WHERE asset_id=?", (a["id"],)).fetchone()
        if old:
            if old["cancelled_at"]:
                # 이전 버전이 남긴 취소 레코드는 기록 보관하지 않는다. 즉시 지우고 새로 등록한다.
                conn.execute("DELETE FROM asset_prebuilds WHERE id=?", (old["id"],))
            else:
                return jsonify(_payload(conn, old))
        now = config.now_iso()
        cur = conn.execute(
            "INSERT INTO asset_prebuilds(asset_id, created_by, created_at, updated_at) "
            "VALUES(?,?,?,?)", (a["id"], g.user["display_name"], now, now))
        row = _get(conn, cur.lastrowid)
        asset_event(conn, a["id"], "선제작등록", {"작업자": g.user["display_name"]})
        audit.log("prebuild_created", target=a["asset_no"])
        return jsonify(_payload(conn, row)), 201


@bp.patch("/prebuilds/<int:pid>")
def update_prebuild(pid):
    require("orders.work")
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").strip()
    value = bool(body.get("value"))
    now, actor = config.now_iso(), g.user["display_name"]
    with tx(write=True) as conn:
        row = _get(conn, pid)
        if action == "cancel":
            if row["used_order_id"]:
                abort(409, description="이미 주문에 사용된 선제작은 취소할 수 없습니다.")
            reason = (body.get("reason") or "").strip()
            if not reason or len(reason) > 500:
                abort(400, description="취소 사유를 1~500자로 입력하세요.")
            asset = conn.execute("SELECT asset_no FROM assets WHERE id=?", (row["asset_id"],)).fetchone()
            asset_event(conn, row["asset_id"], "선제작취소", {"작업자": actor, "사유": reason})
            audit.log("prebuild_cancelled", target=asset["asset_no"], detail={"reason": reason})
            conn.execute("DELETE FROM asset_prebuilds WHERE id=?", (pid,))
            return jsonify({"ok": True, "assetNo": asset["asset_no"]})
        if action == "restore":
            if not row["cancelled_at"]:
                abort(409, description="취소된 선제작이 아닙니다.")
            conn.execute(
                "UPDATE asset_prebuilds SET cancelled_at='', cancelled_by='', cancel_reason='', "
                "updated_at=? WHERE id=?", (now, pid))
            asset = conn.execute("SELECT asset_no FROM assets WHERE id=?", (row["asset_id"],)).fetchone()
            asset_event(conn, row["asset_id"], "선제작복구", {"작업자": actor})
            audit.log("prebuild_restored", target=asset["asset_no"])
            return jsonify(_payload(conn, _get(conn, pid)))
        if row["cancelled_at"]:
            abort(409, description="취소된 선제작입니다. 먼저 복구하세요.")
        if row["used_order_id"]:
            abort(409, description="이미 주문에 사용된 선제작 자산은 수정할 수 없습니다.")
        if action == "production":
            if not value and row["inspection_done"]:
                abort(409, description="선SW검수를 먼저 해제하세요.")
            conn.execute(
                "UPDATE asset_prebuilds SET production_done=?, production_by=?, production_at=?, "
                "updated_at=? WHERE id=?",
                (1 if value else 0, actor if value else "", now if value else "", now, pid))
        elif action == "inspection":
            if value and not row["production_done"]:
                abort(409, description="선제작완료 후 선SW검수를 완료할 수 있습니다.")
            if not value and row["ready_done"]:
                abort(409, description="출고 준비완료를 먼저 해제하세요.")
            conn.execute(
                "UPDATE asset_prebuilds SET inspection_done=?, inspection_by=?, inspection_at=?, "
                "updated_at=? WHERE id=?",
                (1 if value else 0, actor if value else "", now if value else "", now, pid))
        elif action == "spec":
            if row["production_done"]:
                abort(409, description="제작 사양은 선제작완료 전에 저장하세요.")
            ram1 = (body.get("ram1") or "").strip().upper()
            ram2 = (body.get("ram2") or "").strip().upper()
            ssd = (body.get("ssd") or "").strip().upper()
            ram1_type = (body.get("ram1Type") or "").strip().upper()
            ram2_type = (body.get("ram2Type") or "없음").strip().upper()
            ssd_type_raw = (body.get("ssdType") or "").strip()
            ssd_type_map = {"M.2 NVME": "M.2 NVMe", "M.2 SATA": "M.2 SATA",
                            "M.2 SSD": "M.2 SSD"}
            ssd_type = ssd_type_map.get(ssd_type_raw.upper(), "")
            hdd = (body.get("hdd") or "").strip().upper()
            ram_type_allowed = {"온보드", "D3", "D4", "D5"}
            ssd_type_allowed = set(ssd_type_map.values())
            ram_allowed = {"4GB", "8GB", "16GB", "24GB", "32GB", "64GB", "128GB"}
            ssd_allowed = {"128GB", "256GB", "512GB", "1TB", "2TB", "4TB"}
            hdd_allowed = {"없음", "320GB", "500GB", "1TB", "2TB", "4TB"}
            if (ram1_type not in ram_type_allowed or ram1 not in ram_allowed
                    or ram2_type not in (ram_type_allowed | {"없음"})
                    or (ram2_type != "없음" and ram2 not in ram_allowed)
                    or ssd_type not in ssd_type_allowed or ssd not in ssd_allowed
                    or hdd not in hdd_allowed):
                abort(400, description="RAM 구성, SSD 종류·용량, HDD를 확인하세요.")
            if ram2_type == "없음":
                ram2 = ""
            ram_total = int(ram1.removesuffix("GB"))
            if ram2:
                ram_total += int(ram2.removesuffix("GB"))
            ram = f"{ram_total}GB"
            conn.execute(
                "UPDATE asset_prebuilds SET built_ram_type=?, built_ram_primary=?, "
                "built_ram2_type=?, built_ram2=?, built_ram=?, built_ssd_type=?, "
                "built_ssd=?, built_hdd=?, updated_at=? WHERE id=?",
                (ram1_type, ram1, ram2_type, ram2, ram, ssd_type, ssd, hdd, now, pid))
            asset = conn.execute("SELECT asset_no FROM assets WHERE id=?", (row["asset_id"],)).fetchone()
            ram_config = f"{ram1_type} {ram1}" + (f" + {ram2_type} {ram2}" if ram2 else "")
            asset_event(conn, row["asset_id"], "선제작사양", {
                "RAM": f"{ram_config} (총 {ram})", "SSD": f"{ssd_type} {ssd}",
                "HDD": hdd, "작업자": actor})
            audit.log("prebuild_spec", target=asset["asset_no"], detail={
                "ram1Type": ram1_type, "ram1": ram1,
                "ram2Type": ram2_type, "ram2": ram2, "ramTotal": ram,
                "ssdType": ssd_type,
                "ssd": ssd, "hdd": hdd})
            return jsonify(_payload(conn, _get(conn, pid)))
        elif action == "ready":
            if value and not row["inspection_done"]:
                abort(409, description="선SW검수 완료 후 출고 준비완료할 수 있습니다.")
            asset = conn.execute("SELECT ram,ssd FROM assets WHERE id=?", (row["asset_id"],)).fetchone()
            final_ram = row["built_ram"] or asset["ram"] or ""
            final_ssd = row["built_ssd"] or asset["ssd"] or ""
            conn.execute(
                "UPDATE asset_prebuilds SET ready_done=?, ready_by=?, ready_at=?, "
                "built_ram=?, built_ssd=?, credit_voided=0, credit_void_reason='', "
                "spec_change_required=0, spec_change_reason='', updated_at=? WHERE id=?",
                (1 if value else 0, actor if value else "", now if value else "",
                 final_ram if value else row["built_ram"],
                 final_ssd if value else row["built_ssd"], now, pid))
        elif action == "note":
            conn.execute("UPDATE asset_prebuilds SET note=?, updated_at=? WHERE id=?",
                         ((body.get("note") or "").strip()[:500], now, pid))
        else:
            abort(400, description="올바르지 않은 선제작 작업입니다.")
        fresh = _get(conn, pid)
        asset = conn.execute("SELECT asset_no FROM assets WHERE id=?", (row["asset_id"],)).fetchone()
        audit.log("prebuild_stage", target=asset["asset_no"],
                  detail={"action": action, "value": value, "worker": actor})
        return jsonify(_payload(conn, fresh))


@bp.delete("/prebuilds/<int:pid>")
def delete_prebuild(pid):
    require("orders.work")
    with tx(write=True) as conn:
        row = _get(conn, pid)
        if row["used_order_id"]:
            abort(409, description="이미 주문에 사용된 기록은 삭제할 수 없습니다.")
        a = conn.execute("SELECT asset_no FROM assets WHERE id=?", (row["asset_id"],)).fetchone()
        conn.execute("DELETE FROM asset_prebuilds WHERE id=?", (pid,))
        audit.log("prebuild_deleted", target=a["asset_no"])
    return jsonify({"ok": True})
