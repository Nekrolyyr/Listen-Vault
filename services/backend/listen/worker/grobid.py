"""GROBID client and TEI → Document parser.

The parser keeps what is worth listening to: title, authors, date, abstract, body sections and
paragraphs, figure/table captions and equations as placeholders. It drops bibliographic references
inside the text, footnotes and the back matter (references, acknowledgements, annexes).
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import httpx
from lxml import etree

from .docmodel import Document, Paragraph, Section

TEI = "{http://www.tei-c.org/ns/1.0}"
NS = {"t": "http://www.tei-c.org/ns/1.0"}


class GrobidError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def wait_alive(base_url: str, timeout_s: float = 180) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/api/isalive", timeout=5).text.strip() == "true":
                return True
        except httpx.HTTPError:
            pass
        time.sleep(3)
    return False


def process_fulltext(base_url: str, pdf: Path, timeout_s: float = 600, client: httpx.Client | None = None) -> bytes:
    data = {
        "consolidateHeader": "0",
        "consolidateCitations": "0",
        "segmentSentences": "1",
        "teiCoordinates": ["head", "p", "s", "figure", "formula"],  # repeated form field
    }
    try:
        with pdf.open("rb") as f:
            resp = (client or httpx).post(
                f"{base_url}/api/processFulltextDocument",
                files={"input": (pdf.name, f, "application/pdf")},
                data=data,
                timeout=timeout_s,
            )
    except httpx.HTTPError as e:
        raise GrobidError(f"GROBID unreachable: {e}", retryable=True) from e
    if resp.status_code == 503:
        raise GrobidError("GROBID is busy", retryable=True)
    if resp.status_code != 200:
        raise GrobidError(f"GROBID returned HTTP {resp.status_code}")
    return resp.content


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _page(el) -> int | None:
    """First page number from a TEI coords attribute on el or its first coordinated descendant."""
    for node in (el, *el.iter()):
        coords = node.get("coords")
        if coords:
            try:
                return int(coords.split(";")[0].split(",")[0])
            except ValueError:
                return None
    return None


def _boxes(el) -> list[list[float]]:
    """Line boxes "page,x,y,w,h;page,x,y,w,h;..." from el's coords (or its first coordinated descendant)."""
    for node in (el, *el.iter()):
        coords = node.get("coords")
        if coords:
            boxes = []
            for part in coords.split(";"):
                try:
                    vals = [float(v) for v in part.split(",")]
                except ValueError:
                    continue
                if len(vals) == 5:
                    boxes.append(vals)
            return boxes
    return []


def _paragraph_text(p) -> str:
    """Paragraph text without bibliography references like [12] or (Smith et al., 2020)."""
    clone = etree.fromstring(etree.tostring(p))
    for ref in clone.iter(f"{TEI}ref"):
        if ref.get("type") == "bibr":
            parent = ref.getparent()
            tail = ref.tail or ""
            prev = ref.getprevious()
            if prev is not None:
                prev.tail = (prev.tail or "") + tail
            else:
                parent.text = (parent.text or "") + tail
            parent.remove(ref)
    for note in clone.iter(f"{TEI}note"):
        note.getparent().remove(note)
    for s in clone.iter(f"{TEI}s"):  # GROBID writes <s>..</s><s>..</s> without whitespace between
        s.tail = " " + (s.tail or "")
    text = _text(clone)
    # Tidy what removing references leaves behind: "shown ( , ) that" / "results [ ]."
    text = re.sub(r"[(\[]\s*(?:[,;]\s*)*[)\]]", "", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _figure(fig) -> Paragraph | None:
    kind = "table" if fig.get("type") == "table" else "figure"
    head = fig.find("t:head", NS)
    label = fig.find("t:label", NS)
    desc = fig.find("t:figDesc", NS)
    name = _text(head) if head is not None and _text(head) else (
        f"{'Table' if kind == 'table' else 'Figure'} {_text(label)}" if label is not None else kind.title()
    )
    caption = _text(desc) if desc is not None else ""
    if not caption:
        return None
    name = re.sub(r"\s+([.:])", r"\1", name).rstrip(".: ")                      # "Fig. 1 ." -> "Fig. 1"
    caption = re.sub(r"^(?:fig(?:ure)?s?\.?|table|tab\.)\s*[\dA-Z]+[.:]?\s*", "", caption, flags=re.I)
    return Paragraph(text=f"{name}: {caption}", page=_page(fig), kind=kind)


ORG_WORDS = re.compile(
    r"\b(?:university|universit[äéà]t?|institute|institut|laborator(?:y|ies)|lab|department|dept|school|college|"
    r"research|inc|ltd|corp|corporation|company|google|brain|deepmind|microsoft|meta|facebook|openai|amazon|"
    r"ibm|nvidia|apple|centre|center|hospital|foundation)\b",
    re.I,
)


def _clean_title(title: str) -> str:
    """GROBID sometimes prefixes the title with front-matter text (licence notices, venue lines)."""
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) > 150 and ". " in title:
        last = title.rsplit(". ", 1)[1].strip()
        if 1 <= len(last.split()) <= 25:
            return last
    return title


