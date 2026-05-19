# app/whatsapp.py  # F7/F8/F10 fixes applied 2026-04-04
import os
import json
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List
import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlmodel import delete, select
from sqlmodel.ext.asyncio.session import AsyncSession
from app import crud
from app.database import async_session
from app.models import (
    Product,
    PaymentConfig,
    CartItem,
    ShoppingCart,
    DeliveryMethod,
    Bot,
    OrderStatus,
    CartState,
    Contact,
)
from app.openai_client import (
    get_ai_decision,
    get_chat_response_gpt,
    extract_potential_items,
)
from app.item_extraction import (
    extract_items_local,
    extract_items_with_quantities,
    rewrite_as_structured_order,
)
from app.menu_storage import generate_presigned_url
from app.prompt_central import create_central_prompt
from app.tools_definition import tools_schema
from datetime import datetime, timedelta
import regex as re
from app.semantic_router import semantic_intent, THRESHOLDS
from app.payment_service import create_pix_payment
from sqlalchemy.exc import IntegrityError
import mercadopago
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
from app.billing_cache import get_tier_by_phone_id
import logging
import time as _time

FREE_TIER_FOOTER = "\n\n_Atendimento por ZenBotZ®_"


async def _apply_free_tier_branding(text: str, phone_id: str) -> str:
    """Append the Free-tier footer when the bot behind `phone_id` is on Free.

    Fails open: on Redis/DB errors (get_tier_by_phone_id returns None) or
    when the resolved tier is anything other than 'free', the original
    text is returned unchanged.
    """
    if not text or not phone_id:
        return text
    if FREE_TIER_FOOTER.strip() in text:
        return text
    tier = await get_tier_by_phone_id(phone_id)
    if tier == "free":
        return text + FREE_TIER_FOOTER
    return text


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


def _presign_menu_url(raw_url: str | None) -> str | None:
    """Return a presigned S3 URL for the menu, or None if presigning is unavailable."""
    if not raw_url:
        return None
    presigned = generate_presigned_url(raw_url, expiration=86400)
    if presigned != raw_url:
        return presigned
    # URL unchanged — if it's a private S3 URL that couldn't be presigned, skip media
    if ".s3." in raw_url and ".amazonaws.com" in raw_url:
        logger.warning(
            "Presigning skipped for S3 URL (AWS_BUCKET_NAME not set?), sending text-only"
        )
        return None
    # Non-S3 URL (already public), pass through
    return raw_url


@dataclass
class ParsedIngress:
    """Channel-neutral parsed ingress payload (plan/in_browser_bots.md Phase 1.3).

    The parsers (`_parse_whatsapp_payload`, `_parse_web_payload`) turn each
    channel's raw payload into this normalized shape. Downstream gates and
    handlers read from a ParsedIngress instead of a channel-specific envelope,
    which lets the same pipeline serve both WhatsApp webhooks and the
    Phase 2 `/chat` ingress without case analysis in the body.

    Identity rule: `contact_identity` is the value that goes into
    `Contact.phone_number` — for WhatsApp it's the real E.164, for web it's
    the synthesized `web:{session_id}` per A1b. The channel adapter is
    responsible for the synthesis; the pipeline treats it as opaque.

    Channel-specific fields (e.g. incoming_phone_id, bot_display_phone for
    WhatsApp; bot_id for web) live alongside the neutral fields so the
    bot-lookup gate can consume the right one based on channel.
    """

    contact_identity: str
    message_id: str
    text_body: str
    msg_type: str = "text"
    audio_media_id: str | None = None
    # WhatsApp-specific routing fields (used by _find_bot to look up the bot).
    incoming_phone_id: str | None = None
    bot_display_phone: str | None = None
    # Web-specific routing field (URL param identifies the bot directly).
    bot_id: int | None = None
    # Channel adapter session id (only set for web). Mirrored into
    # MessageContext.channel_metadata['session_id'] so ctx.reply() can route.
    session_id: str | None = None


def _parse_whatsapp_payload(data: dict) -> ParsedIngress | None:
    """Extract a ParsedIngress from a Meta Cloud API webhook payload.

    Returns None for non-message events (status updates, read receipts,
    etc.) so the caller can early-return. The few log lines for status
    updates stay here — they describe THIS payload, not the next step.
    """
    entry = data.get("entry", [])[0]
    changes = entry.get("changes", [])[0]
    value = changes.get("value", {})

    if "messages" not in value:
        # Status updates: log them for diagnostics, then signal "skip".
        statuses = value.get("statuses", [])
        if statuses:
            for s in statuses:
                logger.info(
                    "WhatsApp status update: id=%s status=%s recipient=%s errors=%s",
                    s.get("id"),
                    s.get("status"),
                    s.get("recipient_id"),
                    s.get("errors"),
                )
        return None

    message_data = value["messages"][0]
    contact_number = message_data["from"]
    message_id = message_data["id"]
    msg_type = message_data.get("type", "text")

    audio_media_id: str | None = None
    if msg_type in ("audio", "voice"):
        audio_media_id = message_data.get("audio", {}).get("id") or message_data.get(
            "voice", {}
        ).get("id")
        text_body = ""  # Will be replaced by transcript downstream.
    else:
        text_body = message_data.get("text", {}).get("body", "")

    return ParsedIngress(
        contact_identity=contact_number,
        message_id=message_id,
        text_body=text_body,
        msg_type=msg_type,
        audio_media_id=audio_media_id,
        incoming_phone_id=value["metadata"]["phone_number_id"],
        bot_display_phone=value["metadata"]["display_phone_number"],
    )


def _parse_web_payload(data: dict) -> ParsedIngress:
    """Phase 2 implements this. Until then any caller hitting it is
    exercising an incomplete path — fail loudly rather than silently
    constructing a half-valid ParsedIngress.

    Expected payload shape (per plan/in_browser_bots.md §2.1):
      {
        "bot_id": int,
        "session_id": str (UUID),
        "message_id": str (client-generated UUID),
        "text": str,
      }

    Web identity synthesis (A1b): contact_identity := f"web:{session_id}".
    """
    raise NotImplementedError(
        "Web payload parsing arrives with Phase 2 (POST /chat/{bot_id}/message)."
    )


@dataclass
class MessageContext:
    """Bundles the variables threaded through process_whatsapp_message handlers.

    Channel fields below are additive groundwork for plan/in_browser_bots.md
    (Phase 1.2). WhatsApp callers leave them at defaults; the web ingress
    adapter (Phase 2) will set them when constructing a web MessageContext.
    The existing `contact_number`, `token`, `phone_id` fields remain for
    backward compatibility — Phase 1.3 will migrate call sites to use
    `channel_metadata` and a unified `ctx.reply(...)` helper.
    """

    session: AsyncSession
    bot: Bot
    contact: Contact
    cart: ShoppingCart
    contact_number: str
    text_body: str
    token: str
    phone_id: str
    # New channel-aware fields (Phase 1.2). Default to WhatsApp so every
    # existing construction site continues to work without changes.
    channel: str = "whatsapp"  # Channel enum value
    channel_metadata: dict = field(default_factory=dict)

    @property
    def contact_identity(self) -> str:
        """Channel-neutral identity key. Alias of contact_number — same
        value for WhatsApp (E.164) and web (`web:{session_id}` synthesis,
        A1b in the plan). New code paths should reach for this name; the
        legacy attribute stays for the existing 30+ call sites until the
        Phase 1.2c migration replaces them."""
        return self.contact_number

    async def reply(
        self,
        text: str,
        *,
        media_url: str | None = None,
        media_type: str = "image",
    ) -> None:
        """Channel-aware egress seam.

        WhatsApp routes to `send_whatsapp_message` (the existing transport).
        Web routes to `broadcast_web_reply` (Phase 1.5) — publishes an SSE
        event on Redis PubSub channel `chat:{bot_id}:{session_id}`, which
        the Phase 2 `GET /chat/stream` endpoint will forward to the widget.

        This method intentionally does NOT replace direct
        `send_whatsapp_message(...)` calls in this file yet — Phase 1.2c
        will migrate the ~30 call sites under the protection of the full
        simulation-suite regression gate. Until then, `ctx.reply()` is
        the canonical entry point for any NEW egress added during the
        web-channel rollout."""
        if self.channel == "whatsapp":
            await send_whatsapp_message(
                to=self.contact_number,
                message=text,
                token=self.token,
                phone_id=self.phone_id,
                media_url=media_url,
                media_type=media_type,
            )
            return

        if self.channel == "web":
            from app.web_channel import broadcast_web_reply

            session_id = self.channel_metadata.get("session_id")
            if not session_id:
                raise ValueError(
                    "channel='web' MessageContext missing "
                    "channel_metadata['session_id']"
                )
            attachments: list[dict] = []
            if media_url:
                attachments.append({"type": media_type, "url": media_url})
            await broadcast_web_reply(
                bot_id=self.bot.id,
                session_id=session_id,
                text=text,
                attachments=attachments,
            )
            return

        raise ValueError(f"Unknown channel: {self.channel!r}")


# Extrai "QTD + NOME" da pergunta de confirmação (ex.: "1 Gnocchis de la Mémé Forte, 2 X, ...")
_ITEM_FROM_Q_RE = re.compile(
    r"(\d{1,6})\s+([A-Za-zÀ-ÿ'´`^~\- ]+?)(?:\s+por\s*R\$\s*[\d.,]+|\s*(?:,| e |$))",
    re.IGNORECASE,
)


async def _load_bot_payment_config(bot: "Bot", session: AsyncSession) -> None:
    """Load the bot's payment_config without expiring column attributes.

    Uses a direct query + ``set_committed_value`` instead of
    ``session.refresh(bot, attribute_names=["payment_config"])`` which would
    expire every column on the bot object (restaurant_name, delivery_fee, etc.).
    """
    from sqlalchemy.orm.attributes import set_committed_value

    result = await session.execute(
        select(PaymentConfig).where(PaymentConfig.bot_id == bot.id)
    )
    set_committed_value(bot, "payment_config", result.scalars().first())


async def _bot_has_pix(bot: "Bot", session: AsyncSession) -> bool:
    """Check if the bot can accept PIX payments (MP connected or manual pix_key)."""
    await _load_bot_payment_config(bot, session)
    if bot.payment_config and bot.payment_config.is_active:
        return True
    if bot.pix_key:
        return True
    return False


async def _load_cart_items_with_products(
    cart: "ShoppingCart",
    session: AsyncSession,
    load_contact: bool = False,
) -> None:
    """Load cart items with their products (and optionally the contact).

    Uses direct queries + ``set_committed_value`` exclusively — **never**
    ``session.refresh``, which expires every column on the target object
    and triggers ``greenlet_spawn`` errors whenever an expired column is
    later accessed in async context.
    """
    from sqlalchemy.orm.attributes import set_committed_value

    # 1. Load cart items
    result = await session.execute(
        select(CartItem).where(CartItem.cart_id == cart.id).order_by(CartItem.id)
    )
    items = list(result.scalars().all())
    set_committed_value(cart, "items", items)

    # 2. Bulk-load products for all items
    if items:
        product_ids = [item.product_id for item in items]
        result = await session.execute(
            select(Product).where(
                Product.id.in_(product_ids),
                Product.is_deleted == False,  # noqa: E712
            )
        )
        products_by_id = {p.id: p for p in result.scalars().all()}
        for item in items:
            set_committed_value(item, "product", products_by_id.get(item.product_id))

    # 3. Optionally load contact
    if load_contact and cart.contact_id:
        result = await session.execute(
            select(Contact).where(Contact.id == cart.contact_id)
        )
        set_committed_value(cart, "contact", result.scalars().first())


def _payment_prompt(has_pix: bool) -> str:
    """Build the payment method prompt based on PIX availability."""
    if has_pix:
        return "Como quer pagar?\n\n💠 *PIX* (na hora)\n💳 *Cartão* (na entrega)\n💵 *Dinheiro*"
    return "Como quer pagar?\n\n💳 *Cartão* (na entrega)\n💵 *Dinheiro*"


async def _download_whatsapp_media(media_id: str, token: str) -> bytes:
    """Download media from WhatsApp Cloud API (two-step: get URL, then download).

    Args:
        media_id: The media ID from the WhatsApp message payload.
        token: Bot's WhatsApp access token.

    Returns:
        Raw audio bytes.

    Raises:
        httpx.HTTPStatusError: On download failure.
    """
    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        # Step 1: Get download URL
        meta_resp = await client.get(
            f"https://graph.facebook.com/v20.0/{media_id}",
            headers=headers,
        )
        meta_resp.raise_for_status()
        download_url = meta_resp.json()["url"]

        # Step 2: Download the audio binary
        audio_resp = await client.get(download_url, headers=headers)
        audio_resp.raise_for_status()
        return audio_resp.content


async def _build_whisper_prompt(session, bot) -> str:
    """Build a Whisper conditioning prompt from the bot's product catalog.

    Includes restaurant name, ordering phrases, product names (deduplicated),
    and keywords. Limited to ~800 chars (~224 Whisper tokens).
    """
    products = await crud.get_products_by_bot_id(session, bot.id)
    available = [
        p for p in products if p.is_available and not getattr(p, "is_deleted", False)
    ]

    # 1. Restaurant name
    parts: list[str] = []
    if bot.restaurant_name:
        parts.append(bot.restaurant_name)

    # 2. Common ordering phrases
    parts.extend(
        [
            "quero",
            "me manda",
            "pode mandar",
            "vou querer",
            "tira",
            "sem",
            "com extra",
            "adicional",
            "pedido",
            "entrega",
            "retirada",
            "pix",
            "dinheiro",
        ]
    )

    # 3. Product names — deduplicated at word level, longest names first
    seen_words: set[str] = set()
    for p in sorted(available, key=lambda x: len(x.name), reverse=True):
        for word in p.name.split():
            w_lower = word.lower().strip(".,;:!?")
            if w_lower not in seen_words and len(w_lower) > 2:
                parts.append(word)
                seen_words.add(w_lower)

    # 4. Keywords from products (if not already covered by names)
    for p in available:
        if p.keywords:
            for kw in p.keywords.split(","):
                kw = kw.strip()
                if kw.lower() not in seen_words and len(kw) > 2:
                    parts.append(kw)
                    seen_words.add(kw.lower())

    prompt = ", ".join(parts)

    # Truncate to ~800 chars (Whisper's ~224 token limit)
    if len(prompt) > 800:
        prompt = prompt[:800].rsplit(",", 1)[0]

    return prompt


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
    session: AsyncSession,
    incoming_phone_id: str | None = None,
    bot_display_phone: str | None = None,
    *,
    channel: str = "whatsapp",
    bot_id: int | None = None,
) -> Bot | None:
    """Look up the Bot for an incoming message (plan/in_browser_bots.md Phase 1.4).

    WhatsApp path (channel='whatsapp', default): look up by
    `phone_number_id` from the webhook metadata, fall back to
    `display_phone_number` if the index miss happens (occasional Meta
    drift between the two values). Unchanged from pre-Phase 1.4 behavior;
    existing positional callers continue to work because the new
    `channel`/`bot_id` parameters are kwargs with defaults.

    Web path (channel='web'): the URL `POST /chat/{bot_id}/message`
    identifies the bot directly, so we look up by primary key. A
    web-only bot has empty `phone_number_id`, so the WhatsApp path
    would never find it — channel branching is the seam.

    Returns None when no bot matches OR when the call shape is wrong
    (channel='web' without bot_id, etc.). The caller drops the message
    on None — same contract for both channels.
    """
    if channel == "web":
        if bot_id is None:
            logger.error("_find_bot called with channel='web' but no bot_id")
            return None
        result = await session.execute(select(Bot).where(Bot.id == bot_id))
        bot = result.scalars().first()
        if not bot:
            logger.error("Bot not found for web message, bot_id=%s", bot_id)
        return bot

    # WhatsApp path — unchanged behavior.
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
    """Returns True if the bot's plan denies usage (caller should stop).

    Bots without a paid Subscription row fall through to the Free plan,
    which is seeded with a 15 orders/month cap and overage billing — the
    monthly cap is enforced by the usage/billing layer, not this gate.
    """
    sub = await crud.get_subscription_by_bot(session, bot.id)

    grace_period_days = 3
    is_blocked = False

    if not sub:
        # No paid sub → Free plan baseline
        is_blocked = not await crud.is_plan_active(session, "free")
    elif sub.cancel_at_period_end:
        # Self-serve cancellation: keep the paid plan active until the
        # period ends, then fall through to Free automatically.
        now = utcnow()
        period_end = sub.current_period_end.replace(tzinfo=None)
        if now <= period_end:
            is_blocked = not await crud.is_plan_active(session, sub.plan_type)
        else:
            is_blocked = not await crud.is_plan_active(session, "free")
    else:
        now = utcnow()
        expiration_limit = sub.current_period_end.replace(tzinfo=None) + timedelta(
            days=grace_period_days
        )
        if sub.status in ("cancelled", "paused"):
            is_blocked = True
        elif sub.status != "authorized" and now > expiration_limit:
            is_blocked = True
        elif not await crud.is_plan_active(session, sub.plan_type):
            is_blocked = True

    if is_blocked:
        logger.warning(
            "Bot blocked by plan gate: bot_id=%s user_id=%s", bot.id, bot.user_id
        )
        maintenance_msg = (
            "Olá! Nosso atendimento automático está em manutenção no momento.\n\n"
            "Um atendente retornará em breve. Obrigado pela compreensão! 🙏"
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
    menu_url = _presign_menu_url(bot.menu_url)
    media_type = None
    if menu_url:
        media_type = "document" if bot.menu_url.lower().endswith(".pdf") else "image"

    base_msg = bot.closing_message or "No momento não estamos atendendo. 🌙"
    rich_closing_msg = (
        f"{base_msg}\n\n"
        f"⏰ *Voltamos {next_opening}*\n\n"
        "Enquanto isso, confira nosso cardápio e já escolha seus favoritos! 😋"
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


async def _check_bot_has_products(
    session: AsyncSession, bot: Bot, contact_number: str
) -> bool:
    """Returns True if the bot has no products (caller should stop)."""
    products = await crud.get_products_by_bot_id(session, bot.id)
    if products:
        return False

    logger.warning("No products configured: bot_id=%s", bot.id)
    maintenance_msg = (
        "Olá! Nosso atendimento automático está em manutenção no momento.\n\n"
        "Um atendente retornará em breve. Obrigado pela compreensão! 🙏"
    )
    await send_whatsapp_message(
        to=contact_number,
        message=maintenance_msg,
        token=decrypt_value(bot.whatsapp_token),
        phone_id=bot.phone_number_id,
    )
    return True


_SESSION_TIMEOUT = timedelta(minutes=10)
_LONG_TIMEOUT = timedelta(hours=12)


async def _send_welcome_with_menu(
    session, bot, cart, contact_number, text_body, contact=None
):
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

    # F-01: Personalize greeting for returning customers
    restaurant = bot.restaurant_name or "nosso restaurante"
    if contact and contact.name and contact.last_order_date:
        greeting = f"Olá, {contact.name}! Bem-vindo(a) de volta ao *{restaurant}*! 😊"
        if contact.default_address_json:
            addr = contact.default_address_json.get("full_address", "")
            if addr:
                # Collapse multi-line address to single line for clean formatting
                addr_oneline = addr.replace("\n", ", ").strip(", ")
                greeting += f"\n\n📍 Endereço salvo: {addr_oneline}"
        greeting += (
            "\n\nDiga *repetir pedido* para pedir o mesmo"
            " ou escolha algo novo no cardápio!"
            f"\n\n{example_text}"
        )
    else:
        greeting = (
            f"Olá! Bem-vindo(a) ao *{restaurant}*! 😊"
            "\n\nDá uma olhada no cardápio e me conta o que vai querer"
            " — pode digitar ou mandar áudio!"
            f"\n\n{example_text}"
        )

    response_to_user = greeting
    menu_url = _presign_menu_url(bot.menu_url)
    media_type = None
    if menu_url:
        media_type = "document" if bot.menu_url.lower().endswith(".pdf") else "image"

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
    await session.flush()


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
        # Clear conversation history — stale context from old session
        if mctx.contact:
            await crud.clear_contact_history(session, mctx.contact.id)
        cart.state = CartState.GREETING
        cart.delivery_method = None
        await _send_welcome_with_menu(
            session, bot, cart, contact_number, text_body, contact=mctx.contact
        )
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
        # F-01: Check for saved address
        await _load_cart_items_with_products(cart, session, load_contact=True)
        saved_addr = None
        if cart.contact and cart.contact.default_address_json:
            saved_addr = cart.contact.default_address_json.get("full_address")
        if saved_addr:
            cart.pending_address = saved_addr
            cart.state = CartState.AWAITING_ADDRESS_CONFIRMATION
            _addr_oneline = saved_addr.replace("\n", ", ").strip(", ")
            response = f"Entregar no endereço salvo?\n\n📍 {_addr_oneline}\n\nResponda *Sim* ou *Não* (para informar outro endereço)."
        else:
            cart.state = CartState.AWAITING_CEP
            response = "Para a entrega, me informe seu *CEP* 📍"

    elif "retirada" in user_text or "buscar" in user_text or user_text == "2":
        cart.delivery_method = DeliveryMethod.PICKUP
        await _load_cart_items_with_products(cart, session, load_contact=True)
        if cart.contact and cart.contact.name:
            cart.state = CartState.AWAITING_PAYMENT_METHOD
            final_summary = _build_cart_summary_message(cart, bot, "🛍️")
            has_pix = await _bot_has_pix(bot, session)
            response = (
                f"Perfeito, retirada no balcão para *{cart.contact.name}*! 🛍️\n\n"
                f"{final_summary}\n\n"
                f"{_payment_prompt(has_pix)}"
            )
        else:
            cart.state = CartState.AWAITING_CUSTOMER_NAME
            response = "Retirada no balcão! 🛍️ Me diga seu *nome completo* para registrar o pedido."
    else:
        response = "Não entendi. Por favor, responda *Entrega* ou *Retirada*."

    cart.last_activity_at = utcnow()
    session.add(cart)
    await send_whatsapp_message(
        mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
    )
    await crud.add_interaction_to_history(
        session, bot.id, mctx.contact_number, mctx.text_body, response
    )
    await session.flush()
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
        response = "Tudo bem! Quando quiser fazer um pedido, é só me chamar. 😊"
        cart.last_activity_at = utcnow()
        session.add(cart)
        await session.flush()
        await send_whatsapp_message(
            mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
        )
        return response

    # 2. Switch to pickup
    if "retirada" in msg_lower or "buscar" in msg_lower or "balcao" in msg_lower:
        cart.delivery_method = "pickup"
        cart.partial_address = None
        cart.pending_address = "Retirada no Balcão"
        cart.pix_only = False
        from sqlalchemy.orm.attributes import set_committed_value as _scv

        if cart.contact_id:
            _c = await session.execute(
                select(Contact).where(Contact.id == cart.contact_id)
            )
            _scv(cart, "contact", _c.scalars().first())
        if cart.contact and cart.contact.name:
            cart.state = CartState.AWAITING_PAYMENT_METHOD
            has_pix = await _bot_has_pix(bot, session)
            response = (
                f"Combinado! Retirada no balcão para *{cart.contact.name}*, sem taxa de entrega. 🛍️\n\n"
                f"{_payment_prompt(has_pix)}"
            )
        else:
            cart.state = CartState.AWAITING_CUSTOMER_NAME
            response = "Combinado! Retirada no balcão, sem taxa de entrega. 🛍️\nMe diga seu *nome completo* para registrar o pedido."
        cart.last_activity_at = utcnow()
        session.add(cart)
        await session.flush()
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
            "CEP não encontrado. Tente novamente ou escolha:\n\n"
            "🛍️ *Retirada* — retirar no local\n"
            "❌ *Cancelar* — voltar ao início"
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
                f"Infelizmente seu endereço está fora da nossa área de entrega ({msg_erro}, máx {max_rad}km).\n\n"
                "Mas você pode:\n"
                "🛍️ *Retirada* — retirar no local, sem taxa!\n"
                "❌ *Cancelar* — voltar ao início"
            )
        else:
            cart.partial_address = address_data
            cart.delivery_method = "delivery"
            cart.state = CartState.AWAITING_NUMBER_COMPLEMENT
            street = address_data.get("street", "Rua sem nome")
            neigh = address_data.get("neighborhood", "")
            response = (
                f"📍 Encontrei:\n"
                f"*{street} — {neigh}*\n\n"
                f"Informe o *número* e *complemento* (ex: 142, Apto 3)."
            )

    cart.last_activity_at = utcnow()
    session.add(cart)
    await session.flush()
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
        f"{street}, {number_complement}, "
        f"{neighborhood} - {city}/{state}, "
        f"CEP: {cep_display}"
    )

    cart.pending_address = full_address
    cart.partial_address = None
    cart.state = CartState.AWAITING_ADDRESS_CONFIRMATION

    response = f"Confirme seu endereço:\n\n📍 {full_address}\n\nCorreto? *Sim* ou *Não*"

    cart.last_activity_at = utcnow()
    session.add(cart)
    await send_whatsapp_message(
        mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
    )
    await crud.add_interaction_to_history(
        session, mctx.bot.id, mctx.contact_number, mctx.text_body, response
    )
    await session.flush()
    return response


