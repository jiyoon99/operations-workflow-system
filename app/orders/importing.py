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


def suggest_product_code(conn, added):
    """제품코드가 빈 신규 주문의 메모에 '제품코드 후보'를 남긴다(코드 칸은 건드리지 않는다).

    ★왜 코드 칸에 직접 넣지 않는가(2026-09-01 적대 리뷰에서 확정):
      테무 엑셀은 판매자 SKU('제공 sku')가 비어 오는데, 처음엔 '같은 상품명으로 이미 판
      주문의 코드'를 자동으로 박게 했다. 그런데 라이브 데이터가 그 가정을 깬다 —
      같은 상품명(listing)의 실제 모델이 재입고마다 갈아끼워진다:
        '삼성 초가성비 노트북…' → NT371B5M_i7-7_ge AS급+한컴 → NT371B5L_i7-6_ge+한컴
                                → NT371B5M_i7-7_내장 AA급 → NT501R5A_i5-6_내장 AS급+랜동글
      즉 '후보가 하나뿐'은 정답이 아니라 '아직 한 번만 팔렸다'는 뜻이다. 로테이션 직후
      테무에서 먼저 팔리면 옛 코드가 조용히 박히고, 셋팅 재고·송장 요구등급이 전부
      그 코드를 따라가 잘못된 자산이 출고될 때까지 아무 데서도 안 걸린다.
      게다가 박힌 값이 다음 임포트의 '유일 후보'가 되어 오답이 재생산된다.
    → 그래서 **사람이 보는 메모로만** 알린다. 코드 칸은 비어 있어 셋팅 화면이
      '재고 없음 · 제품코드 미입력'으로 분명히 표시하고(대표 2026-08-14 방침),
      담당자가 메모의 후보를 확인해 넣으면 그때부터 정상 대조된다.

    ★신규 주문(added)에만 적용한다 — 중복 판정이 끝난 뒤라 기존 주문의 코드를 덮거나
      쿠팡 교차 중복키(productCode 를 신원 축으로 쓴다)를 흔들지 않는다.
    후보를 남긴 건수를 돌려준다.
    """
    from ..importers.dedupe import is_shaped_product_code
    need = {}
    for o in added:
        if str(o.get("productCode") or "").strip():
            continue
        name = str(o.get("productName") or "").strip()
        if name:
            need.setdefault(name, []).append(o)
    if not need:
        return 0
    hinted = 0
    for name, orders in need.items():
        rows = conn.execute(
            "SELECT TRIM(product_code) AS code, MAX(ordered_at) AS last FROM orders "
            "WHERE product_name=? AND TRIM(product_code)!='' AND cancelled_at='' "
            "GROUP BY TRIM(product_code) ORDER BY last DESC", (name,)).fetchall()
        codes = [r["code"] for r in rows if is_shaped_product_code(r["code"])]
        if not codes:
            continue
        # 최근에 팔린 코드가 앞이다. 여러 개면 '갈아끼워졌다'는 뜻이라 그대로 다 보여준다.
        head = "제품코드 후보: " + " / ".join(codes[:3])
        if len(codes) > 1:
            head += " ※같은 상품명에 코드가 여러 개입니다 — 실제 물건을 확인하세요"
        for o in orders:
            memo = str(o.get("memo") or "").strip()
            o["memo"] = f"{memo} / {head}" if memo else head
            hinted += 1
    return hinted


@bp.post("/orders/import/preview")
def import_preview():
    """저장 없이 시뮬레이션 — 추가/중복/배송지변경 건수와 미리보기."""
    require("orders.import")
    imported, errors = _parse_all(_collect_workbooks())
    with tx() as conn:
        existing, _snap = _load_existing(conn)
        added, shipping_updates = new_unique_orders(
            [dict(d) for d in existing], imported, now=config.now_iso())
        # ★중복 판정이 '끝난 뒤' 신규 건에만 — 기존 주문·중복키를 건드리지 않는다
        code_hints = suggest_product_code(conn, added)
    return jsonify({
        "parsed": len(imported),
        "added": len(added),
        "duplicates": len(imported) - len(added),
        "shippingUpdates": shipping_updates,
        "codeHints": code_hints,
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
        # ★중복 판정이 '끝난 뒤' 신규 건에만 — 기존 주문·중복키를 건드리지 않는다
        code_hints = suggest_product_code(conn, added)
        for o in added:
            insert_import_dict(conn, o, g.user["display_name"])
        changed = writeback_changed(conn, snapshot, existing)
        audit.log("orders_imported", detail={
            "parsed": len(imported), "added": len(added),
            "duplicates": len(imported) - len(added),
            "shippingUpdates": shipping_updates, "codeHints": code_hints,
            "errors": errors,
        })
    return jsonify({
        "parsed": len(imported), "added": len(added),
        "duplicates": len(imported) - len(added),
        "shippingUpdates": shipping_updates, "updatedExisting": changed,
        "codeHints": code_hints, "errors": errors,
    })
