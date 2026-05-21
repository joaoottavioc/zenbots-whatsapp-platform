"""Seed the public demo restaurant.

P3 of plan/portfolio_pivot.md. Creates `Pizzaria do Zé` so the landing
page CTA → /pizzaria-do-ze deep-links into a working web widget that
anyone can chat with — no signup, no setup.

Idempotent by design: re-running is safe. The script keys off the bot
slug (`pizzaria-do-ze`) — if the bot exists it is updated in place;
products are upserted by (bot_id, name) so a re-run won't duplicate them
but a manual product edit through the dashboard is preserved unless the
product name itself collides.

Usage:

    # Inside the backend container:
    docker compose exec backend python -m scripts.seed_demo_restaurant

    # Or against a local venv with DATABASE_URL set:
    python -m scripts.seed_demo_restaurant

The script is invoked as a one-off step in the deploy-dev pipeline so
every dev environment has a fresh demo. Safe to run on production too —
it only adds, never deletes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from sqlalchemy import select

from app.auth import get_password_hash
from app.database import async_session
from app.embedding_service import embed_async
from app.models import Bot, Product, User

logger = logging.getLogger("seed_demo")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ─────────────────────────────────────────────────────────────────────
# Demo configuration
# ─────────────────────────────────────────────────────────────────────

DEMO_USER_EMAIL = "demo@zenbotz.com.br"
DEMO_USER_PASSWORD = (
    "DemoZenBotZ$2026"  # rotated/local-only; never used for login in prod
)
DEMO_BOT_SLUG = "pizzaria-do-ze"
DEMO_BOT_NAME = "Pizzaria do Zé"

DEMO_PRODUCTS: list[dict] = [
    # Pizzas
    {
        "name": "Pizza Margherita",
        "price": 42.00,
        "category": "Pizzas",
        "description": "Molho de tomate fresco, mussarela, manjericão e azeite.",
        "keywords": "margarita, manjericao, classica",
    },
    {
        "name": "Pizza Calabresa",
        "price": 44.00,
        "category": "Pizzas",
        "description": "Calabresa fatiada, cebola roxa, mussarela e azeitona preta.",
        "keywords": "calabresa, linguica, cebola",
    },
    {
        "name": "Pizza Quatro Queijos",
        "price": 49.00,
        "category": "Pizzas",
        "description": "Mussarela, parmesão, gorgonzola e provolone.",
        "keywords": "queijos, gorgonzola, parmesao, provolone",
    },
    {
        "name": "Pizza Portuguesa",
        "price": 46.00,
        "category": "Pizzas",
        "description": "Presunto, ovo, cebola, ervilha, mussarela e azeitona.",
        "keywords": "presunto, ovo, ervilha",
    },
    {
        "name": "Pizza Frango com Catupiry",
        "price": 47.00,
        "category": "Pizzas",
        "description": "Frango desfiado, catupiry cremoso, milho e mussarela.",
        "keywords": "frango, catupiry, milho",
    },
    {
        "name": "Pizza Pepperoni",
        "price": 48.00,
        "category": "Pizzas",
        "description": "Pepperoni importado, mussarela e orégano.",
        "keywords": "pepperoni, picante",
    },
    {
        "name": "Pizza Vegetariana",
        "price": 45.00,
        "category": "Pizzas",
        "description": "Brócolis, palmito, tomate seco, mussarela e azeite.",
        "keywords": "vegetariana, brocolis, palmito",
    },
    # Esfihas
    {
        "name": "Esfiha de Carne",
        "price": 7.50,
        "category": "Esfihas",
        "description": "Massa fresca recheada com carne moída temperada e cebola.",
        "keywords": "esfirra, carne, salgado",
    },
    {
        "name": "Esfiha de Queijo",
        "price": 7.00,
        "category": "Esfihas",
        "description": "Esfiha aberta com mussarela derretida.",
        "keywords": "esfirra, queijo",
    },
    {
        "name": "Esfiha de Calabresa",
        "price": 7.50,
        "category": "Esfihas",
        "description": "Calabresa picada, cebola e mussarela.",
        "keywords": "esfirra, calabresa",
    },
    # Acompanhamentos
    {
        "name": "Batata Frita Pequena",
        "price": 18.00,
        "category": "Acompanhamentos",
        "description": "Porção de batatas fritas crocantes com sal.",
        "keywords": "batata, fritas, porcao",
    },
    {
        "name": "Batata Frita Grande",
        "price": 28.00,
        "category": "Acompanhamentos",
        "description": "Porção grande de batatas fritas com queijo e bacon.",
        "keywords": "batata, bacon, queijo",
    },
    {
        "name": "Onion Rings",
        "price": 22.00,
        "category": "Acompanhamentos",
        "description": "Anéis de cebola empanados e crocantes.",
        "keywords": "cebola, anel, empanado",
    },
    # Bebidas
    {
        "name": "Coca-Cola 350ml",
        "price": 7.00,
        "category": "Bebidas",
        "description": "Refrigerante Coca-Cola lata 350ml gelada.",
        # "coquinha" is the BR-PT diminutive customers use most often;
        # listing it here closes the slang gap that fuzzy/vector lookup
        # can't reliably bridge for diminutives. "Coca gelada" / "coca
        # zero" cover the common qualifier variants.
        "keywords": "coca, coquinha, coca gelada, coca zero, refrigerante, lata",
    },
    {
        "name": "Coca-Cola 2L",
        "price": 14.00,
        "category": "Bebidas",
        "description": "Refrigerante Coca-Cola garrafa 2 litros.",
        "keywords": "coca, coquinha, coca grande, refrigerante, garrafa",
    },
    {
        "name": "Guaraná Antarctica 2L",
        "price": 12.00,
        "category": "Bebidas",
        "description": "Refrigerante Guaraná garrafa 2 litros.",
        "keywords": "guarana, refrigerante",
    },
    {
        "name": "Suco de Laranja 500ml",
        "price": 9.00,
        "category": "Bebidas",
        "description": "Suco natural de laranja sem açúcar.",
        "keywords": "suco, laranja, natural",
    },
    {
        "name": "Água Mineral 500ml",
        "price": 4.00,
        "category": "Bebidas",
        "description": "Água mineral sem gás 500ml.",
        "keywords": "agua, mineral, sem gas",
    },
    # Sobremesas
    {
        "name": "Brownie com Sorvete",
        "price": 16.00,
        "category": "Sobremesas",
        "description": "Brownie de chocolate quente com bola de sorvete de creme.",
        "keywords": "brownie, sorvete, chocolate",
    },
    {
        "name": "Pudim de Leite",
        "price": 12.00,
        "category": "Sobremesas",
        "description": "Pudim tradicional de leite condensado com calda de caramelo.",
        "keywords": "pudim, leite, doce",
    },
]


# ─────────────────────────────────────────────────────────────────────
# Upsert helpers
# ─────────────────────────────────────────────────────────────────────


async def _upsert_user(session) -> User:
    result = await session.execute(select(User).where(User.email == DEMO_USER_EMAIL))
    user = result.scalars().first()
    if user:
        logger.info("Demo user already exists (id=%s)", user.id)
        return user
    user = User(
        email=DEMO_USER_EMAIL,
        hashed_password=get_password_hash(DEMO_USER_PASSWORD),
        is_email_verified=True,
    )
    session.add(user)
    await session.flush()  # need user.id below
    logger.info("Created demo user id=%s email=%s", user.id, user.email)
    return user


async def _upsert_bot(session, user: User) -> Bot:
    result = await session.execute(select(Bot).where(Bot.slug == DEMO_BOT_SLUG))
    bot = result.scalars().first()
    if bot:
        # Keep settings aligned with the demo contract even on re-runs.
        bot.restaurant_name = DEMO_BOT_NAME
        bot.web_widget_enabled = True
        bot.is_open = True
        bot.user_id = user.id
        bot.delivery_fee = 0.0
        bot.min_order_value = 0.0
        # No PIX key — finalization gracefully short-circuits without a
        # real payment flow on the demo.
        bot.pix_key = None
        logger.info("Demo bot already exists (id=%s, slug=%s)", bot.id, bot.slug)
        return bot
    bot = Bot(
        restaurant_name=DEMO_BOT_NAME,
        slug=DEMO_BOT_SLUG,
        user_id=user.id,
        web_widget_enabled=True,
        web_widget_allowed_origins=[],  # empty allowlist — dev convenience
        is_open=True,
        delivery_fee=0.0,
        min_order_value=0.0,
    )
    session.add(bot)
    await session.flush()
    logger.info("Created demo bot id=%s slug=%s", bot.id, bot.slug)
    return bot


async def _seed_products(session, bot: Bot, products: Iterable[dict]) -> int:
    products = list(products)
    # Compute embeddings in one batch — much faster than per-product calls.
    names = [p["name"] for p in products]
    embeddings = await embed_async(names, space="router")

    # Existing products keyed by name so we can skip without churn.
    existing = await session.execute(select(Product).where(Product.bot_id == bot.id))
    by_name = {p.name: p for p in existing.scalars().all()}

    added = 0
    for product_data, vector in zip(products, embeddings):
        if product_data["name"] in by_name:
            continue  # already seeded; manual edits preserved
        product = Product(
            name=product_data["name"],
            description=product_data["description"],
            price=product_data["price"],
            category=product_data["category"],
            keywords=product_data.get("keywords"),
            embedding=vector,
            is_available=True,
            bot_id=bot.id,
        )
        session.add(product)
        added += 1

    if added:
        logger.info("Seeded %d new demo products on bot id=%s", added, bot.id)
    else:
        logger.info("All demo products already present on bot id=%s", bot.id)
    return added


# ─────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────


async def seed() -> dict:
    """Run the seed and return a small status dict for callers (tests, CI)."""
    async with async_session() as session:
        user = await _upsert_user(session)
        bot = await _upsert_bot(session, user)
        added = await _seed_products(session, bot, DEMO_PRODUCTS)
        await session.commit()
        return {
            "user_id": user.id,
            "bot_id": bot.id,
            "bot_slug": bot.slug,
            "products_seeded": added,
            "products_total": len(DEMO_PRODUCTS),
        }


def main() -> None:
    result = asyncio.run(seed())
    logger.info("Seed complete: %s", result)


if __name__ == "__main__":
    main()
