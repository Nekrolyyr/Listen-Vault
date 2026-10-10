# listen.vault.akiba-z.ch — Project Plan

A private, browser-based app that ingests scientific papers (PDF, including scanned ones) and article URLs, structures them into chapters with an overview using a local LLM, renders the full text to audio on CPU in the background, and streams the finished audio next to the original PDF. Single user, English only, self-hosted in Docker Compose on the existing vault host.

Progress is tracked in the checklist (section 11) and in Git history. Each completed step gets its own commit.

## 1. Requirements trace

Every user requirement, and where the plan covers it.

| # | Requirement (from the user) | Covered in |
|---|---|---|
| R1 | Browser app that digitalizes and ingests PDFs and website URLs | §6 Ingestion (incl. OCR for scanned PDFs), §8 UI |
| R2 | Creates audio versions of these files | §7 Speech, audio = full text + overview |
| R3 | Tailored to scientific papers and articles | §6 GROBID structure, speech normalization for citations, math, units |
| R4 | Ingestion creates chapters and overviews with a local LLM | §6.4 Chaptering |
| R5 | LLM is BLOOM 7b1 (confirmed) | §2, §6.4 (BLOOMZ-7b1, runtime fallback) |
| R6 | TTS model runs on CPU with good semantic quality | §7 Kokoro-82M + text normalization |
| R7 | TTS runs asynchronously as a background job; files saved and streamable when ready | §5 job model, §7 per-chapter files, §4 streaming |
| R8 | Foreground service + background service; background does all ML on this machine | §4 architecture |
| R9 | CPU only, Docker Compose, suitable containers | §3 budget, §4 services |
| R10 | Public URL listen.vault.akiba-z.ch | §4 routing, §10 DNS item |
| R11 | Reverse proxy: Traefik | §4 reuse existing Traefik v2.11 |
| R12 | Git, commit regularly | §11 checklist, one commit per phase |
| R13 | Project folder inside the Docker stacks folder | `/home/doctor/docker/listen-vault` (Dockge stack) |
| R14 | 16 CPU cores, 16 GB RAM for this task | §3 budget |
| R15 | English only | §2, §7 |
| R16 | Only the owner has access; a real login (e.g. passkey), not just HTTPS | §9 Authelia with passkey |
| R17 | GPL-3.0 acceptable | §7 Piper allowed as fallback |
| R18 | Manage papers in the UI: add papers/articles, see them | §8 Library |
| R19 | View the PDF while listening | §8 Reader with PDF.js, page follows playback |

## 2. Confirmed decisions

| Topic | Decision |
|---|---|
| Public URL | `https://listen.vault.akiba-z.ch` |
| Reverse proxy | **Existing Traefik v2.11** stack (`traefik-docker`), shared `traefik` network, cert resolver `le`, entrypoint `websecure`. No second proxy; it would collide on ports 80/443. |
| Access | Single user. **Authelia 4.39+** with **passkey (WebAuthn)** login, applied as Traefik `forwardAuth` middleware on every route. |
| LLM | **BLOOMZ-7b1** (`bigscience/bloomz-7b1`, instruction-tuned BLOOM 7.1B, BLOOM RAIL 1.0 license) |
| TTS | **Kokoro-82M** (ONNX, Apache-2.0) as default; **Piper** (GPL-3.0, accepted) as fallback |
| Language | English only |
| Hardware | 16 cores, 16 GB RAM for this stack (host has 30 GB total and also runs Plex, Jellyfin, Nextcloud) |
| Location | `/home/doctor/docker/listen-vault`, managed by Dockge like the other stacks |
| Persistent data | `/vault/configs/listen-vault/` (config, database, models) and `/vault/listen-vault/` (PDFs, text, audio), matching the existing `/vault/configs/<app>` convention |

## 3. Resource budget (16 cores, 16 GB)

Each container gets a `mem_limit` in Compose. The pipeline runs **one heavy stage at a time** (OCR/ingest → chaptering → TTS), so the LLM and TTS never compete for cores.

