"""Tests for S7: Per-account rate limiting on login.

Verifies that the login endpoint rate-limits per email address in addition
to per IP, preventing targeted brute-force attacks on a single account.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import router
from app.database import get_session


@pytest.fixture
def mock_session():
    session = AsyncMock()
    return session


@pytest.fixture
def client(mock_session):
    app = FastAPI()
    app.include_router(router, prefix="/auth")
    app.dependency_overrides[get_session] = lambda: mock_session
    return TestClient(app)


LOGIN_FORM = {"username": "victim@example.com", "password": "WrongPass1!"}


class TestLoginPerEmailRateLimit:
    """Login must rate-limit per email to prevent targeted brute-force."""

    def test_email_rate_limit_triggers_429(self, client):
        """Exceeding per-email limit returns 429 even if IP limit is fine."""
        call_count = 0

        async def mock_is_rate_limited(key, limit, window_seconds):
            nonlocal call_count
            call_count += 1
            # IP-based limit (rl:login:) passes, email-based limit triggers
            if key.startswith("rl:login:email:"):
                return True  # email rate limited
            return False  # IP not rate limited

        with patch("app.auth.is_rate_limited", side_effect=mock_is_rate_limited):
            resp = client.post("/auth/token", data=LOGIN_FORM)

        assert resp.status_code == 429
        assert "account" in resp.json()["detail"].lower()

    def test_email_rate_limit_uses_lowercase_key(self, client, mock_session):
        """Email keys must be lowercased to prevent case-based bypass."""
        captured_keys = []

        async def mock_is_rate_limited(key, limit, window_seconds):
            captured_keys.append(key)
            return False

        mock_user = MagicMock()
        mock_user.hashed_password = "hashed"
        mock_user.is_email_verified = True
        mock_user.email = "User@Example.COM"

        with (
            patch("app.auth.is_rate_limited", side_effect=mock_is_rate_limited),
            patch("app.auth.crud.get_user_by_email", return_value=mock_user),
            patch("app.auth.verify_password", return_value=True),
            patch("app.auth.create_access_token", return_value="fake.jwt.token"),
        ):
            upper_form = {
                "username": "User@Example.COM",
                "password": "StrongPass1!",
            }
            client.post("/auth/token", data=upper_form)

        email_keys = [k for k in captured_keys if k.startswith("rl:login:email:")]
        assert len(email_keys) == 1
        assert email_keys[0] == "rl:login:email:user@example.com"

    def test_different_emails_get_separate_limits(self, client):
        """Two different emails should not share rate limit buckets."""
        triggered_keys = []

        async def mock_is_rate_limited(key, limit, window_seconds):
            triggered_keys.append(key)
            return False

        with (
            patch("app.auth.is_rate_limited", side_effect=mock_is_rate_limited),
            patch("app.auth.crud.get_user_by_email", return_value=None),
        ):
            client.post("/auth/token", data={"username": "a@x.com", "password": "P1!"})
            client.post("/auth/token", data={"username": "b@x.com", "password": "P1!"})

        email_keys = [k for k in triggered_keys if k.startswith("rl:login:email:")]
        assert "rl:login:email:a@x.com" in email_keys
        assert "rl:login:email:b@x.com" in email_keys
