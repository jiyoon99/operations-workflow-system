"""공용 마스터 — 모델 마스터·거래처 마스터 (2026-09-02 대표 방침 "모든 데이터는 OWS·RMS에서 직접 등록·관리").

무엇이 여기 있나
  - 모델 마스터(models / model_categories): 자산 모델명·브랜드·대분류/중분류의 기준표.
      · OWS 화면(기준정보 ▸ 모델 마스터)에서 직접 등록하고, TMS HB_MST모델 행은 연동 창구(data-bridge)가
        실시간 사본으로 준다(sync_models). 월 20~50건이 등록되는 살아 있는 창구라 OWS에 등록 자리가 먼저 있어야 한다.
      · 자산 등록·수정 때 마스터에 있으면 빈 브랜드·카테고리를 채우고, 없으면 **막지 않고** 경고만 응답에 싣는다
        (complete_asset_body — purchase/__init__.py 의 _add_assets / update_asset 이 두 줄로 부른다).
      · RMS 에는 /api/bridge/models(공유 시크릿 X-Bridge-Token, app/__init__.py 전역 게이트)로 자동완성 후보를 준다.
  - 거래처 마스터(suppliers 확장 + supplier_tms_links): 구분(매입/판매/AS업체)·코드·사업자번호·연락처·대표 지정.
      · /suppliers 목록·등록·수정은 이 파일의 뷰가 맡는다 — purchase/__init__.py 의 옛 세 라우트를 **같은 주소·같은
        응답 모양**으로 대체한다(bp.record 로 엔드포인트의 뷰 함수만 바꿔 끼움. 옛 코드는 손대지 않았다).
      · 같은 이름의 TMS 신원이 둘 이상이면 자동으로 합치지 않는다 — 부 신원(supplier_tms_links)으로 남겨
        사람이 [대표 키 지정]/[별도 거래처로 분리]로 정한다. 표기가 다른 같은 업체는 alias_of(대표 거래처)로 묶는다.

연동 규칙(sync_models / sync_partners — tms_link.sync_once 가 화면 반영 뒤에 sync_masters 를 부른다)
  - TMS 행은 키ID 기준 upsert. 이름이 같은 OWS 행이 있으면 새로 만들지 않고 그 행에 키ID 를 붙이고 빈 칸만 채운다.
  - 사람이 OWS에서 만든/고친 행(source='ows' 또는 ows_edited_at)은 덮지 않는다(빈 칸 채우기·삭제 표시만).
  - 연동 행(source='tms', 안 고친 것)은 TMS 값이 정본 — 바뀐 칸을 따라간다.
  - TMS 삭제·목록 이탈은 지우지 않고 tms_deleted_at 표시만. 되살아나면 표시를 지운다.
  - ★거래처코드는 키가 아니다 — TMS에서 사실상 자유 입력이라(실측: 코드 '업체'를 매입처 15곳이 공유, 105곳 중 유일 87)
    같은 코드가 여러 곳에 붙는다. 정보 칸으로만 둔다.
  - 창구 호출은 트랜잭션 밖에서 먼저 다 받고, 반영은 한 트랜잭션에서 한다(외부 호출은 tx(write=True) 안 금지).
  - 실패해도 틱을 깨지 않는다 — 요약에 error 로 남긴다.
"""
import json
import re
from collections import defaultdict
from contextlib import nullcontext

from flask import abort, g, has_app_context, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import get_db, tx
from ..settings import _int_or_400
from . import bp

KINDS = ("매입", "판매", "AS업체")
DEFAULT_KIND = "매입"
MODEL_TABLE = "HB_MST모델"
CATEGORY_TABLE = "HB_MST모델분류"
OVERLAP_SECONDS = 180            # 증분 커서를 3분 겹쳐 다시 본다(반영은 멱등)
PAGE = 500

# 거래처에서 연동이 채우는 칸(사람 행은 빈 칸만, 연동 행은 따라감). kind·enabled·name 은 따로 다룬다.
SUPPLIER_FILL_COLS = ("code", "biz_no", "ceo", "phone", "fax", "email", "address", "address_detail", "zip", "dept", "memo")
# 화면·API 가 받는 거래처 입력 칸 → 컬럼
SUPPLIER_INPUT = (("contact", "contact"), ("phone", "phone"), ("memo", "memo"), ("code", "code"),
                  ("bizNo", "biz_no"), ("ceo", "ceo"), ("fax", "fax"), ("email", "email"),
                  ("address", "address"), ("addressDetail", "address_detail"), ("zip", "zip"), ("dept", "dept"))
MODEL_INPUT = (("brand", "brand"), ("category", "category"), ("subcategory", "subcategory"),
               ("petName", "pet_name"), ("spec", "spec"), ("memo", "memo"))

_last_masters = {}               # 마지막 마스터 연동 요약(메모리) — 화면 상태용


def norm_key(s):
    """정규화 키 — 공백류 제거·대문자. 'NT 850XAC'/'nt850xac' 가 한 모델이다."""
    return re.sub(r"\s+", "", str(s or "")).upper()


def _actor():
    u = g.get("user")
    return u["display_name"] if u else "연동"


def _truthy(v):
    if isinstance(v, bool):
        return v
    return str(v if v is not None else "1").strip().lower() in ("1", "true", "y", "yes", "on")


def _s(it, key):
    v = it.get(key)
    return ("" if v is None else str(v)).strip()


def _limit(raw, default, maximum):
    try:
        return min(max(int(raw or default), 1), maximum)
    except (TypeError, ValueError):
        return default


def _minus_seconds(ts, sec):
    from .tms_link import _minus_seconds as f
    return f(ts, sec)


def _ctx(app):
    """이미 앱/요청 컨텍스트 안이면(화면의 [지금 연동]·sync_once) 그대로 — 새 컨텍스트를 밀면 g.user 가 사라져 감사 기록의 사람이 빠진다."""
    return nullcontext() if has_app_context() else app.app_context()


# ════════════════════════════════════════════════════════════════ 모델 마스터
def _model_payload(r, extra=None):
    out = {"id": r["id"], "name": r["name"], "brand": r["brand"], "category": r["category"],
           "subcategory": r["subcategory"], "petName": r["pet_name"], "spec": r["spec"], "memo": r["memo"],
           "enabled": bool(r["enabled"]), "source": r["source"], "tmsKeyId": r["tms_key_id"],
           "tmsDeletedAt": r["tms_deleted_at"], "owsEditedAt": r["ows_edited_at"], "owsEditedBy": r["ows_edited_by"],
           "createdAt": r["created_at"], "updatedAt": r["updated_at"], "updatedBy": r["updated_by"]}
    if extra:
        out.update(extra)
    return out


def find_model(conn, name):
    """이름(정규화)으로 마스터 행 하나 — 없으면 None."""
    key = norm_key(name)
    if not key:
        return None
    return conn.execute("SELECT * FROM models WHERE norm_name=?", (key,)).fetchone()


def model_names(q, limit):
    """스펙 자동완성(field=model)에 얹는 마스터 후보 — 앞부분 일치 먼저. spec_options 가 부른다."""
    key = norm_key(q)
    conn = get_db()
    sql = "SELECT name, brand, category, subcategory FROM models WHERE enabled=1 AND tms_deleted_at=''"
    params = []
    if key:
        sql += " AND norm_name LIKE ?"
        params.append("%" + key + "%")
    sql += " ORDER BY CASE WHEN norm_name LIKE ? THEN 0 ELSE 1 END, name LIMIT ?"
    params += [key + "%", limit]
    out = []
    for r in conn.execute(sql, params).fetchall():
        hint = " · ".join(x for x in (r["brand"], "/".join(y for y in (r["category"], r["subcategory"]) if y)) if x)
        out.append({"value": r["name"], "source": "master", "hint": hint})
    return out


