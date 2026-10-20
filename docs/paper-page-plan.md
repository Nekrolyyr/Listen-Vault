# Plan: overhaul the paper page

Status: plan, not started. Scope: the paper page (`services/web/src/reader.ts`, its CSS in `style.css`), the tags
and read marker it needs in the API. Branch: `feature/multi-user`. Each phase ends with a commit and a green run of
`npm run build` (web) and the backend tests.

## Where it is now

- Header (`reader.ts`, `head`): back link, title (one line, truncated), a line with authors, year and status, then
  "Source" and "Make audio" buttons. It always takes its full height.
- Keyword strip (`keywordBar`) sits under the header, always visible, up to 14 chips.
- Content: two panes on desktop (document beside a side pane holding "Now reading", the chapter list, the splitter and
  the question box). On phones a two-tab bar (Document, Chapters) hides the side pane. Questions sit under the chapters.
- Player bar: `Listen | Read` mode switch with text labels only. Read mode swaps the audio controls for the read
  controls (`.read-bar`), so the bar changes height between the two modes.
- Controls are small (about 28–36 px); the seek sliders have the browser's default thumb.
- There is no way to mark a paper as read from the page. The paper menu has "Mark as listened", which sets the per-user
  flag `library_entries.listened` (migration `009_accounts.sql`).
- There are no custom tags. The only tags are the automatic area and topic chips on the paper row.

## Decisions

1. **"Read" is the existing per-user flag, relabelled.** The metadata button writes `library_entries.listened`; no new
   column. The UI copy changes from "listened" to "read" everywhere (paper menu, status chip, filter, area page
   counts), so that it matches the button. The mode "Read" (speed reading) is a different thing, and the two names
   will sit on the same screen. Open question: is that clash acceptable, or should the flag be called something else
   (e.g. "Done")? The plan assumes "read".
2. **Tags are per user.** The library is per user (`library_entries`), and tags are a way of organising your own
   library. A shared paper can carry different tags for different people. Table `library_tags(user_id, document_id,
   tag, created_at)`, primary key `(user_id, document_id, tag)`, cascade on document delete.
3. **Tag rules.** Trimmed, lower-cased, at most 32 characters, letters, digits, spaces, `-` and `_` only, at most 20
   tags per paper. Duplicates are ignored, not errors.
4. **Collapsed header keeps only basic navigation:** back to the library, the title on one line, the status dot and an
   expand button. Prev/next paper is not included (it needs the listing context, which the page does not have).
   The collapsed state is remembered per viewer (`storedValue`, like the reading mode).
5. **Three columns when the room is there, tabs otherwise.** Using a container query on the content area: at 1180 px
   or more the content shows Document | Chapters | Questions side by side; below that the three become tabs. Phones
   and tablets get the tabs. Columns keep a splitter between them.
6. **The player bar is one height in both modes.** The same grid rows are reserved in listen and read mode; the
   read controls fill the row the audio seek bar uses. Measured, not assumed.
7. **Bigger touch targets, same visual weight.** Buttons and icon buttons at least 44 × 44 px hit area (the drawn
   icon can stay smaller). Sliders get a 24 px thumb and a 10 px track on touch screens.

## Phase 1: API for tags and read marker

- Migration `013_library_tags.sql`: the table above.
- `api/main.py`:
  - `GET /api/documents/{id}` and the list response: add `tags: string[]` (sorted) to `_doc_out`, for the signed-in
    user only.
  - `PUT /api/documents/{id}/tags` with `{ "tags": [...] }`: replaces the user's tags for the paper after validation.
    Returns the normalised list. 404 if the user does not have the paper in their library. 422 on a bad tag.
  - The read marker needs no endpoint: the existing "listened" action is reused. Rename only its copy in the web app.
