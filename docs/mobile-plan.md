# Mobile optimisation plan

Scope: the web app (`services/web`) on phones (360–430 px wide), plus the background-playback
bug on Android Chrome. Desktop layout must not change.

## Findings (measured at 390 × 844, real library data)

| # | Symptom | Cause | Measured |
|---|---------|-------|----------|
| 1 | PDF / article not shown | `.reader` has four grid rows but five children. On ≤ 900 px the hidden-by-default tab bar lands in the `1fr` row, so `.reader-body` collapses. | tabs 349 px tall, document pane 0 px |
| 2 | Keyword tags take too much room | up to 14 wrapping chips above the reader | 237 px |
| 3 | "Now reading" overlaps the chapter list | `.now-reading` and `.qa` can shrink inside the flex column | text runs into the list |
| 4 | "Ask" button cut off | `.qa` is sized to content while the side pane is height-constrained | button 35 px visible below the player bar |
| 5 | Filters take too much room | the facet column stacks fully above the results | 2 086 px tall on phones |
| 6 | Player bar is tall | 3 rows of controls plus padding | 133 px |

## Status

Implemented and checked with Chromium (see Verification). Items 1–6 and the playback fix are done; item 7 was checked and needs no change.

Found while implementing, also fixed:

- Playback: the audio source was chosen by comparing `chapterIdx`, which points at a chapter still being generated. When it became ready, the previous chapter's audio replayed instead of the next one.
- Playback: the play button and the media session were not refreshed when playback was paused or started while a chapter was pending.
- Desktop: the Ask box and side pane were sized at creation (before layout), which clipped the Ask button by 2 px.
- Library: the Articles tile showed `undefined` while a source filter was active.

## Changes

1. **Reader grid (fixes 1).** Make the grid rows explicit for each child: `head`, `keywords`, `tabs`, `body`, `bar`.
   On phones the tab row is `auto` and the body is `minmax(0, 1fr)`. Desktop keeps the current layout.
2. **Keyword strip (fixes 2).** On phones show keywords as one horizontally scrolling line (`nowrap`, `overflow-x: auto`),
   capped at about 40 px tall.
3. **Side pane (fixes 3 and 4).** `.now-reading`, `.qa` and `.pane-tabs` are `flex: none`. On phones the chapter list
   takes the remaining height and scrolls. The question box is a normal scrolling region, so "Ask" is always reachable.
   The side pane keeps `min-height: 0` so nothing overflows the grid cell.
4. **Player bar (fixes 6).** On phones: smaller padding, a single line for title and speed, slimmer seek row.
   Target ≤ 110 px. The rate select and "Follow" stay reachable.
5. **Library filters (fixes 5).** On phones the filter column becomes a disclosure (`<details>`) titled "Filters"
   with the active count, closed by default. Desktop keeps the sticky column.
6. **Dashboard tiles.** Tighter padding and smaller numbers on phones so the summary takes about half the height.
7. **PDF toolbar.** Verify it fits 390 px; wrap or hide the least-used controls on phones.

## Background playback on Android (bug)

Symptom: when the page is in the background, playback stops at the end of a chapter and the next one does not start.

Likely causes, in the order I checked them:

- `Player.next()` decides whether to autoplay from `audio.paused`. A background pause (audio focus lost, notification action)
  makes that false, so the chapter loads and then waits.
- A chapter that is still being generated sets `waitingFor`. The only thing that resumes it is the reader's
  `setTimeout` poll, which Android Chrome throttles or freezes in the background.
- `play()` rejections are swallowed, so a blocked resume leaves no trace.
- `navigator.mediaSession.playbackState` is never set, so Android's media notification does not track state.

Planned fix:

- Track the listener's intent (`wantPlaying`) separately from `audio.paused`, and use it for auto-advance.
- Keep `wantPlaying` through a `waitingFor` chapter and resume as soon as it is ready. Refresh immediately when the
  page becomes visible again.
- Update `mediaSession.playbackState` on play and pause.
- Do not save a position at `ended`, so resuming after an app kill does not restart at the end of a chapter.

## Verification

- Browser test at 390 × 844 (and 360 × 740, 768 × 1024) against the real API through the dev server.
  Checks: no horizontal overflow; document body taller than 300 px; keyword strip ≤ 56 px; "Ask" button fully inside the
  viewport above the player bar; no overlap between "Now reading" and the chapter list; filters collapsed on phones.
- Screenshots reviewed by eye for the library, the document tab, the chapters tab and the filters.
- Playback: simulated end of chapter (the next chapter ready, then not ready and later ready), checking that the next
  chapter plays. This runs in desktop Chromium. It does not prove the behaviour on Android hardware; a manual check on a
  phone with the screen locked is still needed.
