"""Sentence splitting that knows scientific abbreviations ("Fig. 3", "et al.", "e.g.")."""
from __future__ import annotations

import re

ABBREVIATIONS = {
    "e.g", "i.e", "al", "fig", "figs", "eq", "eqs", "tab", "sect", "sec", "no", "nos", "vs", "cf",
    "approx", "resp", "dr", "prof", "mr", "mrs", "ms", "st", "vol", "pp", "p", "ref", "refs", "ch",
    "ca", "est", "inc", "ltd", "jr", "sr", "dept", "univ", "w.r.t", "viz", "suppl", "chap",
}
BOUNDARY = re.compile(r"""[.!?]+["'”’)\]]*\s+(?=["'“‘(\[]?[A-Z0-9])""")
MAX_CHARS = 350


def _word_before(text: str, end: int) -> str:
    m = re.search(r"(\S+)$", text[:end])
    word = m.group(1) if m else ""
    return word.rstrip(".!?\"'”’)]").lstrip("(\"'“‘[").lower()


def split(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    out: list[str] = []
    start = 0
    for m in BOUNDARY.finditer(text):
        punct_pos = m.start()
        word = _word_before(text, punct_pos + 1)
        if text[punct_pos] == "." and (word in ABBREVIATIONS or re.fullmatch(r"[a-z]", word)):
            continue  # "Fig. 2", "J. Smith"
        out.append(text[start:m.end()].strip())
        start = m.end()
    if start < len(text):
        out.append(text[start:].strip())
    return [piece for s in out for piece in _split_long(s)]


def _split_long(sentence: str) -> list[str]:
    """Break very long sentences at semicolons or commas so timings stay fine-grained."""
    if len(sentence) <= MAX_CHARS:
        return [sentence]
    for sep in ("; ", ", "):
        mid = len(sentence) // 2
        cut = sentence.rfind(sep, 0, mid + MAX_CHARS // 3)
        if cut > 60:
            head, tail = sentence[: cut + 1].strip(), sentence[cut + len(sep):].strip()
            return _split_long(head) + _split_long(tail)
    return [sentence]
