"""사업부 귀속 최초 백필 — 렌탈(RMS) 자산을 OWS 판매재고에서 걷어낸다.

대표 규칙 (2026-08-12 확정)
  "RMS 내에 있는 자산이 OWS에서도 보일텐데 이건 전부 일단 OWS에서 렌탈 자산으로 보이게"

  R1  RMS에 살아있는 자산(휴지통 제외)  → rental   ★상태 불문. 대여중이든 창고에 있든 렌탈 것이다.
  R2  TMS 재고상태 ∈ (렌탈, 반납)        → rental   (RMS DB에 아직 없더라도)
  R3  예외 13건                          → rental + 잠금 + 사유 보존 (2026-08-04 대표 확인)
  제외  자산이 아닌 것 / 관리번호 중복(어느 실물인지 특정 불가 — 데이터는 그대로 둔다)

★상태값(status)은 건드리지 않는다. shipped는 "OWS 손에 없다"는 뜻이라
  렌탈로 나가 있는 물건에도 맞는 상태다. in_stock으로 '되돌리면' 오히려
  판매가능 재고가 늘어난다(2026-08-04 결정 6).

사용:
  python scripts/backfill_division.py --db <경로>          (미리보기 — 아무것도 안 씀)
  python scripts/backfill_division.py --db <경로> --apply  (적용)
"""
import argparse
import collections
import importlib.util
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RMS_DB = os.getenv("RMS_DB", r"E:\rental-system\rental_system.db")
TMS_XLSX = os.getenv("TMS_STOCK_XLSX", str(ROOT / "tms-export" / "재고항목현황.xlsx"))

TMS_RENTAL = {"렌탈", "반납"}

# 결정 1(2026-08-04) — TMS 관리번호 오배정. 같은 판매전표에 여러 사람이 서로 다른
# 자산번호로 붙어 있어 정상 판매일 수 없다. 렌탈로 확정하고 잠근다.
EXCEPTION_13 = [
    "241216-0016", "250102-0001", "250102-0002", "250102-0003", "250102-0004",
    "250102-0005", "250107-0072", "250116-0016", "250121-0047", "250225-0027",
    "250314-0021", "250509-0044", "250925-0001",
]
EXCEPTION_NOTE = ("TMS 관리번호 오배정 — 같은 판매전표에 여러 사람이 서로 다른 자산번호로 붙어 있음. "
                  "OWS 판매전표는 무시하고 렌탈로 확정(2026-08-04 대표 확인). 전표 기록은 보존.")

NOT_ASSETS = {"테스트", "2026-0031"}          # 시험용 더미 / 자산 실체 없음
KST = timezone(timedelta(hours=9))


def load_tms():
    """TMS 재고항목현황 엑셀 → {관리번호: 재고상태}. 파일이 없으면 빈 dict."""
    if not Path(TMS_XLSX).exists():
        print(f"  ※ TMS 파일 없음({TMS_XLSX}) — R2 없이 진행합니다.")
        return {}
    spec = importlib.util.spec_from_file_location(
        "ows_excel", str(ROOT / "app" / "importers" / "excel.py"))
    xl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(xl)
    rows = xl.read_first_sheet(Path(TMS_XLSX).read_bytes())
    mcol = next(c for c in rows[0] if "관리번호" in c)
    scol = next(c for c in rows[0] if "재고상태" in c)
    return {(r.get(mcol) or "").strip(): (r.get(scol) or "").strip()
            for r in rows if (r.get(mcol) or "").strip()}


def load_rms():
    """관리번호 → RMS 자산 목록. 휴지통(deleted)은 제외한다."""
    if not Path(RMS_DB).exists():
        print(f"  ※ RMS DB 없음({RMS_DB}) — R1 없이 진행합니다.")
        return {}
    conn = sqlite3.connect(f"file:{RMS_DB}?mode=ro", uri=True)
    out = {}
    for _k, d in conn.execute("SELECT key, data FROM assets"):
        try:
            j = json.loads(d)
        except ValueError:
            continue
        if j.get("deleted"):
            continue
        no = (j.get("mgmt_no") or "").strip()
        if no:
            out.setdefault(no, []).append(j)
    conn.close()
    return out


