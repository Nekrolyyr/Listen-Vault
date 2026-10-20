"""Storage per user: the PDFs of the user's library and the user's audio count against the limit (1 GiB by default)."""
import dataclasses

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from listen import accounts
from listen.api.main import create_app
from listen.api.security import SESSION_COOKIE
from listen.storage import DocPaths

from .conftest import make_pdf, make_user
from .test_pipeline import ARTICLE, _worker

AUTH = {"X-Listen-Proxy": "test-proxy-secret", "X-Requested-With": "listen-vault"}


@pytest.fixture
def pdf(tmp_path_factory) -> bytes:
    return make_pdf(tmp_path_factory.mktemp("pdf") / "p.pdf", [[(14, "Storage test paper")]]).read_bytes()


@pytest.fixture
def make_client(settings):
    made = []

    def make(email, **changes):
        c = TestClient(create_app(dataclasses.replace(settings)))
        c.__enter__()
        made.append(c)
        user_id, token = make_user(settings.database_url, email)
        c.cookies.set(SESSION_COOKIE, token)
        c.user_id = user_id
        if changes:
            with psycopg.connect(settings.database_url, autocommit=True) as conn:
                for column, value in changes.items():
                    conn.execute(f"UPDATE users SET {column} = %s WHERE id = %s", (value, user_id))
        return c
    yield make
    for c in made:
        c.__exit__(None, None, None)


def db(settings):
    return psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row)


def upload(client, pdf):
    return client.post("/api/documents", files={"file": ("p.pdf", pdf, "application/pdf")}, headers=AUTH)


def test_the_default_limit_is_one_gigabyte_and_the_account_shows_it(make_client):
    ann = make_client("ann@example.org")
    me = ann.get("/api/me", headers=AUTH).json()
    assert me["storage"] == {"used": 0, "quota": 1024 ** 3, "remaining": 1024 ** 3}


def test_an_upload_that_does_not_fit_is_refused(make_client, pdf, settings):
    ann = make_client("ann@example.org", storage_quota_bytes=len(pdf) - 1)
    r = upload(ann, pdf)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "storage_full"
    assert ann.get("/api/documents", headers=AUTH).json() == []


def test_a_shared_paper_counts_for_everyone_who_holds_it(make_client, pdf, settings):
    ann = make_client("ann@example.org")
    bea = make_client("bea@example.org", storage_quota_bytes=len(pdf) - 1)
    assert upload(ann, pdf).status_code == 201
    r = upload(bea, pdf)  # the same bytes: stored once, but Bea's library holds the whole PDF
    assert r.status_code == 403 and r.json()["detail"]["code"] == "storage_full"
    with db(settings) as conn:
        assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 1


def test_audio_counts_and_removing_it_frees_the_space(make_client, pdf, settings):
    ann = make_client("ann@example.org")
    doc = upload(ann, pdf).json()
    with db(settings) as conn:
        conn.execute("UPDATE jobs SET state = 'done'")  # the paper is processed, nothing is waiting
        conn.execute("UPDATE documents SET status = 'ready' WHERE id = %s", (doc["id"],))
        n = conn.execute("INSERT INTO narrations (document_id, user_id, state) SELECT %s, id, 'ready' FROM users "
                         "WHERE email = 'ann@example.org' RETURNING id", (doc["id"],)).fetchone()["id"]
        conn.execute("INSERT INTO narration_chapters (narration_id, idx, audio_file, duration_s, bytes) "
                     "VALUES (%s, 0, 'x.m4a', 5, 1000)", (n,))
    assert ann.get("/api/me", headers=AUTH).json()["storage"]["used"] == len(pdf) + 1000
    assert ann.post(f"/api/documents/{doc['id']}/rerun", json={"stage": "synthesis"}, headers=AUTH).status_code == 202
    assert ann.get("/api/me", headers=AUTH).json()["storage"]["used"] == len(pdf)


def test_a_link_is_refused_only_when_the_storage_is_already_full(make_client, settings):
    ann = make_client("ann@example.org", storage_quota_bytes=0)
    r = ann.post("/api/documents/url", json={"url": "https://example.org/a"}, headers=AUTH)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "storage_full"


def test_audio_stops_at_the_limit_and_keeps_the_chapters_made(make_client, pdf, settings, pool):
    ann = make_client("ann@example.org")
    doc = ann.post("/api/documents/url", json={"url": "https://example.org/sleep"}, headers=AUTH).json()
    DocPaths(settings.docs_dir, doc["id"]).ensure().source_html.write_text(ARTICLE)
    with db(settings) as conn:  # room for almost nothing: the first chapter fits, the second does not
        conn.execute("UPDATE users SET storage_quota_bytes = 1 WHERE email = 'ann@example.org'")
    worker, _ = _worker(settings, pool)
    assert worker.run_once()  # the text of the paper
    assert worker.run_once()  # Ann's audio: the first chapter is made, then the limit stops it
    with db(settings) as conn:
        n = conn.execute("SELECT * FROM narrations").fetchone()
        assert n["state"] == "failed" and "Storage is full" in n["error"]
        assert [r["idx"] for r in conn.execute("SELECT idx FROM narration_chapters").fetchall()] == [0]


def test_an_administrator_changes_the_storage_limit_and_sees_usage(make_client, pdf, settings):
    root = make_client("root@example.org", is_admin=True)
    ann = make_client("ann@example.org")
    upload(ann, pdf)
    row = next(u for u in root.get("/api/admin/users", headers=AUTH).json() if u["email"] == "ann@example.org")
    assert row["storage_used"] == len(pdf) and row["storage_quota"] == 1024 ** 3
    r = root.patch(f"/api/admin/users/{ann.user_id}", json={"storage_quota_bytes": 5000}, headers=AUTH)
    assert r.status_code == 200
    assert ann.get("/api/me", headers=AUTH).json()["storage"]["quota"] == 5000
    assert root.get("/api/admin/stats", headers=AUTH).json()["stored_bytes"] == len(pdf)


def test_the_status_helper_counts_both_parts(settings, make_client, pdf):
    ann = make_client("ann@example.org")
    upload(ann, pdf)
    with db(settings) as conn:
        assert accounts.storage_status(conn, ann.user_id)["used"] == len(pdf)
