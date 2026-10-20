"""Helpers the api routes share: the database pool, the signed-in user, and the admin rule."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request


def pool(request: Request):
    return request.app.state.pool


def user_of(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if user is None:  # only public routes reach here without a session, and they do not ask
        raise HTTPException(401, "Not signed in")
    return user


def uid(request: Request) -> int:
    return user_of(request)["id"]


def admin_only(request: Request) -> dict[str, Any]:
    user = user_of(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Only an administrator can do this")
    return user


def client_ip(request: Request) -> str:
    """The address of the browser: Caddy adds it as X-Forwarded-For (only Caddy can reach the api)."""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")
