#!/usr/bin/env python3
"""
Capture a Qobuz auth token by intercepting the browser login flow,
and save it to .env automatically.

One-time setup:
    pip install playwright
    playwright install chromium

Usage:
    python get_token.py
"""

import re
import sys
from pathlib import Path


def save_token_to_env(token: str) -> None:
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        if re.search(r"^QOBUZ_AUTH_TOKEN=.*", content, re.MULTILINE):
            content = re.sub(r"^QOBUZ_AUTH_TOKEN=.*", f"QOBUZ_AUTH_TOKEN={token}", content, flags=re.MULTILINE)
        else:
            content = content.rstrip("\n") + f"\nQOBUZ_AUTH_TOKEN={token}\n"
    else:
        content = f"QOBUZ_AUTH_TOKEN={token}\n"
    env_path.write_text(content, encoding="utf-8")


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. Run:")
        print("  pip install playwright")
        print("  playwright install chromium")
        sys.exit(1)

    print("Opening Qobuz login page — log in with your credentials.")
    print("The token will be captured automatically once you're logged in.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto("https://play.qobuz.com/login")

        try:
            response = page.wait_for_response(
                lambda r: "api.json" in r.url and "user/login" in r.url and r.status == 200,
                timeout=180_000,  # 3 minutes to log in
            )
        except Exception:
            print("\nTimed out waiting for login. Please try again.")
            browser.close()
            sys.exit(1)

        data = response.json()
        token = data.get("user_auth_token")
        browser.close()

    if not token:
        print("Could not find user_auth_token in the login response.")
        sys.exit(1)

    save_token_to_env(token)
    print(f"\nToken saved to .env successfully.")
    print(f"Token: {token[:12]}...{token[-6:]}")


if __name__ == "__main__":
    main()
