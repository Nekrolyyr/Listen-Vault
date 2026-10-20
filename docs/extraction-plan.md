# Search-term extraction plan: task, methods and datasets

Scope: `services/backend/listen/worker/enrich.py` (the terms that go into `document_keywords` and the library
facets, graph and timeline), and the BLOOMZ-7b1 calls behind it. Topics and plain keywords are kept, but they
pass the same checks.

## What goes wrong now

One BLOOMZ call returns a JSON object with five fields. The stored terms for the author's own papers show four
failure modes:

1. **Instructions echoed as terms.** "analyze the platform's mechanisms" appears as a method and as a dataset;
   "present the main theories of network science" and "explore the psychological and social implications" also
   appear. BLOOMZ repeats the request it was given.
2. **One term in several fields.** The same phrase is stored as task, method and dataset at once, so every facet
   and the similarity vectors count it three times.
3. **Ungrounded or generic answers.** "general", "presentation", "method", "metods" (sic), "politics", "grok"
   for a paper about Instagram; a method named that the paper never uses.
4. **Missed data and methods.** "Visual protest material" yields no dataset, "German federal election 2017" none,
   and the qualitative approach of one paper is only partly named. The model is shown the abstract and the first
   sentences of sections, and the data description is usually in a method section that is not in that excerpt.

Measured on the author's 9 labelled papers (`services/backend/tests/eval/extraction_gold.json`), see
"Results" below.

## Design

1. **One short question per field, same excerpt.** Task, methods and datasets are asked separately, as a
   completion ("Question: … Answer:") with the stop sequence at the end of the line. The excerpt is sent first
   and unchanged, so the server's prompt cache is reused (`cache_prompt`) and only each question is processed
   again. Keywords stay one JSON call with the schema restricted to topics and keywords.
2. **Better excerpt.** Method and data sections contribute the sentences that say what was done and with which
   data (cue words such as "we", "data", "sample", "collected", "survey"), not only their first lines.
3. **Grounding.** A kept term must occur in the paper: a method or dataset as a phrase, or with all its content
   words present. A task may be paraphrased, but must share at least half its content words with the abstract
   and title.
4. **Rejection rules.** Instruction verbs at the start ("present", "explore", "analyze", …), phrases containing
   "the paper", over six words, and generic words ("data", "general", "method", "presentation", …).
5. **One field per term.** Priority task > method > dataset > topic > keyword; a term the model gave for a
   higher field is not repeated in a lower one.
6. **Rules fill gaps.** When the model gives no usable method, names from a list of method names are taken if
   the text mentions them at least twice (longer names win over the words they contain). When it gives no
   usable dataset, sources named in a sentence are taken ("collected from Twitter", "the German federal election
   dataset"). Rules never replace a model answer.
7. **Frequency keywords** are unchanged, added when the model did not already give the word.

## Testing

- **Unit tests** (`tests/test_enrich.py`): grounding, instruction and generic rejection, the task rule, one field
  per term, rule extraction, the excerpt choice, the LLM fake asserting cache use, and the JSON call. They run
  without the LLM.
- **Own papers** (`tests/eval/eval_extraction.py`): the structures of the library papers are copied out of the
  data volume and scored against the hand labels by title. Lenient matching: at least 60% of the gold words are in
  the prediction and at least half of the prediction's words are in the gold. Reported per field: precision,
  recall, the number of terms, and "junk" (instruction-like terms). The comparison is with the committed
  `enrich.py`, run on the same papers and the same LLM.
- The labels were written by hand from the abstracts and data sections. They are a starting point for review,
  not a benchmark. One paper with no abstract is excluded.

## Results

See the table at the end of this file once the runs are complete.
