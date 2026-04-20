"""Single-run lock for the corpus QA runner.

Prevents two QA runs from executing concurrently, which corrupts each
other's bots/test data via the pre_cleanup mechanism. Without this lock
the previous safety net (pre_cleanup 15-minute cutoff in
``run_corpus_qa.py``) was a heuristic; this is the API-layer guarantee.

Storage: a single Redis key ``corpus_qa_lock`` holding a JSON-encoded
``{"owner_id": "<uuid8>", "started_at": <epoch>}`` payload, set with
``NX EX`` semantics. TTL is 25 min — long enough to cover the slowest
realistic run (samples=5 × 30 restaurants ≈ 50 min is borderline, but a
stuck run releases via TTL so the user can retry without manual fixup).

Two entry points use this lock:
- ``app.monitoring_routes.admin_corpus_run_tests`` wraps the subprocess
  invocation with acquire/finally-release. On collision it returns HTTP
  409 with the holder's start time.
- ``tests/simulation/corpus/run_corpus_qa.py`` acquires its own lock when
  invoked directly from the CLI. When invoked from the API endpoint, the
  env var ``CORPUS_LOCK_OWNED=1`` tells the script the API already owns
  the lock and to skip its own acquire (avoids double-acquire failure).
"""

import json
import logging
import os
import time
import uuid
from typing import Optional

import redis.asyncio as redis_asyncio

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

LOCK_KEY = "corpus_qa_lock"
LOCK_TTL_SECONDS = 60 * 60  # 60 min ceiling — samples=5 baseline runs take ~50 min

# Lazy async singleton — same pattern as app/rate_limiter.py
_async_client = None


def _get_async_client():
    global _async_client
    if _async_client is None:
        _async_client = redis_asyncio.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
    return _async_client


async def try_acquire() -> tuple[bool, Optional[dict]]:
    """Attempt to acquire the QA run lock.

    Returns ``(True, holder_info)`` on success — ``holder_info`` is the
    dict we just stored, including ``owner_id`` for later conditional
    release. Returns ``(False, holder_info)`` on collision — ``holder_info``
    is the *existing* holder so the caller can report who owns it.
    Returns ``(False, None)`` if Redis is unreachable (fails closed).
    """
    holder = {
        "owner_id": uuid.uuid4().hex[:8],
        "started_at": time.time(),
    }
    try:
        r = _get_async_client()
        # SET NX EX: only set if not exists, with TTL
        success = await r.set(
            LOCK_KEY,
            json.dumps(holder),
            nx=True,
            ex=LOCK_TTL_SECONDS,
        )
        if success:
            logger.info("[QA_LOCK] acquired owner=%s", holder["owner_id"])
            return True, holder
        # Lock was held — fetch existing for caller-friendly reporting
        existing = await r.get(LOCK_KEY)
        if existing:
            try:
                existing_holder = json.loads(existing)
                logger.info(
                    "[QA_LOCK] busy held_by=%s started=%s",
                    existing_holder.get("owner_id"),
                    existing_holder.get("started_at"),
                )
                return False, existing_holder
            except json.JSONDecodeError:
                return False, None
        return False, None
    except Exception as e:
        logger.error("[QA_LOCK] try_acquire failed: %s", e)
        # Fail CLOSED — if Redis is down, refuse to start a run rather than
        # risk corruption from a missing safety net.
        return False, None


async def release(owner_id: Optional[str] = None) -> None:
    """Release the QA run lock.

    If ``owner_id`` is given, the release is conditional — the lock is
    only deleted if the stored owner matches. This avoids accidentally
    releasing someone else's lock if the API endpoint times out and a
    second run starts before the first's release goes through. If
    ``owner_id`` is None, the lock is force-deleted.
    """
    try:
        r = _get_async_client()
        if owner_id is None:
            await r.delete(LOCK_KEY)
            logger.info("[QA_LOCK] force-released")
            return
        val = await r.get(LOCK_KEY)
        if val:
            try:
                holder = json.loads(val)
                if holder.get("owner_id") == owner_id:
                    await r.delete(LOCK_KEY)
                    logger.info("[QA_LOCK] released owner=%s", owner_id)
                else:
                    logger.warning(
                        "[QA_LOCK] not releasing — lock owned by %s, not %s",
                        holder.get("owner_id"),
                        owner_id,
                    )
            except json.JSONDecodeError:
                # Corrupt value — delete it so the next run can proceed
                await r.delete(LOCK_KEY)
    except Exception as e:
        logger.error("[QA_LOCK] release failed: %s", e)


async def get_holder() -> Optional[dict]:
    """Return the current lock holder dict, or None if no lock is held."""
    try:
        r = _get_async_client()
        val = await r.get(LOCK_KEY)
        if val:
            return json.loads(val)
    except Exception as e:
        logger.error("[QA_LOCK] get_holder failed: %s", e)
    return None
