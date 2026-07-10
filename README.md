# seq_metadata_curator

Agentic pipeline that, given a PubMed query, discovers papers → fetches PDF + supplementary materials → finds INSDC (ENA/SRA/DDBJ) read accessions → maps runs to case/control labels → and (only when linkage succeeds) downloads the raw fastqs.

## Why local execution matters

Several major publishers (Elsevier / Wiley / Taylor & Francis) sit behind Cloudflare bot-mitigation that hard-blocks the EMBL cluster IP range with `HTTP 403 · cf-mitigated: challenge`. Access is IP-based via EMBL's institutional whitelist, and only user-facing browser networks (on-site + VPN) satisfy that.

Running these scripts from a **user's local machine** — where the browser is already logged in / IP-whitelisted — removes that barrier entirely, at the cost of relying on the user's laptop for the paper-fetch pass.

Suggested split when this is deployed at scale:

| Phase | Runs where | Why |
|---|---|---|
| PubMed search → PMID list | anywhere | trivial network |
| Paper + supp fetch | **local** | needs institutional-network IP |
| ENA metadata fetch | anywhere | fully public |
| Reads (fastq) download | **cluster** | terabyte-scale I/O; only after linkage_ok.json |
| Case/control mapping | anywhere | reads TSV/XML only |

## Layout

```
scripts/
  pubmed_search.py          PubMed query → PMIDs
  fetch_paper.py            PMID → PDF + supp (multi-source: PMC-OA →
                            Europe PMC → publisher plugin → Unpaywall)
  fetch_reads.py            accession/PMID → ENA study + samples metadata,
                            gated fastq download
  map_metadata.py           v1 keyword mapper → mapping.tsv + linkage_ok.json
  probe_coverage.py         corpus-level coverage probe (no downloads)
  refresh_pdf_supp.py       cheap in-place re-probe of PDF/supp columns
  refresh_pmc_oa.py         PMC-OA-only re-check
  make_review_table.py      aggregate parts → coverage_review.tsv
                            (preserves human notes on re-run)
  publishers/
    __init__.py  base.py    plugin registry + base class
    nature.py               10.1038 (Nature family — all subjournals)
    nature_legacy.py        10.1038 dotted-suffix DOIs
    springer.py             10.1007 (Springer non-Nature)
    bmj.py                  10.1136 (BMJ HighWire)
config/
  pubmed_query.txt          canonical CRC-microbiome search string
  publisher_cookies.txt     (gitignored — publisher session cookies)
data/
  coverage_review.tsv       reviewable table (small, VERSIONED)
  papers/, reads/, ...      (gitignored — large + regenerable)
```

## Env

Cluster: `/g/typas/Personal_Folders/Nic/miniforge3/envs/pyhmmer/bin/python`
(biopython 1.85, requests 2.32, bs4 4.x; NO lxml — stdlib xml.etree used).

Local: any Python 3.9+ with `biopython`, `requests`, `beautifulsoup4`
(plus `playwright` for Cloudflare-gated publishers).

## Interlocks

- `fetch_reads.py --download-fastq` REFUSES unless `linkage_ok.json` exists in the accession dir. That marker is written only when `map_metadata.py` labels every run.
- Every downloaded fastq is magic-byte-checked to confirm gzip.
- Publisher `fetch_pdf` methods now sniff `%PDF` magic to reject HTML paywalls served under a `.pdf` URL.
