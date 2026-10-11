"""Postgres-backed job queue (one job per document run).

Claiming uses FOR UPDATE SKIP LOCKED; a running job whose heartbeat is older than STALE_AFTER is
considered abandoned (worker crashed or was restarted) and can be claimed again. Stages are
idempotent, so a reclaimed job resumes where the previous run stopped.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from uuid import UUID

from psycopg import Connection

STALE_AFTER = timedelta(minutes=10)
MAX_ATTEMPTS = 3
RETRY_DELAYS_S = (30, 300)  # after attempt 1 and 2

# Order in which the worker runs stages; "from" in job options names the first stage to redo.
STAGES = ("fetch", "ocr", "structure", "enrich", "chapters", "synthesis")


def enqueue(conn: Connection, document_id: UUID | str, options: dict[str, Any] | None = None) -> int:
    row = conn.execute(
        "INSERT INTO jobs (document_id, options) VALUES (%s, %s) RETURNING id",
        (document_id, json.dumps(options or {})),
    ).fetchone()
    conn.execute(
        "UPDATE documents SET status = 'queued', error = NULL, updated_at = now() WHERE id = %s",
        (document_id,),
    )
    return row["id"]


def enqueue_library(conn: Connection, kind: str, options: dict[str, Any] | None = None) -> int | None:
    """Queue a job that is not about one document (e.g. a Zotero sync). Returns None if one is already open."""
    row = conn.execute(
        "INSERT INTO jobs (kind, options) SELECT %s, %s WHERE NOT EXISTS "
        "(SELECT 1 FROM jobs WHERE kind = %s AND state IN ('queued', 'running')) RETURNING id",
        (kind, json.dumps(options or {}), kind),
    ).fetchone()
    return row["id"] if row else None


def has_open_job(conn: Connection, document_id: UUID | str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE document_id = %s AND state IN ('queued', 'running') LIMIT 1",
        (document_id,),
    ).fetchone()
    return row is not None


def claim(conn: Connection, worker: str) -> dict[str, Any] | None:
    """Claim the oldest runnable job, or None. Commits immediately."""
    with conn.transaction():
        row = conn.execute(
            """
            UPDATE jobs SET state = 'running', worker = %(w)s, attempts = attempts + 1,
                            started_at = COALESCE(started_at, now()), heartbeat_at = now(), error = NULL
            WHERE id = (
                SELECT id FROM jobs
                WHERE run_after <= now()
                  AND (state = 'queued' OR (state = 'running' AND heartbeat_at < now() - %(stale)s))
                ORDER BY run_after, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING *
            """,
            {"w": worker, "stale": STALE_AFTER},
        ).fetchone()
        if row and row["kind"] == "document":
            conn.execute(
                "UPDATE documents SET status = 'processing', updated_at = now() WHERE id = %s",
                (row["document_id"],),
            )
    return row


def heartbeat(conn: Connection, job_id: int, stage: str | None = None,
              done: int | None = None, total: int | None = None) -> bool:
    """Record progress. Returns False when the job no longer exists (document was deleted)."""
    with conn.transaction():
        cur = conn.execute(
            """
            UPDATE jobs SET heartbeat_at = now(),
                            stage = COALESCE(%s, stage),
                            progress_done = COALESCE(%s, progress_done),
                            progress_total = COALESCE(%s, progress_total)
            WHERE id = %s AND state = 'running'
            """,
            (stage, done, total, job_id),
        )
    return cur.rowcount == 1


def finish(conn: Connection, job_id: int, document_id: UUID | str | None) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE jobs SET state = 'done', finished_at = now(), heartbeat_at = now() WHERE id = %s",
            (job_id,),
        )
        if document_id is not None:
            conn.execute(
                "UPDATE documents SET status = 'ready', error = NULL, updated_at = now() WHERE id = %s",
                (document_id,),
            )


def fail(conn: Connection, job: dict[str, Any], error: str, retryable: bool) -> bool:
    """Mark a job failed, or re-queue it with backoff. Returns True when it will be retried."""
    attempts = job["attempts"]
    retry = retryable and attempts < MAX_ATTEMPTS
    document_id = job["document_id"]
    with conn.transaction():
        if retry:
            delay = RETRY_DELAYS_S[min(attempts, len(RETRY_DELAYS_S)) - 1]
            conn.execute(
                "UPDATE jobs SET state = 'queued', error = %s, run_after = now() + %s WHERE id = %s",
                (error, timedelta(seconds=delay), job["id"]),
            )
            if document_id is not None:
                conn.execute(
                    "UPDATE documents SET status = 'queued', error = %s, updated_at = now() WHERE id = %s",
                    (error, document_id),
                )
        else:
            conn.execute(
                "UPDATE jobs SET state = 'failed', error = %s, finished_at = now() WHERE id = %s",
                (error, job["id"]),
            )
            if document_id is not None:
                conn.execute(
                    "UPDATE documents SET status = 'failed', error = %s, updated_at = now() WHERE id = %s",
                    (error, document_id),
                )
    return retry
