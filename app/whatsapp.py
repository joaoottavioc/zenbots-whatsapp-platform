# app/whatsapp.py
import os
import json
import random
from dataclasses import dataclass
from typing import Any, Dict, List
import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from app import crud
from app.database import async_session
from app.models import (
    Product,
    ShoppingCart,
    DeliveryMethod,
    Bot,
    OrderStatus,
    CartState,
    Contact,
)
from app.openai_client import (
    classify_user_intent,
    get_ai_decision,
    extract_potential_items,
)
from app.prompt_central import create_central_prompt
from app.tools_definition import tools_schema
from datetime import datetime, timedelta
import regex as re
from app.semantic_router import semantic_intent, THRESHOLDS
from app.payment_service import create_pix_payment
from sqlalchemy.exc import IntegrityError
import mercadopago
from app.models import Subscription
import app.utils


# --- Imports Atualizados ---
# Helper para gerenciar o estado de "ação pendente"
from app.pending_action import (
    save_pending,
    clear_pending,
    has_valid_pending,
    expire_if_needed,
)

# Função padronizada para obter o tempo atual em UTC
from app.time import utcnow
import pytz
from app.rate_limiter import is_spamming, is_rate_limited
from app.broadcast import broadcast_order_update
from app.utils import mask_phone
from app.webhook_security import require_mp_signature
from app.sanitize import sanitize_llm_output
from app.encryption import decrypt_value
from app.distributed_lock import contact_lock
from redis.exceptions import LockError
from app.context import new_trace_id, current_bot_id, current_contact_id
from app.monitoring import record_api_usage, record_business_event, record_error
import logging
import time as _time

logger = logging.getLogger(__name__)


def _safe_int(value, label: str = "value") -> int | None:
    """Convert to int, return None on failure with warning log."""
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Invalid %s for int conversion: %r", label, value)
        return None


load_dotenv()
router = APIRouter()


@dataclass
class MessageContext:
    """Bundles the variables threaded through process_whatsapp_message handlers."""

    session: AsyncSession
    bot: Bot
    contact: Contact
    cart: ShoppingCart
    contact_number: str
    text_body: str
    token: str
    phone_id: str


# Extrai "QTD + NOME" da pergunta de confirmação (ex.: "1 Gnocchis de la Mémé Forte, 2 X, ...")
_ITEM_FROM_Q_RE = re.compile(
    r"(\d{1,6})\s+([A-Za-zÀ-ÿ'´`^~\- ]+?)(?:\s+por\s*R\$\s*[\d.,]+|\s*(?:,| e |$))",
    re.IGNORECASE,
)


async def _check_rate_limit(contact_number: str, phone_id: str = "") -> bool:
    """Returns True if the sender is rate-limited (caller should stop)."""
    if await is_spamming(
        contact_number, limit=20, window_seconds=60, bot_phone_id=phone_id
    ):
        logger.warning(
            "RATE LIMIT: blocking contact %s for excessive messages",
            mask_phone(contact_number),
        )
        return True
    return False


async def _find_bot(
    session: AsyncSession, incoming_phone_id: str, bot_display_phone: str
) -> Bot | None:
    """Finds the Bot by phone_number_id with fallback to display number."""
    result = await session.execute(
        select(Bot).where(Bot.phone_number_id == incoming_phone_id)
    )
    bot = result.scalars().first()
    if not bot:
        logger.warning(
            "Bot not found by phone_number_id %s, trying display number",
            incoming_phone_id,
        )
        bot = await crud.get_bot_by_number(session, bot_display_phone)
    if not bot:
        logger.error("Bot not found for incoming message, skipping")
    return bot


async def _check_subscription(
    session: AsyncSession, bot: Bot, contact_number: str
) -> bool:
    """Returns True if the bot owner's subscription is expired (caller should stop)."""
    result_sub = await session.execute(
        select(Subscription).where(Subscription.user_id == bot.user_id)
    )
    sub = result_sub.scalars().first()

    grace_period_days = 3
    is_blocked = False

    if not sub:
        is_blocked = True
    else:
        now = utcnow()
        expiration_limit = sub.current_period_end.replace(tzinfo=None) + timedelta(
            days=grace_period_days
        )
        if sub.status != "authorized" and now > expiration_limit:
            is_blocked = True

    if is_blocked:
        logger.warning(
            "Subscription expired: bot_id=%s user_id=%s", bot.id, bot.user_id
        )
        maintenance_msg = (
            "Olá! 👋 Nosso atendimento automático está temporariamente indisponível.\n\n"
            "Um de nossos atendentes irá retornar em breve para ajudá-lo. Agradecemos sua paciência! 🙏"
        )
        await send_whatsapp_message(
            to=contact_number,
            message=maintenance_msg,
            token=decrypt_value(bot.whatsapp_token),
            phone_id=bot.phone_number_id,
        )
    return is_blocked


async def _handle_dedup(
    session: AsyncSession, message_id: str, token: str, phone_id: str
) -> bool:
    """Marks message as read, checks dedup. Returns True if already processed (caller should stop)."""
    await mark_message_as_read(message_id, token, phone_id)

    if await crud.is_message_processed(session, message_id):
        logger.info("Duplicate message %s, skipping", message_id)
        return True
    try:
        await crud.add_processed_message(session, message_id)
    except IntegrityError:
        logger.info("Concurrent duplicate message %s, skipping", message_id)
        await session.rollback()
        return True
    return False


async def _handle_store_closed(
    session: AsyncSession, bot: Bot, contact_number: str, text_body: str
) -> bool:
    """If the store is closed, sends a rich closing message. Returns True if handled."""
    if is_store_open(bot):
        return False

    logger.info(
        "Store closed, sending closing message to %s", mask_phone(contact_number)
    )

    next_opening = get_next_opening_text(bot)
    menu_url = bot.menu_url
    media_type = None
    if menu_url:
        media_type = "document" if menu_url.lower().endswith(".pdf") else "image"

    base_msg = bot.closing_message or "No momento não estamos atendendo. 🌙"
    rich_closing_msg = (
        f"{base_msg}\n\n"
        f"⏰ *Voltamos {next_opening}*\n\n"
        "Aproveite para conferir o cardápio acima e já ir escolhendo! Até logo. 😋"
    )

    await send_whatsapp_message(
        to=contact_number,
        message=rich_closing_msg,
        token=decrypt_value(bot.whatsapp_token),
        phone_id=bot.phone_number_id,
        media_url=menu_url,
        media_type=media_type,
    )
    await crud.add_interaction_to_history(
        session, bot.id, contact_number, text_body, rich_closing_msg
    )
    return True


_SESSION_TIMEOUT = timedelta(minutes=10)
_LONG_TIMEOUT = timedelta(hours=12)


async def _send_welcome_with_menu(session, bot, cart, contact_number, text_body):
    """Sends the welcome greeting with menu image/PDF — reused for first contact and session resets."""
    all_products = await crud.get_products_by_bot_id(session, bot.id)
    sample_products = [p for p in all_products if p.is_available]
    if sample_products and len(sample_products) >= 2:
        picks = random.sample(sample_products, 2)
        example_text = f'_Ex: "Quero um(a) {picks[0].name} e um(a) {picks[1].name}"_'
    elif sample_products:
        example_text = f'_Ex: "Quero um(a) {sample_products[0].name}"_'
    else:
        example_text = '_Ex: "Quero uma pizza de calabresa e uma coca-cola"_'

    response_to_user = (
        f"Olá! Seja bem-vindo ao *{bot.restaurant_name or 'nosso restaurante'}*! 😊\n\n"
        "Confira o cardápio acima e me diga o que vai querer. Pode escrever ou mandar um áudio!\n\n"
        f"{example_text}"
    )
    menu_url = bot.menu_url
    media_type = None
    if menu_url:
        media_type = "document" if menu_url.lower().endswith(".pdf") else "image"

    await send_whatsapp_message(
        to=contact_number,
        message=response_to_user,
        token=decrypt_value(bot.whatsapp_token),
        phone_id=bot.phone_number_id,
        media_url=menu_url,
        media_type=media_type,
    )
    await crud.add_interaction_to_history(
        session, bot.id, contact_number, text_body, "Enviou Cardápio (Imagem)"
    )

    cart.state = CartState.SHOPPING
    cart.last_activity_at = utcnow()
    session.add(cart)
    await session.commit()


async def _handle_session_expiry(mctx: MessageContext) -> bool:
    """Checks inactivity timeouts. Returns True if session expired and message was handled."""
    cart, session, bot = mctx.cart, mctx.session, mctx.bot
    contact_number, text_body = mctx.contact_number, mctx.text_body

    expire_if_needed(cart)

    inactivity_duration = utcnow() - cart.last_activity_at.replace(tzinfo=None)

    if inactivity_duration > _LONG_TIMEOUT or inactivity_duration > _SESSION_TIMEOUT:
        logger.info(
            "Session expired after %s, resetting cart and sending welcome with menu",
            inactivity_duration,
        )
        clear_pending(cart)
        await crud.clear_db_cart(session, cart.id)
        cart.state = CartState.GREETING
        cart.delivery_method = None
        await _send_welcome_with_menu(session, bot, cart, contact_number, text_body)
        return True

    return False


