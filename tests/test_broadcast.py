import json
import pytest
from unittest.mock import patch, AsyncMock

from app.broadcast import broadcast_order_update


@pytest.mark.asyncio
async def test_publishes_to_correct_channel():
    mock_redis = AsyncMock()
    mock_redis.publish = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with patch("app.broadcast.redis.from_url", return_value=mock_redis):
        await broadcast_order_update("new_order", {"order_id": 1})

    mock_redis.publish.assert_called_once()
    call_args = mock_redis.publish.call_args
    channel = call_args[0][0]
    message = call_args[0][1]

    assert channel == "dashboard_events"
    assert "new_order" in message
    assert "payload" in message


@pytest.mark.asyncio
async def test_message_format_contains_type_and_payload():
    mock_redis = AsyncMock()
    mock_redis.publish = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with patch("app.broadcast.redis.from_url", return_value=mock_redis):
        await broadcast_order_update("status_change", {"order_id": 42, "status": "paid"})

    call_args = mock_redis.publish.call_args
    message = call_args[0][1]

    assert "type" in message
    assert "payload" in message


@pytest.mark.asyncio
async def test_redis_failure_does_not_raise():
    mock_redis = AsyncMock()
    mock_redis.publish = AsyncMock(side_effect=Exception("Redis connection refused"))
    mock_redis.aclose = AsyncMock()

    with patch("app.broadcast.redis.from_url", return_value=mock_redis):
        # Should not raise any exception even when publish fails
        await broadcast_order_update("new_order", {"order_id": 99})


@pytest.mark.asyncio
async def test_connection_closed_after_publish():
    mock_redis = AsyncMock()
    mock_redis.publish = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with patch("app.broadcast.redis.from_url", return_value=mock_redis):
        await broadcast_order_update("new_order", {"order_id": 7})

    mock_redis.aclose.assert_called_once()


@pytest.mark.asyncio
async def test_published_message_is_valid_json():
    """The message published to Redis must be valid JSON, not Python str() repr."""
    mock_redis = AsyncMock()
    mock_redis.publish = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with patch("app.broadcast.redis.from_url", return_value=mock_redis):
        await broadcast_order_update("new_order", {"order_id": 1, "status": "paid"})

    call_args = mock_redis.publish.call_args
    raw_message = call_args[0][1]

    # Must not raise — the message must be valid JSON
    parsed = json.loads(raw_message)
    assert parsed["type"] == "new_order"
    assert parsed["payload"] == {"order_id": 1, "status": "paid"}


# ---------- Per-bot channel routing ----------

@pytest.mark.asyncio
async def test_publishes_to_bot_specific_channel():
    """When bot_id is provided, publish to dashboard_events:{bot_id}."""
    mock_redis = AsyncMock()
    mock_redis.publish = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with patch("app.broadcast.redis.from_url", return_value=mock_redis):
        await broadcast_order_update("new_order", {"order_id": 1}, bot_id=42)

    call_args = mock_redis.publish.call_args
    channel = call_args[0][0]
    assert channel == "dashboard_events:42"


@pytest.mark.asyncio
async def test_publishes_to_global_channel_without_bot_id():
    """When bot_id is None (default), publish to global dashboard_events."""
    mock_redis = AsyncMock()
    mock_redis.publish = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with patch("app.broadcast.redis.from_url", return_value=mock_redis):
        await broadcast_order_update("new_order", {"order_id": 1})

    call_args = mock_redis.publish.call_args
    channel = call_args[0][0]
    assert channel == "dashboard_events"
