"""Search and indexing terms for a paper or article.

BLOOMZ-7b1 answers one short question per field (task, methods, datasets), then the keywords as JSON. Every
answer is checked before it is kept:

- it must be grounded in the paper: a method or dataset has to appear in the text, a task has to share its
  content words with the abstract and title. BLOOMZ paraphrases and echoes the question; those answers fail here.
- it must not be an instruction ("present the main theories"), a generic word ("data", "general") or a
  sentence, and a term belongs to one field only (the first field in FIELD_PRIORITY wins).

Rules fill gaps the model leaves: a method from a list of method names found in the text, and a dataset named
in a sentence such as "data collected from Twitter". Frequent words of the text are added without the LLM, so a
paper still gets terms when the model is unavailable.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from . import sentences
from .chapters import accept
from .docmodel import Document
from .llm import LlmClient, LlmError, budget

log = logging.getLogger(__name__)

EXCERPT_TOKENS = 900  # what the LLM sees (context is 2,048); methods and data sections are chosen for it
FREQUENCY_TERMS = 8
FREQUENCY_WEIGHT = 0.5
FIELD_PRIORITY = ("task", "method", "dataset", "topic", "keyword")

# One question per field. The paper excerpt comes first and is identical for all of them, so the server reuses
# its cache and only the question is processed again.
# One chat call returns every field as typed JSON (keywords_mode "typed"). The paper excerpt comes first, as it does
# for the legacy questions, so the server reuses its cache.
TYPED_FIELDS = (("task", "task", 1), ("methods", "method", 5), ("datasets", "dataset", 5),
                ("topics", "topic", 5), ("keywords", "keyword", 10))
TYPED_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string"},
        **{name: {"type": "array", "items": {"type": "string"}, "maxItems": limit}
           for name, _, limit in TYPED_FIELDS[1:]},
    },
    "required": [name for name, _, _ in TYPED_FIELDS],
}
TYPED_SYSTEM = ("You index scientific papers for search. Answer with JSON only. Use the paper's own words; "
                "never write instructions or sentences.")
TYPED_REQUEST = ("Index this paper. task: its research question or topic as a short noun phrase. methods: the "
                 "research methods it uses. datasets: the data it analyses, named as the paper names it; an empty list "
                 "when it has no data. topics: its research topics. keywords: its most important keywords. Every item "
                 "is a short phrase of at most six words.")
TAGGER_WEIGHT = 0.6  # a keyword only the tagger found, beside the LLM's own keywords (weight 1.0)

QUESTIONS = {
    "task": ("What is the research question or topic of this paper? Answer with a short noun phrase.", 24, 1),
    "method": ("Which research methods does this paper use? List them separated by commas, in the paper's own words.", 48, 5),
    "dataset": ("Which data does this paper analyse: its dataset, sample or corpus? Name it in the paper's own words, "
                "separated by commas. Answer none if the paper has no data.", 48, 5),
}
KEYWORD_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
    },
    "required": ["topics", "keywords"],
}
KEYWORD_QUESTION = "List the research topics and keywords of this paper as JSON with the keys topics and keywords."

# Words that start an instruction or a summary sentence, not a term.
INSTRUCTION_START = {
    "present", "introduce", "explore", "analyze", "analyse", "identify", "describe", "explain", "discuss", "examine",
    "investigate", "review", "show", "study", "provide", "summarize", "outline", "focus", "list", "state", "answer",
    "determine", "evaluate", "highlight", "illustrate", "compare", "develop", "propose", "understand", "consider",
}
# Terms that name nothing specific in a paper.
GENERIC = {
    "none", "n/a", "na", "general", "presentation", "data", "dataset", "datasets", "method", "methods", "approach",
    "approaches", "analysis", "study", "paper", "results", "research", "model", "models", "framework", "information",
    "content", "other", "various", "different", "section", "figure", "table", "work", "article", "review", "unknown",
    "metods", "methodology", "procedure", "procedures", "technique", "techniques", "tool", "tools",
    "empirical", "information", "description", "topics", "topic", "datasets", "scientific paper", "question",
    "questions", "not specified", "not mentioned", "unclear", "various methods", "other methods",
}
# Answers that say the model found nothing ("not enough information", "no data are mentioned").
NO_ANSWER = re.compile(r"\b(not|no|none|unknown|unclear|enough|mentioned|specified|available)\b", re.I)
QUESTION_START = re.compile(r"^(how|why|what|which|whether|when|where|who)\b(\s+(can|do|does|did|is|are|was|were|should|could|would|we|they|the|a|an))*\s*", re.I)
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
MATCH_STOP = {"of", "and", "the", "a", "an", "in", "on", "for", "to", "with", "from", "by", "at", "its", "their", "this"}
WORD = re.compile(r"[a-z][a-z-]{3,}")

# Method names that are specific enough to be read off the text when the model names none.
METHOD_NAMES = [
    "case study", "content analysis", "discourse analysis", "thematic analysis", "qualitative analysis",
    "quantitative analysis", "mixed methods", "mixed-methods", "survey", "interview", "interviews", "focus group",
    "ethnography", "participant observation", "randomized controlled trial", "randomised controlled trial",
    "controlled experiment", "field experiment", "experiment", "regression", "network analysis",
    "social network analysis", "community detection", "agent-based model", "simulation", "machine learning",
    "deep learning", "topic modeling", "topic modelling", "sentiment analysis", "text mining", "clustering",
    "literature review", "systematic review", "meta-analysis", "bibliometric analysis", "visual analysis",
    "statistical analysis", "hypothesis testing", "time series analysis", "archival research", "comparative analysis",
    "descriptive statistics", "structural equation modeling", "difference-in-differences", "web crawling",
]
PLATFORMS = ["Twitter", "Instagram", "Facebook", "Reddit", "TikTok", "YouTube", "WhatsApp", "Telegram", "Weibo"]

# Phrases that end a noun phrase when reading a dataset name out of a sentence.
PHRASE_END = re.compile(r"[,;:()\[\]]|\b(which|that|who|were|was|are|is|in|during|between|with|to|for|as|including|"
                        r"containing|where|by|and|or|from|on|at)\b", re.I)
DATA_PATTERNS = [
    re.compile(r"\b(?:data(?:set|sets)?|data set|corpus|sample)\s+(?:from|of|on|collected from|obtained from|"
               r"drawn from|sourced from)\s+([^.\n]{3,80})", re.I),
    re.compile(r"\b(?:collected|crawled|scraped|downloaded|obtained|sampled|gathered)\s+(?:from|via|on|through)\s+([^.\n]{3,80})", re.I),
    re.compile(r"\b(?:the|our|a|an|this)\s+((?:[\w'’-]+\s+){0,4}?[\w'’-]+)\s+(?:dataset|data set|corpus)\b", re.I),
]
DATA_CONTEXT = re.compile(r"\b(data|dataset|posts|comments|tweets|messages|sample|corpus)\b", re.I)
METHOD_SECTION = re.compile(r"method|data|sample|participant|procedure|experiment|design|material|corpus|setting|survey", re.I)
CUE = re.compile(r"\b(we|our|data|dataset|sample|corpus|collected|survey|experiment|interview|analy[sz]|method|design|crawl|scrap)\w*", re.I)


@dataclass(frozen=True)
class Term:
    kind: str       # task | method | dataset | topic | keyword
    term: str
    source: str     # llm | rule | frequency
    weight: float


def clean_terms(values, limit: int) -> list[str]:
    """Normalize values: lower case, no punctuation at the ends, no duplicates, short phrases only."""
    out: list[str] = []
    for value in values or []:
        if not isinstance(value, str):
            continue
        term = re.sub(r"\s+", " ", value).strip(" .;:,\"'").lower()
        if 2 <= len(term) <= 60 and term not in out:
            out.append(term)
        if len(out) >= limit:
            break
    return out


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _content(term: str) -> list[str]:
    return [w for w in _words(term) if w not in MATCH_STOP]


def grounded(term: str, text: str) -> bool:
    """The term's phrase occurs in the text, or every content word of it does."""
    norm = " " + " ".join(_words(text)) + " "
    phrase = " " + " ".join(_words(term)) + " "
    if phrase in norm:
        return True
    content = _content(term)
    return bool(content) and all(f" {w} " in norm for w in content)


