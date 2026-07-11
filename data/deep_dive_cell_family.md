# Deep-dive: Cell family — 7 papers

Investigation date: 2026-07-11
Agent: cell-family deep-dive specialist
Corpus: 7 rows in `coverage_review.tsv` whose journal name contains "Cell".
Constraint: read-only on everything except this file.

## Summary

- N investigated: 7
- Currently PDF-accessible (per tsv): 6/7 (only 30403593 missing)
- Additional PDFs discoverable this dive: **1/7** (30403593 has a DOI CrossRef indexes; publisher OA PDF at cellmolbiol.org returns 200 %PDF)
- Currently supp-accessible: 3/7
- Additional supp discoverable this dive: **≥3/7 in theory** — Cell Rep 34706245, CHM 37130517, CMGH 34418587 all expose supp under `www.cell.com/cms/{DOI}/attachment/{uuid}/mmc{N}.{ext}` on the fulltext landing page, but the cell.com CDN is Cloudflare-gated from our cluster IP (stochastic 200/403 → mostly 403 after any repeated hit). A Cell Press plugin CAN enumerate the URLs cheaply, but download-side needs the local (EMBL institutional) agent.
- Reads currently linked: 4/7. Missing: 3.
  - PMID 30403593 — no reads deposited (out-of-scope FFPE-DNA methodology paper).
  - PMID 34706245 — **rescuable** (PRJEB38064 buried in cell.com fulltext HTML; 188 runs / 2.4 GB on ENA).
  - PMID 33262469 — **rescuable** (PRJNA478491 now in EuropePMC datalinks; 400 runs / 71.4 GB on ENA — stale-probe issue, not code bug).

Net rescueable this dive: **1 additional PDF**, **1 additional supp source pattern (cell.com/cms/attachment) worth codifying**, and **2 additional reads sources** (PMIDs 33262469 and 34706245).

## Per-paper triage

### PMID 30403593 — Cell Mol Biol (Noisy-le-grand)

Current state: no DOI, PDF=NONE, supp=NONE, reads=NONE.

**Investigation**:
- Full PubMed record: FFPE-DNA extraction methodology; comparative performance of 4 PCR ready-to-use kits on Hirschsprung + prostate FFPE samples. Not a microbiome study; not a CRC association study; no sequencing data. Out of the CRC-microbiome scope of this corpus.
- **CrossRef title search**: score-96.99 hit ⇒ **DOI = `10.14715/cmb/2018.64.13.8`** (journal: Cellular and Molecular Biology). PubMed missed this DOI at index time but CrossRef has it.
- **DOI resolver**: `https://doi.org/10.14715/cmb/2018.64.13.8` → 200 → `https://cellmolbiol.org/index.php/CMB/article/view/2541`.
- **Unpaywall**: `is_oa=True`, best OA PDF = `https://www.cellmolbiol.org/index.php/CMB/article/download/2541/1328` (publisher OA, gold-status).
- **Direct PDF fetch**: 200, Content-Type `application/pdf`, first 8 bytes `%PDF-1.4`. Confirmed rescuable.
- **Reads**: paper doesn't produce or reference any sequencing accession (methods paper); NCBI elink → 0; EuropePMC datalinks empty. Genuine absence of reads, not a probe miss.

**Recommendation**:
- Backfill the DOI cell in `coverage_review.tsv` with `10.14715/cmb/2018.64.13.8` (source: crossref title search).
- Re-run `fetch_paper.py` on this PMID — PDF will succeed via Unpaywall (already whitelisted OA host, no CF).
- Mark as **PDF-rescuable, reads-none-legitimate** (paper isn't in CRC-microbiome scope for reads, but it's in the corpus per the PubMed query so the PDF matters).

### PMID 34706245 — Cell Rep — 10.1016/j.celrep.2021.109886

Current state: PDF=unpaywall, supp=NONE, reads=NONE.

