"""OpenAlex topics for papers: a reference classification beside the LLM's areas (opt-in, OPENALEX_EMAIL).

Only the title is sent, and the email address in the request (OpenAlex's polite pool). A paper is matched when
OpenAlex's title equals its own after normalising case and punctuation; a miss is stored too, so it is not asked
again. Nothing here runs unless OPENALEX_EMAIL is set.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.openalex.org/works"
PAUSE_S = 1.0  # one request a second


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def filter_title(title: str) -> str:
    """OpenAlex reads commas and pipes in a filter value as separators, so they are replaced."""
    return re.sub(r"[,|]", " ", title)[:300]


def topics_of(work: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for t in work.get("topics") or []:
        out.append({
            "name": t.get("display_name"),
            "subfield": (t.get("subfield") or {}).get("display_name"),
            "field": (t.get("field") or {}).get("display_name"),
            "domain": (t.get("domain") or {}).get("display_name"),
            "score": round(float(t.get("score") or 0), 3),
        })
    return out


def pick(results: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    """The work whose title is the paper's title, or None."""
    wanted = _norm(title)
    for work in results:
        if _norm(work.get("display_name") or "") == wanted:
            return work
    return None


def lookup(client: httpx.Client, title: str, email: str) -> dict[str, Any] | None:
    resp = client.get(BASE, params={"filter": f"title.search:{filter_title(title)}", "per-page": 5, "mailto": email,
                                    "select": "id,display_name,topics"}, timeout=20)
    resp.raise_for_status()
    work = pick(resp.json().get("results", []), title)
    if work is None:
        return None
    return {"work_id": work.get("id"), "topics": topics_of(work)}


def enrich_library(pool, email: str, client: httpx.Client | None = None) -> int:
    """Look up the papers that have no OpenAlex entry yet. Returns the number matched."""
    with pool.connection() as conn:
        todo = conn.execute(
            "SELECT d.id, d.title FROM documents d LEFT JOIN document_openalex o ON o.document_id = d.id "
            "WHERE d.status <> 'failed' AND d.title <> '' AND o.document_id IS NULL ORDER BY d.created_at").fetchall()
    client = client or httpx.Client(headers={"User-Agent": "listen-vault"})
    matched = 0
    for row in todo:
        try:
            hit = lookup(client, row["title"], email)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("OpenAlex lookup failed for %r: %s", row["title"][:60], e)
            time.sleep(PAUSE_S)
            continue
        with pool.connection() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO document_openalex (document_id, work_id, topics) VALUES (%s, %s, %s::jsonb) "
                "ON CONFLICT (document_id) DO UPDATE SET work_id = EXCLUDED.work_id, topics = EXCLUDED.topics, "
                "fetched_at = now()",
                (row["id"], hit["work_id"] if hit else None, json.dumps(hit["topics"] if hit else [], ensure_ascii=False)),
            )
        matched += hit is not None
        time.sleep(PAUSE_S)
    log.info("OpenAlex: %d of %d papers matched", matched, len(todo))
    return matched
