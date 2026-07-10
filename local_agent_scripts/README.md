# local_agent_scripts/

Laptop-side helpers to rescue the Cloudflare-gated PDFs (Elsevier / Wiley /
Taylor & Francis) that the cluster pipeline can't reach because Cloudflare
JS-challenges every request from the EMBL cluster IP range. **These scripts
must run from a machine on the EMBL institutional network** (on-site or VPN);
they cannot run on the cluster.

## Why local-only?

- Cloudflare on `sciencedirect.com`, `onlinelibrary.wiley.com`, and
  `tandfonline.com` hard-blocks the cluster IPs with `HTTP 403 · cf-mitigated:
  challenge`. The cluster has no way around that.
- Even from EMBL WiFi, plain `curl` / `requests` still gets 403 — Cloudflare
  requires JavaScript execution.
- A **real Chrome browser** on EMBL WiFi does clear the challenge (sometimes
  self-solving, sometimes needing one click). We drive that Chrome from Python
  via [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) (a
  stealth-patched Playwright fork).

## What each script does

| Script | Purpose |
|---|---|
| `select_residuals.py` | Filter cluster's `coverage_review.tsv` → PMIDs with `pdf_sources=NONE` AND a Cloudflare-gated DOI prefix (10.1016, 10.1053, 10.1002, 10.1111, 10.1080). |
| `prime_profile.py` | One-time interactive: opens headed Chrome at a dedicated profile, visits one article per publisher, user solves any Cloudflare challenge. Cookies (`cf_clearance`, `__cf_bm`) persist to disk. |
| `fetch_cloudflare_residuals.py` | Batch fetch: iterates a PMID TSV, launches a single headed Chrome (offscreen), navigates to each article page, discovers the signed PDF URL from the DOM, and downloads via `page.expect_download()`. Auto-configures the profile to force PDFs to download (not open inline in the built-in viewer). |
| `diag_pdf_endpoints.py` | Diagnostic that captures per-publisher HTML samples + selector counts. Only needed when adapting the fetcher to a new publisher. |
| `playwright_probe.py`, `patchright_probe.py`, `playwright_probe_with_cookies.py`, `continuous_probe.py`, `export_publisher_cookies.py` | Investigation artifacts kept for reference — trace how we arrived at the current fetcher. Not needed for routine runs. |

## Setup (one-time)

```bash
git clone git@github.com:karchern/seq_metadata_curator.git ~/seq_metadata_curator
cd ~/seq_metadata_curator
python3 -m venv .venv && source .venv/bin/activate
pip install biopython requests beautifulsoup4 patchright
python -m patchright install chromium
```

Verify institutional access (from an EMBL-network laptop):
```bash
curl -sSL -o /dev/null -w '%{http_code}\n' -A 'Mozilla/5.0' \
    https://www.sciencedirect.com/science/article/pii/S1075996417300550
# Expected: 403 with cf-mitigated: challenge — plain curl is always blocked;
# patchright with real Chrome is what actually works.
```

## Routine run

### 1. Pull the residual list from the cluster

```bash
scp karcher@login1.cluster.embl.de:/scratch/karcher/seq_metadata_curator/data/coverage_review.tsv \
    ~/seq_metadata_curator/data_local/coverage_review.tsv

python local_agent_scripts/select_residuals.py \
    --in-tsv  data_local/coverage_review.tsv \
    --out-tsv data_local/pmids_cloudflare_residuals.tsv
```

### 2. Fetch

```bash
python local_agent_scripts/fetch_cloudflare_residuals.py \
    --in-tsv  data_local/pmids_cloudflare_residuals.tsv \
    --out-root data_local/papers \
    --report   data_local/fetch_report.tsv
```

What happens:
- A Chrome window opens, positioned off-screen (`--window-position=-2400,-2400`).
- The script primes cf_clearance on sciencedirect. If Cloudflare shows an
  interactive challenge, drag the Chrome window on-screen from its Dock icon
  and click through — the script auto-detects the clear (poll up to 300 s).
- The script then iterates over each residual PMID. Every Elsevier PMID
  triggers a per-navigation Cloudflare challenge on sciencedirect (this is
  Elsevier's Cloudflare config, not fixable script-side); Wiley and T&F pages
  auto-clear.
- Each successful download lands in `data_local/papers/PMID_{pmid}/paper.pdf`
  with a sibling `metadata.json`. `%PDF` magic bytes are enforced.

### 3. Push results back to the cluster

Small (versioned): commit any code fixes and updated `data_local/fetch_report.tsv`
to a branch; open a PR or push to `main` per your workflow.

Large (PDFs): `rsync` them into the cluster's `data/papers/` layout.
```bash
rsync -av --progress data_local/papers/ \
    karcher@login1.cluster.embl.de:/scratch/karcher/seq_metadata_curator/data/papers/
```

Then, on the cluster, re-run `refresh_pdf_supp.py` to update `coverage_review.tsv`
with the newly-present PDFs.

## Known limits

- **Elsevier challenges the article page for every PMID.** Cloudflare on
  sciencedirect binds cf_clearance to per-connection state that dies at
  navigation; there is no known way to solve this once-per-session from a
  scripted browser.
- **Taylor & Francis `/doi/pdf/…` requires subscription auth** we don't have
  — even when the article page renders. The 3 T&F residuals will always MISS
  as `no_download:Timeout` until someone with a T&F subscription runs the
  fetcher.
- **Some Elsevier journals hide the download link from non-subscribers.**
  Those show `MISS no_pdf_link_on_page`. Best guess: EMBL doesn't hold a
  subscription for that journal, or it's a "Purchase PDF"-only article.
- **Retryable subset:** any URL that ends up at `/abs/pii/…` (abstract-only
  landing) instead of `/pii/…` fails with `no_pdf_link_on_page`. A future
  iteration should retry those with `/abs/` stripped from the article URL.
