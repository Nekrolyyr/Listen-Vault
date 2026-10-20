"""Accounts over the api: register, verify, sign in and out, password, terms, data export and account deletion."""
from __future__ import annotations

import json
import logging
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .. import accounts, config, documents, mail
from ..storage import DocPaths
from .deps import client_ip, pool, user_of
from .security import SESSION_COOKIE

log = logging.getLogger("listen.account")


class RegisterIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)
    accept_terms: bool = False


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class TokenIn(BaseModel):
    token: str = Field(max_length=200)


class PasswordIn(BaseModel):
    current: str = Field(max_length=256)
    new: str = Field(max_length=256)


class TermsIn(BaseModel):
    version: str = Field(max_length=64)


class DeleteIn(BaseModel):
    password: str = Field(max_length=256)


def _me(conn, user: dict[str, Any], current: str | None) -> dict[str, Any]:
    return {
        "id": user["id"], "email": user["email"], "is_admin": user["is_admin"],
        "quota": accounts.quota_status(conn, user),
        "terms": {"current": current,
                  "accepted": accounts.has_current_consent(conn, user["id"], current)},
    }


def guard(request: Request, keys: list[str]) -> None:
    """Count this attempt, and refuse when the keys have used up their attempts. Committed on its own, so a
    refused or failed attempt is still counted."""
    with pool(request).connection() as conn:
        if accounts.rate_limited(conn, keys):
            raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
        accounts.record_attempt(conn, keys)


def _set_session(response: Response, token: str, settings: config.Settings) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=accounts.SESSION_DAYS * 86400, httponly=True,
                        samesite="strict", secure=settings.public_url.startswith("https"), path="/")