def complete_asset_body(conn, body, current=None):
    """자산 등록·수정 입력에 모델 마스터를 보탠다 — 막지 않는다.

    반환 (body, note): body 는 빈 브랜드(·등록 때 빈 카테고리)를 마스터 값으로 채운 사본,
    note 는 응답에 실을 표시 — modelMaster(찾은 행) / modelFilled(채운 칸) / modelWarning(마스터에 없음).
    current 는 수정 때의 기존 자산 행(모델명이 body 에 없으면 기존 모델명으로 찾는다).
    """
    has_model = "model" in body
    model = (body.get("model") if has_model else (current["model"] if current is not None else "")) or ""
    model = str(model).strip()
    if not model:
        return body, {}
    m = find_model(conn, model)
    if m is None:
        return body, {"modelMaster": None, "modelWarning": f"모델 마스터에 없는 모델명입니다: {model}"}
    body = dict(body)
    filled = {}
    maker_now = body.get("maker") if "maker" in body else (current["maker"] if current is not None else "")
    if not str(maker_now or "").strip() and m["brand"]:
        body["maker"] = m["brand"]
        filled["maker"] = m["brand"]
    if current is None and body.get("categoryId") in (None, "") and (m["subcategory"] or m["category"]):
        # ★카테고리 축 = TMS 중분류(2026-09-03, A5) — 마스터 중분류(노트북/태블릿/모니터/…)를 migration 의 대응표로 푼다.
        #   예전엔 대분류 이름과 같은 카테고리만 찾아 태블릿·모니터 모델은 못 채웠다. 마스터 중분류가 비면 대분류로.
        from .migration import category_decision, category_ids   # 순환 import 회피 — migration 은 이 파일을 지연 import 한다
        cid, _basis = category_decision(category_ids(conn), "", "", m)
        if cid:
            body["categoryId"] = cid
            filled["categoryId"] = cid
    note = {"modelMaster": {"id": m["id"], "name": m["name"], "brand": m["brand"],
                            "category": m["category"], "subcategory": m["subcategory"]}}
    if filled:
        note["modelFilled"] = filled
    if m["tms_deleted_at"]:
        note["modelWarning"] = f"TMS에서 삭제 표시된 모델입니다: {m['name']}"
    return body, note


def _model_fields_from_body(body, current=None):
    """등록·수정 공통 칸 파싱. (컬럼, 값) — 없는 키는 건드리지 않는다."""
    sets = {}
    for key, col in MODEL_INPUT:
        if key in body:
            v = str(body.get(key) or "").strip()
            if len(v) > 200:
                abort(400, description=f"{key} 은(는) 200자 이내여야 합니다.")
            if current is None or v != (current[col] or ""):
                sets[col] = v
    if "enabled" in body:
        v = 1 if body.get("enabled") else 0
        if current is None or v != int(current["enabled"]):
            sets["enabled"] = v
    return sets


@bp.get("/models")
def list_models():
    """모델 마스터 목록·자동완성 — 정규화 부분일치, 활성만 기본(all=1 이면 비활성·TMS 삭제 포함)."""
    require("purchase.view")
    a = request.args
    q = norm_key(a.get("q"))
    cat = (a.get("category") or "").strip()
    sub = (a.get("subcategory") or "").strip()
    show_all = (a.get("all") or "") == "1"
    limit = _limit(a.get("limit"), 30, 500)
    where, params = ["1=1"], []
    if not show_all:
        where.append("enabled=1 AND tms_deleted_at=''")
    if q:
        where.append("(norm_name LIKE ? OR UPPER(REPLACE(brand,' ','')) LIKE ? OR UPPER(REPLACE(pet_name,' ','')) LIKE ?)")
        params += ["%" + q + "%"] * 3
    if cat:
        where.append("category=?")
        params.append(cat)
    if sub:
        where.append("subcategory=?")
        params.append(sub)
    conn = get_db()
    sql_where = " WHERE " + " AND ".join(where)
    total = conn.execute("SELECT COUNT(*) AS c FROM models" + sql_where, params).fetchone()["c"]
    rows = conn.execute(
        "SELECT * FROM models" + sql_where +
        " ORDER BY CASE WHEN norm_name LIKE ? THEN 0 ELSE 1 END, name LIMIT ?",
        params + [q + "%", limit]).fetchall()
    items = [_model_payload(r) for r in rows]
    if (a.get("withCounts") or "") == "1" and items:
        # 자산 대수 — 모델명 정규화 키로 한 번에 센다(모델별 개별 쿼리 금지)
        counts = {r["k"]: r["c"] for r in conn.execute(
            "SELECT REPLACE(UPPER(model),' ','') AS k, COUNT(*) AS c FROM assets WHERE model!='' GROUP BY k").fetchall()}
        for it, r in zip(items, rows):
            it["assetCount"] = counts.get(r["norm_name"], 0)
    return jsonify({"items": items, "count": len(items), "total": total})


@bp.get("/models/lookup")
def lookup_model():
    """모델명 하나가 마스터에 있는지 — 자산 입력칸 옆 표시용. ?name="""
    require("purchase.view")
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"found": False, "model": None})
    m = find_model(get_db(), name)
    return jsonify({"found": m is not None, "model": _model_payload(m) if m is not None else None})


@bp.get("/model-categories")
def list_model_categories():
    require("purchase.view")
    show_all = (request.args.get("all") or "") == "1"
    rows = get_db().execute(
        "SELECT * FROM model_categories" + ("" if show_all else " WHERE enabled=1 AND tms_deleted_at=''") +
        " ORDER BY sort, id").fetchall()
    return jsonify({"items": [
        {"id": r["id"], "category": r["category"], "subcategory": r["subcategory"], "sort": r["sort"],
         "enabled": bool(r["enabled"]), "source": r["source"], "tmsKeyId": r["tms_key_id"],
         "tmsDeletedAt": r["tms_deleted_at"]} for r in rows]})


