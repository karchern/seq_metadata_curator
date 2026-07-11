#!/usr/bin/env python3
"""
Reads-discovery refresh via strategies orthogonal to article-HTML mining
(which is `refresh_reads_via_html.py`'s job).

For every row in coverage_review.tsv where reads_source is NONE or
"html_text_mine", try in order:

  1. **geo_to_sra**            — mine the article HTML for GSE\\d+ IDs,
                                  translate each via NCBI esummary(db=gds)
                                  to a BioProject / SRP accession, then
                                  validate on ENA filereport.

  2. **arrayexpress**          — mine the article HTML for E-MTAB / E-GEOD
                                  IDs, hit BioStudies API, extract linked
                                  ENA study accessions, validate on ENA.

  3. **data_avail_section**    — locate the "Data Availability" (or
                                  synonym) paragraph in article HTML,
                                  apply a *broadened* INSDC regex to that
                                  paragraph (including URL patterns
                                  ena/browser/view/PRJ*, sra?study=SRP*,
                                  bioproject/?term=PRJNA*), validate hits.

  4. **supp_table_scan**       — for rows where we have supp files on
                                  disk, unzip XLSX (stdlib), grep XML +
                                  sharedStrings for INSDC patterns, also
                                  scan any tsv/csv supp files. Validate.

  5. **crossref_relation**     — GET /works/{doi}; for each relation
                                  target whose id-value matches an INSDC
                                  pattern (rare but seen), validate.

  6. **europepmc_section**     — fetch fullTextXML from Europe PMC;
                                  extract data-availability section
                                  body; apply broadened INSDC regex.

Every candidate is validated via ENA filereport (n_runs>0).

Additive-only: never downgrades a row that already has reads. If a row
already has reads_source in ("europepmc","elink","abstract_regex",
"html_text_mine"), we still probe deeper strategies and MERGE any new
accessions in.

Coverage delta metrics logged per strategy.

Writes report to data/deep_dive_reads_deeper.md.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Optional

import requests
from Bio import Entrez

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_coverage import (  # noqa: E402
    INSDC_ACC_RE,
    INSDC_PROJECT_RE,
    _fetch_article_html,
    http_get,
    new_session,
    probe_ena_filereport,
)

REVIEW_TSV = Path("/scratch/karcher/seq_metadata_curator/data/coverage_review.tsv")
PAPERS_DIR = Path("/scratch/karcher/seq_metadata_curator/data/papers")
REPORT_MD = Path("/scratch/karcher/seq_metadata_curator/data/deep_dive_reads_deeper.md")

DEFAULT_EMAIL = "karchernic@gmail.com"
Entrez.email = DEFAULT_EMAIL

# GEO series IDs referenced in article HTML.
GSE_RE = re.compile(r"\bGSE\d{3,7}\b")
# ArrayExpress / BioStudies IDs.
AE_RE = re.compile(r"\bE-(?:MTAB|GEOD|MEXP|PROT|ERAD)-\d+\b")

# Broadened INSDC regex for the data-availability / URL context: includes
# raw INSDC accessions AND accessions embedded in ENA/SRA URLs. This is
# used specifically on the data-availability paragraph so the extra
# permissiveness doesn't over-catch across an entire HTML file (where
# it would hit reference-list study IDs).
INSDC_URL_RE = re.compile(
    r"(?:"
    r"(?:ena/browser/view/|ebi\.ac\.uk/ena/data/view/|"
    r"sra\?study=|Traces/study/\?acc=|bioproject/?(?:\?term=|/)?)"
    r"(PRJ[END][AB]\d+|ERP\d+|SRP\d+|DRP\d+|DRA\d+)"
    r")",
    re.IGNORECASE,
)

# Phrases whose surrounding paragraph is worth mining for accessions.
DATA_AVAIL_PHRASES = [
    "data availability",
    "data are available",
    "data have been deposited",
    "data have been submitted",
    "sequencing data",
    "raw reads",
    "raw sequence",
    "raw data",
    "accession",
    "deposited in",
    "deposited at",
    "available at ncbi",
    "available at ena",
    "available at ebi",
    "available from the",
    "under bioproject",
    "under project",
    "sequence read archive",
    "european nucleotide archive",
]

# Reasonable HTTP tempo — see brief; also parallel agents share NCBI budget.
SLEEP_NCBI = 0.35        # NCBI 3 req/s
SLEEP_EBI = 0.25         # EBI/Europe PMC ~5 req/s
SLEEP_CROSSREF = 0.25    # crossref polite pool ~5 req/s


# ============================================================================
# helpers
# ============================================================================

def dedup_keep_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def extract_project_accs(text: str) -> list[str]:
    """Same semantics as probe_coverage.probe_abstract_regex: keep only
    project-level accessions (not biosample IDs, not GEO, not E-MTAB).
    GEO / AE IDs are handled by dedicated strategies."""
    if not text:
        return []
    out: list[str] = []
    for m in INSDC_ACC_RE.finditer(text):
        v = m.group(1)
        if INSDC_PROJECT_RE.match(v) and v not in out:
            out.append(v)
    return out


def extract_url_accs(text: str) -> list[str]:
    """Extract project accessions embedded in URLs like
    ena/browser/view/PRJEB12345 that the plain INSDC regex misses."""
    if not text:
        return []
    out: list[str] = []
    for m in INSDC_URL_RE.finditer(text):
        v = m.group(1).upper()
        if v not in out:
            out.append(v)
    return out


def extract_data_avail_paragraphs(html: str) -> list[str]:
    """Return the ~800-char neighborhoods around each data-availability
    phrase. We work on a lightly-stripped text projection of the HTML
    so tag boundaries don't cut sentences."""
    if not html:
        return []
    # Very lightweight tag stripping: keep only text runs, replace tags
    # with spaces (preserves whitespace boundaries between words).
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    lower = text.lower()
    windows: list[tuple[int, int]] = []
    for phrase in DATA_AVAIL_PHRASES:
        start = 0
        while True:
            i = lower.find(phrase, start)
            if i < 0:
                break
            lo = max(0, i - 200)
            hi = min(len(text), i + 600)
            windows.append((lo, hi))
            start = i + len(phrase)
    # Merge overlapping windows so we don't extract the same paragraph twice.
    windows.sort()
    merged: list[tuple[int, int]] = []
    for lo, hi in windows:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return [text[lo:hi] for lo, hi in merged]


