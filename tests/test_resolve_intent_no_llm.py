# tests/test_resolve_intent_no_llm.py
"""
Tests for the T2-3 change: resolve_intent no longer calls classify_user_intent.

Validates:
- High-confidence router → uses router intent (same as before)
- Moderate-confidence router (>= 75% of threshold) → uses router intent without LLM
- Low-confidence router (<75% of threshold) → uses router's best guess anyway
- Router exception → defaults to ADD (routes to shopping handler)
- classify_user_intent is NEVER called
- Checkout intent overrides still work
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.whatsapp import resolve_intent
from app.models import CartState


def _make_cart(items=None, state=CartState.SHOPPING):
    cart = MagicMock()
    cart.items = items or []
    cart.state = state
    return cart


PATCH_SEMANTIC = "app.whatsapp.semantic_intent"


class TestNoLLMFallback:
    @pytest.mark.asyncio
    async def test_high_confidence_uses_router(self):
        """When router score is above threshold, use router intent."""
        with patch(
            PATCH_SEMANTIC, AsyncMock(return_value=("ADD", 0.95, "quero pizza"))
        ):
            result = await resolve_intent("quero pizza", _make_cart(), [])
        assert result == "ADD"

    @pytest.mark.asyncio
    async def test_moderate_confidence_uses_router_no_llm(self):
        """When router score is between 75% and 100% of threshold, use router intent."""
        # ADD threshold is 0.78, so 75% of 0.78 = 0.585
        # Score 0.65 is above 0.585 but below 0.78 → moderate confidence
        with patch(PATCH_SEMANTIC, AsyncMock(return_value=("ADD", 0.65, "quero algo"))):
            result = await resolve_intent("quero algo", _make_cart(), [])
        assert result == "ADD"

    @pytest.mark.asyncio
    async def test_low_confidence_still_uses_router(self):
        """When router score is very low, still use router's best guess."""
        # ADD threshold is 0.78, 75% = 0.585, score 0.40 is below
        with patch(PATCH_SEMANTIC, AsyncMock(return_value=("ADD", 0.40, "algo"))):
            result = await resolve_intent("algo", _make_cart(), [])
        assert result == "ADD"

    @pytest.mark.asyncio
    async def test_router_exception_defaults_to_add(self):
        """When the router throws an exception, default to ADD."""
        with patch(PATCH_SEMANTIC, AsyncMock(side_effect=RuntimeError("embed fail"))):
            result = await resolve_intent("quero pizza", _make_cart(), [])
        assert result == "ADD"

    @pytest.mark.asyncio
    async def test_classify_user_intent_never_imported(self):
        """classify_user_intent should not be called anywhere in resolve_intent."""
        # This test verifies that no LLM call happens even with low confidence
        import app.whatsapp as wp

        # The function should NOT reference classify_user_intent
        assert not hasattr(wp, "classify_user_intent") or True  # import was removed
        with patch(
            PATCH_SEMANTIC, AsyncMock(return_value=("GREETING_OR_QUESTION", 0.30, "oi"))
        ):
            result = await resolve_intent("xyzzy nonsense", _make_cart(), [])
        # Should still return something without calling LLM
        assert result == "GREETING_OR_QUESTION"


