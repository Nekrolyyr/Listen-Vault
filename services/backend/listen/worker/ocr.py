"""Detect PDFs without a usable text layer and OCR them with OCRmyPDF + Tesseract (English)."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from pypdf import PdfReader

log = logging.getLogger(__name__)

MIN_CHARS_PER_PAGE = 200   # born-digital papers have thousands; scans have ~0
SAMPLE_PAGES = 5


class OcrError(Exception):
    pass


def needs_ocr(pdf: Path) -> bool:
    reader = PdfReader(str(pdf))
    pages = reader.pages[:SAMPLE_PAGES]
    if not pages:
        raise OcrError("The PDF has no pages")
    chars = 0
    for page in pages:
        try:
            chars += len((page.extract_text() or "").strip())
        except Exception:  # noqa: BLE001 - broken text layer counts as no text layer
            pass
    return chars / len(pages) < MIN_CHARS_PER_PAGE


def page_count(pdf: Path) -> int:
    return len(PdfReader(str(pdf)).pages)


def prepare_viewer_pdf(source: Path, target: Path, jobs: int) -> bool:
    """Write the reader's PDF to `target`. Returns True when OCR was applied."""
    if not needs_ocr(source):
        shutil.copyfile(source, target)
        return False
    if shutil.which("ocrmypdf") is None:
        raise OcrError("This PDF is a scan and needs OCR, but ocrmypdf is not installed")
    tmp = target.with_suffix(".ocr.tmp.pdf")
    cmd = [
        "ocrmypdf", "--skip-text", "--language", "eng", "--output-type", "pdf",
        "--rotate-pages", "--deskew", "--jobs", str(jobs), "--quiet", str(source), str(tmp),
    ]
    log.info("running OCR: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise OcrError(f"OCR failed: {proc.stderr.strip()[-500:]}")
    tmp.replace(target)
    return True
