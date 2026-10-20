# Plan: closing the gaps (Zotero removal, scheduled topics, per-user audio, storage quota, terms, onboarding)

Status: plan, not started. Follows `docs/multi-user-plan.md` ("Known gaps"). Branch: `feature/multi-user`.
Each phase ends with a commit as a checkpoint and a green test run.

## Decisions

1. **Audio settings are per user.** Voice, speed, engine and citation handling belong to the account. (Reading
   speed for speed-reading is per user too.)
2. **Audio is made only for the user who added the paper, with that user's audio settings.** Nobody else can hear it
   and nobody else's audio is made from it. A paper added by a second user reuses the processing of the first (no
   fetch, OCR, structure, keywords or chapter plan again) and gets its own audio, made with the second user's settings.
   The first user's audio is not touched.
3. **Audio is made automatically when a paper is ready for the user who added it.** There is no switch for it.
   A user can regenerate their audio (with their current settings) or delete it.
4. **Each user has a storage limit of 1 GB** (editable per user by an administrator, like the page quota).
5. **Zotero is removed completely.**
6. **Topic rebuilds run daily at 11:00 local time, only when a paper finished since the last rebuild.** The time is an
   admin setting (default 11:00). The manual trigger and the other maintenance tasks are in the admin page.
7. **The terms change** (new version, see phase 5), so every user is asked to accept again.
8. **Onboarding** runs once after sign-in: welcome, terms (must be accepted), a picture tutorial, done.

## Phase 1: remove Zotero

Delete, do not disable:

- Code: `listen/zotero.py`, `tests/test_zotero.py`, the Zotero branches in `worker/main.py` (job kind `zotero_sync`,
  `schedule()`), `worker/pipeline.py` (the title and author rules for linked items and the PDF attachment lookup),
  `api/main.py` (`/api/zotero*`, the `source=zotero` and `not_zotero` filters, `from_zotero`), `config.py`
  (`zotero_*`), the library queries that join `zotero_links`.
- Migration `010_drop_zotero.sql`: drop `zotero_links` and `zotero_state`; delete documents with status `catalogued`
  that were never processed (they only held the Zotero record); processed papers stay as ordinary papers.
- Web: the Zotero section in Settings, the "From Zotero" source filter (`listing.ts`, `query.ts`, `api.ts`), the
  "Queue" and "Process all listed" actions for catalogued items.
- Config and docs: `ZOTERO_*` in `.env.template`, `docker-compose.yml`, `README.md`; `docs/zotero-plan.md` moves to
  `docs/archive/` with a note that the feature was removed.

**Gate:** `grep -ri zotero` finds nothing outside the archive and the migration; tests pass; the migration runs on a
copy of a database that has Zotero rows.

## Phase 2: topic rebuild at 11:00 when there is something new

- Worker loop: once a minute, compare the local time (`TZ`) with the admin setting `topics_rebuild_at` (default
  `11:00`). If that time has passed today, no rebuild has run since, and a paper finished since the last rebuild,
  enqueue one `topics` job.
- "Finished since": `EXISTS (SELECT 1 FROM jobs WHERE kind = 'document' AND state = 'done' AND finished_at >
  last_rebuild)`. `last_rebuild` is the setting `topics_last_rebuilt`, written when a topics job finishes.
- A day the worker was down runs at the next check (at most once a day, after 11:00).
- Admin page, section **Maintenance**, the only place for these actions:
  - Rebuild topics now.
  - Place papers in areas; place all papers again; extract keywords for the library.
  - Each shows when it last ran; the topics line shows the next scheduled run and whether new papers are waiting.
  - The daily time, edited as `HH:MM` and validated.
- Settings and Home lose these buttons. Home keeps the read-only note "An administrator places them."

**Gate:** fake-clock tests: nothing before 11:00; one rebuild at 11:00 after a paper finished; none when nothing changed;
a missed day runs once at the next check; the manual endpoint still works; non-admins get 403.

## Phase 3: per-user audio settings and audio per user

### 3.1 Settings per user

- Migration: `user_settings(user_id, key, value jsonb, PK(user_id, key))`. The current `settings` values go to the
  legacy owner. `settings` keeps only admin values (`topics_rebuild_at`, `topics_last_rebuilt`, `registration_open`).
- Audio keys: `engine`, `voice`, `speech_speed`, `citations`. Also `reading_wpm`.
- Defaults stay in `appsettings.py`. The API reads and writes the signed-in user's settings only.
- Settings page: section **Audio** (voice, speed, engine, citations). A note: "Your audio keeps the voice it was made
  with. Regenerate it to use these settings."

### 3.2 Narrations

A narration is the audio of one paper for one user. It is made only for a user who has the paper in their library.

- Tables (`011_narrations.sql`):
  - `narrations(id, document_id, user_id, state, settings jsonb, duration_s, bytes, error, created_at, updated_at)`,
    unique on `(document_id, user_id)`. `settings` is a snapshot of the audio settings used.
  - `narration_chapters(narration_id, idx, audio_file, duration_s, bytes, PK(narration_id, idx))`.
  - `narration_sentences(narration_id, chapter_idx, idx, text, page, start_s, end_s, rects, PK(...))`.
  - `chapters` keeps text, titles and the chapter plan only. `chapters.audio_file`, `chapters.duration_s` and
    `sentences` move to the narration tables.
