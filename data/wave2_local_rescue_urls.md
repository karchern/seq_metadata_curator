# Wave-2 laptop-side rescue — URL pattern cheat sheet

Per-publisher URL patterns the local Playwright runner consumes. Compiled
from the Cell family deep-dive (`data/deep_dive_cell_family.md`), the
Elsevier/Wiley/T&F wave-1 rescue evidence (`local_agent_scripts/README.md`,
`fetch_cloudflare_residuals.py`), and the supp HTML-mining deep-dive
(`data/deep_dive_supp_html_mining.md`).

Placeholder legend:
- `{DOI}` — full DOI, e.g. `10.1016/j.anaerobe.2017.03.012`
- `{PII}` — 17-char Elsevier article ID, e.g. `S1075996417300550`
- `{PII_DASHED}` — dashed PII form, e.g. `S1075-9964(17)30055-0`
- `{PMID}` — PubMed ID, used only in local-agent output paths
- `{JOURNAL_SLUG}` — cell.com per-journal slug (see below)
- `{uuid}` — per-file uuid; discoverable only by parsing the fulltext HTML
- `{N}`, `{ext}` — supp file number + extension

## 1. Elsevier ScienceDirect (`10.1016`, non-Cell-Press)

### PII discovery

CrossRef exposes the PII via the `resource.primary.URL` field.

```
GET https://api.crossref.org/works/{DOI}
=> message.resource.primary.URL == "https://linkinghub.elsevier.com/retrieve/pii/{PII}"
```

### Article landing (fulltext HTML)

```
https://www.sciencedirect.com/science/article/pii/{PII}
```

- Cloudflare-gated from cluster IP. Local agent primed profile clears the
  challenge (interactive on first Elsevier hit of the session; auto after).
- Retry with `?via%3Dihub` if the bare URL 404s (`/abs/` lands on abstract-only).

### PDF endpoint

Signed pdfft link inside the fulltext HTML DOM:
```
a[href*='pdfft'] where href contains 'md5='
```

The signed URL has this shape:
```
https://www.sciencedirect.com/science/article/pii/{PII}/pdfft?md5={md5}&pid=1-s2.0-{PII}-main.pdf
```

Unsigned `.../pdfft` endpoints return a separate Cloudflare challenge page.
Direct construction WITHOUT the md5 will fail.

### Supp endpoint

```
https://ars.els-cdn.com/content/image/1-s2.0-{PII}-mmc{N}.{ext}
```

- Filenames + `N` are discoverable only by parsing the fulltext HTML (JS-
  rendered supp panel; look for `a[href*='ars.els-cdn.com']` or
  `a[href*='mmc']`).
- Not always Cloudflare-gated on `ars.els-cdn.com`, but Referer must be the
  parent PII URL and the cf_clearance cookie helps.

## 2. Cell Press family (`10.1016/j.{cell-brand}.`, hosted on cell.com)

Cell Press journals share the Elsevier DOI prefix but live on `www.cell.com`
(or `www.cmghjournal.org` for CMGH). The DOI-suffix codes we recognize:

| Code | Journal | Slug (cell.com/{slug}/…) |
|---|---|---|
| `cell` | Cell | `cell` |
| `ccell` | Cancer Cell | `cancer-cell` |
| `chom` | Cell Host & Microbe | `cell-host-microbe` |
| `cmet` | Cell Metabolism | `cell-metabolism` |
| `celrep` | Cell Reports | `cell-reports` |
| `xcrm` | Cell Reports Medicine | `cell-reports-medicine` |
| `xgen` | Cell Genomics | `cell-genomics` |
| `stem` | Cell Stem Cell | `cell-stem-cell` |
| `molcel` | Molecular Cell | `molecular-cell` |
| `immuni` | Immunity | `immunity` |
| `cub` | Current Biology | `current-biology` |
| `jcmgh` | Cell Mol Gastroenterol Hepatol | `cellmolgastro` (redirects → cmghjournal.org) |
| `devcel` | Developmental Cell | `developmental-cell` |
| `neuron` | Neuron | `neuron` |
| `med` | Med | `med` |
| `chembiol` | Cell Chemical Biology | `cell-chemical-biology` |
| `xinn` | The Innovation | `the-innovation` |

