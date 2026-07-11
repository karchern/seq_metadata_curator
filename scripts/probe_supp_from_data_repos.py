#!/usr/bin/env python3
"""
Probe external data-repository hosts (Zenodo, Figshare, Dryad, OSF, CrossRef
`relation` field) for supplementary material that our publisher-CDN /
PMC-OA probes miss.

Motivation
----------
Post-2018 microbiome papers frequently deposit datasets and supp tables to
third-party repositories rather than (or in addition to) the publisher CDN.
Our current supp discovery misses these entirely. This probe closes that
gap by hitting each repo's search API for every supp-missing PMID in
coverage_review.tsv.

Approach
--------
1. Pull the 220 supp-missing rows from coverage_review.tsv.
2. For each, fetch title/DOI via Entrez batch efetch (reuses the same
   metadata that our other refresh scripts do).
3. For each row query 5 probes:
   - Zenodo: search by (a) DOI in related_identifiers, (b) exact title.
     Accept hits whose `related_identifiers` explicitly reference the
     paper's DOI. Enumerate the `files` array.
   - Figshare: (a) resource_doi lookup, (b) title search. Accept hits
     whose `resource_doi` matches the paper DOI.
   - Dryad: DOI-indexed search. Follow the dataset endpoint to list
     files.
   - OSF: title icontains + optional DOI match; enumerate files via
     the `files/osfstorage/` endpoint.
   - CrossRef: fetch `works/{DOI}`, walk `relation.isSupplementedBy` /
     `hasPart` / `hasDerivation` for external DOIs; resolve those to
     Zenodo / Figshare / Dryad records for their file lists.
4. Validate each candidate URL: GET → magic-byte sniff (PDF / ZIP /
   XLSX). Reject HTML error pages.
5. Save to data/papers/PMID_<pmid>/supp/ (creating the dir if needed).
6. As the FINAL atomic step, rewrite coverage_review.tsv with
   supp_available=True + supp_source=external:<repo> for rescued rows.
7. Emit data/deep_dive_supp_external_repos.md with per-repo hit rate.

Rate limits
-----------
Zenodo: 1 req/sec (documented at 60 req/min, we stay well under).
Figshare, Dryad, OSF, CrossRef: also 1 req/sec conservatively.

Invocation
----------
Head-session only (no heavy compute):
    /g/typas/Personal_Folders/Nic/miniforge3/envs/pyhmmer/bin/python \\
        scripts/probe_supp_from_data_repos.py [--limit N] [--dry-run]

--limit N restricts to first N supp-missing rows (for smoke testing).
--dry-run skips file downloads and TSV rewrite; still emits report.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_coverage import batch_metadata  # noqa: E402

# ---------------------------- Constants -------------------------------------

PROJECT_ROOT = Path("/scratch/karcher/seq_metadata_curator")
REVIEW_TSV = PROJECT_ROOT / "data/coverage_review.tsv"
PAPERS_DIR = PROJECT_ROOT / "data/papers"
REPORT_MD = PROJECT_ROOT / "data/deep_dive_supp_external_repos.md"

EMAIL = "karchernic@gmail.com"
UA = f"seq_metadata_curator/1.0 (mailto:{EMAIL})"

HEADERS = {
    "User-Agent": f"Mozilla/5.0 (compatible; {UA})",
    "Accept": "application/json",
}

RATE_LIMIT_S = 1.05  # per host
MAX_FILE_MB_ACCEPT = 500  # skip huge single files (>500 MB) — likely raw reads, out of scope

# Accept only these magic bytes as valid supp file content.
MAGIC_ACCEPTS = [
    (b"%PDF", "pdf"),
    (b"PK\x03\x04", "zip/xlsx/docx"),
    (b"\xd0\xcf\x11\xe0", "ole/xls/doc"),
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bz2"),
    (b"7z\xbc\xaf", "7z"),
    (b"Rar!", "rar"),
    (b"ustar", "tar"),          # at offset 257 usually — approximate
    (b"<?xml", "xml"),
    (b"<!DOCTYPE svg", "svg"),
    (b"\x89PNG", "png"),
]

# Text CSV / TSV headers vary; sniff by first 512 chars for delimiter presence.
def looks_texty(head: bytes) -> bool:
    """Return True if head bytes look like plain text (CSV/TSV/BED/FASTA)."""
    if not head:
        return False
    # Reject obvious HTML/JSON error pages
    lower = head[:200].lower().lstrip()
    if lower.startswith((b"<html", b"<!doctype html", b"{\"error", b"{\"detail")):
        return False
    # Accept if mostly printable ASCII/UTF-8
    printable = sum(1 for b in head if 9 <= b <= 13 or 32 <= b <= 126 or b >= 128)
    return printable / max(len(head), 1) > 0.9


def sniff_magic(payload: bytes) -> Optional[str]:
    """Return short label of detected magic, or None if unknown."""
    for magic, label in MAGIC_ACCEPTS:
        if payload.startswith(magic):
            return label
    # Tar magic sits at offset 257, not 0
    if len(payload) >= 265 and payload[257:262] == b"ustar":
        return "tar"
    if looks_texty(payload[:512]):
        return "text"
    return None


# ---------------------------- HTTP helpers ----------------------------------

class RateGate:
    """Simple per-host rate gate: sleep so consecutive calls stay >= interval."""
    def __init__(self, interval_s: float = RATE_LIMIT_S) -> None:
        self.interval = interval_s
        self._last: dict[str, float] = {}

    def wait(self, host: str) -> None:
        now = time.time()
        prev = self._last.get(host, 0.0)
        gap = now - prev
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last[host] = time.time()


def http_get_json(session: requests.Session, url: str, params: Optional[dict] = None,
                  gate: Optional[RateGate] = None, host: Optional[str] = None,
                  timeout: int = 20) -> Optional[dict]:
    if gate and host:
        gate.wait(host)
    try:
        r = session.get(url, params=params, headers=HEADERS, timeout=timeout)
    except (requests.ConnectionError, requests.Timeout) as e:
        print(f"  [http] ERR {url}: {e}", file=sys.stderr)
        return None
    if r.status_code == 429:
        # Zenodo occasionally 429s despite our gate — one polite retry.
        time.sleep(5.0)
        try:
            r = session.get(url, params=params, headers=HEADERS, timeout=timeout)
        except Exception:
            return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def http_download(session: requests.Session, url: str, dest: Path,
                  timeout: int = 60, max_bytes: int = MAX_FILE_MB_ACCEPT * 1024 * 1024) -> Optional[str]:
    """Stream-download URL to dest with size cap. Return magic-label str on success, None on fail."""
    try:
        r = session.get(url, headers=HEADERS, timeout=timeout, stream=True, allow_redirects=True)
    except (requests.ConnectionError, requests.Timeout) as e:
        print(f"  [dl] ERR {url}: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        try:
            r.close()
        except Exception:
            pass
        return None
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    head = b""
    try:
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if not head:
                    head = chunk[:512]
                total += len(chunk)
                if total > max_bytes:
                    r.close()
                    tmp.unlink(missing_ok=True)
                    print(f"  [dl] SKIP oversize (>{MAX_FILE_MB_ACCEPT} MB): {url}", file=sys.stderr)
                    return None
                fh.write(chunk)
    finally:
        try:
            r.close()
        except Exception:
            pass

    label = sniff_magic(head)
    if not label:
        tmp.unlink(missing_ok=True)
        print(f"  [dl] REJECT no-magic ({head[:16]!r}): {url}", file=sys.stderr)
        return None
    # Do not save texty content that looks like an OSF/Zenodo API error page
    # even if it passed looks_texty (JSON error already excluded).
    if total < 128 and label == "text":
        # tiny text files are almost never real supp; likely error stub
        tmp.unlink(missing_ok=True)
        return None
    tmp.rename(dest)
    return label


# ---------------------------- Repo probes -----------------------------------

@dataclass
class CandidateFile:
    """A single downloadable candidate discovered from a repo."""
    repo: str                      # zenodo | figshare | dryad | osf | crossref
    record_id: str
    record_title: str
    filename: str
    url: str
    size_hint: Optional[int] = None
    reason: str = ""               # why we accepted this record (rel/title/doi)


def norm_doi(doi: str) -> str:
    return (doi or "").strip().lower().removeprefix("https://doi.org/").removeprefix("doi:")


def zenodo_probe(session: requests.Session, gate: RateGate, doi: str, title: str) -> list[CandidateFile]:
    """Query Zenodo for records citing this paper's DOI or matching its title."""
    hits: list[dict] = []
    seen_ids: set[str] = set()
    ndoi = norm_doi(doi)

    # (1) DOI in related_identifiers — most precise
    if ndoi:
        j = http_get_json(
            session, "https://zenodo.org/api/records",
            params={"q": f'related.identifier:"{ndoi}"', "size": 10},
            gate=gate, host="zenodo",
        )
        if j:
            for h in j.get("hits", {}).get("hits", []) or []:
                if str(h.get("id")) not in seen_ids:
                    seen_ids.add(str(h.get("id")))
                    hits.append(h)

    # (2) Title exact match — some records don't declare related_identifiers
    if title:
        # Zenodo Elasticsearch treats double-quoted phrases as exact.
        # Strip trailing period from PubMed titles.
        safe_title = title.rstrip(".").replace('"', "'")
        j = http_get_json(
            session, "https://zenodo.org/api/records",
            params={"q": f'title:"{safe_title}"', "size": 5},
            gate=gate, host="zenodo",
        )
        if j:
            for h in j.get("hits", {}).get("hits", []) or []:
                if str(h.get("id")) not in seen_ids:
                    seen_ids.add(str(h.get("id")))
                    hits.append(h)

    candidates: list[CandidateFile] = []
    for h in hits:
        rec_id = str(h.get("id") or "")
        meta = h.get("metadata", {}) or {}
        rec_title = (meta.get("title") or "")[:200]

        # Accept only records that CLAIM a relation to our DOI (avoid
        # unrelated title-collisions from meta-reviews).
        rel_ids = meta.get("related_identifiers", []) or []
        matches_doi = any(
            norm_doi(ri.get("identifier", "")) == ndoi
            for ri in rel_ids
        )
        # If no DOI in either row (e.g. preprints) fall back to strict title
        # equality (case-insensitive, strip trailing period).
        if not matches_doi:
            zt = (meta.get("title") or "").rstrip(".").strip().lower()
            pt = (title or "").rstrip(".").strip().lower()
            if not (pt and zt and pt == zt):
                continue
            reason = "title_exact"
        else:
            reason = "related_id_doi"

        for fobj in h.get("files", []) or []:
            fname = fobj.get("key") or ""
            url = (fobj.get("links") or {}).get("self") or ""
            if not fname or not url:
                continue
            candidates.append(CandidateFile(
                repo="zenodo", record_id=rec_id, record_title=rec_title,
                filename=fname, url=url, size_hint=fobj.get("size"), reason=reason,
            ))
    return candidates


