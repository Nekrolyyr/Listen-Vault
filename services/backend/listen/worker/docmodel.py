"""Normalized document model produced by ingestion and consumed by chaptering and TTS."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Paragraph:
    text: str
    page: int | None = None   # 1-based PDF page where the paragraph starts
    kind: str = "text"        # text | heading | figure | table | equation
    boxes: list[list[float]] = field(default_factory=list)  # [page, x, y, w, h] per text line (GROBID coords)


@dataclass
class Section:
    title: str | None
    paragraphs: list[Paragraph] = field(default_factory=list)
    level: int = 1

    @property
    def word_count(self) -> int:
        return sum(len(p.text.split()) for p in self.paragraphs)


@dataclass
class Document:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    published: str | None = None
    abstract: list[Paragraph] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    source: str = ""          # grobid | pdfminer | trafilatura

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Document":
        d = json.loads(path.read_text())
        return cls(
            title=d["title"],
            authors=d["authors"],
            published=d.get("published"),
            abstract=[Paragraph(**p) for p in d["abstract"]],
            sections=[
                Section(title=s["title"], level=s.get("level", 1), paragraphs=[Paragraph(**p) for p in s["paragraphs"]])
                for s in d["sections"]
            ],
            source=d.get("source", ""),
        )

    @property
    def word_count(self) -> int:
        return sum(s.word_count for s in self.sections) + sum(len(p.text.split()) for p in self.abstract)