- Files: `DATA_DIR/docs/<document>/narrations/<user-id>/<idx>.m4a`. Deleting a narration deletes its folder.
- The narration is removed when the user removes the paper from their library, or deletes the audio.

### 3.3 When audio is made

- A user adds a paper (upload or link). If the paper is new, the document job runs (fetch, OCR, structure, keywords,
  chapter plan) and then the user's `narrate` job runs with the user's audio settings.
- A user adds a paper that another user already has. The document job is skipped (the paper is ready, or still being
  processed by the first user's job, in which case the narrate job waits for it). Only the second user's `narrate` job
  runs, with the second user's settings. The first user's narration is not touched.
- "Regenerate audio" (per paper, for the current user) replaces that user's narration with one made from current
  settings. No other user is affected.
- A narrate job uses the same worker, the same per-user queue limit (five open jobs) and the fair-share claim, so one
  user's audio does not hold up others.

### 3.4 Reader and API

- `GET /api/documents/{id}/chapters/{idx}/audio` and `.../sentences` use the signed-in user's narration. Without one:
  404 with `{"code": "no_audio"}`, and the reader offers "Make audio" (queues a narrate job for this user).
- The library list shows the audio state of the user's own narration (ready chapters, in progress, not made).

### 3.5 Migration

- Existing audio and sentence timings become the narration of the legacy owner, which added every existing paper.
  Files are moved, not regenerated.

**Gate:** two users with different voices add the same paper: one document job, two narrations with different voices;
each user sees their own highlighting; regenerating one user's audio leaves the other's files and timings unchanged;
removing the paper from one library removes only that narration; on a copy of the production database, an upgraded
paper still plays and highlights the same sentences.

## Phase 4: storage quota of 1 GB per user

- **What counts:** the PDF of every paper in the user's library (a shared PDF counts for each library that holds it,
  as the page quota does), and the bytes of the user's own narrations. Text, structure and keywords are small and are
  not counted. The terms say so.
- **Default and limit:** `users.storage_quota_bytes`, default 1 GiB (1,073,741,824 bytes). An administrator can change
  it per user (admin page and `cli set-quota --storage`).
- **Columns:** `documents.pdf_bytes` (set on upload and when the stored copy is hashed), `narration_chapters.bytes`.
  Usage = sum of `pdf_bytes` over the library + sum of the user's narration bytes. No separate counter to drift.
- **Enforcement:**
  - Upload: refused with `{"code": "storage_full"}` when the PDF does not fit. A link is refused only when the user is
    already over the limit (its size is unknown until it is read), as with pages.
  - Narrate job: before each chapter, if the user is at the limit, the narration stops with state `failed` and the
    message "Storage limit reached: delete papers or audio to continue". Chapters already made stay playable.
  - Regenerating removes the old narration first, so its bytes are freed before the new one is made.
- **Freed by:** removing a paper from the library (its PDF counts for that user only), deleting audio, deleting the
  account.
- **Admin page:** storage used and limit per user (bytes, no titles), editable limit, and the total stored on disk.
- **Account page:** a bar, "312 MB of 1 GB used".
- **Size estimate:** about 0.5 MB of AAC audio per minute of speech, so roughly 20 MB for a 35-minute paper. Measure
  this on real papers in phase 3 and correct the figure in the terms and the plan.

**Gate:** tests for usage (PDF counted per holder, narration bytes counted per user); upload refused at the limit;
narration stops at the limit and keeps finished chapters; regenerating frees the old bytes; limit change is immediate.

## Phase 5: terms of use (new version)

The current file `config/terms/2026-10-20.md` stays as it is; it is what the existing accounts accepted. Its section 5
says that the processed audio is one shared copy, which phase 3 changes. A new version is needed, and the terms
are locked once published (the hash must match), so the new text goes in a new file, published when the phases are
merged. Every user is then asked to accept it again.

The draft of the new version is in the appendix below. Changes against the current version:

- Section 4 "Audio": what is made, from whose settings, how long it is kept, that it can contain mistakes.
- Section 5 "Storage and page quota": the 1 GB limit, what counts, and what happens at the limit.
- Section 6: deleting a paper or regenerating audio.
- The audio rules of the current version 2026-10-20 (its section 5, shared papers) are removed by the draft. Only the
  operator-access statement (section 2 of the draft) remains as the privacy rule for uploads.
- The bracketed parts still need the operator's name (in the introduction), and a legal review.

**Gate:** the file loads as the current version; the old version is still accepted by the existing accounts and the
banner asks for the new one; the terms text the app shows matches the file.

## Phase 6: onboarding

Shown once after sign-in when `users.onboarded_at` is empty. The migration sets it for existing accounts.

Steps, one screen each, with Back, Next and a progress dot:

