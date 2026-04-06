"""
Multi-restaurant simulation tests.

Runs the same core scenarios (add, remove, suggestions, unavailable,
checkout protection) against 5 different restaurant types:
  - Sushi, Burger, Hot Dog, Pizza, Açaí

Each restaurant has its own menu and test data. The tests verify that
ZenBots' conversational ordering engine works correctly regardless of
restaurant type, product naming conventions, and menu structure.

Run with:
  docker compose exec backend pytest tests/simulation/test_multi_restaurant.py -v
"""

import uuid
import asyncio
import logging

import pytest
import pytest_asyncio
from sqlmodel import select
from sqlalchemy import delete as sa_delete, text

from app.database import async_session
from app.models import (
    Bot,
    Product,
    User,
    Contact,
    ShoppingCart,
    CartItem,
    ConversationHistory,
    Subscription,
)
from app.embedding_service import embed_async

from .menus import sushi, burger, hotdog, pizza, acai

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Menu registry — each entry is (module, restaurant_id_label)
# ---------------------------------------------------------------------------

ALL_MENUS = [
    (sushi, "sushi"),
    (burger, "burger"),
    (hotdog, "hotdog"),
    (pizza, "pizza"),
    (acai, "acai"),
]

# ---------------------------------------------------------------------------
# Session-scoped setup: create all 5 bots + products once
# ---------------------------------------------------------------------------

_multi_setup_done = False
_multi_setup_skipped = False
_bots: dict[
    str, dict
] = {}  # label -> {"bot_id": int, "user_id": int, "phone_number_id": str, "menu_module": module}


async def _ensure_multi_test_data():
    global _multi_setup_done, _multi_setup_skipped, _bots

    if _multi_setup_done or _multi_setup_skipped:
        return

    try:
        async with async_session() as check_session:
            await check_session.execute(text("SELECT 1"))
    except Exception:
        _multi_setup_skipped = True
        return

    async with async_session() as session:
        for menu_module, label in ALL_MENUS:
            _prefix = f"_mr_{label}_{uuid.uuid4().hex[:6]}"

            user = User(
                email=f"{_prefix}@test.com",
                hashed_password="not-a-real-hash",
                is_email_verified=True,
            )
            session.add(user)
            await session.flush()

            phone_number_id = f"mr-{label}-{_prefix[:8]}"
            bot = Bot(
                user_id=user.id,
                restaurant_name=menu_module.RESTAURANT_NAME,
                whatsapp_number=f"55{_prefix[:11]}",
                whatsapp_token="fake-token",
                phone_number_id=phone_number_id,
            )
            session.add(bot)
            await session.flush()
            await session.refresh(bot)

            from app.time import utcnow
            from datetime import timedelta

            sub = Subscription(
                bot_id=bot.id,
                user_id=user.id,
                mp_subscription_id=f"mr-sub-{_prefix[:8]}",
                status="authorized",
                current_period_end=utcnow() + timedelta(days=365),
            )
            session.add(sub)
            await session.flush()

            # Generate embeddings and create products
            names = [p["name"] for p in menu_module.PRODUCTS]
            embeddings = await embed_async(names, space="products", normalize=False)

            for defn, embedding in zip(menu_module.PRODUCTS, embeddings):
                product = Product(
                    bot_id=bot.id,
                    name=defn["name"],
                    price=defn["price"],
                    category=defn["category"],
                    description=defn["description"],
                    keywords=defn["keywords"],
                    is_available=defn["is_available"],
                    is_deleted=False,
                    embedding=embedding,
                )
                session.add(product)

            await session.flush()

            _bots[label] = {
                "bot_id": bot.id,
                "user_id": user.id,
                "phone_number_id": phone_number_id,
                "menu_module": menu_module,
            }

            logger.info(
                "Multi-restaurant bot created: %s (bot_id=%s, %d products)",
                menu_module.RESTAURANT_NAME,
                bot.id,
                len(menu_module.PRODUCTS),
            )

        await session.commit()
        _multi_setup_done = True


