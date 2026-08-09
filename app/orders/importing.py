"""주문 엑셀 가져오기 — 기존 order-workflow 임포터(4종 헤더 자동감지) 사용.

몰 API 연동 전까지의 주 수집 경로이며, API 연동 후에도 폴백으로 유지한다.
"""
import io
import json
import zipfile

from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import tx
from ..importers import import_workbook, new_unique_orders
from . import bp
from .mapping import insert_import_dict, row_to_import_dict, writeback_changed

MAX_FILES = 10
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
MAX_ARCHIVE_FILES = 20
MAX_ARCHIVE_UNCOMPRESSED = 100 * 1024 * 1024


def _collect_workbooks():
    """업로드 파일(xlsx/xls/zip) → [(파일명, 바이트)] 목록. 한도 검사 포함."""
    files = request.files.getlist("files")
    if not files:
        abort(400, description="업로드된 파일이 없습니다.")
    if len(files) > MAX_FILES:
        abort(400, description=f"파일은 한 번에 최대 {MAX_FILES}개까지 올릴 수 있습니다.")
    out = []
    total = 0
    for f in files:
        content = f.read()
        total += len(content)
        if total > MAX_UPLOAD_BYTES:
            abort(400, description="업로드 용량 한도(30MB)를 초과했습니다.")
        name = f.filename or "upload"
        low = name.lower()
        if low.endswith(".zip"):
            try:
                zf = zipfile.ZipFile(io.BytesIO(content))
            except zipfile.BadZipFile:
                abort(400, description=f"ZIP 파일을 열 수 없습니다: {name}")
            infos = [i for i in zf.infolist()
                     if i.filename.lower().endswith((".xlsx", ".xls")) and not i.filename.startswith("__MACOSX")]
            if len(infos) > MAX_ARCHIVE_FILES:
                abort(400, description=f"ZIP 안의 엑셀은 최대 {MAX_ARCHIVE_FILES}개까지 지원합니다: {name}")
            if sum(i.file_size for i in infos) > MAX_ARCHIVE_UNCOMPRESSED:
                abort(400, description=f"ZIP 압축 해제 용량 한도(100MB)를 초과했습니다: {name}")
            for i in infos:
                out.append((i.filename.rsplit("/", 1)[-1], zf.read(i)))
        elif low.endswith((".xlsx", ".xls")):
            out.append((name, content))
        else:
            abort(400, description=f"지원하지 않는 파일 형식입니다: {name} (xlsx/xls/zip)")
    if not out:
        abort(400, description="가져올 엑셀 파일이 없습니다.")
    return out


def _parse_all(workbooks):
    imported, errors = [], []
    for name, content in workbooks:
        try:
            imported.extend(import_workbook(content, name))
        except ValueError as e:
            errors.append(f"{name}: {e}")
        except Exception:
            errors.append(f"{name}: 파일을 해석하지 못했습니다.")
    return imported, errors


def _load_existing(conn):
    rows = conn.execute("SELECT * FROM orders").fetchall()
    dicts = [row_to_import_dict(r) for r in rows]
    snapshot = {d["_rowId"]: json.dumps(d, ensure_ascii=False, sort_keys=True, default=str) for d in dicts}
    return dicts, snapshot


@bp.post("/orders/import/preview")
def import_preview():
    """저장 없이 시뮬레이션 — 추가/중복/배송지변경 건수와 미리보기."""
    require("orders.import")
    imported, errors = _parse_all(_collect_workbooks())
    with tx() as conn:
        existing, _snap = _load_existing(conn)
    added, shipping_updates = new_unique_orders(
        [dict(d) for d in existing], imported, now=config.now_iso())
    return jsonify({
        "parsed": len(imported),
        "added": len(added),
        "duplicates": len(imported) - len(added),
        "shippingUpdates": shipping_updates,
        "errors": errors,
        "preview": [
            {"channel": o.get("channel"), "orderNumber": o.get("orderNumber"),
             "productName": o.get("productName"), "recipient": o.get("recipient"),
             "quantity": o.get("quantity"), "amount": o.get("amount")}
            for o in added[:30]
        ],
    })


@bp.post("/orders/import")
def import_orders():
    require("orders.import")
    imported, errors = _parse_all(_collect_workbooks())
    with tx(write=True) as conn:
        existing, snapshot = _load_existing(conn)
        added, shipping_updates = new_unique_orders(existing, imported, now=config.now_iso())
        for o in added:
            insert_import_dict(conn, o, g.user["display_name"])
        changed = writeback_changed(conn, snapshot, existing)
        audit.log("orders_imported", detail={
            "parsed": len(imported), "added": len(added),
            "duplicates": len(imported) - len(added),
            "shippingUpdates": shipping_updates, "errors": errors,
        })
    return jsonify({
        "parsed": len(imported), "added": len(added),
        "duplicates": len(imported) - len(added),
        "shippingUpdates": shipping_updates, "updatedExisting": changed,
        "errors": errors,
    })
