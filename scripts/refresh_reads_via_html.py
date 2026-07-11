#!/usr/bin/env python3
"""
Reads-discovery refresh via article-HTML mining.

For every row in coverage_review.tsv where `reads_source == "NONE"` and we
can plausibly reach the article HTML (PMC-OA or a publisher plugin exists),
fetch the HTML, mine INSDC accessions with the broadened regex, and
validate each candidate against ENA's filereport. When a validated
accession appears, update reads_accessions / reads_source / n_runs / total_gb.

Additive-only: never downgrades a row that already has reads.
Preserves verdict / action / user_notes.
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_coverage import (  # noqa: E402
    new_session,
    probe_ena_filereport,
    probe_reads_from_article_html,
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
    n_checked = 0
    n_rescued = 0
    total_added_runs = 0
    total_added_gb = 0.0

    reads_none_rows = [
        r for r in rows
        if (r.get("reads_source") or "NONE") == "NONE"
        and (r.get("pmc_id") or r.get("doi"))
    ]
    print(
        f"[refresh_reads_via_html] scanning {len(reads_none_rows)} rows "
        f"(reads_source=NONE with either pmc_id or doi)",
        file=sys.stderr,
    )

    for i, r in enumerate(reads_none_rows):
        pmid = r.get("pmid") or ""
        doi = r.get("doi") or ""
        pmc = r.get("pmc_id") or ""
        try:
            candidates = probe_reads_from_article_html(session, doi, pmc)
        except Exception as e:
            candidates = []
        n_checked += 1

        confirmed: list[str] = []
        total_runs = 0
        total_gb = 0.0
        for acc in candidates:
            try:
                n_runs, gb = probe_ena_filereport(session, acc)
            except Exception:
                n_runs, gb = 0, 0.0
            if n_runs > 0:
                confirmed.append(acc)
                total_runs += n_runs
                total_gb += gb

        if confirmed:
            r["reads_accessions"] = ",".join(confirmed)
            r["reads_source"] = "html_text_mine"
            r["n_runs"] = str(total_runs)
            r["total_gb"] = str(round(total_gb, 2))
            n_rescued += 1
            total_added_runs += total_runs
            total_added_gb += total_gb

        if (i + 1) % 20 == 0 or i == len(reads_none_rows) - 1:
            print(
                f"[refresh_reads_via_html] {i+1}/{len(reads_none_rows)}  "
                f"rescued={n_rescued}  runs_added={total_added_runs}  "
                f"gb_added={total_added_gb:.1f}",
                file=sys.stderr,
            )
        time.sleep(0.25)

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

    def is_pdf_ok(v: str) -> bool:
        return bool(v) and v not in ("NONE", "paywalled_local_attempted")
    pdf_any = sum(1 for r in rows if is_pdf_ok(r.get("pdf_sources") or "NONE"))
    supp_any = sum(1 for r in rows if (r.get("supp_available") or "").lower() == "true")
    reads_any = sum(1 for r in rows if (r.get("reads_source") or "NONE") != "NONE")
    total = len(rows)

    print("\n[refresh_reads_via_html] DONE", file=sys.stderr)
    print(f"    reads-NONE rows checked : {n_checked}", file=sys.stderr)
    print(f"    reads rescued this round: {n_rescued}", file=sys.stderr)
    print(f"    additional runs recorded: {total_added_runs}", file=sys.stderr)
    print(f"    additional data recorded: {total_added_gb:.1f} GB", file=sys.stderr)
    print(
        f"    NEW coverage: pdf {pdf_any}/{total} ({100*pdf_any/max(1,total):.1f}%)  "
        f"supp {supp_any}/{total} ({100*supp_any/max(1,total):.1f}%)  "
        f"reads {reads_any}/{total} ({100*reads_any/max(1,total):.1f}%)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
