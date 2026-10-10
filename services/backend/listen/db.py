"""Postgres connection pool and schema migrations."""
from __future__ import annotations

import logging
import time
from importlib import resources

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

MIGRATION_LOCK = 7_314_001  # pg_advisory_lock key, so api and worker never migrate concurrently


def make_pool(url: str, max_size: int = 8) -> ConnectionPool:
    pool = ConnectionPool(url, min_size=1, max_size=max_size, kwargs={"row_factory": dict_row}, open=False)
    pool.open(wait=False)
    return pool


def wait_for_db(url: str, timeout_s: float = 60) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with psycopg.connect(url, connect_timeout=3):
                return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def migrate(url: str) -> list[str]:
    """Apply pending migrations in file-name order. Returns the versions applied."""
    files = sorted(
        (f for f in resources.files("listen.migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    applied: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            for f in files:
                version = f.name.removesuffix(".sql")
                if version in done:
                    continue
                with conn.transaction():
                    conn.execute(f.read_text())
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                log.info("applied migration %s", version)
                applied.append(version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))
    return applied
