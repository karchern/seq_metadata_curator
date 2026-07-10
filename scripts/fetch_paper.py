#!/usr/bin/env python3
"""
Given a PMID, fetch the paper's PDF + supplementary files + full-text XML.

Strategy tiers (tried in order, first that yields a PDF wins):
    1. PMC OA package tarball (PDF + supp + JATS XML in one shot)
    2. Europe PMC render (PDF) + supplementary-files API (if OA-full-text but no OA license)
    3. Unpaywall best-OA URL
    4. DOI redirect — only records landing URL for manual follow-up

Output layout (per PMID):
    data/papers/PMID_{pmid}/
        metadata.json     — PMID, DOI, PMC id, title, journal, year, sources tried
        paper.pdf         — main PDF
        fulltext.xml      — JATS XML (only when PMC-OA)
        supp/             — supplementary files
        fetch.log         — chronological log of attempts
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tarfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup  # only used for HTML (landing page) parsing
from Bio import Entrez

# Publisher-specific plugins (Nature, Elsevier, Wiley, …) — dispatched by DOI prefix.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from publishers import get_publisher  # noqa: E402

DEFAULT_EMAIL = "karchernic@gmail.com"
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


# ------------------------------- utilities ----------------------------------

def new_session(email: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/webp,*/*;q=0.8",
            "From": email,
        }
    )
    return s


def http_get(
    session: requests.Session, url: str, *, stream: bool = False, timeout: int = 60
) -> requests.Response:
    """GET with 3 retries + exponential backoff on 429/5xx / connection errors."""
    delay = 1.0
    last_exc: Optional[Exception] = None
    for attempt in range(4):
        try:
            r = session.get(url, stream=stream, timeout=timeout, allow_redirects=True)
            if r.status_code in (429, 500, 502, 503, 504):
                last_exc = RuntimeError(f"HTTP {r.status_code} at {url}")
                try:
                    r.close()  # avoid fd/socket leak under long-running runs
                except Exception:
                    pass
                time.sleep(delay)
                delay *= 2
                continue
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"http_get gave up after retries: {last_exc}")


def download(session: requests.Session, url: str, dest: Path) -> int:
    """Stream URL → dest. Returns bytes written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    n = 0
    with http_get(session, url, stream=True) as r:
        r.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    fh.write(chunk)
                    n += len(chunk)
    tmp.rename(dest)
    return n


# ------------------------------ metadata ------------------------------------

@dataclass
class PaperMeta:
    pmid: str
    pmc_id: Optional[str] = None
    doi: Optional[str] = None
    title: Optional[str] = None
    journal: Optional[str] = None
    year: Optional[str] = None
    is_pmc_oa: bool = False
    oa_license: Optional[str] = None
    tarball_url: Optional[str] = None
    pdf_url_used: Optional[str] = None
    xml_url_used: Optional[str] = None
    unpaywall_status: Optional[str] = None
    supp_source: Optional[str] = None
    landing_url: Optional[str] = None
    attempts: list[str] = field(default_factory=list)


