"""Administration console, data export and deletion, and the operator commands."""
import dataclasses
import io
import json
import zipfile

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from listen import cli
from listen.api.main import create_app
from listen.api.security import SESSION_COOKIE
from listen.storage import DocPaths

from .conftest import make_pdf, make_user

AUTH = {"X-Listen-Proxy": "test-proxy-secret", "X-Requested-With": "listen-vault"}
PASSWORD = "correct horse battery"


@pytest.fixture
def make_client(settings):
    clients = []

    def make(email, admin=False, password=PASSWORD):
        c = TestClient(create_app(dataclasses.replace(settings)))
        c.__enter__()
        clients.append(c)
        user_id, token = make_user(settings.database_url, email, admin=admin, password=password)
        c.cookies.set(SESSION_COOKIE, token)
        c.user_id = user_id
        return c
    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def pdf(tmp_path_factory) -> bytes:
    return make_pdf(tmp_path_factory.mktemp("pdf") / "p.pdf", [[(14, "Secret title of Ann's paper")]]).read_bytes()


def db(settings):
    return psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row)


def test_admin_list_shows_counts_and_never_titles(make_client, settings, pdf):
    root = make_client("root@example.org", admin=True)
    ann = make_client("ann@example.org")
    ann.post("/api/documents", files={"file": ("a.pdf", pdf, "application/pdf")}, headers=AUTH)
    users = {u["email"]: u for u in root.get("/api/admin/users", headers=AUTH).json()}
    assert users["ann@example.org"]["documents"] == 1 and users["ann@example.org"]["pages_used"] == 1
    assert "Secret title" not in json.dumps(users)


def test_quota_change_is_audited_and_refused_for_others(make_client, settings):
    root = make_client("root@example.org", admin=True)
    ann = make_client("ann@example.org")
    r = root.patch(f"/api/admin/users/{ann.user_id}", json={"page_quota": 50}, headers=AUTH)
    assert r.status_code == 200 and r.json()["page_quota"] == 50
    audit = root.get("/api/admin/audit", headers=AUTH).json()
    assert audit[0]["action"] == "user.page_quota" and audit[0]["detail"] == {"from": 300, "to": 50}
    assert audit[0]["actor"] == "root@example.org"
    assert ann.patch(f"/api/admin/users/{ann.user_id}", json={"page_quota": 9999}, headers=AUTH).status_code == 403


def test_disabling_an_account_ends_its_sessions(make_client, settings):
    root = make_client("root@example.org", admin=True)
    ann = make_client("ann@example.org")
    token = ann.cookies[SESSION_COOKIE]
    assert root.patch(f"/api/admin/users/{ann.user_id}", json={"is_disabled": True}, headers=AUTH).status_code == 200
    ann.cookies.set(SESSION_COOKIE, token)
    assert ann.get("/api/me", headers=AUTH).status_code == 401
    with TestClient(create_app(dataclasses.replace(settings))) as fresh:
        r = fresh.post("/api/auth/login", json={"email": "ann@example.org", "password": PASSWORD}, headers=AUTH)
        assert r.status_code == 403
    assert root.patch(f"/api/admin/users/{ann.user_id}", json={"is_disabled": False}, headers=AUTH).status_code == 200


def test_the_last_administrator_cannot_be_removed(make_client, settings):
    root = make_client("root@example.org", admin=True)
    assert root.patch(f"/api/admin/users/{root.user_id}", json={"is_admin": False}, headers=AUTH).status_code == 409
    assert root.patch(f"/api/admin/users/{root.user_id}", json={"is_disabled": True}, headers=AUTH).status_code == 409
    other = make_client("two@example.org", admin=True)
    assert root.patch(f"/api/admin/users/{other.user_id}", json={"is_admin": False}, headers=AUTH).status_code == 200


def test_registration_switch_is_audited(make_client):
    root = make_client("root@example.org", admin=True)
    assert root.get("/api/admin/registration", headers=AUTH).json()["open"] is True
    assert root.put("/api/admin/registration", json={"open": False}, headers=AUTH).json()["open"] is False
    assert root.get("/api/admin/audit", headers=AUTH).json()[0]["action"] == "registration.open"


def test_export_contains_the_summary_and_the_pdfs(make_client, settings, pdf):
    ann = make_client("ann@example.org")
    doc = ann.post("/api/documents", files={"file": ("a.pdf", pdf, "application/pdf")}, headers=AUTH).json()
    r = ann.get("/api/account/export", headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(r.content))
    summary = json.loads(archive.read("account.json"))
    assert summary["email"] == "ann@example.org"
    assert summary["library"][0]["pdf"] == f"pdf/{doc['id']}.pdf"
    assert archive.read(f"pdf/{doc['id']}.pdf") == pdf


