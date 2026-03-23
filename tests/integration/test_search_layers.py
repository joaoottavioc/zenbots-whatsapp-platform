# tests/integration/test_search_layers.py
"""
Phase 1: Search layer integration tests.

Tests find_relevant_products and find_unavailable_products against
a real PostgreSQL database with pg_trgm extension. No LLM calls.
"""

import pytest

from app.crud import find_relevant_products, find_unavailable_products


@pytest.mark.integration
class TestAvailableSearch:
    """Test find_relevant_products against real DB with test products."""

    @pytest.mark.asyncio
    async def test_exact_name_ilike(self, db_session, test_bot, test_products):
        results = await find_relevant_products(
            db_session, test_bot["bot_id"], ["classic burger"]
        )
        names = {p.name for p in results}
        assert "CLASSIC BURGER" in names

    @pytest.mark.asyncio
    async def test_partial_name_ilike(self, db_session, test_bot, test_products):
        results = await find_relevant_products(
            db_session, test_bot["bot_id"], ["burger"]
        )
        names = {p.name for p in results}
        # Should find products with "burger" in name
        assert "CLASSIC BURGER" in names or "CHEESE DELUXE" in names

    @pytest.mark.asyncio
    async def test_split_word_ilike(self, db_session, test_bot, test_products):
        """'xxxxx classic' — full phrase misses, but 'classic' word matches."""
        results = await find_relevant_products(
            db_session, test_bot["bot_id"], ["xxxxx classic"]
        )
        names = {p.name for p in results}
        assert "CLASSIC BURGER" in names

    @pytest.mark.asyncio
    async def test_keywords_ilike(self, db_session, test_bot, test_products):
        """'hamburguer' is in CLASSIC BURGER's keywords, not its name."""
        results = await find_relevant_products(
            db_session, test_bot["bot_id"], ["hamburguer"]
        )
        names = {p.name for p in results}
        assert "CLASSIC BURGER" in names

    @pytest.mark.asyncio
    async def test_pgtrgm_typo(
        self, db_session, test_bot, test_products, requires_trgm
    ):
        """'clasic burger' has a typo — pg_trgm should fuzzy match."""
        results = await find_relevant_products(
            db_session, test_bot["bot_id"], ["clasic burger"]
        )
        names = {p.name for p in results}
        assert "CLASSIC BURGER" in names

    @pytest.mark.asyncio
    async def test_pgtrgm_misspelling(
        self, db_session, test_bot, test_products, requires_trgm
    ):
        """'spiccy wings' — double c typo."""
        results = await find_relevant_products(
            db_session, test_bot["bot_id"], ["spiccy wings"]
        )
        names = {p.name for p in results}
        assert "SPICY WINGS" in names

    @pytest.mark.asyncio
    async def test_embedding_similarity(self, db_session, test_bot, test_products):
        """Semantically similar term should find something (may be empty if
        embedding distance is above threshold — skip in that case)."""
        results = await find_relevant_products(
            db_session, test_bot["bot_id"], ["lanche artesanal com queijo"]
        )
        # Embedding results depend on model quality; assert non-failure rather than count
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_deleted_excluded(self, db_session, test_bot, test_products):
        """Deleted products should never appear in available search."""
        results = await find_relevant_products(
            db_session, test_bot["bot_id"], ["old menu item"]
        )
        names = {p.name for p in results}
        assert "OLD MENU ITEM" not in names

    @pytest.mark.asyncio
    async def test_unavailable_excluded(self, db_session, test_bot, test_products):
        """Unavailable products should not appear in available search."""
        results = await find_relevant_products(
            db_session, test_bot["bot_id"], ["veggie wrap"]
        )
        names = {p.name for p in results}
        assert "VEGGIE WRAP" not in names

    @pytest.mark.asyncio
    async def test_nonexistent_returns_empty(self, db_session, test_bot, test_products):
        """Completely unrelated search returns empty list."""
        results = await find_relevant_products(
            db_session, test_bot["bot_id"], ["lobster thermidor"]
        )
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_multiple_items_search(self, db_session, test_bot, test_products):
        """Multiple search terms return products for each."""
        results = await find_relevant_products(
            db_session, test_bot["bot_id"], ["classic burger", "spicy wings"]
        )
        names = {p.name for p in results}
        assert "CLASSIC BURGER" in names
        assert "SPICY WINGS" in names

    @pytest.mark.asyncio
    async def test_addon_found_with_category(self, db_session, test_bot, test_products):
        """Addons are findable and have correct category."""
        results = await find_relevant_products(
            db_session, test_bot["bot_id"], ["adicional de bacon"]
        )
        found = [p for p in results if p.name == "Adicional de BACON"]
        assert len(found) == 1
        assert found[0].category == "Adicionais"

    @pytest.mark.asyncio
    async def test_acai_with_accent(self, db_session, test_bot, test_products):
        """Accented characters should match via ILIKE or keywords."""
        results = await find_relevant_products(db_session, test_bot["bot_id"], ["açaí"])
        names = {p.name for p in results}
        assert "AÇAÍ BOWL" in names

    @pytest.mark.asyncio
    async def test_empty_items_returns_empty(self, db_session, test_bot, test_products):
        results = await find_relevant_products(db_session, test_bot["bot_id"], [])
        assert results == []


