#!/usr/bin/env python3
"""Single-session batch fetch of Cloudflare-gated PDFs.

For each PMID:
  * derive the publisher article URL from the DOI (Elsevier via CrossRef,
    Wiley + T&F via direct DOI URL patterns);
  * navigate the persistent-profile Chrome page to it (establishes any
    per-domain Cloudflare state within THIS session);
  * fetch the PDF via page.request (shares cookies + TLS session with the
    live browser tab, so cf_clearance is honored);
  * verify %PDF magic bytes;
  * save to data_local/papers/PMID_{pmid}/paper.pdf.

Critical: the browser is opened ONCE at the top and never closed until the
whole batch finishes. Cloudflare's sciencedirect cf_clearance is bound to
session state that dies at browser exit — so this batching approach is the
only known way to serve 27 Elsevier PMIDs from one manual challenge-solve.
"""
from __future__ import annotations
import argparse, csv, json, re, sys, time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin
import requests
from patchright.sync_api import sync_playwright, Page

PROFILE = Path.home() / ".seq_metadata_curator_chrome_profile"


def article_url_only(doi_prefix: str, doi: str,
                     session: requests.Session) -> Optional[str]:
    """Return the publisher article-landing URL for this DOI (browser navigates
    here to establish per-domain Cloudflare state; PDF URL is then discovered
    from the DOM by discover_pdf_url_from_dom)."""
    if doi_prefix in ("10.1016", "10.1053"):
        try:
            r = session.get(f"https://api.crossref.org/works/{doi}", timeout=30)
            r.raise_for_status()
            primary = r.json()["message"].get("resource", {}).get("primary", {}).get("URL", "")
        except Exception:
            return None
        m = re.search(r"/pii/([A-Z0-9]+)", primary)
        if not m:
            return None
        return f"https://www.sciencedirect.com/science/article/pii/{m.group(1)}"
    if doi_prefix in ("10.1002", "10.1111"):
        return f"https://onlinelibrary.wiley.com/doi/{doi}"
    if doi_prefix == "10.1080":
        return f"https://www.tandfonline.com/doi/full/{doi}"
    return None


def discover_pdf_url_from_dom(page: Page, doi_prefix: str, doi: str) -> Optional[str]:
    """After navigating to the article page, return the URL that a real
    browser navigation should be pointed at to fetch the PDF.

    Publisher-specific discovery:
      - Elsevier: the article page renders a signed `/pdfft?md5=...&pid=...`
        link only after JS runs; without the md5 signature the endpoint
        returns a separate Cloudflare challenge page.
      - Wiley: `/doi/pdfdirect/{DOI}` reliably serves the PDF response (with
        application/pdf Content-Type). Chromium's built-in viewer normally
        eats that body, so the fetcher combines this URL with an
        auto-download profile (see ensure_pdf_download_prefs).
      - T&F: `/doi/pdf/{doi}?download=true` — usually paywalled from a
        non-subscribing IP; attempted for completeness.
    """
    if doi_prefix in ("10.1016", "10.1053"):
        try:
            n = page.locator("a[href*='pdfft']").count()
        except Exception:
            n = 0
        for i in range(n):
            href = page.locator("a[href*='pdfft']").nth(i).get_attribute("href")
            if href and "md5=" in href:
                return urljoin(page.url, href)
        return None
    if doi_prefix in ("10.1002", "10.1111"):
        return f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"
    if doi_prefix == "10.1080":
        return f"https://www.tandfonline.com/doi/pdf/{doi}?download=true"
    return None


def ensure_pdf_download_prefs(profile_dir: Path) -> None:
    """Patch the profile's Default/Preferences so Chromium auto-downloads
    PDFs (plugins.always_open_pdf_externally=true) instead of opening them
    in the built-in viewer. Without this, page.goto() on a PDF URL renders
    the PDF inline and Playwright's response.body() returns Chromium's
    viewer wrapper HTML (~500 B), not the PDF bytes.
    """
    prefs_path = profile_dir / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    if prefs_path.exists():
        try:
            prefs = json.loads(prefs_path.read_text())
        except Exception:
            prefs = {}
    else:
        prefs = {}
    plugins = prefs.setdefault("plugins", {})
    if plugins.get("always_open_pdf_externally") is True:
        return  # nothing to do
    plugins["always_open_pdf_externally"] = True
    prefs_path.write_text(json.dumps(prefs))


