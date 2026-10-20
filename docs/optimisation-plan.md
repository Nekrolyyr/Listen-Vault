# Optimisation plan: keywords, topics, areas and ask

Status: plan, not started. Builds on `docs/extraction-plan.md` (keyword rules) and the evaluation in
`services/backend/tests/eval/`. Each phase has a measurable gate. A change goes into the default path only when its
gate is passed; the previous path stays available behind a setting until then.

## Decisions taken

- **LLM:** Qwen3-8B (instruct, Q4_K_M) replaces BLOOMZ-7b1. It does not fit fully in the 6 GB of the GTX 1660 SUPER
  with room for the KV cache, so the plan starts with a VRAM test and a partial-offload fallback (phase 1).
- **Specialist models run beside the LLM, not on the GPU:** SPECTER2 and the keyword tagger are small enough for CPU.
  This keeps the GPU for the LLM.
- **No external text:** only local models receive paper text. OpenAlex is optional and receives titles and DOIs only.
- **Storage:** no pgvector. Embeddings are stored as `REAL[]` in Postgres. A library of a few thousand papers can be
  searched by brute force in numpy.

## Current state (baseline to beat)

| Feature | Where | Model use today | Known weakness |
|---|---|---|---|
| Keywords (task, methods, datasets, topics, keywords) | `worker/enrich.py` | 3 free-text questions + 1 JSON call, BLOOMZ, 900-token excerpt | Instructions echoed as terms, one term in several fields, missed datasets |
| Figure and table summaries | `worker/enrich.py` `element_summaries` | 1 call per element, 700-token input | Captions only when no citing sentence exists |
| Chapter titles | `worker/chapters.py` | 1 call per untitled section, 600 tokens | Generic `Part N` when the check fails |
| Research areas | `worker/classify.py` | 1 JSON call, 700-token body | Placement fixed to the first call's answer |
| Topics | `worker/topics.py` | Name and member check per cluster | Clusters come from keyword co-occurrence, which is noisy |
| Ask | `worker/qa.py` | BM25 over 150-word passages, 1,000-token context, one answer | Lexical only; 2,048-token window; citations often wrong |

Baseline numbers do not exist yet. Phase 0 creates them.

## Phase 0: measurement (before any change)

Goal: a repeatable score for each feature on the current model, so every later change has a number to beat.

0.1 **Keyword gold.** Extend `extraction_gold.json` (9 papers) with the existing lenient matching. Add an `excluded`
    reason for any paper with no abstract, as now.

0.2 **Area gold.** Hand-label the primary area and up to two others for the same 9 papers, from the title and abstract,
    using the fixed list in `listen/areas.py`. Score: primary accuracy, and share of "also" areas that match.

0.3 **Topic gold.** For the library, label which papers belong together (groups of 2–4 papers per topic, 8–12 topics).
    Score: adjusted Rand index against the groups, and the share of papers placed in a topic with a matching group.

0.4 **Ask gold.** Write 15–20 questions with the paper and the page where the answer is. Score: the cited page is in the
    expected page set (page hit rate), and the answer contains the expected key fact (checked by hand for the first run).

0.5 **Runner.** One script `tests/eval/run_all.py` that calls each feature on the 9 papers and the ask gold, with the LLM
    URL as a parameter, and writes `result-<label>.json`. `eval_extraction.py` becomes one of its parts.

0.6 **Baseline run.** Run on BLOOMZ, record latency per paper and per call, and record the GPU memory in use.

**Gate:** the runner works end to end and the baseline JSON is committed under `tests/eval/results/`.

## Phase 1: move the LLM to Qwen3-8B

1.1 **Image check.** The compose file pins `llama.cpp server-cuda-b11515`. Confirm that build loads the Qwen3
    architecture (run it once with the model). If not, pin a newer tag and check that the `/completion` grammar
    (`json_schema`) and `/tokenize` endpoints still behave the same.