async def _handle_delivery_method(mctx: MessageContext) -> str | None:
    """Handles AWAITING_DELIVERY_METHOD state. Returns response string or None."""
    cart, bot, session = mctx.cart, mctx.bot, mctx.session
    if cart.state != CartState.AWAITING_DELIVERY_METHOD:
        return None

    user_text = mctx.text_body.lower().strip()

    if "entrega" in user_text or user_text == "1":
        cart.delivery_method = DeliveryMethod.DELIVERY
        cart.state = CartState.AWAITING_CEP
        response = "Ótimo! 🛵 Para confirmar o endereço, me informe seu *CEP*."

    elif "retirada" in user_text or "buscar" in user_text or user_text == "2":
        cart.delivery_method = DeliveryMethod.PICKUP
        await session.refresh(cart, attribute_names=["contact"])
        if cart.contact and cart.contact.name:
            cart.state = CartState.AWAITING_PAYMENT_METHOD
            final_summary = _build_cart_summary_message(cart, bot, "🛍️")
            response = (
                f"Perfeito, retirada no balcão para *{cart.contact.name}*! 🛍️\n\n"
                f"{final_summary}\n\n"
                "Qual será a forma de pagamento?\n💠 *PIX* | 💳 *Cartão* | 💵 *Dinheiro*"
            )
        else:
            cart.state = CartState.AWAITING_CUSTOMER_NAME
            response = "Retirada confirmada! 🛍️ Para registrar o pedido, me informe seu *nome completo*."
    else:
        response = "Não entendi. 😅 Por favor, responda *Entrega* ou *Retirada*."

    cart.last_activity_at = utcnow()
    session.add(cart)
    await send_whatsapp_message(
        mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
    )
    await crud.add_interaction_to_history(
        session, bot.id, mctx.contact_number, mctx.text_body, response
    )
    await session.commit()
    return response


async def _handle_cep(mctx: MessageContext) -> str | None:
    """Handles AWAITING_CEP state. Returns response string or None."""
    cart, bot, session = mctx.cart, mctx.bot, mctx.session
    if cart.state != CartState.AWAITING_CEP:
        return None

    msg_lower = mctx.text_body.lower().strip()

    # 1. Emergency exit
    if msg_lower in ["cancelar", "sair", "voltar", "tchau", "reiniciar", "encerrar"]:
        cart.state = CartState.GREETING
        cart.partial_address = None
        cart.items = []
        response = "Tudo bem! Voltamos ao início. Qualquer hora que quiser pedir, é só chamar. 😊"
        cart.last_activity_at = utcnow()
        session.add(cart)
        await session.commit()
        await send_whatsapp_message(
            mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
        )
        return response

    # 2. Switch to pickup
    if "retirada" in msg_lower or "buscar" in msg_lower or "balcao" in msg_lower:
        cart.delivery_method = "pickup"
        cart.partial_address = None
        cart.pending_address = "Retirada no Balcão"
        cart.pix_only = True
        await session.refresh(cart, attribute_names=["contact"])
        if cart.contact and cart.contact.name:
            cart.state = CartState.AWAITING_PAYMENT_METHOD
            response = (
                f"Combinado! Retirada no balcão para *{cart.contact.name}*, sem taxa de entrega. 🛍️\n\n"
                "O pagamento para retirada é exclusivamente via *PIX*. 💠\n"
                "Digite *PIX* para continuar."
            )
        else:
            cart.state = CartState.AWAITING_CUSTOMER_NAME
            response = "Combinado! Retirada no balcão, sem taxa de entrega. 🛍️\nPara registrar o pedido, me informe seu *nome completo*."
        cart.last_activity_at = utcnow()
        session.add(cart)
        await session.commit()
        await send_whatsapp_message(
            mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
        )
        return response

    # 3. CEP / Location logic
    cep_text = re.sub(r"\D", "", mctx.text_body)
    input_cep = cep_text  # Always use sanitized digits-only version

    address_data = await app.utils.get_address_from_cep(input_cep)

    if not address_data:
        response = (
            "Não reconheci esse CEP. 🤔 Por favor, tente novamente ou escolha uma opção:\n"
            "🛍️ Digite *Retirada*\n"
            "❌ Digite *Cancelar*"
        )
    else:
        lat = address_data.get("lat")
        lng = address_data.get("lng")
        is_within_radius = False
        distance = 0.0

        if lat and lng and bot.latitude and bot.longitude:
            distance = app.utils.calculate_distance(
                bot.latitude, bot.longitude, lat, lng
            )
            max_rad = getattr(bot, "max_delivery_radius", 10.0)
            is_within_radius = distance <= max_rad
            logger.info(
                "[RADIUS] distance=%.2fkm max=%skm within=%s",
                distance,
                max_rad,
                is_within_radius,
            )
        elif not lat:
            logger.warning("[GEO] No coordinates available, blocking delivery")

        if not is_within_radius:
            cart.partial_address = None
            max_rad = getattr(bot, "max_delivery_radius", 10)
            msg_erro = (
                f"fica a *{distance:.1f}km*" if distance > 0 else "não localizamos"
            )
            response = (
                f"Que pena! Seu endereço {msg_erro} daqui e está fora da nossa área de entrega (raio máx: {max_rad}km). 😔\n\n"
                "Mas você ainda pode:\n"
                "🛍️ Responder com *Retirada* para retirar no local\n"
                "❌ Responder com *Cancelar* para cancelar o pedido"
            )
        else:
            cart.partial_address = address_data
            cart.delivery_method = "delivery"
            cart.state = CartState.AWAITING_NUMBER_COMPLEMENT
            street = address_data.get("street", "Rua sem nome")
            neigh = address_data.get("neighborhood", "")
            response = (
                f"📍 Endereço encontrado:\n"
                f"*{street} — {neigh}*\n\n"
                f"Agora me informe o *número* e o *complemento* (ex: 142, Apto 3)."
            )

    cart.last_activity_at = utcnow()
    session.add(cart)
    await session.commit()
    await send_whatsapp_message(
        mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
    )
    return response


async def _handle_number_complement(mctx: MessageContext) -> str | None:
    """Handles AWAITING_NUMBER_COMPLEMENT state. Returns response string or None."""
    cart, session = mctx.cart, mctx.session
    if cart.state != CartState.AWAITING_NUMBER_COMPLEMENT:
        return None

    partial = cart.partial_address
    if not isinstance(partial, dict):
        cart.state = CartState.AWAITING_CEP
        return "Ocorreu um erro com o endereço. Por favor, informe seu CEP novamente."

    number_complement = mctx.text_body.strip()[:200]
    cep_display = partial.get("cep") or "Não informado"
    street = partial.get("street") or "Rua não informada"
    neighborhood = partial.get("neighborhood") or "Bairro não informado"
    city = partial.get("city") or ""
    state = partial.get("state") or ""

    full_address = (
        f"{street}, {number_complement}\n"
        f"{neighborhood} - {city}/{state}\n"
        f"CEP: {cep_display}"
    )

    cart.pending_address = full_address
    cart.partial_address = None
    cart.state = CartState.AWAITING_ADDRESS_CONFIRMATION

    response = (
        f"Confirme o endereço de entrega:\n\n"
        f"🏠 *{full_address}*\n\n"
        f"Está correto? Responda *Sim* ou *Não*."
    )

    cart.last_activity_at = utcnow()
    session.add(cart)
    await send_whatsapp_message(
        mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
    )
    await crud.add_interaction_to_history(
        session, mctx.bot.id, mctx.contact_number, mctx.text_body, response
    )
    await session.commit()
    return response


async def _handle_address_confirmation(
    mctx: MessageContext, intent: str | None
) -> str | None:
    """Handles AWAITING_ADDRESS_CONFIRMATION state. Returns response string or None."""
    cart, session = mctx.cart, mctx.session
    if cart.state != CartState.AWAITING_ADDRESS_CONFIRMATION:
        return None

    if intent == "CONFIRM":
        final_address = cart.pending_address
        await crud.save_address_to_cart(session, cart.id, final_address)
        cart.pending_address = None
        cart.state = CartState.AWAITING_CUSTOMER_NAME
        response = "Endereço anotado! ✅ Para identificarmos seu pedido, me informe seu *nome completo*."
    elif intent == "NEGATE":
        cart.pending_address = None
        cart.state = CartState.AWAITING_CEP
        response = (
            "Sem problema! Me informe o *CEP* novamente para corrigirmos o endereço."
        )
    else:
        response = "Por favor, responda *Sim* para confirmar ou *Não* para corrigir o endereço."

    cart.last_activity_at = utcnow()
    session.add(cart)
    await send_whatsapp_message(
        mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
    )
    await crud.add_interaction_to_history(
        session, mctx.bot.id, mctx.contact_number, mctx.text_body, response
    )
    await session.commit()
    return response


