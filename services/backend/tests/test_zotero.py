import dataclasses

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from listen import jobs, zotero
from listen.worker import pipeline as pipeline_mod

PDF = b"%PDF-1.4 zotero test"
PAPER = {
    "key": "AAA", "version": 40,
    "data": {
        "key": "AAA", "itemType": "journalArticle", "title": "Sparse heads", "date": "2024",
        "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}],
        "abstractNote": "Heads matter.", "DOI": "10.1/x", "tags": [{"tag": "listen-queue"}], "collections": ["C1"],
    },
}
UNQUEUED = {
    "key": "BBB", "version": 41,
    "data": {"key": "BBB", "itemType": "book", "title": "Not yet", "creators": [], "tags": [], "collections": []},
}
PDF_ATTACHMENT = {
    "key": "PPP", "version": 40,
    "data": {"key": "PPP", "itemType": "attachment", "parentItem": "AAA", "contentType": "application/pdf",
             "linkMode": "imported_file"},
}
NOTE = {"key": "NNN", "version": 40, "data": {"key": "NNN", "itemType": "note", "parentItem": "AAA"}}



def owner(pool) -> int:
    """The administrator whose library the synced items go into."""
    with pool.connection() as conn:
        row = conn.execute("INSERT INTO users (email, is_admin, email_verified_at) VALUES ('admin@example.org', true, now()) "
                           "ON CONFLICT (email) DO UPDATE SET is_admin = true RETURNING id").fetchone()
    return row["id"]

def _client(library: list[dict], version: int = 42, storage_handler=None):
    def handler(request: httpx.Request):
        assert request.headers["Zotero-API-Key"] == "secret"
        assert request.headers["Zotero-API-Version"] == "3"
        since = int(request.url.params["since"])
        start = int(request.url.params["start"])
        changed = [i for i in library if i["version"] > since]
        page = changed[start:start + zotero.PAGE]
        return httpx.Response(200, json=page, headers={"Last-Modified-Version": str(version)})

    http = httpx.Client(base_url=zotero.API_URL, transport=httpx.MockTransport(handler),
                        headers={"Zotero-API-Key": "secret", "Zotero-API-Version": "3"})
    storage = httpx.Client(transport=httpx.MockTransport(storage_handler or (lambda r: httpx.Response(200, content=PDF))))
    return zotero.ZoteroClient("1", "secret", http=http, storage=storage)


def _settings(settings):
    return dataclasses.replace(settings, zotero_user_id="1", zotero_api_key="secret")


def test_sync_catalogues_items_and_queues_tagged_papers(settings, pool):
    s = _settings(settings)
    counts = zotero.sync(pool, s, owner(pool), _client([PAPER, UNQUEUED, PDF_ATTACHMENT, NOTE]))
    assert counts == {"items": 2, "catalogued": 2}
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        paper = conn.execute("SELECT * FROM documents WHERE title = 'Sparse heads'").fetchone()
        assert paper["status"] == "catalogued" and paper["source_type"] == "pdf"
        assert paper["authors"] == ["Ada Lovelace"] and paper["abstract"] == "Heads matter."
        assert paper["doi"] == "10.1/x" and paper["published"] == "2024"
        assert paper["source_url"] == "https://doi.org/10.1/x"
        book = conn.execute("SELECT * FROM documents WHERE title = 'Not yet'").fetchone()
        assert book["status"] == "catalogued" and book["source_type"] == "url" and book["source_url"] is None
        link = conn.execute("SELECT * FROM zotero_links WHERE zotero_key = 'AAA'").fetchone()
        assert link["pdf_attachment"] == "PPP" and link["tags"] == ["listen-queue"] and link["collections"] == ["C1"]
        # importing never starts processing, not even for a paper with a tag
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0
        state = conn.execute("SELECT * FROM zotero_state").fetchone()
        assert state["last_version"] == 42 and state["last_error"] is None and state["last_synced_at"]


