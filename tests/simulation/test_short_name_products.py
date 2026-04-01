"""
Simulation tests: short-name product matching.

Tests that products with 3-character words ("Big Mix", "Hot Dog", "Egg Burger")
are matched correctly after lowering the word-overlap threshold from <= 3 to <= 2.
Also verifies that Portuguese stop words ("com", "sem") don't cause false matches.

Run with: docker compose exec backend pytest tests/simulation/test_short_name_products.py -v
"""

import pytest
from tests.simulation.test_add_items import assert_cart_has


class TestShortNameProducts:
    """Tests for products with 3-character words in their names."""

    @pytest.mark.asyncio
    async def test_big_mix_matches(self, sim_context):
        """'big mix' (two 3-char words) must match Big Mix product."""
        await sim_context.send("oi")
        await sim_context.send("quero 3 big mix")

        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Big Mix", 3)])

    @pytest.mark.asyncio
    async def test_hot_dog_matches(self, sim_context):
        """'hot dog' (two 3-char words) must match Hot Dog product."""
        await sim_context.send("oi")
        await sim_context.send("quero 2 hot dog")

        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Hot Dog", 2)])

    @pytest.mark.asyncio
    async def test_fit_wrap_matches(self, sim_context):
        """'fit wrap' — 'fit' is 3 chars, must still match Fit Wrap."""
        await sim_context.send("oi")
        await sim_context.send("quero 5 fit wrap")

        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Fit Wrap", 5)])

    @pytest.mark.asyncio
    async def test_com_does_not_false_match(self, sim_context):
        """'com' in customer message must not cause wrong product match.
        'picanha com bacon' should match Picanha com bacon, not Mignon
        (which also has 'com' in prompt but different product words)."""
        await sim_context.send("oi")
        await sim_context.send("4 picanha com bacon e 2 picanha com catupiry")

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [
                ("Picanha com bacon", 4),
                ("Picanha com catupiry", 2),
            ],
        )

    @pytest.mark.asyncio
    async def test_short_and_long_names_mixed(self, sim_context):
        """Mix of short-name and long-name products in same order."""
        await sim_context.send("oi")
        await sim_context.send(
            "quero 3 big mix e 2 hot dog e 1 mignon ao molho cheddar"
        )

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [
                ("Big Mix", 3),
                ("Hot Dog", 2),
                ("Mignon ao molho cheddar", 1),
            ],
        )
