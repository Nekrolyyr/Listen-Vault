import dataclasses

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from listen import accounts, mail
from listen.api.main import create_app
from listen.api.security import SESSION_COOKIE

from .conftest import TERMS_DIR, make_user

AUTH = {"X-Listen-Proxy": "test-proxy-secret", "X-Requested-With": "listen-vault"}
PASSWORD = "correct horse battery"


@pytest.fixture
def app_for(settings):
    def make(**changes):
        return create_app(dataclasses.replace(settings, **changes))
    return make


@pytest.fixture
def anon(app_for):
    with TestClient(app_for()) as c:
        yield c


def register(client, email="ann@example.org", password=PASSWORD, accept=True):
    return client.post("/api/auth/register", json={"email": email, "password": password,
                                                   "accept_terms": accept}, headers=AUTH)


def test_register_signs_in_without_mail_and_logs_in_again(anon, settings):
    r = register(anon)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "ok" and body["email"] == "ann@example.org"
    assert body["quota"] == {"used": 0, "quota": 300, "remaining": 300}
    assert body["terms"]["accepted"] is True
    assert anon.get("/api/me", headers=AUTH).json()["email"] == "ann@example.org"

    anon.cookies.clear()
    assert anon.get("/api/me", headers=AUTH).status_code == 401
    r = anon.post("/api/auth/login", json={"email": "ANN@example.org", "password": PASSWORD}, headers=AUTH)
    assert r.status_code == 200 and SESSION_COOKIE in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"] and "samesite=strict" in r.headers["set-cookie"].lower()
    assert "Secure" not in r.headers["set-cookie"]  # plain http in development


def test_register_needs_terms_password_rules_and_unique_email(anon):
    assert register(anon, accept=False).status_code == 422
    assert register(anon, password="short").status_code == 422
    assert register(anon, email="not-an-address").status_code == 422
    assert register(anon).status_code == 201
    assert register(anon).status_code == 409


def test_wrong_password_and_unknown_email_look_the_same(anon):
    register(anon)
    anon.cookies.clear()
    wrong = anon.post("/api/auth/login", json={"email": "ann@example.org", "password": "wrong wrong wrong"}, headers=AUTH)
    unknown = anon.post("/api/auth/login", json={"email": "nobody@example.org", "password": PASSWORD}, headers=AUTH)
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_rate_limit_counts_refused_attempts(anon):
    for _ in range(accounts.RATE_LIMIT):
        assert anon.post("/api/auth/login", json={"email": "x@example.org", "password": "nope nope nope"},
                         headers=AUTH).status_code == 401
    assert anon.post("/api/auth/login", json={"email": "x@example.org", "password": "nope nope nope"},
                     headers=AUTH).status_code == 429


def test_logout_ends_the_session(anon):
    register(anon)
    token = anon.cookies[SESSION_COOKIE]
    assert anon.post("/api/auth/logout", headers=AUTH).status_code == 200
    anon.cookies.set(SESSION_COOKIE, token)  # the old cookie no longer works
    assert anon.get("/api/me", headers=AUTH).status_code == 401


def test_registration_can_be_closed_by_an_administrator(app_for, settings):
    make_user(settings.database_url, "root@example.org", admin=True)
    with TestClient(app_for()) as admin_client:
        token = accounts_token(settings.database_url, "root@example.org")
        admin_client.cookies.set(SESSION_COOKIE, token)
        assert admin_client.put("/api/admin/registration", json={"open": False}, headers=AUTH).status_code == 200
        with TestClient(app_for()) as anon:
            assert register(anon).status_code == 403
        admin_client.put("/api/admin/registration", json={"open": True}, headers=AUTH)
        with TestClient(app_for()) as anon:
            assert register(anon).status_code == 201


def accounts_token(database_url, email):
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
        user = accounts.find_user(conn, email)
        return accounts.new_session(conn, user["id"])