| Container | Memory limit | Threads | Notes |
|---|---|---|---|
| llm (llama.cpp, BLOOMZ-7b1 Q4_K_M) | 6 GB | 14 (configurable) | ~5 GB resident. Fallback runtime needs 8 GB (see §6.4). |
| worker (OCR, normalization, Kokoro ONNX, ffmpeg) | 2.5 GB | 14 (configurable) | Kokoro ~0.5–1 GB, Tesseract during OCR |
| grobid (CRF-only image, `-Xmx2g`) | 3 GB | 4 | The deep-learning GROBID image is heavier; the CRF image is enough for section structure |
| postgres | 0.5 GB | shared | also the job queue (§5) |
| api (FastAPI, serves the SPA) | 0.5 GB | shared | |
| authelia | 0.25 GB | shared | |
| **Total limits** | **~12.75 GB** | | ~3 GB headroom inside the 16 GB |

Two cores are left for the rest of the host by default. Thread counts are tuned after the benchmark (phase 1).

Rough throughput, to be replaced by measured numbers in phase 1: a 20-page paper (~10,000 words, ~65 min of speech) should take a few minutes for chaptering and roughly 20–40 minutes for TTS on 14 threads. The only published CPU figure for Kokoro is a real-time factor of ~0.57 on 4 cores; scaling to 14 threads is an assumption.

## 4. Architecture

```
Internet ── HTTPS ──► Traefik v2.11  (existing stack, network "traefik", resolver "le")
                        │
                        │ router: Host(listen.vault.akiba-z.ch)
                        │   PathPrefix(/auth)  → authelia          (no auth middleware)
                        │   everything else    → api   [middleware: authelia forwardAuth]
                        │
   network "traefik" ───┼──────────────── authelia, api           (only these two)
                        │
   network "listen" ────┴──── api, worker, llm, grobid, postgres, authelia
                              (bridge, no published host ports)

FOREGROUND tier                         BACKGROUND tier (all ML, CPU only)
  api      FastAPI                        worker   job runner
           - serves the SPA (static)               1 fetch / OCR  (OCRmyPDF + Tesseract eng)
           - library CRUD, uploads, URLs           2 structure    (GROBID | trafilatura)
           - job status                            3 chapter      (llm: BLOOMZ-7b1)
           - PDF + audio streaming (Range)         4 normalize    (speech text rules)
           - playback position                     5 synthesize   (Kokoro ONNX → Opus)
  authelia passkey login                  llm      llama.cpp server (OpenAI-compatible API)
                                          grobid   PDF → TEI XML with coordinates
  postgres metadata, jobs, playback       shared volumes: /vault/listen-vault/{pdf,text,audio}
```

Rules:
- **Foreground vs background:** `api` never runs ML. It writes a job row and returns. `worker`, `llm` and `grobid` form the background tier and do all the ML work on this machine.
- **No host ports** on any container. Traffic arrives only through Traefik, so nothing can skip Authelia from the LAN. (Several existing stacks publish ports; this one deliberately does not.)
- Services on both networks carry `traefik.docker.network=traefik`. Without it, Traefik may pick the wrong network, which is one cause of the 504s documented in `docs/troubleshooting-gateway-timeouts.md`.
- **Defense in depth:** other stacks share the `traefik` network and could reach `api` directly with a forged `Remote-User` header. A Traefik `headers` middleware therefore adds a secret `X-Listen-Proxy` header, and `api` rejects requests without it.
- **Streaming:** PDFs and Opus files are served with HTTP Range (206) so the browser can seek. Uploads are capped at 100 MB in `api`. Traefik v2.11 has a 60 s default read timeout on entrypoints. If very slow uploads hit it, the fix belongs in the shared Traefik config, which affects all stacks and needs your approval first.
- **Outbound access:** the worker fetches article URLs, so the `listen` network needs internet access. The fetcher blocks private and internal IP ranges, so a submitted URL cannot be used to probe the other containers (SSRF guard).

