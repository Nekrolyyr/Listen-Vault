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
TERMS_DIR = Path(__file__).parents[3] / "config/terms"


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
        conn.execute("TRUNCATE documents, chapters, narrations, jobs, playback, settings, user_settings, "
                     "document_keywords, users, admin_audit, auth_attempts, terms_versions RESTART IDENTITY CASCADE")
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
        proxy_secret="test-proxy-secret",
        public_url="http://localhost:8080",
        terms_dir=TERMS_DIR,
        max_upload_mb=2,
        static_dir=None,
        models_dir=tmp_path / "models",
        llm_url="http://llm.invalid:8080",
        grobid_url="http://grobid.invalid:8070",
        lexicon_path=Path(__file__).parents[3] / "config/normalizer/lexicon.yaml",
    )


def make_user(database_url: str, email: str = "doctor@example.org", admin: bool = False,
              password: str = "correct horse battery", verified: bool = True) -> tuple[int, str]:
    """A signed-in account: returns its id and a session token (the cookie value)."""
    from listen import accounts
    with psycopg.connect(database_url, autocommit=False) as conn:
        from psycopg.rows import dict_row
        conn.row_factory = dict_row
        user = accounts.create_user(conn, email, password, verified=verified)
        conn.execute("UPDATE users SET is_admin = %s WHERE id = %s", (admin, user["id"]))
        current = accounts.load_terms(conn, TERMS_DIR)
        if current:
            accounts.accept_terms(conn, user["id"], current)
        token = accounts.new_session(conn, user["id"])
        conn.commit()
    return user["id"], token


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
