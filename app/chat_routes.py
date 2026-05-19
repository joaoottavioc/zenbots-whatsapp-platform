"""Web widget HTTP endpoints (plan/in_browser_bots.md Phase 2.3).

  - POST /chat/{bot_id}/message  — enqueue an inbound widget message
  - GET  /chat/{bot_id}/stream   — subscribe to per-session SSE replies
  - POST /chat/{bot_id}/session  — handshake (bot display, welcome, theme)

The endpoint bodies wire together the pieces that already exist:
  - process_chat_message ARQ task (Phase 2.2) for the inbound side
  - broadcast_web_reply (Phase 1.5) → Redis PubSub for the outbound side
  - rate_limiter.is_rate_limited (existing) for per-IP throttle
  - Bot.web_widget_enabled / web_widget_allowed_origins (Phase 2.5) for
    access control

CORS: the Origin header must appear in `bot.web_widget_allowed_origins`,
unless that list is empty AND we're in dev (ENVIRONMENT != 'production'),
in which case any Origin is allowed (mirror the dashboard CORS posture).

Rate limit: 20 messages per minute per (IP, bot_id) for POST /message.
The per-session daily cap on Free tier (plan §5.2) is a Phase 5 deliverable
— at that point we add a second is_rate_limited check on
`chat:session_day:{session_id}:{YYYY-MM-DD}` after looking up the bot's
plan tier.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid

import redis.asyncio as redis
from arq.connections import ArqRedis
from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse

from app import crud
from app import schemas
from app.database import async_session
from app.rate_limiter import is_rate_limited

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["Web Widget"])

# Per-IP rate limit on /chat/{bot_id}/message. The cap is intentionally
# generous — abuse mitigation kicks in at the per-session daily cap
# (Phase 5.2). This limit just prevents a runaway loop or curl-bomb
# from hammering ARQ.
_PER_IP_MESSAGE_LIMIT = 20
_PER_IP_MESSAGE_WINDOW_SECONDS = 60

# SSE keep-alive cadence. Same number main.py uses for the dashboard
# stream so Caddy/CloudFront see traffic on the connection.
_SSE_PING_INTERVAL = 10

# Redis URL — same database (0) as broadcast.py uses for PubSub. The
# rate-limit keys also live on DB 0 but in a disjoint namespace.
_REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")


def _client_ip(request: Request) -> str:
    """Pull the originating client IP. Trusts X-Forwarded-For when behind
    a reverse proxy (ALB/Caddy add it), falls back to request.client.host."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        # First hop is the original client.
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _load_widget_bot(bot_id: int):
    """Look up the bot and confirm web_widget_enabled. Returns the Bot
    instance or raises a 404/403 HTTPException."""
    async with async_session() as session:
        bot = await crud.get_bot_by_id(session, bot_id=bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail={"error": "bot_not_found"})
    if not bot.web_widget_enabled:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "web_widget_disabled",
                "message": "Este restaurante não aceita pedidos pelo widget no momento.",
            },
        )
    return bot


def _check_origin(request: Request, bot) -> None:
    """Validate the Origin header against bot.web_widget_allowed_origins.

    No Origin header (curl, server-to-server) is allowed — only browser
    embeds carry it. Empty allowlist + non-production env = allow any
    Origin (dev convenience). Empty allowlist + production = reject every
    Origin header (the bot owner hasn't configured the widget yet).
    """
    origin = request.headers.get("origin")
    if not origin:
        return  # non-browser caller; let it through (e.g. test harness)

    allowed = list(bot.web_widget_allowed_origins or [])
    if origin in allowed:
        return

    env = os.getenv("ENVIRONMENT", "development").lower()
    if not allowed and "prod" not in env:
        return  # dev convenience: empty allowlist permits anything

    raise HTTPException(
        status_code=403,
        detail={
            "error": "origin_not_allowed",
            "message": "Este domínio não está autorizado a incorporar o widget.",
            "origin": origin,
        },
    )


@router.post(
    "/{bot_id}/message",
    status_code=202,
    response_model=schemas.ChatMessageAccepted,
    summary="Send a customer message to a bot's web widget",
)
async def post_chat_message(
    request: Request,
    bot_id: int = Path(..., ge=1),
    payload: schemas.ChatMessageRequest = ...,
):
    """Enqueue an inbound widget message for processing.

    Returns 202 immediately; the bot's reply arrives asynchronously on
    the SSE stream the widget should already have open.
    """
    bot = await _load_widget_bot(bot_id)
    _check_origin(request, bot)

    # Per-IP rate limit. Scoped by bot so one noisy customer doesn't
    # poison another restaurant's quota.
    client_ip = _client_ip(request)
    rl_key = f"chat:ip:{client_ip}:bot:{bot_id}"
    if await is_rate_limited(
        rl_key,
        _PER_IP_MESSAGE_LIMIT,
        _PER_IP_MESSAGE_WINDOW_SECONDS,
    ):
        logger.warning(
            "chat rate-limit: ip=%s bot_id=%s limit=%d window=%ds",
            client_ip,
            bot_id,
            _PER_IP_MESSAGE_LIMIT,
            _PER_IP_MESSAGE_WINDOW_SECONDS,
        )
        raise HTTPException(
            status_code=429,
            detail={"error": "rate_limited"},
            headers={"Retry-After": str(_PER_IP_MESSAGE_WINDOW_SECONDS)},
        )

    redis_queue: ArqRedis = request.app.state.arq_redis
    await redis_queue.enqueue_job(
        "process_chat_message",
        bot_id,
        payload.session_id,
        payload.message_id,
        payload.text,
    )
    return schemas.ChatMessageAccepted(accepted=True, message_id=payload.message_id)


