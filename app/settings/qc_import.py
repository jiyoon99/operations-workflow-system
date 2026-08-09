"""기존 QC 프로그램(order-workflow) 데이터 이관.

NAS 백업의 data/orders.json + data/users.json을 HMS로 그대로 옮긴다.

옮겨지는 것
- 사용자: 비밀번호 해시가 같은 형식(pbkdf2_sha256$310000$…)이라 **기존 비밀번호 그대로 로그인**된다.
  구 역할(owner/admin/sales_manager/md/as_manager/worker)은 HMS 메뉴 권한으로 환산한다.
- 주문: 단계별 담당자·시각(누가 준비/제작/검수/출고했는지)을 그대로 보존한다.
- 관리번호: TMS 형식(YYMMDD-NNNN)이라 HMS 자산으로 만들고 주문에 매칭한다.
  이미 있는 자산이면 그것을 쓰고, 없으면 '이관 자산'으로 새로 만든다.

재실행해도 안전하다 — 아이디·주문키·관리번호로 중복을 건너뛴다.
"""
import json
import re

from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import PERM_CODES, require
from ..db import get_db, tx
from ..purchase import asset_event
from . import _check_grantable, bp

# 업로드 파일의 비밀번호 해시를 그대로 저장하므로, 형식이 맞는 것만 받는다.
# (임의 문자열을 받으면 공격자가 아는 비밀번호로 만든 해시를 심어 계정을 만들 수 있다.)
_PW_HASH_RE = re.compile(r"^pbkdf2_sha256\$\d{4,}\$[A-Za-z0-9+/=_-]{8,}\$[A-Za-z0-9+/=_-]{16,}$")

# 구 프로그램의 최고권한 역할 — 관리자로 자동 승격하지 않고 '최대 권한 세트'로 환산한다.
# (이관 파일만으로 관리자가 만들어지면 그게 곧 권한 상승 통로다. 승격은 대표가 화면에서 한다.)
TOP_ROLE_PERMS = sorted(PERM_CODES)

# 구 역할 → HMS 권한. 그 사람이 하던 일을 그대로 할 수 있는 최소 권한으로 맞춘다.
ROLE_PERMS = {
    "owner": "__admin__",
    "developer": "__admin__",
    "admin": "__admin__",
    "sales_manager": ["purchase.view", "purchase.edit", "orders.view", "orders.edit",
                      "orders.import", "orders.cancel", "setup.view", "orders.work",
                      "shipping.view", "orders.ship", "reports.view"],
    "md": ["orders.view", "orders.edit", "orders.import", "setup.view", "orders.work",
           "shipping.view", "orders.ship"],
    "as_manager": ["orders.view", "setup.view", "orders.work", "shipping.view",
                   "as.view", "as.manage"],
    "worker": ["setup.view", "orders.work"],
}
DEFAULT_PERMS = ["setup.view", "orders.work"]

_STR = [
    ("importKey", "import_key"), ("orderNumber", "order_no"), ("sourceFile", "source_file"),
    ("channel", "channel"), ("orderedAt", "ordered_at"), ("productName", "product_name"),
    ("optionName", "option_name"), ("productCode", "product_code"), ("recipient", "recipient"),
    ("phone", "phone"), ("postalCode", "postal_code"), ("address", "address"),
    ("deliveryMessage", "delivery_message"), ("memo", "memo"), ("courier", "courier"),
    ("trackingNumber", "tracking_no"), ("preparingBy", "preparing_by"),
    ("preparingAt", "preparing_at"), ("productionBy", "production_by"),
    ("productionAt", "production_at"), ("softwareInspectionBy", "inspection_by"),
    ("softwareInspectionAt", "inspection_at"), ("shippingBy", "shipping_by"),
    ("shippingAt", "shipping_at"), ("cancelledAt", "cancelled_at"),
    ("cancelledBy", "cancelled_by"), ("cancelReason", "cancel_reason"),
    ("restoredAt", "restored_at"), ("restoredBy", "restored_by"),
    ("archivedAt", "archived_at"), ("createdBy", "created_by"),
    ("createdAt", "created_at"), ("updatedAt", "updated_at"),
]


