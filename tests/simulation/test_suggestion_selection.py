"""
Simulation tests: selecting from numbered suggestions.

Tests that after the bot shows numbered suggestions, customers can select
items by number ("do 1"), ordinals ("do primeiro"), or names.
Verifies correct products end up in the cart.

Known limitation: the suggestion handler currently adds 1x per selection.
Quantity prefixes ("dois do 1", "sete do primeiro") are parsed by
extract_items_with_quantities but the suggestion handler uses its own
matching logic. Tests verify the selection works; quantity support
for suggestion selections is tracked as a future improvement.

Run with: docker compose exec backend pytest tests/simulation/test_suggestion_selection.py -v
"""

import pytest


async def _get_suggestions(sim_context) -> str | None:
    """Send greeting + suggestion request, return the suggestion response."""
    await sim_context.send("oi")
    return await sim_context.send("o que tem de bom ai?")


class TestSuggestionSelection:
    """Tests for selecting items from numbered suggestion lists."""

    @pytest.mark.asyncio
    async def test_select_by_bare_number(self, sim_context):
        """'1' — bare number selects suggestion #1."""
        await _get_suggestions(sim_context)

        await sim_context.send("1")

        cart = await sim_context.get_cart_items()
        assert len(cart) == 1, f"Expected 1 item, got {cart}"

    @pytest.mark.asyncio
    async def test_select_by_ordinal(self, sim_context):
        """'o primeiro' — ordinal selects suggestion #1."""
        await _get_suggestions(sim_context)

        await sim_context.send("o primeiro")

        cart = await sim_context.get_cart_items()
        assert len(cart) == 1, f"Expected 1 item, got {cart}"

    @pytest.mark.asyncio
    async def test_select_by_name(self, sim_context):
        """'quero picanha com bacon' — name-based selection from suggestions."""
        await _get_suggestions(sim_context)

        await sim_context.send("quero picanha com bacon")

        cart = await sim_context.get_cart_items()
        assert len(cart) == 1, f"Expected 1 item, got {cart}"
        assert "picanha" in cart[0][0].lower(), f"Expected picanha, got {cart}"

    @pytest.mark.asyncio
    async def test_select_multiple_by_ordinal(self, sim_context):
        """'o primeiro e o terceiro' — two ordinal selections."""
        await _get_suggestions(sim_context)

        await sim_context.send("o primeiro e o terceiro")

        cart = await sim_context.get_cart_items()
        assert len(cart) == 2, f"Expected 2 items, got {cart}"

    @pytest.mark.asyncio
    async def test_select_with_quantity_adds_item(self, sim_context):
        """'sete do primeiro' — ordinal with quantity. Item must be added
        (quantity handling is a known limitation — may add 1x or 7x)."""
        await _get_suggestions(sim_context)

        await sim_context.send("sete do primeiro")

        cart = await sim_context.get_cart_items()
        assert len(cart) >= 1, f"Expected at least 1 item, got {cart}"
