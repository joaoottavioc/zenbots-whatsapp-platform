# app/health_routes.py
"""
Health check endpoints for liveness and readiness probes.

- GET /health/live  — Always returns 200 (process is alive).
- GET /health/ready — Probes PostgreSQL and Redis; returns 200 if all
  dependencies are reachable, 503 otherwise.
"""

import logging
import os
import time

import redis.asyncio as redis
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.database import async_session
from app.monitoring import get_buffer_size, check_queue_depth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["Health"])

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))


@router.get("/live")
async def liveness():
    """Liveness probe — always 200 if the process is running."""
    return {"status": "alive"}


@router.get("/ready")
async def readiness():
    """Readiness probe — checks PostgreSQL and Redis connectivity."""
    checks: dict = {}
    all_ok = True

    # --- PostgreSQL ---
    try:
        start = time.perf_counter()
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        latency = round((time.perf_counter() - start) * 1000, 1)
        checks["postgresql"] = {"status": "up", "latency_ms": latency}
    except Exception as e:
        logger.error("Health check: PostgreSQL unreachable: %s", e)
        checks["postgresql"] = {"status": "down", "error": str(e)}
        all_ok = False

    # --- Redis DB 0 (rate limiter) ---
    try:
        start = time.perf_counter()
        r = redis.from_url(
            f"redis://{REDIS_HOST}:{REDIS_PORT}/0",
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        try:
            await r.ping()
            latency = round((time.perf_counter() - start) * 1000, 1)
            checks["redis_db0"] = {"status": "up", "latency_ms": latency}
        finally:
            await r.aclose()
    except Exception as e:
        logger.error("Health check: Redis DB 0 unreachable: %s", e)
        checks["redis_db0"] = {"status": "down", "error": str(e)}
        all_ok = False

    # --- Redis DB 1 (ARQ job queue) ---
    try:
        start = time.perf_counter()
        r = redis.from_url(
            f"redis://{REDIS_HOST}:{REDIS_PORT}/1",
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        try:
            await r.ping()
            queue_depth = await r.zcard("arq:queue")
            latency = round((time.perf_counter() - start) * 1000, 1)
            checks["redis_db1_arq"] = {
                "status": "up",
                "latency_ms": latency,
                "queue_depth": queue_depth,
            }
        finally:
            await r.aclose()
    except Exception as e:
        logger.error("Health check: Redis DB 1 (ARQ) unreachable: %s", e)
        checks["redis_db1_arq"] = {"status": "down", "error": str(e)}
        all_ok = False

    # --- Monitoring buffer ---
    checks["monitoring_buffer"] = {"pending_events": get_buffer_size()}

    # --- ARQ queue depth ---
    queue_info = await check_queue_depth()
    checks["arq_queue"] = queue_info

    status_code = 200 if all_ok else 503
    return JSONResponse(
        content={"status": "ready" if all_ok else "degraded", "checks": checks},
        status_code=status_code,
    )