async def _handle_customer_name(mctx: MessageContext) -> str | None:
    """Handles AWAITING_CUSTOMER_NAME state. Returns response string or None."""
    cart, session, bot = mctx.cart, mctx.session, mctx.bot
    if cart.state != CartState.AWAITING_CUSTOMER_NAME:
        return None

    customer_name = mctx.text_body.strip()[:100]
    await crud.save_customer_name_to_contact(session, cart.contact_id, customer_name)

    cart.state = CartState.AWAITING_PAYMENT_METHOD
    final_summary = _build_cart_summary_message(cart, bot, "📦")

    if cart.pix_only:
        payment_prompt = "O pagamento para retirada é exclusivamente via *PIX*. 💠\nDigite *PIX* para continuar."
    else:
        payment_prompt = (
            "Qual será a forma de pagamento?\n💠 *PIX* | 💳 *Cartão* | 💵 *Dinheiro*"
        )

    response = (
        f"Anotado, {customer_name.split(' ')[0]}! 😊 Confira o resumo do seu pedido:\n\n"
        f"{final_summary}\n\n"
        f"{payment_prompt}"
    )

    cart.last_activity_at = utcnow()
    session.add(cart)
    await send_whatsapp_message(
        mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
    )
    await crud.add_interaction_to_history(
        session, bot.id, mctx.contact_number, mctx.text_body, response
    )
    await session.commit()
    return response


async def _handle_payment_method(mctx: MessageContext) -> str | None:
    """Handles AWAITING_PAYMENT_METHOD state. Returns response string or None."""
    cart, session, bot, contact = mctx.cart, mctx.session, mctx.bot, mctx.contact
    if cart.state != CartState.AWAITING_PAYMENT_METHOD:
        return None

    user_text = mctx.text_body.lower()
    order_created = False
    pix_code_to_send = None

    detected_method = None
    if "pix" in user_text:
        detected_method = "pix"
    elif any(x in user_text for x in ["cartão", "cartao", "credito", "debito"]):
        detected_method = "card"
    elif any(x in user_text for x in ["dinheiro", "nota", "troco"]):
        detected_method = "money"

    if cart.pix_only and detected_method and detected_method != "pix":
        response = "Para retirada, o pagamento é exclusivamente via *PIX*. 💠\nDigite *PIX* para continuar."
        await send_whatsapp_message(
            mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
        )
        return response

    if not detected_method:
        if cart.pix_only:
            response = "Não entendi. 😅 Digite *PIX* para continuar com o pagamento."
        else:
            response = "Não entendi a forma de pagamento. 😅\nEscolha: *PIX*, *Cartão* ou *Dinheiro*."
        await send_whatsapp_message(
            mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
        )
        return response

    try:
        await session.refresh(cart, attribute_names=["items", "contact"])
        if not cart.contact:
            raise Exception(f"Carrinho {cart.id} sem contacto.")

        bot_id = cart.contact.bot_id
        items_for_order = [
            {
                "product_id": item.product_id,
                "quantity": item.quantity,
                "notes": item.notes,
            }
            for item in cart.items
        ]

        total_amount = sum(item.product.price * item.quantity for item in cart.items)
        if cart.delivery_method == DeliveryMethod.DELIVERY and bot.delivery_fee > 0:
            total_amount += bot.delivery_fee

        order = await crud.create_order(
            session,
            bot_id=bot_id,
            items=items_for_order,
            customer_address=cart.customer_address,
            total_amount=total_amount,
            contact_id=contact.id,
            payment_method=detected_method,
            delivery_method=cart.delivery_method,
            auto_commit=False,
        )

        if not order:
            raise Exception("Falha ao criar ordem no banco.")

        if detected_method == "pix":
            await session.refresh(bot, attribute_names=["payment_config"])
            client_token = (
                decrypt_value(bot.payment_config.access_token)
                if (bot.payment_config and bot.payment_config.is_active)
                else None
            )

            if client_token:
                pix_info = await create_pix_payment(
                    order_id=order.id,
                    total_amount=order.total_amount,
                    bot_name=bot.restaurant_name,
                    contact_phone=contact.phone_number,
                    access_token_cliente=client_token,
                    webhook_token=order.webhook_token,
                )
                if pix_info:
                    response = "Tudo certo! Seu pedido foi registrado. Copie o código abaixo para pagar via PIX:\n\n⏳ *Você tem 15 minutos para realizar o pagamento*, após esse prazo o pedido será cancelado automaticamente."
                    pix_code_to_send = pix_info["pix_copy_paste"]
                    order_created = True
                else:
                    response = "Tivemos um problema ao gerar o QR Code do PIX. 😔 Por favor, escolha *Cartão* ou *Dinheiro* para continuar."
                    await session.rollback()
            else:
                response = f"Tudo certo! Seu pedido foi registrado.\n\n💠 *Chave PIX:* {bot.pix_key}\n\nAvisaremos quando estiver pronto!"
                order_created = True

        elif detected_method == "card":
            order_created = True
            msg_entrega = (
                "O entregador levará a maquininha na hora da entrega."
                if cart.delivery_method == DeliveryMethod.DELIVERY
                else "Pagamento com cartão na retirada."
            )
            response = f"Tudo certo! Seu pedido foi confirmado. {msg_entrega} Avisaremos quando estiver pronto!"

        elif detected_method == "money":
            order_created = True
            response = f"Tudo certo! Seu pedido foi confirmado. Total: *R$ {total_amount:.2f}* em dinheiro. Separe o valor para facilitar. Avisaremos quando estiver pronto!"

        if order_created:
            display_items = [f"{i.quantity}x {i.product.name}" for i in cart.items]
            await crud.clear_db_cart(session, cart.id)
            try:
                await broadcast_order_update(
                    "new_order",
                    {
                        "order_id": order.id,
                        "customer_name": cart.contact.name,
                        "customer_phone": mctx.contact_number,
                        "total": order.total_amount,
                        "status": "pending",
                        "payment_method": detected_method,
                        "items": display_items,
                        "created_at": str(utcnow()),
                    },
                    bot_id=bot.id,
                )
            except Exception as e:
                logger.error("Failed to send broadcast for order: %s", e)

            await send_whatsapp_message(
                mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
            )
            if pix_code_to_send:
                await send_whatsapp_message(
                    mctx.contact_number,
                    pix_code_to_send,
                    token=mctx.token,
                    phone_id=mctx.phone_id,
                )
            await session.commit()
            return response
        else:
            await send_whatsapp_message(
                mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
            )
            await crud.add_interaction_to_history(
                session, bot.id, mctx.contact_number, mctx.text_body, response
            )
            await session.commit()
            return response

    except Exception as e:
        logger.error("Error in AWAITING_PAYMENT handler: %s", e)
        await session.rollback()
        response = (
            "Ocorreu um erro inesperado. 😔 Por favor, tente novamente em instantes."
        )
        await send_whatsapp_message(
            mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
        )
        return response


async def _handle_confirm_negate(
    mctx: MessageContext, intent: str | None
) -> str | None:
    """Handles CONFIRM/NEGATE intents. Returns response string or None."""
    if intent not in ("CONFIRM", "NEGATE"):
        return None

    cart, session, bot = mctx.cart, mctx.session, mctx.bot

    if has_valid_pending(cart):
        if intent == "CONFIRM":
            response = await _execute_pending_action(session, cart, bot=bot)
        else:
            clear_pending(cart)
            response = "Entendido, cancelei a ação. 😊 O que deseja fazer?"

    elif cart.state in [CartState.GREETING, CartState.SHOPPING]:
        if intent == "CONFIRM":
            if getattr(cart, "last_suggestions", None):
                response = "Show! Qual deles você quer? Pode responder '1', 'o segundo' ou '2 do primeiro'."
            else:
                response = "Perfeito! 😊 O que mais posso adicionar?"
        else:
            response = "Sem problema! 😊 Quer ver sugestões do cardápio ou prefere me dizer o que quer?"
    else:
        if intent == "CONFIRM":
            response = "Perfeito! 😊 Posso ajudar com mais alguma coisa?"
        else:
            response = "Entendido! Como posso ajudar? 😊"

    cart.last_activity_at = utcnow()
    session.add(cart)
    await send_whatsapp_message(
        mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
    )
    await crud.add_interaction_to_history(
        session, bot.id, mctx.contact_number, mctx.text_body, response
    )
    await session.commit()
    return response


async def _handle_clear_cart(mctx: MessageContext, intent: str | None) -> str | None:
    """Handles CLEAR_CART intent. Returns response string or None."""
    if intent != "CLEAR_CART":
        return None
    cart, session = mctx.cart, mctx.session
    clear_pending(cart)
    await crud.clear_db_cart(session, cart.id)
    cart.last_suggestions = None
    return "Carrinho esvaziado! 🛒 Pode começar um novo pedido quando quiser."


def _handle_show_cart(mctx: MessageContext, intent: str | None) -> str | None:
    """Handles SHOW_CART intent. Returns response string or None."""
    if intent != "SHOW_CART":
        return None
    return _build_cart_summary_message(mctx.cart, mctx.bot)


