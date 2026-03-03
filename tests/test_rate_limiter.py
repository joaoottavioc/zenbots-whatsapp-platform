# tests/test_rate_limiter.py
"""
Async unit tests for the rate limiter.

All Redis interaction is mocked via AsyncMock — no real Redis required.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import redis.asyncio as redis


@pytest.fixture(autouse=True)
def mock_redis_client():
    """Patch _get_client to return a mocked Redis client with async pipeline."""
    mock_client = MagicMock()  # pipeline() is a sync method on the client

    # Pipeline: incr/ttl are sync (they queue), execute is async
    mock_pipe = MagicMock()
    mock_pipe.incr = MagicMock()
    mock_pipe.ttl = MagicMock()
    mock_pipe.execute = AsyncMock()
    mock_client.pipeline.return_value = mock_pipe
    mock_client.expire = AsyncMock()

    with patch("app.rate_limiter._get_client", return_value=mock_client):
        yield mock_client, mock_pipe


async def test_empty_phone_returns_false(mock_redis_client):
    from app.rate_limiter import is_spamming

    mock_client, _ = mock_redis_client
    result = await is_spamming("")
    assert result is False
    mock_client.pipeline.assert_not_called()


async def test_under_limit_returns_false(mock_redis_client):
    from app.rate_limiter import is_spamming

    _, mock_pipe = mock_redis_client
    mock_pipe.execute.return_value = [5, 30]

    result = await is_spamming("5511999999999")

    assert result is False
    mock_pipe.incr.assert_called_once_with("spam:5511999999999")
    mock_pipe.ttl.assert_called_once_with("spam:5511999999999")
    mock_pipe.execute.assert_awaited_once()


async def test_over_limit_returns_true(mock_redis_client):
    from app.rate_limiter import is_spamming

    _, mock_pipe = mock_redis_client
    mock_pipe.execute.return_value = [16, 30]

    result = await is_spamming("5511999999999")

    assert result is True


async def test_first_message_sets_ttl(mock_redis_client):
    from app.rate_limiter import is_spamming

    mock_client, mock_pipe = mock_redis_client
    mock_pipe.execute.return_value = [1, -1]

    result = await is_spamming("5511999999999", limit=15, window_seconds=60)

    assert result is False
    mock_client.expire.assert_awaited_once_with("spam:5511999999999", 60)


async def test_redis_failure_returns_true(mock_redis_client):
    """When Redis is down, fail closed (block) since ARQ worker also needs Redis."""
    from app.rate_limiter import is_spamming

    _, mock_pipe = mock_redis_client
    mock_pipe.execute.side_effect = redis.RedisError("connection refused")

    result = await is_spamming("5511999999999")

    assert result is True