def overlap(term: str, text: str) -> float:
    """Share of the term's content words that occur in the text (for tasks, which may be paraphrased)."""
    content = _content(term)
    if not content:
        return 0.0
    words = set(_words(text))
    return sum(w in words for w in content) / len(content)


def as_statement(term: str) -> str:
    """A question as a noun phrase: "how can we quantify echo chambers?" -> "quantify echo chambers"."""
    return QUESTION_START.sub("", term.rstrip("?").strip()).strip()


def usable(term: str, kind: str, paper: str, body: str) -> bool:
    """Whether a value the model gave can be kept as a term of this kind."""
    words = term.split()
    if not words or len(words) > 6 or term in GENERIC or NO_ANSWER.search(term):
        return False
    if words[0] in INSTRUCTION_START or " the paper" in term or "this paper" in term:
        return False
    if kind == "task":
        return overlap(term, paper) >= 0.5 and term.split()[0] not in INSTRUCTION_START
    return grounded(term, body)


def split_answer(answer: str) -> list[str]:
    """A list answer: the first line, split on commas and semicolons (not "and": method names contain it)."""
    line = answer.strip().split("\n")[0]
    return [p for p in re.split(r"[,;]", line)]


def frequency_terms(text: str, n: int = FREQUENCY_TERMS) -> list[str]:
    words = [w for w in WORD.findall(text.lower()) if w not in STOPWORDS]
    return [w for w, _ in Counter(words).most_common(n)]