def register_account_routes(app: FastAPI, settings: config.Settings) -> None:

    @app.post("/api/auth/register", status_code=201)
    def register(request: Request, body: RegisterIn, response: Response):
        ip = client_ip(request)
        try:
            email = accounts.normalise_email(body.email)
        except accounts.AccountError as e:
            raise HTTPException(422, str(e)) from e
        guard(request, [f"ip:{ip}", f"email:{email}"])
        with pool(request).connection() as conn, conn.transaction():
            if not accounts.registration_open(conn):
                raise HTTPException(403, "Registration is closed. Ask an administrator for an invitation.")
            try:
                accounts.check_password_rules(body.password)
            except accounts.AccountError as e:
                raise HTTPException(422, str(e)) from e
            current = accounts.current_terms(conn, settings.terms_dir)
            if current is not None and not body.accept_terms:
                raise HTTPException(422, "You must accept the terms of service and the privacy statement")
            try:
                user = accounts.create_user(conn, email, body.password, verified=not settings.smtp_configured)
            except accounts.AccountError as e:
                raise HTTPException(409, str(e)) from e
            if current is not None:
                accounts.accept_terms(conn, user["id"], current)
            if settings.smtp_configured:
                token = accounts.new_verify_token(conn, user["id"])
                try:
                    mail.send(settings, email, "Confirm your listen-vault account",
                              f"Confirm your address to start using listen-vault:\n\n"
                              f"{settings.public_url}/verify?token={token}\n\n"
                              "The link works for 48 hours. If you did not register, ignore this message.\n")
                except mail.MailError as e:
                    log.error("verification email not sent: %s", e)
                    raise HTTPException(503, "The confirmation email could not be sent. Try again later.") from e
                return {"status": "verify_email", "email": email}
            token = accounts.new_session(conn, user["id"])
            _set_session(response, token, settings)
            return {"status": "ok", **_me(conn, user, current)}

    @app.post("/api/auth/verify")
    def verify(request: Request, body: TokenIn):
        with pool(request).connection() as conn, conn.transaction():
            if accounts.consume_verify_token(conn, body.token) is None:
                raise HTTPException(400, "This link is invalid or has expired. Register again or ask for a new one.")
        return {"ok": True}

    @app.post("/api/auth/login")
    def login(request: Request, body: LoginIn, response: Response):
        ip = client_ip(request)
        try:
            email = accounts.normalise_email(body.email)
        except accounts.AccountError:
            raise HTTPException(401, "Wrong email address or password") from None
        guard(request, [f"ip:{ip}", f"email:{email}"])
        with pool(request).connection() as conn, conn.transaction():
            user = accounts.authenticate(conn, email, body.password)
            if user is None:
                raise HTTPException(401, "Wrong email address or password")
            if user["is_disabled"]:
                raise HTTPException(403, "This account is disabled. Ask an administrator.")
            if user["email_verified_at"] is None:
                raise HTTPException(403, "Confirm your email address first: the link was sent when you registered.")
            accounts.touch_login(conn, user["id"])
            token = accounts.new_session(conn, user["id"])
            _set_session(response, token, settings)
            return {"status": "ok", **_me(conn, user, accounts.current_terms(conn, settings.terms_dir))}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        with pool(request).connection() as conn:
            accounts.end_session(conn, request.cookies.get(SESSION_COOKIE, ""))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        user = user_of(request)
        with pool(request).connection() as conn:
            return _me(conn, user, accounts.current_terms(conn, settings.terms_dir))

    @app.post("/api/auth/password")
    def change_password(request: Request, body: PasswordIn):
        user = user_of(request)
        with pool(request).connection() as conn, conn.transaction():
            if accounts.authenticate(conn, user["email"], body.current) is None:
                raise HTTPException(403, "The current password is wrong")
            try:
                accounts.check_password_rules(body.new)
            except accounts.AccountError as e:
                raise HTTPException(422, str(e)) from e
            accounts.set_password(conn, user["id"], body.new)
            accounts.end_sessions(conn, user["id"], keep_token=request.cookies.get(SESSION_COOKIE, ""))
        return {"ok": True}

    @app.get("/api/terms")
    def terms(request: Request):
        """The current terms of service (public: they are read before registering)."""
        with pool(request).connection() as conn:
            version = accounts.current_terms(conn, settings.terms_dir)
        if version is None:
            return {"version": None, "text": ""}
        return {"version": version, "text": accounts.terms_text(settings.terms_dir, version)}

    @app.post("/api/terms/accept")
    def accept(request: Request, body: TermsIn):
        user = user_of(request)
        with pool(request).connection() as conn, conn.transaction():
            if body.version != accounts.current_terms(conn, settings.terms_dir):
                raise HTTPException(409, "The terms have changed. Read the current version and accept it.")
            accounts.accept_terms(conn, user["id"], body.version)
        return {"ok": True}

    @app.get("/api/account/export")
    def export(request: Request):
        """Everything the account holds: a JSON summary and the uploaded PDFs of the library."""
        user = user_of(request)
        with pool(request).connection() as conn:
            library = conn.execute(
                "SELECT d.id, d.title, d.source_type, d.source_url, d.pages, le.added_at, le.listened, "
                "le.last_listened_at FROM library_entries le JOIN documents d ON d.id = le.document_id "
                "WHERE le.user_id = %s ORDER BY le.added_at", (user["id"],)).fetchall()
            consents = conn.execute("SELECT version, accepted_at FROM user_consents WHERE user_id = %s",
                                    (user["id"],)).fetchall()
        summary = {
            "email": user["email"], "created_at": user["created_at"], "exported_at": datetime.now(timezone.utc),
            "consents": consents,
            "library": [{"title": r["title"], "source_type": r["source_type"], "source_url": r["source_url"],
                         "pages": r["pages"], "added_at": r["added_at"], "listened": r["listened"],
                         "last_listened_at": r["last_listened_at"],
                         "pdf": f"pdf/{r['id']}.pdf" if DocPaths(settings.docs_dir, r["id"]).source_pdf.exists()
                         else None} for r in library],
        }
        incoming = settings.data_dir / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=incoming, suffix=".zip", delete=False) as tmp:
            path = tmp.name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("account.json", json.dumps(summary, indent=2, default=str))
            for r in library:
                pdf = DocPaths(settings.docs_dir, r["id"]).source_pdf
                if pdf.exists():
                    archive.write(pdf, f"pdf/{r['id']}.pdf")
        return FileResponse(Path(path), media_type="application/zip", filename="listen-vault-export.zip",
                            background=BackgroundTask(Path(path).unlink, missing_ok=True))

    @app.delete("/api/account", status_code=204)
    def delete_account(request: Request, body: DeleteIn, response: Response):
        """Delete the account, its library entries and its settings. Papers that no other library holds are deleted."""
        user = user_of(request)
        with pool(request).connection() as conn, conn.transaction():
            if accounts.authenticate(conn, user["email"], body.password) is None:
                raise HTTPException(403, "The password is wrong")
            if user["is_admin"] and accounts.admin_count(conn) <= 1:
                raise HTTPException(409, "You are the only administrator. Make another account an administrator first.")
            conn.execute("DELETE FROM users WHERE id = %s", (user["id"],))
            documents.purge_orphans(conn, settings.docs_dir)
        response.delete_cookie(SESSION_COOKIE, path="/")
        log.info("account %s deleted", user["id"])
