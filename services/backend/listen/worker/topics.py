"""Topics: keywords that appear together across the library, named by the LLM.

A keyword counts when at least two papers have it (from the LLM's terms; frequent words of the text are too
noisy). Keywords are compared by the papers they share (cosine), and similar ones are clustered with the same
average-linkage method as the paper groups. Then the LLM names each cluster and checks its members in batches:
a term it says does not belong is left out. Without an LLM a topic is named after its most common term.
Topics are rebuilt as a whole, so the result always matches the current keywords.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from math import sqrt

from psycopg_pool import ConnectionPool

from .. import library
from .llm import LlmClient, LlmError

log = logging.getLogger(__name__)

MIN_PAPERS = 2          # a keyword needs this many papers to define a topic
MAX_TERMS = 300         # the most common keywords are clustered (clustering is quadratic)
CLUSTER_SIMILARITY = 0.3
MIN_TOPIC_TERMS = 2
MAX_TOPICS = 25
BATCH = 40              # members checked per LLM call


def term_vectors(doc_terms: dict[str, set[str]]) -> dict[str, dict[str, float]]:
    """Each keyword as a normalized vector over the papers that have it."""
    papers_of: dict[str, set[str]] = defaultdict(set)
    for doc, terms in doc_terms.items():
        for term in terms:
            papers_of[term].add(doc)
    common = sorted((t for t, docs in papers_of.items() if len(docs) >= MIN_PAPERS),
                    key=lambda t: (-len(papers_of[t]), t))[:MAX_TERMS]
    return {t: {d: 1 / sqrt(len(papers_of[t])) for d in papers_of[t]} for t in common}


def clean_name(answer: str) -> str | None:
    name = answer.strip().strip('"').strip().rstrip(".").strip()
    words = name.split()
    if not 1 <= len(words) <= 5 or "keyword" in name.lower() or "topic" == name.lower():
        return None
    return name[0].upper() + name[1:]


def name_topic(llm: LlmClient, terms: list[str]) -> str | None:
    prompt = (f"Keywords from scientific papers: {', '.join(terms[:25])}.\n\n"
              "Name the research topic these keywords belong to, in two to four words:")
    return clean_name(llm.complete(prompt, 16, 0.2))


def rejected(llm: LlmClient, name: str, terms: list[str]) -> set[str]:
    """Members the LLM says do not belong to the topic."""
    prompt = (f"Topic: {name}.\nKeywords: {', '.join(terms)}.\n\n"
              "List the keywords above that do not belong to the topic, separated by commas. Write none if all belong:")
    answer = llm.complete(prompt, 60, 0.2).lower().strip().rstrip(".")
    if not answer or answer.startswith("none"):
        return set()
    listed = {part.strip().strip(".") for part in answer.split(",")}
    return {t for t in terms if t in listed}


def build(doc_terms: dict[str, set[str]], llm: LlmClient | None) -> list[tuple[str, list[str]]]:
    """Topics as (name, terms), largest first."""
    vecs = term_vectors(doc_terms)
    groups = [g for g in library.clusters(vecs, threshold=CLUSTER_SIMILARITY) if len(g) >= MIN_TOPIC_TERMS]
    groups.sort(key=lambda g: -sum(len(vecs[t]) for t in g))
    topics: list[tuple[str, list[str]]] = []
    for group in groups[:MAX_TOPICS]:
        ordered = sorted(group, key=lambda t: (-len(vecs[t]), t))
        name, kept = None, ordered
        if llm is not None:
            try:
                name = name_topic(llm, ordered)
                if name:
                    bad: set[str] = set()
                    for start in range(0, len(ordered), BATCH):
                        bad |= rejected(llm, name, ordered[start:start + BATCH])
                    kept = [t for t in ordered if t not in bad]
            except LlmError as e:
                log.warning("topic naming failed: %s", e)
                name = None
        if len(kept) < MIN_TOPIC_TERMS:
            continue
        topics.append((name or kept[0].capitalize(), kept))
    return topics


def rebuild(pool: ConnectionPool, llm: LlmClient | None) -> int:
    """Recompute the topics from the LLM keywords of the library. Returns the number of topics."""
    with pool.connection() as conn:
        rows = conn.execute("SELECT document_id, term FROM document_keywords WHERE source = 'llm'").fetchall()
    doc_terms: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        doc_terms[str(r["document_id"])].add(r["term"])
    if llm is not None and not llm.wait_ready(timeout_s=60):
        llm = None
        log.warning("LLM not available; topics are named after their most common keyword")
    topics = build(doc_terms, llm)
    with pool.connection() as conn, conn.transaction():
        conn.execute("DELETE FROM topics")
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO topics (name, terms) VALUES (%s, %s)", topics)
    log.info("topics: %d", len(topics))
    return len(topics)
