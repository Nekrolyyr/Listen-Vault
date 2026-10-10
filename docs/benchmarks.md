# Phase 1 benchmarks (2026-10-10)

Host: AMD Ryzen 7 3800X — **8 physical cores / 16 threads** (Zen 2, AVX2, no AVX-512/VNNI), 30 GB RAM,
running alongside the other vault stacks. All numbers measured natively on this host.

## Text-to-speech

Text: a 107-word scientific paragraph (32.5 s of speech). RTF = synthesis time ÷ audio duration (lower is faster).

| Engine | Variant | Threads | RTF | Speed vs real time |
|---|---|---|---|---|
| Kokoro-82M | fp32 ONNX | **8** | **0.23** | 4.3× |
| Kokoro-82M | fp32 ONNX | 14 | 0.27 | 3.8× |
| Kokoro-82M | int8 ONNX | 8 | 0.30 | 3.3× |
| Kokoro-82M | int8 ONNX | 14 | 0.33 | 3.0× |
| Piper | en_US-lessac-medium | default | 0.028 | 35× |

- **fp32 at 8 threads is fastest.** int8 is slower on Zen 2 (no VNNI), and more threads than physical
  cores slow it down.
- **Real document:** "Attention Is All You Need" (arXiv 1706.03762) produced 37 min of audio in
  about 12 min of synthesis, including normalization and AAC encoding.
- **Intelligibility:** Whisper base.en transcripts of every sample differed from the source only in
  spelling (modelling/modeling) and number formatting (five → 5), so the effective word error rate
  is 0 for both engines. Naturalness has to be judged by ear; samples are in [`samples/`](samples/)
  (`af_heart` default, `af_bella`, `am_michael`, `bf_emma`, `bm_george`, and `piper-lessac` for comparison).

## Language model: BLOOMZ-7b1

`bigscience/bloomz-7b1` converted with llama.cpp `convert_hf_to_gguf.py` (BLOOM architecture is
supported), quantized to **Q4_K_M: 4.4 GiB on disk, fits the 6 GB container limit**.

`llama-bench`, prompt 1,024 tokens, generation 128 tokens:

| Threads | Prompt processing | Generation |
|---|---|---|
| **8** | **57.9 tok/s** | **5.7 tok/s** |
| 14 | 53.2 tok/s | 5.4 tok/s |

So a full 1,200-token chunk with a two-sentence answer takes about 40 s; typical section summaries
took 8–13 s.

### Prompt choice

Three phrasings were compared on real chapters. `"{text}\n\nSummarize the text above in 2 sentences:"`
gave the most consistent, faithful two-sentence summaries. A "main points" phrasing invented a claim
("outperforms … by orders of magnitude") that is not in the paper, so it was not used. BLOOMZ answers
are short by nature; every answer is checked (length, repetition, echoed instructions) and falls
back to an extractive summary when it fails.

## Decisions

| Setting | Value | Reason |
|---|---|---|
| `LLM_THREADS`, `LLM_THREADS_BATCH` | 8 | fastest for both prompt and generation |
| `TTS_THREADS` | 8 | fastest Kokoro configuration |
| Kokoro model | fp32 | faster than int8 on this CPU |
| Default voice | `af_heart` | change in Settings after listening to the samples |