def test_deleting_an_account_keeps_papers_others_hold(make_client, settings, pdf):
    ann = make_client("ann@example.org")
    bea = make_client("bea@example.org")
    shared = ann.post("/api/documents", files={"file": ("a.pdf", pdf, "application/pdf")}, headers=AUTH).json()
    bea.post("/api/documents", files={"file": ("a.pdf", pdf, "application/pdf")}, headers=AUTH)
    own = ann.post("/api/documents/url", json={"url": "https://example.org/only-ann"}, headers=AUTH).json()

    assert ann.request("DELETE", "/api/account", json={"password": "wrong wrong wrong"}, headers=AUTH).status_code == 403
    r = ann.request("DELETE", "/api/account", json={"password": PASSWORD}, headers=AUTH)
    assert r.status_code == 204
    assert ann.get("/api/me", headers=AUTH).status_code == 401
    assert DocPaths(settings.docs_dir, shared["id"]).root.exists()  # bea holds it
    assert bea.get(f"/api/documents/{shared['id']}", headers=AUTH).status_code == 200
    assert not DocPaths(settings.docs_dir, own["id"]).root.exists()  # nobody holds it
    with db(settings) as conn:
        assert conn.execute("SELECT count(*) AS n FROM users WHERE email = 'ann@example.org'").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM documents WHERE id = %s", (own["id"],)).fetchone()["n"] == 0


def test_only_administrator_cannot_delete_their_account(make_client):
    root = make_client("root@example.org", admin=True)
    r = root.request("DELETE", "/api/account", json={"password": PASSWORD}, headers=AUTH)
    assert r.status_code == 409


def test_cli_make_admin_claims_the_legacy_owner(settings, monkeypatch):
    with db(settings) as conn:
        conn.execute("INSERT INTO users (email, is_admin, page_quota) VALUES ('owner@legacy.invalid', true, 1000000)")
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    args = type("A", (), {"email": "Doctor@Example.org", "password_stdin": True})
    cli.make_admin(settings, args)
    with db(settings) as conn:
        user = conn.execute("SELECT * FROM users").fetchall()
    assert [(u["email"], u["is_admin"], u["page_quota"]) for u in user] == [("doctor@example.org", True, 1000000)]
    assert user[0]["password_hash"]


def test_cli_set_quota_and_dedup_of_legacy_copies(settings, tmp_path_factory, capsys):
    pdf_path = make_pdf(tmp_path_factory.mktemp("legacy") / "x.pdf", [[(14, "Same bytes")]])
    with db(settings) as conn:
        owner = conn.execute("INSERT INTO users (email, page_quota) VALUES ('old@example.org', 10) RETURNING id").fetchone()
        ids = []
        for _ in range(2):  # two copies of one paper, from before accounts existed
            doc = conn.execute("INSERT INTO documents (source_type, title, status) VALUES ('pdf', 'X', 'ready') "
                               "RETURNING id").fetchone()["id"]
            DocPaths(settings.docs_dir, doc).ensure()
            DocPaths(settings.docs_dir, doc).source_pdf.write_bytes(pdf_path.read_bytes())
            conn.execute("INSERT INTO library_entries (user_id, document_id, listened) VALUES (%s, %s, %s)",
                         (owner["id"], doc, len(ids) == 1))
            ids.append(doc)
    args = type("A", (), {"email": "old@example.org", "pages": 25, "storage_gb": None})
    cli.set_quota(settings, args)
    with db(settings) as conn:
        assert conn.execute("SELECT page_quota FROM users").fetchone()["page_quota"] == 25

    cli.dedup_existing(settings, type("A", (), {"apply": False})())
    assert "dry run" in capsys.readouterr().out
    assert DocPaths(settings.docs_dir, ids[1]).root.exists()

    cli.dedup_existing(settings, type("A", (), {"apply": True})())
    assert not DocPaths(settings.docs_dir, ids[1]).root.exists()
    with db(settings) as conn:
        rows = conn.execute("SELECT id, content_sha256, pages FROM documents").fetchall()
        entries = conn.execute("SELECT document_id, listened FROM library_entries ORDER BY listened").fetchall()
    assert len(rows) == 1 and rows[0]["id"] == ids[0] and rows[0]["content_sha256"] and rows[0]["pages"] == 1
    assert [e["document_id"] for e in entries] == [ids[0]]  # the same user held both copies: one entry remains