- Filters (optional, not in this plan's gate): a `tag` query parameter on `/api/documents`, so the Papers list can
  filter by tag later. Left out unless asked.
- `web/src/api.ts`: `tags: string[]` on `Doc`; `api.setTags(id, tags)`.

**Gate:** backend tests for: normalisation (case, spaces, length, characters, the 20-tag limit, duplicates); another
user's tags never appear; a paper not in the user's library returns 404; migration applies to a copy of the database.

## Phase 2: collapsible header with metadata

Rebuild `head` as two parts in `reader.ts`:

- **Bar (always shown, about 48 px):** back link, title (one line), status dot, "Show details" toggle
  (`aria-expanded`, `aria-controls`).
- **Details (shown when expanded, collapsed by default on phones):**
  - Authors, year, status text.
  - Actions row: "Mark as read" (toggle, `aria-pressed`, uses the existing listened action and refreshes the page),
    "Source", "Make audio" (when needed).
  - Tags: chips with a remove button each, and an "Add tag" button. The button opens a small inline field (Enter adds,
    Esc cancels). Errors show as a toast, the chip is not added.
  - Keywords: a `<details>` with the count in its title, closed by default. This replaces the always-on keyword strip.
  - Areas and topics (`doc.areas`, `doc.topics`): one line, the same `<details>`.

Desktop: the details open as a panel under the bar (not an overlay), so the content keeps its place.

**Gate:** toggle works with the keyboard; the collapsed bar is 48 px or less on a 390 px phone; "Mark as read" changes
the status chip and the paper's read state in the library list; a tag added here appears in the paper row after
the reload.

## Phase 3: three content tabs and the three-column layout

- Tabs: Document, Chapters, Questions. The tab bar uses the existing `pane-tabs` markup with a third button; the
  Questions pane moves out of `sidePane` into its own container.
- Wide layout: a container query on `.reader-body` (`container-type: inline-size`). At 1180 px or more the three
  panes sit side by side, the tab bar is hidden, and two splitters set the widths (the existing `splitter` component).
  Below that, one pane shows at a time, selected by the tabs.
- Keep `showTab()` the single place that changes the visible pane and pauses speed reading when the Document pane is
  hidden. Speed reading is only meaningful with the Document pane visible; on the wide layout it stays visible.
- The question box keeps its current behaviour (queued answers polled, citations, disabled until the paper is ready).

**Gate:** at 390 × 844, 768 × 1024 and 1280 × 800, the tab set is correct and no horizontal scroll; at 1600 × 900 all
three panes are visible; resizing across the 1180 px threshold keeps the selected pane and the question draft text.

## Phase 4: player bar

- Mode switch: each button gets an icon (headphones for Listen, open book for Read) and the text label. The label
  stays on phones too (the icons alone are not clear enough for the difference the user asked for).
- Fixed height: the bar gets explicit `grid-template-rows` (`info`, `controls`, `seek`) with the same heights in both
  modes. In read mode the `.read-bar` takes the `seek` row and the controls row shows the mode's own hint or is empty
  but kept. The bar's height is the same in both modes; this is checked by measurement.
- Touch targets: play, previous, next, follow and the mode buttons at 44 × 44 px minimum; the rate select at 44 px.
- Sliders: the seek and speed sliders get the larger thumb and track described in decision 7.
- Target height on a phone: no more than 120 px, which is the current height plus the larger targets. The earlier
  mobile plan had a 110 px target; the touch targets make that impossible without hiding a control. This is a trade-off
  the user should confirm.

**Gate:** bar height equal in Listen and Read at each tested width (measured with `getBoundingClientRect`); every
interactive control in the bar has a hit area of at least 44 × 44 px (measured).

## Phase 5: verification

- `npm run build` and `npm test` in `services/web`; backend tests for the tag endpoints.
- Browser checks in Chromium through the dev server against the real API, at 390 × 844, 360 × 740, 768 × 1024 and
  1600 × 900. Checks: no horizontal overflow; collapsed header height; detail panel usable; tabs or columns as per
  the width rule; bar height equal in both modes; touch targets; "Ask" button fully visible above the bar.
- Screenshots reviewed by eye for: collapsed and expanded header, each tab on a phone, the three columns on desktop,
  the bar in both modes.
- Not verified here: the layout on a real phone and a real screen reader. Those need a manual pass.

## Files touched

| File | Change |
|---|---|
| `services/backend/listen/migrations/013_library_tags.sql` | new table |
| `services/backend/listen/api/main.py` | `tags` on the document output, `PUT /api/documents/{id}/tags` |
| `services/backend/tests/test_api.py` | tag tests (or a new `test_tags.py`) |
| `services/web/src/api.ts` | `tags` on `Doc`, `api.setTags` |
| `services/web/src/reader.ts` | header split, tags UI, read button, three tabs, bar icons and sizes |
| `services/web/src/style.css` | reader section: header, container query, bar rows, touch sizes |
| `services/web/src/components.ts`, `query.ts`, `graph.ts`, `area.ts` | copy "listened" → "read" (decision 1) |

Not changed: the paper row's automatic tags, the library filters, the audio pipeline.
