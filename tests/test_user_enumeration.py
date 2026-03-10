"""Tests for S9: User enumeration prevention on /auth/register.

Verifies that the registration endpoint returns the same HTTP status code
and message body regardless of whether the email already exists.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import router
from app.database import get_session


VALID_PAYLOAD = {"email": "test@example.com", "password": "StrongPass1!"}
EXPECTED_MESSAGE = "If this email is available, a verification link has been sent."


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


@pytest.fixture
def client(mock_session):
    app = FastAPI()
    app.include_router(router, prefix="/auth")
    app.dependency_overrides[get_session] = lambda: mock_session
    return TestClient(app)


class TestUserEnumerationPrevention:
    """Registration must not reveal whether an email is already in use."""

    def test_register_new_user_returns_201_with_generic_message(
        self, client, mock_session
    ):
        """A brand-new email should return 201 with the generic message."""
        with (
            patch("app.auth.crud.get_user_by_email", return_value=None),
            patch("app.auth.is_rate_limited", return_value=False),
            patch("app.auth.get_password_hash", return_value="hashed"),
            patch("app.auth.store_email_verification_token", return_value="tok"),
            patch("app.auth.send_verification_email", return_value=None),
        ):
            resp = client.post("/auth/register", json=VALID_PAYLOAD)

        assert resp.status_code == 201
        assert resp.json()["message"] == EXPECTED_MESSAGE

    def test_register_existing_user_returns_201_with_same_message(
        self, client, mock_session
    ):
        """An already-registered email must return the same 201 + message (no leak)."""
        existing_user = MagicMock()
        existing_user.id = 1
        existing_user.email = "test@example.com"

        with (
            patch("app.auth.crud.get_user_by_email", return_value=existing_user),
            patch("app.auth.is_rate_limited", return_value=False),
        ):
            resp = client.post("/auth/register", json=VALID_PAYLOAD)

        assert resp.status_code == 201
        assert resp.json()["message"] == EXPECTED_MESSAGE

    def test_responses_are_indistinguishable(self, client, mock_session):
        """Both paths must produce identical status codes and message bodies."""
        # Path 1: new user
        with (
            patch("app.auth.crud.get_user_by_email", return_value=None),
            patch("app.auth.is_rate_limited", return_value=False),
            patch("app.auth.get_password_hash", return_value="hashed"),
            patch("app.auth.store_email_verification_token", return_value="tok"),
            patch("app.auth.send_verification_email", return_value=None),
        ):
            resp_new = client.post("/auth/register", json=VALID_PAYLOAD)

        # Path 2: existing user
        existing_user = MagicMock()
        existing_user.id = 1
        with (
            patch("app.auth.crud.get_user_by_email", return_value=existing_user),
            patch("app.auth.is_rate_limited", return_value=False),
        ):
            resp_existing = client.post("/auth/register", json=VALID_PAYLOAD)

        assert resp_new.status_code == resp_existing.status_code
        assert resp_new.json() == resp_existing.json()

    def test_old_error_message_not_present(self, client, mock_session):
        """The old 'Email already registered' message must never appear."""
        existing_user = MagicMock()

        with (
            patch("app.auth.crud.get_user_by_email", return_value=existing_user),
            patch("app.auth.is_rate_limited", return_value=False),
        ):
            resp = client.post("/auth/register", json=VALID_PAYLOAD)

        assert "already registered" not in resp.text.lower()
        assert resp.status_code != 400