def download_pdf_via_navigation(page: Page, pdf_url: str,
                                dest: Path) -> tuple[Optional[int], str, int]:
    """Real browser navigation to pdf_url; expects the profile to be
    configured so that PDFs auto-download. Returns (http_status, reason, bytes).

    reason is one of: 'ok', 'no_download', 'download_error:<msg>', or a
    non-200 status. bytes is the on-disk size when reason='ok'.
    """
    try:
        with page.expect_download(timeout=45000) as dl_info:
            try:
                resp = page.goto(pdf_url, wait_until="commit", timeout=30000)
            except Exception:
                resp = None  # goto aborts when the response triggers a download
        download = dl_info.value
    except Exception as e:
        # Either the download event never fired (Chromium rendered inline
        # despite the pref) or the URL returned an error page instead of a PDF.
        return None, f"no_download:{e}", 0
    try:
        download.save_as(dest)
    except Exception as e:
        return None, f"download_save_error:{e}", 0
    size = dest.stat().st_size if dest.exists() else 0
    return 200, "ok", size


def wait_for_challenge_clear(page: Page, timeout_s: int, interactive: bool = False) -> bool:
    """Poll page.title until Cloudflare's interstitial goes away.

    interactive=True switches to a long-poll (up to 300 s) so the user has
    time to click through in the visible Chrome window; no terminal input
    needed. Non-interactive callers respect timeout_s and give up quietly.
    """
    ceiling = 600 if interactive else timeout_s * 2  # long-poll: 300s
    if interactive:
        print(f"    ! Cloudflare challenge on {page.url} — solve it in the Chrome window; "
              f"I'll auto-detect when it clears (up to 300 s).", flush=True)
    for _ in range(ceiling):
        page.wait_for_timeout(500)
        t = page.title().lower()
        if "just a moment" not in t and "attention required" not in t:
            return True
    return False


@dataclass
class FetchResult:
    pmid: str
    doi: str
    doi_prefix: str
    ok: bool
    reason: str
    bytes: int = 0
    article_url: str = ""
    pdf_url: str = ""


