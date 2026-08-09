"""HMS 워치독 — 서버를 자식 프로세스로 띄우고 죽으면 자동 재시작.

Windows 작업 스케줄러에 '부팅 시' 트리거로 이것 하나만 등록한다(단일 진입점).
- 서버가 비정상 종료되면 자동 재기동
- 소스 변경 감지 재시작은 개발용이라 기본 꺼짐(HMS_WATCH_SOURCE=1로 켠다).
  운영 중에 켜 두면 파일 하나만 건드려도 서버가 끊겨 쓰던 사람의 작업이 날아간다.
- 전역 뮤텍스로 워치독 중복 실행 차단 (RMS와 같은 패턴, 이름만 분리)

수동 종료: data/watchdog.stop 파일을 만들면 워치독과 서버가 함께 종료된다.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / "venv" / "Scripts" / "pythonw.exe"
if not PYTHON.exists():
    PYTHON = ROOT / "venv" / "Scripts" / "python.exe"
APP = ROOT / "run.py"
DATA = ROOT / "data"
LOG = DATA / "watchdog.log"
STOP_FILE = DATA / "watchdog.stop"

POLL_INTERVAL = 3
GRACE_SHUTDOWN = 5
RESTART_COOLDOWN = 2

# 소스 변경 감지 재시작은 '개발용'이다. 운영 중에 파일 하나만 건드려도 서버가 끊겨
# 쓰던 사람의 작업이 날아가므로 기본은 끈다. 개발할 때만 HMS_WATCH_SOURCE=1로 켠다.
WATCH_SOURCE = os.getenv("HMS_WATCH_SOURCE") == "1"

_mutex_handle = None


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [HMS-WATCHDOG] {msg}\n"
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except Exception:
        pass


def watch_files():
    """감시 대상 — 서버 코드와 프론트 정적 파일."""
    files = [APP]
    for pattern in ("app/**/*.py", "app/**/*.sql", "static/**/*.js",
                    "static/**/*.css", "static/**/*.html"):
        files.extend(sorted(ROOT.glob(pattern)))
    return files


def get_mtimes() -> dict:
    return {str(p): (p.stat().st_mtime if p.exists() else 0) for p in watch_files()}


def start_server() -> subprocess.Popen:
    log(f"Starting server: {PYTHON} {APP}")
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen([str(PYTHON), str(APP)], cwd=str(ROOT), creationflags=creationflags)
    log(f"Server started PID={proc.pid}")
    return proc


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    log(f"Terminating server PID={proc.pid}")
    try:
        proc.terminate()
        proc.wait(timeout=GRACE_SHUTDOWN)
        log("Server terminated gracefully")
    except subprocess.TimeoutExpired:
        log("Graceful shutdown timeout — killing")
        proc.kill()
        try:
            proc.wait(timeout=3)
        except Exception:
            pass


def acquire_singleton() -> bool:
    """워치독은 항상 1개만. Windows 전역 뮤텍스(프로세스 종료 시 OS가 자동 해제)."""
    global _mutex_handle
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        h = k32.CreateMutexW(None, False, "Global\\HMS_Watchdog_Singleton")
        ERROR_ALREADY_EXISTS = 183
        if (not h) or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            return False
        _mutex_handle = h
        return True
    except Exception:
        return True  # ctypes 실패 시 가용성 우선


def main() -> None:
    log("=" * 50)
    log("HMS watchdog started")
    DATA.mkdir(parents=True, exist_ok=True)
    if STOP_FILE.exists():
        STOP_FILE.unlink()  # 이전 종료 신호 제거

    mtimes = get_mtimes()
    proc = start_server()
    try:
        while True:
            time.sleep(POLL_INTERVAL)

            if STOP_FILE.exists():
                log("stop 파일 감지 — 서버와 워치독 종료")
                stop_server(proc)
                try:
                    STOP_FILE.unlink()
                except OSError:
                    pass
                return

            if proc.poll() is not None:
                code = proc.returncode
                log(f"Server exited (code {code}) — restarting")
                time.sleep(RESTART_COOLDOWN * (2 if code == 0 else 1))
                proc = start_server()
                mtimes = get_mtimes()
                continue

            if not WATCH_SOURCE:
                continue
            new_mtimes = get_mtimes()
            changed = [Path(f).name for f, m in mtimes.items() if m != new_mtimes.get(f, 0)]
            added = [Path(f).name for f in new_mtimes if f not in mtimes]
            if changed or added:
                log(f"Source changed: {(changed + added)[:5]} — restarting server")
                stop_server(proc)
                time.sleep(RESTART_COOLDOWN)
                proc = start_server()
            mtimes = new_mtimes
    except KeyboardInterrupt:
        log("Watchdog stopped by Ctrl+C")
        stop_server(proc)
    except Exception as e:
        log(f"Watchdog error: {e}")
        stop_server(proc)
        raise


if __name__ == "__main__":
    if not acquire_singleton():
        log("DUPLICATE_BLOCKED — 다른 워치독이 이미 실행 중, 종료")
        os._exit(0)
    main()
