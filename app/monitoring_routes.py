# app/monitoring_routes.py
"""
REST API endpoints for monitoring and cost observability.

All endpoints require JWT authentication and filter data to
the authenticated user's bots only.
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import get_current_user
from app.database import get_session
from app.models import Bot, DailyCostSummary, User
from app.monitoring import get_buffer_size

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/monitoring", tags=["Monitoring"])

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))


async def _get_user_bot_ids(session: AsyncSession, user: User) -> list[int]:
    """Get all bot IDs belonging to the authenticated user."""
    result = await session.execute(select(Bot.id).where(Bot.user_id == user.id))
    return [row[0] for row in result.all()]


@router.get("/overview")
async def monitoring_overview(
    days: int = Query(default=7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Aggregate cost overview across all of the user's bots.
    Returns total costs, token counts, and API calls grouped by service.
    """
    bot_ids = await _get_user_bot_ids(session, current_user)
    if not bot_ids:
        return {"days": days, "total_cost_usd": 0, "services": [], "bots_count": 0}

    start_date = date.today() - timedelta(days=days)

    stmt = (
        select(
            DailyCostSummary.service,
            func.sum(DailyCostSummary.total_cost_usd).label("total_cost_usd"),
            func.sum(DailyCostSummary.total_input_tokens).label("total_input_tokens"),
            func.sum(DailyCostSummary.total_output_tokens).label("total_output_tokens"),
            func.sum(DailyCostSummary.total_api_calls).label("total_api_calls"),
            func.sum(DailyCostSummary.total_failed_calls).label("total_failed_calls"),
            func.sum(DailyCostSummary.total_duration_ms).label("total_duration_ms"),
        )
        .where(
            DailyCostSummary.bot_id.in_(bot_ids),
            DailyCostSummary.date >= start_date,
        )
        .group_by(DailyCostSummary.service)
    )

    result = await session.execute(stmt)
    rows = result.all()

    services = []
    total_cost = 0.0
    for row in rows:
        cost = float(row.total_cost_usd or 0)
        total_cost += cost
        services.append(
            {
                "service": row.service,
                "total_cost_usd": round(cost, 6),
                "total_input_tokens": int(row.total_input_tokens or 0),
                "total_output_tokens": int(row.total_output_tokens or 0),
                "total_api_calls": int(row.total_api_calls or 0),
                "total_failed_calls": int(row.total_failed_calls or 0),
                "total_duration_ms": int(row.total_duration_ms or 0),
            }
        )

    return {
        "days": days,
        "total_cost_usd": round(total_cost, 6),
        "services": services,
        "bots_count": len(bot_ids),
        "buffer_size": get_buffer_size(),
    }


