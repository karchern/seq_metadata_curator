#!/usr/bin/env python3
"""Prime the dedicated Chrome profile with fresh cf_clearance cookies.

Opens a HEADED patchright-Chrome pointed at
    ~/.seq_metadata_curator_chrome_profile
and navigates to a sample article on each Cloudflare-gated publisher.
The user solves any Cloudflare challenge that appears (usually only
Elsevier requires it). When all three tabs show the real article page,
press Enter in the terminal; the script closes the browser cleanly so
Chrome flushes cookies to disk. Subsequent HEADLESS runs of the fetcher
can then reuse the cached cf_clearance for typically 30 min–24 h.
"""
import sys
from pathlib import Path
from patchright.sync_api import sync_playwright

TARGETS = [
    ("elsevier",     "https://www.sciencedirect.com/science/article/pii/S1075996417300550?via%3Dihub"),
    ("wiley",        "https://onlinelibrary.wiley.com/doi/10.1002/ibd.21462"),
    ("taylorfrancis","https://www.tandfonline.com/doi/full/10.1080/19490976.2015.1023494"),
]

PROFILE = Path.home() / ".seq_metadata_curator_chrome_profile"


def main():
    PROFILE.mkdir(exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=False,
            channel="chrome",
            no_viewport=True,
        )
        print(f"[prime] profile: {PROFILE}", file=sys.stderr)
        print("[prime] opening three article tabs — solve any Cloudflare "
              "challenge you see, then come back here.", file=sys.stderr)
        for name, url in TARGETS:
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                print(f"[prime] {name}: goto ERROR {e}", file=sys.stderr)
            print(f"[prime] opened {name}: {url}", file=sys.stderr)
        input("[prime] press Enter after all three tabs show the real article page ... ")

        # Report final state per tab.
        for i, page in enumerate(ctx.pages):
            try:
                print(f"[prime] tab {i}: {page.url}\n         title={page.title()[:100]!r}",
                      file=sys.stderr)
            except Exception:
                pass
        ctx.close()  # flushes cookies to disk
        print("[prime] profile closed; cookies persisted.", file=sys.stderr)


if __name__ == "__main__":
    main()
