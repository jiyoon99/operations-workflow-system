import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
PORT = int(os.getenv("HMS_PORT", "5110"))
BASE = f"http://127.0.0.1:{PORT}"
OUT = ROOT / "docs" / "assets"
DB = ROOT / "data" / "portfolio-real-ui.db"


def request(path, method="GET", body=None, cookie=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, res.read().decode("utf-8"), res.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8"), e.headers


def wait_server():
    for _ in range(40):
        try:
            status, _, _ = request("/api/health")
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("server did not start")


def ensure_login():
    status, raw, _ = request("/api/auth/bootstrap")
    if status != 200:
        raise RuntimeError(raw)
    if json.loads(raw).get("needsSetup"):
        body = {
            "username": "admin",
            "displayName": "포트폴리오 관리자",
            "password": "portfolio1234",
        }
        status, raw, headers = request("/api/auth/setup", "POST", body)
        if status not in (200, 409):
            raise RuntimeError(raw)
        cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
        if cookie:
            return cookie
    status, raw, headers = request(
        "/api/auth/login",
        "POST",
        {"username": "admin", "password": "portfolio1234"},
    )
    if status != 200:
        raise RuntimeError(raw)
    return headers.get("Set-Cookie", "").split(";", 1)[0]


def seed_data(cookie):
    # The screenshots are real UI captures. This optional seed uses fake data only;
    # if an endpoint shape changes, the app screens are still captured without it.
    attempts = [
        ("/api/purchase/suppliers", {"name": "포트폴리오 거래처", "phone": "010-0000-0000"}),
        ("/api/orders", {
            "channel": "쿠팡",
            "orderNo": "PORT-2026-0001",
            "recipient": "샘플 고객",
            "phone": "010-0000-0000",
            "address": "서울시 포트폴리오구 샘플로 1",
            "productName": "ThinkPad T14 샘플",
            "optionName": "i5 / 16GB / 512GB",
            "quantity": 1,
            "amount": 459000,
        }),
    ]
    for path, body in attempts:
        status, raw, _ = request(path, "POST", body, cookie)
        if status not in (200, 201, 400, 404, 409):
            print(f"seed warning: {path} {status} {raw[:120]}")


def edge_screenshot(name, url, cookie=None):
    target = OUT / name
    script = ""
    if cookie:
        script = (
            "document.cookie = " + json.dumps(cookie + "; path=/") + "; "
            "setTimeout(() => location.href = " + json.dumps(url) + ", 100);"
        )
        helper = OUT / f"cookie-{name}.html"
        helper.write_text(f"<!doctype html><meta charset='utf-8'><script>{script}</script>", encoding="utf-8")
        url = f"{BASE}/static/../docs/assets/{helper.name}"
    cmd = [
        str(EDGE),
        "--headless=new",
        "--disable-gpu",
        "--disable-gpu-compositing",
        "--use-angle=swiftshader",
        "--window-size=1600,900",
        f"--user-data-dir={os.environ.get('TEMP', str(ROOT))}\\hms-real-ui-{name}",
        f"--screenshot={target}",
        url,
    ]
    if cookie:
        # Flask does not serve docs/. Use a data URL to set the cookie on the app domain is
        # not possible, so fall back to a URL with JS only when served by the app root.
        pass
    subprocess.run(cmd, check=True)
    if not target.exists() or target.stat().st_size < 10_000:
        raise RuntimeError(f"screenshot failed: {target}")


def edge_capture_with_login(name, view, cookie):
    js = f"""
      document.cookie = {json.dumps(cookie + "; path=/")};
      history.replaceState(null, '', '/');
      setTimeout(() => {{
        const a = document.querySelector('nav a[data-view="{view}"]');
        if (a) a.click();
      }}, 1800);
    """
    # Use a temporary static copy of index.html with a tiny injected script.
    # It is served by the real Flask app under /static/, so the actual app JS can call
    # /api/* normally and the capture remains a real UI screenshot.
    src = (ROOT / "static" / "index.html").read_text("utf-8")
    src = src.replace("</body>", f"<script>{js}</script></body>")
    tmp = ROOT / "static" / f"capture-{view}.html"
    tmp.write_text(src, encoding="utf-8")
    url = f"{BASE}/static/{tmp.name}"
    target = OUT / name
    cmd = [
        str(EDGE),
        "--headless=new",
        "--disable-gpu",
        "--disable-gpu-compositing",
        "--use-angle=swiftshader",
        "--allow-file-access-from-files",
        "--window-size=1600,900",
        f"--virtual-time-budget=5000",
        f"--user-data-dir={os.environ.get('TEMP', str(ROOT))}\\hms-real-ui-{view}",
        f"--screenshot={target}",
        url,
    ]
    subprocess.run(cmd, check=True)
    if not target.exists() or target.stat().st_size < 10_000:
        raise RuntimeError(f"screenshot failed: {target}")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "HMS_NO_TRACKER": "1",
        "HMS_NO_TMS_SYNC": "1",
        "HMS_NO_FILE_LOG": "1",
        "HMS_PORT": str(PORT),
        "HMS_DB": str(DB),
    })
    proc = subprocess.Popen([str(ROOT / "venv" / "Scripts" / "python.exe"), "run.py"], cwd=ROOT, env=env)
    try:
        wait_server()
        cookie = ensure_login()
        seed_data(cookie)
        edge_capture_with_login("portfolio-real-dashboard.png", "dashboard", cookie)
        edge_capture_with_login("portfolio-real-purchase.png", "purchase", cookie)
        edge_capture_with_login("portfolio-real-orders.png", "orders", cookie)
        edge_capture_with_login("portfolio-real-shipping.png", "shipping", cookie)
        edge_capture_with_login("portfolio-real-as.png", "as", cookie)
        edge_capture_with_login("portfolio-real-reports.png", "reports", cookie)
        edge_capture_with_login("portfolio-real-settings.png", "settings", cookie)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
