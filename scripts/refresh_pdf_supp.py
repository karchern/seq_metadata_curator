#!/usr/bin/env python3
"""
Comprehensive PDF+supp refresh for coverage_review.tsv, applying every recent
fix at once:

  - Re-fetch metadata via batched efetch (correct ArticleId scoping — fixes
    the ~5% of rows where a reference's PMC ID had contaminated pmc_id).
  - Re-probe PMC-OA against the corrected pmc_id.
  - Re-probe publisher (dispatches across ALL registered publishers now,
    not just Nature — Springer / BMJ / nature_legacy included).
  - Re-probe publisher supp (same ESM CDN pattern used by Nature + Springer
    + nature_legacy).
  - Re-probe Unpaywall (iterates full oa_locations[] beyond best_oa_location).

Reads-related columns (reads_accessions, reads_source, n_runs, total_gb) are
NOT re-probed — those depend on europepmc / ena / ncbi and are orthogonal to
the PDF/supp fixes we're evaluating here. Review columns (verdict / action /
user_notes) are preserved.

Rewrites coverage_review.tsv in place, sorted worst-first.
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_coverage import (  # noqa: E402
    batch_metadata,
    new_session,
    probe_pmc_id_fallback,
    probe_pmc_oa,
    probe_publisher,
    probe_publisher_supp,
    probe_unpaywall,
)

REVIEW_TSV = Path("/scratch/karcher/seq_metadata_curator/data/coverage_review.tsv")


def recompute_gap(r: dict) -> str:
    s = 0
    if (r.get("pdf_sources") or "NONE") == "NONE":
        s += 1
    if (r.get("supp_available") or "").lower() != "true":
        s += 1
    if (r.get("reads_source") or "NONE") == "NONE":
        s += 1
    return str(s)


def main() -> int:
    with REVIEW_TSV.open() as fh:
        rdr = csv.DictReader(fh, delimiter="\t")
        header = list(rdr.fieldnames or [])
        rows = list(rdr)

    pmids = [r["pmid"] for r in rows if r.get("pmid")]
    session = new_session()

    # (1) Fresh metadata (correct ArticleId scoping)
    print(f"[refresh] batched efetch metadata for {len(pmids)} PMIDs ...", file=sys.stderr)
    meta = batch_metadata(pmids, "karchernic@gmail.com")
    print(f"[refresh] metadata retrieved for {len(meta)} of {len(pmids)}", file=sys.stderr)

    pmc_corrections = 0
    doi_corrections = 0
    skipped_no_meta = 0
    for r in rows:
        # If batch_metadata dropped this PMID (network hiccup, retracted,
        # etc.), DO NOT overwrite the row's existing pmc_id / doi with
        # empty strings — that turns a transient upstream failure into
        # permanent data loss on this row.
        if r["pmid"] not in meta:
            skipped_no_meta += 1
            continue

        m = meta[r["pmid"]]
        fresh_pmc = m.get("pmc_id") or ""
        fresh_doi = m.get("doi") or ""
        if (r.get("pmc_id") or "") != fresh_pmc:
            r["pmc_id"] = fresh_pmc
            pmc_corrections += 1
        if (r.get("doi") or "") != fresh_doi:
            r["doi"] = fresh_doi
            doi_corrections += 1
        # doi_prefix follows doi
        if fresh_doi and "/" in fresh_doi:
            r["doi_prefix"] = fresh_doi.split("/", 1)[0]
        elif not fresh_doi:
            r["doi_prefix"] = ""

    print(
        f"[refresh] metadata corrections: pmc_id {pmc_corrections}, doi {doi_corrections} "
        f"(skipped {skipped_no_meta} rows whose PMID was missing from efetch response)",
        file=sys.stderr,
    )

    # (2) Now redo PDF+supp probing per row.
    pdf_sources_reset = 0
    n_ok = {"pmc_oa_pdf": 0, "publisher_pdf": 0, "unpaywall_pdf": 0, "supp": 0}

    for i, r in enumerate(rows):
        # DO NOT destroy pdf_sources / supp_source before probing — a
        # single transient failure (429/timeout) would then permanently
        # regress this row. Instead, gather fresh evidence into local
        # buffers and only commit if every probe completed cleanly.
        pdf_srcs: list[str] = []
        pmc = r.get("pmc_id") or ""
        doi = r.get("doi") or ""

        probe_had_exception = False

        # PMC-OA lookup: only if we have a pmc_id. Try the direct pmc_id
        # first; if none, run the esearch fallback (same as the probe does).
        pmc_oa_ok = False
        if pmc:
            try:
                pmc_oa_ok = probe_pmc_oa(session, pmc)
            except Exception:
                probe_had_exception = True
        else:
            try:
                alt = probe_pmc_id_fallback(session, r["pmid"])
            except Exception:
                alt = None
                probe_had_exception = True
            if alt:
                r["pmc_id"] = alt
                try:
                    pmc_oa_ok = probe_pmc_oa(session, alt)
                except Exception:
                    probe_had_exception = True
        if pmc_oa_ok:
            pdf_srcs.append("pmc_oa")

        # Publisher probe
        if doi:
            try:
                pubname = probe_publisher(session, doi)
            except Exception:
                pubname = None
                probe_had_exception = True
            if pubname:
                pdf_srcs.append(pubname)

        # Unpaywall
        if doi:
            try:
                if probe_unpaywall(session, doi):
                    pdf_srcs.append("unpaywall")
            except Exception:
                probe_had_exception = True

        # Publisher supp — check separately from pmc_oa
        pub_supp_ok = False
        if doi:
            try:
                pub_supp_ok, _n = probe_publisher_supp(session, doi)
            except Exception:
                probe_had_exception = True

        # ---- COMMIT decision ----
        new_pdf_sources = ",".join(pdf_srcs) if pdf_srcs else "NONE"
        old_pdf_sources = r.get("pdf_sources") or "NONE"

        # Regression guard: if ALL probes raised exceptions AND the buffer
        # is empty AND we previously had a positive result, keep the old
        # value. Otherwise commit fresh state.
        if (
            probe_had_exception
            and new_pdf_sources == "NONE"
            and old_pdf_sources != "NONE"
        ):
            skipped_no_meta_pdf = getattr(main, "_pdf_regression_saved", 0) + 1
            main._pdf_regression_saved = skipped_no_meta_pdf
        else:
            r["pdf_sources"] = new_pdf_sources
            if old_pdf_sources != new_pdf_sources:
                pdf_sources_reset += 1

        # Supp commit — same regression logic. PMC-OA wins over publisher
        # (author-tar bundles are richer + include labels via JATS XML).
        old_supp_available = (r.get("supp_available") or "").lower() == "true"
        new_supp_available = pmc_oa_ok or pub_supp_ok
        new_supp_source = "pmc_oa" if pmc_oa_ok else (
            f"publisher:{__import__('probe_coverage').get_publisher(doi).name}"
            if (pub_supp_ok and doi) else "NONE"
        )
        if (
            probe_had_exception
            and not new_supp_available
            and old_supp_available
        ):
            pass  # keep old supp state
        else:
            r["supp_available"] = "True" if new_supp_available else "False"
            r["supp_source"] = new_supp_source
        if pmc_oa_ok:
            n_ok["pmc_oa_pdf"] += 1
        if any(s not in ("pmc_oa", "unpaywall") for s in pdf_srcs):
            n_ok["publisher_pdf"] += 1
        if "unpaywall" in pdf_srcs:
            n_ok["unpaywall_pdf"] += 1
        if new_supp_available:
            n_ok["supp"] += 1

        if (i + 1) % 25 == 0 or i == len(rows) - 1:
            print(
                f"[refresh] {i+1}/{len(rows)}  pmc_oa={n_ok['pmc_oa_pdf']} "
                f"pub={n_ok['publisher_pdf']} unpay={n_ok['unpaywall_pdf']} "
                f"supp={n_ok['supp']}",
                file=sys.stderr,
            )
        time.sleep(0.15)

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

    from collections import Counter
    gs = Counter(int(r["gap_score"]) for r in rows)
    pdf_any = sum(1 for r in rows if (r.get("pdf_sources") or "NONE") != "NONE")
    supp_any = sum(1 for r in rows if (r.get("supp_available") or "").lower() == "true")
    reads_any = sum(1 for r in rows if (r.get("reads_source") or "NONE") != "NONE")
    total = len(rows)

    print("\n[refresh] DONE", file=sys.stderr)
    print(f"    PDF now  : {pdf_any}/{total} ({100*pdf_any/total:.1f}%)", file=sys.stderr)
    print(f"    supp now : {supp_any}/{total} ({100*supp_any/total:.1f}%)", file=sys.stderr)
    print(f"    reads    : {reads_any}/{total} ({100*reads_any/total:.1f}%)  (untouched by this refresh)", file=sys.stderr)
    print(f"    gap_score dist: {dict(sorted(gs.items()))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
