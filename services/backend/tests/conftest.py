"""Test fixtures: a throwaway PostgreSQL 16 (pgserver), migrated once, truncated per test."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import psycopg
import pytest

from listen import config, db

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def database_url():
    if url := os.environ.get("TEST_DATABASE_URL"):
        yield url
        return
    pgserver = pytest.importorskip("pgserver")
    tmp = tempfile.mkdtemp(prefix="listen-pg-")
    server = pgserver.get_server(tmp, cleanup_mode="stop")
    url = server.get_uri()
    db.migrate(url)
    yield url
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def clean_db(database_url):
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE documents, chapters, sentences, jobs, playback, settings, zotero_links, "
                     "document_keywords RESTART IDENTITY CASCADE")
        conn.execute("UPDATE zotero_state SET last_version = 0, last_synced_at = NULL, last_error = NULL")
    return database_url


@pytest.fixture
def pool(clean_db):
    p = db.make_pool(clean_db, max_size=4)
    p.wait()
    yield p
    p.close()


@pytest.fixture
def settings(clean_db, tmp_path) -> config.Settings:
    return config.Settings(
        database_url=clean_db,
        data_dir=tmp_path / "data",
        listen_user="doctor",
        proxy_secret="test-proxy-secret",
        max_upload_mb=2,
        static_dir=None,
        models_dir=tmp_path / "models",
        llm_url="http://llm.invalid:8080",
        grobid_url="http://grobid.invalid:8070",
        lexicon_path=Path(__file__).parents[3] / "config/normalizer/lexicon.yaml",
    )


def make_pdf(path: Path, pages: list[list[tuple[int, str]]]) -> Path:
    """Render a text PDF with Ghostscript. pages: [[(font_size, line), ...], ...]."""
    gs = shutil.which("gs")
    if gs is None:
        pytest.skip("ghostscript not installed")
    ps = ["%!PS"]
    for lines in pages:
        y = 760
        for size, line in lines:
            esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ps.append(f"/Helvetica findfont {size} scalefont setfont 60 {y} moveto ({esc}) show")
            y -= size + 8
        ps.append("showpage")
    src = path.with_suffix(".ps")
    src.write_text("\n".join(ps))
    subprocess.run([gs, "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite", f"-sOutputFile={path}", str(src)],
                   check=True, capture_output=True)
    return path