def figshare_probe(session: requests.Session, gate: RateGate, doi: str, title: str) -> list[CandidateFile]:
    """Figshare: POST /articles/search with resource_doi + title."""
    hits: list[dict] = []
    seen_ids: set[str] = set()
    ndoi = norm_doi(doi)

    def _search(payload: dict) -> list[dict]:
        gate.wait("figshare")
        try:
            r = session.post("https://api.figshare.com/v2/articles/search",
                             json=payload, headers=HEADERS, timeout=20)
        except Exception:
            return []
        if r.status_code != 200:
            return []
        try:
            return r.json() or []
        except Exception:
            return []

    if ndoi:
        for h in _search({"resource_doi": ndoi, "page_size": 10}):
            hid = str(h.get("id"))
            if hid not in seen_ids:
                seen_ids.add(hid); hits.append(h)

    # Title search is noisy — only take EXACT title matches after filter.
    if title:
        raw = _search({"search_for": title.rstrip("."), "item_type": 1, "page_size": 10})
        pt = (title or "").rstrip(".").strip().lower()
        for h in raw:
            ht = (h.get("title") or "").strip().lower()
            if pt and ht and pt == ht:
                hid = str(h.get("id"))
                if hid not in seen_ids:
                    seen_ids.add(hid); hits.append(h)

    candidates: list[CandidateFile] = []
    for h in hits:
        aid = str(h.get("id"))
        # Search results are shallow — need full record for files list
        gate.wait("figshare")
        try:
            r = session.get(f"https://api.figshare.com/v2/articles/{aid}",
                            headers=HEADERS, timeout=20)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        try:
            full = r.json()
        except Exception:
            continue
        rec_title = (full.get("title") or "")[:200]

        # Confirm the record actually references our paper DOI
        # (search's `resource_doi` filter is loose in practice).
        matches_doi = norm_doi(full.get("resource_doi") or "") == ndoi if ndoi else False
        pt = (title or "").rstrip(".").strip().lower()
        matches_title = pt and (full.get("title") or "").strip().lower() == pt
        if not (matches_doi or matches_title):
            continue
        reason = "resource_doi" if matches_doi else "title_exact"

        for fobj in full.get("files", []) or []:
            fname = fobj.get("name") or ""
            url = fobj.get("download_url") or ""
            if not fname or not url:
                continue
            candidates.append(CandidateFile(
                repo="figshare", record_id=aid, record_title=rec_title,
                filename=fname, url=url, size_hint=fobj.get("size"), reason=reason,
            ))
    return candidates