def _s(v):
    return "" if v is None else str(v).strip()


def _grant_perms(role):
    """구 역할을 실제로 부여할 권한 목록으로 바꾼다(관리자 플래그는 주지 않는다)."""
    perms = ROLE_PERMS.get(role, DEFAULT_PERMS)
    if perms == "__admin__":
        perms = TOP_ROLE_PERMS
    return [c for c in perms if c in PERM_CODES]


def _int(v, fallback=0):
    try:
        return int(float(str(v).replace(",", "").strip() or fallback))
    except (TypeError, ValueError):
        return fallback


def _clean_channel(v):
    """구 데이터에 채널 칸으로 금액이 새어 들어간 건이 있다(450000, 660000)."""
    t = _s(v)
    return "" if t.replace(",", "").replace(".", "").isdigit() else t


def _mgmt_numbers(order):
    """관리번호 칸은 여러 개가 줄바꿈/쉼표로 들어 있다."""
    raw = _s(order.get("managementNumber"))
    if not raw:
        return []
    out = []
    for chunk in raw.replace(",", "\n").split("\n"):
        t = chunk.strip()
        if t and t not in out:
            out.append(t)
    return out


def _read_payload():
    """파일 업로드(orders.json/users.json) 또는 서버 경로 지정."""
    orders, users = None, None
    for f in request.files.getlist("files"):
        name = (f.filename or "").lower()
        try:
            data = json.loads(f.read().decode("utf-8"))
        except Exception:
            abort(400, description=f"JSON을 읽지 못했습니다: {f.filename}")
        if "user" in name:
            users = data
        elif "order" in name:
            orders = data
        elif isinstance(data, list) and data and isinstance(data[0], dict) and "importKey" in data[0]:
            orders = data
        else:
            users = data
    body = request.form.get("path") or (request.get_json(silent=True) or {}).get("path")
    if body:
        from pathlib import Path
        base = Path(body)
        if not base.exists():
            abort(400, description=f"경로를 찾을 수 없습니다: {body}")
        odir = base / "data" if (base / "data").exists() else base
        try:
            if orders is None and (odir / "orders.json").exists():
                orders = json.loads((odir / "orders.json").read_text("utf-8"))
            if users is None and (odir / "users.json").exists():
                users = json.loads((odir / "users.json").read_text("utf-8"))
        except Exception as e:
            abort(400, description=f"파일을 읽지 못했습니다: {e}")
    if orders is None and users is None:
        abort(400, description="orders.json 또는 users.json을 올리거나 폴더 경로를 지정하세요.")
    if isinstance(users, dict):
        users = users.get("users", [])
    return (orders or []), (users or [])


def _plan(conn, orders, users):
    """무엇이 들어가고 무엇이 건너뛰어지는지 계산(미리보기와 실행이 같은 결과를 쓰도록)."""
    have_users = {r["username"].lower() for r in conn.execute("SELECT username FROM users").fetchall()}
    have_keys = {r["import_key"] for r in conn.execute(
        "SELECT import_key FROM orders WHERE import_key != ''").fetchall()}
    have_assets = {r["asset_no"]: r["id"] for r in conn.execute(
        "SELECT id, asset_no FROM assets").fetchall()}

    new_users, dup_users = [], []
    for u in users:
        name = _s(u.get("username"))
        if not name or not _s(u.get("passwordHash")):
            continue
        (dup_users if name.lower() in have_users else new_users).append(u)

    # ★이미 들어온 주문이라도 '단계가 더 진행됐으면' 그건 갱신해야 한다.
    #   예전에는 중복이면 통째로 건너뛰어서, 첫 취입 뒤 QC에서 셋팅·검수·출고를 끝내도
    #   HMS는 옛날 상태 그대로였다(2026-08-04 실측: 745건 중 37건 56개 플래그가 어긋나 있었다).
    #   대표가 이 파일을 '목록 최신화'로 쓰므로 갱신이 되어야 목적에 맞는다.
    have_rows = {r["import_key"]: r for r in conn.execute(
        "SELECT import_key, preparing, production_done, inspection_done, shipping_done, "
        "       archived_at, archive_reason "
        "FROM orders WHERE import_key != ''").fetchall()}

    new_orders, dup_orders, adv_orders = [], [], []
    asset_nos = []
    for o in orders:
        key = _s(o.get("importKey")) or f"qc-{_s(o.get('id'))}"
        if key in have_keys:
            fwd = _advance_fields(have_rows.get(key), o)
            if fwd:
                adv_orders.append((key, o, fwd))
            else:
                dup_orders.append(o)
            continue
        new_orders.append((key, o))
        for mn in _mgmt_numbers(o):
            if mn not in asset_nos:
                asset_nos.append(mn)
    new_assets = [a for a in asset_nos if a not in have_assets]
    return {
        "newUsers": new_users, "dupUsers": dup_users,
        "newOrders": new_orders, "dupOrders": dup_orders, "advOrders": adv_orders,
        "newAssets": new_assets, "haveAssets": have_assets,
    }


