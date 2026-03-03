"""Tests for authentication failure logging (V7)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.auth import login_for_access_token


@pytest.fixture
def mock_request():
    req = MagicMock()
    req.client.host = "192.168.1.1"
    return req


@pytest.fixture
def mock_form_data():
    form = MagicMock()
    form.username = "bad@example.com"
    form.password = "wrongpassword"
    return form


@pytest.fixture
def mock_session():
    return AsyncMock()


@pytest.mark.asyncio
async def test_failed_login_logs_warning(mock_request, mock_form_data, mock_session):
    """Failed login should log AUTH_FAIL with email and IP."""
    with (
        patch(
            "app.auth.crud.get_user_by_email", new_callable=AsyncMock, return_value=None
        ),
        patch("app.auth.logger") as mock_logger,
    ):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await login_for_access_token(
                request=mock_request,
                form_data=mock_form_data,
                session=mock_session,
            )
        assert exc_info.value.status_code == 401
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert "AUTH_FAIL" in call_args[0][0]
        assert "bad@example.com" in call_args[0]
        assert "192.168.1.1" in call_args[0]


@pytest.mark.asyncio
async def test_failed_login_wrong_password_logs_warning(
    mock_request, mock_form_data, mock_session
):
    """Wrong password should also log AUTH_FAIL."""
    fake_user = MagicMock()
    fake_user.hashed_password = "some_hash"
    with (
        patch(
            "app.auth.crud.get_user_by_email",
            new_callable=AsyncMock,
            return_value=fake_user,
        ),
        patch("app.auth.verify_password", return_value=False),
        patch("app.auth.logger") as mock_logger,
    ):
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            await login_for_access_token(
                request=mock_request,
                form_data=mock_form_data,
                session=mock_session,
            )
        mock_logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_successful_login_does_not_log(mock_request, mock_session):
    """Successful login should NOT log AUTH_FAIL."""
    fake_user = MagicMock()
    fake_user.hashed_password = "some_hash"
    fake_user.email = "ok@example.com"

    form = MagicMock()
    form.username = "ok@example.com"
    form.password = "Correct1"

    with (
        patch(
            "app.auth.crud.get_user_by_email",
            new_callable=AsyncMock,
            return_value=fake_user,
        ),
        patch("app.auth.verify_password", return_value=True),
        patch("app.auth.create_access_token", return_value="fake-jwt"),
        patch("app.auth.logger") as mock_logger,
    ):
        result = await login_for_access_token(
            request=mock_request,
            form_data=form,
            session=mock_session,
        )
        assert result["access_token"] == "fake-jwt"
        mock_logger.warning.assert_not_called()
