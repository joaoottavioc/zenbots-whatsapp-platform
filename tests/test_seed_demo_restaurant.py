"""Smoke tests for scripts/seed_demo_restaurant.py.

The seed script writes to the real DB; we don't exercise the full
embed_async + bcrypt path in unit tests because both are slow and
environment-sensitive. These tests cover:

1. The product list is sane (no duplicates, all required fields).
2. Module imports cleanly so the CI step that runs `python -m
   scripts.seed_demo_restaurant` doesn't blow up on a stale rename.
3. The expected demo slug constant matches the chat_routes demo guard
   constant — drift between the two would silently disable the demo
   abuse cap.
"""

from __future__ import annotations

import pytest


def test_demo_products_have_required_fields():
    from scripts.seed_demo_restaurant import DEMO_PRODUCTS

    required = {"name", "price", "category", "description"}
    for p in DEMO_PRODUCTS:
        missing = required - set(p.keys())
        assert not missing, f"Product {p.get('name')!r} missing fields: {missing}"
        assert p["price"] > 0, f"Product {p['name']} has non-positive price"
        assert p["name"].strip(), "Empty product name"


def test_demo_products_have_unique_names():
    from scripts.seed_demo_restaurant import DEMO_PRODUCTS

    names = [p["name"] for p in DEMO_PRODUCTS]
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"Duplicate product names: {duplicates}"


def test_demo_products_count_in_target_range():
    """Plan says ~20 products; assert we're in the 15-30 ballpark so a
    careless edit doesn't shrink the demo menu to two items."""
    from scripts.seed_demo_restaurant import DEMO_PRODUCTS

    assert 15 <= len(DEMO_PRODUCTS) <= 30


def test_demo_slug_matches_chat_routes_constant():
    """The seed script and the chat-routes abuse-cap guard both reference
    `pizzaria-do-ze`. They MUST agree or the cap silently disables."""
    from scripts.seed_demo_restaurant import DEMO_BOT_SLUG
    from app.chat_routes import DEMO_BOT_SLUG as ROUTES_DEMO_SLUG

    assert DEMO_BOT_SLUG == ROUTES_DEMO_SLUG


def test_demo_categories_cover_canonical_set():
    """The demo should cover the canonical menu structure: at least one
    Pizza, one drink, one dessert — this validates the corpus QA can
    exercise category-aware paths."""
    from scripts.seed_demo_restaurant import DEMO_PRODUCTS

    categories = {p["category"] for p in DEMO_PRODUCTS}
    assert "Pizzas" in categories
    assert "Bebidas" in categories
    assert "Sobremesas" in categories


@pytest.mark.asyncio
async def test_seed_module_imports_cleanly():
    """Catches accidental top-level breakage (renamed import, syntax)
    without hitting the DB. Important because the CI step runs the
    full `python -m scripts.seed_demo_restaurant` and a broken import
    fails the deploy."""
    import scripts.seed_demo_restaurant as mod

    assert hasattr(mod, "seed")
    assert hasattr(mod, "main")
    assert callable(mod.seed)
    assert callable(mod.main)