def fetch_pubmed_metadata(pmid: str, email: str, api_key: Optional[str]) -> PaperMeta:
    Entrez.email = email
    if api_key:
        Entrez.api_key = api_key

    meta = PaperMeta(pmid=pmid)

    # Article-level metadata + DOI (stdlib xml — no lxml dependency)
    with Entrez.efetch(db="pubmed", id=pmid, retmode="xml") as h:
        xml_bytes = h.read()
    root = ET.fromstring(xml_bytes)
    art = root.find("PubmedArticle")
    if art is not None:
        t = art.find(".//ArticleTitle")
        if t is not None:
            title = "".join(t.itertext()).strip()  # tolerate inline <i>, <sub>, …
            if title:
                meta.title = title
        j = art.find(".//Journal")
        if j is not None:
            iso = j.find("ISOAbbreviation")
            if iso is None:
                iso = j.find("Title")
            if iso is not None and iso.text:
                meta.journal = iso.text.strip()
            y = j.find(".//Year")
            if y is not None and y.text:
                meta.year = y.text.strip()
        # Scope to the article's OWN ArticleIdList; `.//ArticleId` would
        # also match ArticleId elements inside <ReferenceList><Reference>,
        # causing us to grab the wrong PMC / DOI when the article itself
        # lacks a given ID type.
        for eid in art.findall("./PubmedData/ArticleIdList/ArticleId"):
            idtype = (eid.get("IdType") or "").lower()
            val = (eid.text or "").strip()
            if idtype == "doi" and not meta.doi:
                meta.doi = val
            elif idtype == "pmc" and not meta.pmc_id:
                meta.pmc_id = val if val.startswith("PMC") else f"PMC{val}"

    # Fallback PMC lookup: some papers omit the PMC ArticleId even when
    # they ARE in PMC. Ask PMC directly.
    if not meta.pmc_id:
        try:
            with Entrez.esearch(db="pmc", term=f"{pmid}[pmid]", retmax=1) as h:
                r = Entrez.read(h)
            ids = list(r.get("IdList", []))
            if ids:
                meta.pmc_id = f"PMC{ids[0]}"
        except Exception:
            pass
        time.sleep(0.4)

    time.sleep(0.4)
    return meta


# ---------------------------- fetch strategies ------------------------------

