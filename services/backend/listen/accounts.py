"""Accounts: passwords, sessions, email verification, rate limits, terms of service, consent and page quota.

Passwords are hashed with scrypt from the standard library (memory-hard, like argon2id, which is not a
dependency here). Session and email tokens are random; only their SHA-256 is stored, so a database leak does
not give anyone a working login.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any

from psycopg import Connection, errors

SESSION_DAYS = 30
VERIFY_HOURS = 48
MIN_PASSWORD = 12
MAX_PASSWORD = 256
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,24}$")
LEGACY_EMAIL = "owner@legacy.invalid"
REGISTRATION_KEY = "registration_open"
# Rate limits: attempts allowed in the window, per IP and per email address.
RATE_LIMIT = 10
RATE_WINDOW = timedelta(minutes=15)


class AccountError(ValueError):
    """A request the rules do not allow. The message is shown to the user."""


# ---------------------------------------------------------------- passwords

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    enc = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${enc(salt)}${enc(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=32,
                                   n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, base64.b64decode(digest))


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"The password must be at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise AccountError("The password is too long")


def normalise_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise AccountError("Enter a valid email address")
    return email


# ---------------------------------------------------------------- tokens

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------- users

def find_user(conn: Connection, email: str) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()


def create_user(conn: Connection, email: str, password: str | None, verified: bool) -> dict[str, Any]:
    try:
        return conn.execute(
            "INSERT INTO users (email, password_hash, email_verified_at) VALUES (%s, %s, "
            "CASE WHEN %s THEN now() END) RETURNING *",
            (email, hash_password(password) if password else None, verified),
        ).fetchone()
    except errors.UniqueViolation as e:
        raise AccountError("An account with this email address already exists") from e


def authenticate(conn: Connection, email: str, password: str) -> dict[str, Any] | None:
    user = find_user(conn, email)
    if user is None or not verify_password(password, user["password_hash"]):
        return None
    return user


def set_password(conn: Connection, user_id: int, password: str) -> None:
    conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(password), user_id))


def mark_verified(conn: Connection, user_id: int) -> None:
    conn.execute("UPDATE users SET email_verified_at = COALESCE(email_verified_at, now()) WHERE id = %s", (user_id,))


def touch_login(conn: Connection, user_id: int) -> None:
    conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user_id,))


def claim_legacy(conn: Connection, email: str, password: str) -> dict[str, Any] | None:
    """Give the legacy owner (see migration 009) a real address and password. None if there is no such owner."""
    legacy = find_user(conn, LEGACY_EMAIL)
    if legacy is None or legacy["password_hash"] is not None:
        return None
    if find_user(conn, email) is not None:
        raise AccountError("That address already has an account; delete it or choose another address")
    return conn.execute(
        "UPDATE users SET email = %s, password_hash = %s, email_verified_at = now() WHERE id = %s RETURNING *",
        (email, hash_password(password), legacy["id"])).fetchone()


# ---------------------------------------------------------------- sessions

def new_session(conn: Connection, user_id: int) -> str:
    token = new_token()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, now() + %s)",
        (_token_hash(token), user_id, timedelta(days=SESSION_DAYS)))
    return token


def session_user(conn: Connection, token: str) -> dict[str, Any] | None:
    """The user of a live session, or None (unknown, expired, or the account is disabled)."""
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = %s AND s.expires_at > now() AND NOT u.is_disabled",
        (_token_hash(token),)).fetchone()
    if row is not None:
        conn.execute("UPDATE sessions SET last_seen_at = now() WHERE token_hash = %s AND last_seen_at < now() - "
                     "interval '1 hour'", (_token_hash(token),))
    return row


def end_session(conn: Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_token_hash(token),))


def end_sessions(conn: Connection, user_id: int, keep_token: str | None = None) -> int:
    keep = _token_hash(keep_token) if keep_token else ""
    return conn.execute("DELETE FROM sessions WHERE user_id = %s AND token_hash <> %s",
                        (user_id, keep)).rowcount


# ---------------------------------------------------------------- email verification

def new_verify_token(conn: Connection, user_id: int) -> str:
    token = new_token()
    conn.execute("DELETE FROM email_tokens WHERE user_id = %s AND purpose = 'verify'", (user_id,))
    conn.execute("INSERT INTO email_tokens (token_hash, user_id, purpose, expires_at) "
                 "VALUES (%s, %s, 'verify', now() + %s)", (_token_hash(token), user_id, timedelta(hours=VERIFY_HOURS)))
    return token


def consume_verify_token(conn: Connection, token: str) -> int | None:
    row = conn.execute("DELETE FROM email_tokens WHERE token_hash = %s AND purpose = 'verify' "
                       "AND expires_at > now() RETURNING user_id", (_token_hash(token),)).fetchone()
    if row is None:
        return None
    mark_verified(conn, row["user_id"])
    return row["user_id"]


# ---------------------------------------------------------------- rate limits

def rate_limited(conn: Connection, keys: list[str]) -> bool:
    """True when any key has used up its attempts in the window. Records nothing."""
    for key in keys:
        n = conn.execute("SELECT count(*) AS n FROM auth_attempts WHERE key = %s AND at > now() - %s",
                         (key, RATE_WINDOW)).fetchone()["n"]
        if n >= RATE_LIMIT:
            return True
    return False


def record_attempt(conn: Connection, keys: list[str]) -> None:
    for key in keys:
        conn.execute("INSERT INTO auth_attempts (key) VALUES (%s)", (key,))
    conn.execute("DELETE FROM auth_attempts WHERE at < now() - interval '1 day'")


# ---------------------------------------------------------------- terms of service

def load_terms(conn: Connection, terms_dir: Path) -> str | None:
    """Register the terms files in terms_dir (<version>.md) and return the current version, the latest by name.
    A file whose text changed under a published version is refused: users have accepted that text."""
    current = None
    for path in sorted(terms_dir.glob("*.md")):
        version = path.stem
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        row = conn.execute("SELECT text_sha256 FROM terms_versions WHERE version = %s", (version,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO terms_versions (version, text_sha256) VALUES (%s, %s)", (version, digest))
        elif row["text_sha256"] != digest:
            raise RuntimeError(f"terms version {version} was changed after publication; publish a new version")
        current = version
    return current


def current_terms(conn: Connection, terms_dir: Path) -> str | None:
    row = conn.execute("SELECT version FROM terms_versions ORDER BY version DESC LIMIT 1").fetchone()
    return row["version"] if row else None


def terms_text(terms_dir: Path, version: str) -> str:
    return (terms_dir / f"{version}.md").read_text(encoding="utf-8")


def accept_terms(conn: Connection, user_id: int, version: str) -> None:
    conn.execute("INSERT INTO user_consents (user_id, version) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                 (user_id, version))


def has_current_consent(conn: Connection, user_id: int, version: str | None) -> bool:
    if version is None:
        return True  # no terms published yet: nothing to accept
    return conn.execute("SELECT 1 FROM user_consents WHERE user_id = %s AND version = %s",
                        (user_id, version)).fetchone() is not None


# ---------------------------------------------------------------- quota

def pages_used(conn: Connection, user_id: int) -> int:
    return int(conn.execute(
        "SELECT coalesce(sum(d.pages), 0) AS n FROM library_entries le JOIN documents d ON d.id = le.document_id "
        "WHERE le.user_id = %s", (user_id,)).fetchone()["n"])


def quota_status(conn: Connection, user: dict[str, Any]) -> dict[str, int]:
    used = pages_used(conn, user["id"])
    return {"used": used, "quota": user["page_quota"], "remaining": max(0, user["page_quota"] - used)}


# ---------------------------------------------------------------- settings and audit

def registration_open(conn: Connection) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key = %s", (REGISTRATION_KEY,)).fetchone()
    return True if row is None else bool(row["value"])


def set_registration_open(conn: Connection, value: bool) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s::jsonb) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (REGISTRATION_KEY, "true" if value else "false"))


def audit(conn: Connection, actor_id: int | None, action: str, target_user_id: int | None,
          detail: dict[str, Any] | None = None) -> None:
    conn.execute("INSERT INTO admin_audit (actor_id, action, target_user_id, detail) VALUES (%s, %s, %s, %s)",
                 (actor_id, action, target_user_id, json.dumps(detail or {})))


def admin_count(conn: Connection) -> int:
    return conn.execute("SELECT count(*) AS n FROM users WHERE is_admin AND NOT is_disabled").fetchone()["n"]
