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
    args = ap.parse_args()

    part_paths = sorted(glob.glob(args.parts_glob))
    if not part_paths:
        print(f"[review] no partial TSVs matched {args.parts_glob}", file=sys.stderr)
        return 2

    # Merge all part TSVs into memory (small — max ~1128 rows).
    all_rows: list[dict[str, str]] = []
    header: list[str] = []
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

    # Preserve any hand-edited notes from a prior review.
    prior = load_existing_notes(args.out_tsv)
    if prior:
        print(
            f"[review] carrying forward notes on {len(prior)} PMIDs from prior TSV",
            file=sys.stderr,
        )

    # Compute gap score + attach existing notes.
    for row in all_rows:
        row["gap_score"] = str(gap_score(row))
        pmid = row.get("pmid") or ""
        notes = prior.get(pmid, {})
        for c in REVIEW_COLS:
            row[c] = notes.get(c, "")

    # Sort worst-first: high gap_score, then journal, then pmid.
    all_rows.sort(
        key=lambda r: (
            -int(r["gap_score"]),
            (r.get("journal") or "").lower(),
            r.get("pmid") or "",
        )
    )

    # Deduplicate by PMID (in case a PMID accidentally landed in two parts).
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for row in all_rows:
        p = row.get("pmid") or ""
        if p in seen:
            continue
        seen.add(p)
        deduped.append(row)

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
