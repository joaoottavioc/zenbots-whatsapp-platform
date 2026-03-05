import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_CSRF_DISABLED = os.getenv("CSRF_DISABLED", "").lower() in ("1", "true", "yes")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

EXEMPT_PATHS = {
    "/auth/token",
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


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit cookie CSRF protection.

    Only enforced when an ``access_token`` cookie is present (i.e. the
    request uses cookie-based auth).  Bearer-only API clients and webhook
    callbacks are unaffected.
    """

    async def dispatch(self, request: Request, call_next):
        if _CSRF_DISABLED:
            return await call_next(request)

        if request.method in SAFE_METHODS:
            return await call_next(request)

        path = request.url.path
        if path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)

        # Only enforce CSRF when cookie auth is in use
        access_cookie = request.cookies.get("access_token")
        if not access_cookie:
            return await call_next(request)

        csrf_cookie = request.cookies.get("csrf_token")
        csrf_header = request.headers.get("x-csrf-token")

        if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
            logger.warning(
                "CSRF_FAIL path=%s cookie_present=%s header_present=%s",
                path,
                bool(csrf_cookie),
                bool(csrf_header),
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF token missing or invalid"},
            )

        return await call_next(request)
