# tests/test_pure_functions.py
"""
Tests for pure (no I/O) functions in app/whatsapp.py.

Covered:
- is_store_open        (manual toggle + schedule logic)
- get_next_opening_text
- _build_cart_summary_message
- is_likely_shopping_intent
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from app.whatsapp import (
    is_store_open,
    get_next_opening_text,
    _build_cart_summary_message,
    is_likely_shopping_intent,
)
from app.models import DeliveryMethod
from tests.conftest import make_cart_item


# ===========================================================================
# Helpers
# ===========================================================================


def _bot_with_schedule(schedule: dict, is_open: bool = True, tz: str = "UTC"):
    bot = MagicMock()
    bot.is_open = is_open
    bot.schedule = schedule
    bot.timezone = tz
    return bot


# ===========================================================================
# is_store_open
# ===========================================================================


class TestIsStoreOpen:
    def test_manual_close_overrides_schedule(self):
        """is_open=False must return False regardless of schedule."""
        bot = _bot_with_schedule(schedule=None, is_open=False)
        assert is_store_open(bot) is False

    def test_no_schedule_and_open_returns_true(self):
        """No schedule + manual open = always open."""
        bot = _bot_with_schedule(schedule=None, is_open=True)
        assert is_store_open(bot) is True

    def test_closed_day_returns_false(self):
        """Day marked inactive should return False."""
        # Use a fixed schedule where every day is inactive
        schedule = {
            k: {"active": False, "start": "09:00", "end": "22:00"}
            for k in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        }
        bot = _bot_with_schedule(schedule, is_open=True)
        assert is_store_open(bot) is False

    def test_active_day_inside_hours_returns_true(self):
        """Active day, current time is between start and end."""
        # We patch datetime.now to return a controlled time (Monday 12:00)
        schedule = {"mon": {"active": True, "start": "09:00", "end": "22:00"}}
        bot = _bot_with_schedule(schedule, is_open=True, tz="UTC")

        fixed_monday_noon = datetime(2024, 1, 1, 12, 0)  # 2024-01-01 is a Monday
        with patch("app.whatsapp.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_monday_noon
            assert is_store_open(bot) is True

    def test_active_day_before_opening_returns_false(self):
        """Active day, current time is before start."""
        schedule = {"mon": {"active": True, "start": "12:00", "end": "22:00"}}
        bot = _bot_with_schedule(schedule, is_open=True, tz="UTC")

        fixed_monday_early = datetime(2024, 1, 1, 8, 0)  # 08:00 < 12:00
        with patch("app.whatsapp.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_monday_early
            assert is_store_open(bot) is False

    def test_active_day_after_closing_returns_false(self):
        """Active day, current time is after end."""
        schedule = {"mon": {"active": True, "start": "09:00", "end": "22:00"}}
        bot = _bot_with_schedule(schedule, is_open=True, tz="UTC")

        fixed_monday_late = datetime(2024, 1, 1, 23, 0)  # 23:00 > 22:00
        with patch("app.whatsapp.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_monday_late
            assert is_store_open(bot) is False

    def test_broken_timezone_falls_back_to_open(self):
        """Invalid timezone in schedule should not crash — defaults to open."""
        schedule = {"mon": {"active": True, "start": "09:00", "end": "22:00"}}
        bot = _bot_with_schedule(schedule, is_open=True, tz="Invalid/Timezone")
        # Should not raise, and should default to True (fail-open)
        result = is_store_open(bot)
        assert result is True


# ===========================================================================
# get_next_opening_text
# ===========================================================================


class TestGetNextOpeningText:
    def test_no_schedule_returns_em_breve(self):
        bot = MagicMock()
        bot.schedule = None
        assert get_next_opening_text(bot) == "em breve"

    def test_returns_amanha_for_next_day(self):
        """If today is Monday and Tuesday is active, returns 'Amanhã às HH:MM'."""
        schedule = {
            "mon": {"active": False},
            "tue": {"active": True, "start": "10:00"},
        }
        bot = _bot_with_schedule(schedule, tz="UTC")

        fixed_monday = datetime(2024, 1, 1, 20, 0)  # Monday evening
        with patch("app.whatsapp.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_monday
            result = get_next_opening_text(bot)
        assert result == "Amanhã às 10:00"

    def test_returns_weekday_name_for_later_days(self):
        """If today is Monday and next open day is Wednesday, returns 'Quarta às HH:MM'."""
        schedule = {
            "mon": {"active": False},
            "tue": {"active": False},
            "wed": {"active": True, "start": "18:00"},
        }
        bot = _bot_with_schedule(schedule, tz="UTC")

        fixed_monday = datetime(2024, 1, 1, 20, 0)
        with patch("app.whatsapp.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_monday
            result = get_next_opening_text(bot)
        assert result == "Quarta às 18:00"

    def test_all_days_inactive_returns_em_breve(self):
        schedule = {
            k: {"active": False}
            for k in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        }
        bot = _bot_with_schedule(schedule, tz="UTC")
        fixed = datetime(2024, 1, 1, 20, 0)
        with patch("app.whatsapp.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            result = get_next_opening_text(bot)
        assert result == "em breve"


# ===========================================================================
# _build_cart_summary_message
# ===========================================================================


class TestBuildCartSummaryMessage:
    def _make_bot(self, delivery_fee=5.0):
        bot = MagicMock()
        bot.delivery_fee = delivery_fee
        return bot

    def _make_cart(self, items, delivery_method=None):
        cart = MagicMock()
        cart.items = items
        cart.delivery_method = delivery_method
        return cart

    def test_empty_cart_returns_empty_message(self):
        cart = self._make_cart([])
        result = _build_cart_summary_message(cart, self._make_bot())
        assert "vazio" in result.lower()

    def test_single_item_no_delivery_fee(self):
        items = [make_cart_item(1, "Pizza", 30.0, quantity=2)]
        cart = self._make_cart(items, delivery_method=DeliveryMethod.PICKUP)
        result = _build_cart_summary_message(cart, self._make_bot(delivery_fee=5.0))

        assert "Pizza" in result
        assert "60,00" in result or "60.00" in result
        # No delivery fee for pickup
        assert "Entrega:" not in result
        assert "*Total: R$ 60" in result

    def test_delivery_adds_fee(self):
        items = [make_cart_item(1, "Hamburguer", 25.0, quantity=1)]
        cart = self._make_cart(items, delivery_method=DeliveryMethod.DELIVERY)
        result = _build_cart_summary_message(cart, self._make_bot(delivery_fee=5.0))

        assert "Entrega: R$ 5.00" in result
        assert "*Total: R$ 30" in result

    def test_delivery_with_zero_fee_no_fee_line(self):
        items = [make_cart_item(1, "Açaí", 15.0, quantity=1)]
        cart = self._make_cart(items, delivery_method=DeliveryMethod.DELIVERY)
        result = _build_cart_summary_message(cart, self._make_bot(delivery_fee=0.0))

        assert "Entrega:" not in result
        assert "*Total: R$ 15" in result

    def test_item_with_notes_included_in_output(self):
        items = [make_cart_item(1, "Pizza", 30.0, quantity=1, notes="sem cebola")]
        cart = self._make_cart(items)
        result = _build_cart_summary_message(cart, self._make_bot())
        assert "sem cebola" in result

    def test_multiple_items_summed_correctly(self):
        items = [
            make_cart_item(1, "Pizza", 30.0, quantity=1),
            make_cart_item(2, "Coca-Cola", 8.0, quantity=2),
        ]
        cart = self._make_cart(items, delivery_method=DeliveryMethod.PICKUP)
        result = _build_cart_summary_message(cart, self._make_bot())
        # 30 + 16 = 46
        assert "46" in result

    def test_custom_emoji_appears_in_output(self):
        items = [make_cart_item(1, "X-Burguer", 20.0)]
        cart = self._make_cart(items)
        result = _build_cart_summary_message(cart, self._make_bot(), emoji="✅")
        assert "✅" in result


# ===========================================================================
# is_likely_shopping_intent
# ===========================================================================


class TestIsLikelyShoppingIntent:
    @pytest.mark.parametrize(
        "text",
        [
            "quero uma pizza",
            "adiciona mais uma coca",
            "tira o hamburguer",
            "remova o item",
            "ver meu carrinho",
            "limpar pedido",
            "cancelar tudo",
            "quanto custa o açaí",
        ],
    )
    def test_shopping_phrases_return_true(self, text):
        assert is_likely_shopping_intent(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "boa tarde",
            "ok",
            "sim",
            "não",
            "obrigado",
            "12345-678",
            "rua das flores",
        ],
    )
    def test_non_shopping_phrases_return_false(self, text):
        assert is_likely_shopping_intent(text) is False
