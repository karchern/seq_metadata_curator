#!/usr/bin/env python3
"""
Supp-discovery refresh via article-HTML mining.

Structural mirror of `refresh_reads_via_html.py`. For every row in
`coverage_review.tsv` where `supp_available != True` and we can plausibly
reach the article HTML (PMC-OA or a publisher plugin exists), fetch the
HTML, mine candidate supplementary-file URLs via
`probe_supp_from_article_html()`, HTTP-fetch each candidate,
magic-byte-verify the response, and — on any successful verified
download — mark the row as `supp_available=True`,
`supp_source=html_mining` (or `<existing>+html_mining` if a publisher tag
already exists).

Downloads are written to `data/papers/PMID_<pmid>/supp/` in the same
layout the existing publisher plugins use. Existing files are NOT
re-downloaded. All downloads are validated by magic bytes before commit
(HTML masquerading as supp is the #1 integrity risk — see
`_verify_supp_url` in probe_coverage.py).

Additive-only: never downgrades a row that already has supp.
Preserves verdict / action / user_notes.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_coverage import (  # noqa: E402
    SUPP_DOWNLOADABLE_TYPES,
    _verify_supp_url,
    new_session,
    probe_supp_from_article_html,
)

REVIEW_TSV = Path("/scratch/karcher/seq_metadata_curator/data/coverage_review.tsv")
PAPERS_DIR = Path("/scratch/karcher/seq_metadata_curator/data/papers")


def _safe_filename_from_url(url: str, source: str) -> str:
    """Extract a safe filename from a supp URL. Falls back to a
    source-tagged placeholder when the URL has no clean tail component.
    Never returns a path with `/` or `..` — the downstream write is
    always inside `PMID_<n>/supp/`.
    """
    # Special-case: Europe PMC supp bundle endpoint returns a ZIP, but its
    # URL ends in `/supplementaryFiles` (no extension). Give it a
    # descriptive filename so downstream consumers don't have to sniff.
    if url.endswith("/supplementaryFiles"):
        return "europepmc_supp.zip"

    path = urlparse(url).path
    tail = unquote(path.rsplit("/", 1)[-1])
    # Strip any query/hash-y crumbs.
    tail = tail.split("?", 1)[0].split("#", 1)[0]
    # Reject empty / dot-only / traversal.
    if not tail or tail in (".", "..") or tail.startswith("."):
        # Fall back to a stable label derived from source + URL hash.
        import hashlib
        h = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
        tail = f"{source}_{h}"
    # Sanitize any weird chars for FS safety.
    tail = tail.replace("/", "_").replace("\\", "_")
    # Cap length so filesystem allocations stay sane.
    if len(tail) > 200:
        stem, dot, ext = tail.rpartition(".")
        if dot and len(ext) < 8:
            tail = stem[:190] + "." + ext
        else:
            tail = tail[:200]
    return tail


def _download_supp(
    session, url: str, dest: Path, timeout: int = 120
) -> Optional[int]:
    """Stream `url` to `dest`, verifying magic bytes on the first chunk.
    Returns bytes written on success or None on failure. Never leaves a
    partial file at `dest` — writes to `dest.part` and renames on success.
    """
    import requests

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    # Import here to avoid top-level dependency ordering surprises.
    from probe_coverage import _SUPP_MAGIC_BYTES, _SUPP_REJECT_MAGIC

    n = 0
    try:
        with session.get(
            url,
            timeout=timeout,
            stream=True,
            allow_redirects=True,
        ) as r:
            if r.status_code != 200:
                return None
            first_chunk = True
            with tmp.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    if first_chunk:
                        first_chunk = False
                        # Reject HTML-in-disguise BEFORE we write anything.
                        if chunk[:8].startswith(_SUPP_REJECT_MAGIC):
                            return None
                        # Accept only recognised supp magic OR text-supp
                        # (BOM / non-HTML plain text) via a URL-ext check.
                        ok = any(
                            chunk[:16].startswith(m)
                            for m in _SUPP_MAGIC_BYTES[:-1]
                        )
                        if not ok:
                            # Try text-supp
                            content_type = r.headers.get("content-type", "")
                            from probe_coverage import _is_probable_text_supp
                            if not _is_probable_text_supp(
                                chunk[:16], content_type, url
                            ):
                                return None
                    fh.write(chunk)
                    n += len(chunk)
    except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
        return None
    finally:
        # Clean up partial file if it exists AND we returned early.
        if n == 0:
            tmp.unlink(missing_ok=True)

    if n <= 0:
        tmp.unlink(missing_ok=True)
        return None
    try:
        tmp.rename(dest)
    except OSError:
        tmp.unlink(missing_ok=True)
        return None
    return n


def recompute_gap(r: dict) -> str:
    s = 0
    if (r.get("pdf_sources") or "NONE") == "NONE":
        s += 1
    if (r.get("supp_available") or "").lower() != "true":
        s += 1
    if (r.get("reads_source") or "NONE") == "NONE":
        s += 1
    return str(s)


def _merge_supp_source(existing: str, new_tag: str = "html_mining") -> str:
    """Combine an existing supp_source tag with the new html_mining tag.

    Rules:
      * If existing is NONE / empty → replace with new_tag.
      * If existing already ends with `+html_mining` → keep as-is.
      * Else → append `+html_mining` (preserves publisher provenance).
    """
    e = (existing or "").strip()
    if not e or e.upper() == "NONE":
        return new_tag
    if e.endswith(f"+{new_tag}") or e == new_tag:
        return e
    return f"{e}+{new_tag}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="If set, only process the first N rows (for testing).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Probe + verify supp URLs but don't download or update TSV.",
    )
    args = ap.parse_args(argv)

    with REVIEW_TSV.open() as fh:
        rdr = csv.DictReader(fh, delimiter="\t")
        header = list(rdr.fieldnames or [])
        rows = list(rdr)

    session = new_session()
    n_checked = 0
    n_rescued = 0
    n_files_dl = 0
    total_bytes = 0
    n_pmc_bin_seen = 0  # detected but un-fetchable POW-gated URLs

    no_supp_rows = [
        r
        for r in rows
        if (r.get("supp_available") or "").lower() != "true"
        and (r.get("pmc_id") or r.get("doi"))
    ]
    if args.limit:
        no_supp_rows = no_supp_rows[: args.limit]
    print(
        f"[refresh_supp_via_html] scanning {len(no_supp_rows)} rows "
        f"(supp_available!=True with either pmc_id or doi)",
        file=sys.stderr,
    )

    for i, r in enumerate(no_supp_rows):
        pmid = r.get("pmid") or ""
        doi = r.get("doi") or ""
        pmc = r.get("pmc_id") or ""
        try:
            candidates = probe_supp_from_article_html(session, doi, pmc)
        except Exception:
            candidates = {}
        n_checked += 1

        # Track POW-gated detections for reporting even when they don't
        # count as rescues.
        n_pmc_bin_seen += len(candidates.get("pmc_bin", []))

        # Filter to only downloadable-from-cluster URL types.
        actionable: list[tuple[str, str]] = []
        for src_name, urls in candidates.items():
            if src_name not in SUPP_DOWNLOADABLE_TYPES:
                continue
            for u in urls:
                actionable.append((src_name, u))

        if not actionable:
            if (i + 1) % 20 == 0 or i == len(no_supp_rows) - 1:
                print(
                    f"[refresh_supp_via_html] {i+1}/{len(no_supp_rows)}  "
                    f"rescued={n_rescued}  files_dl={n_files_dl}  "
                    f"MB={total_bytes/(1024*1024):.1f}",
                    file=sys.stderr,
                )
            time.sleep(0.25)
            continue

        # Verify each candidate cheaply first; then download the ones
        # that pass verification. Save to data/papers/PMID_<pmid>/supp/.
        row_supp_dir = PAPERS_DIR / f"PMID_{pmid}" / "supp"
        row_supp_dir.mkdir(parents=True, exist_ok=True)
        row_rescued = False
        row_new_bytes = 0
        for src_name, url in actionable:
            fname = _safe_filename_from_url(url, src_name)
            dest = row_supp_dir / fname
            if dest.exists() and dest.stat().st_size > 0:
                # Publisher-existing file — don't re-download; still count
                # as evidence supp is accessible.
                row_rescued = True
                continue
            # Cheap verify: HEAD-ish fetch of first bytes.
            try:
                v = _verify_supp_url(session, url)
            except Exception:
                v = None
            if v is None:
                continue
            if args.dry_run:
                row_rescued = True
                n_files_dl += 1
                continue
            n = _download_supp(session, url, dest)
            if n is None or n < 128:
                continue
            n_files_dl += 1
            row_new_bytes += n
            row_rescued = True

        if row_rescued:
            r["supp_available"] = "True"
            r["supp_source"] = _merge_supp_source(r.get("supp_source", ""))
            n_rescued += 1
            total_bytes += row_new_bytes

        if (i + 1) % 20 == 0 or i == len(no_supp_rows) - 1:
            print(
                f"[refresh_supp_via_html] {i+1}/{len(no_supp_rows)}  "
                f"rescued={n_rescued}  files_dl={n_files_dl}  "
                f"MB={total_bytes/(1024*1024):.1f}  "
                f"pmc_bin_seen(pow-gated)={n_pmc_bin_seen}",
                file=sys.stderr,
            )
        time.sleep(0.35)

    if args.dry_run:
        print("[refresh_supp_via_html] DRY-RUN — TSV NOT written.", file=sys.stderr)
    else:
        # Recompute gap_score for every row (some may have changed).
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

    # Final scoreboard.
    def is_pdf_ok(v: str) -> bool:
        return bool(v) and v not in ("NONE", "paywalled_local_attempted")

    pdf_any = sum(1 for r in rows if is_pdf_ok(r.get("pdf_sources") or "NONE"))
    supp_any = sum(1 for r in rows if (r.get("supp_available") or "").lower() == "true")
    reads_any = sum(1 for r in rows if (r.get("reads_source") or "NONE") != "NONE")
    total = len(rows)

    print("\n[refresh_supp_via_html] DONE", file=sys.stderr)
    print(f"    supp-NONE rows checked  : {n_checked}", file=sys.stderr)
    print(f"    supp rescued this round : {n_rescued}", file=sys.stderr)
    print(f"    supp files downloaded   : {n_files_dl}", file=sys.stderr)
    print(f"    total MB downloaded     : {total_bytes/(1024*1024):.1f}", file=sys.stderr)
    print(
        f"    pmc_bin URLs detected but POW-gated (not counted): {n_pmc_bin_seen}",
        file=sys.stderr,
    )
    print(
        f"    NEW coverage: pdf {pdf_any}/{total} ({100*pdf_any/max(1,total):.1f}%)  "
        f"supp {supp_any}/{total} ({100*supp_any/max(1,total):.1f}%)  "
        f"reads {reads_any}/{total} ({100*reads_any/max(1,total):.1f}%)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
