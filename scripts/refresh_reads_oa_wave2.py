#!/usr/bin/env python3
"""
Wave-2 OA reads rescue driver.

Purpose
-------
Wave-1 `refresh_reads_deeper.py` ran BEFORE `Frontiers`, `MDPI`, `BMC`,
`science_aaas`, and `cell_press` plugins were wired into
`probe_coverage._fetch_article_html()`. Article-HTML-mining strategies
therefore never reached ~123 OA reads gaps in those publishers'
coverage_review.tsv rows. This driver reruns the same reads-deep-dive
strategies on those rows now that dispatch is fixed.

Scope: rows where `reads_source == "NONE"` AND doi_prefix in the OA
publisher clusters:

    10.3389 Frontiers        (~31 gaps)
    10.3390 MDPI             (~37 gaps)
    10.1186 BMC              (~30 gaps)
    10.1007 Springer         (~25 gaps)
    10.1126 Science          (~2 gaps)
    10.1016 Elsevier / Cell  (~43 gaps — Cell Press subset dispatched)

Rows in `data/master_paper_disposition.tsv` with priority `IGNORE-*` are
SKIPPED — they're excluded from coverage numbers by design.

Strategies (identical to refresh_reads_deeper.py's four winning paths):
  W1. Whole-HTML INSDC regex (broadened patterns for DDBJ, ArrayExpress)
  W2. `data availability` section extraction + INSDC/URL regex
  W3. GSE\\d+ → NCBI GDS esummary → BioProject/SRP
  W4. E-MTAB-\\d+ / E-GEOD-\\d+ → BioStudies → ENA study accession

Every candidate is validated against ENA filereport (n_runs > 0).

Output (NO in-place mutation of coverage_review.tsv):
    data/wave2_oa_reads_rescues.tsv  — audit trail (one row per rescue)
    logs/wave2_oa_reads_<ts>.log     — via tee at invocation

A separate finalization step (merge_wave2_into_review) can be invoked with
`--merge` after the rescue pass finishes, and it will:
  1. git pull --rebase (caller does this — merge step assumes fresh HEAD)
  2. Load coverage_review.tsv
  3. Additive merge: for each rescue PMID, add its accessions to the row
     without downgrading anything.
  4. Recompute gap_score, re-sort worst-first.
  5. Rewrite coverage_review.tsv.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_coverage import (  # noqa: E402
    _fetch_article_html,
    http_get,
    new_session,
    probe_ena_filereport,
)
from publishers import get_publisher  # noqa: E402
from refresh_reads_deeper import (  # noqa: E402
    AE_RE,
    GSE_RE,
    biostudies_to_projects,
    dedup_keep_order,
    extract_data_avail_paragraphs,
    extract_project_accs,
    extract_url_accs,
    geo_to_projects,
    recompute_gap,
    SLEEP_EBI,
)

REPO = Path("/scratch/karcher/seq_metadata_curator")
REVIEW_TSV = REPO / "data" / "coverage_review.tsv"
MASTER_TSV = REPO / "data" / "master_paper_disposition.tsv"
RESCUES_TSV = REPO / "data" / "wave2_oa_reads_rescues.tsv"
REPORT_MD = REPO / "data" / "deep_dive_wave2_oa_reads.md"

# DOI prefixes we UNLOCKED with the wave-2 _fetch_article_html() fix.
# Note: 10.1016 is included on the strength of the Cell Press subset —
# cell_press's matches() narrows on the j.{suffix} substring; non-Cell
# Elsevier rows fall through to the "no publisher plugin" branch and
# only PMC-page HTML mining (already Wave-1) would help. But including
# them costs nothing here: get_publisher() gates the article-HTML lookup.
TARGET_PREFIXES = frozenset({
    "10.3389",   # Frontiers
    "10.3390",   # MDPI
    "10.1186",   # BMC
    "10.1007",   # Springer
    "10.1126",   # Science AAAS
    "10.1016",   # Elsevier — Cell Press subset dispatched
})

STRATEGIES = ["whole_html_regex", "data_avail_section", "geo_to_sra", "arrayexpress"]

RESCUE_FIELDS = [
    "pmid",
    "doi",
    "doi_prefix",
    "publisher",
    "strategy",
    "accessions",
    "n_runs",
    "total_gb",
    "gse_ids",
    "ae_ids",
    "notes",
]


# ---------------------------------------------------------------------- helpers

def load_ignore_pmids() -> set[str]:
    """PMIDs marked IGNORE-* in the master disposition table.

    These are DELIBERATELY excluded from coverage numbers (singletons,
    Chinese-institution-only, already-complete). Attempting to rescue
    them wastes HTTP budget and would inflate the merge diff for zero
    real-world value.
    """
    if not MASTER_TSV.exists():
        return set()
    out: set[str] = set()
    with MASTER_TSV.open() as fh:
        rdr = csv.DictReader(fh, delimiter="\t")
        for r in rdr:
            pri = (r.get("priority") or "").upper()
            pmid = (r.get("pmid") or "").strip()
            if pri.startswith("IGNORE") and pmid:
                out.add(pmid)
    return out


def load_review_rows() -> tuple[list[str], list[dict]]:
    with REVIEW_TSV.open() as fh:
        rdr = csv.DictReader(fh, delimiter="\t")
        header = list(rdr.fieldnames or [])
        rows = list(rdr)
    return header, rows


def is_wave2_target(r: dict, ignore_pmids: set[str]) -> bool:
    if (r.get("reads_source") or "NONE") != "NONE":
        return False
    if (r.get("pmid") or "") in ignore_pmids:
        return False
    dp = r.get("doi_prefix") or ""
    return dp in TARGET_PREFIXES


def validate_accs(
    session: requests.Session,
    accs: list[str],
    cache: dict[str, tuple[int, float]],
) -> tuple[list[str], dict[str, tuple[int, float]]]:
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


def publisher_name(r: dict) -> str:
    """Rough publisher tag for reports. Uses DOI prefix to bucket."""
    dp = r.get("doi_prefix") or ""
    return {
        "10.3389": "frontiers",
        "10.3390": "mdpi",
        "10.1186": "bmc",
        "10.1007": "springer",
        "10.1126": "science_aaas",
        "10.1016": "elsevier_incl_cell_press",
    }.get(dp, dp or "unknown")


# ---------------------------------------------------------------------- rescue

def _fetch_publisher_html_only(
    session: requests.Session, doi: str
) -> Optional[str]:
    """Fetch ONLY the publisher-plugin article HTML (skip the PMC page).

    Wave-2's value proposition is that publisher HTML surfaces
    accessions that PMC XML often lacks — a data-availability section
    that's fully rendered on nature.com / frontiersin.org but truncated
    in the PMC JATS. Wave-1 already mined PMC HTML for rows with a PMC
    ID, so re-mining PMC here is redundant; we specifically want the
    publisher surface.
    """
    if not doi:
        return None
    pub = get_publisher(doi)
    if pub is None:
        return None
    try:
        url = pub.article_html_url(session, doi)
    except Exception:
        return None
    if not url:
        return None
    r = http_get(session, url, timeout=30)
    if r is None or r.status_code != 200 or len(r.text) < 8192:
        return None
    return r.text


def rescue_row(
    r: dict,
    session: requests.Session,
    ena_cache: dict[str, tuple[int, float]],
) -> list[dict]:
    """Run W1..W4 on one row. Return a list of rescue records (0..N).

    Each rescue record has one strategy tag. If the same PMID gets hits
    from multiple strategies we emit multiple records — the merge step
    unions the accession lists per PMID.

    We mine BOTH the publisher HTML (via extended _fetch_article_html
    dispatch — the wave-2 unlock) AND — for rows that have a PMC ID but
    where publisher HTML gave us NOTHING — the PMC page. The publisher
    HTML surface is our primary target because Wave-1 already covered
    PMC; we still fall back to the wave-1-style fetch (PMC-first) if
    publisher HTML wasn't fetchable, so single-surface rows still get
    the four strategies applied to their best available HTML.
    """
    pmid = r.get("pmid") or ""
    doi = r.get("doi") or ""
    pmc = r.get("pmc_id") or ""

    if not (doi or pmc):
        return []

    # First: publisher HTML (the wave-2 unlock).
    html: Optional[str] = None
    try:
        html = _fetch_publisher_html_only(session, doi)
    except Exception:
        html = None

    # Fallback for rows where no publisher plugin covers the DOI or the
    # publisher HTML wasn't fetchable: use the standard _fetch_article_html
    # which starts from PMC. This preserves wave-2's coverage of the (rare)
    # rows a wave-1 pass might have skipped due to transient network flap.
    if not html:
        try:
            html = _fetch_article_html(session, doi, pmc)
        except Exception:
            html = None

    records: list[dict] = []

    if not html:
        return records

    # ----- W1: whole-HTML broadened INSDC regex -----
    w1_accs = dedup_keep_order(
        extract_project_accs(html) + extract_url_accs(html)
    )
    if w1_accs:
        validated, valmap = validate_accs(session, w1_accs, ena_cache)
        if validated:
            records.append({
                "pmid": pmid,
                "doi": doi,
                "doi_prefix": r.get("doi_prefix", ""),
                "publisher": publisher_name(r),
                "strategy": "whole_html_regex",
                "accessions": ",".join(validated),
                "n_runs": sum(valmap[a][0] for a in validated),
                "total_gb": round(sum(valmap[a][1] for a in validated), 2),
                "gse_ids": "",
                "ae_ids": "",
                "notes": "",
            })

    # ----- W2: data availability paragraph extraction -----
    paragraphs = extract_data_avail_paragraphs(html)
    w2_accs: list[str] = []
    for p in paragraphs:
        for a in extract_project_accs(p):
            if a not in w2_accs:
                w2_accs.append(a)
        for a in extract_url_accs(p):
            if a not in w2_accs:
                w2_accs.append(a)
    if w2_accs:
        validated, valmap = validate_accs(session, w2_accs, ena_cache)
        if validated:
            records.append({
                "pmid": pmid,
                "doi": doi,
                "doi_prefix": r.get("doi_prefix", ""),
                "publisher": publisher_name(r),
                "strategy": "data_avail_section",
                "accessions": ",".join(validated),
                "n_runs": sum(valmap[a][0] for a in validated),
                "total_gb": round(sum(valmap[a][1] for a in validated), 2),
                "gse_ids": "",
                "ae_ids": "",
                "notes": "",
            })

    # ----- W3: GSE → SRA -----
    gse_ids = dedup_keep_order(GSE_RE.findall(html))[:6]  # cap for politeness
    if gse_ids:
        geo_accs: list[str] = []
        for gse in gse_ids:
            for a in geo_to_projects(session, gse):
                if a not in geo_accs:
                    geo_accs.append(a)
        if geo_accs:
            validated, valmap = validate_accs(session, geo_accs, ena_cache)
            if validated:
                records.append({
                    "pmid": pmid,
                    "doi": doi,
                    "doi_prefix": r.get("doi_prefix", ""),
                    "publisher": publisher_name(r),
                    "strategy": "geo_to_sra",
                    "accessions": ",".join(validated),
                    "n_runs": sum(valmap[a][0] for a in validated),
                    "total_gb": round(sum(valmap[a][1] for a in validated), 2),
                    "gse_ids": ",".join(gse_ids),
                    "ae_ids": "",
                    "notes": "",
                })

    # ----- W4: ArrayExpress / BioStudies -----
    ae_ids = dedup_keep_order(AE_RE.findall(html))[:6]
    if ae_ids:
        ae_accs: list[str] = []
        for ae in ae_ids:
            for a in biostudies_to_projects(session, ae):
                if a not in ae_accs:
                    ae_accs.append(a)
        if ae_accs:
            validated, valmap = validate_accs(session, ae_accs, ena_cache)
            if validated:
                records.append({
                    "pmid": pmid,
                    "doi": doi,
                    "doi_prefix": r.get("doi_prefix", ""),
                    "publisher": publisher_name(r),
                    "strategy": "arrayexpress",
                    "accessions": ",".join(validated),
                    "n_runs": sum(valmap[a][0] for a in validated),
                    "total_gb": round(sum(valmap[a][1] for a in validated), 2),
                    "gse_ids": "",
                    "ae_ids": ",".join(ae_ids),
                    "notes": "",
                })

    return records


def run_rescue() -> None:
    RESCUES_TSV.parent.mkdir(parents=True, exist_ok=True)
    ignore_pmids = load_ignore_pmids()
    print(f"[wave2] ignoring {len(ignore_pmids)} PMIDs from master disposition",
          file=sys.stderr)
    header, rows = load_review_rows()

    target_rows = [r for r in rows if is_wave2_target(r, ignore_pmids)]
    # Deduplicate publisher/prefix counts for logging
    by_prefix = Counter(r.get("doi_prefix", "") for r in target_rows)
    print(f"[wave2] target rows: {len(target_rows)}", file=sys.stderr)
    for dp, n in sorted(by_prefix.items()):
        print(f"[wave2]    {dp}: {n}", file=sys.stderr)

    session = new_session()
    ena_cache: dict[str, tuple[int, float]] = {}

    with RESCUES_TSV.open("w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=RESCUE_FIELDS, delimiter="\t")
        wr.writeheader()

        n_pmids_lifted = 0
        n_records = 0
        strat_hits: Counter = Counter()
        strat_pmids_lifted: Counter = Counter()
        strat_new_runs: Counter = Counter()
        strat_new_gb: dict[str, float] = defaultdict(float)
        pub_hits: Counter = Counter()
        pub_new_runs: Counter = Counter()
        pub_new_gb: dict[str, float] = defaultdict(float)
        pub_pmids_lifted: Counter = Counter()
        pub_tried: Counter = Counter()
        strat_examples: dict[str, list[tuple[str, str, list[str]]]] = defaultdict(list)

        for i, r in enumerate(target_rows):
            pub_tried[publisher_name(r)] += 1
            records = rescue_row(r, session, ena_cache)
            if records:
                pmid = r.get("pmid") or ""
                pmid_lifted_this_row = False
                for rec in records:
                    wr.writerow(rec)
                    fh.flush()
                    n_records += 1
                    strat_hits[rec["strategy"]] += 1
                    strat_new_runs[rec["strategy"]] += int(rec["n_runs"])
                    strat_new_gb[rec["strategy"]] += float(rec["total_gb"])
                    pub_hits[rec["publisher"]] += 1
                    pub_new_runs[rec["publisher"]] += int(rec["n_runs"])
                    pub_new_gb[rec["publisher"]] += float(rec["total_gb"])
                    accs = rec["accessions"].split(",")
                    if len(strat_examples[rec["strategy"]]) < 6:
                        strat_examples[rec["strategy"]].append(
                            (pmid, rec["publisher"], accs)
                        )
                    if not pmid_lifted_this_row:
                        strat_pmids_lifted[rec["strategy"]] += 1
                        pub_pmids_lifted[rec["publisher"]] += 1
                        pmid_lifted_this_row = True
                if pmid_lifted_this_row:
                    n_pmids_lifted += 1
            if (i + 1) % 10 == 0 or i == len(target_rows) - 1:
                print(
                    f"[wave2] {i+1}/{len(target_rows)}  "
                    f"pmids_lifted={n_pmids_lifted}  records={n_records}",
                    file=sys.stderr,
                )

    # ---- write per-run stats JSON alongside the audit tsv
    stats = {
        "n_target_rows": len(target_rows),
        "n_pmids_lifted": n_pmids_lifted,
        "n_records_written": n_records,
        "target_rows_by_prefix": dict(by_prefix),
        "publisher_tried": dict(pub_tried),
        "publisher_hits_pmids_lifted": dict(pub_pmids_lifted),
        "publisher_records": dict(pub_hits),
        "publisher_new_runs": dict(pub_new_runs),
        "publisher_new_gb": {k: round(v, 2) for k, v in pub_new_gb.items()},
        "strategy_records": dict(strat_hits),
        "strategy_pmids_lifted": dict(strat_pmids_lifted),
        "strategy_new_runs": dict(strat_new_runs),
        "strategy_new_gb": {k: round(v, 2) for k, v in strat_new_gb.items()},
        "strategy_examples": {
            k: [{"pmid": p, "publisher": pub, "accessions": a}
                for p, pub, a in ex]
            for k, ex in strat_examples.items()
        },
    }
    (RESCUES_TSV.with_suffix(".stats.json")).write_text(
        json.dumps(stats, indent=2)
    )
    print(f"[wave2] wrote {RESCUES_TSV}", file=sys.stderr)
    print(f"[wave2] wrote {RESCUES_TSV.with_suffix('.stats.json')}",
          file=sys.stderr)
    print(f"[wave2] pmids lifted: {n_pmids_lifted}", file=sys.stderr)


# ---------------------------------------------------------------------- merge

def merge_wave2_into_review() -> None:
    """Merge data/wave2_oa_reads_rescues.tsv into coverage_review.tsv.

    ADDITIVE ONLY. Never downgrades any row. If a row already has
    reads (via some prior wave), we UNION the new accessions in and
    append `wave2_html_mine` to the reads_source. Recomputes gap_score
    and re-sorts worst-first.

    Callers should `git pull --rebase` FIRST so the merge writes onto
    the freshest tree — the brief specifies this is the last atomic step.
    """
    if not RESCUES_TSV.exists():
        print(f"[wave2-merge] no rescues file at {RESCUES_TSV}; nothing to merge",
              file=sys.stderr)
        return

    # Load rescues, group by PMID with union of accessions.
    by_pmid: dict[str, dict] = {}
    with RESCUES_TSV.open() as fh:
        rdr = csv.DictReader(fh, delimiter="\t")
        for rec in rdr:
            pmid = rec["pmid"]
            entry = by_pmid.setdefault(pmid, {"accs": [], "strats": [], "n_runs": 0, "gb": 0.0})
            for a in rec["accessions"].split(","):
                a = a.strip()
                if a and a not in entry["accs"]:
                    entry["accs"].append(a)
                    # Only count runs/GB for accessions we're actually adding
                    # (avoid double-counting when multiple strategies hit
                    # the same accession).
            # Add strategy tag if new
            if rec["strategy"] not in entry["strats"]:
                entry["strats"].append(rec["strategy"])

    if not by_pmid:
        print("[wave2-merge] rescues file is empty; nothing to merge",
              file=sys.stderr)
        return

    # Re-validate summed n_runs/total_gb per accession (single source of
    # truth = ENA filereport). We use the deduped accession list per PMID.
    session = new_session()
    ena_cache: dict[str, tuple[int, float]] = {}

    header, rows = load_review_rows()
    n_rows_lifted_from_none = 0
    n_accs_added = 0

    for r in rows:
        pmid = r.get("pmid") or ""
        rescue = by_pmid.get(pmid)
        if not rescue:
            continue

        existing_accs = [
            a.strip() for a in (r.get("reads_accessions") or "").split(",")
            if a.strip() and a.strip() != "NONE"
        ]
        existing_set = set(existing_accs)
        new_accs = [a for a in rescue["accs"] if a not in existing_set]
        if not new_accs:
            continue

        # Validate each new accession against ENA
        validated: list[str] = []
        val_runs = 0
        val_gb = 0.0
        for a in new_accs:
            if a in ena_cache:
                n, gb = ena_cache[a]
            else:
                try:
                    n, gb = probe_ena_filereport(session, a)
                except Exception:
                    n, gb = 0, 0.0
                ena_cache[a] = (n, gb)
                time.sleep(SLEEP_EBI)
            if n > 0:
                validated.append(a)
                val_runs += n
                val_gb += gb

        if not validated:
            continue

        was_none = (r.get("reads_source") or "NONE") == "NONE"
        merged_accs = existing_accs + validated
        r["reads_accessions"] = ",".join(merged_accs)

        # Merge reads_source with wave2 tag(s)
        old_src = r.get("reads_source") or "NONE"
        parts = [] if old_src == "NONE" else [
            p.strip() for p in old_src.split(",") if p.strip()
        ]
        wave2_tag = "wave2_html_mine"
        if wave2_tag not in parts:
            parts.append(wave2_tag)
        r["reads_source"] = ",".join(parts)

        # Additive n_runs / total_gb
        try:
            r["n_runs"] = str(int(r.get("n_runs") or 0) + val_runs)
        except ValueError:
            r["n_runs"] = str(val_runs)
        try:
            r["total_gb"] = str(round(
                float(r.get("total_gb") or 0.0) + val_gb, 2))
        except ValueError:
            r["total_gb"] = str(round(val_gb, 2))

        if was_none:
            n_rows_lifted_from_none += 1
        n_accs_added += len(validated)

    # Recompute gap_score and re-sort worst-first
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

    print(f"[wave2-merge] rows lifted from NONE: {n_rows_lifted_from_none}",
          file=sys.stderr)
    print(f"[wave2-merge] accessions added: {n_accs_added}",
          file=sys.stderr)

    # Stash merge stats for the report step to consume.
    merge_stats = {
        "rows_lifted_from_none": n_rows_lifted_from_none,
        "accessions_added": n_accs_added,
    }
    (REPO / "data" / "wave2_oa_reads_rescues.merge_stats.json").write_text(
        json.dumps(merge_stats, indent=2)
    )


# ---------------------------------------------------------------------- report

def write_report() -> None:
    stats_path = RESCUES_TSV.with_suffix(".stats.json")
    if not stats_path.exists():
        print(f"[wave2-report] {stats_path} missing; run rescue first",
              file=sys.stderr)
        return
    stats = json.loads(stats_path.read_text())

    merge_stats_path = REPO / "data" / "wave2_oa_reads_rescues.merge_stats.json"
    merge_stats = {}
    if merge_stats_path.exists():
        merge_stats = json.loads(merge_stats_path.read_text())

    lines: list[str] = []
    lines.append("# Deep-dive: Wave-2 OA reads rescue\n")
    lines.append("Run: 2026-07-11  ")
    lines.append("Script: `scripts/refresh_reads_oa_wave2.py`\n")
    lines.append("## Motivation\n")
    lines.append(
        "Wave-1's `refresh_reads_deeper.py` ran BEFORE Frontiers / MDPI / "
        "BMC / science_aaas / cell_press publisher plugins were wired into "
        "`probe_coverage._fetch_article_html()`. The article-HTML dispatch "
        "only knew nature / nature_legacy / springer / bmj, so ~123 OA "
        "reads gaps were unreachable to HTML mining. This wave extends "
        "dispatch (via each plugin's new `article_html_url()` method) "
        "and reruns the four winning strategies from Wave-1.\n"
    )
    lines.append("## Scope\n")
    lines.append(
        f"- Target rows (reads_source=NONE + doi_prefix in OA cluster + "
        f"not IGNORE): **{stats['n_target_rows']}**\n"
    )
    lines.append("Rows tried per publisher:\n")
    lines.append("| Publisher | N tried |")
    lines.append("|---|---:|")
    for pub, n in sorted(
        stats["publisher_tried"].items(),
        key=lambda kv: -kv[1],
    ):
        lines.append(f"| {pub} | {n} |")
    lines.append("")
    lines.append("## Results by publisher\n")
    lines.append(
        "| Publisher | PMIDs lifted | Records | New runs | New GB | Hit rate (pmids/tried) |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|")
    for pub, tried in sorted(
        stats["publisher_tried"].items(),
        key=lambda kv: -kv[1],
    ):
        lifted = stats["publisher_hits_pmids_lifted"].get(pub, 0)
        records = stats["publisher_records"].get(pub, 0)
        runs = stats["publisher_new_runs"].get(pub, 0)
        gb = stats["publisher_new_gb"].get(pub, 0.0)
        rate = f"{100 * lifted / max(1, tried):.1f}%"
        lines.append(
            f"| {pub} | {lifted} | {records} | {runs} | {gb} | {rate} |"
        )
    lines.append("")
    lines.append("## Results by strategy\n")
    lines.append(
        "| Strategy | Records | PMIDs lifted | New runs | New GB |"
    )
    lines.append("|---|---:|---:|---:|---:|")
    for k in ["whole_html_regex", "data_avail_section", "geo_to_sra", "arrayexpress"]:
        recs = stats["strategy_records"].get(k, 0)
        lifted = stats["strategy_pmids_lifted"].get(k, 0)
        runs = stats["strategy_new_runs"].get(k, 0)
        gb = stats["strategy_new_gb"].get(k, 0.0)
        lines.append(f"| {k} | {recs} | {lifted} | {runs} | {gb} |")
    lines.append("")
    lines.append("## Overall\n")
    lines.append(f"- **PMIDs lifted (from reads_source=NONE): "
                 f"{stats['n_pmids_lifted']}**")
    lines.append(f"- Rescue records written: {stats['n_records_written']}")
    if merge_stats:
        lines.append(
            f"- After merge into `coverage_review.tsv`: "
            f"{merge_stats.get('rows_lifted_from_none', 0)} rows lifted "
            f"from NONE (unique PMIDs), "
            f"{merge_stats.get('accessions_added', 0)} accessions added."
        )
    lines.append("")
    lines.append("## Example rescues (up to 6 per strategy)\n")
    for strat, exs in stats.get("strategy_examples", {}).items():
        if not exs:
            continue
        lines.append(f"### {strat}\n")
        for ex in exs[:6]:
            lines.append(
                f"- PMID {ex['pmid']} ({ex['publisher']}) → "
                f"{', '.join(ex['accessions'])}"
            )
        lines.append("")
    lines.append("## Notes\n")
    lines.append(
        "- Every rescued accession was validated against ENA "
        "`filereport?result=read_run` with `n_runs > 0` (no unverified "
        "claims).\n"
    )
    lines.append(
        "- MDPI (10.3390) HTML is Akamai-blocked from cluster IP so the "
        "article-HTML strategies cannot reach the paper body; MDPI hit "
        "rate here is near zero. This is expected and documented in "
        "`publishers/mdpi.py`. PMC-page fallback (Wave-1) is the only "
        "usable path for MDPI reads mining from cluster.\n"
    )
    lines.append(
        "- science_aaas / cell_press use warm-session cookies to defeat "
        "Cloudflare; success is stochastic. Included for completeness.\n"
    )
    lines.append(
        "- IGNORE-* PMIDs from `data/master_paper_disposition.tsv` were "
        "skipped by design.\n"
    )

    REPORT_MD.write_text("\n".join(lines) + "\n")
    print(f"[wave2-report] wrote {REPORT_MD}", file=sys.stderr)


# ---------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--merge", action="store_true",
                    help="Merge existing rescues.tsv into coverage_review.tsv "
                    "and stop (no new HTTP). Callers should git pull --rebase "
                    "immediately before.")
    ap.add_argument("--report", action="store_true",
                    help="Generate the report from existing stats + merge_stats.")
    ap.add_argument("--all", action="store_true",
                    help="Run rescue → merge → report in sequence.")
    args = ap.parse_args()

    if args.merge:
        merge_wave2_into_review()
        return 0
    if args.report:
        write_report()
        return 0

    run_rescue()
    if args.all:
        merge_wave2_into_review()
        write_report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