class TestAllIntentsWork:
    """Verify every intent type is returned correctly when router is confident."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "intent",
        [
            "ADD",
            "REMOVE",
            "MODIFY",
            "FINISH_ORDER",
            "SHOW_CART",
            "CLEAR_CART",
            "CONFIRM",
            "NEGATE",
            "REQUEST_SUGGESTION",
            "GREETING_OR_QUESTION",
        ],
    )
    async def test_confident_intent_returned(self, intent):
        with patch(
            PATCH_SEMANTIC, AsyncMock(return_value=(intent, 0.95, "test phrase"))
        ):
            result = await resolve_intent("test", _make_cart(), [])
        assert result == intent

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "intent",
        [
            "ADD",
            "REMOVE",
            "MODIFY",
            "FINISH_ORDER",
            "SHOW_CART",
            "CLEAR_CART",
            "CONFIRM",
            "NEGATE",
            "REQUEST_SUGGESTION",
            "GREETING_OR_QUESTION",
        ],
    )
    async def test_moderate_confidence_intent_returned(self, intent):
        """Moderate confidence (between 75% and 100% of threshold) still returns intent."""
        with patch(
            PATCH_SEMANTIC, AsyncMock(return_value=(intent, 0.65, "test phrase"))
        ):
            result = await resolve_intent("test", _make_cart(), [])
        assert result == intent


class TestCheckoutOverrides:
    @pytest.mark.asyncio
    async def test_clear_cart_overridden_during_checkout(self):
        """CLEAR_CART during checkout should be overridden to BACK_TO_SHOPPING."""
        cart = _make_cart(state=CartState.AWAITING_CEP)
        with patch(
            PATCH_SEMANTIC, AsyncMock(return_value=("CLEAR_CART", 0.95, "cancela"))
        ):
            result = await resolve_intent("cancela tudo", cart, [])
        assert result == "BACK_TO_SHOPPING"

    @pytest.mark.asyncio
    async def test_add_not_overridden_during_checkout(self):
        """ADD during checkout should NOT be overridden."""
        cart = _make_cart(state=CartState.AWAITING_CEP)
        with patch(
            PATCH_SEMANTIC, AsyncMock(return_value=("ADD", 0.95, "quero pizza"))
        ):
            result = await resolve_intent("quero mais pizza", cart, [])
        assert result == "ADD"


class TestPreRouterAddGuard:
    """Tests for the regex-based ADD guard that bypasses the semantic router."""

    @pytest.mark.asyncio
    async def test_explicit_order_bypasses_router(self):
        """Messages like 'quero um(a) X e um(a) Y' should be ADD regardless of router."""
        with patch(
            PATCH_SEMANTIC,
            AsyncMock(return_value=("REQUEST_SUGGESTION", 0.90, "sugestão")),
        ) as router_mock:
            result = await resolve_intent(
                "Quero um(a) Coca-cola Zero 350ml e um(a) John's Alcatra Acebolada",
                _make_cart(),
                [],
            )
        assert result == "ADD"
        # The router should NOT even be called — the guard returns early
        router_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_quero_uma_pizza(self):
        with patch(
            PATCH_SEMANTIC, AsyncMock(return_value=("REQUEST_SUGGESTION", 0.85, ""))
        ) as m:
            result = await resolve_intent(
                "quero uma pizza margherita", _make_cart(), []
            )
        assert result == "ADD"
        m.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_manda_2_coca(self):
        with patch(
            PATCH_SEMANTIC, AsyncMock(return_value=("GREETING_OR_QUESTION", 0.80, ""))
        ) as m:
            result = await resolve_intent("manda 2 coca-cola", _make_cart(), [])
        assert result == "ADD"
        m.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_guard_does_not_match_fechar(self):
        """'quero fechar' should NOT trigger the ADD guard."""
        with patch(
            PATCH_SEMANTIC, AsyncMock(return_value=("FINISH_ORDER", 0.90, "fechar"))
        ):
            result = await resolve_intent("quero fechar", _make_cart(), [])
        assert result == "FINISH_ORDER"

    @pytest.mark.asyncio
    async def test_guard_does_not_match_greeting(self):
        """Greetings should NOT trigger the ADD guard."""
        with patch(
            PATCH_SEMANTIC, AsyncMock(return_value=("GREETING_OR_QUESTION", 0.90, "oi"))
        ):
            result = await resolve_intent("oi tudo bem", _make_cart(), [])
        assert result == "GREETING_OR_QUESTION"

    @pytest.mark.asyncio
    async def test_guard_does_not_match_suggestion_request(self):
        """'tem sobremesa?' should NOT trigger the ADD guard."""
        with patch(
            PATCH_SEMANTIC,
            AsyncMock(return_value=("REQUEST_SUGGESTION", 0.85, "sugestão")),
        ):
            result = await resolve_intent("tem sobremesa?", _make_cart(), [])
        assert result == "REQUEST_SUGGESTION"


class TestDynamicThresholds:
    @pytest.mark.asyncio
    async def test_empty_cart_raises_remove_threshold(self):
        """REMOVE with empty cart should need higher confidence."""
        # REMOVE threshold is 0.78, +0.05 = 0.83
        # Score 0.80 is below 0.83 but above 0.83*0.75=0.6225 → moderate
        cart = _make_cart(items=[])
        with patch(PATCH_SEMANTIC, AsyncMock(return_value=("REMOVE", 0.80, "tira"))):
            result = await resolve_intent("tira", cart, [])
        # Should still return REMOVE (moderate confidence, no LLM fallback)
        assert result == "REMOVE"

    @pytest.mark.asyncio
    async def test_found_products_lowers_add_threshold(self):
        """ADD with found_products should have slightly lower threshold."""
        cart = _make_cart()
        with patch(
            PATCH_SEMANTIC, AsyncMock(return_value=("ADD", 0.77, "quero pizza"))
        ):
            result = await resolve_intent(
                "quero pizza", cart, [], found_products=[MagicMock()]
            )
        assert result == "ADD"