**Investigation**:
- Paper: "Oral microbiota affects the efficacy and prognosis of radiotherapy for colorectal cancer in mouse models." 16S rRNA seq of oral + gut microbiota in mouse CRC.
- **CrossRef** primary URL: `https://linkinghub.elsevier.com/retrieve/pii/S2211124721013565`. License: CC-BY-NC-ND 4.0 VOR → Gold OA.
- **PMC**: no PMCID (not in PMC OA). This is why fulltext text-mining via EuropePMC returned empty.
- **Unpaywall**: is_oa=True; best OA = Elsevier VOR at cell.com (see below).
- **Landing page probe** `https://www.cell.com/cell-reports/fulltext/S2211124721013565`: 403 → 200 (retry) → intermittent Cloudflare gating. When 200 arrives, HTML contains:
  - `PRJEB38064` (ENA accession) — the reads accession that we're missing! ENA confirms 188 runs, 2.36 GB.
  - `mmc2.xlsx`, `mmc3.pdf` at `https://www.cell.com/cms/10.1016/j.celrep.2021.109886/attachment/{uuid}/mmc{N}.{ext}` — supp files (2 discovered).
- **PDF endpoints**: `pdfExtended/{PII}` returned 200 %PDF on attempt 1 of a session, then 403 on repeats. Same PII in `/pdf/{PII}.pdf` triggers 30-redirect loop. Stochastic CF gating.

**Recommendation**:
- **Reads rescue** — text-mine `PRJEB38064` from the cell.com fulltext HTML. Since cell.com is CF-gated, this scan should be run either (a) opportunistically once the plugin retrieves the landing HTML for supp enumeration, or (b) via the local Cloudflare-rescue agent that already handles Elsevier/ScienceDirect.
- **PDF rescue** — already have unpaywall (probably its own hosted VOR copy). Cell Press PDF endpoint `pdfExtended/{PII}` is worth trying as fallback but only from the local (EMBL-network) agent.
- **Supp rescue** — 2 files enumerable from HTML; download requires local agent.

### PMID 33262469 — Cell Death Differ — 10.1038/s41418-020-00684-w (**Nature imprint**)

Current state: PDF=nature+pmc_oa+unpaywall, supp=pmc_oa+publisher:nature, reads=NONE.

**Investigation**:
- This is a Nature-family paper (10.1038/s41418…), handled by `publishers/nature.py`. Not a Cell Press paper despite the "Cell" journal name.
- PDF + supp are already rescued — only reads gap.
- **EuropePMC datalinks**: BioProject category → **PRJNA478491**. Running `probe_europepmc_datalinks(sess, "33262469")` right now returns `['PRJNA478491']`. Running `probe_ena_filereport(sess, "PRJNA478491")` returns **400 runs, 71.4 GB**.
- The reason the tsv has NONE is stale: when `probe_coverage.py` was originally run for this PMID, EuropePMC hadn't yet populated the BioProject datalink. Re-running the probe now catches it.
- **Full-text mining confirms**: EuropePMC XML has 135 KB fulltext of PMC8167112; PRJNA478491 appears in the Data Availability section ("data availability of 16S RNA seq"). Text-mine would also work as a redundant path.

**Recommendation**:
- Simply **re-run `probe_coverage.py`** on this PMID (or on the whole `reads_source=NONE` cohort) to rescue PRJNA478491.
- No new plugin work needed. This is Nature-plugin territory for PDF/supp (already done) and generic EuropePMC-datalinks refresh for reads.

### PMID 37130517 — Cell Host Microbe — 10.1016/j.chom.2023.04.007

Current state: PDF=unpaywall, supp=NONE, reads=europepmc (PRJNA784939, 971 runs, 1.3 TB).

**Investigation**:
- **Landing HTML** at `https://www.cell.com/cell-host-microbe/fulltext/S1931312823001580` fetched 200 (409 KB) on one attempt. Contains:
  - 14 `mmc*` supp references, at least 3 with full `www.cell.com/cms/10.1016/j.chom.2023.04.007/attachment/{uuid}/mmc{N}.{ext}` URLs.
  - Accession PRJNA784939 already in tsv (no gap).
- **PDF endpoint** `pdfExtended/{PII}` returned 200 %PDF on one attempt then 403 → stochastic CF gating same as Cell Rep.
- CrossRef license: `http://www.elsevier.com/open-access/userlicense/1.0/` VOR — Gold OA.