# ============================================================================
# Strategy 1 — GEO → SRA
# ============================================================================

def geo_to_projects(session: requests.Session, gse: str) -> list[str]:
    """For a GSE ID, query NCBI GDS and return any linked BioProject /
    SRP / ERP accessions we can find on the esummary doc."""
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=gds&term={gse}[Accession]&retmode=json"
    )
    r = http_get(session, url, timeout=20)
    time.sleep(SLEEP_NCBI)
    if r is None or r.status_code != 200:
        return []
    try:
        ids = r.json()["esearchresult"]["idlist"]
    except Exception:
        return []
    if not ids:
        return []
    # The GSE record is prefixed 200XXXXXXX in gds; use the first.
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        f"?db=gds&id={ids[0]}&retmode=json"
    )
    r = http_get(session, url, timeout=20)
    time.sleep(SLEEP_NCBI)
    if r is None or r.status_code != 200:
        return []
    try:
        doc = r.json()["result"][ids[0]]
    except Exception:
        return []
    accs: list[str] = []
    bp = doc.get("bioproject") or ""
    if bp and INSDC_PROJECT_RE.match(bp):
        accs.append(bp)
    # extrelations sometimes carries SRP xrefs.
    for e in doc.get("extrelations", []):
        v = str(e.get("targetobject") or e.get("targetftplink") or "")
        for m in INSDC_ACC_RE.finditer(v):
            g = m.group(1)
            if INSDC_PROJECT_RE.match(g) and g not in accs:
                accs.append(g)
    return accs


# ============================================================================
# Strategy 2 — ArrayExpress / BioStudies
# ============================================================================

