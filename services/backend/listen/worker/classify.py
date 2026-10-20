"""Research areas for every paper (library job 'areas').

BLOOMZ chooses a primary area and up to two more from the fixed list in listen/areas.py. The answer is held to
that list by a JSON schema (llama.cpp grammar), so it cannot invent an area. The paper is described by its title,
abstract, the keywords extracted from it, and its section headings when the full text was processed. Papers that
are only catalogued from Zotero are classified from their metadata alone.

Without the LLM, or when it fails, the cue words of listen/areas.py decide, and the placement is kept as "rules" so
it is tried again later. A placement made by hand is pinned and is never changed by the job.

The papers the user has filed by hand are exemplars. The model is shown the areas of the five papers closest to the
new one (by SPECTER2 vector), and when it is unsure and at least four of them share another area, that area is taken
and the placement is kept as "exemplars". The agreement, the share of the nearest papers in the placed area, is stored
with every placement; a low agreement marks a paper for review.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

from .. import areas, embeddings, jobs, vectors
from ..config import Settings
from ..storage import DocPaths
from . import embed, openalex, topics
from .llm import LlmClient, LlmError, budget

log = logging.getLogger(__name__)

KEYWORDS = 20          # the most important keywords shown to the model
SECTIONS = 12          # section headings shown to the model
N_PREDICT = 120
EXEMPLARS = 5          # the nearest papers the user filed, shown to the model and voted on
EXEMPLAR_WIN = 4       # votes needed to overrule an unsure model


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


def evidence(neighbours: list[tuple[str, float]]) -> str:
    """The areas of the nearest papers the user filed, for the model to weigh."""
    listed = ", ".join(f"{areas.NAMES[a]} ({s:.2f})" for a, s in neighbours)
    return f"Papers the user filed as similar, with their areas and similarity: {listed}."


def llm_placement(llm: LlmClient, body: str, hint: str | None = None) -> areas.Placement:
    """Ask the model for one primary area and up to two more. Raises LlmError when the answer is unusable."""
    names = ", ".join(areas.NAMES[a] for a in areas.IDS if a != areas.OTHER)
    prompt = (
        f"{llm.truncate(body, budget(llm, 'body'))}\n\n"
        + (f"{hint}\n\n" if hint else "")
        + f"Research areas: {names}, or Other.\n"
        "Question: Which research area is this paper mainly about? Also list up to two other areas it clearly "
        "covers, and say how confident you are (high, medium or low). Answer as JSON with the keys primary, "
        "also and confidence."
    )
    answer = llm.chat_json(prompt, schema(), n_predict=N_PREDICT, feature="areas")
    primary = _id_of(answer.get("primary", ""))
    if primary is None:
        raise LlmError(f"unknown area {answer.get('primary')!r}")
    also = [a for a in (_id_of(n) for n in answer.get("also", []) if isinstance(n, str)) if a]
    confidence = answer.get("confidence") if answer.get("confidence") in areas.CONFIDENCE else "low"
    primary, also = areas.clean(primary, also)
    return areas.Placement(primary=primary, also=also, confidence=confidence, source="llm")


def place(title: str | None, abstract: str | None, keywords: list[str], sections: list[str],
          llm: LlmClient | None, neighbours: list[tuple[str, float]] | None = None) -> areas.Placement:
    """The model's placement (shown the nearest exemplars when there are some), or the rules without a model."""
    body = paper_text(title, abstract, keywords, sections)
    if llm is not None:
        try:
            return llm_placement(llm, body, evidence(neighbours) if neighbours else None)
        except LlmError as e:
            log.warning("area classification fell back to the rules: %s", e)
    return areas.rules(body.lower())


def settle(placement: areas.Placement, neighbours: list[tuple[str, float]] | None) -> tuple[areas.Placement, float | None]:
    """Compare a placement with the nearest exemplars. Returns the placement (taken from the exemplars when the model
    was unsure and they agree on another area) and the share of the exemplars in its primary area."""
    if not neighbours:
        return placement, None
    votes = Counter(area for area, _ in neighbours)
    winner, count = votes.most_common(1)[0]
    if placement.confidence == "low" and winner != placement.primary and count >= EXEMPLAR_WIN and winner != areas.OTHER:
        also = [placement.primary] if placement.primary not in (winner, areas.OTHER) else []
        placement = areas.Placement(primary=winner, also=also[:areas.MAX_ALSO], confidence="medium", source="exemplars")
    return placement, round(votes.get(placement.primary, 0) / len(neighbours), 2)


