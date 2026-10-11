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

from fastapi import Body, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .. import appsettings, config, db, jobs
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
       (z.document_id IS NOT NULL) AS from_zotero
FROM documents d
LEFT JOIN LATERAL (SELECT * FROM jobs WHERE document_id = d.id ORDER BY id DESC LIMIT 1) j ON true
LEFT JOIN playback p ON p.document_id = d.id
LEFT JOIN zotero_links z ON z.document_id = d.id
"""
SORTS = {
    "added": "d.created_at DESC",
    "listened": "d.last_listened_at DESC NULLS LAST, d.created_at DESC",
    "title": "lower(d.title) ASC",
}


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


def _question_out(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r["id"], "question": r["question"], "answer": r["answer"], "citations": r["citations"],
            "status": r["status"], "error": r["error"], "created_at": r["created_at"]}


def _doc_out(r: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": str(r["id"]), "title": r["title"] or r.get("original_filename") or r.get("source_url") or "Untitled",
        "authors": r["authors"], "published": r["published"], "source_type": r["source_type"],
        "source_url": r["source_url"], "has_pdf": r["has_pdf"], "ocr_applied": r["ocr_applied"],
        "status": r["status"], "error": r["error"], "duration_s": r["duration_s"], "voice": r["voice"],
        "listened": r["listened"], "last_listened_at": r["last_listened_at"], "created_at": r["created_at"],
    }
    if "job_state" in r:
        out["job"] = None if r["job_state"] is None else {
            "state": r["job_state"], "stage": r["job_stage"], "done": r["progress_done"],
            "total": r["progress_total"], "error": r["job_error"], "retry_at": r["job_run_after"],
        }
        out["chapters_total"] = r["chapter_count"]
        out["chapters_ready"] = r["ready_count"]
        out["playback"] = None if r["play_chapter"] is None else {
            "chapter_idx": r["play_chapter"], "position_s": r["play_position"]}
        out["from_zotero"] = r["from_zotero"]
    return out


KEYWORD_KINDS = ("task", "method", "dataset", "topic", "keyword")


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
    def list_documents(request: Request, q: str = Query("", max_length=200), sort: str = "added"):
        order = SORTS.get(sort, SORTS["added"])
        like = f"%{q.strip()}%"
        with pool(request).connection() as conn:
            rows = conn.execute(
                DOC_LIST_SQL + " WHERE (%(q)s = '' OR d.title ILIKE %(like)s "
                "OR array_to_string(d.authors, ' ') ILIKE %(like)s) ORDER BY " + order,
                {"q": q.strip(), "like": like},
            ).fetchall()
        return [_doc_out(r) for r in rows]

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
            "queue_tag": settings.zotero_queue_tag,
            "last_synced_at": state["last_synced_at"],
            "last_error": state["last_error"],
            "catalogued": catalogued,
            "syncing": syncing,
        }

    @app.post("/api/zotero/sync", status_code=202)
    def zotero_sync(request: Request):
        if not settings.zotero_configured:
            raise HTTPException(503, "Zotero is not configured: set ZOTERO_USER_ID and ZOTERO_API_KEY in .env")
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
