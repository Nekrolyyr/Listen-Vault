"""Request authentication for the api.

The login proxy (Caddy, see docker-compose.yml) checks the login first and only forwards authenticated
requests, adding the Remote-User header (the logged-in user) and X-Listen-Proxy (a shared secret).
Other containers on the same Docker network could reach the api directly, so both headers are required.
State-changing requests must also carry X-Requested-With, which a cross-site form cannot set.
"""
from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

OPEN_PATHS = {"/api/health"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "listen-vault"


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, user: str, proxy_secret: str):
        super().__init__(app)
        if not user or not proxy_secret:
            raise RuntimeError("LISTEN_USER and LISTEN_PROXY_SECRET must be set")
        self.user = user.encode()
        self.secret = proxy_secret.encode()

    async def dispatch(self, request: Request, call_next):
        if request.url.path in OPEN_PATHS:
            return await call_next(request)
        secret = request.headers.get("x-listen-proxy", "").encode()
        user = request.headers.get("remote-user", "").encode()
        if not (hmac.compare_digest(secret, self.secret) and hmac.compare_digest(user, self.user)):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            return JSONResponse({"detail": "Missing X-Requested-With header"}, status_code=403)
        request.state.user = user.decode()
        return await call_next(request)