def dryad_probe(session: requests.Session, gate: RateGate, doi: str) -> list[CandidateFile]:
    """Dryad: search by publication DOI, enumerate files via versions API."""
    ndoi = norm_doi(doi)
    if not ndoi:
        return []
    j = http_get_json(
        session, "https://datadryad.org/api/v2/search",
        params={"q": ndoi}, gate=gate, host="dryad",
    )
    if not j or not j.get("_embedded", {}).get("stash:datasets"):
        return []
    candidates: list[CandidateFile] = []
    for ds in j["_embedded"]["stash:datasets"]:
        # Dryad's `relatedPublicationISSN` / articleISSN don't include DOI;
        # instead the search itself filters by publication DOI, so any hit
        # is by definition tied to this DOI.
        rec_id = ds.get("identifier") or ""
        rec_title = (ds.get("title") or "")[:200]
        # Fetch versions to find files
        vlinks = (ds.get("_links") or {}).get("stash:versions", {}).get("href", "")
        if not vlinks:
            continue
        vurl = f"https://datadryad.org{vlinks}" if vlinks.startswith("/") else vlinks
        vj = http_get_json(session, vurl, gate=gate, host="dryad")
        if not vj:
            continue
        for ver in (vj.get("_embedded") or {}).get("stash:versions", []) or []:
            flinks = (ver.get("_links") or {}).get("stash:files", {}).get("href", "")
            if not flinks:
                continue
            furl = f"https://datadryad.org{flinks}" if flinks.startswith("/") else flinks
            fj = http_get_json(session, furl, gate=gate, host="dryad")
            if not fj:
                continue
            for fobj in (fj.get("_embedded") or {}).get("stash:files", []) or []:
                fname = fobj.get("path") or ""
                dllink = (fobj.get("_links") or {}).get("stash:download", {}).get("href", "")
                if not fname or not dllink:
                    continue
                dl_url = f"https://datadryad.org{dllink}" if dllink.startswith("/") else dllink
                candidates.append(CandidateFile(
                    repo="dryad", record_id=rec_id, record_title=rec_title,
                    filename=fname, url=dl_url, size_hint=fobj.get("size"),
                    reason="publication_doi_search",
                ))
    return candidates


