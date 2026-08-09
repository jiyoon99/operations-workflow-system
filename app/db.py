"""SQLite(WAL) 접근 계층.

원칙(개발계획서 §2):
- 단일 DB 원본, 레코드 단위 upsert — "전체 읽고 전체 덮어쓰기" 금지
- 쓰기는 tx(write=True) 트랜잭션 경유(BEGIN IMMEDIATE)
- 쓰기 전 일별 백업 보장 + 보존기간 초과분 정리
"""
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from flask import current_app, g

from . import config

_backup_lock = threading.Lock()
_SCHEMA = Path(__file__).with_name("schema.sql")


def _connect(db_path):
    conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


# 기존 DB에 나중에 추가된 컬럼들 — 스키마 파일의 CREATE TABLE IF NOT EXISTS는
# 이미 있는 테이블을 바꾸지 않으므로, 여기서 ALTER TABLE로 채운다.
_ADDED_COLUMNS = {
    "assets": [
        ("sale_price", "INTEGER NOT NULL DEFAULT 0"),
        ("location", "TEXT NOT NULL DEFAULT ''"),
        ("cpu", "TEXT NOT NULL DEFAULT ''"),
        ("gpu", "TEXT NOT NULL DEFAULT ''"),
        ("ram", "TEXT NOT NULL DEFAULT ''"),
        ("ssd", "TEXT NOT NULL DEFAULT ''"),
        ("inch", "TEXT NOT NULL DEFAULT ''"),
        ("battery", "TEXT NOT NULL DEFAULT ''"),
        ("charger", "TEXT NOT NULL DEFAULT ''"),
        ("received", "INTEGER NOT NULL DEFAULT 1"),      # 입고확인 완료 여부
        ("received_at", "TEXT NOT NULL DEFAULT ''"),
        ("received_by", "TEXT NOT NULL DEFAULT ''"),
        # 제품코드 = 쇼핑몰 재고의 축(예: 840 G3_i7-6_내장). 자산번호와는 다른 개념이다 —
        # 자산번호는 개체 하나(출고의 축), 제품코드는 같은 사양 묶음(재고의 축).
        # ★대표가 직접 기입한다 — 자동으로 채우지 않는다(2026-08-04 지시).
        ("product_code", "TEXT NOT NULL DEFAULT ''"),
        # 재고반영 여부. ★기본 0 — 제품코드를 넣었다고 바로 몰 재고에 잡히면 안 된다.
        # 수리를 다녀와서 올리는 경우가 있어, 사람이 체크했을 때만 재고로 센다(2026-08-04 지시).
        ("stock_listed", "INTEGER NOT NULL DEFAULT 0"),
        ("stock_listed_at", "TEXT NOT NULL DEFAULT ''"),
        ("stock_listed_by", "TEXT NOT NULL DEFAULT ''"),
        # 재고 3단계(2026-08-04 대표): 양품=바로 판매 가능 / 실재고=부분 수리·보수 후
        # 사용 가능 / 가재고=입고는 됐지만 완전한 수리가 끝나야 쓸 수 있는 상태.
        # ★기존 자산 15,013대는 '양품'으로 시작한다 — 지금까지처럼 출고에 제약이 없다.
        #   가재고만은 매입에서 양품/실재고로 바꾸기 전까지 출고가 막힌다.
        ("tier", "TEXT NOT NULL DEFAULT '양품'"),
    ],
    "purchase_batches": [
        ("slip_no", "TEXT NOT NULL DEFAULT ''"),
        ("stage", "TEXT NOT NULL DEFAULT 'purchased'"),
        ("purchase_type", "TEXT NOT NULL DEFAULT '일반매입'"),
        ("channel", "TEXT NOT NULL DEFAULT ''"),
        ("receive_method", "TEXT NOT NULL DEFAULT '택배'"),
        ("requester", "TEXT NOT NULL DEFAULT ''"),
        ("address", "TEXT NOT NULL DEFAULT ''"),
        ("vat", "INTEGER NOT NULL DEFAULT 0"),
        ("fee", "INTEGER NOT NULL DEFAULT 0"),
        ("shipping_fee", "INTEGER NOT NULL DEFAULT 0"),
        ("shipping_cod", "INTEGER NOT NULL DEFAULT 0"),
        ("tracking_no", "TEXT NOT NULL DEFAULT ''"),
        ("paid", "INTEGER NOT NULL DEFAULT 1"),
        ("confirmed_at", "TEXT NOT NULL DEFAULT ''"),
        ("confirmed_by", "TEXT NOT NULL DEFAULT ''"),
        ("cancelled_at", "TEXT NOT NULL DEFAULT ''"),     # 전표 취소(지우지 않고 표시)
        ("cancelled_by", "TEXT NOT NULL DEFAULT ''"),
        ("cancel_reason", "TEXT NOT NULL DEFAULT ''"),
        ("returned_at", "TEXT NOT NULL DEFAULT ''"),      # 거래처 반품일
        ("return_reason", "TEXT NOT NULL DEFAULT ''"),
        ("updated_at", "TEXT NOT NULL DEFAULT ''"),
        ("paid_amount", "INTEGER NOT NULL DEFAULT 0"),   # 실제 지급액(부분지급 가능)
        ("paid_at", "TEXT NOT NULL DEFAULT ''"),         # 지급일
        ("paid_memo", "TEXT NOT NULL DEFAULT ''"),
    ],
    "orders": [
        # ★수령방식 — 택배가 아닌 건이 있다(대표 2026-08-05: "방문수령이나 퀵 발송 건도 있다").
        #   빈 값 = 택배(예전 주문 전부). 택배가 아니면 송장이 필요 없고, 셋팅·배송 화면이
        #   '송장 발급 대기'로 잡으면 안 된다.
        ("receive_method", "TEXT NOT NULL DEFAULT ''"),
        # ★왜 보관했는지 — 보드에서 내린 이유를 남긴다(대표 2026-08-05).
        #   같은 주문이 두 줄이라 내린 것과, 실제로 출고돼서 내린 것은 뜻이 전혀 다르다.
        #   중복이라 내린 건은 **출고완료로 찍지 않는다** — 찍으면 매출이 그대로 두 배가 된다.
        ("archive_reason", "TEXT NOT NULL DEFAULT ''"),
        ("duplicate_of", "INTEGER"),                      # 같은 주문인 다른 줄
        # 쇼핑몰 관점 상태 — 입금대기 구분과 배송완료(구매확정) 확정용
        ("pay_status", "TEXT NOT NULL DEFAULT 'paid'"),   # paid | unpaid(입금대기)
        ("mall_status", "TEXT NOT NULL DEFAULT ''"),      # 몰이 준 원본 상태(참고용)
        ("delivered_at", "TEXT NOT NULL DEFAULT ''"),     # 배송완료/구매확정 확인 시각
        # 정산 — 판매가가 아니라 '실제로 손에 남는 돈'으로 마진을 봐야 한다
        ("fee_amount", "INTEGER NOT NULL DEFAULT 0"),     # 쇼핑몰·PG 판매수수료
        ("fee_rate", "REAL NOT NULL DEFAULT 0"),          # 적용 요율(%) — 근거를 남긴다
        ("shipping_cost", "INTEGER NOT NULL DEFAULT 0"),  # 출고 택배비(원가)
        ("refund_amount", "INTEGER NOT NULL DEFAULT 0"),  # 환불·부분환불 금액
        ("refund_at", "TEXT NOT NULL DEFAULT ''"),
        ("refund_reason", "TEXT NOT NULL DEFAULT ''"),
        # 사람이 직접 확정한 수수료는 자동 계산·소급 적용이 덮어쓰면 안 된다
        ("fee_manual", "INTEGER NOT NULL DEFAULT 0"),
        # 리뷰어 출고 — 체험단/리뷰용. 제품 없이 빈 박스만 나가는 경우가 있어
        # 셋팅·QC가 일반 주문과 구분해서 다뤄야 한다(2026-07-29 대표 요청)
        ("is_review", "INTEGER NOT NULL DEFAULT 0"),
        ("review_note", "TEXT NOT NULL DEFAULT ''"),
        # 쇼핑몰에 송장번호를 되쏜 결과 — 실패분을 재전송 목록에서 다시 보낼 수 있게 남긴다
        ("mall_sent_at", "TEXT NOT NULL DEFAULT ''"),
        ("mall_send_error", "TEXT NOT NULL DEFAULT ''"),
    ],
    "asset_repairs": [
        # 총액을 넣으면 공급가·부가세를 나눠 기록한다(대표 2026-08-04).
        # 세금계산서 대조와 원가 집계에 쓰인다 — 총액만 있으면 매번 손으로 나눠야 한다.
        ("vat", "INTEGER NOT NULL DEFAULT 0"),          # 부가세(총액의 1/11, 반올림)
        ("net", "INTEGER NOT NULL DEFAULT 0"),          # 공급가 = 총액 - 부가세
        ("parts", "TEXT NOT NULL DEFAULT ''"),          # 교체한 부품(SSD·RAM·배터리 …)
    ],
    "order_assets": [
        ("prev_status", "TEXT NOT NULL DEFAULT 'ready'"),
    ],
    "waybills": [
        # 회수 대상 자산과 회수 전 상태 — 주문 없이 접수되는 A/S 회수도 되돌릴 수 있어야 한다
        ("asset_ids", "TEXT NOT NULL DEFAULT ''"),
        ("asset_prev", "TEXT NOT NULL DEFAULT ''"),
        ("as_ticket_id", "INTEGER"),
    ],
}


