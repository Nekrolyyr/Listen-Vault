"""Foreground api: library, uploads, job status, PDF and audio streaming, playback state.

It never runs ML; it writes rows and files, and the worker picks the jobs up.
"""
from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Body, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .. import appsettings, areas, config, db, jobs, library
from ..storage import DocPaths
from .security import AuthMiddleware

log = logging.getLogger("listen.api")

KOKORO_VOICES = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova",
    "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx", "am_puck", "am_santa", "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]
RERUN_STAGES = {"start": "ocr", "structure": "structure", "chapters": "chapters", "synthesis": "synthesis"}

DOC_LIST_SQL = """
SELECT d.*, j.state AS job_state, j.stage AS job_stage, j.progress_done, j.progress_total,
       j.error AS job_error, j.run_after AS job_run_after,
       (SELECT count(*) FROM chapters c WHERE c.document_id = d.id) AS chapter_count,
       (SELECT count(*) FROM chapters c WHERE c.document_id = d.id AND c.audio_file IS NOT NULL) AS ready_count,
       p.chapter_idx AS play_chapter, p.position_s AS play_position,
       (SELECT c.title FROM chapters c WHERE c.document_id = d.id AND c.idx = p.chapter_idx) AS play_chapter_title,
       (SELECT coalesce(sum(c.duration_s), 0) FROM chapters c
          WHERE c.document_id = d.id AND c.idx < p.chapter_idx) AS play_before_s,
       (z.document_id IS NOT NULL) AS from_zotero,
       (SELECT coalesce(array_agg(t.term ORDER BY t.weight DESC), '{}') FROM (
            SELECT k.term, k.weight FROM document_keywords k
            WHERE k.document_id = d.id AND k.kind IN ('task', 'method', 'topic')
            ORDER BY k.weight DESC, k.term LIMIT 4) t) AS top_terms,
       (SELECT coalesce(json_agg(json_build_object('id', t.id, 'name', t.name) ORDER BY t.paper_count DESC, t.name),
                        '[]'::json)
          FROM document_topics dt JOIN topics t ON t.id = dt.topic_id WHERE dt.document_id = d.id) AS doc_topics,
       ar.primary_area AS area_primary, ar.also_areas AS area_also, ar.confidence AS area_confidence,
       ar.source AS area_source, ar.pinned AS area_pinned
FROM documents d
LEFT JOIN LATERAL (SELECT * FROM jobs WHERE document_id = d.id ORDER BY id DESC LIMIT 1) j ON true
LEFT JOIN playback p ON p.document_id = d.id
LEFT JOIN zotero_links z ON z.document_id = d.id
LEFT JOIN document_areas ar ON ar.document_id = d.id
"""
SORTS = {
    "added": "d.created_at DESC",
    "listened": "d.last_listened_at DESC NULLS LAST, d.created_at DESC",
    "title": "lower(d.title) ASC",
}


