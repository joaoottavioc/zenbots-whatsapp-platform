"""
Simulation tests: ADD items to cart.

5 scenarios testing the full pipeline (process_whatsapp_message) with
a fictional restaurant menu. No mocks on the core logic — only
send_whatsapp_message is intercepted to capture responses.

Run with: docker compose exec backend pytest tests/simulation/test_add_items.py -v
"""

import pytest


def assert_cart_has(cart_items, expected, strict=True):
    """Assert cart contains expected items with correct quantities.

    Args:
        cart_items: list of (product_name, quantity) from sim_context.get_cart_items()
        expected: list of (name_substring, quantity) to match
        strict: if True, cart must have exactly len(expected) items
    """
    if strict:
        assert len(cart_items) == len(expected), (
            f"Expected {len(expected)} items but cart has {len(cart_items)}: {cart_items}"
        )

    _used = set()
    for name_part, qty in expected:
        # Prefer exact match, then substring match (avoids "Mignon" matching "Mignon ao molho cheddar")
        best_idx = None
        best_exact = False
        for i, (n, q) in enumerate(cart_items):
            if i in _used:
                continue
            if n.lower() == name_part.lower():
                best_idx = i
                best_exact = True
                break
            if name_part.lower() in n.lower() and not best_exact:
                if best_idx is None:
                    best_idx = i

        assert best_idx is not None, (
            f"No cart item matching '{name_part}'. Cart: {cart_items}"
        )
        _used.add(best_idx)
        actual_name, actual_qty = cart_items[best_idx]
        assert actual_qty == qty, (
            f"'{actual_name}' expected qty={qty} but got qty={actual_qty}. Cart: {cart_items}"
        )


class TestAddItems:
    """5 ADD scenarios covering single items, multi-items, typos, and compound numbers."""

    @pytest.mark.asyncio
    async def test_single_item_basic(self, sim_context):
        """Single item with basic verb+qty: 'quero uma picanha com bacon'."""
        await sim_context.send("oi")
        await sim_context.send("quero uma picanha com bacon")

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [
                ("Picanha com bacon", 1),
            ],
        )

    @pytest.mark.asyncio
    async def test_multi_item_with_typos(self, sim_context):
        """3 items with number typos and product name typos."""
        await sim_context.send("oi")

        await sim_context.send(
            "quatro picanha com baco doze mignon com cheddar e cuarenta e dois mignon"
        )

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [
                ("Picanha com bacon", 4),
                ("Mignon ao molho cheddar", 12),
                ("Mignon", 42),
            ],
        )

    @pytest.mark.asyncio
    async def test_number_typos_and_hyphenated_names(self, sim_context):
        """Number typos (desesseis, corenta, trez) + hyphenated product name."""
        await sim_context.send("oi")

        await sim_context.send("desesseis x-burger e corenta e trez coca cola")

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [
                ("X-Burger", 16),
                ("Coca-Cola 600ml", 43),
            ],
        )

    @pytest.mark.asyncio
    async def test_large_compound_numbers(self, sim_context):
        """Large compound numbers: 'vinte e sete' + 'cento e vinte'."""
        await sim_context.send("oi")

        await sim_context.send(
            "vinte e sete alcatra acebolada e cento e vinte batata frita"
        )

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [
                ("Alcatra acebolada", 27),
                ("Batata frita", 120),
            ],
        )

    @pytest.mark.asyncio
    async def test_three_items_with_product_and_number_typos(self, sim_context):
        """3 items combining product name typos + number typos."""
        await sim_context.send("oi")

        await sim_context.send(
            "tres picanha com baco e cinco mignom com chedar e oitu coca"
        )

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [
                ("Picanha com bacon", 3),
                ("Mignon ao molho cheddar", 5),
                ("Coca-Cola 600ml", 8),
            ],
        )