# 새로 추가된 컬럼을 참조하는 인덱스 — 반드시 _migrate() 이후에 만든다.
_POST_MIGRATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_batches_stage ON purchase_batches(stage)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_batches_slip ON purchase_batches(slip_no) WHERE slip_no != ''",
]


def _rebuild_purchase_batches(conn):
    """구형 테이블의 supplier_id NOT NULL 제약을 푼다.

    가입고(V전표)는 '택배로 물건만 먼저 받고 거래처는 나중에 채우는' 단계라
    거래처가 비어 있어야 한다. 그런데 초기 테이블이 NOT NULL이라 저장이 통째로 실패했다
    (스키마 파일은 고쳤지만 CREATE TABLE IF NOT EXISTS는 기존 테이블을 바꾸지 않는다).
    SQLite에는 제약 해제가 없어 테이블을 다시 만들어 옮긴다.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='purchase_batches'").fetchone()
    if row is None:
        return
    ddl = (row["sql"] or "").replace(" ", "").replace("\n", "")
    if "supplier_idINTEGERNOTNULL" not in ddl:
        return                      # 이미 정상

    cols = [r["name"] for r in conn.execute("PRAGMA table_info(purchase_batches)").fetchall()]
    # executescript가 암묵 커밋을 하므로 여기서는 트랜잭션을 쓰지 않는다.
    # 대신 실패하면 옛 테이블을 제자리로 돌려놓아 데이터를 잃지 않게 한다.
    conn.execute("PRAGMA foreign_keys=OFF")
    # ★RENAME이 '다른 테이블의 참조까지' 따라 바꾸지 못하게 막는다.
    #   SQLite 3.25부터 ALTER TABLE ... RENAME은 그 테이블을 가리키는 다른 테이블의
    #   REFERENCES 문구도 새 이름으로 자동 수정한다. 그래서 여기서 이름을 바꾸는 순간
    #   assets.batch_id가 purchase_batches_old를 가리키게 되고, 아래에서 그 테이블을
    #   지우면 assets는 '없는 테이블'을 참조한 채 남아 자산 등록이 전부 실패한다
    #   (2026-07-29 운영에서 실제로 발생 — '서버 내부 오류'로만 보였다).
    conn.execute("PRAGMA legacy_alter_table=ON")
    renamed = False
    try:
        conn.execute("ALTER TABLE purchase_batches RENAME TO purchase_batches_old")
        renamed = True
        conn.executescript(_SCHEMA.read_text("utf-8"))          # 올바른 정의로 재생성
        new_cols = {r["name"] for r in conn.execute("PRAGMA table_info(purchase_batches)").fetchall()}
        keep = [c for c in cols if c in new_cols]
        if keep:
            conn.execute(
                f"INSERT INTO purchase_batches({','.join(keep)}) "
                f"SELECT {','.join(keep)} FROM purchase_batches_old")
        conn.execute("DROP TABLE purchase_batches_old")
    except Exception:
        if renamed:
            try:
                conn.execute("DROP TABLE IF EXISTS purchase_batches")
                conn.execute("ALTER TABLE purchase_batches_old RENAME TO purchase_batches")
            except Exception:                                    # noqa: BLE001
                pass
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate(conn):
    _rebuild_purchase_batches(conn)
    for table, columns in _ADDED_COLUMNS.items():
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, ddl in columns:
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    for sql in _POST_MIGRATE_INDEXES:
        conn.execute(sql)


def init_db(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA.read_text("utf-8"))
        _migrate(conn)
        row = conn.execute("SELECT COUNT(*) AS c FROM categories").fetchone()
        if row["c"] == 0:
            conn.execute("BEGIN IMMEDIATE")
            for i, name in enumerate(config.DEFAULT_CATEGORIES):
                conn.execute(
                    "INSERT INTO categories(name, sort, enabled, created_at) VALUES(?,?,1,?)",
                    (name, i, config.now_iso()),
                )
            conn.execute("COMMIT")
    finally:
        conn.close()


def get_db():
    if "db" not in g:
        g.db = _connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@contextmanager
def tx(write=False):
    """트랜잭션 경계. 쓰기는 BEGIN IMMEDIATE로 잠금을 선점한다."""
    conn = get_db()
    if write:
        ensure_daily_backup(current_app.config["DB_PATH"])
    conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- backups

def backup_dir(db_path) -> Path:
    return Path(db_path).parent / "backups"


def make_backup(db_path, target: Path):
    """sqlite 온라인 백업 API로 일관된 스냅샷 생성(tmp 후 원자적 교체).

    tmp 이름에 스레드 ID를 붙여 동시 호출이 같은 tmp를 잡는 충돌을 차단한다.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f"{target.name}.tmp-{threading.get_ident()}"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    tmp.replace(target)


