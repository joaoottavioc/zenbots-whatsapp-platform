# app/distributed_lock.py
import logging
import os
import redis.asyncio as redis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = redis.from_url(
            REDIS_URL, decode_responses=True,
            socket_timeout=2, socket_connect_timeout=2,
        )
    return _client


def contact_lock(contact_id: int):
    """
    Returns a Redis distributed lock for the given contact.
    Use as: async with contact_lock(contact_id): ...
    """
    logger.debug("Acquiring lock for contact_id=%s", contact_id)
    return _get_client().lock(
        name=f"contact_lock:{contact_id}",
        timeout=90,
        blocking_timeout=95,
        sleep=0.5,
    )
