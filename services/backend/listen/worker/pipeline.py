"""The background pipeline for one job: fetch → OCR → structure → chapters → synthesis.

Each stage writes an artifact (see listen.storage) and is skipped when the artifact exists, so a job
that was interrupted resumes where it stopped. Chapters become playable one by one.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

from .. import appsettings, jobs
from ..config import Settings
from ..storage import DocPaths
from . import article, audio, boxes, chapters, fetch, grobid, ocr, pdf_fallback, sentences
from .audio import Segment
from .docmodel import Document
from .llm import LlmClient
from .normalize import Normalizer
from .tts import Engine, make_engine

log = logging.getLogger(__name__)


class Cancelled(Exception):
    """The document was deleted while its job was running."""


class Interrupted(Exception):
    """The worker is shutting down; the job is handed back to the queue."""


class PipelineError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class Pipeline:
    def __init__(self, settings: Settings, pool: ConnectionPool,
                 llm: LlmClient | None = None,
                 engine_factory: Callable[..., Engine] = make_engine,
                 should_stop: Callable[[], bool] = lambda: False):
        self.settings = settings
        self.pool = pool
        self.llm = llm
        self.engine_factory = engine_factory
        self.should_stop = should_stop
        self._engine: Engine | None = None
        self._engine_key: tuple | None = None

    # ------------------------------------------------------------ helpers
    def _progress(self, job_id: int, stage: str) -> Callable[[int, int], bool]:
        last = 0.0

        def report(done: int, total: int) -> bool:
            nonlocal last
            if self.should_stop():
                raise Interrupted()
            now = time.monotonic()
            if now - last < 2 and done not in (0, total):
                return True
            last = now
            with self.pool.connection() as conn:
                if not jobs.heartbeat(conn, job_id, stage, done, total):
                    raise Cancelled()
            return True

        return report

    def _engine_for(self, prefs: dict[str, Any]) -> Engine:
        key = (prefs["engine"], prefs["voice"], prefs["speech_speed"])
        if self._engine is None or self._engine_key != key:
            self._engine = self.engine_factory(prefs["engine"], self.settings.models_dir, prefs["voice"],
                                               prefs["speech_speed"], self.settings.tts_threads)
            self._engine_key = key
        return self._engine

    def _doc_row(self, document_id) -> dict[str, Any]:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id = %s", (document_id,)).fetchone()
        if row is None:
            raise Cancelled()
        return row

    # ------------------------------------------------------------ entry point
    def run(self, job: dict[str, Any]) -> None:
        document_id = job["document_id"]
        paths = DocPaths(self.settings.docs_dir, document_id).ensure()
        options = job.get("options") or {}

        if redo := options.get("from"):
            log.info("job %s: redoing from %s", job["id"], redo)
            paths.reset_from(redo)
            paths.ensure()
            with self.pool.connection() as conn, conn.transaction():
                if redo in ("ocr", "structure", "chapters"):
                    conn.execute("DELETE FROM chapters WHERE document_id = %s", (document_id,))
                else:
                    conn.execute("UPDATE chapters SET audio_file = NULL, duration_s = NULL WHERE document_id = %s",
                                 (document_id,))
                    conn.execute("DELETE FROM sentences WHERE chapter_id IN "
                                 "(SELECT id FROM chapters WHERE document_id = %s)", (document_id,))
                # Do not redo again if this job is retried or resumed after a restart.
                conn.execute("UPDATE jobs SET options = options - 'from' WHERE id = %s", (job["id"],))

        with self.pool.connection() as conn:
            prefs = appsettings.get_all(conn)

        self._fetch(job, paths)
        self._ocr(job, paths)
        doc = self._structure(job, paths)
        plans = self._chapters(job, paths, doc, prefs)
        self._synthesize(job, paths, doc, plans, prefs)

    # ------------------------------------------------------------ stages
    def _fetch(self, job, paths: DocPaths) -> None:
        if paths.source_pdf.exists() or paths.source_html.exists():
            return
        row = self._doc_row(job["document_id"])
        if row["source_type"] != "url":
            raise PipelineError("The uploaded PDF is missing on disk")
        self._progress(job["id"], "fetch")(0, 1)
        try:
            got = fetch.fetch_article(row["source_url"])
        except fetch.FetchError as e:
            raise PipelineError(str(e), e.retryable) from e
        if got.is_pdf:
            paths.source_pdf.write_bytes(got.content)
        else:
            paths.source_html.write_bytes(got.content)
        with self.pool.connection() as conn:
            conn.execute("UPDATE documents SET has_pdf = %s, updated_at = now() WHERE id = %s",
                         (got.is_pdf, job["document_id"]))

    def _ocr(self, job, paths: DocPaths) -> None:
        if not paths.source_pdf.exists() or paths.viewer_pdf.exists():
            return
        self._progress(job["id"], "ocr")(0, 1)
        try:
            applied = ocr.prepare_viewer_pdf(paths.source_pdf, paths.viewer_pdf, self.settings.ocr_jobs)
        except ocr.OcrError as e:
            raise PipelineError(str(e)) from e
        except Exception as e:  # noqa: BLE001 - pypdf raises many types for broken files
            raise PipelineError(f"Could not read this PDF: {e}") from e
        with self.pool.connection() as conn:
            conn.execute("UPDATE documents SET has_pdf = true, ocr_applied = %s, updated_at = now() WHERE id = %s",
                         (applied, job["document_id"]))

    def _structure(self, job, paths: DocPaths) -> Document:
        if paths.structure.exists():
            return Document.load(paths.structure)
        self._progress(job["id"], "structure")(0, 1)
        row = self._doc_row(job["document_id"])
        if paths.viewer_pdf.exists():
            doc = self._structure_pdf(paths.viewer_pdf)
        else:
            html = paths.source_html.read_bytes().decode("utf-8", "replace")
            try:
                doc = article.parse_html(html, row["source_url"])
            except article.ArticleError as e:
                raise PipelineError(str(e)) from e
        if not doc.sections and not doc.abstract:
            raise PipelineError("No readable text was found in this document")
        doc.save(paths.structure)

        filename_title = Path(row["original_filename"] or "").stem
        with self.pool.connection() as conn:
            conn.execute(
                """UPDATE documents SET
                     title = CASE WHEN title = '' OR title = %(fn)s OR title = source_url THEN %(title)s ELSE title END,
                     authors = %(authors)s, published = %(published)s, updated_at = now()
                   WHERE id = %(id)s""",
                {"fn": filename_title, "title": doc.title or row["title"], "authors": doc.authors[:20],
                 "published": doc.published, "id": job["document_id"]},
            )
        return doc

    def _structure_pdf(self, pdf: Path) -> Document:
        base = self.settings.grobid_url
        if grobid.wait_alive(base, timeout_s=self.settings.grobid_wait_s):
            try:
                doc = grobid.parse_tei(grobid.process_fulltext(base, pdf))
                if doc.sections:
                    return doc
                log.warning("GROBID found no body sections; using the layout fallback")
            except grobid.GrobidError as e:
                log.warning("GROBID failed (%s); using the layout fallback", e)
            except Exception as e:  # noqa: BLE001 - malformed TEI
                log.warning("GROBID output unusable (%s); using the layout fallback", e)
        else:
            log.warning("GROBID not reachable; using the layout fallback")
        fallback = pdf_fallback.parse_pdf(pdf)
        return fallback

    def _chapters(self, job, paths: DocPaths, doc: Document, prefs) -> list[chapters.ChapterPlan]:
        if paths.chapters.exists():
            plans = chapters.load(paths.chapters)
        else:
            plans = chapters.plan(doc)
            llm = self.llm if self.llm is not None and self.llm.wait_ready(self.settings.llm_wait_s) else None
            if llm is None:
                log.warning("LLM not available; chapter intros and overview will be extractive")
            enricher = chapters.Enricher(llm, self._progress(job["id"], "chapters"))
            enricher.run(doc, plans, intros=prefs["chapter_intros"])
            log.info("chaptering: %d chapters, %d LLM calls, %d fell back", len(plans), enricher.calls,
                     enricher.failures)
            chapters.save(paths.chapters, plans)

        with self.pool.connection() as conn, conn.transaction():
            existing = conn.execute("SELECT count(*) AS n FROM chapters WHERE document_id = %s",
                                    (job["document_id"],)).fetchone()["n"]
            if existing != len(plans):
                conn.execute("DELETE FROM chapters WHERE document_id = %s", (job["document_id"],))
                for idx, p in enumerate(plans):
                    conn.execute(
                        "INSERT INTO chapters (document_id, idx, kind, title, intro, start_page, word_count) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (job["document_id"], idx, p.kind, p.title, p.intro, p.start_page, p.word_count),
                    )
        return plans

    def _synthesize(self, job, paths: DocPaths, doc: Document, plans, prefs) -> None:
        normalizer = Normalizer.from_file(self.settings.lexicon_path, prefs["citations"])
        report = self._progress(job["id"], "synthesis")
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT id, idx, audio_file FROM chapters WHERE document_id = %s ORDER BY idx",
                                (job["document_id"],)).fetchall()
        total = len(rows)
        for n, row in enumerate(rows):
            report(n, total)
            out = paths.audio(row["idx"])
            if row["audio_file"] and out.exists():
                continue
            engine = self._engine_for(prefs)
            segments = (overview_segments(doc, plans[0], normalizer) if row["idx"] == 0
                        else chapter_segments(plans[row["idx"]], normalizer, prefs["chapter_intros"]))
            duration, timed = audio.render(engine, segments, out, lambda: report(n, total))
            with self.pool.connection() as conn, conn.transaction():
                conn.execute("DELETE FROM sentences WHERE chapter_id = %s", (row["id"],))
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO sentences (chapter_id, idx, text, page, start_s, end_s, rects) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
                        [(row["id"], i, t.text, t.page, t.start_s, t.end_s, json.dumps(t.rects) if t.rects else None)
                         for i, t in enumerate(timed)],
                    )
                cur = conn.execute("UPDATE chapters SET audio_file = %s, duration_s = %s WHERE id = %s",
                                   (out.name, duration, row["id"]))
                if cur.rowcount == 0:
                    raise Cancelled()
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE documents SET duration_s = (SELECT sum(duration_s) FROM chapters WHERE document_id = %(id)s), "
                "voice = %(voice)s, updated_at = now() WHERE id = %(id)s",
                {"id": job["document_id"], "voice": f"{prefs['engine']}:{self._engine.voice if self._engine else prefs['voice']}"},
            )
        report(total, total)


# ---------------------------------------------------------------- what gets spoken

def _sentences(text: str, page: int | None, normalizer: Normalizer, last_pause: str = "paragraph",
               lines: list | None = None) -> list[Segment]:
    """Split a paragraph into sentence segments. `lines` are the paragraph's GROBID line boxes, if known."""
    flat = boxes.flat(text)
    segs: list[Segment] = []
    cursor = 0
    for s in sentences.split(text):
        start = flat.find(s, cursor)
        start = cursor if start < 0 else start
        cursor = start + len(s)
        segs.append(Segment(s, normalizer(s), page, rects=boxes.sentence_rects(lines or [], flat, start, cursor)))
    if segs:
        segs[-1].pause = last_pause
    return segs


