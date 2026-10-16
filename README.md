# listen-vault

A browser app that turns scientific papers (PDF, including scans) and article links into audio, chapter
by chapter, and plays the audio next to the PDF. It runs on your own computer with Docker and CPU only.

The stack, all started by `docker compose`:

- **caddy**: the login page (HTTP basic auth) and the only port, bound to `127.0.0.1`.
- **api**: FastAPI and the web app.
- **worker**: fetch, OCR, structure, chapters and speech.
- **llm**: BLOOMZ-7b1 on llama.cpp, used for keywords, summaries and questions.
- **grobid**: paper structure.
- **postgres**: library, jobs and playback positions.

Design notes are in [docs/](docs/). Measurements are in [docs/benchmarks.md](docs/benchmarks.md).

## Requirements

- Docker with Compose v2 (`docker compose version`). Your user must be able to run Docker without `sudo`
  (usually by being in the `docker` group).
- About 8 GB of RAM free while it runs. The LLM alone needs about 6 GB.
- About 6 GB of disk for the models. Converting BLOOMZ from the original weights needs about 35 GB free for
  a while.

## First start

Run these from the project folder.

1. **Create `.env` and the secrets:**
   ```sh
   scripts/init-secrets.sh
   ```
   This copies `.env.template` to `.env`, generates `LISTEN_PROXY_SECRET` and `POSTGRES_PASSWORD`, sets
   `LISTEN_USER` to your user name, and creates `data/` and `config/caddy/`. Open `.env` to change anything
   else (the login name, the time zone, CPU threads).