def test_second_sync_is_incremental_and_does_not_requeue(settings, pool):
    s = _settings(settings)
    zotero.sync(pool, s, owner(pool), _client([PAPER, UNQUEUED, PDF_ATTACHMENT]))
    # nothing changed since version 42
    assert zotero.sync(pool, s, owner(pool), _client([PAPER, UNQUEUED, PDF_ATTACHMENT], version=42)) == {
        "items": 0, "catalogued": 0}
    with psycopg.connect(settings.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_sync_pages_through_large_libraries(settings, pool):
    many = [{"key": f"K{n:03d}", "version": 5,
             "data": {"key": f"K{n:03d}", "itemType": "book", "title": f"Book {n}", "tags": [], "creators": []}}
            for n in range(zotero.PAGE + 3)]
    counts = zotero.sync(pool, _settings(settings), owner(pool), _client(many, version=7))
    assert counts["items"] == zotero.PAGE + 3


def test_pdf_download_never_sends_the_key_to_storage(settings):
    seen = {}

    def storage(request: httpx.Request):
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, content=PDF)

    def http_handler(request: httpx.Request):
        return httpx.Response(302, headers={"location": "https://storage.example.org/file.pdf?sig=1"})

    http = httpx.Client(base_url=zotero.API_URL, transport=httpx.MockTransport(http_handler),
                        headers={"Zotero-API-Key": "secret"})
    client = zotero.ZoteroClient("1", "secret", http=http,
                                 storage=httpx.Client(transport=httpx.MockTransport(storage)))
    assert client.download_pdf("PPP") == PDF
    assert "zotero-api-key" not in seen["headers"]


def test_download_rejects_non_pdf(settings):
    http = httpx.Client(base_url=zotero.API_URL, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>")))
    client = zotero.ZoteroClient("1", "secret", http=http)
    with pytest.raises(zotero.ZoteroError, match="not a PDF"):
        client.download_pdf("PPP")


def test_sync_error_is_recorded_and_raised(settings, pool):
    def handler(request):
        return httpx.Response(403)

    http = httpx.Client(base_url=zotero.API_URL, transport=httpx.MockTransport(handler),
                        headers={"Zotero-API-Key": "bad"})
    client = zotero.ZoteroClient("1", "bad", http=http)
    with pytest.raises(zotero.ZoteroError, match="refused the API key"):
        zotero.sync(pool, _settings(settings), owner(pool), client)
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        assert "refused" in conn.execute("SELECT last_error FROM zotero_state").fetchone()["last_error"]


def test_enqueue_library_job_is_not_duplicated(pool):
    with pool.connection() as conn, conn.transaction():
        first = jobs.enqueue_library(conn, "zotero_sync")
    with pool.connection() as conn, conn.transaction():
        second = jobs.enqueue_library(conn, "zotero_sync")
    assert first is not None and second is None


def test_fetch_uses_the_zotero_pdf(settings, pool, monkeypatch):
    """A queued Zotero paper downloads its PDF from Zotero instead of the (absent) upload."""
    s = _settings(settings)
    zotero.sync(pool, s, owner(pool), _client([PAPER, PDF_ATTACHMENT]))
    pdf_client = zotero.ZoteroClient("1", "secret", http=httpx.Client(
        base_url=zotero.API_URL, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=PDF))))
    monkeypatch.setattr(zotero, "ZoteroClient", lambda *a, **k: pdf_client)
    with pool.connection() as conn:
        doc_id = conn.execute("SELECT id FROM documents WHERE title = 'Sparse heads'").fetchone()["id"]
        jobs.enqueue(conn, doc_id)  # what the Queue button does
    with pool.connection() as conn:
        job = jobs.claim(conn, "test")  # the worker claims jobs before running them
    from listen.storage import DocPaths
    paths = DocPaths(s.docs_dir, doc_id).ensure()
    p = pipeline_mod.Pipeline(s, pool, llm=None)
    p._fetch(job, paths)
    assert paths.source_pdf.read_bytes() == PDF
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        assert conn.execute("SELECT has_pdf, source_type FROM documents").fetchone() == {"has_pdf": True, "source_type": "pdf"}


