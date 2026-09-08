r"""Capture the public application with synthetic data in a temporary database.

Requires playwright and a local Microsoft Edge installation.
Run: .venv\Scripts\python.exe scripts/capture_real_ui.py
"""
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server

    with tempfile.TemporaryDirectory(prefix="ows-capture-") as temp:
        os.environ.update({
            "OWS_DB": str(Path(temp) / "demo.db"),
            "OWS_NO_TRACKER": "1", "OWS_NO_TMS_SYNC": "1",
            "OWS_NO_FILE_LOG": "1", "OWS_SMS_BLOCK": "1",
        })
        from app import config, create_app

        app = create_app()
        app.testing = True
        client = app.test_client()

        def post(url, body):
            response = client.post(url, json=body)
            if response.status_code not in (200, 201):
                raise RuntimeError(f"Seed {url}: {response.status_code} {response.get_json()}")
            return response.get_json()

        post("/api/auth/setup", {
            "username": "portfolio", "displayName": "데모 관리자",
            "password": secrets.token_urlsafe(24),
        })
        categories = client.get("/api/categories").get_json()
        supplier = post("/api/suppliers", {"name": "데모 거래처", "phone": "010-0000-0000"})
        batch = post("/api/purchase-batches", {
            "supplierId": supplier["id"], "purchaseDate": config.now().strftime("%Y-%m-%d"),
            "totalAmount": 900000,
        })
        for i, (maker, model) in enumerate([
            ("Lenovo", "ThinkPad T14"), ("Dell", "Latitude 5420"), ("HP", "EliteBook 840"),
        ]):
            post("/api/assets", {
                "categoryId": categories[0]["id"], "maker": maker, "model": model,
                "grade": "SA", "purchasePrice": 300000, "qty": 1,
                "batchId": batch["id"],
            })
            post("/api/orders", {
                "channel": "수기", "orderNo": f"DEMO-000{i + 1}",
                "recipient": f"데모 고객 {i + 1}", "phone": "010-0000-0000",
                "address": "서울특별시 예시구 예시로 1", "postalCode": "00000",
                "productName": model, "productCode": f"DEMO{i + 1}_i5_내장",
                "optionName": "16GB / 512GB", "quantity": 1, "amount": 450000,
            })
        for i, symptom in enumerate(["화면 점검", "충전 상태 점검", "키보드 점검"]):
            ticket = post("/api/as-tickets", {
                "customer": f"데모 고객 {i + 1}", "phone": "010-0000-0000",
                "symptom": symptom, "chargeTo": "customer" if i == 0 else "company",
                "intakeItems": "본체, 충전기", "intake": "visit" if i == 0 else "parcel",
                "model": "ThinkPad T14",
            })
            if i == 0:
                response = client.patch(f"/api/as-tickets/{ticket['id']}", json={"status": "done"})
                assert response.status_code == 200, response.get_json()
        cookie = client.get_cookie(config.COOKIE_NAME)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        output = ROOT / "docs" / "assets"
        output.mkdir(parents=True, exist_ok=True)
        errors = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                context = browser.new_context(viewport={"width": 1600, "height": 1000}, locale="ko-KR")
                context.add_cookies([{"name": config.COOKIE_NAME, "value": cookie.value, "url": base}])
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base)
                page.locator("#app").wait_for(state="visible")
                for view in ["dashboard", "purchase", "orders", "setup", "shipping", "as", "settings"]:
                    page.locator(f'nav a[data-view="{view}"]').click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(500)
                    assert "OWS" in page.locator(".sidebar .brand").inner_text()
                    page.screenshot(path=str(output / f"portfolio-real-{view}.png"), full_page=True)
                    print(f"Captured {view}", flush=True)
                    if view == "as":
                        page.locator('[data-atab="board"]').click()
                        page.wait_for_load_state("networkidle")
                        page.wait_for_timeout(500)
                        assert "결제 전" in page.locator("#main").inner_text()
                        page.screenshot(path=str(output / "portfolio-real-as-board.png"), full_page=True)
                # Reports are part of the settings screen.
                page.evaluate("state.settingsTab = 'sales'; state.salesView = 'period'; go('settings')")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(500)
                page.screenshot(path=str(output / "portfolio-real-reports.png"), full_page=True)
                page.evaluate("go('dashboard')")
                page.wait_for_load_state("networkidle")
                page.set_viewport_size({"width": 390, "height": 844})
                page.reload()
                page.locator("#app").wait_for(state="visible")
                page.wait_for_load_state("networkidle")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "Mobile overflow"
                page.screenshot(path=str(output / "portfolio-mobile-dashboard.png"), full_page=True)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        if errors:
            raise RuntimeError("Browser errors: " + "; ".join(errors))
        print("Desktop/mobile capture completed without JavaScript errors.")


if __name__ == "__main__":
    main()
