"""A narration is the audio of one paper for one user (docs/gaps-plan.md, phase 3).

Audio is made only for a user who has the paper in their library, with that user's audio settings. The document
job makes the text; the narrate job makes the audio. Files are removed after the database transaction commits.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from uuid import UUID

from psycopg import Connection

from . import jobs


def queue(conn: Connection, document_id: UUID | str, user_id: int) -> bool:
    """Ask for this user's audio. Returns False when a narration job for it is already waiting."""
    row = conn.execute(
        "INSERT INTO jobs (document_id, kind, options, user_id) SELECT %s, 'narrate', '{}', %s WHERE NOT EXISTS "
        "(SELECT 1 FROM jobs WHERE document_id = %s AND user_id = %s AND kind = 'narrate' "
        "AND state IN ('queued', 'running')) RETURNING id",
        (document_id, user_id, document_id, user_id)).fetchone()
    return row is not None


def get(conn: Connection, document_id: UUID | str, user_id: int) -> dict | None:
    return conn.execute("SELECT * FROM narrations WHERE document_id = %s AND user_id = %s",
                        (document_id, user_id)).fetchone()


def take(conn: Connection, document_id: UUID | str, user_id: int) -> list[str]:
    """Delete the narration's rows and return the files to delete (relative to the paper's folder), for after commit."""
    row = conn.execute("DELETE FROM narrations WHERE document_id = %s AND user_id = %s RETURNING id",
                       (document_id, user_id)).fetchone()
    if row is None:
        return []
    return [r["audio_file"] for r in conn.execute(
        "SELECT audio_file FROM narration_chapters WHERE narration_id = %s", (row["id"],)).fetchall()] + [
        f"narrations/{row['id']}"]


def delete_files(docs_dir: Path, document_id: UUID | str, paths: list[str]) -> None:
    root = docs_dir / str(document_id)
    for rel in paths:
        target = root / rel
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)


def defer(conn: Connection, job_id: int, seconds: int) -> None:
    """Put a narrate job back in the queue for later (its paper is not ready yet). Not counted as an attempt."""
    jobs.defer(conn, job_id, seconds)
