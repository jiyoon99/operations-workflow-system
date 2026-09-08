"""쇼핑몰 재고 동기화 — OWS의 '재고반영' 자산 수를 몰별 상품 재고로 밀어넣는다.

개념(대표 확정 2026-08-04):
  제품코드(예: 840 G3_i7-6_내장) = 쇼핑몰 재고의 축. 대표가 매입 자산에 직접 기입한다.
  자산번호 = 개체 하나(셋팅·QC 출고의 축). 서로 다른 역할이다.

OWS 기준 재고 = 제품코드가 같고 + 재고반영(stock_listed=1) + 판매가능 + 입고완료인 자산 수.
  ★stock_listed 가 핵심이다. 매입 등록만 하면 0(꺼짐)이라 아무것도 몰에 잡히지 않고,
    수리를 다녀온 물건을 사람이 [재고반영]을 체크한 순간부터 세어진다.

안전 원칙:
  ① 몰별 토글 기본 OFF — 설정에 명시적으로 켜기 전에는 어떤 몰에도 아무것도 보내지 않는다.
     OFF인 몰은 '기존에 몰에 등록된 재고값'이 그대로 유지된다(OWS가 손대지 않는다).
  ② 외부 호출은 절대 tx(write=True) 안에서 하지 않는다(원칙 #1).
     보낼 목록을 먼저 계산해 두고, 트랜잭션 밖에서 몰을 부르고, 결과만 다시 기록한다.
  ③ 시험 모드(dry run) — 무엇을 보낼지 계산만 하고 실제로 보내지 않는 경로를 항상 제공한다.
  ④ 보내기 전의 몰 재고를 조회해 기록해 둔다 — 잘못 밀었을 때 되돌릴 근거.
  ⑤ 실패는 건별로 기록하고 다음 주기에 다시 시도한다. 전체를 멈추지 않는다.
"""
import json

from flask import abort, g, jsonify, request

from .. import audit, config
from ..auth.perms import require
from ..db import get_db, sale_only, tx
from ..settings import bp
from .base import get_adapter
from .collect import _mall_settings

# ★재고를 셀 때 포함하는 자산 상태 — 셋팅 화면(product_info.stock_by_code)과 같은 기준.
#   예전엔 여기만 ('in_stock','ready') 하드코딩이라 정비중(refurbishing)이 빠지고
#   가재고가 섞였다 — 주석은 "판매가능과 같다"였는데 실제로는 달랐다(2026-08-07 검증).
#   기준이 두 개면 몰 재고와 셋팅 재고가 서로 다른 숫자를 말하게 된다.
from ..purchase import AVAILABLE_STATUSES  # noqa: E402

_COUNT_SQL = (
    "SELECT product_code, COUNT(*) AS n FROM assets "
    "WHERE stock_listed=1 AND received=1 AND TRIM(product_code)<>'' "
    f"AND status IN ({','.join('?' * len(AVAILABLE_STATUSES))}) "
    "AND tier <> '가재고'"
    # ★division이 빠지면 렌탈 사업부 자산이 쇼핑몰 재고로 올라간다.
    #   바깥으로 나가는 유일한 경로라 가장 중요한 지점이다.
    + sale_only("") + " GROUP BY product_code"
)


def ows_stock(conn):
    """제품코드별 OWS 기준 재고 수량."""
    return {r["product_code"]: r["n"]
            for r in conn.execute(_COUNT_SQL, list(AVAILABLE_STATUSES)).fetchall()}


def _sync_conf(conn):
    """몰별 재고 동기화 설정. ★없으면 전부 꺼진 것으로 본다(기본 OFF)."""
    row = conn.execute("SELECT value FROM settings WHERE key='stock_sync'").fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["value"]) or {}
    except ValueError:
        return {}


def _is_on(conf, mall):
    """켜짐 판정은 명시적 True 하나뿐이다 — 빈 값·문자열·1 도 켜짐으로 보지 않는다.

    실수로 켜지는 경로를 없애기 위한 엄격한 판정(대표: 기본값은 무조건 OFF).
    """
    return conf.get(mall, {}).get("enabled") is True


@bp.get("/stock-sync")
def stock_sync_status():
    """설정 화면 — 몰별 토글 상태와 OWS 기준 재고 미리보기."""
    require("settings.manage")
    conn = get_db()
    conf = _sync_conf(conn)
    stock = ows_stock(conn)
    from . import MALLS
    malls = [{
        "code": m["code"], "name": m["name"],
        "enabled": _is_on(conf, m["code"]),
        # 어댑터 구현 여부 — 아직 안 만든 몰은 화면에서 '준비 중'으로 표시
        "supported": m["code"] in _PUSHERS,
    } for m in MALLS]
    return jsonify({
        "malls": malls,
        "codes": [{"productCode": k, "count": v} for k, v in sorted(stock.items())],
        "totalCodes": len(stock), "totalUnits": sum(stock.values()),
    })


