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
    probe_pmc_supp_verified,
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
        stored_pmc = r.get("pmc_id") or ""
        stored_doi = r.get("doi") or ""
        # Overwrite the stored value only when fresh actually contributes
        # information (non-empty) or when stored is also empty. This
        # protects against a partial efetch response silently blanking
        # good on-disk IDs — but does NOT prevent correction of a stored
        # (post-R1-2 bug) contaminated PMC ID whose fresh value is legit
        # empty for an article that genuinely has no PMC deposit.
        if fresh_pmc and fresh_pmc != stored_pmc:
            r["pmc_id"] = fresh_pmc
            pmc_corrections += 1
        if fresh_doi and fresh_doi != stored_doi:
            r["doi"] = fresh_doi
            doi_corrections += 1
        # doi_prefix follows doi (only if we actually updated doi)
        if fresh_doi and "/" in fresh_doi:
            r["doi_prefix"] = fresh_doi.split("/", 1)[0]

    print(
        f"[refresh] metadata corrections: pmc_id {pmc_corrections}, doi {doi_corrections} "
        f"(skipped {skipped_no_meta} rows whose PMID was missing from efetch response)",
        file=sys.stderr,
    )

    # (2) Now redo PDF+supp probing per row.
    pdf_sources_reset = 0
    n_ok = {"pmc_oa_pdf": 0, "publisher_pdf": 0, "unpaywall_pdf": 0, "supp": 0}

    _MISSING = object()  # sentinel meaning "probe couldn't run this cycle"
    for i, r in enumerate(rows):
        pmc = r.get("pmc_id") or ""
        doi = r.get("doi") or ""

        # Per-source result buckets. Each starts as sentinel _MISSING so
        # the commit stage can distinguish "probed → False" from
        # "probe raised or wasn't attempted".
        res_pmc_oa: object = _MISSING          # bool or _MISSING
        res_publisher: object = _MISSING        # str name or "" or _MISSING
        res_unpaywall: object = _MISSING        # bool or _MISSING
        res_pmc_supp: object = _MISSING         # bool or _MISSING (verified via hasSuppl)
        res_pub_supp: object = _MISSING         # bool or _MISSING

        # ---- PMC-OA probe (also backfill pmc_id via esearch when missing) ----
        if not pmc:
            try:
                alt = probe_pmc_id_fallback(session, r["pmid"])
                if alt:
                    r["pmc_id"] = alt
                    pmc = alt
            except Exception:
                pass
        if pmc:
            try:
                res_pmc_oa = probe_pmc_oa(session, pmc)
            except Exception:
                pass  # stays _MISSING
        else:
            res_pmc_oa = False

        # ---- PMC-supp verification via Europe PMC hasSuppl ----
        # This replaces the previous blanket "pmc_oa implies supp" claim,
        # which R2-9 fixed only inside fetch_paper.try_pmc_oa_tarball but
        # not in the refresh scripts (I-C2 / H-4).
        if res_pmc_oa is True and pmc:
            try:
                res_pmc_supp = probe_pmc_supp_verified(session, pmc)
            except Exception:
                pass  # stays _MISSING; commit stage will keep old supp value
        elif res_pmc_oa is False:
            # PMC-OA is definitively False this cycle → so is PMC-OA supp.
            # Without this branch (R5-4 = N-1), the supp guard would leave
            # res_pmc_supp as _MISSING and preserve a stale
            # supp_source=pmc_oa for a row whose PMC-OA is gone.
            res_pmc_supp = False

        # ---- Publisher PDF probe ----
        if doi:
            try:
                pn = probe_publisher(session, doi)
                res_publisher = pn or ""
            except Exception:
                pass

        # ---- Unpaywall probe ----
        if doi:
            try:
                res_unpaywall = probe_unpaywall(session, doi)
            except Exception:
                pass

        # ---- Publisher supp probe ----
        if doi:
            try:
                ok, _n = probe_publisher_supp(session, doi)
                res_pub_supp = ok
            except Exception:
                pass

        # ---- COMMIT decision (per-source) ----
        # For PDF sources: rebuild the pdf_sources string from FRESH results
        # where we have them, and CARRY FORWARD prior contributions from
        # any source whose probe couldn't run this cycle. Prevents G-2 /
        # H-1 partial-source regression.
        old_pdf_sources = r.get("pdf_sources") or "NONE"
        old_srcs = set(old_pdf_sources.split(",")) if old_pdf_sources != "NONE" else set()
        fresh_srcs: set[str] = set()

        if res_pmc_oa is True:
            fresh_srcs.add("pmc_oa")
        elif res_pmc_oa is _MISSING and "pmc_oa" in old_srcs:
            fresh_srcs.add("pmc_oa")

        if isinstance(res_publisher, str) and res_publisher:
            fresh_srcs.add(res_publisher)
        elif res_publisher is _MISSING:
            # carry forward any known publisher-type source
            for s in old_srcs:
                if s in ("nature", "nature_legacy", "springer", "bmj"):
                    fresh_srcs.add(s)

        if res_unpaywall is True:
            fresh_srcs.add("unpaywall")
        elif res_unpaywall is _MISSING and "unpaywall" in old_srcs:
            fresh_srcs.add("unpaywall")

        new_pdf_sources = ",".join(sorted(fresh_srcs)) if fresh_srcs else "NONE"
        if old_pdf_sources != new_pdf_sources:
            pdf_sources_reset += 1
        r["pdf_sources"] = new_pdf_sources

        # For supp: PMC-OA verified via hasSuppl OR publisher supp probe.
        old_supp_available = (r.get("supp_available") or "").lower() == "true"
        old_supp_source = r.get("supp_source") or "NONE"

        # Confident-True cases first.
        new_supp_flag: bool = False
        new_supp_source = "NONE"
        if res_pmc_supp is True:
            new_supp_flag = True
            new_supp_source = "pmc_oa"
        elif res_pub_supp is True:
            new_supp_flag = True
            from probe_coverage import get_publisher
            pub = get_publisher(doi) if doi else None
            new_supp_source = f"publisher:{pub.name}" if pub else "publisher"
        else:
            # Neither probe returned confident True. Distinguish "both
            # probes ran and both said False" from "at least one probe
            # couldn't run and we can't verify a downgrade".
            any_missing = (
                res_pmc_supp is _MISSING or res_pub_supp is _MISSING
            )
            if any_missing and old_supp_available:
                # Can't confidently regress a previously-True row when
                # one of the two supp signals didn't get to speak.
                new_supp_flag = old_supp_available
                new_supp_source = old_supp_source
            # else: both probes ran, both said False → downgrade cleanly

        r["supp_available"] = "True" if new_supp_flag else "False"
        r["supp_source"] = new_supp_source

        # Counters
        if res_pmc_oa is True:
            n_ok["pmc_oa_pdf"] += 1
        if isinstance(res_publisher, str) and res_publisher:
            n_ok["publisher_pdf"] += 1
        if res_unpaywall is True:
            n_ok["unpaywall_pdf"] += 1
        if new_supp_flag:
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
    print(f"    PDF now  : {pdf_any}/{total} ({100*pdf_any/max(1,total):.1f}%)", file=sys.stderr)
    print(f"    supp now : {supp_any}/{total} ({100*supp_any/max(1,total):.1f}%)", file=sys.stderr)
    print(f"    reads    : {reads_any}/{total} ({100*reads_any/max(1,total):.1f}%)  (untouched by this refresh)", file=sys.stderr)
    print(f"    gap_score dist: {dict(sorted(gs.items()))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
