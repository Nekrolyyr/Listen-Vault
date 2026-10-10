# listen.vault.akiba-z.ch — Project Plan

A private, browser-based app that ingests scientific papers (PDF) and article URLs, builds chapters and an overview with a local LLM, renders the text to audio on CPU in the background, and streams the finished audio. Single user, English only, fully self-hosted in Docker Compose.

Progress is tracked in the checklist at the end of this file and in Git history. Each completed step is committed separately.

## 1. Confirmed decisions

| Topic | Decision |
|---|---|
| Public URL | `https://listen.vault.akiba-z.ch` |
| Reverse proxy | **Traefik v3** (TLS via Let's Encrypt, ACME) |
| Access | **Single user** only, login required on every route. Authentication via **passkey (WebAuthn)**, enforced by **Authelia** as a Traefik `forwardAuth` middleware. No public sign-up. |
| LLM | **BLOOMZ-7b1** (confirmed as the requested model), served via llama.cpp (GGUF, Q4_K_M) behind an OpenAI-compatible API. Swappable through config. |
| TTS | **Kokoro-82M** (ONNX, Apache-2.0) as default. **Piper** (GPL-3.0, accepted) as selectable fallback. |
| Language | **English only.** No multilingual handling in ingestion, TTS, or prompts. |
| Hardware | **16 CPU cores, 16 GB RAM**, CPU only |
| Source control | Git, repository at `/home/doctor/listen-vault`, regular commits |

## 2. Hardware budget (16 cores, 16 GB)

The machine has to host all services at once, so memory and CPU are split explicitly. Each container gets a memory limit in Compose.

| Service | Memory (approx.) | CPU share |
|---|---|---|
| llm (BLOOMZ-7b1 Q4_K_M via llama.cpp) | ~5 GB | 8 threads during chaptering |
| worker: Kokoro TTS (ONNX) | ~1 GB | 6 threads during synthesis |
| grobid (JVM, `-Xmx2g`) | ~3 GB | 2 cores during ingestion |
| postgres | ~0.5 GB | shared |
| redis | ~0.1 GB | shared |
| api, web, traefik, authelia | ~1 GB total | shared |
| **Total** | **~11 GB**, about 5 GB headroom | |

Rules that follow from this:
- Heavy stages run **one at a time per worker** (ingest → chapter → TTS). No concurrent LLM and TTS jobs, to avoid memory and CPU contention.
- BLOOMZ-7b1 has a short context window. Chaptering uses map-reduce over chunks sized to the model's context, not a single long prompt.
- Thread counts are configuration values, so they can be tuned after the benchmark spike.

## 3. Architecture

```
Browser ── HTTPS ──► Traefik (TLS, routing)
                        │
                        ├── forwardAuth ──► Authelia (passkey login, session cookie)
                        │
                        ├── /        ──► web      (SPA: library, reader, player)
                        └── /api     ──► api      (FastAPI, FOREGROUND service)
                                           │
                    ┌──────────────────────┼─────────────────────┐
                    ▼                      ▼                     ▼
               postgres                 redis            volume: pdfs/, audio/
           (documents, chapters,   (job queue)          (original PDFs, WAV→Opus,
            jobs, playback state)        │                  text extracts)
                                         ▼
                              worker (BACKGROUND service, CPU ML)
                               1. ingest   GROBID (PDF) | trafilatura (URL)
                               2. chapter  LLM map-reduce → chapters + overview
                               3. tts      Kokoro ONNX per chapter → Opus
                               4. publish  mark READY, store page map
                                         │            │             │
                                         ▼            ▼             ▼
                                       llm         grobid         (kokoro in worker)
                                   llama.cpp      GROBID
```

Notes:
- **Foreground vs background:** `api` only accepts requests and serves files (Range-capable). Everything CPU-heavy runs in `worker`, so the UI stays responsive.
- **Authentication:** Traefik sends every request to Authelia first. `api` also accepts only requests carrying the identity header Authelia sets, so it cannot be reached around the proxy.
- **Job states:** `QUEUED → INGESTING → CHAPTERING → SYNTHESIZING (progress per chapter) → READY | FAILED`. The UI polls a job-status endpoint.
- **Streaming:** each chapter is a separate Opus file, so playback can start after the first chapter is ready. Seeking uses HTTP Range (206).

## 4. Docker and repository layout

Everything Docker-related lives under `Docker/`, one stack per folder. Application source lives in `services/`.

```
listen-vault/
  PLAN.md
  README.md
  .gitignore
  Docker/
    README.md              # overview of the stacks and start order
    compose.yaml           # top-level: includes the stacks below
    traefik/               # traefik.yml, dynamic config, ACME storage (not committed)
    authelia/              # configuration.yml, users file template (secrets not committed)
    llm/                   # llama.cpp server, model download script
    grobid/                # GROBID service
    app/                   # postgres, redis, api, worker, web
  services/
    api/                   # FastAPI
    worker/                # ingestion, chaptering, TTS pipeline
    web/                   # frontend
  models/                  # downloaded GGUF and ONNX files (git-ignored)
```

Secrets, certificates, the Authelia user file, and model weights are never committed. `.gitignore` covers them, and `.env.example` files document the required values.

## 5. Ingestion

- **PDF (scientific):** GROBID returns TEI XML with title, authors, abstract, sections, and references. Clean-up rules:
  - drop references, footnotes, page headers and footers;
  - replace figure and table bodies with a short spoken note, keeping captions;
  - convert inline math to spoken form where simple, and skip equations otherwise.
- **URL (article):** trafilatura for main-content extraction, readability as fallback.
- Both produce one normalized document model: metadata → sections → paragraphs, plus a **page map** for PDFs (which page each section starts on). The page map drives the PDF view during playback.

## 6. User interface

**Library**
- List of papers and articles with title, source type (PDF/URL), date added, length, and status (queued, processing, ready, failed).
- Add a paper by uploading a PDF or pasting a URL. Adding starts the job.
- Delete a paper (removes PDF, text, and audio), and re-run a failed or outdated job.

**Reader / player (per paper)**
- Chapter list with overview at the top.
- Audio player with play/pause, seek, speed, and chapter navigation. Playback position is saved to postgres and restored on next visit.
- **PDF viewer beside the player** (PDF.js). Selecting a chapter jumps the PDF to that chapter's start page, and the current chapter is highlighted as playback moves on. For URL articles, the extracted text is shown instead of a PDF.
- The PDF is streamed from the same volume, behind the same login.

## 7. Phased plan with commit checkpoints

Each phase ends with a commit. Commit messages use a short prefix (`docs:`, `feat:`, `infra:`, `fix:`) and describe what changed.

1. **Benchmark spike** — measure Kokoro ONNX and BLOOMZ-7b1 Q4_K_M on this machine (RTF, tokens/sec, peak memory). Listen to samples. Record results in `docs/benchmarks.md`.
2. **Infrastructure skeleton** — `Docker/` layout, Traefik with TLS, Authelia with passkey login, one-user setup, `forwardAuth` working end to end.
3. **Data layer** — postgres schema, redis, migrations, and the api skeleton with auth header check.
4. **Ingestion** — GROBID service, PDF and URL extraction, normalized model, page map, tests on sample papers.
5. **Chaptering** — llama.cpp service, map-reduce chapter and overview prompts, JSON validation with retries.
6. **TTS worker** — Kokoro ONNX, sentence chunking, WAV → Opus per chapter, progress reporting.
7. **API endpoints** — library CRUD, upload and URL intake, job status, Range-capable audio and PDF streaming, playback state.
8. **Frontend** — library, add-paper flow, reader with PDF viewer and player, playback resume.
9. **Deployment hardening** — memory limits, restart policies, backups for postgres and the content volume, rate limits, upload size and type checks.
10. **End-to-end test** — ingest a real paper and a URL, confirm playback, PDF sync, and login from a fresh browser with a passkey.

## 8. Progress checklist

- [x] Research and initial plan (`docs: initial research and architecture plan`)
- [x] Project folder and Git repository created
- [ ] 1. Benchmark spike
- [ ] 2. Infrastructure skeleton (Traefik, Authelia)
- [ ] 3. Data layer and api skeleton
- [ ] 4. Ingestion
- [ ] 5. Chaptering
- [ ] 6. TTS worker
- [ ] 7. API endpoints
- [ ] 8. Frontend
- [ ] 9. Deployment hardening
- [ ] 10. End-to-end test

## 9. Open items

- Confirm that the Authelia version chosen supports passwordless passkey login as the first factor. If not, passkey will be the second factor after a password. This is checked during phase 2.
- Confirm the exact GGUF build of BLOOMZ-7b1 and its quantization during the benchmark spike. The model is an older release with a 2k context window, so the chunk size follows from the measured context.
