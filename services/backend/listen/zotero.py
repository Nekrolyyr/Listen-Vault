"""Zotero integration: every item of the personal library is catalogued; papers tagged for the queue are processed.

The Zotero Web API (v3) is read incrementally: `since=<library version>` returns what changed, attachments
included. Catalogued items carry the metadata (title, authors, date, abstract, DOI); a PDF attachment is only
downloaded when the paper is processed. The API key is sent to api.zotero.org only: a file download is
redirected to storage without the key.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx
from psycopg import Connection
from psycopg_pool import ConnectionPool

from . import jobs
from .config import Settings

log = logging.getLogger(__name__)

API_URL = "https://api.zotero.org"
PAGE = 100
NOT_PAPERS = {"attachment", "note"}
PDF_LINKS = {"imported_file", "imported_url"}  # stored in Zotero: downloadable through the API


class ZoteroError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class ZoteroClient:
    def __init__(self, user_id: str, api_key: str, http: httpx.Client | None = None,
                 storage: httpx.Client | None = None):
        self.user_id = user_id
        self.http = http or httpx.Client(base_url=API_URL, timeout=60,
                                         headers={"Zotero-API-Key": api_key, "Zotero-API-Version": "3"})
        self.storage = storage or httpx.Client(timeout=300)  # no API key here

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        try:
            resp = self.http.get(path, params=params)
        except httpx.HTTPError as e:
            raise ZoteroError(f"Zotero unreachable: {e}", retryable=True) from e
        if resp.status_code in (429, 503):
            raise ZoteroError("Zotero is busy or rate limiting; it will be tried again", retryable=True)
        if resp.status_code == 403:
            raise ZoteroError("Zotero refused the API key (check ZOTERO_API_KEY and its library access)")
        if resp.status_code >= 400:
            raise ZoteroError(f"Zotero returned HTTP {resp.status_code}")
        return resp

    def changes_since(self, version: int) -> tuple[list[dict[str, Any]], int]:
        """Items (and attachments) changed after `version`, and the library version to remember afterwards."""
        items: list[dict[str, Any]] = []
        start = 0
        library_version = version
        while True:
            resp = self._get(f"/users/{self.user_id}/items",
                             {"since": version, "format": "json", "limit": PAGE, "start": start})
            if start == 0:
                # the version at the start of the sync: anything changed while paging is fetched next time
                library_version = int(resp.headers.get("Last-Modified-Version", version))
            page = resp.json()
            items += page
            if len(page) < PAGE:
                return items, library_version
            start += PAGE

    def download_pdf(self, attachment_key: str) -> bytes:
        path = f"/users/{self.user_id}/items/{attachment_key}/file"
        try:
            resp = self.http.get(path, follow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    raise ZoteroError("Zotero sent a redirect without a location")
                resp = self.storage.get(urljoin(str(resp.url), location), follow_redirects=True)
        except httpx.HTTPError as e:
            raise ZoteroError(f"Zotero file download failed: {e}", retryable=True) from e
        if resp.status_code != 200:
            raise ZoteroError(f"Zotero file download failed: HTTP {resp.status_code}")
        if not resp.content.startswith(b"%PDF-"):
            raise ZoteroError("The attachment in Zotero is not a PDF")
        return resp.content


def _name(creator: dict[str, Any]) -> str:
    return " ".join(p for p in (creator.get("firstName"), creator.get("lastName")) if p).strip() or \
        (creator.get("name") or "").strip()


def _catalogue(conn: Connection, key: str, version: int, data: dict[str, Any], pdf_key: str | None,
               tags: list[str], collections: list[str], queue_tag: str) -> str:
    """Create or refresh the document of a Zotero item. Returns "queued", "catalogued" or "kept"."""
    title = (data.get("title") or "").strip() or "Untitled"
    authors = [n for c in data.get("creators", []) if c.get("creatorType") == "author" and (n := _name(c))]
    published = (data.get("date") or "").strip() or None
    abstract = (data.get("abstractNote") or "").strip() or None
    doi = (data.get("DOI") or "").strip() or None
    url = (data.get("url") or "").strip() or (f"https://doi.org/{doi}" if doi else None)
    if url and not url.startswith(("http://", "https://")):
        url = None
    source_type = "pdf" if pdf_key else "url"
    link = conn.execute("SELECT z.document_id, d.status FROM zotero_links z JOIN documents d ON d.id = z.document_id "
                        "WHERE z.zotero_key = %s", (key,)).fetchone()
    if link is None:
        doc_id = conn.execute(
            "INSERT INTO documents (source_type, source_url, title, authors, published, abstract, doi, has_pdf, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, false, 'catalogued') RETURNING id",
            (source_type, url, title, authors, published, abstract, doi),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO zotero_links (document_id, zotero_key, library_version, pdf_attachment, tags, collections) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (doc_id, key, version, pdf_key, tags, collections),
        )
        status = "catalogued"
    else:
        doc_id, status = link["document_id"], link["status"]
        conn.execute(
            "UPDATE documents SET title = %(title)s, authors = %(authors)s, published = %(published)s, "
            "abstract = %(abstract)s, doi = %(doi)s, source_url = COALESCE(%(url)s, source_url), "
            "source_type = CASE WHEN status = 'catalogued' THEN %(source_type)s ELSE source_type END, "
            "updated_at = now() WHERE id = %(id)s",
            {"title": title, "authors": authors, "published": published, "abstract": abstract, "doi": doi,
             "url": url, "source_type": source_type, "id": doc_id},
        )
        conn.execute(
            "UPDATE zotero_links SET library_version = %s, pdf_attachment = %s, tags = %s, collections = %s, "
            "synced_at = now() WHERE document_id = %s",
            (version, pdf_key, tags, collections, doc_id),
        )
    if status == "catalogued" and queue_tag in tags and (pdf_key or url):
        jobs.enqueue(conn, doc_id)
        return "queued"
    return "catalogued" if status == "catalogued" else "kept"


def sync(pool: ConnectionPool, settings: Settings, client: ZoteroClient | None = None) -> dict[str, int]:
    """Bring the catalogue up to date with the Zotero library. Returns counts for the log."""
    client = client or ZoteroClient(settings.zotero_user_id, settings.zotero_api_key)
    with pool.connection() as conn:
        last = conn.execute("SELECT last_version FROM zotero_state WHERE id = 1").fetchone()["last_version"]
    try:
        items, version = client.changes_since(last)
    except ZoteroError as e:
        with pool.connection() as conn:
            conn.execute("UPDATE zotero_state SET last_error = %s WHERE id = 1", (str(e),))
        raise

    papers = {i["key"]: i["data"] for i in items if i["data"].get("itemType") not in NOT_PAPERS}
    pdfs: dict[str, str] = {}  # parent key -> PDF attachment key (first one wins)
    for i in items:
        d = i["data"]
        if d.get("itemType") == "attachment" and d.get("contentType") == "application/pdf" \
                and d.get("linkMode") in PDF_LINKS and d.get("parentItem"):
            pdfs.setdefault(d["parentItem"], i["key"])

    counts = {"items": len(papers), "queued": 0, "catalogued": 0}
    with pool.connection() as conn, conn.transaction():
        for i in items:
            if i["key"] in papers:
                d = papers[i["key"]]
                tags = [t["tag"] for t in d.get("tags", []) if t.get("tag")]
                collections = list(d.get("collections", []))
                outcome = _catalogue(conn, i["key"], i["version"], d, pdfs.get(i["key"]), tags, collections,
                                     settings.zotero_queue_tag)
                if outcome == "queued":
                    counts["queued"] += 1
                elif outcome == "catalogued":
                    counts["catalogued"] += 1
        # a PDF attached to an item that did not change itself
        for parent, att in pdfs.items():
            if parent not in papers:
                conn.execute("UPDATE zotero_links SET pdf_attachment = %s WHERE zotero_key = %s", (att, parent))
        conn.execute("UPDATE zotero_state SET last_version = %s, last_synced_at = now(), last_error = NULL WHERE id = 1",
                     (version,))
    log.info("zotero sync: %s", counts)
    return counts