def osf_probe(session: requests.Session, gate: RateGate, doi: str, title: str) -> list[CandidateFile]:
    """OSF: title icontains filter, then confirm each node references our DOI."""
    if not title:
        return []
    ndoi = norm_doi(doi)
    # Use first 60 chars of title (icontains) — too-specific yields nothing
    key_frag = title.rstrip(".").strip()[:60]
    j = http_get_json(
        session, "https://api.osf.io/v2/nodes/",
        params={"filter[title][icontains]": key_frag, "page[size]": 10},
        gate=gate, host="osf",
    )
    if not j:
        return []

    candidates: list[CandidateFile] = []
    pt = (title or "").rstrip(".").strip().lower()
    for d in j.get("data", []) or []:
        node_id = d.get("id") or ""
        attrs = d.get("attributes", {}) or {}
        rec_title = (attrs.get("title") or "")[:200]
        # Confirm strict title match OR node.description contains DOI —
        # OSF is a general-purpose repo and icontains is broad
        matches_title = pt and (attrs.get("title") or "").strip().lower() == pt
        desc = (attrs.get("description") or "").lower()
        matches_doi = bool(ndoi and ndoi in desc)
        if not (matches_title or matches_doi):
            continue
        reason = "title_exact" if matches_title else "description_doi"

        # Fetch files listing
        files_url = f"https://api.osf.io/v2/nodes/{node_id}/files/osfstorage/"
        fj = http_get_json(session, files_url, gate=gate, host="osf")
        if not fj:
            continue
        for f in fj.get("data", []) or []:
            fattrs = f.get("attributes", {}) or {}
            if fattrs.get("kind") != "file":
                continue
            fname = fattrs.get("name") or ""
            dl_url = ((f.get("links") or {}).get("download")) or ""
            if not fname or not dl_url:
                continue
            candidates.append(CandidateFile(
                repo="osf", record_id=node_id, record_title=rec_title,
                filename=fname, url=dl_url, size_hint=fattrs.get("size"), reason=reason,
            ))
    return candidates


