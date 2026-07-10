#!/usr/bin/env python3
"""
Step 5 (v1): build a run ↔ case/control mapping for one ENA project.

Reads:
    data/reads/{acc}/samples.tsv    (from fetch_reads.py)
    data/reads/{acc}/samples.xml    (from fetch_reads.py)

Writes:
    data/reads/{acc}/mapping.tsv    — one row per run, with columns
        run_accession
        sample_accession
        sample_alias
        sample_title
        library_layout
        library_strategy
        fastq_ftp
        group_raw          — text snippet that matched
        group_fine         — canonical fine-grained label (e.g. case_advanced_adenoma)
        group_coarse       — binary case / control
        confidence         — high | medium | low | none
        method             — which signal produced the label

    data/reads/{acc}/linkage_ok.json  — ONLY if EVERY run got a non-null label.
        Otherwise linkage_partial.json (mapping still written).

v1 signals (ordered — first match wins):
    1. filereport `sample_title`   (highest — direct label at INSDC level)
    2. filereport `experiment_title` (weaker fallback)
    3. sample-XML SAMPLE_ATTRIBUTES: TAG in (host_disease, disease, phenotype, …)
    4. filereport `sample_alias`   (last resort — usually cryptic IDs)

Deliberately NOT in v1: LLM-based inference, cross-reference to paper's
supp tables, cluster-count-matching against paper's stated N. Those are
step-5-v2+ concerns; the scaffolding here (schema + interlock) is fixed.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ------------- ordered pattern table (specific → general) ----------------
# (compiled regex, group_fine, group_coarse)
_RAW_PATTERNS: list[tuple[str, str, str]] = [
    # explicit healthy / control
    (r"\bhealthy\s+control", "control_healthy", "control"),
    (r"\bhealthy\b", "control_healthy", "control"),
    (r"\bnon[-\s]?(?:tumou?r|cancer|malignant|neoplas\w*)", "control_matched", "control"),
    (r"\badjacent\s+(?:normal|non[-\s]?tumou?r)", "control_matched", "control"),
    (r"\bnormal\b", "control_normal", "control"),
    (r"\bcontrol", "control", "control"),
    # advanced adenoma before adenoma; cancer/carcinoma before generic tumor
    (r"\badvanced\s+adenoma", "case_advanced_adenoma", "case"),
    (r"\bhigh[-\s]?grade\s+adenoma", "case_advanced_adenoma", "case"),
    (r"\badenoma", "case_adenoma", "case"),
    (r"\bpolyp", "case_polyp", "case"),
    (r"\bcarcinoma", "case_carcinoma", "case"),
    (r"\bcolorectal\s+cancer", "case_crc", "case"),
    (r"\bCRC\b", "case_crc", "case"),
    (r"\bmalignant", "case_malignant", "case"),
    (r"\btumou?r", "case_tumor", "case"),
    (r"\bcancer", "case_cancer", "case"),
    # IBD family (in scope per user notes)
    (r"\bulcerative\s+colitis", "case_uc", "case"),
    (r"\bcrohn'?s?\b", "case_cd", "case"),
    (r"\bIBD\b", "case_ibd", "case"),
]
PATTERNS = [(re.compile(pat, re.IGNORECASE), fine, coarse)
            for pat, fine, coarse in _RAW_PATTERNS]

SAMPLE_ATTR_TAGS = {
    "host_disease", "disease", "phenotype", "diagnosis",
    "clinical_status", "condition", "disease_state", "subject_group",
    "host_disease_status", "sample_type",
}


# ---------------------------- extraction -----------------------------------

def extract_group(text: str) -> Optional[tuple[str, str, str]]:
    """Return (matched_snippet, group_fine, group_coarse) or None."""
    if not text:
        return None
    for pat, fine, coarse in PATTERNS:
        m = pat.search(text)
        if m:
            return (m.group(0), fine, coarse)
    return None


# --------------------- parse samples.xml → attribute map -------------------

def load_sample_attributes(xml_path: Path) -> dict[str, dict[str, str]]:
    """{sample_accession: {tag_lowercase: value}} — only keys we care about."""
    out: dict[str, dict[str, str]] = {}
    if not xml_path.exists():
        return out
    # samples.xml is a concatenation of multiple SAMPLE_SET chunks — wrap in a
    # single root so ET can parse it.
    raw = xml_path.read_text()
    # Strip xml declarations that appear inside the concatenated body.
    cleaned = re.sub(r"<\?xml[^>]+\?>", "", raw)
    doc = f"<ROOT>{cleaned}</ROOT>"
    try:
        root = ET.fromstring(doc)
    except ET.ParseError as e:
        print(f"[map_metadata] samples.xml parse error: {e}", file=sys.stderr)
        return out
    for sample in root.iter("SAMPLE"):
        acc = sample.get("accession") or ""
        if not acc:
            for pid in sample.findall(".//PRIMARY_ID"):
                if (pid.text or "").strip().startswith(("SAMN", "SAMEA", "SAMD")):
                    acc = (pid.text or "").strip()
                    break
        if not acc:
            continue
        attrs: dict[str, str] = {}
        # <TITLE> at sample-level is also worth capturing.
        t = sample.find("TITLE")
        if t is not None and t.text:
            attrs["_title"] = t.text.strip()
        for sa in sample.findall(".//SAMPLE_ATTRIBUTE"):
            tag = (sa.findtext("TAG") or "").strip().lower().replace(" ", "_")
            val = (sa.findtext("VALUE") or "").strip()
            if tag and val:
                attrs[tag] = val
        out[acc] = attrs
    return out


# ------------------------------- main --------------------------------------

def process(acc_dir: Path) -> dict:
    samples_tsv = acc_dir / "samples.tsv"
    samples_xml = acc_dir / "samples.xml"
    if not samples_tsv.exists():
        raise FileNotFoundError(f"missing {samples_tsv} — run fetch_reads.py first")

    attr_by_sample = load_sample_attributes(samples_xml)

    with samples_tsv.open() as fh:
        rdr = csv.DictReader(fh, delimiter="\t")
        rows = list(rdr)

    out_rows: list[dict[str, str]] = []
    method_counts: Counter[str] = Counter()
    fine_counts: Counter[str] = Counter()
    coarse_counts: Counter[str] = Counter()
    unresolved_examples: list[str] = []

    for row in rows:
        group_raw = ""
        group_fine = ""
        group_coarse = ""
        confidence = "none"
        method = "none"

        # (1) sample_title
        st = row.get("sample_title", "")
        hit = extract_group(st)
        if hit:
            group_raw, group_fine, group_coarse = hit
            confidence = "high"
            method = "sample_title"

        # (2) experiment_title
        if not hit:
            et = row.get("experiment_title", "")
            hit = extract_group(et)
            if hit:
                group_raw, group_fine, group_coarse = hit
                confidence = "medium"
                method = "experiment_title"

        # (3) SAMPLE_ATTRIBUTES: check any candidate tag
        if not hit:
            attrs = attr_by_sample.get(row.get("sample_accession", ""), {})
            for tag in SAMPLE_ATTR_TAGS:
                v = attrs.get(tag)
                if v:
                    hit = extract_group(v)
                    if hit:
                        group_raw, group_fine, group_coarse = hit
                        confidence = "high"
                        method = f"sample_attribute:{tag}"
                        break

        # (4) sample_alias — cryptic; only very obvious matches
        if not hit:
            sa = row.get("sample_alias", "")
            hit = extract_group(sa)
            if hit:
                group_raw, group_fine, group_coarse = hit
                confidence = "low"
                method = "sample_alias"

        if not hit and st:
            unresolved_examples.append(st)

        method_counts[method] += 1
        if group_fine:
            fine_counts[group_fine] += 1
            coarse_counts[group_coarse] += 1

        out_rows.append(
            {
                "run_accession": row.get("run_accession", ""),
                "sample_accession": row.get("sample_accession", ""),
                "sample_alias": row.get("sample_alias", ""),
                "sample_title": st,
                "library_layout": row.get("library_layout", ""),
                "library_strategy": row.get("library_strategy", ""),
                "fastq_ftp": row.get("fastq_ftp", ""),
                "group_raw": group_raw,
                "group_fine": group_fine,
                "group_coarse": group_coarse,
                "confidence": confidence,
                "method": method,
            }
        )

    mapping_path = acc_dir / "mapping.tsv"
    with mapping_path.open("w", newline="") as fh:
        wr = csv.DictWriter(
            fh, fieldnames=list(out_rows[0].keys()), delimiter="\t"
        )
        wr.writeheader()
        wr.writerows(out_rows)

    n_runs = len(out_rows)
    n_mapped = sum(1 for r in out_rows if r["group_fine"])
    n_samples = len({r["sample_accession"] for r in out_rows if r["sample_accession"]})
    n_samples_mapped = len({
        r["sample_accession"] for r in out_rows
        if r["group_fine"] and r["sample_accession"]
    })

    summary = {
        "accession": acc_dir.name,
        "n_runs": n_runs,
        "n_runs_mapped": n_mapped,
        "n_samples": n_samples,
        "n_samples_mapped": n_samples_mapped,
        "groups_fine": dict(fine_counts),
        "groups_coarse": dict(coarse_counts),
        "methods_used": dict(method_counts),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mapper_version": "v1_keyword",
        "unresolved_examples": sorted(set(unresolved_examples))[:20],
    }

    marker = "linkage_ok" if n_mapped == n_runs and n_runs > 0 else "linkage_partial"
    (acc_dir / f"{marker}.json").write_text(json.dumps(summary, indent=2))

    # Housekeeping: if we just declared 'ok', remove any stale 'partial' marker.
    stale = acc_dir / (
        "linkage_partial.json" if marker == "linkage_ok" else "linkage_ok.json"
    )
    stale.unlink(missing_ok=True)

    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--accession", required=True, help="Project accession (e.g. PRJEB7774)."
    )
    ap.add_argument(
        "--reads-root",
        type=Path,
        default=Path("/scratch/karcher/seq_metadata_curator/data/reads"),
    )
    args = ap.parse_args()

    acc_dir = args.reads_root / args.accession
    if not acc_dir.exists():
        print(f"[map_metadata] no such directory: {acc_dir}", file=sys.stderr)
        return 2

    summary = process(acc_dir)
    marker = "linkage_ok" if summary["n_runs_mapped"] == summary["n_runs"] else "linkage_partial"

    print(
        f"[map_metadata] {args.accession}: "
        f"{summary['n_runs_mapped']}/{summary['n_runs']} runs mapped, "
        f"{summary['n_samples_mapped']}/{summary['n_samples']} samples mapped → "
        f"{marker}.json",
        file=sys.stderr,
    )
    print(f"  fine: {summary['groups_fine']}", file=sys.stderr)
    print(f"  coarse: {summary['groups_coarse']}", file=sys.stderr)
    print(f"  methods: {summary['methods_used']}", file=sys.stderr)
    if summary["unresolved_examples"]:
        print(f"  unresolved examples: {summary['unresolved_examples'][:5]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