def parse_tei(xml: bytes) -> Document:
    root = etree.fromstring(xml)
    doc = Document(source="grobid")

    title = root.find(".//t:teiHeader/t:fileDesc/t:titleStmt/t:title", NS)
    doc.title = _clean_title(_text(title)) if title is not None else ""

    for author in root.findall(".//t:teiHeader//t:sourceDesc/t:biblStruct/t:analytic/t:author/t:persName", NS):
        names = [_text(n) for n in author if n.tag in (f"{TEI}forename", f"{TEI}surname")]
        name = " ".join(n for n in names if n)
        if name and not ORG_WORDS.search(name):  # affiliations occasionally land in persName
            doc.authors.append(name)

    date = root.find(".//t:teiHeader//t:publicationStmt/t:date", NS)
    if date is None:
        date = root.find(".//t:teiHeader//t:sourceDesc//t:imprint/t:date", NS)
    if date is not None:
        doc.published = date.get("when") or _text(date) or None

    for p in root.findall(".//t:teiHeader/t:profileDesc/t:abstract//t:p", NS):
        if text := _paragraph_text(p):
            doc.abstract.append(Paragraph(text=text, page=_page(p), boxes=_boxes(p)))

    body = root.find(".//t:text/t:body", NS)
    if body is None:
        return doc
    pending_title: str | None = None  # title of an empty parent heading, e.g. "2 Methods"
    for div in body.findall("t:div", NS):
        head = div.find("t:head", NS)
        title_text = (_text(head) if head is not None else "") or None
        number = head.get("n") if head is not None else None
        level = number.rstrip(".").count(".") + 1 if number else 1
        if pending_title:
            title_text = f"{pending_title}: {title_text}" if title_text else pending_title
            pending_title = None
        section = Section(title=title_text, level=level)
        for child in div:
            tag = etree.QName(child).localname
            if tag == "p":
                if text := _paragraph_text(child):
                    section.paragraphs.append(Paragraph(text=text, page=_page(child), boxes=_boxes(child)))
            elif tag == "formula":
                section.paragraphs.append(Paragraph(text="", page=_page(child), kind="equation"))
            elif tag == "figure":
                if para := _figure(child):
                    section.paragraphs.append(para)
        if section.paragraphs:
            doc.sections.append(section)
        else:
            pending_title = section.title

    # GROBID usually places figures and tables at body level, outside any div. Read each caption
    # right after the last paragraph that starts on or before the figure's page.
    for fig in body.findall("t:figure", NS):
        para = _figure(fig)
        if para is None or not doc.sections:
            continue
        if para.page is None:
            doc.sections[-1].paragraphs.append(para)
            continue
        target, pos = doc.sections[0], 0
        for section in doc.sections:
            for i, p in enumerate(section.paragraphs):
                if p.page is not None and para.page is not None and p.page <= para.page:
                    target, pos = section, i + 1
        target.paragraphs.insert(pos, para)
    return doc
