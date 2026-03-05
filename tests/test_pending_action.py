"""Tests for pending_action timezone handling and error handling."""

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.pending_action import _to_naive_utc, has_valid_pending, expire_if_needed


class TestToNaiveUtc:
    def test_naive_datetime_unchanged(self):
        dt = datetime(2026, 1, 1, 12, 0, 0)
        result = _to_naive_utc(dt)
        assert result == dt
        assert result.tzinfo is None

    def test_utc_aware_datetime_stripped(self):
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = _to_naive_utc(dt)
        assert result == datetime(2026, 1, 1, 12, 0, 0)
        assert result.tzinfo is None

    def test_non_utc_aware_datetime_converted(self):
        # UTC+3
        tz_plus3 = timezone(timedelta(hours=3))
        dt = datetime(2026, 1, 1, 15, 0, 0, tzinfo=tz_plus3)
        result = _to_naive_utc(dt)
        # 15:00 UTC+3 = 12:00 UTC
        assert result == datetime(2026, 1, 1, 12, 0, 0)
        assert result.tzinfo is None


class TestHasValidPending:
    @patch("app.pending_action.utcnow")
    def test_valid_with_utc_aware_expiry(self, mock_utcnow):
        mock_utcnow.return_value = datetime(2026, 1, 1, 12, 0, 0)
        cart = MagicMock()
        cart.pending_action_tool = "add_items_to_cart"
        # Expiry is in the future, UTC-aware
        cart.pending_action_expires_at = datetime(
            2026, 1, 1, 12, 5, 0, tzinfo=timezone.utc
        )
        assert has_valid_pending(cart) is True

    @patch("app.pending_action.utcnow")
    def test_expired_with_non_utc_aware(self, mock_utcnow):
        mock_utcnow.return_value = datetime(2026, 1, 1, 12, 0, 0)
        cart = MagicMock()
        cart.pending_action_tool = "add_items_to_cart"
        # UTC+3 => 09:00 UTC+3 = 06:00 UTC, which is before 12:00 UTC
        tz_plus3 = timezone(timedelta(hours=3))
        cart.pending_action_expires_at = datetime(2026, 1, 1, 9, 0, 0, tzinfo=tz_plus3)
        assert has_valid_pending(cart) is False

    @patch("app.pending_action.utcnow")
    def test_valid_with_naive_expiry(self, mock_utcnow):
        mock_utcnow.return_value = datetime(2026, 1, 1, 12, 0, 0)
        cart = MagicMock()
        cart.pending_action_tool = "add_items_to_cart"
        cart.pending_action_expires_at = datetime(2026, 1, 1, 12, 5, 0)
        assert has_valid_pending(cart) is True


class TestExpireIfNeeded:
    @patch("app.pending_action.utcnow")
    def test_clears_expired_utc_aware(self, mock_utcnow):
        mock_utcnow.return_value = datetime(2026, 1, 1, 12, 0, 0)
        cart = MagicMock()
        cart.pending_action_tool = "test"
        cart.pending_action_expires_at = datetime(
            2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc
        )
        expire_if_needed(cart)
        assert cart.pending_action_tool is None

    @patch("app.pending_action.utcnow")
    def test_keeps_valid_pending(self, mock_utcnow):
        mock_utcnow.return_value = datetime(2026, 1, 1, 12, 0, 0)
        cart = MagicMock()
        cart.pending_action_tool = "test"
        cart.pending_action_expires_at = datetime(
            2026, 1, 1, 13, 0, 0, tzinfo=timezone.utc
        )
        expire_if_needed(cart)
        # Should NOT have been cleared
        assert cart.pending_action_tool == "test"


class TestCorruptedPendingActionArgs:
    @pytest.mark.asyncio
    async def test_corrupted_json_logs_and_returns_error(self, caplog):
        """Corrupted pending_action_args should log a warning and return user-friendly message."""
        from app.whatsapp import _execute_pending_action

        cart = MagicMock()
        cart.id = 99
        cart.pending_action_tool = "add_items_to_cart"
        cart.pending_action_args = "{invalid"
        cart.pending_action_question = "Adicionar pizza?"

        bot = MagicMock()
        bot.id = 1

        session = AsyncMock()

        with caplog.at_level(logging.WARNING, logger="app.whatsapp"):
            result = await _execute_pending_action(session, cart, bot)

        assert "Houve um erro ao processar sua confirmação" in result
        assert "Corrupted pending_action_args" in caplog.text
