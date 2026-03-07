import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app import crud, schemas
from app.auth import require_admin
from app.database import get_session
from app.models import Bot, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])


@router.get("/plans", response_model=List[schemas.PlanResponse])
async def list_plans(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    return await crud.list_plans(session)


@router.post("/plans", response_model=schemas.PlanResponse, status_code=201)
async def create_plan(
    payload: schemas.PlanCreate,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    existing = await crud.get_plan_by_key(session, payload.key)
    if existing:
        raise HTTPException(status_code=409, detail="Plan key already exists.")
    plan = await crud.create_plan(session, payload.model_dump())
    return plan


@router.put("/plans/{plan_id}", response_model=schemas.PlanResponse)
async def update_plan(
    plan_id: int,
    payload: schemas.PlanUpdate,
    _admin: User = Depends(require_admin),
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
    return plan


@router.delete("/plans/{plan_id}", status_code=204)
async def delete_plan(
    plan_id: int,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    deleted = await crud.delete_plan(session, plan_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Plan not found.")


@router.get("/users/{user_id}/bots", response_model=List[schemas.AdminBotSummary])
async def list_user_bots(
    user_id: int,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    result = await session.execute(select(Bot).where(Bot.user_id == user_id))
    return result.scalars().all()


@router.get("/users/by-email/{email}/bots", response_model=List[schemas.AdminBotSummary])
async def list_user_bots_by_email(
    email: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    bots_result = await session.execute(select(Bot).where(Bot.user_id == user.id))
    return bots_result.scalars().all()


@router.put("/subscriptions/{bot_id}", response_model=schemas.SubscriptionStatusResponse)
async def admin_upsert_subscription(
    bot_id: int,
    payload: schemas.AdminUpsertSubscription,
    _admin: User = Depends(require_admin),
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

    sub = await crud.upsert_subscription(
        session=session,
        user_id=bot.user_id,
        bot_id=bot.id,
        mp_id=f"admin_grant_{bot.id}",
        status=payload.status,
        plan_type=payload.plan_type,
    )
    await session.commit()

    remaining = (sub.current_period_end - datetime.now(timezone.utc)).days
    return {
        "status": sub.status,
        "is_active": sub.status == "authorized" and remaining > -3,
        "days_remaining": max(0, remaining),
        "next_payment": sub.current_period_end,
        "plan_type": sub.plan_type,
    }
