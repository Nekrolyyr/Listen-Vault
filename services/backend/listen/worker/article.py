"""Web article → Document with trafilatura (which has readability and jusText fallbacks built in)."""
from __future__ import annotations

import re

import trafilatura
from lxml import etree

from .docmodel import Document, Paragraph, Section

PART_WORDS = 1500  # split heading-less articles into parts of about this many words


class ArticleError(Exception):
    pass


WEB_REFS = re.compile(r"\[(?:\d+|[a-z]|citation needed|note \d+|clarification needed)\]", re.I)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", WEB_REFS.sub("", text)).strip()


def parse_html(html: str, url: str | None = None) -> Document:
    xml = trafilatura.extract(
        html,
        url=url,
        output_format="xml",
        include_comments=False,
        include_tables=False,
        include_images=False,
        include_links=False,
        with_metadata=True,
    )  # default mode: favor_precision drops headings on pages like Wikipedia
    if not xml:
        raise ArticleError("No readable article text found on this page")
    root = etree.fromstring(xml.encode())
    doc = Document(source="trafilatura")
    doc.title = _clean(root.get("title") or "")
    # "Attention - Wikipedia" -> "Attention": drop a trailing site name (sitename or host name)
    host = (root.get("hostname") or "").removeprefix("www.").split(".")[0]
    for site in filter(None, (_clean(root.get("sitename") or ""), host)):
        doc.title = re.sub(rf"\s+[-–—|·:]\s+{re.escape(site)}$", "", doc.title, flags=re.I) or doc.title
    if author := root.get("author"):
        doc.authors = [a.strip() for a in re.split(r";|\band\b|,(?=\s*[A-Z])", author) if a.strip()]
    doc.published = root.get("date") or None

    main = root.find("main")
    if main is None:
        main = root
    section = Section(title=None)
    doc.sections.append(section)
    for el in main:
        tag = el.tag
        if tag == "head":
            title = _clean("".join(el.itertext()))
            if not title or title == doc.title:
                continue  # the article's own <h1> repeats the title
            section = Section(title=title, level=2 if el.get("rend") in ("h3", "h4") else 1)
            doc.sections.append(section)
        elif tag in ("p", "quote"):
            if text := _clean("".join(el.itertext())):
                section.paragraphs.append(Paragraph(text=text))
        elif tag == "list":
            for item in el.iter("item"):
                if text := _clean("".join(item.itertext())):
                    section.paragraphs.append(Paragraph(text=text if text[-1] in ".!?" else text.rstrip(",;:") + "."))
    doc.sections = [s for s in doc.sections if s.paragraphs]
    if doc.title and doc.sections and doc.sections[0].paragraphs[0].text == doc.title:
        doc.sections[0].paragraphs.pop(0)

    if len(doc.sections) == 1 and doc.sections[0].title is None:
        doc.sections = _split_parts(doc.sections[0].paragraphs)
    return doc


def _split_parts(paragraphs: list[Paragraph]) -> list[Section]:
    parts: list[Section] = []
    current = Section(title=None)
    words = 0
    for p in paragraphs:
        n = len(p.text.split())
        if current.paragraphs and words + n > PART_WORDS:
            parts.append(current)
            current, words = Section(title=None), 0
        current.paragraphs.append(p)
        words += n
    if current.paragraphs:
        parts.append(current)
    return parts
