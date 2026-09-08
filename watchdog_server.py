"""OWS 워치독 — 서버를 자식 프로세스로 띄우고 죽으면 자동 재시작.

Windows 작업 스케줄러에 '부팅 시' 트리거로 이것 하나만 등록한다(단일 진입점).
- 서버가 비정상 종료되면 자동 재기동
- 소스 변경 감지 재시작은 개발용이라 기본 꺼짐(OWS_WATCH_SOURCE=1로 켠다).
  운영 중에 켜 두면 파일 하나만 건드려도 서버가 끊겨 쓰던 사람의 작업이 날아간다.
- 전역 뮤텍스로 워치독 중복 실행 차단 (RMS와 같은 패턴, 이름만 분리)

수동 종료: data/watchdog.stop 파일을 만들면 워치독과 서버가 함께 종료된다.
"""
import os
import re
import socket
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
# 쓰던 사람의 작업이 날아가므로 기본은 끈다. 개발할 때만 OWS_WATCH_SOURCE=1로 켠다.
WATCH_SOURCE = os.getenv("OWS_WATCH_SOURCE") == "1"

_mutex_handle = None


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [OWS-WATCHDOG] {msg}\n"
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


def local_root_for_unc():
    r"""공유 경로(\\host\D\...)로 띄워졌지만 그 host가 이 PC면 로컬 경로(D:\...)를 돌려준다. 아니면 None.

    ★공유 폴더 경로로 띄우면 서버가 SQLite를 SMB로 열게 돼 'disk I/O error'가 난다
      (2026-08-07·2026-09-01~02 실측). 탐색기에서 \\127.0.0.1\D 로 들어가 START를 눌러도,
      자동 시작 작업이 예전에 공유 경로로 등록돼 있어도 여기서 로컬 경로로 바로잡는다.
    """
    m = re.match(r"^[\\/]{2}([^\\/]+)[\\/]([A-Za-z])\$?[\\/](.*)$", str(ROOT))
    if not m:
        return None
    host, drive, rest = m.groups()
    local = Path(f"{drive}:\\{rest}")
    if not (local / "run.py").exists():
        return None
    names = {socket.gethostname().lower(), "localhost", "127.0.0.1"}
    try:
        names.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return local if host.lower() in names else None


def relaunch_local(local: Path) -> None:
    """로컬 경로의 워치독을 새로 띄우고(자동 시작 작업도 로컬 경로로 재등록) 이 프로세스는 물러난다."""
    py = local / "venv" / "Scripts" / "pythonw.exe"
    if not py.exists():
        py = local / "venv" / "Scripts" / "python.exe"
    log(f"공유 경로로 기동됨({ROOT}) → 로컬 경로로 다시 띄움: {local}")
    try:
        subprocess.run(["schtasks", "/Create", "/TN", "OperationsSystemAutoStart",
                        "/TR", f'"{py}" "{local / "watchdog_server.py"}"',
                        "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"], capture_output=True, timeout=30)
    except Exception:
        pass                                   # 작업 재등록 실패는 치명적이지 않다 — 다음 START에서 다시
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(py), str(local / "watchdog_server.py")], cwd=str(local),
                     creationflags=flags, close_fds=True)


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
        h = k32.CreateMutexW(None, False, "Global\\OWS_Watchdog_Singleton")
        ERROR_ALREADY_EXISTS = 183
        if (not h) or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            return False
        _mutex_handle = h
        return True
    except Exception:
        return True  # ctypes 실패 시 가용성 우선


def main() -> None:
    log("=" * 50)
    log("OWS watchdog started")
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
    _local = local_root_for_unc()
    if _local is not None:                     # 뮤텍스를 잡기 전에 물러나야 로컬 워치독이 잡을 수 있다
        relaunch_local(_local)
        os._exit(0)
    if not acquire_singleton():
        log("DUPLICATE_BLOCKED — 다른 워치독이 이미 실행 중, 종료")
        os._exit(0)
    main()
