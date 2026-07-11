#!/usr/bin/env python3
"""
Coverage probe: for every PMID in the search-result set, report whether
we can access (a) the paper's PDF, (b) supplementary information, and
(c) associated INSDC reads.

Probing only — no PDF downloads, no fastq pulls. We hit oa.fcgi / article
URLs / Unpaywall / ENA APIs enough to know a fetch WOULD work.

Output:
    data/coverage_report.tsv     — one row per PMID (streaming, crash-safe)
    data/coverage_summary.json   — aggregate counts at end
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import requests
from Bio import Entrez

# Publisher plugins
sys.path.insert(0, str(Path(__file__).resolve().parent))
from publishers import get_publisher  # noqa: E402

DEFAULT_EMAIL = "karchernic@gmail.com"
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

INSDC_ACC_RE = re.compile(
    # BioProject variants (INSDC): PRJ{DB}{AB}\d+ = PRJEB, PRJNA, PRJEA, PRJDA, PRJDB
    # Study accessions: ERP, SRP, DRP
    # Biosample IDs: SAMN (SRA), SAMEA (ENA), SAMD (DDBJ)
    # GEO series: GSE
    # DDBJ Sequence Read Archive: DRA (added R-Batch-B per Nature deep-dive; would have
    #   caught PMID 31171880's 645 runs)
    # ArrayExpress: E-MTAB, E-GEOD, E-MEXP, E-PROT, E-ERAD (also added R-Batch-B —
    #   Nature papers frequently deposit gene-expression alongside INSDC reads)
    r"\b("
    r"PRJ[END][AB]\d+"
    r"|ERP\d+|SRP\d+|DRP\d+"
    r"|SAMN\d+|SAMEA\d+|SAMD\d+"
    r"|GSE\d+"
    r"|DRA\d+"
    r"|E-(?:MTAB|GEOD|MEXP|PROT|ERAD)-\d+"
    r")\b"
)
INSDC_PROJECT_RE = re.compile(
    r"^(PRJ[END][AB]|ERP|SRP|DRP|DRA)\d+$", re.IGNORECASE
)


@dataclass
class Row:
    pmid: str
    journal: str = ""
    doi: str = ""
    pmc_id: str = ""
    doi_prefix: str = ""
    pdf_sources: str = ""            # e.g. "pmc_oa,nature,unpaywall"
    supp_source: str = "NONE"        # first-hit source name
    supp_available: bool = False
    reads_accessions: str = ""
    reads_source: str = "NONE"       # europepmc | elink | abstract_regex | NONE
    n_runs: int = 0
    total_gb: float = 0.0
    note: str = ""


# --------------------------- HTTP session -----------------------------------

def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": BROWSER_UA, "From": DEFAULT_EMAIL})
    return s


def http_get(session: requests.Session, url: str, timeout: int = 30) -> Optional[requests.Response]:
    for _ in range(2):
        try:
            r = session.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code in (429, 500, 502, 503, 504):
                try:
                    r.close()
                except Exception:
                    pass
                time.sleep(1.5)
                continue
            return r
        except (requests.ConnectionError, requests.Timeout):
            time.sleep(1.0)
    return None


def http_head(session: requests.Session, url: str, timeout: int = 20) -> Optional[requests.Response]:
    try:
        r = session.head(url, timeout=timeout, allow_redirects=True)
        # Some hosts don't like HEAD; fall back to GET without streaming body
        if r.status_code >= 400:
            r = session.get(url, timeout=timeout, allow_redirects=True, stream=True)
            r.close()
        return r
    except (requests.ConnectionError, requests.Timeout):
        return None


# --------------------------- Metadata batch ---------------------------------

def batch_metadata(pmids: list[str], email: str) -> dict[str, dict]:
    """Batch efetch → dict of pmid → {title, journal, doi, pmc_id, abstract}."""
    Entrez.email = email
    out: dict[str, dict] = {}
    CHUNK = 200
    for i in range(0, len(pmids), CHUNK):
        chunk = pmids[i : i + CHUNK]
        with Entrez.efetch(db="pubmed", id=",".join(chunk), retmode="xml") as h:
            xml_bytes = h.read()
        root = ET.fromstring(xml_bytes)
        for art in root.findall(".//PubmedArticle"):
            pmid_el = art.find(".//PMID")
            if pmid_el is None or not pmid_el.text:
                continue
            pmid = pmid_el.text.strip()
            title = ""
            journal = ""
            doi = ""
            pmc_id = ""
            abstract = ""
            t = art.find(".//ArticleTitle")
            if t is not None:
                title = "".join(t.itertext()).strip()
            j = art.find(".//Journal")
            if j is not None:
                iso = j.find("ISOAbbreviation")
                if iso is None:
                    iso = j.find("Title")
                if iso is not None and iso.text:
                    journal = iso.text.strip()
            # Scope to the article's OWN ArticleIdList; `.//ArticleId` also
            # matches ArticleId elements inside <ReferenceList><Reference>
            # and would silently pick up a reference's PMC/DOI when the
            # article itself lacks that ID type.
            for eid in art.findall("./PubmedData/ArticleIdList/ArticleId"):
                idtype = (eid.get("IdType") or "").lower()
                val = (eid.text or "").strip()
                if idtype == "doi" and not doi:
                    doi = val
                elif idtype == "pmc" and not pmc_id:
                    pmc_id = val if val.startswith("PMC") else f"PMC{val}"
            # Abstract
            abstract_parts = []
            for a in art.findall(".//Abstract/AbstractText"):
                abstract_parts.append("".join(a.itertext()).strip())
            abstract = " ".join(p for p in abstract_parts if p)
            out[pmid] = {
                "title": title, "journal": journal, "doi": doi,
                "pmc_id": pmc_id, "abstract": abstract,
            }
        time.sleep(0.4)
    return out


# --------------------------- Individual probes ------------------------------

def probe_pmc_id_fallback(session: requests.Session, pmid: str) -> Optional[str]:
    try:
        with Entrez.esearch(db="pmc", term=f"{pmid}[pmid]", retmax=1) as h:
            r = Entrez.read(h)
        ids = list(r.get("IdList", []))
        if ids:
            return f"PMC{ids[0]}"
    except Exception:
        pass
    return None


def probe_pmc_oa(session: requests.Session, pmc_id: str) -> bool:
    # NOTE the host: pmc.ncbi.nlm.nih.gov returns 404 for this endpoint.
    # The working host is www.ncbi.nlm.nih.gov with /pmc/ in the path.
    url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmc_id}"
    r = http_get(session, url, timeout=25)
    if r is None:
        # http_get exhausted retries; treat as transient failure so callers
        # (esp. refresh_pdf_supp.py's regression guard) can distinguish
        # "definitely not OA" from "network flap during refresh".
        raise RuntimeError(f"probe_pmc_oa: HTTP exhausted retries for {pmc_id}")
    if r.status_code != 200:
        return False
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return False
    if root.find(".//error") is not None:
        return False
    rec = root.find(".//record")
    if rec is None:
        return False
    for link in rec.findall("link"):
        if link.get("format") == "tgz":
            return True
    return False


def probe_publisher(session: requests.Session, doi: str) -> Optional[str]:
    """Return publisher.name iff its own probe_reachable() says yes.

    Each publisher module knows its own PDF endpoint; base.Publisher.probe_reachable
    peeks it + magic-byte sniffs. Adding a new publisher (Elsevier proxy, ASM, …)
    doesn't require touching this function.

    Does NOT swallow exceptions: network failures raise so refresh_pdf_supp's
    regression guard can distinguish transient from definitive.
    """
    pub = get_publisher(doi)
    if pub is None:
        return None
    return pub.name if pub.probe_reachable(session, doi) else None


def probe_publisher_supp(session: requests.Session, doi: str) -> tuple[bool, int]:
    """Delegate to the matching publisher's probe_supp() method.

    Each publisher knows its own URL construction (e.g. Springer might
    resolve at /article/ OR /chapter/, BMJ scans DOI landing HTML for
    supp candidates). Keeping this thin means adding a new publisher
    doesn't require touching this function.

    Deliberately does NOT swallow exceptions — callers (e.g.
    refresh_pdf_supp.py) rely on network-failure exceptions cascading
    through so their regression guard can distinguish "definitely no
    supp" from "transient probe failure".
    """
    pub = get_publisher(doi)
    if pub is None:
        return (False, 0)
    return pub.probe_supp(session, doi)


def probe_pmc_supp_verified(session: requests.Session, pmc_id: str) -> bool:
    """True iff Europe PMC's `hasSuppl` field confirms this PMC article has
    supplementary material. Prevents the false-positive where refresh
    scripts blanket-claim supp for any PMC-OA hit even when the tarball
    contains only the PDF + JATS XML.

    Cost: one Europe PMC REST search per PMC ID. Roughly 200 ms.
    """
    url = (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        f"?query=PMCID:{pmc_id}&format=json&resultType=lite"
    )
    r = http_get(session, url, timeout=20)
    if r is None or r.status_code != 200:
        raise RuntimeError(f"probe_pmc_supp_verified: HTTP fail for {pmc_id}")
    try:
        j = r.json()
    except Exception as e:
        raise RuntimeError(f"probe_pmc_supp_verified: bad JSON for {pmc_id}: {e}")
    hits = j.get("resultList", {}).get("result", [])
    if not hits:
        return False
    return (hits[0].get("hasSuppl") or "").upper() == "Y"


def probe_unpaywall(session: requests.Session, doi: str) -> bool:
    """True iff Unpaywall claims to know a directly-fetchable PDF for this DOI.

    Only counts `url_for_pdf` (or a `url` ending in .pdf) as evidence — a
    bare `url` field usually points at a landing page that will yield HTML
    rather than a real PDF, so accepting it here would inflate probe
    numbers vs. what fetch_paper.py's downstream %PDF-magic gate actually
    accepts. Prefer a small false-negative over a fetch-mismatched
    false-positive.

    Raises RuntimeError on network exhaustion so caller's regression guard
    can distinguish transient from definitive.
    """
    url = f"https://api.unpaywall.org/v2/{doi}?email={DEFAULT_EMAIL}"
    r = http_get(session, url, timeout=20)
    if r is None:
        raise RuntimeError(f"probe_unpaywall: HTTP exhausted retries for {doi}")
    if r.status_code != 200:
        return False
    try:
        j = r.json()
    except Exception:
        return False

    def _has_pdf(loc: dict) -> bool:
        if loc.get("url_for_pdf"):
            return True
        u = (loc.get("url") or "").lower().split("?", 1)[0]
        return u.endswith(".pdf")

    if _has_pdf(j.get("best_oa_location") or {}):
        return True
    for loc in (j.get("oa_locations") or []):
        if _has_pdf(loc):
            return True
    return False


def probe_europepmc_datalinks(session: requests.Session, pmid: str) -> list[str]:
    """Return INSDC project accessions discovered via Europe PMC's datalinks."""
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/MED/{pmid}/datalinks?format=json"
    r = http_get(session, url, timeout=20)
    if r is None or r.status_code != 200:
        return []
    try:
        j = r.json()
    except Exception:
        return []
    out: list[str] = []
    for cat in j.get("dataLinkList", {}).get("Category", []):
        for sec in cat.get("Section", []):
            for link in sec.get("Linklist", {}).get("Link", []):
                target = link.get("Target", {}).get("Identifier", {})
                i = str(target.get("ID", ""))
                if INSDC_PROJECT_RE.match(i):
                    if i not in out:
                        out.append(i)
    return out