async def _handle_finish_order(mctx: MessageContext, intent: str | None) -> str | None:
    """Handles FINISH_ORDER intent. Returns response string or None if not applicable.
    Returns the response string when handled (caller must still send+commit for non-early-return paths)."""
    if intent != "FINISH_ORDER":
        return None

    cart, session, bot = mctx.cart, mctx.session, mctx.bot
    contact_number, text_body = mctx.contact_number, mctx.text_body

    await session.refresh(cart, attribute_names=["items", "contact"])

    if not cart.items:
        response = "Seu carrinho está vazio. 🛒 Me diga o que gostaria de pedir!"
        await send_whatsapp_message(
            contact_number, response, token=mctx.token, phone_id=mctx.phone_id
        )
        await crud.add_interaction_to_history(
            session, bot.id, contact_number, text_body, response
        )
        await session.commit()
        return response

    current_total = sum(item.product.price * item.quantity for item in cart.items)

    if (
        bot.min_order_value
        and bot.min_order_value > 0
        and current_total < bot.min_order_value
    ):
        missing = bot.min_order_value - current_total
        response = (
            f"⚠️ *Pedido mínimo não atingido*\n\n"
            f"O valor mínimo é *R$ {bot.min_order_value:.2f}* e seu carrinho está em *R$ {current_total:.2f}*.\n\n"
            f"Faltam apenas *R$ {missing:.2f}* — que tal adicionar uma bebida ou sobremesa? 🥤🍫"
        )
        await send_whatsapp_message(
            contact_number, response, token=mctx.token, phone_id=mctx.phone_id
        )
        await crud.add_interaction_to_history(
            session, bot.id, contact_number, text_body, response
        )
        await session.commit()
        return response

    elif cart.delivery_method is None:
        cart.state = CartState.AWAITING_DELIVERY_METHOD
        entrega_line = f"1️⃣  *Entrega* 🛵 (R$ {bot.delivery_fee:.2f})"
        retirada_line = "2️⃣  *Retirada no local* 🛍️ (R$ 0.00)"
        response = (
            "Ótimo! Como prefere receber seu pedido?\n\n"
            f"{entrega_line}\n"
            f"{retirada_line}\n\n"
            "Responda com o número ou a palavra."
        )

    elif cart.customer_address and cart.contact and cart.contact.name:
        logger.info("Address and name already saved, skipping to payment")
        cart.state = CartState.AWAITING_PAYMENT_METHOD
        response = (
            f"Já tenho seus dados salvos! 😊\n\n"
            f"🏠 *{cart.customer_address}*\n"
            f"👤 *{cart.contact.name}*\n\n"
            f"Qual será a forma de pagamento?\n💠 *PIX* | 💳 *Cartão* | 💵 *Dinheiro*"
        )
    elif cart.customer_address:
        logger.info("Address saved but name missing, requesting name")
        cart.state = CartState.AWAITING_CUSTOMER_NAME
        response = "Já tenho seu endereço salvo! 📍 Para finalizar, me informe seu *nome completo*."
    elif cart.pending_address:
        logger.info("Resuming flow: awaiting address confirmation")
        cart.state = CartState.AWAITING_ADDRESS_CONFIRMATION
        response = (
            f"Vamos retomar! Confirme o endereço de entrega:\n\n"
            f"🏠 *{cart.pending_address}*\n\n"
            f"Está correto? Responda *Sim* ou *Não*."
        )
    elif cart.partial_address:
        address_data = cart.partial_address
        logger.info("Resuming flow: awaiting number/complement for CEP")
        cart.state = CartState.AWAITING_NUMBER_COMPLEMENT
        response = (
            f"Vamos retomar de onde paramos! 📍\n\n"
            f"*{address_data['street']}, {address_data['neighborhood']}*\n"
            f"*{address_data['city']} — {address_data['state']}*\n\n"
            f"Agora me informe o *número* e o *complemento* (se houver)."
        )
    else:
        cart.state = CartState.AWAITING_CEP
        response = "Ótimo! 🛵 Para prosseguir com a entrega, me informe seu *CEP*."

    return response


