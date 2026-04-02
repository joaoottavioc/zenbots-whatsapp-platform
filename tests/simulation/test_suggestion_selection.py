"""
Simulation tests: selecting from numbered suggestions.

Tests that after the bot shows numbered suggestions, customers can select
items by number ("do 1"), ordinals ("do primeiro"), names, and with
quantity prefixes ("dois do 1", "sete do primeiro").

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
        """'1' — bare number selects suggestion #1 with qty=1."""
        await _get_suggestions(sim_context)

        await sim_context.send("1")

        cart = await sim_context.get_cart_items()
        assert len(cart) == 1, f"Expected 1 item, got {cart}"
        assert cart[0][1] == 1, f"Expected qty=1, got {cart}"

    @pytest.mark.asyncio
    async def test_select_by_ordinal(self, sim_context):
        """'o primeiro' — ordinal selects suggestion #1 with qty=1."""
        await _get_suggestions(sim_context)

        await sim_context.send("o primeiro")

        cart = await sim_context.get_cart_items()
        assert len(cart) == 1, f"Expected 1 item, got {cart}"
        assert cart[0][1] == 1, f"Expected qty=1, got {cart}"

    @pytest.mark.asyncio
    async def test_select_by_name(self, sim_context):
        """'quero picanha com bacon' — name-based selection from suggestions."""
        await _get_suggestions(sim_context)

        await sim_context.send("quero picanha com bacon")

        cart = await sim_context.get_cart_items()
        assert len(cart) == 1, f"Expected 1 item, got {cart}"
        assert "picanha" in cart[0][0].lower(), f"Expected picanha, got {cart}"

    @pytest.mark.asyncio
    async def test_qty_with_digit_index(self, sim_context):
        """'dois do 1' — quantity + digit index selects 2x of suggestion #1."""
        await _get_suggestions(sim_context)

        await sim_context.send("dois do 1")

        cart = await sim_context.get_cart_items()
        assert len(cart) == 1, f"Expected 1 item, got {cart}"
        assert cart[0][1] == 2, f"Expected qty=2, got {cart}"

    @pytest.mark.asyncio
    async def test_qty_with_ordinal(self, sim_context):
        """'sete do primeiro' — quantity + ordinal selects 7x of suggestion #1."""
        await _get_suggestions(sim_context)

        await sim_context.send("sete do primeiro")

        cart = await sim_context.get_cart_items()
        assert len(cart) == 1, f"Expected 1 item, got {cart}"
        assert cart[0][1] == 7, f"Expected qty=7, got {cart}"

    @pytest.mark.asyncio
    async def test_qty_with_digit_index_written_number(self, sim_context):
        """'tres do 2' — written number + digit index selects 3x of suggestion #2."""
        await _get_suggestions(sim_context)

        await sim_context.send("tres do 2")

        cart = await sim_context.get_cart_items()
        assert len(cart) == 1, f"Expected 1 item, got {cart}"
        assert cart[0][1] == 3, f"Expected qty=3, got {cart}"

    @pytest.mark.asyncio
    async def test_qty_compound_number_with_ordinal(self, sim_context):
        """'doze do primeiro' — compound written number + ordinal."""
        await _get_suggestions(sim_context)

        await sim_context.send("doze do primeiro")

        cart = await sim_context.get_cart_items()
        assert len(cart) == 1, f"Expected 1 item, got {cart}"
        assert cart[0][1] == 12, f"Expected qty=12, got {cart}"

    @pytest.mark.asyncio
    async def test_multiple_qty_selections(self, sim_context):
        """'dois do 1 e cinco do 3' — multiple selections with quantities."""
        await _get_suggestions(sim_context)

        await sim_context.send("dois do 1 e cinco do 3")

        cart = await sim_context.get_cart_items()
        assert len(cart) == 2, f"Expected 2 items, got {cart}"
        quantities = sorted([qty for _, qty in cart])
        assert quantities == [2, 5], f"Expected [2, 5], got {quantities}"

    @pytest.mark.asyncio
    async def test_bare_number_still_adds_one(self, sim_context):
        """'3' — bare digit without quantity prefix still adds 1x (not 3x)."""
        await _get_suggestions(sim_context)

        await sim_context.send("3")

        cart = await sim_context.get_cart_items()
        assert len(cart) == 1, f"Expected 1 item, got {cart}"
        assert cart[0][1] == 1, (
            f"Expected qty=1 (index selection, not quantity), got {cart}"
        )