## 5. Job model

- The queue is **Postgres-backed** (`SELECT … FOR UPDATE SKIP LOCKED`), not Redis. A single worker on one machine does not need a separate broker, and this removes one container.
- States: `QUEUED → FETCHING → OCR (if needed) → STRUCTURING → CHAPTERING → SYNTHESIZING (n/m chapters) → READY | FAILED`.
- Every stage writes its output to disk and records completion. After a restart or crash, the job resumes at the first unfinished stage or chapter, not from scratch.
- Chapters become playable one by one: chapter 1 can be streamed while chapter 2 is still synthesizing.
- The UI polls job status every few seconds while a job is active. It can show a browser notification when the paper becomes READY.

## 6. Ingestion module

### 6.1 Fetch and digitalize
- **PDF upload:** checked by type and size. If the PDF has no usable text layer (very few extractable characters per page), it is OCR'd with **OCRmyPDF + Tesseract (eng)** using `--skip-text`. The OCR'd PDF replaces the original as the viewer copy, so search and highlighting also work for scans.
- **URL:** fetched with a timeout and size limit. If the response is a PDF, it follows the PDF path. Otherwise **trafilatura** extracts the main content, with a readability fallback. The page title, author and date are kept as metadata.

### 6.2 Structure
- **PDF → GROBID** `processFulltextDocument` with TEI coordinates. This yields title, authors, abstract, section headings, paragraphs and references, plus page coordinates for each paragraph.
- The coordinates give a **page map**: for each chapter and sentence, which PDF page it is on. The reader uses this to keep the PDF in sync with the audio.
- **Articles:** HTML `h2`/`h3` headings define sections. Without headings, the text is split at paragraph boundaries into ~1,500-word parts.

### 6.3 Clean-up for listening
- Removed: reference list, footnotes, running headers and footers, page numbers, acknowledgements (configurable), URLs and DOIs.
- Figures and tables are replaced with a short spoken note ("Figure 3 shows …"), using the caption.
- Line-break hyphenation and ligatures are repaired.

### 6.4 Chaptering and overview (BLOOMZ-7b1)
BLOOMZ-7b1 has a 2,048-token context window and tends to give short answers. The design uses it where it is reliable and does not depend on it for structure:
- **Chapters follow the document's own sections** (GROBID headings or HTML headings). The LLM does not invent the structure. Very short sections are merged; very long ones are split at paragraph boundaries.
- **Per chapter**, the LLM writes a 2–3 sentence spoken introduction. Input is capped at ~1,200 tokens per call, leaving room for the prompt and ~300 output tokens. Longer chapters are summarized in parts first (map), then combined (reduce).
- **Overview:** built hierarchically from the abstract and the chapter summaries, covering the research question, method, key results and conclusion. It becomes the first audio track.
- **Untitled article sections** get a short title from the LLM.
- **Output checks:** each LLM output is checked for length, repetition and leftover prompt text. One retry with a stricter prompt, then an **extractive fallback** (abstract or first sentences), so a job never fails because of the LLM.

**Runtime:** llama.cpp server with a GGUF converted from `bigscience/bloomz-7b1` (`convert_hf_to_gguf.py`, then Q4_K_M). Phase 1 must confirm that current llama.cpp loads BLOOM. My research did not confirm this.
**Fallback runtime:** CTranslate2 int8, which supports BLOOM, at about 7.5 GB RAM. It still fits if the llm limit goes up to 8 GB (total ~14.75 GB). Plain Transformers in bf16 needs ~14 GB for the model alone and does not fit.

## 7. Speech

### 7.1 Text normalization (key to semantic quality)
Before TTS, a rule-based normalizer rewrites scientific notation so that it reads naturally. Each rule is unit-tested.
- Citations: `[12]`, `[3–5]` are removed. `(Smith et al., 2020)` is removed, or read as "Smith and colleagues, 2020" (setting).
- Abbreviations: e.g., i.e., et al., Fig., Eq., Tab., approx., vs.
- Numbers and symbols: ranges, percentages, `p < 0.05` → "p less than 0.05", ±, ×, ≤, ≥, scientific notation, Greek letters (α → alpha), common units (ms, µm, kDa, °C).
- Math: simple inline expressions are spoken. Display equations become "an equation is shown here".
- Acronyms: a pronunciation lexicon (spell out "DNA", say "NASA" as a word), editable as a config file.