def ensure_daily_backup(db_path):
    """주기 백업 — 마지막 백업이 BACKUP_INTERVAL_HOURS보다 오래됐으면 새로 만든다.

    ★하루 1회로 두면 그날 아침 백업 뒤에 벌어진 일(이관 626건 같은 큰 변화)이
      통째로 백업에 없는 상태로 하루를 보낸다(2026-07-28 실제 발생). 몇 시간 주기로 남긴다.
    """
    bdir = backup_dir(db_path)
    now = config.now().timestamp()
    stamp = config.now().strftime("%Y%m%d-%H%M%S")
    target = bdir / f"hms-{stamp}.db"
    with _backup_lock:
        if _recent_backup_exists(bdir, now):
            return
        make_backup(db_path, target)
        _cleanup_old_backups(bdir)
    mirror_backup(target, db_path)   # 2차 사본은 락 밖에서(느린 외장/NAS가 쓰기를 막지 않게)


def _recent_backup_exists(bdir, now_ts) -> bool:
    if not bdir.exists():
        return False
    cutoff = now_ts - config.BACKUP_INTERVAL_HOURS * 3600
    for f in bdir.glob("hms-*.db"):
        try:
            if f.stat().st_mtime >= cutoff:
                return True
        except OSError:
            continue
    return False


