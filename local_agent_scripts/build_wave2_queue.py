#!/usr/bin/env python3
"""Build the wave-2 laptop-side rescue queue TSV.

Reads `data/master_paper_disposition.tsv` (priority + publisher bucket) and
`data/coverage_review.tsv` (the current known artifact state) and emits one
row per PMID x missing-artifact for P1-local-rescue publishers only.

Columns emitted:
  pmid
  doi
  publisher_bucket
  artifact_type ∈ {pdf, supp, reads}
  target_url                 or "SCRAPE-FROM-ARTICLE-HTML"
  rationale
  expected_local_action

Skips:
  - IGNORE-* priorities
  - artifacts already flagged `yes` in the disposition table
  - non-P1-local-rescue rows

Deterministic ordering: publisher_bucket (Elsevier first — biggest gap),
then descending gap_score, then pmid.

Run on cluster from repo root:
  /g/typas/Personal_Folders/Nic/miniforge3/envs/pyhmmer/bin/python \\
      local_agent_scripts/build_wave2_queue.py \\
      --out data/wave2_local_rescue_queue.tsv
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Optional

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]

# publisher_bucket ordering (largest gap first)
BUCKET_ORDER = {
    "elsevier-or-cell_press": 0,
    "wiley": 1,
    "taylor-francis": 2,
    "elsevier-gastro": 3,
}
TARGET_BUCKETS = set(BUCKET_ORDER)

# Cell Press DOI-suffix codes (mirrors publishers/cell_press.py CELL_PRESS_SUFFIXES)
_CELL_PRESS_SUFFIXES = {
    "cell", "ccell", "chom", "cmet", "celrep", "xcrm", "xgen", "stem",
    "molcel", "immuni", "cub", "jcmgh", "devcel", "neuron", "med",
    "chembiol", "xinn",
}
_CELL_PRESS_SUFFIX_RE = re.compile(r"^10\.1016/j\.([a-z]+)\.\d{4}\.")

# Cell.com journal-slug map (for building fulltext URLs when we know the code)
_CELL_JOURNAL_SLUG = {
    "cell": "cell",
    "ccell": "cancer-cell",
    "chom": "cell-host-microbe",
    "cmet": "cell-metabolism",
    "celrep": "cell-reports",
    "xcrm": "cell-reports-medicine",
    "xgen": "cell-genomics",
    "stem": "cell-stem-cell",
    "molcel": "molecular-cell",
    "immuni": "immunity",
    "cub": "current-biology",
    "jcmgh": "cellmolgastro",   # redirects to cmghjournal.org
    "devcel": "developmental-cell",
    "neuron": "neuron",
    "med": "med",
    "chembiol": "cell-chemical-biology",
    "xinn": "the-innovation",
}


def _cell_press_code(doi: str) -> Optional[str]:
    if not isinstance(doi, str):
        return None
    m = _CELL_PRESS_SUFFIX_RE.match(doi)
    if not m:
        return None
    code = m.group(1)
    return code if code in _CELL_PRESS_SUFFIXES else None


def _is_ignore(priority: str) -> bool:
    return isinstance(priority, str) and priority.startswith("IGNORE-")


def _pdf_target(doi: str, bucket: str) -> tuple[str, str, str]:
    """Return (target_url, rationale, expected_local_action) for a PDF gap."""
    if bucket == "elsevier-or-cell_press":
        cp = _cell_press_code(doi)
        if cp is not None:
            # Cell Press family — cell.com CDN. Actual PII must be discovered
            # via CrossRef `resource.primary.URL` at fetch time; article
            # landing goes through the DOI resolver.
            slug = _CELL_JOURNAL_SLUG.get(cp, cp)
            article = f"https://doi.org/{doi}"
            rationale = (
                f"Cell Press ({cp}) on www.cell.com — Cloudflare-gated from cluster IP; "
                f"cluster fetch returns 403 CF challenge."
            )
            action = (
                f"Navigate {article} in primed Chrome; wait for CF clear; capture "
                f"the DOM href matching /action/showPdf?pii=... or "
                f"https://www.cell.com/{slug}/pdfExtended/{{PII}} ; download via "
                "page.expect_download(); verify %PDF magic; write to "
                "data/papers/PMID_{pmid}/paper.pdf."
            )
            return article, rationale, action
        # Elsevier ScienceDirect
        article = (
            "https://api.crossref.org/works/" + doi
            + "  -> resource.primary.URL -> "
            + "https://www.sciencedirect.com/science/article/pii/{PII}"
        )
        rationale = (
            "Elsevier ScienceDirect — Cloudflare bot-mitigation blocks cluster "
            "IP (403 cf-mitigated). Requires stealth-Chromium + primed "
            "cf_clearance."
        )
        action = (
            "1) CrossRef GET https://api.crossref.org/works/" + doi + " -> extract PII "
            "from resource.primary.URL; 2) navigate "
            "https://www.sciencedirect.com/science/article/pii/{PII} in primed "
            "Chrome (wait_for_challenge_clear, interactive=True); 3) parse DOM for "
            "signed a[href*='pdfft'] link containing md5=; 4) navigate the signed "
            "URL and page.expect_download(); 5) %PDF magic check; write to "
            "data/papers/PMID_{pmid}/paper.pdf."
        )
        return article, rationale, action
    if bucket == "elsevier-gastro":
        # 10.1053/j.gastro.YYYY.MM.NNN — Gastroenterology (Elsevier); either lives
        # on sciencedirect.com or gastrojournal.org (both Cloudflare-gated).
        article = (
            "https://api.crossref.org/works/" + doi
            + "  -> resource.primary.URL -> "
            + "https://www.sciencedirect.com/science/article/pii/{PII}"
        )
        rationale = (
            "Elsevier Gastroenterology (10.1053) on sciencedirect.com — Cloudflare-"
            "gated from cluster IP."
        )
        action = (
            "Same as Elsevier ScienceDirect: CrossRef -> PII -> sciencedirect PII "
            "URL -> DOM-signed pdfft link -> page.expect_download() -> %PDF check "
            "-> data/papers/PMID_{pmid}/paper.pdf."
        )
        return article, rationale, action
    if bucket == "wiley":
        article = f"https://onlinelibrary.wiley.com/doi/{doi}"
        rationale = (
            "Wiley onlinelibrary.wiley.com — Cloudflare-gated from cluster IP."
        )
        action = (
            f"Navigate {article} in primed Chrome; PDF endpoint is "
            f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi} ; "
            "page.expect_download() (auto-download prefs); %PDF magic check; "
            "write to data/papers/PMID_{pmid}/paper.pdf."
        )
        return article, rationale, action
    if bucket == "taylor-francis":
        article = f"https://www.tandfonline.com/doi/full/{doi}"
        rationale = (
            "Taylor & Francis tandfonline.com — Cloudflare-gated from cluster IP; "
            "subscription may also be required for some titles."
        )
        action = (
            f"Navigate {article} in primed Chrome; PDF endpoint is "
            f"https://www.tandfonline.com/doi/pdf/{doi}?download=true ; "
            "page.expect_download() (auto-download prefs); %PDF magic check; "
            "write to data/papers/PMID_{pmid}/paper.pdf. NOTE: T&F often "
            "returns paywall page instead of PDF — record as no_subscription."
        )
        return article, rationale, action
    return "", "unknown-bucket", ""


def _supp_target(doi: str, bucket: str) -> tuple[str, str, str]:
    """Return (target_url, rationale, expected_local_action) for a supp gap."""
    if bucket in ("elsevier-or-cell_press", "elsevier-gastro"):
        cp = _cell_press_code(doi)
        if cp is not None:
            slug = _CELL_JOURNAL_SLUG.get(cp, cp)
            article = f"https://www.cell.com/{slug}/fulltext/{{PII}}"
            rationale = (
                f"Cell Press supp ({cp}) — supp files at "
                f"https://www.cell.com/cms/{doi}/attachment/{{uuid}}/mmc{{N}}.{{ext}} ; "
                "uuid is per-file and only discoverable by parsing the fulltext HTML."
            )
            action = (
                f"1) Look for a pre-cached manifest at data/papers/PMID_{{pmid}}/"
                "supp/manifest_pending_playwright.tsv (written by publisher plugin "
                "if it managed a landing fetch). If it exists, download each URL "
                "and skip HTML-parse. Otherwise: 2) fetch fulltext HTML via "
                f"CrossRef -> PII -> https://www.cell.com/{slug}/fulltext/{{PII}} in "
                "primed Chrome; 3) regex-extract "
                f"'https://www.cell.com/cms/{doi}/attachment/<uuid>/mmc<N>.<ext>' "
                "URLs; 4) navigate each in the same browser to trigger download; "
                "5) magic-check (%PDF/PK/…); 6) write to "
                "data/papers/PMID_{pmid}/supp/<mmc*.ext>."
            )
            return article, rationale, action
        # ScienceDirect supp lives at ars.els-cdn.com/content/image/1-s2.0-{PII}-mmcN.ext
        # Enumeration MUST come from the article HTML (list of supp filenames + IDs).
        article = "https://www.sciencedirect.com/science/article/pii/{PII}"
        rationale = (
            "Elsevier ScienceDirect supp — supp files hosted at "
            "https://ars.els-cdn.com/content/image/1-s2.0-{PII}-mmc{N}.{ext} ; "
            "filenames + N discoverable only by parsing the article HTML (JS-"
            "rendered supp panel)."
        )
        action = (
            "1) Resolve PII via CrossRef; 2) navigate "
            "https://www.sciencedirect.com/science/article/pii/{PII} in primed "
            "Chrome; wait for JS supp panel to render (looking for div.Appendices "
            "or a[href*='mmc']); 3) extract every href matching "
            "'https://ars.els-cdn.com/content/image/1-s2.0-<PII>-mmc<N>.<ext>' or "
            "'/pii/<PII>/1-s2.0-<PII>-mmc<N>.<ext>' ; 4) navigate each in the same "
            "browser context (cf_clearance + PII referer), page.expect_download() ; "
            "5) magic-check ; 6) write to data/papers/PMID_{pmid}/supp/<name>."
        )
        return article, rationale, action
    if bucket == "wiley":
        article = f"https://onlinelibrary.wiley.com/doi/{doi}"
        rationale = (
            "Wiley supp — links exposed on article HTML as "
            "https://onlinelibrary.wiley.com/action/downloadSupplement?"
            f"doi={doi}&file=<file> ; file names are per-article and require HTML "
            "parse."
        )
        action = (
            f"1) Navigate {article} in primed Chrome; 2) parse DOM for anchors "
            "matching /action/downloadSupplement?doi=...&file=... (also try "
            "'section.article-section__supporting-information a[href*=\"downloadSupplement\"]') ; "
            "3) navigate each URL, page.expect_download(); 4) magic-check; 5) "
            "write to data/papers/PMID_{pmid}/supp/<file>."
        )
        return article, rationale, action
    if bucket == "taylor-francis":
        article = f"https://www.tandfonline.com/doi/full/{doi}"
        rationale = (
            "T&F supp — anchors at "
            "https://www.tandfonline.com/doi/suppl/{doi}/suppl_file/<file> ; "
            "only enumerable from article HTML supp section."
        )
        action = (
            f"1) Navigate {article} in primed Chrome; 2) parse DOM for anchors "
            "matching /doi/suppl/{doi}/suppl_file/<file>; 3) navigate each URL, "
            "page.expect_download(); 4) magic-check; 5) write to "
            "data/papers/PMID_{pmid}/supp/<file>. NOTE: often paywall-gated even "
            "on institutional network — record 'no_subscription' when so."
        )
        return article, rationale, action
    return "", "unknown-bucket", ""


def _reads_target(doi: str, bucket: str) -> tuple[str, str, str]:
    """Return (target_url, rationale, expected_local_action) for a reads gap.

    Reads-mining strategy is *article-HTML* scraping: laptop-side Playwright
    fetches the fulltext HTML (which the cluster couldn't reach), regex-mines
    INSDC accessions from Data Availability / Methods sections, then emits an
    entry to `data/wave2_local_reads_rescues.tsv`. Actual fastq downloads
    remain a separate cluster-side step (guarded by `linkage_ok.json`).
    """
    common_rationale = (
        "Reads gap where cluster-side EuropePMC datalinks + NCBI elink + "
        "abstract-regex returned NONE. Cluster couldn't fetch the fulltext HTML "
        "(publisher CDN is Cloudflare-gated), so any accession embedded in a "
        "Data Availability section wasn't visible."
    )
    common_action = (
        "Fetch the Cloudflare-gated article HTML in primed Chrome, regex-extract "
        "INSDC accessions (broadened set: PRJ[END][AB]\\d+, ERP\\d+, SRP\\d+, "
        "DRP\\d+, DRA\\d+, E-MTAB-\\d+, E-GEOD-\\d+, GSE\\d+); dedupe; probe "
        "each against ENA (https://www.ebi.ac.uk/ena/portal/api/filereport?"
        "accession=<ACC>&result=read_run&fields=run_accession,fastq_bytes) to "
        "count runs + total_gb; append one row per PMID to "
        "data/wave2_local_reads_rescues.tsv "
        "(pmid,reads_accessions,reads_source=laptop_scrape,n_runs,total_gb)."
    )
    return "SCRAPE-FROM-ARTICLE-HTML", common_rationale, common_action


def build_queue(disp_tsv: Path, out_tsv: Path) -> tuple[int, dict[tuple[str, str], int]]:
    disp = pd.read_csv(disp_tsv, sep="\t", dtype=str).fillna("")
    disp["gap_score"] = pd.to_numeric(disp["gap_score"], errors="coerce").fillna(0).astype(int)

    # keep only P1-local-rescue in TARGET_BUCKETS, exclude IGNORE-*
    mask = (
        (disp["priority"] == "P1-local-rescue")
        & (disp["publisher_bucket"].isin(TARGET_BUCKETS))
        & (~disp["priority"].map(_is_ignore))
    )
    sub = disp[mask].copy()

    rows_out: list[dict] = []

    # deterministic bucket order (Elsevier -> Wiley -> T&F -> ElsevierGastro),
    # then gap_score desc, then pmid asc
    sub["_bucket_order"] = sub["publisher_bucket"].map(BUCKET_ORDER)
    sub.sort_values(
        by=["_bucket_order", "gap_score", "pmid"],
        ascending=[True, False, True],
        inplace=True,
    )

    per_bucket_artifact_counts: dict[tuple[str, str], int] = {}

    for _, r in sub.iterrows():
        pmid = r["pmid"]
        doi = r["doi"]
        bucket = r["publisher_bucket"]

        # PDF gap
        if r["cur_pdf_ok"] == "no":
            url, rationale, action = _pdf_target(doi, bucket)
            rows_out.append({
                "pmid": pmid,
                "doi": doi,
                "publisher_bucket": bucket,
                "artifact_type": "pdf",
                "target_url": url,
                "rationale": rationale,
                "expected_local_action": action,
            })
            per_bucket_artifact_counts[(bucket, "pdf")] = (
                per_bucket_artifact_counts.get((bucket, "pdf"), 0) + 1
            )

        # SUPP gap
        if r["cur_supp_ok"] == "no":
            url, rationale, action = _supp_target(doi, bucket)
            rows_out.append({
                "pmid": pmid,
                "doi": doi,
                "publisher_bucket": bucket,
                "artifact_type": "supp",
                "target_url": url,
                "rationale": rationale,
                "expected_local_action": action,
            })
            per_bucket_artifact_counts[(bucket, "supp")] = (
                per_bucket_artifact_counts.get((bucket, "supp"), 0) + 1
            )

        # READS gap
        if r["cur_reads_ok"] == "no":
            url, rationale, action = _reads_target(doi, bucket)
            rows_out.append({
                "pmid": pmid,
                "doi": doi,
                "publisher_bucket": bucket,
                "artifact_type": "reads",
                "target_url": url,
                "rationale": rationale,
                "expected_local_action": action,
            })
            per_bucket_artifact_counts[(bucket, "reads")] = (
                per_bucket_artifact_counts.get((bucket, "reads"), 0) + 1
            )

    out_tsv.parent.mkdir(parents=True, exist_ok=True)
    with out_tsv.open("w", newline="") as fh:
        w = csv.DictWriter(
            fh,
            delimiter="\t",
            fieldnames=[
                "pmid", "doi", "publisher_bucket", "artifact_type",
                "target_url", "rationale", "expected_local_action",
            ],
        )
        w.writeheader()
        w.writerows(rows_out)

    return len(rows_out), per_bucket_artifact_counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--disp",
        type=Path,
        default=REPO_ROOT / "data" / "master_paper_disposition.tsv",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "data" / "wave2_local_rescue_queue.tsv",
    )
    args = ap.parse_args()

    if not args.disp.exists():
        print(f"disposition TSV missing: {args.disp}", file=sys.stderr)
        return 2

    total, per_bucket = build_queue(args.disp, args.out)
    print(f"wrote {total} rows to {args.out}", file=sys.stderr)
    print("Per bucket x artifact:", file=sys.stderr)
    for (bucket, art), n in sorted(per_bucket.items()):
        print(f"  {bucket:26s} {art:5s} {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
