# Pipeline state as of 2026-07-10

Live coverage snapshot from the most recent refresh (probe over 510 of the 1128 CRC-microbiome search hits; the other 618 were never probed because I killed the initial probe run early). Reference for the local agent so it knows what's already handled vs. what still needs work.

## Coverage on the 510-row corpus

| Metric | Value | Residual |
|---|---|---|
| **PDF accessible** (any source) | **451 / 510 = 88.4 %** | 59 |
| **Supplementary accessible** | **362 / 510 = 71.0 %** | 148 |
| **INSDC reads accessible** | 115 / 510 = 22.5 % | 395 |
| gap_score = 0 (all three green) | 102 | — |
| gap_score = 3 (all three missing) | 57 | — |

### PDF sources actually hit (multiple can co-fire per row)

| Source | Rows | Notes |
|---|---|---|
| PMC-OA tarball | 344 | main workhorse; 85 % of PMC-tracked papers are OA |
| Unpaywall | 432 | broadest but softest (may point at green-OA landing pages) |
| Publisher plugin: nature (10.1038) | ~55 | |
| Publisher plugin: nature_legacy (dotted 10.1038) | 1 | |
| Publisher plugin: springer (10.1007) | ~13 | |
| Publisher plugin: bmj (10.1136) | 1 | |
| **Publisher plugins missing** | — | Elsevier, Wiley, Taylor & Francis, ACS, Karger, LWW, RSC, etc. |

### Supp sources actually hit

| Source | Rows | Notes |
|---|---|---|
| PMC-OA tarball | 344 | includes supp when article is OA |
| publisher:nature (ESM CDN) | 11 in Feng-2015 alone; corpus total ~15 | Springer / Nature-legacy share this CDN |
| publisher:springer | small | many Springer articles genuinely lack supp |

## Residual PDF-NONE (59 rows) — where the gap lives

| Bucket | Count | Cause | Rescueable how? |
|---|---|---|---|
| **Elsevier (10.1016 + 10.1053)** | **27** | Cloudflare `cf-mitigated: challenge` on cluster IP | Fetch from **local** machine (EMBL-network IP is on Elsevier whitelist) |
| **Wiley (10.1002 + 10.1111)** | **6** | Same Cloudflare block; PDF viewer is JS-loaded | Local machine + real browser (Playwright) |
| **Taylor & Francis (10.1080)** | **3** | Same Cloudflare block; one paper truly paywalled | Local machine |
| BMJ paywalled | 3 | genuine paywall (Unpaywall says `closed`) | not rescueable |
| Chinese regional (10.12122 / 10.19723 / 10.3969) | 3 | requires Chinese-institution login | not rescueable |
| Long-tail commercial (ACS, Karger, LWW, Liebert, SAGE, …) | 15 | paywalled or captcha | mostly not rescueable |
| No DOI / broken DOI | 2 | metadata gap | not rescueable |
| **Total local-machine-rescueable** | **~36** | | |

## Verified from-cluster failure mode

`curl -A "$UA" https://www.sciencedirect.com/science/article/pii/S1075996417300550?via%3Dihub`
- **From compute node**: `HTTP 403`, `server: cloudflare`, `cf-mitigated: challenge`
- **From login1**: same 403 (checked 2026-07-10)
- **From user's browser**: fetches article fine (institutional whitelist)
- **With uploaded browser cookies from cluster**: still 403 — Cloudflare `cf_clearance` is IP-bound; cookies don't carry across networks

**Conclusion for the local agent**: the Cloudflare-gated residuals must be fetched from an EMBL-institutional-network IP. That's a laptop on EMBL WiFi, or on EMBL VPN, not the cluster.

## Publisher plugins status

Implemented (all with `probe_reachable` + `fetch_pdf` + `fetch_supp`):
- `publishers/nature.py` — DOI prefix `10.1038` (any Nature-family journal)
- `publishers/nature_legacy.py` — `10.1038` dotted-suffix legacy DOIs (e.g. `10.1038/onc.2017.314`)
- `publishers/springer.py` — DOI prefix `10.1007` (Springer non-Nature; PDF endpoint works even for subscription-only articles)
- `publishers/bmj.py` — DOI prefix `10.1136` (BMJ HighWire; %PDF magic sniff distinguishes OA from paywall HTML)

Not yet implemented and hard-blocked without institutional access:
- Elsevier (`10.1016`, `10.1053`) — ScienceDirect Cloudflare
- Wiley (`10.1002`, `10.1111`) — OnlineLibrary Cloudflare
- Taylor & Francis (`10.1080`) — tandfonline Cloudflare
- Everything else in the tail

## Interlocks the local agent MUST respect

1. **No fastq download until `linkage_ok.json` exists** — `fetch_reads.py --download-fastq` refuses. Marker is written by `map_metadata.py` only when every run has a case/control label.
2. **All PDFs must pass `%PDF` magic sniff** — silently accepting HTML paywall pages as PDF is the #1 integrity risk. Every fetch path (Nature/Springer/BMJ/Europe PMC/Unpaywall/tarball) already enforces this.
3. **Reads land only as `.fastq.gz`** — `fetch_reads.py` rejects non-gz URLs and asserts gzip magic post-download.
4. **User notes preserved on re-run** — `make_review_table.py` and the refresh scripts carry forward `verdict / action / user_notes` in `coverage_review.tsv` keyed by PMID.

## Bug-hunt cycle (rounds 1–8) — CONVERGED

Eight rounds of parallel read-only bug-hunting (3 hunters/round) on the PDF+supp code path yielded the following:

| Round | Hunter concurrence | Fixes shipped |
|---|---|---|
| 1 | 3× hunters (A/B/C) | 7 fixes |
| 2 | 3× hunters (D/E/F) | 9 fixes |
| 3 | 3× hunters (G/H/I) | 8 fixes |
| 4 | 3× hunters (J/K/L) | 10 fixes |
| 5 | 3× hunters (M/N/O) | 6 fixes |
| 6 | 3× hunters (P/Q/R) | 4 fixes |
| 7 | 3× hunters (S/T/U) | 1 fix (S-1: `.part` exclusion in newly_added) |
| 8 | 3× hunters (V/W/X) | 1 fix (V-1: same `.part` exclusion needed in R5-2 fallback path) |

All prior findings verified present on-disk. Round 8: **V/W/X = 1L + 0 + 0**. Round-7 W explicitly recommended ending; round-8 W/X concurred. Cycle converged after round 8.

A comprehensive commit-by-commit change log is on the git branch (each round is a single commit with a detailed message). All fixes are in `main` at https://github.com/karchern/seq_metadata_curator.

## Doc-rot: coverage numbers

The 88.4%/71.0%/22.5% figures above are the PRE-bug-hunt-cycle snapshot from 2026-07-10. Most of the R1-R8 fixes improve *correctness* rather than *headline coverage* — the biggest change since is that supp coverage is now hasSuppl-verified rather than blanket-True on PMC-OA (so the 71.0% is likely to REDUCE toward a more honest number on the next full refresh). A fresh refresh_pdf_supp.py run will produce the post-fix numbers.

Round-8 hunter X did an end-to-end dry-run of `refresh_pdf_supp.main()` on a `/tmp` copy of the 510-row TSV under all-transient-probe conditions and confirmed rows+columns+values are preserved.
