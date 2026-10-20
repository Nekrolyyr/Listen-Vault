"""Foreground api: library, uploads, job status, PDF and audio streaming, playback state.

It never runs ML; it writes rows and files, and the worker picks the jobs up. Every paper is seen through the
signed-in user's library entries: a paper outside the library answers 404, as if it did not exist.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import Body, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from pypdf import PdfReader

from .. import accounts, appsettings, areas, config, db, documents, embeddings, jobs, library
from ..storage import DocPaths
from .admin import register_admin_routes
from .deps import admin_only, pool, uid, user_of
from .account import register_account_routes
from .security import AuthMiddleware

log = logging.getLogger("listen.api")

KOKORO_VOICES = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova",
    "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx", "am_puck", "am_santa", "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]
RERUN_STAGES = {"start": "ocr", "structure": "structure", "chapters": "chapters", "synthesis": "synthesis"}

# The documents of the signed-in user's library, with their state for that user. Every document query starts
# from this join, so a paper that is not in the library is never found.
LIB_FROM = "documents d JOIN library_entries le ON le.document_id = d.id AND le.user_id = %(uid)s"

DOC_LIST_SQL = """
SELECT d.*, le.listened, le.last_listened_at,
       j.state AS job_state, j.stage AS job_stage, j.progress_done, j.progress_total,
       j.error AS job_error, j.run_after AS job_run_after,
       (SELECT count(*) FROM chapters c WHERE c.document_id = d.id) AS chapter_count,
       (SELECT count(*) FROM chapters c WHERE c.document_id = d.id AND c.audio_file IS NOT NULL) AS ready_count,
       p.chapter_idx AS play_chapter, p.position_s AS play_position,
       (SELECT c.title FROM chapters c WHERE c.document_id = d.id AND c.idx = p.chapter_idx) AS play_chapter_title,
       (SELECT coalesce(sum(c.duration_s), 0) FROM chapters c
          WHERE c.document_id = d.id AND c.idx < p.chapter_idx) AS play_before_s,
       (SELECT coalesce(array_agg(t.term ORDER BY t.weight DESC), '{}') FROM (
            SELECT k.term, k.weight FROM document_keywords k
            WHERE k.document_id = d.id AND k.kind IN ('task', 'method', 'topic')
            ORDER BY k.weight DESC, k.term LIMIT 4) t) AS top_terms,
       (SELECT coalesce(json_agg(json_build_object('id', t.id, 'name', t.name) ORDER BY t.name), '[]'::json)
          FROM document_topics dt JOIN topics t ON t.id = dt.topic_id WHERE dt.document_id = d.id) AS doc_topics,
       ar.primary_area AS area_primary, ar.also_areas AS area_also, ar.confidence AS area_confidence,
       ar.source AS area_source, ar.pinned AS area_pinned
FROM documents d
JOIN library_entries le ON le.document_id = d.id AND le.user_id = %(uid)s
LEFT JOIN LATERAL (SELECT * FROM jobs WHERE document_id = d.id ORDER BY id DESC LIMIT 1) j ON true
LEFT JOIN playback p ON p.document_id = d.id AND p.user_id = %(uid)s
LEFT JOIN document_areas ar ON ar.document_id = d.id
"""
SORTS = {
    "added": "le.added_at DESC",
    "listened": "le.last_listened_at DESC NULLS LAST, le.added_at DESC",
    "title": "lower(d.title) ASC",
}


def parse_filters(
    term: list[str] = Query(default=[], max_length=20),
    year_from: int | None = Query(default=None, ge=1000, le=2999),
    year_to: int | None = Query(default=None, ge=1000, le=2999),
    source: str | None = Query(default=None, pattern=r"^(pdf|url)$"),
    listened: bool | None = None,
    status: str | None = Query(default=None, pattern=r"^(queued|processing|ready|failed)$"),
    area: str | None = Query(default=None, pattern=r"^[a-z_]{2,20}$"),
    topic: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    """Library filters shared by the list, the facets and the graph. Terms must all match.
    Years are inclusive; a paper without a publication year is outside any year range. An area matches the
    primary and the other areas of a paper; a topic matches the papers on it."""
    return {"term": [t.strip().lower() for t in term if t.strip()][:20], "year_from": year_from,
            "year_to": year_to, "source": source, "listened": listened, "status": status,
            "area": area, "topic": topic}


def filter_sql(f: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """SQL condition (starting with AND, or empty) for the filters, over the aliases `d` and `le`."""
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
    if f["source"] in ("pdf", "url"):
        clauses.append("d.source_type = %(source)s")
        params["source"] = f["source"]
    if f["listened"] is not None:
        clauses.append("le.listened = %(listened)s")
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
        "pages": r["pages"], "listened": r["listened"], "last_listened_at": r["last_listened_at"],
        "created_at": r["created_at"], "abstract": r.get("abstract"),
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
    return out


KEYWORD_KINDS = ("task", "method", "dataset", "topic", "keyword")


def library_year_bounds(conn, user_id: int) -> list[int] | None:
    """First and last publication year of the user's library (not the filtered set), for the year slider."""
    row = conn.execute(
        "SELECT min(y) AS lo, max(y) AS hi FROM (SELECT substring(d.published from '[0-9]{4}')::int AS y "
        f"FROM {LIB_FROM} WHERE d.published IS NOT NULL) t WHERE y IS NOT NULL", {"uid": user_id}).fetchone()
    return None if row["lo"] is None else [row["lo"], row["hi"]]


