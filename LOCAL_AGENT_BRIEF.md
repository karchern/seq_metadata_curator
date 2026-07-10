# Local-agent brief for the seq_metadata_curator pipeline

**Read this first, then [STATE.md](STATE.md) for current numbers and known gaps.**

## Your mission

The parent (cluster) pipeline curates public sequencing data + linked metadata for a CRC-microbiome corpus. It has hit a hard wall: **~36 residual PDFs are gated by Cloudflare bot-mitigation on the cluster IP** (Elsevier/Wiley/T&F). Fetching them requires an EMBL-institutional-network IP — i.e. this laptop. That is the primary reason you exist.

You may also run the whole pipeline end-to-end locally if the user asks (the code is identical); the paragraphs below assume the Cloudflare-rescue task first, then generalize.

## Setup

```bash
# 1. Clone
git clone git@github.com:karchern/seq_metadata_curator.git ~/seq_metadata_curator
cd ~/seq_metadata_curator

# 2. Python 3.9+. Create a venv if you don't have biopython/requests/bs4:
python3 -m venv .venv && source .venv/bin/activate
pip install biopython requests beautifulsoup4

# 3. For Cloudflare-gated publishers only, install Playwright (~90 MB):
pip install playwright
python -m playwright install chromium
```

Then confirm you are actually on EMBL's institutional network — a laptop-side sanity check:
```bash
UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
curl -sSL -o /dev/null -w '%{http_code}\n' -A "$UA" \
    'https://www.sciencedirect.com/science/article/pii/S1075996417300550?via%3Dihub'
```
- Expect `200`  → you're whitelisted; proceed with plain `requests`.
- If `403 cf-mitigated: challenge` → Playwright headless-browser fetch is required (still works from your IP).

## Where everything lives

Compared to the cluster (`/scratch/karcher/seq_metadata_curator/…`), you use a local mirror:

| Path role | Cluster | Local (recommended) |
|---|---|---|
| Repo checkout | `/scratch/karcher/seq_metadata_curator` | `~/seq_metadata_curator` |
| Runtime data | `.../data/` | `~/seq_metadata_curator_data/` |
| Small state (TSVs, JSON, MD) | `.../data/coverage_review.tsv`, etc. | mirror the layout, but keep in the same repo checkout under `data_local/` so git can carry small changes back |

The cluster scripts hardcode `/scratch/karcher/seq_metadata_curator/data/...` as default paths. Locally you MUST override with `--out-root` / `--out-tsv` / `--reads-root` flags on every invocation, or the scripts will try to write into a nonexistent `/scratch/`. Do NOT edit the scripts to change defaults.

## Primary task — rescue the Cloudflare-gated PDFs

Fetch the (~36) papers the cluster couldn't reach.

### Step 1 — pull the residual list from the cluster
```bash
# get the current coverage_review.tsv
git pull  # if the cluster has pushed newer coverage_review.tsv
# OR scp:
scp karcher@login1.cluster.embl.de:/scratch/karcher/seq_metadata_curator/data/coverage_review.tsv \
    ~/seq_metadata_curator/data_local/coverage_review.tsv
mkdir -p ~/seq_metadata_curator/data_local/papers ~/seq_metadata_curator/data_local/logs
```

### Step 2 — extract the target PMIDs
```python
# scripts/select_cloudflare_residuals.py — you WRITE this small utility.
# Read data_local/coverage_review.tsv, keep rows where:
#   pdf_sources == "NONE"
#   AND doi_prefix in {10.1016, 10.1053, 10.1002, 10.1111, 10.1080}
# Emit PMIDs to data_local/pmids_cloudflare_residuals.txt.
```
(Keep the utility out of the main scripts/ dir — put it in `local_agent_scripts/` so cluster runs never trip on it.)

### Step 3 — fetch each one and write into the normal papers/ layout

The existing `scripts/fetch_paper.py` orchestrator ALREADY dispatches to publisher modules and falls back to Unpaywall — it doesn't have Elsevier/Wiley/T&F plugins yet.

Two approaches, in order of preference:

**Approach A — extend `publishers/` with proper plugins.** File-per-publisher:
- `publishers/elsevier.py` (DOI prefix `10.1016` + `10.1053`; article URL `sciencedirect.com/science/article/pii/{PII}`; PII discoverable via CrossRef API `https://api.crossref.org/works/{DOI}` → `resource.primary.URL`; PDF endpoint is `.../pdfft?isDNSHijacked=true`)
- `publishers/wiley.py` (DOI prefix `10.1002` + `10.1111`; article URL `onlinelibrary.wiley.com/doi/{DOI}`; PDF endpoint `.../pdfdirect/{DOI}` or `.../doi/pdfdirect/{DOI}`)
- `publishers/taylor_francis.py` (DOI prefix `10.1080`; article URL `tandfonline.com/doi/full/{DOI}`; PDF endpoint `.../doi/pdf/{DOI}?download=true`)