# 단계 플래그 ↔ 담당자/시각 짝
_STAGES = (
    ("preparing", "preparing", "preparingBy", "preparing_by", "preparingAt", "preparing_at"),
    ("productionDone", "production_done", "productionBy", "production_by",
     "productionAt", "production_at"),
    ("softwareInspectionDone", "inspection_done", "softwareInspectionBy", "inspection_by",
     "softwareInspectionAt", "inspection_at"),
    ("shippingDone", "shipping_done", "shippingBy", "shipping_by", "shippingAt", "shipping_at"),
)


_STAGE_LABEL = {"preparing": "셋팅", "production_done": "제작완료",
                "inspection_done": "SW검수", "shipping_done": "출고완료"}


def _advance_fields(row, o):
    """이미 있는 주문에서 '앞으로 나간 단계'만 뽑는다.

    ★전진만 반영한다(미완료 → 완료). 되돌리지 않는다.
      HMS에서도 같은 주문을 만지고 있어서, 파일이 옛것이면 되돌리기가 일을 지운다.
      끝난 일을 안 끝난 것으로 만드는 사고는 되돌릴 방법이 없다.
    """
    if row is None:
        return {}
    # ★★'중복이라 내린 줄'은 되살리지 않는다(2026-08-05 사고).
    #   같은 주문이 우리 쪽에 두 줄이라 한쪽을 정리했는데, QC 파일에는 그 줄의 키가 그대로
    #   남아 있다. 그래서 이 함수가 20초마다 shipping_done=1로 되돌리고 → 정리가 다시 0으로
    #   → 무한 반복이 됐다(실측: 감시 17회 동안 같은 62쌍을 13번 다시 합쳤고, 매출이
    #   1억2,770만 ↔ 1억3,533만 사이를 계속 오갔다).
    #   정리한 줄은 사람이 판단해 내린 것이므로 파일 쪽 상태보다 우선한다.
    try:
        if str(row["archive_reason"] or "").startswith(("중복", "렌탈")):
            return {}
    except (IndexError, KeyError):
        pass                      # 옛 호출부(그 칸을 안 읽어 온 경우) — 예전대로 동작
    out = {}
    for src_done, col_done, src_by, col_by, src_at, col_at in _STAGES:
        if o.get(src_done) and not row[col_done]:
            out[col_done] = 1
            if _s(o.get(src_by)):
                out[col_by] = _s(o.get(src_by))
            if _s(o.get(src_at)):
                out[col_at] = _s(o.get(src_at))
    # ★보관(마감)도 같은 '전진만' 규칙으로 따라온다. 단계만 옮기고 마감을 두고 오면
    #   QC에서 닫은 주문이 HMS에선 계속 열려 있어 '한쪽에서만 닫힌 주문'이 쌓인다
    #   (2026-08-04 감사: 전진철·해피모바일 2건). 해제는 절대 하지 않는다.
    if _s(o.get("archivedAt")) and not row["archived_at"]:
        out["archived_at"] = _s(o.get("archivedAt"))
    return out


