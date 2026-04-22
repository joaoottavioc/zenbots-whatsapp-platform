"""Redis-backed plan-tier lookup used by the send-message hot path.

Every customer-facing WhatsApp reply needs to know the bot's current plan
tier to decide whether to append the Free-tier "Atendimento por ZenBotZ®"
footer. Querying Postgres on every send would double the hot-path latency,
so tier is cached per phone_number_id with a 5 minute TTL and invalidated
explicitly whenever a Subscription status changes.

Keys: `bot_plan:{phone_number_id}` → one of `free | pro | founder | plus |
enterprise` or the sentinel string `_unknown` (distinguishes a negative
cache from a miss). Negative caches are short-lived to avoid pinning a
transient DB error for 5 minutes.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import redis.asyncio as redis
from sqlalchemy import select

from app.database import async_session
from app.models import Bot, Plan, Subscription

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL_SECONDS = 300
NEGATIVE_CACHE_TTL_SECONDS = 30
_UNKNOWN_SENTINEL = "_unknown"

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


def _cache_key(phone_number_id: str) -> str:
    return f"bot_plan:{phone_number_id}"


async def _resolve_tier_from_db(phone_number_id: str) -> Optional[str]:
    """Look up the effective plan tier for a bot's phone_number_id.

    Mirrors the gate logic in _check_subscription: an authorized
    Subscription uses its plan_type, otherwise the bot falls through to
    the Free plan.
    """
    async with async_session() as session:
        bot_q = await session.execute(
            select(Bot.id).where(Bot.phone_number_id == phone_number_id)
        )
        bot_id = bot_q.scalar_one_or_none()
        if bot_id is None:
            return None

        sub_q = await session.execute(
            select(Subscription.plan_type).where(
                Subscription.bot_id == bot_id,
                Subscription.status == "authorized",
            )
        )
        plan_key = sub_q.scalar_one_or_none() or "free"

        tier_q = await session.execute(select(Plan.tier).where(Plan.key == plan_key))
        return tier_q.scalar_one_or_none()


async def get_tier_by_phone_id(phone_number_id: str) -> Optional[str]:
    """Return the bot's plan tier, or None if lookup fails.

    A None return means "do not apply branding" — callers must fail-open
    so a Redis/DB hiccup never adds an incorrect footer to a Pro reply.
    """
    if not phone_number_id:
        return None

    key = _cache_key(phone_number_id)

    try:
        cached = await _get_client().get(key)
    except Exception as exc:
        logger.warning("billing_cache: Redis GET failed (%s), bypassing cache", exc)
        cached = None

    if cached == _UNKNOWN_SENTINEL:
        return None
    if cached:
        return cached

    try:
        tier = await _resolve_tier_from_db(phone_number_id)
    except Exception as exc:
        logger.error(
            "billing_cache: DB lookup failed for phone_id=%s: %s", phone_number_id, exc
        )
        return None

    try:
        client = _get_client()
        if tier is None:
            await client.set(key, _UNKNOWN_SENTINEL, ex=NEGATIVE_CACHE_TTL_SECONDS)
        else:
            await client.set(key, tier, ex=CACHE_TTL_SECONDS)
    except Exception as exc:
        logger.warning("billing_cache: Redis SET failed (%s)", exc)

    return tier


async def invalidate_by_phone_id(phone_number_id: str) -> None:
    if not phone_number_id:
        return
    try:
        await _get_client().delete(_cache_key(phone_number_id))
    except Exception as exc:
        logger.warning(
            "billing_cache: invalidate failed for %s: %s", phone_number_id, exc
        )


async def invalidate_by_bot_id(bot_id: int) -> None:
    """Resolve the bot's phone_number_id and delete its cache entry."""
    try:
        async with async_session() as session:
            res = await session.execute(
                select(Bot.phone_number_id).where(Bot.id == bot_id)
            )
            phone_id = res.scalar_one_or_none()
    except Exception as exc:
        logger.warning(
            "billing_cache: bot lookup failed for bot_id=%s: %s", bot_id, exc
        )
        return
    if phone_id:
        await invalidate_by_phone_id(phone_id)
