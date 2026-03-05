import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from app import crud, schemas
from app.auth import require_admin
from app.database import get_session
from app.models import User

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
