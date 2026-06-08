# app/arq_pool.py
"""Resilient access to the ARQ Redis pool used to enqueue worker jobs.

The pool is normally created once in main.py's lifespan. But if the backend
boots while Redis is briefly unavailable (e.g. a Fargate Spot reclaim of the
Redis task races the backend coming up), create_pool() fails and the app
starts WITHOUT app.state.arq_redis ever being set. The app passes health
checks (those use separate lazy Redis connections), yet every enqueue then
raises AttributeError -> 500, surfacing in the web widget as "Houve um
problema ao enviar sua mensagem" — and it never recovers because the pool was
only built at startup.

get_arq_pool() fixes that: it returns the existing pool, or lazily (re)creates
it on demand if startup missed it. The app self-heals on the next request once
Redis is reachable again, without a manual restart.
"""

import logging
import os

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

logger = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DATABASE = 1  # ARQ queue DB — must match worker.py and main.py


def redis_settings() -> RedisSettings:
    return RedisSettings(host=REDIS_HOST, port=REDIS_PORT, database=REDIS_DATABASE)


async def create_arq_pool() -> ArqRedis:
    """Create a fresh ARQ Redis pool. Raises on connection failure."""
    return await create_pool(redis_settings())


async def get_arq_pool(app) -> ArqRedis:
    """Return the app's ARQ pool, creating it on demand if startup missed it.

    Self-heals the backend-booted-while-Redis-was-down case. If Redis is still
    unreachable here, create_pool raises and the caller surfaces an error to
    the client (correct — the job genuinely can't be enqueued yet)."""
    pool = getattr(app.state, "arq_redis", None)
    if pool is None:
        logger.warning(
            "ARQ pool missing at enqueue time; creating on demand "
            "(backend startup likely raced a Redis outage). Self-healing."
        )
        pool = await create_arq_pool()
        app.state.arq_redis = pool
    return pool
