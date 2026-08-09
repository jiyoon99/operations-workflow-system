"""HMS 전역 상수/경로.

모든 경로는 프로젝트 루트 기준 상대 경로로 계산한다 — 폴더 복사만으로
다른 PC로 이전할 수 있어야 한다(2026-07-28 대표 결정 #2).
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = Path(os.getenv("HMS_DB", str(DATA_DIR / "hms.db")))

# ★운영 DB의 '고정' 경로 — 환경변수로 뒤집히지 않는다.
#   DB_PATH는 HMS_DB로 갈아끼울 수 있어서, 검증 서버가 사본을 열면 DB_PATH도 그 사본이 된다.
#   그래서 'DB_PATH와 같은가'로 운영 여부를 판정하면 검증 서버가 스스로를 운영이라고 답한다.
#   실제로 그 구멍 때문에 ①문자가 나갈 뻔했고 ②2차 백업 폴더가 테스트 사본으로 오염됐다.
#   바깥으로 나가는 일(문자·미러 백업 등)은 반드시 이 값을 기준으로 판정한다.
LIVE_DB_PATH = DATA_DIR / "hms.db"

# ★pid 파일은 '그 서버가 연 DB' 옆에 쓴다 — 운영 폴더에 고정하면 안 된다.
#   고정해 두면 검증 서버(HMS_DB로 사본을 여는)가 운영 pid를 덮어써서
#   stop_server.bat이 이미 죽은 pid를 kill하고 진짜 서버는 계속 살아 있게 된다
#   (2026-07-30 실제로 발생). 포트도 다르므로 파일 이름에 포트를 넣어 섞이지 않게 한다.
_PORT_FOR_PID = os.getenv("HMS_PORT", "5100")
PID_FILE = (DB_PATH.parent / ("hms.pid" if _PORT_FOR_PID == "5100"
                              else f"hms-{_PORT_FOR_PID}.pid"))

HOST = os.getenv("HMS_HOST", "0.0.0.0")
PORT = int(os.getenv("HMS_PORT", "5100"))

KST = timezone(timedelta(hours=9))

SESSION_HOURS = 12
COOKIE_NAME = "hms_session"
PBKDF2_ITERATIONS = 310_000
LOGIN_FAILURE_LIMIT = 5
LOGIN_BLOCK_SECONDS = 5 * 60
BACKUP_RETENTION_DAYS = 14
# 하루 1회로는 부족하다 — 아침 백업 뒤 벌어진 하루치 작업이 통째로 빠진다.
BACKUP_INTERVAL_HOURS = float(os.getenv("HMS_BACKUP_INTERVAL_HOURS", "4"))
# 2차 백업 위치(다른 디스크/NAS). 비우면 1차만 남는다 — 디스크 고장 시 함께 사라진다.
BACKUP_MIRROR = os.getenv("HMS_BACKUP_MIRROR", str(Path("D:/hms-backups")) if Path("D:/").exists() else "")

# 신규 DB 생성 시 1회 시드. 이후에는 대표가 설정 화면에서 직접 관리한다.
# TMS 대분류 체계를 따른다(노트북도 PC로 분류).
DEFAULT_CATEGORIES = ["PC", "태블릿", "모니터", "웨어러블", "기타"]


def now() -> datetime:
    return datetime.now(KST)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def today_str() -> str:
    return now().strftime("%Y%m%d")
