#!/usr/bin/env python3
"""Probe Cloudflare-gated publishers with Playwright + real Chrome cookies
loaded from a Netscape-format cookies.txt (produced by export_publisher_cookies.py).
"""
import argparse
import http.cookiejar
from pathlib import Path
from playwright.sync_api import sync_playwright

TARGETS = [
    ("elsevier",     "https://www.sciencedirect.com/science/article/pii/S1075996417300550?via%3Dihub"),
    ("wiley",        "https://onlinelibrary.wiley.com/doi/10.1002/ibd.21462"),
    ("taylorfrancis","https://www.tandfonline.com/doi/full/10.1080/19490976.2015.1023494"),
]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
"""


def load_cookies(path: Path) -> list[dict]:
    jar = http.cookiejar.MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    out: list[dict] = []
    for c in jar:
        item = {
            "name": c.name,
            "value": c.value or "",
            "domain": c.domain,
            "path": c.path or "/",
            "secure": bool(c.secure),
            # Netscape jar lacks httpOnly; assume False (Playwright accepts).
        }
        if c.expires:
            item["expires"] = c.expires
        out.append(item)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies", type=Path, required=True)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--wait-ms", type=int, default=15000)
    args = ap.parse_args()

    cookies = load_cookies(args.cookies)
    print(f"[cookies] loaded {len(cookies)} cookies")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=UA,
            locale="en-US",
            viewport={"width": 1400, "height": 900},
        )
        ctx.add_init_script(STEALTH_JS)
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        for name, url in TARGETS:
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                status = resp.status if resp else None
                # Poll for Cloudflare challenge to clear.
                for _ in range(args.wait_ms // 500):
                    page.wait_for_timeout(500)
                    t = page.title()
                    if "just a moment" not in t.lower() and "attention required" not in t.lower():
                        break
                title = page.title()
                pdf_anchor_count = page.locator("a[href*='pdf' i]").count()
                print(f"{name:14s} status={status} title={title[:80]!r} pdf_links={pdf_anchor_count}")
            except Exception as e:  # noqa: BLE001
                print(f"{name:14s} ERROR {e}")
        browser.close()


if __name__ == "__main__":
    main()