@router.get(
    "/{bot_id}/stream",
    summary="Subscribe to the bot's reply stream via SSE",
)
async def get_chat_stream(
    request: Request,
    bot_id: int = Path(..., ge=1),
    session_id: str = Query(..., min_length=1, max_length=64),
):
    """Server-Sent Events forwarder for a single web session.

    Subscribes to `chat:{bot_id}:{session_id}` on Redis PubSub and streams
    each envelope as `data: <json>\\n\\n`. Sends a `data: {"type":"ping"}`
    heartbeat every 10s so proxies don't close the connection for idle.
    The subscription is per (bot, session) — never cross-leaked.
    """
    bot = await _load_widget_bot(bot_id)
    _check_origin(request, bot)

    channel_name = f"chat:{bot_id}:{session_id}"

    async def event_generator():
        r = redis.from_url(
            _REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        pubsub = r.pubsub()
        await pubsub.subscribe(channel_name)
        try:
            logger.info("Chat SSE connected: bot_id=%s session=%s", bot_id, session_id)
            # Initial ping so the widget knows the stream is alive even
            # before the bot says anything.
            yield f"data: {json.dumps({'type': 'ping', 'message': 'connected'})}\n\n"
            last_ping = time.monotonic()

            while True:
                if await request.is_disconnected():
                    logger.info(
                        "Chat SSE disconnected: bot_id=%s session=%s",
                        bot_id,
                        session_id,
                    )
                    break

                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message:
                    raw = message["data"]
                    # broadcast.py-style validation: payload is already JSON.
                    try:
                        json.loads(raw)
                        payload_str = raw
                    except json.JSONDecodeError:
                        logger.warning(
                            "Chat SSE: invalid JSON from Redis on %s", channel_name
                        )
                        payload_str = json.dumps(
                            {"type": "error", "message": "invalid payload"}
                        )
                    yield f"data: {payload_str}\n\n"
                    last_ping = time.monotonic()
                else:
                    now = time.monotonic()
                    if now - last_ping >= _SSE_PING_INTERVAL:
                        yield f"data: {json.dumps({'type': 'ping'})}\n\n"
                        last_ping = now
        except asyncio.CancelledError:
            logger.info("Chat SSE cancelled: bot_id=%s session=%s", bot_id, session_id)
        except Exception as e:
            logger.error("Chat SSE error on %s: %s", channel_name, e)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            try:
                await pubsub.unsubscribe(channel_name)
                await r.aclose()
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disable Nginx/CloudFront response buffering — SSE needs
            # immediate flushing of each event.
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/{bot_id}/session",
    response_model=schemas.ChatSessionResponse,
    summary="Optional handshake — get bot display name, welcome, theme",
)
async def post_chat_session(
    request: Request,
    bot_id: int = Path(..., ge=1),
):
    """Issue a fresh session_id and return widget config.

    Client behavior: call this on widget mount; persist the returned
    session_id in localStorage and reuse it on subsequent visits. The
    response carries the bot's display name, welcome message, theme,
    and plan tier (the widget renders 'Powered by ZenBotZ®' iff
    plan_tier == 'free').
    """
    bot = await _load_widget_bot(bot_id)
    _check_origin(request, bot)

    theme = dict(bot.web_widget_theme or {})
    welcome = theme.get("welcome_message") or (
        f"Olá! Bem-vindo(a) ao {bot.restaurant_name or 'nosso restaurante'}! 😊"
    )

    # Plan tier — drives the free-tier branding decision on the widget.
    # The lookup mirrors _check_subscription's logic but only cares about
    # the tier name; full status enforcement is the gate inside
    # process_chat_message.
    async with async_session() as session:
        sub = await crud.get_subscription_by_bot(session, bot.id)
    plan_tier = "free"
    if sub and getattr(sub, "plan_type", None):
        plan_tier = sub.plan_type

    return schemas.ChatSessionResponse(
        session_id=str(uuid.uuid4()),
        bot_display_name=bot.restaurant_name or "Restaurante",
        welcome_message=welcome,
        theme=theme,
        plan_tier=plan_tier,
    )