@bp.post("/migrate/qc/preview")
def qc_preview():
    require("settings.manage")
    require("users.manage")
    orders, users = _read_payload()
    with tx() as conn:
        p = _plan(conn, orders, users)
    return jsonify({
        "orders": {"total": len(orders), "toCreate": len(p["newOrders"]),
                   "duplicates": len(p["dupOrders"]),
                   # 이미 있는 주문인데 단계가 더 진행된 것 — 갱신 대상
                   "toAdvance": len(p["advOrders"]),
                   "advanceSample": [
                       {"orderNumber": _s(o.get("orderNumber")),
                        "recipient": _s(o.get("recipient")),
                        "단계": ", ".join(_STAGE_LABEL.get(c, c) for c in fwd if c in _STAGE_LABEL)}
                       for _k, o, fwd in p["advOrders"][:15]]},
        "users": {"total": len(users), "toCreate": len(p["newUsers"]), "duplicates": len(p["dupUsers"]),
                  "list": [{"username": _s(u.get("username")), "displayName": _s(u.get("displayName")),
                            "role": _s(u.get("role")),
                            "grant": f"{len(_grant_perms(_s(u.get('role'))))}개 권한"}
                           for u in p["newUsers"]]},
        "assets": {"toCreate": len(p["newAssets"]), "sample": p["newAssets"][:10]},
        "sample": [{"channel": _clean_channel(o.get("channel")), "orderNumber": _s(o.get("orderNumber")),
                    "productName": _s(o.get("productName"))[:40], "recipient": _s(o.get("recipient")),
                    "관리번호": ", ".join(_mgmt_numbers(o))}
                   for _k, o in p["newOrders"][:15]],
    })


