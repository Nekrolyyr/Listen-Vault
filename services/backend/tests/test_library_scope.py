"""Each user sees only their own library; a stored PDF is shared by everyone who adds the same file."""
import dataclasses

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from listen import areas
from listen.api.main import create_app
from listen.api.security import SESSION_COOKIE
from listen.storage import DocPaths

from .conftest import make_pdf, make_user

AUTH = {"X-Listen-Proxy": "test-proxy-secret", "X-Requested-With": "listen-vault"}


@pytest.fixture
def pdf(tmp_path_factory) -> bytes:
    return make_pdf(tmp_path_factory.mktemp("pdf") / "two.pdf", [[(14, "Head pruning")], [(12, "Methods")]]).read_bytes()


@pytest.fixture
def make_client(settings, tmp_path):
    def make(email, admin=False, **changes):
        app = create_app(dataclasses.replace(settings, **changes))
        c = TestClient(app)
        c.__enter__()
        user_id, token = make_user(settings.database_url, email, admin=admin)
        c.cookies.set(SESSION_COOKIE, token)
        c.user_id = user_id
        return c
    clients = []

    def tracked(*args, **kwargs):
        c = make(*args, **kwargs)
        clients.append(c)
        return c
    yield tracked
    for c in clients:
        c.__exit__(None, None, None)


def upload(client, pdf, name="Paper.pdf"):
    return client.post("/api/documents", files={"file": (name, pdf, "application/pdf")}, headers=AUTH)


def db(settings):
    return psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row)


def test_another_users_papers_do_not_exist_for_you(make_client, settings, pdf):
    ann = make_client("ann@example.org")
    bea = make_client("bea@example.org")
    doc = upload(ann, pdf).json()
    doc_id = doc["id"]
    assert [d["id"] for d in ann.get("/api/documents", headers=AUTH).json()] == [doc_id]
    assert bea.get("/api/documents", headers=AUTH).json() == []
    assert bea.get(f"/api/documents/{doc_id}", headers=AUTH).status_code == 404
    assert bea.get(f"/api/documents/{doc_id}/pdf", headers=AUTH).status_code == 404
    assert bea.get(f"/api/documents/{doc_id}/text", headers=AUTH).status_code == 404
    assert bea.get(f"/api/documents/{doc_id}/chapters/0/audio", headers=AUTH).status_code == 404
    assert bea.get(f"/api/documents/{doc_id}/chapters/0/sentences", headers=AUTH).status_code == 404
    assert bea.get(f"/api/documents/{doc_id}/playback", headers=AUTH).status_code == 404
    assert bea.put(f"/api/documents/{doc_id}/playback", json={"chapter_idx": 0, "position_s": 1},
                   headers=AUTH).status_code == 404
    assert bea.patch(f"/api/documents/{doc_id}", json={"title": "Stolen"}, headers=AUTH).status_code == 404
    assert bea.patch(f"/api/documents/{doc_id}/classification", json={"primary": areas.IDS[0]},
                     headers=AUTH).status_code == 404
    assert bea.post(f"/api/documents/{doc_id}/rerun", json={"stage": "chapters"}, headers=AUTH).status_code == 404
    assert bea.get(f"/api/documents/{doc_id}/related", headers=AUTH).status_code == 404
    assert bea.get(f"/api/documents/{doc_id}/questions", headers=AUTH).status_code == 404
    assert bea.post(f"/api/documents/{doc_id}/questions", json={"question": "why?"}, headers=AUTH).status_code == 404
    assert bea.delete(f"/api/documents/{doc_id}", headers=AUTH).status_code == 404
    facets = bea.get("/api/library/facets", headers=AUTH).json()
    assert facets["total"] == 0 and facets["terms"]["task"] == [] and facets["year_bounds"] is None
    assert bea.get("/api/library/overview", headers=AUTH).json()["papers"] == 0
    assert bea.get("/api/library/graph", headers=AUTH).json() == {"nodes": [], "links": []}
    assert bea.get("/api/library/timeline?term=head%20pruning", headers=AUTH).json()["items"] == []
    # the paper itself is still there for its owner
    assert ann.get(f"/api/documents/{doc_id}", headers=AUTH).status_code == 200


def test_questions_are_private_to_the_user_who_asked(make_client, settings, pdf):
    ann = make_client("ann@example.org")
    bea = make_client("bea@example.org")
    doc_id = upload(ann, pdf).json()["id"]
    with db(settings) as conn:
        conn.execute("INSERT INTO library_entries (user_id, document_id) SELECT id, %s FROM users "
                     "WHERE email = 'bea@example.org'", (doc_id,))
        conn.execute("UPDATE documents SET status = 'ready' WHERE id = %s", (doc_id,))
        conn.execute("INSERT INTO questions (document_id, question, user_id) VALUES (%s, 'Ann asks?', %s)",
                     (doc_id, ann.user_id))
    assert [q["question"] for q in ann.get(f"/api/documents/{doc_id}/questions", headers=AUTH).json()] == ["Ann asks?"]
    assert bea.get(f"/api/documents/{doc_id}/questions", headers=AUTH).json() == []


