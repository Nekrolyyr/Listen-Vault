# Zotero import: diagnosis and fix plan

## Symptom

The library's Zotero import catalogues nothing. `zotero_state.last_error` is "Zotero returned HTTP 400" and
`zotero_links` is empty. The worker logs `zotero sync failed: Zotero returned HTTP 400` every 30 minutes, with
nothing else to go on.

## Diagnosis (checked against the live API from the worker container)

- `ZOTERO_USER_ID` in `.env` is set to the username (`Erudyyr`). Zotero's library paths need the numeric user ID.
  `GET /users/Erudyyr/items` returns 400 "Invalid user ID".
- `GET /keys/current` with the same key returns the numeric `userID`, and its `username` matches `ZOTERO_USER_ID`.
- The code path that looks the ID up from the key (commit 74badc2) only runs when `ZOTERO_USER_ID` is empty.
  A non-numeric value in the environment is used as it is.
- The 400 response body ("Invalid user ID") was thrown away: `ZoteroError` kept only the status code, and the
  worker logged only that. That is why the failure looked unexplained.

## Plan

1. **Use the numeric ID.** `ZoteroClient` accepts a user ID only when it is all digits. Anything else (a username
   pasted by mistake) is logged as a warning and the ID is looked up from the API key instead. The sync and the
   PDF download both go through the client, so both are fixed. `.env` does not need to change to get working
   imports, but the warning says to clear it.
2. **Keep the reason of an error.** Errors from Zotero include its response text (trimmed), so the message reads
   "Zotero returned HTTP 400: Invalid user ID".
3. **Logging for diagnostics.** The worker and the client log:
   - each sync start: which user ID and how it was obtained (configured or looked up), the version it starts from;
   - each page of changes (debug), and the summary at the end (items, PDFs, new versus kept, new version);
   - each failure with the HTTP status, the request path (never the key or query secrets), and the response text;
   - PDF downloads: start, size, and the reason when refused.
   Logs use the `listen.zotero` logger so they can be filtered.
4. **Tests** (`tests/test_zotero.py`): a username in `ZOTERO_USER_ID` triggers the key lookup; an HTTP 400 carries
   Zotero's text into the error and into `last_error`; a successful sync logs its summary; the key never appears
   in a log line.
5. **Verify live**: run one sync from the worker container with the fixed code and confirm that items are
   catalogued and `last_error` is cleared.

## Out of scope

- Changing `.env` (it holds secrets; the user clears `ZOTERO_USER_ID` or leaves it, both work after the fix).