def _section_is_method(title: str | None) -> bool:
    return bool(title and METHOD_SECTION.search(title))


def excerpt(doc: Document, llm: LlmClient) -> str:
    """What the LLM reads: abstract, section titles, the first sentences of each section, and the sentences of
    method and data sections that say what was done and with which data."""
    parts = [" ".join(p.text for p in doc.abstract)]
    parts += [s.title for s in doc.sections if s.title]
    for section in doc.sections:
        method_like = _section_is_method(section.title)
        for p in section.paragraphs[: 6 if method_like else 2]:
            if p.kind != "text" or not p.text:
                continue
            sents = sentences.split(p.text)
            if method_like:
                chosen = [s for s in sents if CUE.search(s)][:4] or sents[:1]
            else:
                chosen = sents[:2]
            parts.append(" ".join(chosen))
    return llm.truncate("\n".join(x for x in parts if x), budget(llm, "excerpt"))


def _paper_text(doc: Document) -> tuple[str, str]:
    """(abstract and title, whole text) as plain strings."""
    paper = " ".join([doc.title, *(p.text for p in doc.abstract)])
    body = " ".join([paper, *(p.text for s in doc.sections for p in s.paragraphs)])
    return paper, body


def typed_terms(doc: Document, llm: LlmClient) -> list[Term]:
    """Every field from one chat call with a JSON schema (keywords_mode "typed"). Each item is checked the same way as
    the legacy answers: it must be grounded, and it must not be an instruction or a generic word."""
    paper, body = _paper_text(doc)
    try:
        answer = llm.chat_json(f"{excerpt(doc, llm)}\n\n{TYPED_REQUEST}", TYPED_SCHEMA, system=TYPED_SYSTEM,
                               n_predict=400, cache_prompt=True, feature="keywords")
    except LlmError as e:
        log.warning("keyword extraction by the LLM failed: %s", e)
        return []
    terms: list[Term] = []
    for field_name, kind, limit in TYPED_FIELDS:
        values = answer.get(field_name)
        items = clean_terms([values] if isinstance(values, str) else values, limit)
        for item in items:
            if kind == "task":
                item = as_statement(item)
            if usable(item, kind, paper, body):
                terms.append(Term(kind, item, "llm", 1.0))
    return terms


