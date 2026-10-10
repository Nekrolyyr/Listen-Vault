import shutil
import subprocess

import numpy as np
import psycopg
import pytest
from psycopg.rows import dict_row

from listen import jobs
from listen.storage import DocPaths
from listen.worker import grobid
from listen.worker.main import Worker
from listen.worker.pipeline import Pipeline

from .conftest import make_pdf
from .test_structure import ARTICLE, FakeLlm

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


class FakeEngine:
    name = "fake"
    voice = "tone"
    sample_rate = 16_000

    def __init__(self):
        self.texts = []

    def synth(self, text):
        self.texts.append(text)
        return (0.1 * np.sin(np.arange(len(text) * 160) / 8)).astype(np.float32)  # 10 ms per character


class ReadyLlm(FakeLlm):
    def wait_ready(self, timeout_s=0):
        return True


def _worker(settings, pool, engine=None):
    engine = engine or FakeEngine()
    pipeline = Pipeline(settings, pool, llm=ReadyLlm(), engine_factory=lambda *a: engine)
    return Worker(settings, pool, pipeline), engine


def _new_doc(settings, conn, kind, **cols):
    doc_id = conn.execute(
        "INSERT INTO documents (source_type, title, original_filename, source_url) VALUES (%s, %s, %s, %s) RETURNING id",
        (kind, cols.get("title", ""), cols.get("filename"), cols.get("url")),
    ).fetchone()["id"]
    jobs.enqueue(conn, doc_id)
    return doc_id, DocPaths(settings.docs_dir, doc_id).ensure()


def _paper(path):
    body = ("Transformers are widely used for language tasks and they keep getting larger every year. "
            "This makes it important to understand which parts of the model really matter. ") * 6
    return make_pdf(path, [
        [(20, "Pruning Heads"), (10, "Abstract"), (10, "We prune heads (p < 0.05) and keep accuracy."),
         (14, "1 Introduction"), *[(10, body[i:i + 90]) for i in range(0, len(body), 90)]],
        [(14, "2 Results"), *[(10, body[i:i + 90]) for i in range(0, len(body), 90)],
         (14, "References"), (10, "[1] Someone. 2020.")],
    ])


def test_pdf_end_to_end_with_fallback_structure(settings, pool, monkeypatch):
    monkeypatch.setattr(grobid, "wait_alive", lambda *a, **k: False)  # GROBID down -> pdfminer fallback
    with pool.connection() as conn:
        doc_id, paths = _new_doc(settings, conn, "pdf", title="paper", filename="paper.pdf")
    _paper(paths.source_pdf)
    worker, engine = _worker(settings, pool)
    assert worker.run_once()

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        doc = conn.execute("SELECT * FROM documents").fetchone()
        assert doc["status"] == "ready", doc["error"]
        assert doc["title"] == "Pruning Heads"          # filename title replaced by the parsed one
        assert doc["has_pdf"] and not doc["ocr_applied"]
        chapters = conn.execute("SELECT * FROM chapters ORDER BY idx").fetchall()
        assert [c["title"] for c in chapters] == ["Overview", "Abstract", "Introduction", "Results"]
        assert all(c["audio_file"] and c["duration_s"] > 0 for c in chapters)
        assert doc["duration_s"] == pytest.approx(sum(c["duration_s"] for c in chapters))
        sents = conn.execute("SELECT s.* FROM sentences s JOIN chapters c ON c.id = s.chapter_id "
                             "WHERE c.idx = 3 ORDER BY s.idx").fetchall()
        assert sents[0]["text"] == "Results" and sents[0]["page"] == 2
        assert all(a["end_s"] <= b["start_s"] for a, b in zip(sents, sents[1:]))
        job = conn.execute("SELECT * FROM jobs").fetchone()
        assert job["state"] == "done" and job["progress_done"] == job["progress_total"] == 4

    # spoken text is normalized; displayed text is not
    assert "We prune heads (p less than 0.05) and keep accuracy." in engine.texts
    assert paths.viewer_pdf.exists()
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate",
                            "-of", "csv=p=0", str(paths.audio(3))], capture_output=True, text=True)
    assert probe.stdout.strip() == "aac,16000"


