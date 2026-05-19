"""Channel-aware bot lookup (plan/in_browser_bots.md Phase 1.4).

`_find_bot` is the only gate whose LOGIC differs by channel:
  - WhatsApp identifies the bot via the incoming webhook's phone_number_id
    (with display-number fallback for occasional Meta drift).
  - Web identifies the bot via the URL `POST /chat/{bot_id}/message`,
    so the lookup is just a primary key fetch.

These tests pin the contract before Phase 2 wires the web ingress. They
also verify the existing positional callers (one site in
process_whatsapp_message) keep working — the new `channel`/`bot_id`
parameters are kwargs with defaults, so backward compat is structural.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.whatsapp import _find_bot


def _make_session_returning(bot_or_none):
    """Build an AsyncMock session whose .execute().scalars().first() returns the given value."""
    session = MagicMock()
    session.execute = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = bot_or_none
    session.execute.return_value = result
    return session


@pytest.mark.asyncio
async def test_whatsapp_default_channel_looks_up_by_phone_number_id():
    """Positional call from process_whatsapp_message: existing behavior
    unchanged. The phone_number_id index hits, no display-number fallback."""
    bot = MagicMock()
    bot.id = 42
    session = _make_session_returning(bot)

    result = await _find_bot(session, "881274981741368", "+15551312070")
    assert result is bot
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_whatsapp_falls_back_to_display_number_on_miss():
    """If phone_number_id doesn't match, _find_bot falls through to
    crud.get_bot_by_number — covers occasional Meta drift between the two."""
    session = _make_session_returning(None)  # phone_number_id miss
    fallback_bot = MagicMock()
    fallback_bot.id = 99

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.whatsapp.crud.get_bot_by_number",
            AsyncMock(return_value=fallback_bot),
        )
        result = await _find_bot(session, "missing-id", "+15551312070")

    assert result is fallback_bot


@pytest.mark.asyncio
async def test_whatsapp_returns_none_when_both_lookups_miss():
    """If neither phone_number_id nor display number match, _find_bot
    returns None — caller drops the message silently (same as today)."""
    session = _make_session_returning(None)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.whatsapp.crud.get_bot_by_number",
            AsyncMock(return_value=None),
        )
        result = await _find_bot(session, "missing-id", "+missing")

    assert result is None


@pytest.mark.asyncio
async def test_web_channel_looks_up_by_bot_id():
    """Web ingress: bot_id from the URL is the lookup key.
    phone_number_id is not used."""
    bot = MagicMock()
    bot.id = 7
    session = _make_session_returning(bot)

    result = await _find_bot(session, channel="web", bot_id=7)
    assert result is bot
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_web_channel_returns_none_when_bot_missing():
    """Web bot_id that doesn't match any row — caller drops."""
    session = _make_session_returning(None)

    result = await _find_bot(session, channel="web", bot_id=9999)
    assert result is None


@pytest.mark.asyncio
async def test_web_channel_without_bot_id_returns_none_and_does_not_query():
    """Misconstruction guard: a web call must provide bot_id. Returns None
    without hitting the DB — protects against accidental SELECT *."""
    session = MagicMock()
    session.execute = AsyncMock()

    result = await _find_bot(session, channel="web", bot_id=None)
    assert result is None
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_web_channel_ignores_whatsapp_args():
    """Even if a WhatsApp-shaped phone_number_id is passed alongside
    channel='web', the web branch ignores it and uses bot_id. Prevents
    accidental cross-channel lookups."""
    bot = MagicMock()
    bot.id = 12
    session = _make_session_returning(bot)

    result = await _find_bot(
        session,
        "some-wa-phone-id",  # ignored
        "+wa-display",  # ignored
        channel="web",
        bot_id=12,
    )
    assert result is bot