@router.get("/bot/{bot_id}")
async def monitoring_bot_detail(
    bot_id: int,
    days: int = Query(default=7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Per-bot cost detail with daily breakdown.
    """
    # Verify ownership
    bot_ids = await _get_user_bot_ids(session, current_user)
    if bot_id not in bot_ids:
        raise HTTPException(status_code=404, detail="Bot not found")

    start_date = date.today() - timedelta(days=days)

    # Daily breakdown
    stmt = (
        select(
            DailyCostSummary.date,
            DailyCostSummary.service,
            DailyCostSummary.total_cost_usd,
            DailyCostSummary.total_input_tokens,
            DailyCostSummary.total_output_tokens,
            DailyCostSummary.total_api_calls,
            DailyCostSummary.total_failed_calls,
            DailyCostSummary.total_duration_ms,
            DailyCostSummary.avg_cost_per_call,
            DailyCostSummary.max_cost_single_call,
        )
        .where(
            DailyCostSummary.bot_id == bot_id,
            DailyCostSummary.date >= start_date,
        )
        .order_by(DailyCostSummary.date.desc())
    )

    result = await session.execute(stmt)
    rows = result.all()

    daily_data = []
    total_cost = 0.0
    for row in rows:
        cost = float(row.total_cost_usd or 0)
        total_cost += cost
        daily_data.append(
            {
                "date": str(row.date),
                "service": row.service,
                "total_cost_usd": round(cost, 6),
                "total_input_tokens": int(row.total_input_tokens or 0),
                "total_output_tokens": int(row.total_output_tokens or 0),
                "total_api_calls": int(row.total_api_calls or 0),
                "total_failed_calls": int(row.total_failed_calls or 0),
                "total_duration_ms": int(row.total_duration_ms or 0),
                "avg_cost_per_call": round(float(row.avg_cost_per_call or 0), 8),
                "max_cost_single_call": round(float(row.max_cost_single_call or 0), 8),
            }
        )

    # Get today's real-time data from Redis
    today_realtime = {}
    try:
        r = redis.from_url(
            f"redis://{REDIS_HOST}:{REDIS_PORT}/2",
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        try:
            hour_key = datetime.now(timezone.utc).strftime("%Y%m%d")
            # Get today's data from the daily sorted set
            today_cost = await r.zscore(f"monitor:daily_cost:{hour_key}", str(bot_id))
            today_realtime = {
                "today_cost_usd": round(float(today_cost), 6) if today_cost else 0.0,
            }
        finally:
            await r.aclose()
    except Exception:
        today_realtime = {"today_cost_usd": 0.0}

    return {
        "bot_id": bot_id,
        "days": days,
        "total_cost_usd": round(total_cost, 6),
        "daily": daily_data,
        "realtime": today_realtime,
    }


@router.get("/leaderboard")
async def monitoring_leaderboard(
    days: int = Query(default=1, ge=1, le=30),
    limit: int = Query(default=10, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Top N bots by cost for the specified period.
    Only shows bots belonging to the authenticated user.
    """
    bot_ids = await _get_user_bot_ids(session, current_user)
    if not bot_ids:
        return {"days": days, "bots": []}

    start_date = date.today() - timedelta(days=days)

    stmt = (
        select(
            DailyCostSummary.bot_id,
            func.sum(DailyCostSummary.total_cost_usd).label("total_cost_usd"),
            func.sum(DailyCostSummary.total_api_calls).label("total_api_calls"),
            func.sum(DailyCostSummary.total_failed_calls).label("total_failed_calls"),
        )
        .where(
            DailyCostSummary.bot_id.in_(bot_ids),
            DailyCostSummary.date >= start_date,
        )
        .group_by(DailyCostSummary.bot_id)
        .order_by(func.sum(DailyCostSummary.total_cost_usd).desc())
        .limit(limit)
    )

    result = await session.execute(stmt)
    rows = result.all()

    # Get bot names
    bot_names = {}
    if rows:
        name_result = await session.execute(
            select(Bot.id, Bot.restaurant_name).where(
                Bot.id.in_([r.bot_id for r in rows])
            )
        )
        bot_names = {row[0]: row[1] for row in name_result.all()}

    bots = []
    for rank, row in enumerate(rows, 1):
        bots.append(
            {
                "rank": rank,
                "bot_id": row.bot_id,
                "restaurant_name": bot_names.get(row.bot_id, "Unknown"),
                "total_cost_usd": round(float(row.total_cost_usd or 0), 6),
                "total_api_calls": int(row.total_api_calls or 0),
                "total_failed_calls": int(row.total_failed_calls or 0),
            }
        )

    return {"days": days, "bots": bots}


@router.get("/alerts/stream")
async def monitoring_alerts_stream(
    current_user: User = Depends(get_current_user),
):
    """
    SSE stream for real-time monitoring alerts (anomaly detection).
    Subscribes to Redis PubSub channel 'monitoring_alerts'.
    """

    async def event_generator():
        r = redis.from_url(
            f"redis://{REDIS_HOST}:{REDIS_PORT}/2",
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        pubsub = r.pubsub()
        await pubsub.subscribe("monitoring_alerts")

        try:
            yield f"data: {json.dumps({'type': 'connected', 'message': 'Monitoring alerts stream active'})}\n\n"

            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message:
                    yield f"data: {message['data']}\n\n"
                else:
                    yield ": keep-alive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await pubsub.unsubscribe("monitoring_alerts")
                await r.aclose()
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
