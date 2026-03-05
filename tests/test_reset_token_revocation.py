# tests/test_reset_token_revocation.py
"""
Tests for JWT password reset token single-use revocation.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from jose import jwt


SECRET_KEY = "test-secret-key-for-testing"
ALGORITHM = "HS256"


def _make_reset_token(email="test@example.com", jti="unique-jti-123", token_type="reset"):
    from app.time import utcnow
    from datetime import timedelta

    payload = {"sub": email, "type": token_type, "exp": utcnow() + timedelta(minutes=15)}
    if jti:
        payload["jti"] = jti
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


@pytest.mark.asyncio
async def test_used_token_rejected_with_400():
    """A reset token that has already been used should be rejected."""
    token = _make_reset_token(jti="already-used-jti")

    with (
        patch("app.auth.SECRET_KEY", SECRET_KEY),
        patch("app.auth.is_reset_token_used", new_callable=AsyncMock, return_value=True),
        patch("app.auth.mark_reset_token_used", new_callable=AsyncMock),
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=False),
    ):
        from app.auth import reset_password, ResetPasswordRequest
        from fastapi import HTTPException

        mock_session = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await reset_password(
                payload=ResetPasswordRequest(token=token, new_password="NewPass1!"),
                session=mock_session,
            )

        assert exc_info.value.status_code == 400
        assert "já foi utilizado" in exc_info.value.detail


@pytest.mark.asyncio
async def test_token_marked_used_after_success():
    """After successful reset, the token JTI is marked as used."""
    token = _make_reset_token(jti="fresh-jti-456")

    mock_user = MagicMock()
    mock_user.hashed_password = "oldhash"

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = mock_user
    mock_session.execute = AsyncMock(return_value=mock_result)

    with (
        patch("app.auth.SECRET_KEY", SECRET_KEY),
        patch("app.auth.is_reset_token_used", new_callable=AsyncMock, return_value=False),
        patch("app.auth.mark_reset_token_used", new_callable=AsyncMock) as mock_mark,
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=False),
        patch("app.auth.get_password_hash", return_value="newhash"),
    ):
        from app.auth import reset_password, ResetPasswordRequest

        result = await reset_password(
            payload=ResetPasswordRequest(token=token, new_password="NewPass1!"),
            session=mock_session,
        )

        assert result["message"] == "Senha atualizada com sucesso!"
        mock_mark.assert_awaited_once_with("fresh-jti-456", ttl=900)


@pytest.mark.asyncio
async def test_token_without_jti_still_works():
    """Tokens without JTI (backward compat) should still work normally."""
    token = _make_reset_token(jti=None)

    mock_user = MagicMock()
    mock_user.hashed_password = "oldhash"

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = mock_user
    mock_session.execute = AsyncMock(return_value=mock_result)

    with (
        patch("app.auth.SECRET_KEY", SECRET_KEY),
        patch("app.auth.is_reset_token_used", new_callable=AsyncMock, return_value=False) as mock_check,
        patch("app.auth.mark_reset_token_used", new_callable=AsyncMock) as mock_mark,
        patch("app.auth.is_rate_limited", new_callable=AsyncMock, return_value=False),
        patch("app.auth.get_password_hash", return_value="newhash"),
    ):
        from app.auth import reset_password, ResetPasswordRequest

        result = await reset_password(
            payload=ResetPasswordRequest(token=token, new_password="NewPass1!"),
            session=mock_session,
        )

        assert result["message"] == "Senha atualizada com sucesso!"
        # Without JTI, revocation helpers should not be called
        mock_check.assert_not_awaited()
        mock_mark.assert_not_awaited()
