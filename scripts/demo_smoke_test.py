"""Smoke-test a running interview demo in Microsoft Edge."""
import argparse
import json
import urllib.request

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5110")
    args = parser.parse_args()
    with urllib.request.urlopen(args.url + "/_demo/info", timeout=3) as response:
        info = json.load(response)
    assert info == {"externalCalls": False, "instance": info["instance"], "syntheticData": True}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="ko-KR")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url)
        page.locator("#auth-screen").wait_for(state="visible")
        page.locator("#login-username").fill("admin")
        page.locator("#login-password").fill("12345678")
        page.locator("#login-form button[type=submit]").click()
        page.locator("#app").wait_for(state="visible")
        page.locator('nav a[data-view="as"]').click()
        page.locator('[data-atab="board"]').click()
        page.wait_for_load_state("networkidle")
        assert "유상 · 금액 미입력" in page.locator("#main").inner_text()
        page.locator("#logout-btn").click()
        page.locator("#auth-screen").wait_for(state="visible")
        page.locator("#login-username").fill("viewer")
        page.locator("#login-password").fill("12345678")
        page.locator("#login-form button[type=submit]").click()
        page.locator("#app").wait_for(state="visible")
        assert page.locator('nav a[data-view="orders"]').count() == 1
        assert not page.locator('nav a[data-view="as"]').is_visible()
        browser.close()
    if errors:
        raise RuntimeError("Browser errors: " + "; ".join(errors))
    print("Demo smoke test passed: login, A/S payment guard, scoped user permissions.")


if __name__ == "__main__":
    main()
