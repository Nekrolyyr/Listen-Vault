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

`llama-bench`, prompt 1,024 tokens, generation 128 tokens (CPU only):

| Threads | Prompt processing | Generation |
|---|---|---|
| **8** | **57.9 tok/s** | **5.7 tok/s** |
| 14 | 53.2 tok/s | 5.4 tok/s |

So a full 1,200-token chunk with a two-sentence answer takes about 40 s on CPU; typical section summaries
took 8–13 s.

### GPU offload (GTX 1660, 6 GB VRAM)

llama-server, CUDA build `server-cuda-b11515`, 1,126-token prompt, 128 generated tokens, end-of-sequence ignored
so the full length is generated. Each run started from a loaded model after a warm-up request.

| `LLM_GPU_LAYERS` | Prompt processing | Generation | Peak VRAM |
|---|---|---|---|
| 0 (CPU) | 34 tok/s | 5.1 tok/s | 266 MiB |
| 20 (previous default) | 77 tok/s | 12.4 tok/s | 3,998 MiB |
| **99 (all, default)** | **81 tok/s** | **39.9 tok/s** | **5,584 MiB** |

All 31 layers fit with about 560 MiB free. Generation is about 8× faster than CPU. A 1,126-token chunk
with a two-sentence answer (about 50 tokens) now takes about 15 s (14 s prompt processing, about 1 s
generation), not about 40 s. The context stays at 2,048 tokens: a smaller context or quantized
KV cache would be needed to free more VRAM.

Extraction quality (`services/backend/tests/eval/eval_extraction.py`, 9 hand-labelled papers, two runs each):

| Run | Task recall (GPU / CPU) | Methods recall (GPU / CPU) | Datasets recall (GPU / CPU) | Eval time (GPU / CPU) |
|---|---|---|---|---|
| 1 | 0.06 / 0.17 | 0.16 / 0.24 | 0.50 / 0.50 | 144 s / 506 s |
| 2 | 0.06 / 0.11 | 0.20 / 0.24 | 0.50 / 0.50 | 150 s / 498 s |

Methods and datasets match within noise. Task terms recall lower on the GPU in both runs (18 gold terms,
so the gap is a few terms). Extraction is about 3.4× faster. Set `LLM_GPU_LAYERS=0` to go back to CPU if
the task terms matter more than speed.

### Prompt choice

Three phrasings were compared on real chapters. `"{text}\n\nSummarize the text above in 2 sentences:"`
gave the most consistent, faithful two-sentence summaries. A "main points" phrasing invented a claim
("outperforms … by orders of magnitude") that is not in the paper, so it was not used. BLOOMZ answers
are short by nature; every answer is checked (length, repetition, echoed instructions) and falls
back to an extractive summary when it fails.

## Decisions

| Setting | Value | Reason |
|---|---|---|
| `LLM_GPU_LAYERS` | 99 | all layers on the GPU: 8× the CPU generation speed, fits in 6 GB VRAM |
| `LLM_THREADS`, `LLM_THREADS_BATCH` | 8 | fastest for both prompt and generation on CPU |
| `TTS_THREADS` | 8 | fastest Kokoro configuration |
| Kokoro model | fp32 | faster than int8 on this CPU |
| Default voice | `af_heart` | change in Settings after listening to the samples |
