"""Tests for Google Sign-In (POST /auth/google) and its interaction with
password auth for Google-only accounts.

Mirrors the app-building pattern in tests/test_cookie_auth.py: a minimal
FastAPI app wrapping the real auth router, with get_session overridden by a
mock session and crud/Google verification patched per test.
"""

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


def _make_user(
    *, google_sub=None, hashed_password="$2b$12$dummyhash", is_email_verified=True
):
    user = MagicMock(spec=User)
    user.id = 1
    user.email = "test@example.com"
    user.hashed_password = hashed_password
    user.google_sub = google_sub
    user.is_admin = False
    user.is_email_verified = is_email_verified
    return user


def _make_app(mock_session):
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.include_router(auth_router, prefix="/auth")
    app.dependency_overrides[get_session] = lambda: mock_session

    @app.post("/protected-post")
    async def protected_post(current_user=Depends(get_current_user)):
        return {"user_id": current_user.id}

    return app


@pytest.fixture
def mock_session():
    return AsyncMock()


@pytest.fixture
def client(mock_session):
    return TestClient(_make_app(mock_session))


GOOGLE_CLAIMS = {
    "sub": "google-sub-123",
    "email": "test@example.com",
    "email_verified": True,
}


def _patched(**overrides):
    """Common patch set for a successful Google verification call."""
    defaults = dict(
        verify_oauth2_token=MagicMock(return_value=dict(GOOGLE_CLAIMS)),
        is_rate_limited=AsyncMock(return_value=False),
        client_id="test-google-client-id",
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# New user via Google
# ---------------------------------------------------------------------------


def test_google_login_creates_new_user(client, mock_session):
    p = _patched()
    with (
        patch("app.auth.GOOGLE_CLIENT_ID", p["client_id"]),
        patch("app.auth.google_id_token.verify_oauth2_token", p["verify_oauth2_token"]),
        patch("app.auth.is_rate_limited", p["is_rate_limited"]),
        patch(
            "app.auth.crud.get_user_by_google_sub",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.auth.crud.get_user_by_email", new_callable=AsyncMock, return_value=None
        ),
    ):
        resp = client.post("/auth/google", json={"credential": "fake-jwt"})

    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body and "csrf_token" in body
    assert COOKIE_NAME in resp.cookies
    assert CSRF_COOKIE_NAME in resp.cookies
    added_user = mock_session.add.call_args[0][0]
    assert added_user.email == GOOGLE_CLAIMS["email"]
    assert added_user.google_sub == GOOGLE_CLAIMS["sub"]


def test_google_login_found_by_sub_reuses_existing_user(client, mock_session):
    existing = _make_user(google_sub=GOOGLE_CLAIMS["sub"])
    p = _patched()
    with (
        patch("app.auth.GOOGLE_CLIENT_ID", p["client_id"]),
        patch("app.auth.google_id_token.verify_oauth2_token", p["verify_oauth2_token"]),
        patch("app.auth.is_rate_limited", p["is_rate_limited"]),
        patch(
            "app.auth.crud.get_user_by_google_sub",
            new_callable=AsyncMock,
            return_value=existing,
        ),
        patch(
            "app.auth.crud.get_user_by_email", new_callable=AsyncMock
        ) as mock_by_email,
    ):
        resp = client.post("/auth/google", json={"credential": "fake-jwt"})

    assert resp.status_code == 200
    mock_by_email.assert_not_called()  # already found by google_sub, no email lookup needed


def test_google_login_auto_links_existing_password_account(client, mock_session):
    """A user who signed up with email/password gets linked on first Google login."""
    existing = _make_user(google_sub=None, is_email_verified=False)
    p = _patched()
    with (
        patch("app.auth.GOOGLE_CLIENT_ID", p["client_id"]),
        patch("app.auth.google_id_token.verify_oauth2_token", p["verify_oauth2_token"]),
        patch("app.auth.is_rate_limited", p["is_rate_limited"]),
        patch(
            "app.auth.crud.get_user_by_google_sub",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.auth.crud.get_user_by_email",
            new_callable=AsyncMock,
            return_value=existing,
        ),
    ):
        resp = client.post("/auth/google", json={"credential": "fake-jwt"})

    assert resp.status_code == 200
    assert existing.google_sub == GOOGLE_CLAIMS["sub"]
    assert existing.is_email_verified is True  # Google already verified it


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def test_google_login_rejects_unverified_email(client, mock_session):
    claims = dict(GOOGLE_CLAIMS, email_verified=False)
    with (
        patch("app.auth.GOOGLE_CLIENT_ID", "test-google-client-id"),
        patch(
            "app.auth.google_id_token.verify_oauth2_token",
            MagicMock(return_value=claims),
        ),
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=False),
    ):
        resp = client.post("/auth/google", json={"credential": "fake-jwt"})

    assert resp.status_code == 401


def test_google_login_rejects_invalid_token(client, mock_session):
    with (
        patch("app.auth.GOOGLE_CLIENT_ID", "test-google-client-id"),
        patch(
            "app.auth.google_id_token.verify_oauth2_token",
            MagicMock(side_effect=ValueError("bad signature")),
        ),
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=False),
    ):
        resp = client.post("/auth/google", json={"credential": "garbage"})

    assert resp.status_code == 401


def test_google_login_returns_503_when_not_configured(client, mock_session):
    with (
        patch("app.auth.GOOGLE_CLIENT_ID", None),
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=False),
    ):
        resp = client.post("/auth/google", json={"credential": "fake-jwt"})

    assert resp.status_code == 503


def test_google_login_rate_limited(client, mock_session):
    with (
        patch("app.auth.GOOGLE_CLIENT_ID", "test-google-client-id"),
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=True),
    ):
        resp = client.post("/auth/google", json={"credential": "fake-jwt"})

    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Password login / change-password guards for Google-only accounts
# ---------------------------------------------------------------------------


def test_password_login_fails_cleanly_for_google_only_account(client, mock_session):
    """A Google-only user (no hashed_password) attempting password login gets
    a clean 401, not a crash inside verify_password(None)."""
    google_only_user = _make_user(hashed_password=None, google_sub="google-sub-123")
    with (
        patch(
            "app.auth.crud.get_user_by_email",
            new_callable=AsyncMock,
            return_value=google_only_user,
        ),
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=False),
    ):
        resp = client.post(
            "/auth/token",
            data={"username": "test@example.com", "password": "whatever"},
        )

    assert resp.status_code == 401


def test_change_password_sets_first_password_for_google_only_account(
    client, mock_session
):
    """Google-only users have no current password to verify against — the
    endpoint should let them set their first password directly."""
    from app.auth import create_access_token

    google_only_user = _make_user(hashed_password=None, google_sub="google-sub-123")
    token = create_access_token(data={"sub": google_only_user.email})
    csrf = "test-csrf-value"

    with (
        patch(
            "app.auth.crud.get_user_by_email",
            new_callable=AsyncMock,
            return_value=google_only_user,
        ),
        # Real bcrypt is broken under local Python 3.14 (see CLAUDE.md /
        # TestPasswordHashing) — mock it out like the other endpoint tests
        # in this suite do, since we're testing the null-password branch,
        # not bcrypt itself.
        patch("app.auth.get_password_hash", return_value="$2b$12$newhash"),
    ):
        resp = client.post(
            "/auth/change-password",
            json={"current_password": "irrelevant", "new_password": "NewStrongPass1!"},
            cookies={COOKIE_NAME: token, CSRF_COOKIE_NAME: csrf},
            headers={"X-CSRF-Token": csrf},
        )

    assert resp.status_code == 200
    assert google_only_user.hashed_password == "$2b$12$newhash"