@bp.post("/migrate/qc")
def qc_run():
    # 업로드 파일이 계정을 만드는 경로다 — 계정 관리 권한까지 함께 요구한다.
    require("settings.manage")
    require("users.manage")
    orders, users = _read_payload()
    ts = config.now_iso()
    actor = g.user["display_name"]
    created_u = created_o = created_a = matched = 0
    advanced = advanced_stages = 0
    imported_names = []

    with tx(write=True) as conn:
        p = _plan(conn, orders, users)

        # 1) 사용자 — 비밀번호 해시를 그대로 옮겨 기존 비밀번호로 로그인되게 한다.
        #    관리자로는 절대 만들지 않는다(_grant_perms 참고). 승격은 대표가 화면에서.
        for u in p["newUsers"]:
            username = _s(u.get("username"))
            pw_hash = _s(u.get("passwordHash"))
            if not _PW_HASH_RE.match(pw_hash):
                abort(400, description=f"'{username}'의 비밀번호 형식이 올바르지 않습니다. "
                                       f"QC 프로그램의 users.json이 맞는지 확인해 주세요.")
            # 이관 파일이 '내가 못 주는 권한'을 우회로 부여하면 안 된다(계정 생성과 같은 규칙).
            # 담당 분류도 전체 보기로 열지 않는다 — 대표가 화면에서 정한다.
            perms = _grant_perms(_s(u.get("role")))
            _check_grantable(perms)
            cur = conn.execute(
                "INSERT INTO users(username, display_name, pw_hash, is_admin, all_categories, "
                "enabled, created_at, updated_at) VALUES(?,?,?,0,?,?,?,?)",
                (username, _s(u.get("displayName")) or username, pw_hash,
                 1 if g.user["is_admin"] else 0,
                 1 if u.get("enabled", True) else 0,
                 _s(u.get("createdAt")) or ts, ts))
            uid = cur.lastrowid
            for code in perms:
                conn.execute("INSERT INTO user_perms(user_id, perm) VALUES(?,?)", (uid, code))
            created_u += 1
            imported_names.append(username)

        # 2) 관리번호 → 자산(없는 것만 생성)
        cat = conn.execute(
            "SELECT id FROM categories WHERE enabled=1 ORDER BY sort, id LIMIT 1").fetchone()
        cat_id = cat["id"] if cat else None
        assets = dict(p["haveAssets"])
        for no in p["newAssets"]:
            cur = conn.execute(
                "INSERT INTO assets(asset_no, category_id, grade, status, notes, created_by, "
                "created_at, updated_at) VALUES(?,?,'미정','shipped',?,?,?,?)",
                (no, cat_id, "[이관] QC 프로그램 주문에 기록된 관리번호", actor, ts, ts))
            assets[no] = cur.lastrowid
            asset_event(conn, cur.lastrowid, "이관등록", {"출처": "QC 프로그램", "관리번호": no})
            created_a += 1

        # 3) 주문
        for key, o in p["newOrders"]:
            vals = {col: _s(o.get(src)) for src, col in _STR}
            vals["import_key"] = key
            vals["channel"] = _clean_channel(o.get("channel"))
            vals["dedupe_key"] = key
            vals["quantity"] = max(1, _int(o.get("quantity"), 1))
            vals["amount"] = _int(o.get("amount"))
            vals["preparing"] = 1 if o.get("preparing") else 0
            vals["production_done"] = 1 if o.get("productionDone") else 0
            vals["inspection_done"] = 1 if o.get("softwareInspectionDone") else 0
            vals["shipping_done"] = 1 if o.get("shippingDone") else 0
            vals["raw"] = json.dumps(o, ensure_ascii=False)
            vals["created_at"] = vals.get("created_at") or ts
            vals["updated_at"] = vals.get("updated_at") or ts
            vals["created_by"] = vals.get("created_by") or "이관"
            cols = list(vals.keys())
            cur = conn.execute(
                f"INSERT INTO orders({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                [vals[c] for c in cols])
            oid = cur.lastrowid
            created_o += 1
            for mn in _mgmt_numbers(o):
                aid = assets.get(mn)
                if not aid:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO order_assets(order_id, asset_id, prev_status, "
                    "matched_by, matched_at) VALUES(?,?,?,?,?)",
                    (oid, aid, "ready", _s(o.get("managementNumberBy")) or "이관",
                     _s(o.get("managementNumberAt")) or ts))
                matched += 1

        # 3) 이미 있는 주문의 '더 진행된 단계'를 반영한다(전진만).
        #    ★취소·보관 같은 상태는 손대지 않는다 — 여기서 건드리면 HMS 쪽 판단을 뒤집는다.
        for _key, o, fwd in p["advOrders"]:
            cols = list(fwd.keys())
            conn.execute(
                "UPDATE orders SET " + ", ".join(f"{c}=?" for c in cols) +
                ", updated_at=? WHERE import_key=?",
                [fwd[c] for c in cols] + [ts, _key])
            advanced += 1
            advanced_stages += len([c for c in cols if c in _STAGE_LABEL])

        audit.log("qc_migrated", target=f"주문 {created_o} / 사용자 {created_u} / 자산 {created_a}",
                  detail={"orders": created_o, "users": created_u, "assets": created_a,
                          "matched": matched, "advanced": advanced,
                          "계정": imported_names})   # 누가 생겼는지 남긴다

    msg = (f"주문 {created_o}건, 사용자 {created_u}명, 자산 {created_a}대를 옮겼습니다.")
    if advanced:
        msg += f" 이미 있던 주문 {advanced}건의 진행 단계({advanced_stages}개)를 최신으로 맞췄습니다."
    return jsonify({
        "ok": True,
        "orders": {"created": created_o, "duplicates": len(p["dupOrders"]),
                   "advanced": advanced, "advancedStages": advanced_stages},
        "users": {"created": created_u, "duplicates": len(p["dupUsers"])},
        "assets": {"created": created_a, "matched": matched},
        "message": msg + " 직원들은 쓰던 비밀번호 그대로 로그인할 수 있습니다.",
    })