2. **Set the login password** (see [Authentication](#authentication)):
   ```sh
   scripts/add-user.sh
   ```

3. **Download the models** (fills in whatever is missing in `./models`):
   ```sh
   scripts/fetch-models.sh
   ```
   The repository's `models/` folder may already contain some of them. Nothing is downloaded twice.

4. **Start the stack:**
   ```sh
   docker compose up -d --build
   docker compose ps          # api, llm and postgres report "healthy" after about a minute
   ```
   The first build takes several minutes (web app, Python packages, ffmpeg and tesseract).

5. **Open http://localhost:8080** and sign in with `LISTEN_USER` and the password from step 2.

If the folder contains another compose file named `compose.yaml`, Docker uses that one instead. In that
case add `-f docker-compose.yml` to every `docker compose` command, for example
`docker compose -f docker-compose.yml up -d --build`. The scripts already do this.

## Authentication

The web app has a single user and no sign-up. Access works like this:

- **Login:** the browser asks for a username and password (HTTP basic auth, checked by Caddy). The username
  is `LISTEN_USER` in `.env`. The password is stored only as a bcrypt hash in `config/caddy/users.caddy`,
  which `scripts/add-user.sh` writes. The password itself is never saved.
- **Sign out:** there is no sign-out button, because basic auth has no logout. Your browser keeps the login
  until you close the window or clear the site's data.
- **Change the password:** run `scripts/add-user.sh` again, then `docker compose restart caddy`.
- **Change the username:** set `LISTEN_USER` in `.env`, run `scripts/add-user.sh` again, then
  `docker compose up -d && docker compose restart caddy`. The api only accepts the user named in `LISTEN_USER`.
- **Internal check:** after Caddy has checked the login, it forwards the user name in a `Remote-User` header
  and the shared `LISTEN_PROXY_SECRET` in an `X-Listen-Proxy` header. The api rejects every request without
  both. Its state-changing requests also need an `X-Requested-With` header, which the web app sends. A
  page on another site cannot set that header, so it cannot make your browser change anything.
- **Network access:** port 8080 is bound to `127.0.0.1`, so only this computer can reach the app. The login
  is plain HTTP. Do not publish the port on your network or the internet as it is. If you need remote
  access, put your own HTTPS reverse proxy in front of it.
- **Rotate the shared secret:** replace `LISTEN_PROXY_SECRET` in `.env` with a new random value, then run
  `docker compose up -d`. Caddy and the api read it when they start.

`.env`, `config/caddy/users.caddy`, `data/` and `models/` are git-ignored. Keep them private.

## Using the app

- **Add:** drop PDFs on the library page, or paste a link (arXiv abstract pages and publisher pages that
  link their PDF are fetched as PDF).
- **Listen:** chapters become playable as they finish. The PDF follows the voice; scroll freely and
  press **Follow** to rejoin. The position is saved, so you can continue on another device.
- **Voice:** Settings → voice, speed, citations. Then use "Regenerate audio" on a document. Voice samples
  are in [docs/samples](docs/samples).
- **Pronunciation:** add words to `config/normalizer/lexicon.yaml`. It is read for every job.
- **Overview:** the first track reads the paper's abstract. Chapters keep the names the paper gives them;
  the LLM only names untitled sections.
- **Keywords:** each paper gets a task, methods, datasets, topics and keywords, shown under its title.
  For papers added earlier: Settings → Library → "Extract keywords for the library".
- **Figures and tables:** read with a short summary, taken from the table's cells or from the text that
  cites the figure. Figure images are not described.
- **Ask:** the reader's "Ask about this paper" answers from the paper's passages, with the page of each
  citation. Answers take about 30 seconds.
- **Library:** the dashboard and filter column narrow the list. Groups, Graph and Timeline show related
  papers by their keywords. The Timeline shows a topic (or keyword) over the years. Topics are rebuilt from
  Settings.
- **Zotero (optional):** set `ZOTERO_API_KEY` in `.env` (the numeric user ID is read from the key). Every
  item is imported and listed, but nothing is processed automatically: queue a paper from the library, or
  use "Process all listed papers" in Settings. PDFs need Zotero's file storage (not WebDAV).
- **Upgrading an existing library:** "Process from scratch" gives a paper the abstract overview and the
  exact sentence positions on the PDF.

Typical timing for a 15-page paper: OCR (scans only) under a minute, structure about 30 s, keywords and
summaries 2–4 min, audio about 12 min for 35 min of speech on an 8-core CPU.

## Configuration

Every setting is in `.env` (see `.env.template` for the full list). Empty values fall back to the defaults in
`docker-compose.yml`.

| Variable | Purpose |
|---|---|
| `LISTEN_USER` | Login name |
| `LISTEN_PORT` | Port on 127.0.0.1 (default 8080) |
| `TZ` | Time zone for logs and timestamps (default UTC) |
| `MAX_UPLOAD_MB` | Largest upload (default 100) |
| `LLM_GPU_LAYERS` | Language model layers on the NVIDIA GPU (default 99 = all, about 5.6 GB VRAM). Set 0 to run on CPU only. |
| `LLM_THREADS`, `LLM_THREADS_BATCH`, `TTS_THREADS`, `OCR_JOBS` | CPU threads per task (default 8). Use your physical core count. |
| `ZOTERO_*` | Optional Zotero import (see above) |

Folders on the host:

| Folder | Contents |
|---|---|
| `data/library/` | Documents, text, audio (mounted as `/data`) |
| `data/postgres/` | Database files |
| `models/` | Model files (read-only in the containers) |
| `config/normalizer/` | Pronunciation lexicon |
| `config/caddy/` | `Caddyfile` and the login file written by `add-user.sh` |

## Operations

| Task | Command |
|---|---|
| Logs | `docker compose logs -f worker` (or `api`, `llm`, `caddy`) |
| Stop (keeps all data) | `docker compose down` |
| Update after a `git pull` | `docker compose up -d --build` |
| Backup (keeps the last 7) | `scripts/backup.sh` → `./backups/<date>/` |
| Restore | `scripts/restore.sh backups/<date>` |

A nightly backup via cron, for example:
`0 3 * * * cd /path/to/listen-vault && NO_AUDIO=1 scripts/backup.sh`

Backups contain the database and your documents. Keep them private.

## Development

```sh
uv venv --python 3.12 .venv && uv pip install -e "services/backend[worker,dev]" pgserver
.venv/bin/python -m pytest services/backend          # real PostgreSQL via pgserver
cd services/web && npm ci && npm run build            # type-check and build the web app
```

`tests/e2e/ui_check.py` drives the library, the reader and the phone layout in Chromium against an api
running on `127.0.0.1:8000`. It needs Playwright.
