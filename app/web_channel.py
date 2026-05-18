"""Egress for the embeddable web widget channel.

Phase 1.5 of plan/in_browser_bots.md. Publishes channel-aware SSE events
to per-session Redis PubSub channels — the widget's `GET /chat/stream`
endpoint (Phase 2) subscribes and forwards events to the browser.

Channel naming: `chat:{bot_id}:{session_id}`. Disjoint from the existing
`dashboard_events:{bot_id}` namespace used by `broadcast.py` for KDS
updates, so a web message broadcast does not leak into the dashboard
stream and vice versa.

Event types the widget consumes:
  - 'message'      → {text: str, attachments: list[dict]}
  - 'typing'       → {on: bool}
  - 'payment_qr'   → {qr_data_url: str, payment_url: str, expires_at: iso}
  - 'order_status' → {order_id: int, status: str}
  - 'ping'         → {} (sent every 10s by the SSE endpoint, not from here)

Implementation mirrors `app/broadcast.py`: short-lived Redis connection,
JSON-encoded payload, error-tolerant (logs and returns — does NOT raise,
because an egress failure should not propagate up the worker pipeline
and stop downstream handlers from running).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)

# Reuses the same Redis DB as `broadcast.py` (DB 0 — PubSub bus). PubSub
# is independent of the rate-limit keys also stored on DB 0, so there's
# no key-collision risk.
_REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")


def _channel(bot_id: int, session_id: str) -> str:
    """Return the PubSub channel name for a given (bot, session) pair."""
    return f"chat:{bot_id}:{session_id}"


async def broadcast_web_event(
    bot_id: int,
    session_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Publish one SSE event to the session's PubSub channel.

    Low-level primitive — most callers use `broadcast_web_reply` or
    `broadcast_typing_indicator` instead. Public so the future payment
    flow (Phase 6.1) can publish `payment_qr` and `order_status` events.
    """
    r: Optional[redis.Redis] = None
    try:
        r = redis.from_url(
            _REDIS_URL,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        envelope = {"type": event_type, "payload": payload}
        channel = _channel(bot_id, session_id)
        await r.publish(channel, json.dumps(envelope))
        logger.debug(
            "Web event published: type=%s bot_id=%s session_id=%s",
            event_type,
            bot_id,
            session_id,
        )
    except Exception as e:
        # Egress failure should not crash the worker handler — log and
        # return. Worst case: the widget misses one event and reconnects.
        logger.error(
            "Web broadcast failed: type=%s bot_id=%s session_id=%s err=%s",
            event_type,
            bot_id,
            session_id,
            e,
        )
    finally:
        if r is not None:
            await r.aclose()


async def broadcast_web_reply(
    bot_id: int,
    session_id: str,
    text: str,
    attachments: Optional[list[dict]] = None,
) -> None:
    """Publish a 'message' event — the most common web egress.

    Mirrors `send_whatsapp_message(to, message, ...)` shape so callers
    (especially `MessageContext.reply`) treat the two channels symmetrically.
    """
    await broadcast_web_event(
        bot_id,
        session_id,
        "message",
        {"text": text, "attachments": attachments or []},
    )


async def broadcast_typing_indicator(
    bot_id: int,
    session_id: str,
    on: bool,
) -> None:
    """Publish a 'typing' event so the widget can render the typing dots.

    Use pattern: emit `on=True` before kicking off an LLM call, emit
    `on=False` when the reply is in flight (the `message` event implicitly
    ends the typing state, but explicit `off` covers error paths where
    no message follows).
    """
    await broadcast_web_event(bot_id, session_id, "typing", {"on": on})
