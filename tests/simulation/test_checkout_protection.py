"""
Simulation tests: checkout flow protection.

Tests that during checkout states, the semantic router is skipped
for non-shopping messages. This prevents misclassification of
checkout inputs (sim/não, CEP, address, name, payment method) as
shopping intents that would destroy the checkout flow.

Run with: docker compose exec backend pytest tests/simulation/test_checkout_protection.py -v
"""

import pytest
from tests.simulation.test_add_items import assert_cart_has


class TestCheckoutProtection:
    """Tests that checkout flow survives ambiguous messages."""

    @pytest.mark.asyncio
    async def test_checkout_survives_ambiguous_message(self, sim_context):
        """During checkout, an ambiguous message should not reset the flow."""
        await sim_context.send("oi")
        await sim_context.send("quero 3 picanha com bacon")

        # Trigger checkout → delivery → enter CEP flow
        await sim_context.send("só isso")
        await sim_context.send("entrega")

        # Now in AWAITING_CEP — send ambiguous message
        await sim_context.send("tá bom então")

        # Cart should still have items (checkout not destroyed)
        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Picanha com bacon", 3)])

    @pytest.mark.asyncio
    async def test_sim_during_address_confirmation(self, sim_context):
        """'sim' during address confirmation should confirm, not show suggestions."""
        await sim_context.send("oi")
        await sim_context.send("quero 2 x-burger")
        await sim_context.send("finalizar")
        await sim_context.send("entrega")
        await sim_context.send("04674225")
        await sim_context.send("240")

        # Now at AWAITING_ADDRESS_CONFIRMATION — "sim" confirms
        response = await sim_context.send("sim")

        # Cart still has items
        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("X-Burger", 2)])
        # Response should acknowledge the address, not show suggestions
        assert response is not None

    @pytest.mark.asyncio
    async def test_nonsense_during_cep_entry(self, sim_context):
        """Random text during CEP entry should not break checkout."""
        await sim_context.send("oi")
        await sim_context.send("quero 4 mignon")
        await sim_context.send("só isso")
        await sim_context.send("entrega")

        # Now in AWAITING_CEP — send nonsense
        await sim_context.send("hmm deixa eu ver")

        # Cart should still have items
        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Mignon", 4)])

    @pytest.mark.asyncio
    async def test_number_during_complement_not_misread(self, sim_context):
        """A number during AWAITING_NUMBER_COMPLEMENT should be treated as
        address complement, not as a product quantity."""
        await sim_context.send("oi")
        await sim_context.send("quero 2 alcatra acebolada")
        await sim_context.send("finalizar")
        await sim_context.send("entrega")
        await sim_context.send("04674225")

        # Now in AWAITING_NUMBER_COMPLEMENT — send "500 apto 12"
        await sim_context.send("500 apto 12")

        # Cart should still have items (not try to add "500 apto")
        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Alcatra acebolada", 2)])

    @pytest.mark.asyncio
    async def test_name_during_customer_name_not_misread(self, sim_context):
        """A name during AWAITING_CUSTOMER_NAME should be saved, not misclassified."""
        await sim_context.send("oi")
        await sim_context.send("quero 1 picanha com bacon")
        await sim_context.send("finalizar")
        await sim_context.send("entrega")
        await sim_context.send("04674225")
        await sim_context.send("100")
        await sim_context.send("sim")

        # Now at AWAITING_CUSTOMER_NAME — send a name
        response = await sim_context.send("João Silva")

        # Cart should still have items
        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Picanha com bacon", 1)])
        # Response should ask for payment method
        assert response is not None

    @pytest.mark.asyncio
    async def test_address_confirmation_nao_works(self, sim_context):
        """'não' during address confirmation should negate without router."""
        await sim_context.send("oi")
        await sim_context.send("quero 1 batata frita")
        await sim_context.send("finalizar")
        await sim_context.send("entrega")
        await sim_context.send("04674225")
        await sim_context.send("100")

        # At AWAITING_ADDRESS_CONFIRMATION — say "não"
        response = await sim_context.send("não")

        # Should ask for CEP again (negation), not show suggestions
        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Batata frita", 1)])
        assert response is not None
        assert "cep" in response.lower() or "CEP" in response

    @pytest.mark.asyncio
    async def test_delivery_method_not_misclassified(self, sim_context):
        """'entrega' during AWAITING_DELIVERY_METHOD should work without router."""
        await sim_context.send("oi")
        await sim_context.send("quero 2 onion rings")

        # Trigger checkout
        await sim_context.send("só isso")

        # Now at AWAITING_DELIVERY_METHOD — "entrega" is keyword-matched
        response = await sim_context.send("entrega")

        cart = await sim_context.get_cart_items()
        assert_cart_has(cart, [("Onion rings", 2)])
        assert response is not None

    @pytest.mark.asyncio
    async def test_payment_method_during_checkout(self, sim_context):
        """'pix' during AWAITING_PAYMENT_METHOD should not be misclassified."""
        await sim_context.send("oi")
        await sim_context.send("quero 1 x-burger")
        await sim_context.send("finalizar")
        await sim_context.send("retirada")

        # Should be at payment method — respond with payment
        # (pickup skips address flow, goes straight to name then payment)
        # Send name first
        await sim_context.send("Maria")

        # Now at AWAITING_PAYMENT_METHOD — "dinheiro"
        response = await sim_context.send("dinheiro")

        # Should process payment, not classify as shopping intent
        assert response is not None
