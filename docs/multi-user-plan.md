# Multi-user plan: self-registration, page quotas, shared documents

Status: implemented on `feature/multi-user` (phases 1 to 8, see "Implementation status" at the end). Turns the
single-user app into one where people register themselves, accept terms of service, each have a library measured
in pages, and can share stored copies of the same paper.

## What exists today (and what it blocks)

| Area | Today | Why it blocks multi-user |
|---|---|---|
| Login | Caddy basic auth; `api` accepts only `LISTEN_USER` (`api/security.py`) | No registration, one identity, no roles |
| Documents | One global `documents` table, no owner. Files live in `DATA_DIR/docs/<id>/` | Every user sees and changes every paper |
| Library views | Facets, overview, areas, topics, graph and timeline query all documents (`api/main.py`) | Another user's papers would appear in every view |
| Topics | `worker/topics.py` clusters all non-failed papers | Topic membership must follow each library |
| Settings | Global `settings` table (`appsettings.py`): voice, speed, citations | Per-user preferences needed, and audio depends on voice |
| Zotero | One `ZOTERO_API_KEY` in `.env` | Each user needs their own key and library |
| Worker | One queue, no user on jobs | Quota and fair scheduling need a user on every job |
| Routes | 35 routes in `api/main.py` | Every one needs an owner check |

## Decisions to confirm first

The rest of the plan assumes these. Recommendations are marked.

1. **Sign-up:** email and password, email verification, rate limits on register and login. Option: an invite-only
   switch in admin settings. **Recommended:** verification on, invite-only switch available, off by default.
2. **Quota unit:** pages of the PDF (or of the extracted text for URLs), counted per library entry.
   **Recommended:** the quota counts pages in the user's own library, even when the stored copy is shared. Sharing
   saves disk, not quota. Default for new users: 300 pages, editable per user.
3. **Shared copies:** a user gets access to a stored copy only by supplying the same file (upload) or the same URL
   (fetch). Matching by SHA-256 of the file. **Recommended:** no access to a copy without a matching upload, so a
   user never reads a paper they did not add themselves.
4. **Audio and voice:** audio depends on the voice. **Recommended:** audio is cached per voice
   (`audio/<voice>/<idx>.m4a`). Text, structure, keywords and areas are shared; audio is generated once per voice.
5. **Admins:** one or more admin accounts, set by a CLI command (`scripts/make-admin.sh`) on the host. No admin can
   be created through the web form.
6. **Zotero keys:** one key per user, stored encrypted with a key from `.env` (not in the database in clear text).
   The global `ZOTERO_API_KEY` stays as the default for the first user only during migration.

## Phase 1: data model

Migration `009_users.sql` (and following files):

- `users`: id, email (unique, lower-case), password_hash (argon2id), display_name, is_admin, is_disabled,
  email_verified_at, page_quota, created_at.
- `sessions`: token hash, user_id, created_at, expires_at, last_seen_at. Server-side, so a logout or an admin
  disable takes effect at once.
- `terms_versions`: version, text_sha256, published_at. `user_consents`: user_id, version, accepted_at. A user must
  accept the current version to upload; a new version asks again.
- `library_entries`: user_id, document_id, added_at, pages (counted at add time), listened, last_listened_at,
  playback position, Zotero link, and per-user topic overrides and manual area pins. Replaces the per-document
  columns `listened` and `last_listened_at` and the per-document `playback` row.
- `documents` keeps the shared work: title, metadata, `content_sha256` (unique for PDFs), `pages`, status, the
  structure, chapters, keywords and areas. `owner_id` is removed.
- `quota_usage` is not stored. Usage is the sum of `pages` in the user's library entries plus reservations of
  uploads in progress (see Phase 5).
- Per-user settings: `user_settings(user_id, key, value)`. Global admin settings stay in `settings`.

Backfill: the existing documents get a library entry for the user named in `LISTEN_USER` (created on first start
with the email from `.env`, `LISTEN_USER_EMAIL`). Nothing is deleted or moved on disk in this phase.

**Gate:** migration runs on a copy of the production database; every existing paper appears once in the owner's
library; row counts match before and after.

## Phase 2: accounts and sessions

- New `api/auth.py`: register, verify email, log in, log out, change password, sessions with a `HttpOnly`,
  `Secure`, `SameSite=Strict` cookie. Passwords hashed with argon2id.
