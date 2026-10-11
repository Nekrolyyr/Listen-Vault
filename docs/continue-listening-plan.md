# Continue listening, progress, year slider and "not from Zotero"

## Goals

1. Papers in progress are easy to find and resume, and show how far along they are.
2. The year filter is a range slider instead of a list of single years.
3. A filter hides papers imported from Zotero.

## Progress

The position is stored per paper as a chapter index and a position within that chapter (`playback`). Overall
progress needs the length of the chapters before it, which is in `chapters.duration_s`.

- **Backend:** the library list adds, for a paper with a saved position:
  - `playback.chapter_title`, the title of the chapter being listened to;
  - `playback.listened_s`, the length of the earlier chapters plus the position;
  - `playback.progress`, `listened_s / duration_s`, clamped to 0–1.
- **Continue listening:** the library shows a section above the results for papers with a saved position that are
  not marked listened, most recently played first. Each card has the title, the chapter, a progress bar with
  "42% · 12:30 of 45:10", and a Resume button (the reader already resumes at the saved position).
- **Cards:** papers in progress show the same progress bar and "Continue at <chapter>" under the status chips.

The continue section follows the current filters, so the Zotero or year filters apply to it too.

## Year slider

- **Filter:** `year` (single year) becomes `year_from` and `year_to` (both optional, inclusive). The bar chart
  in the dashboard sets both to the clicked year.
- **Bounds:** the slider's range is the range of all papers in the library, not the filtered set, so it does not
  shrink as it is used. The facets response includes `year_bounds`.
- **Control:** two range inputs on one track, with the chosen range shown as "2016–2024". Changes are applied when
  the handle is released, so dragging does not send a request per pixel. "Any year" clears it.
- Papers without a publication year are excluded while a year range is set (as before).

## "Not from Zotero"

- **Filter:** `not_zotero=true` excludes papers that have a Zotero link.
- **Control:** a checkbox "Exclude Zotero items" in the Source group. It is independent of "From Zotero" (the
  existing source choice), so choosing both gives nothing.

## Tests

- **Backend** (`tests/test_api.py`): year range including its ends and exclusion of undated papers; `not_zotero`
  against a linked paper; the list's playback progress and chapter title from chapter durations; year bounds
  unaffected by filters.
- **Browser** (desktop and phone): the continue section lists only papers in progress, Resume opens them, the
  slider narrows the list and its handles stay within the bounds, the Zotero checkbox hides linked papers, and the
  cards show progress.

## Status

Implemented and checked: API tests (filters, Zotero exclusion, progress, full-library year bounds), the full
backend suite (141 passing), the web typecheck, and a browser check against the redeployed API: the continue
section lists the paper in progress with its chapter and progress, the year handle narrows the list and is
sent as `year_from`, "Exclude Zotero items" is sent as `not_zotero=true`, and "Any year" restores the list.
