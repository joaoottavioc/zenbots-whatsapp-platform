# tests/test_order_status_filter.py
"""Tests for OrderStatus enum validation in list_bot_orders route."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models import OrderStatus


# Ensure fitz (PyMuPDF) doesn't block import of bot_routes in test env
if "fitz" not in sys.modules:
    sys.modules["fitz"] = MagicMock()


from app.bot_routes import list_bot_orders  # noqa: E402


class TestOrderStatusFilter:
    """Tests for status filter validation in the orders endpoint."""

    @pytest.mark.asyncio
    async def test_valid_status_passes_enum_to_crud(self):
        """A valid status string is converted to OrderStatus and forwarded to CRUD."""
        mock_session = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = 1

        mock_bot = MagicMock()
        mock_bot.user_id = 1

        with (
            patch("app.bot_routes.crud.get_bot_by_id", new=AsyncMock(return_value=mock_bot)),
            patch(
                "app.bot_routes.crud.list_orders_by_bot", new=AsyncMock(return_value=[])
            ) as mock_list,
        ):
            result = await list_bot_orders(
                bot_id=1, status="paid", session=mock_session, current_user=mock_user
            )

            mock_list.assert_awaited_once()
            call_kwargs = mock_list.call_args
            assert call_kwargs[1]["status_filter"] == OrderStatus.PAID
            assert call_kwargs[1]["bot_id"] == 1
            assert result == []

    @pytest.mark.asyncio
    async def test_none_status_passes_none_to_crud(self):
        """No status filter passes None (returns all orders)."""
        mock_session = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = 1

        mock_bot = MagicMock()
        mock_bot.user_id = 1

        with (
            patch("app.bot_routes.crud.get_bot_by_id", new=AsyncMock(return_value=mock_bot)),
            patch(
                "app.bot_routes.crud.list_orders_by_bot", new=AsyncMock(return_value=[])
            ) as mock_list,
        ):
            result = await list_bot_orders(
                bot_id=1, status=None, session=mock_session, current_user=mock_user
            )

            mock_list.assert_awaited_once()
            call_kwargs = mock_list.call_args
            assert call_kwargs[1]["status_filter"] is None

    @pytest.mark.asyncio
    async def test_invalid_status_raises_422(self):
        """An invalid status string raises HTTP 422."""
        mock_session = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = 1

        mock_bot = MagicMock()
        mock_bot.user_id = 1

        with patch("app.bot_routes.crud.get_bot_by_id", new=AsyncMock(return_value=mock_bot)):
            with pytest.raises(HTTPException) as exc_info:
                await list_bot_orders(
                    bot_id=1,
                    status="INVALID_STATUS",
                    session=mock_session,
                    current_user=mock_user,
                )

            assert exc_info.value.status_code == 422
            assert "INVALID_STATUS" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_case_insensitive_status(self):
        """Status filter is case-insensitive (e.g., 'PAID' → OrderStatus.PAID)."""
        mock_session = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = 1

        mock_bot = MagicMock()
        mock_bot.user_id = 1

        with (
            patch("app.bot_routes.crud.get_bot_by_id", new=AsyncMock(return_value=mock_bot)),
            patch(
                "app.bot_routes.crud.list_orders_by_bot", new=AsyncMock(return_value=[])
            ) as mock_list,
        ):
            await list_bot_orders(
                bot_id=1, status="PAID", session=mock_session, current_user=mock_user
            )

            mock_list.assert_awaited_once()
            call_kwargs = mock_list.call_args
            assert call_kwargs[1]["status_filter"] == OrderStatus.PAID