1.2 **Model file.** Add `scripts/fetch-models.sh` step for `Qwen3-8B-Q4_K_M.gguf` from the official Qwen GGUF repository.
    Check the file's SHA-256 against the repository and record it in the script. Disk has about 26 GB free; the model
    is about 5 GB.

1.3 **VRAM test ladder.** The card has 6 GB and the current server uses 5.6 GB, so a full offload is unlikely to fit with
    the KV cache. Start with these settings and step down until the server starts and a 2,000-token prompt runs:
    - `--ctx-size 4096`
    - `--cache-type-k q8_0` (V-cache quantisation needs flash attention; test it separately)
    - `--n-gpu-layers 36`, then 32, 28, 24 (Qwen3-8B is 36 layers; confirm in the GGUF metadata)

    Record for each step: the GPU memory in use, prompt-processing and generation speed, and whether it starts.

1.4 **Fallback.** If no setting from the ladder meets the latency gate below, use the highest layer count that starts and
    accept the slower speed, or drop to Qwen3-4B (about 2.5 GB, fits fully) as a documented second option. The layer count is
    set in `.env` as `LLM_GPU_LAYERS`, which `compose.template.yml` already reads.

1.5 **Chat format.** Qwen3 is a chat model; the raw `Question: … Answer:` format used for BLOOMZ is replaced by its
    template. Non-thinking mode is set by an empty think block after the assistant tag:

    ```
    <|im_start|>system
    {system}<|im_end|>
    <|im_start|>user
    {user}<|im_end|>
    <|im_start|>assistant
    <think>

    </think>

    ```

    Add `LlmClient.chat(system, user, n_predict, temperature, stop, schema)` to `worker/llm.py` using this template. Keep
    `complete()` and `complete_json()` for now. Verify with a unit test that the template string is exact.

1.6 **Context budgets.** Raise the constants with the window (2,048 → 4,096 tokens) and move them into one place (`listen/config.py`
    or `worker/llm.py`), so they are not repeated across modules:

    | Constant | Now | Target |
    |---|---|---|
    | `enrich.EXCERPT_TOKENS` | 900 | 2,500 |
    | `chapters.TITLE_TOKENS` | 600 | 2,500 |
    | `classify.BODY_TOKENS` | 700 | 2,000 |
    | `enrich.ELEMENT_TOKENS` | 700 | 2,000 |
    | `qa.CONTEXT_TOKENS` | 1,000 | 3,000 |

1.7 **Thinking.** Confirm that non-thinking mode produces no `<think>` text in any answer. Strip it defensively in `accept()`
    and `usable()` if it appears.

**Gates:**
- The server runs for 24 hours without an out-of-memory error or restart.
- Median LLM call latency is under 20 s on the ladder's chosen setting. A paper's enrichment (about 5 keyword calls, up to 15
  element summaries, chapter titles) should finish in under 15 minutes. Measure and adjust the element limit if it doesn't.
- Phase 0's keyword and area scores are at least equal to the baseline. If they drop, the prompt is wrong, not the model: fix
  the prompt before going on.

## Phase 2: keywords

2.1 **Typed JSON in one call.** One chat call with a schema of the five fields (task string, methods array, datasets array,
    topics array, keywords array), replacing the three free-text questions. Keep the existing `usable()` and `grounded()` checks
    as the final filter: a model answer must still appear in the text. Tests in `test_enrich.py` stay.

2.2 **Keyword tagger as a second source.** (The `source` check is widened in `009_sources.sql`.) Add a token-classification model, `ml6team/keyphrase-extraction-kbir-inspec`, run on
    CPU over the excerpt and the abstract. Its spans are extractive, so they are grounded by construction. Its output is
    compared with the LLM's keywords:
    - a span that both the LLM and the tagger return is kept with weight 1.0;
    - a tagger-only span is kept with weight 0.6 (new `source` value `tagger`, migration `009_sources.sql`);
    - an LLM-only keyword is kept as now.

    The tagger was trained on Inspec, which has short abstracts. Test on the 9 papers before it goes in, and drop it if it does
    not add to the LLM's correct terms.

