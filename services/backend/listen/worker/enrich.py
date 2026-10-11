"""Search and indexing terms for a paper or article.

BLOOMZ-7b1 is asked for a JSON object with the task, methods, datasets, topics and keywords. The request is
constrained by a JSON schema, so the answer always parses; the values are still cleaned. Frequent words of
the text are added without the LLM, so a paper still gets terms when the model is unavailable or its answer
is unusable.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass

from . import sentences
from .docmodel import Document
from .llm import LlmClient, LlmError

log = logging.getLogger(__name__)

EXCERPT_TOKENS = 900  # what the LLM sees: abstract, section titles and openings (context is 2,048)
FREQUENCY_TERMS = 8
FREQUENCY_WEIGHT = 0.5

SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string"},
        "methods": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "datasets": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "topics": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
    },
    "required": ["task", "methods", "datasets", "topics", "keywords"],
}

KINDS = {"task": "task", "methods": "method", "datasets": "dataset", "topics": "topic", "keywords": "keyword"}

# Words that say nothing about the subject of a paper: generic English and the vocabulary of papers.
STOPWORDS = set("""
about above across after again against along also although among another around because been before being
below between beyond both change could data does done during each either else even ever every example
first found from further gives given good great have having here higher hence however into itself just
known large last later least less like likely long made make many more most much must need never next
none nor note only other others otherwise over paper papers particular past per perhaps please possible
present previous proposed propose provide provided rather really same section sections several shall
should show shown shows since small some such take than that their them then there these they thing this
those though three through thus together toward under until upon used uses using usually value very
want was ways well were what when where whether which while whole whose will with within without work
would yield your figure figures table tables results result study studies approach approaches based model
models method methods introduction conclusion conclusions abstract references acknowledgements
""".split())
WORD = re.compile(r"[a-z][a-z-]{3,}")


@dataclass(frozen=True)
class Term:
    kind: str       # task | method | dataset | topic | keyword
    term: str
    source: str     # llm | frequency
    weight: float


def clean_terms(values, limit: int) -> list[str]:
    """Normalize LLM values: lower case, no punctuation at the ends, no duplicates, short phrases only."""
    out: list[str] = []
    for value in values or []:
        if not isinstance(value, str):
            continue
        term = re.sub(r"\s+", " ", value).strip(" .;:,").lower()
        if 2 <= len(term) <= 60 and term not in out:
            out.append(term)
        if len(out) >= limit:
            break
    return out


def frequency_terms(text: str, n: int = FREQUENCY_TERMS) -> list[str]:
    words = [w for w in WORD.findall(text.lower()) if w not in STOPWORDS]
    return [w for w, _ in Counter(words).most_common(n)]


def excerpt(doc: Document, llm: LlmClient) -> str:
    """The part of the paper the LLM reads: abstract, section titles and the first sentences of each section."""
    parts = [" ".join(p.text for p in doc.abstract)]
    parts += [s.title for s in doc.sections if s.title]
    for section in doc.sections:
        for p in section.paragraphs[:2]:
            if p.kind == "text" and p.text:
                parts.append(" ".join(sentences.split(p.text)[:2]))
    return llm.truncate("\n".join(x for x in parts if x), EXCERPT_TOKENS)


def llm_terms(doc: Document, llm: LlmClient) -> list[Term]:
    prompt = (f"{excerpt(doc, llm)}\n\n"
              "Describe the scientific paper above: its task, methods, datasets, topics and keywords.")
    try:
        answer = llm.complete_json(prompt, SCHEMA)
    except LlmError as e:
        log.warning("keyword extraction by the LLM failed: %s", e)
        return []
    terms: list[Term] = []
    for field_name, kind in KINDS.items():
        values = answer.get(field_name)
        if field_name == "task" and isinstance(values, str):
            values = [values]
        limit = 1 if field_name == "task" else 10
        for term in clean_terms(values, limit):
            terms.append(Term(kind, term, "llm", 1.0))
    return terms


def extract(doc: Document, llm: LlmClient | None) -> list[Term]:
    text = " ".join([*(p.text for p in doc.abstract), *(p.text for s in doc.sections for p in s.paragraphs)])
    terms = llm_terms(doc, llm) if llm is not None else []
    known = {t.term for t in terms}
    for word in frequency_terms(text):
        if word not in known:
            terms.append(Term("keyword", word, "frequency", FREQUENCY_WEIGHT))
    return terms
