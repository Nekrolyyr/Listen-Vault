"""Vector helpers that need no ML library: the api and the worker both use them.

Papers are compared by cosine similarity of their SPECTER2 vectors (worker/embed.py). Vectors are plain lists of floats;
a library of a few thousand papers is small enough to compare all pairs.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable, Sequence

log = logging.getLogger(__name__)

Vector = Sequence[float]
MAX_CLUSTER_ITEMS = 800  # average linkage is cubic in the number of items; above this the caller must do something else


def normalize(vec: Vector) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else [float(x) for x in vec]


def cosine(a: Vector, b: Vector) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


def centroid(vectors: Iterable[Vector]) -> list[float]:
    rows = [list(v) for v in vectors]
    if not rows:
        return []
    return normalize([sum(col) / len(rows) for col in zip(*rows)])


def nearest(query: Vector, items: Iterable[tuple[str, Vector]], k: int,
            exclude: str | None = None) -> list[tuple[str, float]]:
    """The k items most similar to the query, best first, as (key, similarity)."""
    scored = [(key, cosine(query, vec)) for key, vec in items if key != exclude]
    scored.sort(key=lambda s: (-s[1], s[0]))
    return scored[:k]


def rrf(rankings: list[list[str]], k: int = 60) -> list[str]:
    """Reciprocal rank fusion: each ranking adds 1 / (k + rank) to the keys it lists. Keys in more rankings, and
    higher in them, come first."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))


def average_linkage(keys: list[str], sim: Callable[[str, str], float], threshold: float) -> list[list[str]]:
    """Merge the two most similar groups while their average similarity is at least `threshold`.

    The same method as library.clusters, for items compared by a similarity function instead of term vectors.
    Quadratic per merge; the caller keeps the number of items below MAX_CLUSTER_ITEMS.
    """
    if len(keys) > MAX_CLUSTER_ITEMS:
        raise ValueError(f"{len(keys)} items is too many to cluster (limit {MAX_CLUSTER_ITEMS})")
    groups: dict[str, list[str]] = {k: [k] for k in keys}
    table: dict[str, dict[str, float]] = {k: {j: sim(k, j) for j in keys if j != k} for k in keys}
    while len(groups) > 1:
        best, pair = threshold, None
        for a in groups:
            for b, s in table[a].items():
                if b in groups and a < b and s >= best:
                    best, pair = s, (a, b)
        if pair is None:
            break
        a, b = pair
        na, nb = len(groups[a]), len(groups[b])
        for c in groups:
            if c not in (a, b):
                merged = (na * table[a].get(c, 0.0) + nb * table[b].get(c, 0.0)) / (na + nb)
                table[a][c] = table[c][a] = merged
        groups[a] += groups.pop(b)
        for c in table:
            table[c].pop(b, None)
        table.pop(b, None)
    return list(groups.values())
