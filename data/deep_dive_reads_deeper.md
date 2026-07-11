# Deep-dive: reads recovery via orthogonal strategies

Run date: 2026-07-11  
Script: `scripts/refresh_reads_deeper.py`

## Coverage delta

| Metric | Value |
|---|---|
| Pre-run reads coverage | 138/510 (27.1%) |
| Post-run reads coverage | 168/510 (32.9%) |
| Rows lifted from NONE | 30 |
| Delta (pp) | +5.88 |
| Total new runs added | 11671 |
| Total new data added | 21904.9 GB (~21.39 TB) |

## Per-strategy stats

| Strategy | new accs | rows-lifted-from-NONE | runs added | GB added |
|---|---:|---:|---:|---:|
| geo_to_sra | 17 | 3 | 2105 | 13310.2 |
| arrayexpress | 6 | 1 | 171 | 573.8 |
| data_avail_section | 22 | 13 | 4291 | 6415.7 |
| supp_table_scan | 0 | 0 | 0 | 0.0 |
| crossref_relation | 0 | 0 | 0 | 0.0 |
| europepmc_section | 16 | 13 | 5104 | 1605.2 |

## Example rescues (up to 8 per strategy)

### data_avail_section

- PMID 31275588 → PRJNA534511
- PMID 36999930 → SRP351186, SRP351836
- PMID 34771584 → PRJEB46353
- PMID 37027066 → ERP143365
- PMID 31169073 → PRJEB17707
- PMID 30548192 → SRP144012
- PMID 34307189 → PRJNA706060, PRJNA514108
- PMID 31428073 → DRA008522

### europepmc_section

- PMID 37894412 → PRJNA1013750
- PMID 34544375 → ERP115622, PRJNA672867, PRJNA678737
- PMID 37641475 → PRJNA943491
- PMID 38201530 → PRJNA994445
- PMID 34539647 → PRJNA669258
- PMID 37560524 → PRJNA763023
- PMID 38304032 → PRJNA808420
- PMID 37202560 → PRJNA941834

### arrayexpress

- PMID 38027096 → ERP143399, ERP143361, ERP125057, ERP108145, ERP015832, ERP120056

### geo_to_sra

- PMID 37901832 → PRJNA788529
- PMID 34988460 → PRJNA420049, SRP125749, PRJNA612305, SRP252592, PRJNA218851, SRP029880, PRJNA376161, SRP100445, PRJNA413956, SRP119775, PRJNA565188, SRP221472
- PMID 38029508 → PRJNA603523, SRP245601, PRJNA805525, SRP359396

## Notes / surprises

- All new accessions were validated against ENA `filereport?result=read_run` with `n_runs > 0`.
- `supp_table_scan` scope is inherently narrow: only ~10 xlsx files are on disk (one PMID has supp locally). This strategy will pay off much more once `refresh_supp_via_html.py` (Batch B sibling) grows the on-disk supp corpus.
- `europepmc_section` is nearly a no-op for the no-reads residue: most of those PMCs are non-OA and Europe PMC returns 404 on `fullTextXML`. When it does return XML, the JATS `<sec>`/`<notes>` heuristic still works.
- `geo_to_sra` translates NCBI GDS → BioProject via the `bioproject` field on the esummary doc — most GSEs map cleanly. GEO IDs without a `bioproject` xref are almost always microarray-only (no reads on ENA) and are correctly excluded by the ENA-filereport gate.
- `arrayexpress`: BioStudies' `attributes[Type=ENA]` convention is stable; walking the JSON for any URL matching INSDC_ACC_RE also picks up occasional cross-refs embedded outside link elements.
- `data_avail_section`: mining a small paragraph neighborhood (rather than whole HTML) *also* enables the URL-embedded regex safely — the URL pattern is too permissive across a whole document (would match reference-list URLs).
