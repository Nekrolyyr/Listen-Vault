# listen-vault

A browser app that turns scientific papers (PDF, including scans) and article links into audio, chapter
by chapter, and plays the audio next to the PDF. It runs on your own computer with Docker and CPU only.

The stack, all started by `docker compose`:

- **caddy**: the only port, bound to `127.0.0.1`. It passes requests on to the api.
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
   This copies `.env.template` to `.env`, generates `LISTEN_PROXY_SECRET` and `POSTGRES_PASSWORD`, and
   creates `data/`. Open `.env` to change anything else (the public address, mail, the time zone, CPU threads).

2. **Make the first administrator** (see [Accounts](#accounts)):
   ```sh
   scripts/make-admin.sh you@example.org
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

5. **Open http://localhost:8080** and sign in with the account from step 2. Other people can create their own
   accounts from the sign-in page, unless you close sign-up in the administration page.

If the folder contains another compose file named `compose.yaml`, Docker uses that one instead. In that
case add `-f docker-compose.yml` to every `docker compose` command, for example
`docker compose -f docker-compose.yml up -d --build`. The scripts already do this.

## Accounts

Anyone can create an account on the sign-in page (unless an administrator closes sign-up). Each account has:

- **Sign-in:** email and password. Passwords are stored only as scrypt hashes. A session cookie keeps the
  browser signed in for 30 days; **Sign out** on the account page ends it, and changing the password ends
  the other sessions.
- **Confirmation email:** if `SMTP_HOST` is set in `.env`, new accounts confirm their address by email before
  they can sign in. Without it, accounts are confirmed at once, and the api logs a warning. For Infomaniak
  mail: `SMTP_HOST=mail.infomaniak.com`, `SMTP_PORT=587`, and the mailbox address and its generated password.
- **Terms of service:** the text is in `config/terms/` (one file per version). New accounts accept it when they
  register, and everyone is asked again when a new version is published. The terms say that the operator can
  read the PDFs uploaded (see [docs/multi-user-plan.md](docs/multi-user-plan.md) for the decisions behind them).
  The text is a draft: have it reviewed, and replace the bracketed parts, before other people register.
- **Page quota:** each account has a page quota (300 pages by default). The pages of the papers in its
  library count against it. An administrator changes it in the administration page.
- **Own library:** every account has its own library, listening progress and questions. A PDF that is added
  by several people is stored once.
- **Download or delete:** the account page downloads the library summary and PDFs as a zip, and deletes the
  account. Papers that no other library holds are deleted with it.

**Topics** are rebuilt every day at 11:00 (`TZ`, editable in the administration page), when a paper has finished
since the last rebuild. Areas are placed as papers finish. The administration page's Maintenance section starts
the other tasks by hand and shows when each last ran.

**Administrators** (the Admin link) see the accounts with their counts (never the titles of papers), change
quotas, disable accounts, make other administrators, sign out an account everywhere, close or open sign-up, and
read a log of these changes. Library-wide maintenance (areas, topics, keywords) is done by an
administrator too.

Operator commands, run from the folder that holds `docker-compose.yml`:

| Command | What it does |
|---|---|
| `scripts/make-admin.sh you@example.org` | Makes an account an administrator (creates it if needed) and sets its password. The first account of an existing library is claimed this way. |
| `docker compose exec api python -m listen.cli set-quota you@example.org 500` | Sets an account's page quota from the command line. |
| `docker compose exec api python -m listen.cli dedup-existing` | Lists PDFs stored twice from before accounts existed; add `--apply` to merge them. |

- **Internal check:** Caddy adds the shared `LISTEN_PROXY_SECRET` to every request, and the api rejects requests
  without it, so only Caddy can reach the api. State-changing requests also need an `X-Requested-With` header,
  which a page on another site cannot set.
- **Network access:** port 8080 is bound to `127.0.0.1`. Do not publish it on your network or the internet as it
  is: the connection is plain HTTP. For remote access, put your own HTTPS reverse proxy in front of it and set
  `PUBLIC_URL` to its address.

`.env`, `data/` and `models/` are git-ignored. Keep them private.

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
- **Upgrading an existing library:** "Process from scratch" gives a paper the abstract overview and the
  exact sentence positions on the PDF.

Typical timing for a 15-page paper: OCR (scans only) under a minute, structure about 30 s, keywords and
summaries 2–4 min, audio about 12 min for 35 min of speech on an 8-core CPU.

## Configuration

Every setting is in `.env` (see `.env.template` for the full list). Empty values fall back to the defaults in
`docker-compose.yml`.

| Variable | Purpose |
|---|---|
| `PUBLIC_URL` | Address of the app, used in links in emails |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Mail for confirmation emails (see Accounts) |
| `LISTEN_PORT` | Port on 127.0.0.1 (default 8080) |
| `TZ` | Time zone for logs and timestamps (default UTC) |
| `MAX_UPLOAD_MB` | Largest upload (default 100) |
| `LLM_GPU_LAYERS` | Language model layers on the NVIDIA GPU (default 99 = all, about 5.6 GB VRAM). Set 0 to run on CPU only. |
| `LLM_THREADS`, `LLM_THREADS_BATCH`, `TTS_THREADS`, `OCR_JOBS` | CPU threads per task (default 8). Use your physical core count. |

Folders on the host:

| Folder | Contents |
|---|---|
| `data/library/` | Documents, text, audio (mounted as `/data`) |
| `data/postgres/` | Database files |
| `models/` | Model files (read-only in the containers) |
| `config/normalizer/` | Pronunciation lexicon |
| `config/caddy/` | `Caddyfile` |
| `config/terms/` | The terms of service, one file per version |

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