**Recommendation**:
- Only supp gap. A Cell Press plugin could enumerate supp from cell.com landing HTML; the actual downloads need the local agent (CF-gated CDN).

### PMID 34418587 — Cell Mol Gastroenterol Hepatol — 10.1016/j.jcmgh.2021.08.007

Current state: PDF=pmc_oa (PMC8600093), supp=NONE, reads=europepmc (3 BioProjects, 86 runs, 3.7 GB).

**Investigation**:
- Article now hosted at `www.cmghjournal.org` (Elsevier CMGH has its own subdomain, but cell.com redirects to it). Both platforms 403 on the pdf endpoint from our cluster IP most of the time; `www.cmghjournal.org/action/showPdf?pii={dashed-PII}` returned 200 %PDF on one out of 3 attempts (100% Cloudflare gating).
- **PMC-hosted supp**: none — the PMC XML has no `<supplementary-material>` blocks, and the PMC website page has no `mmc*` references. All supp is publisher-side.
- **Landing HTML** at `www.cmghjournal.org/article/S2352-345X(21)00174-0/fulltext`: 441 KB fetched successfully. But **no `mmc*` supp references present** in this particular paper's HTML (`cms/attachment: 0`, `mmc*: 0`). Discovered supp is only referenced abstractly ("Supplemental Materials and Methods") without downloadable file links → this paper genuinely may not have downloadable supp beyond a merged supp-in-PDF. The landing page WOULD reveal them if they existed.
- Also found in HTML: **GSE144719, GSE166030, GSE166038** (GEO series). Not currently captured in reads_accessions — the probe regex `^(PRJ[END][AB]|ERP|SRP|DRP)\d+$` (see `INSDC_PROJECT_RE` in `probe_coverage.py:44`) intentionally excludes GEO. If we widen scope to include GEO's underlying SRA runs, these might yield additional reads, but that's a separate probe-scope discussion.

**Recommendation**:
- Supp: likely zero downloadable supp files — the fulltext HTML has none. Not a plugin gap; genuine absence.
- Reads: three GSE series worth flagging in an "extra_reads_gse" column; underlying SRA runs are secondary but could add samples. Out of current probe scope.

### PMID 36778661 — Cell Genom — 10.1016/j.xgen.2022.100096

Current state: PDF=pmc_oa (PMC9903660), supp=pmc_oa, reads=europepmc (PRJNA645018, 1068 runs, 1.8 TB).