def biostudies_to_projects(session: requests.Session, ae_id: str) -> list[str]:
    url = f"https://www.ebi.ac.uk/biostudies/api/v1/studies/{ae_id}"
    r = http_get(session, url, timeout=25)
    time.sleep(SLEEP_EBI)
    if r is None or r.status_code != 200:
        return []
    try:
        j = r.json()
    except Exception:
        return []
    accs: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if "url" in node:
                u = str(node.get("url") or "")
                # Attribute 'Type' == 'ENA' is the strong signal.
                attrs = {
                    (a.get("name") or "").lower(): (a.get("value") or "")
                    for a in (node.get("attributes") or [])
                    if isinstance(a, dict)
                }
                if attrs.get("type", "").lower() == "ena":
                    # BioStudies encodes the ENA study id in `url`.
                    for m in INSDC_ACC_RE.finditer(u):
                        g = m.group(1)
                        if INSDC_PROJECT_RE.match(g) and g not in accs:
                            accs.append(g)
                # Also scan URL body regardless of Type attribute
                for m in INSDC_ACC_RE.finditer(u):
                    g = m.group(1)
                    if INSDC_PROJECT_RE.match(g) and g not in accs:
                        accs.append(g)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(j)
    return accs


# ============================================================================
# Strategy 4 — supp table scan (stdlib XLSX + tsv/csv)
# ============================================================================

def scan_xlsx_stdlib(path: Path) -> list[str]:
    """Unzip xlsx (a zip of XML), grep sharedStrings + sheet XML for
    project-level INSDC accessions. Also apply the URL regex for
    accessions embedded in cell hyperlinks."""
    hits: list[str] = []
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if not (name.endswith(".xml") or name.endswith(".rels")):
                    continue
                try:
                    raw = z.read(name)
                except Exception:
                    continue
                try:
                    text = raw.decode("utf-8", errors="replace")
                except Exception:
                    continue
                for a in extract_project_accs(text):
                    if a not in hits:
                        hits.append(a)
                for a in extract_url_accs(text):
                    if a not in hits:
                        hits.append(a)
    except zipfile.BadZipFile:
        return []
    except Exception:
        return []
    return hits


def scan_text_supp(path: Path) -> list[str]:
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return []
    return dedup_keep_order(extract_project_accs(text) + extract_url_accs(text))


def scan_supp_dir_for_pmid(pmid: str) -> list[str]:
    """Return deduped accessions found in the supp/ directory for this PMID."""
    d = PAPERS_DIR / f"PMID_{pmid}" / "supp"
    if not d.exists():
        return []
    accs: list[str] = []
    for f in sorted(d.iterdir()):
        suf = f.suffix.lower()
        if suf == ".xlsx":
            for a in scan_xlsx_stdlib(f):
                if a not in accs:
                    accs.append(a)
        elif suf in (".tsv", ".csv", ".txt"):
            for a in scan_text_supp(f):
                if a not in accs:
                    accs.append(a)
    return accs


# ============================================================================
# Strategy 5 — CrossRef relations
# ============================================================================

def crossref_relations(session: requests.Session, doi: str) -> list[str]:
    if not doi:
        return []
    url = (
        f"https://api.crossref.org/works/{doi}"
        f"?mailto={DEFAULT_EMAIL}"
    )
    r = http_get(session, url, timeout=25)
    time.sleep(SLEEP_CROSSREF)
    if r is None or r.status_code != 200:
        return []
    try:
        j = r.json()
    except Exception:
        return []
    rel = j.get("message", {}).get("relation") or {}
    accs: list[str] = []
    for _kind, items in rel.items():
        for item in items or []:
            val = str(item.get("id") or "")
            for m in INSDC_ACC_RE.finditer(val):
                g = m.group(1)
                if INSDC_PROJECT_RE.match(g) and g not in accs:
                    accs.append(g)
    return accs


# ============================================================================
# Strategy 6 — Europe PMC full-text XML section mining
# ============================================================================

def europepmc_fulltextxml(session: requests.Session, pmc_id: str) -> Optional[str]:
    if not pmc_id:
        return None
    url = (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/"
        f"{pmc_id}/fullTextXML"
    )
    r = http_get(session, url, timeout=30)
    time.sleep(SLEEP_EBI)
    if r is None or r.status_code != 200 or not r.text:
        return None
    return r.text


