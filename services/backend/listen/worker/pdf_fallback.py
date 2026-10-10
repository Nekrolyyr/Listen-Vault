"""Layout-based PDF structuring with pdfminer.six, used when GROBID is unavailable or fails.

Works line by line: headings are found by font size and numbering, running headers/footers by
repetition across pages, paragraphs are consecutive body lines of one text box; reading stops at the
reference list.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTChar, LTTextContainer, LTTextLine

from .docmodel import Document, Paragraph, Section

NUMBERED_HEADING = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[IVX]{1,5}\.)\s+[A-Z][^.!?]{1,90}$")
END_MATTER = re.compile(r"^(?:\d+\.?\s*)?(references|bibliography|literature cited|acknowledge?ments?)\b", re.I)
ABSTRACT = re.compile(r"^abstract\b[\s.:—–-]*", re.I)


@dataclass
class Line:
    page: int
    box: int
    text: str
    size: float


def _lines(pdf: Path) -> tuple[list[Line], int]:
    lines: list[Line] = []
    pages = 0
    box_id = 0
    for page_no, layout in enumerate(extract_pages(str(pdf), laparams=LAParams()), start=1):
        pages = page_no
        for el in layout:
            if not isinstance(el, LTTextContainer):
                continue
            box_id += 1
            for line in el:
                if not isinstance(line, LTTextLine):
                    continue
                text = re.sub(r"\s+", " ", line.get_text()).strip()
                sizes = [c.size for c in line if isinstance(c, LTChar)]
                if text and sizes:
                    lines.append(Line(page_no, box_id, text, round(statistics.median(sizes), 1)))
    return lines, pages


def _join(lines: list[str]) -> str:
    text = ""
    for line in lines:
        if text.endswith("-") and line[:1].islower():
            text = text[:-1] + line          # hyphenated line break
        else:
            text = f"{text} {line}".strip()
    return text


def parse_pdf(pdf: Path) -> Document:
    lines, pages = _lines(pdf)
    doc = Document(source="pdfminer")
    if not lines:
        return doc

    weights = Counter()
    for ln in lines:
        weights[ln.size] += len(ln.text)
    body_size = weights.most_common(1)[0][0]

    # Running headers/footers: the same text (ignoring digits) on at least half of the pages.
    repeated: set[str] = set()
    if pages >= 3:
        per_text = Counter(key for _, key in {(ln.page, re.sub(r"\d+", "#", ln.text)) for ln in lines})
        repeated = {k for k, n in per_text.items() if n >= pages / 2 and len(k) < 120}

    first_page = [ln for ln in lines if ln.page == 1 and len(ln.text) > 3]
    title_size = max(ln.size for ln in first_page) if first_page else 0
    if first_page and title_size > body_size:
        doc.title = " ".join(ln.text for ln in first_page if ln.size == title_size)[:300]

    section: Section | None = None
    target: list[Paragraph] | None = None    # where body text goes: abstract or current section
    buf: list[str] = []
    buf_page, buf_box = 0, -1

    def flush():
        nonlocal buf
        if buf and target is not None:
            target.append(Paragraph(text=_join(buf), page=buf_page))
        buf = []

    for ln in lines:
        text = ln.text
        if (re.fullmatch(r"[\d\s]+", text) or re.sub(r"\d+", "#", text) in repeated
                or (ln.page == 1 and ln.size == title_size and doc.title)):
            continue
        words = len(text.split())
        is_heading = words <= 15 and not text.endswith(".") and (
            ln.size >= body_size + 1.0 or (NUMBERED_HEADING.match(text) and ln.size >= body_size - 0.1)
        )
        if ABSTRACT.match(text) and not doc.sections and (words <= 2 or ln.size >= body_size):
            flush()
            target = doc.abstract
            rest = ABSTRACT.sub("", text, count=1)
            if rest:
                buf, buf_page, buf_box = [rest], ln.page, ln.box
            continue
        if is_heading:
            flush()
            if END_MATTER.match(text):
                break
            section = Section(title=text)
            doc.sections.append(section)
            target = section.paragraphs
            continue
        if target is None:
            if ln.page == 1 and ln.size > body_size:
                continue  # author block, affiliations
            section = Section(title=None)
            doc.sections.append(section)
            target = section.paragraphs
        if ln.box != buf_box:
            flush()
            buf_page, buf_box = ln.page, ln.box
        buf.append(text)
    flush()

    doc.sections = [s for s in doc.sections if s.paragraphs]
    return doc