def probe_ncbi_elink_bioproject(pmid: str) -> list[str]:
    # Throttle: NCBI's 3-req/s per-IP limit is shared across parallel
    # subagents on the same node — be conservative.
    time.sleep(0.35)
    try:
        with Entrez.elink(dbfrom="pubmed", id=pmid, db="bioproject") as h:
            recs = Entrez.read(h)
        uids: list[str] = []
        for grp in recs:
            for lset in grp.get("LinkSetDb", []):
                for link in lset.get("Link", []):
                    uids.append(link["Id"])
        if not uids:
            return []
        with Entrez.esummary(db="bioproject", id=",".join(uids)) as h:
            summary = Entrez.read(h)
        accs: list[str] = []
        for doc in summary.get("DocumentSummarySet", {}).get("DocumentSummary", []):
            acc = doc.get("Project_Acc")
            if acc and acc not in accs:
                accs.append(acc)
        return accs
    except Exception:
        return []


def probe_abstract_regex(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for m in INSDC_ACC_RE.finditer(text):
        v = m.group(1)
        # Keep only project-level accessions here (we probe those against ENA).
        if INSDC_PROJECT_RE.match(v) and v not in out:
            out.append(v)
    return out


def _fetch_article_html(
    session: requests.Session, doi: str, pmc_id: str
) -> Optional[str]:
    """Best-effort fetch of the article's full-text HTML.

    Tries, in order:
      1. PMC article page (works for all OA-in-PMC + author-manuscript records)
      2. Publisher plugin's article URL if a plugin exists for the DOI:
         - Nature/nature_legacy: nature.com/articles/{slug}
         - Springer: /article/{doi} then /chapter/{doi}
         - BMJ: DOI resolution
      3. Give up (Elsevier/Wiley/T&F are Cloudflare-gated from cluster IP)

    Returns HTML text or None. This is a READ-ONLY probe helper — no
    downloads to disk, no side effects.
    """
    # 1. PMC page — cheap, works for a lot of OA articles
    if pmc_id:
        pmc_num = pmc_id.replace("PMC", "")
        r = http_get(
            session,
            f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{pmc_num}/",
            timeout=30,
        )
        if r is not None and r.status_code == 200 and len(r.text) > 8192:
            return r.text

    # 2. Publisher plugin
    if doi:
        pub = get_publisher(doi)
        if pub is not None:
            urls_to_try: list[str] = []
            if pub.name in ("nature", "nature_legacy"):
                slug = pub.article_slug(doi)
                urls_to_try.append(f"https://www.nature.com/articles/{slug}")
            elif pub.name == "springer":
                urls_to_try.append(f"https://link.springer.com/article/{doi}")
                urls_to_try.append(f"https://link.springer.com/chapter/{doi}")
            elif pub.name == "bmj":
                urls_to_try.append(f"https://doi.org/{doi}")

            for u in urls_to_try:
                r = http_get(session, u, timeout=30)
                if r is not None and r.status_code == 200 and len(r.text) > 8192:
                    return r.text
    return None


def probe_reads_from_article_html(
    session: requests.Session, doi: str, pmc_id: str
) -> list[str]:
    """Deep-mine an article's full-text HTML for INSDC accessions the shallow
    europepmc + elink + abstract probes missed.

    Per the Nature-family deep-dive (2026-07-10): 9 of 16 no-reads Nature
    papers have their INSDC accession(s) clearly in the article HTML but
    absent from Europe PMC's `accessionTypeList`. Recovery via this path
    lifted Nature reads coverage from 41% → 74% in that sample. The
    broadened INSDC regex above (DDBJ DRA + ArrayExpress E-MTAB etc.) is
    what makes DDBJ-heavy papers like PMID 31171880 (645 runs) rescueable.

    Returns a deduplicated list of project-level INSDC accessions found
    in the article HTML. Caller should validate each against ENA
    filereport before trusting them.
    """
    html = _fetch_article_html(session, doi, pmc_id)
    if not html:
        return []
    return probe_abstract_regex(html)


def probe_ena_filereport(session: requests.Session, acc: str) -> tuple[int, float]:
    """Return (n_runs, total_gb) for one project accession via ENA."""
    url = (
        "https://www.ebi.ac.uk/ena/portal/api/filereport"
        f"?accession={acc}&result=read_run&fields=run_accession,fastq_bytes&format=tsv"
    )
    r = http_get(session, url, timeout=30)
    if r is None or r.status_code != 200:
        return (0, 0.0)
    text = r.text.strip()
    lines = text.splitlines()
    if len(lines) < 2:
        return (0, 0.0)
    n = 0
    total = 0
    for ln in lines[1:]:
        cells = ln.split("\t")
        if not cells or not cells[0]:
            continue
        n += 1
        if len(cells) >= 2:
            for b in cells[1].split(";"):
                b = b.strip()
                if b.isdigit():
                    total += int(b)
    return (n, total / (1024 ** 3))


# --------------------------- Per-PMID probe ---------------------------------

def probe_pmid(pmid: str, meta: dict, session: requests.Session) -> Row:
    row = Row(
        pmid=pmid,
        journal=meta.get("journal", ""),
        doi=meta.get("doi", ""),
        pmc_id=meta.get("pmc_id", ""),
    )
    if row.doi and "/" in row.doi:
        row.doi_prefix = row.doi.split("/", 1)[0]

    if not row.pmc_id:
        pmc = probe_pmc_id_fallback(session, pmid)
        if pmc:
            row.pmc_id = pmc

    pdf_sources: list[str] = []
    if row.pmc_id:
        try:
            if probe_pmc_oa(session, row.pmc_id):
                pdf_sources.append("pmc_oa")
        except Exception:
            pass  # transient probe failure; row stays without pmc_oa this pass
    if row.doi:
        try:
            pub_name = probe_publisher(session, row.doi)
            if pub_name:
                pdf_sources.append(pub_name)
        except Exception:
            pass
    if row.doi:
        try:
            if probe_unpaywall(session, row.doi):
                pdf_sources.append("unpaywall")
        except Exception:
            pass
    row.pdf_sources = ",".join(pdf_sources) if pdf_sources else "NONE"

    # supp: verify PMC-OA supp claim via Europe PMC hasSuppl (previously
    # a blanket True on any PMC-OA hit — inflated coverage stats vs. what
    # fetch_paper.py could actually retrieve, per R3 finding I-C2 / H-4).
    if "pmc_oa" in pdf_sources and row.pmc_id:
        try:
            if probe_pmc_supp_verified(session, row.pmc_id):
                row.supp_source = "pmc_oa"
                row.supp_available = True
        except Exception:
            pass
    if not row.supp_available and row.doi and get_publisher(row.doi):
        try:
            ok, _n = probe_publisher_supp(session, row.doi)
        except Exception:
            ok = False
        if ok:
            row.supp_source = f"publisher:{get_publisher(row.doi).name}"
            row.supp_available = True

    # reads — order chosen from PART1 subagent's empirical finding:
    # europepmc datalinks did ~99% of the read-linkage work while NCBI
    # elink and abstract regex added <1% between them. Gate the slower
    # sources behind europepmc==NONE for ~4× throughput.
    accs: list[str] = []
    src = "NONE"
    epmc = probe_europepmc_datalinks(session, pmid)
    if epmc:
        accs.extend(epmc)
        src = "europepmc"
    else:
        ncbi = probe_ncbi_elink_bioproject(pmid)
        for a in ncbi:
            if a not in accs:
                accs.append(a)
        if ncbi:
            src = "elink"
        else:
            regex_hits = probe_abstract_regex(meta.get("abstract", ""))
            for a in regex_hits:
                if a not in accs:
                    accs.append(a)
            if regex_hits:
                src = "abstract_regex"

    # Confirm each project accession is real + has reads on ENA
    kept: list[str] = []
    total_runs = 0
    total_gb = 0.0
    for acc in accs:
        n, gb = probe_ena_filereport(session, acc)
        if n > 0:
            kept.append(acc)
            total_runs += n
            total_gb += gb
    row.reads_accessions = ",".join(kept) if kept else "NONE"
    row.reads_source = src if kept else "NONE"
    row.n_runs = total_runs
    row.total_gb = round(total_gb, 2)
    return row


# --------------------------- main -------------------------------------------

def run(pmids: list[str], out_tsv: Path, out_summary: Path, email: str) -> None:
    session = new_session()
    print(f"[probe] fetching batch metadata for {len(pmids)} PMIDs ...", file=sys.stderr)
    meta = batch_metadata(pmids, email)
    print(f"[probe] metadata retrieved for {len(meta)} of {len(pmids)}", file=sys.stderr)

    out_tsv.parent.mkdir(parents=True, exist_ok=True)
    fields = list(asdict(Row(pmid="")).keys())
    fh = out_tsv.open("w", newline="")
    wr = csv.DictWriter(fh, fieldnames=fields, delimiter="\t")
    wr.writeheader()
    fh.flush()

    counters: dict[str, Counter] = {
        "pdf_any": Counter(), "pdf_by_source": Counter(),
        "supp_any": Counter(), "supp_by_source": Counter(),
        "reads_any": Counter(), "reads_by_source": Counter(),
        "journal": Counter(), "doi_prefix": Counter(),
    }
    total_runs = 0
    total_gb = 0.0

    for i, pmid in enumerate(pmids):
        m = meta.get(pmid, {})
        try:
            row = probe_pmid(pmid, m, session)
        except Exception as e:
            row = Row(pmid=pmid, note=f"exception:{type(e).__name__}:{e}")

        wr.writerow(asdict(row))
        fh.flush()

        counters["journal"][row.journal or "?"] += 1
        counters["doi_prefix"][row.doi_prefix or "?"] += 1
        counters["pdf_any"]["yes" if row.pdf_sources != "NONE" else "no"] += 1
        for s in row.pdf_sources.split(",") if row.pdf_sources != "NONE" else []:
            counters["pdf_by_source"][s] += 1
        counters["supp_any"]["yes" if row.supp_available else "no"] += 1
        counters["supp_by_source"][row.supp_source] += 1
        counters["reads_any"]["yes" if row.n_runs > 0 else "no"] += 1
        counters["reads_by_source"][row.reads_source] += 1
        total_runs += row.n_runs
        total_gb += row.total_gb

        if (i + 1) % 25 == 0 or i == len(pmids) - 1:
            print(
                f"[probe] {i+1}/{len(pmids)}  "
                f"pdf:{counters['pdf_any']['yes']} "
                f"supp:{counters['supp_any']['yes']} "
                f"reads:{counters['reads_any']['yes']} "
                f"(runs={total_runs} data={total_gb:.1f}GB)",
                file=sys.stderr,
            )

    fh.close()

    summary = {
        "n_pmids": len(pmids),
        "n_metadata": len(meta),
        "pdf_any": dict(counters["pdf_any"]),
        "pdf_by_source": dict(counters["pdf_by_source"]),
        "supp_any": dict(counters["supp_any"]),
        "supp_by_source": dict(counters["supp_by_source"]),
        "reads_any": dict(counters["reads_any"]),
        "reads_by_source": dict(counters["reads_by_source"]),
        "top_journals": dict(counters["journal"].most_common(20)),
        "top_doi_prefixes": dict(counters["doi_prefix"].most_common(15)),
        "total_runs_reachable": total_runs,
        "total_gb_reachable": round(total_gb, 2),
    }
    out_summary.write_text(json.dumps(summary, indent=2))
    print(f"[probe] wrote {out_tsv} and {out_summary}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pmid-file", type=Path, help="File with one PMID per line.")
    ap.add_argument(
        "--query",
        default=None,
        help="If --pmid-file is not given, run this PubMed query first "
        "(defaults to config/pubmed_query.txt).",
    )
    ap.add_argument("--retmax", type=int, default=1500)
    ap.add_argument(
        "--out-tsv",
        type=Path,
        default=Path("/scratch/karcher/seq_metadata_curator/data/coverage_report.tsv"),
    )
    ap.add_argument(
        "--out-summary",
        type=Path,
        default=Path("/scratch/karcher/seq_metadata_curator/data/coverage_summary.json"),
    )
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    args = ap.parse_args()

    Entrez.email = args.email
    if args.pmid_file:
        pmids = [ln.strip() for ln in args.pmid_file.read_text().splitlines() if ln.strip()]
    else:
        q = args.query
        if q is None:
            q = Path(
                "/scratch/karcher/seq_metadata_curator/config/pubmed_query.txt"
            ).read_text().strip()
        with Entrez.esearch(db="pubmed", term=q, retmax=args.retmax, sort="pub_date") as h:
            r = Entrez.read(h)
        pmids = list(r["IdList"])
        print(
            f"[probe] search returned Count={r.get('Count')}, retrieved {len(pmids)}",
            file=sys.stderr,
        )

    run(pmids, args.out_tsv, args.out_summary, args.email)
    return 0


if __name__ == "__main__":
    sys.exit(main())