def tagger_terms(doc: Document, tagger: Callable[[str], list[str]]) -> list[Term]:
    """Keyphrases the keyword tagger finds in the text (see worker/tagger.py), checked like the LLM's."""
    paper, body = _paper_text(doc)
    try:
        found = tagger(body)
    except Exception as e:  # noqa: BLE001 - a broken tagger must not stop the keywords
        log.warning("keyword tagger failed: %s", e)
        return []
    return [Term("keyword", t, "tagger", TAGGER_WEIGHT) for t in found if usable(t, "keyword", paper, body)]


def llm_terms(doc: Document, llm: LlmClient) -> list[Term]:
    paper, body = _paper_text(doc)
    prefix = f"{excerpt(doc, llm)}\n\n"
    terms: list[Term] = []
    for kind, (question, n_predict, limit) in QUESTIONS.items():
        try:
            answer = llm.complete(f"{prefix}Question: {question}\nAnswer:", n_predict=n_predict, temperature=0.1,
                                  stop=["\n", "Question:"], cache_prompt=True)
        except LlmError as e:
            log.warning("%s extraction by the LLM failed: %s", kind, e)
            continue
        for item in clean_terms(split_answer(answer), 8):
            if kind == "task":
                item = as_statement(item)
            if usable(item, kind, paper, body):
                terms.append(Term(kind, item, "llm", 1.0))
        terms = _limit(terms, kind, limit)
    try:
        answer = llm.complete_json(f"{prefix}Question: {KEYWORD_QUESTION}", KEYWORD_SCHEMA, cache_prompt=True)
    except LlmError as e:
        log.warning("keyword extraction by the LLM failed: %s", e)
        return terms
    for field_name, kind, limit in (("topics", "topic", 5), ("keywords", "keyword", 10)):
        for item in clean_terms(answer.get(field_name), limit):
            if usable(item, kind, paper, body):
                terms.append(Term(kind, item, "llm", 1.0))
    return terms


def _limit(terms: list[Term], kind: str, limit: int) -> list[Term]:
    """Keep at most `limit` terms of one field."""
    kept = [t for t in terms if t.kind == kind][:limit]
    return [t for t in terms if t.kind != kind] + kept


# Captured spans that are not a source: sentence fragments, numbers and words that describe a sample's size.
NOT_A_SOURCE = re.compile(r"\b(we|our|section|describe|describes|figure|table|large|small|training|people|www|"
                          r"platform|relevant|fixed|budget)\b", re.I)


def _phrase(raw: str) -> str | None:
    """The noun phrase at the start of a captured span, without articles."""
    head = PHRASE_END.split(raw.strip(), maxsplit=1)[0]
    head = re.sub(r"^(the|a|an|our|this|these|their)\s+", "", head.strip(), flags=re.I)
    term = re.sub(r"\s+", " ", head).strip(" .'\"").lower()
    if not term or len(term.split()) > 6 or term in GENERIC or NOT_A_SOURCE.search(term) or re.fullmatch(r"[\d\s.,]+", term):
        return None
    return term


def method_rules(body: str) -> list[str]:
    """Method names from the list that the text mentions at least twice."""
    found = []
    for name in METHOD_NAMES:
        if len(re.findall(rf"\b{re.escape(name)}\b", body, re.I)) >= 2:
            found.append(name.lower())
    # drop a name when a longer one containing it was found ("experiment" inside "controlled experiment")
    return [n for n in found if not any(n != m and n in m for m in found)]


def dataset_rules(body: str) -> list[str]:
    """Data sources named in a sentence ("data collected from Twitter", "the German election dataset")."""
    found: list[str] = []
    for pattern in DATA_PATTERNS:
        for m in pattern.finditer(body):
            term = _phrase(m.group(1))
            if term and term not in found:
                found.append(term)
    for platform in PLATFORMS:
        if re.search(rf"\b{platform}\b", body) and re.search(
                rf"{DATA_CONTEXT.pattern}[^.\n]{{0,40}}\b{platform}\b|\b{platform}\b[^.\n]{{0,20}}{DATA_CONTEXT.pattern}",
                body, re.I):
            if platform.lower() not in found:
                found.append(platform.lower())
    return found


