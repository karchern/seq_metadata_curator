#!/usr/bin/env python3
"""Diagnostic: for each publisher, navigate to the article + PDF URL,
capture the HTML/body of the PDF URL, and probe the article page for
downloadable-PDF selectors so we can pick a click-based fetch strategy.

Emits per-publisher artifacts under data_local/debug/.
"""
import re
from pathlib import Path
from patchright.sync_api import sync_playwright

PROFILE = Path.home() / ".seq_metadata_curator_chrome_profile"
DBG     = Path.home() / "seq_metadata_curator/data_local/debug"
DBG.mkdir(parents=True, exist_ok=True)

TARGETS = [
    ("elsevier",
     "https://www.sciencedirect.com/science/article/pii/S1075996417300550?via%3Dihub",
     "https://www.sciencedirect.com/science/article/pii/S1075996417300550/pdfft?isDNSHijacked=true"),
    ("wiley",
     "https://onlinelibrary.wiley.com/doi/10.1002/ijc.34398",
     "https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/ijc.34398"),
    ("tandf",
     "https://www.tandfonline.com/doi/full/10.1080/15257770.2021.2008432",
     "https://www.tandfonline.com/doi/pdf/10.1080/15257770.2021.2008432?download=true"),
]

def clear_cf(page, ceiling_s=120):
    for _ in range(ceiling_s * 2):
        page.wait_for_timeout(500)
        t = page.title().lower()
        if "just a moment" not in t and "attention required" not in t:
            return True
    return False

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        headless=False,
        channel="chrome",
        no_viewport=True,
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    for name, article_url, pdf_url in TARGETS:
        print(f"\n=== {name} ===")
        # 1. article page — inspect for PDF-download selectors
        page.goto(article_url, wait_until="domcontentloaded", timeout=60000)
        clear_cf(page)
        page.wait_for_timeout(2000)
        (DBG / f"{name}_article.html").write_text(page.content())
        print(f"  article title: {page.title()[:80]!r}")

        # Candidate selectors — save what we find for each.
        selectors = {
            "a[href*='pdf' i]":                "generic pdf-link",
            "a[href*='pdfft']":                "elsevier pdfft direct",
            "a[href*='pdfdirect']":            "wiley pdfdirect",
            "a[href*='/doi/pdf/']":            "tandf/wiley doi-pdf",
            "a[class*='download' i]":          "download-labeled anchor",
            "a[data-track-action*='PDF' i]":   "elsevier PDF button (analytics tag)",
            "button[class*='pdf' i]":          "pdf-labeled button",
        }
        for sel, label in selectors.items():
            try:
                cnt = page.locator(sel).count()
                if cnt > 0:
                    hrefs = []
                    for i in range(min(cnt, 3)):
                        try:
                            h = page.locator(sel).nth(i).get_attribute("href")
                            if h:
                                hrefs.append(h)
                        except Exception:
                            pass
                    print(f"    [{cnt:2d}] {sel}  ({label})  {hrefs[:2]}")
            except Exception as e:
                print(f"    ERR {sel}: {e}")

        # 2. navigate directly to the PDF URL — save what came back
        try:
            resp = page.goto(pdf_url, wait_until="commit", timeout=45000)
        except Exception as e:
            print(f"  goto pdf ERROR {e}")
            continue
        if resp is None:
            print("  pdf goto: no response")
            continue
        print(f"  pdf goto: status={resp.status} final_url={page.url}")
        try:
            body = resp.body()
        except Exception as e:
            print(f"  body ERROR {e}")
            continue
        (DBG / f"{name}_pdf_body.bin").write_bytes(body)
        head = body[:16]
        print(f"  pdf body: size={len(body)} first16={head!r}")

        # 3. Also grep the html body for any obvious PDF URL patterns
        text = body.decode(errors="ignore")[:400000]
        pdf_hits = re.findall(r'https?://[^\s"\'&lt;]+\.pdf[^\s"\'&lt;]*', text)[:5]
        signed_hits = re.findall(r'https?://[^\s"\'&lt;]+/pdfft[^\s"\'&lt;]*', text)[:5]
        pdfdirect_hits = re.findall(r'https?://[^\s"\'&lt;]+/pdfdirect[^\s"\'&lt;]*', text)[:5]
        if pdf_hits:      print(f"    embedded .pdf urls: {pdf_hits}")
        if signed_hits:   print(f"    embedded /pdfft urls: {signed_hits}")
        if pdfdirect_hits:print(f"    embedded /pdfdirect urls: {pdfdirect_hits}")

    page.wait_for_timeout(2000)
    ctx.close()
    print(f"\n[diag] artifacts in {DBG}")