1. **Welcome:** what the app does, in two sentences.
2. **Terms:** the current text in a scrollable box. The operator-access sentence (section 2) is shown in bold above it.
   Next stays disabled until the box is read to the end and "I accept" is ticked. Declining signs the user out.
3. **Picture tutorial:** five steps, each with an illustration and two lines of text:
   1. Add a paper: drop a PDF or paste a link.
   2. Wait: the paper shows its progress; the first track is the overview.
   3. Listen: the player and the PDF follow each other; Follow rejoins the text.
   4. Find your way: "Ask about this paper", and areas and topics on the Library map.
   5. Your settings: audio settings are yours, your audio is made from them, and the storage bar is on the account page.
4. **Done:** a button to the library. "Show this tutorial again" is also on the account page.

Illustrations are SVG files in `services/web/public/onboarding/`, in the app's colours, with no real titles or personal
data. The text is in the app, not in the images, so it can be translated and read by screen readers. Each image has
alt text.

- API: `me` gains `onboarded`; `POST /api/account/onboarded` sets it; `DELETE` resets it (replay).
- The router sends a signed-in, not-onboarded user to `/welcome` for every page except terms and sign-out.
- Phone width: each step fits 390 px; images scale down; no horizontal scroll.

**Gate:** a new account goes through all steps and lands in the library; a refresh resumes at the same step; the terms
step cannot be passed without accepting; replay works; checked in a real browser with screenshots at 390 px and desktop
width before merging.

## Order

1. Phase 1 (Zotero removal), so later phases do not touch that code.
2. Phase 2 (scheduled topics, admin Maintenance section).
3. Phase 3 (per-user audio). The largest change; the migration is tested on a copy of the database first.
4. Phase 4 (storage quota), which needs the narration sizes from phase 3.
5. Phase 5 (terms), which describes what phases 3 and 4 do.
6. Phase 6 (onboarding), which shows the terms and the tutorial.

## Risks

- **CPU time** is spent per user: a shared paper costs audio time for each user who adds it. The queue limits and fair
  claim keep one user from blocking others; the host still makes one narration at a time.
- **Disk:** audio per user multiplies the storage. The 1 GB limit bounds it per user; the total is shown to admins.
  Backups grow with it.
- **Migration:** existing audio files and sentence timings are moved exactly, or highlighting breaks. Tested on a copy.
- **Terms:** still a draft, and now a larger change. Onboarding puts them in front of every new user.
- **Tutorial pictures** must not show real titles or personal data.

## Appendix: draft of the next terms version

Draft only. Save as `config/terms/<date-of-publication>.md`. Do not place it in `config/terms/` before the phases are
merged: a file there becomes the current version at once.

```markdown
# Terms of service and privacy statement (version 2026-11-xx)

> DRAFT. Replace the bracketed parts and have the text reviewed before other people register.

These terms apply to listen-vault, a service that turns scientific papers and web articles into audio.
The service is run by **[operator name and address]** (the "operator").

## 1. Your PDFs are visible to the operator

**The operator can read the PDF files you upload.** The service stores each PDF so that it can show the PDF
next to the audio, extract its text and make the audio. The operator can therefore see the files, and can
open them when needed to keep the service running, to fix an error you report, or to meet a legal duty.
The operator does not sell or publish your files. Do not upload anything you want to keep private from the
operator.

## 2. How your text is processed

Text and audio are made only on the operator's own computer, by models that run there. No text from your papers
is sent to other companies. The one exception is the sending of account emails (see section 3).

## 3. Your account and the email addresses

To register, the service keeps your email address, a password in a form that cannot be read back (a hash),
the time you accepted these terms, and the times you signed in. Confirmation emails are sent through an
email provider that receives your address and the confirmation link. The provider is used only for account emails.

## 4. Audio

The service reads each paper you add aloud, with the voice and speed in your audio settings, and keeps the audio
until you delete it, regenerate it, or remove the paper. The audio is made by a computer. It can mispronounce
words, read formulas and tables badly, and leave things out. It is not a substitute for the paper.

## 5. Storage and page quota

Each account has a storage limit of 1 GB and a page quota (300 pages by default). Storage counts the PDF of each paper
in your library (a shared PDF counts for every library that holds it) and the audio made for you. Text and other
processing data are not counted. Pages count the pages of the papers in your library; a web article counts about one
page per 500 words. When a limit is reached you cannot add papers, and audio stops being made until you delete papers
or audio. The operator can change either limit.

## 6. Deleting papers, audio and accounts

- Removing a paper removes it from your library at once, together with the audio made for you. The stored copy is
  deleted when no library holds it.
- Regenerating audio replaces the old audio.
- You can download your data (your library summary, your PDFs and the list of your audio) from the account page.
- You can delete your account from the account page. Your library, audio, listening progress and questions are
  deleted with it. Papers that nobody else holds are deleted too.
- **Backups.** The operator keeps backups of the database and the files. A deleted paper, audio or account can remain
  in a backup for up to seven days.

## 7. Contact

Questions about these terms or your data: **Akiba Zwick, adatech09@gmail.com**.
```
