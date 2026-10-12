"""Topics: keywords that appear together across the library, named by the LLM.

A keyword counts when at least two papers have it (from the LLM's terms; frequent words of the text are too
noisy). Keywords are compared by the papers they share (cosine), and similar ones are clustered with the same
average-linkage method as the paper groups. Then the LLM names each cluster and checks its members in batches:
a term it says does not belong is left out. Without an LLM a topic is named after its most common term.

A paper is on a topic when it has one of the topic's keywords, or when at least CONTENT_MATCHES of the topic's
terms appear in its title or abstract. The second rule places the catalogued papers, which have no keywords yet.
A topic's area is the most common primary area of its papers. Topics are rebuilt as a whole, so the result always
matches the current keywords, placements and the papers' manual topic choices (see document_topic_overrides).
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from math import sqrt

from psycopg_pool import ConnectionPool

from .. import areas, library
from .llm import LlmClient, LlmError

log = logging.getLogger(__name__)

MIN_PAPERS = 2          # a keyword needs this many papers to define a topic
MAX_TERMS = 300         # the most common keywords are clustered (clustering is quadratic)
CLUSTER_SIMILARITY = 0.3
MIN_TOPIC_TERMS = 2
MAX_TOPICS = 25
BATCH = 40              # members checked per LLM call
CONTENT_MATCHES = 2     # terms of a topic that must appear in a paper's title or abstract for it to count


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


def in_topic(terms: list[str], keywords: set[str], text: str) -> bool:
    """True when the paper has one of the terms as a keyword, or the terms appear in its text (lower case)."""
    if any(t in keywords for t in terms):
        return True
    found = sum(1 for t in terms if re.search(rf"\b{re.escape(t)}\b", text))
    return found >= CONTENT_MATCHES


def topic_area(members: list[str], primary: dict[str, str]) -> str:
    """The most common primary area of the members; other only when nothing else fits."""
    counts = Counter(primary[m] for m in members if primary.get(m, areas.OTHER) != areas.OTHER)
    return counts.most_common(1)[0][0] if counts else areas.OTHER


def matching_topic(terms: set[str], old: dict[int, set[str]], taken: set[int]) -> int | None:
    """The id of the earlier topic whose terms overlap these the most (at least half of both), so that a topic keeps
    its id, and its links, when the library is rebuilt."""
    best, best_score = None, 0.5
    for topic_id, old_terms in old.items():
        if topic_id in taken or not old_terms:
            continue
        score = len(terms & old_terms) / len(terms | old_terms)
        if score >= best_score:
            best, best_score = topic_id, score
    return best


def rebuild(pool: ConnectionPool, llm: LlmClient | None) -> int:
    """Recompute the topics and their papers. Returns the number of topics."""
    with pool.connection() as conn:
        rows = conn.execute("SELECT document_id, term FROM document_keywords WHERE source = 'llm'").fetchall()
        everything = conn.execute("SELECT document_id, term FROM document_keywords").fetchall()
        papers = conn.execute("SELECT id, title, abstract FROM documents WHERE status <> 'failed'").fetchall()
        placed = conn.execute("SELECT document_id, primary_area FROM document_areas").fetchall()
        overrides = conn.execute("SELECT document_id, topic_name, include FROM document_topic_overrides").fetchall()
    doc_terms: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        doc_terms[str(r["document_id"])].add(r["term"])
    keywords: dict[str, set[str]] = defaultdict(set)
    for r in everything:
        keywords[str(r["document_id"])].add(r["term"])
    text = {str(p["id"]): f"{p['title'] or ''} {p['abstract'] or ''}".lower() for p in papers}
    primary = {str(r["document_id"]): r["primary_area"] for r in placed}
    if llm is not None and not llm.wait_ready(timeout_s=60):
        llm = None
        log.warning("LLM not available; topics are named after their most common keyword")
    built = build(doc_terms, llm)
    with pool.connection() as conn, conn.transaction():
        old = {r["id"]: set(r["terms"]) for r in conn.execute("SELECT id, terms FROM topics")}
        taken: set[int] = set()
        for name, terms in built:
            members = [doc for doc in text if in_topic(terms, keywords.get(doc, set()), text[doc])]
            values = (name, terms, topic_area(members, primary), len(members))
            topic_id = matching_topic(set(terms), old, taken)
            if topic_id is None:
                topic_id = conn.execute(
                    "INSERT INTO topics (name, terms, area, paper_count) VALUES (%s, %s, %s, %s) RETURNING id",
                    values).fetchone()["id"]
            else:
                conn.execute("UPDATE topics SET name = %s, terms = %s, area = %s, paper_count = %s, built_at = now() "
                             "WHERE id = %s", (*values, topic_id))
            taken.add(topic_id)
            conn.execute("DELETE FROM document_topics WHERE topic_id = %s", (topic_id,))
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO document_topics (document_id, topic_id) VALUES (%s, %s)",
                                [(doc, topic_id) for doc in members])
        conn.execute("DELETE FROM topics WHERE NOT (id = ANY(%s::int[]))", (list(taken),))
        for o in overrides:
            if o["include"]:
                conn.execute(
                    "INSERT INTO document_topics (document_id, topic_id) SELECT %s, id FROM topics WHERE name = %s "
                    "ON CONFLICT DO NOTHING", (o["document_id"], o["topic_name"]))
            else:
                conn.execute(
                    "DELETE FROM document_topics WHERE document_id = %s AND topic_id IN "
                    "(SELECT id FROM topics WHERE name = %s)", (o["document_id"], o["topic_name"]))
        conn.execute("UPDATE topics t SET paper_count = (SELECT count(*) FROM document_topics dt "
                     "WHERE dt.topic_id = t.id)")
    log.info("topics: %d", len(built))
    return len(built)