### 7.2 Synthesis
- **Kokoro-82M via `kokoro-onnx`** (ONNX Runtime, CPU). English voices (American and British), selectable in settings. The default voice is picked in phase 1 by listening.
- Text is synthesized **per sentence** and joined with short pauses. Each chapter starts with its title and intro.
- **Piper** is the fallback engine (faster, lower quality). It can be switched per job.
- Output: per chapter, mono **Opus** at ~48 kbps via ffmpeg (about 20 MB per hour). Sentence start times are stored, so the reader can highlight the current sentence and follow the PDF page.

## 8. User interface

Stack: Vite + TypeScript SPA, built into static files that `api` serves (same origin, no CORS). PDF rendering with **PDF.js**. Works on desktop and phone.

**Library**
- All papers and articles with title, authors, source (PDF/URL), date added, duration, and status with live progress per stage.
- **Add:** drag-and-drop or choose PDF files, or paste a URL. Adding queues the job immediately.
- Search and filter by title and author. Sort by date added or by last listened.
- Per item: open, re-run (for example after changing the voice), delete (removes PDF, text and audio), mark as listened.

**Reader / player (per paper)**
- **Split view:** the PDF (PDF.js) on one side, chapters and the player on the other. On a phone, PDF and chapters are tabs above a fixed player bar.
- **PDF follows playback:** the viewer scrolls to the page of the sentence being read. Clicking a chapter jumps both the audio and the PDF. A "follow" toggle lets you browse the PDF freely without losing your place.
- For URL articles, the cleaned text is shown instead of a PDF, with the current sentence highlighted.
- Player: play/pause, ±15 s, speed 0.75–2×, chapter list with the overview first. Playback position is saved on the server and restored on any device. The Media Session API gives lock-screen and headset controls on a phone.

## 9. Security and login

- **Authelia 4.39+** in this stack, using the file-based user backend with one user. SQLite storage under `/vault/configs/listen-vault/authelia`.
- **Passkey login:** Authelia supports passwordless passkey login since 4.39. Under the `two_factor` policy, it asks for the password as well after a passkey login.
  - **Recommended:** `two_factor` (passkey + password) for this public site.
  - **Option:** `one_factor` with passkey only, if you prefer convenience.
- Access control: `default_policy: deny`, with a single rule for `listen.vault.akiba-z.ch`.
- **Portal on a subpath** (`https://listen.vault.akiba-z.ch/auth`), so only one DNS name is needed. If the subpath setup causes problems in phase 2, use `auth.vault.akiba-z.ch`, which needs a second DNS record.
- **Passkey enrollment without email:** the filesystem notifier writes the one-time enrollment code to a file on the host. No SMTP setup is needed.
- Brute-force protection: Authelia regulation, banning after repeated failed logins.
- Secrets (session secret, storage encryption key, JWT secret, `X-Listen-Proxy` value) live in `.env` or files under `config/authelia/secrets/`. They are git-ignored and documented in `.env.example`.

## 10. Repository layout

```
/home/doctor/docker/listen-vault/        # Dockge stack folder, Git repo
  compose.yaml                           # all services; attaches to external "traefik" network
  .env.example
  PLAN.md
  README.md
  config/
    authelia/configuration.yml           # secrets/ and users file are git-ignored
    normalizer/lexicon.yaml              # pronunciation lexicon
  services/
    api/                                 # FastAPI + built SPA
    worker/                              # pipeline stages
    web/                                 # SPA source (Vite + TypeScript)
  scripts/
    fetch-models.sh                      # downloads and converts BLOOMZ, Kokoro, Piper voices
  docs/
    benchmarks.md                        # phase 1 results
```

