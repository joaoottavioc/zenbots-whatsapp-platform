import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

_CSRF_DISABLED = os.getenv("CSRF_DISABLED", "").lower() in ("1", "true", "yes")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

EXEMPT_PATHS = {
    "/auth/token",
    "/auth/google",
    "/auth/register",
    "/auth/forgot-password",
    "/auth/reset-password",
    "/auth/logout",
}

EXEMPT_PREFIXES = (
    "/webhook",
    "/payments/webhooks/",
    "/billing/webhook",
    "/health/",
)


class CSRFMiddleware:
    """Double-submit cookie CSRF protection (pure ASGI).

    Only enforced when an ``access_token`` cookie is present (i.e. the
    request uses cookie-based auth).  Bearer-only API clients and webhook
    callbacks are unaffected.

    Implemented as a pure ASGI middleware (not BaseHTTPMiddleware) to avoid
    the known Starlette bug where BaseHTTPMiddleware's call_next wrapper
    swallows CORS headers on error/empty-body responses.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if _CSRF_DISABLED:
            await self.app(scope, receive, send)
            return

        request = Request(scope)

        if request.method in SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        path = request.url.path
        if path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Only enforce CSRF when cookie auth is in use
        access_cookie = request.cookies.get("access_token")
        if not access_cookie:
            await self.app(scope, receive, send)
            return

        csrf_cookie = request.cookies.get("csrf_token")
        csrf_header = request.headers.get("x-csrf-token")

        if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
            logger.warning(
                "CSRF_FAIL path=%s cookie_present=%s header_present=%s",
                path,
                bool(csrf_cookie),
                bool(csrf_header),
            )
            response = JSONResponse(
                status_code=403,
                content={"detail": "CSRF token missing or invalid"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