@bp.post("/models")
def create_model():
    """OWS 등록 창구 — 정규화 이름이 겹치면 거부(409). 연동 행이 나중에 같은 이름으로 오면 이 행에 키ID 가 붙는다."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 100:
        abort(400, description="모델명은 1~100자여야 합니다.")
    sets = _model_fields_from_body(body)
    ts = config.now_iso()
    with tx(write=True) as conn:
        dup = find_model(conn, name)
        if dup is not None:
            abort(409, description=f"이미 등록된 모델명입니다: {dup['name']}"
                                   + (" (TMS 삭제 표시)" if dup["tms_deleted_at"] else "")
                                   + ("" if dup["enabled"] else " (사용 안 함)"))
        cols = {"name": name, "norm_name": norm_key(name), "source": "ows", "enabled": 1,
                "ows_edited_at": ts, "ows_edited_by": _actor(), "created_at": ts,
                "updated_at": ts, "updated_by": _actor()}
        cols.update(sets)
        cur = conn.execute(
            f"INSERT INTO models({','.join(cols)}) VALUES({','.join('?' * len(cols))})", list(cols.values()))
        mid = cur.lastrowid
        audit.log("model_created", target=name, detail={k: v for k, v in sets.items() if v not in ("", None)})
        row = conn.execute("SELECT * FROM models WHERE id=?", (mid,)).fetchone()
    return jsonify(_model_payload(row)), 201


@bp.patch("/models/<int:mid>")
def update_model(mid):
    """수정 — 사람이 고친 행은 ows_edited_at 이 찍혀 연동이 덮지 않는다(TMS 삭제 표시만 따라온다)."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    ts = config.now_iso()
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM models WHERE id=?", (mid,)).fetchone()
        if row is None:
            abort(404, description="모델을 찾을 수 없습니다.")
        sets = _model_fields_from_body(body, row)
        if "name" in body:
            name = str(body.get("name") or "").strip()
            if not name or len(name) > 100:
                abort(400, description="모델명은 1~100자여야 합니다.")
            if name != row["name"]:
                dup = conn.execute("SELECT name FROM models WHERE norm_name=? AND id!=?",
                                   (norm_key(name), mid)).fetchone()
                if dup:
                    abort(409, description=f"이미 등록된 모델명입니다: {dup['name']}")
                sets["name"], sets["norm_name"] = name, norm_key(name)
        if not sets:
            abort(400, description="변경할 항목이 없습니다.")
        sets.update(ows_edited_at=ts, ows_edited_by=_actor(), updated_at=ts, updated_by=_actor())
        conn.execute("UPDATE models SET " + ", ".join(f"{c}=?" for c in sets) + " WHERE id=?",
                     list(sets.values()) + [mid])
        audit.log("model_updated", target=row["name"],
                  detail={k: v for k, v in sets.items() if k not in ("ows_edited_at", "ows_edited_by", "updated_at", "updated_by")})
        new = conn.execute("SELECT * FROM models WHERE id=?", (mid,)).fetchone()
    return jsonify(_model_payload(new))


@bp.get("/bridge/models")
def bridge_models():
    """RMS 창구 — 모델 자동완성(이름·브랜드·대분류/중분류). 인증은 app/__init__.py 전역 게이트(X-Bridge-Token)."""
    a = request.args
    q = norm_key(a.get("q"))
    limit = _limit(a.get("limit"), 20, 200)
    sql = "SELECT name, brand, category, subcategory, pet_name, spec FROM models WHERE enabled=1 AND tms_deleted_at=''"
    params = []
    if q:
        sql += " AND (norm_name LIKE ? OR UPPER(REPLACE(brand,' ','')) LIKE ?)"
        params += ["%" + q + "%"] * 2
    sql += " ORDER BY CASE WHEN norm_name LIKE ? THEN 0 ELSE 1 END, name LIMIT ?"
    params += [q + "%", limit]
    items = [{"name": r["name"], "brand": r["brand"], "category": r["category"], "subcategory": r["subcategory"],
              "petName": r["pet_name"], "spec": r["spec"]} for r in get_db().execute(sql, params).fetchall()]
    return jsonify({"ok": True, "count": len(items), "items": items})


@bp.get("/models/missing")
def missing_models():
    """모델명 정비 캠페인(2026-09-03) — 살아 있는 자산의 모델명 가운데 마스터에 없는 것.

    살아 있는 자산 = 매입취소(cancelled)·거래처반품(returned)이 아닌 자산(리포트와 같은 정의 — 판매된 자산도 센다).
    모델명은 정규화 키(norm_key — 공백·대소문자 무시)로 묶고, 보이는 이름은 그 키에서 가장 많이 쓰인 표기.
    마스터에 '있다'는 사용 안 함·TMS 삭제 표시 행도 포함한다(POST /models 가 어차피 409 를 낸다).
    ?limit=200(≤1000) → {items:[{name, count, brand, category, lastAt, spellings}], total, liveModels}
      brand = 그 자산들의 브랜드 최다(빈 값 제외) · category = 카테고리 이름 최다 · lastAt = 가장 최근 등록일 ·
      spellings = 같은 키의 표기 가짓수. 등록은 기존 POST /models 를 행마다 부른다(일괄도 같은 창구 — 이 라우트는 읽기만).
    """
    require("purchase.view")
    limit = _limit(request.args.get("limit"), 200, 1000)
    conn = get_db()
    known = {r["norm_name"] for r in conn.execute("SELECT norm_name FROM models").fetchall()}
    rows = conn.execute(
        "SELECT a.model, TRIM(COALESCE(a.maker,'')) AS maker, COALESCE(c.name,'') AS cat, "
        "       COUNT(*) AS c, MAX(a.created_at) AS last "
        "FROM assets a LEFT JOIN categories c ON c.id=a.category_id "
        "WHERE TRIM(COALESCE(a.model,'')) != '' AND a.status NOT IN ('cancelled','returned') "
        "GROUP BY a.model, maker, cat").fetchall()
    groups, live = {}, set()
    for r in rows:
        key = norm_key(r["model"])
        if not key:
            continue
        live.add(key)
        if key in known:
            continue
        grp = groups.setdefault(key, {"count": 0, "last": "", "names": defaultdict(int),
                                      "brands": defaultdict(int), "cats": defaultdict(int)})
        grp["count"] += r["c"]
        grp["last"] = max(grp["last"], r["last"] or "")
        grp["names"][str(r["model"]).strip()] += r["c"]
        if r["maker"]:
            grp["brands"][r["maker"]] += r["c"]
        if r["cat"]:
            grp["cats"][r["cat"]] += r["c"]

    def top(d):
        return max(d.items(), key=lambda kv: (kv[1], kv[0]))[0] if d else ""

    items = sorted(({"name": top(grp["names"]), "count": grp["count"], "brand": top(grp["brands"]),
                     "category": top(grp["cats"]), "lastAt": (grp["last"] or "")[:10], "spellings": len(grp["names"])}
                    for grp in groups.values()),
                   key=lambda it: (-it["count"], it["name"]))
    return jsonify({"items": items[:limit], "total": len(items), "liveModels": len(live)})


# ---------------------------------------------------------------- 모델 연동
def _fetch_table(client, table, since=None):
    """창구 사본 표를 전부(페이징) 받는다 — 삭제 행 포함(표시용). since 는 _synced_at 기준 증분."""
    items, after = [], None
    while True:
        data = client.get(f"/tables/{table}", after_key=after, since=since, include_deleted=1, limit=PAGE)
        page = data.get("items") or []
        items.extend(page)
        after = data.get("next_after_key")
        if not page or not after:
            break
    return items