def test_password_change_keeps_this_session_and_ends_the_others(anon, settings):
    register(anon)
    here = anon.cookies[SESSION_COOKIE]
    other = TestClient(anon.app)
    other.post("/api/auth/login", json={"email": "ann@example.org", "password": PASSWORD}, headers=AUTH)
    other_token = other.cookies[SESSION_COOKIE]
    r = anon.post("/api/auth/password", json={"current": PASSWORD, "new": "a new long password"}, headers=AUTH)
    assert r.status_code == 200
    anon.cookies.set(SESSION_COOKIE, here)
    assert anon.get("/api/me", headers=AUTH).status_code == 200
    other.cookies.set(SESSION_COOKIE, other_token)
    assert other.get("/api/me", headers=AUTH).status_code == 401
    assert anon.post("/api/auth/password", json={"current": "wrong wrong wrong", "new": "another long one"},
                     headers=AUTH).status_code == 403


def test_terms_are_public_and_a_new_version_must_be_accepted(app_for, settings, tmp_path):
    with TestClient(app_for()) as anon:
        terms = anon.get("/api/terms", headers=AUTH).json()
        assert terms["version"] == "2026-10-20" and "operator" in terms["text"].lower()
    # a new version appears: existing accounts have not accepted it
    new_dir = tmp_path / "terms"
    new_dir.mkdir()
    (new_dir / "2026-10-20.md").write_text((TERMS_DIR / "2026-10-20.md").read_text())
    (new_dir / "2026-11-01.md").write_text("# Newer terms\n")
    with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
        accounts.load_terms(conn, new_dir)
    user_id, token = make_user(settings.database_url, "bea@example.org")
    with TestClient(app_for(terms_dir=new_dir)) as c:
        c.cookies.set(SESSION_COOKIE, token)
        me = c.get("/api/me", headers=AUTH).json()
        assert me["terms"] == {"current": "2026-11-01", "accepted": False}
        assert c.post("/api/terms/accept", json={"version": "2026-10-20"}, headers=AUTH).status_code == 409
        assert c.post("/api/terms/accept", json={"version": "2026-11-01"}, headers=AUTH).status_code == 200
        assert c.get("/api/me", headers=AUTH).json()["terms"]["accepted"] is True


def test_a_published_terms_version_cannot_be_edited(settings, tmp_path):
    d = tmp_path / "terms"
    d.mkdir()
    (d / "2026-10-20.md").write_text("first text")
    with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
        accounts.load_terms(conn, d)
        (d / "2026-10-20.md").write_text("edited text")
        with pytest.raises(RuntimeError, match="changed after publication"):
            accounts.load_terms(conn, d)


def test_verification_is_required_when_mail_is_configured(app_for, settings, monkeypatch):
    sent = []
    monkeypatch.setattr(mail, "send", lambda s, to, subject, body: sent.append((to, body)))
    smtp = dict(smtp_host="smtp.example.org", smtp_from="listen@example.org")
    with TestClient(app_for(**smtp)) as c:
        r = register(c)
        assert r.status_code == 201 and r.json() == {"status": "verify_email", "email": "ann@example.org"}
        assert SESSION_COOKIE not in c.cookies
        assert c.post("/api/auth/login", json={"email": "ann@example.org", "password": PASSWORD},
                      headers=AUTH).status_code == 403
        link_token = sent[0][1].split("token=")[1].split()[0]
        assert c.post("/api/auth/verify", json={"token": "wrong"}, headers=AUTH).status_code == 400
        assert c.post("/api/auth/verify", json={"token": link_token}, headers=AUTH).status_code == 200
        assert c.post("/api/auth/verify", json={"token": link_token}, headers=AUTH).status_code == 400  # one use
        assert c.post("/api/auth/login", json={"email": "ann@example.org", "password": PASSWORD},
                      headers=AUTH).status_code == 200


def test_a_failed_mail_does_not_create_the_account(app_for, settings, monkeypatch):
    def fail(*_):
        raise mail.MailError("no route to host")
    monkeypatch.setattr(mail, "send", fail)
    with TestClient(app_for(smtp_host="smtp.example.org", smtp_from="listen@example.org")) as c:
        assert register(c).status_code == 503
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
