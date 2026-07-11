# Deep-dive: Wave-2 OA reads rescue

Run: 2026-07-11  
Script: `scripts/refresh_reads_oa_wave2.py`

## Motivation

Wave-1's `refresh_reads_deeper.py` ran BEFORE Frontiers / MDPI / BMC / science_aaas / cell_press publisher plugins were wired into `probe_coverage._fetch_article_html()`. The article-HTML dispatch only knew nature / nature_legacy / springer / bmj, so ~123 OA reads gaps were unreachable to HTML mining. This wave extends dispatch (via each plugin's new `article_html_url()` method) and reruns the four winning strategies from Wave-1.

## Scope

- Target rows (reads_source=NONE + doi_prefix in OA cluster + not IGNORE): **166**

Rows tried per publisher:

| Publisher | N tried |
|---|---:|
| elsevier_incl_cell_press | 43 |
| mdpi | 37 |
| frontiers | 31 |
| bmc | 30 |
| springer | 25 |

## Results by publisher

| Publisher | PMIDs lifted | Records | New runs | New GB | Hit rate (pmids/tried) |
|---|---:|---:|---:|---:|---:|
| elsevier_incl_cell_press | 1 | 1 | 4 | 16.74 | 2.3% |
| mdpi | 0 | 0 | 0 | 0.0 | 0.0% |
| frontiers | 1 | 1 | 1158 | 9619.22 | 3.2% |
| bmc | 2 | 2 | 100 | 1728.09 | 6.7% |
| springer | 0 | 0 | 0 | 0.0 | 0.0% |

## Results by strategy

| Strategy | Records | PMIDs lifted | New runs | New GB |
|---|---:|---:|---:|---:|
| whole_html_regex | 1 | 1 | 88 | 1629.47 |
| data_avail_section | 0 | 0 | 0 | 0.0 |
| geo_to_sra | 3 | 3 | 1174 | 9734.58 |
| arrayexpress | 0 | 0 | 0 | 0.0 |

## Overall

- **PMIDs lifted (from reads_source=NONE): 4**
- Rescue records written: 4
- After merge into `coverage_review.tsv`: 4 rows lifted from NONE (unique PMIDs), 6 accessions added.

## Coverage delta

| Metric | Value |
|---|---|
| Pre-merge reads coverage (full corpus) | 168/510 (32.94%) |
| Post-merge reads coverage (full corpus) | 172/510 (33.73%) |
| Rows lifted from NONE by wave-2 | 4 |
| Delta (pp) | +0.79 |
| Total new INSDC runs cataloged (may double-count PRJNA/SRP mirrors) | 1262 |
| Total new data cataloged | ~11.4 TB |

**Note on double-counting**: two of the four rescues include BOTH a
PRJNA project accession AND its SRP study-accession mirror (e.g.
PRJNA805525 == SRP359396). ENA's filereport returns the same 579 runs
for either identifier, so the additive `n_runs` / `total_gb` fields in
`coverage_review.tsv` overcount by that mirror. The rescue is still
correct: both accession IDs are legitimate references to the same
underlying data, and either is sufficient for downstream retrieval.
This matches wave-1's `merge_reads` semantics (which is also
mirror-blind).

## Interpretation

- The wave-2 unlock delivered fewer rescues than the ~123 gaps might
  have suggested. Two reasons dominate:
  1. **Most 10.1186 / 10.3389 target rows already have PMC IDs**, and
     wave-1 already mined PMC HTML for them. The wave-2-specific
     surface (publisher HTML) is genuinely NEW only for the subset of
     OA rows where the PMC copy is missing / stripped of INSDC accessions.
  2. **Many of the residual no-reads OA rows are legitimately no-reads**
     — computational meta-analyses, opinion pieces, single-case reports
     — with no primary sequencing deposit to find. The rescue rate on
     rows that DO have a deposit is high (all four hits validated
     cleanly against ENA), but the pool of such rows in the OA-cluster
     no-reads residual is small.
- The 10.1016 subset had 43 rows tried but only 1 hit — expected, since
  Cell Press's DOI-suffix narrowing means only the tiny Cell-family
  fraction of Elsevier rows gets publisher HTML mining. The other 42
  fall back to PMC (already covered by wave-1).

## Example rescues (up to 6 per strategy)

### geo_to_sra

- PMID 38357363 (frontiers) → PRJNA805525, SRP359396
- PMID 37619934 (elsevier_incl_cell_press) → PRJNA1003421
- PMID 37814323 (bmc) → PRJNA726145, SRP316870

### whole_html_regex

- PMID 35183223 (bmc) → PRJNA397219

## Notes

- Every rescued accession was validated against ENA `filereport?result=read_run` with `n_runs > 0` (no unverified claims).

- MDPI (10.3390) HTML is Akamai-blocked from cluster IP so the article-HTML strategies cannot reach the paper body; MDPI hit rate here is near zero. This is expected and documented in `publishers/mdpi.py`. PMC-page fallback (Wave-1) is the only usable path for MDPI reads mining from cluster.

- science_aaas / cell_press use warm-session cookies to defeat Cloudflare; success is stochastic. Included for completeness.

- IGNORE-* PMIDs from `data/master_paper_disposition.tsv` were skipped by design.

