"""Request authentication for the api.

Caddy forwards every request with the shared X-Listen-Proxy secret, so other containers on the same Docker
network cannot reach the api directly. Under /api, a request is served only with a live session cookie
(see listen.accounts), except for the few public paths below. State-changing requests must also carry
X-Requested-With, which a cross-site form cannot set.
"""
from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import accounts

SESSION_COOKIE = "listen_session"
PUBLIC_API = {
    "/api/health",          # Docker healthcheck (no secret, see compose.template.yml)
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/verify",
    "/api/terms",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "listen-vault"


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, proxy_secret: str):
        super().__init__(app)
        if not proxy_secret:
            raise RuntimeError("LISTEN_PROXY_SECRET must be set")
        self.secret = proxy_secret.encode()

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/api/health":
            return await call_next(request)
        secret = request.headers.get("x-listen-proxy", "").encode()
        if not hmac.compare_digest(secret, self.secret):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        if path.startswith("/api/") and request.method not in SAFE_METHODS \
                and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            return JSONResponse({"detail": "Missing X-Requested-With header"}, status_code=403)
        if not path.startswith("/api/") or path in PUBLIC_API:
            request.state.user = None
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        with request.app.state.pool.connection() as conn:
            user = accounts.session_user(conn, token)
        if user is None:
            return JSONResponse({"detail": "Not signed in"}, status_code=401)
        request.state.user = user
        return await call_next(request)