async def _handle_address_confirmation(
    mctx: MessageContext, intent: str | None
) -> str | None:
    """Handles AWAITING_ADDRESS_CONFIRMATION state. Returns response string or None."""
    cart, session = mctx.cart, mctx.session
    if cart.state != CartState.AWAITING_ADDRESS_CONFIRMATION:
        return None

    # When intent classification was skipped (checkout context-aware guard),
    # resolve CONFIRM/NEGATE locally from the message text.
    if intent is None:
        _lower = mctx.text_body.lower().strip()
        _CONFIRM_WORDS = {
            "sim",
            "s",
            "isso",
            "correto",
            "certo",
            "ok",
            "yes",
            "aham",
            "positivo",
            "confirmo",
            "isso mesmo",
        }
        _NEGATE_WORDS = {
            "nao",
            "não",
            "n",
            "no",
            "errado",
            "incorreto",
            "nope",
            "negativo",
            "trocar",
        }
        if _lower in _CONFIRM_WORDS or _lower.startswith("sim"):
            intent = "CONFIRM"
        elif (
            _lower in _NEGATE_WORDS
            or _lower.startswith("nao")
            or _lower.startswith("não")
        ):
            intent = "NEGATE"

    if intent == "CONFIRM":
        final_address = cart.pending_address
        await crud.save_address_to_cart(session, cart.id, final_address)
        cart.pending_address = None
        cart.state = CartState.AWAITING_CUSTOMER_NAME
        response = "Endereço salvo! ✅ Agora me diga seu *nome completo*."
    elif intent == "NEGATE":
        cart.pending_address = None
        cart.state = CartState.AWAITING_CEP
        response = (
            "Sem problema! Me informe o *CEP* novamente para corrigirmos o endereço."
        )
    else:
        response = "Responda *Sim* para confirmar ou *Não* para corrigir."

    cart.last_activity_at = utcnow()
    session.add(cart)
    await send_whatsapp_message(
        mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
    )
    await crud.add_interaction_to_history(
        session, mctx.bot.id, mctx.contact_number, mctx.text_body, response
    )
    await session.flush()
    return response


