"""Integration tests for cookie-based JWT authentication."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import (
    COOKIE_NAME,
    CSRF_COOKIE_NAME,
    get_current_user,
    router as auth_router,
)
from app.csrf import CSRFMiddleware
from app.database import get_session
from app.models import User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user() -> User:
    user = MagicMock(spec=User)
    user.id = 1
    user.email = "test@example.com"
    user.hashed_password = "$2b$12$dummyhash"
    user.is_admin = False
    return user


def _make_app(mock_session):
    """App with auth router + a protected test endpoint + CSRF middleware."""
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.include_router(auth_router, prefix="/auth")

    app.dependency_overrides[get_session] = lambda: mock_session

    @app.get("/protected")
    async def protected(current_user=Depends(get_current_user)):
        return {"user_id": current_user.id}

    @app.post("/protected-post")
    async def protected_post(current_user=Depends(get_current_user)):
        return {"user_id": current_user.id}

    return app


@pytest.fixture
def user():
    return _make_user()


@pytest.fixture
def mock_session():
    session = AsyncMock()
    return session


@pytest.fixture
def client(user, mock_session):
    app = _make_app(mock_session)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Login sets cookies
# ---------------------------------------------------------------------------


def test_login_sets_cookies(client, user, mock_session):
    with patch("app.auth.crud.get_user_by_email", new_callable=AsyncMock, return_value=user), \
         patch("app.auth.verify_password", return_value=True):
        resp = client.post(
            "/auth/token",
            data={"username": "test@example.com", "password": "Test1234!"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert "csrf_token" in body
    assert body["token_type"] == "bearer"

    # Check Set-Cookie headers
    cookies = resp.cookies
    assert COOKIE_NAME in cookies
    assert CSRF_COOKIE_NAME in cookies


# ---------------------------------------------------------------------------
# Cookie auth on GET (no CSRF needed)
# ---------------------------------------------------------------------------


def test_get_with_cookie_auth(client, user, mock_session):
    """GET endpoint works with cookie auth, no Bearer header needed."""
    from app.auth import create_access_token

    token = create_access_token(data={"sub": user.email})

    with patch("app.auth.crud.get_user_by_email", new_callable=AsyncMock, return_value=user):
        resp = client.get(
            "/protected",
            cookies={COOKIE_NAME: token},
        )

    assert resp.status_code == 200
    assert resp.json()["user_id"] == user.id


# ---------------------------------------------------------------------------
# Cookie auth on POST requires CSRF
# ---------------------------------------------------------------------------


def test_post_with_cookie_and_csrf(client, user, mock_session):
    """POST with cookie auth + valid X-CSRF-Token succeeds."""
    from app.auth import create_access_token

    token = create_access_token(data={"sub": user.email})
    csrf = "test-csrf-value"

    with patch("app.auth.crud.get_user_by_email", new_callable=AsyncMock, return_value=user):
        resp = client.post(
            "/protected-post",
            cookies={COOKIE_NAME: token, CSRF_COOKIE_NAME: csrf},
            headers={"X-CSRF-Token": csrf},
        )

    assert resp.status_code == 200


def test_post_with_cookie_missing_csrf_returns_403(client, user, mock_session):
    """POST with cookie auth but no CSRF header returns 403."""
    from app.auth import create_access_token

    token = create_access_token(data={"sub": user.email})

    resp = client.post(
        "/protected-post",
        cookies={COOKIE_NAME: token, CSRF_COOKIE_NAME: "some-csrf"},
    )

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Bearer-only auth still works without CSRF
# ---------------------------------------------------------------------------


def test_bearer_auth_without_csrf(client, user, mock_session):
    """Bearer-only POST works without CSRF header (no access_token cookie)."""
    from app.auth import create_access_token

    token = create_access_token(data={"sub": user.email})

    with patch("app.auth.crud.get_user_by_email", new_callable=AsyncMock, return_value=user):
        resp = client.post(
            "/protected-post",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert resp.json()["user_id"] == user.id


# ---------------------------------------------------------------------------
# Logout clears cookies
# ---------------------------------------------------------------------------


def test_logout_clears_cookies(client):
    resp = client.post(
        "/auth/logout",
        cookies={COOKIE_NAME: "old-jwt", CSRF_COOKIE_NAME: "old-csrf"},
    )

    assert resp.status_code == 200
    assert resp.json()["message"] == "Logged out successfully"

    # Cookies should be deleted (set to empty / max-age=0)
    set_cookies = resp.headers.get_list("set-cookie")
    cookie_str = " ".join(set_cookies)
    assert COOKIE_NAME in cookie_str
    assert CSRF_COOKIE_NAME in cookie_str


# ---------------------------------------------------------------------------
# Invalid cookie token returns 401
# ---------------------------------------------------------------------------


def test_invalid_cookie_token_returns_401(client, mock_session):
    resp = client.get(
        "/protected",
        cookies={COOKIE_NAME: "invalid-jwt-garbage"},
    )

    assert resp.status_code == 401