### PII discovery

Same as Elsevier ScienceDirect: `CrossRef -> resource.primary.URL -> /pii/{PII}`.

### Article landing (fulltext HTML)

```
https://www.cell.com/{JOURNAL_SLUG}/fulltext/{PII}
```

- CMGH: `https://www.cmghjournal.org/article/{PII_DASHED}/fulltext`
- Cloudflare gating is *stochastic* — sometimes 200 first try, sometimes 403
  every attempt for minutes. Local browser session tolerates this better
  than cluster requests.

### PDF endpoint

Two options, tried in this order:

1. DOM lookup — parse fulltext HTML for `a[href*='showPdf']`:
   ```
   https://www.cell.com/action/showPdf?pii={PII_DASHED}
   ```
   The dashed PII form is required here (compact PII 404s).
2. Fallback:
   ```
   https://www.cell.com/{JOURNAL_SLUG}/pdfExtended/{PII}
   ```
   Works ~15-30% of the time from cluster; higher from EMBL institutional network.

Not-recommended: `.../pdf/{PII}.pdf` — triggers 30-redirect loop.

### Supp endpoint

```
https://www.cell.com/cms/{DOI}/attachment/{uuid}/mmc{N}.{ext}
```

- Enumeration only from parsing fulltext HTML — the `uuid` is per-file and
  isn't derivable.
- Regex: `/cms/[^"'\s<>]+/attachment/[^"'\s<>]+/mmc\d+\.[A-Za-z0-9]+`
- The `scripts/publishers/cell_press.py` plugin already writes discovered
  URLs to `data/papers/PMID_<pmid>/supp/manifest_pending_playwright.tsv`
  when it fetches an HTML successfully. The wave-2 runner reads this
  manifest first before re-fetching the HTML.

## 3. Elsevier Gastroenterology (`10.1053`)

Same infrastructure as ScienceDirect. `Gastroenterology` articles live at
`www.sciencedirect.com/science/article/pii/{PII}` (or occasionally at
`www.gastrojournal.org`, which redirects).

- PII discovery: CrossRef → `resource.primary.URL`.
- PDF + supp endpoints: identical to Elsevier ScienceDirect above.

## 4. Wiley (`10.1002`, `10.1111`)

### Article landing

```
https://onlinelibrary.wiley.com/doi/{DOI}
```

- Cloudflare-gated but generally auto-clears (no interactive challenge in
  wave-1 evidence). Warm profile helps.

### PDF endpoint

```
https://onlinelibrary.wiley.com/doi/pdfdirect/{DOI}
```

- Alternative: `/doi/pdf/{DOI}` — sometimes bounces to viewer HTML.
- `page.expect_download()` catches the file when `always_open_pdf_externally`
  is on in the Chrome profile prefs (`ensure_pdf_download_prefs`).

### Supp endpoint

```
https://onlinelibrary.wiley.com/action/downloadSupplement?doi={DOI}&file={file}
```

- Enumerable only from article HTML. Regex:
  `/action/downloadSupplement\?doi=[^"'\s<>]+&(?:amp;)?file=[^"'\s<>]+`
- Look for the "Supporting Information" section:
  `section.article-section__supporting-information a[href*='downloadSupplement']`
- Filename comes from the `&file=` argument (URL-decoded).

## 5. Taylor & Francis (`10.1080`)

### Article landing

```
https://www.tandfonline.com/doi/full/{DOI}
```

- Cloudflare gating typically auto-clears from a warm profile; some journals
  (esp. non-gold-OA) require a subscription that EMBL may or may not hold.
  When only paywall renders, expect `no_download` or a paywall HTML.

