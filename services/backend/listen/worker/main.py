"""Background worker: claims jobs from Postgres and runs them, one at a time."""
from __future__ import annotations

import logging
import signal
import socket
import time
from uuid import UUID

from .. import config, db, jobs, zotero
from ..storage import DocPaths
from . import classify, qa, topics
from .fetch import FetchError
from .grobid import GrobidError
from .llm import LlmClient
from .pipeline import Cancelled, Interrupted, Pipeline, PipelineError

log = logging.getLogger("listen.worker")

IDLE_SLEEP_S = 3
ORPHAN_SWEEP_S = 3600
AREAS_CHECK_S = 60


class Worker:
    def __init__(self, settings: config.Settings, pool, pipeline: Pipeline | None = None):
        self.settings = settings
        self.pool = pool
        self.name = f"{socket.gethostname()}:{id(self) & 0xffff:x}"
        self.stopping = False
        self.pipeline = pipeline or Pipeline(settings, pool, LlmClient(settings.llm_url),
                                             should_stop=lambda: self.stopping)

    def stop(self, *_):
        log.info("stop requested; handing the current job back to the queue")
        self.stopping = True

    def run_once(self) -> bool:
        """Claim and process one job. Returns False when the queue was empty."""
        with self.pool.connection() as conn:
            job = jobs.claim(conn, self.name)
        if job is None:
            return False
        log.info("job %s (%s) for document %s (attempt %s)", job["id"], job["kind"], job["document_id"],
                 job["attempts"])
        t0 = time.monotonic()
        try:
            if job["kind"] == "document":
                self.pipeline.run(job)
            else:
                self._library_job(job)
        except Cancelled:
            log.info("document %s was deleted; dropping its files", job["document_id"])
            DocPaths(self.settings.docs_dir, job["document_id"]).remove()
            return True
        except Interrupted:
            with self.pool.connection() as conn:
                conn.execute("UPDATE jobs SET state = 'queued', attempts = attempts - 1 WHERE id = %s", (job["id"],))
            return True
        except (PipelineError, FetchError, GrobidError) as e:
            self._fail(job, str(e), getattr(e, "retryable", False))
            return True
        except Exception as e:  # noqa: BLE001 - a job must never take the worker down
            log.exception("job %s crashed", job["id"])
            self._fail(job, f"Internal error: {e}", retryable=True)
            return True
        with self.pool.connection() as conn:
            jobs.finish(conn, job["id"], jobs.document_of(job))
        log.info("job %s done in %.0fs", job["id"], time.monotonic() - t0)
        return True

    def _library_job(self, job) -> None:
        if job["kind"] == "zotero_sync":
            if job["user_id"] is None:  # queued by an administrator; without one there is no library to fill
                raise PipelineError("The Zotero sync was queued without an administrator")
            try:
                zotero.sync(self.pool, self.settings, job["user_id"])
            except zotero.ZoteroError as e:  # recorded in zotero_state.last_error; the next sync tries again
                log.warning("zotero sync failed: %s", e)
            return
        if job["kind"] == "qa":
            qa.run(self.pool, self.settings, self.pipeline.llm, job)
            return
        if job["kind"] == "topics":
            topics.rebuild(self.pool, self.pipeline.llm, self.settings)
            return
        if job["kind"] == "areas":
            classify.run(self.pool, self.settings.docs_dir, self.pipeline.llm, job["id"], job["options"] or {},
                         self.settings)
            return
        raise PipelineError(f"Unknown job kind {job['kind']!r}")

    def _fail(self, job, message: str, retryable: bool) -> None:
        with self.pool.connection() as conn:
            retry = jobs.fail(conn, job, message, retryable)
        log.warning("job %s %s: %s", job["id"], "will be retried" if retry else "failed", message)

    def schedule(self) -> None:
        """Queue the periodic library jobs (the Zotero sync), unless one is already waiting or running."""
        with self.pool.connection() as conn, conn.transaction():
            if self.settings.zotero_configured:
                jobs.enqueue_library(conn, "zotero_sync")

    def schedule_areas(self) -> None:
        """Place papers that have no research area yet (new imports, new uploads)."""
        with self.pool.connection() as conn, conn.transaction():
            if classify.unplaced_exists(conn):
                jobs.enqueue_library(conn, "areas")

    def sweep_orphans(self) -> int:
        """Remove document folders whose database row is gone (deleted while a job was running)."""
        root = self.settings.docs_dir
        if not root.is_dir():
            return 0
        with self.pool.connection() as conn:
            known = {str(r["id"]) for r in conn.execute("SELECT id FROM documents")}
        removed = 0
        for d in root.iterdir():
            try:
                UUID(d.name)
            except ValueError:
                continue
            if d.is_dir() and d.name not in known:
                DocPaths(root, d.name).remove()
                removed += 1
        return removed

    def loop(self) -> None:
        last_sweep = 0.0
        last_zotero: float | None = None
        last_areas: float | None = None
        while not self.stopping:
            if time.monotonic() - last_sweep > ORPHAN_SWEEP_S:
                if n := self.sweep_orphans():
                    log.info("removed %d orphaned document folders", n)
                last_sweep = time.monotonic()
            if self.settings.zotero_configured and (
                    last_zotero is None or time.monotonic() - last_zotero >= self.settings.zotero_sync_s):
                last_zotero = time.monotonic()
                self.schedule()
            if last_areas is None or time.monotonic() - last_areas >= AREAS_CHECK_S:
                last_areas = time.monotonic()
                self.schedule_areas()
            if not self.run_once():
                time.sleep(IDLE_SLEEP_S)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # phonemizer resets its level on every call; a filter survives that. The warning is harmless.
    logging.getLogger("phonemizer").addFilter(lambda r: "words count mismatch" not in r.getMessage())
    settings = config.load()
    db.wait_for_db(settings.database_url)
    db.migrate(settings.database_url)
    settings.docs_dir.mkdir(parents=True, exist_ok=True)
    pool = db.make_pool(settings.database_url, max_size=4)
    worker = Worker(settings, pool)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    log.info("worker %s started", worker.name)
    worker.loop()
    pool.close()


if __name__ == "__main__":
    run()
