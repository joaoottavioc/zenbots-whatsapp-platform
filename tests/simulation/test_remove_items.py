"""
Simulation tests: REMOVE/MODIFY items from cart.

5 scenarios testing the full pipeline for cart reduction via
_programmatic_cart_reduce. Each test first populates the cart via ADD,
then sends a MODIFY/REMOVE message and verifies the resulting cart state.

Covers: digit numbers, written PT-BR numbers (including compound like
"sessenta e dois"), single-item removal, multi-item removal, and
full removal (qty → 0).

Run with: docker compose exec backend pytest tests/simulation/test_remove_items.py -v
"""

import pytest
from tests.simulation.test_add_items import assert_cart_has


class TestRemoveItems:
    """5 REMOVE/MODIFY scenarios."""

    @pytest.mark.asyncio
    async def test_remove_single_item_digit(self, sim_context):
        """Remove a single item using digit number."""
        await sim_context.send("oi")
        await sim_context.send("10 picanha com bacon e 5 x-burger")

        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Picanha com bacon", 10), ("X-Burger", 5)])

        await sim_context.send("tira 4 picanha com bacon")

        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Picanha com bacon", 6), ("X-Burger", 5)])

    @pytest.mark.asyncio
    async def test_remove_single_item_written_compound_number(self, sim_context):
        """Remove a single item using compound written number (sessenta e dois)."""
        await sim_context.send("oi")
        await sim_context.send("80 mignon e 15 batata frita")

        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Mignon", 80), ("Batata frita", 15)])

        await sim_context.send("tira sessenta e dois mignon")

        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Mignon", 18), ("Batata frita", 15)])

    @pytest.mark.asyncio
    async def test_remove_multiple_items_digits(self, sim_context):
        """Remove several items at once using digit numbers."""
        await sim_context.send("oi")
        await sim_context.send("15 x-burger e 10 coca cola e 8 onion rings")

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [("X-Burger", 15), ("Coca-Cola 600ml", 10), ("Onion rings", 8)],
        )

        await sim_context.send("tira 5 x-burger 3 coca cola e 2 onion rings")

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [("X-Burger", 10), ("Coca-Cola 600ml", 7), ("Onion rings", 6)],
        )

    @pytest.mark.asyncio
    async def test_remove_multiple_items_written_numbers(self, sim_context):
        """Remove several items at once using written PT-BR numbers."""
        await sim_context.send("oi")
        await sim_context.send(
            "30 picanha com bacon e 25 alcatra acebolada e 20 batata frita"
        )

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [
                ("Picanha com bacon", 30),
                ("Alcatra acebolada", 25),
                ("Batata frita", 20),
            ],
        )

        await sim_context.send(
            "tira vinte picanha com bacon e quinze alcatra acebolada e dez batata frita"
        )

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [
                ("Picanha com bacon", 10),
                ("Alcatra acebolada", 10),
                ("Batata frita", 10),
            ],
        )

    @pytest.mark.asyncio
    async def test_remove_to_zero_and_partial_mixed(self, sim_context):
        """Full removal (qty → 0) + partial removal, mixing digit and written numbers."""
        await sim_context.send("oi")
        await sim_context.send("5 x-burger e 3 coca cola e 50 batata frita")

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [("X-Burger", 5), ("Coca-Cola 600ml", 3), ("Batata frita", 50)],
        )

        await sim_context.send(
            "tira cinco x-burger e 3 coca cola e quarenta e sete batata frita"
        )

        cart = await sim_context.get_cart_items()
        # X-Burger and Coca-Cola fully removed (qty → 0), Batata frita 50 → 3
        assert_cart_has(cart, [("Batata frita", 3)])