def test_article_and_resume_after_restart(settings, pool):
    with pool.connection() as conn:
        doc_id, paths = _new_doc(settings, conn, "url", title="https://example.org/sleep", url="https://example.org/sleep")
    paths.source_html.write_text(ARTICLE)  # skip the network fetch

    class Crash(Exception):
        pass

    class CrashingEngine(FakeEngine):
        def synth(self, text):
            if len(self.texts) == 6:
                raise Crash()
            return super().synth(text)

    worker, _ = _worker(settings, pool, CrashingEngine())
    worker.run_once()  # crashes in synthesis -> retryable internal error
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        job = conn.execute("SELECT * FROM jobs").fetchone()
        assert job["state"] == "queued" and "Internal error" in job["error"]
        ready = conn.execute("SELECT idx FROM chapters WHERE audio_file IS NOT NULL ORDER BY idx").fetchall()
        conn.execute("UPDATE jobs SET run_after = now()")
        conn.commit()
    assert ready == [{"idx": 0}]  # the overview finished before the crash

    worker2, engine2 = _worker(settings, pool)
    assert worker2.run_once()
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        doc = conn.execute("SELECT * FROM documents").fetchone()
        assert doc["status"] == "ready" and doc["title"] == "How sleep consolidates memory"
        n = conn.execute("SELECT count(*) AS n FROM chapters WHERE audio_file IS NOT NULL").fetchone()["n"]
        assert n == 2  # overview + one chapter (the short sections are merged)
    assert not any("How sleep consolidates memory." == t for t in engine2.texts)  # overview not redone


def test_delete_during_job_cancels_and_cleans_up(settings, pool):
    with pool.connection() as conn:
        doc_id, paths = _new_doc(settings, conn, "url", url="https://example.org/sleep")
    paths.source_html.write_text(ARTICLE)

    class DeletingEngine(FakeEngine):
        def synth(self, text):
            if len(self.texts) == 3:
                with psycopg.connect(settings.database_url, autocommit=True) as c:
                    c.execute("DELETE FROM documents")
            return super().synth(text)

    worker, _ = _worker(settings, pool, DeletingEngine())
    assert worker.run_once()
    assert not paths.root.exists()
    with psycopg.connect(settings.database_url) as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_rerun_synthesis_keeps_chapters(settings, pool):
    with pool.connection() as conn:
        doc_id, paths = _new_doc(settings, conn, "url", url="https://example.org/sleep")
    paths.source_html.write_text(ARTICLE)
    worker, _ = _worker(settings, pool)
    worker.run_once()
    chapters_json = paths.chapters.read_text()
    with pool.connection() as conn:
        jobs.enqueue(conn, doc_id, {"from": "synthesis"})
    worker2, engine2 = _worker(settings, pool)
    worker2.run_once()
    assert paths.chapters.read_text() == chapters_json and len(engine2.texts) > 0
    with pool.connection() as conn:
        assert conn.execute("SELECT status FROM documents").fetchone()["status"] == "ready"
        assert conn.execute("SELECT options FROM jobs ORDER BY id DESC").fetchone()["options"] == {}


def test_orphan_sweep(settings, pool):
    worker, _ = _worker(settings, pool)
    stray = DocPaths(settings.docs_dir, "00000000-0000-0000-0000-000000000001").ensure()
    (settings.docs_dir / "not-a-uuid").mkdir()
    assert worker.sweep_orphans() == 1
    assert not stray.root.exists() and (settings.docs_dir / "not-a-uuid").exists()


def test_unpronounceable_fragments_are_dropped():
    from listen.worker import chapters as ch
    from listen.worker.docmodel import Paragraph
    from listen.worker.normalize import Normalizer
    from listen.worker.pipeline import chapter_segments

    plan = ch.ChapterPlan(kind="chapter", title="Results", paragraphs=[Paragraph("It works. Done.", 3), Paragraph("]", 3), Paragraph("* †", 4)])
    segs = chapter_segments(plan, Normalizer(), intros=False)
    assert [s.display for s in segs] == ["Results", "It works.", "Done."]
    assert Normalizer()("It works. ] * Done †.") == "It works. Done."
