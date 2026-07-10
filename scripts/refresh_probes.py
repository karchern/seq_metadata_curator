#!/usr/bin/env python3
"""
Targeted re-probe of coverage_review.tsv rows to reflect recent fixes:

  - probe_publisher() now dispatches across ALL registered publishers
    (Springer, BMJ, nature_legacy) — not just Nature.
  - probe_publisher_supp() now covers Nature + nature_legacy + Springer.
  - probe_unpaywall() now iterates oa_locations[] beyond best_oa_location.

Only touches rows where PDF or supp is still missing — cheap and idempotent.
Preserves verdict / action / user_notes columns.
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_coverage import (  # noqa: E402
    new_session,
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

    session = new_session()
    pdf_rescued = 0
    supp_rescued = 0
    n_probed_pub = 0
    n_probed_unpay = 0
    n_probed_supp = 0

    for i, r in enumerate(rows):
        pdf_none = (r.get("pdf_sources") or "NONE") == "NONE"
        supp_missing = (r.get("supp_available") or "").lower() != "true"
        doi = r.get("doi") or ""

        # (1) publisher probe — only if PDF missing
        if pdf_none and doi:
            n_probed_pub += 1
            try:
                name = probe_publisher(session, doi)
            except Exception:
                name = None
            if name:
                r["pdf_sources"] = name
                pdf_rescued += 1
                pdf_none = False

        # (2) unpaywall probe — only if PDF still missing (fixed oa_locations[])
        if pdf_none and doi:
            n_probed_unpay += 1
            try:
                if probe_unpaywall(session, doi):
                    srcs = r.get("pdf_sources") or "NONE"
                    r["pdf_sources"] = "unpaywall" if srcs == "NONE" else srcs + ",unpaywall"
                    pdf_rescued += 1
                    pdf_none = False
            except Exception:
                pass

        # (3) publisher supp probe — only if supp missing (now covers
        #     nature / nature_legacy / springer via same ESM CDN pattern)
        if supp_missing and doi:
            n_probed_supp += 1
            try:
                ok, _n = probe_publisher_supp(session, doi)
            except Exception:
                ok = False
            if ok:
                r["supp_available"] = "True"
                # Preserve prior supp_source if it was pmc_oa; otherwise
                # tag with the publisher module.
                if not (r.get("supp_source") or "").startswith("publisher:"):
                    from probe_coverage import get_publisher
                    pub = get_publisher(doi)
                    r["supp_source"] = f"publisher:{pub.name}" if pub else "publisher"
                supp_rescued += 1

        if (i + 1) % 25 == 0 or i == len(rows) - 1:
            print(
                f"[refresh] {i+1}/{len(rows)}  "
                f"pdf_rescued={pdf_rescued}  supp_rescued={supp_rescued}  "
                f"probes: pub={n_probed_pub} unpay={n_probed_unpay} supp={n_probed_supp}",
                file=sys.stderr,
            )
        time.sleep(0.2)

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
    print(f"    PDF now  : {pdf_any}/{total} ({100*pdf_any/total:.1f}%)  (rescued this round: {pdf_rescued})", file=sys.stderr)
    print(f"    supp now : {supp_any}/{total} ({100*supp_any/total:.1f}%)  (rescued this round: {supp_rescued})", file=sys.stderr)
    print(f"    reads    : {reads_any}/{total} ({100*reads_any/total:.1f}%)  (unchanged)", file=sys.stderr)
    print(f"    gap_score dist: {dict(sorted(gs.items()))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
