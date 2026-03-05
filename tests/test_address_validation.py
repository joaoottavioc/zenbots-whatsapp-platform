# tests/test_address_validation.py
"""Tests for address field validation in _handle_number_complement."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.models import CartState


class TestAddressValidation:
    def _make_mctx(self, partial_address, text_body="142, Apto 3"):
        """Create a minimal MessageContext mock."""
        cart = MagicMock()
        cart.state = CartState.AWAITING_NUMBER_COMPLEMENT
        cart.partial_address = partial_address
        cart.pending_address = None
        cart.last_activity_at = None

        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()

        mctx = MagicMock()
        mctx.cart = cart
        mctx.session = session
        mctx.text_body = text_body
        mctx.contact_number = "5511999999999"
        mctx.token = "token"
        mctx.phone_id = "phone_id"
        mctx.bot = MagicMock()
        mctx.bot.id = 1
        return mctx

    @pytest.mark.asyncio
    @patch("app.whatsapp.send_whatsapp_message", new_callable=AsyncMock)
    @patch("app.whatsapp.crud.add_interaction_to_history", new_callable=AsyncMock)
    async def test_address_with_missing_fields_uses_fallbacks(self, mock_history, mock_send):
        """When partial_address has only CEP, fallbacks should be used."""
        from app.whatsapp import _handle_number_complement

        mctx = self._make_mctx(partial_address={"cep": "01001000"})
        result = await _handle_number_complement(mctx)

        assert result is not None
        assert "Rua não informada" in result
        assert "Bairro não informado" in result

    @pytest.mark.asyncio
    @patch("app.whatsapp.send_whatsapp_message", new_callable=AsyncMock)
    @patch("app.whatsapp.crud.add_interaction_to_history", new_callable=AsyncMock)
    @patch("app.whatsapp.app.utils.get_address_from_cep", new_callable=AsyncMock)
    async def test_cep_only_digits_reach_external_api(self, mock_get_cep, mock_history, mock_send):
        """Special chars in CEP input should be stripped — only digits reach the API."""
        from app.whatsapp import _handle_cep

        mock_get_cep.return_value = None

        mctx = self._make_mctx(partial_address={}, text_body="01001-000!@#$%")
        mctx.cart.state = CartState.AWAITING_CEP
        mctx.bot.latitude = None
        mctx.bot.longitude = None

        await _handle_cep(mctx)

        mock_get_cep.assert_awaited_once()
        called_cep = mock_get_cep.call_args[0][0]
        assert called_cep.isdigit(), f"Expected digits-only, got: {called_cep!r}"

    @pytest.mark.asyncio
    async def test_none_partial_address_resets_to_cep(self):
        """When partial_address is None, state should reset to AWAITING_CEP."""
        from app.whatsapp import _handle_number_complement

        mctx = self._make_mctx(partial_address=None)
        result = await _handle_number_complement(mctx)

        assert result is not None
        assert "CEP" in result
        assert mctx.cart.state == CartState.AWAITING_CEP
