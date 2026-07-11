# Deep-dive: Supplementary-material HTML mining — Batch D

Investigation date: 2026-07-11
Investigator: SUPP-HTML-MINING wave-1 delivery agent
Log: `/scratch/karcher/seq_metadata_curator/logs/refresh_supp_via_html_2026-07-11-11-*.log`

## Summary

- Rows scanned : **220** (`supp_available != True` with either `pmc_id` or `doi`)
- Rows rescued : **70**
- Files downloaded : **70** (each row got exactly one Europe PMC OA supp bundle)
- Total data pulled : **58.0 MB** (avg ~830 KB / row)
- Supp coverage : **56.9 %** → **70.6 %** (+13.7 pp; 290 → 360 out of 510)
- Residual supp=False : **138** rows (from 220 down to 138)
- **Commit + push**: see final report line for hash.

## Method

Mirrors the reads-HTML-mining pattern (Batch B `probe_reads_from_article_html`). New helpers in `scripts/probe_coverage.py`:

- `probe_supp_from_article_html(session, doi, pmc_id)` — fetches article HTML via the shared `_fetch_article_html()` helper, then extracts every candidate supp URL matching a curated pattern set:
  - Nature/Springer ESM CDN (`static-content.springer.com/…/MediaObjects/…`)
  - Cell Press mmc (`www.cell.com/cms/…/attachment/…/mmc*.ext`)
  - PMC per-article `bin/` URLs (`/articles/instance/N/bin/…`)
  - **Europe PMC OA supp bundle (`www.ebi.ac.uk/europepmc/webservices/rest/PMC…/supplementaryFiles`)** — canonically synthesized from the pmc_id, not just when present in HTML
  - Zenodo (`zenodo.org/record/…`)
  - Figshare (`ndownloader.figshare.com/files/…` and article pages)
  - OSF (`osf.io/xxxxx`)
  - Wolters Kluwer LWW supp (`links.lww.com/…/…`)
  - Wiley `downloadSupplement` links
  - SAGE `doi/suppl` links
  - OUP supplementary-data links

- `_verify_supp_url()` — streams the first 64 bytes of each candidate, magic-checks against the accept-set (`%PDF`, `PK` zip, `\x1f\x8b` gzip, `BZh`, `Rar!`, legacy MS Office, RTF, UTF-8 BOM plain text) and reject-set (`<htm`, `<!DO`, `<?xm`, JSON error blobs). Any HTML masquerading as supp gets rejected.

- `SUPP_DOWNLOADABLE_TYPES` frozenset marks which patterns are downloadable from cluster IP. `pmc_bin`, `cell_mmc`, `wiley_download_supp`, `sage_supp` are DETECTED (audit-only) but NOT verified — they're all Cloudflare or POW gated. Future browser-driven rescue can pick these up.

New driver `scripts/refresh_supp_via_html.py`:

1. Iterate rows where `supp_available != True` and we have `pmc_id` OR `doi`.
2. Call `probe_supp_from_article_html()` → set of candidate URLs.
3. For each URL in `SUPP_DOWNLOADABLE_TYPES`, `_verify_supp_url()` and — on success — stream-download to `data/papers/PMID_<pmid>/supp/<safe_name>`, re-magic-checking the first chunk.
4. Skip already-present files (existing publisher supp downloads are respected).
5. Update `supp_available=True`, `supp_source=html_mining` (or existing tag + `+html_mining` if a publisher tag was already present — none in this pass).
6. Recompute `gap_score`; preserve `verdict / action / user_notes`; sort; write TSV as the LAST step.

Every rescued row has `verdict / action / user_notes` preserved (all 70 rescued rows had empty user_notes; sanity-checked).

## What actually rescued the 70 rows

Every one of the 70 rescues came from the **Europe PMC OA supp bundle endpoint**:

    https://www.ebi.ac.uk/europepmc/webservices/rest/PMC{N}/supplementaryFiles

Which returns a magic-verified `application/zip` for OA-subset articles (bundles every supp media file the PMC record indexes). The endpoint 404s for author-manuscript PMC entries (NIHMS) and non-PMC rows, so those pass through unmodified.

Every other pattern in the probe (Springer ESM, Cell mmc, Zenodo, Figshare, LWW, OSF, OUP) fired 0 times across the 220 no-supp rows because:
- Nature/Springer rows are already covered by the publisher plugins → not in the supp=NO cohort at all.
- Cell / Elsevier / Wiley / T&F rows fail at the `_fetch_article_html` step: those publisher sites are Cloudflare-gated for the cluster IP; PMC has no full text for the DOIs; the doi resolver → publisher landing chain returns 403.
- Third-party data-repo links (Zenodo, Figshare, OSF, Dryad) simply don't appear in the fetched HTML for these papers.

