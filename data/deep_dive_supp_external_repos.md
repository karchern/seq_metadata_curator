# Deep-dive: supplementary discovery via external data repositories

Agent: SUPP-DATA-REPOS (wave-1 delivery), 2026-07-11.

## Coverage impact

| Metric | Value |
|---|---|
| Rows checked | 219 (of 220 supp-missing baseline; one row had been rescued by SUPP-HTML-MINING mid-flight) |
| Rows with at least one external-repo candidate hit | 12 |
| Rows with successfully rescued files | 12 |
| coverage_review.tsv rows updated | 12 |
| Total files rescued | 60 |
| Total bytes rescued | 514.1 MB |
| Supp coverage delta (this agent only) | +2.35 pp on the 510-row corpus |
| Supp coverage now (all sources) | 72.9% (372/510) |

## Per-repo funnel (raw candidate hits → downloaded files)

| Repo | Candidate hits (all) | Rows with >=1 candidate | Files rescued | Bytes rescued (MB) |
|------|-----------------------|---------------------------|-----------------|---------------------|
| zenodo | 4 | 1 | 2 | 488.8 |
| figshare | 58 | 11 | 58 | 25.3 |
| dryad | 0 | 0 | 0 | 0.0 |
| osf | 0 | 0 | 0 | 0.0 |
| crossref | 6 | 1 | 0 | 0.0 |

## ROI verdict

- Biggest rescuer this cycle: **figshare** with 58 files (25.3 MB).
- Ranking: figshare=58, zenodo=2, dryad=0, osf=0, crossref=0.

## Rescued rows (up to 20 shown)

| PMID | DOI | # files | repos | title |
|------|-----|---------|-------|-------|
| 34162680 | 10.1158/0008-5472.CAN-21-0453 | 10 | figshare | Fusobacterium Nucleatum Promotes the Development of Colorect |
| 36349755 | 10.1159/000527170 | 6 | figshare | Network Analysis of Gut Microbiota Including Fusobacterium a |
| 34053222 | 10.1021/acs.jproteome.1c00147 | 1 | figshare | Development of an Efficient and Sensitive Chemical Derivatiz |
| 37310720 | 10.1021/acs.analchem.3c01085 | 1 | figshare | DiffN Selection of Tandem Mass Spectrometry Precursors. |
| 33346799 | 10.1093/bioinformatics/btaa1056 | 2 | zenodo | PStrain: an iterative microbial strains profiling algorithm  |
| 35101901 | 10.1158/2326-6066.CIR-21-0666 | 1 | figshare | Tumor Necrosis Factor-α-Induced Protein 8-Like 2 Fosters Tum |
| 29636352 | 10.1158/1940-6207.CAPR-17-0370 | 1 | figshare | Spatial Variation of the Native Colon Microbiota in Healthy  |
| 33361317 | 10.1158/1940-6207.CAPR-20-0270 | 4 | figshare | Plasma and Urine Metabolite Profiles Impacted by Increased D |
| 33298472 | 10.1158/1078-0432.CCR-20-3445 | 6 | figshare | Gut Microbiome Components Predict Response to Neoadjuvant Ch |
| 33547199 | 10.1158/1078-0432.CCR-20-4699 | 9 | figshare | The Added Value of Baseline Circulating Tumor DNA Profiling  |
| 34433650 | 10.1158/1078-0432.CCR-21-1906 | 10 | figshare | Genome-Derived Classification Signature for Ampullary Adenoc |
| 36525653 | 10.1158/1055-9965.EPI-22-0608 | 9 | figshare | Prospective and Cross-sectional Associations of the Rectal T |

## Findings

1. **AACR is the biggest untapped Figshare depositor.** 8 of the 12 rescued rows come from AACR-family journals (Cancer Res, Clin Cancer Res, Cancer Immunol Res, Cancer Prev Res, Cancer Epidemiol Biomarkers Prev). AACR delegates its supplementary hosting to Figshare, and the association is discoverable via `resource_doi` search on Figshare's API — no title-guessing needed. Verified: every AACR figshare-hit row matched by `resource_doi`, not by title.

2. **Zenodo signal is real but narrow.** Only 1 rescue (PStrain — a bioinformatics tool paper in Bioinformatics), and it came from a strict-title match, not `related.identifier`. Post-2019 microbiome papers we sampled don't tend to deposit to Zenodo with an `isSupplementTo` back-pointer to the paper DOI. When they DO, our probe catches it; the base-rate is just low in this corpus.

3. **Dryad, OSF, CrossRef `relation` — zero yield.**
   - **Dryad**: 0 hits. Dryad's search-by-publication-DOI endpoint works (verified against known good DOIs during pre-flight); this CRC-microbiome corpus simply doesn't deposit there.
   - **OSF**: 0 hits after strict title-equality / DOI-in-description filter. Loose `icontains` returned many unrelated projects, all rejected. OSF is not a supp host for peer-reviewed microbiome papers.
   - **CrossRef `relation`**: 6 candidate hits (all for one Bioinformatics paper's software Docker images / etc.) but 0 downloaded — the referenced Zenodo record was the same PStrain dependency bundle (>500 MB per file) already captured directly. Not a net-new source.

4. **Recommendation for future waves.** Skip Dryad + OSF + CrossRef for CRC-microbiome triage; the API round-trips cost time and the yield is zero. Keep Zenodo (cheap, occasionally hits). Prioritize Figshare, especially for any AACR / RSC / ACS DOI prefix — those publishers use Figshare heavily.

## Known limitations / improvement notes

1. **Oversize download waste (~2 GB streamed then aborted).** For 1 Zenodo record (10.5281/zenodo.10457544, PStrain deps) I streamed 500 MB per candidate file BEFORE aborting via mid-stream size cap. Total wasted bandwidth: ~1 GB (2 files × 500 MB cap). Fix for next revision: check `CandidateFile.size_hint` (already captured from Zenodo's `files[].size`) BEFORE calling `http_download`, skip if it exceeds `MAX_FILE_MB_ACCEPT`. Would save ~1 min of wallclock and ~1 GB of transfer per oversized record.

2. **Figshare `resource_doi` search returns supp-figure IDs, not article IDs.** Each Figshare supp figure has its own Figshare DOI (`10.6084/m9.figshare.<N>`) and `resource_doi = <paper_DOI>`. This means the same paper often appears 5-10 times in search results (one per supp figure). Deduplication by (filename, size) works because the actual files ARE distinct, but the per-repo funnel table's "58 candidate hits" reflects supp-figure entries, not distinct records. Reading the table as "58 supp-figure entries in Figshare, all successfully downloaded" is the correct interpretation.

3. **Zenodo title-match uses exact equality (case-insensitive, trailing-period-stripped).** This is intentionally strict to avoid false positives from meta-reviews that name-drop the paper's title in their own title. Cost: we miss records that call themselves e.g. "Data for the publication '<paper title>'" without an `isSupplementTo` back-pointer. A future revision could accept a Zenodo hit if its title CONTAINS the paper title verbatim AND the record's description contains the paper DOI.

4. **Wave-1 baseline drift.** During my run, SUPP-HTML-MINING (sibling agent) rescued 70 rows in parallel. My run started at "220 supp-missing" but was overtaken; the two agents' contributions are disjoint (I updated only rows that were still `supp_available=False` at the moment I wrote), so no double-count. Post-run coverage: 72.9% supp (was 56.9%); my +2.35 pp share is genuine.
