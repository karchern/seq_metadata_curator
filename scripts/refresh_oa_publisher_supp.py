#!/usr/bin/env python3
"""Refresh supplementary files for BMC / Frontiers / MDPI DOIs.

Runs the newly-added publisher plugins (bmc, frontiers, mdpi) over every
corpus row whose DOI prefix is 10.1186 / 10.3389 / 10.3390. For each row:

  1. Call `publisher.fetch_supp(session, doi, out_dir)` where `out_dir` is
     `data/papers/PMID_{pmid}/`.
  2. Diff the supp/ directory before-vs-after to determine newly-landed
     files (excludes .part / manifest.tsv per convention).
  3. Print a per-row status line + a per-publisher summary at the end.

DOES NOT modify `coverage_review.tsv`. The canonical way to fold new
supp discoveries into the coverage table is `refresh_pdf_supp.py`, which
this script complements. This one's sole job is to actually land files
so that when the canonical refresh runs, `supp_available` flips to True
because the supp/ dir exists.

Skip-if-exists is enforced by each publisher plugin's fetch_supp
implementation (each file's dest.exists() → skip).
"""
from __future__ import annotations

import csv
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

from publishers import get_publisher  # noqa: E402

REVIEW_TSV = Path("/scratch/karcher/seq_metadata_curator/data/coverage_review.tsv")
PAPERS_ROOT = Path("/scratch/karcher/seq_metadata_curator/data/papers")
TARGET_PREFIXES = {"10.1186", "10.3389", "10.3390"}
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _current_supp_files(paper_dir: Path) -> set[str]:
    """Return {filename} for real supp files (excludes manifest / .part)."""
    supp_dir = paper_dir / "supp"
    if not supp_dir.exists():
        return set()
    return {
        p.name
        for p in supp_dir.iterdir()
        if p.is_file()
        and p.name != "manifest.tsv"
        and not p.name.endswith(".part")
    }


def _load_rows() -> list[dict]:
    with REVIEW_TSV.open() as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def main() -> int:
    rows = _load_rows()
    targets = [r for r in rows if r.get("doi_prefix") in TARGET_PREFIXES]
    print(f"[refresh-oa] {len(targets)} target rows across "
          f"{','.join(sorted(TARGET_PREFIXES))}", flush=True)

    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_UA})

    per_pub = defaultdict(lambda: Counter())
    landed_by_pub: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    for i, r in enumerate(targets, 1):
        doi = r.get("doi", "")
        pmid = r.get("pmid", "")
        if not doi or not pmid:
            continue
        pub = get_publisher(doi)
        if pub is None:
            continue
        paper_dir = PAPERS_ROOT / f"PMID_{pmid}"
        paper_dir.mkdir(parents=True, exist_ok=True)
        before = _current_supp_files(paper_dir)
        try:
            _ = pub.fetch_supp(session, doi, paper_dir)
        except Exception as e:
            per_pub[pub.name]["fetch_err"] += 1
            print(f"[refresh-oa] {i}/{len(targets)} {doi} ERR: {e}",
                  flush=True)
            continue
        after = _current_supp_files(paper_dir)
        newly = sorted(after - before)
        if newly:
            per_pub[pub.name]["rows_with_new_files"] += 1
            per_pub[pub.name]["new_files_total"] += len(newly)
            landed_by_pub[pub.name].append((doi, newly))
        else:
            per_pub[pub.name]["rows_no_new_files"] += 1

        if i % 15 == 0 or i == len(targets):
            print(f"[refresh-oa] {i}/{len(targets)}  "
                  f"pub={pub.name}  "
                  f"newly={len(newly)}  "
                  f"total_supp_files={len(after)}",
                  flush=True)
        time.sleep(0.15)

    print("\n[refresh-oa] SUMMARY", flush=True)
    for name in sorted(per_pub.keys()):
        st = per_pub[name]
        print(f"  {name}: rows_with_new={st['rows_with_new_files']}  "
              f"new_files={st['new_files_total']}  "
              f"no_new={st['rows_no_new_files']}  "
              f"errors={st['fetch_err']}", flush=True)

    # Print per-row rescues for the report
    print("\n[refresh-oa] RESCUES BY PUBLISHER", flush=True)
    for pub_name, entries in landed_by_pub.items():
        print(f"\n  === {pub_name} ({len(entries)} rows rescued) ===",
              flush=True)
        for doi, files in entries[:30]:
            print(f"    {doi}: {files}", flush=True)
        if len(entries) > 30:
            print(f"    ... and {len(entries) - 30} more", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
