# app/billing_routes.py

import logging
import os
import mercadopago
import pytz

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from datetime import datetime, timezone  # <--- Adicione timezone aqui

from app.database import get_session
from app.auth import get_current_user
from app.models import User, Plan, Bot
from app import crud, schemas, billing_cache, founder
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


@router.get("/founder-remaining")
async def founder_remaining():
    """Public — Founder promo state for the landing-page counter banner."""
    return {
        "remaining": await founder.slots_remaining(),
        "total_slots": founder.FOUNDER_LIMIT,
        "sunset_at": founder.sunset_date().isoformat(),
        "available": await founder.is_available(),
        "price_brl": founder.FOUNDER_PRICE_BRL,
    }


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

    # 3. Handle existing subscriptions
    existing_sub = await crud.get_subscription_by_bot(session, req.bot_id)
    if existing_sub:
        # Admin-granted subscriptions are managed via admin endpoints only
        if existing_sub.mp_subscription_id.startswith("admin_grant_"):
            raise HTTPException(
                status_code=409,
                detail="Assinatura gerenciada por administrador.",
            )
        # Same plan already active → block duplicate
        if (
            existing_sub.status == "authorized"
            and existing_sub.plan_type == req.plan_key
        ):
            raise HTTPException(
                status_code=409,
                detail="Este bot já possui uma assinatura ativa.",
            )
        # Pending subscription (clicked but never paid) → cancel old on MP before creating new
        if existing_sub.status == "pending":
            try:
                _get_sdk().preapproval().update(
                    existing_sub.mp_subscription_id, {"status": "cancelled"}
                )
                logger.info(
                    "Cancelled stale pending MP sub %s for bot_id=%s",
                    existing_sub.mp_subscription_id,
                    req.bot_id,
                )
            except Exception:
                logger.warning(
                    "Failed to cancel pending MP sub %s",
                    existing_sub.mp_subscription_id,
                )
        # authorized + different plan = upgrade attempt → allowed, new MP sub created
        # cancelled/paused = re-subscribe → allowed

    # 3b. Founder plan: gate behind slot availability + sunset.
    # Slot is reserved BEFORE creating the MP preapproval so paid promos
    # can't race past 30. Released on MP failure below.
    founder_slot_reserved = False
    if req.plan_key == "founder":
        if founder.sunset_passed():
            raise HTTPException(
                status_code=410,
                detail="Oferta de Fundador encerrada.",
            )
        if not await founder.reserve_slot():
            raise HTTPException(
                status_code=409,
                detail="As 30 vagas de Fundador foram esgotadas.",
            )
        founder_slot_reserved = True

    # 4. Cria Preferência no Mercado Pago
    # `billing_cycle_months` must drive MP's auto_recurring.frequency — otherwise
    # an annual plan (R$1068/year) gets charged as R$1068/month.
    subscription_data = {
        "reason": f"{selected_plan.title} - {bot.restaurant_name}",
        "auto_recurring": {
            "frequency": selected_plan.billing_cycle_months,
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
            if founder_slot_reserved:
                await founder.release_slot()
            raise HTTPException(
                status_code=400,
                detail="Erro ao processar pagamento. Tente novamente.",
            )

        checkout_link = result["response"]["init_point"]
        return {"checkout_url": checkout_link}

    except HTTPException:
        raise
    except Exception:
        logger.exception("Checkout creation failed")
        if founder_slot_reserved:
            await founder.release_slot()
        raise HTTPException(
            status_code=500,
            detail="Erro interno ao processar pagamento. Tente novamente mais tarde.",
        )


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
                    existing_sub = await crud.get_subscription_by_bot(session, bot_id)

                    # Skip if current subscription is admin-granted
                    if existing_sub and existing_sub.mp_subscription_id.startswith(
                        "admin_grant_"
                    ):
                        logger.info(
                            "Ignoring MP webhook for admin-granted subscription, bot_id=%s",
                            bot_id,
                        )
                        return {"status": "ok"}

                    # If mp_id doesn't match existing sub, decide whether to accept
                    if (
                        existing_sub
                        and existing_sub.mp_subscription_id != preapproval_id
                    ):
                        if status == "authorized":
                            # Confirmed upgrade/new subscription — cancel old MP sub
                            try:
                                _get_sdk().preapproval().update(
                                    existing_sub.mp_subscription_id,
                                    {"status": "cancelled"},
                                )
                                logger.info(
                                    "Cancelled old MP sub %s (replaced by %s) for bot_id=%s",
                                    existing_sub.mp_subscription_id,
                                    preapproval_id,
                                    bot_id,
                                )
                            except Exception:
                                logger.warning(
                                    "Failed to cancel old MP sub %s",
                                    existing_sub.mp_subscription_id,
                                )
                        else:
                            # Stale webhook from orphan/old subscription — skip
                            logger.info(
                                "Ignoring stale webhook: mp_id=%s status=%s, "
                                "current mp_id=%s for bot_id=%s",
                                preapproval_id,
                                status,
                                existing_sub.mp_subscription_id,
                                bot_id,
                            )
                            return {"status": "ok"}

                    # Use existing plan_type as fallback for legacy external_references
                    if plan_key == "pro" and len(parts) <= 2 and existing_sub:
                        plan_key = existing_sub.plan_type

                    # Extract subscription frequency from MP response
                    auto_recurring = sub_info.get("auto_recurring", {})
                    frequency = auto_recurring.get("frequency", 1)
                    frequency_type = auto_recurring.get("frequency_type", "months")
                    # Convert to months (MP uses "months" or "days")
                    if frequency_type == "months":
                        plan_frequency_months = frequency
                    elif frequency_type == "days":
                        plan_frequency_months = max(1, frequency // 30)
                    else:
                        plan_frequency_months = 1

                    sub = await crud.upsert_subscription(
                        session=session,
                        user_id=bot.user_id,
                        bot_id=bot.id,
                        mp_id=preapproval_id,
                        status=status,
                        plan_type=plan_key,
                        plan_frequency_months=plan_frequency_months,
                    )

                    # Founder lifetime snapshot: stamp on first transition to
                    # authorized so the R$59,90 lock survives Plan.price edits.
                    if (
                        sub is not None
                        and plan_key == "founder"
                        and status == "authorized"
                        and not sub.is_founder
                    ):
                        plan_row = await crud.get_plan_by_key(session, "founder")
                        if plan_row is not None:
                            sub.is_founder = True
                            sub.snapshotted_price_brl = plan_row.price
                            session.add(sub)

                    await session.commit()
                    await billing_cache.invalidate_by_bot_id(bot.id)
                    logger.info(
                        "Subscription updated for bot_id=%s, status=%s, plan=%s",
                        bot_id,
                        status,
                        plan_key,
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


@router.get("/usage", response_model=schemas.UsageSummary)
async def get_usage(
    bot_id: int,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Real-time usage for the dashboard widget.

    Reads BotMonthlyUsage.completed_orders (populated by the hot-path order
    counter) and applies the active plan's thresholds on the fly. Overage
    values in the response are computed live — the row's stored
    overage_orders/overage_amount_brl is written by the billing cron and
    can lag, but the widget must always reflect the current state.
    """
    from calendar import monthrange

    # 1. Ownership
    bot = await session.get(Bot, bot_id)
    if not bot or bot.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Acesso negado ao bot.")

    # 2. Resolve plan — active Subscription's plan_type, else Free
    sub = await crud.get_subscription_by_bot(session, bot_id)
    plan_key = sub.plan_type if sub and sub.status == "authorized" else "free"
    plan = await crud.get_plan_by_key(session, plan_key)
    if plan is None:
        # Free plan seed should always exist — hard fail if it's missing
        raise HTTPException(
            status_code=500, detail=f"Plano '{plan_key}' não encontrado."
        )

    # 3. Current BRT month window
    brt = pytz.timezone("America/Sao_Paulo")
    now_brt = datetime.now(brt)
    year_month = now_brt.strftime("%Y-%m")
    days_in_month = monthrange(now_brt.year, now_brt.month)[1]
    start_of_month = now_brt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days_elapsed = (now_brt - start_of_month).days + 1  # inclusive, min 1

    # 4. Read the counter row (may be absent if no billable orders yet)
    usage_row = await crud.get_monthly_usage(session, bot_id, year_month)
    completed = usage_row.completed_orders if usage_row else 0

    # 5. Thresholds + live overage math
    cap = plan.monthly_order_cap
    overage_threshold = plan.overage_starts_at or cap  # falls back if no grace
    per_order = plan.overage_per_order_brl or 0.0

    overage_orders = max(0, completed - overage_threshold) if overage_threshold else 0
    overage_amount = overage_orders * per_order

    # 6. Month-end projection (linear extrapolation from today's rate)
    projected = (
        round(completed * days_in_month / days_elapsed)
        if days_elapsed > 0 and cap is not None
        else None
    )
    projected_overage = (
        max(0, projected - overage_threshold) * per_order
        if projected is not None and overage_threshold is not None
        else None
    )

    pct_used = min(1.0, completed / cap) if cap and cap > 0 else 0.0
    orders_remaining = max(0, cap - completed) if cap is not None else None

    return schemas.UsageSummary(
        bot_id=bot_id,
        plan=schemas.UsagePlan(
            key=plan.key,
            tier=plan.tier,
            title=plan.title,
            monthly_order_cap=cap,
            overage_per_order_brl=per_order,
            price=plan.price,
        ),
        current_period=schemas.UsageCurrentPeriod(
            year_month=year_month,
            completed_orders=completed,
            cap=cap,
            overage_starts_at=plan.overage_starts_at,
            orders_remaining=orders_remaining,
            pct_used=pct_used,
            overage_orders=overage_orders,
            overage_amount_brl=overage_amount,
            projected_month_end_orders=projected,
            projected_overage_brl=projected_overage,
        ),
        upgrade_offer=schemas.UsageUpgradeOffer(available_plans=[]),
    )
