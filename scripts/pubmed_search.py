#!/usr/bin/env python3
"""
Query PubMed → write PMIDs to a file, one per line.

Usage:
    pubmed_search.py --query "cancer AND microbiome AND (case OR control)" \
        --retmax 500 --out /scratch/karcher/seq_metadata_curator/data/pmids.txt

Notes:
  - NCBI E-utilities want an email; we use the account address by default.
  - Without an NCBI API key, we throttle to <3 req/s.
"""
import argparse
import sys
import time
from pathlib import Path

from Bio import Entrez


def search(query: str, retmax: int, email: str, api_key: str | None) -> list[str]:
    Entrez.email = email
    if api_key:
        Entrez.api_key = api_key

    handle = Entrez.esearch(
        db="pubmed",
        term=query,
        retmax=retmax,
        usehistory="n",
        sort="pub_date",
    )
    record = Entrez.read(handle)
    handle.close()

    ids = list(record.get("IdList", []))
    total = int(record.get("Count", len(ids)))
    print(
        f"[pubmed_search] query returned {total} hits; retrieved {len(ids)} "
        f"(retmax={retmax})",
        file=sys.stderr,
    )
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--query", required=True, help="PubMed query string")
    ap.add_argument("--retmax", type=int, default=500)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--email", default="karchernic@gmail.com")
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args()

    ids = search(args.query, args.retmax, args.email, args.api_key)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(ids) + ("\n" if ids else ""))
    print(f"[pubmed_search] wrote {len(ids)} PMIDs → {args.out}", file=sys.stderr)
    time.sleep(0.4)
    return 0


if __name__ == "__main__":
    sys.exit(main())