def _apply_category(conn, it, ts, res):
    key = it.get("키ID")
    cat, sub = _s(it, "대분류"), _s(it, "중분류")
    if key is None or not cat:
        res["skipped"] += 1
        return
    key = int(key)
    enabled = 1 if _truthy(it.get("사용여부", 1)) else 0
    deleted = ts if it.get("_deleted_at") else ""
    row = conn.execute("SELECT * FROM model_categories WHERE tms_key_id=?", (key,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM model_categories WHERE category=? AND subcategory=?", (cat, sub)).fetchone()
        if row is not None:
            if row["tms_key_id"] is not None:
                res["skipped"] += 1              # 다른 키가 이미 이 분류를 쓴다
                return
            conn.execute("UPDATE model_categories SET tms_key_id=?, tms_deleted_at=?, updated_at=? WHERE id=?",
                         (key, deleted, ts, row["id"]))
            res["attached"] += 1
            return
        conn.execute(
            "INSERT INTO model_categories(category, subcategory, sort, enabled, source, tms_key_id, tms_deleted_at, "
            "created_at, updated_at) VALUES(?,?,?,?,'tms',?,?,?,?)", (cat, sub, key, enabled, key, deleted, ts, ts))
        res["created"] += 1
        return
    sets = {}
    if row["source"] == "tms":
        if (row["category"], row["subcategory"]) != (cat, sub):
            clash = conn.execute("SELECT id FROM model_categories WHERE category=? AND subcategory=? AND id!=?",
                                 (cat, sub, row["id"])).fetchone()
            if clash is None:
                sets.update(category=cat, subcategory=sub)
        if int(row["enabled"]) != enabled:
            sets["enabled"] = enabled
    if (row["tms_deleted_at"] or "") and not deleted:
        sets["tms_deleted_at"] = ""
    elif deleted and not row["tms_deleted_at"]:
        sets["tms_deleted_at"] = deleted
    if sets:
        sets["updated_at"] = ts
        conn.execute("UPDATE model_categories SET " + ", ".join(f"{c}=?" for c in sets) + " WHERE id=?",
                     list(sets.values()) + [row["id"]])
        res["updated"] += 1


def _model_fields_from_tms(it):
    return {"brand": _s(it, "브랜드"), "category": _s(it, "대분류"), "subcategory": _s(it, "중분류"),
            "pet_name": _s(it, "펫네임"), "spec": _s(it, "모델사양"), "memo": _s(it, "모델메모"),
            "enabled": 1 if _truthy(it.get("사용여부", 1)) else 0}


def _apply_model(conn, it, ts, res):
    key = it.get("키ID")
    name = _s(it, "모델명")
    if key is None or not name:
        res["skipped"] += 1
        return
    key = int(key)
    norm = norm_key(name)
    f = _model_fields_from_tms(it)
    deleted = ts if it.get("_deleted_at") else ""
    row = conn.execute("SELECT * FROM models WHERE tms_key_id=?", (key,)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM models WHERE norm_name=?", (norm,)).fetchone()
        if row is not None:
            if row["tms_key_id"] is not None:
                # TMS 가 다른 키의 이름을 이 이름으로 바꿔 충돌 — 기존 행을 지키고 기록만
                res["skipped"] += 1
                res["notes"].append(f"이름 충돌 건너뜀: {name}(키 {key}) ↔ 기존 키 {row['tms_key_id']}")
                return
            # OWS 등록 행에 키를 붙이고 빈 칸만 채운다(사람 값 불가침)
            sets = {"tms_key_id": key, "tms_deleted_at": deleted, "updated_at": ts}
            for col in ("brand", "category", "subcategory", "pet_name", "spec", "memo"):
                if not (row[col] or "").strip() and f[col]:
                    sets[col] = f[col]
            conn.execute("UPDATE models SET " + ", ".join(f"{c}=?" for c in sets) + " WHERE id=?",
                         list(sets.values()) + [row["id"]])
            res["attached"] += 1
            return
        conn.execute(
            "INSERT INTO models(name, norm_name, brand, category, subcategory, pet_name, spec, memo, enabled, source, "
            "tms_key_id, tms_deleted_at, created_at, updated_at, updated_by) VALUES(?,?,?,?,?,?,?,?,?,'tms',?,?,?,?,'연동')",
            (name, norm, f["brand"], f["category"], f["subcategory"], f["pet_name"], f["spec"], f["memo"],
             f["enabled"], key, deleted, ts, ts))
        res["created"] += 1
        if deleted:
            res["deleted"] += 1
        return
    protected = row["source"] == "ows" or bool(row["ows_edited_at"])
    sets = {}
    if not protected:
        for col in ("brand", "category", "subcategory", "pet_name", "spec", "memo", "enabled"):
            if (row[col] if col == "enabled" else (row[col] or "")) != f[col]:
                sets[col] = f[col]
        if name != row["name"]:
            clash = conn.execute("SELECT id FROM models WHERE norm_name=? AND id!=?", (norm, row["id"])).fetchone()
            if clash is None:
                sets["name"], sets["norm_name"] = name, norm
            else:
                res["notes"].append(f"이름 변경 건너뜀(충돌): {row['name']} → {name}")
    else:
        res["protected"] += 1
    if deleted and not row["tms_deleted_at"]:
        sets["tms_deleted_at"] = deleted
        res["deleted"] += 1
    elif not deleted and row["tms_deleted_at"]:
        sets["tms_deleted_at"] = ""
        res["restored"] += 1
    if sets:
        sets["updated_at"] = ts
        sets["updated_by"] = "연동"
        conn.execute("UPDATE models SET " + ", ".join(f"{c}=?" for c in sets) + " WHERE id=?",
                     list(sets.values()) + [row["id"]])
        if not protected:
            res["updated"] += 1


def sync_models(app, client, since=None):
    """모델·모델분류 마스터를 창구에서 받아 반영한다. since=None 이면 전량(첫 가동·재대사)."""
    cats = _fetch_table(client, CATEGORY_TABLE)           # 8행 — 늘 전량
    rows = _fetch_table(client, MODEL_TABLE, since=since)
    cursor = max([str(x.get("_synced_at") or "")[:19] for x in rows + cats] + [""])
    res = {"rows": len(rows), "created": 0, "updated": 0, "attached": 0, "protected": 0, "deleted": 0,
           "restored": 0, "skipped": 0, "notes": [],
           "categories": {"rows": len(cats), "created": 0, "updated": 0, "attached": 0, "skipped": 0}}
    with _ctx(app):
        ts = config.now_iso()
        with tx(write=True) as conn:
            for it in cats:
                _apply_category(conn, it, ts, res["categories"])
            for it in rows:
                _apply_model(conn, it, ts, res)
            changed = any(res[k] for k in ("created", "updated", "attached", "deleted", "restored")) or \
                any(res["categories"][k] for k in ("created", "updated", "attached"))
            if changed:
                audit.log("masters_models", detail={k: v for k, v in res.items() if k != "notes"})
    res["notes"] = res["notes"][:10]
    res["cursor"] = cursor or None
    app.logger.info("모델 마스터 연동 — 행 %d / 신규 %d / 갱신 %d / 연결 %d / 보호 %d / 삭제표시 %d",
                    res["rows"], res["created"], res["updated"], res["attached"], res["protected"], res["deleted"])
    return res


# ════════════════════════════════════════════════════════════════ 거래처 마스터
def _kind_or_400(v, default=DEFAULT_KIND):
    k = str(v or "").strip() or default
    if k not in KINDS:
        abort(400, description=f"거래처 구분은 {', '.join(KINDS)} 중 하나여야 합니다.")
    return k


def _supplier_payload(r, extra=None):
    out = {"id": r["id"], "name": r["name"], "kind": r["kind"], "code": r["code"], "bizNo": r["biz_no"],
           "ceo": r["ceo"], "contact": r["contact"], "phone": r["phone"], "fax": r["fax"], "email": r["email"],
           "address": r["address"], "addressDetail": r["address_detail"], "zip": r["zip"], "dept": r["dept"],
           "memo": r["memo"], "enabled": bool(r["enabled"]), "source": r["source"], "tmsKeyId": r["tms_key_id"],
           "tmsDeletedAt": r["tms_deleted_at"], "aliasOf": r["alias_of"], "owsEditedAt": r["ows_edited_at"],
           "owsEditedBy": r["ows_edited_by"], "createdAt": r["created_at"], "updatedAt": r["updated_at"]}
    if extra:
        out.update(extra)
    return out


def _link_payload(l):
    try:
        payload = json.loads(l["payload"]) if l["payload"] else {}
    except ValueError:
        payload = {}
    return {"keyId": l["tms_key_id"], "kind": l["kind"], "code": l["code"], "name": l["name"],
            "isPrimary": bool(l["is_primary"]), "deletedAt": l["deleted_at"], "seenAt": l["seen_at"],
            "payload": payload}


def resolve_supplier_name(conn, name):
    """거래처 이름 → 대표 거래처 id (없으면 None) — 전표를 '이름'으로 만드는 두 경로가 쓴다
    (전표 등록의 supplierName: purchase/__init__.py _batch_fields_from_body · TMS 매입현황: migration.apply_rows).

    ① 정규화 이름(공백·대소문자 무시)이 같은 suppliers 행 — 대표 행(alias_of 없음) 먼저, 그다음 오래된 행
    ② 없으면 살아 있는 부 신원(supplier_tms_links.name)의 거래처
    → 어느 쪽이든 별칭(alias_of)이면 대표 행으로 올린다.
    ★자동 병합은 하지 않는다 — 있는 행에 붙일 뿐, 행을 합치거나 이름을 바꾸지 않는다(사람 결정 원칙).
    """
    key = norm_key(name)
    if not key:
        return None
    rows = [r for r in conn.execute("SELECT id, name, norm_name, alias_of FROM suppliers").fetchall()
            if (r["norm_name"] or norm_key(r["name"])) == key]
    hit = None
    if rows:
        rows.sort(key=lambda r: (r["alias_of"] is not None, r["id"]))
        hit = rows[0]
    else:
        for l in conn.execute("SELECT supplier_id, name FROM supplier_tms_links WHERE deleted_at='' "
                              "ORDER BY is_primary DESC, tms_key_id").fetchall():
            if norm_key(l["name"]) == key:
                hit = conn.execute("SELECT id, alias_of FROM suppliers WHERE id=?", (l["supplier_id"],)).fetchone()
                if hit is not None:
                    break
    if hit is None:
        return None
    if hit["alias_of"] is not None:
        rep = conn.execute("SELECT id FROM suppliers WHERE id=?", (hit["alias_of"],)).fetchone()
        if rep is not None:
            return rep["id"]
    return hit["id"]


def supplier_names(q, limit, kind=None):
    """거래처 이름 후보(자동완성) — 구분을 주면 그 구분(부 신원 포함)만. 별칭 행은 대표로 보낸다."""
    key = norm_key(q)
    conn = get_db()
    rows = conn.execute(
        "SELECT s.id, s.name, s.norm_name, s.kind, s.alias_of, COUNT(b.id) AS c FROM suppliers s "
        "LEFT JOIN purchase_batches b ON b.supplier_id = s.id WHERE s.enabled = 1 AND s.alias_of IS NULL "
        "GROUP BY s.id ORDER BY c DESC, s.name").fetchall()
    kinds_by = defaultdict(set)
    if kind:
        for l in conn.execute("SELECT supplier_id, kind FROM supplier_tms_links WHERE deleted_at=''").fetchall():
            kinds_by[l["supplier_id"]].add(l["kind"])
    out = []
    for r in rows:
        if kind and kind != r["kind"] and kind not in kinds_by[r["id"]]:
            continue
        if key and key not in (r["norm_name"] or norm_key(r["name"])):
            continue
        out.append(r["name"])
        if len(out) >= limit:
            break
    return out


def list_suppliers():
    """GET /suppliers?q=&kind= — 옛 응답(배열)과 같은 모양에 마스터 칸·중복 후보·TMS 신원을 더한다."""
    require("purchase.view")
    a = request.args
    q = norm_key(a.get("q"))
    kind = (a.get("kind") or "").strip()
    conn = get_db()
    rows = conn.execute(
        "SELECT s.*, "
        " (SELECT COUNT(*) FROM purchase_batches b WHERE b.supplier_id = s.id) AS batch_count, "
        " (SELECT COALESCE(SUM(b.total_amount),0) FROM purchase_batches b WHERE b.supplier_id = s.id) AS total_amount "
        "FROM suppliers s ORDER BY s.name").fetchall()
    links = defaultdict(list)
    for l in conn.execute("SELECT * FROM supplier_tms_links ORDER BY is_primary DESC, tms_key_id").fetchall():
        links[l["supplier_id"]].append(l)
    by_id = {r["id"]: r for r in rows}
    groups = defaultdict(list)               # (정규화 이름, 구분) → 대표 후보 id 들 (별칭 미지정 행끼리)
    children = defaultdict(list)             # 대표 id → 별칭 행들
    for r in rows:
        if r["alias_of"] is None:
            groups[((r["norm_name"] or norm_key(r["name"])), r["kind"])].append(r["id"])
        elif r["alias_of"] in by_id:
            children[r["alias_of"]].append(r)
    out = []
    for r in rows:
        norm = r["norm_name"] or norm_key(r["name"])
        kinds = [k for k in KINDS if k == r["kind"] or any(l["kind"] == k and not l["deleted_at"] for l in links[r["id"]])]
        if kind and kind not in kinds:
            continue
        # 코드·사업자번호는 하이픈을 빼고 견준다('111 22' 로 '111-22-33333' 을 찾는다)
        qd = q.replace("-", "")
        if q and not (q in norm or (r["code"] and qd in norm_key(r["code"]).replace("-", ""))
                      or (r["biz_no"] and qd in norm_key(r["biz_no"]).replace("-", ""))):
            continue
        p = _supplier_payload(r, {
            "batchCount": r["batch_count"], "totalAmount": r["total_amount"], "kinds": kinds,
            "aliasOfName": by_id[r["alias_of"]]["name"] if r["alias_of"] in by_id else None,
            "aliasCount": len(children[r["id"]]),
            "groupBatchCount": r["batch_count"] + sum(c["batch_count"] for c in children[r["id"]]),
            "groupTotalAmount": r["total_amount"] + sum(c["total_amount"] for c in children[r["id"]]),
            "dupIds": [i for i in groups.get((norm, r["kind"]), []) if i != r["id"]],
            "tmsLinks": [_link_payload(l) for l in links[r["id"]]],
        })
        out.append(p)
    return jsonify(out)


def _supplier_input(body, current=None):
    sets = {}
    for key, col in SUPPLIER_INPUT:
        if key in body:
            v = str(body.get(key) or "").strip()
            if len(v) > 300:
                abort(400, description=f"{key} 은(는) 300자 이내여야 합니다.")
            if current is None or v != (current[col] or ""):
                sets[col] = v
    return sets


def create_supplier():
    """POST /suppliers — 옛 본문({name, contact, phone, memo})도 그대로 받는다. 구분 기본 '매입'."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "").strip()
    if not name:
        abort(400, description="거래처 이름을 입력하세요.")
    if len(name) > 100:
        abort(400, description="거래처 이름은 100자 이내여야 합니다.")
    kind = _kind_or_400(body.get("kind"))
    sets = _supplier_input(body)
    ts = config.now_iso()
    with tx(write=True) as conn:
        hit = conn.execute("SELECT kind FROM suppliers WHERE name=?", (name,)).fetchone()
        if hit:
            abort(409, description=f"이미 존재하는 거래처입니다(구분: {hit['kind']}). 이름은 한 곳에 하나만 둘 수 있습니다 — "
                                   "구분이 다르면 기존 거래처의 구분을 확인하세요.")
        dup = conn.execute("SELECT name FROM suppliers WHERE norm_name=? AND kind=? AND alias_of IS NULL",
                           (norm_key(name), kind)).fetchone()
        if dup:
            abort(409, description=f"공백·대소문자만 다른 거래처가 이미 있습니다: {dup['name']}")
        cols = {"name": name, "norm_name": norm_key(name), "kind": kind, "source": "ows", "enabled": 1,
                "ows_edited_at": ts, "ows_edited_by": _actor(), "created_at": ts, "updated_at": ts}
        cols.update(sets)
        cur = conn.execute(f"INSERT INTO suppliers({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                           list(cols.values()))
        sid = cur.lastrowid
        audit.log("supplier_created", target=name,
                  detail={"kind": kind, **{k: v for k, v in sets.items() if v}})
    return jsonify({"id": sid, "name": name, "kind": kind}), 201


def update_supplier(sid):
    """PATCH /suppliers/<id> — 옛 칸(name/contact/phone/memo/enabled) + 구분·코드·사업자번호·연락처·별칭."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    ts = config.now_iso()
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
        if row is None:
            abort(404, description="거래처를 찾을 수 없습니다.")
        changes = _supplier_input(body, row)
        kind = row["kind"]
        if "kind" in body:
            kind = _kind_or_400(body.get("kind"), row["kind"])
            if kind != row["kind"]:
                changes["kind"] = kind
        if "name" in body:
            name = str(body.get("name") or "").strip()
            if not name:
                abort(400, description="거래처 이름을 입력하세요.")
            if len(name) > 100:
                abort(400, description="거래처 이름은 100자 이내여야 합니다.")
            if name != row["name"]:
                if conn.execute("SELECT id FROM suppliers WHERE name=? AND id != ?", (name, sid)).fetchone():
                    abort(409, description="이미 존재하는 거래처 이름입니다.")
                changes["name"], changes["norm_name"] = name, norm_key(name)
        if "name" in changes or "kind" in changes:
            dup = conn.execute(
                "SELECT name FROM suppliers WHERE norm_name=? AND kind=? AND alias_of IS NULL AND id != ?",
                (changes.get("norm_name", row["norm_name"] or norm_key(row["name"])), kind, sid)).fetchone()
            if dup:
                abort(409, description=f"공백·대소문자만 다른 거래처가 이미 있습니다: {dup['name']}")
        if "enabled" in body:
            v = 1 if body["enabled"] else 0
            if v != int(row["enabled"]):
                changes["enabled"] = v
        if "aliasOf" in body:
            target = body.get("aliasOf")
            if target in (None, ""):
                if row["alias_of"] is not None:
                    changes["alias_of"] = None
            else:
                target = _int_or_400(target, "대표 거래처 ID")
                if target == sid:
                    abort(400, description="자기 자신을 대표로 지정할 수 없습니다.")
                rep = conn.execute("SELECT id, alias_of FROM suppliers WHERE id=?", (target,)).fetchone()
                if rep is None:
                    abort(400, description="존재하지 않는 대표 거래처입니다.")
                if rep["alias_of"] is not None:
                    abort(400, description="별칭인 거래처를 대표로 지정할 수 없습니다. 그 대표를 고르세요.")
                if target != row["alias_of"]:
                    changes["alias_of"] = target
        if not changes:
            abort(400, description="변경할 항목이 없습니다.")
        sets = dict(changes)
        sets.update(updated_at=ts, ows_edited_at=ts, ows_edited_by=_actor())
        conn.execute("UPDATE suppliers SET " + ", ".join(f"{c}=?" for c in sets) + " WHERE id=?",
                     list(sets.values()) + [sid])
        if "alias_of" in changes and changes["alias_of"] is not None:
            # 이 행을 대표로 삼던 별칭들도 새 대표로 따라간다(별칭의 별칭 금지)
            conn.execute("UPDATE suppliers SET alias_of=? WHERE alias_of=?", (changes["alias_of"], sid))
        audit.log("supplier_updated", target=row["name"],
                  detail={k: (bool(v) if k == "enabled" else v) for k, v in changes.items() if k != "norm_name"})
    return jsonify({"ok": True})


@bp.post("/suppliers/<int:sid>/canonical")
def supplier_canonical(sid):
    """대표 거래처 지정 — {aliasIds:[...], detachIds:[...]}. 전표·자산의 supplier_id 는 그대로 둔다(표시만 대표로)."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    alias_ids = [_int_or_400(x, "거래처 ID") for x in (body.get("aliasIds") or [])]
    detach_ids = [_int_or_400(x, "거래처 ID") for x in (body.get("detachIds") or [])]
    ts = config.now_iso()
    with tx(write=True) as conn:
        rep = conn.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
        if rep is None:
            abort(404, description="거래처를 찾을 수 없습니다.")
        if rep["alias_of"] is not None and alias_ids:
            abort(400, description="이 거래처는 다른 거래처의 별칭입니다 — 먼저 별칭을 풀거나 그 대표를 고르세요.")
        done, freed = [], []
        for aid in alias_ids:
            if aid == sid:
                continue
            if conn.execute("SELECT id FROM suppliers WHERE id=?", (aid,)).fetchone() is None:
                abort(400, description=f"존재하지 않는 거래처입니다: {aid}")
            conn.execute("UPDATE suppliers SET alias_of=?, updated_at=?, ows_edited_at=?, ows_edited_by=? WHERE id=?",
                         (sid, ts, ts, _actor(), aid))
            conn.execute("UPDATE suppliers SET alias_of=? WHERE alias_of=?", (sid, aid))
            done.append(aid)
        for aid in detach_ids:
            cur = conn.execute("UPDATE suppliers SET alias_of=NULL, updated_at=?, ows_edited_at=?, ows_edited_by=? "
                               "WHERE id=? AND alias_of=?", (ts, ts, _actor(), aid, sid))
            if cur.rowcount:
                freed.append(aid)
        if done:
            conn.execute("UPDATE suppliers SET alias_of=NULL WHERE id=?", (sid,))   # 대표는 별칭이 아니다
        if not done and not freed:
            abort(400, description="지정하거나 풀 거래처가 없습니다.")
        audit.log("supplier_canonical", target=rep["name"], detail={"aliasIds": done, "detachIds": freed})
    return jsonify({"ok": True, "id": sid, "aliasIds": done, "detachIds": freed})


@bp.post("/suppliers/<int:sid>/tms-primary")
def supplier_tms_primary(sid):
    """같은 이름의 TMS 신원이 여럿일 때 대표 신원(키ID)을 사람이 고른다 — 칸 값은 건드리지 않는다."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    key = _int_or_400(body.get("keyId"), "키ID")
    ts = config.now_iso()
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
        if row is None:
            abort(404, description="거래처를 찾을 수 없습니다.")
        link = conn.execute("SELECT * FROM supplier_tms_links WHERE tms_key_id=? AND supplier_id=?", (key, sid)).fetchone()
        if link is None:
            abort(400, description="이 거래처에 연결된 TMS 신원이 아닙니다.")
        conn.execute("UPDATE supplier_tms_links SET is_primary=0 WHERE supplier_id=?", (sid,))
        conn.execute("UPDATE supplier_tms_links SET is_primary=1 WHERE tms_key_id=?", (key,))
        # ★ows_edited_at 은 찍지 않는다 — 값을 고친 게 아니라 '어느 TMS 신원이 정본인지'를 고른 것이다.
        #   고른 신원의 값을 바로 반영한다(사람 행이면 빈 칸만 — 연동 규칙 그대로).
        conn.execute("UPDATE suppliers SET tms_key_id=?, tms_deleted_at=?, updated_at=? WHERE id=?",
                     (key, link["deleted_at"] or "", ts, sid))
        try:
            payload = json.loads(link["payload"]) if link["payload"] else {}
        except ValueError:
            payload = {}
        if payload:
            sup = conn.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
            _apply_partner_to_supplier(conn, sup, "", _partner_fields(payload), ts,
                                       {"updated": 0, "protected": 0, "restored": 0, "notes": []})
        audit.log("supplier_tms_primary", target=row["name"], detail={"from": row["tms_key_id"], "to": key})
    return jsonify({"ok": True, "id": sid, "tmsKeyId": key})


@bp.post("/suppliers/<int:sid>/tms-split")
def supplier_tms_split(sid):
    """부 신원을 사람이 정한 이름의 별도 거래처로 분리한다 — {keyId, name}. 이름은 시스템이 만들지 않는다."""
    require("purchase.edit")
    body = request.get_json(silent=True) or {}
    key = _int_or_400(body.get("keyId"), "키ID")
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 100:
        abort(400, description="새 거래처 이름을 1~100자로 입력하세요.")
    ts = config.now_iso()
    with tx(write=True) as conn:
        row = conn.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
        if row is None:
            abort(404, description="거래처를 찾을 수 없습니다.")
        link = conn.execute("SELECT * FROM supplier_tms_links WHERE tms_key_id=? AND supplier_id=?", (key, sid)).fetchone()
        if link is None:
            abort(400, description="이 거래처에 연결된 TMS 신원이 아닙니다.")
        if link["is_primary"]:
            abort(400, description="대표 신원은 분리할 수 없습니다. 먼저 다른 신원을 대표로 지정하세요.")
        if conn.execute("SELECT id FROM suppliers WHERE name=?", (name,)).fetchone():
            abort(409, description="이미 존재하는 거래처 이름입니다.")
        try:
            payload = json.loads(link["payload"]) if link["payload"] else {}
        except ValueError:
            payload = {}
        f = _partner_fields(payload) if payload else {"kind": link["kind"] or DEFAULT_KIND, "code": link["code"], "enabled": 1}
        kind = f.get("kind") if f.get("kind") in KINDS else DEFAULT_KIND
        cols = {"name": name, "norm_name": norm_key(name), "kind": kind, "source": "tms", "enabled": f.get("enabled", 1),
                "tms_key_id": key, "tms_deleted_at": link["deleted_at"] or "", "created_at": ts, "updated_at": ts}
        for col in SUPPLIER_FILL_COLS:
            cols[col] = f.get(col, "") or ""
        cur = conn.execute(f"INSERT INTO suppliers({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                           list(cols.values()))
        new_id = cur.lastrowid
        conn.execute("UPDATE supplier_tms_links SET supplier_id=?, is_primary=1 WHERE tms_key_id=?", (new_id, key))
        audit.log("supplier_tms_split", target=row["name"], detail={"keyId": key, "newId": new_id, "newName": name})
        new = conn.execute("SELECT * FROM suppliers WHERE id=?", (new_id,)).fetchone()
    return jsonify(_supplier_payload(new)), 201


# ---------------------------------------------------------------- 거래처 연동
def _partner_fields(it):
    return {"kind": _s(it, "거래처구분"), "code": _s(it, "거래처코드"), "biz_no": _s(it, "사업자등록번호"),
            "ceo": _s(it, "대표"), "phone": _s(it, "전화번호"), "fax": _s(it, "FAX"), "email": _s(it, "이메일"),
            "address": _s(it, "주소"), "address_detail": _s(it, "상세주소"), "zip": _s(it, "우편번호"),
            "dept": _s(it, "부서"), "memo": _s(it, "거래처메모"),
            "enabled": 1 if _truthy(it.get("사용여부", 1)) else 0}


def _apply_partner_to_supplier(conn, sup, name, f, ts, res):
    """대표 신원의 값을 거래처 행에 반영 — 사람 행은 빈 칸만, 연동 행은 따라간다."""
    protected = sup["source"] == "ows" or bool(sup["ows_edited_at"])
    sets = {}
    if protected:
        for col in SUPPLIER_FILL_COLS:
            if not (sup[col] or "").strip() and f[col]:
                sets[col] = f[col]
    else:
        for col in SUPPLIER_FILL_COLS + ("kind",):
            if (sup[col] or "") != f[col] and (col != "kind" or f[col] in KINDS):
                sets[col] = f[col]
        if int(sup["enabled"]) != f["enabled"]:
            sets["enabled"] = f["enabled"]
        if name and name != sup["name"]:
            if conn.execute("SELECT id FROM suppliers WHERE name=? AND id!=?", (name, sup["id"])).fetchone():
                res["notes"].append(f"이름 변경 건너뜀(충돌): {sup['name']} → {name}")
            else:
                sets["name"], sets["norm_name"] = name, norm_key(name)
    if sup["tms_deleted_at"]:
        sets["tms_deleted_at"] = ""
        res["restored"] += 1
    if sets:
        sets["updated_at"] = ts
        conn.execute("UPDATE suppliers SET " + ", ".join(f"{c}=?" for c in sets) + " WHERE id=?",
                     list(sets.values()) + [sup["id"]])
        res["protected" if protected else "updated"] += 1


def sync_partners(app, client):
    """업무 거래처(매입·판매·AS업체)를 창구에서 받아 키ID 기준으로 반영한다. 늘 전량(105행 안팎, 창구가 증분을 안 준다)."""
    data = client.get("/partners", kinds=",".join(KINDS))
    items = data.get("items") or []
    res = {"rows": len(items), "created": 0, "attached": 0, "updated": 0, "protected": 0, "dups": 0,
           "removed": 0, "restored": 0, "skipped": 0, "notes": []}
    with _ctx(app):
        ts = config.now_iso()
        with tx(write=True) as conn:
            links = {l["tms_key_id"]: l for l in conn.execute("SELECT * FROM supplier_tms_links").fetchall()}
            seen = set()
            for it in items:
                key = it.get("키ID")
                name = _s(it, "거래처명")
                f = _partner_fields(it)
                if key is None or not name or f["kind"] not in KINDS:
                    res["skipped"] += 1
                    continue
                key = int(key)
                seen.add(key)
                payload = json.dumps(it, ensure_ascii=False, default=str)
                link = links.get(key)
                if link is not None:
                    sup = conn.execute("SELECT * FROM suppliers WHERE id=?", (link["supplier_id"],)).fetchone()
                    if sup is None:                      # 거래처 행이 없어진 링크 — 다시 만든다
                        del links[key]
                        conn.execute("DELETE FROM supplier_tms_links WHERE tms_key_id=?", (key,))
                        link = None
                    else:
                        if link["is_primary"]:
                            _apply_partner_to_supplier(conn, sup, name, f, ts, res)
                        elif link["deleted_at"]:
                            res["restored"] += 1
                        conn.execute("UPDATE supplier_tms_links SET kind=?, code=?, name=?, payload=?, deleted_at='', seen_at=? "
                                     "WHERE tms_key_id=?", (f["kind"], f["code"], name, payload, ts, key))
                        continue
                # 처음 보는 키 — 이름이 같은 행에 붙인다(원문 → 정규화+구분 순)
                sup = conn.execute("SELECT * FROM suppliers WHERE name=?", (name,)).fetchone()
                if sup is None:
                    sup = conn.execute("SELECT * FROM suppliers WHERE norm_name=? AND kind=? AND tms_key_id IS NULL "
                                       "ORDER BY id LIMIT 1", (norm_key(name), f["kind"])).fetchone()
                if sup is not None and sup["tms_key_id"] in (None, key):
                    # 키가 없는 행에 붙인다(키만 있고 링크가 없는 행은 링크를 되살린다). 이름은 그대로(사람 표기 유지)
                    _apply_partner_to_supplier(conn, sup, "", f, ts, res)
                    conn.execute("UPDATE suppliers SET tms_key_id=?, updated_at=? WHERE id=?", (key, ts, sup["id"]))
                    conn.execute("INSERT INTO supplier_tms_links(tms_key_id, supplier_id, is_primary, kind, code, name, "
                                 "payload, seen_at, created_at) VALUES(?,?,1,?,?,?,?,?,?)",
                                 (key, sup["id"], f["kind"], f["code"], name, payload, ts, ts))
                    res["attached"] += 1
                elif sup is not None:
                    # 같은 이름에 이미 다른 TMS 신원이 붙어 있다 → 자동으로 합치지 않고 부 신원으로 남긴다
                    conn.execute("INSERT INTO supplier_tms_links(tms_key_id, supplier_id, is_primary, kind, code, name, "
                                 "payload, seen_at, created_at) VALUES(?,?,0,?,?,?,?,?,?)",
                                 (key, sup["id"], f["kind"], f["code"], name, payload, ts, ts))
                    res["dups"] += 1
                else:
                    cols = {"name": name, "norm_name": norm_key(name), "kind": f["kind"], "source": "tms",
                            "enabled": f["enabled"], "tms_key_id": key, "created_at": ts, "updated_at": ts}
                    for col in SUPPLIER_FILL_COLS:
                        cols[col] = f[col]
                    cur = conn.execute(f"INSERT INTO suppliers({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                                       list(cols.values()))
                    conn.execute("INSERT INTO supplier_tms_links(tms_key_id, supplier_id, is_primary, kind, code, name, "
                                 "payload, seen_at, created_at) VALUES(?,?,1,?,?,?,?,?,?)",
                                 (key, cur.lastrowid, f["kind"], f["code"], name, payload, ts, ts))
                    res["created"] += 1
            # 목록에서 빠진 신원 — 지우지 않고 표시만(빈 응답이면 아무것도 표시하지 않는다)
            if items:
                for key, link in links.items():
                    if key in seen or link["deleted_at"]:
                        continue
                    conn.execute("UPDATE supplier_tms_links SET deleted_at=? WHERE tms_key_id=?", (ts, key))
                    if link["is_primary"]:
                        conn.execute("UPDATE suppliers SET tms_deleted_at=?, updated_at=? WHERE id=? AND tms_deleted_at=''",
                                     (ts, ts, link["supplier_id"]))
                    res["removed"] += 1
            if any(res[k] for k in ("created", "attached", "updated", "dups", "removed", "restored")):
                audit.log("masters_partners", detail={k: v for k, v in res.items() if k != "notes"})
    res["notes"] = res["notes"][:10]
    app.logger.info("거래처 마스터 연동 — 행 %d / 신규 %d / 연결 %d / 갱신 %d / 보호 %d / 중복후보 %d / 이탈 %d",
                    res["rows"], res["created"], res["attached"], res["updated"], res["protected"], res["dups"], res["removed"])
    return res


def sync_masters(app, client, state, full=False):
    """tms_link.sync_once 가 화면 반영 뒤에 부른다. 커서는 state['masters_cursor'](tms_link 상태 파일에 같이 저장)."""
    out = {"at": config.now_iso()}
    since = None if (full or not state.get("masters_cursor")) else _minus_seconds(state["masters_cursor"], OVERLAP_SECONDS)
    try:
        out["models"] = sync_models(app, client, since)
        if out["models"].get("cursor"):
            state["masters_cursor"] = out["models"]["cursor"]
    except Exception as e:                                       # noqa: BLE001
        app.logger.exception("모델 마스터 연동 오류")
        out["models"] = {"error": str(e)[:200]}
    try:
        out["partners"] = sync_partners(app, client)
    except Exception as e:                                       # noqa: BLE001
        app.logger.exception("거래처 마스터 연동 오류")
        out["partners"] = {"error": str(e)[:200]}
    _last_masters.clear()
    _last_masters.update(out)
    return out


@bp.get("/masters/status")
def masters_status():
    require("purchase.view")
    conn = get_db()
    cnt = {
        "models": conn.execute("SELECT COUNT(*) AS c FROM models").fetchone()["c"],
        "modelsTms": conn.execute("SELECT COUNT(*) AS c FROM models WHERE source='tms'").fetchone()["c"],
        "modelsOws": conn.execute("SELECT COUNT(*) AS c FROM models WHERE source='ows'").fetchone()["c"],
        "suppliers": conn.execute("SELECT COUNT(*) AS c FROM suppliers").fetchone()["c"],
        "suppliersLinked": conn.execute("SELECT COUNT(*) AS c FROM suppliers WHERE tms_key_id IS NOT NULL").fetchone()["c"],
        "supplierDupLinks": conn.execute("SELECT COUNT(*) AS c FROM supplier_tms_links WHERE is_primary=0").fetchone()["c"],
    }
    from .tms_link import _read_state, load_config
    cfg = load_config()
    return jsonify({"counts": cnt, "last": _last_masters, "cursor": _read_state().get("masters_cursor"),
                    "linkEnabled": cfg["enabled"]})


@bp.post("/masters/sync")
def masters_sync_now():
    """마스터만 지금 연동(?full=1 이면 모델 전량 재대사). 창구 설정이 없으면 400."""
    require("purchase.edit")
    from flask import current_app
    from .tms_link import Client, _read_state, _write_state, load_config
    cfg = load_config()
    if not cfg["enabled"]:
        abort(400, description="연동 창구 설정이 없습니다(설정 ▸ 데이터 연동).")
    full = (request.args.get("full") or "").strip() == "1"
    state = _read_state()
    out = sync_masters(current_app._get_current_object(), Client(cfg["url"], cfg["token"]), state, full=full)
    _write_state(state)
    return jsonify({"ok": True, "result": out})


# ---------------------------------------------------------------- 옛 라우트 교체
def _replace_view(endpoint, func):
    """purchase/__init__.py 의 옛 뷰를 같은 주소에서 이 파일의 뷰로 바꿔 끼운다(옛 코드 무접촉).

    블루프린트가 앱에 등록될 때 add_url_rule 이 먼저 옛 함수를 매핑하고, 그 뒤에 이 기록이 실행되어
    view_functions['purchase.<endpoint>'] 만 새 함수로 바꾼다. 주소·메서드·엔드포인트 이름은 그대로다.
    """
    bp.record(lambda st: st.app.view_functions.__setitem__(
        f"{st.name_prefix}.{st.name}.{endpoint}".lstrip("."), func))


_replace_view("list_suppliers", list_suppliers)
_replace_view("create_supplier", create_supplier)
_replace_view("update_supplier", update_supplier)
