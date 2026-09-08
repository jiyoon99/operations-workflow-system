"""Local interview demo. Each new server starts with a fresh synthetic database."""
import argparse
import hmac
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DEMO_DIR = Path(tempfile.gettempdir()) / "operations-workflow-demo"
STATE = DEMO_DIR / "server.json"


def running():
    try:
        info = json.loads(STATE.read_text("utf-8"))
        with urllib.request.urlopen(f"http://127.0.0.1:{int(info['port'])}/_demo/info", timeout=2) as response:
            health = json.load(response)
        return info if health.get("instance") == info["instance"] else None
    except (OSError, ValueError, KeyError):
        return None


def stop(info):
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/_demo/stop", data=b"",
        headers={"X-Demo-Control": info["control"]}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        if response.status != 200:
            raise RuntimeError("Could not stop the existing demo.")
    for _ in range(30):
        if not running():
            return
        time.sleep(0.1)
    raise RuntimeError("Existing demo is still stopping; try again shortly.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    info = running()
    if info and (args.stop or args.reset):
        stop(info)
        info = None
    if args.stop:
        print("Demo stopped.")
        return
    if info:
        url = f"http://127.0.0.1:{info['port']}"
        print(f"Demo already running: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return

    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    instance = secrets.token_hex(8)
    db = DEMO_DIR / f"demo-{instance}.db"
    os.environ.update({
        "OWS_DB": str(db), "OWS_HOST": "127.0.0.1", "OWS_NO_TRACKER": "1",
        "OWS_NO_TMS_SYNC": "1", "OWS_NO_FILE_LOG": "1", "OWS_SMS_BLOCK": "1",
        "OWS_BACKUP_MIRROR": "",
    })
    import requests
    from flask import abort, jsonify, request
    from werkzeug.serving import make_server

    def block_external(*args, **kwargs):
        raise requests.RequestException("Demo mode: external integrations are disabled.")

    def block_external_url(*args, **kwargs):
        raise urllib.error.URLError("Demo mode: external integrations are disabled.")

    requests.sessions.Session.request = block_external
    urllib.request.urlopen = block_external_url
    from app import create_app
    from demo_data import DEMO_PASSWORD, DEMO_USER, seed_demo

    app = create_app(db_path=db)
    control = secrets.token_urlsafe(32)
    server = None

    @app.get("/_demo/info")
    def demo_info():
        return jsonify({"instance": instance, "syntheticData": True, "externalCalls": False})

    @app.post("/_demo/stop")
    def demo_stop():
        if not hmac.compare_digest(request.headers.get("X-Demo-Control", ""), control):
            abort(403)
        threading.Thread(target=server.shutdown, daemon=True).start()
        return jsonify({"stopping": True})

    seed_demo(app)

    for port in range(5110, 5120):
        try:
            server = make_server("127.0.0.1", port, app, threaded=True)
            break
        except (OSError, SystemExit):
            continue
    if server is None:
        raise RuntimeError("No free demo port in 5110-5119.")
    STATE.write_text(json.dumps({"port": server.server_port, "pid": os.getpid(),
                                "instance": instance, "control": control}), "utf-8")
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Interview demo: {url}\nLogin: {DEMO_USER} / {DEMO_PASSWORD}\n"
          "Synthetic data only. External integrations are blocked.\n"
          "Use DEMO-RESET.bat for a fresh scenario; DEMO-STOP.bat to stop.", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            if json.loads(STATE.read_text("utf-8")).get("instance") == instance:
                STATE.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
