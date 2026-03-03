# tests/test_resolve_intent.py
"""
Tests for the lazy LLM fallback in resolve_intent.

The semantic router runs first. If its confidence is above threshold,
the LLM is never called. Otherwise, the LLM is used as fallback.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.whatsapp import resolve_intent


def _make_cart(items=None):
    cart = MagicMock()
    cart.items = items or []
    return cart


PATCH_SEMANTIC = "app.whatsapp.semantic_intent"
PATCH_LLM = "app.whatsapp.classify_user_intent"


class TestResolveIntent:
    @pytest.mark.asyncio
    async def test_router_confident_skips_llm(self):
        """When the semantic router score is above threshold, LLM is NOT called."""
        with (
            patch(
                PATCH_SEMANTIC, AsyncMock(return_value=("ADD", 0.95, "quero pizza"))
            ) as router_mock,
            patch(
                PATCH_LLM, AsyncMock(return_value="GREETING_OR_QUESTION")
            ) as llm_mock,
        ):
            result = await resolve_intent("quero pizza", _make_cart(), [])

        assert result == "ADD"
        router_mock.assert_awaited_once()
        llm_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_router_low_confidence_falls_back_to_llm(self):
        """When the semantic router score is below threshold, LLM is called and its result used."""
        with (
            patch(
                PATCH_SEMANTIC, AsyncMock(return_value=("ADD", 0.50, "quero algo"))
            ) as router_mock,
            patch(
                PATCH_LLM, AsyncMock(return_value="GREETING_OR_QUESTION")
            ) as llm_mock,
        ):
            result = await resolve_intent("quero algo", _make_cart(), [])

        assert result == "GREETING_OR_QUESTION"
        router_mock.assert_awaited_once()
        llm_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_router_exception_falls_back_to_llm(self):
        """When the semantic router throws, LLM is called as fallback."""
        with (
            patch(
                PATCH_SEMANTIC, AsyncMock(side_effect=RuntimeError("embed fail"))
            ) as router_mock,
            patch(PATCH_LLM, AsyncMock(return_value="ADD")) as llm_mock,
        ):
            result = await resolve_intent("quero pizza", _make_cart(), [])

        assert result == "ADD"
        router_mock.assert_awaited_once()
        llm_mock.assert_awaited_once()
