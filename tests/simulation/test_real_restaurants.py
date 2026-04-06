"""
Real restaurant simulation tests.

Runs scenarios against 5 real restaurant bots created via the API
(Nakato Sushi, Bullguer, Black Dog, Pizzaria Macedos, Tubarao do Acai).
Products were added via POST /bots/{id}/products (embeddings auto-generated).
Some products randomly marked unavailable via PUT.

Prerequisites:
  1. Run: python -m tests.simulation.real_restaurant_setup
  2. Create subscriptions in DB (see setup output)
  3. Run: docker compose exec backend pytest tests/simulation/test_real_restaurants.py -v

Each test creates a fresh contact and sends messages through process_whatsapp_message.
"""

import uuid
import pytest
import pytest_asyncio
from sqlmodel import select
from sqlalchemy import text

from app.database import async_session
from app.models import Bot, Contact, ShoppingCart, CartItem, Product

from .real_restaurant_setup import RESTAURANTS, TEST_SCENARIOS


# ---------------------------------------------------------------------------
# Phone number IDs for the API-created bots
# ---------------------------------------------------------------------------

PHONE_IDS = {label: r["phone_number_id"] for label, r in RESTAURANTS.items()}
LABELS = list(RESTAURANTS.keys())

# Cache bot_id lookups
_bot_id_cache: dict[str, int] = {}


async def _get_bot_id(label: str) -> int:
    if label in _bot_id_cache:
        return _bot_id_cache[label]
    phone_id = PHONE_IDS[label]
    async with async_session() as session:
        result = await session.execute(
            select(Bot.id).where(Bot.phone_number_id == phone_id)
        )
        row = result.first()
        if not row:
            raise RuntimeError(
                f"Bot with phone_number_id={phone_id} not found. "
                "Run: python -m tests.simulation.real_restaurant_setup"
            )
        _bot_id_cache[label] = row[0]
        return row[0]


# ---------------------------------------------------------------------------
# SimContext for API-created bots
# ---------------------------------------------------------------------------


class RealSimContext:
    """Simulation context for a real restaurant bot."""

    def __init__(self, bot_id: int, phone_number_id: str, contact_phone: str):
        self.bot_id = bot_id
        self.contact_phone = contact_phone
        self.phone_number_id = phone_number_id
        self.responses: list[str] = []

    def _build_payload(self, text_body: str) -> dict:
        return {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {
                                    "phone_number_id": self.phone_number_id,
                                    "display_phone_number": self.contact_phone,
                                },
                                "messages": [
                                    {
                                        "from": self.contact_phone,
                                        "id": f"wamid.rr{uuid.uuid4().hex[:12]}",
                                        "text": {"body": text_body},
                                        "type": "text",
                                    }
                                ],
                            }
                        }
                    ]
                }
            ],
        }

    async def send(self, text_body: str) -> str | None:
        import app.whatsapp as wh
        from app.worker import process_whatsapp_message
        import redis.asyncio as aioredis

        local_captured = []
        original_send = wh.send_whatsapp_message

        async def capture_send(*args, **kwargs):
            local_captured.append((args, kwargs))

        wh.send_whatsapp_message = capture_send
        try:
            pool = aioredis.from_url("redis://redis:6379/1")
            ctx = {"redis": pool}
            await process_whatsapp_message(ctx, self._build_payload(text_body))
            await pool.aclose()
        finally:
            wh.send_whatsapp_message = original_send

        if local_captured:
            for call_args, call_kwargs in local_captured:
                msg = call_kwargs.get("message", "")
                if not msg and len(call_args) > 1:
                    msg = call_args[1] if isinstance(call_args[1], str) else ""
                if msg and len(msg) > 5:
                    self.responses.append(msg)
                    return msg
        return None

    async def get_cart_items(self) -> list[tuple[str, int]]:
        async with async_session() as fresh_session:
            contact_result = await fresh_session.execute(
                select(Contact).where(
                    Contact.phone_number == self.contact_phone,
                    Contact.bot_id == self.bot_id,
                )
            )
            contact_obj = contact_result.scalars().first()
            if not contact_obj:
                return []

            cart_result = await fresh_session.execute(
                select(ShoppingCart).where(ShoppingCart.contact_id == contact_obj.id)
            )
            cart_obj = cart_result.scalars().first()
            if not cart_obj:
                return []

            items_result = await fresh_session.execute(
                select(CartItem)
                .where(CartItem.cart_id == cart_obj.id)
                .order_by(CartItem.id)
            )
            items = list(items_result.scalars().all())

            result = []
            for item in items:
                prod_result = await fresh_session.execute(
                    select(Product).where(Product.id == item.product_id)
                )
                prod = prod_result.scalars().first()
                if prod:
                    result.append((prod.name, item.quantity))
            return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_check():
    """Verify DB is reachable and bots exist."""
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("Database not reachable")

    # Verify at least one bot exists
    async with async_session() as session:
        result = await session.execute(
            select(Bot.id).where(Bot.phone_number_id == PHONE_IDS["sushi"])
        )
        if not result.first():
            pytest.skip(
                "Real restaurant bots not found. "
                "Run: python -m tests.simulation.real_restaurant_setup"
            )


