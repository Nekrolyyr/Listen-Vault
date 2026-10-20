"""Upgrading a library made by the single-user version: its papers, listened state and playback move to the
placeholder owner, and the placeholder can be claimed by an administrator."""
import io
from importlib import resources

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from listen import accounts, cli, config, db


def _migrations_before_009(url: str) -> None:
    files = sorted((f for f in resources.files("listen.migrations").iterdir() if f.name.endswith(".sql")),
                   key=lambda f: f.name)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                     "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        for f in files:
            if f.name < "009":
                conn.execute(f.read_text())
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.name.removesuffix(".sql"),))


def test_existing_library_moves_to_the_placeholder_owner(clean_db, tmp_path, monkeypatch):
    url = make_conninfo(clean_db, dbname="legacy_upgrade")
    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS legacy_upgrade WITH (FORCE)")
        conn.execute("CREATE DATABASE legacy_upgrade")
    _migrations_before_009(url)
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("INSERT INTO documents (source_type, title, status, listened, last_listened_at, created_at) "
                     "VALUES ('pdf', 'Old paper', 'ready', true, now(), '2025-01-01'), "
                     "('url', 'Old article', 'ready', false, NULL, '2025-02-01')")
        doc = conn.execute("SELECT id FROM documents WHERE title = 'Old paper'").fetchone()["id"]
        conn.execute("INSERT INTO playback (document_id, chapter_idx, position_s) VALUES (%s, 2, 30)", (doc,))
        conn.execute("INSERT INTO questions (document_id, question) VALUES (%s, 'What?')", (doc,))
    db.migrate(url)

    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        owner = conn.execute("SELECT * FROM users").fetchone()
        assert owner["email"] == accounts.LEGACY_EMAIL and owner["is_admin"] and owner["password_hash"] is None
        entries = conn.execute("SELECT d.title, le.listened, le.added_at FROM library_entries le "
                               "JOIN documents d ON d.id = le.document_id ORDER BY d.title").fetchall()
        assert [(e["title"], e["listened"]) for e in entries] == [("Old article", False), ("Old paper", True)]
        assert conn.execute("SELECT count(*) AS n FROM playback WHERE user_id = %s", (owner["id"],)).fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM questions WHERE user_id = %s", (owner["id"],)).fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM documents WHERE content_sha256 IS NULL").fetchone()["n"] == 2

    # the operator claims the placeholder with a real address and password
    monkeypatch.setattr("sys.stdin", io.StringIO("correct horse battery\n"))
    settings = config.Settings(database_url=url, data_dir=tmp_path)
    cli.make_admin(settings, type("A", (), {"email": "owner@example.org", "password_stdin": True})())
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        claimed = conn.execute("SELECT email, is_admin FROM users").fetchall()
        assert claimed == [{"email": "owner@example.org", "is_admin": True}]
        assert conn.execute("SELECT count(*) AS n FROM library_entries").fetchone()["n"] == 2
    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute("DROP DATABASE legacy_upgrade WITH (FORCE)")