Data stays outside the repo:
- `/vault/configs/listen-vault/{authelia,postgres,models}`
- `/vault/listen-vault/{pdf,text,audio}`

## 11. Phases and progress

Each phase ends with a commit (prefixes `docs:`, `feat:`, `infra:`, `fix:`, `chore:`).

- [x] Research and initial plan
- [x] Project folder and Git repository, moved into `/home/doctor/docker`
- [x] Plan validated against all user inputs and the existing host setup
- [ ] **1. Benchmark spike:** convert BLOOMZ-7b1 to GGUF and confirm llama.cpp loads it (else CTranslate2). Measure tokens/s, Kokoro RTF on 14 threads, peak RAM, and GROBID on 3 sample papers (one scanned). Pick a voice. Write up in `docs/benchmarks.md`.
- [ ] **2. Stack skeleton and login:** `compose.yaml`, networks, memory limits, Authelia with passkey on `/auth`, forwardAuth and proxy-secret header, a placeholder page behind login, reachable over HTTPS.
- [ ] **3. Data layer:** Postgres schema (documents, chapters, sentences, jobs, playback), migrations, api skeleton with the auth checks.
- [ ] **4. Ingestion:** upload and URL intake, OCR detection, GROBID, trafilatura, clean-up, page map. Tests on sample papers.
- [ ] **5. Chaptering:** llm service, prompts, output checks, extractive fallback.
- [ ] **6. Speech:** normalizer with unit tests, Kokoro synthesis, Opus encoding, sentence timings, Piper fallback.
- [ ] **7. API:** library CRUD, job status, Range streaming for PDF and audio, playback position.
- [ ] **8. Frontend:** library, add flow, reader with PDF.js sync, player, mobile layout.
- [ ] **9. Hardening:** healthchecks, restart policies, backup of Postgres and `/vault/listen-vault`, upload limits, job retries, log rotation.
- [ ] **10. End-to-end:** one born-digital paper, one scanned paper and one article URL, from upload to playback with PDF sync, logging in from a fresh browser with a passkey.

## 12. Needed from you before phase 2

1. **DNS:** `listen.vault.akiba-z.ch` does not resolve yet, and there is no wildcard for `*.vault.akiba-z.ch`. Add an A record to `144.2.97.77` (the address `jelly.vault.akiba-z.ch` resolves to) or a CNAME to `jelly.vault.akiba-z.ch`. Let's Encrypt's HTTP challenge needs it.
2. **Data folders:** `/vault` is owned by root. Run once:
   `sudo mkdir -p /vault/configs/listen-vault /vault/listen-vault && sudo chown -R doctor:doctor /vault/configs/listen-vault /vault/listen-vault`
3. **Login policy:** passkey + password (recommended) or passkey only.

## 13. Verify during implementation

- llama.cpp support for the BLOOM architecture (phase 1). Fallback: CTranslate2 int8.
- The exact Authelia 4.39 setting for passkey login and the subpath portal setup (phase 2).
- Kokoro's real-time factor on this CPU at 14 threads (phase 1).

## Sources

- Kokoro CPU benchmark (community): https://prismix.dev/news/5dbcdf80e32e
- Kokoro local guide: https://aireiter.com/blog/kokoro-82m-tts-local-guide
- Piper overview: https://offlinetts.com/tts/piper/
- BLOOMZ-7b1 model card: https://huggingface.co/bigscience/bloomz-7b1
- bloomz.cpp (older ggml BLOOM port): https://mymirror.duckdns.org/nmntz/bloomz.cpp
- Authelia passkeys: https://www.authelia.com/overview/authentication/security-key/
- Authelia authentication overview: https://www.authelia.com/overview/authentication/
- CPU LLM benchmark (4-core Xeon): https://dev.to/manoir_yantai_f22f01340f0/-running-llms-on-cpu-in-2026-real-benchmarks-from-a-4-core-xeon-server-a-4-4pll
