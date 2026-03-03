# tests/test_auth_rate_limit.py
"""
Tests for the generic is_rate_limited function and the auth rate-limit dependencies.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import redis.asyncio as redis
from fastapi import HTTPException


# ===========================================================================
# is_rate_limited
# ===========================================================================


@pytest.fixture()
def mock_redis():
    """Patch _get_client to return a mocked Redis client with async pipeline."""
    mock_client = MagicMock()
    mock_pipe = MagicMock()
    mock_pipe.incr = MagicMock()
    mock_pipe.ttl = MagicMock()
    mock_pipe.execute = AsyncMock()
    mock_client.pipeline.return_value = mock_pipe
    mock_client.expire = AsyncMock()

    with patch("app.rate_limiter._get_client", return_value=mock_client):
        yield mock_client, mock_pipe


async def test_under_limit_returns_false(mock_redis):
    from app.rate_limiter import is_rate_limited

    _, mock_pipe = mock_redis
    mock_pipe.execute.return_value = [3, 200]

    result = await is_rate_limited("rl:test:127.0.0.1", limit=5, window_seconds=300)
    assert result is False


async def test_over_limit_returns_true(mock_redis):
    from app.rate_limiter import is_rate_limited

    _, mock_pipe = mock_redis
    mock_pipe.execute.return_value = [6, 200]

    result = await is_rate_limited("rl:test:127.0.0.1", limit=5, window_seconds=300)
    assert result is True


async def test_redis_failure_returns_true_fail_closed(mock_redis):
    """Redis failure must fail closed (return True = block)."""
    from app.rate_limiter import is_rate_limited

    _, mock_pipe = mock_redis
    mock_pipe.execute.side_effect = redis.RedisError("connection refused")

    result = await is_rate_limited("rl:test:127.0.0.1", limit=5, window_seconds=300)
    assert result is True


# ===========================================================================
# Auth rate limit dependencies
# ===========================================================================


def _make_request(ip="127.0.0.1"):
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = ip
    return req


async def test_auth_rate_limit_raises_429_when_over_limit():
    from app.auth import _check_auth_rate_limit

    with patch("app.auth.is_rate_limited", new=AsyncMock(return_value=True)):
        with pytest.raises(HTTPException) as exc_info:
            await _check_auth_rate_limit(_make_request())
        assert exc_info.value.status_code == 429


async def test_auth_rate_limit_passes_when_under_limit():
    from app.auth import _check_auth_rate_limit

    with patch("app.auth.is_rate_limited", new=AsyncMock(return_value=False)):
        # Should not raise
        await _check_auth_rate_limit(_make_request())


async def test_login_rate_limit_raises_429_when_over_limit():
    from app.auth import _check_login_rate_limit

    with patch("app.auth.is_rate_limited", new=AsyncMock(return_value=True)):
        with pytest.raises(HTTPException) as exc_info:
            await _check_login_rate_limit(_make_request())
        assert exc_info.value.status_code == 429


async def test_login_rate_limit_passes_when_under_limit():
    from app.auth import _check_login_rate_limit

    with patch("app.auth.is_rate_limited", new=AsyncMock(return_value=False)):
        await _check_login_rate_limit(_make_request())
