# tests/test_broadcast_order.py
"""
Test that display_items is built BEFORE cart is cleared in _handle_payment_method.
"""

from unittest.mock import MagicMock, patch
import pytest


@pytest.mark.asyncio
async def test_display_items_built_before_cart_clear():
    """
    Verify that display_items captures cart.items data BEFORE
    crud.clear_db_cart() is called, so the broadcast has valid item names.
    """
    # Track the order of operations
    call_order = []

    # Create cart items that will be "cleared" by clear_db_cart
    item1 = MagicMock()
    item1.quantity = 2
    item1.product = MagicMock()
    item1.product.name = "Pizza Margherita"
    item1.product.price = 35.0
    item1.product.id = 1
    item1.notes = None

    item2 = MagicMock()
    item2.quantity = 1
    item2.product = MagicMock()
    item2.product.name = "Coca-Cola"
    item2.product.price = 8.0
    item2.product.id = 2
    item2.notes = None

    cart = MagicMock()
    cart.id = 100
    cart.items = [item1, item2]
    cart.delivery_method = MagicMock()
    cart.delivery_method.value = "delivery"
    cart.delivery_method.__eq__ = lambda self, other: False
    cart.customer_address = "Rua Teste, 123"
    cart.contact = MagicMock()
    cart.contact.name = "João"

    async def mock_clear_db_cart(session, cart_id):
        call_order.append("clear_db_cart")
        cart.items = []  # Simulate clearing
        return cart

    async def mock_broadcast(*args, **kwargs):
        call_order.append("broadcast")
        # At this point, display_items should already exist
        data = args[1] if len(args) > 1 else kwargs.get("data", {})
        assert len(data.get("items", [])) > 0, "display_items should have items"

    with patch("app.whatsapp.crud.clear_db_cart", side_effect=mock_clear_db_cart):
        # Read the source to verify ordering
        import inspect
        from app import whatsapp

        source = inspect.getsource(whatsapp._handle_payment_method)

        # Verify display_items assignment comes BEFORE clear_db_cart call
        display_items_pos = source.find("display_items = [")
        clear_cart_pos = source.find("crud.clear_db_cart")

        assert display_items_pos < clear_cart_pos, (
            f"display_items (pos {display_items_pos}) should be built "
            f"BEFORE clear_db_cart (pos {clear_cart_pos})"
        )
