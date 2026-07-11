# Master paper disposition — shared-memory table for all subagents

Companion to `data/master_paper_disposition.tsv`. Priority levels drive where subagents
invest effort. Rows marked IGNORE are DELIBERATELY excluded from coverage improvement work
per 80/20 rule (long-tail singletons and Chinese-institution-only journals).

## Priority ledger

| Priority | N rows | Meaning |
|---|---|---|
| P0-plugin-done | 75 | Publisher plugin already exists; use as-is. |
| P0-pure-OA | 196 | Pure-OA big-N publisher; build/have plugin, aggressive supp mining. |
| P0-done | 11 | Already at 100/100 (PLOS etc.); no further work. |
| P1-local-rescue | 116 | Cloudflare-gated from cluster; local Playwright agent handles. |
| P2-batch-probe | 53 | Mid-N; run generic OA-probe passes but no bespoke plugin. |
| IGNORE-not-worth-chasing | 54 | Long-tail singleton in an unfamiliar publisher; 80/20 exclude. |
| IGNORE-chinese-institution-only | 3 | Chinese regional journal, Chinese-institution login required. |
| IGNORE-already-complete | 2 | Singleton but already fully served by unpaywall/PMC. |

**Effective active corpus = 451 / 510 rows (88.4%).**
**Deliberately ignored = 59 rows (11.6%).**

## Rules of engagement for subagents

- If a row is IGNORE-*, DO NOT probe it further, DO NOT count it against coverage numbers.
  Skip it in every driver loop. This is the 80/20 discipline.
- P0-plugin-done rows: only rescue if a specific gap is documented in the row.
- P0-pure-OA rows: aggressive supp mining is valid — these are the biggest lever.
- P1-local-rescue rows: from cluster, only *identify* the URL patterns; the actual
  fetches happen laptop-side via Playwright. Don't burn cycles on cluster fetches
  beyond one confirmation HEAD.
- P2-batch-probe rows: hit them with the generic HTML-mining passes but do NOT
  build bespoke publisher plugins.

## Coverage on ACTIVE corpus (ignoring the IGNOREs)

| Metric | Active N | Currently ok |
|---|---|---|
| PDF   | 451 | 408 (90.5%) |
| Supp  | 451 | 280 (62.1%) |
| Reads | 451 | 131 (29.0%) |

## Next-highest-leverage buckets (by paper count × current gap)

| Bucket | N | PDF gap | Supp gap | Reads gap | Est. leverage |
|---|---|---|---|---|---|
| elsevier-or-cell_press (P1-local-rescue) | 54 | 20 | 39 | 44 | 103 |
| mdpi (P0-pure-OA) | 54 | 0 | 15 | 41 | 56 |
| frontiers (P0-pure-OA) | 79 | 0 | 10 | 39 | 49 |
| wiley (P1-local-rescue) | 31 | 2 | 16 | 29 | 47 |
| springer (P0-plugin-done) | 31 | 0 | 12 | 26 | 38 |
| bmc (P0-pure-OA) | 45 | 0 | 3 | 32 | 35 |
| taylor-francis (P1-local-rescue) | 23 | 3 | 9 | 19 | 31 |
| aacr (P2-batch-probe) | 13 | 2 | 12 | 12 | 26 |
| bmj (P0-plugin-done) | 14 | 3 | 7 | 14 | 24 |
| oup (P2-batch-probe) | 10 | 2 | 8 | 10 | 20 |
| elsevier-gastro (P1-local-rescue) | 8 | 3 | 8 | 7 | 18 |
| lww (P2-batch-probe) | 7 | 3 | 7 | 6 | 16 |
| sage (P2-batch-probe) | 7 | 3 | 6 | 6 | 15 |
| mid-N-unknown (P2-batch-probe) | 9 | 0 | 5 | 9 | 14 |
| nature (P0-plugin-done) | 27 | 0 | 0 | 12 | 12 |
| asm (P0-pure-OA) | 18 | 0 | 8 | 2 | 10 |
| oncotarget (P2-batch-probe) | 7 | 0 | 3 | 6 | 9 |
| plos (P0-done) | 11 | 0 | 0 | 6 | 6 |
| science_aaas (P0-plugin-done) | 3 | 2 | 3 | 0 | 5 |
