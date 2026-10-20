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


def remove_entry(conn: Connection, user_id: int, document_id: UUID | str) -> tuple[bool, list[str]]:
    """Take a document out of a user's library. Returns (False, []) when it was not there, otherwise (True, ids of
    the documents nobody holds any more). Their files are removed by delete_files, after the transaction commits."""
    cur = conn.execute("DELETE FROM library_entries WHERE user_id = %s AND document_id = %s",
                       (user_id, document_id))
    if cur.rowcount == 0:
        return False, []
    return True, purge_orphans(conn, document_id)


def purge_orphans(conn: Connection, only: UUID | str | None = None) -> list[str]:
    """Delete the documents no library holds (only the given one, if one is given). Returns their ids. Their files
    are not touched here: call delete_files once the transaction has committed."""
    if only is not None:
        rows = conn.execute("DELETE FROM documents d WHERE d.id = %s AND NOT EXISTS "
                            "(SELECT 1 FROM library_entries le WHERE le.document_id = d.id) RETURNING d.id",
                            (only,)).fetchall()
    else:
        rows = conn.execute("DELETE FROM documents d WHERE NOT EXISTS "
                            "(SELECT 1 FROM library_entries le WHERE le.document_id = d.id) RETURNING d.id").fetchall()
    return [str(r["id"]) for r in rows]


def delete_files(docs_dir: Path, ids: list[str]) -> None:
    for doc_id in ids:
        DocPaths(docs_dir, doc_id).remove()
    if ids:
        log.info("removed %d document(s) no library holds", len(ids))