@bp.put("/stock-sync/<mall>")
def stock_sync_toggle(mall):
    """몰 하나의 재고 동기화를 켜거나 끈다. 켤 때는 명시적 enabled=true 여야 한다."""
    require("settings.manage")
    from . import MALLS
    if mall not in {m["code"] for m in MALLS}:
        abort(404, description="알 수 없는 쇼핑몰입니다.")
    body = request.get_json(silent=True) or {}
    want = body.get("enabled") is True
    if want and mall not in _PUSHERS:
        abort(400, description="이 쇼핑몰은 아직 재고 전송이 준비되지 않았습니다.")
    with tx(write=True) as conn:
        conf = _sync_conf(conn)
        conf.setdefault(mall, {})["enabled"] = want
        conn.execute(
            "INSERT INTO settings(key, value, updated_at, updated_by) VALUES('stock_sync', ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (json.dumps(conf, ensure_ascii=False), config.now_iso(), g.user["display_name"]))
        audit.log("stock_sync_toggle", target=mall, detail={"enabled": want})
    return jsonify({"mall": mall, "enabled": want})


@bp.post("/stock-sync/preview")
def stock_sync_preview():
    """시험 실행(dry run) — 어느 몰에 어떤 코드·수량을 보낼지 계산만 한다. 아무것도 안 보낸다."""
    require("settings.manage")
    conn = get_db()
    conf = _sync_conf(conn)
    stock = ows_stock(conn)
    from . import MALLS
    out = []
    for m in MALLS:
        code = m["code"]
        if not _is_on(conf, code):
            out.append({"mall": code, "name": m["name"], "enabled": False, "items": []})
            continue
        out.append({"mall": code, "name": m["name"], "enabled": True,
                    "items": [{"productCode": k, "count": v} for k, v in sorted(stock.items())]})
    return jsonify({"preview": out, "note": "시험 계산입니다 — 아무것도 전송하지 않았습니다."})


# ---------------------------------------------------------------------------
# 몰별 전송기 — 실제로 보낼 수 있는 몰만 여기 등록한다. 등록 안 된 몰은
# 토글이 '준비 중'으로 막힌다(켜기 자체가 안 됨 — 기본 OFF 보장).
#
# ★2026-08-04 타당성 조사(67에이전트, 공개 스펙 원문 확인) 결과, 지금은 어느 몰도
#   실제 전송을 붙일 수 없다. 몰 기능이 없어서가 아니라 전제조건이 미해결이라서다:
#
#   고도몰  — 재고변경 API(Goods_Stock.php)는 '수량'을 직접 받지 않는다.
#             data_url(우리가 XML을 올려 둔, 인터넷에서 접근 가능한 주소) 하나만 받고
#             고도몰 서버가 그 주소로 파일을 가지러 온다(스펙 정의서 3.5, p.28).
#             OWS는 사내 PC라 외부 접속 주소가 없다 → 대표 결정 필요.
#             또 옵션 상품은 sno·stockCnt·optionViewFl·optionSellFl·optionPrice 전부
#             필수라, 먼저 상품조회로 현재 옵션값을 읽어 보관한 뒤 그대로 되돌려 보내야
#             등급별 추가금액(-30,000원 등)이 날아가지 않는다.
#   쿠팡    — 수량만 바꾸는 안전한 API(PUT .../vendor-items/{id}/quantities/{n})가
#             있으나 키가 vendorItemId(옵션ID)다. OWS는 이 번호를 저장한 적이 없고,
#             확보하려면 쿠팡 상품조회 API 3종을 새로 붙여 대조표부터 만들어야 한다.
#   스마트스토어·카카오 — 재고만 바꾸는 API가 없다. 상품 전체 덮어쓰기라
#             빠뜨린 항목(상세설명·태그·옵션가)이 삭제된다 → 당분간 제외.
#   ESM     — 옵션 상품은 본품 재고 입력이 무시되고, 재고 0을 보낼 수 없다(1~99,999).
#   11번가·롯데온·테무 — 키/IP 미비로 규격 확인조차 불가.
# ---------------------------------------------------------------------------

_PUSHERS = {}


@bp.post("/stock-sync/run")
def stock_sync_run():
    """켜진 몰에 OWS 기준 재고를 실제로 보낸다(수동 실행 버튼).

    ★원칙 #1 — 외부 호출은 트랜잭션 밖에서. 계산(읽기) → 전송(외부) → 기록(쓰기) 순서.
    body.dry=true 면 전송 없이 결과만 보여준다.
    """
    require("settings.manage")
    body = request.get_json(silent=True) or {}
    dry = bool(body.get("dry"))
    only = (body.get("mall") or "").strip()          # 특정 몰만 보낼 때

    conn = get_db()
    conf = _sync_conf(conn)
    stock = ows_stock(conn)
    items = [{"productCode": k, "count": v} for k, v in sorted(stock.items())]

    from . import MALLS
    report = []
    for m in MALLS:
        code = m["code"]
        if only and code != only:
            continue
        if not _is_on(conf, code):
            continue                                  # OFF — 이 몰은 아무것도 건드리지 않는다
        pusher = _PUSHERS.get(code)
        if pusher is None:
            report.append({"mall": code, "error": "전송기가 준비되지 않았습니다.", "items": []})
            continue
        settings = _mall_settings(conn, code)
        adapter, why = get_adapter(code, settings)
        if adapter is None:
            report.append({"mall": code, "error": why, "items": []})
            continue
        # ▼ 외부 호출 — 트랜잭션 밖
        results = pusher(adapter, items, dry=dry)
        report.append({"mall": code, "error": "", "items": results})

    # 결과 기록(쓰기 트랜잭션은 외부 호출이 끝난 뒤에만)
    sent = sum(1 for r in report for x in r["items"] if x.get("ok"))
    failed = sum(1 for r in report for x in r["items"] if not x.get("ok"))
    with tx(write=True) as wconn:
        audit.log("stock_sync_run", target=("시험" if dry else "실행"),
                  detail={"sent": sent, "failed": failed,
                          "malls": [r["mall"] for r in report]})
        wconn.execute(
            "INSERT INTO settings(key, value, updated_at, updated_by) "
            "VALUES('stock_sync_last', ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (json.dumps({"at": config.now_iso(), "dry": dry, "report": report},
                        ensure_ascii=False), config.now_iso(), g.user["display_name"]))
    return jsonify({"dry": dry, "sent": sent, "failed": failed, "report": report})