def crossref_probe(session: requests.Session, gate: RateGate, doi: str) -> list[CandidateFile]:
    """CrossRef: walk relation.hasPart / isSupplementedBy / hasDerivation.
    Resolve any pointed-to DOIs against Zenodo/Figshare for file lists.
    """
    ndoi = norm_doi(doi)
    if not ndoi:
        return []
    j = http_get_json(
        session, f"https://api.crossref.org/works/{quote_plus(ndoi)}",
        gate=gate, host="crossref",
    )
    if not j:
        return []
    rel = ((j.get("message") or {}).get("relation") or {})
    interesting = []
    for key in ("has-part", "is-supplemented-by", "has-derivation",
                "references-supplement", "documented-by"):
        for ent in rel.get(key, []) or []:
            rid = ent.get("id") or ""
            rtype = ent.get("id-type") or ""
            if not rid:
                continue
            interesting.append((key, rid, rtype))

    candidates: list[CandidateFile] = []
    for reltype, ref_id, id_type in interesting:
        rid_norm = ref_id.strip().lower()
        # Resolve Zenodo DOIs (10.5281/zenodo.NNNNN) directly
        if rid_norm.startswith("10.5281/zenodo."):
            zid = rid_norm.split(".")[-1]
            zj = http_get_json(
                session, f"https://zenodo.org/api/records/{zid}",
                gate=gate, host="zenodo",
            )
            if not zj:
                continue
            rec_title = ((zj.get("metadata") or {}).get("title") or "")[:200]
            for fobj in zj.get("files", []) or []:
                fname = fobj.get("key") or ""
                url = (fobj.get("links") or {}).get("self") or ""
                if not fname or not url:
                    continue
                candidates.append(CandidateFile(
                    repo="crossref->zenodo", record_id=zid, record_title=rec_title,
                    filename=fname, url=url, size_hint=fobj.get("size"),
                    reason=f"crossref:{reltype}",
                ))
        elif "figshare" in rid_norm:
            # Figshare DOI pattern: 10.6084/m9.figshare.NNNNN[.vN]
            m = re.search(r"figshare\.(\d+)", rid_norm)
            if not m:
                continue
            fid = m.group(1)
            gate.wait("figshare")
            try:
                r = session.get(f"https://api.figshare.com/v2/articles/{fid}",
                                headers=HEADERS, timeout=20)
                if r.status_code != 200:
                    continue
                full = r.json()
            except Exception:
                continue
            rec_title = (full.get("title") or "")[:200]
            for fobj in full.get("files", []) or []:
                fname = fobj.get("name") or ""
                url = fobj.get("download_url") or ""
                if not fname or not url:
                    continue
                candidates.append(CandidateFile(
                    repo="crossref->figshare", record_id=fid, record_title=rec_title,
                    filename=fname, url=url, size_hint=fobj.get("size"),
                    reason=f"crossref:{reltype}",
                ))
        # (Dryad DOIs also appear as 10.5061/dryad.* — resolve similarly)
        elif rid_norm.startswith("10.5061/dryad."):
            # Dryad's API lookup by DOI
            enc = quote_plus(f"doi:{rid_norm}")
            dj = http_get_json(
                session, f"https://datadryad.org/api/v2/datasets/{enc}",
                gate=gate, host="dryad",
            )
            if not dj:
                continue
            rec_title = (dj.get("title") or "")[:200]
            # Traverse versions → files as in dryad_probe
            vlinks = (dj.get("_links") or {}).get("stash:versions", {}).get("href", "")
            if not vlinks:
                continue
            vurl = f"https://datadryad.org{vlinks}" if vlinks.startswith("/") else vlinks
            vj = http_get_json(session, vurl, gate=gate, host="dryad")
            if not vj:
                continue
            for ver in (vj.get("_embedded") or {}).get("stash:versions", []) or []:
                flinks = (ver.get("_links") or {}).get("stash:files", {}).get("href", "")
                if not flinks:
                    continue
                furl = f"https://datadryad.org{flinks}" if flinks.startswith("/") else flinks
                fj = http_get_json(session, furl, gate=gate, host="dryad")
                if not fj:
                    continue
                for fobj in (fj.get("_embedded") or {}).get("stash:files", []) or []:
                    fname = fobj.get("path") or ""
                    dllink = (fobj.get("_links") or {}).get("stash:download", {}).get("href", "")
                    if not fname or not dllink:
                        continue
                    dl_url = f"https://datadryad.org{dllink}" if dllink.startswith("/") else dllink
                    candidates.append(CandidateFile(
                        repo="crossref->dryad", record_id=rid_norm, record_title=rec_title,
                        filename=fname, url=dl_url, size_hint=fobj.get("size"),
                        reason=f"crossref:{reltype}",
                    ))
    return candidates


