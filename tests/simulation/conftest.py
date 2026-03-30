"""
Simulation test fixtures.

Creates a fictional restaurant ("Sabor da Serra") with a realistic menu
in the test database. Each test gets a fresh contact + cart, while the
bot and products are shared (session-scoped for speed).

Run with: docker compose exec backend pytest tests/simulation/ -v
"""

import uuid
import logging
import asyncio

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

from .menu import RESTAURANT_NAME, PRODUCTS

logger = logging.getLogger(__name__)


def pytest_collection_modifyitems(items):
    for item in items:
        if "simulation" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Global shared state
# ---------------------------------------------------------------------------

_setup_done = False
_setup_skipped = False
_bot_data = {}
_product_map = {}
_prefix = ""


async def _ensure_test_data():
    global _setup_done, _setup_skipped, _bot_data, _product_map, _prefix

    if _setup_done or _setup_skipped:
        return

    try:
        async with async_session() as check_session:
            await check_session.execute(text("SELECT 1"))
    except Exception:
        _setup_skipped = True
        return

    _prefix = f"_sim_{uuid.uuid4().hex[:8]}"

    async with async_session() as session:
        # Create user
        user = User(
            email=f"{_prefix}@test.com",
            hashed_password="not-a-real-hash",
            is_email_verified=True,
        )
        session.add(user)
        await session.flush()

        # Create a subscription plan + active subscription so the bot works
        # Create bot first (subscription references bot_id)
        bot = Bot(
            user_id=user.id,
            restaurant_name=RESTAURANT_NAME,
            whatsapp_number=f"55{_prefix[:11]}",
            whatsapp_token="fake-token",
            phone_number_id=f"sim-{_prefix[:8]}",
        )
        session.add(bot)
        await session.flush()
        await session.refresh(bot)

        from app.time import utcnow
        from datetime import timedelta

        subscription = Subscription(
            bot_id=bot.id,
            user_id=user.id,
            mp_subscription_id=f"sim-sub-{_prefix[:8]}",
            status="authorized",
            current_period_end=utcnow() + timedelta(days=365),
        )
        session.add(subscription)
        await session.flush()

        _bot_data = {"bot_id": bot.id, "user_id": user.id}

        # Generate embeddings and create products
        names = [p["name"] for p in PRODUCTS]
        embeddings = await embed_async(names, space="products", normalize=False)

        for defn, embedding in zip(PRODUCTS, embeddings):
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
            await session.refresh(product)
            _product_map[defn["name"]] = product.id

        await session.commit()
        _setup_done = True
        logger.info(
            "Simulation test data created: bot_id=%s, %d products",
            bot.id,
            len(_product_map),
        )


async def _cleanup_test_data():
    global _setup_done
    if not _setup_done:
        return

    bot_id = _bot_data["bot_id"]
    user_id = _bot_data["user_id"]

    async with async_session() as session:
        await session.execute(
            sa_delete(ConversationHistory).where(ConversationHistory.bot_id == bot_id)
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

    _setup_done = False
    logger.info("Simulation test data cleaned up")


def pytest_sessionfinish(session, exitstatus):
    if _setup_done:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_cleanup_test_data())
            else:
                loop.run_until_complete(_cleanup_test_data())
        except Exception as e:
            logger.warning("Simulation test cleanup failed: %s", e)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session():
    await _ensure_test_data()
    if _setup_skipped:
        pytest.skip("Database not reachable (simulation tests require Docker services)")
    async with async_session() as session:
        yield session


@pytest.fixture
def sim_bot():
    return _bot_data


@pytest.fixture
def sim_products():
    return _product_map


@pytest.fixture
def sim_phone_number_id():
    return f"sim-{_prefix[:8]}"


@pytest_asyncio.fixture
async def sim_context(db_session, sim_bot, sim_phone_number_id):
    """
    Provides a fresh contact + cart + helpers for each test.
    Returns a SimContext with send_message() and get_cart() methods.
    """
    # Phone must be numeric-only (WhatsApp format)
    _rand = str(uuid.uuid4().int)[:10]
    contact_phone = f"5500{_rand}"

    # Don't pre-create contact/cart — let process_whatsapp_message
    # create them naturally via get_or_create_contact/get_or_create_cart.

    # Capture sent messages
    captured_responses: list[str] = []

    class SimContext:
        def __init__(self):
            self.bot_id = sim_bot["bot_id"]
            self.contact_phone = contact_phone
            self.phone_number_id = sim_phone_number_id
            self.responses = captured_responses

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
                                            "id": f"wamid.sim{uuid.uuid4().hex[:12]}",
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
            """Send a message through the full pipeline. Returns bot's response text."""
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
                    # Response text can be positional (arg index 1) or keyword
                    msg = call_kwargs.get("message", "")
                    if not msg and len(call_args) > 1:
                        msg = call_args[1] if isinstance(call_args[1], str) else ""
                    if msg and len(msg) > 5:
                        captured_responses.append(msg)
                        return msg
            return None

        async def get_cart_items(self) -> list[tuple[str, int]]:
            """Query current cart items as [(product_name, quantity), ...]."""
            async with async_session() as fresh_session:
                # Find contact by phone number (created by process_whatsapp_message)
                contact_result = await fresh_session.execute(
                    select(Contact).where(
                        Contact.phone_number == contact_phone,
                        Contact.bot_id == sim_bot["bot_id"],
                    )
                )
                contact_obj = contact_result.scalars().first()
                if not contact_obj:
                    return []

                cart_result = await fresh_session.execute(
                    select(ShoppingCart).where(
                        ShoppingCart.contact_id == contact_obj.id
                    )
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

    ctx = SimContext()
    yield ctx