def exemplars(conn) -> list[tuple[str, str, list[float]]]:
    """(paper, primary area, vector) of the papers the user placed by hand, when they have a vector."""
    rows = conn.execute(
        "SELECT a.document_id, a.primary_area, e.vector FROM document_areas a "
        "JOIN document_embeddings e ON e.document_id = a.document_id "
        "WHERE a.pinned AND e.model = %s", (embeddings.MODEL,)).fetchall()
    return [(str(r["document_id"]), r["primary_area"], list(r["vector"])) for r in rows]


def neighbour_areas(vector: list[float] | None, pool_of_exemplars: list[tuple[str, str, list[float]]],
                    doc_id: str) -> list[tuple[str, float]] | None:
    """(area, similarity) of the nearest exemplars, best first. None when the paper has no vector or nothing to
    compare with."""
    if vector is None or not pool_of_exemplars:
        return None
    area_of = {doc: area for doc, area, _ in pool_of_exemplars}
    ranked = vectors.nearest(vector, [(doc, vec) for doc, _, vec in pool_of_exemplars], EXEMPLARS, exclude=doc_id)
    return [(area_of[doc], sim) for doc, sim in ranked] or None


def _sections(docs_dir: Path, doc_id: str) -> list[str]:
    path = DocPaths(docs_dir, doc_id).structure
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [str(s.get("title")) for s in data.get("sections", []) if s.get("title")]


def store(conn, doc_id: str, placement: areas.Placement, agreement: float | None = None) -> None:
    """Save a placement, unless it was made by hand (pinned)."""
    conn.execute(
        "INSERT INTO document_areas (document_id, primary_area, also_areas, confidence, source, agreement) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (document_id) DO UPDATE SET "
        "primary_area = EXCLUDED.primary_area, also_areas = EXCLUDED.also_areas, confidence = EXCLUDED.confidence, "
        "source = EXCLUDED.source, agreement = EXCLUDED.agreement, updated_at = now() "
        "WHERE NOT document_areas.pinned",
        (doc_id, placement.primary, placement.also, placement.confidence, placement.source, agreement),
    )


def run(pool: ConnectionPool, docs_dir: Path, llm: LlmClient | None, job_id: int, options: dict[str, Any],
        settings: Settings | None = None) -> int:
    """Classify the papers that have no placement yet (or all unpinned ones with force), then rebuild the topics.
    With settings, the paper vectors come first and the exemplars are used; OpenAlex topics are looked up when
    OPENALEX_EMAIL is set. Returns the number of papers placed."""
    llm_ok = llm is not None and llm.wait_ready(timeout_s=60)
    if not llm_ok:
        log.warning("LLM not available; areas come from the rules and are retried when it is back")
    use_exemplars = settings is not None and settings.embeddings and settings.areas_exemplars
    if settings is not None and settings.embeddings:
        embed.ensure_library(pool, settings)
    with pool.connection() as conn:
        pool_of_exemplars = exemplars(conn) if use_exemplars else []
        vector_of = embeddings.load_all(conn) if use_exemplars and pool_of_exemplars else {}
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
        near = neighbour_areas(vector_of.get(doc_id), pool_of_exemplars, doc_id)
        placement = place(row["title"], row["abstract"], keywords, sections, llm if llm_ok else None, near)
        placement, agreement = settle(placement, near)
        with pool.connection() as conn, conn.transaction():
            store(conn, doc_id, placement, agreement)
            jobs.heartbeat(conn, job_id, stage="areas", done=done, total=len(todo))
        placed += 1
    if settings is not None and settings.openalex_email:
        openalex.enrich_library(pool, settings.openalex_email)
    # the topics use the placements, so they are rebuilt after them
    topics.rebuild(pool, llm if llm_ok else None, settings)
    log.info("areas: %d papers placed", placed)
    return placed


def unplaced_exists(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM documents d LEFT JOIN document_areas a ON a.document_id = d.id "
        "WHERE a.document_id IS NULL AND d.status <> 'failed' LIMIT 1").fetchone()
    return row is not None

