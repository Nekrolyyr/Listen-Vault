"""Background worker: claims jobs from Postgres and runs the pipeline, one job at a time."""
from __future__ import annotations

import logging
import signal
import socket
import time
from uuid import UUID

from .. import config, db, jobs
from ..storage import DocPaths
from .fetch import FetchError
from .grobid import GrobidError
from .llm import LlmClient
from .pipeline import Cancelled, Interrupted, Pipeline, PipelineError

log = logging.getLogger("listen.worker")

IDLE_SLEEP_S = 3
ORPHAN_SWEEP_S = 3600


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
        log.info("job %s for document %s (attempt %s)", job["id"], job["document_id"], job["attempts"])
        t0 = time.monotonic()
        try:
            self.pipeline.run(job)
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
            jobs.finish(conn, job["id"], job["document_id"])
        log.info("job %s done in %.0fs", job["id"], time.monotonic() - t0)
        return True

    def _fail(self, job, message: str, retryable: bool) -> None:
        with self.pool.connection() as conn:
            retry = jobs.fail(conn, job, message, retryable)
        log.warning("job %s %s: %s", job["id"], "will be retried" if retry else "failed", message)

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
        while not self.stopping:
            if time.monotonic() - last_sweep > ORPHAN_SWEEP_S:
                if n := self.sweep_orphans():
                    log.info("removed %d orphaned document folders", n)
                last_sweep = time.monotonic()
            if not self.run_once():
                time.sleep(IDLE_SLEEP_S)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("phonemizer").setLevel(logging.ERROR)  # espeak "words count mismatch" noise
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