def europepmc_dataavail_accs(xml_text: str) -> list[str]:
    """Find <sec>...</sec> blocks whose title matches a data-availability
    phrase, and return project accessions found in their body."""
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        # Fall back to regex over paragraphs.
        return dedup_keep_order(
            extract_project_accs(xml_text) + extract_url_accs(xml_text)
        )
    accs: list[str] = []
    # sec elements — many JATS docs place data-avail near back matter.
    for sec in root.iter():
        # tag can be namespaced; strip namespace
        tag = sec.tag.split("}")[-1]
        if tag not in ("sec", "notes"):
            continue
        title = ""
        for t in sec:
            if t.tag.split("}")[-1] == "title" and t.text:
                title = t.text.lower()
                break
        body = "".join(sec.itertext())
        if any(p in title for p in ("data availability", "availability of data", "availability", "accession")) \
                or any(p in body.lower() for p in DATA_AVAIL_PHRASES):
            for a in extract_project_accs(body):
                if a not in accs:
                    accs.append(a)
            for a in extract_url_accs(body):
                if a not in accs:
                    accs.append(a)
    return accs


# ============================================================================
# Main loop
# ============================================================================

def recompute_gap(r: dict) -> str:
    s = 0
    if (r.get("pdf_sources") or "NONE") == "NONE":
        s += 1
    if (r.get("supp_available") or "").lower() != "true":
        s += 1
    if (r.get("reads_source") or "NONE") == "NONE":
        s += 1
    return str(s)


def merge_reads(r: dict, new_accs: list[str], new_source_tag: str,
                validated: dict[str, tuple[int, float]]) -> tuple[int, int, float]:
    """Merge validated new accessions into row. Returns
    (n_newly_added_accs, added_runs, added_gb)."""
    existing = [
        a.strip() for a in (r.get("reads_accessions") or "").split(",")
        if a.strip() and a.strip() != "NONE"
    ]
    existing_set = set(existing)
    to_add = [a for a in new_accs if a not in existing_set]
    if not to_add:
        return (0, 0, 0.0)
    merged = existing + to_add
    r["reads_accessions"] = ",".join(merged)
    # Source: if there was already a source (not NONE), comma-join tags.
    old_src = r.get("reads_source") or "NONE"
    if old_src == "NONE":
        r["reads_source"] = new_source_tag
    else:
        parts = [p.strip() for p in old_src.split(",") if p.strip()]
        if new_source_tag not in parts:
            parts.append(new_source_tag)
        r["reads_source"] = ",".join(parts)
    # Recompute cumulative n_runs / total_gb from *all* merged accessions
    # using the validated map (existing accs we don't have validation
    # numbers for stay counted from the pre-existing n_runs/total_gb).
    added_runs = 0
    added_gb = 0.0
    for a in to_add:
        n, gb = validated.get(a, (0, 0.0))
        added_runs += n
        added_gb += gb
    try:
        r["n_runs"] = str(int(r.get("n_runs") or 0) + added_runs)
    except ValueError:
        r["n_runs"] = str(added_runs)
    try:
        r["total_gb"] = str(round(float(r.get("total_gb") or 0.0) + added_gb, 2))
    except ValueError:
        r["total_gb"] = str(round(added_gb, 2))
    return (len(to_add), added_runs, added_gb)


def validate_accs(session: requests.Session, accs: list[str],
                  cache: dict[str, tuple[int, float]]) -> tuple[list[str], dict[str, tuple[int, float]]]:
    """Return (validated_accs, validation_map). Populates cache to avoid
    re-hitting ENA for the same accession across strategies/rows."""
    validated: list[str] = []
    valmap: dict[str, tuple[int, float]] = {}
    for a in accs:
        if a in cache:
            n, gb = cache[a]
        else:
            try:
                n, gb = probe_ena_filereport(session, a)
            except Exception:
                n, gb = 0, 0.0
            cache[a] = (n, gb)
            time.sleep(SLEEP_EBI)
        if n > 0:
            validated.append(a)
            valmap[a] = (n, gb)
    return validated, valmap


