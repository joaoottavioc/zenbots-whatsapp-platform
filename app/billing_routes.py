# app/billing_routes.py

import logging
import os
import mercadopago

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from datetime import datetime, timezone  # <--- Adicione timezone aqui

from app.database import get_session
from app.auth import get_current_user
from app.models import User, Plan, Bot
from app import crud, schemas
from app.webhook_security import require_mp_signature


router = APIRouter(prefix="/billing", tags=["SaaS Billing"])

# SDK com SEU token de admin (quem recebe o dinheiro da assinatura)
# Lazy initialization to avoid import-time errors when env var is not set (e.g., tests)
_sdk = None


def _get_sdk():
    global _sdk
    if _sdk is None:
        token = os.getenv("MP_ADMIN_ACCESS_TOKEN", "")
        _sdk = mercadopago.SDK(token)
    return _sdk


@router.get("/plans", response_model=list[schemas.PlanResponse])
async def list_plans(session: AsyncSession = Depends(get_session)):
    """Public endpoint — returns all available plans (no auth required)."""
    return await crud.list_plans(session)


@router.post("/checkout")
async def create_checkout(
    req: schemas.CheckoutRequest,  # Recebe o JSON novo
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    # 1. Valida se o Bot existe e pertence ao usuário
    bot = await session.get(Bot, req.bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado.")
    if bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Este bot não pertence a você.")

    # 2. Busca o Plano
    query = select(Plan).where(Plan.key == req.plan_key)
    result = await session.execute(query)
    selected_plan = result.scalars().first()

    if not selected_plan:
        raise HTTPException(status_code=404, detail="Plano não encontrado")

    # 3. Cria Preferência no Mercado Pago
    subscription_data = {
        "reason": f"{selected_plan.title} - {bot.restaurant_name}",
        "auto_recurring": {
            "frequency": 1,
            "frequency_type": "months",
            "transaction_amount": selected_plan.price,
            "currency_id": "BRL",
        },
        "payer_email": current_user.email,
        # Encode bot_id and plan_key so the webhook can extract both
        "external_reference": f"BOT_{bot.id}_{req.plan_key}",
        "back_url": f"{os.getenv('BASE_URL', 'http://localhost:3000')}/dashboard/settings",
        "status": "pending",
    }

    try:
        result = _get_sdk().preapproval().create(subscription_data)

        if result["status"] != 201:
            error_detail = result.get("response", {}).get(
                "message", "Erro desconhecido"
            )
            logger.error("Mercado Pago preapproval creation failed: %s", error_detail)
            raise HTTPException(status_code=400, detail=f"Erro MP: {error_detail}")

        checkout_link = result["response"]["init_point"]
        return {"checkout_url": checkout_link}

    except Exception as e:
        logger.exception("Checkout creation failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/webhook")
async def billing_webhook(
    request: Request, session: AsyncSession = Depends(get_session)
):
    try:
        data = await request.json()

        # --- Signature verification (P0-1) ---
        data_id = str(data.get("data", {}).get("id", ""))
        await require_mp_signature(request, data_id)

        topic = data.get("topic") or data.get("type")

        if topic == "subscription_preapproval":
            preapproval_id = data.get("data", {}).get("id")

            # Busca status atualizado no MP
            sub_info = _get_sdk().preapproval().get(preapproval_id)["response"]
            status = sub_info["status"]
            external_ref = sub_info["external_reference"]  # Ex: "BOT_12_pro"

            # Parse external_reference: "BOT_{id}_{plan_key}" or legacy "BOT_{id}"
            if external_ref and external_ref.startswith("BOT_"):
                parts = external_ref.split("_", 2)  # ["BOT", "12", "pro"]
                bot_id = int(parts[1])
                plan_key = parts[2] if len(parts) > 2 else "pro"

                bot = await session.get(Bot, bot_id)
                if bot:
                    await crud.upsert_subscription(
                        session=session,
                        user_id=bot.user_id,
                        bot_id=bot.id,
                        mp_id=preapproval_id,
                        status=status,
                        plan_type=plan_key,
                    )
                    await session.commit()
                    logger.info(
                        "Subscription updated for bot_id=%s, status=%s, plan=%s",
                        bot_id, status, plan_key,
                    )

        return {"status": "ok"}
    except Exception:
        logger.exception("Billing webhook processing failed")
        return {"status": "error"}


@router.get("/status", response_model=schemas.SubscriptionStatusResponse)
async def check_subscription_status(
    bot_id: int,  # <--- Agora exige bot_id na URL
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    # Verifica se o bot é do usuário
    bot = await session.get(Bot, bot_id)
    if not bot or bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado ao bot.")

    # Busca a assinatura deste bot específico
    sub = await crud.get_subscription_by_bot(session, bot_id)

    if not sub:
        return {
            "status": "inactive",
            "is_active": False,
            "days_remaining": 0,
            "next_payment": datetime.now(timezone.utc),
            "plan_type": "free",
        }

    remaining = (sub.current_period_end - datetime.now(timezone.utc)).days
    is_active = sub.status == "authorized" and remaining > -3

    return {
        "status": sub.status,
        "is_active": is_active,
        "days_remaining": max(0, remaining),
        "next_payment": sub.current_period_end,
        "plan_type": sub.plan_type,
    }
