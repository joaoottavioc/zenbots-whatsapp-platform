"""Web channel egress module (plan/in_browser_bots.md Phase 1.5).

Covers the Redis PubSub publishing primitives used by MessageContext.reply()
for channel='web', and verifies the failure-tolerance contract: egress
errors are logged and swallowed so a downstream handler does not crash
the worker on a transient Redis hiccup.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.web_channel import (
    _channel,
    broadcast_typing_indicator,
    broadcast_web_event,
    broadcast_web_reply,
)


def test_channel_name_format():
    """Channel namespace is `chat:{bot_id}:{session_id}` — disjoint from
    the existing `dashboard_events:{bot_id}` namespace used by broadcast.py."""
    assert _channel(42, "abc-123") == "chat:42:abc-123"
    assert not _channel(42, "abc-123").startswith("dashboard_events")


@pytest.mark.asyncio
async def test_broadcast_web_event_publishes_envelope():
    """The published payload is a JSON envelope {type, payload}, mirroring
    broadcast.py's shape so the SSE consumer can use one decoder."""
    mock_redis = MagicMock()
    mock_redis.publish = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with patch("app.web_channel.redis.from_url", return_value=mock_redis):
        await broadcast_web_event(
            bot_id=42,
            session_id="abc-123",
            event_type="payment_qr",
            payload={"qr_data_url": "data:image/png;base64,xxx"},
        )

    mock_redis.publish.assert_awaited_once()
    channel_arg, body_arg = mock_redis.publish.await_args.args
    assert channel_arg == "chat:42:abc-123"
    envelope = json.loads(body_arg)
    assert envelope == {
        "type": "payment_qr",
        "payload": {"qr_data_url": "data:image/png;base64,xxx"},
    }


@pytest.mark.asyncio
async def test_broadcast_web_reply_envelope_includes_text_and_attachments():
    mock_redis = MagicMock()
    mock_redis.publish = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with patch("app.web_channel.redis.from_url", return_value=mock_redis):
        await broadcast_web_reply(
            bot_id=7,
            session_id="sess",
            text="hello",
            attachments=[{"type": "image", "url": "http://x/y.png"}],
        )

    body = mock_redis.publish.await_args.args[1]
    envelope = json.loads(body)
    assert envelope["type"] == "message"
    assert envelope["payload"]["text"] == "hello"
    assert envelope["payload"]["attachments"] == [
        {"type": "image", "url": "http://x/y.png"}
    ]


@pytest.mark.asyncio
async def test_broadcast_web_reply_defaults_empty_attachments():
    mock_redis = MagicMock()
    mock_redis.publish = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with patch("app.web_channel.redis.from_url", return_value=mock_redis):
        await broadcast_web_reply(bot_id=7, session_id="sess", text="hi")

    body = mock_redis.publish.await_args.args[1]
    envelope = json.loads(body)
    assert envelope["payload"]["attachments"] == []


@pytest.mark.asyncio
async def test_broadcast_typing_indicator_payload():
    mock_redis = MagicMock()
    mock_redis.publish = AsyncMock()
    mock_redis.aclose = AsyncMock()

    with patch("app.web_channel.redis.from_url", return_value=mock_redis):
        await broadcast_typing_indicator(bot_id=7, session_id="sess", on=True)

    body = mock_redis.publish.await_args.args[1]
    envelope = json.loads(body)
    assert envelope["type"] == "typing"
    assert envelope["payload"] == {"on": True}


@pytest.mark.asyncio
async def test_broadcast_failure_is_swallowed_not_raised():
    """Egress failures must NOT propagate — a Redis hiccup should at worst
    cost one missed event, never crash the worker handler that called us."""
    mock_redis = MagicMock()
    mock_redis.publish = AsyncMock(side_effect=ConnectionError("Redis down"))
    mock_redis.aclose = AsyncMock()

    with patch("app.web_channel.redis.from_url", return_value=mock_redis):
        # Must not raise.
        await broadcast_web_reply(bot_id=1, session_id="s", text="x")


@pytest.mark.asyncio
async def test_broadcast_closes_redis_connection_even_on_failure():
    """The short-lived connection pattern (mirror of broadcast.py) must
    aclose() the connection in the finally block, error or not."""
    mock_redis = MagicMock()
    mock_redis.publish = AsyncMock(side_effect=RuntimeError("boom"))
    mock_redis.aclose = AsyncMock()

    with patch("app.web_channel.redis.from_url", return_value=mock_redis):
        await broadcast_web_event(1, "s", "message", {})

    mock_redis.aclose.assert_awaited_once()