def _heading(text: str, page: int | None, normalizer: Normalizer, lines: list | None = None) -> Segment:
    spoken = normalizer(text)
    return Segment(text, spoken if re.search(r"[.!?]$", spoken) else spoken + ".", page, "heading",
                   rects=boxes.paragraph_rects(lines or [], text))


def _byline(authors: list[str]) -> str:
    if not authors:
        return ""
    if len(authors) == 1:
        return f"By {authors[0]}."
    if len(authors) <= 3:
        return f"By {', '.join(authors[:-1])} and {authors[-1]}."
    return f"By {authors[0]}, {authors[1]} and colleagues."


def overview_segments(doc: Document, overview: chapters.ChapterPlan, normalizer: Normalizer) -> list[Segment]:
    segs: list[Segment] = []
    if doc.title:
        segs.append(_heading(doc.title, 1, normalizer))
    if byline := _byline(doc.authors):
        segs.append(Segment(byline, normalizer(byline), 1, "paragraph"))
    segs.append(_heading("Overview", 1, normalizer))
    if overview.intro:
        segs += _sentences(overview.intro, None, normalizer)
    return [s for s in segs if re.search(r"[A-Za-z0-9]", s.spoken)]


def chapter_segments(plan: chapters.ChapterPlan, normalizer: Normalizer, intros: bool) -> list[Segment]:
    segs = [_heading(plan.title or "Untitled section", plan.start_page, normalizer)]
    if intros and plan.intro:
        segs.append(Segment("In brief:", "In brief.", plan.start_page, "sentence"))
        segs += _sentences(plan.intro, plan.start_page, normalizer)
    for p in plan.paragraphs:
        if p.kind == "heading":
            segs.append(_heading(p.text, p.page, normalizer, p.boxes))
        elif p.kind == "equation":
            segs.append(Segment("[Equation]", "The paper shows an equation here.", p.page, "paragraph"))
        elif p.kind in ("figure", "table"):
            name, _, caption = p.text.partition(": ")
            segs.append(Segment(p.text, normalizer(f"{name}. {caption}"), p.page, "paragraph"))
        else:
            segs += _sentences(p.text, p.page, normalizer, lines=p.boxes)
    return [s for s in segs if re.search(r"[A-Za-z0-9]", s.spoken)]  # drop stray "]" or "*" fragments
