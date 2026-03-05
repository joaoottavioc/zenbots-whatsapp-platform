# app/monitoring.py
"""
Core instrumentation module for cost tracking and usage attribution.

Records every external API call (LLM, geocoding, S3, WhatsApp, etc.)
into an in-memory buffer that periodically flushes to PostgreSQL and
updates Redis real-time counters.

Usage:
    await record_llm_usage(bot_id, "get_ai_decision", "gpt-4o-mini", usage, 350)
    await record_api_usage(bot_id, "google_maps", "geocode", cost_usd=0.005, duration_ms=200)
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as redis

from app.context import trace_id_var, current_bot_id, current_contact_id
from app.models import UsageEvent
from app.time import utcnow

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────
# Pricing (USD per token)
# ────────────────────────────────────────────────────────────────

PRICING: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {
        "input": 0.15 / 1_000_000,  # $0.15 per 1M input tokens
        "output": 0.60 / 1_000_000,  # $0.60 per 1M output tokens
    },
    "gpt-4o": {
        "input": 2.50 / 1_000_000,  # $2.50 per 1M input tokens
        "output": 10.0 / 1_000_000,  # $10.00 per 1M output tokens
    },
}

# Fixed costs per API call
API_COSTS: dict[str, float] = {
    "google_maps_geocode": 0.005,  # ~$5 per 1K requests
    "s3_put": 0.000005,  # $0.005 per 1K PUTs
    "s3_storage_gb": 0.023 / (1024**3),
    "whatsapp_message": 0.05,  # ~$0.05 per conversation window
}

# ────────────────────────────────────────────────────────────────
# Buffer configuration
# ────────────────────────────────────────────────────────────────

_FLUSH_THRESHOLD = 50  # Flush after this many buffered events
_FLUSH_INTERVAL = 10.0  # Flush every N seconds
_BUFFER_HARD_CAP = 500  # Drop oldest events beyond this

_event_buffer: list[UsageEvent] = []
_buffer_lock = asyncio.Lock()
_flush_task: Optional[asyncio.Task] = None

# ────────────────────────────────────────────────────────────────
# Redis (DB 2) for real-time counters
# ────────────────────────────────────────────────────────────────

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

_redis_monitor: Optional[redis.Redis] = None


async def _get_redis_monitor() -> redis.Redis:
    """Lazy-initialized Redis client for monitoring counters (DB 2)."""
    global _redis_monitor
    if _redis_monitor is None:
        _redis_monitor = redis.from_url(
            f"redis://{REDIS_HOST}:{REDIS_PORT}/2",
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
    return _redis_monitor


# ────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────


async def record_llm_usage(
    bot_id: int | None,
    operation: str,
    model: str,
    usage,  # OpenAI CompletionUsage object (has prompt_tokens, completion_tokens)
    duration_ms: int,
    contact_id: int | None = None,
    success: bool = True,
) -> None:
    """Record an LLM API call with token counts and cost."""
    # Resolve bot_id / contact_id from context if not passed
    if bot_id is None:
        bot_id = current_bot_id.get(None)
    if not bot_id:
        logger.debug("Skipping LLM usage event for %s — no bot_id", operation)
        return
    if contact_id is None:
        contact_id = current_contact_id.get(None)

    input_tokens = getattr(usage, "prompt_tokens", 0) or 0 if usage else 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0 if usage else 0
    cached = getattr(usage, "cached_tokens", 0) or 0 if usage else 0

    pricing = PRICING.get(model, {"input": 0, "output": 0})
    cost = input_tokens * pricing["input"] + output_tokens * pricing["output"]

    event = UsageEvent(
        bot_id=bot_id,
        service="openai",
        operation=operation,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached,
        cost_usd=round(cost, 8) if success else 0.0,
        duration_ms=duration_ms,
        success=success,
        contact_id=contact_id,
        trace_id=trace_id_var.get(""),
        created_at=utcnow(),
    )
    await _buffer_append(event)


async def record_api_usage(
    bot_id: int | None,
    service: str,
    operation: str,
    cost_usd: float = 0.0,
    quantity: int = 1,
    duration_ms: int = 0,
    success: bool = True,
    contact_id: int | None = None,
) -> None:
    """Record a non-LLM external API call."""
    if bot_id is None:
        bot_id = current_bot_id.get(None)
    if not bot_id:
        logger.debug("Skipping usage event for %s/%s — no bot_id", service, operation)
        return
    if contact_id is None:
        contact_id = current_contact_id.get(None)

    event = UsageEvent(
        bot_id=bot_id,
        service=service,
        operation=operation,
        model=None,
        cost_usd=round(cost_usd, 8) if success else 0.0,
        quantity=quantity,
        duration_ms=duration_ms,
        success=success,
        contact_id=contact_id,
        trace_id=trace_id_var.get(""),
        created_at=utcnow(),
    )
    await _buffer_append(event)


# ────────────────────────────────────────────────────────────────
# Buffer management
# ────────────────────────────────────────────────────────────────


async def _buffer_append(event: UsageEvent) -> None:
    """Thread-safe append to the event buffer; flushes when threshold reached."""
    async with _buffer_lock:
        # Hard cap: drop oldest events if buffer overflows
        if len(_event_buffer) >= _BUFFER_HARD_CAP:
            dropped = len(_event_buffer) - _BUFFER_HARD_CAP + 1
            del _event_buffer[:dropped]
            logger.warning(
                "Monitoring buffer overflow: dropped %d oldest events", dropped
            )
        _event_buffer.append(event)
        should_flush = len(_event_buffer) >= _FLUSH_THRESHOLD

    # Update Redis counters (non-blocking, fire-and-forget on error)
    try:
        await _update_redis_counters(event)
    except Exception as e:
        logger.debug("Redis counter update failed (non-critical): %s", e)

    if should_flush:
        await flush_buffer()


async def flush_buffer(session=None) -> int:
    """Bulk-insert buffered events into PostgreSQL. Returns count flushed."""
    async with _buffer_lock:
        if not _event_buffer:
            return 0
        batch = list(_event_buffer)
        _event_buffer.clear()

    try:
        if session is None:
            from app.database import async_session

            async with async_session() as session:
                session.add_all(batch)
                await session.commit()
        else:
            session.add_all(batch)
            await session.commit()
        logger.debug("Flushed %d monitoring events to DB", len(batch))
        return len(batch)
    except Exception as e:
        logger.error("Failed to flush monitoring buffer (%d events): %s", len(batch), e)
        # Re-add to buffer so they aren't lost
        async with _buffer_lock:
            _event_buffer[:0] = batch  # prepend
            # Enforce hard cap after re-adding
            if len(_event_buffer) > _BUFFER_HARD_CAP:
                del _event_buffer[_BUFFER_HARD_CAP:]
        return 0


# ────────────────────────────────────────────────────────────────
# Redis real-time counters
# ────────────────────────────────────────────────────────────────


async def _update_redis_counters(event: UsageEvent) -> None:
    """Update hourly and daily counters in Redis DB 2."""
    r = await _get_redis_monitor()
    hour_key = event.created_at.strftime("%Y%m%d%H")

    pipe = r.pipeline()

    # Hourly counters
    calls_key = f"monitor:hourly:{event.bot_id}:{event.service}:{hour_key}:calls"
    cost_key = f"monitor:hourly:{event.bot_id}:{event.service}:{hour_key}:cost"
    tokens_key = f"monitor:hourly:{event.bot_id}:{event.service}:{hour_key}:tokens"

    pipe.incr(calls_key)
    pipe.incrbyfloat(cost_key, event.cost_usd)
    pipe.incr(tokens_key, event.input_tokens + event.output_tokens)

    # Auto-expire after 48 hours
    pipe.expire(calls_key, 48 * 3600)
    pipe.expire(cost_key, 48 * 3600)
    pipe.expire(tokens_key, 48 * 3600)

    # Daily cost sorted set (for leaderboard)
    date_str = event.created_at.strftime("%Y%m%d")
    daily_key = f"monitor:daily_cost:{date_str}"
    pipe.zincrby(daily_key, event.cost_usd, str(event.bot_id))
    pipe.expire(daily_key, 8 * 86400)  # 8-day TTL

    await pipe.execute()

    # Check for anomalies
    try:
        await _check_anomaly(event, r)
    except Exception as e:
        logger.debug("Anomaly check failed (non-critical): %s", e)


async def _check_anomaly(event: UsageEvent, r: redis.Redis) -> None:
    """Compare current hour cost vs 7-day average. Alert on spikes."""
    avg7d_raw = await r.get(f"monitor:avg7d:{event.bot_id}")
    if not avg7d_raw:
        return  # No baseline yet

    avg_daily = float(avg7d_raw)
    avg_hourly = avg_daily / 24.0
    if avg_hourly <= 0:
        return

    hour_key = event.created_at.strftime("%Y%m%d%H")
    cost_key = f"monitor:hourly:{event.bot_id}:{event.service}:{hour_key}:cost"
    current_cost_raw = await r.get(cost_key)
    current_cost = float(current_cost_raw) if current_cost_raw else 0.0

    ratio = current_cost / avg_hourly if avg_hourly > 0 else 0

    severity = None
    if ratio > 10:
        severity = "critical"
    elif ratio > 3:
        severity = "warning"

    if severity is None:
        return

    # Deduplication: only alert once per bot per hour
    dedup_key = f"monitor:alert_sent:{event.bot_id}:{hour_key}"
    was_set = await r.set(dedup_key, "1", nx=True, ex=3600)
    if not was_set:
        return  # Already alerted this hour

    alert = {
        "type": "cost_anomaly",
        "severity": severity,
        "bot_id": event.bot_id,
        "service": event.service,
        "current_hourly_cost": round(current_cost, 6),
        "avg_hourly_cost": round(avg_hourly, 6),
        "ratio": round(ratio, 1),
        "hour": hour_key,
    }
    logger.warning(
        "Cost anomaly detected: bot_id=%s severity=%s ratio=%.1fx",
        event.bot_id,
        severity,
        ratio,
    )
    await r.publish("monitoring_alerts", json.dumps(alert))


# ────────────────────────────────────────────────────────────────
# Business & operational metrics (Redis counters)
# ────────────────────────────────────────────────────────────────


async def record_business_event(
    event_name: str, bot_id: int | None = None, increment: int = 1
) -> None:
    """
    Increment a Redis counter for a business/operational event.

    Keys use the pattern: monitor:biz:{event_name}:{YYYYMMDD}[:bot_id]
    Auto-expire after 8 days.
    """
    if bot_id is None:
        bot_id = current_bot_id.get(None)

    try:
        r = await _get_redis_monitor()
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")

        pipe = r.pipeline()
        # Global counter
        global_key = f"monitor:biz:{event_name}:{date_str}"
        pipe.incr(global_key, increment)
        pipe.expire(global_key, 8 * 86400)

        # Per-bot counter (if bot_id available)
        if bot_id:
            bot_key = f"monitor:biz:{event_name}:{date_str}:{bot_id}"
            pipe.incr(bot_key, increment)
            pipe.expire(bot_key, 8 * 86400)

        await pipe.execute()
    except Exception as e:
        logger.debug("Business event counter failed (non-critical): %s", e)


async def record_error(source: str, error_type: str = "unknown") -> None:
    """Increment error counter. Source examples: 'openai', 'whatsapp', 'payment'."""
    try:
        r = await _get_redis_monitor()
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        hour_key = datetime.now(timezone.utc).strftime("%Y%m%d%H")

        pipe = r.pipeline()
        pipe.incr(f"monitor:errors:{source}:{date_str}", 1)
        pipe.expire(f"monitor:errors:{source}:{date_str}", 8 * 86400)
        pipe.incr(f"monitor:errors:{source}:{hour_key}", 1)
        pipe.expire(f"monitor:errors:{source}:{hour_key}", 48 * 3600)
        await pipe.execute()
    except Exception as e:
        logger.debug("Error counter increment failed: %s", e)


async def track_sse_connection(delta: int) -> None:
    """Track SSE connection count. delta=1 on connect, delta=-1 on disconnect."""
    try:
        r = await _get_redis_monitor()
        await r.incr("monitor:sse_connections", delta)
    except Exception:
        pass


async def record_worker_job(
    function_name: str, duration_ms: int, success: bool
) -> None:
    """Track worker job execution metrics."""
    try:
        r = await _get_redis_monitor()
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")

        pipe = r.pipeline()
        status = "ok" if success else "fail"
        pipe.incr(f"monitor:worker:{function_name}:{date_str}:{status}")
        pipe.expire(f"monitor:worker:{function_name}:{date_str}:{status}", 8 * 86400)

        # LLM latency ring buffer (keep last 100 durations per function)
        latency_key = f"monitor:latency:{function_name}"
        pipe.lpush(latency_key, duration_ms)
        pipe.ltrim(latency_key, 0, 99)
        pipe.expire(latency_key, 48 * 3600)
        await pipe.execute()
    except Exception as e:
        logger.debug("Worker job metrics failed: %s", e)


async def check_queue_depth() -> dict:
    """Check ARQ queue depth for monitoring. Returns queue stats."""
    try:
        r = redis.from_url(
            f"redis://{REDIS_HOST}:{REDIS_PORT}/1",
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        try:
            depth = await r.zcard("arq:queue")
            return {"queue_depth": depth, "status": "ok"}
        finally:
            await r.aclose()
    except Exception as e:
        return {"queue_depth": -1, "status": "error", "error": str(e)}


# ────────────────────────────────────────────────────────────────
# Background flush task
# ────────────────────────────────────────────────────────────────


async def _periodic_flush():
    """Background task that flushes the buffer every _FLUSH_INTERVAL seconds."""
    while True:
        try:
            await asyncio.sleep(_FLUSH_INTERVAL)
            await flush_buffer()
        except asyncio.CancelledError:
            # Final flush on shutdown
            await flush_buffer()
            break
        except Exception as e:
            logger.error("Monitoring periodic flush error: %s", e)


def start_flush_task() -> None:
    """Start the background flush task. Call during lifespan startup."""
    global _flush_task
    if _flush_task is not None:
        return
    _flush_task = asyncio.create_task(_periodic_flush())
    logger.info(
        "Monitoring: periodic flush task started (interval=%ss)", _FLUSH_INTERVAL
    )


async def stop_flush_task() -> None:
    """Stop the background flush task and do a final flush. Call during shutdown."""
    global _flush_task
    if _flush_task is not None:
        _flush_task.cancel()
        try:
            await _flush_task
        except asyncio.CancelledError:
            pass
        _flush_task = None
        logger.info("Monitoring: periodic flush task stopped")

    # Close Redis monitor connection
    global _redis_monitor
    if _redis_monitor is not None:
        await _redis_monitor.aclose()
        _redis_monitor = None


def get_buffer_size() -> int:
    """Return current buffer size (for health checks)."""
    return len(_event_buffer)


# ────────────────────────────────────────────────────────────────
# Daily aggregation
# ────────────────────────────────────────────────────────────────


async def aggregate_daily_costs(target_date=None) -> int:
    """
    Aggregate usage_events for a given date into daily_cost_summary.

    Called by the worker cron job (typically for yesterday).
    Returns the number of rows upserted.
    """
    from datetime import timedelta
    from sqlalchemy import func, case, and_
    from sqlmodel import select
    from app.models import DailyCostSummary

    if target_date is None:
        target_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    from app.database import async_session

    async with async_session() as session:
        # 1. Aggregate usage_events for the target date
        day_start = datetime.combine(target_date, datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
        day_end = day_start + timedelta(days=1)

        stmt = (
            select(
                UsageEvent.bot_id,
                UsageEvent.service,
                func.sum(UsageEvent.cost_usd).label("total_cost_usd"),
                func.sum(UsageEvent.input_tokens).label("total_input_tokens"),
                func.sum(UsageEvent.output_tokens).label("total_output_tokens"),
                func.count().label("total_api_calls"),
                func.sum(case((UsageEvent.success == False, 1), else_=0)).label(
                    "total_failed_calls"
                ),
                func.sum(UsageEvent.duration_ms).label("total_duration_ms"),
                func.avg(UsageEvent.cost_usd).label("avg_cost_per_call"),
                func.max(UsageEvent.cost_usd).label("max_cost_single_call"),
            )
            .where(
                and_(
                    UsageEvent.created_at >= day_start, UsageEvent.created_at < day_end
                )
            )
            .group_by(UsageEvent.bot_id, UsageEvent.service)
        )

        result = await session.execute(stmt)
        rows = result.all()

        upserted = 0
        for row in rows:
            # Check if summary already exists
            existing = await session.execute(
                select(DailyCostSummary).where(
                    DailyCostSummary.bot_id == row.bot_id,
                    DailyCostSummary.date == target_date,
                    DailyCostSummary.service == row.service,
                )
            )
            summary = existing.scalars().first()

            if summary:
                summary.total_cost_usd = float(row.total_cost_usd or 0)
                summary.total_input_tokens = int(row.total_input_tokens or 0)
                summary.total_output_tokens = int(row.total_output_tokens or 0)
                summary.total_api_calls = int(row.total_api_calls or 0)
                summary.total_failed_calls = int(row.total_failed_calls or 0)
                summary.total_duration_ms = int(row.total_duration_ms or 0)
                summary.avg_cost_per_call = float(row.avg_cost_per_call or 0)
                summary.max_cost_single_call = float(row.max_cost_single_call or 0)
            else:
                summary = DailyCostSummary(
                    bot_id=row.bot_id,
                    date=target_date,
                    service=row.service,
                    total_cost_usd=float(row.total_cost_usd or 0),
                    total_input_tokens=int(row.total_input_tokens or 0),
                    total_output_tokens=int(row.total_output_tokens or 0),
                    total_api_calls=int(row.total_api_calls or 0),
                    total_failed_calls=int(row.total_failed_calls or 0),
                    total_duration_ms=int(row.total_duration_ms or 0),
                    avg_cost_per_call=float(row.avg_cost_per_call or 0),
                    max_cost_single_call=float(row.max_cost_single_call or 0),
                )
                session.add(summary)
            upserted += 1

        await session.commit()

        # 2. Update Redis avg7d for each bot
        try:
            r = await _get_redis_monitor()
            # Get unique bot_ids from this aggregation
            bot_ids = {row.bot_id for row in rows}
            for bot_id in bot_ids:
                # Get last 7 days total cost
                seven_days_ago = target_date - timedelta(days=7)
                cost_stmt = select(func.sum(DailyCostSummary.total_cost_usd)).where(
                    DailyCostSummary.bot_id == bot_id,
                    DailyCostSummary.date >= seven_days_ago,
                    DailyCostSummary.date <= target_date,
                )
                cost_result = await session.execute(cost_stmt)
                total_7d = cost_result.scalar() or 0.0
                avg_daily = total_7d / 7.0
                await r.set(f"monitor:avg7d:{bot_id}", str(avg_daily), ex=8 * 86400)
        except Exception as e:
            logger.warning("Failed to update Redis avg7d: %s", e)

        # 3. Purge old events (older than 90 days) in batches
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=90)
            from sqlalchemy import delete

            deleted = await session.execute(
                delete(UsageEvent).where(UsageEvent.created_at < cutoff)
            )
            await session.commit()
            if deleted.rowcount:
                logger.info(
                    "Purged %d usage events older than 90 days", deleted.rowcount
                )
        except Exception as e:
            logger.error("Failed to purge old usage events: %s", e)

        logger.info(
            "Daily cost aggregation complete: %d summaries upserted for %s",
            upserted,
            target_date,
        )
        return upserted


async def run_aggregate_daily_costs(ctx) -> None:
    """ARQ cron wrapper for daily cost aggregation."""
    await aggregate_daily_costs()
