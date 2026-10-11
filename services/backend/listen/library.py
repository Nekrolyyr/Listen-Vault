"""Similarity between papers, from their search terms (see worker/enrich.py).

Every paper becomes a normalized vector over its terms. The task and methods count most, then datasets and
topics, then plain keywords. Two papers are similar when their vectors point the same way (cosine).
Groups are built by average-linkage clustering; graph edges connect each paper to its nearest neighbours.
The thresholds are tuned on real libraries: a group needs an average similarity of GROUP_SIMILARITY, a
graph edge needs EDGE_SIMILARITY.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable

KIND_WEIGHT = {"task": 2.0, "method": 2.0, "dataset": 1.5, "topic": 1.5, "keyword": 1.0}
GROUP_SIMILARITY = 0.15
EDGE_SIMILARITY = 0.1
EDGE_NEIGHBOURS = 3

Vector = dict[str, float]


def vectors(rows: Iterable[tuple[str, str, str, float]]) -> dict[str, Vector]:
    """(document id, kind, term, row weight) -> one L2-normalized vector per document."""
    raw: dict[str, Vector] = defaultdict(dict)
    for doc, kind, term, weight in rows:
        raw[doc][term] = raw[doc].get(term, 0.0) + KIND_WEIGHT.get(kind, 1.0) * weight
    out: dict[str, Vector] = {}
    for doc, vec in raw.items():
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        out[doc] = {t: v / norm for t, v in vec.items()}
    return out


def similarity(a: Vector, b: Vector) -> float:
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    return sum(v * large[t] for t, v in small.items() if t in large)


def clusters(vecs: dict[str, Vector], threshold: float = GROUP_SIMILARITY) -> list[list[str]]:
    """Average-linkage agglomerative clustering: merge the two most similar groups while their average
    similarity is at least `threshold`. Quadratic in the number of papers, which is fine for a personal
    library."""
    groups: dict[str, list[str]] = {i: [i] for i in vecs}
    sim: dict[str, dict[str, float]] = {i: {j: similarity(vecs[i], vecs[j]) for j in vecs if j != i} for i in vecs}
    while len(groups) > 1:
        best, pair = threshold, None
        for a in groups:
            for b, s in sim[a].items():
                if b in groups and a < b and s >= best:
                    best, pair = s, (a, b)
        if pair is None:
            break
        a, b = pair
        na, nb = len(groups[a]), len(groups[b])
        for c in groups:
            if c not in (a, b):
                merged = (na * sim[a].get(c, 0.0) + nb * sim[b].get(c, 0.0)) / (na + nb)
                sim[a][c] = sim[c][a] = merged
        groups[a] += groups.pop(b)
        for c in sim:
            sim[c].pop(b, None)
        sim.pop(b, None)
    return list(groups.values())


def label(members: list[str], vecs: dict[str, Vector], n: int = 3) -> list[str]:
    """The terms that describe a group: shared by several members first, then by weight."""
    total: Counter = Counter()
    for m in members:
        total.update(vecs[m])
    needed = min(2, len(members))
    shared = {t: w for t, w in total.items() if sum(t in vecs[m] for m in members) >= needed}
    pool = shared or dict(total)
    return [t for t, _ in sorted(pool.items(), key=lambda kv: -kv[1])[:n]]


def shared_terms(a: Vector, b: Vector, n: int = 4) -> list[str]:
    common = [t for t in a if t in b]
    return sorted(common, key=lambda t: -(a[t] * b[t]))[:n]


def edges(vecs: dict[str, Vector], neighbours: int = EDGE_NEIGHBOURS,
          threshold: float = EDGE_SIMILARITY) -> list[tuple[str, str, float]]:
    """Each paper's nearest neighbours above the threshold, as undirected edges (a, b, similarity)."""
    best: dict[tuple[str, str], float] = {}
    for a in vecs:
        scored = sorted(((similarity(vecs[a], vecs[b]), b) for b in vecs if b != a), reverse=True)[:neighbours]
        for s, b in scored:
            if s >= threshold:
                key = tuple(sorted((a, b)))
                best[key] = max(best.get(key, 0.0), s)
    return [(a, b, round(s, 3)) for (a, b), s in best.items()]