2.3 **Rules.** Keep the method-name and dataset rules as a fallback when the LLM answer is empty. Keep frequency terms.

2.4 **Element summaries.** Keep table rows as the input for tables and the caption plus citing sentences for figures. Remove the
    single-sentence fallback; a summary that fails `accept()` is simply left out, which is the current behaviour.

**Gate:** keyword precision and recall on the 9 papers at least 10% better than baseline, and junk (instruction-like) terms at 0.
If the gate fails, the change is the prompt or the schema. Re-run before adding the tagger.

## Phase 3: research areas and paper categories

3.1 **Area-aware keywords (experiment, off by default).** Add the paper's primary area to the keyword prompt as a hint. Test
    whether it helps; it may bias the keywords towards the area and hide cross-area terms, so it stays behind a flag.

3.2 **Exemplar placement.** Papers the user has placed by hand (`document_areas.pinned = true`) are exemplars. For a new paper,
    compute its SPECTER2 embedding (phase 4.1) and take the 5 nearest exemplars. Give the nearest exemplars' areas to the LLM as
    evidence, and use them as the second signal when the LLM's confidence is low.

3.3 **Confidence.** Store the agreement between the LLM's primary area and the exemplar vote in `document_areas`
    (new column `agreement real`, migration `011_area_agreement.sql`). Papers with low agreement are listed in the library for
    review rather than trusted silently.

3.4 **Categories in the pipeline.** Areas use the keywords as input, so the order stays: keywords, then areas, then topics
    (already the case). The change is in the UI and the topics: each topic is shown inside its papers' main area, and topic
    names are checked against the area's cue words, so the library has one spine, area first, then topics within it.

3.5 **OpenAlex reference labels (optional, opt-in).** Where a paper has a DOI, fetch its OpenAlex `topics` (up to three, with
    field and subfield) and store them as reference labels next to the LLM's area. Only the DOI or title is sent. Off by
    default; enabled by `OPENALEX_EMAIL` in `.env`.

**Gate:** area primary accuracy on the 9 papers at or above baseline, and every area placement has an agreement value. The
library lists the papers with low agreement.

## Phase 4: connecting papers through topics

4.1 **Embeddings.** Add the `allenai/specter2_base` model with the proximity adapter (`allenai/specter2`), running on CPU in the
    worker. Input is title and abstract, as the model expects. Store a `REAL[768]` per paper in a new table
    `document_embeddings (document_id, model, vector, updated_at)` (migration `009_embeddings.sql` or the next free number).
    Add a job `embed` that runs after `_structure` for new papers and for the whole library on demand. Migration
    `010_embeddings.sql`.

4.2 **Related papers.** Brute-force cosine search over the stored vectors. Add `GET /documents/{id}/related` returning the 10
    nearest papers with their similarity. Show them on the paper page. This is the first user-visible use of the embeddings.

4.3 **Topic clustering.** Replace the keyword-only clustering in `topics.rebuild`:
    - Cluster the SPECTER2 vectors with average-linkage on cosine distance, threshold tuned on the topic gold (phase 0.3).
    - Keep keyword co-occurrence as a second signal: the similarity between two papers is the mean of the vector cosine and the
      Jaccard of their keywords, so both count.
    - Keep the manual overrides in `document_topic_overrides` unchanged; they are applied after clustering.

4.4 **Topic names.** The LLM names each cluster from its papers' titles (up to 10 titles nearest the centroid), not from keyword
    lists. Output is limited to 2–4 words. The existing member check is kept, now over titles.

4.5 **Topic gate.** Adjusted Rand index against the topic gold at or above the keyword-only baseline, and fewer papers with no
    topic. Keep the keyword-only path behind `TOPICS_MODE=keywords` until the gate passes.

## Phase 5: the ask function