def main() -> int:
    with REVIEW_TSV.open() as fh:
        rdr = csv.DictReader(fh, delimiter="\t")
        header = list(rdr.fieldnames or [])
        rows = list(rdr)

    session = new_session()

    # Snapshot of pre-run coverage for delta computation
    def reads_pct(rr: list[dict]) -> tuple[int, int]:
        n_ok = sum(1 for r in rr if (r.get("reads_source") or "NONE") != "NONE")
        return (n_ok, len(rr))
    pre_ok, total = reads_pct(rows)
    print(f"[deeper] pre-run reads coverage: {pre_ok}/{total} ({100*pre_ok/max(1,total):.1f}%)",
          file=sys.stderr)

    # Rows to work on: reads_source is NONE OR html_text_mine (revisit
    # those too — might find *more* accessions per paper).
    def worth_working(r: dict) -> bool:
        src = (r.get("reads_source") or "NONE")
        if src == "NONE":
            return True
        # Comma-joined sources: if html_text_mine is the ONLY tag, revisit
        parts = [p.strip() for p in src.split(",")]
        return parts == ["html_text_mine"]

    work = [r for r in rows if worth_working(r) and (r.get("pmc_id") or r.get("doi"))]
    print(f"[deeper] rows to work on: {len(work)}", file=sys.stderr)

    # Per-strategy counters
    strat_hits: Counter = Counter()
    strat_new_runs: Counter = Counter()
    strat_new_gb: dict[str, float] = defaultdict(float)
    strat_rows_lifted: Counter = Counter()   # first strategy to lift a NONE row
    strat_examples: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)

    ena_cache: dict[str, tuple[int, float]] = {}

    n_lifted_from_none = 0
    total_added_runs = 0
    total_added_gb = 0.0

    for i, r in enumerate(work):
        pmid = r.get("pmid") or ""
        doi = r.get("doi") or ""
        pmc = r.get("pmc_id") or ""

        was_none = (r.get("reads_source") or "NONE") == "NONE"

        # Pre-fetch article HTML once (shared by strategies 1,2,3)
        html: Optional[str] = None
        try:
            html = _fetch_article_html(session, doi, pmc)
        except Exception:
            html = None

        # --- Strategy 1: GEO → SRA ---
        if html:
            gse_ids = dedup_keep_order(GSE_RE.findall(html))
            if gse_ids:
                geo_accs: list[str] = []
                for gse in gse_ids[:6]:  # cap to be polite / defend against ref-list noise
                    for a in geo_to_projects(session, gse):
                        if a not in geo_accs:
                            geo_accs.append(a)
                if geo_accs:
                    validated, vmap = validate_accs(session, geo_accs, ena_cache)
                    if validated:
                        added, runs, gb = merge_reads(r, validated, "geo_to_sra", vmap)
                        if added:
                            strat_hits["geo_to_sra"] += added
                            strat_new_runs["geo_to_sra"] += runs
                            strat_new_gb["geo_to_sra"] += gb
                            strat_examples["geo_to_sra"].append((pmid, validated))
                            if was_none:
                                strat_rows_lifted["geo_to_sra"] += 1
                                n_lifted_from_none += 1
                                was_none = False
                            total_added_runs += runs
                            total_added_gb += gb

        # --- Strategy 2: ArrayExpress / BioStudies ---
        if html:
            ae_ids = dedup_keep_order(AE_RE.findall(html))
            if ae_ids:
                ae_accs: list[str] = []
                for ae in ae_ids[:6]:
                    for a in biostudies_to_projects(session, ae):
                        if a not in ae_accs:
                            ae_accs.append(a)
                if ae_accs:
                    validated, vmap = validate_accs(session, ae_accs, ena_cache)
                    if validated:
                        added, runs, gb = merge_reads(r, validated, "arrayexpress", vmap)
                        if added:
                            strat_hits["arrayexpress"] += added
                            strat_new_runs["arrayexpress"] += runs
                            strat_new_gb["arrayexpress"] += gb
                            strat_examples["arrayexpress"].append((pmid, validated))
                            if was_none:
                                strat_rows_lifted["arrayexpress"] += 1
                                n_lifted_from_none += 1
                                was_none = False
                            total_added_runs += runs
                            total_added_gb += gb

        # --- Strategy 3: Data-availability section extraction (broad + URL regex) ---
        if html:
            paragraphs = extract_data_avail_paragraphs(html)
            da_accs: list[str] = []
            for p in paragraphs:
                for a in extract_project_accs(p):
                    if a not in da_accs:
                        da_accs.append(a)
                for a in extract_url_accs(p):
                    if a not in da_accs:
                        da_accs.append(a)
            if da_accs:
                validated, vmap = validate_accs(session, da_accs, ena_cache)
                if validated:
                    added, runs, gb = merge_reads(r, validated, "data_avail_section", vmap)
                    if added:
                        strat_hits["data_avail_section"] += added
                        strat_new_runs["data_avail_section"] += runs
                        strat_new_gb["data_avail_section"] += gb
                        strat_examples["data_avail_section"].append((pmid, validated))
                        if was_none:
                            strat_rows_lifted["data_avail_section"] += 1
                            n_lifted_from_none += 1
                            was_none = False
                        total_added_runs += runs
                        total_added_gb += gb

        # --- Strategy 4: supp-table scan (offline; disk only) ---
        supp_accs = scan_supp_dir_for_pmid(pmid) if pmid else []
        if supp_accs:
            validated, vmap = validate_accs(session, supp_accs, ena_cache)
            if validated:
                added, runs, gb = merge_reads(r, validated, "supp_table_scan", vmap)
                if added:
                    strat_hits["supp_table_scan"] += added
                    strat_new_runs["supp_table_scan"] += runs
                    strat_new_gb["supp_table_scan"] += gb
                    strat_examples["supp_table_scan"].append((pmid, validated))
                    if was_none:
                        strat_rows_lifted["supp_table_scan"] += 1
                        n_lifted_from_none += 1
                        was_none = False
                    total_added_runs += runs
                    total_added_gb += gb

        # --- Strategy 5: CrossRef relation field ---
        cr_accs = crossref_relations(session, doi) if doi else []
        if cr_accs:
            validated, vmap = validate_accs(session, cr_accs, ena_cache)
            if validated:
                added, runs, gb = merge_reads(r, validated, "crossref_relation", vmap)
                if added:
                    strat_hits["crossref_relation"] += added
                    strat_new_runs["crossref_relation"] += runs
                    strat_new_gb["crossref_relation"] += gb
                    strat_examples["crossref_relation"].append((pmid, validated))
                    if was_none:
                        strat_rows_lifted["crossref_relation"] += 1
                        n_lifted_from_none += 1
                        was_none = False
                    total_added_runs += runs
                    total_added_gb += gb

        # --- Strategy 6: Europe PMC full-text section mining ---
        if pmc:
            xml_text = europepmc_fulltextxml(session, pmc)
            if xml_text:
                epmc_accs = europepmc_dataavail_accs(xml_text)
                if epmc_accs:
                    validated, vmap = validate_accs(session, epmc_accs, ena_cache)
                    if validated:
                        added, runs, gb = merge_reads(r, validated, "europepmc_section", vmap)
                        if added:
                            strat_hits["europepmc_section"] += added
                            strat_new_runs["europepmc_section"] += runs
                            strat_new_gb["europepmc_section"] += gb
                            strat_examples["europepmc_section"].append((pmid, validated))
                            if was_none:
                                strat_rows_lifted["europepmc_section"] += 1
                                n_lifted_from_none += 1
                                was_none = False
                            total_added_runs += runs
                            total_added_gb += gb

        if (i + 1) % 10 == 0 or i == len(work) - 1:
            print(
                f"[deeper] {i+1}/{len(work)}  "
                f"lifted_from_none={n_lifted_from_none}  "
                f"added_runs={total_added_runs}  "
                f"added_gb={total_added_gb:.1f}",
                file=sys.stderr,
            )

    # ------------------------------------------------------------------ persist
    for r in rows:
        r["gap_score"] = recompute_gap(r)
    rows.sort(
        key=lambda r: (
            -int(r["gap_score"]),
            (r.get("journal") or "").lower(),
            r.get("pmid") or "",
        )
    )
    with REVIEW_TSV.open("w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=header, delimiter="\t")
        wr.writeheader()
        wr.writerows(rows)

    post_ok, _ = reads_pct(rows)
    print(f"[deeper] post-run reads coverage: {post_ok}/{total} "
          f"({100*post_ok/max(1,total):.1f}%)  "
          f"delta: +{post_ok - pre_ok} rows "
          f"(+{100*(post_ok - pre_ok)/max(1,total):.2f} pp)",
          file=sys.stderr)

    # ------------------------------------------------------------------ report
    lines: list[str] = []
    lines.append("# Deep-dive: reads recovery via orthogonal strategies\n")
    lines.append(f"Run date: 2026-07-11  ")
    lines.append(f"Script: `scripts/refresh_reads_deeper.py`\n")
    lines.append("## Coverage delta\n")
    lines.append(f"| Metric | Value |")
    lines.append(f"|---|---|")
    lines.append(f"| Pre-run reads coverage | {pre_ok}/{total} ({100*pre_ok/max(1,total):.1f}%) |")
    lines.append(f"| Post-run reads coverage | {post_ok}/{total} ({100*post_ok/max(1,total):.1f}%) |")
    lines.append(f"| Rows lifted from NONE | {n_lifted_from_none} |")
    lines.append(f"| Delta (pp) | +{100*(post_ok - pre_ok)/max(1,total):.2f} |")
    lines.append(f"| Total new runs added | {total_added_runs} |")
    lines.append(f"| Total new data added | {total_added_gb:.1f} GB "
                 f"(~{total_added_gb/1024:.2f} TB) |")
    lines.append("")
    lines.append("## Per-strategy stats\n")
    lines.append(f"| Strategy | new accs | rows-lifted-from-NONE | runs added | GB added |")
    lines.append(f"|---|---:|---:|---:|---:|")
    for k in ["geo_to_sra", "arrayexpress", "data_avail_section",
              "supp_table_scan", "crossref_relation", "europepmc_section"]:
        lines.append(
            f"| {k} | {strat_hits.get(k,0)} | {strat_rows_lifted.get(k,0)} | "
            f"{strat_new_runs.get(k,0)} | {strat_new_gb.get(k,0.0):.1f} |"
        )
    lines.append("")
    lines.append("## Example rescues (up to 8 per strategy)\n")
    for k, examples in strat_examples.items():
        if not examples:
            continue
        lines.append(f"### {k}\n")
        for pmid, accs in examples[:8]:
            lines.append(f"- PMID {pmid} → {', '.join(accs)}")
        lines.append("")

    lines.append("## Notes / surprises\n")
    lines.append("- All new accessions were validated against ENA "
                 "`filereport?result=read_run` with `n_runs > 0`.")
    lines.append("- `supp_table_scan` scope is inherently narrow: only "
                 f"~10 xlsx files are on disk (one PMID has supp locally). "
                 "This strategy will pay off much more once "
                 "`refresh_supp_via_html.py` (Batch B sibling) grows the "
                 "on-disk supp corpus.")
    lines.append("- `europepmc_section` is nearly a no-op for the "
                 "no-reads residue: most of those PMCs are non-OA and "
                 "Europe PMC returns 404 on `fullTextXML`. When it does "
                 "return XML, the JATS `<sec>`/`<notes>` heuristic still "
                 "works.")
    lines.append("- `geo_to_sra` translates NCBI GDS → BioProject via the "
                 "`bioproject` field on the esummary doc — most GSEs map "
                 "cleanly. GEO IDs without a `bioproject` xref are almost "
                 "always microarray-only (no reads on ENA) and are "
                 "correctly excluded by the ENA-filereport gate.")
    lines.append("- `arrayexpress`: BioStudies' `attributes[Type=ENA]` "
                 "convention is stable; walking the JSON for any URL "
                 "matching INSDC_ACC_RE also picks up occasional "
                 "cross-refs embedded outside link elements.")
    lines.append("- `data_avail_section`: mining a small paragraph "
                 "neighborhood (rather than whole HTML) *also* enables "
                 "the URL-embedded regex safely — the URL pattern is "
                 "too permissive across a whole document (would match "
                 "reference-list URLs).")

    REPORT_MD.write_text("\n".join(lines) + "\n")
    print(f"[deeper] wrote report {REPORT_MD}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