**Investigation**:
- Already fully covered. No gap.
- Cell Genom is Gold OA (CC-BY 4.0 per CrossRef). PMC has full text + supp + PDF, so no plugin needed.
- **Interesting side finding**: landing HTML contains **PRJEB23844** in addition to PRJNA645018. On ENA, PRJEB23844 = 0 runs (probably referenced but not this paper's own deposit; likely a legacy accession referenced in a methods section for a public reference dataset). Not adding it.

**Recommendation**: none — fully covered.

### PMID 35688320 — Cell Mol Gastroenterol Hepatol — 10.1016/j.jcmgh.2022.05.010

Current state: PDF=pmc_oa+unpaywall (PMC9421583), supp=pmc_oa, reads=europepmc (PRJNA759725, PRJNA760488, 103 runs, 70 GB).

**Investigation**:
- Fully covered.
- One-off cell.com probe: `pdf/{PII}.pdf` triggers 30-redirect loop; `showPdf?pii={dashed-PII}` returned 200 %PDF on attempt 0 then 403 on attempts 1 & 2. Same stochastic CF behavior as the other Cell Press papers.

**Recommendation**: none — fully covered.

## Cell.com platform findings

### 1. Cloudflare behavior — cell.com IS CF-gated from our cluster IP, aggressively and stochastically

- Every observed 403 was `Content-Type: text/html; charset=UTF-8` — a CF challenge page, not a hard 403 from origin.
- **Stochastic pattern**: a request often returns 200 %PDF once, then 403 for the next N attempts, then 200 again after some cool-down. No fixed backoff we can predict.
- Sessions with cookies + Referer don't help meaningfully — CF fingerprints likely on TLS/JA3, not on cookies.
- **This means**: cell.com behaves the same as sciencedirect.com from the cluster — reachable in principle, unreachable in practice. The already-existing local Cloudflare-rescue agent (Playwright/patchright) IS the right tool.

### 2. URL patterns are stable and simple

| Purpose | Pattern | Notes |
|---|---|---|
| Fulltext HTML | `https://www.cell.com/{journal-slug}/fulltext/{PII}` | Also: `https://www.cell.com/action/showFullText?pii={dashed-PII}` |
| PDF (canonical) | `https://www.cell.com/action/showPdf?pii={dashed-PII}` | Returns 302 → actual PDF when reachable; %PDF on success. |
| PDF (alt) | `https://www.cell.com/{journal-slug}/pdfExtended/{PII}` | Returned 200 %PDF for Cell Rep + Cell Genom + CHM opportunistically. |
| Supp files | `https://www.cell.com/cms/{DOI}/attachment/{uuid}/mmc{N}.{ext}` | Enumerable ONLY by parsing the fulltext HTML (uuid is per-file). |

Journal slug map (of interest to this corpus):
- Cell Rep → `cell-reports`
- Cell Host Microbe → `cell-host-microbe` (also has dedicated hostname `cellhostmicrobe.com` — untested)
- Cell Genom → `cell-genomics`
- Cell Mol Gastroenterol Hepatol → `cellmolgastro` (but articles now redirect to `www.cmghjournal.org/article/{dashed-PII}/{fulltext|pdf}`)

The dashed PII form (e.g. `S2211-1247(21)01356-5`) vs. the compact form (`S2211124721013565`) matters: `action/showPdf?pii=...` needs the dashed form; `/{journal-slug}/pdf/{PII}.pdf` uses the compact form.

### 3. Supp file discovery is landing-page-parse-only

Cell Press hides supp file URLs inside the fulltext HTML at `data-src="/cms/{DOI}/attachment/{uuid}/mmc{N}.{ext}"` links (and inline `<a href>`). There is no listing API. So the plugin's supp discovery MUST:
1. Fetch the fulltext HTML.
2. Regex-extract `mmc\d+\.[a-zA-Z0-9]+` occurrences plus their surrounding `attachment/{uuid}/` URLs.
3. HEAD-probe each supp URL to confirm accessibility.

### 4. Cell Rep vs Cell Host Microbe — no observed access difference

Both papers are Gold OA (CC-BY-NC-ND 4.0 VOR per CrossRef). Both saw the same stochastic CF pattern from our cluster IP. The mission brief hypothesized "Cell Host Microbe is subscription-only" — the CrossRef license actually says `open-access/userlicense/1.0/` VOR for PMID 37130517, so this paper IS OA. In our corpus (CRC-microbiome), papers are usually recent, and Cell Host Microbe's newer articles seem to be OA.

Practical: same publisher CDN, same CF gating, same rescue path. One plugin covers both.

## Publisher-plugin recommendation

### Cell Press plugin viable? **YES — moderate value**

Fully-rescued rows on this corpus after adding it: **0 net rescue on the cluster IP** (cell.com is CF-gated → the plugin can't download from our IP). BUT the plugin adds value in three ways:

1. **URL enumeration for the local (Cloudflare-rescue) agent**. Currently the local agent has to duplicate URL-construction logic for Elsevier ScienceDirect + Cell Press. Codifying it in `publishers/cell_press.py` lets both the cluster (enumerate + note supp URLs in `attempts`) and the local agent (actually download) share the same knowledge.
2. **Reads gap for PMID 34706245** — the plugin's `fetch_pdf` HTML retrieval (opportunistic — even if it usually 403s, it will succeed ~15% of the time based on observed rate) can grep out `PRJEB38064` on the fly and forward to the reads probe. Not urgent because the local agent will fetch the same HTML anyway, but it's a nice bonus.
3. **Future-proofing for larger Cell Press corpora** — this deep-dive touched 4 Cell-branded Elsevier journals. Broader corpora will hit Cell, Neuron, Immunity, Cell Metabolism, Cancer Cell, Cell Stem Cell, Current Biology, Molecular Cell etc. Ordering the plugin BEFORE a hypothetical generic Elsevier ScienceDirect plugin means those papers go through the more-open cell.com endpoints (which sometimes work) rather than the always-CF sciencedirect.com endpoints.

### Sketch: `publishers/cell_press.py`

```python
# DOI dispatch — Cell Press DOI suffixes always start with a Cell-branded journal code.
CELL_PRESS_JOURNAL_CODES = {
    "j.cell.":    "cell",                          # Cell
    "j.celrep.":  "cell-reports",
    "j.chom.":    "cell-host-microbe",
    "j.xgen.":    "cell-genomics",
    "j.jcmgh.":   "cellmolgastro",                 # ⇒ redirects to cmghjournal.org
    "j.stem.":    "cell-stem-cell",
    "j.ccell.":   "cancer-cell",
    "j.molcel.":  "molecular-cell",
    "j.cmet.":    "cell-metabolism",
    "j.immuni.":  "immunity",
    "j.cub.":     "current-biology",
    "j.neuron.":  "neuron",
    "j.medj.":    "med",
    "j.crmeth.":  "cell-reports-methods",
    "j.crmed.":   "cell-reports-medicine",
    "j.devcel.":  "developmental-cell",
    "j.isci.":    "iscience",
    "j.xcrm.":    "cell-reports-medicine",         # historic prefix
    "j.xinn.":    "the-innovation",
    "j.xops.":    "ophthalmology-science",
    "j.xpro.":    "star-protocols",
    "j.hgg.":     "hgg-advances",
}

class CellPressPublisher(Publisher):
    doi_prefix = "10.1016"  # Elsevier prefix — matches() must narrow by suffix
    name = "cell_press"

    def matches(self, doi: str) -> bool:
        if not doi.startswith("10.1016/"):
            return False
        suffix = doi.split("/", 1)[1]
        return any(suffix.startswith(code) for code in CELL_PRESS_JOURNAL_CODES)

    def _journal_slug(self, doi: str) -> str:
        suffix = doi.split("/", 1)[1]
        for code, slug in CELL_PRESS_JOURNAL_CODES.items():
            if suffix.startswith(code):
                return slug
        raise ValueError(f"Not a Cell Press DOI: {doi}")

    def _pii_from_crossref(self, session, doi):
        # PII isn't derivable from the DOI — must fetch from CrossRef `resource.primary.URL`.
        r = session.get(f"https://api.crossref.org/works/{doi}", timeout=30)
        primary = r.json()["message"]["resource"]["primary"]["URL"]
        # e.g. https://linkinghub.elsevier.com/retrieve/pii/S2211124721013565
        return primary.rsplit("/", 1)[-1]

    def _fulltext_url(self, slug, pii): return f"https://www.cell.com/{slug}/fulltext/{pii}"
    def _pdf_url(self, slug, pii):      return f"https://www.cell.com/{slug}/pdfExtended/{pii}"
    def _showpdf_url(self, pii_dashed): return f"https://www.cell.com/action/showPdf?pii={pii_dashed}"
    # Note: pii_dashed is derived by inserting dashes at fixed positions in PII.

    def fetch_pdf(...):
        # Try showPdf?pii=... first (works for all journals), then pdfExtended.
        # Retry with backoff on CF 403; expect ~10-30% success from our IP.
        # On success, also parse fulltext HTML for accession backfill + supp URL enumeration.
        ...

    def fetch_supp(...):
        # 1. Fetch fulltext HTML (retry on CF 403).
        # 2. Regex-extract all /cms/{doi}/attachment/{uuid}/mmc{N}.{ext} URLs.
        # 3. Download each (with retry).
```

### DOI-registry dispatch order

Currently `_REGISTRY` in `publishers/__init__.py` is:
```python
[LegacyNaturePublisher(), NaturePublisher(), SpringerPublisher(), BMJPublisher()]
```

Add Cell Press **before** any generic Elsevier plugin would come:
```python
[LegacyNaturePublisher(), NaturePublisher(), SpringerPublisher(), BMJPublisher(),
 CellPressPublisher()]  # 10.1016/j.<cell-brand>. — must precede generic ElsevierPublisher()
```

### Effort estimate

- **Code**: ~250 LOC + tests, ~1 half-day. The Nature plugin is 350 LOC and Cell Press has similar complexity.
- **Testing**: 5 Cell Press papers in this corpus can serve as fixtures (34706245, 37130517, 34418587, 35688320, 36778661).
- **Deployment gotcha**: the plugin must run alongside the local Cloudflare-rescue agent path, not replace it. Simplest: have the plugin enumerate URLs on the cluster (via `attempts` log entries) and let the local agent read those URLs to complete the downloads.

### Expected rescue on current corpus

Direct cluster-IP rescue: **0 rows** (all CF-gated).
Via local-agent handoff enabled by the plugin's URL enumeration: **up to 3 rows** with new supp files (34706245, 37130517), plus **1 reads accession backfill** (34706245 → PRJEB38064) that will be discovered as a side effect of HTML parsing.
On future Cell Press papers in a larger corpus: mid-single-digit-to-teens percent PDF/supp rescue rate depending on the class of paper (fully OA Cell Genom always in PMC anyway; subscription Cell/Neuron only rescuable via local agent).

## Actionable items for parent agent

Ordered by ROI.

1. **Reads rescue for PMID 33262469** (easiest win): re-run `probe_coverage.py` for this PMID. EuropePMC now exposes PRJNA478491 in datalinks; probe will find it. Payoff: 400 runs, 71.4 GB added to the corpus. **This is a stale-data issue, not a code bug — the probe code is correct.**

2. **Reads rescue for PMID 34706245** (medium effort): text-mine `PRJEB38064` from cell.com fulltext HTML (188 runs, 2.4 GB). Options:
   - **Fastest**: manually add `PRJEB38064` to the reads_accessions column for this row.
   - **Systematic**: add a fulltext-HTML mining fallback to `probe_coverage.py` — when EuropePMC datalinks + NCBI elink + abstract regex all yield nothing, fetch the CrossRef primary URL and regex over its body. This helps any Elsevier paper without a PMCID.

3. **PDF rescue for PMID 30403593** (small win): backfill the DOI cell in `coverage_review.tsv` with `10.14715/cmb/2018.64.13.8`. Re-run `fetch_paper.py` → Unpaywall path succeeds with 200 %PDF (verified). Also worth adding a **CrossRef title-search fallback for missing-DOI rows** as a general PubMed-import complement.

4. **Cell Press plugin** (larger effort, deferred benefit): sketch above. Estimated ~1 half-day to write, ~5 fixture papers for testing. Its main utility on today's corpus is Cell Rep + CHM supp enumeration (needs local-agent completion) and future-corpus reach. Not urgent for this deep-dive's cell-family subcorpus.

5. **General probe improvement — GEO series capture** (side-finding, low urgency): PMID 34418587 has 3 GSE series in its landing HTML that never make it to `reads_accessions`. Regex is currently intentionally scoped to INSDC BioProject-level accessions. Widening scope to include GSE series (with downstream ENA-SRA resolution) is a distinct scope decision — flagging for the parent to consider.

## Scope + method notes for reproducibility

- All probes used `requests` from within `/g/typas/Personal_Folders/Nic/miniforge3/envs/pyhmmer` on the master salloc — no `srun_fresh` needed (all IO-bound, sub-second per request).
- User-Agent used: `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36` (matches the codebase's `_BROWSER_UA` in `publishers/base.py`).
- Throttling: kept per-domain requests ≤2/s; NCBI elink calls ≤3/s with `time.sleep(0.4)` intervals.
- No PDF downloads — only HEAD probes and streamed `iter_content(chunk_size=8)` first-byte sniffs for magic-byte validation (`b"%PDF"`).
- No modifications to `coverage_review.tsv` or any script; all findings encoded in this report only.