5.1 **Hybrid retrieval.** Keep BM25 and add a small dense passage model for passages (a sentence-embedding model such as
    `BAAI/bge-small-en-v1.5`; not yet evaluated, so choose it in this phase by the ask gold). Run it on CPU. Combine the two
    rankings with reciprocal rank fusion. Store passage vectors with the paper's other embeddings, keyed by passage.

5.2 **Larger context.** Put 8 passages (up to 3,000 tokens) into the prompt instead of 4 (1,000 tokens), each numbered with its
    page and section.

5.3 **Chat prompt.** Answer using only the numbered passages, cite as `[n]`, and say when the paper does not answer the question.
    Use the chat template from 1.5 and keep the citation check.

5.4 **Citation check.** A citation must point to a passage that was actually given. Remove citations that point elsewhere and
    report the sentence as uncited. An answer with no valid citation is asked again once, as now.

5.5 **Ask gate.** Page hit rate on the 15–20 questions at least 15 points above the BM25-only baseline, with zero invented
    citations (a citation number that was not in the prompt).

## Phase 6: figures (optional)

6.1 **Charts.** For figures that are charts, run `google/deplot` (Pix2Struct, about 280M parameters, on CPU) to turn the image into a
    table, then summarise the table with the LLM. Only figures that are charts benefit; diagrams and photos do not.

6.2 **Gate.** Use this only if the phase 0 gold includes a figure question that the caption and citing sentences cannot answer.
    Otherwise skip it.

## Cross-cutting

- **Feature flags.** `KEYWORDS_MODE`, `AREAS_EXEMPLARS`, `TOPICS_MODE`, `QA_RETRIEVAL` (bm25 | hybrid) in `listen/config.py`. Each
  phase's new path ships behind its flag, and the flag defaults to the old path until its gate passes.
- **Re-running the library.** After a gate passes, re-run the affected stages with the existing job options: `from=enrich` for
  keywords, `areas` with `force` for placements, and the topic rebuild. Papers ingested during the change keep their old
  data until re-run. Record the model file name and its SHA-256 in the existing app settings (`appsettings`), so the library can
  show which model produced which data. No per-paper column is added.
- **Tests.** Unit tests for the chat template, the typed-JSON parser, the citation check, the clustering on fixed vectors, and
  reciprocal rank fusion. The eval runner is not part of the unit suite (it needs the model).
- **Logging.** Each LLM call logs its feature, prompt token count, output token count and duration at debug level, so the cost
  of each feature can be read from the logs.

## Risks

| Risk | Effect | Mitigation |
|---|---|---|
| Qwen3-8B does not fully fit | Slow generation; the GPU shares work with the CPU | Phase 1 ladder; accept a partial offload if the gates still pass |
| Pinned llama.cpp build too old | Model fails to load | Phase 1.1 check; bump the image tag |
| Non-thinking output still contains reasoning text | Broken JSON or keywords | Template in 1.5; defensive strip in 1.7 |
| Latency per paper grows | Ingestion takes too long; the library waits behind it | Latency gate in phase 1; element limit |
| KBIR tagger trained on Inspec abstracts | Poor keywords for full papers | Test on the 9 papers before using it (2.2) |
| SPECTER2 clusters papers by method, not topic | Topics look odd | Keyword co-occurrence kept as a second signal (4.3) |
| Small gold sets (9 papers, 15–20 questions) | Scores are noisy | Report counts with each score; grow the gold over time |

## Open decisions

1. Keep BLOOMZ as a fallback on a second port, or remove it after the gates pass? (Recommendation: remove after 2 weeks of clean
   runs.)
2. Include the OpenAlex reference labels (3.5)? They send titles and DOIs to an external service.
3. Include figure summaries via DePlot (phase 6)? Only if the gold shows a need.
4. Accept the partial-offload speed if the full fit is not possible, or move Qwen3-8B to a second, larger GPU? (The current card is the only one.)
