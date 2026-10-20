"""Paper vectors in the database: one SPECTER2 vector per paper (table document_embeddings, migration 010).

The vectors are made by worker/embed.py. This module stores them and answers "which papers are related", so the api can
use it without any ML library.
"""
from __future__ import annotations

from typing import Any

from . import vectors

MODEL = "specter2-base+proximity"


def store(conn, document_id, vector) -> None:
    conn.execute(
        "INSERT INTO document_embeddings (document_id, model, vector) VALUES (%s, %s, %s) "
        "ON CONFLICT (document_id) DO UPDATE SET model = EXCLUDED.model, vector = EXCLUDED.vector, updated_at = now()",
        (document_id, MODEL, [float(x) for x in vector]),
    )


def load_all(conn) -> dict[str, list[float]]:
    rows = conn.execute("SELECT document_id, vector FROM document_embeddings WHERE model = %s", (MODEL,)).fetchall()
    return {str(r["document_id"]): list(r["vector"]) for r in rows}


def missing(conn) -> list[dict[str, Any]]:
    """Papers without a vector of the current model (not the failed ones)."""
    return conn.execute(
        "SELECT d.id, d.title, d.abstract FROM documents d "
        "LEFT JOIN document_embeddings e ON e.document_id = d.id "
        "WHERE d.status <> 'failed' AND (e.document_id IS NULL OR e.model <> %s) ORDER BY d.created_at",
        (MODEL,),
    ).fetchall()


def related(conn, document_id: str, k: int = 10) -> list[dict[str, Any]]:
    """The k papers whose vectors are closest to this paper's, with their similarity. Empty when it has no vector."""
    vecs = load_all(conn)
    key = str(document_id)
    if key not in vecs:
        return []
    ranked = vectors.nearest(vecs[key], vecs.items(), k, exclude=key)
    if not ranked:
        return []
    rows = {str(r["id"]): r for r in conn.execute(
        "SELECT id, title, authors, published FROM documents WHERE id = ANY(%s::uuid[])",
        ([doc for doc, _ in ranked],)).fetchall()}
    out = []
    for doc, sim in ranked:
        row = rows.get(doc)
        if row is None:
            continue
        out.append({"id": doc, "title": row["title"], "authors": row["authors"], "published": row["published"],
                    "similarity": round(sim, 3)})
    return out