async def _handle_customer_name(mctx: MessageContext) -> str | None:
    """Handles AWAITING_CUSTOMER_NAME state. Returns response string or None."""
    cart, session, bot = mctx.cart, mctx.session, mctx.bot
    if cart.state != CartState.AWAITING_CUSTOMER_NAME:
        return None

    customer_name = mctx.text_body.strip()[:100]
    await crud.save_customer_name_to_contact(session, cart.contact_id, customer_name)

    await _load_cart_items_with_products(cart, session)
    cart.state = CartState.AWAITING_PAYMENT_METHOD
    final_summary = _build_cart_summary_message(cart, bot, "📦")

    has_pix = await _bot_has_pix(bot, session)
    payment_prompt = _payment_prompt(has_pix)

    response = (
        f"Perfeito, {customer_name.split(' ')[0]}! 😊\n\n"
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
    await session.flush()
    return response


async def _handle_payment_method(mctx: MessageContext) -> str | None:
    """Handles AWAITING_PAYMENT_METHOD state. Returns response string or None."""
    cart, session, bot, contact = mctx.cart, mctx.session, mctx.bot, mctx.contact
    if cart.state != CartState.AWAITING_PAYMENT_METHOD:
        return None

    user_text = mctx.text_body.lower()
    order_created = False
    pix_code_to_send = None
    has_pix = await _bot_has_pix(bot, session)

    detected_method = None
    if "pix" in user_text:
        detected_method = "pix"
    elif any(x in user_text for x in ["cartão", "cartao", "credito", "debito"]):
        detected_method = "card"
    elif any(x in user_text for x in ["dinheiro", "nota", "troco"]):
        detected_method = "money"

    # Reject PIX if the bot doesn't support it
    if detected_method == "pix" and not has_pix:
        response = "O pagamento via PIX não está disponível no momento.\nEscolha: *Cartão* ou *Dinheiro*."
        await send_whatsapp_message(
            mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
        )
        return response

    if not detected_method:
        prompt = _payment_prompt(has_pix)
        response = f"Não entendi a forma de pagamento.\n{prompt}"
        await send_whatsapp_message(
            mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
        )
        return response

    # Capture scalar values before any session.rollback() can expire them
    _bot_id = bot.id
    _bot_restaurant_name = bot.restaurant_name
    _bot_delivery_fee = bot.delivery_fee
    _bot_pix_key = bot.pix_key

    try:
        await _load_cart_items_with_products(cart, session, load_contact=True)
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
        if cart.delivery_method == DeliveryMethod.DELIVERY and _bot_delivery_fee > 0:
            total_amount += _bot_delivery_fee

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
            await _load_bot_payment_config(bot, session)
            client_token = (
                decrypt_value(bot.payment_config.access_token)
                if (bot.payment_config and bot.payment_config.is_active)
                else None
            )

            if client_token:
                pix_info = await create_pix_payment(
                    order_id=order.id,
                    total_amount=order.total_amount,
                    bot_name=_bot_restaurant_name,
                    contact_phone=contact.phone_number,
                    access_token_cliente=client_token,
                    webhook_token=order.webhook_token,
                )
                if pix_info:
                    response = "Pedido registrado! ✅\n\nCopie o código PIX abaixo para pagar.\n⏳ *Validade: 15 minutos*"
                    pix_code_to_send = pix_info["pix_copy_paste"]
                    order_created = True
                else:
                    response = "Problema ao gerar o PIX. Escolha *Cartão* ou *Dinheiro* para continuar."
                    await session.rollback()
            else:
                response = f"Pedido confirmado! ✅\n\n💠 *Chave PIX:* {_bot_pix_key}\n\nFaça o pagamento e enviaremos a confirmação aqui mesmo!"
                order_created = True

        elif detected_method == "card":
            order_created = True
            if cart.delivery_method == DeliveryMethod.DELIVERY:
                response = "Pedido confirmado! ✅\n\n💳 O entregador levará a maquininha. Avisaremos quando sair para entrega!"
            else:
                response = "Pedido confirmado! ✅\n\n💳 Pagamento na retirada. Avisaremos quando estiver pronto!"

        elif detected_method == "money":
            order_created = True
            response = f"Pedido confirmado! ✅\n\n💵 Total: *R$ {total_amount:.2f}* em dinheiro\nSepare o valor certinho para facilitar na entrega!"

        if order_created:
            # F-07: Append ETA to confirmation message
            eta_minutes = None
            if (
                cart.delivery_method == DeliveryMethod.DELIVERY
                and bot.default_delivery_time_minutes
            ):
                eta_minutes = bot.default_delivery_time_minutes
            elif (
                cart.delivery_method == DeliveryMethod.PICKUP
                and bot.default_pickup_time_minutes
            ):
                eta_minutes = bot.default_pickup_time_minutes
            if eta_minutes:
                if cart.delivery_method == DeliveryMethod.DELIVERY:
                    response += f"\n\n⏱️ Estimativa de tempo para entrega: ~{eta_minutes} minutos"
                else:
                    response += f"\n\n⏱️ Estimativa de tempo para ficar pronto: ~{eta_minutes} minutos"

            display_items = [f"{i.quantity}x {i.product.name}" for i in cart.items]
            # Capture ORM values before commit expires all objects
            _order_id = order.id
            _order_total = order.total_amount
            _customer_name = cart.contact.name if cart.contact else "Cliente"
            _cart_id = cart.id
            _owner_phone = bot.owner_notification_phone
            _delivery_method = cart.delivery_method
            _customer_address = cart.customer_address

            # F-01: Save address to contact for future sessions
            if (
                _delivery_method == DeliveryMethod.DELIVERY
                and _customer_address
                and contact
            ):
                contact.default_address_json = {
                    "full_address": _customer_address,
                }
                contact.last_order_date = utcnow()
                session.add(contact)
            elif contact:
                contact.last_order_date = utcnow()
                session.add(contact)

            await crud.clear_db_cart(session, _cart_id)
            # Clear conversation history — fresh context for next visit
            if contact:
                await crud.clear_contact_history(session, contact.id)
            # Commit BEFORE broadcast so the order is visible when the
            # frontend refetches via the SSE-triggered invalidation.
            await session.commit()

            try:
                await broadcast_order_update(
                    "new_order",
                    {
                        "order_id": _order_id,
                        "customer_name": _customer_name,
                        "customer_phone": mctx.contact_number,
                        "total": _order_total,
                        "status": "pending",
                        "payment_method": detected_method,
                        "items": display_items,
                        "created_at": str(utcnow()),
                    },
                    bot_id=_bot_id,
                )
            except Exception as e:
                logger.error("Failed to send broadcast for order: %s", e)

            # F-09: Send owner notification
            if _owner_phone:
                try:
                    items_str = "\n".join(display_items)
                    method_label = {
                        "pix": "PIX",
                        "card": "Cartão",
                        "money": "Dinheiro",
                    }.get(detected_method, detected_method)
                    owner_msg = (
                        f"🔔 *Novo pedido #{_order_id}*\n\n"
                        f"{items_str}\n\n"
                        f"💰 Total: R$ {_order_total:.2f}\n"
                        f"👤 {_customer_name}\n"
                        f"💳 {method_label}"
                    )
                    if (
                        _delivery_method == DeliveryMethod.DELIVERY
                        and _customer_address
                    ):
                        _addr_line = _customer_address.replace("\n", ", ").strip(", ")
                        owner_msg += f"\n📍 {_addr_line}"
                    owner_phone_formatted = (
                        _owner_phone
                        if _owner_phone.startswith("55")
                        else f"55{_owner_phone}"
                    )
                    await send_whatsapp_message(
                        to=owner_phone_formatted,
                        message=owner_msg,
                        token=mctx.token,
                        phone_id=mctx.phone_id,
                    )
                except Exception as e:
                    logger.warning("Failed to send owner notification: %s", e)

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
            return response
        else:
            await send_whatsapp_message(
                mctx.contact_number, response, token=mctx.token, phone_id=mctx.phone_id
            )
            await crud.add_interaction_to_history(
                session, _bot_id, mctx.contact_number, mctx.text_body, response
            )
            await session.flush()
            return response

    except Exception as e:
        logger.exception("Error in AWAITING_PAYMENT handler: %s", e)
        await session.rollback()
        response = "Ops, algo deu errado. Tente novamente em instantes."
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
            response = "Cancelado! O que mais posso ajudar? 😊"

    elif cart.state in [CartState.GREETING, CartState.SHOPPING]:
        if intent == "CONFIRM":
            if getattr(cart, "last_suggestions", None):
                # Show alternatives as a numbered list
                sug_res = await session.execute(
                    select(Product).where(
                        Product.id.in_(cart.last_suggestions),
                        Product.is_deleted == False,
                        Product.is_available == True,
                    )
                )
                sug_map = {p.id: p for p in sug_res.scalars().all()}
                sug_prods = [
                    sug_map[sid] for sid in cart.last_suggestions if sid in sug_map
                ]
                if sug_prods:
                    response = _format_product_suggestions_message(
                        sug_prods, "Aqui estão as opções disponíveis:"
                    )
                else:
                    cart.last_suggestions = None
                    # Nothing to confirm — fall through to shopping flow
                    return None
            else:
                # Nothing to confirm — fall through to shopping flow
                return None
        else:
            response = "Sem problema! Quer ver sugestões ou já sabe o que quer? 😊"
    else:
        if intent == "CONFIRM":
            # Nothing to confirm — fall through to shopping flow
            return None
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
    await session.flush()
    return response


async def _handle_clear_cart(mctx: MessageContext, intent: str | None) -> str | None:
    """Handles CLEAR_CART intent. Returns response string or None."""
    if intent != "CLEAR_CART":
        return None
    cart, session = mctx.cart, mctx.session
    clear_pending(cart)
    await crud.clear_db_cart(session, cart.id)
    cart.last_suggestions = None
    return "Carrinho limpo! 🛒 Quando quiser, é só pedir!"


async def _handle_show_cart(mctx: MessageContext, intent: str | None) -> str | None:
    """Handles SHOW_CART intent. Returns response string or None."""
    if intent != "SHOW_CART":
        return None
    await _load_cart_items_with_products(mctx.cart, mctx.session)
    return _build_cart_summary_message(mctx.cart, mctx.bot)


async def _handle_finish_order(mctx: MessageContext, intent: str | None) -> str | None:
    """Handles FINISH_ORDER intent. Returns response string or None if not applicable.
    Returns the response string when handled (caller must still send+commit for non-early-return paths)."""
    if intent != "FINISH_ORDER":
        return None

    cart, session, bot = mctx.cart, mctx.session, mctx.bot

    await _load_cart_items_with_products(cart, session, load_contact=True)

    if not cart.items:
        return "Seu carrinho está vazio. 🛒 Me diga o que quer pedir!"

    current_total = sum(item.product.price * item.quantity for item in cart.items)

    if (
        bot.min_order_value
        and bot.min_order_value > 0
        and current_total < bot.min_order_value
    ):
        missing = bot.min_order_value - current_total
        return (
            f"O pedido mínimo é *R$ {bot.min_order_value:.2f}* e você está em *R$ {current_total:.2f}*.\n\n"
            f"Faltam só *R$ {missing:.2f}* — que tal uma bebida ou sobremesa? 🥤"
        )

    elif cart.delivery_method is None:
        cart.state = CartState.AWAITING_DELIVERY_METHOD
        entrega_line = f"1️⃣ *Entrega* 🛵 — R$ {bot.delivery_fee:.2f}"
        retirada_line = "2️⃣ *Retirada* 🛍️ — Grátis"
        response = (
            "Como prefere receber?\n\n"
            f"{entrega_line}\n"
            f"{retirada_line}\n\n"
            "Responda *1* ou *2*."
        )

    elif cart.customer_address and cart.contact and cart.contact.name:
        logger.info("Address and name already saved, skipping to payment")
        cart.state = CartState.AWAITING_PAYMENT_METHOD
        has_pix = await _bot_has_pix(bot, session)
        _addr_display = (
            cart.customer_address.replace("\n", ", ").strip(", ")
            if cart.customer_address
            else ""
        )
        response = (
            f"Seus dados já estão salvos! 😊\n\n"
            f"📍 {_addr_display}\n"
            f"👤 *{cart.contact.name}*\n\n"
            f"{_payment_prompt(has_pix)}"
        )
    elif cart.customer_address:
        logger.info("Address saved but name missing, requesting name")
        cart.state = CartState.AWAITING_CUSTOMER_NAME
        response = "Endereço salvo! 📍 Me diga seu *nome completo* para finalizar."
    elif cart.pending_address:
        logger.info("Resuming flow: awaiting address confirmation")
        cart.state = CartState.AWAITING_ADDRESS_CONFIRMATION
        _pending_display = (
            cart.pending_address.replace("\n", ", ").strip(", ")
            if cart.pending_address
            else ""
        )
        response = (
            f"Vamos retomar! Confirme o endereço de entrega:\n\n"
            f"📍 {_pending_display}\n\n"
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
        response = "Para a entrega, me informe seu *CEP* 📍"

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
    # Filter out assistant messages containing "em falta"/"indisponível" to prevent
    # history contamination where the LLM repeats old unavailability messages.
    past_messages = []
    for h in history_records:
        if h.role == "assistant" and (
            "em falta" in (h.content or "").lower()
            or "indisponível" in (h.content or "").lower()
        ):
            continue
        past_messages.append({"role": h.role, "content": h.content})
    await _load_cart_items_with_products(cart, session)
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
            select(Product).where(
                Product.id.in_(cart.last_suggestions), Product.is_deleted == False
            )
        )
        sug_map = {p.id: p for p in sug_res.scalars().all()}
        recent_suggestions = [
            sug_map[id] for id in cart.last_suggestions if id in sug_map
        ]

    response_to_user = "Não entendi bem. 😅 Pode tentar de outra forma?"
    _em_falta_msg = ""
    cart_tool_processed = False
    _variant_map: dict[str, list[str]] = {}
    _full_menu: list = []

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
                    concept = re.sub(
                        r"[^\w\s\-áàâãéèêíìîóòôõúùûçÁÀÂÃÉÈÊÍÌÎÓÒÔÕÚÙÛÇ]", "", concept
                    ).strip()
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
            title = "Aqui estão algumas das nossas sugestões:"
        else:
            logger.info("Using AI-extracted suggestion concept: %s", concept)
            title = f"Encontrei estas opções relacionadas a '{concept}':"

        found_products = await crud.find_relevant_products(session, bot.id, [concept])

        if not found_products:
            # Fallback: show top products from the menu instead of a dead-end message
            _all_products_res = await session.execute(
                select(Product)
                .where(
                    Product.bot_id == bot.id,
                    Product.is_available == True,
                    Product.is_deleted == False,
                )
                .limit(10)
            )
            found_products = list(_all_products_res.scalars().all())
            title = "Aqui estão algumas das nossas sugestões:"
        if not found_products:
            response_to_user = "Puxa, não encontrei nenhuma sugestão no momento. Mas nosso cardápio está cheio de delícias! O que você gostaria?"
        else:
            found_products = await _get_meal_suggestions(
                session, bot.id, found_products
            )
            response_to_user = _format_product_suggestions_message(
                found_products, title
            )
            cart.last_suggestions = [p.id for p in found_products]
            # If only 1 suggestion, set pending action so "pode ser" adds it
            if len(found_products) == 1:
                save_pending(
                    cart,
                    "add_items_to_cart",
                    {
                        "items": [
                            {
                                "product_id": found_products[0].id,
                                "quantity": 1,
                                "product_name": found_products[0].name,
                            }
                        ]
                    },
                    f"Sugestão: {found_products[0].name}",
                )

    else:
        # ADD / REMOVE / MODIFY
        # T2-1: Local extraction first (no LLM call), LLM fallback only if RAG finds nothing
        extracted_items = extract_items_local(text_body)
        search_terms = extracted_items if extracted_items else [text_body]

        # Pre-parse quantity-item pairs for structured LLM query rewrite.
        # This prevents quantity shifting when items are unavailable.
        _qty_pairs = extract_items_with_quantities(text_body)

        # --- Programmatic cart reduction for "tire/tira N X" ---
        # Small LLMs (phi3:mini) struggle with subtraction; handle it directly.
        if intent == "MODIFY" and _qty_pairs and cart.items:
            # Detect full removal: "tira o X" without explicit quantity.
            # A standalone digit (not part of a size like "500g", "300ml")
            # means explicit qty. No standalone digit = remove ALL.
            import re as _re_mod

            # F2 fix (2026-04-09): the unit alternation needs `\b` AFTER it
            # because the bare letters `l` and `g` would otherwise match the
            # first letter of words like "latte", "limão", "linguiça",
            # "galinha", "goiabada", causing "deixa só 1 latte" to be parsed
            # as "1L atte" (a 1-litre size token), which made
            # _has_explicit_qty=False → full_removal=True → bot removes the
            # entire item instead of setting it to qty 1. The bug was latent
            # because pre-F2, "deixa só N X" went to ADD intent, never
            # reaching this code path.
            _has_explicit_qty = bool(
                _re_mod.search(
                    r"(?<![a-záàâãéèêíìîóòôõúùûç])\d+(?!\s*(?:ml|l|g|kg|un|pç|pecas|peças)\b)\b",
                    text_body,
                    _re_mod.IGNORECASE,
                )
            ) or any(q != 1 for q, _ in _qty_pairs)
            _full_removal = not _has_explicit_qty

            # F2: detect "deixa só N X" / "fica só N X" / "muda pra N X" —
            # SET semantics (N is the final qty), not subtract semantics.
            _qty_set_mode = bool(_QTY_REDUCE_RE.search(text_body))
            if _qty_set_mode:
                # Override full_removal — set-mode always has an explicit qty
                _full_removal = False

            _reduce_result = _programmatic_cart_reduce(
                _qty_pairs,
                cart.items,
                text_body,
                full_removal=_full_removal,
                set_mode=_qty_set_mode,
            )
            if _reduce_result:
                _updates, _response_parts = _reduce_result
                for _upd_item, _new_qty in _updates:
                    if _new_qty <= 0:
                        await session.execute(
                            delete(CartItem).where(CartItem.id == _upd_item.id)
                        )
                    else:
                        _upd_item.quantity = _new_qty
                        session.add(_upd_item)
                await session.flush()
                # Reload cart to build summary
                await _load_cart_items_with_products(cart, session)
                response_to_user = _build_cart_summary_message(cart, bot)
                return response_to_user

        # --- Programmatic cart ADD for multi-item orders ---
        # When extract_items_with_quantities finds explicit qty+name pairs,
        # match them directly against the product catalog. This is more
        # reliable than the LLM for structured orders with typos.
        if intent == "ADD" and _qty_pairs:
            _real_pairs = [(q, n) for q, n in _qty_pairs if q > 1 or len(n.split()) > 1]
            # Single-word qty=1 items (e.g. "brahma") are normally excluded from
            # programmatic matching to avoid false positives. But if the word is
            # long enough (≥4 chars), it's likely an abbreviated product name —
            # promote it to _real_pairs so it uses the reliable programmatic path
            # instead of falling through to the LLM (which often picks
            # answer_conversationally and triggers Option C). (Fix for F8)
            _single_word_candidates = [
                (q, n)
                for q, n in _qty_pairs
                if q == 1 and len(n.split()) == 1 and len(n) >= 4
            ]
            if _single_word_candidates:
                _real_pairs.extend(_single_word_candidates)
            # Track remaining single-word items (too short to promote) for
            # unavailable detection.
            _filtered_out_items = [
                n
                for q, n in _qty_pairs
                if q == 1 and len(n.split()) == 1 and len(n) >= 3 and len(n) < 4
            ]
            if _real_pairs:
                # Load all available products for fuzzy matching
                _all_prods_res = await session.execute(
                    select(Product).where(
                        Product.bot_id == bot.id,
                        Product.is_available == True,  # noqa: E712
                        Product.is_deleted == False,  # noqa: E712
                    )
                )
                _all_prods = list(_all_prods_res.scalars().all())

                if _all_prods:
                    from difflib import SequenceMatcher as _ProdSM

                    def _stem_word(w: str) -> str:
                        if len(w) > 4 and w.endswith("s"):
                            return w[:-1]
                        return w

                    def _extract_size_token(s: str) -> str | None:
                        """Extract numeric size like '300ml', '500g', '1kg'."""
                        import re as _re

                        m = _re.search(
                            r"(\d+\s*(?:ml|l|g|kg|un|pç|pecas|peças))\b",
                            s.lower(),
                        )
                        return m.group(1).replace(" ", "") if m else None

                    def _norm_for_match(s: str) -> str:
                        """Normalize for word-overlap matching: lowercase, strip
                        apostrophes, replace hyphens with spaces, remove digits."""
                        import re as _re

                        s = s.lower().replace("'", "").replace("\u2019", "")
                        s = s.replace("-", " ")
                        s = _re.sub(r"\d+", "", s)
                        return s.strip()

                    def _fuzzy_word_overlap(
                        words_a: set[str], words_b: set[str]
                    ) -> int:
                        """Count matching words with fuzzy tolerance for typos.
                        Exact matches count, plus fuzzy matches (ratio >= 0.75)
                        for words > 2 chars. Only 1-2 char words excluded."""
                        score = 0
                        _used_b = set()
                        for wa in words_a:
                            if len(wa) <= 2:
                                continue
                            if wa in words_b:
                                score += 1
                                _used_b.add(wa)
                                continue
                            # Fuzzy match for typos: "mignom"→"mignon", "chedar"→"cheddar"
                            for wb in words_b:
                                if wb in _used_b or len(wb) <= 2:
                                    continue
                                if _ProdSM(None, wa, wb).ratio() >= 0.75:
                                    score += 1
                                    _used_b.add(wb)
                                    break
                        return score

                    def _match_product(
                        item_name: str, exclude_ids: set[int]
                    ) -> Product | None:
                        name_norm = _norm_for_match(item_name)
                        name_words = {
                            _stem_word(w) for w in name_norm.split() if len(w) > 2
                        }
                        if not name_words:
                            return None

                        req_size = _extract_size_token(item_name)

                        best, best_score = None, 0
                        for p in _all_prods:
                            if p.id in exclude_ids:
                                continue
                            # Size constraint: if user specified 300ml,
                            # reject products with a different size (500ml)
                            prod_size = _extract_size_token(p.name)
                            if req_size and prod_size and req_size != prod_size:
                                continue
                            p_norm = _norm_for_match(p.name)
                            p_words = {
                                _stem_word(w) for w in p_norm.split() if len(w) > 2
                            }
                            # Word overlap score with fuzzy typo tolerance
                            overlap = _fuzzy_word_overlap(name_words, p_words)
                            # Boost when size tokens match exactly
                            if req_size and prod_size and req_size == prod_size:
                                overlap += 0.5
                            if overlap > best_score:
                                best_score = overlap
                                best = p
                            elif overlap == best_score and overlap > 0 and best:
                                # Tie-break: prefer higher string similarity
                                new_sim = _ProdSM(None, name_norm, p_norm).ratio()
                                old_sim = _ProdSM(
                                    None, name_norm, best.name.lower()
                                ).ratio()
                                if new_sim > old_sim:
                                    best = p
                        return best if best_score >= 1 else None

                    _prog_selections: list[tuple] = []  # (product, qty, search_term)
                    _prog_seen: set[int] = set()
                    _prog_unmatched: list[str] = list(_filtered_out_items)

                    for qty, item_name in _real_pairs:
                        matched = _match_product(item_name, _prog_seen)
                        if matched:
                            _prog_selections.append((matched, qty, item_name))
                            _prog_seen.add(matched.id)
                        elif item_name.strip() and len(item_name.strip()) >= 3:
                            _prog_unmatched.append(item_name.strip())

                    # Cross-check: detect unavailable substitutions (F10 fix).
                    # When "capuccino paçoca" (unavailable) fuzzy-matches
                    # "FROZEN CAPUCINO" (available), the user wanted the
                    # unavailable product. Compare similarity scores and
                    # remove the substitution before adding to cart.
                    if _prog_selections:
                        _xcheck_terms = [t for _, _, t in _prog_selections]
                        _xcheck_unavail = await crud.find_unavailable_products(
                            session, bot.id, _xcheck_terms
                        )
                        if _xcheck_unavail:

                            def _xn(s: str) -> str:
                                return (
                                    s.lower()
                                    .replace("'", "")
                                    .replace("\u2019", "")
                                    .strip()
                                )

                            _to_remove: set[int] = set()
                            for i, (matched_prod, _q, search_term) in enumerate(
                                _prog_selections
                            ):
                                t = _xn(search_term)
                                avail_sim = _ProdSM(
                                    None, t, _xn(matched_prod.name)
                                ).ratio()
                                best_unavail = max(
                                    _xcheck_unavail,
                                    key=lambda p: _ProdSM(None, t, _xn(p.name)).ratio(),
                                )
                                unavail_sim = _ProdSM(
                                    None, t, _xn(best_unavail.name)
                                ).ratio()
                                # Unavailable product is a clearly better match →
                                # user wanted the unavailable one, not the
                                # substitution. Require a meaningful gap (0.15)
                                # to avoid false positives when names are similar
                                # (e.g. "mignon com chedar" matching both
                                # "Mignon ao molho cheddar" and "Mignon com cebola").
                                if (
                                    unavail_sim >= 0.6
                                    and unavail_sim > avail_sim + 0.15
                                ):
                                    _to_remove.add(i)
                                    _prog_unmatched.append(search_term)
                            if _to_remove:
                                _prog_selections = [
                                    s
                                    for i, s in enumerate(_prog_selections)
                                    if i not in _to_remove
                                ]

                    if _prog_selections:
                        items_to_add = [
                            {
                                "product_id": p.id,
                                "quantity": qty,
                                "product_name": p.name,
                            }
                            for p, qty, _term in _prog_selections
                        ]
                        skipped: list[str] = []
                        await crud.add_items_to_db_cart(
                            session,
                            cart.id,
                            items_to_add,
                            bot_id=bot.id,
                            skipped_items=skipped,
                        )
                        await _load_cart_items_with_products(cart, session)

                        if cart.items:
                            cart.state = CartState.SHOPPING
                            clear_pending(cart)
                            response_to_user = (
                                _build_cart_summary_message(cart, bot, "✅")
                                + "\n\nAdicionado! Mais alguma coisa ou *só isso* para finalizar? 😊"
                            )
                            if skipped:
                                names = ", ".join(skipped)
                                response_to_user += (
                                    f"\n\n⚠️ Indisponível no momento: {names}"
                                )
                            # Check unmatched items against unavailable products
                            if _prog_unmatched:
                                _unavail_hits = await crud.find_unavailable_products(
                                    session, bot.id, _prog_unmatched
                                )
                                if _unavail_hits:
                                    _unavail_names = ", ".join(
                                        f"*{p.name}*" for p in _unavail_hits
                                    )
                                    _verb = (
                                        "estão" if len(_unavail_hits) > 1 else "está"
                                    )
                                    response_to_user += f"\n\nPuxa, {_unavail_names} {_verb} em falta no momento. 😕"
                            return response_to_user

        _structured_query = rewrite_as_structured_order(_qty_pairs)
        found_products = await crud.find_relevant_products(
            session, bot.id, search_terms
        )
        # LLM fallback: if local extraction + RAG found nothing, try LLM extraction
        unavailable_matches: list = []
        if not found_products:
            llm_items = await extract_potential_items(text_body)
            if llm_items:
                found_products = await crud.find_relevant_products(
                    session, bot.id, llm_items
                )

        # Always check for unavailable products — even when available ones were found.
        # This catches the case where "Johns Alcatra com Bacon" (unavailable) gets
        # silently substituted by "Johns Alcatra com Frango" (available).
        unavailable_candidates = await crud.find_unavailable_products(
            session, bot.id, search_terms
        )
        # Track which user search term matched which unavailable product,
        # so the LLM sees e.g. "cheesuburger" → THE CHEESEBURGER (explicit link).
        _unavail_term_map: dict[int, str] = {}  # product_id → user's search term
        if unavailable_candidates:
            if not found_products:
                # No available products — but don't blindly accept all candidates.
                # Verify each candidate has reasonable similarity to a search term.
                from difflib import SequenceMatcher as _SM

                _MIN_BLIND_SIM = 0.55

                def _norm_blind(s: str) -> str:
                    return s.lower().replace("'", "").replace("\u2019", "").strip()

                for cand in unavailable_candidates:
                    for term in search_terms:
                        t_norm = _norm_blind(term)
                        c_norm = _norm_blind(cand.name)
                        # Accept if SequenceMatcher passes OR if the search
                        # term is a substring of the product name (handles
                        # abbreviations like "brahma" → "Cerveja Brahma 600ml"
                        # where ILIKE matched but string ratio is low). (Fix F7)
                        if _SM(None, t_norm, c_norm).ratio() >= _MIN_BLIND_SIM or (
                            len(t_norm) >= 4 and t_norm in c_norm
                        ):
                            unavailable_matches.append(cand)
                            _unavail_term_map[cand.id] = term
                            break
            else:
                # Available products found — compare name similarity to decide
                # if the customer wanted the unavailable product instead.
                # Use extracted search terms (not full message) for fair comparison,
                # and strip apostrophes so "johns bacon" vs "john's bacon" scores high.
                from difflib import SequenceMatcher

                _MIN_UNAVAIL_SIM = (
                    0.45  # minimum absolute similarity to flag as em falta
                )

                def _norm(s: str) -> str:
                    return s.lower().replace("'", "").replace("\u2019", "").strip()

                _seen_unavail_ids: set[int] = set()
                for term in search_terms:
                    term_norm = _norm(term)
                    best_unavail = max(
                        unavailable_candidates,
                        key=lambda p: SequenceMatcher(
                            None, term_norm, _norm(p.name)
                        ).ratio(),
                    )
                    unavail_score = SequenceMatcher(
                        None, term_norm, _norm(best_unavail.name)
                    ).ratio()
                    # Substring containment is a strong signal even when
                    # SequenceMatcher ratio is low (abbreviations like
                    # "brahma" → "Cerveja Brahma 600ml"). (Fix F7)
                    _is_substring = len(term_norm) >= 4 and term_norm in _norm(
                        best_unavail.name
                    )
                    # Must exceed minimum absolute threshold AND beat available match
                    if unavail_score < _MIN_UNAVAIL_SIM and not _is_substring:
                        continue
                    best_avail_score = max(
                        SequenceMatcher(None, term_norm, _norm(p.name)).ratio()
                        for p in found_products
                    )
                    # Substring match for unavailable AND no substring match
                    # for any available product → clearly wanted the unavailable one
                    _avail_has_substring = (
                        any(term_norm in _norm(p.name) for p in found_products)
                        if _is_substring
                        else False
                    )
                    if (
                        unavail_score > best_avail_score
                        or (_is_substring and not _avail_has_substring)
                    ) and best_unavail.id not in _seen_unavail_ids:
                        unavailable_matches.append(best_unavail)
                        _seen_unavail_ids.add(best_unavail.id)
                        _unavail_term_map[best_unavail.id] = term

                # Fetch alternatives from same category as the first unavailable match
                if unavailable_matches:
                    _first_unavail = unavailable_matches[0]
                    if _first_unavail.category:
                        _unavail_ids = {p.id for p in unavailable_matches}
                        alt_query = (
                            select(Product)
                            .where(
                                Product.bot_id == bot.id,
                                Product.is_available == True,
                                Product.is_deleted == False,
                                Product.category == _first_unavail.category,
                                Product.id.not_in(_unavail_ids),
                            )
                            .limit(5)
                        )
                        alt_res = await session.execute(alt_query)
                        alternatives = list(alt_res.scalars().all())
                    else:
                        alternatives = []

                    if alternatives:
                        cart.last_suggestions = [p.id for p in alternatives]
                    clear_pending(cart)

        # Build em falta message programmatically (don't rely on LLM).
        # The LLM will focus on adding valid items; we append this after.
        _em_falta_msg = ""
        if unavailable_matches:
            _unavail_names = ", ".join(f"*{p.name}*" for p in unavailable_matches)
            _verb = "estão" if len(unavailable_matches) > 1 else "está"
            _em_falta_msg = (
                f"\n\nPuxa, {_unavail_names} {_verb} em falta no momento. 😕"
            )
            if cart.last_suggestions:
                _em_falta_msg += " Diga *'sim'* para ver alternativas!"

        # Filter false-positive search results before sending to LLM.
        # When embedding returns a product for a term that actually matches
        # an unavailable product, the LLM will wrongly substitute it.
        # Only keep products whose name has reasonable similarity to a search term.
        if found_products and len(found_products) > 1:
            from difflib import SequenceMatcher as _FilterSM

            _MIN_NAME_SIM = 0.35

            def _fnorm(s: str) -> str:
                return s.lower().replace("'", "").replace("\u2019", "").strip()

            _filtered_for_prompt = [
                p
                for p in found_products
                if any(
                    _FilterSM(None, _fnorm(term), _fnorm(p.name)).ratio()
                    >= _MIN_NAME_SIM
                    for term in search_terms
                )
            ]
            # Only use filtered list if it's not empty (preserve at least something)
            _prompt_products = (
                _filtered_for_prompt if _filtered_for_prompt else found_products
            )
        else:
            _prompt_products = found_products

        # Fetch ALL products for this bot (available + unavailable) — gives
        # LLM full context for disambiguation and em falta detection.
        _all_products_result = await session.execute(
            select(Product)
            .where(
                Product.bot_id == bot.id,
                Product.is_deleted == False,
            )
            .order_by(Product.category, Product.name)
        )
        _full_menu = list(_all_products_result.scalars().all())

        # Fetch distinct categories for the bot's menu
        _cat_result = await session.execute(
            select(Product.category)
            .distinct()
            .where(
                Product.bot_id == bot.id,
                Product.is_available == True,
                Product.is_deleted == False,
                Product.category.isnot(None),
            )
        )
        _available_categories = [r[0] for r in _cat_result]

        # Build variant map: {unavailable_name: [available variant names]}
        # so the prompt can explicitly tell the LLM not to substitute.
        _variant_map: dict[str, list[str]] = {}
        if unavailable_matches and _full_menu:
            for up in unavailable_matches:
                root = None
                for w in up.name.split():
                    if len(w) > 2:
                        root = w.lower()
                        break
                if root:
                    variants = [
                        p.name
                        for p in _full_menu
                        if p.name.lower().startswith(root) and p.id != up.id
                    ]
                    if variants:
                        _variant_map[up.name] = variants

        # Use structured query if available (pre-parsed quantities),
        # otherwise fall back to original message.
        _llm_query = _structured_query or text_body

        prompt = create_central_prompt(
            user_query=_llm_query,
            history=past_messages,
            restaurant_name=bot.restaurant_name,
            cart_items=cart_items,
            search_results=_full_menu,
            recent_suggestions=recent_suggestions,
            unavailable_products=unavailable_matches,
            unavailable_term_map=_unavail_term_map,
            available_categories=_available_categories,
            variant_map=_variant_map,
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

                # Skip additional cart-modifying tools if one was already processed
                if cart_tool_processed and tool_name in _CART_MODIFYING_TOOLS:
                    continue

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
                    # Guardrail: block LLM from adding items when the
                    # customer's message had no product references.
                    # This prevents the LLM from proactively adding items
                    # when the customer asked for suggestions, said something
                    # conversational, etc.
                    if not found_products and not _qty_pairs:
                        _src = _full_menu or []
                        _filtered = await _get_meal_suggestions(session, bot.id, _src)
                        if _filtered:
                            cart.last_suggestions = [p.id for p in _filtered]
                            response_to_user = _format_product_suggestions_message(
                                _filtered,
                                "Posso te ajudar! Veja nossas opções:",
                            )
                        else:
                            response_to_user = "O que você gostaria de pedir? 😊"
                        break

                    items_arg = tool_args.get("items", [])

                    if not items_arg:
                        # T2-1: Use local extraction for error message display
                        extracted_items_for_msg = extract_items_local(text_body)
                        items_str = (
                            " e ".join(f"'{item}'" for item in extracted_items_for_msg)
                            if extracted_items_for_msg
                            else "O item que você pediu"
                        )
                        if unavailable_matches:
                            names = ", ".join(p.name for p in unavailable_matches)
                            response_to_user = f"*{names}* está em falta no momento. 😕 Quer tentar outro item ou ver nossas sugestões?"
                        else:
                            response_to_user = f"Não encontrei *{items_str}* no nosso cardápio. 😕 Quer tentar outro item ou ver nossas sugestões?"
                    else:
                        # Override LLM quantities with programmatic ones.
                        # The LLM picks the right product; the extractor
                        # already parsed the right quantity.
                        if _qty_pairs and items_arg:
                            from difflib import SequenceMatcher as _QtySM

                            for llm_item in items_arg:
                                llm_name = (
                                    llm_item.get("product_name", "") or ""
                                ).lower()
                                if not llm_name:
                                    continue
                                best_pair_qty = None
                                best_pair_score = 0.0
                                for pq, pn in _qty_pairs:
                                    score = _QtySM(None, pn.lower(), llm_name).ratio()
                                    if score > best_pair_score:
                                        best_pair_score = score
                                        best_pair_qty = pq
                                if (
                                    best_pair_qty is not None
                                    and best_pair_score >= 0.4
                                    and best_pair_qty != llm_item.get("quantity")
                                ):
                                    logger.info(
                                        "[QTY_OVERRIDE] '%s': LLM=%d → prog=%d (score=%.2f)",
                                        llm_name,
                                        llm_item.get("quantity", 0),
                                        best_pair_qty,
                                        best_pair_score,
                                    )
                                    llm_item["quantity"] = best_pair_qty

                        clear_pending(cart)
                        qty_before = sum(item.quantity for item in cart.items)

                        skipped_items: list[str] = []
                        await crud.add_items_to_db_cart(
                            session,
                            cart.id,
                            items_arg,
                            bot_id=bot.id,
                            skipped_items=skipped_items,
                        )
                        await _load_cart_items_with_products(cart, session)

                        qty_after = sum(item.quantity for item in cart.items)

                        if qty_after > qty_before:
                            product_ids = [
                                i.get("product_id")
                                for i in items_arg
                                if isinstance(i, dict)
                                and i.get("product_id") is not None
                            ]
                            if (
                                not any(
                                    pid in (cart.last_suggestions or [])
                                    for pid in product_ids
                                )
                                and not unavailable_matches
                            ):
                                # Only clear suggestions if there are no
                                # unavailable matches — em falta sets
                                # last_suggestions for the "sim" alternatives
                                # flow and must not be overwritten here.
                                cart.last_suggestions = None

                            skipped_msg = ""
                            if skipped_items:
                                names = ", ".join(skipped_items)
                                skipped_msg = f"\n\n⚠️ Indisponível no momento: {names}"

                            response_to_user = (
                                _build_cart_summary_message(cart, bot, "✅")
                                + "\n\nAdicionado! Mais alguma coisa ou *só isso* para finalizar? 😊"
                                + skipped_msg
                            )
                        else:
                            # T2-1: Use local extraction for error message display
                            extracted_items_for_msg = extract_items_local(text_body)
                            items_str = (
                                " e ".join(
                                    f"'{item}'" for item in extracted_items_for_msg
                                )
                                if extracted_items_for_msg
                                else "O item que você pediu"
                            )
                            if unavailable_matches:
                                names = ", ".join(p.name for p in unavailable_matches)
                                response_to_user = f"*{names}* está em falta no momento. 😕 Quer tentar outro item ou ver nossas sugestões?"
                            else:
                                response_to_user = f"Não encontrei *{items_str}* no nosso cardápio. 😕 Quer tentar outro item ou ver nossas sugestões?"
                    cart_tool_processed = True
                    continue

                elif tool_name in (
                    "remove_items_from_cart",
                    "modify_item_quantity",
                    "bulk_modify_quantities",
                    "update_item_observation",
                ):
                    clear_pending(cart)
                    _items_res = await session.execute(
                        select(CartItem).where(CartItem.cart_id == cart.id)
                    )
                    from sqlalchemy.orm.attributes import (
                        set_committed_value as _scv2,
                    )

                    _scv2(cart, "items", list(_items_res.scalars().all()))
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
                            await _load_cart_items_with_products(cart, session)
                            response_to_user = (
                                _build_cart_summary_message(cart, bot, "🔄")
                                + "\n\nAlgo mais?"
                            )

                    elif tool_name == "modify_item_quantity":
                        pid = _safe_int(tool_args.get("product_id"), "product_id")
                        newq = _safe_int(tool_args.get("new_quantity"), "new_quantity")

                        if pid is None or newq is None:
                            response_to_user = "Esse item não está no seu carrinho. Posso adicioná-lo para você?"
                        elif pid not in current_ids and newq > 0:
                            # Fallback: item not in cart yet — add it
                            skipped_items: list[str] = []
                            await crud.add_items_to_db_cart(
                                session,
                                cart.id,
                                [{"product_id": pid, "quantity": newq}],
                                bot_id=bot.id,
                                skipped_items=skipped_items,
                            )
                            await _load_cart_items_with_products(cart, session)
                            if skipped_items:
                                names = ", ".join(skipped_items)
                                response_to_user = f"*{names}* está em falta no momento. 😕 Quer tentar outro item?"
                            elif any(it.product_id == pid for it in cart.items):
                                response_to_user = (
                                    _build_cart_summary_message(cart, bot, "✅")
                                    + "\n\nAdicionado! Mais alguma coisa ou *só isso* para finalizar? 😊"
                                )
                            else:
                                response_to_user = "Esse item não está no seu carrinho. Posso adicioná-lo para você?"
                        elif pid not in current_ids:
                            response_to_user = "Esse item não está no seu carrinho. Posso adicioná-lo para você?"
                        else:
                            await crud.modify_item_quantity_in_db_cart(
                                session, cart.id, pid, newq
                            )
                            await _load_cart_items_with_products(cart, session)
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
                            await _load_cart_items_with_products(cart, session)
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
                        modify_updates = []
                        add_items = []
                        for upd in updates_raw:
                            pid = _safe_int(upd.get("product_id"), "product_id")
                            newq = _safe_int(upd.get("new_quantity"), "new_quantity")
                            if pid is None or newq is None:
                                continue
                            if pid in current_ids:
                                modify_updates.append(
                                    {"product_id": pid, "new_quantity": newq}
                                )
                            elif newq > 0:
                                # Fallback: item not in cart yet — add it
                                add_items.append({"product_id": pid, "quantity": newq})

                        if not modify_updates and not add_items:
                            response_to_user = "Não encontrei esses itens no seu carrinho. Posso sugerir opções para adicionar?"
                        else:
                            for upd in modify_updates:
                                await crud.modify_item_quantity_in_db_cart(
                                    session,
                                    cart.id,
                                    upd["product_id"],
                                    upd["new_quantity"],
                                )
                            skipped_items_bulk: list[str] = []
                            if add_items:
                                await crud.add_items_to_db_cart(
                                    session,
                                    cart.id,
                                    add_items,
                                    bot_id=bot.id,
                                    skipped_items=skipped_items_bulk,
                                )
                            await _load_cart_items_with_products(cart, session)

                            emoji = "✅" if add_items and not modify_updates else "✏️"
                            skipped_msg = ""
                            if skipped_items_bulk:
                                names = ", ".join(skipped_items_bulk)
                                skipped_msg = f"\n\n⚠️ Indisponível no momento: {names}"

                            suffix = (
                                "\n\nAdicionado! Mais alguma coisa ou *só isso* para finalizar? 😊"
                                if add_items and not modify_updates
                                else "\n\nAlgo mais?"
                            )
                            response_to_user = (
                                _build_cart_summary_message(cart, bot, emoji)
                                + suffix
                                + skipped_msg
                            )
                    cart_tool_processed = True
                    continue

                elif tool_name == "propose_and_confirm_action":
                    # If a cart tool already succeeded, skip propose_and_confirm —
                    # the LLM is likely trying to handle an unavailable item which
                    # we already handle programmatically via _em_falta_msg.
                    if cart_tool_processed:
                        continue
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
                                    Product.bot_id == bot.id,
                                    Product.id == pid,
                                    Product.is_deleted == False,
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
                    await session.flush()
                    return None

                elif tool_name in _CONVERSATIONAL_TOOLS:
                    if tool_name == "answer_conversationally":
                        conv_text = sanitize_llm_output(
                            tool_args.get(
                                "response_text",
                                "Não entendi o que você quis dizer. Pode tentar de outra forma?",
                            )
                        )
                        # Option C: When the LLM responds conversationally
                        # without adding anything to the cart, show the numbered
                        # product list directly instead of the LLM's text.
                        # This eliminates hallucinated categories and reduces
                        # friction by one turn (no need for "sim" follow-up).
                        _SHOPPING_INTENTS = {
                            "ADD",
                            "ADD_ITEMS",
                            "MODIFY",
                            "REMOVE",
                            "REQUEST_SUGGESTION",
                        }
                        if (
                            not cart_tool_processed
                            and intent in _SHOPPING_INTENTS
                            and not unavailable_matches
                        ):
                            _src = found_products or []
                            _filtered = await _get_meal_suggestions(
                                session, bot.id, _src
                            )
                            if _filtered:
                                cart.last_suggestions = [p.id for p in _filtered]
                                # Only show "Não encontrei X" when search terms
                                # look like actual food items, not conversational
                                # noise ("poxa tia chegou", "gente muita fome").
                                # Heuristic: if extraction kept >50% of the original
                                # words, it didn't really "extract" — it's noise.
                                _orig_word_count = len(text_body.split())
                                _extracted_word_count = sum(
                                    len(t.split()) for t in search_terms
                                )
                                _is_food_search = (
                                    _orig_word_count > 0
                                    and _extracted_word_count / _orig_word_count <= 0.5
                                )
                                _food_like_terms = (
                                    [t for t in search_terms if len(t) > 2]
                                    if _is_food_search
                                    else []
                                )
                                if not found_products and _food_like_terms:
                                    _items_str = ", ".join(
                                        f"*{t}*" for t in _food_like_terms
                                    )
                                    _title = (
                                        f"Não encontrei {_items_str} no nosso cardápio. 😕 "
                                        "Mas veja nossas opções:"
                                    )
                                else:
                                    # Use the LLM's conversational text as the title
                                    # if it's not garbage (not a fallback/error/em falta).
                                    _BAD_TITLE_MARKERS = {
                                        "não entendi",
                                        "nao entendi",
                                        "desculpe",
                                        "em falta",
                                        "indisponível",
                                        "não está disponível",
                                        "não encontrei",
                                    }
                                    _llm_title = conv_text.split("\n")[0].strip()
                                    if (
                                        _llm_title
                                        and len(_llm_title) > 5
                                        and not any(
                                            m in _llm_title.lower()
                                            for m in _BAD_TITLE_MARKERS
                                        )
                                    ):
                                        _title = _llm_title + " Veja nossas opções:"
                                    else:
                                        _title = "Posso te ajudar! Veja nossas opções:"
                                conv_text = _format_product_suggestions_message(
                                    _filtered, _title
                                )

                        if cart_tool_processed:
                            # Cart tool already succeeded — skip LLM's conversational
                            # follow-up entirely. Appending it causes issues like
                            # confirmation questions ("Só pra confirmar...") that
                            # lead to quantity doubling when the user says "sim".
                            pass
                        else:
                            response_to_user = conv_text

                    elif tool_name == "search_catalog_for_suggestions":
                        # LLM correctly identified a vague/suggestion request
                        # but we're in the ADD path — execute the search here.
                        try:
                            sug_concept = tool_args.get(
                                "search_concept", "prato principal"
                            )
                            sug_concept = str(sug_concept)[:100]
                            sug_concept = re.sub(
                                r"[^\w\s\-áàâãéèêíìîóòôõúùûçÁÀÂÃÉÈÊÍÌÎÓÒÔÕÚÙÛÇ]",
                                "",
                                sug_concept,
                            ).strip()
                            if not sug_concept:
                                sug_concept = "prato principal"
                        except (TypeError, AttributeError):
                            sug_concept = "prato principal"

                        sug_products = await crud.find_relevant_products(
                            session, bot.id, [sug_concept]
                        )
                        if sug_products:
                            sug_products = await _get_meal_suggestions(
                                session, bot.id, sug_products
                            )
                            cart.last_suggestions = [p.id for p in sug_products]
                            response_to_user = _format_product_suggestions_message(
                                sug_products,
                                f"Encontrei estas opções relacionadas a '{sug_concept}':",
                            )
                        else:
                            response_to_user = (
                                "Puxa, não encontrei nenhuma sugestão no momento. "
                                "Mas nosso cardápio está cheio de delícias! O que você gostaria?"
                            )
                        break

                    continue

                else:
                    # Unknown tool — show suggestions instead of "Não entendi"
                    if intent in ("ADD", "ADD_ITEMS"):
                        _search = search_terms if search_terms else ["prato principal"]
                        _alt = await crud.find_relevant_products(
                            session, bot.id, _search
                        )
                        _alt = (
                            await _get_meal_suggestions(session, bot.id, _alt)
                            if _alt
                            else []
                        )
                        if _alt:
                            cart.last_suggestions = [p.id for p in _alt]
                            response_to_user = _format_product_suggestions_message(
                                _alt, "Posso te ajudar! Veja nossas opções:"
                            )
                        else:
                            response_to_user = (
                                "Me diga o que gostaria de pedir e posso ajudar! 😊"
                            )
                    else:
                        response_to_user = "Não consegui entender sua solicitação. 😅 Pode reformular de outra forma?"
                    break

        else:
            # AI returned no tool calls — show suggestions instead of "Não entendi"
            if intent in ("ADD", "ADD_ITEMS"):
                _search = search_terms if search_terms else ["prato principal"]
                _alt = await crud.find_relevant_products(session, bot.id, _search)
                _alt = (
                    await _get_meal_suggestions(session, bot.id, _alt) if _alt else []
                )
                if _alt:
                    cart.last_suggestions = [p.id for p in _alt]
                    response_to_user = _format_product_suggestions_message(
                        _alt, "Posso te ajudar! Veja nossas opções:"
                    )
                else:
                    response_to_user = (
                        "Me diga o que gostaria de pedir e posso ajudar! 😊"
                    )
            else:
                response_to_user = (
                    "Desculpe, não consegui processar. 😅 Pode tentar de outra forma?"
                )

    # Final safety net: if the default "Não entendi" survived all handlers
    # (e.g. answer_with_found_products has no dispatch handler), show
    # menu suggestions instead for ADD intent.
    _DEFAULT_FALLBACK = "Não entendi bem. 😅 Pode tentar de outra forma?"
    if response_to_user == _DEFAULT_FALLBACK and intent in ("ADD", "ADD_ITEMS"):
        _alt = await _get_meal_suggestions(
            session, bot.id, found_products if found_products else []
        )
        if _alt:
            cart.last_suggestions = [p.id for p in _alt]
            response_to_user = _format_product_suggestions_message(
                _alt, "Posso te ajudar! Veja nossas opções:"
            )
        else:
            response_to_user = "Me diga o que gostaria de pedir e posso ajudar! 😊"

    # Post-process: remove auto-substituted variant products from cart.
    # The LLM may add variants (e.g. "Picanha com catupiry" when user said "picanha"
    # and "Picanha" is unavailable). Deterministically remove them.
    if cart_tool_processed and _variant_map:
        _variant_ids = {
            p.id
            for p in _full_menu
            if p.name in {n for names in _variant_map.values() for n in names}
        }
        _items_removed = False
        for item in list(cart.items):
            if item.product_id in _variant_ids:
                await crud.modify_item_quantity_in_db_cart(
                    session, cart.id, item.product_id, 0
                )
                _items_removed = True
        if _items_removed:
            await _load_cart_items_with_products(cart, session)
            if cart.items:
                response_to_user = (
                    _build_cart_summary_message(cart, bot, "✅")
                    + "\n\nAdicionado! Mais alguma coisa ou *só isso* para finalizar? 😊"
                )
            else:
                response_to_user = ""

    # Append programmatic em falta message if unavailable products were detected.
    # Strip any LLM-generated em falta text first to avoid duplication.
    if _em_falta_msg and unavailable_matches:
        # Build a set of words/terms to match against LLM's em falta text.
        # Includes: full product names, significant words (>3 chars) from names,
        # and user's search terms from _unavail_term_map.
        _match_terms: set[str] = set()
        for p in unavailable_matches:
            _match_terms.add(p.name.lower())
            for w in p.name.lower().split():
                if len(w) > 3:
                    _match_terms.add(w)
            # Also add the user's typo (e.g. "cheesuburger")
            _user_term = _unavail_term_map.get(p.id)
            if _user_term:
                _match_terms.add(_user_term.lower())

        _em_falta_keywords = {
            "em falta",
            "indisponível",
            "indisponivel",
            "não está disponível",
            "nao esta disponivel",
            "não temos",
            "nao temos",
            "não disponível",
            "não encontrei",
            "nao encontrei",
        }
        _cleaned_lines = []
        for _line in response_to_user.split("\n"):
            _line_lower = _line.lower()
            _is_duplicate = any(term in _line_lower for term in _match_terms) and any(
                kw in _line_lower for kw in _em_falta_keywords
            )
            if not _is_duplicate:
                _cleaned_lines.append(_line)
        response_to_user = "\n".join(_cleaned_lines).strip()
        response_to_user += _em_falta_msg

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
    _rolled_back = False
    try:
        await _process_contact_message_inner(
            session,
            bot,
            contact,
            contact_number,
            text_body,
            current_token,
            current_phone_id,
        )
    except Exception:
        _rolled_back = True
        if session.is_active:
            await session.rollback()
        raise
    finally:
        if not _rolled_back and session.is_active:
            await session.commit()


async def _handle_suggestion_selection(mctx: MessageContext) -> str | None:
    """Handle when user selects from last_suggestions (number, name fragment, or confirm).
    Returns response string if handled, None to fall through to normal flow."""
    cart, session, bot = mctx.cart, mctx.session, mctx.bot
    text = mctx.text_body.strip()

    if not cart.last_suggestions:
        return None

    # Let the matching logic below (ordinals, digits, name fragments) try first.
    # If nothing matches, we clear stale suggestions at the end before returning.

    # Only intercept reasonably short messages (not full paragraphs)
    if len(text) > 120:
        return None

    # Load suggested products
    sug_res = await session.execute(
        select(Product).where(
            Product.id.in_(cart.last_suggestions),
            Product.is_deleted == False,
            Product.is_available == True,
        )
    )
    sug_map = {p.id: p for p in sug_res.scalars().all()}
    suggestions = [sug_map[sid] for sid in cart.last_suggestions if sid in sug_map]

    if not suggestions:
        cart.last_suggestions = None
        return None

    # --- Ordinal and name-matching helpers ---
    _ORDINALS = {
        "primeiro": 0,
        "primeira": 0,
        "segundo": 1,
        "segunda": 1,
        "terceiro": 2,
        "terceira": 2,
        "quarto": 3,
        "quarta": 3,
        "quinto": 4,
        "quinta": 4,
    }

    def _stem(w: str) -> str:
        if len(w) > 4 and w.endswith("s"):
            return w[:-1]
        return w

    def _match_name_to_suggestion(
        item_name: str,
        exclude_ids: set[int] | None = None,
    ) -> Product | None:
        """Match an item name (from extraction) against suggestions by ordinal or name overlap."""
        name_lower = item_name.lower().strip()
        _exclude = exclude_ids or set()

        # 1. Ordinal match: "primeiro" → suggestion[0]
        for word, idx in _ORDINALS.items():
            if word in name_lower and 0 <= idx < len(suggestions):
                if suggestions[idx].id not in _exclude:
                    return suggestions[idx]

        # 2. Name overlap match (normalize hyphens for "coca cola" vs "Coca-Cola")
        name_norm = name_lower.replace("'", "").replace("\u2019", "").replace("-", " ")
        name_words = {_stem(w) for w in name_norm.split() if len(w) > 3}
        if not name_words:
            return None
        best_match, best_score = None, 0
        for p in suggestions:
            if p.id in _exclude:
                continue
            p_norm = (
                p.name.lower().replace("'", "").replace("\u2019", "").replace("-", " ")
            )
            p_words = {_stem(w) for w in p_norm.split() if len(w) > 3}
            score = len(name_words & p_words)
            if score > best_score:
                best_score = score
                best_match = p
        return best_match if best_score >= 1 else None

    # --- Primary path: robust parsing via extract_items_with_quantities ---
    # Handles ordinals ("noventa e dois do primeiro e treze do segundo")
    # and product-name orders ("quatro picanha com baco doze mignon com cheddar").
    # Falls through to legacy path for simple selections ("2", "o primeiro", "sim").
    qty_pairs = extract_items_with_quantities(text)
    if qty_pairs:
        selections: list[tuple] = []
        seen_ids: set[int] = set()
        unmatched_names: list[str] = []

        for qty, item_name in qty_pairs:
            matched = _match_name_to_suggestion(item_name, exclude_ids=seen_ids)
            if matched:
                selections.append((matched, qty))
                seen_ids.add(matched.id)
            elif qty > 1 and item_name.strip() and len(item_name.strip()) >= 3:
                # Only route to shopping flow if there was an explicit quantity.
                # Default qty=1 pairs (e.g. (1, "veja")) are noise.
                unmatched_names.append(item_name.strip())

        if selections:
            items_to_add = [
                {"product_id": p.id, "quantity": qty, "product_name": p.name}
                for p, qty in selections
            ]
            skipped: list[str] = []
            await crud.add_items_to_db_cart(
                session,
                cart.id,
                items_to_add,
                bot_id=bot.id,
                skipped_items=skipped,
            )
            await _load_cart_items_with_products(cart, session)
            cart.last_suggestions = None
            clear_pending(cart)

            if not cart.items:
                names = ", ".join(f"*{p.name}*" for p, _ in selections)
                return f"{names} não está disponível no momento. 😕 O que mais posso ajudar?"

            cart.state = CartState.SHOPPING
            response = (
                _build_cart_summary_message(cart, bot, "✅")
                + "\n\nAdicionado! Mais alguma coisa ou *só isso* para finalizar? 😊"
            )
            if skipped:
                names = ", ".join(skipped)
                response += f"\n\n⚠️ Indisponível no momento: {names}"

            if unmatched_names:
                return response, unmatched_names
            return response

    # --- Legacy path: simple selections ("2", "o primeiro", "1 e 3", "sim") ---
    # extract_items_with_quantities returned empty — message has no qty+item pairs.
    from app.item_extraction import _QTY_VALUES

    def _fuzzy_qty(word: str) -> int | None:
        val = _QTY_VALUES.get(word)
        if val is not None:
            return val
        if len(word) < 4:
            return None
        from difflib import SequenceMatcher

        best_val, best_ratio = None, 0.0
        for key, v in _QTY_VALUES.items():
            if abs(len(key) - len(word)) > 3:
                continue
            ratio = SequenceMatcher(None, word, key).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_val = v
        return best_val if best_ratio >= 0.85 else None

    def _parse_compound_qty(words: list[str]) -> int | None:
        total = 0
        current = 0
        found_any = False
        for w in words:
            if w == "e":
                continue
            val = _fuzzy_qty(w)
            if val is None:
                continue
            found_any = True
            if val == 1000:
                current = max(current, 1) * 1000
                total += current
                current = 0
            elif val >= 100:
                current += val
            else:
                current += val
        total += current
        return total if found_any else None

    def _parse_qty_from_text(part_text: str) -> int:
        words = part_text.lower().split()
        compound = _parse_compound_qty(words)
        if compound is not None:
            return compound
        qty_digit = re.search(r"\b(\d{1,4})\b", part_text)
        if qty_digit:
            return int(qty_digit.group(1))
        return 1

    def _match_part(part: str) -> tuple | None:
        part_lower = part.lower().strip()
        if not part_lower:
            return None

        # Try ordinal match
        for word, idx in _ORDINALS.items():
            if word in part_lower and 0 <= idx < len(suggestions):
                before = part_lower.split(word)[0]
                return (suggestions[idx], _parse_qty_from_text(before))

        # Try digit-as-index match
        digit_match = re.search(r"\b(\d{1,2})\b", part_lower)
        if digit_match:
            idx = int(digit_match.group(1)) - 1
            if 0 <= idx < len(suggestions):
                # Extract quantity from text before the digit
                before = part_lower[: digit_match.start()]
                qty = _parse_qty_from_text(before) if before.strip() else 1
                return (suggestions[idx], qty)

        # Try name overlap match
        part_norm = part_lower.replace("'", "").replace("\u2019", "")
        part_words = {_stem(w) for w in part_norm.split() if len(w) > 3}
        if part_words:
            matched = _match_name_to_suggestion(part_lower)
            if matched:
                return (matched, _parse_qty_from_text(part_lower))

        return None

    # Merge compound numbers into digits before splitting so "vinte e sete
    # do primeiro" doesn't get split at the "e" between "vinte" and "sete".
    def _merge_compound_numbers(txt: str) -> str:
        words = txt.lower().split()
        result = []
        i = 0
        while i < len(words):
            if _fuzzy_qty(words[i]) is not None:
                j = i
                while j + 1 < len(words):
                    nxt = words[j + 1]
                    if _fuzzy_qty(nxt) is not None:
                        j += 1
                    elif (
                        nxt == "e"
                        and j + 2 < len(words)
                        and _fuzzy_qty(words[j + 2]) is not None
                    ):
                        j += 2
                    else:
                        break
                if j > i:
                    qty_words = words[i : j + 1]
                    val = _parse_compound_qty(qty_words)
                    result.append(str(val) if val else words[i])
                    i = j + 1
                else:
                    result.append(words[i])
                    i += 1
            else:
                result.append(words[i])
                i += 1
        return " ".join(result)

    _merged = _merge_compound_numbers(text)
    parts = re.split(r"\s*(?:\be\b|,|;)\s*", _merged, flags=re.IGNORECASE)
    selections_legacy: list[tuple] = []
    seen_ids_legacy: set[int] = set()

    unmatched_parts: list[str] = []
    for part in parts:
        match = _match_part(part)
        if match and match[0].id not in seen_ids_legacy:
            selections_legacy.append(match)
            seen_ids_legacy.add(match[0].id)
        elif part.strip():
            unmatched_parts.append(part.strip())

    # Fallback: if no parts matched but the whole message matches, use it
    if not selections_legacy:
        whole_match = _match_part(text)
        if whole_match:
            selections_legacy.append(whole_match)
            unmatched_parts = []

    selections = selections_legacy

    if not selections:
        return None

    # Add all selected products to cart
    items_to_add = [
        {"product_id": p.id, "quantity": qty, "product_name": p.name}
        for p, qty in selections
    ]
    skipped: list[str] = []
    await crud.add_items_to_db_cart(
        session,
        cart.id,
        items_to_add,
        bot_id=bot.id,
        skipped_items=skipped,
    )
    await _load_cart_items_with_products(cart, session)
    cart.last_suggestions = None
    clear_pending(cart)

    if not cart.items:
        names = ", ".join(f"*{p.name}*" for p, _ in selections)
        return f"{names} não está disponível no momento. 😕 O que mais posso ajudar?"

    cart.state = CartState.SHOPPING
    response = (
        _build_cart_summary_message(cart, bot, "✅")
        + "\n\nAdicionado! Mais alguma coisa ou *só isso* para finalizar? 😊"
    )
    if skipped:
        names = ", ".join(skipped)
        response += f"\n\n⚠️ Indisponível no momento: {names}"

    # Filter unmatched parts: remove stopwords/noise, keep only food-like terms
    _food_unmatched: list[str] = []
    if unmatched_parts:
        from app.item_extraction import extract_items_local

        for part in unmatched_parts:
            extracted = extract_items_local(part)
            for item in extracted:
                if len(item) > 2 and item != part.strip().lower():
                    _food_unmatched.append(item)
                elif len(item) > 2:
                    _food_unmatched.append(item)

    if _food_unmatched:
        return response, _food_unmatched
    return response


async def _handle_order_cancel(mctx: MessageContext) -> str:
    """F-17: Handle customer-initiated order cancellation via WhatsApp."""
    session, bot, contact = mctx.session, mctx.bot, mctx.contact

    active_order = await crud.get_latest_active_order(session, contact.id, bot.id)
    if not active_order:
        return "Você não tem nenhum pedido em andamento para cancelar. 😊"

    from app.models import OrderStatus

    if active_order.status in (OrderStatus.PREPARING, OrderStatus.READY):
        return (
            "Seu pedido já está sendo preparado e não pode ser cancelado pelo chat. "
            "Entre em contato diretamente com o restaurante. 📞"
        )

    # Check cancellation window
    order_age = utcnow() - active_order.created_at.replace(tzinfo=None)
    window = timedelta(minutes=bot.cancellation_window_minutes)
    if order_age > window:
        return (
            f"O prazo de cancelamento de {bot.cancellation_window_minutes} minutos já passou. "
            "Entre em contato diretamente com o restaurante. 📞"
        )

    # Cancel the order
    active_order.status = OrderStatus.CANCELED
    session.add(active_order)

    # Clear conversation history — fresh context for next interaction
    if mctx.contact:
        await crud.clear_contact_history(session, mctx.contact.id)

    _order_id = active_order.id
    _bot_id = bot.id
    await session.flush()

    try:
        await broadcast_order_update(
            "order_status_changed",
            {"order_id": _order_id, "status": "canceled"},
            bot_id=_bot_id,
        )
    except Exception as e:
        logger.warning("Failed to broadcast cancellation: %s", e)

    return f"Pedido #{_order_id} cancelado com sucesso. ✅"


async def _handle_order_repeat(mctx: MessageContext) -> str:
    """F-01: Handle reorder — add last order's items to cart."""
    session, bot, contact, cart = mctx.session, mctx.bot, mctx.contact, mctx.cart

    last_items = await crud.get_last_completed_order_items(session, contact.id, bot.id)
    if not last_items:
        return "Não encontrei nenhum pedido anterior para repetir. 😕 O que gostaria de pedir?"

    skipped_items: list[str] = []
    await crud.add_items_to_db_cart(
        session, cart.id, last_items, bot_id=bot.id, skipped_items=skipped_items
    )
    await _load_cart_items_with_products(cart, session)

    if not cart.items:
        return "Os itens do seu último pedido não estão mais disponíveis. 😕 O que gostaria de pedir?"

    cart.state = CartState.SHOPPING
    response = (
        _build_cart_summary_message(cart, bot, "🔄")
        + "\n\nPedido anterior adicionado! Quer mais alguma coisa? 😊"
    )
    if skipped_items:
        names = ", ".join(skipped_items)
        response += f"\n\n⚠️ Indisponível no momento: {names}"
    return response


async def _process_contact_message_inner(
    session, bot, contact, contact_number, text_body, current_token, current_phone_id
):
    """Inner handler logic — all DB changes use flush(), commit happens in the caller."""
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
    _CHECKOUT_SKIP_STATES = {
        CartState.AWAITING_CEP,
        CartState.AWAITING_NUMBER_COMPLEMENT,
        CartState.AWAITING_ADDRESS_CONFIRMATION,
        CartState.AWAITING_CUSTOMER_NAME,
        CartState.AWAITING_PAYMENT_METHOD,
        CartState.AWAITING_DELIVERY_METHOD,
    }
    if cart.state in _CHECKOUT_SKIP_STATES and not is_likely_shopping_intent(text_body):
        logger.info(
            "State %s detected, message not a shopping intent, skipping classification",
            cart.state,
        )
        intent = None  # Skip classification — let checkout handler parse the message
    else:
        # Caso contrário, executamos a classificação de intenção normalmente
        await _load_cart_items_with_products(cart, session)
        cart_items_for_intent = [
            {"id": item.product_id, "name": item.product.name} for item in cart.items
        ]
        intent = await resolve_intent(text_body, cart, cart_items_for_intent)
        logger.info("[INTENT] resolved intent: %s", intent)

    # INTERCEPTADOR DE BOAS-VINDAS COM IMAGEM
    if intent == "GREETING_OR_QUESTION" and cart.state == CartState.GREETING:
        await _send_welcome_with_menu(
            session, bot, cart, contact_number, text_body, contact=contact
        )
        logger.info("[GREETING] Cart %s state updated to SHOPPING", cart.id)
        return

    # Handle suggestion selection (number, name, or confirm) BEFORE checkout FSM
    # Skip if the message is clearly a removal request — stale suggestions
    # from a previous failed ADD must not hijack MODIFY/REMOVE messages.
    _is_removal = _REMOVE_KEYWORD_RE.search(text_body)
    if _is_removal and cart.last_suggestions:
        cart.last_suggestions = None
    # Clear suggestions when the message is clearly a multi-item order
    # (e.g., "8 alcatra e 3 coca cola"). Without this, the suggestion
    # handler misinterprets digits as suggestion selections.
    if cart.last_suggestions:
        _order_pairs = extract_items_with_quantities(text_body)
        _real_order_pairs = [(q, n) for q, n in _order_pairs if q > 1]
        if len(_real_order_pairs) >= 2:
            cart.last_suggestions = None
    # Pre-empt suggestion handler when an unambiguous FINISH verb is present
    # AND there is no selection signal (digit/ordinal). The greedy name-overlap
    # matcher in _handle_suggestion_selection cannot distinguish "fecha com
    # mignon" (FINISH) from "vou de mignon" (selection) — the discriminator is
    # the verb. If the message has both a digit/ordinal AND a FINISH verb
    # (e.g. "1 e fecha o pedido"), Option H below chains the FINISH after
    # the selection has been added.
    if cart.last_suggestions and _FINISH_KEYWORD_RE.search(text_body):
        _has_selection_signal = bool(re.search(r"\b\d+\b", text_body)) or any(
            o in text_body.lower()
            for o in ("primeir", "segund", "terceir", "quart", "quint")
        )
        if not _has_selection_signal:
            logger.info(
                "[SUGGESTION] cleared by FINISH keyword (no selection signal): %r",
                text_body[:80],
            )
            cart.last_suggestions = None
            clear_pending(cart)
    # F5 (2026-04-09) — verb-prefix bypass for ADD and QTY_REDUCE patterns.
    #
    # Without this, the suggestion handler's name-overlap matcher fires on
    # explicit shopping commands that happen to mention products sharing
    # 4+ char words with the active suggestion list. Example failure:
    # bot offers [Coca Cola Zero, Guaraná, Sprite] as suggestions, customer
    # says "vou querer um Coca Lata" — handler sees "coca" overlapping
    # with "Coca Cola Zero", treats it as selection of option 1, adds the
    # WRONG product. The trap_unrelated_add scenario fails this way.
    #
    # Part A — ADD verb + qty + product, no selection signal: same safety
    # pattern as the FINISH guard above. _ADD_KEYWORD_RE only matches when
    # the message has the unambiguous "verb + qty word/digit + product
    # letter" shape, which is a clear shopping command. The selection-signal
    # check (digit OR ordinal anywhere in the message) is the safety net
    # for ambiguous cases like "vou querer 1 do primeiro" — let the
    # suggestion handler try those first.
    if cart.last_suggestions and _ADD_KEYWORD_RE.search(text_body):
        _has_selection_signal = bool(re.search(r"\b\d+\b", text_body)) or any(
            o in text_body.lower()
            for o in ("primeir", "segund", "terceir", "quart", "quint")
        )
        if not _has_selection_signal:
            logger.info(
                "[SUGGESTION] cleared by ADD verb (no selection signal): %r",
                text_body[:80],
            )
            cart.last_suggestions = None
            clear_pending(cart)
    # Part B — QTY_REDUCE patterns are NEVER selections. The "deixa só N X"
    # / "fica só N X" / "muda pra N X" shape is unambiguously a quantity
    # update on an existing cart item — bypass unconditionally so F2's
    # pre-router QTY_REDUCE guard can take over and dispatch SET semantics
    # via _programmatic_cart_reduce(set_mode=True).
    if cart.last_suggestions and _QTY_REDUCE_RE.search(text_body):
        logger.info("[SUGGESTION] cleared by QTY_REDUCE pattern: %r", text_body[:80])
        cart.last_suggestions = None
        clear_pending(cart)
    # Part C (F4, 2026-04-09) — CLEAR keywords are NEVER selections.
    # "limpa tudo" / "esvazia o carrinho" / "zera o pedido" / "apaga tudo"
    # is unambiguously a total-clear command — bypass unconditionally so
    # F4's pre-router CLEAR guard can take over and route to CLEAR_CART
    # (which then dispatches `_handle_clear_cart` → `clear_db_cart`).
    # Without this, the suggestion handler intercepts "limpa tudo",
    # extracts (1, 'limpa tudo'), fails to match any product, and falls
    # through to the LLM with no intent hint — clears the cart only ~30%
    # of the time. The trap_clear scenario specifically tests this.
    if cart.last_suggestions and _CLEAR_KEYWORD_RE.search(text_body):
        logger.info("[SUGGESTION] cleared by CLEAR keyword: %r", text_body[:80])
        cart.last_suggestions = None
        clear_pending(cart)
    if cart.state in [CartState.GREETING, CartState.SHOPPING] and cart.last_suggestions:
        sug_result = await _handle_suggestion_selection(mctx)
        if sug_result is not None:
            # sug_result can be a string or (string, unmatched_parts) tuple
            _sug_unmatched: list[str] = []
            if isinstance(sug_result, tuple):
                sug_result, _sug_unmatched = sug_result

            if _sug_unmatched:
                # Check FINISH against the ORIGINAL text_body (not the filtered
                # unmatched parts) — the legacy path's _food_unmatched filter
                # strips "o pedido" from "fecha o pedido", leaving only "fecha"
                # which doesn't match the regex. The original text always
                # preserves the full structure.
                if _FINISH_KEYWORD_RE.search(text_body):
                    # Multi-intent: customer selected suggestion(s) AND wants
                    # to finalize ("1 e fecha o pedido", "primeiro e finaliza").
                    # The selection has already been added to cart by the
                    # handler above. Run FINISH on the same turn so the customer
                    # sees both the cart confirmation AND the next-step prompt
                    # in one response.
                    finish_result = await _handle_finish_order(mctx, "FINISH_ORDER")
                    if finish_result:
                        sug_result = sug_result + "\n\n" + finish_result
                else:
                    # Route unmatched parts through the shopping flow.
                    # Override mctx.text_body so _handle_shopping_intent searches
                    # for the unmatched items (not the full original message).
                    _original_text = mctx.text_body
                    mctx.text_body = " e ".join(_sug_unmatched)
                    shopping_result = await _handle_shopping_intent(mctx, "ADD")
                    mctx.text_body = _original_text  # restore
                    if shopping_result:
                        # Shopping flow returns full cart summary — use it instead
                        sug_result = shopping_result

            cart.last_activity_at = utcnow()
            session.add(cart)
            await session.flush()
            await send_whatsapp_message(
                contact_number,
                sug_result,
                token=current_token,
                phone_id=current_phone_id,
            )
            await crud.add_interaction_to_history(
                session, bot.id, contact_number, text_body, sug_result
            )
            await session.commit()
            return

        # Suggestion selection returned None (no match). If the message
        # has no product references and no other clear intent (show cart,
        # finalize, etc.), the customer is likely indecisive
        # ("nao sei o que decidir", "hmm", "tô em dúvida"). Re-show
        # suggestions to keep the conversation alive.
        _lower = text_body.lower()
        # Don't re-show for messages with clear non-suggestion intent
        # Phrases that signal clear non-suggestion intent.
        # Use phrases (not single words) to avoid false positives
        # like "tem dicas de pedidos?" matching "pedido".
        _PASSTHROUGH_PHRASES = [
            "meu pedido",
            "meus pedidos",
            "meu carrinho",
            "finalizar",
            "fechar pedido",
            "fechar o pedido",
            "pagar",
            "pagamento",
            "pix",
            "endereço",
            "endereco",
            "entrega",
            "retirada",
            "cancelar",
            "limpar",
            "obrigado",
            "valeu",
            "tchau",
            "até logo",
            "tirar",
            "tira ",
            "remover",
            "remove ",
            "qual o total",
            "quanto deu",
            "quanto ficou",
        ]
        _has_passthrough = any(p in _lower for p in _PASSTHROUGH_PHRASES)
        # Also check for product references — if the message has explicit
        # quantities (digits or written numbers > 1), it's an order, not
        # indecision. Let it fall through to the shopping flow.
        import regex as _sug_re

        _has_digit = bool(_sug_re.search(r"\d", text_body))
        _ext_pairs = extract_items_with_quantities(text_body) if not _has_digit else []
        _has_product_ref = _has_digit or any(q > 1 for q, _ in _ext_pairs)
        if not _has_passthrough and not _has_product_ref:
            _sug_ids = cart.last_suggestions
            _sug_prods_result = await session.execute(
                select(Product).where(
                    Product.id.in_(_sug_ids),
                    Product.is_deleted == False,  # noqa: E712
                )
            )
            _sug_prods = list(_sug_prods_result.scalars().all())
            # Preserve original suggestion order
            _id_order = {pid: i for i, pid in enumerate(_sug_ids)}
            _sug_prods.sort(key=lambda p: _id_order.get(p.id, 999))
            if _sug_prods:
                _reshow_msg = _format_product_suggestions_message(
                    _sug_prods,
                    "Sem problemas! Dá uma olhada nas nossas sugestões:",
                )
                cart.last_activity_at = utcnow()
                session.add(cart)
                await session.flush()
                await send_whatsapp_message(
                    contact_number,
                    _reshow_msg,
                    token=current_token,
                    phone_id=current_phone_id,
                )
                await crud.add_interaction_to_history(
                    session, bot.id, contact_number, text_body, _reshow_msg
                )
                await session.commit()
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
        "BACK_TO_SHOPPING",
    ]
    finalizing_states = [
        CartState.AWAITING_CEP,
        CartState.AWAITING_NUMBER_COMPLEMENT,
        CartState.AWAITING_ADDRESS_CONFIRMATION,
        CartState.AWAITING_CUSTOMER_NAME,
    ]

    _MIN_CHECKOUT_BREAK_SCORE = 0.80
    if intent in intents_that_resume_shopping and cart.state in finalizing_states:
        if _last_router_score >= _MIN_CHECKOUT_BREAK_SCORE:
            logger.info(
                "Customer resumed shopping (intent=%s score=%.2f), "
                "resetting state from %s to GREETING",
                intent,
                _last_router_score,
                cart.state,
            )
            cart.state = CartState.GREETING
            await session.flush()
        else:
            logger.info(
                "Ignoring low-confidence shopping intent during checkout: "
                "intent=%s score=%.2f state=%s (threshold=%.2f)",
                intent,
                _last_router_score,
                cart.state,
                _MIN_CHECKOUT_BREAK_SCORE,
            )

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
    # If CONFIRM had nothing to confirm, treat as ADD so Option C can show suggestions
    if intent == "CONFIRM":
        intent = "ADD"

    # F-17: Order cancellation via WhatsApp
    if intent == "ORDER_CANCEL":
        response_to_user = await _handle_order_cancel(mctx)
        cart.last_activity_at = utcnow()
        session.add(cart)
        await session.flush()
        await send_whatsapp_message(
            mctx.contact_number,
            response_to_user,
            token=mctx.token,
            phone_id=mctx.phone_id,
        )
        await crud.add_interaction_to_history(
            session, bot.id, mctx.contact_number, text_body, response_to_user
        )
        await session.commit()
        return

    # F-01: Reorder last order
    if intent == "ORDER_REPEAT":
        response_to_user = await _handle_order_repeat(mctx)
        cart.last_activity_at = utcnow()
        session.add(cart)
        await session.flush()
        await send_whatsapp_message(
            mctx.contact_number,
            response_to_user,
            token=mctx.token,
            phone_id=mctx.phone_id,
        )
        await crud.add_interaction_to_history(
            session, bot.id, mctx.contact_number, text_body, response_to_user
        )
        await session.commit()
        return

    response_to_user = "Não entendi bem. 😅 Pode tentar de outra forma?"

    if intent == "BACK_TO_SHOPPING":
        await _load_cart_items_with_products(cart, session)
        response_to_user = (
            _build_cart_summary_message(cart, bot)
            + "\n\nDe volta ao cardápio! O que mais quer pedir? 😊"
        )
    elif (clear_result := await _handle_clear_cart(mctx, intent)) is not None:
        response_to_user = clear_result
    elif (show_cart_result := await _handle_show_cart(mctx, intent)) is not None:
        response_to_user = show_cart_result
    elif intent == "FINISH_ORDER":
        finish_result = await _handle_finish_order(mctx, intent)
        if finish_result is not None:
            response_to_user = finish_result
            # _handle_finish_order does its own early returns for empty cart / min order;
            # for the rest, we fall through to the final send+commit block below.
    elif intent == "GREETING_OR_QUESTION" and cart.state != CartState.GREETING:
        # Conversational message while shopping — answer without cart tools.
        # Simple LLM call with no tools to avoid accidental cart mutations.
        _conv_prompt = [
            {
                "role": "system",
                "content": (
                    f"Você é o atendente virtual do {bot.restaurant_name}. "
                    "Responda de forma breve e simpática. "
                    "NÃO adicione itens ao carrinho. "
                    "NÃO liste o cardápio. "
                    "Se o cliente quiser pedir algo, "
                    "pergunte o que ele gostaria. "
                    'Responda em json: {{"response_to_user": "..."}}'
                ),
            },
            {"role": "user", "content": text_body},
        ]
        _conv_text = await get_chat_response_gpt(_conv_prompt)
        if _conv_text:
            import json as _json

            try:
                _parsed = _json.loads(_conv_text)
                response_to_user = _parsed.get("response_to_user", _conv_text)
            except _json.JSONDecodeError:
                response_to_user = _conv_text
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
    await session.flush()
    logger.info("Message processed and response sent")


def _classify_error_message(exc: Exception) -> str:
    """Return a user-facing error message based on the exception type."""
    exc_str = str(exc).lower()
    if isinstance(
        exc, (httpx.TimeoutException, httpx.ConnectError, ConnectionError, TimeoutError)
    ):
        return "Estamos com lentidão na conexão. Tente novamente em instantes."
    if "database" in exc_str or "sqlalchemy" in type(exc).__name__.lower():
        return "Sistema temporariamente fora do ar. Tente novamente em instantes."
    if "openai" in type(exc).__name__.lower() or "rate_limit" in exc_str:
        return "Não consegui processar sua mensagem. Tente enviá-la novamente."
    if "mercadopago" in exc_str or "payment" in exc_str or "pix" in exc_str:
        return (
            "Problema no sistema de pagamento. Tente novamente ou escolha outra forma."
        )
    return "Algo deu errado. Tente novamente em instantes."


async def process_whatsapp_message(ctx, data: Dict[str, Any]):
    """ARQ task entry for WhatsApp messages.

    Phase 1.3 (plan/in_browser_bots.md): the WhatsApp-envelope parsing is
    extracted into `_parse_whatsapp_payload`, which returns a channel-neutral
    `ParsedIngress`. The rest of this function consumes that result. Phase 2
    will add a sibling `process_chat_message` ARQ task that builds a
    `ParsedIngress` from a `/chat` POST body and runs through the same
    downstream pipeline.
    """
    new_trace_id()
    contact_number: str | None = None
    current_token: str | None = None
    current_phone_id: str | None = None
    async with async_session() as session:
        try:
            # 1. Parse the WhatsApp webhook envelope. Returns None for
            #    non-message events (status updates) — we just drop those.
            parsed = _parse_whatsapp_payload(data)
            if parsed is None:
                return

            # Unpack into the local variable names the rest of this body
            # already uses, so the 250-line downstream block stays untouched.
            contact_number = parsed.contact_identity
            message_id = parsed.message_id
            text_body = parsed.text_body
            msg_type = parsed.msg_type
            _audio_media_id = parsed.audio_media_id
            incoming_phone_id = parsed.incoming_phone_id
            bot_display_phone = parsed.bot_display_phone

            # Gate: Rate limiting (scoped by bot phone_id)
            if await _check_rate_limit(contact_number, phone_id=incoming_phone_id):
                return
            logger.info(
                "Message received (%s) from %s to phone_id %s",
                msg_type,
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

            # --- Audio transcription (after bot is found, need token for media download) ---
            if _audio_media_id:
                from app.openai_client import transcribe_audio

                try:
                    _audio_bytes = await _download_whatsapp_media(
                        _audio_media_id, current_token
                    )
                    _whisper_prompt = await _build_whisper_prompt(session, bot)
                    text_body = await transcribe_audio(
                        _audio_bytes, prompt=_whisper_prompt, bot_id=bot.id
                    )
                except Exception as e:
                    logger.error("Audio transcription failed: %s", e)
                    text_body = ""

                if not text_body or len(text_body.strip()) < 2:
                    await send_whatsapp_message(
                        to=contact_number,
                        message=(
                            "Não consegui entender o áudio. 😕 "
                            "Pode tentar enviar novamente ou digitar o pedido?"
                        ),
                        token=current_token,
                        phone_id=current_phone_id,
                    )
                    return

                logger.info(
                    "[AUDIO] Transcribed: %s",
                    text_body[:100],
                )

            # Gate: Deduplication
            if await _handle_dedup(
                session, message_id, current_token, current_phone_id
            ):
                return

            # Gate: Store closed
            if await _handle_store_closed(session, bot, contact_number, text_body):
                return

            # Gate: No products configured
            if await _check_bot_has_products(session, bot, contact_number):
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
                    message="Ainda estou processando sua mensagem anterior. Um momento!",
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


def _programmatic_cart_reduce(
    qty_pairs: list[tuple[int, str]],
    cart_items: list,
    text_body: str,
    full_removal: bool = False,
    set_mode: bool = False,
) -> tuple[list[tuple], list[str]] | None:
    """Programmatically reduce cart quantities for 'tire/tira N X' patterns.

    When full_removal=True (no explicit quantity in a remove message),
    "tira o X" removes ALL of X instead of just 1.

    F2 (2026-04-09): when set_mode=True, the function uses SET semantics
    instead of SUBTRACT — for "deixa só N X" / "fica só N X" / "muda pra N X"
    patterns where N is the desired FINAL quantity (not the amount to
    subtract). Caller is responsible for distinguishing the two modes
    via the pre-router QTY_REDUCE guard.

    Returns list of (cart_item, new_quantity) tuples and response parts,
    or None if no matches found.
    """
    import re as _re
    from difflib import SequenceMatcher

    if not qty_pairs or not cart_items:
        return None

    updates: dict[int, tuple] = {}  # cart_item.id → (cart_item, new_qty)
    response_parts: list[str] = []

    # ── Remove verb words that leak into extracted item names ──
    _REMOVE_VERBS = {
        "tira",
        "tire",
        "tiro",
        "tirar",
        "remove",
        "remover",
        "retira",
        "retirar",
        "retiro",
        "sem",
        "cancela",
        "cancelar",
        "esquece",
        "esquecer",
        "deixa",
        "fora",
    }

    def _extract_size_token(s: str) -> str | None:
        """Extract numeric size token like '300ml', '500g', '1kg', '2l'."""
        m = _re.search(r"(\d+\s*(?:ml|l|g|kg|un|pç|pecas|peças))\b", s.lower())
        return m.group(1).replace(" ", "") if m else None

    def _norm(s: str) -> str:
        """Normalize: lowercase, hyphens to spaces, strip punctuation.
        Preserves digits attached to units (300ml) for size matching."""
        s = s.lower().replace("-", " ")
        s = _re.sub(r"[^\w\sáàâãéèêíìîóòôõúùûç]", " ", s)  # strip punctuation
        return s.strip()

    def _norm_words(s: str) -> str:
        """Normalize for word overlap: strips digits entirely."""
        s = _norm(s)
        s = _re.sub(r"\d+\w*", "", s)
        return s.strip()

    def _word_overlap_score(req: str, product: str) -> float:
        """Score match by counting shared significant words (len > 2).
        Returns (overlap_count, seq_ratio) for tie-breaking."""
        req_words = {w for w in req.split() if len(w) > 2}
        prod_words = {w for w in product.split() if len(w) > 2}
        if not req_words:
            return 0.0
        overlap = len(req_words & prod_words)
        return overlap + SequenceMatcher(None, req, product).ratio() * 0.1

    for req_qty, req_name in qty_pairs:
        # Strip remove verbs from item name
        req_clean = " ".join(
            w for w in req_name.lower().split() if w not in _REMOVE_VERBS
        )
        req_lower = _norm_words(req_clean)
        req_size = _extract_size_token(req_name)

        best_item = None
        best_score = 0.0

        for ci in cart_items:
            prod_size = _extract_size_token(ci.product.name)
            # Hard reject: user specified a size that doesn't match
            if req_size and prod_size and req_size != prod_size:
                continue

            pname = _norm_words(ci.product.name)
            score = _word_overlap_score(req_lower, pname)

            # Boost score when size tokens match exactly
            if req_size and prod_size and req_size == prod_size:
                score += 0.5

            if score > best_score:
                best_score = score
                best_item = ci

        if best_item and best_score >= 1.0:
            if full_removal:
                # "tira o X" without explicit qty → remove all
                new_qty = 0
            elif set_mode:
                # F2: "deixa só N X" — N is the final qty, not a delta.
                new_qty = max(0, req_qty)
            elif best_item.id in updates:
                _, prev_qty = updates[best_item.id]
                new_qty = max(0, prev_qty - req_qty)
            else:
                new_qty = max(0, best_item.quantity - req_qty)
            updates[best_item.id] = (best_item, new_qty)
            if new_qty == 0:
                response_parts.append(f"Removido {best_item.product.name} do carrinho.")
            else:
                response_parts.append(
                    f"{best_item.product.name}: {best_item.quantity} \u2192 {new_qty}"
                )
            logger.info(
                "[REDUCE] %s: %d %s %s = %d (score=%.2f, full_removal=%s, set_mode=%s)",
                best_item.product.name,
                best_item.quantity,
                "→" if set_mode else "-",
                "ALL" if full_removal else str(req_qty),
                new_qty,
                best_score,
                full_removal,
                set_mode,
            )

    if not updates:
        return None

    return list(updates.values()), response_parts


def _build_cart_summary_message(cart: ShoppingCart, bot: Bot, emoji: str = "🛒") -> str:
    if not cart.items:
        return "🛒 *Seu carrinho está vazio.*"

    cart_summary_lines = []
    subtotal = 0.0
    for item in cart.items:
        line_total = item.product.price * item.quantity
        subtotal += line_total

        # Formata a linha do item
        item_line = f"• {item.quantity}x {item.product.name} — R$ {line_total:.2f}"

        # Se tiver observação, adiciona na linha de baixo
        if item.notes:
            item_line += f"\n  _↳ {item.notes}_"

        # Adiciona à lista APENAS UMA VEZ
        cart_summary_lines.append(item_line)

    summary_text = f"{emoji} *Seu pedido:*\n" + "\n".join(cart_summary_lines)

    total_amount = subtotal

    # Verifica se o método é entrega E se a taxa é maior que zero
    if cart.delivery_method == DeliveryMethod.DELIVERY and bot.delivery_fee > 0:
        total_amount += bot.delivery_fee
        summary_text += f"\n\n🛵 Entrega: R$ {bot.delivery_fee:.2f}"

    summary_text += f"\n\n*Total: R$ {total_amount:.2f}*"
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

    message = await _apply_free_tier_branding(message or "", phone_id)

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
    logger.info(
        "Sending WhatsApp message to=%s phone_id=%s media=%s",
        to,
        phone_id,
        bool(media_url),
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(url, headers=headers, json=data)
            response.raise_for_status()
            _elapsed = (_time.perf_counter_ns() - _start) // 1_000_000
            logger.info(
                "WhatsApp send OK: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            # WhatsApp Cloud API bills per CONVERSATION (24h window), not per
            # message. Service conversations (customer messages first, business
            # replies within 24h) are FREE since Meta's Nov 2024 pricing change.
            # ZenBots' entire flow is service-conversation based — every chat
            # starts with the customer typing "oi" — so the realistic cost per
            # send_message call is $0.00. The previous $0.05 hardcoded constant
            # was an outdated overestimate.
            #
            # When we start sending paid templates (Marketing/Utility/Auth)
            # for order-status updates, OTPs, or marketing blasts, we should
            # add a separate record_api_usage call with the appropriate
            # operation name and per-template cost (Brazil rates: utility
            # ~$0.008, auth ~$0.0315, marketing ~$0.0625).
            await record_api_usage(
                None, "whatsapp", "send_message", cost_usd=0.0, duration_ms=_elapsed
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
                cart.id,
                cart.pending_action_args,
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
            select(Product.id).where(
                Product.bot_id == bot_id,
                Product.id.in_(item_ids),
                Product.is_deleted == False,
            )
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

        skipped_items: list[str] = []
        await crud.add_items_to_db_cart(
            session, cart.id, valid_items, bot_id=bot_id, skipped_items=skipped_items
        )
        await _load_cart_items_with_products(cart, session)
        clear_pending(cart)
        skipped_msg = ""
        if skipped_items:
            names = ", ".join(skipped_items)
            skipped_msg = f"\n\n⚠️ Indisponível no momento: {names}"
        return (
            _build_cart_summary_message(cart, bot, "✅")
            + "\n\nAlgo mais?"
            + skipped_msg
        )

    if tool == "modify_item_quantity":
        pid = _safe_int(args.get("product_id"), "product_id")
        newq = _safe_int(args.get("new_quantity"), "new_quantity")
        if pid is None or newq is None:
            clear_pending(cart)
            return "A proposta para modificar o item estava incompleta. Pode repetir?"

        await crud.modify_item_quantity_in_db_cart(session, cart.id, pid, newq)
        await _load_cart_items_with_products(cart, session)
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
        await _load_cart_items_with_products(cart, session)
        clear_pending(cart)
        # --- CORREÇÃO APLICADA ---
        return _build_cart_summary_message(cart, bot, "🔄") + "\n\nAlgo mais?"

    if tool == "answer_with_found_products":
        names = args.get("product_names", [])
        if names:
            res = await session.execute(
                select(Product).where(
                    Product.bot_id == bot_id,
                    Product.name.in_(names),
                    Product.is_deleted == False,
                    Product.is_available == True,
                )
            )
            prods = res.scalars().all()
            if prods:
                cart.last_suggestions = [p.id for p in prods]
                text = _format_product_suggestions_message(
                    prods, "Encontrei essas opções para você:"
                )
                # If only 1 suggestion, set pending action so "pode ser" adds it
                if len(prods) == 1:
                    save_pending(
                        cart,
                        "add_items_to_cart",
                        {
                            "items": [
                                {
                                    "product_id": prods[0].id,
                                    "quantity": 1,
                                    "product_name": prods[0].name,
                                }
                            ]
                        },
                        f"Sugestão: {prods[0].name}",
                    )
            else:
                clear_pending(cart)
                text = "Posso sugerir algumas opções, se quiser."
        else:
            clear_pending(cart)
            text = "Posso sugerir algumas opções, se quiser."
        return text

    clear_pending(cart)
    return "A proposta não pôde ser executada. Pode me dizer de novo o que deseja?"


async def _product_ids_for_bot(session: AsyncSession, bot_id: int) -> set[int]:
    res = await session.execute(
        select(Product.id).where(Product.bot_id == bot_id, Product.is_deleted == False)
    )
    return set(res.scalars().all())


async def _resolve_items_for_proposal(
    session: AsyncSession, bot_id: int, items_arg: list[dict] | None
) -> list[dict]:
    if not items_arg:
        return []
    valid_ids, resolved = await _product_ids_for_bot(session, bot_id), []
    for it in items_arg:
        if not isinstance(it, dict):
            continue
        q = int(it.get("quantity", 0) or 0)
        if q <= 0:
            continue
        pid = it.get("product_id")
        if pid is not None:
            pid = _safe_int(pid, "product_id")
            if pid is None:
                continue
            if pid not in valid_ids:
                continue
            resolved.append({"product_id": pid, "quantity": q})
            continue
        pname = (it.get("product_name") or it.get("name") or "").strip()
        if not pname:
            continue
        found = await session.execute(
            select(Product)
            .where(
                Product.bot_id == bot_id,
                Product.is_deleted == False,
                Product.name.ilike(f"%{pname}%"),
            )
            .limit(1)
        )
        p = found.scalars().first()
        if not p:
            continue
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


_CHECKOUT_STATES = {
    CartState.AWAITING_DELIVERY_METHOD,
    CartState.AWAITING_CEP,
    CartState.AWAITING_NUMBER_COMPLEMENT,
    CartState.AWAITING_ADDRESS_CONFIRMATION,
    CartState.AWAITING_CUSTOMER_NAME,
    CartState.AWAITING_PAYMENT_METHOD,
}

# Intents that should be reinterpreted during checkout
# "cancelar" / "CLEAR_CART" during checkout → return to SHOPPING, not wipe cart
_CHECKOUT_INTENT_OVERRIDES = {
    "CLEAR_CART": "BACK_TO_SHOPPING",
}


# Pre-router pattern: "quero/manda/coloca/... [qty] [product name]"
# Detects obvious ADD messages that the semantic router may misclassify
# when the message contains specific product names (long messages diverge
# from short prototypes, causing false REQUEST_SUGGESTION matches).
# Written quantity words for pre-router ADD guard.
# Includes standard Portuguese, common typos/Spanishisms (cuatro, sinco, etc.).
_ADD_QTY_WORDS = (
    "um|uma|uns|umas|dois|duas|três|tres|quatro|cuatro|cinco|sinco|seis|ceis|sete|oito|"
    "nove|dez|onze|doze|treze|quatorze|catorze|quinze|dezesseis|dezessete|dezoito|"
    "dezenove|vinte|trinta|quarenta|cuarenta|cinquenta|sinquenta|sessenta|setenta|"
    "oitenta|noventa|cem|cento|duzentos|dusentos|duzentas|trezentos|tresentos|trezentas|"
    "quatrocentos|quinhentos|seiscentos|setecentos|oitocentos|novecentos|mil"
)
_REMOVE_KEYWORD_RE = re.compile(
    r"(?:(?:pode|por\s+favor|favor|quero|preciso|d[aá]\s+pra|tem\s+como)\s+)?"
    r"(?:tire|tira|tirar|retira|retire|retirar|remov[ea]|remove|remover)"
    r"\s+(?:"
    rf"(?:{_ADD_QTY_WORDS}|\d+)\s*\(?[a-záàâãéèêíìîóòôõúùûç]"
    r"|"
    r"(?:o|a|os|as|todo|toda|todos|todas)\s+[a-záàâãéèêíìîóòôõúùûç]"
    r"(?!\w*\s+(?:do|da|dos|das|no|na|nos|nas)\s)"
    r")",
    re.IGNORECASE,
)
# Extended remove patterns:
#  - "sem o X", "cancela o X", "esquece(r) o X", "deixa sem o X", "tira fora o X"
#  - "deixa o X pra la" / "para lá" (set-aside form)
# The first alternative is the original verb-style; the second covers the
# slang Tier 2 phrase "deixa o X pra la".
_REMOVE_ALT_RE = re.compile(
    r"(?:"
    r"(?:sem|cancela|esquece(?:r)?|deixa\s+sem|tira\s+fora)"
    r"\s+(?:o|a|os|as)\s+[a-záàâãéèêíìîóòôõúùûç]"
    r"|"
    r"deixa\s+(?:o|a|os|as)\s+[a-záàâãéèêíìîóòôõúùûç][^\n]*?\s+(?:pra|para)\s+l[aá]"
    r")",
    re.IGNORECASE,
)
# Negation-style REMOVE patterns: "não quero (mais) X", "não precisa do X",
# "não manda o X", "eu não quero X". The semantic router classifies these
# as NEGATE or low-confidence ADD; without this guard they're treated as
# new orders. Pattern: optional "eu" + "não" + (quero|precisa|manda) +
# optional article + product token. "esquece o X" stays in _REMOVE_ALT_RE.
#
# A negative lookahead excludes Portuguese function words at the product
# position. Without it, "não quero mais" would match because the regex
# backtracks and treats "mais" as the product name. The product token must
# be an actual content word, not a quantifier/pronoun/intensifier.
_REMOVE_NEGATION_FUNC_WORDS = (
    "mais|nada|isso|aquilo|disso|dessa|disto|nisso|naquilo|"
    "nenhum|nenhuma|nenhuns|nenhumas|muito|muita|muitos|muitas|"
    "pouco|pouca|tudo|nem|agora|nunca|jamais|sso|isto"
)
_REMOVE_NEGATION_RE = re.compile(
    r"\b(?:eu\s+)?n[aã]o\s+"
    r"(?:quero(?:\s+mais)?|precisa(?:\s+do)?|preciso(?:\s+do)?|manda(?:\s+o)?)"
    r"\s+(?:o|a|os|as|do|da|dos|das)?\s*"
    rf"(?!(?:{_REMOVE_NEGATION_FUNC_WORDS})\b)"
    r"[a-záàâãéèêíìîóòôõúùûç]\w{2,}",
    re.IGNORECASE,
)
_ADD_KEYWORD_RE = re.compile(
    r"(?:quero(?:\s+pedir)?|manda(?:\s+ver)?|coloca|bota|adiciona|me\s+v[eê]|vou\s+querer|pode\s+mandar)"
    rf"(?:\s+\w{{1,5}}){{0,2}}\s+(?:{_ADD_QTY_WORDS}|\d+)\s*\(?[a-záàâãéèêíìîóòôõúùûç]",
    re.IGNORECASE,
)
# Pre-router FINISH pattern: explicit "finalizar / fechar pedido / fechar a conta"
# keywords are unambiguous and should bypass the semantic router. The router
# misclassifies slang phrases like "vamo finalizar, pode mandar aí" as ADD
# (because "pode mandar" is an ADD prototype) even though "finalizar" is the
# real intent. Matches at word boundaries to avoid false positives.
#
# Day 1.5 finding F1b (2026-04-08): the regex previously required "fechar"
# to be followed by "pedido" / "a conta". Real Brazilian customers very
# commonly say "vamo fechar", "pode fechar", "vou fechar", "bora fechar"
# without the noun. We now match those verb-prefixed bare-fechar forms too.
# False-positive risk ("fechar com X" meaning "include X") is small in our
# corpus and the bot recovers gracefully — the customer can just say "ainda
# quero adicionar X" and we route back through ADD.
_FINISH_KEYWORD_RE = re.compile(
    r"\b("
    r"finaliz\w*"
    r"|fecha(?:r|ndo)?\s+(?:o\s+|meu\s+|esse\s+|este\s+)?pedido"
    r"|fecho\s+(?:o\s+|meu\s+)?pedido"
    r"|fecha(?:r)?\s+a\s+conta"
    r"|encerra(?:r)?\s+(?:o\s+)?pedido"
    # F1b: verb-prefixed bare fechar
    r"|(?:vamo[s]?|bora|vou|pode(?:\s+j[aá])?)\s+fecha(?:r)?\b"
    r")\b",
    re.IGNORECASE,
)

# Pre-router CONTINUATION pattern: messages starting with a continuation
# marker ("e", "mais", "também", "tb", "ah e") followed by a quantity and a
# product token. This is the most common production pattern (S8) — the
# customer adds an item, then sends a verb-less follow-up like "e uma coca".
# The semantic router can't recognize these as ADD without the verb, and the
# existing _ADD_KEYWORD_RE requires explicit verbs (quero/manda/etc.).
# A relative-quantity-only message like "mais uma" without a product name is
# NOT matched here — that's a different scenario (P2.4).
_CONTINUATION_KEYWORD_RE = re.compile(
    r"^\s*"
    r"(?:ah\s+)?"
    r"(?:e|mais|tamb[eé]m|tb)\s+"
    rf"(?:mais\s+|tamb[eé]m\s+)?(?:{_ADD_QTY_WORDS}|\d+)\s+"
    r"[\w-]",
    re.IGNORECASE,
)

# Pre-router BARE-ADD pattern: messages that ARE the order, with no shopping
# verb. e.g. "1 cookies", "tipo um cheeseburger", "três coxinhas". Brazilian
# customers in a hurry skip the verb and just type the quantity + product.
# Today these fall to the semantic router which often misclassifies them as
# REQUEST_SUGGESTION (the embedding lands near "o que tem" prototypes).
# A negative lookahead excludes time/currency/filler words at the product
# position so "2 horas" / "3 reais" / "1 momento" don't false-positive.
_BARE_ADD_FILLER_WORDS = (
    "horas?|minutos?|segundos?|dias?|reais?|momentos?|vezes?|anos?|"
    "semanas?|meses?|gente|favor"
)
_BARE_ADD_RE = re.compile(
    r"^\s*"
    r"(?:tipo\s+)?"
    rf"(?:{_ADD_QTY_WORDS}|\d+)\s+"
    rf"(?!(?:{_BARE_ADD_FILLER_WORDS})\b)"
    r"\w{3,}",
    re.IGNORECASE,
)

# F2 (2026-04-09) — Quantity-set ("deixa só N X") pre-router pattern.
#
# Catches messages like "deixa só 1 latte", "fica só 2 coca", "muda pra 3
# pizza", "põe só uma água" — where the customer wants to SET an item's
# quantity (not add to or subtract from it). Without this guard, the
# semantic router classifies it as ADD (because "1 latte" embeds toward
# ADD prototypes), the LLM calls add_items_to_cart with qty=1, the cart-
# merge logic adds 1 to the existing qty, and the customer ends up with
# qty+1 instead of just N. Reproduced live before shipping: cart had
# Latte×3 + Café Preto×2; "deixa só 1 latte" → bot replied "✅ 4x Latte"
# (added 1 to existing 3 instead of setting to 1).
#
# Routes to MODIFY intent. The static system prompt + a new few-shot
# example teach the LLM to use modify_item_quantity with new_quantity=N
# (NOT add_items_to_cart). The pre-router guard ensures the LLM gets
# MODIFY intent — without it the LLM might still pick the wrong tool
# even with examples.
#
# Negative lookahead excludes follow-up words that would create false
# positives ("deixa só pensar", "deixa só pra hoje" — both lack a real
# qty word and product name in the right position).
_QTY_REDUCE_RE = re.compile(
    r"\b(?:deixa|fica|muda|coloca|p[oõ]e)\s+"
    r"(?:s[oó]|apenas|pra|para)\s+"
    rf"(?:{_ADD_QTY_WORDS}|\d+)\s+"
    r"\w{3,}",
    re.IGNORECASE,
)

# F4 (2026-04-09) — Clear-cart pre-router pattern.
#
# Catches "limpa tudo", "esvazia o carrinho", "zera o pedido", "apaga
# tudo" — total cart-clear commands. Without this guard, "limpa tudo"
# falls through every pre-router branch, gets intercepted by the
# suggestion handler (which extracts "[(1, 'limpa tudo')]" and tries
# to match it against suggestion products → no match), then lands at
# the LLM with no intent hint. The LLM clears the cart only ~20-40%
# of the time.
#
# Routes to CLEAR_CART. The downstream `_handle_clear_cart` already
# wires this intent to `clear_db_cart` and the suggestion-handler
# bypass below ensures the message reaches resolve_intent.
#
# Verbs: limpa(r|e), esvazia(r|e), zera(r), apaga(r|e). Objects:
# tudo / td / (o|meu|todo) (carrinho|pedido). False-positive risk on
# "limpa o copo" / "limpa minha mesa" is zero — those don't match
# the carrinho|pedido|tudo object alternation.
_CLEAR_KEYWORD_RE = re.compile(
    r"\b(?:"
    r"limp[ae](?:r|ndo)?"
    r"|esvazi[ae](?:r|ndo)?"
    r"|zer[ae](?:r|ndo)?"
    r"|apag[ae](?:r|ndo)?"
    r")\s+"
    r"(?:tudo|td|(?:o\s+|meu\s+|todo\s+(?:o\s+)?)?(?:carrinho|pedido))\b",
    re.IGNORECASE,
)


# Score from the last resolve_intent call. Used by the checkout guard
# to block low-confidence shopping intents from breaking checkout flow.
_last_router_score: float = 0.0


async def resolve_intent(text_body, cart, cart_items_for_intent, found_products=None):
    global _last_router_score
    _last_router_score = 0.0

    # 0. Cart-state-aware override: during checkout, some intents are reinterpreted
    cart_state = getattr(cart, "state", None)
    in_checkout = cart_state in _CHECKOUT_STATES

    # 0a. Pre-router QTY-REDUCE guard (F2, 2026-04-09).
    # Catches "deixa só N X" / "fica só N X" / "muda pra N X" — quantity-SET
    # patterns where the customer wants the item's qty to BECOME N, not add
    # N to the existing total. Routes to MODIFY so the LLM picks
    # modify_item_quantity (with new_quantity=N) instead of add_items_to_cart
    # (which would merge with the existing qty and overshoot).
    # Placed BEFORE the REMOVE block so the precedence is explicit, even
    # though the patterns don't actually overlap (_REMOVE_ALT_RE matches
    # "deixa o X pra la" / "deixa sem o X" — completely different shape).
    if _QTY_REDUCE_RE.search(text_body):
        logger.info("[INTENT] pre-router QTY-REDUCE guard matched: %r", text_body[:80])
        _last_router_score = 1.0
        final_intent = "MODIFY"
        if in_checkout and final_intent in _CHECKOUT_INTENT_OVERRIDES:
            final_intent = _CHECKOUT_INTENT_OVERRIDES[final_intent]
        return final_intent

    # 0a2. Pre-router CLEAR guard (F4, 2026-04-09).
    # Catches "limpa tudo" / "esvazia o carrinho" / "zera o pedido" /
    # "apaga tudo" — total cart-clear commands. The semantic router
    # already lists CLEAR_CART prototypes but only fires above 0.83 sim,
    # which "limpa tudo" doesn't always clear. Pre-router gives 100% recall
    # on the pattern. CLEAR_CART is in _CHECKOUT_INTENT_OVERRIDES so during
    # checkout it routes to BACK_TO_SHOPPING (existing behavior preserved).
    if _CLEAR_KEYWORD_RE.search(text_body):
        logger.info("[INTENT] pre-router CLEAR guard matched: %r", text_body[:80])
        _last_router_score = 1.0
        final_intent = "CLEAR_CART"
        if in_checkout and final_intent in _CHECKOUT_INTENT_OVERRIDES:
            final_intent = _CHECKOUT_INTENT_OVERRIDES[final_intent]
        return final_intent

    # 0b. Pre-router guard: obvious REMOVE/MODIFY patterns bypass the semantic router.
    # "tire/tira/retira/remove" + qty + product is unambiguously a cart reduction,
    # but the router misclassifies it as ADD because product names shift the embedding.
    # _REMOVE_KEYWORD_RE: explicit verbs (tira/retira/remov/remove)
    # _REMOVE_ALT_RE: alternative phrases (sem/cancela/esquece/deixa sem)
    # _REMOVE_NEGATION_RE: negation patterns (não quero/precisa/manda o X)
    if (
        _REMOVE_KEYWORD_RE.search(text_body)
        or _REMOVE_ALT_RE.search(text_body)
        or _REMOVE_NEGATION_RE.search(text_body)
    ):
        logger.info("[INTENT] pre-router REMOVE guard matched: %r", text_body[:80])
        _last_router_score = 1.0
        final_intent = "MODIFY"
        if in_checkout and final_intent in _CHECKOUT_INTENT_OVERRIDES:
            final_intent = _CHECKOUT_INTENT_OVERRIDES[final_intent]
        return final_intent

    # 0b2. Pre-router guard: explicit FINISH_ORDER keywords ("finalizar",
    # "fechar pedido", etc.) are unambiguous and should bypass the router.
    # Without this guard, slang like "vamo finalizar, pode mandar aí que eu
    # to com fome" gets misclassified as ADD because "pode mandar" is an ADD
    # prototype. Placed before the ADD guard so "finalizar" wins over "pode mandar".
    if _FINISH_KEYWORD_RE.search(text_body):
        logger.info("[INTENT] pre-router FINISH guard matched: %r", text_body[:80])
        _last_router_score = 1.0
        final_intent = "FINISH_ORDER"
        # FINISH_ORDER is not in _CHECKOUT_INTENT_OVERRIDES — during checkout
        # the customer reaffirming "finalizar" should still drive the flow forward.
        return final_intent

    # 0b3. Pre-router CONTINUATION guard. Verb-less follow-up messages like
    # "e uma coca", "mais um cheeseburger", "ah e dois pasteis" are the most
    # common production pattern (S8). The semantic router doesn't classify
    # them as ADD without a verb, and _ADD_KEYWORD_RE requires "quero/manda".
    # The continuation regex requires a marker + quantity + product token.
    if _CONTINUATION_KEYWORD_RE.search(text_body):
        logger.info(
            "[INTENT] pre-router CONTINUATION guard matched: %r", text_body[:80]
        )
        _last_router_score = 1.0
        final_intent = "ADD"
        if in_checkout and final_intent in _CHECKOUT_INTENT_OVERRIDES:
            final_intent = _CHECKOUT_INTENT_OVERRIDES[final_intent]
        return final_intent

    # 0c. Pre-router guard: obvious ADD patterns bypass the semantic router.
    # Messages like "quero um(a) Coca-cola e um(a) X" are unambiguously ADD,
    # but the router may misclassify them because product names in long messages
    # shift the embedding away from short ADD prototypes.
    if _ADD_KEYWORD_RE.search(text_body):
        logger.info("[INTENT] pre-router ADD guard matched: %r", text_body[:80])
        _last_router_score = 1.0
        final_intent = "ADD"
        if in_checkout and final_intent in _CHECKOUT_INTENT_OVERRIDES:
            final_intent = _CHECKOUT_INTENT_OVERRIDES[final_intent]
        return final_intent

    # 0c2. Pre-router BARE-ADD guard. Catches verb-less qty+product messages
    # like "1 cookies", "tipo um cheeseburger", "três coxinhas". These are
    # real Brazilian patterns (the customer is in a hurry / it's a Friday
    # night) but they fall through both the ADD guard (no verb) and the
    # CONTINUATION guard (no marker). The semantic router misclassifies them
    # as REQUEST_SUGGESTION because the embedding lands near "o que tem"
    # prototypes. Worst-case false positive ("2 horas") routes to ADD where
    # the LLM gracefully says "não encontrei isso no cardápio".
    if _BARE_ADD_RE.search(text_body):
        logger.info("[INTENT] pre-router BARE-ADD guard matched: %r", text_body[:80])
        _last_router_score = 1.0
        final_intent = "ADD"
        if in_checkout and final_intent in _CHECKOUT_INTENT_OVERRIDES:
            final_intent = _CHECKOUT_INTENT_OVERRIDES[final_intent]
        return final_intent

    # 0d. Pre-router guard: "dicas/sugestões de pedido(s)" is REQUEST_SUGGESTION,
    # not ORDER_REPEAT. The word "pedidos" shifts the embedding toward ORDER_REPEAT
    # but the customer is asking for recommendations, not repeating a previous order.
    _lower_body = text_body.lower()
    _SUGGESTION_PHRASES = [
        "dicas de pedido",
        "sugestões de pedido",
        "sugestão de pedido",
        "dica de pedido",
        "me manda sugest",
        "manda sugest",
        "tem sugest",
        "quero sugest",
    ]
    if any(p in _lower_body for p in _SUGGESTION_PHRASES):
        logger.info("[INTENT] pre-router SUGGESTION guard matched: %r", text_body[:80])
        _last_router_score = 1.0
        final_intent = "REQUEST_SUGGESTION"
        if in_checkout and final_intent in _CHECKOUT_INTENT_OVERRIDES:
            final_intent = _CHECKOUT_INTENT_OVERRIDES[final_intent]
        return final_intent

    # 0e. Pre-router guard: obvious QUESTION patterns.
    # "quanto custa o X?", "qual o valor do X?" should NOT add to cart.
    # Only trigger when no ADD verb is present (avoids "quanto custa? manda um").
    _QUESTION_STARTERS = (
        "quanto custa",
        "quanto é ",
        "quanto eh ",
        "quanto fica",
        "quanto tá ",
        "quanto ta ",
        "quanto sai",
        "qual o valor",
        "qual o preco",
        "qual o preço",
        "aceita pix",
        "aceita cartao",
        "aceita cartão",
        "tem maquininha",
        "qual a taxa",
        "vocês entregam",
        "vcs entregam",
        "ate que horas",
        "até que horas",
        "quanto tempo demora",
    )
    _ADD_VERBS = ("quero", "manda", "bota", "coloca", "me vê", "me ve", "adiciona")
    if any(
        _lower_body.startswith(q) or f" {q}" in _lower_body for q in _QUESTION_STARTERS
    ):
        if not any(v in _lower_body for v in _ADD_VERBS):
            logger.info(
                "[INTENT] pre-router QUESTION guard matched: %r", text_body[:80]
            )
            _last_router_score = 1.0
            final_intent = "GREETING_OR_QUESTION"
            if in_checkout and final_intent in _CHECKOUT_INTENT_OVERRIDES:
                final_intent = _CHECKOUT_INTENT_OVERRIDES[final_intent]
            return final_intent

    # 1. Try the fast semantic router first
    router_intent, router_score = None, 0.0
    try:
        r_intent, r_score, matched = await semantic_intent(text_body)
        router_intent, router_score = r_intent, r_score
        _last_router_score = router_score
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

    # T2-3: Eliminated classify_user_intent() LLM call.
    # After T2-2 expanded the semantic router prototypes, coverage is ~85-90%.
    # For the remaining ~10-15%, we trust the router's best guess (which is
    # still the closest match, just below threshold) or default to ADD which
    # routes through the tool-calling prompt that implicitly classifies intent.

    # 3. If router is confident, return immediately
    if router_intent and router_score >= thresh:
        logger.info(
            "[INTENT] router confident: %s score=%.2f threshold=%.2f",
            router_intent,
            router_score,
            thresh,
        )
        final_intent = router_intent
    elif router_intent and router_score >= thresh * 0.75:
        # 4. Moderate confidence — trust router's best guess without LLM.
        # Exception: FINISH_ORDER requires high confidence to avoid
        # misclassifying vague messages as "finalize order".
        if router_intent == "FINISH_ORDER":
            logger.info(
                "[INTENT] FINISH_ORDER demoted at moderate confidence: score=%.2f threshold=%.2f → default ADD",
                router_score,
                thresh,
            )
            final_intent = "ADD"
        elif router_intent == "ADD":
            # Word-ratio guard: if most words survive extraction AND no
            # qty+item pairs were found, the message is conversational noise.
            # If extract_items_with_quantities finds pairs, the message
            # contains "N product" patterns and is clearly an order.
            _qty_pairs = extract_items_with_quantities(text_body)
            # A real order has at least one explicit quantity (not default 1).
            # Conversational messages produce only (1, "noise") default pairs.
            _has_explicit_qty = any(q > 1 for q, _ in _qty_pairs)
            if _has_explicit_qty:
                logger.info(
                    "[INTENT] moderate ADD confirmed by explicit qty: score=%.2f"
                    " pairs=%s",
                    router_score,
                    [(q, n[:20]) for q, n in _qty_pairs],
                )
                final_intent = "ADD"
            else:
                _ext = extract_items_local(text_body)
                _owc = len(text_body.split())
                _ewc = sum(len(t.split()) for t in _ext)
                _rat = _ewc / _owc if _owc > 0 else 1.0
                if _rat > 0.5:
                    logger.info(
                        "[INTENT] moderate ADD overridden by word-ratio: score=%.2f"
                        " ratio=%.2f → GREETING_OR_QUESTION",
                        router_score,
                        _rat,
                    )
                    final_intent = "GREETING_OR_QUESTION"
                else:
                    logger.info(
                        "[INTENT] router moderate ADD confirmed by word-ratio:"
                        " score=%.2f ratio=%.2f",
                        router_score,
                        _rat,
                    )
                    final_intent = "ADD"
        else:
            logger.info(
                "[INTENT] router moderate: %s score=%.2f threshold=%.2f (no LLM fallback)",
                router_intent,
                router_score,
                thresh,
            )
            final_intent = router_intent
    else:
        # 5. Very low confidence or router failed.
        # Use word-ratio heuristic to distinguish food orders from conversational noise.
        # Food orders lose most words after extraction (ratio ≤ 0.5);
        # conversational messages keep most words (ratio > 0.5).
        _extracted = extract_items_local(text_body)
        _orig_wc = len(text_body.split())
        _ext_wc = sum(len(t.split()) for t in _extracted)
        _ratio = _ext_wc / _orig_wc if _orig_wc > 0 else 1.0

        if _ratio > 0.5:
            # Conversational noise — let the LLM handle it conversationally
            logger.info(
                "[INTENT] low confidence + noise: %s score=%.2f"
                " ratio=%.2f → GREETING_OR_QUESTION",
                router_intent,
                router_score,
                _ratio,
            )
            final_intent = "GREETING_OR_QUESTION"
        else:
            # Safety net: if router says REMOVE/MODIFY and the message
            # contains a removal keyword stem, trust it — removal verbs
            # are unambiguous and should not default to ADD.
            _REMOVAL_STEMS = ("tir", "retir", "remov")
            _lower = text_body.lower()
            if router_intent in ("REMOVE", "MODIFY") and any(
                s in _lower for s in _REMOVAL_STEMS
            ):
                logger.info(
                    "[INTENT] low confidence but removal keyword found: %s"
                    " score=%.2f ratio=%.2f → MODIFY",
                    router_intent,
                    router_score,
                    _ratio,
                )
                final_intent = "MODIFY"
            else:
                # Likely a food order — default to ADD as before
                logger.info(
                    "[INTENT] low confidence: %s score=%.2f"
                    " thresh=%.2f ratio=%.2f → default ADD",
                    router_intent,
                    router_score,
                    thresh,
                    _ratio,
                )
                final_intent = "ADD"

    # 6. Cart-state-aware override: reinterpret intents during checkout
    if in_checkout and final_intent in _CHECKOUT_INTENT_OVERRIDES:
        original = final_intent
        final_intent = _CHECKOUT_INTENT_OVERRIDES[final_intent]
        logger.info(
            "[INTENT] checkout override: %s → %s (cart state=%s)",
            original,
            final_intent,
            cart_state,
        )

    return final_intent


# Categories excluded from meal suggestions (addons, drinks, etc.)
_SUGGESTION_EXCLUDED_CATEGORIES = re.compile(
    r"(?i)^(adicionais|bebidas|cervejas|extras|complementos|acompanhamentos)$"
)


def _filter_meal_suggestions(products: List[Product]) -> List[Product]:
    """Filter products for meal suggestions: exclude addon/drink categories,
    sort by price descending (surfaces main dishes). Returns empty list if
    nothing passes — caller should use _fetch_meal_products as fallback."""
    filtered = [
        p
        for p in products
        if not p.category or not _SUGGESTION_EXCLUDED_CATEGORIES.match(p.category)
    ]
    return sorted(filtered, key=lambda p: p.price, reverse=True)


async def _get_meal_suggestions(
    session,
    bot_id: int,
    products: List[Product],
    min_results: int = 3,
    max_results: int = 4,
) -> List[Product]:
    """Filter products for meal suggestions. If filtering gives fewer than
    min_results, tops up from a direct DB query for non-excluded products."""
    filtered = _filter_meal_suggestions(products)
    if len(filtered) >= min_results:
        return filtered[:max_results]
    # Top up: query DB directly for meal products we don't already have
    from sqlalchemy import select as sa_select

    existing_ids = {p.id for p in filtered}
    query = (
        sa_select(Product)
        .where(
            Product.bot_id == bot_id,
            Product.is_available == True,
            Product.is_deleted == False,
        )
        .order_by(Product.price.desc())
        .limit(max_results * 3)
    )
    result = await session.execute(query)
    all_products = list(result.scalars().all())
    extras = _filter_meal_suggestions(
        [p for p in all_products if p.id not in existing_ids]
    )
    combined = sorted(filtered + extras, key=lambda p: p.price, reverse=True)
    return combined[:max_results] if combined else all_products[:max_results]


def _format_product_suggestions_message(products: List[Product], title: str) -> str:
    if not products:
        return "Puxa, não encontrei nenhuma sugestão específica no momento. Mas nosso cardápio está cheio de delícias! O que você gostaria?"
    message_parts = [f"{title} ✨\n"]
    for i, p in enumerate(products):
        price_formatted = f"R$ {p.price:.2f}".replace(".", ",")
        item_str = f"{i + 1}️⃣ *{p.name}* — {price_formatted}"
        if p.description:
            item_str += f"\n_{p.description}_"
        message_parts.append(item_str)
    footer = "\nQual você quer? Responda com o número! 😉"
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
                    msg = "Pagamento confirmado! ✅\n\nSeu pedido já está sendo preparado. Obrigado pela preferência! 🙏"

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
