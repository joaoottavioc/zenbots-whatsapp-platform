"""Channel dispatch on MessageContext.reply (plan/in_browser_bots.md Phase 1.2b).

These tests pin the egress seam BEFORE the larger Phase 1.2c call-site
migration. They guarantee:

  1. The default channel is 'whatsapp' so every existing call site
     that constructs MessageContext positionally continues to work.
  2. ctx.reply() with channel='whatsapp' routes to send_whatsapp_message
     with the right kwargs (to/message/token/phone_id, plus media when
     supplied).
  3. ctx.reply() with channel='web' raises NotImplementedError — until
     Phase 2 wires broadcast_web_reply, a web caller should fail loudly
     rather than silently drop a message.
  4. ctx.contact_identity is the channel-neutral alias of contact_number
     and returns the same value (A1b synthesis).
  5. An unknown channel value raises ValueError (defensive — keeps the
     dispatch table closed).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.whatsapp import MessageContext


def _make_ctx(channel: str = "whatsapp") -> MessageContext:
    """Build a MessageContext with cheap mocks for fields we don't exercise."""
    return MessageContext(
        session=MagicMock(),
        bot=MagicMock(),
        contact=MagicMock(),
        cart=MagicMock(),
        contact_number="5511999999999",
        text_body="oi",
        token="fake-wa-token",
        phone_id="fake-phone-id",
        channel=channel,
    )


def test_default_channel_is_whatsapp():
    """Positional construction (the pattern the existing ~30 call sites
    use) must default channel to 'whatsapp' so behavior is unchanged."""
    ctx = MessageContext(
        session=MagicMock(),
        bot=MagicMock(),
        contact=MagicMock(),
        cart=MagicMock(),
        contact_number="5511999999999",
        text_body="oi",
        token="t",
        phone_id="p",
    )
    assert ctx.channel == "whatsapp"
    assert ctx.channel_metadata == {}


def test_contact_identity_aliases_contact_number():
    """ctx.contact_identity is the channel-neutral name; until the
    Phase 1.2c rename it must return the same value as contact_number."""
    ctx = _make_ctx()
    assert ctx.contact_identity == ctx.contact_number


@pytest.mark.asyncio
async def test_reply_whatsapp_routes_to_send_whatsapp_message():
    ctx = _make_ctx(channel="whatsapp")
    with patch("app.whatsapp.send_whatsapp_message", new=AsyncMock()) as mock_send:
        await ctx.reply("olá!")

    mock_send.assert_awaited_once_with(
        to="5511999999999",
        message="olá!",
        token="fake-wa-token",
        phone_id="fake-phone-id",
        media_url=None,
        media_type="image",
    )


@pytest.mark.asyncio
async def test_reply_whatsapp_passes_media():
    ctx = _make_ctx(channel="whatsapp")
    with patch("app.whatsapp.send_whatsapp_message", new=AsyncMock()) as mock_send:
        await ctx.reply(
            "veja o cardápio",
            media_url="https://example.com/menu.pdf",
            media_type="document",
        )

    mock_send.assert_awaited_once()
    kwargs = mock_send.await_args.kwargs
    assert kwargs["media_url"] == "https://example.com/menu.pdf"
    assert kwargs["media_type"] == "document"


@pytest.mark.asyncio
async def test_reply_web_routes_to_broadcast_web_reply():
    """Phase 1.5: web channel egress dispatches to broadcast_web_reply
    with the session_id pulled from channel_metadata."""
    ctx = _make_ctx(channel="web")
    ctx.bot.id = 42
    ctx.channel_metadata = {"session_id": "abc-123"}

    with patch(
        "app.web_channel.broadcast_web_reply", new=AsyncMock()
    ) as mock_broadcast:
        await ctx.reply("olá pelo navegador")

    mock_broadcast.assert_awaited_once_with(
        bot_id=42,
        session_id="abc-123",
        text="olá pelo navegador",
        attachments=[],
    )


@pytest.mark.asyncio
async def test_reply_web_passes_media_as_attachment():
    """Media on web is sent as a structured attachment in the SSE payload,
    not as a separate transport call (the widget renders it inline)."""
    ctx = _make_ctx(channel="web")
    ctx.bot.id = 42
    ctx.channel_metadata = {"session_id": "abc-123"}

    with patch(
        "app.web_channel.broadcast_web_reply", new=AsyncMock()
    ) as mock_broadcast:
        await ctx.reply(
            "veja o cardápio",
            media_url="https://example.com/menu.pdf",
            media_type="document",
        )

    kwargs = mock_broadcast.await_args.kwargs
    assert kwargs["attachments"] == [
        {"type": "document", "url": "https://example.com/menu.pdf"}
    ]


@pytest.mark.asyncio
async def test_reply_web_without_session_id_raises():
    """A web MessageContext without a session_id in channel_metadata is a
    misconstruction — fail loudly rather than silently drop the message."""
    ctx = _make_ctx(channel="web")
    ctx.channel_metadata = {}  # missing session_id

    with pytest.raises(ValueError, match="session_id"):
        await ctx.reply("ignored")


@pytest.mark.asyncio
async def test_reply_unknown_channel_raises():
    ctx = _make_ctx(channel="telegram")
    with pytest.raises(ValueError, match="Unknown channel"):
        await ctx.reply("ignored")
