"""A bot with no products must say its menu isn't ready — not that the
service is under maintenance.

Both channels bounce off the same gate when a bot's catalogue is empty
(`_check_bot_has_products` on WhatsApp, gate 7 of `process_chat_message`
on the web widget). Both used to emit the generic maintenance notice,
which reads as an outage to a customer and tells the owner nothing during
setup. They now share `EMPTY_MENU_MESSAGE`.

The genuine blocks — plan gate, widget disabled — deliberately keep the
maintenance text; those are covered by test_conversation_states.py and
test_subscription_paused_blocked.py.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.whatsapp import EMPTY_MENU_MESSAGE, _check_bot_has_products
from tests.conftest import PATCH_CRUD, PATCH_SEND


def _make_bot():
    bot = MagicMock()
    bot.id = 16
    bot.whatsapp_token = None  # decrypt_value(None) -> None, unused by the assertion
    bot.phone_number_id = "phone_123"
    return bot


class TestEmptyMenuMessage:
    def test_message_is_not_the_maintenance_notice(self):
        """The whole point of the split: an empty menu must not be reported
        as an outage."""
        assert "manuten" not in EMPTY_MENU_MESSAGE.lower()

    def test_message_makes_no_promise_of_a_human(self):
        """The web widget can't tell an owner testing from a real customer,
        and human takeover may be inactive — so don't promise an attendant."""
        assert "atendente" not in EMPTY_MENU_MESSAGE.lower()

    def test_message_explains_the_menu_is_not_ready(self):
        assert "cardápio" in EMPTY_MENU_MESSAGE.lower()

    @pytest.mark.asyncio
    async def test_whatsapp_sends_empty_menu_message_when_no_products(self):
        """No products -> caller is told to stop, customer gets the new text."""
        bot = _make_bot()

        with patch(PATCH_CRUD) as crud_mock, patch(PATCH_SEND) as send_mock:
            crud_mock.get_products_by_bot_id = AsyncMock(return_value=[])
            send_mock.return_value = AsyncMock()

            should_stop = await _check_bot_has_products(
                MagicMock(), bot, "5500099990001"
            )

        assert should_stop is True
        send_mock.assert_called_once()
        assert send_mock.call_args.kwargs["message"] == EMPTY_MENU_MESSAGE

    @pytest.mark.asyncio
    async def test_whatsapp_stays_quiet_when_products_exist(self):
        """Bot with a menu falls through to the normal pipeline, sends nothing."""
        bot = _make_bot()

        with patch(PATCH_CRUD) as crud_mock, patch(PATCH_SEND) as send_mock:
            crud_mock.get_products_by_bot_id = AsyncMock(return_value=[MagicMock()])

            should_stop = await _check_bot_has_products(
                MagicMock(), bot, "5500099990001"
            )

        assert should_stop is False
        send_mock.assert_not_called()

    def test_web_gate_uses_the_shared_constant(self):
        """Gate 7 of the web pipeline must emit EMPTY_MENU_MESSAGE, while the
        genuine blocks (widget disabled, plan gate) keep `_maintenance_msg`.

        Asserted against the source because `process_chat_message` runs every
        gate inside one `async_session()` block — reaching gate 7 through the
        public entrypoint needs the real DB, which lives in tests/simulation
        and is excluded from CI.
        """
        import inspect

        from app.whatsapp import process_chat_message

        src = inspect.getsource(process_chat_message)
        marker = 'logger.warning(\n                    "No products configured for web message'
        assert marker in src, "web no-products gate moved; update this test"

        after_gate = src.split(marker, 1)[1]
        emit_call = after_gate[: after_gate.index("return")]
        assert "_emit(EMPTY_MENU_MESSAGE)" in emit_call
        assert "_maintenance_msg" not in emit_call
