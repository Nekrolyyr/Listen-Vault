"""Library operations on shared documents: a user holds a library entry, a document is stored once.

A document is removed from disk only when no library entry refers to it any more.
"""
from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from psycopg import Connection

from .storage import DocPaths

log = logging.getLogger(__name__)


def add_entry(conn: Connection, user_id: int, document_id: UUID | str) -> bool:
    """Put a document in a user's library. False if it was already there."""
    row = conn.execute(
        "INSERT INTO library_entries (user_id, document_id) VALUES (%s, %s) "
        "ON CONFLICT DO NOTHING RETURNING user_id",
        (user_id, document_id)).fetchone()
    return row is not None


def in_library(conn: Connection, user_id: int, document_id: UUID | str) -> bool:
    return conn.execute("SELECT 1 FROM library_entries WHERE user_id = %s AND document_id = %s",
                        (user_id, document_id)).fetchone() is not None


def remove_entry(conn: Connection, user_id: int, document_id: UUID | str, docs_dir: Path) -> bool:
    """Take a document out of a user's library. Returns False when it was not there."""
    cur = conn.execute("DELETE FROM library_entries WHERE user_id = %s AND document_id = %s",
                       (user_id, document_id))
    if cur.rowcount == 0:
        return False
    purge_orphans(conn, docs_dir, document_id)
    return True


def purge_orphans(conn: Connection, docs_dir: Path, only: UUID | str | None = None) -> list[str]:
    """Delete documents that no library holds any more, with their files. Returns the ids deleted."""
    if only is not None:
        rows = conn.execute("DELETE FROM documents d WHERE d.id = %s AND NOT EXISTS "
                            "(SELECT 1 FROM library_entries le WHERE le.document_id = d.id) RETURNING d.id",
                            (only,)).fetchall()
    else:
        rows = conn.execute("DELETE FROM documents d WHERE NOT EXISTS "
                            "(SELECT 1 FROM library_entries le WHERE le.document_id = d.id) RETURNING d.id").fetchall()
    ids = [str(r["id"]) for r in rows]
    for doc_id in ids:
        DocPaths(docs_dir, doc_id).remove()
    if ids:
        log.info("removed %d document(s) no library holds", len(ids))
    return ids
