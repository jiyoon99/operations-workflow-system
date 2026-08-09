"""HMS 권한 체계.

고정 역할 대신 기능 단위 권한 토글(대표가 설정 화면에서 사용자별로 부여).
관리자(is_admin)는 전 권한 보유. 기존 order-workflow 역할 6종은 폐기됨(2026-07-28 결정).
"""
from flask import abort, g

from ..db import get_db

# 메뉴(좌측 탭) 단위로 접근을 켜고 끈다. 메뉴 권한이 없으면 그 메뉴는 아예 보이지 않는다.
# 각 메뉴 안에서 "무엇까지 할 수 있는지"는 하위 권한으로 나눈다.
MENUS = [
    {
        "menu": "purchase", "label": "매입", "view": "purchase.view",
        "desc": "거래처·가입고·매입 전표·자산(재고) 관리",
        "perms": [("purchase.edit", "등록·수정 (읽기만 하려면 끄기)")],
    },
    {
        "menu": "orders", "label": "주문관리", "view": "orders.view",
        "desc": "쇼핑몰·수기 주문 조회 및 관리",
        "perms": [
            ("orders.edit", "주문 등록·수정"),
            ("orders.import", "주문 가져오기(엑셀/API)"),
            ("orders.cancel", "주문 취소·복구"),
        ],
    },
    {
        # 메뉴마다 고유한 접근 권한을 준다 — 주문 권한만 줬는데 셋팅·배송까지 열리면 안 된다
        "menu": "setup", "label": "셋팅", "view": "setup.view",
        "desc": "제품 준비·QC 작업보드 (담당자 체크)",
        "perms": [("orders.work", "준비/QC 체크·자산 매칭")],
    },
    {
        "menu": "shipping", "label": "배송 / 송장", "view": "shipping.view",
        "desc": "송장 출력·출고 확인",
        "perms": [
            ("orders.ship", "송장 발급·출고 확정"),
            ("waybills.manage", "송장 관리(취소·재발행)"),
        ],
    },
    {
        "menu": "as", "label": "A/S", "view": "as.view",
        "desc": "A/S 접수·처리 (준비 중)",
        "perms": [("as.manage", "A/S 처리")],
    },
    {
        "menu": "reports", "label": "리포트", "view": "reports.view",
        "desc": "매입·판매·마진, 담당자 실적, 재고 체류",
        "perms": [],
    },
    {
        "menu": "settings", "label": "설정", "view": "settings.view",
        "desc": "사용자·API·백업 등 시스템 관리",
        "perms": [
            ("settings.manage", "시스템 설정·API 키 관리"),
            ("users.manage", "사용자·권한 관리"),
            ("audit.view", "감사 로그 조회"),
        ],
    },
]

# 메뉴 접근 권한(view) + 하위 권한을 모두 모은 것
PERM_DEFS = []
for _m in MENUS:
    if not any(_m["view"] == c for c, _, _ in PERM_DEFS):
        PERM_DEFS.append((_m["view"], f"{_m['label']} 메뉴 접근", _m["label"]))
    for _code, _label in _m["perms"]:
        PERM_DEFS.append((_code, _label, _m["label"]))

PERM_CODES = {c for c, _, _ in PERM_DEFS}
PERM_LABELS = {c: label for c, label, _ in PERM_DEFS}

# 메뉴 접근 권한만 모은 집합(프론트 내비게이션 표시용)
MENU_VIEW_PERMS = {m["menu"]: m["view"] for m in MENUS}

# 주문 데이터를 읽는 화면들 — 이 중 하나라도 있으면 /api/orders 조회를 허용한다
ORDER_READ_PERMS = ("orders.view", "setup.view", "shipping.view")


def registry():
    """메뉴 구조 그대로 반환 — 설정 화면이 메뉴별 카드로 그린다."""
    return {
        "menus": [
            {
                "menu": m["menu"], "label": m["label"], "desc": m["desc"],
                "view": m["view"],
                "perms": [{"code": c, "label": label} for c, label in m["perms"]],
            }
            for m in MENUS
        ],
        "flat": [{"code": c, "label": label, "group": grp} for c, label, grp in PERM_DEFS],
    }


def user_perm_set(user_id) -> set:
    rows = get_db().execute("SELECT perm FROM user_perms WHERE user_id=?", (user_id,)).fetchall()
    return {r["perm"] for r in rows if r["perm"] in PERM_CODES}


def user_category_ids(user_id) -> list:
    rows = get_db().execute(
        "SELECT category_id FROM user_categories WHERE user_id=? ORDER BY category_id", (user_id,)
    ).fetchall()
    return [r["category_id"] for r in rows]


def require(perm):
    """핸들러 내부에서 호출하는 권한 게이트. 미보유 시 403."""
    if perm not in PERM_CODES:
        raise ValueError(f"unknown perm: {perm}")
    if g.user["is_admin"]:
        return
    if perm not in g.perms:
        abort(403, description=f"'{PERM_LABELS[perm]}' 권한이 없습니다. 관리자에게 문의하세요.")


def require_any(*perms):
    """나열된 권한 중 하나라도 있으면 통과."""
    unknown = [p for p in perms if p not in PERM_CODES]
    if unknown:
        raise ValueError(f"unknown perms: {unknown}")
    if g.user["is_admin"]:
        return
    if not g.perms.intersection(perms):
        labels = " / ".join(PERM_LABELS[p] for p in perms)
        abort(403, description=f"'{labels}' 중 하나의 권한이 필요합니다.")
