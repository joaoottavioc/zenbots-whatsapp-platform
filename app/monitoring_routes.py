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
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import get_current_user, require_admin
from app.database import get_session
from app.models import Bot, DailyCostSummary, UsageEvent, User
from app.monitoring import aggregate_daily_costs, flush_buffer, get_buffer_size

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


# --- Admin-only endpoints (require is_admin=True) ---


@router.get("/admin/overview")
async def admin_overview(
    days: int = Query(default=30, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    _admin: User = Depends(require_admin),
):
    """Aggregate cost overview across ALL bots (admin only).

    Reads historical data from daily_cost_summary AND merges in today's
    raw usage_events so the response is "live" — costs incurred since
    the last 3 AM aggregation are visible immediately.

    Includes a static AWS infrastructure estimate row so customers see
    the full cost picture, not just OpenAI/API call costs.
    """
    today = date.today()
    start_date = today - timedelta(days=days)

    # 1. Historical: from daily_cost_summary (excludes today)
    historical_stmt = (
        select(
            DailyCostSummary.service,
            func.sum(DailyCostSummary.total_cost_usd).label("total_cost_usd"),
            func.sum(DailyCostSummary.total_input_tokens).label("total_input_tokens"),
            func.sum(DailyCostSummary.total_output_tokens).label("total_output_tokens"),
            func.sum(DailyCostSummary.total_api_calls).label("total_api_calls"),
            func.sum(DailyCostSummary.total_failed_calls).label("total_failed_calls"),
        )
        .where(
            DailyCostSummary.date >= start_date,
            DailyCostSummary.date < today,
        )
        .group_by(DailyCostSummary.service)
    )
    historical_rows = (await session.execute(historical_stmt)).all()

    # 2. Today: from raw usage_events (real-time, not yet aggregated).
    # Flush the in-memory monitoring buffer first so the query sees
    # everything that's been recorded since the last periodic flush.
    # Use naive datetime — usage_events.created_at is TIMESTAMP WITHOUT
    # TIME ZONE so passing tz-aware here would crash asyncpg.
    await flush_buffer()
    today_start = datetime.combine(today, datetime.min.time())
    from sqlalchemy import case as sa_case

    today_stmt = (
        select(
            UsageEvent.service,
            func.sum(UsageEvent.cost_usd).label("total_cost_usd"),
            func.sum(UsageEvent.input_tokens).label("total_input_tokens"),
            func.sum(UsageEvent.output_tokens).label("total_output_tokens"),
            func.count().label("total_api_calls"),
            func.sum(
                sa_case((UsageEvent.success == False, 1), else_=0)  # noqa: E712
            ).label("total_failed_calls"),
        )
        .where(UsageEvent.created_at >= today_start)
        .group_by(UsageEvent.service)
    )
    today_rows = (await session.execute(today_stmt)).all()

    # 3. Merge historical + today by service
    services_map: dict[str, dict] = {}

    def _merge(row):
        svc = row.service
        d = services_map.setdefault(
            svc,
            {
                "service": svc,
                "total_cost_usd": 0.0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_api_calls": 0,
                "total_failed_calls": 0,
            },
        )
        d["total_cost_usd"] += float(row.total_cost_usd or 0)
        d["total_input_tokens"] += int(row.total_input_tokens or 0)
        d["total_output_tokens"] += int(row.total_output_tokens or 0)
        d["total_api_calls"] += int(row.total_api_calls or 0)
        d["total_failed_calls"] += int(row.total_failed_calls or 0)

    for row in historical_rows:
        _merge(row)
    for row in today_rows:
        _merge(row)

    # 4. Round costs and compute total
    services = []
    total_cost = 0.0
    for d in services_map.values():
        d["total_cost_usd"] = round(d["total_cost_usd"], 6)
        total_cost += d["total_cost_usd"]
        services.append(d)

    # 5. Append static AWS infrastructure cost estimate.
    # Source: docs/aws_architecture.md and CLAUDE.md — dev env runs ~50 hrs/wk
    # at ~$20/month total (ECS Fargate Spot + RDS + fck-nat + S3 + Redis ECS).
    # When prod is provisioned, switch to ~$108/month.
    # Replace with AWS Cost Explorer API integration in P5+.
    environment = os.getenv("ENVIRONMENT", "development").lower()
    if "prod" in environment:
        infra_monthly = 108.0
        infra_label = "AWS Infrastructure (prod, estimated)"
    else:
        infra_monthly = 20.0
        infra_label = "AWS Infrastructure (dev, estimated)"
    infra_cost = round((infra_monthly / 30.0) * days, 6)
    services.append(
        {
            "service": infra_label,
            "total_cost_usd": infra_cost,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_api_calls": 0,
            "total_failed_calls": 0,
            "estimated": True,
        }
    )
    total_cost += infra_cost

    # Total bots and users
    bot_count = (await session.execute(select(func.count(Bot.id)))).scalar() or 0

    return {
        "days": days,
        "total_cost_usd": round(total_cost, 6),
        "services": services,
        "total_bots": bot_count,
    }


@router.post("/admin/aggregate-now")
async def admin_aggregate_now(
    _admin: User = Depends(require_admin),
):
    """Manually trigger daily cost aggregation for yesterday + today.

    The cron job runs at 3 AM UTC daily and only processes "yesterday".
    This endpoint lets the operator refresh the Costs tab on demand by:
    1. Flushing the in-memory monitoring buffer to PostgreSQL
    2. Aggregating yesterday's events into daily_cost_summary (idempotent)
    3. Aggregating today's events into daily_cost_summary (partial day)

    Returns counts so the caller can verify the aggregation worked.
    """
    flushed = await flush_buffer()
    yesterday = date.today() - timedelta(days=1)
    yesterday_upserted = await aggregate_daily_costs(yesterday)
    today_upserted = await aggregate_daily_costs(date.today())
    return {
        "flushed_events": flushed,
        "yesterday_date": str(yesterday),
        "yesterday_upserted": yesterday_upserted,
        "today_date": str(date.today()),
        "today_upserted": today_upserted,
    }


@router.get("/admin/daily")
async def admin_daily_costs(
    days: int = Query(default=30, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    _admin: User = Depends(require_admin),
):
    """Daily cost breakdown across ALL bots, grouped by date+service (admin only)."""
    start_date = date.today() - timedelta(days=days)

    stmt = (
        select(
            DailyCostSummary.date,
            DailyCostSummary.service,
            func.sum(DailyCostSummary.total_cost_usd).label("total_cost_usd"),
            func.sum(DailyCostSummary.total_api_calls).label("total_api_calls"),
        )
        .where(DailyCostSummary.date >= start_date)
        .group_by(DailyCostSummary.date, DailyCostSummary.service)
        .order_by(DailyCostSummary.date)
    )
    result = await session.execute(stmt)
    rows = result.all()

    daily = []
    for row in rows:
        daily.append(
            {
                "date": str(row.date),
                "service": row.service,
                "total_cost_usd": round(float(row.total_cost_usd or 0), 6),
                "total_api_calls": int(row.total_api_calls or 0),
            }
        )

    return {"days": days, "daily": daily}


@router.get("/admin/leaderboard")
async def admin_leaderboard(
    days: int = Query(default=30, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _admin: User = Depends(require_admin),
):
    """Top N bots by cost across ALL users (admin only)."""
    start_date = date.today() - timedelta(days=days)

    stmt = (
        select(
            DailyCostSummary.bot_id,
            func.sum(DailyCostSummary.total_cost_usd).label("total_cost_usd"),
            func.sum(DailyCostSummary.total_api_calls).label("total_api_calls"),
            func.sum(DailyCostSummary.total_failed_calls).label("total_failed_calls"),
            func.sum(DailyCostSummary.total_input_tokens).label("total_input_tokens"),
            func.sum(DailyCostSummary.total_output_tokens).label("total_output_tokens"),
        )
        .where(DailyCostSummary.date >= start_date)
        .group_by(DailyCostSummary.bot_id)
        .order_by(func.sum(DailyCostSummary.total_cost_usd).desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.all()

    bot_names = {}
    bot_users = {}
    if rows:
        info_result = await session.execute(
            select(Bot.id, Bot.restaurant_name, Bot.user_id).where(
                Bot.id.in_([r.bot_id for r in rows])
            )
        )
        for row_info in info_result.all():
            bot_names[row_info[0]] = row_info[1]
            bot_users[row_info[0]] = row_info[2]

    bots = []
    for rank, row in enumerate(rows, 1):
        bots.append(
            {
                "rank": rank,
                "bot_id": row.bot_id,
                "restaurant_name": bot_names.get(row.bot_id, "Unknown"),
                "user_id": bot_users.get(row.bot_id),
                "total_cost_usd": round(float(row.total_cost_usd or 0), 6),
                "total_api_calls": int(row.total_api_calls or 0),
                "total_failed_calls": int(row.total_failed_calls or 0),
                "total_input_tokens": int(row.total_input_tokens or 0),
                "total_output_tokens": int(row.total_output_tokens or 0),
            }
        )

    return {"days": days, "bots": bots}


@router.get("/admin/qa/runs")
async def admin_qa_runs(
    limit: int = Query(default=20, ge=1, le=100),
    _admin: User = Depends(require_admin),
):
    """List recent QA test reports from S3 (admin only)."""
    from app.qa_reports import list_qa_reports

    reports = list_qa_reports(limit=limit)
    return {"reports": reports}


@router.get("/admin/qa/run/{filename}")
async def admin_qa_run_detail(
    filename: str,
    _admin: User = Depends(require_admin),
):
    """Fetch a single QA test report from S3 (admin only)."""
    from app.qa_reports import get_qa_report, _reports_prefix

    key = f"{_reports_prefix()}{filename}"
    report = get_qa_report(key)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


# ---------------------------------------------------------------------------
# Corpus Management (admin only) — classifier game + validation + test runs
# ---------------------------------------------------------------------------

CORPUS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "tests", "simulation", "corpus"
)
PROSPECT_POOL_PATH = os.path.join(CORPUS_DIR, "prospect_pool.json")


@router.get("/admin/corpus/pending")
async def admin_corpus_pending(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=5000),
    _admin: User = Depends(require_admin),
):
    """Return pending image URLs for the classifier game (shuffled)."""
    import random as _rng

    pending_path = os.path.join(CORPUS_DIR, "pending_images.json")
    if not os.path.exists(pending_path):
        return {"images": [], "total": 0}

    with open(pending_path, encoding="utf-8") as f:
        all_images = json.load(f)

    _rng.shuffle(all_images)

    return {
        "images": all_images[offset : offset + limit],
        "total": len(all_images),
    }


@router.get("/admin/corpus/stats")
async def admin_corpus_stats(
    _admin: User = Depends(require_admin),
):
    """Return corpus statistics."""
    manifest_path = os.path.join(CORPUS_DIR, "manifest.json")
    if not os.path.exists(manifest_path):
        return {"total": 0, "validated": 0, "rejected": 0, "categories": {}}

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    validated = [e for e in manifest if e.get("validated")]
    rejected = [e for e in manifest if e.get("validated") is False]

    cats = {}
    for e in validated:
        cat = e.get("category", "outros")
        cats[cat] = cats.get(cat, 0) + 1

    return {
        "total": len(manifest),
        "validated": len(validated),
        "rejected": len(rejected),
        "unvalidated": len(manifest) - len(validated) - len(rejected),
        "categories": cats,
    }


@router.post("/admin/corpus/accept")
async def admin_corpus_accept(
    body: dict,
    _admin: User = Depends(require_admin),
):
    """Download and save an accepted menu image to the corpus."""
    import httpx as _httpx

    image_url = body.get("src", "")
    alt = body.get("alt", "")

    if not image_url:
        raise HTTPException(status_code=400, detail="Missing 'src'")

    # Download image
    try:
        resp = _httpx.get(
            image_url,
            timeout=25,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200 or len(resp.content) < 2000:
            raise HTTPException(
                status_code=422,
                detail=f"Download failed: status={resp.status_code}, size={len(resp.content)}",
            )
    except _httpx.HTTPError as e:
        raise HTTPException(status_code=422, detail=f"Download error: {e}")

    # Determine extension
    ct = resp.headers.get("content-type", "")
    if "png" in ct:
        ext = ".png"
    elif "webp" in ct:
        ext = ".webp"
    else:
        ext = ".jpg"

    # Load manifest
    manifest_path = os.path.join(CORPUS_DIR, "manifest.json")
    images_dir = os.path.join(CORPUS_DIR, "images")
    os.makedirs(images_dir, exist_ok=True)

    manifest = []
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)

    # Generate ID
    n = len(manifest) + 1
    image_id = f"menu_{n:04d}"
    filename = f"{image_id}{ext}"
    filepath = os.path.join(images_dir, filename)

    # Save image
    with open(filepath, "wb") as f:
        f.write(resp.content)

    # Update manifest. validated=None means "never tried" (the validate
    # script picks these up). validated=True means "passed validation".
    # validated=False is reserved for entries the validator rejected and
    # moved to rejected/.
    # menu_type tracks the source quality tier:
    #   "template"        — design templates (DDG image scrape, what we have today)
    #   "real_restaurant" — iFood/Rappi/Google Maps screenshots
    #   "phone_photo"     — handwritten or phone-captured menus
    # The accept endpoint defaults to "template" because it's hit by the
    # classifier game which scrapes design templates. Real menu / phone photo
    # entries should be re-tagged after upload.
    manifest.append(
        {
            "id": image_id,
            "file": f"images/{filename}",
            "category": "",
            "restaurant_name": (alt or "")[:60],
            "source_url": image_url[:500],
            "source_type": "classifier_game",
            "menu_type": "template",
            "added_date": date.today().isoformat(),
            "file_size_kb": len(resp.content) // 1024,
            "product_count": 0,
            "validated": None,
            "last_used": None,
        }
    )

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    size_kb = len(resp.content) // 1024
    logger.info("Corpus image saved: %s (%dKB) — %s", image_id, size_kb, alt[:40])

    return {"id": image_id, "file": filename, "size_kb": size_kb, "total": n}


@router.post("/admin/corpus/validate")
async def admin_corpus_validate(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Validate unvalidated corpus images via Cadastro Mágico extraction.

    Uses async subprocess so the event loop isn't blocked (avoids deadlock
    when the script calls back into this API).
    """
    manifest_path = os.path.join(CORPUS_DIR, "manifest.json")
    if not os.path.exists(manifest_path):
        return {"validated": 0, "rejected": 0, "message": "No manifest found"}

    project_root = os.path.dirname(os.path.dirname(__file__))
    proc = await asyncio.create_subprocess_exec(
        "python",
        "tests/simulation/corpus/validate_corpus.py",
        cwd=project_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ},
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)

    return {
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "returncode": proc.returncode,
    }


@router.post("/admin/corpus/run-tests", status_code=202)
async def admin_corpus_run_tests(
    request: Request,
    samples: int = Query(
        default=None,
        ge=1,
        le=10,
        description=(
            "Per-restaurant sample count (Pillar 1, F8). N=1 keeps the "
            "historical single-attempt behavior. N=3 cuts per-scenario "
            "stddev by ~1.7x by re-running pytest 3 times against the same "
            "test data with fresh phone numbers each time. Higher N = "
            "longer runs (~1-2 min per sample). "
            "Accepted via query param OR JSON body."
        ),
    ),
    pool: str = Query(
        default=None,
        regex="^(random|baseline|prospect)$",
        description=(
            "Restaurant selection mode (Phase 1C). 'random' picks 5 random "
            "validated restaurants from the corpus (broad-baseline check). "
            "'baseline' uses the locked 10-restaurant pool from "
            "baseline_pool.json (clean per-fix attribution — same restaurants "
            "every run, so deltas are real not sampling lottery). "
            "'prospect' uses prospect_pool.json entries for demo verification. "
            "Accepted via query param OR JSON body."
        ),
    ),
    _admin: User = Depends(require_admin),
):
    """Run QA simulation tests against the validated corpus.

    Fire-and-forget: acquires the Redis lock, launches the subprocess with
    stdout/stderr redirected to a log file (not a pipe), and returns HTTP
    202 immediately. The subprocess owns the lock and releases it when
    done. The frontend polls GET /admin/corpus/run-tests/status and
    fetches the report from S3 when the lock is released.

    Previous design blocked the HTTP response for the full run duration
    (15-50 min for samples=5). This caused two failure modes:
    1. Uvicorn --reload kills the worker (and its child subprocess) when
       code files change, aborting the run mid-flight.
    2. Browser/ngrok connection timeout causes the asyncio task to cancel;
       the subprocess pipe fills up (64KB buffer), print() blocks, and the
       run never reaches _upload_report().

    By redirecting output to a file and returning immediately, the
    subprocess is independent of the HTTP connection lifecycle and survives
    uvicorn reloads (it's a separate OS process with no pipe dependency).
    """
    # Accept samples/pool from JSON body as well as query params.
    # The frontend sends POST with a JSON body; query-param-only meant
    # every frontend run silently defaulted to samples=1.
    if samples is None or pool is None:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if samples is None:
            samples = body.get("samples", 1)
        if pool is None:
            pool = body.get("pool", "random")
    # Clamp / validate after merge
    samples = max(1, min(10, int(samples)))
    if pool not in ("random", "baseline", "prospect"):
        pool = "random"

    from app import qa_lock

    acquired, holder = await qa_lock.try_acquire()
    if not acquired:
        if holder is None:
            raise HTTPException(
                status_code=503,
                detail="QA run lock unavailable (Redis unreachable). Try again.",
            )
        elapsed_min = max(
            0, int((__import__("time").time() - holder.get("started_at", 0)) / 60)
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"A QA run is already in progress (started ~{elapsed_min} min ago, "
                f"owner={holder.get('owner_id')}). Wait for it to finish before starting another."
            ),
        )

    owner_id = holder["owner_id"]
    project_root = os.path.dirname(os.path.dirname(__file__))
    log_path = os.path.join(project_root, ".qa_run_output.log")
    log_file = open(log_path, "w", encoding="utf-8")

    cmd = [
        "python",
        "tests/simulation/corpus/run_corpus_qa.py",
        f"--samples={samples}",
        f"--pool={pool}",
    ]
    # The subprocess owns the lock: CORPUS_LOCK_OWNED is NOT set, so the
    # script will skip its own acquire (it sees the existing lock via Redis
    # and the owner_id matches). The script releases the lock in its own
    # finally block when done.
    subprocess_env = {
        **os.environ,
        "CORPUS_LOCK_OWNED": "1",
        "CORPUS_LOCK_OWNER_ID": owner_id,
        "PYTHONUNBUFFERED": "1",  # flush stdout immediately → live log_tail
    }
    # start_new_session=True detaches the subprocess from uvicorn's process
    # group so it survives --reload kills. stdout/stderr go to a file (not
    # a pipe) so there is no 64KB buffer deadlock.
    await asyncio.create_subprocess_exec(
        *cmd,
        cwd=project_root,
        stdout=log_file,
        stderr=log_file,
        env=subprocess_env,
        start_new_session=True,
    )
    # Don't close log_file here — the subprocess inherited the fd and will
    # write to it until it exits. Closing it in this process just releases
    # OUR reference; the subprocess keeps its own fd open.
    log_file.close()

    return {
        "status": "started",
        "samples": samples,
        "pool": pool,
        "owner_id": owner_id,
        "message": (
            "QA run launched in the background. "
            "Poll GET /admin/corpus/run-tests/status for progress, "
            "then fetch the report from GET /admin/qa/runs once the lock is released."
        ),
    }


@router.get("/admin/corpus/run-tests/status")
async def admin_corpus_run_status(_admin: User = Depends(require_admin)):
    """Return whether a QA run is currently in progress, plus holder info.

    Used by the frontend to disable the Run button while a sibling tab,
    a CLI invocation, or a previous click is still running. Also returns
    the tail of the run log so the frontend can show live progress.
    """
    from app import qa_lock
    import time as _time

    holder = await qa_lock.get_holder()
    if holder is None:
        return {"in_progress": False}

    # Read tail of the log file for live progress
    log_tail = ""
    log_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), ".qa_run_output.log"
    )
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            # Read last 4KB for a reasonable progress snapshot
            f.seek(0, 2)  # seek to end
            size = f.tell()
            f.seek(max(0, size - 4096))
            log_tail = f.read()
    except FileNotFoundError:
        pass

    return {
        "in_progress": True,
        "owner_id": holder.get("owner_id"),
        "started_at": holder.get("started_at"),
        "elapsed_seconds": int(_time.time() - holder.get("started_at", _time.time())),
        "log_tail": log_tail,
    }


# ---------------------------------------------------------------------------
# Prospect Pool Management (admin only) — sales demo preparation
# ---------------------------------------------------------------------------


def _load_prospect_pool() -> dict:
    """Load prospect_pool.json, returning {"version": 1, "entries": []}."""
    if not os.path.exists(PROSPECT_POOL_PATH):
        return {"version": 1, "entries": []}
    with open(PROSPECT_POOL_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_prospect_pool(pool: dict) -> None:
    with open(PROSPECT_POOL_PATH, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)


def _next_prospect_id(pool: dict) -> str:
    """Generate next prospect_NNNN id."""
    existing = [e["id"] for e in pool.get("entries", [])]
    n = 1
    while f"prospect_{n:04d}" in existing:
        n += 1
    return f"prospect_{n:04d}"


@router.get("/admin/prospect/{prospect_id}/products")
async def admin_prospect_products(
    prospect_id: str,
    _admin: User = Depends(require_admin),
):
    """Fetch extracted products for a prospect."""
    pool = _load_prospect_pool()
    entry = next((e for e in pool["entries"] if e["id"] == prospect_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Prospect not found")

    extraction_path = os.path.join(CORPUS_DIR, entry.get("extraction_file", ""))
    if not os.path.exists(extraction_path):
        raise HTTPException(status_code=404, detail="Extraction file not found")

    with open(extraction_path, encoding="utf-8") as f:
        extraction = json.load(f)

    return {
        "id": prospect_id,
        "restaurant_name": entry["restaurant_name"],
        "category": extraction.get("category", ""),
        "products": extraction.get("products", []),
    }


@router.get("/admin/prospect/pool")
async def admin_prospect_pool(
    _admin: User = Depends(require_admin),
):
    """List all prospect entries with their current status."""
    pool = _load_prospect_pool()
    return {"entries": pool.get("entries", [])}


@router.post("/admin/prospect/add")
async def admin_prospect_add(
    restaurant_name: str = Form(...),
    category: str = Form(""),
    city: str = Form(""),
    image_url: str = Form(""),
    files: list[UploadFile] = File(None),
    _admin: User = Depends(require_admin),
):
    """Add a prospect restaurant: save menu image(s), extract products via gpt-4o-mini.

    Accepts up to 10 image file uploads or a single image URL. Products from
    multiple images are extracted in parallel and deduplicated. Returns the
    extracted products for review before creating a demo bot.
    """
    import httpx as _httpx
    import unicodedata

    from app.openai_client import extract_products_from_image

    # --- 1. Collect image bytes ---
    image_list: list[tuple[bytes, str, str]] = []  # (bytes, ext, mime)

    has_files = files and any(f.filename for f in files)
    if has_files:
        if len(files) > 10:
            raise HTTPException(
                status_code=400, detail="Maximum 10 images per prospect."
            )
        for f in files:
            if not f.filename:
                continue
            data = await f.read()
            if len(data) < 2000:
                raise HTTPException(
                    status_code=422, detail=f"Image too small: '{f.filename}'"
                )
            ct = f.content_type or ""
            ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
            mime = (
                "image/png"
                if ext == ".png"
                else "image/webp"
                if ext == ".webp"
                else "image/jpeg"
            )
            image_list.append((data, ext, mime))
    elif image_url:
        try:
            resp = _httpx.get(
                image_url,
                timeout=25,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code != 200 or len(resp.content) < 2000:
                raise HTTPException(
                    status_code=422,
                    detail=f"Download failed: status={resp.status_code}, size={len(resp.content)}",
                )
            ct = resp.headers.get("content-type", "")
            ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
            mime = (
                "image/png"
                if ext == ".png"
                else "image/webp"
                if ext == ".webp"
                else "image/jpeg"
            )
            image_list.append((resp.content, ext, mime))
        except _httpx.HTTPError as e:
            raise HTTPException(status_code=422, detail=f"Download error: {e}")
    else:
        raise HTTPException(
            status_code=400, detail="Provide either 'files' or 'image_url'"
        )

    # --- 2. Save images ---
    pool = _load_prospect_pool()
    prospect_id = _next_prospect_id(pool)

    images_dir = os.path.join(CORPUS_DIR, "images")
    os.makedirs(images_dir, exist_ok=True)

    saved_files: list[str] = []
    for i, (data, ext, _mime) in enumerate(image_list):
        suffix = f"_{i + 1}" if len(image_list) > 1 else ""
        filename = f"{prospect_id}{suffix}{ext}"
        filepath = os.path.join(images_dir, filename)
        with open(filepath, "wb") as fp:
            fp.write(data)
        saved_files.append(f"images/{filename}")

    # --- 3. Extract products via gpt-4o-mini (parallel for multi-image) ---
    tasks = [extract_products_from_image(data, mime) for data, _ext, mime in image_list]
    results = await asyncio.gather(*tasks)

    all_products: list[dict] = []
    for page_products in results:
        if page_products:
            all_products.extend(page_products)

    # Deduplicate across images
    if len(image_list) > 1 and all_products:
        seen: dict[str, dict] = {}
        for p in all_products:
            name = p.get("name", "")
            key = (
                unicodedata.normalize("NFKD", name)
                .encode("ascii", "ignore")
                .decode()
                .lower()
                .strip()
            )
            if not key:
                continue
            existing = seen.get(key)
            if existing is None or len(p.get("description", "") or "") > len(
                existing.get("description", "") or ""
            ):
                seen[key] = p
        all_products = list(seen.values())

    if not all_products:
        # Cleanup saved images on extraction failure
        for rel in saved_files:
            path = os.path.join(CORPUS_DIR, rel)
            if os.path.exists(path):
                os.remove(path)
        raise HTTPException(status_code=422, detail="Extraction returned no products")

    # --- 4. Detect category ---
    cat_keywords = {
        "pizzaria": ["pizza", "margherita", "calabresa", "pepperoni", "mussarela"],
        "hamburgueria": ["burger", "hambúrguer", "hamburger", "smash", "cheeseburger"],
        "sushi": ["temaki", "sashimi", "sushi", "uramaki"],
        "pastelaria": ["pastel"],
        "açaiteria": ["açaí", "acai", "tigela"],
        "lanchonete": ["x-burger", "x-tudo", "x-salada", "lanche"],
        "padaria": ["pão", "croissant", "bolo", "focaccia"],
        "marmitaria": ["marmita", "parmegiana", "executivo", "prato feito"],
        "cafeteria": ["café", "espresso", "latte", "cappuccino"],
        "doceria": ["brigadeiro", "trufa", "doce", "brownie", "torta"],
        "espetaria": ["espetinho", "espeto", "churrasco"],
    }
    if not category:
        text = " ".join(
            f"{p.get('name', '')} {p.get('category', '')} {p.get('description', '')}"
            for p in all_products
        ).lower()
        scores = {
            cat: sum(1 for kw in kws if kw in text) for cat, kws in cat_keywords.items()
        }
        best = max(scores, key=scores.get)
        category = best if scores[best] > 0 else "outros"

    # --- 5. Save extraction JSON ---
    extractions_dir = os.path.join(CORPUS_DIR, "extractions")
    os.makedirs(extractions_dir, exist_ok=True)
    extraction_path = os.path.join(extractions_dir, f"{prospect_id}.json")
    with open(extraction_path, "w", encoding="utf-8") as fp:
        json.dump(
            {"products": all_products, "category": category},
            fp,
            ensure_ascii=False,
            indent=2,
        )

    # --- 6. Update prospect pool ---
    entry = {
        "id": prospect_id,
        "restaurant_name": restaurant_name[:60],
        "category": category,
        "city": city[:60],
        "image_file": saved_files[0],
        "image_files": saved_files,
        "extraction_file": f"extractions/{prospect_id}.json",
        "product_count": len(all_products),
        "demo_bot_id": None,
        "status": "extracted",
        "added_date": date.today().isoformat(),
    }
    pool["entries"].append(entry)
    _save_prospect_pool(pool)

    logger.info(
        "Prospect added: %s (%s) — %d image(s), %d products, category=%s",
        prospect_id,
        restaurant_name[:30],
        len(image_list),
        len(all_products),
        category,
    )

    return {
        "id": prospect_id,
        "restaurant_name": restaurant_name,
        "category": category,
        "product_count": len(all_products),
        "products": all_products,
        "image_count": len(image_list),
    }


@router.post("/admin/prospect/{prospect_id}/demo")
async def admin_prospect_create_demo(
    prospect_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Create a persistent demo bot from a prospect's extracted products."""
    import re
    import time as _time
    import unicodedata

    from app.crud import create_bot, create_product
    from app.models import Subscription

    pool = _load_prospect_pool()
    entry = next((e for e in pool["entries"] if e["id"] == prospect_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Prospect not found")

    if entry.get("demo_bot_id"):
        raise HTTPException(
            status_code=409,
            detail=f"Demo bot already active (bot_id={entry['demo_bot_id']})",
        )

    # Load extraction
    extraction_path = os.path.join(CORPUS_DIR, entry["extraction_file"])
    if not os.path.exists(extraction_path):
        raise HTTPException(status_code=404, detail="Extraction file not found")

    with open(extraction_path, encoding="utf-8") as f:
        extraction = json.load(f)
    products = extraction.get("products", [])

    if not products:
        raise HTTPException(status_code=422, detail="No products in extraction")

    # Generate slug for phone_number_id
    name = entry.get("restaurant_name", prospect_id)
    slug = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")[:30]
    ts = int(_time.time())
    phone_id = f"prospect-{slug}-{ts}"

    # Create bot
    bot = await create_bot(
        session,
        user_id=_admin.id,
        restaurant_name=f"Demo · {name[:50]}",
        phone_number_id=phone_id,
        whatsapp_token="demo-token",
        whatsapp_number=f"55prospect{ts % 999999:06d}",
        is_open=True,
    )
    if not bot:
        raise HTTPException(status_code=500, detail="Failed to create demo bot")

    bot_id = bot.id

    # Create products
    created_count = 0
    for p in products:
        price = p.get("price")
        if price is None or price <= 0:
            continue
        await create_product(
            session,
            bot_id=bot_id,
            name=p.get("name", "Unknown"),
            description=p.get("description", ""),
            price=float(price),
            keywords=", ".join(p.get("keywords", []))
            if isinstance(p.get("keywords"), list)
            else p.get("keywords", ""),
            category=p.get("category", "Geral"),
        )
        created_count += 1

    # Insert subscription (30-day demo)
    sub = Subscription(
        bot_id=bot_id,
        user_id=_admin.id,
        mp_subscription_id=f"prospect-sub-{bot_id}",
        status="authorized",
        current_period_end=datetime.now(timezone.utc) + timedelta(days=30),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        plan_type="pro_monthly",
    )
    session.add(sub)
    await session.commit()

    # Update prospect pool
    entry["demo_bot_id"] = bot_id
    entry["status"] = "demo_active"
    _save_prospect_pool(pool)

    logger.info(
        "Prospect demo created: %s → bot_id=%d (%d products)",
        prospect_id,
        bot_id,
        created_count,
    )

    return {
        "bot_id": bot_id,
        "phone_number_id": phone_id,
        "product_count": created_count,
        "status": "demo_active",
    }


@router.delete("/admin/prospect/{prospect_id}/demo")
async def admin_prospect_delete_demo(
    prospect_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Clean up a prospect's demo bot and all its data."""
    from sqlalchemy import text as sa_text

    pool = _load_prospect_pool()
    entry = next((e for e in pool["entries"] if e["id"] == prospect_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Prospect not found")

    bot_id = entry.get("demo_bot_id")
    if not bot_id:
        raise HTTPException(
            status_code=404, detail="No active demo bot for this prospect"
        )

    # Cascade delete (same pattern as validate_corpus.py:delete_bot)
    r = await session.execute(
        sa_text("SELECT id FROM contact WHERE bot_id = :bid"), {"bid": bot_id}
    )
    cids = [row[0] for row in r.fetchall()]
    if cids:
        cids_str = ",".join(str(x) for x in cids)
        r2 = await session.execute(
            sa_text(f"SELECT id FROM shoppingcart WHERE contact_id IN ({cids_str})")
        )
        cart_ids = [row[0] for row in r2.fetchall()]
        if cart_ids:
            carts_str = ",".join(str(x) for x in cart_ids)
            await session.execute(
                sa_text(f"DELETE FROM cartitem WHERE cart_id IN ({carts_str})")
            )
            await session.execute(
                sa_text(f"DELETE FROM shoppingcart WHERE id IN ({carts_str})")
            )
        await session.execute(
            sa_text(f"DELETE FROM conversationhistory WHERE contact_id IN ({cids_str})")
        )
        await session.execute(sa_text(f"DELETE FROM contact WHERE id IN ({cids_str})"))

    await session.execute(
        sa_text("DELETE FROM conversationhistory WHERE bot_id = :bid"), {"bid": bot_id}
    )
    await session.execute(
        sa_text("DELETE FROM product WHERE bot_id = :bid"), {"bid": bot_id}
    )
    await session.execute(
        sa_text("DELETE FROM subscription WHERE bot_id = :bid"), {"bid": bot_id}
    )
    await session.execute(sa_text("DELETE FROM bot WHERE id = :bid"), {"bid": bot_id})
    await session.commit()

    # Update prospect pool
    entry["demo_bot_id"] = None
    entry["status"] = "archived"
    _save_prospect_pool(pool)

    logger.info("Prospect demo cleaned up: %s (bot_id=%d)", prospect_id, bot_id)

    return {"status": "archived", "deleted_bot_id": bot_id}


@router.delete("/admin/prospect/{prospect_id}")
async def admin_prospect_remove(
    prospect_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Remove a prospect entirely — clean up demo bot if active, delete files."""
    pool = _load_prospect_pool()
    entry = next((e for e in pool["entries"] if e["id"] == prospect_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Prospect not found")

    # If demo bot is active, clean it up first
    if entry.get("demo_bot_id"):
        from sqlalchemy import text as sa_text

        bot_id = entry["demo_bot_id"]
        r = await session.execute(
            sa_text("SELECT id FROM contact WHERE bot_id = :bid"), {"bid": bot_id}
        )
        cids = [row[0] for row in r.fetchall()]
        if cids:
            cids_str = ",".join(str(x) for x in cids)
            r2 = await session.execute(
                sa_text(f"SELECT id FROM shoppingcart WHERE contact_id IN ({cids_str})")
            )
            cart_ids = [row[0] for row in r2.fetchall()]
            if cart_ids:
                carts_str = ",".join(str(x) for x in cart_ids)
                await session.execute(
                    sa_text(f"DELETE FROM cartitem WHERE cart_id IN ({carts_str})")
                )
                await session.execute(
                    sa_text(f"DELETE FROM shoppingcart WHERE id IN ({carts_str})")
                )
            await session.execute(
                sa_text(
                    f"DELETE FROM conversationhistory WHERE contact_id IN ({cids_str})"
                )
            )
            await session.execute(
                sa_text(f"DELETE FROM contact WHERE id IN ({cids_str})")
            )
        await session.execute(
            sa_text("DELETE FROM conversationhistory WHERE bot_id = :bid"),
            {"bid": bot_id},
        )
        await session.execute(
            sa_text("DELETE FROM product WHERE bot_id = :bid"), {"bid": bot_id}
        )
        await session.execute(
            sa_text("DELETE FROM subscription WHERE bot_id = :bid"), {"bid": bot_id}
        )
        await session.execute(
            sa_text("DELETE FROM bot WHERE id = :bid"), {"bid": bot_id}
        )
        await session.commit()

    # Delete image file
    image_file = entry.get("image_file", "")
    if image_file:
        image_path = os.path.join(CORPUS_DIR, image_file)
        if os.path.exists(image_path):
            os.remove(image_path)

    # Delete extraction file
    extraction_file = entry.get("extraction_file", "")
    if extraction_file:
        extraction_path = os.path.join(CORPUS_DIR, extraction_file)
        if os.path.exists(extraction_path):
            os.remove(extraction_path)

    # Remove from pool
    pool["entries"] = [e for e in pool["entries"] if e["id"] != prospect_id]
    _save_prospect_pool(pool)

    logger.info("Prospect removed: %s", prospect_id)

    return {"status": "removed", "id": prospect_id}


@router.post("/admin/prospect/{prospect_id}/promote")
async def admin_prospect_promote(
    prospect_id: str,
    request: Request,
    _admin: User = Depends(require_admin),
):
    """Promote a prospect into the corpus manifest as a real_restaurant entry.

    Accepts optional JSON body with `category` override (e.g., fix
    auto-detected "padaria" → "hamburgueria" before promoting).
    """
    pool = _load_prospect_pool()
    entry = next((e for e in pool["entries"] if e["id"] == prospect_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Prospect not found")

    if entry.get("status") == "promoted":
        raise HTTPException(status_code=409, detail="Prospect already promoted")

    # Optional category override from JSON body
    category_override = None
    try:
        body = await request.json()
        category_override = body.get("category")
    except Exception:
        pass

    category = category_override or entry.get("category", "outros")

    # Verify extraction file exists
    extraction_file = entry.get("extraction_file", "")
    extraction_path = os.path.join(CORPUS_DIR, extraction_file)
    if not os.path.exists(extraction_path):
        raise HTTPException(status_code=404, detail="Extraction file not found")

    # Load manifest
    manifest_path = os.path.join(CORPUS_DIR, "manifest.json")
    manifest = []
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)

    # Check for duplicate
    if any(e["id"] == prospect_id for e in manifest):
        raise HTTPException(status_code=409, detail="Entry already exists in manifest")

    # Build manifest entry
    manifest_entry = {
        "id": prospect_id,
        "file": entry.get("image_file", ""),
        "category": category,
        "restaurant_name": entry.get("restaurant_name", ""),
        "source_url": "",
        "source_type": "prospect_promotion",
        "menu_type": "real_restaurant",
        "added_date": entry.get("added_date", date.today().isoformat()),
        "file_size_kb": 0,
        "product_count": entry.get("product_count", 0),
        "validated": True,
        "last_used": None,
    }

    # Calculate file size
    image_path = os.path.join(CORPUS_DIR, entry.get("image_file", ""))
    if os.path.exists(image_path):
        manifest_entry["file_size_kb"] = os.path.getsize(image_path) // 1024

    manifest.append(manifest_entry)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # Add directly to baseline_pool.json — the whole point of promoting
    baseline_path = os.path.join(CORPUS_DIR, "baseline_pool.json")
    baseline_pool_entry = {
        "id": prospect_id,
        "category": category,
        "restaurant_name": entry.get("restaurant_name", ""),
        "menu_type": "real_restaurant",
        "product_count": entry.get("product_count", 0),
    }
    added_to_baseline = False
    if os.path.exists(baseline_path):
        with open(baseline_path, encoding="utf-8") as f:
            baseline = json.load(f)

        # Replace a template entry in the same category if one exists,
        # otherwise append
        replaced_idx = None
        for i, be in enumerate(baseline.get("entries", [])):
            if (
                be.get("category") == category
                and be.get("menu_type", "template") == "template"
            ):
                replaced_idx = i
                break

        if replaced_idx is not None:
            old = baseline["entries"][replaced_idx]
            baseline["entries"][replaced_idx] = baseline_pool_entry
            added_to_baseline = True
            logger.info(
                "Baseline: replaced template %s with real %s in category %s",
                old["id"],
                prospect_id,
                category,
            )
        else:
            # No template to replace — append (grows the pool)
            baseline["entries"].append(baseline_pool_entry)
            baseline["size"] = len(baseline["entries"])
            added_to_baseline = True
            logger.info(
                "Baseline: appended real %s in category %s (no template to replace)",
                prospect_id,
                category,
            )

        with open(baseline_path, "w", encoding="utf-8") as f:
            json.dump(baseline, f, ensure_ascii=False, indent=2)

    # Update prospect pool status
    entry["status"] = "promoted"
    entry["category"] = category  # persist the override
    _save_prospect_pool(pool)

    logger.info(
        "Prospect promoted to corpus: %s (%s) — category=%s, %d products, baseline=%s",
        prospect_id,
        entry.get("restaurant_name", "")[:30],
        category,
        entry.get("product_count", 0),
        added_to_baseline,
    )

    return {
        "status": "promoted",
        "id": prospect_id,
        "category": category,
        "added_to_baseline": added_to_baseline,
        "manifest_entry": manifest_entry,
    }


@router.delete("/admin/prospect/{prospect_id}/promote")
async def admin_prospect_unpromote(
    prospect_id: str,
    _admin: User = Depends(require_admin),
):
    """Remove a prospect from the corpus manifest (undo promotion)."""
    pool = _load_prospect_pool()
    entry = next((e for e in pool["entries"] if e["id"] == prospect_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Prospect not found")

    if entry.get("status") != "promoted":
        raise HTTPException(status_code=409, detail="Prospect is not promoted")

    # Remove from manifest
    manifest_path = os.path.join(CORPUS_DIR, "manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        manifest = [e for e in manifest if e["id"] != prospect_id]
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

    # Remove from baseline_pool.json too
    baseline_path = os.path.join(CORPUS_DIR, "baseline_pool.json")
    if os.path.exists(baseline_path):
        with open(baseline_path, encoding="utf-8") as f:
            baseline = json.load(f)
        baseline["entries"] = [e for e in baseline["entries"] if e["id"] != prospect_id]
        baseline["size"] = len(baseline["entries"])
        with open(baseline_path, "w", encoding="utf-8") as f:
            json.dump(baseline, f, ensure_ascii=False, indent=2)

    # Reset prospect status
    entry["status"] = "extracted"
    _save_prospect_pool(pool)

    logger.info("Prospect unpromoted: %s", prospect_id)

    return {"status": "extracted", "id": prospect_id}
