"""
Simulation tests: transition from suggestions to order.

Tests that when suggestions are active (last_suggestions set), the customer
can still place a full order and the bot processes it correctly instead of
re-showing suggestions. Covers multi-item orders, unavailable items, and
the "manda ver" pattern.

Run with: docker compose exec backend pytest tests/simulation/test_suggestion_to_order.py -v
"""

import pytest
from tests.simulation.test_add_items import assert_cart_has
from tests.simulation.test_unavailable_items import (
    assert_response_contains_em_falta,
)


def assert_cart_empty(cart_items):
    assert len(cart_items) == 0, (
        f"Cart should be empty but has {len(cart_items)} items: {cart_items}"
    )


class TestSuggestionToOrder:
    """Tests for placing orders while suggestions are active."""

    @pytest.mark.asyncio
    async def test_order_after_suggestions_with_manda_ver(self, sim_context):
        """Customer asks for suggestions then orders with 'manda ver' + written numbers."""
        await sim_context.send("oi")
        await sim_context.send("o que tem de bom ai?")

        # Suggestions now active. Place a real order.
        await sim_context.send("manda ver cinco picanha com bacon e tres mignon")

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [
                ("Picanha com bacon", 5),
                ("Mignon", 3),
            ],
        )

    @pytest.mark.asyncio
    async def test_order_after_suggestions_with_unavailable(self, sim_context):
        """Customer asks for suggestions then orders mix of available + unavailable."""
        await sim_context.send("oi")
        await sim_context.send("primeira vez aqui, o que recomenda?")

        # Suggestions active. Order includes unavailable X-Egg.
        response = await sim_context.send(
            "quero 10 x-burger e 4 x-egg e 2 batata frita"
        )

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [
                ("X-Burger", 10),
                ("Batata frita", 2),
            ],
        )
        assert_response_contains_em_falta(response, "X-Egg")

    @pytest.mark.asyncio
    async def test_order_after_suggestions_with_digits(self, sim_context):
        """Customer asks for suggestions then orders with digit numbers."""
        await sim_context.send("oi")
        await sim_context.send("tem dicas de pedidos?")

        # Suggestions active. Order with digit numbers.
        await sim_context.send("8 alcatra acebolada e 3 coca cola e 2 onion rings")

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [
                ("Alcatra acebolada", 8),
                ("Coca-Cola 600ml", 3),
                ("Onion rings", 2),
            ],
        )

    @pytest.mark.asyncio
    async def test_indecisive_then_order(self, sim_context):
        """Customer is indecisive (re-shown suggestions) then places order."""
        await sim_context.send("oi")
        await sim_context.send("o que tem de bom ai?")

        # Indecisive message — should re-show suggestions
        await sim_context.send("hmm nao sei")

        # Now place a real order
        await sim_context.send("quero 4 picanha com catupiry e 6 batata frita")

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [
                ("Picanha com catupiry", 4),
                ("Batata frita", 6),
            ],
        )

    @pytest.mark.asyncio
    async def test_suggestion_request_never_adds_items(self, sim_context):
        """Multiple suggestion requests in a row never mutate the cart."""
        await sim_context.send("oi")
        await sim_context.send("o que recomenda?")
        await sim_context.send("tem mais opções?")
        await sim_context.send("me manda sugestões")

        cart = await sim_context.get_cart_items()
        assert_cart_empty(cart)