## Rescue distribution

By DOI prefix:

| Prefix | Rescued | Journal-family |
|---|---|---|
| 10.3390 | 14 | MDPI (Cancers, Microorganisms, Genes, …) |
| 10.3389 | 9 | Frontiers (Front Cell Infect Microbiol, Front Microbiol, …) |
| 10.1128 | 8 | ASM (mSystems, mBio, Microbiol Spectr, …) |
| 10.1097 | 4 | Wolters Kluwer (Ann Surg, Chin Med J, …) |
| 10.1002 | 4 | Wiley OA subset (Cancer Med, BJUI Compass, …) |
| 10.1155 | 4 | Hindawi/Wiley OA |
| 10.1186 | 3 | BioMed Central (Springer OA) |
| 10.2147 | 3 | Dove Press |
| 10.1136 | 3 | BMJ Open (OA subset) |
| 10.18632 | 3 | Impact Journals (Oncotarget) |
| 10.3748 | 2 | World J Gastroenterol (Baishideng) |
| 12 others | 1 each | long tail |

Note the clean pattern: **all rescues are from OA-only publishers whose PMC-OA hosting includes indexed supp files**. For MDPI / Frontiers / ASM etc. this endpoint reliably yields the supp zip.

## Residual — 138 rows still supp=False

Composition of the 138 supp=False residual after this pass:

| DOI prefix | N | Bucket / rescue path |
|---|---|---|
| 10.1016 | 38 | Elsevier / Cell Press — Cloudflare-gated; local Playwright pass needed |
| 10.1007 | 11 | Springer non-Nature — supp exists on `static-content.springer.com` but the plugin probe already checked; probably ARE False supp (no supp attached) |
| 10.1002 | 10 | Wiley non-OA — Cloudflare-gated; local Playwright |
| 10.1080 | 9 | Taylor & Francis — Cloudflare-gated; local Playwright |
| 10.1053 | 8 | Elsevier Gastro — same as 10.1016 |
| 10.1093 | 7 | OUP — the OUP supp URL pattern didn't fire; likely because the OUP DOI resolver redirects to `academic.oup.com` which is behind bot-mitigation from cluster; local Playwright would help |
| 10.1177 | 5 | SAGE — Cloudflare-gated |
| 10.1158 | 4 | AACR — Cloudflare-gated |
| 10.1136 | 4 | BMJ — plugin already covers OA; residual likely genuine "no supp" |
| Others | 42 | Long tail: 10.1097 (Wolters Kluwer non-OA), 10.1126 (Science non-OA), 10.1159 (Karger), 10.1101 (bioRxiv preprint), … |

Of the 138 residuals:
- **46** have a `pmc_id` — they're on PMC as author-manuscripts / hybrid, but Europe PMC's OA endpoint 404s them (not in OA-subset) and PMC's direct `bin/` supp download is POW-gated. **Fixable ONLY via a browser session that solves the PMC proof-of-work challenge**, or via local-agent Playwright on the publisher page.
- **92** have no `pmc_id` at all — supp only accessible from the publisher landing page, which for the majority is Cloudflare-gated.

## Key patterns / findings for future work

### 1. Europe PMC OA supp bundle is a free, permissive endpoint that should be probed FIRST, not last

Currently the pipeline only claims supp from PMC-OA via `hasSuppl=Y`, but doesn't actually download a bundle. The Europe PMC bundle endpoint IS a real, IP-permissive URL returning a magic-verifiable ZIP. It fired for 70/220 supp=NONE rows in this pass — I'd expect similar or higher hit rates against a broader corpus. **Recommendation: promote this endpoint from "html-mining backup" to a first-class check in `probe_coverage.py:probe_pmc_supp_verified()` or as a new `probe_pmc_oa_supp_bundle()` step.**

### 2. PMC's per-file `bin/` URL is POW-gated — 93 detections were unactionable

The PMC "new viewer" serves a JS-solving proof-of-work interstitial (`window.ncbi.pmc.pow.init(POW_CHALLENGE, POW_DIFFICULTY=4, …)`) before releasing supp downloads. Author-manuscript articles use this path and were the main reason 46 of the residual supp=False rows still have `supp_available=False` despite the article-page HTML listing supp files.

