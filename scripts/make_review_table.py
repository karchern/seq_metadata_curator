#!/usr/bin/env python3
"""
Aggregate coverage_report_part*.tsv into a single reviewable TSV with:

  - all probe columns (pmid, journal, doi, pmc_id, doi_prefix, pdf_sources,
    supp_source, supp_available, reads_accessions, reads_source, n_runs,
    total_gb, note)
  - a `gap_score` column (0-3): +1 per missing capability (pdf/supp/reads)
  - three writable review columns: `verdict`, `action`, `user_notes`

Row order: gap_score DESC, then journal ASC, then pmid ASC — worst-first,
so the papers that most need human review float to the top.

Re-run behaviour: if the output TSV already exists, existing `verdict` /
`action` / `user_notes` values are merged in by PMID before rewriting. So
you can edit the table in a spreadsheet, save, we iterate on the pipeline,
re-run this script, and your notes survive.
"""
from __future__ import annotations

import argparse
import csv
import glob
import sys
from pathlib import Path

REVIEW_COLS = ("verdict", "action", "user_notes")


def load_existing_notes(path: Path) -> dict[str, dict[str, str]]:
    """Return {pmid: {verdict, action, user_notes}} from a prior review TSV."""
    if not path.exists():
        return {}
    with path.open() as fh:
        rdr = csv.DictReader(fh, delimiter="\t")
        out: dict[str, dict[str, str]] = {}
        for row in rdr:
            pmid = row.get("pmid") or ""
            if not pmid:
                continue
            keep = {c: row.get(c, "") for c in REVIEW_COLS}
            if any(keep.values()):
                out[pmid] = keep
    return out


def gap_score(row: dict[str, str]) -> int:
    s = 0
    if (row.get("pdf_sources") or "NONE") == "NONE":
        s += 1
    if (row.get("supp_available") or "").lower() != "true":
        s += 1
    if (row.get("reads_source") or "NONE") == "NONE":
        s += 1
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--parts-glob",
        default="/scratch/karcher/seq_metadata_curator/data/coverage_parts/coverage_report_part*.tsv",
    )
    ap.add_argument(
        "--out-tsv",
        type=Path,
        default=Path("/scratch/karcher/seq_metadata_curator/data/coverage_review.tsv"),
    )
    ap.add_argument(
        "--rebuild-from-parts",
        action="store_true",
        help="Ignore an existing coverage_review.tsv and rebuild from raw "
             "coverage_report_part*.tsv files (may lose refresh-script fixes).",
    )
    args = ap.parse_args()

    part_paths = sorted(glob.glob(args.parts_glob))

    # ---- Source selection ---------------------------------------------------
    # If coverage_review.tsv already exists, it is the AUTHORITATIVE source
    # (it carries subsequent refresh_pdf_supp.py / refresh_pmc_oa.py fixes
    # AND user notes). Only re-aggregate from parts on first run or when
    # explicitly asked via --rebuild-from-parts.
    all_rows: list[dict[str, str]] = []
    header: list[str] = []
    used_source = "parts"
    if args.out_tsv.exists() and not args.rebuild_from_parts:
        with args.out_tsv.open() as fh:
            rdr = csv.DictReader(fh, delimiter="\t")
            header = list(rdr.fieldnames or [])
            all_rows = list(rdr)
        used_source = f"existing {args.out_tsv.name}"
        print(
            f"[review] refreshing from existing {args.out_tsv.name} "
            f"({len(all_rows)} rows). Use --rebuild-from-parts to force a "
            f"full rebuild from coverage_report_part*.tsv.",
            file=sys.stderr,
        )
    elif part_paths:
        for p in part_paths:
            with open(p) as fh:
                rdr = csv.DictReader(fh, delimiter="\t")
                if not header:
                    header = list(rdr.fieldnames or [])
                for row in rdr:
                    all_rows.append(row)
        print(
            f"[review] merged {len(all_rows)} rows from {len(part_paths)} part files",
            file=sys.stderr,
        )
    else:
        print(
            f"[review] no partial TSVs matched {args.parts_glob} and "
            f"{args.out_tsv} does not exist",
            file=sys.stderr,
        )
        return 2

    # Preserve any hand-edited notes from a prior review.
    prior = load_existing_notes(args.out_tsv)
    if prior:
        print(
            f"[review] carrying forward notes on {len(prior)} PMIDs from prior TSV",
            file=sys.stderr,
        )

    # Ensure review columns exist even when source is a raw part TSV.
    # If reading from existing coverage_review.tsv, gap_score + review cols
    # are already present — this is a no-op for them and just refills any
    # user-note fields that were dropped by an earlier bug.
    for row in all_rows:
        row["gap_score"] = str(gap_score(row))
        pmid = row.get("pmid") or ""
        notes = prior.get(pmid, {})
        for c in REVIEW_COLS:
            # Existing values win; prior is a fallback only.
            if not row.get(c):
                row[c] = notes.get(c, "")

    # ---- Dedup BEFORE sort ------------------------------------------------
    # When a PMID appears in more than one part, keep the row with the
    # LOWEST gap_score (best result). The old code deduped AFTER sorting
    # worst-first, so the worst row won — silently regressing coverage on
    # any part overlap.
    best_by_pmid: dict[str, dict[str, str]] = {}
    for row in all_rows:
        p = row.get("pmid") or ""
        if not p:
            continue
        existing = best_by_pmid.get(p)
        if existing is None or int(row["gap_score"]) < int(existing["gap_score"]):
            best_by_pmid[p] = row
    deduped = list(best_by_pmid.values())

    # Sort worst-first for review UX.
    deduped.sort(
        key=lambda r: (
            -int(r["gap_score"]),
            (r.get("journal") or "").lower(),
            r.get("pmid") or "",
        )
    )
    print(f"[review] source: {used_source}; deduped to {len(deduped)} PMIDs", file=sys.stderr)

    out_header = ["gap_score"] + list(REVIEW_COLS) + header  # review cols up front
    args.out_tsv.parent.mkdir(parents=True, exist_ok=True)
    with args.out_tsv.open("w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=out_header, delimiter="\t")
        wr.writeheader()
        wr.writerows(deduped)

    # Summary line.
    from collections import Counter
    gs_counts = Counter(int(r["gap_score"]) for r in deduped)
    pdf_any = sum(1 for r in deduped if (r.get("pdf_sources") or "NONE") != "NONE")
    supp_any = sum(1 for r in deduped if (r.get("supp_available") or "").lower() == "true")
    reads_any = sum(1 for r in deduped if (r.get("reads_source") or "NONE") != "NONE")
    total = len(deduped)

    print(f"[review] wrote {args.out_tsv} — {total} unique PMIDs", file=sys.stderr)
    print(f"    PDF-accessible : {pdf_any}/{total} ({100*pdf_any/total:.1f}%)", file=sys.stderr)
    print(f"    supp-accessible: {supp_any}/{total} ({100*supp_any/total:.1f}%)", file=sys.stderr)
    print(f"    reads-accessible: {reads_any}/{total} ({100*reads_any/total:.1f}%)", file=sys.stderr)
    print(f"    gap_score distribution: {dict(sorted(gs_counts.items()))}", file=sys.stderr)

    print(
        "\nOpen the TSV in LibreOffice / Excel:\n"
        f"  soffice --calc {args.out_tsv}\n"
        "Fill in 'verdict' / 'action' / 'user_notes' as you go; re-run this\n"
        "script anytime and your notes are preserved.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
