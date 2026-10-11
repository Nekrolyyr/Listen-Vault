# listen-vault

Private browser app at **https://listen.vault.akiba-z.ch** that turns scientific papers (PDF, including
scans) and article links into audio, chapter by chapter, and plays it next to the PDF.

- **Foreground:** `authelia` (passkey login) and `api` (FastAPI + the web app).
- **Background:** `worker` (fetch, OCR, structure, chapters, speech), `llm` (BLOOMZ-7b1 on
  llama.cpp), `grobid` (paper structure), `postgres`.
- CPU only. Everything is routed through the existing `traefik-docker` stack; no host ports are published.

Design, decisions and progress: [PLAN.md](PLAN.md). Measurements: [docs/benchmarks.md](docs/benchmarks.md).

## First deployment

All commands run on the vault host from `/home/doctor/docker/listen-vault`.

1. **DNS.** Point `listen.vault.akiba-z.ch` at the host (A record `144.2.97.77`, the address
   `jelly.vault.akiba-z.ch` uses). Let's Encrypt's HTTP challenge needs this before the first start.

2. **Docker access.** Your user is not in the `docker` group. Either add it once
   (`sudo usermod -aG docker doctor`, then log in again) or run the compose commands with `sudo`.
   Dockge (port 5001) also lists this folder as a stack.

3. **Data folders** (`/vault` is owned by root):
   ```sh
   sudo mkdir -p /vault/configs/listen-vault /vault/listen-vault
   sudo chown -R doctor:doctor /vault/configs/listen-vault /vault/listen-vault
   ```

4. **Secrets and the login user:**
   ```sh
   scripts/init-secrets.sh        # creates .env, random secrets, Authelia secret files
   scripts/add-user.sh            # asks for your password (min. 12 characters), stores an argon2 hash
   ```

5. **Models** (~5.5 GB on disk when done):
   ```sh
   # The converted model from the phase 1 benchmark is already here; copying it skips the 14 GB download:
   cp -r models/llm models/kokoro models/piper /vault/configs/listen-vault/models/
   scripts/fetch-models.sh        # fills in anything missing (and converts BLOOMZ if needed)
   ```

6. **Start:**
   ```sh
   docker compose up -d --build
   docker compose ps              # api, llm and postgres report healthy after about a minute
   ```

7. **Enroll your passkey** (once):
   1. Open https://listen.vault.akiba-z.ch and sign in with username `doctor` and your password.
   2. Choose **Register device**, then **Add**.
   3. Authelia asks for a one-time code. There is no e-mail, so read it from the host:
      ```sh
      tail -n 30 /vault/configs/listen-vault/authelia/notification.txt
      ```
   4. Name the passkey (e.g. "iPhone") and confirm on your device.

   From then on: **Sign in with a passkey**, then your password. The access rule is `two_factor`. To make the
   passkey alone enough, change `policy: two_factor` to `policy: one_factor` in
   `config/authelia/configuration.yml` and run `docker compose restart authelia`.

## Everyday use

- **Add:** drop PDFs on the library page, or paste a link (arXiv abstract pages and publisher pages that
  link their PDF are fetched as PDF).
- **Listen:** chapters become playable as they finish. The PDF follows the voice; scroll freely and
  press **Follow** to rejoin. The position is saved, so you can continue on another device.
- **Voice:** Settings → voice, speed, citations. Then use "Regenerate audio" on a
  document. Voice samples are in [docs/samples](docs/samples).
- **Pronunciation:** add words to `config/normalizer/lexicon.yaml`; it is read for every job.
- **Overview:** the first track reads the paper's abstract. Chapters are named as they are (the LLM only
  names untitled sections).
- **Keywords:** each paper gets a task, methods, datasets, topics and keywords, shown under its title. Papers
  added before this existed: Settings → Library → "Extract keywords for the library".
- **Figures and tables:** read with a short summary (from the table's cells, or from the text that cites the
  figure). Figure images are not described.
- **Ask:** the reader's "Ask about this paper" answers from the paper's passages, with the page of each
  citation. Answers take about 30 seconds.
- **Library:** the dashboard and filter column narrow the list. Groups, Graph and Timeline show related papers
  by their keywords; the Timeline shows a topic (or keyword) over the years. Topics are rebuilt from Settings.
- **Zotero:** set `ZOTERO_API_KEY` in `.env` (the numeric user ID is read from the key). Every item is imported and listed, and
  nothing is processed automatically: queue a paper from the library, or use "Process all listed papers" in
  Settings. PDFs need Zotero's file storage (not WebDAV).
- **Upgrading an existing library:** "Process from scratch" gives a paper the abstract overview and the exact
  sentence positions on the PDF.

Typical timing for a 15-page paper: OCR (scans only) under a minute, structure ~30 s, keywords and summaries
~2–4 min, audio ~12 min for ~35 min of speech.

## Operations

| Task | Command |
|---|---|
| Logs | `docker compose logs -f worker` (or `api`, `llm`, `authelia`) |
| Backup (keeps last 7) | `scripts/backup.sh` → `/vault/backups/listen-vault/<date>/` |
| Restore | `scripts/restore.sh /vault/backups/listen-vault/<date>` |
| Update images | edit the pinned tags in `compose.yaml`, then `docker compose up -d --build` |
| Tune CPU threads | `LLM_THREADS`, `TTS_THREADS`, `OCR_JOBS` in `.env` (8 = physical cores, fastest here) |

A nightly backup via cron: `0 3 * * * cd /home/doctor/docker/listen-vault && NO_AUDIO=1 scripts/backup.sh`.
The backup contains the Authelia database with your passkey registration, so keep it private.

## Development

```sh
uv venv --python 3.12 .venv && uv pip install -e "services/backend[worker,dev]" pgserver
.venv/bin/python -m pytest services/backend          # 128 tests, real PostgreSQL via pgserver
cd services/web && npm ci && npm run build            # type-check and build the web app
```

`tests/e2e/` holds the browser checks used during development:
- `passkey_e2e.py` drives a virtual passkey through Traefik and Authelia.
- `labels_to_file.py` turns the compose labels into a native Traefik config.
- `ui_check.py` exercises the library, reader and phone layout.
