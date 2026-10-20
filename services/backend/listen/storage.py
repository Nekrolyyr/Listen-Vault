"""On-disk layout for one document: DATA_DIR/docs/<id>/...

  source.pdf | source.html   what was uploaded or fetched
  viewer.pdf                 PDF shown in the reader (OCR'd copy for scans)
  structure.json             normalized document model
  enriched.json              marker: search terms were extracted (their rows are in document_keywords)
  chapters.json              chapter plan (overview = abstract, LLM-written chapter titles)
  narrations/<id>/<idx>.m4a  one audio file per chapter of one user's narration (idx 0 = overview); see narrations.py
  audio/<idx>.m4a            audio made before per-user narrations (its rows are narration rows of the legacy owner)
"""
from __future__ import annotations

import shutil
from pathlib import Path
from uuid import UUID


class DocPaths:
    def __init__(self, docs_dir: Path, document_id: UUID | str):
        self.root = docs_dir / str(UUID(str(document_id)))  # UUID() rejects path tricks

    source_pdf = property(lambda self: self.root / "source.pdf")
    source_html = property(lambda self: self.root / "source.html")
    viewer_pdf = property(lambda self: self.root / "viewer.pdf")
    structure = property(lambda self: self.root / "structure.json")
    enriched = property(lambda self: self.root / "enriched.json")
    chapters = property(lambda self: self.root / "chapters.json")

    def ensure(self) -> "DocPaths":
        self.root.mkdir(parents=True, exist_ok=True)
        return self

    def remove(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def reset_from(self, stage: str) -> None:
        """Delete artifacts of `stage` and everything after it, so those stages run again."""
        order = ["ocr", "structure", "chapters"]
        if stage not in order:
            return
        later = order[order.index(stage):]
        if "ocr" in later:
            self.viewer_pdf.unlink(missing_ok=True)
        if "structure" in later:
            self.structure.unlink(missing_ok=True)
        if "chapters" in later:
            self.chapters.unlink(missing_ok=True)
        shutil.rmtree(self.root / "narrations", ignore_errors=True)