Two mitigation paths (out of scope for this batch):
- **Playwright with POW-solver**: the challenge is a 4-difficulty preimage — solvable in the browser within a few seconds. Local-agent Playwright already handles Cloudflare `cf_clearance`, so this would slot in cleanly.
- **NCBI's official OAI-PMH interface or FTP fulltext download** — I did NOT investigate whether these expose supp material; probably worth a look.

### 3. `_fetch_article_html` has a stealth-fail-mode: the reCAPTCHA stub

For some author-manuscript PMC articles, `pmc.ncbi.nlm.nih.gov/articles/PMCxxx/` returns a ~21 KB Google reCAPTCHA challenge page (title `<title>Recaptcha</title>`, body redirects to `google.com/recaptcha/challengepage/`) instead of article HTML. The current 8192-byte length gate in `_fetch_article_html` waves this through, so downstream probes get a garbage document. I did NOT change `_fetch_article_html` for this pass (sibling agent territory — READS-DEEPER may extend it). Recommend adding a follow-up patch that rejects HTMLs containing `google.com/recaptcha/challengepage` OR `ncbi.pmc.pow.init` OR title matching `Recaptcha|Preparing to download`. Currently these masquerade as full HTML — they just don't contain any URL patterns, so the probe silently returns 0 candidates for those rows. Low priority since the residual is still small.

### 4. Publisher-plugin gaps identified

Journals where a plugin would push supp coverage further, **assuming the publisher's supp URL host is reachable from cluster**:

- **MDPI** (10.3390) — supp lives at `mdpi.com/xxxx/y/z/s1` links; the MDPI main site is generally reachable from cluster. A dedicated MDPI plugin would rescue the ~15 MDPI residual rows that aren't in Europe PMC's OA-subset.
- **Frontiers** (10.3389) — supp at `frontiersin.org/api/…/supplementary`; also reachable.
- **ASM** (10.1128) — mSystems / mBio; supp at `journals.asm.org/doi/suppl/…`.
- **Hindawi** (10.1155, now under Wiley/Hindawi PLoS Open) — supp at `downloads.hindawi.com`.

For each of these, a simple plugin (`probe_reachable` + `probe_supp` + `fetch_supp`) modeled on `publishers/nature.py` would be < 100 lines and would knock another 20-40 rows off the residual.

## `_fetch_article_html` extension marker (READS-DEEPER coordination note)

Per brief, I did NOT edit `_fetch_article_html()` (Batch B territory). I only ADDED new callers and helpers. The existing signature and return contract are preserved. My new probe function calls `_fetch_article_html(session, doi, pmc_id)` unchanged — if READS-DEEPER extends it to reject reCAPTCHA stubs (per finding #3 above), the supp-mining probe automatically benefits without any changes on my side.

## Coverage numbers (post this batch)

| Metric | Before | After | Delta |
|---|---|---|---|
| PDF accessible | 88.2 % | 88.2 % | 0 (untouched) |
| **Supp accessible** | **56.9 %** | **70.6 %** | **+13.7 pp** |
| INSDC reads | ~ | 32.9 % | (this run also read the reads column but did not modify) |

Notes:
- **Reads coverage jumped from the docs' 27.1% → 32.9%** between the brief being written and this run — someone (probably READS-DEEPER doing sibling work in parallel) landed additional reads rescues. That's fine; my code only reads the reads column for the coverage summary, never modifies it.
- Pre-run supp count of 290/510 = 56.9 % matches the brief's "Supp 56.9%". Post-run 360/510 = 70.6 %.

## Files added / modified

- Modified `scripts/probe_coverage.py` — added `probe_supp_from_article_html()`, `_verify_supp_url()`, `_is_probable_text_supp()`, `_SUPP_URL_PATTERNS`, `_SUPP_MAGIC_BYTES`, `_SUPP_REJECT_MAGIC`, `SUPP_DOWNLOADABLE_TYPES`. Did NOT modify `_fetch_article_html` or `probe_reads_from_article_html` (sibling territory).
- Added `scripts/refresh_supp_via_html.py` — driver (structural mirror of `refresh_reads_via_html.py`).
- Updated `data/coverage_review.tsv` — 70 rows flipped from `supp_available=False → True`, `supp_source=NONE → html_mining`. `gap_score` recomputed. Row order re-sorted (gap desc, journal, pmid). All human-notes columns preserved.
- Added `data/papers/PMID_<pmid>/supp/europepmc_supp.zip` for each of the 70 rescued rows.
- `.gitignore` already excludes `data/papers/` (large binaries stay out of git).