def fetch_one(page: Page, row: dict, out_root: Path,
              cx_session: requests.Session) -> FetchResult:
    pmid = row["pmid"]
    doi = row["doi"]
    prefix = row["doi_prefix"]

    dest = out_root / f"PMID_{pmid}" / "paper.pdf"
    if dest.exists() and dest.stat().st_size > 8192:
        with dest.open("rb") as fh:
            if fh.read(4).startswith(b"%PDF"):
                return FetchResult(pmid, doi, prefix, True, "already-cached",
                                   dest.stat().st_size)

    article_url = article_url_only(prefix, doi, cx_session)
    if not article_url:
        return FetchResult(pmid, doi, prefix, False, "url_resolution_failed")

    try:
        page.goto(article_url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        return FetchResult(pmid, doi, prefix, False, f"nav_error:{e}",
                           article_url=article_url)

    interactive = (prefix in ("10.1016", "10.1053"))  # Elsevier is the strict one
    if not wait_for_challenge_clear(page, timeout_s=8, interactive=interactive):
        return FetchResult(pmid, doi, prefix, False, "cloudflare_stuck",
                           article_url=article_url)

    # Let article-page JS finish (signed pdfft URLs are rendered client-side).
    page.wait_for_timeout(2500)

    pdf_url = discover_pdf_url_from_dom(page, prefix, doi)
    if not pdf_url:
        return FetchResult(pmid, doi, prefix, False, "no_pdf_link_on_page",
                           article_url=article_url)

    dest.parent.mkdir(parents=True, exist_ok=True)
    status, reason, size = download_pdf_via_navigation(page, pdf_url, dest)
    if status != 200 or reason != "ok":
        return FetchResult(pmid, doi, prefix, False, reason,
                           article_url=article_url, pdf_url=pdf_url)

    # %PDF magic sniff on the saved file.
    with dest.open("rb") as fh:
        head = fh.read(8)
    if not head.startswith(b"%PDF"):
        dest.unlink(missing_ok=True)
        return FetchResult(pmid, doi, prefix, False,
                           f"not_pdf:first8={head!r} size={size}",
                           article_url=article_url, pdf_url=pdf_url)
    body_size = size
    (dest.parent / "metadata.json").write_text(json.dumps({
        "pmid": pmid, "doi": doi, "doi_prefix": prefix,
        "article_url": article_url, "pdf_url": pdf_url,
        "size": body_size,
        "source": f"local_playwright:{prefix}",
    }, indent=2))
    return FetchResult(pmid, doi, prefix, True, "ok", body_size,
                       article_url=article_url, pdf_url=pdf_url)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-tsv", type=Path,
                    default=Path.home() / "seq_metadata_curator/data_local/pmids_cloudflare_residuals.tsv")
    ap.add_argument("--out-root", type=Path,
                    default=Path.home() / "seq_metadata_curator/data_local/papers")
    ap.add_argument("--report", type=Path,
                    default=Path.home() / "seq_metadata_curator/data_local/fetch_report.tsv")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only process the first N rows (for smoke-testing).")
    ap.add_argument("--prime-first", action="store_true", default=True,
                    help="Visit a canonical sciencedirect article FIRST so any "
                         "Cloudflare challenge is solved once before we start.")
    args = ap.parse_args()

    if not args.in_tsv.exists():
        print(f"input TSV missing: {args.in_tsv}", file=sys.stderr)
        return 2

    with args.in_tsv.open() as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    if args.limit:
        rows = rows[: args.limit]
    print(f"[batch] {len(rows)} PMIDs to fetch", file=sys.stderr)

    cx_session = requests.Session()
    cx_session.headers["User-Agent"] = "seq_metadata_curator/local_agent (mailto:karchernic@gmail.com)"

    results: list[FetchResult] = []
    ensure_pdf_download_prefs(PROFILE)

    # Chrome must be headed (Cloudflare rejects headless), but we push the
    # window far off-screen so it doesn't grab focus during the batch. macOS
    # keeps a rejected off-screen window in the corner; that's fine — the
    # only time the user needs it visible is to solve a Cloudflare challenge
    # (rare — Wiley/T&F auto-clear; Elsevier only occasionally). If a
    # challenge shows, drag the window back on-screen from its Dock icon.
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=False,
            channel="chrome",
            accept_downloads=True,
            no_viewport=True,
            args=[
                "--window-position=-2400,-2400",
                "--window-size=800,600",
            ],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if args.prime_first:
            canonical = "https://www.sciencedirect.com/science/article/pii/S1075996417300550?via%3Dihub"
            print(f"[prime] visiting {canonical}", file=sys.stderr)
            page.goto(canonical, wait_until="domcontentloaded", timeout=45000)
            if not wait_for_challenge_clear(page, timeout_s=12, interactive=True):
                print("[prime] could not clear Elsevier challenge; aborting.", file=sys.stderr)
                ctx.close()
                return 3
            print(f"[prime] cleared. title={page.title()[:80]!r}", file=sys.stderr)

        for i, row in enumerate(rows, 1):
            print(f"[{i}/{len(rows)}] pmid={row['pmid']} prefix={row['doi_prefix']} "
                  f"doi={row['doi']}", file=sys.stderr)
            res = fetch_one(page, row, args.out_root, cx_session)
            results.append(res)
            marker = "OK " if res.ok else "MISS"
            print(f"  → {marker} {res.reason} ({res.bytes} B)", file=sys.stderr)
            time.sleep(0.5)  # gentle pacing

        # Give the browser 3 s to flush any final network writes, then close.
        page.wait_for_timeout(3000)
        ctx.close()

    # write report
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("w") as fh:
        w = csv.DictWriter(
            fh, delimiter="\t",
            fieldnames=["pmid","doi","doi_prefix","ok","reason","bytes",
                        "article_url","pdf_url"],
        )
        w.writeheader()
        for r in results:
            w.writerow({
                "pmid": r.pmid, "doi": r.doi, "doi_prefix": r.doi_prefix,
                "ok": int(r.ok), "reason": r.reason, "bytes": r.bytes,
                "article_url": r.article_url, "pdf_url": r.pdf_url,
            })

    n_ok = sum(1 for r in results if r.ok)
    print(f"\n[batch] {n_ok}/{len(results)} rescued. Report: {args.report}", file=sys.stderr)
    for prefix in sorted({r.doi_prefix for r in results}):
        sub = [r for r in results if r.doi_prefix == prefix]
        ok = sum(1 for r in sub if r.ok)
        print(f"  {prefix}: {ok}/{len(sub)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
