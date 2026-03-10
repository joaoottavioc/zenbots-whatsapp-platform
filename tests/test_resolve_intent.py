# tests/test_resolve_intent.py
"""
Tests for resolve_intent.

After T2-3, the semantic router is the sole intent classifier.
classify_user_intent (LLM) is no longer called.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.whatsapp import resolve_intent


def _make_cart(items=None):
    cart = MagicMock()
    cart.items = items or []
    return cart


PATCH_SEMANTIC = "app.whatsapp.semantic_intent"


class TestResolveIntent:
    @pytest.mark.asyncio
    async def test_router_confident_returns_intent(self):
        """When the semantic router score is above threshold, use router intent."""
        with patch(
            PATCH_SEMANTIC, AsyncMock(return_value=("ADD", 0.95, "quero pizza"))
        ) as router_mock:
            result = await resolve_intent("quero pizza", _make_cart(), [])

        assert result == "ADD"
        router_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_router_low_confidence_still_uses_router(self):
        """After T2-3: low confidence still uses router's best guess (no LLM)."""
        with patch(
            PATCH_SEMANTIC, AsyncMock(return_value=("ADD", 0.50, "quero algo"))
        ) as router_mock:
            result = await resolve_intent("quero algo", _make_cart(), [])

        # T2-3: uses router's best guess instead of LLM fallback
        assert result == "ADD"
        router_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_router_exception_defaults_to_add(self):
        """When the semantic router throws, default to ADD (no LLM fallback)."""
        with patch(
            PATCH_SEMANTIC, AsyncMock(side_effect=RuntimeError("embed fail"))
        ) as router_mock:
            result = await resolve_intent("quero pizza", _make_cart(), [])

        assert result == "ADD"
        router_mock.assert_awaited_once()
