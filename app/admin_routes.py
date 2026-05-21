import logging
from datetime import datetime, timezone
from typing import List
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app import billing_cache, crud, schemas
from app.auth import require_admin
from app.database import get_session
from app.models import Bot, User
from app.rate_limiter import is_rate_limited

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])


async def _admin_rate_limit(admin: User = Depends(require_admin)) -> User:
    """Shared rate-limit gate for every admin endpoint (30 req/min per admin)."""
    if await is_rate_limited(f"rl:admin:{admin.id}", limit=30, window_seconds=60):
        logger.warning("ADMIN_RATE_LIMIT_HIT admin_id=%s", admin.id)
        raise HTTPException(status_code=429, detail="Too many admin requests.")
    return admin


@router.get("/plans", response_model=List[schemas.PlanResponse])
async def list_plans(
    _admin: User = Depends(_admin_rate_limit),
    session: AsyncSession = Depends(get_session),
):
    return await crud.list_plans(session)


@router.post("/plans", response_model=schemas.PlanResponse, status_code=201)
async def create_plan(
    payload: schemas.PlanCreate,
    admin: User = Depends(_admin_rate_limit),
    session: AsyncSession = Depends(get_session),
):
    existing = await crud.get_plan_by_key(session, payload.key)
    if existing:
        raise HTTPException(status_code=409, detail="Plan key already exists.")
    plan = await crud.create_plan(session, payload.model_dump())
    logger.info(
        "ADMIN_PLAN_CREATE admin_id=%s plan_id=%s key=%s",
        admin.id,
        plan.id,
        plan.key,
    )
    return plan


@router.put("/plans/{plan_id}", response_model=schemas.PlanResponse)
async def update_plan(
    plan_id: int,
    payload: schemas.PlanUpdate,
    admin: User = Depends(_admin_rate_limit),
    session: AsyncSession = Depends(get_session),
):
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update.")

    # If key is being changed, check uniqueness
    if "key" in update_data:
        existing = await crud.get_plan_by_key(session, update_data["key"])
        if existing and existing.id != plan_id:
            raise HTTPException(status_code=409, detail="Plan key already exists.")

    plan = await crud.update_plan(session, plan_id, update_data)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found.")
    logger.info(
        "ADMIN_PLAN_UPDATE admin_id=%s plan_id=%s fields=%s",
        admin.id,
        plan_id,
        sorted(update_data.keys()),
    )
    return plan


@router.delete("/plans/{plan_id}", status_code=204)
async def delete_plan(
    plan_id: int,
    admin: User = Depends(_admin_rate_limit),
    session: AsyncSession = Depends(get_session),
):
    deleted = await crud.delete_plan(session, plan_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Plan not found.")
    logger.info("ADMIN_PLAN_DELETE admin_id=%s plan_id=%s", admin.id, plan_id)


@router.get("/users/{user_id}/bots", response_model=List[schemas.AdminBotSummary])
async def list_user_bots(
    user_id: int,
    _admin: User = Depends(_admin_rate_limit),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    result = await session.execute(select(Bot).where(Bot.user_id == user_id))
    return result.scalars().all()


@router.get("/users/bots", response_model=List[schemas.AdminBotSummary])
async def list_user_bots_by_email(
    email: str = Query(..., max_length=320),
    _admin: User = Depends(_admin_rate_limit),
    session: AsyncSession = Depends(get_session),
):
    """Look up a user's bots by email. Email is a query parameter so it
    doesn't land in access logs / browser history / structured-log URL fields.
    """
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    bots_result = await session.execute(select(Bot).where(Bot.user_id == user.id))
    return bots_result.scalars().all()


@router.put(
    "/subscriptions/{bot_id}", response_model=schemas.SubscriptionStatusResponse
)
async def admin_upsert_subscription(
    bot_id: int,
    payload: schemas.AdminUpsertSubscription,
    admin: User = Depends(_admin_rate_limit),
    session: AsyncSession = Depends(get_session),
):
    bot = await session.get(Bot, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found.")

    plan = await crud.get_plan_by_key(session, payload.plan_type)
    if not plan:
        raise HTTPException(
            status_code=400,
            detail=f"Plan '{payload.plan_type}' not found. Create it first via POST /admin/plans.",
        )

    # Reuse the existing admin_grant id for this bot if one is already set —
    # otherwise mint a fresh uuid. Keeps the unique constraint stable across
    # repeat upserts and avoids leaking bot_id through a predictable mp_id.
    existing_sub = await crud.get_subscription_by_bot(session, bot.id)
    if existing_sub and existing_sub.mp_subscription_id.startswith("admin_grant_"):
        mp_id = existing_sub.mp_subscription_id
    else:
        mp_id = f"admin_grant_{uuid4().hex}"

    sub = await crud.upsert_subscription(
        session=session,
        user_id=bot.user_id,
        bot_id=bot.id,
        mp_id=mp_id,
        status=payload.status,
        plan_type=payload.plan_type,
    )
    await session.commit()
    await billing_cache.invalidate_by_bot_id(bot.id)

    logger.info(
        "ADMIN_SUBSCRIPTION_UPSERT admin_id=%s bot_id=%s plan=%s status=%s",
        admin.id,
        bot.id,
        payload.plan_type,
        payload.status,
    )

    remaining = (sub.current_period_end - datetime.now(timezone.utc)).days
    return {
        "status": sub.status,
        "is_active": sub.status == "authorized" and remaining > -3,
        "days_remaining": max(0, remaining),
        "next_payment": sub.current_period_end,
        "plan_type": sub.plan_type,
    }
