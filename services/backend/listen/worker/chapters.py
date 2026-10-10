"""Turn a Document into chapters, then let BLOOMZ-7b1 add intros, missing titles and an overview.

Structure comes from the document itself (GROBID or HTML headings), never from the LLM: BLOOMZ has
a 2,048-token context and prefers short answers, so it is only asked for short pieces of text.
Every LLM answer is checked; a failed check falls back to an extractive summary.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import sentences
from .docmodel import Document, Paragraph, Section
from .llm import LlmClient, LlmError

log = logging.getLogger(__name__)

MIN_WORDS = 150      # shorter sections are merged into the previous chapter
MAX_WORDS = 2500     # longer sections are split into parts
CHUNK_TOKENS = 1200  # input budget per LLM call (context is 2,048 incl. prompt and answer)

SKIP_TITLES = re.compile(
    r"^(?:[\dIVX]+(?:\.\d+)*\.?\s*)?(?:acknowledge?ments?|funding|author contributions?|competing interests?|"
    r"conflicts? of interest|declarations?|data availability|code availability|ethics|supplementary|"
    r"references|bibliography|abbreviations|appendix|open access|see also|external links|further reading|notes)\b",
    re.I,
)


@dataclass
class ChapterPlan:
    kind: str                                  # overview | chapter
    title: str
    paragraphs: list[Paragraph] = field(default_factory=list)
    intro: str | None = None
    start_page: int | None = None
    intro_source: str = "none"                 # llm | extractive | none

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.paragraphs if p.kind != "equation")

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def save(path: Path, plans: list[ChapterPlan]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(p) for p in plans], ensure_ascii=False, indent=1))
    tmp.replace(path)


def load(path: Path) -> list[ChapterPlan]:
    out = []
    for d in json.loads(path.read_text()):
        d["paragraphs"] = [Paragraph(**p) for p in d["paragraphs"]]
        out.append(ChapterPlan(**d))
    return out


def _clean_title(title: str) -> str:
    title = re.sub(r"^(?:[\dIVX]+(?:\.\d+)*\.?)\s+", "", title.strip())  # "2.1 Methods" -> "Methods"
    return title[:1].upper() + title[1:] if title else title


CONCLUSION = re.compile(r"^(?:[\dIVX]+(?:\.\d+)*\.?\s*)?(?:conclusions?|concluding remarks|summary and conclusions?)\b", re.I)


def _main_text(sections):
    """Drop appendix material: sections starting two or more pages after the conclusion section
    (the reference list sits in between)."""
    out, end_page = [], None
    for s in sections:
        page = next((p.page for p in s.paragraphs if p.page is not None), None)
        if end_page is not None and page is not None and page >= end_page + 2:
            break
        if s.title and CONCLUSION.match(s.title):
            # GROBID can attach appendix text/figures to the conclusion: cut at the first jump of 2+ pages
            kept, last = [], None
            for p in s.paragraphs:
                if p.page is not None:
                    if last is not None and p.page >= last + 2:
                        break
                    last = p.page
                kept.append(p)
            end_page = last
            s = Section(title=s.title, level=s.level, paragraphs=kept)
        out.append(s)
    return out


def plan(doc: Document) -> list[ChapterPlan]:
    """Chapter structure without any LLM call. Index 0 is the overview."""
    plans = [ChapterPlan(kind="overview", title="Overview", start_page=1)]
    if doc.abstract:
        plans.append(ChapterPlan(kind="chapter", title="Abstract", paragraphs=list(doc.abstract),
                                 start_page=doc.abstract[0].page))
    for section in _main_text(doc.sections):
        if section.title and SKIP_TITLES.match(section.title):
            continue
        title = _clean_title(section.title) if section.title else ""
        prev = plans[-1] if len(plans) > 1 else None
        merge = (
            prev is not None
            and prev.title != "Abstract"
            and (section.word_count < MIN_WORDS or (section.level > 1 and prev.word_count < MIN_WORDS))
            and prev.word_count + section.word_count <= MAX_WORDS
        )
        if merge:
            if title:
                prev.paragraphs.append(Paragraph(text=title, page=section.paragraphs[0].page, kind="heading"))
            prev.paragraphs.extend(section.paragraphs)
            continue
        plans.extend(_split_long(title, section.paragraphs))

    # A first "chapter" that is still tiny (e.g. a one-line untitled preamble) joins the next one.
    if len(plans) > 2 and plans[1].title != "Abstract" and plans[1].word_count < 40:
        small = plans.pop(1)
        plans[1].paragraphs = small.paragraphs + plans[1].paragraphs
    for p in plans[1:]:
        p.start_page = next((x.page for x in p.paragraphs if x.page is not None), None)
    return plans


def _split_paragraph(p: Paragraph) -> list[Paragraph]:
    """Break a paragraph longer than MAX_WORDS into sentence groups (layout extraction can merge pages)."""
    if len(p.text.split()) <= MAX_WORDS:
        return [p]
    out, current = [], []
    for s in sentences.split(p.text):
        if current and len(" ".join(current + [s]).split()) > MAX_WORDS // 2:
            out.append(Paragraph(" ".join(current), p.page, p.kind))
            current = []
        current.append(s)
    if current:
        out.append(Paragraph(" ".join(current), p.page, p.kind))
    return out


def _split_long(title: str, paragraphs: list[Paragraph]) -> list[ChapterPlan]:
    parts: list[list[Paragraph]] = [[]]
    words = 0
    for p in (piece for para in paragraphs for piece in _split_paragraph(para)):
        n = len(p.text.split())
        if parts[-1] and words + n > MAX_WORDS:
            parts.append([])
            words = 0
        parts[-1].append(p)
        words += n
    if len(parts) == 1:
        return [ChapterPlan(kind="chapter", title=title, paragraphs=parts[0])]
    return [
        ChapterPlan(kind="chapter", title=f"{title}, part {i}" if title else "", paragraphs=chunk)
        for i, chunk in enumerate(parts, start=1)
    ]


# ---------------------------------------------------------------- LLM enrichment

def _extractive(text: str, n: int) -> str:
    return " ".join(sentences.split(text)[:n])


def _words(s: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", s.lower())


IMPERATIVES = {
    "define", "explain", "review", "look", "understand", "learn", "describe", "discuss", "identify",
    "list", "compare", "write", "read", "note", "consider", "explore", "examine", "find", "use", "see",
    "visualize", "visualise", "combine", "apply", "align", "calculate", "compute", "implement", "train",
    "build", "create", "show", "plot", "analyze", "analyse", "evaluate", "measure", "test", "add", "remove",
    "replace", "run", "try", "take", "make", "give", "get", "set", "check", "choose", "select", "follow",
    "start", "keep", "avoid", "remember", "think", "imagine", "let", "suppose", "assume", "recall",
}


def accept(answer: str, min_words: int, max_words: int) -> str | None:
    """Return a cleaned LLM answer, or None if it looks unusable."""
    answer = answer.strip().strip('"').strip()
    answer = re.sub(r"^(?:summary|answer|title|overview)\s*:\s*", "", answer, flags=re.I)
    answer = re.sub(r"\s?\[\s*\d+(?:\s*[-–,]\s*\d+)*\s*\]", "", answer)       # copied citation markers
    answer = re.sub(r"\s+([,.;:!?])", r"\1", re.sub(r"\s+", " ", answer))  # "keys K ." -> "keys K."
    if answer.endswith(":"):
        return None  # a list lead-in ("...the following techniques:") is not a summary
    words = _words(answer)
    if len(words) < min_words:
        return None
    if len(set(words)) < 0.5 * len(words) and len(words) > 8:
        return None  # repetition loop
    if re.search(r"summari[sz]e|the text above|write a short", answer, re.I):
        return None  # echoed the instruction
    firsts = [s.split()[0].lower() for s in sentences.split(answer) if s.split()]
    if firsts and sum(w in IMPERATIVES for w in firsts) * 2 >= len(firsts):
        return None  # study objectives ("Define X. Explain Y.") instead of a summary
    if len(words) > max_words:
        kept = sentences.split(answer)
        answer = ""
        for s in kept:
            if len(_words(answer + " " + s)) > max_words:
                break
            answer = f"{answer} {s}".strip()
        if not answer:
            return None
    if answer[-1] not in ".!?":
        answer += "."
    return answer[0].upper() + answer[1:]


class Enricher:
    def __init__(self, llm: LlmClient | None, progress: Callable[[int, int], bool] = lambda d, t: True):
        self.llm = llm
        self.progress = progress
        self.calls = 0
        self.failures = 0

    def _ask(self, prompt: str, n_predict: int, min_words: int, max_words: int) -> str | None:
        if self.llm is None:
            return None
        self.calls += 1
        try:
            for temperature in (0.2, 0.6):  # one retry with more variety
                if answer := accept(self.llm.complete(prompt, n_predict, temperature), min_words, max_words):
                    return answer
        except LlmError as e:
            log.warning("LLM call failed: %s", e)
        self.failures += 1
        return None

    def _chunks(self, text: str) -> list[str]:
        """Split text at sentence boundaries into pieces of at most CHUNK_TOKENS tokens."""
        assert self.llm is not None
        if self.llm.count(text) <= CHUNK_TOKENS:
            return [text]
        chunks, current = [], ""
        for s in sentences.split(text):
            candidate = f"{current} {s}".strip()
            if current and self.llm.count(candidate) > CHUNK_TOKENS:
                chunks.append(current)
                current = s
            else:
                current = candidate
        if current:
            chunks.append(self.llm.truncate(current, CHUNK_TOKENS))
        return chunks

    def summarize(self, text: str, n_sentences: int = 2) -> str | None:
        if self.llm is None:
            return None
        try:
            chunks = self._chunks(text)
        except LlmError:
            return None
        if len(chunks) > 1:  # map: summarize each part, then reduce the summaries
            partial = [self._ask(_summary_prompt(c, 2), 120, 6, 70) for c in chunks]
            text = " ".join(p for p in partial if p) or _extractive(text, 6)
        return self._ask(_summary_prompt(text, n_sentences), 140, 8, 25 * n_sentences + 10)

    def title(self, text: str) -> str | None:
        if self.llm is None:
            return None
        try:
            excerpt = self.llm.truncate(text, 600)
        except LlmError:
            return None
        answer = self._ask(f"{excerpt}\n\nWhat is a good short title for the text above?", 24, 1, 10)
        return answer.rstrip(".") if answer else None

    def run(self, doc: Document, plans: list[ChapterPlan], intros: bool = True) -> None:
        chapters = plans[1:]
        total = len(chapters) + 1
        for i, ch in enumerate(chapters, start=1):
            if not self.progress(i - 1, total):
                return
            if not ch.title:
                ch.title = self.title(ch.text) or f"Part {i}"
            if intros and ch.title != "Abstract" and ch.word_count >= MIN_WORDS:
                if summary := self.summarize(ch.text, 2):
                    ch.intro, ch.intro_source = summary, "llm"
        self.progress(len(chapters), total)

        overview = plans[0]
        source = " ".join(p.text for p in doc.abstract) or " ".join(
            c.intro or _extractive(c.text, 2) for c in chapters
        )
        if summary := self.summarize(source, 4):
            overview.intro, overview.intro_source = summary, "llm"
        else:
            overview.intro = _extractive(source, 4)
            overview.intro_source = "extractive" if overview.intro else "none"
        self.progress(total, total)


def _summary_prompt(text: str, n: int) -> str:
    return f"{text}\n\nSummarize the text above in {n} sentences:"