async def _cleanup_multi_test_data():
    global _multi_setup_done
    if not _multi_setup_done:
        return

    async with async_session() as session:
        for label, data in _bots.items():
            bot_id = data["bot_id"]
            user_id = data["user_id"]

            await session.execute(
                sa_delete(ConversationHistory).where(
                    ConversationHistory.bot_id == bot_id
                )
            )
            cart_ids_res = await session.execute(
                select(ShoppingCart.id).join(Contact).where(Contact.bot_id == bot_id)
            )
            cart_ids = [r[0] for r in cart_ids_res]
            if cart_ids:
                await session.execute(
                    sa_delete(CartItem).where(CartItem.cart_id.in_(cart_ids))
                )
                await session.execute(
                    sa_delete(ShoppingCart).where(ShoppingCart.id.in_(cart_ids))
                )
            await session.execute(sa_delete(Contact).where(Contact.bot_id == bot_id))
            await session.execute(sa_delete(Product).where(Product.bot_id == bot_id))
            await session.execute(
                sa_delete(Subscription).where(Subscription.bot_id == bot_id)
            )
            await session.execute(sa_delete(Bot).where(Bot.id == bot_id))
            await session.execute(sa_delete(User).where(User.id == user_id))

        await session.commit()

    _multi_setup_done = False
    logger.info("Multi-restaurant test data cleaned up")


def pytest_sessionfinish(session, exitstatus):
    if _multi_setup_done:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_cleanup_multi_test_data())
            else:
                loop.run_until_complete(_cleanup_multi_test_data())
        except Exception as e:
            logger.warning("Multi-restaurant cleanup failed: %s", e)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def multi_db():
    await _ensure_multi_test_data()
    if _multi_setup_skipped:
        pytest.skip("Database not reachable (simulation tests require Docker services)")


class MultiSimContext:
    """Simulation context for a specific restaurant bot."""

    def __init__(self, bot_data: dict, contact_phone: str):
        self.bot_id = bot_data["bot_id"]
        self.contact_phone = contact_phone
        self.phone_number_id = bot_data["phone_number_id"]
        self.menu_module = bot_data["menu_module"]
        self.responses: list[str] = []

    def _build_payload(self, text: str) -> dict:
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
                                        "id": f"wamid.mr{uuid.uuid4().hex[:12]}",
                                        "text": {"body": text},
                                        "type": "text",
                                    }
                                ],
                            }
                        }
                    ]
                }
            ],
        }

    async def send(self, text: str) -> str | None:
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
            await process_whatsapp_message(ctx, self._build_payload(text))
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


def _make_context(label: str) -> MultiSimContext:
    """Create a fresh SimContext for a restaurant label."""
    _rand = str(uuid.uuid4().int)[:10]
    contact_phone = f"5500{_rand}"
    return MultiSimContext(_bots[label], contact_phone)


# ---------------------------------------------------------------------------
# Parameterized test IDs
# ---------------------------------------------------------------------------

RESTAURANT_LABELS = ["sushi", "burger", "hotdog", "pizza", "acai"]
RESTAURANT_NAMES = {
    "sushi": "Sushi Kento",
    "burger": "Smash & Co.",
    "hotdog": "Dog House SP",
    "pizza": "Bella Napoli",
    "acai": "Açaí da Barra",
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMultiRestaurantAddSingle:
    """Add a single item — works across all restaurant types."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", RESTAURANT_LABELS, ids=lambda x: f"{x}")
    async def test_add_single_item(self, multi_db, label):
        ctx = _make_context(label)
        scenarios = _bots[label]["menu_module"].TEST_SCENARIOS

        await ctx.send("oi")
        await ctx.send(scenarios["add_single_msg"])

        cart = await ctx.get_cart_items()
        expected = scenarios["add_single_expected"]
        assert len(cart) >= 1, f"[{label}] Expected at least 1 item, got {cart}"
        for exp_name, exp_qty in expected:
            found = [(n, q) for n, q in cart if exp_name.lower() in n.lower()]
            assert found, f"[{label}] Expected '{exp_name}' in cart, got {cart}"
            assert found[0][1] == exp_qty, (
                f"[{label}] Expected qty={exp_qty} for '{exp_name}', got {found[0][1]}"
            )


class TestMultiRestaurantAddMulti:
    """Add multiple items with quantities — works across all restaurant types."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", RESTAURANT_LABELS, ids=lambda x: f"{x}")
    async def test_add_multi_items(self, multi_db, label):
        ctx = _make_context(label)
        scenarios = _bots[label]["menu_module"].TEST_SCENARIOS

        await ctx.send("oi")
        await ctx.send(scenarios["add_multi_msg"])

        cart = await ctx.get_cart_items()
        expected = scenarios["add_multi_expected"]
        assert len(cart) >= len(expected), (
            f"[{label}] Expected at least {len(expected)} items, got {cart}"
        )
        for exp_name, exp_qty in expected:
            found = [(n, q) for n, q in cart if exp_name.lower() in n.lower()]
            assert found, f"[{label}] Expected '{exp_name}' in cart, got {cart}"
            assert found[0][1] == exp_qty, (
                f"[{label}] Expected qty={exp_qty} for '{exp_name}', got {found[0][1]}"
            )