def _keywords_by_kind(rows) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {k: [] for k in KEYWORD_KINDS}
    for r in rows:
        out[r["kind"]].append(r["term"])
    return out


def _title_from_filename(name: str) -> str:
    stem = Path(name).stem
    return re.sub(r"[_\s]+", " ", stem).strip()[:300] or "Untitled PDF"


def _count_pages(path: Path) -> int:
    return len(PdfReader(str(path)).pages)


def create_app(settings: config.Settings | None = None) -> FastAPI:
    settings = settings or config.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.wait_for_db(settings.database_url)
        db.migrate(settings.database_url)
        settings.docs_dir.mkdir(parents=True, exist_ok=True)
        app.state.pool = db.make_pool(settings.database_url)
        app.state.pool.wait(timeout=30)
        if settings.terms_dir.is_dir():
            with app.state.pool.connection() as conn, conn.transaction():
                accounts.load_terms(conn, settings.terms_dir)
        if not settings.smtp_configured:
            log.warning("SMTP is not configured: new accounts are verified at once (set SMTP_HOST and SMTP_FROM)")
        yield
        app.state.pool.close()

    app = FastAPI(title="listen-vault", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.add_middleware(AuthMiddleware, proxy_secret=settings.proxy_secret)

    def get_doc(conn, doc_id: UUID | str, user_id: int) -> dict[str, Any]:
        row = conn.execute(DOC_LIST_SQL + " WHERE d.id = %(doc)s", {"uid": user_id, "doc": doc_id}).fetchone()
        if row is None:
            raise HTTPException(404, "Document not found")
        return row

    def require_terms(conn, user: dict[str, Any]) -> None:
        version = accounts.current_terms(conn, settings.terms_dir)
        if not accounts.has_current_consent(conn, user["id"], version):
            raise HTTPException(403, {"code": "terms_required",
                                      "message": "Accept the current terms of service before adding papers"})

    def require_job_room(conn, user: dict[str, Any]) -> None:
        """One user cannot fill the queue: a few papers at a time, the rest wait for their turn."""
        if jobs.open_for_user(conn, user["id"]) >= jobs.MAX_OPEN_PER_USER:
            raise HTTPException(429, {"code": "busy", "message": f"You already have {jobs.MAX_OPEN_PER_USER} papers "
                                                                 "waiting to be processed. Let them finish first."})

    def require_quota(conn, user: dict[str, Any], pages: int) -> None:
        status = accounts.quota_status(conn, user)
        if status["used"] + pages > status["quota"]:
            raise HTTPException(403, {
                "code": "quota_exceeded", "used": status["used"], "quota": status["quota"],
                "message": f"Your page quota is full ({status['used']} of {status['quota']} pages). "
                           "Remove papers or ask an administrator for more."})

    # ------------------------------------------------------------------ health & session
    @app.get("/api/health")
    def health(request: Request):
        with pool(request).connection() as conn:
            conn.execute("SELECT 1")
        return {"ok": True}

    register_account_routes(app, settings)
    register_admin_routes(app, settings)

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
                {**params, "uid": uid(request), "q": q.strip(), "like": like, "area_ids": area_ids},
            ).fetchall()
        return [_doc_out(r) for r in rows]

    @app.get("/api/library/facets")
    def library_facets(request: Request, filters: dict = Depends(parse_filters)):
        """Counts for the facet column and the dashboard, over the papers that match the filters."""
        clause, params = filter_sql(filters)
        params = {**params, "uid": uid(request)}
        with pool(request).connection() as conn:
            def count_by(expr: str) -> dict[str, int]:
                rows = conn.execute(f"SELECT {expr} AS v, count(*) AS n FROM {LIB_FROM} "
                                    f"WHERE TRUE{clause} GROUP BY 1", params).fetchall()
                return {str(r["v"]): r["n"] for r in rows if r["v"] is not None}

            totals = conn.execute(
                "SELECT count(*) AS n, coalesce(sum(d.duration_s), 0) AS audio, "
                "coalesce(sum(d.duration_s) FILTER (WHERE le.listened), 0) AS listened_audio "
                f"FROM {LIB_FROM} WHERE TRUE{clause}", params).fetchone()
            terms = conn.execute(
                "SELECT k.kind, k.term, count(*) AS n FROM document_keywords k "
                "JOIN documents d ON d.id = k.document_id "
                "JOIN library_entries le ON le.document_id = d.id AND le.user_id = %(uid)s "
                f"WHERE TRUE{clause} GROUP BY k.kind, k.term ORDER BY n DESC, k.term", params).fetchall()
            by_kind: dict[str, list[dict[str, Any]]] = {k: [] for k in KEYWORD_KINDS}
            for r in terms:
                if len(by_kind[r["kind"]]) < 40:
                    by_kind[r["kind"]].append({"term": r["term"], "count": r["n"]})
            return {
                "total": totals["n"],
                "audio_s": totals["audio"],
                "listened_audio_s": totals["listened_audio"],
                "status": count_by("d.status"),
                "listened": {str(k).lower(): v for k, v in count_by("le.listened").items()},
                "source": count_by("d.source_type"),
                "years": [{"year": int(y), "count": n}
                          for y, n in sorted(count_by("substring(d.published from '[0-9]{4}')").items())],
                "year_bounds": library_year_bounds(conn, params["uid"]),
                "terms": by_kind,
            }

    @app.get("/api/library/overview")
    def library_overview(request: Request):
        """The map: every research area with the user's papers, unread and progress, and the papers that need a look."""
        user_id = uid(request)
        with pool(request).connection() as conn:
            totals = conn.execute(
                "SELECT count(*) AS papers, count(*) FILTER (WHERE NOT le.listened) AS unread, "
                "count(*) FILTER (WHERE d.status = 'failed') AS failed, coalesce(sum(d.duration_s), 0) AS audio_s "
                f"FROM {LIB_FROM}", {"uid": user_id}).fetchone()
            unplaced = conn.execute(
                f"SELECT count(*) AS n FROM {LIB_FROM} LEFT JOIN document_areas a ON a.document_id = d.id "
                "WHERE a.document_id IS NULL AND d.status <> 'failed'", {"uid": user_id}).fetchone()["n"]
            per_area = {r["area"]: r for r in conn.execute(
                "SELECT u.area, count(*) AS papers, count(*) FILTER (WHERE le.listened) AS listened, "
                "count(*) FILTER (WHERE NOT le.listened) AS unread "
                "FROM document_areas a JOIN documents d ON d.id = a.document_id "
                "JOIN library_entries le ON le.document_id = d.id AND le.user_id = %(uid)s, "
                "LATERAL unnest(array[a.primary_area] || a.also_areas) AS u(area) GROUP BY u.area",
                {"uid": user_id}).fetchall()}
            topics_by_area: dict[str, list[dict[str, Any]]] = {}
            for t in conn.execute(
                    "SELECT t.id, t.name, t.area, count(*) AS papers FROM topics t "
                    "JOIN document_topics dt ON dt.topic_id = t.id "
                    "JOIN library_entries le ON le.document_id = dt.document_id AND le.user_id = %(uid)s "
                    "WHERE t.area IS NOT NULL GROUP BY t.id, t.name, t.area ORDER BY papers DESC, t.name",
                    {"uid": user_id}):
                topics_by_area.setdefault(t["area"], []).append(
                    {"id": t["id"], "name": t["name"], "papers": t["papers"]})
            attention = conn.execute(
                "SELECT d.id, d.title, d.status, a.confidence, a.pinned, a.document_id AS placed "
                f"FROM {LIB_FROM} LEFT JOIN document_areas a ON a.document_id = d.id "
                "WHERE d.status = 'failed' OR a.document_id IS NULL OR (a.confidence = 'low' AND NOT a.pinned) "
                "ORDER BY le.added_at DESC LIMIT 20", {"uid": user_id}).fetchall()
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
            "papers": totals["papers"], "unread": totals["unread"],
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
                "SELECT count(*) AS papers, count(*) FILTER (WHERE le.listened) AS listened, "
                "count(*) FILTER (WHERE NOT le.listened) AS unread "
                "FROM document_areas a JOIN documents d ON d.id = a.document_id "
                "JOIN library_entries le ON le.document_id = d.id AND le.user_id = %(uid)s "
                "WHERE a.primary_area = %(a)s OR %(a)s = ANY(a.also_areas)",
                {"uid": uid(request), "a": area_id}).fetchone()
            topic_rows = conn.execute(
                "SELECT t.id, t.name, t.terms, count(*) AS papers, count(*) FILTER (WHERE NOT le.listened) AS unread "
                "FROM topics t JOIN document_topics dt ON dt.topic_id = t.id "
                "JOIN library_entries le ON le.document_id = dt.document_id AND le.user_id = %(uid)s "
                "WHERE t.area = %(area)s GROUP BY t.id ORDER BY papers DESC, t.name",
                {"uid": uid(request), "area": area_id}).fetchall()
        return {
            "id": area_id, "name": areas.NAMES[area_id], "papers": totals["papers"],
            "listened": totals["listened"], "unread": totals["unread"],
            "topics": [{"id": r["id"], "name": r["name"], "terms": list(r["terms"])[:4], "papers": r["papers"],
                        "unread": r["unread"]} for r in topic_rows],
        }

    @app.get("/api/library/topics/{topic_id}")
    def library_topic(request: Request, topic_id: int):
        """A topic: its terms, the user's papers on it, related topics, and the topics before and after it in its area."""
        user_id = uid(request)
        with pool(request).connection() as conn:
            row = conn.execute("SELECT id, name, terms, area FROM topics WHERE id = %s", (topic_id,)).fetchone()
            counts = conn.execute(
                "SELECT count(*) AS papers, count(*) FILTER (WHERE NOT le.listened) AS unread FROM document_topics dt "
                "JOIN library_entries le ON le.document_id = dt.document_id AND le.user_id = %(uid)s "
                "WHERE dt.topic_id = %(topic)s", {"uid": user_id, "topic": topic_id}).fetchone() if row else None
            if row is None or counts["papers"] == 0:
                raise HTTPException(404, "Topic not found")
            related = conn.execute(
                "SELECT t2.id, t2.name, count(*) AS shared FROM document_topics a "
                "JOIN library_entries le ON le.document_id = a.document_id AND le.user_id = %(uid)s "
                "JOIN document_topics b ON b.document_id = a.document_id AND b.topic_id <> a.topic_id "
                "JOIN topics t2 ON t2.id = b.topic_id WHERE a.topic_id = %(topic)s "
                "GROUP BY t2.id, t2.name ORDER BY shared DESC, t2.name LIMIT 6",
                {"uid": user_id, "topic": topic_id}).fetchall()
            siblings = conn.execute(
                "SELECT t.id, t.name, count(*) AS papers FROM topics t "
                "JOIN document_topics dt ON dt.topic_id = t.id "
                "JOIN library_entries le ON le.document_id = dt.document_id AND le.user_id = %(uid)s "
                "WHERE t.area IS NOT DISTINCT FROM %(area)s GROUP BY t.id, t.name ORDER BY papers DESC, t.name",
                {"uid": user_id, "area": row["area"]}).fetchall()
        order = [{"id": r["id"], "name": r["name"]} for r in siblings]
        i = [r["id"] for r in siblings].index(topic_id)
        return {
            "id": row["id"], "name": row["name"], "terms": list(row["terms"]), "area": row["area"],
            "area_name": areas.NAMES.get(row["area"] or "", None), "papers": counts["papers"],
            "unread": counts["unread"],
            "related": [{"id": r["id"], "name": r["name"], "shared": r["shared"]} for r in related],
            "prev": order[i - 1] if i > 0 else None,
            "next": order[i + 1] if i + 1 < len(order) else None,
        }

    @app.patch("/api/documents/{doc_id}/classification")
    def put_classification(request: Request, doc_id: UUID, body: ClassificationIn):
        """A hand-made placement: the areas and topics chosen here are kept when the library is classified again.
        The placement is kept on the paper, so it is the same for every user who has the paper."""
        if body.primary not in areas.NAMES or any(a not in areas.NAMES for a in body.also):
            raise HTTPException(422, "Unknown area")
        primary, also = areas.clean(body.primary, body.also)
        with pool(request).connection() as conn, conn.transaction():
            get_doc(conn, doc_id, uid(request))
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
            return _doc_out(get_doc(conn, doc_id, uid(request)))

    @app.get("/api/library/topics")
    def library_topics(request: Request):
        with pool(request).connection() as conn:
            rows = conn.execute(
                "SELECT t.id, t.name, t.terms, t.area, count(*) AS documents FROM topics t "
                "JOIN document_topics dt ON dt.topic_id = t.id "
                "JOIN library_entries le ON le.document_id = dt.document_id AND le.user_id = %(uid)s "
                "GROUP BY t.id", {"uid": uid(request)}).fetchall()
        out = [{"id": r["id"], "name": r["name"], "terms": list(r["terms"]), "area": r["area"],
                "documents": r["documents"]} for r in rows]
        return sorted(out, key=lambda t: (-t["documents"], t["name"]))

    @app.get("/api/library/timeline")
    def library_timeline(request: Request, topic: int | None = None, term: str | None = Query(None, max_length=80)):
        """Papers in the user's library that have a topic's keywords (or one keyword), in date order."""
        with pool(request).connection() as conn:
            if topic is not None:
                row = conn.execute("SELECT name, terms FROM topics WHERE id = %s", (topic,)).fetchone()
                if row is None:
                    raise HTTPException(404, "Topic not found")
                label, terms = row["name"], list(row["terms"])
                rows = conn.execute(
                    "SELECT d.id, d.title, d.published, d.status, le.listened, d.source_type FROM "
                    f"{LIB_FROM} JOIN document_topics dt ON dt.document_id = d.id WHERE dt.topic_id = %(topic)s",
                    {"uid": uid(request), "topic": topic}).fetchall()
            elif term and term.strip():
                label, terms = term.strip(), [term.strip().lower()]
                rows = conn.execute(
                    "SELECT d.id, d.title, d.published, d.status, le.listened, d.source_type FROM "
                    f"{LIB_FROM} WHERE EXISTS (SELECT 1 FROM document_keywords k WHERE k.document_id = d.id "
                    "AND k.term = ANY(%(terms)s))", {"uid": uid(request), "terms": terms}).fetchall()
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
        """Nodes are the user's papers, edges connect papers with similar terms; `group` numbers the groups."""
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
        params = {**params, "uid": uid(request)}
        with pool(request).connection() as conn:
            rows = conn.execute(
                f"SELECT d.id, k.kind, k.term, k.weight FROM {LIB_FROM} "
                f"JOIN document_keywords k ON k.document_id = d.id WHERE TRUE{clause}", params,
            ).fetchall()
            meta = conn.execute(
                f"SELECT d.id, d.title, d.published, d.status, le.listened, d.source_type, d.duration_s "
                f"FROM {LIB_FROM} WHERE TRUE{clause}", params).fetchall()
        docs = {str(m["id"]): {"id": str(m["id"]), "title": m["title"] or "Untitled",
                               "year": (m["published"] or "")[:4] or None, "status": m["status"],
                               "listened": m["listened"], "source": m["source_type"]} for m in meta}
        vecs = library.vectors((str(r["id"]), r["kind"], r["term"], r["weight"]) for r in rows)
        return vecs, docs

    # ------------------------------------------------------------------ adding papers
    @app.post("/api/documents", status_code=201)
    def upload(request: Request, file: UploadFile = File(...)):
        """Add a PDF. A PDF already stored (same bytes) is not stored again: it joins the user's library."""
        user = user_of(request)
        limit = settings.max_upload_mb * 1024 * 1024
        if int(request.headers.get("content-length") or 0) > limit + 64 * 1024:
            raise HTTPException(413, f"PDFs larger than {settings.max_upload_mb} MB are not accepted")
        head = file.file.read(5)
        if head != b"%PDF-":
            raise HTTPException(415, "This file is not a PDF")
        incoming = settings.data_dir / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        tmp = incoming / f"{uuid4()}.pdf"
        digest = hashlib.sha256()
        try:
            with tmp.open("wb") as out:
                size = len(head)
                digest.update(head)
                out.write(head)
                while chunk := file.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"PDFs larger than {settings.max_upload_mb} MB are not accepted")
                    digest.update(chunk)
                    out.write(chunk)
            try:
                pages = _count_pages(tmp)
            except Exception as e:  # noqa: BLE001 - any parser failure means an unreadable file
                raise HTTPException(415, "This PDF could not be read") from e
            with pool(request).connection() as conn, conn.transaction():
                require_terms(conn, user)
                require_job_room(conn, user)
                existing = conn.execute("SELECT id, pages FROM documents WHERE content_sha256 = %s",
                                        (digest.hexdigest(),)).fetchone()
                if existing is not None:
                    if documents.in_library(conn, user["id"], existing["id"]):
                        raise HTTPException(409, "This paper is already in your library")
                    require_quota(conn, user, existing["pages"] or 0)
                    documents.add_entry(conn, user["id"], existing["id"])
                    return _doc_out(get_doc(conn, existing["id"], user["id"]))
                require_quota(conn, user, pages)
                row = conn.execute(
                    "INSERT INTO documents (source_type, original_filename, title, has_pdf, content_sha256, pages) "
                    "VALUES ('pdf', %s, %s, true, %s, %s) RETURNING id",
                    (Path(file.filename or "upload.pdf").name[:300], _title_from_filename(file.filename or ""),
                     digest.hexdigest(), pages)).fetchone()
                paths = DocPaths(settings.docs_dir, row["id"]).ensure()
                tmp.replace(paths.source_pdf)
                documents.add_entry(conn, user["id"], row["id"])
                jobs.enqueue(conn, row["id"], user_id=user["id"])
                return _doc_out(get_doc(conn, row["id"], user["id"]))
        finally:
            tmp.unlink(missing_ok=True)

    @app.post("/api/documents/url", status_code=201)
    def add_url(request: Request, body: UrlIn):
        user = user_of(request)
        url = body.url.strip()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise HTTPException(422, "Enter a full http(s) URL")
        with pool(request).connection() as conn, conn.transaction():
            require_terms(conn, user)
            # A page's size is known only after it is read, so a link is refused only when the quota is already full.
            require_quota(conn, user, 0)
            require_job_room(conn, user)
            existing = conn.execute("SELECT id FROM documents WHERE source_type = 'url' AND source_url = %s "
                                    "ORDER BY created_at LIMIT 1", (url,)).fetchone()
            if existing is not None:
                if documents.in_library(conn, user["id"], existing["id"]):
                    raise HTTPException(409, "This link is already in your library")
                documents.add_entry(conn, user["id"], existing["id"])
                return _doc_out(get_doc(conn, existing["id"], user["id"]))
            row = conn.execute(
                "INSERT INTO documents (source_type, source_url, title) VALUES ('url', %s, %s) RETURNING id",
                (url, url),
            ).fetchone()
            DocPaths(settings.docs_dir, row["id"]).ensure()
            documents.add_entry(conn, user["id"], row["id"])
            jobs.enqueue(conn, row["id"], user_id=user["id"])
            return _doc_out(get_doc(conn, row["id"], user["id"]))

    @app.get("/api/documents/{doc_id}")
    def document(request: Request, doc_id: UUID):
        with pool(request).connection() as conn:
            out = _doc_out(get_doc(conn, doc_id, uid(request)))
            out["chapters"] = [
                {"idx": c["idx"], "kind": c["kind"], "title": c["title"], "intro": c["intro"],
                 "start_page": c["start_page"], "word_count": c["word_count"],
                 "duration_s": c["duration_s"], "ready": c["audio_file"] is not None}
                for c in conn.execute("SELECT * FROM chapters WHERE document_id = %s ORDER BY idx", (doc_id,))
            ]
            out["keywords"] = _keywords_by_kind(conn.execute(
                "SELECT kind, term FROM document_keywords WHERE document_id = %s ORDER BY weight DESC, term",
                (doc_id,)).fetchall())
            ref = conn.execute("SELECT work_id, topics FROM document_openalex WHERE document_id = %s",
                               (doc_id,)).fetchone()
            out["reference_topics"] = ref["topics"] if ref and ref["work_id"] else []
        return out

    @app.get("/api/documents/{doc_id}/related")
    def related_papers(request: Request, doc_id: UUID):
        """Papers in the user's library whose SPECTER2 vectors are closest to this one (empty until it has a vector)."""
        with pool(request).connection() as conn:
            get_doc(conn, doc_id, uid(request))
            ranked = embeddings.related(conn, str(doc_id), k=200)
            mine = {str(r["document_id"]) for r in conn.execute(
                "SELECT document_id FROM library_entries WHERE user_id = %s", (uid(request),)).fetchall()}
        return {"papers": [p for p in ranked if p["id"] in mine][:10]}

    @app.get("/api/library/review")
    def review_areas(request: Request, limit: int = Query(50, ge=1, le=200)):
        """Papers whose area is disputed: fewer than 2 of their 5 nearest filed papers share it (agreement < 0.4).
        Pinned papers are the user's own placements and are not listed. Library-wide, so administrators only."""
        admin_only(request)
        with pool(request).connection() as conn:
            rows = conn.execute(
                "SELECT d.id, d.title, a.primary_area, a.agreement, a.source FROM document_areas a "
                "JOIN documents d ON d.id = a.document_id WHERE NOT a.pinned AND a.agreement IS NOT NULL "
                "AND a.agreement < 0.4 ORDER BY a.agreement, d.created_at LIMIT %s", (limit,)).fetchall()
        return {"papers": [{"id": str(r["id"]), "title": r["title"], "area": r["primary_area"],
                            "agreement": r["agreement"], "source": r["source"]} for r in rows]}

    @app.get("/api/documents/{doc_id}/questions")
    def list_questions(request: Request, doc_id: UUID):
        with pool(request).connection() as conn:
            get_doc(conn, doc_id, uid(request))
            rows = conn.execute(
                "SELECT * FROM questions WHERE document_id = %s AND user_id = %s ORDER BY id DESC LIMIT 50",
                (doc_id, uid(request))).fetchall()
        return [_question_out(r) for r in rows]

    @app.post("/api/documents/{doc_id}/questions", status_code=202)
    def ask_question(request: Request, doc_id: UUID, body: QuestionIn):
        with pool(request).connection() as conn, conn.transaction():
            row = get_doc(conn, doc_id, uid(request))
            if row["status"] != "ready":
                raise HTTPException(409, "Ask once the paper has been processed")
            question = conn.execute(
                "INSERT INTO questions (document_id, question, user_id) VALUES (%s, %s, %s) RETURNING *",
                (doc_id, body.question.strip(), uid(request))).fetchone()
            jobs.enqueue_question(conn, doc_id, question["id"], uid(request))
        return _question_out(question)

    # ------------------------------------------------------------------ changing and removing
    @app.patch("/api/documents/{doc_id}")
    def patch_document(request: Request, doc_id: UUID, body: DocPatch):
        """The title is shared by everyone who has the paper; listened state is the user's own."""
        user_id = uid(request)
        with pool(request).connection() as conn, conn.transaction():
            get_doc(conn, doc_id, user_id)
            if body.title is not None and body.title.strip():
                conn.execute("UPDATE documents SET title = %s, updated_at = now() WHERE id = %s",
                             (body.title.strip(), doc_id))
            if body.listened is not None:
                conn.execute("UPDATE library_entries SET listened = %s WHERE user_id = %s AND document_id = %s",
                             (body.listened, user_id, doc_id))
            return _doc_out(get_doc(conn, doc_id, user_id))

    @app.delete("/api/documents/{doc_id}", status_code=204)
    def delete_document(request: Request, doc_id: UUID):
        """Take the paper out of the user's library. The stored copy is deleted when no library holds it."""
        with pool(request).connection() as conn, conn.transaction():
            removed, orphans = documents.remove_entry(conn, uid(request), doc_id)
        if not removed:
            raise HTTPException(404, "Document not found")
        documents.delete_files(settings.docs_dir, orphans)

    @app.post("/api/documents/{doc_id}/rerun", status_code=202)
    def rerun(request: Request, doc_id: UUID, body: RerunIn = Body(default_factory=RerunIn)):
        if body.stage not in RERUN_STAGES:
            raise HTTPException(422, f"stage must be one of {', '.join(RERUN_STAGES)}")
        with pool(request).connection() as conn, conn.transaction():
            row = get_doc(conn, doc_id, uid(request))
            if jobs.has_open_job(conn, doc_id):
                raise HTTPException(409, "This document is already being processed")
            require_job_room(conn, user_of(request))
            if body.stage == "start" and row["source_type"] == "url":
                paths = DocPaths(settings.docs_dir, doc_id)
                paths.source_pdf.unlink(missing_ok=True)
                paths.source_html.unlink(missing_ok=True)
            jobs.enqueue(conn, doc_id, {"from": RERUN_STAGES[body.stage]}, user_id=uid(request))
            return _doc_out(get_doc(conn, doc_id, uid(request)))

    # ------------------------------------------------------------------ content
    def member_or_404(request: Request, doc_id: UUID) -> None:
        with pool(request).connection() as conn:
            if not documents.in_library(conn, uid(request), doc_id):
                raise HTTPException(404, "Document not found")

    @app.get("/api/documents/{doc_id}/pdf")
    def pdf(request: Request, doc_id: UUID):
        member_or_404(request, doc_id)
        paths = DocPaths(settings.docs_dir, doc_id)
        target = paths.viewer_pdf if paths.viewer_pdf.exists() else paths.source_pdf
        if not target.exists():
            raise HTTPException(404, "No PDF for this document")
        return FileResponse(target, media_type="application/pdf",
                            headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/documents/{doc_id}/chapters/{idx}/audio")
    def chapter_audio(request: Request, doc_id: UUID, idx: int):
        member_or_404(request, doc_id)
        f = DocPaths(settings.docs_dir, doc_id).audio(idx)
        if idx < 0 or not f.exists():
            raise HTTPException(404, "This chapter has no audio yet")
        return FileResponse(f, media_type="audio/mp4", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/documents/{doc_id}/chapters/{idx}/sentences")
    def chapter_sentences(request: Request, doc_id: UUID, idx: int):
        member_or_404(request, doc_id)
        with pool(request).connection() as conn:
            rows = conn.execute(
                "SELECT s.idx, s.text, s.page, s.start_s, s.end_s, s.rects FROM sentences s "
                "JOIN chapters c ON c.id = s.chapter_id WHERE c.document_id = %s AND c.idx = %s ORDER BY s.idx",
                (doc_id, idx),
            ).fetchall()
        return [{"i": r["idx"], "text": r["text"], "page": r["page"], "start": r["start_s"], "end": r["end_s"],
                 "rects": r["rects"] or []} for r in rows]

    @app.get("/api/documents/{doc_id}/text")
    def document_text(request: Request, doc_id: UUID):
        """Readable text per chapter, for articles without a PDF (available before audio is done)."""
        member_or_404(request, doc_id)
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
        member_or_404(request, doc_id)
        with pool(request).connection() as conn:
            row = conn.execute("SELECT * FROM playback WHERE document_id = %s AND user_id = %s",
                               (doc_id, uid(request))).fetchone()
        return {"chapter_idx": 0, "position_s": 0, "rate": 1.0} if row is None else {
            "chapter_idx": row["chapter_idx"], "position_s": row["position_s"], "rate": row["rate"]}

    @app.put("/api/documents/{doc_id}/playback")
    def put_playback(request: Request, doc_id: UUID, body: PlaybackIn):
        user_id = uid(request)
        with pool(request).connection() as conn, conn.transaction():
            if not documents.in_library(conn, user_id, doc_id):
                raise HTTPException(404, "Document not found")
            conn.execute(
                "INSERT INTO playback (user_id, document_id, chapter_idx, position_s, rate, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, now()) ON CONFLICT (user_id, document_id) DO UPDATE SET "
                "chapter_idx = EXCLUDED.chapter_idx, position_s = EXCLUDED.position_s, rate = EXCLUDED.rate, "
                "updated_at = now()",
                (user_id, doc_id, body.chapter_idx, body.position_s, body.rate),
            )
            conn.execute("UPDATE library_entries SET last_listened_at = now() WHERE user_id = %s AND document_id = %s",
                         (user_id, doc_id))
        return {"ok": True}

    @app.get("/api/settings")
    def get_settings(request: Request):
        with pool(request).connection() as conn:
            values = appsettings.get_all(conn)
        return {"values": values, "voices": KOKORO_VOICES, "engines": ["kokoro", "piper"]}

    @app.put("/api/settings")
    def put_settings(request: Request, body: dict[str, Any] = Body(...)):
        """Voice and speech settings. Shared by every user for now: audio is generated once per paper."""
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
