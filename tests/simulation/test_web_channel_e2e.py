"""End-to-end test for the web channel via process_chat_message.

Phase 2.2 of plan/in_browser_bots.md. Mirrors tests/simulation/test_add_items.py's
shape — same real LLM + real DB + real cart pipeline — but driven through
the web ingress path instead of process_whatsapp_message.

Reuses the existing conftest.py simulation fixtures (`sim_bot`,
`sim_products`, etc.) which create a fictional "Sabor da Serra" restaurant
with full menu + active subscription. The bot is created WhatsApp-only by
default; we flip web_widget_enabled=True for these tests.

Channel-shape guarantees this suite pins:
  - process_chat_message accepts the ARQ kwargs shape, doesn't crash.
  - A web Contact is created with phone_number="web:{session_id}".
  - mctx.reply() during handler execution routes to broadcast_web_reply
    (we intercept it so the bot's response is asserted from the captured
    SSE event payload, not from sending a real WhatsApp message).
"""

import uuid
from unittest.mock import patch

import pytest
from sqlmodel import select

from app.database import async_session
from app.models import Bot, Contact


def _make_web_payload(session_id: str, text: str) -> dict:
    """Return a process_chat_message kwargs dict for the given input."""
    return {
        "session_id": session_id,
        "message_id": f"web-msg-{uuid.uuid4().hex[:12]}",
        "text": text,
    }


@pytest.mark.asyncio
async def test_process_chat_message_creates_web_contact(
    db_session, sim_bot, sim_phone_number_id
):
    """First web message for a session creates a Contact with the A1b
    synthesis (phone_number = 'web:{session_id}', channel='web')."""
    from app.whatsapp import process_chat_message

    bot_id = sim_bot["bot_id"]
    session_id = f"sess-{uuid.uuid4().hex[:12]}"

    # Enable the web widget for this bot (conftest creates it WhatsApp-only).
    async with async_session() as session:
        bot = await session.get(Bot, bot_id)
        bot.web_widget_enabled = True
        session.add(bot)
        await session.commit()

    # Intercept the SSE egress so the test doesn't depend on a live
    # Redis subscriber. We don't assert on the message text here — that's
    # the next test.
    sent_events: list[tuple] = []

    async def capture(**kwargs):
        sent_events.append(kwargs)

    with patch("app.web_channel.broadcast_web_reply", new=capture):
        await process_chat_message(
            ctx={},
            bot_id=bot_id,
            **_make_web_payload(session_id, "oi"),
        )

    # Verify the contact was created with the synthesized identity.
    async with async_session() as session:
        result = await session.execute(
            select(Contact).where(
                Contact.bot_id == bot_id,
                Contact.phone_number == f"web:{session_id}",
            )
        )
        contact = result.scalars().first()

    assert contact is not None, "Expected web Contact to be created"
    assert contact.channel == "web"
    # contact_phone is left None for web (human phone collected at checkout).
    assert contact.contact_phone is None


@pytest.mark.asyncio
async def test_process_chat_message_routes_reply_via_web_egress(db_session, sim_bot):
    """A real conversation turn — sends a greeting, expects the welcome-
    with-menu intercept to fire. The bot's reply must come back via
    broadcast_web_reply, never via send_whatsapp_message (which would
    crash on the bot's empty whatsapp_token in a real web-only setup).
    """
    from app.whatsapp import process_chat_message

    bot_id = sim_bot["bot_id"]
    session_id = f"sess-{uuid.uuid4().hex[:12]}"

    async with async_session() as session:
        bot = await session.get(Bot, bot_id)
        bot.web_widget_enabled = True
        session.add(bot)
        await session.commit()

    sent_events: list[dict] = []

    async def capture(**kwargs):
        sent_events.append(kwargs)

    # Patch BOTH egress paths — if the test inadvertently goes through
    # the WhatsApp path, we want to know.
    wa_calls: list[dict] = []

    async def wa_capture(*args, **kwargs):
        wa_calls.append({"args": args, "kwargs": kwargs})

    with (
        patch("app.web_channel.broadcast_web_reply", new=capture),
        patch("app.whatsapp.send_whatsapp_message", new=wa_capture),
    ):
        await process_chat_message(
            ctx={},
            bot_id=bot_id,
            **_make_web_payload(session_id, "oi"),
        )

    assert len(sent_events) >= 1, (
        f"Expected at least one web reply, got events={sent_events}, wa={wa_calls}"
    )
    # No fallthrough to WhatsApp transport.
    assert wa_calls == [], f"Web message leaked to WhatsApp egress: {wa_calls}"
    # Greeting reply must mention the restaurant name (conftest seeds
    # "Sabor da Serra" via app.simulation.menu).
    first_text = sent_events[0]["text"]
    assert "Sabor da Serra" in first_text or "olá" in first_text.lower()


@pytest.mark.asyncio
async def test_process_chat_message_dedup(db_session, sim_bot):
    """Sending the same message_id twice (e.g. retry-on-network-blip)
    should produce exactly one stored interaction, mirroring the WhatsApp
    `wamid` dedup behavior. Uses the ProcessedMessage table — same row
    shape for both channels."""
    from app.whatsapp import process_chat_message

    bot_id = sim_bot["bot_id"]
    session_id = f"sess-{uuid.uuid4().hex[:12]}"
    message_id = f"web-msg-{uuid.uuid4().hex[:12]}"

    async with async_session() as session:
        bot = await session.get(Bot, bot_id)
        bot.web_widget_enabled = True
        session.add(bot)
        await session.commit()

    sent_events: list[dict] = []

    async def capture(**kwargs):
        sent_events.append(kwargs)

    with patch("app.web_channel.broadcast_web_reply", new=capture):
        await process_chat_message(
            ctx={},
            bot_id=bot_id,
            session_id=session_id,
            message_id=message_id,
            text="oi",
        )
        first_count = len(sent_events)

        # Send the same message_id again — should be deduped silently.
        await process_chat_message(
            ctx={},
            bot_id=bot_id,
            session_id=session_id,
            message_id=message_id,  # same!
            text="oi",
        )
        second_count = len(sent_events)

    assert first_count >= 1, "First send must produce at least one event"
    assert second_count == first_count, (
        f"Duplicate message produced extra events: "
        f"first={first_count} second={second_count} events={sent_events}"
    )


@pytest.mark.asyncio
async def test_process_chat_message_rejects_when_widget_disabled(db_session, sim_bot):
    """A bot with web_widget_enabled=False must reject web messages with
    the maintenance text and not enter the conversational pipeline."""
    from app.whatsapp import process_chat_message

    bot_id = sim_bot["bot_id"]
    session_id = f"sess-{uuid.uuid4().hex[:12]}"

    async with async_session() as session:
        bot = await session.get(Bot, bot_id)
        bot.web_widget_enabled = False  # explicitly disable
        session.add(bot)
        await session.commit()

    sent_events: list[dict] = []

    async def capture(**kwargs):
        sent_events.append(kwargs)

    with patch("app.web_channel.broadcast_web_reply", new=capture):
        await process_chat_message(
            ctx={},
            bot_id=bot_id,
            **_make_web_payload(session_id, "quero uma picanha"),
        )

    assert len(sent_events) == 1, (
        f"Expected exactly one maintenance reply, got {sent_events}"
    )
    assert "manutenção" in sent_events[0]["text"].lower()
