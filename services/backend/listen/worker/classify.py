"""Research areas for every paper (library job 'areas').

BLOOMZ chooses a primary area and up to two more from the fixed list in listen/areas.py. The answer is held to
that list by a JSON schema (llama.cpp grammar), so it cannot invent an area. The paper is described by its title,
abstract, the keywords extracted from it, and its section headings when the full text was processed. Papers that
are only catalogued from Zotero are classified from their metadata alone.

Without the LLM, or when it fails, the cue words of listen/areas.py decide, and the placement is kept as "rules" so
it is tried again later. A placement made by hand is pinned and is never changed by the job.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

from .. import areas, jobs
from ..storage import DocPaths
from . import topics
from .llm import LlmClient, LlmError

log = logging.getLogger(__name__)

BODY_TOKENS = 700      # the paper text the model sees (the question and the answer are added to it)
KEYWORDS = 20          # the most important keywords shown to the model
SECTIONS = 12          # section headings shown to the model
N_PREDICT = 120


def schema() -> dict[str, Any]:
    names = [areas.NAMES[a] for a in areas.IDS]
    return {
        "type": "object",
        "properties": {
            "primary": {"type": "string", "enum": names},
            "also": {"type": "array", "items": {"type": "string", "enum": names}, "maxItems": areas.MAX_ALSO},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["primary", "also", "confidence"],
    }


def _id_of(name: str) -> str | None:
    for area_id, label in areas.NAMES.items():
        if label == name:
            return area_id
    return None


def paper_text(title: str | None, abstract: str | None, keywords: list[str], sections: list[str]) -> str:
    parts = [f"Title: {title or 'untitled'}", f"Abstract: {abstract or 'not available'}"]
    if keywords:
        parts.append("Keywords: " + ", ".join(keywords[:KEYWORDS]))
    if sections:
        parts.append("Sections: " + "; ".join(sections[:SECTIONS]))
    return "\n".join(parts)


def llm_placement(llm: LlmClient, body: str) -> areas.Placement:
    """Ask the model for one primary area and up to two more. Raises LlmError when the answer is unusable."""
    names = ", ".join(areas.NAMES[a] for a in areas.IDS if a != areas.OTHER)
    prompt = (
        f"{llm.truncate(body, BODY_TOKENS)}\n\n"
        f"Research areas: {names}, or Other.\n"
        "Question: Which research area is this paper mainly about? Also list up to two other areas it clearly "
        "covers, and say how confident you are (high, medium or low). Answer as JSON with the keys primary, "
        "also and confidence.\nAnswer:"
    )
    answer = llm.complete_json(prompt, schema(), n_predict=N_PREDICT)
    primary = _id_of(answer.get("primary", ""))
    if primary is None:
        raise LlmError(f"unknown area {answer.get('primary')!r}")
    also = [a for a in (_id_of(n) for n in answer.get("also", []) if isinstance(n, str)) if a]
    confidence = answer.get("confidence") if answer.get("confidence") in areas.CONFIDENCE else "low"
    primary, also = areas.clean(primary, also)
    return areas.Placement(primary=primary, also=also, confidence=confidence, source="llm")


def place(title: str | None, abstract: str | None, keywords: list[str], sections: list[str],
          llm: LlmClient | None) -> areas.Placement:
    body = paper_text(title, abstract, keywords, sections)
    if llm is not None:
        try:
            return llm_placement(llm, body)
        except LlmError as e:
            log.warning("area classification fell back to the rules: %s", e)
    return areas.rules(body.lower())


def _sections(docs_dir: Path, doc_id: str) -> list[str]:
    path = DocPaths(docs_dir, doc_id).structure
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [str(s.get("title")) for s in data.get("sections", []) if s.get("title")]


def store(conn, doc_id: str, placement: areas.Placement) -> None:
    """Save a placement, unless it was made by hand (pinned)."""
    conn.execute(
        "INSERT INTO document_areas (document_id, primary_area, also_areas, confidence, source) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (document_id) DO UPDATE SET "
        "primary_area = EXCLUDED.primary_area, also_areas = EXCLUDED.also_areas, confidence = EXCLUDED.confidence, "
        "source = EXCLUDED.source, updated_at = now() WHERE NOT document_areas.pinned",
        (doc_id, placement.primary, placement.also, placement.confidence, placement.source),
    )


def run(pool: ConnectionPool, docs_dir: Path, llm: LlmClient | None, job_id: int, options: dict[str, Any]) -> int:
    """Classify the papers that have no placement yet (or all unpinned ones with force), then rebuild the topics.
    Returns the number of papers placed."""
    llm_ok = llm is not None and llm.wait_ready(timeout_s=60)
    if not llm_ok:
        log.warning("LLM not available; areas come from the rules and are retried when it is back")
    force = bool(options.get("force"))
    with pool.connection() as conn:
        todo = conn.execute(
            "SELECT d.id, d.title, d.abstract FROM documents d LEFT JOIN document_areas a ON a.document_id = d.id "
            "WHERE d.status <> 'failed' AND (a.document_id IS NULL OR (NOT a.pinned AND (%(force)s OR "
            "(a.source = 'rules' AND %(llm)s)))) ORDER BY d.created_at",
            {"force": force, "llm": llm_ok},
        ).fetchall()
    placed = 0
    for done, row in enumerate(todo, start=1):
        doc_id = str(row["id"])
        with pool.connection() as conn:
            keywords = [r["term"] for r in conn.execute(
                "SELECT term FROM document_keywords WHERE document_id = %s ORDER BY weight DESC, term", (row["id"],))]
        sections = _sections(docs_dir, doc_id)
        placement = place(row["title"], row["abstract"], keywords, sections, llm if llm_ok else None)
        with pool.connection() as conn, conn.transaction():
            store(conn, doc_id, placement)
            jobs.heartbeat(conn, job_id, stage="areas", done=done, total=len(todo))
        placed += 1
    # the topics use the placements, so they are rebuilt after them
    topics.rebuild(pool, llm if llm_ok else None)
    log.info("areas: %d papers placed", placed)
    return placed


def unplaced_exists(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM documents d LEFT JOIN document_areas a ON a.document_id = d.id "
        "WHERE a.document_id IS NULL AND d.status <> 'failed' LIMIT 1").fetchone()
    return row is not None

