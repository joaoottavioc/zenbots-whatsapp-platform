"""Web widget HTTP endpoints (plan/in_browser_bots.md Phase 2.1).

Phase 2.1 freezes the public contract — Pydantic schemas in app/schemas.py
plus the three route signatures here. The route bodies return 501 until
Phase 2.2-2.4 land:

  - 2.2 wires `process_chat_message` ARQ task that builds a ParsedIngress
    from a /chat POST body and runs through the same downstream pipeline.
  - 2.3 implements the per-IP rate limit, CORS check against
    bot.web_widget_allowed_origins, and the SSE forwarder that subscribes
    to Redis PubSub `chat:{bot_id}:{session_id}` and streams events.
  - 2.4 wires Contact creation with channel='web' and the A1b synthesis
    (phone_number=f'web:{session_id}').

Why ship the empty contract now: the frontend widget (Phase 3) can build
against the schemas in parallel. Returning 501 instead of leaving the
endpoints unmounted prevents the widget team from inventing a fictional
shape — they see the real status codes and Pydantic validators.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Path

from app import schemas

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["Web Widget"])


_NOT_IMPLEMENTED_DETAIL = {
    "error": "phase_2_not_implemented",
    "message": (
        "The /chat endpoints are scaffolded in Phase 2.1 but their bodies "
        "arrive with Phase 2.2-2.4. See plan/in_browser_bots.md."
    ),
}


@router.post(
    "/{bot_id}/message",
    status_code=202,
    response_model=schemas.ChatMessageAccepted,
    summary="Send a customer message to a bot's web widget",
)
async def post_chat_message(
    bot_id: int = Path(..., ge=1),
    payload: schemas.ChatMessageRequest = ...,
):
    """Enqueue an inbound widget message for processing.

    Phase 2.2 will:
      1. Validate Origin against bot.web_widget_allowed_origins.
      2. Apply per-IP rate limit (5.1) and per-session daily cap (5.2).
      3. Enqueue ARQ task `process_chat_message(bot_id, session_id,
         message_id, text)`.
      4. Return 202 with `accepted: true`, `message_id: <echo>`.

    The reply itself arrives via the SSE stream — keep that connection
    open BEFORE sending the POST so the widget doesn't miss the first
    typing/message events.
    """
    raise HTTPException(status_code=501, detail=_NOT_IMPLEMENTED_DETAIL)


@router.get(
    "/{bot_id}/stream",
    summary="Subscribe to the bot's reply stream via SSE",
)
async def get_chat_stream(
    bot_id: int = Path(..., ge=1),
    session_id: str = ...,
):
    """Server-Sent Events endpoint streaming the bot's replies.

    Phase 2.3 will:
      1. Validate session_id format (UUID-ish).
      2. Open a Redis PubSub subscription to `chat:{bot_id}:{session_id}`.
      3. Stream each published envelope as `data: <json>\\n\\n`.
      4. Send `data: {"type": "ping"}\\n\\n` every 10s to keep proxies
         from closing the connection.
      5. Drop the subscription cleanly when the client disconnects.

    Event types the widget consumes (envelopes documented in
    app/schemas.py as ChatSSETyping / ChatSSEMessage / ChatSSEPaymentQR /
    ChatSSEOrderStatus / ChatSSEPing).
    """
    raise HTTPException(status_code=501, detail=_NOT_IMPLEMENTED_DETAIL)


@router.post(
    "/{bot_id}/session",
    response_model=schemas.ChatSessionResponse,
    summary="Optional handshake — get bot display name, welcome, theme",
)
async def post_chat_session(
    bot_id: int = Path(..., ge=1),
):
    """Issue or echo a session id, return widget config so the UI can
    render before the first user message.

    Phase 2.4 will:
      1. Look up the bot (must have web_widget_enabled=True).
      2. Apply per-IP throttle on session creation (mitigates session-
         farming abuse — anonymous sessions are free without it).
      3. Generate a fresh session_id if the client didn't supply one.
      4. Pull theme + welcome_message from bot.web_widget_theme.
      5. Return the bot's display name and plan tier so the widget can
         decide whether to render the "Powered by ZenBotZ®" footer.
    """
    raise HTTPException(status_code=501, detail=_NOT_IMPLEMENTED_DETAIL)
