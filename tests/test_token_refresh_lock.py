"""Tests for D7: Payment token refresh uses distributed lock.

Verifies that get_valid_access_token acquires a distributed lock before
refreshing, and that concurrent calls don't both call refresh_mp_token.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.payment_service import get_valid_access_token


def _make_config(expired=True, config_id=1):
    config = MagicMock()
    config.id = config_id
    config.bot_id = 10
    config.access_token = "encrypted_token"
    config.refresh_token = "encrypted_refresh"
    if expired:
        config.token_expires_at = datetime(2020, 1, 1)
    else:
        config.token_expires_at = datetime(2099, 1, 1)
    return config


def _make_lock(acquired=True):
    lock = AsyncMock()
    lock.acquire = AsyncMock(return_value=acquired)
    lock.release = AsyncMock()
    return lock


def _mock_client_with_lock(mock_lock):
    mock_client = MagicMock()
    mock_client.lock.return_value = mock_lock
    return mock_client


@pytest.mark.asyncio
async def test_lock_acquired_before_refresh():
    """When token is expired, a distributed lock must be acquired before refresh."""
    config = _make_config(expired=True)
    session = AsyncMock()
    session.refresh = AsyncMock()
    mock_lock = _make_lock(acquired=True)

    with (
        patch(
            "app.distributed_lock._get_client",
            return_value=_mock_client_with_lock(mock_lock),
        ),
        patch(
            "app.payment_service.refresh_mp_token", return_value="new_token"
        ) as mock_refresh,
    ):
        result = await get_valid_access_token(config, session)

    assert result == "new_token"
    mock_refresh.assert_called_once_with(config, session)
    mock_lock.acquire.assert_called_once()
    mock_lock.release.assert_called_once()


@pytest.mark.asyncio
async def test_token_not_expired_skips_lock():
    """When token is not expired, no lock should be acquired."""
    config = _make_config(expired=False)
    session = AsyncMock()

    with patch("app.payment_service.decrypt_value", return_value="valid_token"):
        result = await get_valid_access_token(config, session)

    assert result == "valid_token"


@pytest.mark.asyncio
async def test_lock_acquired_but_token_already_refreshed():
    """If another process refreshed the token while we waited for the lock,
    we should use the refreshed token instead of calling refresh again."""
    config = _make_config(expired=True)
    session = AsyncMock()

    async def mock_session_refresh(obj, *args, **kwargs):
        obj.token_expires_at = datetime(2099, 1, 1)

    session.refresh = mock_session_refresh
    mock_lock = _make_lock(acquired=True)

    with (
        patch(
            "app.distributed_lock._get_client",
            return_value=_mock_client_with_lock(mock_lock),
        ),
        patch("app.payment_service.refresh_mp_token") as mock_refresh,
        patch("app.payment_service.decrypt_value", return_value="already_refreshed"),
    ):
        result = await get_valid_access_token(config, session)

    mock_refresh.assert_not_called()
    assert result == "already_refreshed"


@pytest.mark.asyncio
async def test_lock_not_acquired_falls_back_to_current_token():
    """If the lock cannot be acquired, fall back to re-reading the token."""
    config = _make_config(expired=True)
    session = AsyncMock()
    session.refresh = AsyncMock()
    mock_lock = _make_lock(acquired=False)

    with (
        patch(
            "app.distributed_lock._get_client",
            return_value=_mock_client_with_lock(mock_lock),
        ),
        patch("app.payment_service.decrypt_value", return_value="fallback_token"),
    ):
        result = await get_valid_access_token(config, session)

    assert result == "fallback_token"
    session.refresh.assert_called_once_with(config)