async def _handle_shopping_intent(
    mctx: MessageContext, intent: str | None
) -> str | None:
    """Handles ADD, REMOVE, MODIFY, REQUEST_SUGGESTION intents.
    Returns response string, or None if it already did send+commit (propose_and_confirm path)."""
    session, bot, cart = mctx.session, mctx.bot, mctx.cart
    contact_number, text_body = mctx.contact_number, mctx.text_body

    # Common preparation
    history_records = await crud.get_history_for_contact(
        session, bot.id, contact_number
    )
    past_messages = [{"role": h.role, "content": h.content} for h in history_records]
    await session.refresh(cart, attribute_names=["items"])
    cart_items = [
        {
            "product_id": item.product.id,
            "name": item.product.name,
            "quantity": item.quantity,
        }
        for item in cart.items
    ]
    recent_suggestions = None
    if cart.last_suggestions:
        sug_res = await session.execute(
            select(Product).where(Product.id.in_(cart.last_suggestions), Product.is_deleted == False)
        )
        sug_map = {p.id: p for p in sug_res.scalars().all()}
        recent_suggestions = [
            sug_map[id] for id in cart.last_suggestions if id in sug_map
        ]

    response_to_user = "Não entendi bem. 😅 Pode tentar de outra forma?"

    if intent == "REQUEST_SUGGESTION":
        concept = None
        prompt_args = {
            "user_query": text_body,
            "history": past_messages,
            "restaurant_name": bot.restaurant_name,
            "cart_items": cart_items,
            "search_results": [],
            "recent_suggestions": recent_suggestions,
        }
        prompt = create_central_prompt(**prompt_args)
        ai_message = await get_ai_decision(prompt, tools_schema, force_tool=True)

        if (
            ai_message
            and ai_message.tool_calls
            and ai_message.tool_calls[0].function.name
            == "search_catalog_for_suggestions"
        ):
            try:
                tool_args = json.loads(ai_message.tool_calls[0].function.arguments)
                concept = tool_args.get("search_concept")
                if concept:
                    concept = str(concept)[:100]
                    concept = re.sub(r"[^\w\s\-áàâãéèêíìîóòôõúùûçÁÀÂÃÉÈÊÍÌÎÓÒÔÕÚÙÛÇ]", "", concept).strip()
                    if not concept:
                        concept = None  # will trigger "prato principal" fallback below
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Malformed JSON in search_catalog_for_suggestions tool_call, "
                    "falling back to default concept"
                )

        if not concept:
            concept = "prato principal"
            logger.info("Using default suggestion theme: %s", concept)
            title = "Claro! Aqui estão algumas das nossas sugestões da casa:"
        else:
            logger.info("Using AI-extracted suggestion concept: %s", concept)
            title = f"Claro! Encontrei estas opções relacionadas a '{concept}':"

        found_products = await crud.find_relevant_products(session, bot.id, [concept])

        if not found_products:
            response_to_user = "Puxa, não encontrei nenhuma sugestão no momento. Mas nosso cardápio está cheio de delícias! O que você gostaria?"
        else:
            response_to_user = _format_product_suggestions_message(
                found_products, title
            )
            cart.last_suggestions = [p.id for p in found_products]

    else:
        # ADD / REMOVE / MODIFY
        extracted_items = await extract_potential_items(text_body)
        search_terms = extracted_items if extracted_items else [text_body]
        found_products = await crud.find_relevant_products(
            session, bot.id, search_terms
        )

        prompt = create_central_prompt(
            user_query=text_body,
            history=past_messages,
            restaurant_name=bot.restaurant_name,
            cart_items=cart_items,
            search_results=found_products,
            recent_suggestions=recent_suggestions,
        )
        ai_message = await get_ai_decision(prompt, tools_schema, force_tool=True)

        if ai_message and ai_message.tool_calls:
            from app.tool_arg_schemas import TOOL_VALIDATORS
            from pydantic import ValidationError as PydanticValidationError

            _CART_MODIFYING_TOOLS = {
                "add_items_to_cart",
                "remove_items_from_cart",
                "modify_item_quantity",
                "bulk_modify_quantities",
                "update_item_observation",
            }
            _CONVERSATIONAL_TOOLS = {
                "answer_conversationally",
                "answer_with_found_products",
                "search_catalog_for_suggestions",
            }

            for tool_call in ai_message.tool_calls:
                tool_name = tool_call.function.name
                try:
                    tool_args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    response_to_user = "Desculpe, não entendi. Pode repetir?"
                    break

                # --- Validate LLM tool arguments ---
                _validator_cls = TOOL_VALIDATORS.get(tool_name)
                if _validator_cls:
                    try:
                        _validated = _validator_cls(**tool_args)
                        tool_args = _validated.model_dump()
                    except PydanticValidationError:
                        response_to_user = "Desculpe, não entendi. Pode repetir?"
                        break

                if tool_name == "add_items_to_cart":
                    items_arg = tool_args.get("items", [])

                    if not items_arg:
                        extracted_items_for_msg = await extract_potential_items(
                            text_body
                        )
                        items_str = (
                            " e ".join(f"'{item}'" for item in extracted_items_for_msg)
                            if extracted_items_for_msg
                            else "O item que você pediu"
                        )
                        response_to_user = f"Não encontrei *{items_str}* no nosso cardápio. 😕 Quer tentar outro item ou ver nossas sugestões?"
                    else:
                        clear_pending(cart)
                        qty_before = sum(item.quantity for item in cart.items)

                        await crud.add_items_to_db_cart(
                            session, cart.id, items_arg, bot_id=bot.id
                        )
                        await session.refresh(cart, attribute_names=["items"])

                        qty_after = sum(item.quantity for item in cart.items)

                        if qty_after > qty_before:
                            product_ids = [
                                i.get("product_id")
                                for i in items_arg
                                if isinstance(i, dict)
                                and i.get("product_id") is not None
                            ]
                            if not any(
                                pid in (cart.last_suggestions or [])
                                for pid in product_ids
                            ):
                                cart.last_suggestions = None

                            response_to_user = (
                                _build_cart_summary_message(cart, bot, "✅")
                                + "\n\nAdicionado. Se quiser incluir algo mais é só falar!"
                            )
                        else:
                            extracted_items_for_msg = await extract_potential_items(
                                text_body
                            )
                            items_str = (
                                " e ".join(
                                    f"'{item}'" for item in extracted_items_for_msg
                                )
                                if extracted_items_for_msg
                                else "O item que você pediu"
                            )
                            response_to_user = f"Não encontrei *{items_str}* no nosso cardápio. 😕 Quer tentar outro item ou ver nossas sugestões?"
                    break

                elif tool_name in (
                    "remove_items_from_cart",
                    "modify_item_quantity",
                    "bulk_modify_quantities",
                    "update_item_observation",
                ):
                    clear_pending(cart)
                    await session.refresh(cart, attribute_names=["items"])
                    current_ids = {it.product_id for it in cart.items}

                    if tool_name == "remove_items_from_cart":
                        raw_ids = tool_args.get("product_ids", []) or []
                        targets = []
                        for pid in raw_ids:
                            pid = _safe_int(pid, "product_id")
                            if pid is None:
                                continue
                            if pid in current_ids:
                                targets.append(pid)

                        if not targets:
                            response_to_user = "Esse item não está no seu carrinho. Quer que eu adicione alguma coisa?"
                        else:
                            for pid in targets:
                                await crud.modify_item_quantity_in_db_cart(
                                    session, cart.id, pid, 0
                                )
                            await session.refresh(cart, attribute_names=["items"])
                            response_to_user = (
                                _build_cart_summary_message(cart, bot, "❌")
                                + "\n\nAlgo mais?"
                            )

                    elif tool_name == "modify_item_quantity":
                        pid = _safe_int(tool_args.get("product_id"), "product_id")
                        newq = _safe_int(tool_args.get("new_quantity"), "new_quantity")

                        if pid is None or newq is None or pid not in current_ids:
                            response_to_user = "Esse item não está no seu carrinho. Posso adicioná-lo para você?"
                        else:
                            await crud.modify_item_quantity_in_db_cart(
                                session, cart.id, pid, newq
                            )
                            await session.refresh(cart, attribute_names=["items"])
                            response_to_user = (
                                _build_cart_summary_message(cart, bot, "✏️")
                                + "\n\nAlgo mais?"
                            )

                    elif tool_name == "update_item_observation":
                        pid = _safe_int(tool_args.get("product_id"), "product_id")
                        notes = tool_args.get("notes")

                        if pid is not None and notes and pid in current_ids:
                            await crud.update_item_notes(
                                session, cart.id, pid, str(notes)[:200]
                            )
                            await session.refresh(cart, attribute_names=["items"])
                            response_to_user = (
                                _build_cart_summary_message(cart, bot, "✏️")
                                + "\n\nObservação anotada! Mais alguma coisa?"
                            )
                        elif pid is not None and notes:
                            response_to_user = "Esse item não está no seu carrinho. Posso adicionar algo para você?"
                        else:
                            response_to_user = (
                                "Não entendi qual item você quer alterar. Pode repetir?"
                            )

                    else:
                        # bulk_modify_quantities
                        updates_raw = tool_args.get("updates", []) or []
                        updates = []
                        for upd in updates_raw:
                            pid = _safe_int(upd.get("product_id"), "product_id")
                            newq = _safe_int(upd.get("new_quantity"), "new_quantity")
                            if pid is None or newq is None:
                                continue
                            if pid in current_ids:
                                updates.append(
                                    {"product_id": pid, "new_quantity": newq}
                                )

                        if not updates:
                            response_to_user = "Não encontrei esses itens no seu carrinho. Posso sugerir opções para adicionar?"
                        else:
                            for upd in updates:
                                await crud.modify_item_quantity_in_db_cart(
                                    session,
                                    cart.id,
                                    upd["product_id"],
                                    upd["new_quantity"],
                                )
                            await session.refresh(cart, attribute_names=["items"])
                            response_to_user = (
                                _build_cart_summary_message(cart, bot, "✏️")
                                + "\n\nAlgo mais?"
                            )
                    break

                elif tool_name == "propose_and_confirm_action":
                    question = tool_args.get("confirmation_question")
                    proposed = tool_args.get("proposed_action", {}) or {}
                    ptool, pargs = (
                        proposed.get("tool_name"),
                        proposed.get("tool_args") or proposed.get("parameters") or {},
                    )
                    normalized_args = None
                    if ptool == "add_items_to_cart":
                        raw_items = pargs.get("items") or pargs.get("products")
                        resolved = (
                            await _resolve_items_for_proposal(
                                session, bot.id, raw_items
                            )
                            if raw_items
                            else []
                        )
                        if not resolved and question:
                            resolved = await _items_from_confirmation_question(
                                session, bot.id, question
                            )
                        if resolved:
                            normalized_args = {"items": resolved}
                    elif ptool == "modify_item_quantity":
                        pid = _safe_int(pargs.get("product_id"), "product_id")
                        newq = _safe_int(pargs.get("new_quantity"), "new_quantity")
                        if pid is not None and newq is not None:
                            chk = await session.execute(
                                select(Product.id).where(
                                    Product.bot_id == bot.id, Product.id == pid, Product.is_deleted == False
                                )
                            )
                            if chk.scalars().first():
                                normalized_args = {
                                    "product_id": pid,
                                    "new_quantity": newq,
                                }

                    if question and ptool and normalized_args:
                        sanitized_question = sanitize_llm_output(question)
                        save_pending(cart, ptool, normalized_args, sanitized_question)
                        response_to_user = sanitized_question
                    else:
                        response_to_user = "Não encontrei itens válidos para essa ação. 😔 Pode me dizer novamente o que deseja pedir?"

                    # propose_and_confirm does its own send+commit — signal caller with None
                    await send_whatsapp_message(
                        contact_number,
                        response_to_user,
                        token=mctx.token,
                        phone_id=mctx.phone_id,
                    )
                    await crud.add_interaction_to_history(
                        session, bot.id, contact_number, text_body, response_to_user
                    )
                    cart.last_activity_at = utcnow()
                    session.add(cart)
                    await session.commit()
                    return None

                elif tool_name in _CONVERSATIONAL_TOOLS:
                    if tool_name == "answer_conversationally":
                        response_to_user = sanitize_llm_output(
                            tool_args.get(
                                "response_text",
                                "Não entendi o que você quis dizer. Pode tentar de outra forma?",
                            )
                        )
                    continue

                else:
                    final_intent = intent
                    if final_intent in ("ADD", "ADD_ITEMS") and extracted_items:
                        items_str = " e ".join(f"'{item}'" for item in extracted_items)
                        response_to_user = f"Não encontrei *{items_str}* no nosso cardápio. 😕 Quer tentar outro item ou ver nossas sugestões?"
                    else:
                        response_to_user = "Não consegui entender sua solicitação. 😅 Pode reformular de outra forma?"
                    break

        else:
            # AI returned no tool calls
            final_intent = intent
            if final_intent in ("ADD", "ADD_ITEMS") and extracted_items:
                items_str = " e ".join(f"'{item}'" for item in extracted_items)
                response_to_user = f"Não localizei {items_str} no cardápio. 😔 Temos muitas outras opções — quer ver algumas sugestões?"
            else:
                response_to_user = (
                    "Desculpe, não consegui processar. 😅 Pode tentar de outra forma?"
                )

    return response_to_user


