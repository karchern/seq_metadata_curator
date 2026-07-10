#!/usr/bin/env python3
"""Single-session probe: launch headed patchright ONCE, human solves any
Cloudflare challenge on Elsevier at start of the session, then all three
publisher article pages are fetched WITHOUT closing the browser.

Proves whether Cloudflare's Elsevier cf_clearance is session-bound
(dies at browser restart) or profile-bound (survives restart).
"""
import argparse
from pathlib import Path
from patchright.sync_api import sync_playwright

ELSEVIER_URL     = "https://www.sciencedirect.com/science/article/pii/S1075996417300550?via%3Dihub"
WILEY_URL        = "https://onlinelibrary.wiley.com/doi/10.1002/ibd.21462"
TANDFONLINE_URL  = "https://www.tandfonline.com/doi/full/10.1080/19490976.2015.1023494"

PROFILE = Path.home() / ".seq_metadata_curator_chrome_profile"


def is_challenge(page):
    t = page.title().lower()
    return "just a moment" in t or "attention required" in t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait-ms", type=int, default=20000)
    args = ap.parse_args()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=False,
            channel="chrome",
            no_viewport=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Step 1: navigate to Elsevier FIRST — this is the strict one.
        print(f"[step1] navigating to Elsevier: {ELSEVIER_URL}")
        page.goto(ELSEVIER_URL, wait_until="domcontentloaded", timeout=45000)
        # Wait for challenge to clear, either automatically or via user.
        for _ in range(args.wait_ms // 500):
            page.wait_for_timeout(500)
            if not is_challenge(page):
                break
        if is_challenge(page):
            print("[step1] Cloudflare challenge is still showing on Elsevier.")
            input("        SOLVE IT manually in the browser window, then press Enter here ... ")
        print(f"[step1] Elsevier title: {page.title()[:100]!r}")

        # Step 2 & 3: navigate to Wiley + T&F WITHOUT closing the browser.
        for name, url in [("wiley", WILEY_URL), ("tandf", TANDFONLINE_URL)]:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            for _ in range(20):
                page.wait_for_timeout(500)
                if not is_challenge(page):
                    break
            print(f"[{name}] title: {page.title()[:100]!r}")

        # Step 4: RE-VISIT Elsevier — the key test. If cf_clearance held
        # within-session, we get the article again. If it randomly rechallenges
        # each visit, that reveals a per-request fingerprint check.
        page.goto(ELSEVIER_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        print(f"[revisit] Elsevier title: {page.title()[:100]!r}")

        print("[done] browser stays open for inspection. Ctrl-C to exit; DON'T "
              "close the window manually.")
        try:
            input("press Enter to close cleanly ... ")
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