### PDF endpoint

```
https://www.tandfonline.com/doi/pdf/{DOI}?download=true
```

- Also try `.../doi/epdf/{DOI}` if the download endpoint returns viewer HTML.

### Supp endpoint

```
https://www.tandfonline.com/doi/suppl/{DOI}/suppl_file/{file}
```

- Enumerable from article HTML. Regex:
  `/doi/suppl/[^"'\s<>]+/suppl_file/[^"'\s<>]+`
- Filename is the last path segment.

## 6. Reads-mining from Cloudflare-gated fulltext HTML

Papers where cluster-side reads-mining came up empty because the article
HTML was CF-blocked. Laptop-side Playwright reaches the same HTML.

### Broadened INSDC accession regex

Mirrors `scripts/probe_coverage.py:INSDC_ACC_RE` (post-Batch-B):

```
\b(
  PRJ[END][AB]\d+               # BioProject INSDC
  | ERP\d+ | SRP\d+ | DRP\d+    # Study accessions
  | DRA\d+                       # DDBJ SRA
  | E-(?:MTAB|GEOD|MEXP|PROT|ERAD)-\d+  # ArrayExpress
  | GSE\d+                       # GEO series (best-effort)
)\b
```

The runner scopes the regex to a window around any of the following
markers to keep signal high:
- `data availability` / `availability of data`
- `accession number(s)`
- `deposited at` / `biosample` / `bioproject`
- `sequencing data` / `sequence read archive`

If none match, the whole document is scanned (fallback).

### ENA verification

Each candidate accession is HEAD-verified against ENA's filereport API:

```
GET https://www.ebi.ac.uk/ena/portal/api/filereport?
    accession={ACC}&result=read_run&fields=run_accession,fastq_bytes&format=tsv
```

- Zero rows in the response → accession discarded (probably referenced
  externally, not deposited by this paper).
- ≥1 row → `n_runs` is the row count, `total_gb` is the sum of `fastq_bytes`
  values (in gigabytes, 1 GB = 2**30 bytes).

GSE / E-MTAB are recorded to `wave2_local_reads_rescues.tsv` for the parent
cluster pipeline to resolve to SRA / ArrayExpress later — the runner does
not attempt ENA lookup for those (they'll come back with 0 runs).

## 7. Cloudflare mitigation strategies observed on cluster (reference)

These do NOT apply to the local agent (which uses a real Chrome browser);
listed here as record of what the cluster tried and why it failed:

- `time.sleep(1.5)` on 429/5xx — doesn't help; 403-CF is persistent.
- Session cookies + Referer header — CF fingerprints JA3 (TLS), not cookies.
- Rotating User-Agent — no observed effect.
- Warm session (visit publisher root before article) — occasionally shifts
  50% success to 55%; not reliable enough to depend on.
- `requests` with any header combination — always 403-CF after N attempts.

The local Playwright runner sidesteps all of this by using a real Chrome
process on the EMBL institutional network. The primed profile at
`~/.seq_metadata_curator_chrome_profile` already carries an issued
`cf_clearance` from the wave-1 pass; the runner refreshes it as needed.

## 8. Browser preference and cookie priming (recap)

- **Profile path**: `~/.seq_metadata_curator_chrome_profile` (persistent).
- **PDF prefs**: `plugins.always_open_pdf_externally=true` in the profile's
  `Default/Preferences` JSON. `ensure_pdf_download_prefs()` in the runner
  writes it if missing (idempotent).
- **First Elsevier hit** requires user to click through the Cloudflare
  challenge (auto-detected via `wait_for_challenge_clear(interactive=True)`;
  300 s poll). Subsequent Elsevier hits usually auto-clear in the same
  session.
- **Wiley / T&F** typically auto-clear without user interaction.
- **cell.com** is stochastic — often auto-clears; occasionally needs a
  manual click.