def rule_terms(doc: Document, have: list[Term]) -> list[Term]:
    """Fill fields the model left empty. Rules never replace a model answer."""
    _, body = _paper_text(doc)
    present = {t.kind for t in have}
    out: list[Term] = []
    if "method" not in present:
        out += [Term("method", m, "rule", 1.0) for m in method_rules(body)[:3]]
    if "dataset" not in present:
        out += [Term("dataset", d, "rule", 1.0) for d in dataset_rules(body)[:3]]
    return out


def dedupe(terms: list[Term]) -> list[Term]:
    """One field per term: the first kind in FIELD_PRIORITY that has it keeps it."""
    rank = {k: i for i, k in enumerate(FIELD_PRIORITY)}
    kept: list[Term] = []
    seen: set[str] = set()
    for t in sorted(terms, key=lambda t: rank.get(t.kind, len(rank))):
        if t.term in seen:
            continue
        seen.add(t.term)
        kept.append(t)
    return kept


MAX_ELEMENTS = 15          # figures and tables summarized per paper (each call takes ~10–20 s)
ELEMENT_TOKENS = 700       # input budget per figure or table
CITING_SENTENCES = 3       # sentences from the text that mention the figure or table


def _citations(doc: Document, name: str) -> list[str]:
    """Sentences of the body text that refer to a figure or table, e.g. "Figure 3" -> "Fig. 3", "Figs. 3"."""
    m = re.search(r"(\d+[a-z]?)$", name.strip())
    if m is None:
        return []
    label = re.escape(m.group(1))
    pattern = re.compile(rf"\b(?:fig(?:ure)?s?\.?|tab(?:le)?s?\.?)\s*{label}\b", re.I)
    found: list[str] = []
    for section in doc.sections:
        for p in section.paragraphs:
            if p.kind == "text":
                found += [s for s in sentences.split(p.text) if pattern.search(s)]
    return found[:CITING_SENTENCES]


def element_summaries(doc: Document, llm: LlmClient) -> int:
    """Write summaries for figures and tables: from their rows when they have them, otherwise from the
    text that cites them. Returns the number of summaries written."""
    elements = [p for s in doc.sections for p in s.paragraphs if p.kind in ("figure", "table")]
    written = 0
    for p in elements[:MAX_ELEMENTS]:
        name, _, caption = p.text.partition(": ")
        if p.kind == "table" and p.rows:
            source = f"{p.text}\n{p.rows}"
            question = "Summarize the table above in 2 sentences:"
        else:
            cited = _citations(doc, name)
            if not cited:
                p.summary = None
                continue
            source = f"{caption}\n" + " ".join(cited)
            question = f"Summarize what {name} shows, based on the text above, in 2 sentences:"
        try:
            text = llm.truncate(source, budget(llm, "element"))
            answer = llm.chat(f"{text}\n\n{question}", n_predict=120, temperature=0.2, feature="figures")
        except LlmError as e:
            log.warning("summary of %s failed: %s", name, e)
            continue
        p.summary = accept(answer, 6, 50)
        written += p.summary is not None
    return written


def extract(doc: Document, llm: LlmClient | None, *, typed: bool = False,
            tagger: Callable[[str], list[str]] | None = None) -> list[Term]:
    """All search terms of a paper. `typed` picks the one-call extractor over the legacy questions; `tagger` adds
    the keyphrases of the keyword tagger. The LLM's terms come first, so a term both found keep the LLM's weight."""
    text = " ".join([*(p.text for p in doc.abstract), *(p.text for s in doc.sections for p in s.paragraphs)])
    terms: list[Term] = []
    if llm is not None:
        terms = typed_terms(doc, llm) if typed else llm_terms(doc, llm)
    terms += rule_terms(doc, terms)
    if tagger is not None:
        terms += tagger_terms(doc, tagger)
    terms = dedupe(terms)
    known = {t.term for t in terms}
    for word in frequency_terms(text):
        if word not in known:
            terms.append(Term("keyword", word, "frequency", FREQUENCY_WEIGHT))
    return terms
