# Optimisation results (docs/optimisation-plan.md)

Measured on the 9 gold papers (18 library rows: each paper is in the corpus twice) with the runner in
`services/backend/tests/eval/run_all.py`. Result files: `services/backend/tests/eval/results/`. The gold labels for
areas, ask and topics are proposals (`reviewed: false`) and the sets are small: treat the numbers as direction, not proof.

| Run | Keywords task P/R | Keywords methods P/R | Datasets P/R | Areas accuracy | Ask answered / page hit | Topics ARI |
|---|---|---|---|---|---|---|
| baseline (BLOOMZ, old code) | 0.25 / 0.11 | 0.15 / 0.15 | 0.14 / 0.50 | 0.44 | 0.60 / 1.00 (6 scored) | 0.00 |
| q-typed (Qwen3-8B, typed keywords) | 0.50 / 0.44 | 0.25 / 0.46 | 0.12 / 0.50 | 0.56 | 1.00 / 1.00 | keyword topics: 0 topics |
| q-typed-tagger (+ KBIR tagger) | 0.50 / 0.44 | 0.25 / 0.46 | 0.12 / 0.50 | 0.44 | 1.00 / 1.00 | — |
| q-hybrid (+ passage vectors) | 0.50 / 0.44 | 0.25 / 0.46 | 0.12 / 0.50 | — | 1.00 / 1.00 (13 s vs 6 s per question) | — |
| q-exemplars (leave-one-out) | 0.50 / 0.44 | 0.25 / 0.46 | 0.12 / 0.50 | 0.44 | — | — |
| q-topics-emb (SPECTER2 clusters) | — | — | — | — | — | 0.32 (2 topics over 8 papers) |

## Gate verdicts and defaults

- **Typed keywords (phase 2.1): passed.** Task and methods precision and recall rise well above the 10% gate. Dataset
  precision falls slightly (0.14 → 0.12). Default `KEYWORDS_MODE=typed`.
- **KBIR tagger (phase 2.2): failed.** No change in the scored fields (the scorer does not score keyword-kind terms),
  and area accuracy falls 0.56 → 0.44 because areas read all keywords. Default `KEYWORD_TAGGER=off`.
- **Hybrid retrieval (phase 5.5): not decided.** Both rankings answer all 10 questions on the page. The gold set cannot
  show the 15-point gain the gate asks for, and hybrid doubles the answer time. Default `QA_RETRIEVAL=bm25`. Revisit
  after a harder ask set is labelled.
- **Exemplar areas (phase 3.2): failed.** Accuracy falls 0.56 → 0.44; the settlement step never fired on these papers.
  Default `AREAS_EXEMPLARS=off`. The agreement score code path exists but is not used by default.
- **Embedding topics (phase 4.3): passed on a weak measure.** ARI 0.32 against 0.00 for keyword topics, on 8 papers.
  Default `TOPICS_MODE=embeddings`. In the library rebuild (94 papers, 7 topics) one cluster holds 40 papers; the merge
  threshold needs review. One topic name fell back to a keyword ("#fridaysforfuture") in the evaluation, so the naming
  step's fallback is worth checking.
- **Related papers (phase 4.2):** implemented (API and reader panel); no separate score.
- **Phase 6 (DePlot, figures):** not implemented. Its own gate says to build it only if a figure question cannot be
  answered from captions and citing text, and the pipeline keeps no figure images to run it on.
- **OpenAlex (phase 3.5):** implemented, opt-in via `OPENALEX_EMAIL`, title-only lookups. Off by default.

## Deviations from the plan

- Context budgets: title and figure prompts were cut to 1,200 tokens (from 2,500 and 2,000) because prompt processing
  on the GTX 1660 SUPER is about 15 ms per token.
- The whole model fits on the GPU: at 4,096 context with a q8 K-cache, Qwen3-8B used 4,990 MiB of 6,144 MiB. The plan's
  partial-offload ladder was not needed.
- The worker's memory limit was raised from 2.5 GB to 4 GB (torch and the embedding models).
- BLOOMZ is kept as a fallback (`LLM_FORMAT=raw`, `LLM_CTX_SIZE=2048`, `LLM_MODEL_FILE=bloomz-…`) but is not downloaded
  by default (`FETCH_BLOOMZ=1`).
- The library holds each gold paper twice (same title, two document ids). The evaluation dedupes by title; the library
  does not.

## Operations

- Production stack: `compose.yaml` (private, gitignored) and the tracked `compose.template.yml` carry the same settings.
- Migrations 009–012 are applied to the production database. A topics rebuild was queued and ran (94 vectors, 7 topics).
- Existing papers keep their keywords from the BLOOMZ run. To re-extract them with the typed extractor, use the
  backfill action or rerun with `from=enrich`; this takes about 20 s per paper on the GPU.