def try_pmc_oa_tarball(
    session: requests.Session, meta: PaperMeta, out_dir: Path
) -> bool:
    """Fetch PMC-OA tarball, extract PDF + supp + JATS XML."""
    if not meta.pmc_id:
        return False
    meta.attempts.append("pmc_oa_lookup")

    # NOTE the host: pmc.ncbi.nlm.nih.gov returns 404 for this endpoint.
    # The working host is www.ncbi.nlm.nih.gov with /pmc/ in the path.
    oa_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={meta.pmc_id}"
    r = http_get(session, oa_url)
    if r.status_code != 200:
        meta.attempts.append(f"pmc_oa_lookup:HTTP {r.status_code}")
        return False

    root = ET.fromstring(r.content)
    err = root.find(".//error")
    if err is not None:
        meta.attempts.append(f"pmc_oa_lookup:error={(err.text or '').strip()}")
        return False

    rec = root.find(".//record")
    if rec is None:
        meta.attempts.append("pmc_oa_lookup:no_record")
        return False
    meta.oa_license = rec.get("license")

    tgz_link = None
    for link in rec.findall("link"):
        if link.get("format") == "tgz":
            tgz_link = link.get("href")
            break
    if not tgz_link:
        meta.attempts.append("pmc_oa_lookup:no_tgz")
        return False

    # ftp:// → https:// (compute nodes often can't do plain FTP)
    tgz_https = tgz_link.replace("ftp://", "https://", 1)
    # NCBI moved OA tarballs to /pub/pmc/deprecated/oa_package/ but the
    # oa.fcgi metadata still advertises the old /pub/pmc/oa_package/ path
    # (which now 404s). Rewrite if we see the old shape.
    if (
        "/pub/pmc/oa_package/" in tgz_https
        and "/pub/pmc/deprecated/" not in tgz_https
    ):
        tgz_https = tgz_https.replace(
            "/pub/pmc/oa_package/",
            "/pub/pmc/deprecated/oa_package/",
            1,
        )
    meta.tarball_url = tgz_https
    meta.is_pmc_oa = True
    meta.attempts.append(f"tarball_download:{tgz_https}")

    r = http_get(session, tgz_https, stream=True)
    r.raise_for_status()
    buf = io.BytesIO(r.content)

    supp_dir = out_dir / "supp"
    supp_dir.mkdir(parents=True, exist_ok=True)
    pdf_found = False
    xml_found = False
    supp_written = False
    with tarfile.open(fileobj=buf, mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            name = Path(member.name).name  # strip PMC-dir prefix
            if not name:
                continue
            data = tf.extractfile(member)
            if data is None:
                continue
            payload = data.read()

            lower = name.lower()
            if lower.endswith(".pdf") and not pdf_found:
                # Sniff %PDF magic — some PMC-OA tarballs contain
                # replacement HTML placeholders named .pdf when the real
                # PDF was withdrawn (e.g. author-manuscript revocations).
                if not payload.startswith(b"%PDF"):
                    meta.attempts.append(
                        f"tarball_pdf:reject_not_pdf:{name}:first8="
                        f"{payload[:8]!r}"
                    )
                    continue
                (out_dir / "paper.pdf").write_bytes(payload)
                meta.pdf_url_used = f"tarball://{name}"
                pdf_found = True
            elif lower.endswith(".nxml") and not xml_found:
                # JATS full-text ships as .nxml. Bare .xml files inside
                # PMC-OA tarballs are almost always supplementary data
                # (schema files, coordinate tables, etc.) — do NOT treat
                # them as the article's fulltext.
                (out_dir / "fulltext.xml").write_bytes(payload)
                meta.xml_url_used = f"tarball://{name}"
                xml_found = True
            else:
                (supp_dir / name).write_bytes(payload)
                supp_written = True

    # Only claim supp if we ACTUALLY wrote supp files — an empty tarball
    # (nothing besides PDF+XML) shouldn't set supp_source.
    if supp_written:
        meta.supp_source = "pmc_oa_tarball"
    return pdf_found


def try_europepmc(
    session: requests.Session, meta: PaperMeta, out_dir: Path
) -> bool:
    """Fetch PDF via Europe PMC render + supplementary API (works for many PMC papers
    that aren't in the OA-tarball subset)."""
    if not meta.pmc_id:
        return False

    pmc_num = meta.pmc_id.replace("PMC", "")
    pdf_url = (
        f"https://europepmc.org/backend/ptpmcrender.fcgi?"
        f"accid={meta.pmc_id}&blobtype=pdf"
    )
    meta.attempts.append(f"europepmc_pdf:{pdf_url}")

    try:
        n = download(session, pdf_url, out_dir / "paper.pdf")
    except Exception as e:
        meta.attempts.append(f"europepmc_pdf:fail:{e}")
        return False

    if n < 8192:  # a real paper is not < 8KB
        (out_dir / "paper.pdf").unlink(missing_ok=True)
        meta.attempts.append(f"europepmc_pdf:fail:size={n}")
        return False

    # Europe PMC returns an HTML "PDF not available" page for some articles.
    # Sniff %PDF magic bytes to reject those.
    try:
        with (out_dir / "paper.pdf").open("rb") as fh:
            head = fh.read(8)
    except OSError:
        head = b""
    if not head.startswith(b"%PDF"):
        (out_dir / "paper.pdf").unlink(missing_ok=True)
        meta.attempts.append(f"europepmc_pdf:fail:not_pdf:first8={head!r}")
        return False

    meta.pdf_url_used = pdf_url

    # supp files zip (only present for OA full-text subset)
    supp_url = (
        f"https://europepmc.org/api/pmc/OA/{meta.pmc_id}/supplementaryFiles"
        f"?includeInlineImage=false"
    )
    meta.attempts.append(f"europepmc_supp:{supp_url}")
    r = None
    try:
        r = http_get(session, supp_url, stream=True)
        if r.status_code == 200:
            content = r.content  # closes stream after read
            # Reject empty (Content-Length header defaulting to "1" fooled
            # the old check into always passing) + non-zip bodies (an HTML
            # "no supp available" page can arrive here).
            if len(content) >= 4 and content.startswith(b"PK\x03\x04"):
                (out_dir / "supp").mkdir(parents=True, exist_ok=True)
                (out_dir / "supp" / "epmc_supplementary.zip").write_bytes(content)
                meta.supp_source = "europepmc_supp_zip"
            else:
                meta.attempts.append(
                    f"europepmc_supp:reject_non_zip:len={len(content)}:"
                    f"first4={content[:4]!r}"
                )
        else:
            meta.attempts.append(
                f"europepmc_supp:HTTP {r.status_code} (no supp available)"
            )
    except Exception as e:
        meta.attempts.append(f"europepmc_supp:fail:{e}")
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass

    return True


def try_unpaywall(
    session: requests.Session, meta: PaperMeta, out_dir: Path, email: str
) -> bool:
    if not meta.doi:
        return False
    url = f"https://api.unpaywall.org/v2/{meta.doi}?email={email}"
    meta.attempts.append(f"unpaywall_lookup:{url}")
    r = http_get(session, url)
    if r.status_code != 200:
        meta.attempts.append(f"unpaywall_lookup:HTTP {r.status_code}")
        return False
    try:
        j = r.json()
    except Exception as e:
        meta.attempts.append(f"unpaywall_lookup:bad_json:{e}")
        return False

    meta.unpaywall_status = j.get("oa_status")

    # Assemble candidate PDF URLs: best_oa_location + all oa_locations[].
    # Prior version only used best_oa_location; some records only expose
    # url_for_pdf under an alternate green-OA location (institutional
    # repository, figshare, etc.).
    candidates: list[str] = []
    for loc in [j.get("best_oa_location") or {}] + list(j.get("oa_locations") or []):
        u = loc.get("url_for_pdf") or loc.get("url")
        if u and u not in candidates:
            candidates.append(u)
    if not candidates:
        return False

    for pdf in candidates:
        meta.attempts.append(f"unpaywall_pdf:{pdf}")
        try:
            n = download(session, pdf, out_dir / "paper.pdf")
        except Exception as e:
            meta.attempts.append(f"unpaywall_pdf:fail:{e}")
            continue
        if n < 8192:
            (out_dir / "paper.pdf").unlink(missing_ok=True)
            meta.attempts.append(f"unpaywall_pdf:fail:size={n}")
            continue
        # Some green-OA landing pages return HTML pretending to be PDF.
        # Sniff %PDF magic bytes; only accept the URL when it delivers a
        # real PDF.
        try:
            with (out_dir / "paper.pdf").open("rb") as fh:
                head = fh.read(8)
        except OSError:
            head = b""
        if not head.startswith(b"%PDF"):
            (out_dir / "paper.pdf").unlink(missing_ok=True)
            meta.attempts.append(f"unpaywall_pdf:fail:not_pdf:first8={head!r}")
            continue
        meta.pdf_url_used = pdf
        break
    else:
        # No candidate delivered a real PDF.
        return False

    # meta.pdf_url_used was set inside the loop above.
    return True


def try_doi_landing(
    session: requests.Session, meta: PaperMeta, out_dir: Path
) -> bool:
    """Follow DOI to the publisher landing page. Do NOT attempt aggressive
    scraping — just record where we landed so a human can follow up."""
    if not meta.doi:
        return False
    url = f"https://doi.org/{meta.doi}"
    meta.attempts.append(f"doi_landing:{url}")
    try:
        r = http_get(session, url)
        meta.landing_url = r.url
        # opportunistic scan for a "download PDF" link — publisher-neutral
        # heuristic; will miss most and that's fine for v1.
        soup = BeautifulSoup(r.text, "html.parser")
        candidates: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            label = (a.get_text() or "").lower()
            if href.lower().endswith(".pdf") or "pdf" in label:
                candidates.append(href)
        (out_dir / "landing_candidates.txt").write_text(
            f"{meta.landing_url}\n\n" + "\n".join(sorted(set(candidates)))
        )
    except Exception as e:
        meta.attempts.append(f"doi_landing:fail:{e}")
    return False  # never claim PDF here; leave to human inspection


# --------------------------------- main -------------------------------------

def process_one(
    pmid: str, out_root: Path, email: str, api_key: Optional[str], force: bool
) -> PaperMeta:
    out_dir = out_root / f"PMID_{pmid}"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "fetch.log"

    # Early-exit REVISED (R4-5 + R5-2): only skip the PDF pipeline when
    # paper.pdf exists. Still run the supp pipeline — a prior fetch may
    # have landed a PDF but zero supp (e.g. PMC-OA tarball delivered PDF,
    # but publisher supp lives on the publisher CDN).
    #
    # When metadata.json is missing / corrupt / schema-drifted, DO NOT fall
    # back to `PaperMeta(pmid=pmid)` — that would give empty DOI + PMC ID,
    # `get_publisher(None) → None`, and the supp pipeline no-ops (defeating
    # R4-5's whole intent). Worse, the empty PaperMeta would overwrite the
    # existing metadata.json at the end of process_one, causing data loss.
    # Instead re-fetch metadata via NCBI for continuity.
    skip_pdf_fetch = (out_dir / "paper.pdf").exists() and not force
    if skip_pdf_fetch:
        print(
            f"[{pmid}] paper.pdf already present — skipping PDF fetch, "
            f"still running supp pipeline (use --force to redo everything)",
            file=sys.stderr,
        )
        session = new_session(email)
        need_refetch = False
        try:
            loaded = json.loads((out_dir / "metadata.json").read_text())
            meta = PaperMeta(**loaded)
            # Sanity: if the on-disk record has no DOI/PMC (empty file or
            # schema-drifted), re-fetch so the supp pipeline can dispatch.
            if not (meta.doi or meta.pmc_id):
                need_refetch = True
        except Exception:
            need_refetch = True

        if need_refetch:
            fresh = fetch_pubmed_metadata(pmid, email, api_key)
            # Preserve supp_source if supp/ actually contains files (R-2):
            # the refetch would otherwise wipe a previously-established
            # supp_source, and publisher.fetch_supp's newly_added=∅ on a
            # complete re-run would fail to re-tag it. Coverage report /
            # refresh scripts can then re-derive the exact source, but at
            # least "some supp exists" is preserved.
            supp_dir_probe = out_dir / "supp"
            if supp_dir_probe.exists():
                non_manifest_files = [
                    p for p in supp_dir_probe.iterdir()
                    if p.is_file()
                    and p.name != "manifest.tsv"
                    and not p.name.endswith(".part")
                ]
                if non_manifest_files:
                    fresh.supp_source = "existing_on_disk"
            meta = fresh
        pdf_ok = True
    else:
        session = new_session(email)
        meta = fetch_pubmed_metadata(pmid, email, api_key)

    print(
        f"[{pmid}] title={meta.title!r} doi={meta.doi} pmc={meta.pmc_id}",
        file=sys.stderr,
    )

    # ---- PDF pipeline: PMC-OA → Europe PMC → Publisher → Unpaywall → DOI ----
    # SKIPPED when paper.pdf already exists on disk (see R4-5 early-exit
    # above). Supp pipeline still runs.
    publisher = get_publisher(meta.doi)
    if not skip_pdf_fetch:
        pdf_ok = False
        if meta.pmc_id:
            try:
                pdf_ok = try_pmc_oa_tarball(session, meta, out_dir)
            except Exception as e:
                meta.attempts.append(f"pmc_oa_tarball:exception:{e}")
            if not pdf_ok:
                try:
                    pdf_ok = try_europepmc(session, meta, out_dir)
                except Exception as e:
                    meta.attempts.append(f"europepmc:exception:{e}")

        if publisher and not pdf_ok:
            try:
                res = publisher.fetch_pdf(session, meta.doi, out_dir)
                meta.attempts.extend(res.attempts)
                if res.pdf_path is not None:
                    pdf_ok = True
                    meta.pdf_url_used = res.pdf_url
            except Exception as e:
                meta.attempts.append(f"publisher_pdf:exception:{e}")

        if not pdf_ok:
            try:
                pdf_ok = try_unpaywall(session, meta, out_dir, email)
            except Exception as e:
                meta.attempts.append(f"unpaywall:exception:{e}")

        if not pdf_ok:
            try:
                try_doi_landing(session, meta, out_dir)
            except Exception as e:
                meta.attempts.append(f"doi_landing:exception:{e}")

    # ---- Supp pipeline: independent of PDF. Publisher runs even if PMC gave
    # a PDF, because a paper can be OA-in-PMC yet the FULL supp set lives on
    # the publisher CDN. The prior `supp_dir_empty` gate silently skipped
    # publisher-supp whenever PMC-OA had written *anything* into supp/, which
    # contradicted the docstring above.
    #
    # We now always call publisher.fetch_supp when a publisher exists (each
    # publisher's fetch_supp is idempotent — it skips files already on disk).
    # We only tag the publisher as a supp source when it actually WROTE new
    # files (R4-8): the skip-existing branch appends to `result.supp_files`
    # too, which would otherwise cause "supp_source=pmc_oa+publisher:X" to
    # appear even when the publisher contributed nothing on this run.
    supp_dir = out_dir / "supp"
    supp_files_before: set[str] = set()
    if supp_dir.exists():
        supp_files_before = {p.name for p in supp_dir.iterdir() if p.is_file()}
    if publisher:
        try:
            res = publisher.fetch_supp(session, meta.doi, out_dir)
            meta.attempts.extend(res.attempts)
        except Exception as e:
            meta.attempts.append(f"publisher_supp:exception:{e}")

        # Compute the newly-added diff OUTSIDE the try (per P-2). If
        # publisher.fetch_supp raised mid-download after writing 2 of 5
        # files, those 2 files are on disk and should count toward the
        # publisher's contribution. Wrapping the diff inside the try would
        # silently ignore them on the exception path.
        supp_files_after: set[str] = set()
        if supp_dir.exists():
            supp_files_after = {
                p.name for p in supp_dir.iterdir() if p.is_file()
            }
        # Publisher fetch_supp writes a manifest.tsv even when it wrote
        # nothing else (R5-5). Excluding it from the diff prevents
        # spurious publisher-tag inflation on a re-run where all supp
        # files already existed and only the manifest was rewritten.
        # Also exclude `.part` files (S-1): a mid-download exception can
        # leave an incomplete `foo.pdf.part` on disk which otherwise gets
        # counted as a new supp file and inflates the publisher tag.
        newly_added = {
            n for n in (supp_files_after - supp_files_before)
            if n != "manifest.tsv" and not n.endswith(".part")
        }
        if newly_added:
            pub_tag = f"publisher:{publisher.name}"
            if not meta.supp_source:
                meta.supp_source = pub_tag
            elif pub_tag not in meta.supp_source:
                meta.supp_source = f"{meta.supp_source}+{pub_tag}"

    (out_dir / "metadata.json").write_text(json.dumps(asdict(meta), indent=2))
    log_path.write_text("\n".join(meta.attempts) + "\n")

    n_supp = 0
    if supp_dir.exists():
        n_supp = sum(1 for _ in supp_dir.iterdir() if _.is_file())
    print(
        f"[{pmid}] pdf={'OK' if pdf_ok else 'MISS'} "
        f"supp={meta.supp_source or '-'} n_supp_files={n_supp} "
        f"attempts={len(meta.attempts)}",
        file=sys.stderr,
    )
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pmid", help="A single PMID.")
    src.add_argument("--pmid-file", type=Path, help="File with one PMID per line.")
    ap.add_argument(
        "--out-root",
        type=Path,
        default=Path("/scratch/karcher/seq_metadata_curator/data/papers"),
    )
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.pmid:
        pmids = [args.pmid.strip()]
    else:
        pmids = [
            ln.strip() for ln in args.pmid_file.read_text().splitlines() if ln.strip()
        ]

    args.out_root.mkdir(parents=True, exist_ok=True)

    fails = 0
    for pmid in pmids:
        try:
            meta = process_one(
                pmid, args.out_root, args.email, args.api_key, args.force
            )
            if not meta.pdf_url_used:
                fails += 1
        except Exception as e:
            print(f"[{pmid}] FATAL: {e}", file=sys.stderr)
            fails += 1
        time.sleep(0.4)

    print(
        f"[fetch_paper] done — {len(pmids) - fails}/{len(pmids)} PDFs acquired",
        file=sys.stderr,
    )
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