def test_user_id_is_looked_up_from_the_api_key(settings, pool):
    """The library path needs the numeric userID; a username is refused by Zotero ("Invalid user ID")."""
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url.path)
        if request.url.path == "/keys/current":
            return httpx.Response(200, json={"userID": 19247461, "username": "someone"})
        assert request.url.path == "/users/19247461/items"
        return httpx.Response(200, json=[PAPER], headers={"Last-Modified-Version": "42"})

    http = httpx.Client(base_url=zotero.API_URL, transport=httpx.MockTransport(handler),
                        headers={"Zotero-API-Key": "secret", "Zotero-API-Version": "3"})
    client = zotero.ZoteroClient(None, "secret", http=http)
    counts = zotero.sync(pool, _settings(settings), owner(pool), client)
    assert counts["items"] == 1
    assert seen == ["/keys/current", "/users/19247461/items"]  # looked up once, then used


def test_username_in_the_environment_is_replaced_by_the_key_lookup(settings, pool, caplog):
    """ZOTERO_USER_ID set to the username (as in .env) used to be sent as it is and refused with 400."""
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url.path)
        if request.url.path == "/keys/current":
            return httpx.Response(200, json={"userID": 19247461, "username": "Erudyyr"})
        if request.url.path == "/users/Erudyyr/items":
            return httpx.Response(400, text="Invalid user ID")
        return httpx.Response(200, json=[PAPER], headers={"Last-Modified-Version": "42"})

    http = httpx.Client(base_url=zotero.API_URL, transport=httpx.MockTransport(handler),
                        headers={"Zotero-API-Key": "secret", "Zotero-API-Version": "3"})
    client = zotero.ZoteroClient("Erudyyr", "secret", http=http)
    with caplog.at_level("INFO", logger="listen.zotero"):
        counts = zotero.sync(pool, _settings(settings), owner(pool), client)
    assert counts["items"] == 1
    assert seen == ["/keys/current", "/users/19247461/items"]
    assert "not a numeric user ID" in caplog.text
    assert "Zotero user ID 19247461 taken from the API key" in caplog.text
    assert "zotero sync done: 1 papers" in caplog.text


def test_refusal_keeps_zotero_text_in_the_error_and_the_log(settings, pool, caplog):
    http = httpx.Client(base_url=zotero.API_URL,
                        transport=httpx.MockTransport(lambda r: httpx.Response(400, text="Invalid user ID")),
                        headers={"Zotero-API-Key": "secret"})
    client = zotero.ZoteroClient("1", "secret", http=http)
    with caplog.at_level("WARNING", logger="listen.zotero"), pytest.raises(zotero.ZoteroError) as info:
        zotero.sync(pool, _settings(settings), owner(pool), client)
    assert str(info.value) == "Zotero returned HTTP 400: Invalid user ID"
    assert "GET /users/1/items: HTTP 400: Invalid user ID" in caplog.text
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        assert conn.execute("SELECT last_error FROM zotero_state").fetchone()["last_error"].endswith("Invalid user ID")


def test_the_api_key_never_reaches_the_log(settings, pool, caplog):
    http = httpx.Client(base_url=zotero.API_URL,
                        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
                        headers={"Zotero-API-Key": "secret"})
    client = zotero.ZoteroClient("1", "secret", http=http)
    with caplog.at_level("DEBUG", logger="listen.zotero"), pytest.raises(zotero.ZoteroError):
        zotero.sync(pool, _settings(settings), owner(pool), client)
    assert "secret" not in caplog.text


def test_later_change_to_the_paper_keeps_its_pdf_attachment(settings, pool):
    s = _settings(settings)
    zotero.sync(pool, s, owner(pool), _client([PAPER, PDF_ATTACHMENT]))
    # a tag is added later: the paper changes, its PDF attachment does not come back in this sync
    retagged = {**PAPER, "version": 50, "data": {**PAPER["data"], "tags": [{"tag": "listen-queue"}, {"tag": "x"}]}}
    zotero.sync(pool, s, owner(pool), _client([retagged], version=50))
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        link = conn.execute("SELECT pdf_attachment FROM zotero_links WHERE zotero_key = 'AAA'").fetchone()
    assert link["pdf_attachment"] == "PPP"