# ---------------------------- Orchestration ---------------------------------

@dataclass
class RowResult:
    pmid: str
    doi: str
    title: str
    per_repo_hits: dict = field(default_factory=lambda: {"zenodo": 0, "figshare": 0,
                                                          "dryad": 0, "osf": 0, "crossref": 0})
    files_saved: list = field(default_factory=list)  # (repo, filename, bytes)
    err: Optional[str] = None


def dedupe_candidates(candidates: list[CandidateFile]) -> list[CandidateFile]:
    """Deduplicate by (filename, size). Prefer zenodo > figshare > dryad > osf."""
    priority = {"zenodo": 0, "figshare": 1, "dryad": 2, "osf": 3,
                "crossref->zenodo": 0, "crossref->figshare": 1, "crossref->dryad": 2}
    seen: dict[tuple, CandidateFile] = {}
    for c in candidates:
        key = (c.filename, c.size_hint)
        if key not in seen or priority.get(c.repo, 9) < priority.get(seen[key].repo, 9):
            seen[key] = c
    return list(seen.values())


def process_row(session: requests.Session, gate: RateGate, pmid: str, doi: str,
                title: str, dry_run: bool) -> RowResult:
    res = RowResult(pmid=pmid, doi=doi, title=title)

    try:
        z = zenodo_probe(session, gate, doi, title)
    except Exception as e:
        z = []
        print(f"  [zenodo] EXC {pmid}: {e}", file=sys.stderr)
    try:
        fs = figshare_probe(session, gate, doi, title)
    except Exception as e:
        fs = []
        print(f"  [figshare] EXC {pmid}: {e}", file=sys.stderr)
    try:
        dr = dryad_probe(session, gate, doi)
    except Exception as e:
        dr = []
        print(f"  [dryad] EXC {pmid}: {e}", file=sys.stderr)
    try:
        osf = osf_probe(session, gate, doi, title)
    except Exception as e:
        osf = []
        print(f"  [osf] EXC {pmid}: {e}", file=sys.stderr)
    try:
        cr = crossref_probe(session, gate, doi)
    except Exception as e:
        cr = []
        print(f"  [crossref] EXC {pmid}: {e}", file=sys.stderr)

    res.per_repo_hits = {
        "zenodo": len(z), "figshare": len(fs),
        "dryad": len(dr), "osf": len(osf), "crossref": len(cr),
    }

    all_cands = z + fs + dr + osf + cr
    if not all_cands:
        return res
    all_cands = dedupe_candidates(all_cands)

    if dry_run:
        # Even in dry-run, we log the count of candidates for reporting.
        res.files_saved = [(c.repo, c.filename, c.size_hint or 0) for c in all_cands]
        return res

    supp_dir = PAPERS_DIR / f"PMID_{pmid}" / "supp"
    supp_dir.mkdir(parents=True, exist_ok=True)

    for c in all_cands:
        # Sanitize filename — repo can supply arbitrary path components
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", c.filename)[:120] or "unnamed.bin"
        # Prefix with repo tag so we can trace provenance later
        dest = supp_dir / f"external_{c.repo.replace('->','_')}_{c.record_id}_{safe_name}"
        if dest.exists() and dest.stat().st_size > 0:
            res.files_saved.append((c.repo, c.filename, dest.stat().st_size))
            continue
        label = http_download(session, c.url, dest)
        if label is None:
            continue
        res.files_saved.append((c.repo, c.filename, dest.stat().st_size))
    return res


# ---------------------------- TSV update ------------------------------------

def recompute_gap(r: dict) -> str:
    s = 0
    if (r.get("pdf_sources") or "NONE") == "NONE":
        s += 1
    if (r.get("supp_available") or "").lower() != "true":
        s += 1
    if (r.get("reads_source") or "NONE") == "NONE":
        s += 1
    return str(s)


