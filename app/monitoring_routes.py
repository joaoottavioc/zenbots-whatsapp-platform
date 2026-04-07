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

from app.auth import get_current_user, require_admin
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


# --- Admin-only endpoints (require is_admin=True) ---


@router.get("/admin/overview")
async def admin_overview(
    days: int = Query(default=30, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    _admin: User = Depends(require_admin),
):
    """Aggregate cost overview across ALL bots (admin only)."""
    start_date = date.today() - timedelta(days=days)

    stmt = (
        select(
            DailyCostSummary.service,
            func.sum(DailyCostSummary.total_cost_usd).label("total_cost_usd"),
            func.sum(DailyCostSummary.total_input_tokens).label("total_input_tokens"),
            func.sum(DailyCostSummary.total_output_tokens).label("total_output_tokens"),
            func.sum(DailyCostSummary.total_api_calls).label("total_api_calls"),
            func.sum(DailyCostSummary.total_failed_calls).label("total_failed_calls"),
        )
        .where(DailyCostSummary.date >= start_date)
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
            }
        )

    # Total bots and users
    bot_count = (await session.execute(select(func.count(Bot.id)))).scalar() or 0

    return {
        "days": days,
        "total_cost_usd": round(total_cost, 6),
        "services": services,
        "total_bots": bot_count,
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


@router.post("/admin/corpus/run-tests")
async def admin_corpus_run_tests(
    _admin: User = Depends(require_admin),
):
    """Run QA simulation tests against the validated corpus.

    Uses async subprocess so the event loop isn't blocked (avoids deadlock
    when the script calls back into this API to create bots).
    """
    project_root = os.path.dirname(os.path.dirname(__file__))
    proc = await asyncio.create_subprocess_exec(
        "python",
        "tests/simulation/corpus/run_corpus_qa.py",
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
