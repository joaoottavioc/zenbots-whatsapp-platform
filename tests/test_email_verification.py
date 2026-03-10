"""Tests for email verification flow (registration, verify, resend, login gate)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.auth import login_for_access_token, register, verify_email, resend_verification
from app.auth import VerifyEmailRequest, ResendVerificationRequest, RegisterRequest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.refresh = AsyncMock()
    return session


@pytest.fixture
def mock_request():
    req = MagicMock()
    req.client.host = "127.0.0.1"
    return req


@pytest.fixture
def mock_response():
    from fastapi import Response

    return Response()


@pytest.fixture
def unverified_user():
    user = MagicMock()
    user.id = 1
    user.email = "new@example.com"
    user.hashed_password = "hashed"
    user.is_email_verified = False
    return user


@pytest.fixture
def verified_user():
    user = MagicMock()
    user.id = 2
    user.email = "verified@example.com"
    user.hashed_password = "hashed"
    user.is_email_verified = True
    return user


# ---------------------------------------------------------------------------
# Registration sends verification email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_sends_verification_email(mock_session, mock_response):
    """Registration should send a verification email."""
    register_data = RegisterRequest(email="new@example.com", password="StrongPass1!")

    with (
        patch(
            "app.auth.crud.get_user_by_email", new_callable=AsyncMock, return_value=None
        ),
        patch("app.auth.get_password_hash", return_value="hashed"),
        patch(
            "app.auth.store_email_verification_token",
            new_callable=AsyncMock,
            return_value="test-token",
        ) as mock_store,
        patch("app.auth.send_verification_email", new_callable=AsyncMock) as mock_send,
    ):
        result = await register(
            register_data=register_data, response=mock_response, session=mock_session
        )

    assert "message" in result
    mock_store.assert_called_once()
    mock_send.assert_called_once_with("new@example.com", "test-token")


@pytest.mark.asyncio
async def test_register_succeeds_even_if_email_fails(mock_session, mock_response):
    """Registration should succeed even if the verification email fails to send."""
    register_data = RegisterRequest(email="new@example.com", password="StrongPass1!")

    with (
        patch(
            "app.auth.crud.get_user_by_email", new_callable=AsyncMock, return_value=None
        ),
        patch("app.auth.get_password_hash", return_value="hashed"),
        patch(
            "app.auth.store_email_verification_token",
            new_callable=AsyncMock,
            side_effect=Exception("Redis down"),
        ),
    ):
        result = await register(
            register_data=register_data, response=mock_response, session=mock_session
        )

    # Registration still succeeds
    assert "message" in result


# ---------------------------------------------------------------------------
# Login gate: unverified users blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_blocked_when_email_not_verified(
    mock_request, mock_response, unverified_user, mock_session
):
    """Login should return 403 when email is not verified."""
    form = MagicMock()
    form.username = "new@example.com"
    form.password = "StrongPass1!"

    with (
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=False),
        patch(
            "app.auth.crud.get_user_by_email",
            new_callable=AsyncMock,
            return_value=unverified_user,
        ),
        patch("app.auth.verify_password", return_value=True),
    ):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await login_for_access_token(
                request=mock_request,
                response=mock_response,
                form_data=form,
                session=mock_session,
            )
        assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_login_allowed_when_email_verified(
    mock_request, mock_response, verified_user, mock_session
):
    """Login should succeed when email is verified."""
    form = MagicMock()
    form.username = "verified@example.com"
    form.password = "StrongPass1!"

    with (
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=False),
        patch(
            "app.auth.crud.get_user_by_email",
            new_callable=AsyncMock,
            return_value=verified_user,
        ),
        patch("app.auth.verify_password", return_value=True),
        patch("app.auth.create_access_token", return_value="fake-jwt"),
    ):
        result = await login_for_access_token(
            request=mock_request,
            response=mock_response,
            form_data=form,
            session=mock_session,
        )
    assert result["access_token"] == "fake-jwt"


# ---------------------------------------------------------------------------
# Verify email endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_email_success(mock_session, unverified_user):
    """Valid token should mark user as verified."""
    payload = VerifyEmailRequest(token="valid-token")

    with (
        patch(
            "app.auth.consume_email_verification_token",
            new_callable=AsyncMock,
            return_value=(1, "new@example.com"),
        ),
        patch(
            "app.auth.crud.get_user_by_id",
            new_callable=AsyncMock,
            return_value=unverified_user,
        ),
    ):
        result = await verify_email(payload=payload, session=mock_session)

    assert unverified_user.is_email_verified is True
    assert (
        "sucesso" in result["message"].lower()
        or "verificado" in result["message"].lower()
    )


@pytest.mark.asyncio
async def test_verify_email_invalid_token(mock_session):
    """Invalid/expired token should return 400."""
    payload = VerifyEmailRequest(token="bad-token")

    with patch(
        "app.auth.consume_email_verification_token",
        new_callable=AsyncMock,
        return_value=None,
    ):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await verify_email(payload=payload, session=mock_session)
        assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_verify_email_already_verified(mock_session, verified_user):
    """Already verified user should get idempotent success response."""
    payload = VerifyEmailRequest(token="valid-token")

    with (
        patch(
            "app.auth.consume_email_verification_token",
            new_callable=AsyncMock,
            return_value=(2, "verified@example.com"),
        ),
        patch(
            "app.auth.crud.get_user_by_id",
            new_callable=AsyncMock,
            return_value=verified_user,
        ),
    ):
        result = await verify_email(payload=payload, session=mock_session)

    assert "já verificado" in result["message"].lower()


@pytest.mark.asyncio
async def test_verify_email_user_email_mismatch(mock_session):
    """Token email must match user's current email."""
    payload = VerifyEmailRequest(token="valid-token")
    user = MagicMock()
    user.id = 1
    user.email = "changed@example.com"

    with (
        patch(
            "app.auth.consume_email_verification_token",
            new_callable=AsyncMock,
            return_value=(1, "old@example.com"),
        ),
        patch(
            "app.auth.crud.get_user_by_id", new_callable=AsyncMock, return_value=user
        ),
    ):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await verify_email(payload=payload, session=mock_session)
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Resend verification endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resend_verification_sends_email(
    mock_request, mock_session, unverified_user
):
    """Resend should send a new verification email for unverified users."""
    payload = ResendVerificationRequest(email="new@example.com")

    with (
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=False),
        patch(
            "app.auth.crud.get_user_by_email",
            new_callable=AsyncMock,
            return_value=unverified_user,
        ),
        patch(
            "app.auth.store_email_verification_token",
            new_callable=AsyncMock,
            return_value="new-token",
        ) as mock_store,
        patch("app.auth.send_verification_email", new_callable=AsyncMock) as mock_send,
    ):
        await resend_verification(
            payload=payload, request=mock_request, session=mock_session
        )

    mock_store.assert_called_once()
    mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_resend_verification_already_verified_does_not_reveal(
    mock_request, mock_session, verified_user
):
    """Resend for verified user should not reveal the user's status."""
    payload = ResendVerificationRequest(email="verified@example.com")

    with (
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=False),
        patch(
            "app.auth.crud.get_user_by_email",
            new_callable=AsyncMock,
            return_value=verified_user,
        ),
        patch("app.auth.send_verification_email", new_callable=AsyncMock) as mock_send,
    ):
        result = await resend_verification(
            payload=payload, request=mock_request, session=mock_session
        )

    # Should NOT send email but return same generic message
    mock_send.assert_not_called()
    assert "se o e-mail existir" in result["message"].lower()


@pytest.mark.asyncio
async def test_resend_verification_nonexistent_user(mock_request, mock_session):
    """Resend for nonexistent email should not reveal user doesn't exist."""
    payload = ResendVerificationRequest(email="nobody@example.com")

    with (
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=False),
        patch(
            "app.auth.crud.get_user_by_email", new_callable=AsyncMock, return_value=None
        ),
        patch("app.auth.send_verification_email", new_callable=AsyncMock) as mock_send,
    ):
        result = await resend_verification(
            payload=payload, request=mock_request, session=mock_session
        )

    mock_send.assert_not_called()
    assert "se o e-mail existir" in result["message"].lower()


@pytest.mark.asyncio
async def test_resend_verification_rate_limited(
    mock_request, mock_session, unverified_user
):
    """Resend should return 429 when rate limited per email."""
    payload = ResendVerificationRequest(email="new@example.com")

    with (
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=True),
    ):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await resend_verification(
                payload=payload, request=mock_request, session=mock_session
            )
        assert exc_info.value.status_code == 429
