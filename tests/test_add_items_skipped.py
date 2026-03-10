"""Tests for L3: add_items_to_db_cart reports skipped unavailable products.

Verifies that when products are unavailable or belong to wrong bot,
their names are appended to the skipped_items list for user feedback.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.crud import add_items_to_db_cart


def _make_cart(cart_id=1, items=None):
    cart = MagicMock()
    cart.id = cart_id
    cart.items = items or []
    return cart


def _make_product(product_id, name, is_available=True, bot_id=1):
    product = MagicMock()
    product.id = product_id
    product.name = name
    product.is_available = is_available
    product.bot_id = bot_id
    return product


@pytest.mark.asyncio
async def test_skipped_items_populated_for_unavailable_product():
    """Unavailable products should be added to skipped_items with their name."""
    cart = _make_cart()
    unavailable_product = _make_product(10, "Pizza Margherita", is_available=False)

    session = AsyncMock()
    session.get = AsyncMock(
        side_effect=lambda model, id, **kw: (
            cart if model.__name__ == "ShoppingCart" else unavailable_product
        )
    )
    session.flush = AsyncMock()

    skipped: list[str] = []
    await add_items_to_db_cart(
        session,
        cart.id,
        [{"product_id": 10, "quantity": 1}],
        skipped_items=skipped,
    )

    assert "Pizza Margherita" in skipped


@pytest.mark.asyncio
async def test_skipped_items_populated_for_nonexistent_product():
    """Non-existent products should use a fallback name in skipped_items."""
    cart = _make_cart()

    session = AsyncMock()
    session.get = AsyncMock(
        side_effect=lambda model, id, **kw: (
            cart if model.__name__ == "ShoppingCart" else None
        )
    )
    session.flush = AsyncMock()

    skipped: list[str] = []
    await add_items_to_db_cart(
        session,
        cart.id,
        [{"product_id": 999, "quantity": 1}],
        skipped_items=skipped,
    )

    assert len(skipped) == 1
    assert "999" in skipped[0]


@pytest.mark.asyncio
async def test_skipped_items_populated_for_wrong_bot():
    """Products belonging to a different bot should be in skipped_items."""
    cart = _make_cart()
    wrong_bot_product = _make_product(10, "Coxinha", bot_id=99)

    session = AsyncMock()
    session.get = AsyncMock(
        side_effect=lambda model, id, **kw: (
            cart if model.__name__ == "ShoppingCart" else wrong_bot_product
        )
    )
    session.flush = AsyncMock()

    skipped: list[str] = []
    await add_items_to_db_cart(
        session,
        cart.id,
        [{"product_id": 10, "quantity": 1}],
        bot_id=1,
        skipped_items=skipped,
    )

    assert "Coxinha" in skipped


@pytest.mark.asyncio
async def test_skipped_items_none_by_default_no_error():
    """When skipped_items is not passed, function still works (backward compat)."""
    cart = _make_cart()
    unavailable = _make_product(10, "Suco", is_available=False)

    session = AsyncMock()
    session.get = AsyncMock(
        side_effect=lambda model, id, **kw: (
            cart if model.__name__ == "ShoppingCart" else unavailable
        )
    )
    session.flush = AsyncMock()

    # Should not raise — skipped_items defaults to None
    result = await add_items_to_db_cart(
        session,
        cart.id,
        [{"product_id": 10, "quantity": 1}],
    )

    assert result is cart


@pytest.mark.asyncio
async def test_available_products_not_in_skipped():
    """Available products should NOT appear in skipped_items."""
    cart = _make_cart()
    good_product = _make_product(10, "Hamburguer", is_available=True, bot_id=1)

    session = AsyncMock()
    session.get = AsyncMock(
        side_effect=lambda model, id, **kw: (
            cart if model.__name__ == "ShoppingCart" else good_product
        )
    )
    session.flush = AsyncMock()
    session.add = MagicMock()

    skipped: list[str] = []
    await add_items_to_db_cart(
        session,
        cart.id,
        [{"product_id": 10, "quantity": 1}],
        bot_id=1,
        skipped_items=skipped,
    )

    assert skipped == []
