"""Founder plan slot counter.

First 30 Pro signups get the lifetime R$59.90/mo Founder price. This module
is the atomic, concurrency-safe source of truth for whether a slot is still
available and owns the INCR/DECR on the Redis counter.

Rules (from backlog_pricing.md 2.2):
- Reserve a slot at checkout time (before MP preapproval is created) so
  paid promotions can't race past the cap.
- Release on MP preapproval creation failure, or on a founder subscription
  cancellation that happens inside the 30-day refund window.
- Outside the refund window, cancellations do NOT free the slot (slot is
  consumed forever; determined abusers can't game it by cancelling and
  resigning).
- Sunset: after FOUNDER_SUNSET_AT, no new founders regardless of counter.
- Lifetime price: Subscription.snapshotted_price_brl captures R$59.90 at
  checkout so later Plan.price edits don't affect existing founders.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)

FOUNDER_LIMIT = 30
FOUNDER_PRICE_BRL = 59.90
FOUNDER_COUNTER_KEY = "founder:redemptions"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Default sunset 90 days out so dev envs don't accidentally close the promo.
# Prod should set FOUNDER_SUNSET_AT (ISO date) explicitly per launch plan.
_DEFAULT_SUNSET = "2026-07-22"

_client: Optional[redis.Redis] = None


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
    return _client


def sunset_date() -> date:
    raw = os.getenv("FOUNDER_SUNSET_AT", _DEFAULT_SUNSET)
    try:
        return date.fromisoformat(raw)
    except ValueError:
        logger.warning("Invalid FOUNDER_SUNSET_AT=%r, falling back to default", raw)
        return date.fromisoformat(_DEFAULT_SUNSET)


def sunset_passed() -> bool:
    return datetime.now(timezone.utc).date() > sunset_date()


async def slots_used() -> int:
    try:
        value = await _get_client().get(FOUNDER_COUNTER_KEY)
    except Exception as exc:
        logger.error("founder: counter read failed: %s", exc)
        # Fail closed — if Redis is down, treat the promo as full rather
        # than risk overselling the 30 lifetime slots.
        return FOUNDER_LIMIT
    return int(value) if value else 0


async def slots_remaining() -> int:
    return max(0, FOUNDER_LIMIT - await slots_used())


async def is_available() -> bool:
    if sunset_passed():
        return False
    return await slots_remaining() > 0


async def reserve_slot() -> bool:
    """Atomic reservation. Returns True if the caller got a slot.

    Uses INCR and rolls back with DECR if the post-increment count would
    exceed FOUNDER_LIMIT. This avoids a TOCTOU race between a plain
    "check-then-set" pattern.
    """
    if sunset_passed():
        return False
    client = _get_client()
    try:
        current = await client.incr(FOUNDER_COUNTER_KEY)
    except Exception as exc:
        logger.error("founder: reserve_slot INCR failed: %s", exc)
        return False
    if current > FOUNDER_LIMIT:
        try:
            await client.decr(FOUNDER_COUNTER_KEY)
        except Exception as exc:
            logger.error("founder: reserve_slot rollback DECR failed: %s", exc)
        return False
    return True


async def release_slot() -> None:
    """Release a previously reserved slot.

    Called on MP preapproval creation failure or on a founder cancellation
    inside the refund window. Clamps at 0 so a double-release can't push
    the counter negative.
    """
    client = _get_client()
    try:
        current = await client.decr(FOUNDER_COUNTER_KEY)
        if current < 0:
            await client.set(FOUNDER_COUNTER_KEY, 0)
    except Exception as exc:
        logger.warning("founder: release_slot failed: %s", exc)