Each subclass `Publisher` from `publishers/base.py`. Implement `probe_reachable` (peek + `%PDF` sniff), `fetch_pdf` (download + `%PDF` sniff — mandatory), `fetch_supp` (scrape article HTML for supp URLs; conventions differ per publisher — investigate).

**Approach B — Playwright fallback wrapper.** If plain requests still 403 from your laptop (Cloudflare occasionally fingerprints even legit browsers), add a Playwright-driven fetch helper that uses a real Chromium instance. Skeleton:
```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(user_agent=UA)
    page = ctx.new_page()
    page.goto(article_url, wait_until="networkidle")
    # For a real PDF endpoint, capture the download event:
    with page.expect_download() as dl_info:
        page.click("a[href*='pdf']")
    dl_info.value.save_as(dest)
```
Use only when needed — plain requests is faster and more reproducible.

### Step 4 — verify
For each rescued PMID, confirm on-disk `paper.pdf` starts with `%PDF` magic bytes:
```bash
for pdf in ~/seq_metadata_curator/data_local/papers/PMID_*/paper.pdf; do
    head -c 4 "$pdf" | grep -q '%PDF' && echo "OK  $pdf" || echo "BAD $pdf"
done
```
If any file lands as HTML (paywall), delete it — do NOT keep half-fetches; the cluster's `fetch_paper.py` refuses to trust bad PDFs.

### Step 5 — ship results back
Two channels:

- **Small, versioned:** commit new publisher modules + any code fixes to git, `git push`. The cluster will `git pull` and re-run the coverage refresh.
- **Large binaries (PDFs + supp):** rsync to the cluster's `data/papers/`. Do NOT rely on git for these.
  ```bash
  rsync -av --progress ~/seq_metadata_curator/data_local/papers/ \
      karcher@login1.cluster.embl.de:/scratch/karcher/seq_metadata_curator/data/papers/
  ```

## Hard constraints — do not violate

1. **No fastq downloads without a `linkage_ok.json`.** `fetch_reads.py --download-fastq` refuses without it — do not add flags to bypass. That marker exists because case/control mapping must be verified before pulling potentially hundreds of GB of reads.
2. **Every PDF must pass `%PDF` magic sniff.** Every publisher plugin, Europe PMC, Unpaywall, and PMC-OA tarball extraction already enforces this on the cluster side. Do the same in any new plugin.
3. **Every fastq must pass gzip magic (`\x1f\x8b`) sniff and be a `.gz` URL.** Handled by `fetch_reads.py`; do not weaken.
4. **Preserve `verdict`/`action`/`user_notes` in `coverage_review.tsv`.** The cluster's `make_review_table.py` and `refresh_*.py` merge these by PMID. If you rewrite the file locally, use the same merge logic — do not overwrite blindly.
5. **Do NOT commit `config/publisher_cookies.txt` or any secret.** `.gitignore` already excludes it. Verify with `git status` before every push.
6. **Do NOT push code changes that hardcode local paths.** Cluster and local both share `scripts/`. Use CLI flags for path overrides.

## Communication back to the parent (cluster) agent

- Push code changes and `data/coverage_review.tsv` updates to git → cluster pulls.
- Push PDFs + supp via rsync to `data/papers/` on the cluster.
- If you make a decision the parent should know (e.g. "Wiley PDF endpoint requires a specific referrer header"), append to `STATE.md` under a new dated section and commit.
- If you find a bug in a shared script, describe it in a `data_local/bugs_from_local_agent.md` file, commit + push — the parent has an ongoing bug-hunt cycle and will fold it in.

## Task 0 — first thing to actually run

Read [STATE.md](STATE.md), then:
1. Sanity-check institutional access (the `curl` in the "Setup" section).
2. Pull the current `data/coverage_review.tsv` from cluster (or wait for it to be committed and `git pull`).
3. Ask the user: "Approach A (build proper Elsevier/Wiley/T&F plugins) or Approach B (Playwright fallback wrapper)?"
4. Once user picks, execute + verify + rsync results.

Report back to the user with: how many of the 36 residuals were rescued, which publisher modules you added, and any new bug/observation worth flagging.
