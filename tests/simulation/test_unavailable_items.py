"""
Simulation tests: unavailable items (em falta) flow.

5 scenarios testing that unavailable products trigger an em falta message
while available products in the same order are still added to the cart.

Unavailable products in Sabor da Serra menu:
- X-Egg (Lanches Clássicos, R$28.00) — no close available match

Note: "Mignon com cebola" (unavailable) fuzzy-matches "Mignon ao molho cheddar"
(available) due to shared "mignon" stem — so we use X-Egg exclusively for
em falta tests to avoid false matches. Improving the fuzzy matcher to distinguish
these is tracked separately.

Run with: docker compose exec backend pytest tests/simulation/test_unavailable_items.py -v
"""

import pytest
from tests.simulation.test_add_items import assert_cart_has


def assert_response_contains_em_falta(response: str, *product_names: str):
    """Assert the response mentions em falta for the given product names."""
    assert response is not None, "No response received"
    lower = response.lower()
    assert "em falta" in lower or "indisponível" in lower, (
        f"Response missing em falta message. Got: {response[:300]}"
    )
    for name in product_names:
        assert name.lower() in lower, (
            f"Response should mention '{name}' as unavailable. Got: {response[:300]}"
        )


class TestUnavailableItems:
    """5 scenarios covering unavailable product detection in mixed orders."""

    @pytest.mark.asyncio
    async def test_one_available_one_unavailable(self, sim_context):
        """1 available + 1 unavailable: cart gets the available, em falta for the other."""
        await sim_context.send("oi")
        response = await sim_context.send("quero 2 picanha com bacon e 3 x-egg")

        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Picanha com bacon", 2)])
        assert_response_contains_em_falta(response, "X-Egg")

    @pytest.mark.asyncio
    async def test_multiple_available_one_unavailable(self, sim_context):
        """2 available + 1 unavailable: both available added, em falta for one."""
        await sim_context.send("oi")
        response = await sim_context.send("5 x-burger e 3 batata frita e 2 x egg")

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [
                ("X-Burger", 5),
                ("Batata frita", 3),
            ],
        )
        assert_response_contains_em_falta(response, "X-Egg")

    @pytest.mark.asyncio
    async def test_one_available_one_unavailable_with_typo(self, sim_context):
        """Available item with typo + 1 unavailable: typo resolves, em falta works."""
        await sim_context.send("oi")
        response = await sim_context.send("7 mignon molho cheddar e 2 x egg")

        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Mignon ao molho cheddar", 7)])
        assert_response_contains_em_falta(response, "X-Egg")

    @pytest.mark.asyncio
    async def test_many_available_one_unavailable(self, sim_context):
        """3 available + 1 unavailable: all available added, em falta for one."""
        await sim_context.send("oi")
        response = await sim_context.send(
            "3 alcatra acebolada e 10 onion rings e 2 guarana e 1 x egg"
        )

        cart = await sim_context.get_cart_items()
        assert_cart_has(
            cart,
            [
                ("Alcatra acebolada", 3),
                ("Onion rings", 10),
                ("Guaraná Antarctica", 2),
            ],
        )
        assert_response_contains_em_falta(response, "X-Egg")

    @pytest.mark.asyncio
    async def test_large_qty_available_and_unavailable(self, sim_context):
        """Large quantities for both available and unavailable items."""
        await sim_context.send("oi")
        response = await sim_context.send("vinte e cinco coca cola e quinze x-egg")

        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Coca-Cola 600ml", 25)])
        assert_response_contains_em_falta(response, "X-Egg")
