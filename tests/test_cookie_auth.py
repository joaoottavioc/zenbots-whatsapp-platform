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
    with (
        patch(
            "app.auth.crud.get_user_by_email", new_callable=AsyncMock, return_value=user
        ),
        patch("app.auth.verify_password", return_value=True),
    ):
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

    with patch(
        "app.auth.crud.get_user_by_email", new_callable=AsyncMock, return_value=user
    ):
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

    with patch(
        "app.auth.crud.get_user_by_email", new_callable=AsyncMock, return_value=user
    ):
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

    with patch(
        "app.auth.crud.get_user_by_email", new_callable=AsyncMock, return_value=user
    ):
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


# ---------------------------------------------------------------------------
# Cookie domain tests
# ---------------------------------------------------------------------------


def test_login_sets_cookie_domain_when_configured(user, mock_session):
    """Login sets Domain=.zenbotz.com.br when COOKIE_DOMAIN is configured."""
    with (
        patch("app.auth.COOKIE_DOMAIN", ".zenbotz.com.br"),
        patch("app.auth._is_secure_cookie", return_value=True),
        patch(
            "app.auth.crud.get_user_by_email", new_callable=AsyncMock, return_value=user
        ),
        patch("app.auth.verify_password", return_value=True),
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=False),
    ):
        app = _make_app(mock_session)
        client = TestClient(app)
        resp = client.post(
            "/auth/token",
            data={"username": "test@example.com", "password": "Test1234!"},
        )

    assert resp.status_code == 200
    set_cookies = resp.headers.get_list("set-cookie")
    cookie_str = " ".join(set_cookies)
    assert "domain=.zenbotz.com.br" in cookie_str.lower()


def test_login_omits_domain_when_not_configured(user, mock_session):
    """Login omits Domain attribute when COOKIE_DOMAIN is not set."""
    with (
        patch("app.auth.COOKIE_DOMAIN", None),
        patch(
            "app.auth.crud.get_user_by_email", new_callable=AsyncMock, return_value=user
        ),
        patch("app.auth.verify_password", return_value=True),
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=False),
    ):
        app = _make_app(mock_session)
        client = TestClient(app)
        resp = client.post(
            "/auth/token",
            data={"username": "test@example.com", "password": "Test1234!"},
        )

    assert resp.status_code == 200
    set_cookies = resp.headers.get_list("set-cookie")
    cookie_str = " ".join(set_cookies)
    assert "domain=" not in cookie_str.lower()


def test_logout_includes_domain_when_configured(mock_session):
    """Logout delete_cookie includes domain when COOKIE_DOMAIN is set."""
    with patch("app.auth.COOKIE_DOMAIN", ".zenbotz.com.br"):
        app = _make_app(mock_session)
        client = TestClient(app)
        resp = client.post(
            "/auth/logout",
            cookies={COOKIE_NAME: "old-jwt", CSRF_COOKIE_NAME: "old-csrf"},
        )

    assert resp.status_code == 200
    set_cookies = resp.headers.get_list("set-cookie")
    cookie_str = " ".join(set_cookies)
    assert "domain=.zenbotz.com.br" in cookie_str.lower()


# ---------------------------------------------------------------------------
# _is_secure_cookie tests
# ---------------------------------------------------------------------------


def test_is_secure_cookie_true_when_cookie_domain_set():
    """_is_secure_cookie returns True when COOKIE_DOMAIN is set + ENVIRONMENT=development."""
    from app.auth import _is_secure_cookie

    with (
        patch("app.auth.COOKIE_SAMESITE", "lax"),
        patch("app.auth.COOKIE_DOMAIN", ".zenbotz.com.br"),
        patch.dict("os.environ", {"ENVIRONMENT": "development"}),
    ):
        assert _is_secure_cookie() is True


def test_is_secure_cookie_false_in_pure_local_dev():
    """_is_secure_cookie returns False in pure local dev (no domain, no samesite=none)."""
    from app.auth import _is_secure_cookie

    with (
        patch("app.auth.COOKIE_SAMESITE", "lax"),
        patch("app.auth.COOKIE_DOMAIN", None),
        patch.dict("os.environ", {"ENVIRONMENT": "development"}),
    ):
        assert _is_secure_cookie() is False
