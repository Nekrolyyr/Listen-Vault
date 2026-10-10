"""On-disk layout for one document: DATA_DIR/docs/<id>/...

  source.pdf | source.html   what was uploaded or fetched
  viewer.pdf                 PDF shown in the reader (OCR'd copy for scans)
  structure.json             normalized document model
  chapters.json              chapter plan with LLM intros and overview
  audio/<idx>.m4a            one audio file per chapter (idx 0 = overview)
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
    chapters = property(lambda self: self.root / "chapters.json")
    audio_dir = property(lambda self: self.root / "audio")

    def audio(self, idx: int) -> Path:
        return self.audio_dir / f"{int(idx)}.m4a"

    def ensure(self) -> "DocPaths":
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        return self

    def remove(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def reset_from(self, stage: str) -> None:
        """Delete artifacts of `stage` and everything after it, so those stages run again."""
        order = ["ocr", "structure", "chapters", "synthesis"]
        if stage not in order:
            return
        later = order[order.index(stage):]
        if "ocr" in later:
            self.viewer_pdf.unlink(missing_ok=True)
        if "structure" in later:
            self.structure.unlink(missing_ok=True)
        if "chapters" in later:
            self.chapters.unlink(missing_ok=True)
        shutil.rmtree(self.audio_dir, ignore_errors=True)