def test_same_pdf_is_stored_once_and_listened_to_separately(make_client, settings, pdf):
    ann = make_client("ann@example.org")
    bea = make_client("bea@example.org")
    first = upload(ann, pdf).json()
    second = upload(bea, pdf, name="copy.pdf")
    assert second.status_code == 201 and second.json()["id"] == first["id"]
    with db(settings) as conn:
        assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 1
    assert ann.patch(f"/api/documents/{first['id']}", json={"listened": True}, headers=AUTH).json()["listened"] is True
    assert bea.get(f"/api/documents/{first['id']}", headers=AUTH).json()["listened"] is False

    assert ann.delete(f"/api/documents/{first['id']}", headers=AUTH).status_code == 204
    root = DocPaths(settings.docs_dir, first["id"]).root
    assert root.exists()  # bea still holds it
    assert bea.get(f"/api/documents/{first['id']}/pdf", headers=AUTH).status_code == 200
    assert bea.delete(f"/api/documents/{first['id']}", headers=AUTH).status_code == 204
    assert not root.exists()  # nobody holds it any more
    with db(settings) as conn:
        assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 0


def test_adding_the_same_paper_twice_is_refused(make_client, pdf):
    ann = make_client("ann@example.org")
    assert upload(ann, pdf).status_code == 201
    assert upload(ann, pdf).status_code == 409


def test_same_web_address_joins_the_shared_copy(make_client, settings):
    ann = make_client("ann@example.org")
    bea = make_client("bea@example.org")
    url = {"url": "https://example.org/article"}
    first = ann.post("/api/documents/url", json=url, headers=AUTH).json()
    joined = bea.post("/api/documents/url", json=url, headers=AUTH)
    assert joined.status_code == 201 and joined.json()["id"] == first["id"]
    assert bea.post("/api/documents/url", json=url, headers=AUTH).status_code == 409


def test_quota_counts_the_pages_of_your_library(make_client, settings, pdf):
    ann = make_client("ann@example.org")
    bea = make_client("bea@example.org")
    with db(settings) as conn:
        conn.execute("UPDATE users SET page_quota = 3 WHERE email = 'ann@example.org'")
        conn.execute("UPDATE users SET page_quota = 1 WHERE email = 'bea@example.org'")
    assert upload(ann, pdf, "one.pdf").status_code == 201  # 2 of 3 pages
    refused = upload(ann, make_pdf(settings.data_dir.parent / "b.pdf", [[(14, "Other")], [(12, "x")]]).read_bytes(),
                     "two.pdf")
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "quota_exceeded"
    assert ann.get("/api/me", headers=AUTH).json()["quota"] == {"used": 2, "quota": 3, "remaining": 1}
    # a shared copy counts against the quota of the user who adds it, even though it is stored once
    assert bea.post("/api/documents", files={"file": ("x.pdf", pdf, "application/pdf")}, headers=AUTH).status_code == 403
    assert bea.get("/api/me", headers=AUTH).json()["quota"]["used"] == 0


def test_an_administrator_changes_the_quota_at_once(make_client, settings, pdf):
    admin = make_client("root@example.org", admin=True)
    ann = make_client("ann@example.org")
    with db(settings) as conn:
        conn.execute("UPDATE users SET page_quota = 1 WHERE email = 'ann@example.org'")
    assert upload(ann, pdf).status_code == 403
    assert admin.patch(f"/api/admin/users/{ann.user_id}", json={"page_quota": 10}, headers=AUTH).status_code == 200
    assert upload(ann, pdf).status_code == 201


def test_terms_must_be_accepted_before_adding_papers(make_client, settings, pdf):
    ann = make_client("ann@example.org")
    with db(settings) as conn:
        conn.execute("DELETE FROM user_consents WHERE user_id = %s", (ann.user_id,))
    r = upload(ann, pdf)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "terms_required"
    assert ann.post("/api/documents/url", json={"url": "https://example.org/x"}, headers=AUTH).status_code == 403


def test_each_user_has_a_few_papers_waiting_at_most(make_client, settings):
    ann = make_client("ann@example.org")
    for n in range(5):
        assert ann.post("/api/documents/url", json={"url": f"https://example.org/{n}"}, headers=AUTH).status_code == 201
    r = ann.post("/api/documents/url", json={"url": "https://example.org/6"}, headers=AUTH)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "busy"


def test_library_wide_actions_are_for_administrators(make_client):
    ann = make_client("ann@example.org")
    assert ann.get("/api/zotero", headers=AUTH).status_code == 403
    assert ann.post("/api/zotero/sync", headers=AUTH).status_code == 403
    assert ann.post("/api/zotero/queue-all", headers=AUTH).status_code == 403
    assert ann.post("/api/library/classify", headers=AUTH).status_code == 403
    assert ann.post("/api/library/topics/rebuild", headers=AUTH).status_code == 403
    assert ann.post("/api/library/keywords/backfill", headers=AUTH).status_code == 403
    assert ann.get("/api/library/review", headers=AUTH).status_code == 403
    assert ann.get("/api/admin/users", headers=AUTH).status_code == 403
    root = make_client("root@example.org", admin=True)
    assert root.post("/api/library/topics/rebuild", headers=AUTH).json() == {"queued": True}
