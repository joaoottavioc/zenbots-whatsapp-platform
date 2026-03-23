# tests/integration/conftest.py
"""
Fixtures for Docker-based integration tests.

Design: Create test data ONCE per module via a setup function called
at the start of each test. Uses a global flag to avoid re-creation.
All tests share the same bot/products. Cleanup at module exit.

Run with: docker compose exec backend pytest tests/integration/ -v
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
)
from app.embedding_service import embed_async

logger = logging.getLogger(__name__)


def pytest_collection_modifyitems(items):
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def event_loop():
    """Single event loop for all integration tests.
    Prevents 'Future attached to a different loop' errors when
    the connection pool is shared across tests."""
    import asyncio

    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Product definitions (menu-agnostic)
# ---------------------------------------------------------------------------

PRODUCT_DEFS = [
    {
        "name": "CLASSIC BURGER",
        "price": 25.0,
        "category": "Lanches",
        "description": "Hamburguer artesanal com queijo e salada",
        "keywords": "burger, hamburguer, classico, classic, burguer",
        "is_available": True,
        "is_deleted": False,
    },
    {
        "name": "CHEESE DELUXE",
        "price": 30.0,
        "category": "Lanches",
        "description": "Burger premium com cheddar derretido",
        "keywords": "cheese, queijo, deluxe, cheeseburguer",
        "is_available": True,
        "is_deleted": False,
    },
    {
        "name": "VEGGIE WRAP",
        "price": 22.0,
        "category": "Lanches",
        "description": "Wrap vegetariano com legumes frescos",
        "keywords": "veggie, vegetariano, wrap, vegan",
        "is_available": False,
        "is_deleted": False,
    },
    {
        "name": "SPICY WINGS",
        "price": 18.0,
        "category": "Porções",
        "description": "Asinhas de frango crocantes com molho picante",
        "keywords": "wings, asas, picante, spicy, frango",
        "is_available": True,
        "is_deleted": False,
    },
    {
        "name": "OLD MENU ITEM",
        "price": 10.0,
        "category": "Lanches",
        "description": "Item antigo do cardapio",
        "keywords": "old, antigo",
        "is_available": False,
        "is_deleted": True,
    },
    {
        "name": "AÇAÍ BOWL",
        "price": 20.0,
        "category": "Sobremesas",
        "description": "Açaí cremoso com granola e frutas",
        "keywords": "acai, açaí, bowl, sobremesa",
        "is_available": True,
        "is_deleted": False,
    },
    {
        "name": "Adicional de BACON",
        "price": 5.0,
        "category": "Adicionais",
        "description": "Porção extra de bacon crocante",
        "keywords": "bacon, extra, adicional",
        "is_available": True,
        "is_deleted": False,
    },
    {
        "name": "TROPICAL DRINK",
        "price": 12.0,
        "category": "Bebidas",
        "description": "Suco tropical refrescante",
        "keywords": "tropical, suco, drink, bebida, refri",
        "is_available": True,
        "is_deleted": False,
    },
]


# ---------------------------------------------------------------------------
# Global shared state: created once, reused by all tests
# ---------------------------------------------------------------------------

_setup_done = False
_setup_skipped = False
_bot_data = {}  # {"bot_id": ..., "user_id": ...}
_product_map = {}  # {"CLASSIC BURGER": 123, ...}
_has_trgm = False


async def _ensure_test_data():
    """Create test bot + products if not already done. Skips if DB unreachable."""
    global _setup_done, _setup_skipped, _bot_data, _product_map, _has_trgm

    if _setup_done or _setup_skipped:
        return

    # Check if DB is reachable before attempting setup
    try:
        async with async_session() as check_session:
            await check_session.execute(text("SELECT 1"))
    except Exception:
        _setup_skipped = True
        return

    async with async_session() as session:
        # Check pg_trgm
        try:
            async with session.begin_nested():
                await session.execute(text("SELECT similarity('test', 'tset')"))
            _has_trgm = True
        except Exception:
            _has_trgm = False

        # Create user + bot
        prefix = f"_inttest_{uuid.uuid4().hex[:8]}"
        user = User(
            email=f"{prefix}@test.com",
            hashed_password="not-a-real-hash",
            is_email_verified=True,
        )
        session.add(user)
        await session.flush()

        bot = Bot(
            user_id=user.id,
            restaurant_name=f"{prefix}_restaurant",
            whatsapp_number=f"55{prefix[:11]}",
            whatsapp_token="fake-token",
            phone_number_id=f"fake-{prefix[:8]}",
        )
        session.add(bot)
        await session.flush()
        await session.refresh(bot)

        _bot_data = {"bot_id": bot.id, "user_id": user.id}

        # Generate embeddings
        names = [d["name"] for d in PRODUCT_DEFS]
        embeddings = await embed_async(names, space="products", normalize=False)

        # Create products
        for defn, embedding in zip(PRODUCT_DEFS, embeddings):
            product = Product(
                bot_id=bot.id,
                name=defn["name"],
                price=defn["price"],
                category=defn["category"],
                description=defn["description"],
                keywords=defn["keywords"],
                is_available=defn["is_available"],
                is_deleted=defn["is_deleted"],
                embedding=embedding,
            )
            session.add(product)
            await session.flush()
            await session.refresh(product)
            _product_map[defn["name"]] = product.id

        await session.commit()
        _setup_done = True
        logger.info(
            "Integration test data created: bot_id=%s, %d products",
            bot.id,
            len(_product_map),
        )


async def _cleanup_test_data():
    """Remove all test data."""
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
        await session.execute(sa_delete(Bot).where(Bot.id == bot_id))
        await session.execute(sa_delete(User).where(User.id == user_id))
        await session.commit()

    _setup_done = False
    logger.info("Integration test data cleaned up")


def pytest_sessionfinish(session, exitstatus):
    """Clean up test data when all tests are done."""
    if _setup_done:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_cleanup_test_data())
            else:
                loop.run_until_complete(_cleanup_test_data())
        except Exception as e:
            logger.warning("Integration test cleanup failed: %s", e)


# ---------------------------------------------------------------------------
# Fixtures: each test gets a fresh session + shared test data
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session():
    """Fresh session per test. Test data is created on first use."""
    await _ensure_test_data()
    if _setup_skipped:
        pytest.skip(
            "Database not reachable (integration tests require Docker services)"
        )
    async with async_session() as session:
        yield session


@pytest.fixture
def test_bot():
    """Returns shared bot info dict."""
    return _bot_data


@pytest.fixture
def test_products():
    """Returns shared product map {name: product_id}."""
    return _product_map


@pytest.fixture
def integration_db():
    """Returns DB info including pg_trgm availability."""
    return {"has_trgm": _has_trgm}


@pytest.fixture
def requires_trgm(integration_db):
    """Skip test if pg_trgm is not installed."""
    if not integration_db["has_trgm"]:
        pytest.skip("pg_trgm extension not available")