async def _process_contact_message(
    session,
    bot,
    contact,
    contact_number,
    text_body,
    current_token,
    current_phone_id,
):
    """Cart operations executed while holding the per-contact Redis lock."""
    cart = await crud.get_or_create_cart(session, contact.id)

    # Gate: Human takeover
    if cart.human_takeover_active:
        logger.info(
            "Human takeover active for %s, bot skipping message",
            mask_phone(contact_number),
        )
        return

    # Gate: Session expiry
    mctx = MessageContext(
        session=session,
        bot=bot,
        contact=contact,
        cart=cart,
        contact_number=contact_number,
        text_body=text_body,
        token=current_token,
        phone_id=current_phone_id,
    )
    if await _handle_session_expiry(mctx):
        return

    # ▼▼▼ INÍCIO DA NOVA LÓGICA DE RESET DE ESTADO ▼▼▼
    # Se o cliente realizar uma ação de compra enquanto estivermos finalizando,
    # o bot entende que ele voltou a "fazer o pedido"
    if cart.state in [
        CartState.AWAITING_CEP,
        CartState.AWAITING_NUMBER_COMPLEMENT,
        CartState.AWAITING_CUSTOMER_NAME,
    ] and not is_likely_shopping_intent(text_body):
        logger.info(
            "State %s detected, message not a shopping intent, skipping classification",
            cart.state,
        )
        intent = None  # Definimos a intenção como None para pular a lógica de reset
    else:
        # Caso contrário, executamos a classificação de intenção normalmente
        await session.refresh(cart, attribute_names=["items"])
        cart_items_for_intent = [
            {"id": item.product_id, "name": item.product.name} for item in cart.items
        ]
        intent = await resolve_intent(text_body, cart, cart_items_for_intent)
        logger.info("[INTENT] resolved intent: %s", intent)

    # INTERCEPTADOR DE BOAS-VINDAS COM IMAGEM
    if intent == "GREETING_OR_QUESTION" and cart.state == CartState.GREETING:
        await _send_welcome_with_menu(session, bot, cart, contact_number, text_body)
        logger.info("[GREETING] Cart %s state updated to SHOPPING", cart.id)
        return

    # A lógica de reset de estado continua a mesma, mas agora só será
    # acionada por intenções de compra genuínas.
    intents_that_resume_shopping = [
        "ADD",
        "REMOVE",
        "MODIFY",
        "REQUEST_SUGGESTION",
        "SHOW_CART",
        "CLEAR_CART",
        "ADD_ITEMS",
        "REMOVE_ITEMS",
    ]
    finalizing_states = [
        CartState.AWAITING_CEP,
        CartState.AWAITING_NUMBER_COMPLEMENT,
        CartState.AWAITING_ADDRESS_CONFIRMATION,
        CartState.AWAITING_CUSTOMER_NAME,
    ]

    if intent in intents_that_resume_shopping and cart.state in finalizing_states:
        logger.info(
            "Customer resumed shopping (intent=%s), resetting state from %s to GREETING",
            intent,
            cart.state,
        )
        cart.state = CartState.GREETING
        await session.flush()

    # Checkout FSM: dispatch to state handlers
    checkout_result = await _handle_delivery_method(mctx)
    if checkout_result is not None:
        return
    checkout_result = await _handle_cep(mctx)
    if checkout_result is not None:
        return
    checkout_result = await _handle_number_complement(mctx)
    if checkout_result is not None:
        return
    checkout_result = await _handle_address_confirmation(mctx, intent)
    if checkout_result is not None:
        return
    checkout_result = await _handle_customer_name(mctx)
    if checkout_result is not None:
        return
    checkout_result = await _handle_payment_method(mctx)
    if checkout_result is not None:
        return

    # Intent handlers: confirm/negate, clear, show, finish
    result = await _handle_confirm_negate(mctx, intent)
    if result is not None:
        return

    response_to_user = "Não entendi bem. 😅 Pode tentar de outra forma?"

    clear_result = await _handle_clear_cart(mctx, intent)
    if clear_result is not None:
        response_to_user = clear_result
    elif _handle_show_cart(mctx, intent) is not None:
        response_to_user = _handle_show_cart(mctx, intent)
    elif intent == "FINISH_ORDER":
        finish_result = await _handle_finish_order(mctx, intent)
        if finish_result is not None:
            response_to_user = finish_result
            # _handle_finish_order does its own early returns for empty cart / min order;
            # for the rest, we fall through to the final send+commit block below.
    else:
        # Shopping logic: ADD, REMOVE, MODIFY, REQUEST_SUGGESTION
        shopping_result = await _handle_shopping_intent(mctx, intent)
        if shopping_result is None:
            # propose_and_confirm path already did send+commit
            return
        response_to_user = shopping_result

    cart.last_activity_at = utcnow()
    session.add(cart)

    await send_whatsapp_message(
        contact_number,
        response_to_user,
        token=decrypt_value(bot.whatsapp_token),
        phone_id=bot.phone_number_id,
    )
    await crud.add_interaction_to_history(
        session, bot.id, contact_number, text_body, response_to_user
    )
    await session.commit()
    logger.info("Message processed and response sent")


def _classify_error_message(exc: Exception) -> str:
    """Return a user-facing error message based on the exception type."""
    exc_str = str(exc).lower()
    if isinstance(
        exc, (httpx.TimeoutException, httpx.ConnectError, ConnectionError, TimeoutError)
    ):
        return "Estamos com dificuldade de conexão no momento. Por favor, tente novamente em alguns instantes."
    if "database" in exc_str or "sqlalchemy" in type(exc).__name__.lower():
        return "Nosso sistema está temporariamente indisponível. Por favor, tente novamente em alguns instantes."
    if "openai" in type(exc).__name__.lower() or "rate_limit" in exc_str:
        return "Nosso assistente está com dificuldade no momento. Tente enviar sua mensagem novamente."
    if "mercadopago" in exc_str or "payment" in exc_str or "pix" in exc_str:
        return "Tivemos um problema com o sistema de pagamento. Por favor, tente novamente ou escolha outra forma de pagamento."
    return "Desculpe, ocorreu um erro. Por favor, tente novamente em alguns instantes."


async def process_whatsapp_message(ctx, data: Dict[str, Any]):
    trace_id = new_trace_id()
    contact_number: str | None = None
    current_token: str | None = None
    current_phone_id: str | None = None
    async with async_session() as session:
        try:
            # 1. Extração segura dos dados
            entry = data.get("entry", [])[0]
            changes = entry.get("changes", [])[0]
            value = changes.get("value", {})

            if "messages" not in value:
                return

            message_data = value["messages"][0]
            contact_number = message_data["from"]
            text_body = message_data.get("text", {}).get("body", "")
            message_id = message_data["id"]

            incoming_phone_id = value["metadata"]["phone_number_id"]
            bot_display_phone = value["metadata"]["display_phone_number"]

            # Gate: Rate limiting (scoped by bot phone_id)
            if await _check_rate_limit(contact_number, phone_id=incoming_phone_id):
                return
            logger.info(
                "Message received from %s to phone_id %s",
                mask_phone(contact_number),
                incoming_phone_id,
            )
            await record_business_event("message_received")

            # Gate: Find bot
            bot = await _find_bot(session, incoming_phone_id, bot_display_phone)
            if not bot:
                return
            current_bot_id.set(bot.id)

            # Gate: Subscription check
            if await _check_subscription(session, bot, contact_number):
                return

            current_token = decrypt_value(bot.whatsapp_token)
            current_phone_id = bot.phone_number_id

            # Gate: Deduplication
            if await _handle_dedup(
                session, message_id, current_token, current_phone_id
            ):
                return

            # Gate: Store closed
            if await _handle_store_closed(session, bot, contact_number, text_body):
                return

            contact = await crud.get_or_create_contact(session, bot.id, contact_number)
            current_contact_id.set(contact.id)

            try:
                async with contact_lock(contact.id):
                    await _process_contact_message(
                        session,
                        bot,
                        contact,
                        contact_number,
                        text_body,
                        current_token,
                        current_phone_id,
                    )
            except LockError:
                logger.warning("Lock timeout for contact_id=%s", contact.id)
                await send_whatsapp_message(
                    to=contact_number,
                    message="Estou processando sua mensagem anterior. Aguarde um instante e tente novamente.",
                    token=current_token,
                    phone_id=current_phone_id,
                )

        except Exception as e:
            logger.error("Critical error processing message: %s", e, exc_info=True)
            await record_business_event("message_failed")
            await record_error("whatsapp", type(e).__name__)

            # Tenta reverter o banco para não deixar travado
            if session.is_active:
                try:
                    await session.rollback()
                except Exception:
                    pass

            # Envia mensagem de erro classificada ao usuário
            if contact_number and current_token and current_phone_id:
                try:
                    await send_whatsapp_message(
                        to=contact_number,
                        message=_classify_error_message(e),
                        token=current_token,
                        phone_id=current_phone_id,
                    )
                except Exception as send_err:
                    logger.error("Failed to send error message to user: %s", send_err)


def _build_cart_summary_message(cart: ShoppingCart, bot: Bot, emoji: str = "🛒") -> str:
    if not cart.items:
        return "🗑️ *Seu carrinho agora está vazio.*"

    cart_summary_lines = []
    subtotal = 0.0
    for item in cart.items:
        line_total = item.product.price * item.quantity
        subtotal += line_total

        # Formata a linha do item
        item_line = f"- {item.quantity}x {item.product.name} (R$ {line_total:.2f})"

        # Se tiver observação, adiciona na linha de baixo
        if item.notes:
            item_line += f"\n   ↳ _Obs: {item.notes}_"

        # Adiciona à lista APENAS UMA VEZ
        cart_summary_lines.append(item_line)

    summary_text = f"{emoji} *Seu Pedido Atual:*\n" + "\n".join(cart_summary_lines)

    total_amount = subtotal

    # Verifica se o método é entrega E se a taxa é maior que zero
    if cart.delivery_method == DeliveryMethod.DELIVERY and bot.delivery_fee > 0:
        total_amount += bot.delivery_fee
        summary_text += f"\n\nTaxa de Entrega: R$ {bot.delivery_fee:.2f}"

    summary_text += f"\n\nTotal: *R$ {total_amount:.2f}*"
    return summary_text


@router.get("/webhook")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    if params.get("hub.mode") == "subscribe" and params.get(
        "hub.verify_token"
    ) == os.getenv("META_VERIFY_TOKEN"):
        return PlainTextResponse(content=params.get("hub.challenge"))
    return PlainTextResponse(content="Invalid verification", status_code=403)


