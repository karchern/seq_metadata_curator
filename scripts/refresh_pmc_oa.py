#!/usr/bin/env python3
"""
Cheap refresh: re-run probe_pmc_oa on every row of coverage_review.tsv
that has a pmc_id, and update pdf_sources / supp_source / supp_available
/ gap_score in place. Preserves verdict / action / user_notes.

Motivation: we discovered the PMC-OA endpoint host was wrong
(pmc.ncbi.nlm.nih.gov → www.ncbi.nlm.nih.gov/pmc). Every prior PMC-OA
result was a silent 404. This script re-checks just that source without
re-running the whole probe.

Order dependency (per R4-10): this script is MONOTONE-UP — it never
removes a source, only adds. `refresh_pdf_supp.py` is comprehensive and
can BOTH add and downgrade (via the _MISSING sentinel regression guard).
Recommended order in an automated pipeline:
    1. refresh_pmc_oa.py   (~5 min: cheap PMC-OA incremental gain)
    2. refresh_pdf_supp.py (~15 min: comprehensive re-probe)
If you must run just one, run refresh_pdf_supp.py — it's a superset.
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_coverage import new_session, probe_pmc_oa, probe_pmc_supp_verified  # noqa: E402


REVIEW_TSV = Path("/scratch/karcher/seq_metadata_curator/data/coverage_review.tsv")


def recompute_gap_score(r: dict) -> str:
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

    session = new_session()
    changed_pdf = 0
    changed_supp = 0
    unlocked_from_none_pdf = 0
    n_checked = 0
    n_oa = 0
    n_pmc = sum(1 for r in rows if r.get("pmc_id"))
    print(f"[refresh] {n_pmc} rows have a pmc_id (of {len(rows)}). Probing...", file=sys.stderr)

    for i, r in enumerate(rows):
        pmc = r.get("pmc_id") or ""
        if not pmc:
            continue
        n_checked += 1
        try:
            is_oa = probe_pmc_oa(session, pmc)
        except Exception as e:
            print(f"  {pmc}: EXC {e}", file=sys.stderr)
            is_oa = False

        if is_oa:
            n_oa += 1
            # Merge pmc_oa into pdf_sources (idempotent).
            srcs = r.get("pdf_sources") or "NONE"
            if "pmc_oa" not in srcs.split(","):
                if srcs == "NONE":
                    r["pdf_sources"] = "pmc_oa"
                    unlocked_from_none_pdf += 1
                else:
                    r["pdf_sources"] = "pmc_oa," + srcs
                changed_pdf += 1
            # supp: verify via Europe PMC hasSuppl — the older blanket
            # assumption "PMC-OA tarball routinely bundles supp" caused
            # ~inflated supp coverage (empty tarballs still claimed True).
            if (r.get("supp_available") or "").lower() != "true":
                try:
                    has_supp = probe_pmc_supp_verified(session, pmc)
                except Exception:
                    has_supp = False  # don't overclaim on transient
                if has_supp:
                    r["supp_available"] = "True"
                    r["supp_source"] = "pmc_oa"
                    changed_supp += 1

        if (i + 1) % 25 == 0 or i == len(rows) - 1:
            print(
                f"[refresh] {i+1}/{len(rows)}  checked={n_checked} "
                f"oa={n_oa} pdf_upd={changed_pdf} supp_upd={changed_supp}",
                file=sys.stderr,
            )
        time.sleep(0.35)  # ncbi rate

    for r in rows:
        r["gap_score"] = recompute_gap_score(r)

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
    print(f"    PMC IDs re-checked : {n_checked}", file=sys.stderr)
    print(f"    PMC-OA confirmed   : {n_oa} ({100*n_oa/max(1,n_checked):.1f}%)", file=sys.stderr)
    print(f"    rows gaining pmc_oa: {changed_pdf} (of which {unlocked_from_none_pdf} moved from PDF-NONE)", file=sys.stderr)
    print(f"    rows gaining supp  : {changed_supp}", file=sys.stderr)
    print(
        f"    NEW coverage: pdf {pdf_any}/{total} ({100*pdf_any/max(1,total):.1f}%)  "
        f"supp {supp_any}/{total} ({100*supp_any/max(1,total):.1f}%)  "
        f"reads {reads_any}/{total} ({100*reads_any/max(1,total):.1f}%)",
        file=sys.stderr,
    )
    print(f"    gap_score dist: {dict(sorted(gs.items()))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
