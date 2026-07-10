#!/usr/bin/env python3
"""patchright-based probe of the three Cloudflare-gated publishers.

patchright is a drop-in stealth-patched fork of Playwright; the API is
identical, only the import differs.
"""
import argparse
from pathlib import Path
from patchright.sync_api import sync_playwright

TARGETS = [
    ("elsevier",     "https://www.sciencedirect.com/science/article/pii/S1075996417300550?via%3Dihub"),
    ("wiley",        "https://onlinelibrary.wiley.com/doi/10.1002/ibd.21462"),
    ("taylorfrancis","https://www.tandfonline.com/doi/full/10.1080/19490976.2015.1023494"),
]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def probe(page, name, url, wait_ms):
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        status = resp.status if resp else None
        for _ in range(wait_ms // 500):
            page.wait_for_timeout(500)
            t = page.title()
            if "just a moment" not in t.lower() and "attention required" not in t.lower():
                break
        title = page.title()
        pdf_anchors = page.locator("a[href*='pdf' i]").count()
        print(f"{name:14s} status={status} title={title[:80]!r} pdf_links={pdf_anchors}")
        return "just a moment" not in title.lower()
    except Exception as e:  # noqa: BLE001
        print(f"{name:14s} ERROR {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--channel", default=None,
                    help="chrome | msedge | chromium (default: patchright's chromium)")
    ap.add_argument("--wait-ms", type=int, default=15000)
    args = ap.parse_args()

    with sync_playwright() as p:
        # patchright docs: prefer persistent context + real chrome + headed.
        # Do NOT set user_agent or --disable-blink-features (those reveal automation).
        data_dir = Path.home() / ".seq_metadata_curator_chrome_profile"
        data_dir.mkdir(exist_ok=True)
        launch_kwargs = dict(
            user_data_dir=str(data_dir),
            headless=args.headless,
            channel=args.channel or "chrome",
            no_viewport=True,
        )
        ctx = p.chromium.launch_persistent_context(**launch_kwargs)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for name, url in TARGETS:
            probe(page, name, url, args.wait_ms)
        ctx.close()


if __name__ == "__main__":
    main()
