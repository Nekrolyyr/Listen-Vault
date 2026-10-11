"""Where a sentence sits on the PDF.

GROBID gives one coordinate box per text line of a paragraph. A sentence's share of the paragraph's
characters is spread over those lines in proportion to their width, which gives one rectangle per line
the sentence touches. This is an estimate, precise enough to scroll to the right line and highlight it.
"""
from __future__ import annotations

import re

# [page, x, y, width, height] in PDF points, origin top-left (GROBID's coordinate system)
Rect = list[float]


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def sentence_rects(lines: list[Rect], text: str, start: int, end: int) -> list[Rect]:
    """Rectangles covering text[start:end] of a paragraph whose line boxes are `lines`.

    `text` must be the whitespace-normalized paragraph text the offsets refer to.
    """
    total = sum(line[3] for line in lines)
    if not lines or total <= 0 or not text or end <= start:
        return []
    a0 = start / len(text) * total
    a1 = end / len(text) * total
    rects: list[Rect] = []
    cum = 0.0
    for page, x, y, w, h in lines:
        lo, hi = max(a0, cum), min(a1, cum + w)
        if hi > lo:
            rects.append([int(page), round(x + lo - cum, 2), y, round(hi - lo, 2), h])
        cum += w
    return rects


def paragraph_rects(lines: list[Rect], text: str) -> list[Rect]:
    flat_text = flat(text)
    return sentence_rects(lines, flat_text, 0, len(flat_text))