def mirror_dir():
    """2차 백업 위치 — 설정하지 않으면 None(그때는 1차만 남는다)."""
    raw = (config.BACKUP_MIRROR or "").strip()
    return Path(raw) if raw else None


def _is_live_db(db_path) -> bool:
    """미러는 '운영 DB'의 사본일 때만 의미가 있다.

    테스트·검증 서버는 임시 DB로 돌기 때문에, 이걸 안 막으면 테스트를 한 번 돌릴 때마다
    2차 백업 폴더에 쓸모없는 사본이 수천 개 쌓인다(2026-07-29 실제 발생).

    ★config.DB_PATH와 비교하면 안 된다. 그 값은 HMS_DB 환경변수에서 오기 때문에,
      라이브를 복사해 HMS_DB=<사본>으로 검증 서버를 띄우면 '자기 자신과 같다'가 되어
      항상 참이 된다. 실제로 그래서 테스트 계정이 섞인 사본이 D: 미러에 올라갔다
      (2026-07-29 hms-20260729-103002.db, 격리함). 환경변수를 타지 않는 고정 경로로 본다.
    """
    try:
        return Path(db_path).resolve() == config.LIVE_DB_PATH.resolve()
    except OSError:
        return False


def mirror_backup(src: Path, db_path=None):
    """다른 디스크/NAS에 사본 하나 더. 실패해도 본 백업은 유지된다.

    같은 디스크에만 두면 디스크 고장·랜섬웨어에 원본과 함께 사라진다.
    """
    dst_dir = mirror_dir()
    if dst_dir is None or not src.exists():
        return None
    if db_path is not None and not _is_live_db(db_path):
        return None
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        tmp = dst_dir / f"{src.name}.tmp"
        shutil.copy2(src, tmp)
        tmp.replace(dst_dir / src.name)
        _cleanup_old_backups(dst_dir)
        return dst_dir / src.name
    except OSError:
        return None


def run_manual_backup(db_path) -> Path:
    """수동 백업 — 주기 백업과 같은 락으로 직렬화(동시 실행 충돌 방지)."""
    ts = config.now().strftime("%Y%m%d-%H%M%S")
    with _backup_lock:
        target = backup_dir(db_path) / f"hms-{ts}.db"
        make_backup(db_path, target)
    mirror_backup(target, db_path)
    return target


def _cleanup_old_backups(bdir: Path):
    cutoff = config.now().timestamp() - config.BACKUP_RETENTION_DAYS * 86400
    for f in bdir.glob("hms-*.db"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def list_backups(db_path):
    bdir = backup_dir(db_path)
    out = []
    if bdir.exists():
        for f in sorted(bdir.glob("*.db"), reverse=True):
            st = f.stat()
            out.append({
                "name": f.name,
                "sizeBytes": st.st_size,
                "modifiedAt": datetime.fromtimestamp(st.st_mtime, config.KST).isoformat(timespec="seconds"),
            })
    return out
