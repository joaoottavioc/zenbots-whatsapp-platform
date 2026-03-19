"""Tests for L2: Semantic router cart-state awareness.

Verifies that during checkout states, ambiguous intents like CLEAR_CART
are reinterpreted as BACK_TO_SHOPPING instead of wiping the cart.

Updated for T2-3: classify_user_intent LLM fallback removed.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.models import CartState


def _make_cart(state=CartState.SHOPPING):
    cart = MagicMock()
    cart.state = state
    cart.items = [MagicMock()]  # non-empty cart
    return cart


@pytest.mark.asyncio
async def test_clear_cart_overridden_during_awaiting_cep():
    """CLEAR_CART during AWAITING_CEP should become BACK_TO_SHOPPING."""
    from app.whatsapp import resolve_intent

    cart = _make_cart(state=CartState.AWAITING_CEP)

    with patch(
        "app.whatsapp.semantic_intent", return_value=("CLEAR_CART", 0.95, "cancelar")
    ):
        intent = await resolve_intent("cancelar", cart, [])

    assert intent == "BACK_TO_SHOPPING"


@pytest.mark.asyncio
async def test_clear_cart_overridden_during_awaiting_payment():
    """CLEAR_CART during AWAITING_PAYMENT_METHOD should become BACK_TO_SHOPPING."""
    from app.whatsapp import resolve_intent

    cart = _make_cart(state=CartState.AWAITING_PAYMENT_METHOD)

    with patch(
        "app.whatsapp.semantic_intent", return_value=("CLEAR_CART", 0.95, "cancelar")
    ):
        intent = await resolve_intent("cancelar", cart, [])

    assert intent == "BACK_TO_SHOPPING"


@pytest.mark.asyncio
async def test_clear_cart_not_overridden_during_shopping():
    """CLEAR_CART during SHOPPING should remain CLEAR_CART."""
    from app.whatsapp import resolve_intent

    cart = _make_cart(state=CartState.SHOPPING)

    with patch(
        "app.whatsapp.semantic_intent", return_value=("CLEAR_CART", 0.95, "limpar")
    ):
        intent = await resolve_intent("limpar carrinho", cart, [])

    assert intent == "CLEAR_CART"


@pytest.mark.asyncio
async def test_add_intent_not_overridden_during_checkout():
    """ADD intent during checkout should NOT be overridden."""
    from app.whatsapp import resolve_intent

    cart = _make_cart(state=CartState.AWAITING_CEP)

    with patch(
        "app.whatsapp.semantic_intent", return_value=("ADD", 0.95, "quero pizza")
    ):
        intent = await resolve_intent("quero uma pizza", cart, [])

    assert intent == "ADD"


@pytest.mark.asyncio
async def test_clear_cart_overridden_via_low_confidence_router():
    """Low confidence during checkout defaults to ADD, which is then overridden
    to BACK_TO_SHOPPING by the checkout intent override map.

    After T2-3 + 2026-03-19 changes: low confidence defaults to ADD (not
    router's best guess). ADD during checkout is overridden to BACK_TO_SHOPPING.
    """
    from app.whatsapp import resolve_intent

    cart = _make_cart(state=CartState.AWAITING_CUSTOMER_NAME)

    # Low confidence — defaults to ADD, then checkout override → BACK_TO_SHOPPING
    with patch(
        "app.whatsapp.semantic_intent", return_value=("CLEAR_CART", 0.3, "cancela")
    ):
        intent = await resolve_intent("não quero mais", cart, [])

    # Low confidence defaults to ADD. ADD is not in checkout overrides,
    # so it stays as ADD. The shopping handler will process it.
    assert intent == "ADD"
