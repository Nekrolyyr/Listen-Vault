"""Audio is per user: made for the user who added the paper, with their settings, and visible only to them."""
import dataclasses

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from listen import narrations
from listen.api.main import create_app
from listen.api.security import SESSION_COOKIE
from listen.storage import DocPaths

from .conftest import make_user
from .test_pipeline import ARTICLE, _worker

AUTH = {"X-Listen-Proxy": "test-proxy-secret", "X-Requested-With": "listen-vault"}


@pytest.fixture
def clients(settings):
    made = []

    def make(email):
        c = TestClient(create_app(dataclasses.replace(settings)))
        c.__enter__()
        made.append(c)
        user_id, token = make_user(settings.database_url, email)
        c.cookies.set(SESSION_COOKIE, token)
        c.user_id = user_id
        return c
    yield make
    for c in made:
        c.__exit__(None, None, None)


def add_url(client, url="https://example.org/sleep"):
    return client.post("/api/documents/url", json={"url": url}, headers=AUTH).json()


def test_each_user_gets_audio_with_their_own_settings(settings, pool, clients):
    ann, bea = clients("ann@example.org"), clients("bea@example.org")
    assert ann.put("/api/settings", json={"voice": "bm_george"}, headers=AUTH).status_code == 200
    doc = add_url(ann)
    DocPaths(settings.docs_dir, doc["id"]).ensure().source_html.write_text(ARTICLE)  # skip the network fetch
    worker, _ = _worker(settings, pool)
    assert worker.run_once()  # the text of the paper
    assert worker.run_once()  # Ann's audio, with her voice
    assert ann.get(f"/api/documents/{doc['id']}/chapters/0/sentences", headers=AUTH).status_code == 200

    # Bea adds the same paper: no text work again, only her own audio, with her settings
    assert bea.put("/api/settings", json={"voice": "af_bella"}, headers=AUTH).status_code == 200
    assert add_url(bea)["id"] == doc["id"]
    worker2, _ = _worker(settings, pool)
    assert worker2.run_once()
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        rows = conn.execute("SELECT u.email, n.state, n.settings->>'voice' AS voice FROM narrations n "
                            "JOIN users u ON u.id = n.user_id ORDER BY u.email").fetchall()
        assert conn.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'document'").fetchone()["n"] == 1
    assert [(r["email"], r["state"], r["voice"]) for r in rows] == [
        ("ann@example.org", "ready", "bm_george"), ("bea@example.org", "ready", "af_bella")]

    # neither can hear the other's audio: each sees their own, a stranger sees none
    assert ann.get(f"/api/documents/{doc['id']}/chapters/1/audio", headers=AUTH).status_code == 200
    assert bea.get(f"/api/documents/{doc['id']}/chapters/1/audio", headers=AUTH).status_code == 200
    stranger = clients("cy@example.org")
    stranger_add = add_url(stranger)
    assert stranger_add["id"] == doc["id"] and stranger_add["audio_state"] is None
    assert stranger.get(f"/api/documents/{doc['id']}/chapters/1/audio", headers=AUTH).status_code == 404


def test_audio_is_not_made_twice_and_regenerating_touches_only_your_own(settings, pool, clients):
    ann, bea = clients("ann@example.org"), clients("bea@example.org")
    doc = add_url(ann)
    add_url(bea)
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("UPDATE documents SET status = 'ready' WHERE id = %s", (doc["id"],))
        conn.execute("INSERT INTO narrations (document_id, user_id, state) SELECT %s, id, 'ready' FROM users "
                     "WHERE email = 'ann@example.org'", (doc["id"],))
        conn.execute("UPDATE jobs SET state = 'done' WHERE kind = 'narrate' AND user_id IS NOT NULL "
                     "AND user_id = (SELECT id FROM users WHERE email = 'ann@example.org')")
    assert ann.post(f"/api/documents/{doc['id']}/audio", headers=AUTH).status_code == 409  # she has audio
    assert bea.post(f"/api/documents/{doc['id']}/audio", headers=AUTH).json() == {"queued": False}  # already queued
    assert bea.post(f"/api/documents/{doc['id']}/rerun", json={"stage": "synthesis"}, headers=AUTH).status_code == 409
    assert ann.post(f"/api/documents/{doc['id']}/rerun", json={"stage": "synthesis"}, headers=AUTH).status_code == 202
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        queued = conn.execute("SELECT u.email FROM jobs j JOIN users u ON u.id = j.user_id "
                              "WHERE j.kind = 'narrate' AND j.state = 'queued'").fetchall()
        assert conn.execute("SELECT count(*) AS n FROM narrations").fetchone()["n"] == 0  # hers was taken away
    assert {r["email"] for r in queued} == {"ann@example.org", "bea@example.org"}


def test_removing_a_paper_removes_only_your_audio(settings, clients):
    ann, bea = clients("ann@example.org"), clients("bea@example.org")
    doc = add_url(ann)
    add_url(bea)
    root = DocPaths(settings.docs_dir, doc["id"]).ensure().root
    with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
        for email in ("ann@example.org", "bea@example.org"):
            n = conn.execute("INSERT INTO narrations (document_id, user_id, state) SELECT %s, id, 'ready' FROM users "
                             "WHERE email = %s RETURNING id", (doc["id"], email)).fetchone()["id"]
            (root / "narrations" / str(n)).mkdir(parents=True)
            (root / "narrations" / str(n) / "0.m4a").write_bytes(b"audio")
            conn.execute("INSERT INTO narration_chapters (narration_id, idx, audio_file, duration_s) "
                         "VALUES (%s, 0, %s, 5)", (n, f"narrations/{n}/0.m4a"))
    assert ann.delete(f"/api/documents/{doc['id']}", headers=AUTH).status_code == 204
    assert bea.get(f"/api/documents/{doc['id']}/chapters/0/audio", headers=AUTH).status_code == 200
    assert root.exists()
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        assert [r["email"] for r in conn.execute(
            "SELECT u.email FROM narrations n JOIN users u ON u.id = n.user_id").fetchall()] == ["bea@example.org"]


def test_audio_waits_for_the_paper_without_using_an_attempt(settings, pool, clients):
    ann = clients("ann@example.org")
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        doc = conn.execute("INSERT INTO documents (source_type, title, status) VALUES ('url', 'Not yet', 'queued') "
                           "RETURNING id").fetchone()[0]
        conn.execute("INSERT INTO library_entries (user_id, document_id) SELECT id, %s FROM users "
                     "WHERE email = 'ann@example.org'", (doc,))
    with pool.connection() as conn:
        narrations.queue(conn, doc, ann.user_id)
    worker, _ = _worker(settings, pool)
    assert worker.run_once()  # claimed, found the paper not ready, and put back in the queue
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE kind = 'narrate'").fetchone()
        assert job["state"] == "queued" and job["attempts"] == 0 and job["run_after"] > job["created_at"]
        assert conn.execute("SELECT count(*) AS n FROM narrations").fetchone()["n"] == 0
