#!/usr/bin/env python3
"""Wave-2 laptop-side rescue runner — PDFs + supp + reads-mining.

Companion to the wave-1 `fetch_cloudflare_residuals.py`. That script only
rescued PDFs; this one extends the same single-browser-session pattern to:

  1. PDFs (delegates to the same discovery/download logic as wave 1;
     safe to re-run against already-cached PMIDs, they skip cleanly).
  2. Supplementary files (Cell Press mmc URLs, Elsevier ScienceDirect
     ars.els-cdn mmc URLs, Wiley downloadSupplement URLs, T&F suppl_file
     URLs).
  3. Reads accession scraping: fetches the Cloudflare-gated article HTML,
     regex-mines INSDC accessions from the Data Availability / Methods
     sections, HEAD-probes each accession against ENA to record run count
     + total gigabytes, and writes an aggregated outcome to
     data/wave2_local_reads_rescues.tsv.

Reuses:
  * The PROFILE at `~/.seq_metadata_curator_chrome_profile` (already primed
    for cf_clearance from wave 1).
  * The `ensure_pdf_download_prefs` + off-screen headed Chromium pattern.
  * The `wait_for_challenge_clear` polling loop.

DOES NOT run from cluster — Cloudflare will 403 every request. This is a
laptop-side script; the queue TSV was built cluster-side.

Idempotence: every artifact is checked on-disk before fetching; already-
present files are skipped. Re-runs are safe.

Input:
  data/wave2_local_rescue_queue.tsv (built by build_wave2_queue.py)

Outputs:
  data/papers/PMID_<pmid>/paper.pdf                     — PDFs
  data/papers/PMID_<pmid>/supp/<file>                   — supp
  data/wave2_local_reads_rescues.tsv                    — one row/PMID
  data/wave2_local_rescue_outcomes.tsv                  — per-artifact log
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import unquote, urljoin, urlparse

import requests

# patchright = stealth-patched Playwright fork. `fetch_cloudflare_residuals.py`
# uses the same import. If patchright is unavailable, fall back to playwright.
# On the cluster (where this file lives in git but is NEVER RUN), neither
# module is installed; that's fine — we set the symbols to None and let
# main() report the missing dep only when the driver actually runs.
try:
    from patchright.sync_api import sync_playwright, Page  # type: ignore
except ImportError:  # pragma: no cover — laptop-side, not cluster-tested
    try:
        from playwright.sync_api import sync_playwright, Page  # type: ignore
    except ImportError:
        sync_playwright = None  # type: ignore
        Page = None  # type: ignore


PROFILE = Path.home() / ".seq_metadata_curator_chrome_profile"

# Local-agent runtime data layout — must match the wave-1 script.
DEFAULT_REPO = Path.home() / "seq_metadata_curator"
DEFAULT_DATA = DEFAULT_REPO / "data_local"

# Broadened INSDC accession set (matches the extended regex from
# scripts/probe_coverage.py:INSDC_ACC_RE, but reads-focused). Used to mine
# accessions out of scraped fulltext HTML sections.
_INSDC_ACC_RE = re.compile(
    r"\b("
    r"PRJ[END][AB]\d+"          # BioProject INSDC
    r"|ERP\d+|SRP\d+|DRP\d+"    # Study accessions
    r"|DRA\d+"                  # DDBJ SRA
    r"|E-(?:MTAB|GEOD|MEXP|PROT|ERAD)-\d+"  # ArrayExpress
    r"|GSE\d+"                  # GEO series (best-effort)
    r")\b"
)

# %PDF, ZIP, GZIP, BZIP2, RAR, MS Office legacy, RTF, UTF-8 BOM plain text
_SUPP_ACCEPT_MAGIC = (
    b"%PDF", b"PK\x03\x04", b"\x1f\x8b", b"BZh", b"Rar!",
    b"\xd0\xcf\x11\xe0",  # legacy MS Office
    b"{\\rtf", b"\xef\xbb\xbf",
    b"<?xml",  # some supp .xml — accept
)
_SUPP_REJECT_MAGIC = (
    b"<htm", b"<HTM", b"<!DO", b"<!do",
)


# ---------------------------- data classes -----------------------------------

@dataclass
class QueueRow:
    pmid: str
    doi: str
    publisher_bucket: str
    artifact_type: str
    target_url: str
    rationale: str
    expected_local_action: str


@dataclass
class Outcome:
    pmid: str
    doi: str
    publisher_bucket: str
    artifact_type: str
    outcome: str
    filepath: str = ""
    bytes: int = 0
    detail: str = ""


@dataclass
class ReadsFinding:
    pmid: str
    accessions: list[str] = field(default_factory=list)
    n_runs: int = 0
    total_gb: float = 0.0
    detail: str = ""


# ---------------------------- browser plumbing -------------------------------

def ensure_pdf_download_prefs(profile_dir: Path) -> None:
    """Mirror of the wave-1 helper — force auto-download for PDFs."""
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
        return
    plugins["always_open_pdf_externally"] = True
    prefs_path.write_text(json.dumps(prefs))


def wait_for_challenge_clear(page: "Page", timeout_s: int, interactive: bool = False) -> bool:
    """Poll page.title until the Cloudflare interstitial goes away."""
    ceiling = 600 if interactive else timeout_s * 2
    if interactive:
        print(
            f"    ! Cloudflare challenge on {page.url} — solve it in the Chrome "
            f"window; auto-detecting clear (up to 300 s).",
            flush=True,
        )
    for _ in range(ceiling):
        page.wait_for_timeout(500)
        try:
            t = page.title().lower()
        except Exception:
            continue
        if "just a moment" not in t and "attention required" not in t:
            return True
    return False


# ---------------------------- URL discovery ----------------------------------

def _crossref_pii(session: requests.Session, doi: str) -> Optional[str]:
    """Resolve an Elsevier DOI to its PII via CrossRef's resource.primary.URL.

    E.g. `https://linkinghub.elsevier.com/retrieve/pii/S1075996417300550`.
    """
    try:
        r = session.get(f"https://api.crossref.org/works/{doi}", timeout=30)
        r.raise_for_status()
        primary = r.json()["message"]["resource"]["primary"]["URL"]
    except Exception:
        return None
    m = re.search(r"/pii/([A-Z0-9]+)", primary)
    return m.group(1) if m else None


def _dashed_pii(pii: str) -> str:
    """Convert compact `S2211124721013565` to dashed `S2211-1247(21)01356-5`.

    Cell Press's `/action/showPdf?pii=<dashed>` endpoint requires the dashed
    form. Elsevier PIIs are 17 chars total (`S` + 16 digits), formatted as
    `S{4}-{4}({2}){5}-{1}`.
    """
    m = re.match(r"^S(\d{4})(\d{4})(\d{2})(\d{5})(\d)$", pii)
    if not m:
        return pii
    g1, g2, g3, g4, g5 = m.groups()
    return f"S{g1}-{g2}({g3}){g4}-{g5}"


_CELL_PRESS_SUFFIX_RE = re.compile(r"^10\.1016/j\.([a-z]+)\.\d{4}\.")
_CELL_PRESS_SUFFIXES = {
    "cell", "ccell", "chom", "cmet", "celrep", "xcrm", "xgen", "stem",
    "molcel", "immuni", "cub", "jcmgh", "devcel", "neuron", "med",
    "chembiol", "xinn",
}
_CELL_JOURNAL_SLUG = {
    "cell": "cell",
    "ccell": "cancer-cell",
    "chom": "cell-host-microbe",
    "cmet": "cell-metabolism",
    "celrep": "cell-reports",
    "xcrm": "cell-reports-medicine",
    "xgen": "cell-genomics",
    "stem": "cell-stem-cell",
    "molcel": "molecular-cell",
    "immuni": "immunity",
    "cub": "current-biology",
    "jcmgh": "cellmolgastro",
    "devcel": "developmental-cell",
    "neuron": "neuron",
    "med": "med",
    "chembiol": "cell-chemical-biology",
    "xinn": "the-innovation",
}


def _cell_press_code(doi: str) -> Optional[str]:
    m = _CELL_PRESS_SUFFIX_RE.match(doi)
    if not m:
        return None
    return m.group(1) if m.group(1) in _CELL_PRESS_SUFFIXES else None


def article_url_for_html_fetch(row: QueueRow, session: requests.Session) -> Optional[str]:
    """Return the URL whose fulltext HTML we should fetch for THIS artifact.

    For Elsevier ScienceDirect / Gastro, this is the PII fulltext URL (needs
    a CrossRef roundtrip). For Cell Press, cell.com/<slug>/fulltext/<PII>.
    For Wiley / T&F, the DOI-based landing URL.
    """
    bucket = row.publisher_bucket
    doi = row.doi
    if bucket in ("elsevier-or-cell_press", "elsevier-gastro"):
        cp = _cell_press_code(doi)
        if cp is not None:
            slug = _CELL_JOURNAL_SLUG.get(cp, cp)
            # Cell Press landing — fetch via the cell.com slug URL. We still
            # need the PII to construct it; fall back to the DOI resolver if
            # CrossRef doesn't give one.
            pii = _crossref_pii(session, doi)
            if pii:
                return f"https://www.cell.com/{slug}/fulltext/{pii}"
            return f"https://doi.org/{doi}"
        pii = _crossref_pii(session, doi)
        if not pii:
            return None
        return f"https://www.sciencedirect.com/science/article/pii/{pii}"
    if bucket == "wiley":
        return f"https://onlinelibrary.wiley.com/doi/{doi}"
    if bucket == "taylor-francis":
        return f"https://www.tandfonline.com/doi/full/{doi}"
    return None


# --------- PDF discovery: re-use wave-1 logic per publisher ------------------

def discover_pdf_url_from_dom(page: "Page", row: QueueRow) -> Optional[str]:
    bucket = row.publisher_bucket
    doi = row.doi
    if bucket == "wiley":
        return f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"
    if bucket == "taylor-francis":
        return f"https://www.tandfonline.com/doi/pdf/{doi}?download=true"
    if bucket in ("elsevier-or-cell_press", "elsevier-gastro"):
        cp = _cell_press_code(doi)
        if cp is not None:
            # Try the DOM's showPdf link first (populated when fulltext loads).
            try:
                n = page.locator("a[href*='showPdf']").count()
            except Exception:
                n = 0
            for i in range(n):
                href = page.locator("a[href*='showPdf']").nth(i).get_attribute("href")
                if href:
                    return urljoin(page.url, href)
            # Fallback: pdfExtended endpoint (needs slug + PII from URL).
            slug = _CELL_JOURNAL_SLUG.get(cp, cp)
            m = re.search(r"/fulltext/([A-Z0-9]+)", page.url)
            if m:
                return f"https://www.cell.com/{slug}/pdfExtended/{m.group(1)}"
            return None
        # ScienceDirect signed pdfft link (must contain md5=).
        try:
            n = page.locator("a[href*='pdfft']").count()
        except Exception:
            n = 0
        for i in range(n):
            href = page.locator("a[href*='pdfft']").nth(i).get_attribute("href")
            if href and "md5=" in href:
                return urljoin(page.url, href)
        return None
    return None


# ---------------------------- supp discovery ---------------------------------

_CELL_MMC_RE = re.compile(
    r'(?:https?://www\.(?:cell|cmghjournal)\.com)?/cms/'
    r'[^"\'\s<>]+/attachment/[^"\'\s<>]+/mmc\d+\.[A-Za-z0-9]+',
    re.IGNORECASE,
)
_SD_MMC_RE = re.compile(
    r'https?://ars\.els-cdn\.com/content/image/'
    r'1-s2\.0-[A-Z0-9]+-mmc\d+\.[A-Za-z0-9]+',
    re.IGNORECASE,
)
_WILEY_SUPP_RE = re.compile(
    r'/action/downloadSupplement\?doi=[^"\'\s<>]+&(?:amp;)?file=[^"\'\s<>]+',
    re.IGNORECASE,
)
_TF_SUPP_RE = re.compile(
    r'/doi/suppl/[^"\'\s<>]+/suppl_file/[^"\'\s<>]+',
    re.IGNORECASE,
)


def _absolutize(base_url: str, url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        return urljoin(base_url, url)
    return urljoin(base_url, "/" + url)


def discover_supp_urls_from_html(html: str, base_url: str,
                                 bucket: str, doi: str) -> list[str]:
    urls: list[str] = []
    # Preferred: a pre-cached manifest is checked elsewhere by the runner.
    if bucket in ("elsevier-or-cell_press", "elsevier-gastro"):
        cp = _cell_press_code(doi)
        if cp is not None:
            for hit in set(_CELL_MMC_RE.findall(html)):
                urls.append(_absolutize(base_url, hit))
        else:
            urls.extend(sorted(set(_SD_MMC_RE.findall(html))))
    elif bucket == "wiley":
        for hit in set(_WILEY_SUPP_RE.findall(html)):
            urls.append(_absolutize(base_url, hit))
    elif bucket == "taylor-francis":
        for hit in set(_TF_SUPP_RE.findall(html)):
            urls.append(_absolutize(base_url, hit))
    # Dedupe preserving order
    seen = set()
    out: list[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


# ---------------------------- reads discovery --------------------------------

def _text_snippet_below_data_availability(html: str) -> str:
    """Return the HTML slice around a Data Availability section, plus the whole
    Methods / References tail (accession lists sometimes live there). Falls back
    to the whole doc if no marker is found.
    """
    lowered = html.lower()
    markers = (
        "data availability", "availability of data", "accession number",
        "accession numbers", "deposited at", "biosample", "bioproject",
        "sequencing data", "sequence read archive",
    )
    starts = [lowered.find(m) for m in markers if lowered.find(m) >= 0]
    if not starts:
        return html  # regex-scan whole document
    start = min(starts)
    # Take a generous window: 20k chars before and 60k after (accession lists
    # often trail into supplementary methods).
    lo = max(0, start - 20_000)
    hi = min(len(html), start + 60_000)
    return html[lo:hi]


def mine_reads_accessions(html: str) -> list[str]:
    """Regex-scan the fulltext HTML for INSDC accessions."""
    snippet = _text_snippet_below_data_availability(html)
    found = set()
    for m in _INSDC_ACC_RE.finditer(snippet):
        found.add(m.group(1).upper())
    return sorted(found)


def probe_ena_filereport(session: requests.Session, acc: str) -> tuple[int, float]:
    """Query ENA for run count + total_gb for a single accession.

    Returns (0, 0.0) on failure or non-INSDC (e.g. GSE) accession.
    """
    if not re.match(r"^(PRJ[END][AB]|ERP|SRP|DRP|DRA)\d+$", acc):
        return (0, 0.0)
    url = (
        "https://www.ebi.ac.uk/ena/portal/api/filereport?"
        f"accession={acc}&result=read_run&fields=run_accession,fastq_bytes&format=tsv"
    )
    try:
        r = session.get(url, timeout=30)
        if r.status_code != 200:
            return (0, 0.0)
        body = r.text.strip().splitlines()
        if len(body) <= 1:
            return (0, 0.0)
        n_runs = 0
        total_bytes = 0
        for line in body[1:]:
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            run = parts[0].strip()
            fb = parts[1].strip()
            if not run:
                continue
            n_runs += 1
            for chunk in fb.split(";"):
                try:
                    total_bytes += int(chunk)
                except (ValueError, TypeError):
                    pass
        return (n_runs, round(total_bytes / (1024 ** 3), 3))
    except Exception:
        return (0, 0.0)


# ---------------------------- download helper --------------------------------

def download_via_page(page: "Page", url: str, dest: Path,
                      timeout_ms: int = 60_000) -> tuple[str, int]:
    """Real-browser navigation to url; expects auto-download prefs.

    Returns (outcome, bytes_written). outcome is 'ok', 'no_download:<why>',
    'download_error:<why>'.
    """
    try:
        with page.expect_download(timeout=timeout_ms) as dl_info:
            try:
                page.goto(url, wait_until="commit", timeout=45_000)
            except Exception:
                pass  # goto aborts when the download triggers
        download = dl_info.value
    except Exception as e:
        return (f"no_download:{e}", 0)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        download.save_as(dest)
    except Exception as e:
        return (f"download_error:{e}", 0)
    return ("ok", dest.stat().st_size if dest.exists() else 0)


def _magic_sniff(dest: Path) -> tuple[bool, bytes]:
    with dest.open("rb") as fh:
        head = fh.read(8)
    for m in _SUPP_REJECT_MAGIC:
        if head.startswith(m):
            return (False, head)
    for m in _SUPP_ACCEPT_MAGIC:
        if head.startswith(m):
            return (True, head)
    return (False, head)


def _pdf_magic_ok(dest: Path) -> bool:
    with dest.open("rb") as fh:
        return fh.read(4).startswith(b"%PDF")


# ---------------------------- per-artifact dispatch --------------------------

def handle_pdf(page: "Page", row: QueueRow, paper_dir: Path,
               cx: requests.Session) -> Outcome:
    dest = paper_dir / "paper.pdf"
    if dest.exists() and dest.stat().st_size > 8192 and _pdf_magic_ok(dest):
        return Outcome(row.pmid, row.doi, row.publisher_bucket, "pdf",
                       "already_cached", str(dest), dest.stat().st_size)

    article = article_url_for_html_fetch(row, cx)
    if not article:
        return Outcome(row.pmid, row.doi, row.publisher_bucket, "pdf",
                       "url_resolution_failed")

    try:
        page.goto(article, wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        return Outcome(row.pmid, row.doi, row.publisher_bucket, "pdf",
                       f"nav_error", detail=str(e))

    interactive = row.publisher_bucket in ("elsevier-or-cell_press",
                                           "elsevier-gastro")
    if not wait_for_challenge_clear(page, timeout_s=10, interactive=interactive):
        return Outcome(row.pmid, row.doi, row.publisher_bucket, "pdf",
                       "cloudflare_wall")
    page.wait_for_timeout(2500)  # let JS-rendered pdfft links populate

    pdf_url = discover_pdf_url_from_dom(page, row)
    if not pdf_url:
        return Outcome(row.pmid, row.doi, row.publisher_bucket, "pdf",
                       "no_pdf_link_on_page")

    outcome, size = download_via_page(page, pdf_url, dest)
    if outcome != "ok":
        return Outcome(row.pmid, row.doi, row.publisher_bucket, "pdf",
                       outcome, detail=pdf_url)
    if size < 8192 or not _pdf_magic_ok(dest):
        dest.unlink(missing_ok=True)
        return Outcome(row.pmid, row.doi, row.publisher_bucket, "pdf",
                       "magic_fail", detail=pdf_url)

    (paper_dir / "metadata.json").write_text(json.dumps({
        "pmid": row.pmid, "doi": row.doi,
        "doi_prefix": row.doi.split("/", 1)[0] if "/" in row.doi else "",
        "article_url": article, "pdf_url": pdf_url,
        "size": size,
        "source": f"local_playwright:{row.publisher_bucket}:wave2",
    }, indent=2))
    return Outcome(row.pmid, row.doi, row.publisher_bucket, "pdf",
                   "ok", str(dest), size)


def _load_cell_manifest(paper_dir: Path) -> list[str]:
    """Read a pre-cached Cell Press manifest_pending_playwright.tsv if present."""
    m = paper_dir / "supp" / "manifest_pending_playwright.tsv"
    if not m.exists():
        return []
    urls: list[str] = []
    with m.open() as fh:
        r = csv.reader(fh, delimiter="\t")
        header = next(r, None)
        for row in r:
            if row and row[0].strip():
                urls.append(row[0].strip())
    return urls


def handle_supp(page: "Page", row: QueueRow, paper_dir: Path,
                cx: requests.Session) -> Outcome:
    supp_dir = paper_dir / "supp"
    supp_dir.mkdir(parents=True, exist_ok=True)

    # Determine candidate URLs: first check pre-cached manifest, then parse HTML.
    urls = _load_cell_manifest(paper_dir)
    article = article_url_for_html_fetch(row, cx)

    if not urls:
        if not article:
            return Outcome(row.pmid, row.doi, row.publisher_bucket, "supp",
                           "url_resolution_failed")
        try:
            page.goto(article, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:
            return Outcome(row.pmid, row.doi, row.publisher_bucket, "supp",
                           f"nav_error", detail=str(e))
        interactive = row.publisher_bucket in ("elsevier-or-cell_press",
                                               "elsevier-gastro")
        if not wait_for_challenge_clear(page, timeout_s=10, interactive=interactive):
            return Outcome(row.pmid, row.doi, row.publisher_bucket, "supp",
                           "cloudflare_wall")
        page.wait_for_timeout(2500)
        html = page.content()
        urls = discover_supp_urls_from_html(html, page.url,
                                            row.publisher_bucket, row.doi)

    if not urls:
        return Outcome(row.pmid, row.doi, row.publisher_bucket, "supp",
                       "no_supp_links")

    n_ok = 0
    n_fail = 0
    failed_details: list[str] = []
    for url in urls:
        # File name derivation
        filename = unquote(url.rsplit("/", 1)[-1])
        # Wiley downloadSupplement is a querystring endpoint — pull &file= arg
        if "downloadSupplement" in url and "file=" in url:
            m = re.search(r"[?&]file=([^&]+)", url)
            if m:
                filename = unquote(m.group(1))
        # Sanitize filename to avoid slashes / query fragments
        filename = re.sub(r"[?#].*$", "", filename)
        filename = filename.strip().lstrip("/") or f"supp_{n_ok+n_fail+1}.bin"
        dest = supp_dir / filename
        if dest.exists() and dest.stat().st_size > 512:
            n_ok += 1
            continue
        outcome, size = download_via_page(page, url, dest, timeout_ms=90_000)
        if outcome != "ok" or size < 256:
            if dest.exists() and size < 256:
                dest.unlink(missing_ok=True)
            failed_details.append(f"{url} -> {outcome}")
            n_fail += 1
            continue
        ok, head = _magic_sniff(dest)
        if not ok:
            dest.unlink(missing_ok=True)
            failed_details.append(f"{url} -> magic_fail:{head!r}")
            n_fail += 1
            continue
        n_ok += 1

    if n_ok == 0:
        return Outcome(row.pmid, row.doi, row.publisher_bucket, "supp",
                       "all_failed",
                       detail="; ".join(failed_details[:3]))
    detail = f"n_ok={n_ok} n_fail={n_fail}"
    return Outcome(row.pmid, row.doi, row.publisher_bucket, "supp",
                   "ok", str(supp_dir), n_ok, detail=detail)


def handle_reads(page: "Page", row: QueueRow, cx: requests.Session,
                 reads_agg: dict) -> Outcome:
    article = article_url_for_html_fetch(row, cx)
    if not article:
        return Outcome(row.pmid, row.doi, row.publisher_bucket, "reads",
                       "url_resolution_failed")
    try:
        page.goto(article, wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        return Outcome(row.pmid, row.doi, row.publisher_bucket, "reads",
                       f"nav_error", detail=str(e))
    interactive = row.publisher_bucket in ("elsevier-or-cell_press",
                                           "elsevier-gastro")
    if not wait_for_challenge_clear(page, timeout_s=10, interactive=interactive):
        return Outcome(row.pmid, row.doi, row.publisher_bucket, "reads",
                       "cloudflare_wall")
    page.wait_for_timeout(2000)
    try:
        html = page.content()
    except Exception as e:
        return Outcome(row.pmid, row.doi, row.publisher_bucket, "reads",
                       "html_capture_error", detail=str(e))
    accs = mine_reads_accessions(html)
    if not accs:
        return Outcome(row.pmid, row.doi, row.publisher_bucket, "reads",
                       "no_accessions_found")

    # Probe ENA for run count + size for each project-level accession.
    n_runs_total = 0
    total_gb = 0.0
    verified: list[str] = []
    for acc in accs:
        n, gb = probe_ena_filereport(cx, acc)
        if n > 0:
            verified.append(acc)
            n_runs_total += n
            total_gb += gb
    if not verified:
        return Outcome(row.pmid, row.doi, row.publisher_bucket, "reads",
                       "accessions_unverified",
                       detail=",".join(accs[:5]))
    reads_agg[row.pmid] = ReadsFinding(
        pmid=row.pmid,
        accessions=verified,
        n_runs=n_runs_total,
        total_gb=round(total_gb, 3),
        detail=",".join(accs[len(verified):]),
    )
    return Outcome(row.pmid, row.doi, row.publisher_bucket, "reads",
                   "ok", "",
                   n_runs_total,
                   detail=f"acc={','.join(verified)} gb={total_gb:.2f}")


# ---------------------------- driver -----------------------------------------

def run(queue_tsv: Path, out_root: Path, outcomes_tsv: Path,
        reads_tsv: Path, limit: Optional[int], prime_first: bool) -> int:
    if sync_playwright is None:
        print("neither patchright nor playwright is installed — this script "
              "MUST run on the laptop with `pip install patchright && "
              "python -m patchright install chromium`. It cannot run on the "
              "cluster.", file=sys.stderr)
        return 3
    if not queue_tsv.exists():
        print(f"queue TSV missing: {queue_tsv}", file=sys.stderr)
        return 2

    rows: list[QueueRow] = []
    with queue_tsv.open() as fh:
        for d in csv.DictReader(fh, delimiter="\t"):
            rows.append(QueueRow(
                pmid=d["pmid"], doi=d["doi"],
                publisher_bucket=d["publisher_bucket"],
                artifact_type=d["artifact_type"],
                target_url=d["target_url"],
                rationale=d["rationale"],
                expected_local_action=d["expected_local_action"],
            ))
    if limit:
        rows = rows[:limit]

    print(f"[wave2] {len(rows)} queue entries loaded", file=sys.stderr)

    cx = requests.Session()
    cx.headers["User-Agent"] = (
        "seq_metadata_curator/local_agent_wave2 (mailto:karchernic@gmail.com)"
    )

    ensure_pdf_download_prefs(PROFILE)
    outcomes: list[Outcome] = []
    reads_agg: dict[str, ReadsFinding] = {}

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

        if prime_first:
            canonical = (
                "https://www.sciencedirect.com/science/article/pii/"
                "S1075996417300550?via%3Dihub"
            )
            print(f"[prime] visiting {canonical}", file=sys.stderr)
            try:
                page.goto(canonical, wait_until="domcontentloaded", timeout=45_000)
            except Exception as e:
                print(f"[prime] nav_error: {e}", file=sys.stderr)
            if not wait_for_challenge_clear(page, timeout_s=12, interactive=True):
                print("[prime] could not clear Elsevier challenge; continuing anyway.",
                      file=sys.stderr)
            else:
                print(f"[prime] cleared. title={page.title()[:80]!r}",
                      file=sys.stderr)

        for i, row in enumerate(rows, 1):
            print(f"[{i}/{len(rows)}] pmid={row.pmid} bucket={row.publisher_bucket} "
                  f"artifact={row.artifact_type}", file=sys.stderr)
            paper_dir = out_root / f"PMID_{row.pmid}"
            paper_dir.mkdir(parents=True, exist_ok=True)
            try:
                if row.artifact_type == "pdf":
                    outc = handle_pdf(page, row, paper_dir, cx)
                elif row.artifact_type == "supp":
                    outc = handle_supp(page, row, paper_dir, cx)
                elif row.artifact_type == "reads":
                    outc = handle_reads(page, row, cx, reads_agg)
                else:
                    outc = Outcome(row.pmid, row.doi, row.publisher_bucket,
                                   row.artifact_type,
                                   "unknown_artifact_type")
            except Exception as e:
                outc = Outcome(row.pmid, row.doi, row.publisher_bucket,
                               row.artifact_type, "runtime_error",
                               detail=str(e))
            outcomes.append(outc)
            marker = "OK  " if outc.outcome == "ok" else "MISS"
            print(f"  → {marker} {outc.outcome} ({outc.bytes} B) "
                  f"{outc.detail[:120]}", file=sys.stderr)
            time.sleep(0.4)  # gentle pacing

        page.wait_for_timeout(3000)
        ctx.close()

    # Write outcomes TSV
    outcomes_tsv.parent.mkdir(parents=True, exist_ok=True)
    with outcomes_tsv.open("w", newline="") as fh:
        w = csv.DictWriter(fh, delimiter="\t", fieldnames=[
            "pmid", "doi", "publisher_bucket", "artifact_type",
            "outcome", "filepath", "bytes", "detail",
        ])
        w.writeheader()
        for o in outcomes:
            w.writerow({
                "pmid": o.pmid, "doi": o.doi,
                "publisher_bucket": o.publisher_bucket,
                "artifact_type": o.artifact_type,
                "outcome": o.outcome, "filepath": o.filepath,
                "bytes": o.bytes, "detail": o.detail,
            })

    # Write per-PMID reads rescues TSV (append-safe: overwrite semantics for
    # simplicity; the parent cluster pipeline will merge these into
    # coverage_review.tsv).
    reads_tsv.parent.mkdir(parents=True, exist_ok=True)
    with reads_tsv.open("w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["pmid", "reads_accessions", "reads_source",
                    "n_runs", "total_gb"])
        for pmid, f in sorted(reads_agg.items()):
            w.writerow([pmid, ",".join(f.accessions), "laptop_scrape",
                        f.n_runs, f.total_gb])

    # Summary
    n_ok = sum(1 for o in outcomes if o.outcome == "ok")
    n_cached = sum(1 for o in outcomes if o.outcome == "already_cached")
    print(f"\n[wave2] {n_ok}/{len(outcomes)} rescued this pass "
          f"({n_cached} already-cached)", file=sys.stderr)
    print(f"outcomes → {outcomes_tsv}", file=sys.stderr)
    print(f"reads rescues (per PMID) → {reads_tsv}", file=sys.stderr)
    by_bucket_art: dict[tuple[str, str], list[Outcome]] = {}
    for o in outcomes:
        by_bucket_art.setdefault((o.publisher_bucket, o.artifact_type), []).append(o)
    for (b, a), lst in sorted(by_bucket_art.items()):
        ok = sum(1 for o in lst if o.outcome in ("ok", "already_cached"))
        print(f"  {b:26s} {a:6s} {ok}/{len(lst)}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in-tsv", type=Path,
                    default=DEFAULT_DATA / "wave2_local_rescue_queue.tsv",
                    help="Wave-2 queue TSV built by build_wave2_queue.py.")
    ap.add_argument("--out-root", type=Path,
                    default=DEFAULT_DATA / "papers",
                    help="Root directory for PMID_<pmid>/ output layout.")
    ap.add_argument("--outcomes-tsv", type=Path,
                    default=DEFAULT_DATA / "wave2_local_rescue_outcomes.tsv",
                    help="Per-artifact outcome log.")
    ap.add_argument("--reads-tsv", type=Path,
                    default=DEFAULT_DATA / "wave2_local_reads_rescues.tsv",
                    help="Per-PMID reads rescue TSV.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only process the first N queue rows.")
    ap.add_argument("--no-prime", action="store_true",
                    help="Skip the initial Cloudflare-prime pass.")
    args = ap.parse_args()

    return run(
        queue_tsv=args.in_tsv,
        out_root=args.out_root,
        outcomes_tsv=args.outcomes_tsv,
        reads_tsv=args.reads_tsv,
        limit=args.limit,
        prime_first=not args.no_prime,
    )


if __name__ == "__main__":
    sys.exit(main())