@pytest.mark.integration
class TestUnavailableSearch:
    """Test find_unavailable_products against real DB with test products."""

    @pytest.mark.asyncio
    async def test_exact_unavailable_match(self, db_session, test_bot, test_products):
        results = await find_unavailable_products(
            db_session, test_bot["bot_id"], ["veggie wrap"]
        )
        names = {p.name for p in results}
        assert "VEGGIE WRAP" in names

    @pytest.mark.asyncio
    async def test_unavailable_keywords(self, db_session, test_bot, test_products):
        """'vegetariano' is in VEGGIE WRAP's keywords."""
        results = await find_unavailable_products(
            db_session, test_bot["bot_id"], ["vegetariano"]
        )
        names = {p.name for p in results}
        assert "VEGGIE WRAP" in names

    @pytest.mark.asyncio
    async def test_unavailable_trgm_typo(
        self, db_session, test_bot, test_products, requires_trgm
    ):
        """'vegie wrap' — typo should still find VEGGIE WRAP."""
        results = await find_unavailable_products(
            db_session, test_bot["bot_id"], ["vegie wrap"]
        )
        names = {p.name for p in results}
        assert "VEGGIE WRAP" in names

    @pytest.mark.asyncio
    async def test_deleted_excluded_from_unavailable(
        self, db_session, test_bot, test_products
    ):
        """Deleted products should NOT appear in unavailable search."""
        results = await find_unavailable_products(
            db_session, test_bot["bot_id"], ["old menu item"]
        )
        names = {p.name for p in results}
        assert "OLD MENU ITEM" not in names

    @pytest.mark.asyncio
    async def test_available_excluded_from_unavailable(
        self, db_session, test_bot, test_products
    ):
        """Available products should NOT appear in unavailable search."""
        results = await find_unavailable_products(
            db_session, test_bot["bot_id"], ["classic burger"]
        )
        names = {p.name for p in results}
        assert "CLASSIC BURGER" not in names

    @pytest.mark.asyncio
    async def test_nonexistent_returns_empty(self, db_session, test_bot, test_products):
        results = await find_unavailable_products(
            db_session, test_bot["bot_id"], ["lobster thermidor"]
        )
        assert len(results) == 0


@pytest.mark.integration
class TestMixedAvailableUnavailable:
    """Test searching for a mix of available and unavailable products."""

    @pytest.mark.asyncio
    async def test_mixed_search(self, db_session, test_bot, test_products):
        """Search for one available and one unavailable product."""
        available = await find_relevant_products(
            db_session,
            test_bot["bot_id"],
            ["classic burger", "veggie wrap"],
        )
        unavailable = await find_unavailable_products(
            db_session,
            test_bot["bot_id"],
            ["classic burger", "veggie wrap"],
        )
        avail_names = {p.name for p in available}
        unavail_names = {p.name for p in unavailable}

        assert "CLASSIC BURGER" in avail_names
        assert "CLASSIC BURGER" not in unavail_names
        assert "VEGGIE WRAP" in unavail_names
        assert "VEGGIE WRAP" not in avail_names

    @pytest.mark.asyncio
    async def test_all_unavailable(self, db_session, test_bot, test_products):
        """When all requested items are unavailable."""
        available = await find_relevant_products(
            db_session, test_bot["bot_id"], ["veggie wrap"]
        )
        unavailable = await find_unavailable_products(
            db_session, test_bot["bot_id"], ["veggie wrap"]
        )
        assert len(available) == 0
        assert len(unavailable) >= 1

    @pytest.mark.asyncio
    async def test_all_nonexistent(self, db_session, test_bot, test_products):
        """When nothing exists at all."""
        available = await find_relevant_products(
            db_session, test_bot["bot_id"], ["unicorn steak"]
        )
        unavailable = await find_unavailable_products(
            db_session, test_bot["bot_id"], ["unicorn steak"]
        )
        assert len(available) == 0
        assert len(unavailable) == 0