def decide(tms, rms, ows_nos):
    """관리번호 → (division, locked, note, 적용규칙). rental로 판정된 것만 돌려준다."""
    out = {}
    for no in ows_nos:
        if no in NOT_ASSETS:
            continue
        # ★중복 등록된 관리번호는 어느 실물인지 특정할 수 없다 → 건드리지 않는다.
        #   대표 지시: "중복등록 데이터로 남겨주면 자산번호를 바꾸면 되니 그대로 남겨줘."
        if len(rms.get(no, [])) > 1:
            continue
        if no in EXCEPTION_13:
            out[no] = ("rental", 1, EXCEPTION_NOTE, "R3 예외(잠금)")
        elif no in rms:
            out[no] = ("rental", 0, "", "R1 RMS 보유")
        elif tms.get(no) in TMS_RENTAL:
            out[no] = ("rental", 0, "", "R2 TMS 렌탈/반납")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    live = Path(r"H:\OWS\operations-system\data\ows.db")
    if live.exists() and Path(args.db).resolve() == live.resolve() and args.apply:
        sys.exit("★중단★ 라이브 DB에는 직접 쓰지 않습니다. 사본에 적용하고 화면/API로 반영하세요.")

    tms, rms = load_tms(), load_rms()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    ows = {r["asset_no"]: r for r in conn.execute(
        "SELECT id, asset_no, status, division, division_locked, stock_listed FROM assets")}

    plan = decide(tms, rms, list(ows))
    SELL = ("in_stock", "ready")
    sell_before = sum(1 for r in ows.values() if r["status"] in SELL)
    drop = sum(1 for no in plan if ows[no]["status"] in SELL)
    listed = [no for no in plan if ows[no]["stock_listed"]]
    changing = [no for no, (d, lk, _n, _r) in plan.items()
                if ows[no]["division"] != d or (lk and not ows[no]["division_locked"])]

    print("=" * 66)
    print(f"  대상 DB : {args.db}")
    print(f"  모드    : {'★적용(APPLY)★' if args.apply else '미리보기 — 아무것도 쓰지 않음'}")
    print("=" * 66)
    print(f"\n[판정] rental {len(plan):,}건 / 실제 변경 {len(changing):,}건")
    for r, n in sorted(collections.Counter(v[3] for v in plan.values()).items()):
        print(f"   {r:22s} {n:>6,}")
    print(f"\n[제외] 자산 아님 {sorted(NOT_ASSETS)}")
    print(f"[제외] 관리번호 중복 {sum(1 for v in rms.values() if len(v) > 1)}건 — 데이터 그대로 둠")
    print(f"\n[영향] 판매가능(in_stock+ready)  {sell_before:,} → {sell_before - drop:,}  (-{drop:,})")
    print(f"       몰 재고반영 중인 렌탈 자산  {len(listed)}건 {listed[:6]}")
    print(f"\n[rental 확정분의 현재 status]")
    for k, v in collections.Counter(ows[no]["status"] for no in plan).most_common():
        print(f"   {k:12s} {v:>6,}")

    if not args.apply:
        print("\n※ --apply 를 붙이면 실제로 씁니다. 지금은 아무것도 바꾸지 않았습니다.")
        return

    ts = datetime.now(KST).isoformat(timespec="seconds")
    n = 0
    conn.execute("BEGIN IMMEDIATE")
    for no, (d, locked, note, rule) in plan.items():
        a = ows[no]
        # ★이미 잠긴 자산은 자동 판정이 건드리지 않는다(사람 판단이 위).
        if a["division_locked"] and not locked:
            continue
        conn.execute(
            "UPDATE assets SET division=?, division_locked=?, division_note=?, "
            " division_since=?, division_by=?, division_ref='BACKFILL-20260812', "
            " stock_listed=0, stock_listed_at='', stock_listed_by='', updated_at=? WHERE id=?",
            (d, locked, note, ts, "백필(대표 규칙 2026-08-12)", ts, a["id"]))
        conn.execute(
            "INSERT INTO asset_events(asset_id, ts, action, actor, detail) VALUES(?,?,?,?,?)",
            (a["id"], ts, "사업부 귀속 백필", "시스템",
             json.dumps({"사업부": d, "규칙": rule, "이전": a["division"],
                         "잠금": bool(locked), "사유": note}, ensure_ascii=False)))
        n += 1
    conn.commit()
    after = sum(1 for r in conn.execute(
        "SELECT status FROM assets WHERE division='sale'") if r["status"] in SELL)
    print(f"\n★적용 완료: {n:,}건")
    print(f"  판매가능 재고 실측: {sell_before:,} → {after:,}")
    conn.close()


if __name__ == "__main__":
    main()