class TestMultiRestaurantUnavailable:
    """Unavailable product detection — available items added, unavailable flagged."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", RESTAURANT_LABELS, ids=lambda x: f"{x}")
    async def test_unavailable_mix(self, multi_db, label):
        ctx = _make_context(label)
        scenarios = _bots[label]["menu_module"].TEST_SCENARIOS

        await ctx.send("oi")
        response = await ctx.send(scenarios["unavailable_msg"])

        cart = await ctx.get_cart_items()
        available_name = scenarios["unavailable_available"]
        unavailable_name = scenarios["unavailable_name"]

        # Available product should be in cart
        found = [n for n, q in cart if available_name.lower() in n.lower()]
        assert found, f"[{label}] Expected '{available_name}' in cart, got {cart}"

        # Unavailable product should NOT be in cart
        unavail_in_cart = [n for n, q in cart if unavailable_name.lower() in n.lower()]
        assert not unavail_in_cart, (
            f"[{label}] '{unavailable_name}' should NOT be in cart, got {cart}"
        )

        # Response should mention em falta
        assert response is not None, f"[{label}] Expected a response"
        response_lower = response.lower()
        assert (
            "falta" in response_lower
            or "indisponível" in response_lower
            or "disponível" in response_lower
        ), f"[{label}] Expected 'em falta' mention in response: {response[:200]}"


class TestMultiRestaurantRemove:
    """Remove items from cart — add first, then remove."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", RESTAURANT_LABELS, ids=lambda x: f"{x}")
    async def test_remove_item(self, multi_db, label):
        ctx = _make_context(label)
        scenarios = _bots[label]["menu_module"].TEST_SCENARIOS

        await ctx.send("oi")
        await ctx.send(scenarios["remove_add_msg"])

        # Verify item was added
        cart = await ctx.get_cart_items()
        exp_name, exp_qty = scenarios["remove_add_expected"]
        found = [n for n, q in cart if exp_name.lower() in n.lower()]
        assert found, f"[{label}] Setup failed: '{exp_name}' not in cart: {cart}"

        # Remove it
        await ctx.send(scenarios["remove_msg"])

        # Cart should be empty or item removed
        cart_after = await ctx.get_cart_items()
        still_there = [n for n, q in cart_after if exp_name.lower() in n.lower()]
        assert not still_there, (
            f"[{label}] '{exp_name}' should be removed, still in cart: {cart_after}"
        )


class TestMultiRestaurantSuggestions:
    """Request suggestions — cart should remain empty."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", RESTAURANT_LABELS, ids=lambda x: f"{x}")
    async def test_suggestion_no_cart_add(self, multi_db, label):
        ctx = _make_context(label)
        scenarios = _bots[label]["menu_module"].TEST_SCENARIOS

        await ctx.send("oi")
        response = await ctx.send(scenarios["suggestion_msg"])

        cart = await ctx.get_cart_items()
        assert len(cart) == 0, (
            f"[{label}] Suggestions should NOT add to cart, got {cart}"
        )
        assert response is not None, f"[{label}] Expected a response with suggestions"


class TestMultiRestaurantCheckoutProtection:
    """Checkout flow protection — ambiguous messages don't break checkout."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", RESTAURANT_LABELS, ids=lambda x: f"{x}")
    async def test_checkout_survives_ambiguous(self, multi_db, label):
        ctx = _make_context(label)
        scenarios = _bots[label]["menu_module"].TEST_SCENARIOS

        await ctx.send("oi")
        await ctx.send(scenarios["checkout_product_msg"])

        # Trigger checkout
        await ctx.send("só isso")
        await ctx.send("entrega")

        # Send ambiguous message during CEP entry
        await ctx.send("tá bom então")

        # Cart should still have items
        cart = await ctx.get_cart_items()
        exp_name, exp_qty = scenarios["checkout_product_expected"]
        found = [n for n, q in cart if exp_name.lower() in n.lower()]
        assert found, f"[{label}] Checkout destroyed! '{exp_name}' not in cart: {cart}"


class TestMultiRestaurantGreeting:
    """Greeting message — should not add anything to cart."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", RESTAURANT_LABELS, ids=lambda x: f"{x}")
    async def test_greeting_empty_cart(self, multi_db, label):
        ctx = _make_context(label)

        response = await ctx.send("oi, boa noite")

        cart = await ctx.get_cart_items()
        assert len(cart) == 0, f"[{label}] Greeting should NOT add to cart, got {cart}"
        assert response is not None, f"[{label}] Expected a greeting response"