async def send_whatsapp_message(
    to: str,
    message: str,
    token: str,
    phone_id: str,
    media_url: str = None,
    media_type: str = "image",  # Pode ser "image" ou "document" (para PDF)
):
    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Lógica para decidir se manda Texto Puro ou Mídia
    if media_url:
        data = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": media_type,
            media_type: {
                "link": media_url,
                "caption": message,  # O texto vai junto com a imagem
            },
        }
        # Se for documento, podemos adicionar um nome de arquivo bonito
        if media_type == "document":
            data["document"]["filename"] = "Cardapio_Restaurante.pdf"
    else:
        data = {"messaging_product": "whatsapp", "to": to, "text": {"body": message}}

    _start = _time.perf_counter_ns()
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(url, headers=headers, json=data)
            response.raise_for_status()
            _elapsed = (_time.perf_counter_ns() - _start) // 1_000_000
            await record_api_usage(
                None, "whatsapp", "send_message", cost_usd=0.05, duration_ms=_elapsed
            )
        except httpx.HTTPStatusError as e:
            _elapsed = (_time.perf_counter_ns() - _start) // 1_000_000
            await record_api_usage(
                None,
                "whatsapp",
                "send_message",
                cost_usd=0.0,
                duration_ms=_elapsed,
                success=False,
            )
            logger.error(
                "Failed to send WhatsApp message: status=%s body=%s",
                e.response.status_code,
                e.response.text,
            )


async def mark_message_as_read(message_id: str, token: str, phone_id: str):
    url = f"https://graph.facebook.com/v20.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(url, headers=headers, json=data)
            response.raise_for_status()
            logger.info("Message %s marked as read", message_id)
        except httpx.HTTPStatusError as e:
            logger.error(
                "Failed to mark message %s as read: status=%s",
                message_id,
                e.response.status_code,
            )


async def _execute_pending_action(
    session: AsyncSession, cart: ShoppingCart, bot: Bot
) -> str:
    tool = cart.pending_action_tool
    args = cart.pending_action_args or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            logger.warning(
                "Corrupted pending_action_args for cart %s: %r",
                cart.id, cart.pending_action_args,
            )
            clear_pending(cart)
            return "Houve um erro ao processar sua confirmação. Pode repetir o pedido?"

    logger.info("Executing pending action: tool=%s", tool)

    # Obtém o bot_id a partir do objeto bot para usar na lógica existente
    bot_id = bot.id

    if not tool:
        clear_pending(cart)
        return "Não encontrei nenhuma ação para confirmar. Quer continuar seu pedido?"

    if tool == "add_items_to_cart":
        items_to_add = args.get("items", [])
        if not items_to_add:
            rebuilt = await _items_from_confirmation_question(
                session, bot_id, cart.pending_action_question
            )
            if rebuilt:
                items_to_add = rebuilt
        if not items_to_add:
            clear_pending(cart)
            return "A proposta para adicionar itens estava incompleta. Pode repetir o que deseja?"

        item_ids = [
            item.get("product_id")
            for item in items_to_add
            if isinstance(item, dict) and item.get("product_id") is not None
        ]
        if not item_ids:
            clear_pending(cart)
            return "Não consegui identificar os produtos na proposta. Poderia me dizer novamente?"

        res = await session.execute(
            select(Product.id).where(Product.bot_id == bot_id, Product.id.in_(item_ids), Product.is_deleted == False)
        )
        valid_product_ids = set(res.scalars().all())
        valid_items = [
            item
            for item in items_to_add
            if isinstance(item, dict) and item.get("product_id") in valid_product_ids
        ]

        if not valid_items:
            clear_pending(cart)
            return "Os itens propostos não foram encontrados em nosso cardápio. Quer ver outras opções?"

        await crud.add_items_to_db_cart(session, cart.id, valid_items, bot_id=bot_id)
        await session.refresh(cart, attribute_names=["items"])
        clear_pending(cart)
        # --- CORREÇÃO APLICADA ---
        return _build_cart_summary_message(cart, bot, "✅") + "\n\nAlgo mais?"

    if tool == "modify_item_quantity":
        pid = _safe_int(args.get("product_id"), "product_id")
        newq = _safe_int(args.get("new_quantity"), "new_quantity")
        if pid is None or newq is None:
            clear_pending(cart)
            return "A proposta para modificar o item estava incompleta. Pode repetir?"

        await crud.modify_item_quantity_in_db_cart(
            session, cart.id, pid, newq
        )
        await session.refresh(cart, attribute_names=["items"])
        clear_pending(cart)
        # --- CORREÇÃO APLICADA ---
        return _build_cart_summary_message(cart, bot, "✏️") + "\n\nAlgo mais?"

    if tool == "remove_items_from_cart":
        ids = args.get("product_ids", [])
        for pid in ids:
            pid = _safe_int(pid, "product_id")
            if pid is None:
                continue
            await crud.modify_item_quantity_in_db_cart(session, cart.id, pid, 0)
        await session.refresh(cart, attribute_names=["items"])
        clear_pending(cart)
        # --- CORREÇÃO APLICADA ---
        return _build_cart_summary_message(cart, bot, "❌") + "\n\nAlgo mais?"

    if tool == "answer_with_found_products":
        names = args.get("product_names", [])
        text = (
            "Essas são algumas sugestões para você:\n"
            + "\n".join(f"{i + 1}. {n}" for i, n in enumerate(names))
            if names
            else "Posso sugerir algumas opções, se quiser."
        )
        if names:
            res = await session.execute(
                select(Product).where(Product.bot_id == bot_id, Product.name.in_(names), Product.is_deleted == False)
            )
            prods = res.scalars().all()
            if prods:
                cart.last_suggestions = [p.id for p in prods]
        clear_pending(cart)
        return text

    clear_pending(cart)
    return "A proposta não pôde ser executada. Pode me dizer de novo o que deseja?"


async def _product_ids_for_bot(session: AsyncSession, bot_id: int) -> set[int]:
    res = await session.execute(select(Product.id).where(Product.bot_id == bot_id, Product.is_deleted == False))
    return set(res.scalars().all())


async def _resolve_items_for_proposal(
    session: AsyncSession, bot_id: int, items_arg: list[dict] | None
) -> list[dict]:
    if not items_arg:
        return []
    valid_ids, resolved = await _product_ids_for_bot(session, bot_id), []
    for it in items_arg:
        if not isinstance(it, dict):
            return []
        q = int(it.get("quantity", 0) or 0)
        if q <= 0:
            return []
        pid = it.get("product_id")
        if pid is not None:
            pid = _safe_int(pid, "product_id")
            if pid is None:
                return []
            if pid not in valid_ids:
                return []
            resolved.append({"product_id": pid, "quantity": q})
            continue
        pname = (it.get("product_name") or it.get("name") or "").strip()
        if not pname:
            return []
        found = await session.execute(
            select(Product)
            .where(Product.bot_id == bot_id, Product.is_deleted == False, Product.name.ilike(f"%{pname}%"))
            .limit(1)
        )
        p = found.scalars().first()
        if not p:
            return []
        resolved.append({"product_id": p.id, "quantity": q})
    return resolved


async def _items_from_confirmation_question(
    session: AsyncSession, bot_id: int, question: str
) -> list[dict]:
    if not question:
        return []
    cands = []
    for qty_txt, name in _ITEM_FROM_Q_RE.findall(question):
        try:
            qty = int(qty_txt)
        except Exception:
            continue
        name = name.strip()
        if qty > 0 and name:
            cands.append({"product_name": name, "quantity": qty})
    return await _resolve_items_for_proposal(session, bot_id, cands) if cands else []


async def resolve_intent(text_body, cart, cart_items_for_intent, found_products=None):
    # 1. Try the fast semantic router first
    router_intent, router_score = None, 0.0
    try:
        r_intent, r_score, matched = await semantic_intent(text_body)
        router_intent, router_score = r_intent, r_score
        logger.info("[ROUTER] intent=%s score=%.2f", router_intent, router_score)
    except Exception as e:
        logger.error("[ROUTER] semantic intent failed: %s", e)

    # 2. Compute dynamic threshold
    thresh = THRESHOLDS.get(router_intent, 0.80) if router_intent else 1.0
    if not getattr(cart, "items", []):
        if router_intent in ("MODIFY", "REMOVE"):
            thresh += 0.05
    if found_products and router_intent == "ADD":
        thresh -= 0.02

    # 3. If router is confident, return immediately (skip LLM)
    if router_intent and router_score >= thresh:
        logger.info(
            "[INTENT] router confident: %s score=%.2f threshold=%.2f (LLM skipped)",
            router_intent,
            router_score,
            thresh,
        )
        return router_intent

    # 4. Router not confident — fall back to LLM
    intent_llm = await classify_user_intent(text_body, cart_items_for_intent)
    logger.info(
        "[INTENT] llm=%s router=%s score=%.2f threshold=%.2f final=%s",
        intent_llm,
        router_intent,
        router_score,
        thresh,
        intent_llm,
    )
    return intent_llm


def _format_product_suggestions_message(products: List[Product], title: str) -> str:
    if not products:
        return "Puxa, não encontrei nenhuma sugestão específica no momento. Mas nosso cardápio está cheio de delícias! O que você gostaria?"
    message_parts = [f"*{title}* ✨\n"]
    for i, p in enumerate(products):
        price_formatted = f"R$ {p.price:.2f}".replace(".", ",")
        item_str = f"{i + 1}️⃣ *{p.name.upper()}* - `{price_formatted}`"
        if p.description:
            item_str += f"\n_{p.description}_"
        message_parts.append(item_str)
    footer = "\nÉ só me dizer o número ou o nome do que você mais gostou! 😉"
    return "\n\n".join(message_parts) + footer