def update_tsv(row_results_by_pmid: dict[str, RowResult]) -> tuple[int, int]:
    """Return (rows_updated, rows_total)."""
    with REVIEW_TSV.open() as fh:
        rdr = csv.DictReader(fh, delimiter="\t")
        header = list(rdr.fieldnames or [])
        rows = list(rdr)

    n_updated = 0
    for r in rows:
        pmid = r.get("pmid") or ""
        rr = row_results_by_pmid.get(pmid)
        if not rr or not rr.files_saved:
            continue
        # Compose supp_source: external:<repos comma-list>
        repos = sorted({rec[0].replace("crossref->", "").replace("crossref", "crossref")
                        for rec in rr.files_saved})
        tag = f"external:{','.join(repos)}"
        old_source = r.get("supp_source") or "NONE"
        if old_source == "NONE":
            r["supp_source"] = tag
        elif "external:" not in old_source:
            r["supp_source"] = f"{old_source}+{tag}"
        r["supp_available"] = "True"
        r["gap_score"] = recompute_gap(r)
        n_updated += 1

    # Re-sort worst-first, matching refresh_pdf_supp.py convention
    rows.sort(
        key=lambda r: (
            -int(r.get("gap_score") or "0"),
            (r.get("journal") or "").lower(),
            r.get("pmid") or "",
        )
    )
    # Atomic rewrite
    tmp = REVIEW_TSV.with_suffix(".tsv.new")
    with tmp.open("w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=header, delimiter="\t")
        wr.writeheader()
        for r in rows:
            wr.writerow(r)
    tmp.replace(REVIEW_TSV)
    return n_updated, len(rows)


# ---------------------------- Report ----------------------------------------

def write_report(results: list[RowResult], n_rows_checked: int, n_updated: int,
                 total_bytes: int, dry_run: bool) -> None:
    per_repo_totals = {"zenodo": 0, "figshare": 0, "dryad": 0, "osf": 0, "crossref": 0}
    per_repo_rows_with_hit = {k: 0 for k in per_repo_totals}
    per_repo_rescued_files = {k: 0 for k in per_repo_totals}
    per_repo_rescued_bytes = {k: 0 for k in per_repo_totals}

    for rr in results:
        for k, v in rr.per_repo_hits.items():
            per_repo_totals[k] += v
            if v > 0:
                per_repo_rows_with_hit[k] += 1
        for repo, _fname, size in rr.files_saved:
            base_repo = repo.replace("crossref->", "").split("->")[0]
            if base_repo == "crossref":
                base_repo = "crossref"
            if base_repo in per_repo_rescued_files:
                per_repo_rescued_files[base_repo] += 1
                per_repo_rescued_bytes[base_repo] += size

    rows_with_any_rescue = sum(1 for rr in results if rr.files_saved)

    lines = []
    lines.append("# Deep-dive: supplementary discovery via external data repositories")
    lines.append("")
    lines.append(f"- Mode: {'DRY-RUN (no downloads, no TSV rewrite)' if dry_run else 'live'}")
    lines.append(f"- Rows checked: {n_rows_checked} (of 220 supp-missing baseline)")
    lines.append(f"- Rows with at least one candidate hit: "
                 f"{sum(1 for rr in results if any(rr.per_repo_hits.values()))}")
    lines.append(f"- Rows with successfully rescued files: {rows_with_any_rescue}")
    lines.append(f"- coverage_review.tsv rows updated: {n_updated}")
    lines.append(f"- Total files rescued: {sum(len(rr.files_saved) for rr in results)}")
    lines.append(f"- Total bytes rescued: {total_bytes / 1024 / 1024:.1f} MB")
    lines.append("")

    lines.append("## Per-repo funnel (raw candidate hits → downloaded files)")
    lines.append("")
    lines.append("| Repo | Candidate hits (all) | Rows with >=1 candidate | Files rescued | Bytes rescued (MB) |")
    lines.append("|------|-----------------------|---------------------------|-----------------|---------------------|")
    for k in ("zenodo", "figshare", "dryad", "osf", "crossref"):
        lines.append(
            f"| {k} | {per_repo_totals[k]} | {per_repo_rows_with_hit[k]} | "
            f"{per_repo_rescued_files[k]} | {per_repo_rescued_bytes[k] / 1024 / 1024:.1f} |"
        )
    lines.append("")

    # ROI note
    top_repo = max(per_repo_rescued_files, key=lambda k: per_repo_rescued_files[k])
    if per_repo_rescued_files[top_repo] > 0:
        lines.append(f"## ROI verdict")
        lines.append("")
        lines.append(f"- Biggest rescuer this cycle: **{top_repo}** with "
                     f"{per_repo_rescued_files[top_repo]} files "
                     f"({per_repo_rescued_bytes[top_repo] / 1024 / 1024:.1f} MB).")
        # Rank all repos by files rescued
        ranked = sorted(per_repo_rescued_files.items(), key=lambda kv: -kv[1])
        lines.append(f"- Ranking: {', '.join(f'{k}={v}' for k,v in ranked)}.")
        lines.append("")

    # Show rows where we rescued (top 20)
    rescued = [rr for rr in results if rr.files_saved]
    if rescued:
        lines.append("## Rescued rows (up to 20 shown)")
        lines.append("")
        lines.append("| PMID | DOI | # files | repos | title |")
        lines.append("|------|-----|---------|-------|-------|")
        for rr in rescued[:20]:
            repos = sorted({rec[0] for rec in rr.files_saved})
            title_short = (rr.title or "")[:60].replace("|", "/")
            lines.append(
                f"| {rr.pmid} | {rr.doi} | {len(rr.files_saved)} | "
                f"{','.join(repos)} | {title_short} |"
            )
        lines.append("")

    # Zenodo/figshare-heavy candidate hits without downloads — worth manual triage
    hit_no_rescue = [rr for rr in results
                     if any(rr.per_repo_hits.values()) and not rr.files_saved]
    if hit_no_rescue:
        lines.append("## Candidate hits without successful download (up to 10 shown)")
        lines.append("")
        lines.append("Likely: paywalled / restricted-access / oversize (>500 MB) records.")
        lines.append("")
        for rr in hit_no_rescue[:10]:
            hits_str = ", ".join(f"{k}={v}" for k, v in rr.per_repo_hits.items() if v > 0)
            lines.append(f"- PMID {rr.pmid} ({rr.doi}): {hits_str}")
        lines.append("")

    REPORT_MD.write_text("\n".join(lines))
    print(f"[report] wrote {REPORT_MD}", file=sys.stderr)


# ---------------------------- Main ------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="Only process first N supp-missing rows (0 = all)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Probe and report, but skip file downloads + TSV rewrite")
    args = ap.parse_args()

    # (1) Load supp-missing rows
    with REVIEW_TSV.open() as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    missing = [r for r in rows if (r.get("supp_available") or "").lower() != "true"]
    print(f"[main] {len(missing)} supp-missing rows in coverage_review.tsv", file=sys.stderr)
    if args.limit:
        missing = missing[: args.limit]
        print(f"[main] limited to first {len(missing)}", file=sys.stderr)

    # (2) Fetch titles via Entrez batch
    pmids = [r["pmid"] for r in missing if r.get("pmid")]
    print(f"[main] batch-fetching Entrez metadata for {len(pmids)} PMIDs ...", file=sys.stderr)
    meta = batch_metadata(pmids, EMAIL)
    print(f"[main] got metadata for {len(meta)}/{len(pmids)}", file=sys.stderr)

    # (3) Iterate — probe each row through all 5 endpoints
    session = requests.Session()
    gate = RateGate()
    results: list[RowResult] = []
    row_results_by_pmid: dict[str, RowResult] = {}

    for i, r in enumerate(missing, 1):
        pmid = r["pmid"]
        doi = r.get("doi") or ""
        m = meta.get(pmid, {})
        title = m.get("title") or ""
        # If Entrez didn't return DOI/title, we still try with what we have
        if not doi:
            doi = m.get("doi") or ""

        try:
            rr = process_row(session, gate, pmid, doi, title, args.dry_run)
        except Exception as e:
            rr = RowResult(pmid=pmid, doi=doi, title=title, err=str(e))
            print(f"  [row] EXC {pmid}: {e}", file=sys.stderr)
        results.append(rr)
        row_results_by_pmid[pmid] = rr

        # Progress logging every row (this is I/O bound; ~5-8s per row)
        hits_short = ",".join(f"{k[0]}{v}" for k, v in rr.per_repo_hits.items() if v)
        print(
            f"[{i}/{len(missing)}] pmid={pmid} doi={doi[:40]:40s} "
            f"hits=[{hits_short or 'none':30s}] rescued={len(rr.files_saved)}",
            file=sys.stderr,
        )

    # (4) Rewrite TSV (only if not dry-run)
    n_updated = 0
    if not args.dry_run:
        n_updated, _n_total = update_tsv(row_results_by_pmid)
        print(f"[main] coverage_review.tsv rows updated: {n_updated}", file=sys.stderr)

    # (5) Write report
    total_bytes = sum(size for rr in results for (_r, _f, size) in rr.files_saved)
    write_report(results, len(missing), n_updated, total_bytes, args.dry_run)

    # (6) Final summary to stderr
    n_hit_any = sum(1 for rr in results if any(rr.per_repo_hits.values()))
    n_rescued = sum(1 for rr in results if rr.files_saved)
    print("=" * 60, file=sys.stderr)
    print(f"SUMMARY: {len(results)} rows probed, {n_hit_any} with candidate hits, "
          f"{n_rescued} with rescued files, "
          f"{total_bytes / 1024 / 1024:.1f} MB downloaded.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
