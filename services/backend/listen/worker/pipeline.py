"""The background pipeline for one job: fetch → OCR → structure → chapters; and narrate, the audio of one user.

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
from pypdf import PdfReader

from .. import accounts, appsettings, embeddings, jobs, narrations
from ..config import Settings
from ..storage import DocPaths
from . import article, audio, boxes, chapters, embed, enrich, fetch, grobid, ocr, pdf_fallback, sentences, tagger
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
        self._tagger: tagger.Tagger | None = None

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
        """The text of a paper: fetch, OCR, structure, keywords and the chapter plan. Audio is made per user by
        narrate(); the audio of every holder of the paper is made again when the text changes."""
        document_id = job["document_id"]
        paths = DocPaths(self.settings.docs_dir, document_id).ensure()
        options = job.get("options") or {}

        if (redo := options.get("from")) == "enrich":
            # keywords only: the audio and the chapters stay as they are
            log.info("job %s: extracting keywords again", job["id"])
            paths.enriched.unlink(missing_ok=True)
            with self.pool.connection() as conn, conn.transaction():
                conn.execute("DELETE FROM document_keywords WHERE document_id = %s", (document_id,))
                conn.execute("UPDATE jobs SET options = options - 'from' WHERE id = %s", (job["id"],))
        elif redo:
            log.info("job %s: redoing from %s", job["id"], redo)
            paths.reset_from(redo)
            paths.ensure()
            with self.pool.connection() as conn, conn.transaction():
                if redo in ("ocr", "structure", "chapters"):
                    conn.execute("DELETE FROM chapters WHERE document_id = %s", (document_id,))
                # the text may have changed: every narration of the paper is made again, for each holder
                for holder in conn.execute("SELECT user_id FROM library_entries WHERE document_id = %s",
                                           (document_id,)).fetchall():
                    narrations.take(conn, document_id, holder["user_id"])
                    narrations.queue(conn, document_id, holder["user_id"])
                # Do not redo again if this job is retried or resumed after a restart.
                conn.execute("UPDATE jobs SET options = options - 'from' WHERE id = %s", (job["id"],))

        self._fetch(job, paths)
        self._ocr(job, paths)
        doc = self._structure(job, paths)
        self._embed(job, doc)
        self._enrich(job, paths, doc)
        self._chapters(job, paths, doc)

    def narrate(self, job: dict[str, Any]) -> str:
        """One user's audio for a paper, with that user's audio settings. Chapters already made are kept, so a stopped
        narration resumes. Returns "deferred" when the paper is not ready yet (its job waits and is tried again)."""
        document_id, user_id = job["document_id"], job["user_id"]
        if user_id is None:
            raise PipelineError("The audio was requested without a user")
        row = self._doc_row(document_id)
        if row["status"] == "failed":
            raise PipelineError("The paper could not be processed, so there is no audio for it")
        if row["status"] != "ready":
            return "deferred"
        paths = DocPaths(self.settings.docs_dir, document_id).ensure()
        with self.pool.connection() as conn, conn.transaction():
            prefs = appsettings.get_all(conn, user_id)
            narration = conn.execute(
                "INSERT INTO narrations (document_id, user_id, state, settings) VALUES (%s, %s, 'running', %s) "
                "ON CONFLICT (document_id, user_id) DO UPDATE SET state = 'running', error = NULL, "
                "settings = CASE WHEN narrations.state = 'ready' THEN narrations.settings ELSE EXCLUDED.settings END, "
                "updated_at = now() RETURNING id, settings",
                (document_id, user_id, json.dumps(prefs))).fetchone()
        narration_id = narration["id"]
        # the settings the audio is made with: the ones it was started with
        prefs = {**prefs, **(narration["settings"] or {})}
        doc = Document.load(paths.structure)
        plans = chapters.load(paths.chapters)
        normalizer = Normalizer.from_file(self.settings.lexicon_path, prefs["citations"])
        report = self._progress(job["id"], "audio")
        total = len(plans)
        for idx in range(total):
            report(idx, total)
            with self.pool.connection() as conn:
                made = conn.execute("SELECT 1 FROM narration_chapters WHERE narration_id = %s AND idx = %s",
                                    (narration_id, idx)).fetchone()
            if made:
                continue
            with self.pool.connection() as conn:
                if accounts.storage_status(conn, user_id)["remaining"] <= 0:
                    # the chapters made so far stay playable; the rest is made once there is room
                    raise PipelineError("Storage is full: delete papers or audio to continue making audio", False)
            engine = self._engine_for(prefs)
            segments = (overview_segments(doc, plans[0], normalizer) if idx == 0
                        else chapter_segments(plans[idx], normalizer))
            out = paths.root / "narrations" / str(narration_id) / f"{idx}.m4a"
            out.parent.mkdir(parents=True, exist_ok=True)
            duration, timed = audio.render(engine, segments, out, lambda: report(idx, total))
            with self.pool.connection() as conn, conn.transaction():
                if conn.execute("SELECT 1 FROM narrations WHERE id = %s", (narration_id,)).fetchone() is None:
                    out.unlink(missing_ok=True)
                    raise Cancelled()  # removed while it was being made
                conn.execute("DELETE FROM narration_sentences WHERE narration_id = %s AND chapter_idx = %s",
                             (narration_id, idx))
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO narration_sentences (narration_id, chapter_idx, idx, text, page, start_s, end_s, "
                        "rects) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                        [(narration_id, idx, i, t.text, t.page, t.start_s, t.end_s,
                          json.dumps(t.rects) if t.rects else None) for i, t in enumerate(timed)],
                    )
                conn.execute(
                    "INSERT INTO narration_chapters (narration_id, idx, audio_file, duration_s, bytes) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (narration_id, idx) DO UPDATE SET "
                    "audio_file = EXCLUDED.audio_file, duration_s = EXCLUDED.duration_s, bytes = EXCLUDED.bytes",
                    (narration_id, idx, out.relative_to(paths.root).as_posix(), duration, out.stat().st_size))
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                "UPDATE narrations n SET state = 'ready', error = NULL, updated_at = now(), "
                "duration_s = (SELECT sum(duration_s) FROM narration_chapters WHERE narration_id = n.id), "
                "bytes = (SELECT coalesce(sum(bytes), 0) FROM narration_chapters WHERE narration_id = n.id) "
                "WHERE id = %s", (narration_id,))
        report(total, total)
        return "done"

    # ------------------------------------------------------------ stages
    def _fetch(self, job, paths: DocPaths) -> None:
        if paths.source_pdf.exists() or paths.source_html.exists():
            return
        row = self._doc_row(job["document_id"])
        if row["source_type"] != "url":
            raise PipelineError("The uploaded PDF is missing on disk")
        if not row["source_url"]:
            raise PipelineError("There is no PDF or link to fetch for this item")
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
        # A web article has no pages: about 500 words make one page, for the page quota.
        words = sum(len(p.text.split()) for p in doc.abstract) + sum(
            len(p.text.split()) for s in doc.sections for p in s.paragraphs)
        pdf_pages = None  # a PDF whose page count was not taken at upload is counted here
        if row["source_type"] == "pdf" and not row["pages"]:
            try:
                pdf_pages = len(PdfReader(str(paths.viewer_pdf if paths.viewer_pdf.exists() else paths.source_pdf)).pages)
            except Exception:  # noqa: BLE001 - an unreadable page count only leaves the quota at zero for this paper
                log.warning("could not count the pages of document %s", job["document_id"])
        with self.pool.connection() as conn:
            conn.execute(
                """UPDATE documents SET
                     title = CASE WHEN title = '' OR title = %(fn)s OR title = source_url THEN %(title)s ELSE title END,
                     authors = %(authors)s,
                     published = %(published)s,
                     pages = CASE WHEN source_type = 'url' THEN %(pages)s ELSE coalesce(pages, %(pdf_pages)s) END,
                     updated_at = now()
                   WHERE id = %(id)s""",
                {"fn": filename_title, "title": doc.title or row["title"], "authors": doc.authors[:20],
                 "published": doc.published, "pages": max(1, -(-words // 500)), "pdf_pages": pdf_pages,
                 "id": job["document_id"]},
            )
        return doc

    def _embed(self, job, doc: Document) -> None:
        """The paper's SPECTER2 vector (see worker/embed.py). Optional: without it the related papers, the exemplars
        and the topic clusters wait until ensure_library makes it."""
        if not self.settings.embeddings:
            return
        with self.pool.connection() as conn:
            done = conn.execute("SELECT 1 FROM document_embeddings WHERE document_id = %s AND model = %s",
                                (job["document_id"], embeddings.MODEL)).fetchone()
        if done:
            return
        abstract = " ".join(p.text for p in doc.abstract) or " ".join(
            p.text for s in doc.sections[:2] for p in s.paragraphs[:2] if p.kind == "text")
        if not (doc.title or abstract):
            return
        try:
            vector = embed.specter(self.settings.models_dir).paper_vectors([(doc.title or "", abstract)])[0]
        except Exception as e:  # noqa: BLE001 - an optional stage must not fail the document
            log.warning("paper vector skipped: %s", e)
            return
        with self.pool.connection() as conn, conn.transaction():
            embeddings.store(conn, job["document_id"], vector)

    def _keyword_tagger(self):
        """The keyword tagger when KEYWORD_TAGGER is on, else None."""
        if not self.settings.keyword_tagger:
            return None
        if self._tagger is None:
            self._tagger = tagger.Tagger(self.settings.tagger_dir)
        return self._tagger

    def _enrich(self, job, paths: DocPaths, doc: Document) -> None:
        """Search and indexing terms (see enrich.py). Skipped once done; redone by a job with from=enrich."""
        if paths.enriched.exists():
            return
        self._progress(job["id"], "enrich")(0, 1)
        llm = self.llm if self.llm is not None and self.llm.wait_ready(self.settings.llm_wait_s) else None
        if llm is None:
            log.warning("LLM not available; keywords come from the text only")
        terms = enrich.extract(doc, llm, typed=self.settings.keywords_mode == "typed", tagger=self._keyword_tagger())
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM document_keywords WHERE document_id = %s", (job["document_id"],))
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO document_keywords (document_id, kind, term, source, weight) VALUES (%s, %s, %s, %s, %s)",
                    [(job["document_id"], t.kind, t.term, t.source, t.weight) for t in terms],
                )
        log.info("keywords: %d terms (%d from the LLM)", len(terms), sum(t.source == "llm" for t in terms))
        if llm is not None:
            written = enrich.element_summaries(doc, llm)
            doc.save(paths.structure)  # the summaries travel with the paragraphs into the chapter plan
            log.info("figure and table summaries: %d", written)
        paths.enriched.write_text("{}")

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

    def _chapters(self, job, paths: DocPaths, doc: Document) -> list[chapters.ChapterPlan]:
        if paths.chapters.exists():
            plans = chapters.load(paths.chapters)
        else:
            plans = chapters.plan(doc)
            llm = self.llm if self.llm is not None and self.llm.wait_ready(self.settings.llm_wait_s) else None
            if llm is None:
                log.warning("LLM not available; untitled chapters get generic titles")
            enricher = chapters.Enricher(llm, self._progress(job["id"], "chapters"))
            enricher.run(plans)
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
    segs.append(_heading("Abstract" if overview.intro_source == "abstract" else "Overview", 1, normalizer))
    if overview.intro:
        segs += _sentences(overview.intro, None, normalizer)
    return [s for s in segs if re.search(r"[A-Za-z0-9]", s.spoken)]


def chapter_segments(plan: chapters.ChapterPlan, normalizer: Normalizer) -> list[Segment]:
    segs = [_heading(plan.title or "Untitled section", plan.start_page, normalizer)]
    for p in plan.paragraphs:
        if p.kind == "heading":
            segs.append(_heading(p.text, p.page, normalizer, p.boxes))
        elif p.kind == "equation":
            segs.append(Segment("[Equation]", "The paper shows an equation here.", p.page, "paragraph"))
        elif p.kind in ("figure", "table"):
            name, _, caption = p.text.partition(": ")
            segs.append(Segment(p.text, normalizer(f"{name}. {p.summary or caption}"), p.page, "paragraph"))
        else:
            segs += _sentences(p.text, p.page, normalizer, lines=p.boxes)
    return [s for s in segs if re.search(r"[A-Za-z0-9]", s.spoken)]  # drop stray "]" or "*" fragments
