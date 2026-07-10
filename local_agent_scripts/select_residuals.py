#!/usr/bin/env python3
"""Filter coverage_review.tsv → PMIDs with no PDF whose DOI is in a
Cloudflare-gated publisher family we can rescue from an EMBL-network laptop.

Emits a TSV with pmid, doi, doi_prefix, journal.
"""
import argparse, csv, sys
from pathlib import Path

# Publisher families we have a working local fetch path for (via headed
# patchright + primed profile, with human solving Elsevier's challenge once).
RESCUEABLE_PREFIXES = {"10.1016", "10.1053", "10.1002", "10.1111", "10.1080"}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-tsv", type=Path, required=True)
    ap.add_argument("--out-tsv", type=Path, required=True)
    args = ap.parse_args()

    rows_out = []
    n_no_pdf = 0
    with args.in_tsv.open() as fh:
        rdr = csv.DictReader(fh, delimiter="\t")
        for row in rdr:
            if (row.get("pdf_sources") or "").strip() != "NONE":
                continue
            n_no_pdf += 1
            prefix = (row.get("doi_prefix") or "").strip()
            if prefix in RESCUEABLE_PREFIXES:
                rows_out.append({
                    "pmid":       row["pmid"],
                    "doi":        row["doi"],
                    "doi_prefix": prefix,
                    "journal":    row.get("journal", ""),
                })

    with args.out_tsv.open("w") as fh:
        w = csv.DictWriter(fh, fieldnames=["pmid","doi","doi_prefix","journal"], delimiter="\t")
        w.writeheader()
        w.writerows(rows_out)

    print(f"[select] total pdf_sources=NONE: {n_no_pdf}", file=sys.stderr)
    print(f"[select] rescueable (matching prefixes): {len(rows_out)}", file=sys.stderr)
    by_prefix: dict[str,int] = {}
    for r in rows_out:
        by_prefix[r["doi_prefix"]] = by_prefix.get(r["doi_prefix"], 0) + 1
    for p, n in sorted(by_prefix.items()):
        print(f"  {p}: {n}", file=sys.stderr)
    print(f"[select] wrote {args.out_tsv}", file=sys.stderr)

if __name__ == "__main__":
    main()
