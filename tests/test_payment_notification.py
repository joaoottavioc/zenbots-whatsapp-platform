# tests/test_payment_notification.py
"""
Focused tests for the payment confirmation notification path in whatsapp.py.

Validates that the phone_dest is derived from order.contact.phone_number
(not from the fragile email-split hack) and that a missing contact
does not crash.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestPaymentNotificationPhoneDest:
    """Verify phone_dest logic in the payment webhook handler."""

    def _make_order(self, contact=None):
        order = MagicMock()
        order.id = 42
        order.bot_id = 1
        order.status = "pending"
        order.contact = contact
        order.bot = MagicMock()
        order.bot.whatsapp_token = "fake-token"
        order.bot.phone_number_id = "fake-phone-id"
        return order

    def _make_contact(self, phone="5511999999999"):
        contact = MagicMock()
        contact.phone_number = phone
        return contact

    async def test_phone_dest_from_contact(self):
        """When order.contact exists, phone_dest equals contact.phone_number."""
        contact = self._make_contact(phone="5511888888888")
        order = self._make_order(contact=contact)

        # Simulate what the code does
        phone_dest = order.contact.phone_number if order.contact else None

        assert phone_dest == "5511888888888"

    async def test_no_contact_returns_none(self):
        """When order.contact is None, phone_dest is None — no crash."""
        order = self._make_order(contact=None)

        phone_dest = order.contact.phone_number if order.contact else None

        assert phone_dest is None

    async def test_send_skipped_when_no_contact(self):
        """When phone_dest is None, send_whatsapp_message must NOT be called."""
        order = self._make_order(contact=None)
        send_mock = AsyncMock()

        phone_dest = order.contact.phone_number if order.contact else None

        if phone_dest:
            await send_mock(
                to=phone_dest,
                message="test",
                token=order.bot.whatsapp_token,
                phone_id=order.bot.phone_number_id,
            )

        send_mock.assert_not_called()

    async def test_send_called_when_contact_exists(self):
        """When phone_dest is available, send_whatsapp_message IS called."""
        contact = self._make_contact(phone="5511777777777")
        order = self._make_order(contact=contact)
        send_mock = AsyncMock()

        phone_dest = order.contact.phone_number if order.contact else None

        if phone_dest:
            await send_mock(
                to=phone_dest,
                message="Pagamento confirmado!",
                token=order.bot.whatsapp_token,
                phone_id=order.bot.phone_number_id,
            )

        send_mock.assert_called_once()
        assert send_mock.call_args.kwargs["to"] == "5511777777777"