async def _make_ctx(label: str) -> RealSimContext:
    bot_id = await _get_bot_id(label)
    _rand = str(uuid.uuid4().int)[:10]
    contact_phone = f"5500{_rand}"
    return RealSimContext(bot_id, PHONE_IDS[label], contact_phone)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRealAddSingle:
    """Add a single item to cart."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", LABELS, ids=lambda x: x)
    async def test_add_single(self, db_check, label):
        ctx = await _make_ctx(label)
        sc = TEST_SCENARIOS[label]

        await ctx.send("oi")
        await ctx.send(sc["add_single_msg"])

        cart = await ctx.get_cart_items()
        expected = sc["add_single_expected"]
        assert len(cart) >= 1, f"[{label}] Expected >= 1 item, got {cart}"
        for exp_name, exp_qty in expected:
            found = [(n, q) for n, q in cart if exp_name.lower() in n.lower()]
            assert found, f"[{label}] '{exp_name}' not in cart: {cart}"
            assert found[0][1] == exp_qty, (
                f"[{label}] qty mismatch for '{exp_name}': "
                f"expected {exp_qty}, got {found[0][1]}"
            )


class TestRealAddMulti:
    """Add multiple items with quantities."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", LABELS, ids=lambda x: x)
    async def test_add_multi(self, db_check, label):
        ctx = await _make_ctx(label)
        sc = TEST_SCENARIOS[label]

        await ctx.send("oi")
        await ctx.send(sc["add_multi_msg"])

        cart = await ctx.get_cart_items()
        expected = sc["add_multi_expected"]
        assert len(cart) >= len(expected), (
            f"[{label}] Expected >= {len(expected)} items, got {cart}"
        )
        for exp_name, exp_qty in expected:
            found = [(n, q) for n, q in cart if exp_name.lower() in n.lower()]
            assert found, f"[{label}] '{exp_name}' not in cart: {cart}"
            assert found[0][1] == exp_qty, (
                f"[{label}] qty mismatch for '{exp_name}': "
                f"expected {exp_qty}, got {found[0][1]}"
            )


class TestRealUnavailable:
    """Unavailable product detection — available added, unavailable flagged."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", LABELS, ids=lambda x: x)
    async def test_unavailable(self, db_check, label):
        ctx = await _make_ctx(label)
        sc = TEST_SCENARIOS[label]

        await ctx.send("oi")
        response = await ctx.send(sc["unavailable_msg"])

        cart = await ctx.get_cart_items()
        avail = sc["unavailable_available"]
        unavail = sc["unavailable_name"]

        # Available product should be in cart
        found = [n for n, q in cart if avail.lower() in n.lower()]
        assert found, f"[{label}] '{avail}' not in cart: {cart}"

        # Unavailable should NOT be in cart
        bad = [n for n, q in cart if unavail.lower() in n.lower()]
        assert not bad, f"[{label}] '{unavail}' should NOT be in cart: {cart}"

        # Response should mention em falta
        assert response is not None
        resp_lower = response.lower()
        assert any(
            w in resp_lower
            for w in (
                "falta",
                "indisponivel",
                "indisponível",
                "disponivel",
                "disponível",
            )
        ), f"[{label}] No em falta mention: {response[:200]}"


class TestRealRemove:
    """Add item then remove it."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", LABELS, ids=lambda x: x)
    async def test_remove(self, db_check, label):
        ctx = await _make_ctx(label)
        sc = TEST_SCENARIOS[label]

        await ctx.send("oi")
        await ctx.send(sc["remove_add_msg"])

        cart = await ctx.get_cart_items()
        exp_name, exp_qty = sc["remove_add_expected"]
        found = [n for n, q in cart if exp_name.lower() in n.lower()]
        assert found, f"[{label}] Setup: '{exp_name}' not in cart: {cart}"

        await ctx.send(sc["remove_msg"])

        cart_after = await ctx.get_cart_items()
        still = [n for n, q in cart_after if exp_name.lower() in n.lower()]
        assert not still, f"[{label}] '{exp_name}' still in cart: {cart_after}"


class TestRealSuggestions:
    """Suggestions should not add to cart."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", LABELS, ids=lambda x: x)
    async def test_suggestions(self, db_check, label):
        ctx = await _make_ctx(label)
        sc = TEST_SCENARIOS[label]

        await ctx.send("oi")
        response = await ctx.send(sc["suggestion_msg"])

        cart = await ctx.get_cart_items()
        assert len(cart) == 0, f"[{label}] Suggestions added to cart: {cart}"
        assert response is not None


class TestRealCheckout:
    """Checkout flow survives ambiguous messages."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", LABELS, ids=lambda x: x)
    async def test_checkout_protection(self, db_check, label):
        ctx = await _make_ctx(label)
        sc = TEST_SCENARIOS[label]

        await ctx.send("oi")
        await ctx.send(sc["checkout_product_msg"])
        await ctx.send("finalizar")
        await ctx.send("entrega")

        # Ambiguous message during AWAITING_CEP
        await ctx.send("hmm deixa eu pensar")

        cart = await ctx.get_cart_items()
        exp_name, exp_qty = sc["checkout_product_expected"]
        found = [n for n, q in cart if exp_name.lower() in n.lower()]
        assert found, f"[{label}] Checkout destroyed! '{exp_name}' not in cart: {cart}"


class TestRealGreeting:
    """Greeting should not add to cart."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", LABELS, ids=lambda x: x)
    async def test_greeting(self, db_check, label):
        ctx = await _make_ctx(label)

        response = await ctx.send("oi, boa noite")

        cart = await ctx.get_cart_items()
        assert len(cart) == 0, f"[{label}] Greeting added to cart: {cart}"
        assert response is not None