def parse_filters(
    term: list[str] = Query(default=[], max_length=20),
    year_from: int | None = Query(default=None, ge=1000, le=2999),
    year_to: int | None = Query(default=None, ge=1000, le=2999),
    source: str | None = Query(default=None, pattern=r"^(pdf|url|zotero)$"),
    not_zotero: bool = False,
    listened: bool | None = None,
    status: str | None = Query(default=None, pattern=r"^(catalogued|queued|processing|ready|failed)$"),
    area: str | None = Query(default=None, pattern=r"^[a-z_]{2,20}$"),
    topic: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    """Library filters shared by the list, the facets and the graph. Terms must all match.
    Years are inclusive; a paper without a publication year is outside any year range. An area matches the
    primary and the other areas of a paper; a topic matches the papers on it."""
    return {"term": [t.strip().lower() for t in term if t.strip()][:20], "year_from": year_from,
            "year_to": year_to, "source": source, "not_zotero": not_zotero, "listened": listened, "status": status,
            "area": area, "topic": topic}


def filter_sql(f: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """SQL condition (starting with AND, or empty) for the filters, over the alias `d` (documents)."""
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for i, term in enumerate(f["term"]):
        clauses.append(f"EXISTS (SELECT 1 FROM document_keywords k WHERE k.document_id = d.id AND k.term = %(t{i})s)")
        params[f"t{i}"] = term
    if f["year_from"] is not None or f["year_to"] is not None:
        year = "substring(d.published from '[0-9]{4}')::int"
        if f["year_from"] is not None:
            clauses.append(f"{year} >= %(year_from)s")
            params["year_from"] = f["year_from"]
        if f["year_to"] is not None:
            clauses.append(f"{year} <= %(year_to)s")
            params["year_to"] = f["year_to"]
    if f["not_zotero"]:
        clauses.append("NOT EXISTS (SELECT 1 FROM zotero_links z WHERE z.document_id = d.id)")
    if f["source"] == "zotero":
        clauses.append("EXISTS (SELECT 1 FROM zotero_links z WHERE z.document_id = d.id)")
    elif f["source"] in ("pdf", "url"):
        clauses.append("d.source_type = %(source)s")
        params["source"] = f["source"]
    if f["listened"] is not None:
        clauses.append("d.listened = %(listened)s")
        params["listened"] = f["listened"]
    if f["status"]:
        clauses.append("d.status = %(status)s")
        params["status"] = f["status"]
    if f.get("area"):
        clauses.append("EXISTS (SELECT 1 FROM document_areas a WHERE a.document_id = d.id "
                       "AND (a.primary_area = %(area)s OR %(area)s = ANY(a.also_areas)))")
        params["area"] = f["area"]
    if f.get("topic") is not None:
        clauses.append("EXISTS (SELECT 1 FROM document_topics dt WHERE dt.document_id = d.id "
                       "AND dt.topic_id = %(topic)s)")
        params["topic"] = f["topic"]
    return "".join(f" AND {c}" for c in clauses), params


class UrlIn(BaseModel):
    url: str = Field(max_length=2000)


class DocPatch(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    listened: bool | None = None


class PlaybackIn(BaseModel):
    chapter_idx: int = Field(ge=0)
    position_s: float = Field(ge=0)
    rate: float = Field(default=1.0, ge=0.5, le=3.0)


class RerunIn(BaseModel):
    stage: str = "synthesis"


class QuestionIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class ClassificationIn(BaseModel):
    primary: str = Field(max_length=20)
    also: list[str] = Field(default_factory=list, max_length=areas.MAX_ALSO)
    topics: dict[str, bool] = Field(default_factory=dict, max_length=100)  # topic name -> on this paper or not


def _question_out(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r["id"], "question": r["question"], "answer": r["answer"], "citations": r["citations"],
            "status": r["status"], "error": r["error"], "created_at": r["created_at"]}


def _playback_out(r: dict[str, Any]) -> dict[str, Any]:
    """Where a paper was left: the chapter, the position, and how far through the paper that is."""
    listened_s = float(r["play_before_s"] or 0) + float(r["play_position"] or 0)
    total = float(r["duration_s"] or 0)
    return {
        "chapter_idx": r["play_chapter"], "chapter_title": r["play_chapter_title"],
        "position_s": r["play_position"], "listened_s": round(listened_s, 1),
        "progress": round(min(1.0, listened_s / total), 4) if total > 0 else None,
    }


def _doc_out(r: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": str(r["id"]), "title": r["title"] or r.get("original_filename") or r.get("source_url") or "Untitled",
        "authors": r["authors"], "published": r["published"], "source_type": r["source_type"],
        "source_url": r["source_url"], "has_pdf": r["has_pdf"], "ocr_applied": r["ocr_applied"],
        "status": r["status"], "error": r["error"], "duration_s": r["duration_s"], "voice": r["voice"],
        "listened": r["listened"], "last_listened_at": r["last_listened_at"], "created_at": r["created_at"],
        "abstract": r.get("abstract"),
    }
    if "top_terms" in r:
        out["top_terms"] = list(r["top_terms"] or [])
    if "doc_topics" in r:
        out["topics"] = r["doc_topics"] or []
    if "area_primary" in r:
        out["areas"] = None if r["area_primary"] is None else {
            "primary": r["area_primary"], "also": list(r["area_also"] or []), "confidence": r["area_confidence"],
            "source": r["area_source"], "pinned": r["area_pinned"],
        }
    if "job_state" in r:
        out["job"] = None if r["job_state"] is None else {
            "state": r["job_state"], "stage": r["job_stage"], "done": r["progress_done"],
            "total": r["progress_total"], "error": r["job_error"], "retry_at": r["job_run_after"],
        }
        out["chapters_total"] = r["chapter_count"]
        out["chapters_ready"] = r["ready_count"]
        out["playback"] = None if r["play_chapter"] is None else _playback_out(r)
        out["from_zotero"] = r["from_zotero"]
    return out


KEYWORD_KINDS = ("task", "method", "dataset", "topic", "keyword")


def library_year_bounds(conn) -> list[int] | None:
    """First and last publication year of the whole library (not the filtered set), for the year slider."""
    row = conn.execute(
        "SELECT min(y) AS lo, max(y) AS hi FROM (SELECT substring(published from '[0-9]{4}')::int AS y "
        "FROM documents WHERE published IS NOT NULL) t WHERE y IS NOT NULL").fetchone()
    return None if row["lo"] is None else [row["lo"], row["hi"]]


def _keywords_by_kind(rows) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {k: [] for k in KEYWORD_KINDS}
    for r in rows:
        out[r["kind"]].append(r["term"])
    return out


def _title_from_filename(name: str) -> str:
    stem = Path(name).stem
    return re.sub(r"[_\s]+", " ", stem).strip()[:300] or "Untitled PDF"


def create_app(settings: config.Settings | None = None) -> FastAPI:
    settings = settings or config.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.wait_for_db(settings.database_url)
        db.migrate(settings.database_url)
        settings.docs_dir.mkdir(parents=True, exist_ok=True)
        app.state.pool = db.make_pool(settings.database_url)
        app.state.pool.wait(timeout=30)
        yield
        app.state.pool.close()

    app = FastAPI(title="listen-vault", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(AuthMiddleware, user=settings.listen_user, proxy_secret=settings.proxy_secret)

    def pool(request: Request):
        return request.app.state.pool

    def get_doc(conn, doc_id: UUID) -> dict[str, Any]:
        row = conn.execute(DOC_LIST_SQL + " WHERE d.id = %s", (doc_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Document not found")
        return row

    # ------------------------------------------------------------------ health & session
    @app.get("/api/health")
    def health(request: Request):
        with pool(request).connection() as conn:
            conn.execute("SELECT 1")
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        return {"user": request.state.user, "logout_url": "/auth/logout"}

    # ------------------------------------------------------------------ library
    @app.get("/api/documents")
    def list_documents(request: Request, q: str = Query("", max_length=200), sort: str = "added",
                       filters: dict = Depends(parse_filters)):
        order = SORTS.get(sort, SORTS["added"])
        like = f"%{q.strip()}%"
        clause, params = filter_sql(filters)
        area_ids = [a for a in areas.IDS if q.strip() and q.strip().lower() in areas.NAMES[a].lower()]
        with pool(request).connection() as conn:
            rows = conn.execute(
                DOC_LIST_SQL + " WHERE (%(q)s = '' OR d.title ILIKE %(like)s "
                "OR array_to_string(d.authors, ' ') ILIKE %(like)s OR d.abstract ILIKE %(like)s "
                "OR EXISTS (SELECT 1 FROM document_keywords k WHERE k.document_id = d.id AND k.term ILIKE %(like)s) "
                "OR EXISTS (SELECT 1 FROM document_topics dt JOIN topics t ON t.id = dt.topic_id "
                "WHERE dt.document_id = d.id AND t.name ILIKE %(like)s) "
                "OR ar.primary_area = ANY(%(area_ids)s))" + clause + " ORDER BY " + order,
                {**params, "q": q.strip(), "like": like, "area_ids": area_ids},
            ).fetchall()
        return [_doc_out(r) for r in rows]

    @app.get("/api/library/facets")
    def library_facets(request: Request, filters: dict = Depends(parse_filters)):
        """Counts for the facet column and the dashboard, over the papers that match the filters."""
        clause, params = filter_sql(filters)
        with pool(request).connection() as conn:
            def count_by(expr: str) -> dict[str, int]:
                rows = conn.execute(f"SELECT {expr} AS v, count(*) AS n FROM documents d "
                                    f"WHERE TRUE{clause} GROUP BY 1", params).fetchall()
                return {str(r["v"]): r["n"] for r in rows if r["v"] is not None}

            totals = conn.execute(
                "SELECT count(*) AS n, coalesce(sum(duration_s), 0) AS audio, "
                "coalesce(sum(duration_s) FILTER (WHERE listened), 0) AS listened_audio "
                f"FROM documents d WHERE TRUE{clause}", params).fetchone()
            terms = conn.execute(
                "SELECT k.kind, k.term, count(*) AS n FROM document_keywords k JOIN documents d ON d.id = k.document_id "
                f"WHERE TRUE{clause} GROUP BY k.kind, k.term ORDER BY n DESC, k.term", params).fetchall()
            by_kind: dict[str, list[dict[str, Any]]] = {k: [] for k in KEYWORD_KINDS}
            for r in terms:
                if len(by_kind[r["kind"]]) < 40:
                    by_kind[r["kind"]].append({"term": r["term"], "count": r["n"]})
            zotero_docs = conn.execute(
                "SELECT count(*) AS n FROM documents d WHERE EXISTS (SELECT 1 FROM zotero_links z "
                f"WHERE z.document_id = d.id) AND TRUE{clause}", params).fetchone()["n"]
        return {
            "total": totals["n"],
            "audio_s": totals["audio"],
            "listened_audio_s": totals["listened_audio"],
            "status": count_by("d.status"),
            "listened": {str(k).lower(): v for k, v in count_by("d.listened").items()},
            "source": {**count_by("d.source_type"), "zotero": zotero_docs},
            "years": [{"year": int(y), "count": n} for y, n in sorted(count_by("substring(d.published from '[0-9]{4}')").items())],
            "year_bounds": library_year_bounds(conn),
            "terms": by_kind,
        }

    @app.get("/api/library/overview")
    def library_overview(request: Request):
        """The map: every research area with its papers, unread and progress, and the papers that need a look."""
        with pool(request).connection() as conn:
            totals = conn.execute(
                "SELECT count(*) AS papers, count(*) FILTER (WHERE NOT d.listened) AS unread, "
                "count(*) FILTER (WHERE d.status = 'catalogued') AS not_processed, "
                "count(*) FILTER (WHERE d.status = 'failed') AS failed, coalesce(sum(d.duration_s), 0) AS audio_s "
                "FROM documents d").fetchone()
            unplaced = conn.execute(
                "SELECT count(*) AS n FROM documents d LEFT JOIN document_areas a ON a.document_id = d.id "
                "WHERE a.document_id IS NULL AND d.status <> 'failed'").fetchone()["n"]
            per_area = {r["area"]: r for r in conn.execute(
                "SELECT u.area, count(*) AS papers, count(*) FILTER (WHERE d.listened) AS listened, "
                "count(*) FILTER (WHERE NOT d.listened) AS unread "
                "FROM document_areas a JOIN documents d ON d.id = a.document_id, "
                "LATERAL unnest(array[a.primary_area] || a.also_areas) AS u(area) GROUP BY u.area").fetchall()}
            topics_by_area: dict[str, list[dict[str, Any]]] = {}
            for t in conn.execute("SELECT id, name, area, paper_count FROM topics WHERE area IS NOT NULL "
                                  "ORDER BY paper_count DESC, name"):
                topics_by_area.setdefault(t["area"], []).append(
                    {"id": t["id"], "name": t["name"], "papers": t["paper_count"]})
            attention = conn.execute(
                "SELECT d.id, d.title, d.status, a.confidence, a.pinned, a.document_id AS placed FROM documents d "
                "LEFT JOIN document_areas a ON a.document_id = d.id "
                "WHERE d.status = 'failed' OR a.document_id IS NULL OR (a.confidence = 'low' AND NOT a.pinned) "
                "ORDER BY d.created_at DESC LIMIT 20").fetchall()
        out_areas = []
        for area_id in areas.IDS:
            stats = per_area.get(area_id)
            papers = stats["papers"] if stats else 0
            listened = stats["listened"] if stats else 0
            out_areas.append({
                "id": area_id, "name": areas.NAMES[area_id], "papers": papers,
                "unread": stats["unread"] if stats else 0, "listened": listened,
                "progress": round(listened / papers, 4) if papers else None,
                "topics": topics_by_area.get(area_id, [])[:3],
            })
        return {
            "papers": totals["papers"], "unread": totals["unread"], "not_processed": totals["not_processed"],
            "failed": totals["failed"], "audio_s": totals["audio_s"], "unplaced": unplaced, "areas": out_areas,
            "attention": [{"id": str(r["id"]), "title": r["title"] or "Untitled", "status": r["status"],
                           "reason": "failed" if r["status"] == "failed" else
                           "no area yet" if r["placed"] is None else "low confidence"} for r in attention],
        }

    @app.get("/api/library/areas/{area_id}")
    def library_area(request: Request, area_id: str):
        if area_id not in areas.NAMES:
            raise HTTPException(404, "Area not found")
        with pool(request).connection() as conn:
            totals = conn.execute(
                "SELECT count(*) AS papers, count(*) FILTER (WHERE d.listened) AS listened, "
                "count(*) FILTER (WHERE NOT d.listened) AS unread "
                "FROM document_areas a JOIN documents d ON d.id = a.document_id "
                "WHERE a.primary_area = %(a)s OR %(a)s = ANY(a.also_areas)", {"a": area_id}).fetchone()
            topic_rows = conn.execute(
                "SELECT t.id, t.name, t.terms, t.paper_count AS papers, "
                "count(d.id) FILTER (WHERE NOT d.listened) AS unread FROM topics t "
                "LEFT JOIN document_topics dt ON dt.topic_id = t.id LEFT JOIN documents d ON d.id = dt.document_id "
                "WHERE t.area = %s GROUP BY t.id ORDER BY t.paper_count DESC, t.name", (area_id,)).fetchall()
        return {
            "id": area_id, "name": areas.NAMES[area_id], "papers": totals["papers"],
            "listened": totals["listened"], "unread": totals["unread"],
            "topics": [{"id": r["id"], "name": r["name"], "terms": list(r["terms"])[:4], "papers": r["papers"],
                        "unread": r["unread"]} for r in topic_rows],
        }

    @app.get("/api/library/topics/{topic_id}")
    def library_topic(request: Request, topic_id: int):
        """A topic: its terms, its papers by status, related topics, and the topics before and after it in its area."""
        with pool(request).connection() as conn:
            row = conn.execute("SELECT id, name, terms, area, paper_count FROM topics WHERE id = %s",
                               (topic_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Topic not found")
            counts = conn.execute(
                "SELECT count(*) FILTER (WHERE NOT d.listened) AS unread FROM document_topics dt "
                "JOIN documents d ON d.id = dt.document_id WHERE dt.topic_id = %s", (topic_id,)).fetchone()
            related = conn.execute(
                "SELECT t2.id, t2.name, count(*) AS shared FROM document_topics a "
                "JOIN document_topics b ON b.document_id = a.document_id AND b.topic_id <> a.topic_id "
                "JOIN topics t2 ON t2.id = b.topic_id WHERE a.topic_id = %s "
                "GROUP BY t2.id, t2.name ORDER BY shared DESC, t2.name LIMIT 6", (topic_id,)).fetchall()
            siblings = conn.execute(
                "SELECT id, name FROM topics WHERE area IS NOT DISTINCT FROM %s ORDER BY paper_count DESC, name",
                (row["area"],)).fetchall()
        order = [{"id": r["id"], "name": r["name"]} for r in siblings]
        i = [r["id"] for r in siblings].index(topic_id)
        return {
            "id": row["id"], "name": row["name"], "terms": list(row["terms"]), "area": row["area"],
            "area_name": areas.NAMES.get(row["area"] or "", None), "papers": row["paper_count"],
            "unread": counts["unread"],
            "related": [{"id": r["id"], "name": r["name"], "shared": r["shared"]} for r in related],
            "prev": order[i - 1] if i > 0 else None,
            "next": order[i + 1] if i + 1 < len(order) else None,
        }

    @app.patch("/api/documents/{doc_id}/classification")
    def put_classification(request: Request, doc_id: UUID, body: ClassificationIn):
        """A hand-made placement: the areas and topics chosen here are kept when the library is classified again."""
        if body.primary not in areas.NAMES or any(a not in areas.NAMES for a in body.also):
            raise HTTPException(422, "Unknown area")
        primary, also = areas.clean(body.primary, body.also)
        with pool(request).connection() as conn, conn.transaction():
            get_doc(conn, doc_id)
            conn.execute(
                "INSERT INTO document_areas (document_id, primary_area, also_areas, confidence, source, pinned) "
                "VALUES (%s, %s, %s, 'high', 'manual', true) ON CONFLICT (document_id) DO UPDATE SET "
                "primary_area = EXCLUDED.primary_area, also_areas = EXCLUDED.also_areas, confidence = 'high', "
                "source = 'manual', pinned = true, updated_at = now()",
                (doc_id, primary, also))
            for name, on in body.topics.items():
                name = name[:200]
                conn.execute(
                    "INSERT INTO document_topic_overrides (document_id, topic_name, include) VALUES (%s, %s, %s) "
                    "ON CONFLICT (document_id, topic_name) DO UPDATE SET include = EXCLUDED.include",
                    (doc_id, name, on))
                if on:
                    conn.execute("INSERT INTO document_topics (document_id, topic_id) SELECT %s, id FROM topics "
                                 "WHERE name = %s ON CONFLICT DO NOTHING", (doc_id, name))
                else:
                    conn.execute("DELETE FROM document_topics WHERE document_id = %s AND topic_id IN "
                                 "(SELECT id FROM topics WHERE name = %s)", (doc_id, name))
            conn.execute("UPDATE topics t SET paper_count = (SELECT count(*) FROM document_topics dt "
                         "WHERE dt.topic_id = t.id)")
            return _doc_out(get_doc(conn, doc_id))

    @app.post("/api/library/classify", status_code=202)
    def classify_library(request: Request, force: bool = False):
        """Place the papers in research areas. Without force only the papers that have no area yet are placed."""
        with pool(request).connection() as conn, conn.transaction():
            job = jobs.enqueue_library(conn, "areas", {"force": force})
        return {"queued": job is not None}

    @app.get("/api/library/topics")
    def library_topics(request: Request):
        with pool(request).connection() as conn:
            rows = conn.execute("SELECT id, name, terms, area, paper_count FROM topics").fetchall()
        out = [{"id": r["id"], "name": r["name"], "terms": list(r["terms"]), "area": r["area"],
                "documents": r["paper_count"]} for r in rows]
        return sorted(out, key=lambda t: (-t["documents"], t["name"]))

    @app.post("/api/library/topics/rebuild", status_code=202)
    def rebuild_topics(request: Request):
        with pool(request).connection() as conn, conn.transaction():
            job = jobs.enqueue_library(conn, "topics")
        return {"queued": job is not None}

    @app.get("/api/library/timeline")
    def library_timeline(request: Request, topic: int | None = None, term: str | None = Query(None, max_length=80)):
        """Papers and articles that have a topic's keywords (or one keyword), in date order."""
        with pool(request).connection() as conn:
            if topic is not None:
                row = conn.execute("SELECT name, terms FROM topics WHERE id = %s", (topic,)).fetchone()
                if row is None:
                    raise HTTPException(404, "Topic not found")
                label, terms = row["name"], list(row["terms"])
                rows = conn.execute(
                    "SELECT d.id, d.title, d.published, d.status, d.listened, d.source_type FROM documents d "
                    "JOIN document_topics dt ON dt.document_id = d.id WHERE dt.topic_id = %s", (topic,)).fetchall()
            elif term and term.strip():
                label, terms = term.strip(), [term.strip().lower()]
                rows = conn.execute(
                    "SELECT d.id, d.title, d.published, d.status, d.listened, d.source_type FROM documents d "
                    "WHERE EXISTS (SELECT 1 FROM document_keywords k WHERE k.document_id = d.id AND k.term = ANY(%s))",
                    (terms,)).fetchall()
            else:
                raise HTTPException(422, "Give a topic or a keyword")
        items = []
        for r in rows:
            dated = library.publication_date(r["published"])
            items.append({"id": str(r["id"]), "title": r["title"] or "Untitled", "date": dated[0] if dated else None,
                          "precision": dated[1] if dated else None, "source": r["source_type"],
                          "status": r["status"], "listened": r["listened"]})
        items.sort(key=lambda i: (i["date"] is None, i["date"] or "", i["title"]))
        return {"label": label, "terms": terms, "items": items}

    @app.get("/api/library/graph")
    def library_graph(request: Request, filters: dict = Depends(parse_filters)):
        """Nodes are papers, edges connect papers with similar terms; `group` numbers the groups."""
        vecs, docs = _library_vectors(request, filters)
        group_of: dict[str, int] = {}
        for n, members in enumerate(library.clusters(vecs)):
            for m in members:
                group_of[m] = n
        nodes = [{**docs[i], "group": group_of.get(i, -1)} for i in docs]
        links = []
        for a, b, weight in library.edges(vecs):
            links.append({"source": a, "target": b, "weight": weight,
                          "terms": library.shared_terms(vecs[a], vecs[b])})
        return {"nodes": nodes, "links": links}

    def _library_vectors(request: Request, filters: dict[str, Any]):
        clause, params = filter_sql(filters)
        with pool(request).connection() as conn:
            rows = conn.execute(
                "SELECT d.id, k.kind, k.term, k.weight FROM documents d "
                f"JOIN document_keywords k ON k.document_id = d.id WHERE TRUE{clause}", params,
            ).fetchall()
            meta = conn.execute(
                f"SELECT d.id, d.title, d.published, d.status, d.listened, d.source_type, d.duration_s "
                f"FROM documents d WHERE TRUE{clause}", params).fetchall()
        docs = {str(m["id"]): {"id": str(m["id"]), "title": m["title"] or "Untitled",
                               "year": (m["published"] or "")[:4] or None, "status": m["status"],
                               "listened": m["listened"], "source": m["source_type"]} for m in meta}
        vecs = library.vectors((str(r["id"]), r["kind"], r["term"], r["weight"]) for r in rows)
        return vecs, docs

    @app.post("/api/documents", status_code=201)
    def upload(request: Request, file: UploadFile = File(...)):
        limit = settings.max_upload_mb * 1024 * 1024
        if int(request.headers.get("content-length") or 0) > limit + 64 * 1024:
            raise HTTPException(413, f"PDFs larger than {settings.max_upload_mb} MB are not accepted")
        head = file.file.read(5)
        if head != b"%PDF-":
            raise HTTPException(415, "This file is not a PDF")
        with pool(request).connection() as conn, conn.transaction():
            row = conn.execute(
                "INSERT INTO documents (source_type, original_filename, title, has_pdf) "
                "VALUES ('pdf', %s, %s, true) RETURNING id",
                (Path(file.filename or "upload.pdf").name[:300], _title_from_filename(file.filename or "")),
            ).fetchone()
            paths = DocPaths(settings.docs_dir, row["id"]).ensure()
            try:
                with paths.source_pdf.open("wb") as out:
                    out.write(head)
                    size = len(head)
                    while chunk := file.file.read(1024 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise HTTPException(413, f"PDFs larger than {settings.max_upload_mb} MB are not accepted")
                        out.write(chunk)
            except BaseException:
                paths.remove()
                raise
            jobs.enqueue(conn, row["id"])
            return _doc_out(get_doc(conn, row["id"]))

    @app.post("/api/documents/url", status_code=201)
    def add_url(request: Request, body: UrlIn):
        url = body.url.strip()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise HTTPException(422, "Enter a full http(s) URL")
        with pool(request).connection() as conn, conn.transaction():
            row = conn.execute(
                "INSERT INTO documents (source_type, source_url, title) VALUES ('url', %s, %s) RETURNING id",
                (url, url),
            ).fetchone()
            DocPaths(settings.docs_dir, row["id"]).ensure()
            jobs.enqueue(conn, row["id"])
            return _doc_out(get_doc(conn, row["id"]))

    @app.get("/api/documents/{doc_id}")
    def document(request: Request, doc_id: UUID):
        with pool(request).connection() as conn:
            out = _doc_out(get_doc(conn, doc_id))
            out["chapters"] = [
                {"idx": c["idx"], "kind": c["kind"], "title": c["title"], "intro": c["intro"],
                 "start_page": c["start_page"], "word_count": c["word_count"],
                 "duration_s": c["duration_s"], "ready": c["audio_file"] is not None}
                for c in conn.execute("SELECT * FROM chapters WHERE document_id = %s ORDER BY idx", (doc_id,))
            ]
            out["keywords"] = _keywords_by_kind(conn.execute(
                "SELECT kind, term FROM document_keywords WHERE document_id = %s ORDER BY weight DESC, term",
                (doc_id,)).fetchall())
        return out

    @app.get("/api/documents/{doc_id}/questions")
    def list_questions(request: Request, doc_id: UUID):
        with pool(request).connection() as conn:
            get_doc(conn, doc_id)
            rows = conn.execute(
                "SELECT * FROM questions WHERE document_id = %s ORDER BY id DESC LIMIT 50", (doc_id,)).fetchall()
        return [_question_out(r) for r in rows]

    @app.post("/api/documents/{doc_id}/questions", status_code=202)
    def ask_question(request: Request, doc_id: UUID, body: QuestionIn):
        with pool(request).connection() as conn, conn.transaction():
            row = get_doc(conn, doc_id)
            if row["status"] != "ready":
                raise HTTPException(409, "Ask once the paper has been processed")
            question = conn.execute(
                "INSERT INTO questions (document_id, question) VALUES (%s, %s) RETURNING *",
                (doc_id, body.question.strip())).fetchone()
            jobs.enqueue_question(conn, doc_id, question["id"])
        return _question_out(question)

    @app.post("/api/documents/{doc_id}/queue", status_code=202)
    def queue_document(request: Request, doc_id: UUID):
        """Start processing a catalogued item (a Zotero entry that is not queued yet)."""
        with pool(request).connection() as conn, conn.transaction():
            row = get_doc(conn, doc_id)
            if row["status"] != "catalogued":
                raise HTTPException(409, "This item is already queued or processed")
            if not (row["source_type"] == "pdf" or row["source_url"]):
                raise HTTPException(422, "This item has no PDF or link in Zotero to process")
            jobs.enqueue(conn, doc_id)
            return _doc_out(get_doc(conn, doc_id))

    @app.get("/api/zotero")
    def zotero_status(request: Request):
        with pool(request).connection() as conn:
            state = conn.execute("SELECT * FROM zotero_state WHERE id = 1").fetchone()
            catalogued = conn.execute("SELECT count(*) AS n FROM documents WHERE status = 'catalogued'").fetchone()["n"]
            syncing = conn.execute("SELECT 1 FROM jobs WHERE kind = 'zotero_sync' AND state IN ('queued', 'running')"
                                   ).fetchone() is not None
        return {
            "configured": settings.zotero_configured,
            "last_synced_at": state["last_synced_at"],
            "last_error": state["last_error"],
            "catalogued": catalogued,
            "syncing": syncing,
        }

    @app.post("/api/zotero/queue-all", status_code=202)
    def zotero_queue_all(request: Request):
        """Start processing every catalogued item that has a PDF or a link. Only ever started by hand."""
        with pool(request).connection() as conn, conn.transaction():
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM documents WHERE status = 'catalogued' AND (source_type = 'pdf' OR source_url IS NOT NULL) "
                "ORDER BY title")]
            for doc_id in ids:
                jobs.enqueue(conn, doc_id)
        return {"queued": len(ids)}

    @app.post("/api/zotero/sync", status_code=202)
    def zotero_sync(request: Request):
        if not settings.zotero_configured:
            raise HTTPException(503, "Zotero is not configured: set ZOTERO_API_KEY in .env")
        with pool(request).connection() as conn, conn.transaction():
            job = jobs.enqueue_library(conn, "zotero_sync")
        return {"queued": job is not None}

    @app.post("/api/library/keywords/backfill", status_code=202)
    def backfill_keywords(request: Request):
        """Queue keyword extraction for ready papers that have no keywords yet (e.g. from before it existed)."""
        with pool(request).connection() as conn, conn.transaction():
            ids = [r["id"] for r in conn.execute(
                "SELECT d.id FROM documents d WHERE d.status = 'ready' "
                "AND NOT EXISTS (SELECT 1 FROM document_keywords k WHERE k.document_id = d.id) "
                "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.document_id = d.id AND j.state IN ('queued', 'running'))"
            )]
            for doc_id in ids:
                jobs.enqueue(conn, doc_id, {"from": "enrich"})
        return {"queued": len(ids)}

    @app.patch("/api/documents/{doc_id}")
    def patch_document(request: Request, doc_id: UUID, body: DocPatch):
        with pool(request).connection() as conn:
            get_doc(conn, doc_id)
            if body.title is not None and body.title.strip():
                conn.execute("UPDATE documents SET title = %s, updated_at = now() WHERE id = %s",
                             (body.title.strip(), doc_id))
            if body.listened is not None:
                conn.execute("UPDATE documents SET listened = %s, updated_at = now() WHERE id = %s",
                             (body.listened, doc_id))
            return _doc_out(get_doc(conn, doc_id))

    @app.delete("/api/documents/{doc_id}", status_code=204)
    def delete_document(request: Request, doc_id: UUID):
        with pool(request).connection() as conn:
            cur = conn.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Document not found")
        DocPaths(settings.docs_dir, doc_id).remove()

    @app.post("/api/documents/{doc_id}/rerun", status_code=202)
    def rerun(request: Request, doc_id: UUID, body: RerunIn = Body(default_factory=RerunIn)):
        if body.stage not in RERUN_STAGES:
            raise HTTPException(422, f"stage must be one of {', '.join(RERUN_STAGES)}")
        with pool(request).connection() as conn, conn.transaction():
            row = get_doc(conn, doc_id)
            if jobs.has_open_job(conn, doc_id):
                raise HTTPException(409, "This document is already being processed")
            if body.stage == "start" and row["source_type"] == "url":
                paths = DocPaths(settings.docs_dir, doc_id)
                paths.source_pdf.unlink(missing_ok=True)
                paths.source_html.unlink(missing_ok=True)
            jobs.enqueue(conn, doc_id, {"from": RERUN_STAGES[body.stage]})
            return _doc_out(get_doc(conn, doc_id))

    # ------------------------------------------------------------------ content
    @app.get("/api/documents/{doc_id}/pdf")
    def pdf(request: Request, doc_id: UUID):
        paths = DocPaths(settings.docs_dir, doc_id)
        target = paths.viewer_pdf if paths.viewer_pdf.exists() else paths.source_pdf
        if not target.exists():
            raise HTTPException(404, "No PDF for this document")
        return FileResponse(target, media_type="application/pdf",
                            headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/documents/{doc_id}/chapters/{idx}/audio")
    def chapter_audio(doc_id: UUID, idx: int):
        f = DocPaths(settings.docs_dir, doc_id).audio(idx)
        if idx < 0 or not f.exists():
            raise HTTPException(404, "This chapter has no audio yet")
        return FileResponse(f, media_type="audio/mp4", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/documents/{doc_id}/chapters/{idx}/sentences")
    def chapter_sentences(request: Request, doc_id: UUID, idx: int):
        with pool(request).connection() as conn:
            rows = conn.execute(
                "SELECT s.idx, s.text, s.page, s.start_s, s.end_s, s.rects FROM sentences s "
                "JOIN chapters c ON c.id = s.chapter_id WHERE c.document_id = %s AND c.idx = %s ORDER BY s.idx",
                (doc_id, idx),
            ).fetchall()
        return [{"i": r["idx"], "text": r["text"], "page": r["page"], "start": r["start_s"], "end": r["end_s"],
                 "rects": r["rects"] or []} for r in rows]

    @app.get("/api/documents/{doc_id}/text")
    def document_text(doc_id: UUID):
        """Readable text per chapter, for articles without a PDF (available before audio is done)."""
        paths = DocPaths(settings.docs_dir, doc_id)
        if paths.chapters.exists():
            data = json.loads(paths.chapters.read_text())
            return [{"idx": i, "title": c["title"], "intro": c.get("intro"),
                     "paragraphs": [{"text": p["text"], "kind": p["kind"]} for p in c["paragraphs"]]}
                    for i, c in enumerate(data)]
        if paths.structure.exists():
            data = json.loads(paths.structure.read_text())
            return [{"idx": None, "title": s["title"], "intro": None,
                     "paragraphs": [{"text": p["text"], "kind": p["kind"]} for p in s["paragraphs"]]}
                    for s in data["sections"]]
        return []

    # ------------------------------------------------------------------ playback & settings
    @app.get("/api/documents/{doc_id}/playback")
    def get_playback(request: Request, doc_id: UUID):
        with pool(request).connection() as conn:
            row = conn.execute("SELECT * FROM playback WHERE document_id = %s", (doc_id,)).fetchone()
        return {"chapter_idx": 0, "position_s": 0, "rate": 1.0} if row is None else {
            "chapter_idx": row["chapter_idx"], "position_s": row["position_s"], "rate": row["rate"]}

    @app.put("/api/documents/{doc_id}/playback")
    def put_playback(request: Request, doc_id: UUID, body: PlaybackIn):
        with pool(request).connection() as conn, conn.transaction():
            get_doc(conn, doc_id)
            conn.execute(
                "INSERT INTO playback (document_id, chapter_idx, position_s, rate, updated_at) "
                "VALUES (%s, %s, %s, %s, now()) ON CONFLICT (document_id) DO UPDATE SET "
                "chapter_idx = EXCLUDED.chapter_idx, position_s = EXCLUDED.position_s, rate = EXCLUDED.rate, "
                "updated_at = now()",
                (doc_id, body.chapter_idx, body.position_s, body.rate),
            )
            conn.execute("UPDATE documents SET last_listened_at = now() WHERE id = %s", (doc_id,))
        return {"ok": True}

    @app.get("/api/settings")
    def get_settings(request: Request):
        with pool(request).connection() as conn:
            values = appsettings.get_all(conn)
        return {"values": values, "voices": KOKORO_VOICES, "engines": ["kokoro", "piper"]}

    @app.put("/api/settings")
    def put_settings(request: Request, body: dict[str, Any] = Body(...)):
        if "voice" in body and body["voice"] not in KOKORO_VOICES:
            raise HTTPException(422, "Unknown voice")
        try:
            with pool(request).connection() as conn:
                values = appsettings.update(conn, body)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return {"values": values, "voices": KOKORO_VOICES, "engines": ["kokoro", "piper"]}

    # ------------------------------------------------------------------ single-page app
    static = settings.static_dir
    if static is not None:
        root = static.resolve()

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                return JSONResponse({"detail": "Not found"}, status_code=404)
            candidate = (root / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(root):
                cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
                return FileResponse(candidate, headers={"Cache-Control": cache})
            return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=8000, proxy_headers=True, forwarded_allow_ips="*",
                access_log=False)