- `AuthMiddleware` resolves the session cookie to a user and sets `request.state.user`. The `X-Requested-With`
  check and the `X-Listen-Proxy` secret stay (defense in depth, see `docs/` architecture notes).
- Caddy stops doing basic auth. It keeps TLS, the static route and the proxy header. `scripts/add-user.sh` is
  replaced by `scripts/make-admin.sh`.
- Email sending: an SMTP setting in `.env` (`SMTP_*`). Without it, verification is off and sign-up is limited to
  the invite-only switch.
  - **Provider:** Infomaniak, which hosts the domain. Its SMTP submission is `mail.infomaniak.com`, port 587 with
    STARTTLS (or 465 with implicit TLS), with authentication. The username is the full mailbox address, and the
    password is the one generated for that mailbox (not the account login). Source:
    [Infomaniak mail ports and protocols](https://www.infomaniak.com/en/support/faq/468/understanding-mail-ports-and-protocols).
  - **Open check before Phase 2:** the documentation found describes mailbox sending, not a transactional relay. Ask
    Infomaniak support, or check the [mail service knowledge base](https://www.infomaniak.com/en/support/faq/admin2/email-service?page=7),
    whether a mailbox can send verification mail to any address at the expected volume (a few per sign-up), and what
    the sending limits are. If it cannot, a transactional relay is the fallback.
  - **DNS for the sending domain:** SPF, DKIM and DMARC records for the domain, set in Infomaniak's DNS. Test that
    verification mail reaches Gmail, Outlook and a second address before verification is turned on.
  - **Privacy:** Infomaniak receives the addresses and verification links. Name it in the terms as a processor.
- Rate limits on register, login and verification (per IP and per email), in Postgres, no new container.

**Gate:** a second account cannot read the first account's documents; log-out invalidates the cookie; a disabled
account gets 401 on the next request; the existing tests in `tests/test_api.py` and `test_settings.py` are
updated for sessions and still pass.

## Phase 3: terms of service and privacy

- The terms are a file in `config/terms/<version>.md`. Registration shows them and needs an explicit checkbox
  ("I have read and accept the terms and the privacy statement"). The acceptance is stored with the version and
  time.
- Required content, at least:
  - **Operator access:** the operator of the service can read the PDFs that users upload. Say so in plain words,
    next to the upload button as well as in the terms.
  - **Processing:** text is processed only by the local models on the host. No text is sent to other companies.
    (This must stay true when the optimisation plan adds a model, see `docs/optimisation-plan.md`. OpenAlex, if
    added, gets titles and DOIs only.)
  - **Shared copies:** the same file uploaded by several people is stored once. Each person's library and quota are
    their own, and a person can read a stored copy only because they uploaded the same file.
  - **Deletion:** deleting a paper removes it from the user's library at once. The stored copy is deleted when no
    library refers to it. Nightly backups keep copies for up to 7 days after that.
  - **Rights:** users upload only papers they have the right to store and read.
  - **Retention and contact:** what is kept, for how long, and who to write to.
- Account pages: export of the library (JSON and the uploaded PDFs) and account deletion. Deletion removes the
  user's library entries, sessions and settings, and the copies nobody else uses.
- This plan does not replace legal advice. Have the terms reviewed before people other than you register,
  especially if any user is in the EU or Switzerland (data protection law applies to the data of people you do not
  know).

**Gate:** registration cannot complete without accepting the current version; a changed terms version asks every
user again; export and deletion work on a test account, and the deleted account's PDFs are gone from `data/`.

## Phase 4: scope everything to the user

- Every route in `api/main.py` takes the user from the request and filters by `library_entries.user_id`. A paper
  outside the user's library answers **404**, not 403, so a user cannot tell that someone else has it.
- Views: facets, overview, areas, topics, graph and timeline count only the user's own library.
- Topics: rebuilt per user from that user's library entries. The shared keyword data is reused, so the cost is
  the clustering only. Topic names may repeat across users; the rows are keyed by user.
- Playback, listened state and Zotero links move to the library entry.
- Settings: voice, speed and citations are per user. The worker reads them from the job's user.
- Zotero: sync and queue operate on the user's key and only add to that user's library.
- Worker: jobs carry `user_id`. The worker iterates over all documents only for shared steps (for example
  keyword backfill), not for topics.

**Gate:** an isolation test suite (new `tests/test_isolation.py`) creates two users and checks every route for
both directions: user A cannot read, change, queue, delete or find user B's papers, nor see them in any count.

## Phase 5: page quota

- The page count comes from the PDF at upload (a PDF reader such as `pypdf`; no OCR needed). For a URL, the
  count comes from the fetched PDF, or from the extracted text in pages of 500 words when the source is HTML.
- Enforcement: at upload or URL submission, reserve the pages (`reservations` row). The reservation is released
  when the job fails or when the entry is created, and the entry is charged when the job completes. If the
  count does not fit, the request is refused with the remaining quota in the message.
- A shared copy counts against the quota of the user adding it, the same as an own copy.
- Zotero: queueing a Zotero item reserves the pages it will need (known from Zotero metadata, or from the PDF after
  download). Items over the quota stay listed but are not queued.
- The admin console edits the quota (Phase 7). The web app shows usage, for example "212 of 300 pages".

**Gate:** tests for reserve, release on failure, refusal at the limit, and a quota change taking effect at once.

## Phase 6: shared storage (deduplication)

- New layout: `DATA_DIR/blobs/<sha256>.pdf` for uploaded PDFs, `DATA_DIR/docs/<document-id>/` for the processed work.
  `DocPaths` keeps the same API, so the worker code changes little.
- Upload: compute SHA-256 while streaming. If a document with that hash exists, add a library entry and return the
  same response shape as a new upload (no hint that someone else had it).
- Deleting a library entry: when the last entry for a document is removed, the document, its blob and its audio
  are deleted. The check and the delete run in one transaction, so a concurrent add cannot lose a file.
- Audio: `audio/<voice>/<idx>.m4a`. Generating a voice that another user already made is free.
- Existing data: a one-time script hashes the current `source.pdf` files and fills `content_sha256`. Papers that
  have the same hash are merged, with the duplicate's library entry moved to the surviving document. Run it on a
  copy first.

**Gate:** two users uploading the same file produce one `blobs` file, one processed document and two library
entries; deleting one entry keeps the file for the other user; deleting both removes the files; disk use is
measured before and after on the existing library.

## Phase 7: admin console

- `/admin` (only for `is_admin`): list of users with email, pages used, quota, last login, status, and documents.
- Actions: edit a user's page quota, disable or re-enable an account, end all sessions of an account, promote or
  demote admins (not your own last admin), switch invite-only registration on or off, edit the terms version.
- Every change is written to `admin_audit` (who, what, old value, new value, time).
- The admin console does not show paper titles or PDF text of other users. It shows counts only, so the operator's
  access stays limited to what the terms describe. Reading a user's PDF is possible only through the same paths the
  user has, and any such access is logged.

**Gate:** quota change visible to the user at once; disabled account blocked; audit row for each action; the admin
page does not reveal titles of papers in other users' libraries.

## Phase 8: operations and limits

- **Worker capacity:** one LLM and one worker mean papers from many users are processed one after another. Add a
  per-user limit on queued jobs (for example 5), and take jobs in round-robin order by user, so one large upload
  does not hold up everyone else.
- **Upload size and rate:** keep `MAX_UPLOAD_MB`; add a per-user limit on uploads per hour.
- **Backups:** `scripts/backup.sh` must include `blobs/` and the new tables. `scripts/restore.sh` must restore the
  users and sessions tables. A restore signs everyone out, which is acceptable and should be noted.
- **Logs:** no paper text or PDF names in logs beyond the document id; email addresses are logged only on failed
  login as a hashed value.
- **Readme and docs:** the Authentication section is rewritten for accounts. The terms and the privacy statement
  are linked from the sign-in page.

## Order and effort

Phases 1 and 2 are the critical path: nothing else can be tested with more than one user until both are done.
Phase 3 can be written in parallel with Phase 2, but the terms should not be published before Phase 4 is done,
because the terms describe the isolation and deletion behaviour. Phases 5 and 6 can be built together.

| Phase | Depends on | Rough size |
|---|---|---|
| 1 Data model | none | medium |
| 2 Accounts | 1 | large |
| 3 Terms | 2 (text in parallel) | small code, large review |
| 4 Scope | 1 | large (35 routes, views, worker) |
| 5 Quota | 4 | medium |
| 6 Shared storage | 4 | medium |
| 7 Admin console | 2, 5 | medium |
| 8 Operations | 6 | medium |

## Risks

- **Isolation bugs** are the main risk. Phase 4's test suite is the gate for everything after it; do not open
  registration to others before it passes.
- **Shared copies and the existence of a file:** a successful upload that is answered faster when the copy already
  exists can show that someone else has the paper. Keep the response and the timing the same for both cases, or
  accept the risk in the terms.
- **Deleted copies in backups** stay for up to 7 days. The terms say so.
- **Operator access** is a trust matter, not a technical one. The plan makes it visible and limited, but the
  terms must say what the operator does with the PDFs.
- **Hardware:** the host has one GPU and CPU budget (see the optimisation plan). More users mean longer queues, and
  the queue limits in Phase 8 are a fairness measure, not extra capacity.

## Open items

- Infomaniak sending limits and whether a mailbox is suitable for verification mail (Phase 2, see above).
- Default page quota (300 is a placeholder).
- Whether any user will be outside the EU or Switzerland (affects the terms review).
- Whether Zotero is offered to other users at all, or only to the owner (it is per user in this plan).

## Implementation status

Done, with the tests in `services/backend/tests/` (`test_accounts.py`, `test_library_scope.py`,
`test_admin_account.py`, plus the updated existing tests):

- **Phase 1 and 4:** `migrations/009_accounts.sql`. Papers are stored once; `library_entries` holds each user's
  library, listened state and playback. Every document query goes through the signed-in user's library, so a paper
  outside it is a 404. The existing library moves to a placeholder owner, claimed with `scripts/make-admin.sh`.
- **Phase 2:** accounts, scrypt password hashes, server-side sessions, verification email, rate limits, password
  change, `listen/api/security.py` (session cookie instead of the `Remote-User` header). Caddy no longer does
  basic auth. The password hash is scrypt from the standard library, not argon2id (no argon2 package is installed).
- **Phase 3:** `config/terms/2026-10-20.md` (a draft, to be reviewed), accepted at registration and asked again for a
  new version. Adding papers needs the current version. Account export (zip) and deletion.
- **Phase 5:** page quota: pages counted at upload, for web articles from the word count, for Zotero PDFs when
  processed. Refused when the quota is full. A link is refused only when the quota is already full, since its size
  is not known until it is read (see "Known gaps").
- **Phase 6:** one stored copy per PDF (SHA-256) and per web address; `dedup-existing` merges older copies.
- **Phase 7:** `/admin` with user counts, quota, disable, admin role, sign out everywhere, sign-up switch, audit log.
  It shows no paper titles of other users.
- **Phase 8 (backend):** at most 5 open jobs per user; the worker gives the next turn to the user with the fewest
  running jobs. Backups need no change: `pg_dump` includes the new tables and `data/library/docs` holds the stored
  papers.

Web app: sign-in, registration with the terms, confirmation, terms page, account page (quota, password, download,
delete) and the administration page (`services/web/src/accounts.ts`). Admin-only controls are hidden from others.

## Known gaps (decide before opening registration to other people)

These go against the plan, and each one is a deliberate shortcut:

- **Voice and speech settings are shared.** Audio is generated once per paper, so a per-user voice needs audio per
  voice. Settings are global for now.
- **Topics, areas and manual placements are shared.** Topic clusters are built from all papers, and a topic's terms can
  come from papers the user does not have. The lists show only the user's own paper counts and papers, but the names
  of the topics are shared. Per-user topics are the fix.
- **Zotero is one account's.** Items go into the library of the administrator who runs the sync. Per-user Zotero keys
  are not implemented.
- **A link's page quota is checked before the page is read.** A large web article can take the account over the quota
  by one paper.
- **Titles can be edited by any member** of a paper's library, because the title is shared.
- **Existence of a paper:** a PDF that is already stored answers with the same status as a new one, but not in the same
  time. Someone with a file can tell whether it was added before. The terms should say so.
- **Operator access** is a trust matter. The terms say the operator can read the PDFs; the code does not limit it.
- **Legal review** of the terms is needed before other people register (see the draft's brackets).
- **`compose.yaml`** in this folder is a stale Authelia setup that is git-ignored. Docker prefers it over
  `docker-compose.yml` when you run `docker compose` without `-f`. The scripts pass `-f docker-compose.yml`.

