"""Questions about a paper, answered from the paper's own passages.

Retrieval: BM25 over passages of about PASSAGE_WORDS words, fused with the ranking by passage vectors (bge-small, see
worker/embed.py) by reciprocal rank fusion. The fused best passages go into one prompt, numbered; the model must cite
them as [n]. Citations give the page, so the reader can jump to the place the answer comes from. Answers that are
empty, loop, or cite nothing are rejected and asked again once. QA_RETRIEVAL=bm25 turns the vectors off.
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from .. import config
from ..storage import DocPaths
from ..vectors import rrf
from .docmodel import Document
from . import embed, sentences
from .enrich import STOPWORDS
from .llm import LlmClient, LlmError, budget

log = logging.getLogger(__name__)

PASSAGE_WORDS = 150
TOP_K = 4               # passages for keyword search alone
TOP_K_HYBRID = 8        # passages after fusing keyword and vector rankings
SYSTEM = "You answer questions about a scientific paper from numbered passages. Cite them as [n]."
K1, B = 1.5, 0.75
TOKEN = re.compile(r"[a-z0-9][a-z0-9-]+")
CITE = re.compile(r"\[(\d+)\]")


@dataclass
class Passage:
    n: int          # 1-based, the number used in citations
    text: str
    page: int | None
    title: str


SUFFIXES = ("ations", "ation", "ings", "ing", "ed", "ies", "es", "s")


def stem(token: str) -> str:
    """Light stemming so that "pruned", "pruning" and "prune" meet ("heads" -> "head")."""
    for suffix in SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return token


QUESTION_WORDS = {"how", "what", "which", "who", "whom", "whose", "why", "when", "where", "are", "was", "were",
                  "does", "did", "doing", "can", "could", "would", "should", "there", "their", "this", "that",
                  "these", "those", "into", "than", "then", "also", "about", "any", "all"}


def tokens(text: str) -> list[str]:
    return [stem(t) for t in TOKEN.findall(text.lower())
            if t not in STOPWORDS and t not in QUESTION_WORDS and len(t) > 2]


def _pieces(text: str) -> list[str]:
    """A paragraph, or its sentence groups when it is longer than PASSAGE_WORDS."""
    if len(text.split()) <= PASSAGE_WORDS:
        return [text]
    pieces, current = [], []
    for s in sentences.split(text):
        if current and len(" ".join(current + [s]).split()) > PASSAGE_WORDS:
            pieces.append(" ".join(current))
            current = []
        current.append(s)
    if current:
        pieces.append(" ".join(current))
    return pieces


def passages(doc: Document) -> list[Passage]:
    """One passage per paragraph of the abstract and body, so each citation points to the exact page."""
    units = [(p.text, p.page, "Abstract") for p in doc.abstract]
    for section in doc.sections:
        units += [(p.text, p.page, section.title or "") for p in section.paragraphs if p.kind == "text" and p.text]
    out: list[Passage] = []
    for body, page, title in units:
        for piece in _pieces(body):
            out.append(Passage(len(out) + 1, piece, page, title))
    return out


def rank(question: str, items: list[Passage], k: int = TOP_K) -> list[Passage]:
    """The passages that match the question best (BM25), best first. Passages without any match are left out."""
    query = set(tokens(question))
    if not query or not items:
        return []
    docs = [Counter(tokens(p.text)) for p in items]
    lengths = [sum(c.values()) for c in docs]
    avg = sum(lengths) / len(lengths) or 1.0
    df = Counter(t for c in docs for t in query if t in c)
    n = len(items)
    scored = []
    for passage, counts, length in zip(items, docs, lengths):
        score = 0.0
        for t in query:
            tf = counts.get(t, 0)
            if tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / avg))
        if score > 0:
            scored.append((score, passage))
    scored.sort(key=lambda s: -s[0])
    return [p for _, p in scored[:k]]


def fit(llm: LlmClient, candidates: list[Passage], limit: int | None = None) -> list[Passage]:
    """Keep the best passages whose total length fits the context budget; renumber them 1..n."""
    limit = budget(llm, "context") if limit is None else limit
    chosen: list[Passage] = []
    used = 0
    for p in candidates:
        cost = llm.count(p.text)
        if used + cost > limit:
            continue
        used += cost
        chosen.append(Passage(len(chosen) + 1, p.text, p.page, p.title))
    return chosen


def retrieve(question: str, items: list[Passage], encoder=None) -> list[Passage]:
    """The passages to show the model, best first. Without an encoder: BM25 alone. With one: the BM25 ranking and the
    vector ranking are fused, so a passage that shares few words with the question can still come through."""
    if encoder is None:
        return rank(question, items)
    bm25 = [p.n for p in rank(question, items, k=len(items))]
    query = encoder.query_vector(question)
    vecs = encoder.passage_vectors([p.text for p in items])
    order = sorted(range(len(items)), key=lambda i: -sum(x * y for x, y in zip(query, vecs[i])))
    dense = [items[i].n for i in order]
    by_n = {p.n: p for p in items}
    return [by_n[n] for n in rrf([bm25, dense])[:TOP_K_HYBRID]]


def prompt(question: str, context: list[Passage]) -> str:
    blocks = "\n\n".join(f"[{p.n}] ({p.title or 'Text'}, p. {p.page or '?'}) {p.text}" for p in context)
    return (f"Passages from a scientific paper:\n\n{blocks}\n\n"
            f"Question: {question}\n"
            "Answer in two or three sentences, using only the passages above. Cite the passage you use, like [1]. "
            "If the passages do not answer the question, say so.")


def clean(answer: str, n: int) -> tuple[str, list[int]] | None:
    """Return (answer, cited passage numbers), or None if the answer is unusable."""
    text = re.sub(r"^\s*(?:answer)\s*:\s*", "", answer.strip().strip('"'), flags=re.I)
    cited = [int(x) for x in CITE.findall(text) if 1 <= int(x) <= n]
    text = CITE.sub(lambda m: m.group(0) if 1 <= int(m.group(1)) <= n else "", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text).strip()
    words = re.findall(r"[A-Za-z0-9']+", text.lower())
    if len(words) < 4:
        return None
    if len(set(words)) < 0.4 * len(words) and len(words) > 12:
        return None  # repetition loop
    if re.search(r"passages? above|the question", text, re.I):
        return None  # echoed the prompt
    if text[-1] not in ".!?":
        text += "."
    return text[0].upper() + text[1:], list(dict.fromkeys(cited))


def answer(llm: LlmClient, document: Document, question: str, encoder=None) -> tuple[str, list[dict]] | None:
    context = fit(llm, retrieve(question, passages(document), encoder))
    if not context:
        return None
    for temperature in (0.2, 0.6):  # one retry with more variety
        try:
            raw = llm.chat(prompt(question, context), system=SYSTEM, n_predict=budget(llm, "answer"),
                           temperature=temperature, feature="ask")
        except LlmError as e:
            log.warning("question answering failed: %s", e)
            return None
        if result := clean(raw, len(context)):
            text, cited = result
            by_n = {p.n: p for p in context}
            picked = [by_n[n] for n in cited] or [context[0]]  # no citation: point at the best passage
            return text, [{"n": p.n, "page": p.page, "title": p.title, "text": p.text[:240]} for p in picked]
    return None


def run(pool: ConnectionPool, settings: config.Settings, llm: LlmClient | None, job: dict) -> None:
    """Answer one question. The question row records the outcome; the job itself does not fail."""
    question_id = (job.get("options") or {}).get("question_id")
    with pool.connection() as conn:
        row = conn.execute("SELECT question FROM questions WHERE id = %s", (question_id,)).fetchone()
    if row is None:
        return  # the question was deleted with its document
    result, error = None, None
    structure = DocPaths(settings.docs_dir, job["document_id"]).structure
    if llm is None or not llm.wait_ready(timeout_s=60):
        error = "The language model is not running; ask again later."
    elif not structure.exists():
        error = "This paper has no text yet."
    else:
        encoder = embed.passage_encoder(settings.models_dir) if settings.qa_retrieval == "hybrid" else None
        result = answer(llm, Document.load(structure), row["question"], encoder)
        if result is None:
            error = "The model could not answer from this paper. Try a more specific question."
    with pool.connection() as conn, conn.transaction():
        if result is None:
            conn.execute("UPDATE questions SET status = 'failed', error = %s WHERE id = %s", (error, question_id))
        else:
            text, citations = result
            conn.execute("UPDATE questions SET status = 'done', answer = %s, citations = %s::jsonb, error = NULL "
                         "WHERE id = %s", (text, json.dumps(citations, ensure_ascii=False), question_id))