def is_likely_shopping_intent(text: str) -> bool:
    """Verifica se o texto contém palavras-chave que indicam uma intenção de compra."""
    shopping_keywords = [
        "quero",
        "gostaria",
        "adiciona",
        "mais",
        "tira",
        "remove",
        "muda",
        "troca",
        "quanto custa",
        "cardapio",
        "menu",
        "ver",
        "pedido",
        "carrinho",
        "esvaziar",
        "limpar",
        "cancelar",
        "tirar",
        "remover",
        "modificar",
        "tire",
        "remova",
    ]
    text_lower = text.lower()
    # Usamos \b para garantir que estamos pegando a palavra inteira (evita "quero" em "qualquer")
    return any(
        re.search(r"\b" + keyword + r"\b", text_lower) for keyword in shopping_keywords
    )


@router.post("/payments/webhooks/payment-confirm/{order_id}")
async def handle_payment_notification(order_id: int, request: Request):
    """
    Webhook dinâmico:
    1. Recebe o ID do pedido na URL.
    2. Busca o pedido no banco para descobrir quem é o BOT dono.
    3. Usa o Token desse BOT para consultar o Mercado Pago.
    """
    # --- Rate limiting per order (P1) ---
    if await is_rate_limited(f"rl:webhook:{order_id}", limit=5, window_seconds=60):
        return JSONResponse(content={"status": "rate_limited"}, status_code=429)

    # --- Signature verification (P0-1) ---
    data = await request.json()
    payment_id_str = data.get("data", {}).get("id")
    await require_mp_signature(request, str(payment_id_str or ""))

    async with async_session() as session:
        try:
            # 1. Busca o Pedido e o Bot Dono
            from app.models import Order

            result = await session.execute(select(Order).where(Order.id == order_id))
            order = result.scalars().first()

            if not order:
                return JSONResponse(
                    content={"status": "order_not_found"}, status_code=404
                )

            # --- Webhook token validation (P3) ---
            import secrets as _secrets

            url_token = request.query_params.get("token", "")
            if not url_token or not _secrets.compare_digest(
                url_token, order.webhook_token
            ):
                return JSONResponse(
                    content={"status": "invalid_token"}, status_code=403
                )

            # Carrega o Bot e a Configuração
            await session.refresh(order, attribute_names=["bot"])
            await session.refresh(order.bot, attribute_names=["payment_config"])

            if (
                not order.bot.payment_config
                or not order.bot.payment_config.access_token
            ):
                logger.error(
                    "Bot for order_id=%s has no payment token configured", order_id
                )
                return JSONResponse(content={"status": "no_token"}, status_code=200)

            # 2. Inicializa o SDK com o token DO CLIENTE (Dono do Bot), descriptografado
            specific_sdk = mercadopago.SDK(
                decrypt_value(order.bot.payment_config.access_token)
            )

            # 3. Processa a notificação (Igual antes, mas usando specific_sdk)
            notification_type = data.get("type") or data.get(
                "topic"
            )  # MP as vezes manda 'topic'
            payment_id_str = data.get("data", {}).get("id")

            if notification_type != "payment" or not payment_id_str:
                return JSONResponse(content={"status": "ignored"}, status_code=200)

            # Consulta o MP
            payment_info = specific_sdk.payment().get(payment_id_str)

            if payment_info["status"] != 200:
                logger.error(
                    "Mercado Pago API error for order_id=%s: status=%s",
                    order_id,
                    payment_info["status"],
                )
                return JSONResponse(content={"status": "mp_error"}, status_code=200)

            payment_data = payment_info["response"]
            payment_status = payment_data.get("status")

            # --- IDOR cross-check (P0-2): verify external_reference matches this order ---
            external_ref = payment_data.get("external_reference", "")
            expected_ref = str(order_id)
            if external_ref != expected_ref:
                logger.warning(
                    "IDOR blocked: external_reference=%s does not match order_id=%s",
                    external_ref,
                    expected_ref,
                )
                return JSONResponse(
                    content={"status": "reference_mismatch"}, status_code=403
                )

            # Mapeia Status
            db_status = None
            if payment_status == "approved":
                db_status = OrderStatus.PAID
            elif payment_status in ("rejected", "cancelled"):
                db_status = OrderStatus.FAILED

            if db_status:
                await crud.update_order_status_by_id(
                    session, order_id, db_status, str(payment_id_str)
                )

                # --- INÍCIO DA ALTERAÇÃO (Broadcast para o Painel) ---
                if db_status == OrderStatus.PAID:
                    try:
                        # Precisamos garantir que temos os dados do contato para mostrar o nome no painel
                        await session.refresh(order, attribute_names=["contact"])
                        customer_name = (
                            order.contact.name if order.contact else "Cliente"
                        )

                        logger.info(
                            "Broadcasting payment confirmed for order_id=%s", order.id
                        )

                        # Envia o sinal para o Front (page.tsx) atualizar de Amarelo para Verde
                        await broadcast_order_update(
                            "payment_confirmed",
                            {
                                "id": order.id,
                                "status": "PAID",
                                "customer_name": customer_name,
                                "bot_id": order.bot_id,
                            },
                            bot_id=order.bot_id,
                        )
                    except Exception as e:
                        logger.error(
                            "Failed to broadcast payment update for order_id=%s: %s",
                            order.id,
                            e,
                        )
                # --- FIM DA ALTERAÇÃO ---

                # Notifica no WhatsApp se aprovado (Seu código original continua aqui)
                if db_status == OrderStatus.PAID:
                    msg = "Pagamento confirmado! Seu pedido foi recebido e já está sendo preparado. Obrigado pela preferência!"

                    phone_raw = order.contact.phone_number if order.contact else None
                    phone_dest = (
                        f"55{phone_raw}"
                        if phone_raw and not phone_raw.startswith("55")
                        else phone_raw
                    )

                    if phone_dest:
                        await send_whatsapp_message(
                            to=phone_dest,
                            message=msg,
                            token=decrypt_value(order.bot.whatsapp_token),
                            phone_id=order.bot.phone_number_id,
                        )
                    else:
                        logger.warning(
                            "Order %s has no contact to notify about payment", order.id
                        )

            return JSONResponse(content={"status": "ok"}, status_code=200)

        except Exception as e:
            logger.error("Payment webhook error for order_id=%s: %s", order_id, e)
            return JSONResponse(content={"status": "error"}, status_code=500)


def is_store_open(bot: Bot) -> bool:
    """
    Verifica se a loja está aberta baseada na configuração manual E no horário.
    Prioridade:
    1. Se o botão manual (is_open) for False -> FECHADO (Férias/Emergência).
    2. Se manual for True -> Verifica o horário agendado (schedule).
    """
    # 1. Bloqueio Manual (O "Disjuntor")
    if not bot.is_open:
        return False

    # Se não tiver horário configurado, assumimos que segue apenas o botão manual (Aberto)
    if not bot.schedule:
        return True

    try:
        # 2. Obtém a hora atual no fuso do restaurante
        tz = pytz.timezone(bot.timezone)
        now = datetime.now(tz)

        # Mapeia dia da semana (0=Segunda, 6=Domingo) para nossas chaves
        weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        today_key = weekdays[now.weekday()]

        day_config = bot.schedule.get(today_key)

        # Se não tem config para hoje ou o dia está inativo
        if not day_config or not day_config.get("active", False):
            return False  # Fechado neste dia

        start_time = day_config.get("start", "00:00")
        end_time = day_config.get("end", "23:59")

        # Converte strings "HH:MM" para objetos comparáveis
        current_time_str = now.strftime("%H:%M")

        # Lógica simples de comparação de strings (funciona bem para formato 24h)
        # Se passar da meia-noite (ex: 18:00 as 02:00), a lógica precisaria ser mais complexa.
        # Para o MVP, assumimos que abre e fecha no mesmo dia operacional.
        if start_time <= current_time_str <= end_time:
            return True
        else:
            return False

    except Exception as e:
        logger.error("Error calculating store hours: %s, assuming open", e)
        return True


def get_next_opening_text(bot: Bot) -> str:
    """
    Calcula o próximo horário de abertura baseado no schedule do bot.
    Retorna algo como: "Amanhã às 18:00" ou "Segunda às 10:00".
    """
    if not bot.schedule:
        return "em breve"

    try:
        tz = pytz.timezone(bot.timezone)
        now = datetime.now(tz)
        weekdays_map = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        weekdays_pt = [
            "Segunda",
            "Terça",
            "Quarta",
            "Quinta",
            "Sexta",
            "Sábado",
            "Domingo",
        ]

        current_day_idx = now.weekday()

        # Procura nos próximos 7 dias
        for i in range(1, 8):
            next_day_idx = (current_day_idx + i) % 7
            day_key = weekdays_map[next_day_idx]

            day_config = bot.schedule.get(day_key)

            if day_config and day_config.get("active"):
                start_time = day_config.get("start", "00:00")

                # Se for amanhã
                if i == 1:
                    return f"Amanhã às {start_time}"
                # Se for hoje (caso raro de janelas multiplas, mas simplificamos aqui)
                elif i == 0:
                    return f"Hoje às {start_time}"
                else:
                    return f"{weekdays_pt[next_day_idx]} às {start_time}"

        return "em breve"
    except Exception:
        return "em breve"
