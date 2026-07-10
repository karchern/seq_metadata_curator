#!/usr/bin/env python3
"""
Given an INSDC project accession (PRJEB*/PRJNA*/PRJDB*/ERP*/SRP*/DRP*) OR
a PMID, gather per-run + per-sample metadata from ENA — and, on request,
download the fastqs.

Why ENA as the single entrypoint: ENA mirrors SRA and DDBJ, so the same
REST endpoints resolve accessions from all three archives.

Output layout (per project accession):
    data/reads/{accession}/
        study.xml         — project-level XML (title, description, links)
        samples.tsv       — filereport: one row per run, with sample_alias,
                            sample_title, library_*, fastq_ftp, sizes, …
        samples.xml       — bulk per-sample XML (checklist attributes)
        summary.json      — accession, n_runs, n_samples, total_bytes,
                            group counts inferred from sample_title
        fastq/            — only when --download-fastq is set
        fetch.log         — chronological attempts
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from Bio import Entrez

DEFAULT_EMAIL = "karchernic@gmail.com"
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ENA filereport fields we want — covers per-run + sample-level basics.
# The rich sample-attribute key/values live in the sample XML, not here.
FILEREPORT_FIELDS = ",".join(
    [
        "study_accession",
        "sample_accession",
        "experiment_accession",
        "run_accession",
        "scientific_name",
        "tax_id",
        "library_name",
        "library_strategy",
        "library_source",
        "library_selection",
        "library_layout",
        "instrument_platform",
        "instrument_model",
        "sample_alias",
        "sample_title",
        "experiment_title",
        "study_title",
        "read_count",
        "base_count",
        "fastq_ftp",
        "fastq_md5",
        "fastq_bytes",
        "submitted_ftp",
        "submitted_md5",
        "submitted_bytes",
        "submitted_format",
        "first_public",
        "last_updated",
    ]
)

PROJECT_ACC_PATTERN = re.compile(r"^(PRJ[END][AB]|ERP|SRP|DRP)\d+$", re.IGNORECASE)


# ------------------------------- utilities ----------------------------------

def new_session(email: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": BROWSER_UA, "From": email})
    return s


def http_get(
    session: requests.Session, url: str, *, stream: bool = False, timeout: int = 60
) -> requests.Response:
    delay = 1.0
    last_exc: Optional[Exception] = None
    for _ in range(4):
        try:
            r = session.get(url, stream=stream, timeout=timeout, allow_redirects=True)
            if r.status_code in (429, 500, 502, 503, 504):
                last_exc = RuntimeError(f"HTTP {r.status_code} at {url}")
                time.sleep(delay)
                delay *= 2
                continue
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"http_get gave up: {last_exc}")


def download(session: requests.Session, url: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    n = 0
    with http_get(session, url, stream=True) as r:
        r.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    fh.write(chunk)
                    n += len(chunk)
    tmp.rename(dest)
    return n


def bytes_h(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


# ---------------------- accession discovery from PMID -----------------------

def pmid_to_project_accessions(
    pmid: str, session: requests.Session, email: str, api_key: Optional[str]
) -> list[str]:
    """Try to find one or more INSDC project accessions associated with a PMID.

    Strategy:
      1. NCBI elink pubmed → bioproject → then map BioProject UID → PRJNA acc.
      2. Europe PMC cross-reference API (finds ENA/BioProject cross-refs).
    """
    Entrez.email = email
    if api_key:
        Entrez.api_key = api_key

    accs: list[str] = []

    # 1) NCBI elink pubmed → bioproject
    try:
        h = Entrez.elink(dbfrom="pubmed", id=pmid, db="bioproject")
        rec = Entrez.read(h)
        h.close()
        uids = [
            link["Id"]
            for group in rec
            for lset in group.get("LinkSetDb", [])
            for link in lset.get("Link", [])
        ]
        for uid in uids:
            h = Entrez.esummary(db="bioproject", id=uid)
            summary = Entrez.read(h)
            h.close()
            for doc in summary.get("DocumentSummarySet", {}).get(
                "DocumentSummary", []
            ):
                acc = doc.get("Project_Acc")
                if acc:
                    accs.append(acc)
        time.sleep(0.4)
    except Exception as e:
        print(f"[pmid_to_accs] NCBI elink failed: {e}", file=sys.stderr)

    # 2) Europe PMC cross-refs
    try:
        url = (
            "https://www.ebi.ac.uk/europepmc/webservices/rest/MED/"
            f"{pmid}/datalinks?format=json"
        )
        r = http_get(session, url)
        if r.status_code == 200:
            j = r.json()
            for cat in j.get("dataLinkList", {}).get("Category", []):
                for src in cat.get("Section", []):
                    for link in src.get("Linklist", {}).get("Link", []):
                        target = link.get("Target", {}).get("Identifier", {})
                        i = target.get("ID", "")
                        if PROJECT_ACC_PATTERN.match(i):
                            accs.append(i)
    except Exception as e:
        print(f"[pmid_to_accs] EuropePMC datalinks failed: {e}", file=sys.stderr)

    # unique, preserve order
    return list(dict.fromkeys(accs))


# --------------------------- fetch project data -----------------------------

@dataclass
class Summary:
    accession: str
    n_runs: int = 0
    n_samples: int = 0
    total_fastq_bytes: int = 0
    library_strategy_counts: dict[str, int] = field(default_factory=dict)
    library_source_counts: dict[str, int] = field(default_factory=dict)
    sample_title_counts: dict[str, int] = field(default_factory=dict)
    attempts: list[str] = field(default_factory=list)


def fetch_study_xml(
    session: requests.Session, acc: str, out_dir: Path, summary: Summary
) -> None:
    url = f"https://www.ebi.ac.uk/ena/browser/api/xml/{acc}"
    summary.attempts.append(f"study_xml:{url}")
    r = http_get(session, url)
    r.raise_for_status()
    (out_dir / "study.xml").write_bytes(r.content)


def fetch_filereport(
    session: requests.Session, acc: str, out_dir: Path, summary: Summary
) -> list[dict]:
    url = (
        "https://www.ebi.ac.uk/ena/portal/api/filereport"
        f"?accession={acc}&result=read_run&fields={FILEREPORT_FIELDS}&format=tsv"
    )
    summary.attempts.append(f"filereport:{url}")
    r = http_get(session, url)
    r.raise_for_status()
    text = r.text.strip()
    (out_dir / "samples.tsv").write_text(text + "\n")

    lines = text.splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    rows = [dict(zip(header, ln.split("\t"))) for ln in lines[1:]]

    summary.n_runs = len(rows)
    summary.n_samples = len({r["sample_accession"] for r in rows if r.get("sample_accession")})
    summary.library_strategy_counts = dict(
        Counter(r.get("library_strategy", "") for r in rows)
    )
    summary.library_source_counts = dict(
        Counter(r.get("library_source", "") for r in rows)
    )
    summary.sample_title_counts = dict(
        Counter(r.get("sample_title", "") for r in rows)
    )
    total = 0
    for r in rows:
        for b in (r.get("fastq_bytes") or "").split(";"):
            b = b.strip()
            if b.isdigit():
                total += int(b)
    summary.total_fastq_bytes = total
    return rows


def fetch_sample_xmls(
    session: requests.Session, rows: list[dict], out_dir: Path, summary: Summary
) -> None:
    """Bulk-fetch per-sample XML from ENA — one XML with a SAMPLE per sample."""
    sample_accs = list({r["sample_accession"] for r in rows if r.get("sample_accession")})
    if not sample_accs:
        return
    all_xml = []
    # ENA browser API accepts comma-separated lists; keep chunk size modest.
    CHUNK = 50
    for i in range(0, len(sample_accs), CHUNK):
        chunk = ",".join(sample_accs[i : i + CHUNK])
        url = f"https://www.ebi.ac.uk/ena/browser/api/xml/{chunk}"
        summary.attempts.append(f"samples_xml_chunk:{i}-{i+CHUNK}")
        r = http_get(session, url)
        r.raise_for_status()
        all_xml.append(r.text)
        time.sleep(0.2)

    # Concatenate — good enough for archival; downstream can re-parse.
    (out_dir / "samples.xml").write_text("\n".join(all_xml))


def _is_gzipped(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(2) == b"\x1f\x8b"


def download_fastqs(
    session: requests.Session, rows: list[dict], out_dir: Path, summary: Summary
) -> None:
    fastq_dir = out_dir / "fastq"
    fastq_dir.mkdir(parents=True, exist_ok=True)
    n_ok = 0
    n_skip = 0
    n_fail = 0
    for row in rows:
        for url in (row.get("fastq_ftp") or "").split(";"):
            url = url.strip()
            if not url:
                continue
            if not url.startswith("http"):
                url = "https://" + url
            # ENA serves reads as .fastq.gz. If we ever see a URL that
            # isn't gzipped, that's a signal something is off — abort.
            if not url.endswith(".gz"):
                summary.attempts.append(f"fastq_reject_non_gz:{url}")
                n_fail += 1
                continue
            dest = fastq_dir / Path(url).name
            if dest.exists():
                n_skip += 1
                continue
            try:
                download(session, url, dest)
                if not _is_gzipped(dest):
                    dest.unlink(missing_ok=True)
                    summary.attempts.append(f"fastq_bad_gzip_magic:{url}")
                    n_fail += 1
                    continue
                n_ok += 1
            except Exception as e:
                summary.attempts.append(f"fastq_fail:{url}:{e}")
                n_fail += 1
    print(
        f"[fetch_reads] fastq: {n_ok} downloaded, {n_skip} pre-existing, "
        f"{n_fail} failed",
        file=sys.stderr,
    )


# --------------------------------- main -------------------------------------

def process_accession(
    acc: str,
    out_root: Path,
    session: requests.Session,
    download_fastq: bool,
    max_bytes_gb: float,
) -> Summary:
    out_dir = out_root / acc
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = Summary(accession=acc)

    fetch_study_xml(session, acc, out_dir, summary)
    rows = fetch_filereport(session, acc, out_dir, summary)
    fetch_sample_xmls(session, rows, out_dir, summary)

    print(
        f"[{acc}] n_runs={summary.n_runs} n_samples={summary.n_samples} "
        f"total_fastq={bytes_h(summary.total_fastq_bytes)}",
        file=sys.stderr,
    )
    if summary.sample_title_counts:
        print(f"[{acc}] sample_title distribution:", file=sys.stderr)
        for title, n in sorted(
            summary.sample_title_counts.items(), key=lambda kv: -kv[1]
        ):
            print(f"    {n:4d}  {title!r}", file=sys.stderr)

    if download_fastq:
        # Metadata-linkage interlock: step 5 writes linkage_ok.json when it
        # has successfully mapped runs → case/control. Refuse to pull
        # potentially many-GB of reads before that gate has been cleared.
        linkage_marker = out_dir / "linkage_ok.json"
        if not linkage_marker.exists():
            print(
                f"[{acc}] REFUSING to download reads: no linkage_ok.json in "
                f"{out_dir}. Run the metadata-mapping step first; it writes "
                f"that marker on success.",
                file=sys.stderr,
            )
            return summary

        cap = int(max_bytes_gb * 1024 ** 3)
        if summary.total_fastq_bytes > cap:
            print(
                f"[{acc}] REFUSING to download: total {bytes_h(summary.total_fastq_bytes)} "
                f"> --max-download-gb ({max_bytes_gb} GB). Re-run with a larger cap.",
                file=sys.stderr,
            )
        else:
            download_fastqs(session, rows, out_dir, summary)

    (out_dir / "summary.json").write_text(json.dumps(asdict(summary), indent=2))
    (out_dir / "fetch.log").write_text("\n".join(summary.attempts) + "\n")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--accession",
        help="INSDC project accession (PRJEB*/PRJNA*/PRJDB*/ERP*/SRP*/DRP*).",
    )
    src.add_argument(
        "--pmid",
        help="PubMed ID; we'll look up associated project accession(s).",
    )
    ap.add_argument(
        "--out-root",
        type=Path,
        default=Path("/scratch/karcher/seq_metadata_curator/data/reads"),
    )
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    ap.add_argument("--api-key", default=None)
    ap.add_argument(
        "--download-fastq",
        action="store_true",
        help="Also download all fastq files (potentially large — see --max-download-gb).",
    )
    ap.add_argument(
        "--max-download-gb",
        type=float,
        default=20.0,
        help="Refuse to download if total fastq bytes exceed this cap.",
    )
    args = ap.parse_args()

    session = new_session(args.email)

    if args.accession:
        accessions = [args.accession.strip()]
    else:
        accessions = pmid_to_project_accessions(
            args.pmid.strip(), session, args.email, args.api_key
        )
        if not accessions:
            print(
                f"[fetch_reads] no INSDC project accession found for PMID {args.pmid}",
                file=sys.stderr,
            )
            return 2
        print(
            f"[fetch_reads] PMID {args.pmid} → {accessions}", file=sys.stderr
        )

    args.out_root.mkdir(parents=True, exist_ok=True)
    for acc in accessions:
        process_accession(
            acc, args.out_root, session, args.download_fastq, args.max_download_gb
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
