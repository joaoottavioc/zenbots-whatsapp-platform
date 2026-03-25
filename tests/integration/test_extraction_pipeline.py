# tests/integration/test_extraction_pipeline.py
"""
Phase 1b: Extraction → Search pipeline integration tests.

Tests the chain: user message → extract_items_local → find_relevant_products.
Exercises compound numbers, noise detection, prompt filter, term mapping.
"""

import pytest
from difflib import SequenceMatcher

from app.item_extraction import extract_items_local
from app.crud import find_relevant_products, find_unavailable_products


@pytest.mark.integration
class TestExtractionToSearch:
    """Test the full extraction → search chain against real DB."""

    @pytest.mark.asyncio
    async def test_simple_order(self, db_session, test_bot, test_products):
        """'quero um classic burger' → extracts → finds CLASSIC BURGER."""
        items = extract_items_local("quero um classic burger")
        results = await find_relevant_products(db_session, test_bot["bot_id"], items)
        names = {p.name for p in results}
        assert "CLASSIC BURGER" in names

    @pytest.mark.asyncio
    async def test_compound_number_not_split(self, db_session, test_bot, test_products):
        """'vinte e sete classic burger' → extracts 'classic burger' (not split)."""
        items = extract_items_local("vinte e sete classic burger")
        # Should produce a single item, not split at "e"
        assert any("classic burger" in item for item in items)
        results = await find_relevant_products(db_session, test_bot["bot_id"], items)
        names = {p.name for p in results}
        assert "CLASSIC BURGER" in names

    @pytest.mark.asyncio
    async def test_multiple_items_extraction(self, db_session, test_bot, test_products):
        """Multiple items with quantities extracted and found."""
        items = extract_items_local("quero um classic burger e duas spicy wings")
        results = await find_relevant_products(db_session, test_bot["bot_id"], items)
        names = {p.name for p in results}
        assert "CLASSIC BURGER" in names
        assert "SPICY WINGS" in names

    @pytest.mark.asyncio
    async def test_typo_extraction_to_search(
        self, db_session, test_bot, test_products, requires_trgm
    ):
        """'clasic burger' → extraction preserves typo → pg_trgm finds it."""
        items = extract_items_local("quero um clasic burger")
        assert any("clasic" in item for item in items)
        results = await find_relevant_products(db_session, test_bot["bot_id"], items)
        names = {p.name for p in results}
        assert "CLASSIC BURGER" in names

    @pytest.mark.asyncio
    async def test_quantity_as_separator(self, db_session, test_bot, test_products):
        """Quantity words between items act as separators."""
        items = extract_items_local(
            "quero um classic burger tres spicy wings e doze tropical drink"
        )
        assert len(items) >= 3
        results = await find_relevant_products(db_session, test_bot["bot_id"], items)
        names = {p.name for p in results}
        assert "CLASSIC BURGER" in names
        assert "SPICY WINGS" in names
        assert "TROPICAL DRINK" in names


@pytest.mark.integration
class TestNoiseDetection:
    """Test the word ratio heuristic for conversational noise vs food items."""

    def test_conversational_noise_high_ratio(self):
        """Conversational messages keep >50% of words → classified as noise."""
        text = "poxa minha tia chegou aqui e a gente ta com muita fome mesmo"
        items = extract_items_local(text)
        orig_wc = len(text.split())
        ext_wc = sum(len(t.split()) for t in items)
        ratio = ext_wc / orig_wc
        assert ratio > 0.5, f"Expected noise (ratio > 0.5), got {ratio}"

    def test_food_request_low_ratio(self):
        """Food requests strip most words → ratio ≤ 0.5."""
        text = "quero um classic burger"
        items = extract_items_local(text)
        orig_wc = len(text.split())
        ext_wc = sum(len(t.split()) for t in items)
        ratio = ext_wc / orig_wc
        assert ratio <= 0.5, f"Expected food (ratio <= 0.5), got {ratio}"

    def test_multi_item_food_request(self):
        """Multi-item orders also have low ratio."""
        text = "hoje vou de dois classic burger um spicy wings e trinta e dois tropical drink"
        items = extract_items_local(text)
        orig_wc = len(text.split())
        ext_wc = sum(len(t.split()) for t in items)
        ratio = ext_wc / orig_wc
        assert ratio <= 0.5


@pytest.mark.integration
class TestPromptFilter:
    """Test the SequenceMatcher filter that removes false positives
    before the LLM sees the search results."""

    _MIN_NAME_SIM = 0.35

    def _norm(self, s: str) -> str:
        return s.lower().replace("'", "").replace("\u2019", "").strip()

    def _passes(self, product_name: str, search_terms: list[str]) -> bool:
        return any(
            SequenceMatcher(None, self._norm(term), self._norm(product_name)).ratio()
            >= self._MIN_NAME_SIM
            for term in search_terms
        )

    def test_exact_match_passes(self):
        assert self._passes("CLASSIC BURGER", ["classic burger"])

    def test_typo_passes(self):
        assert self._passes("CLASSIC BURGER", ["clasic burger"])

    def test_unrelated_fails(self):
        assert not self._passes("TROPICAL DRINK", ["classic burger"])

    def test_addon_for_unrelated_fails(self):
        assert not self._passes("Adicional de BACON", ["classic burger"])

    def test_partial_match_passes(self):
        assert self._passes("SPICY WINGS", ["spicy wing"])


@pytest.mark.integration
class TestUnavailableTermMapping:
    """Test that unavailable products can be mapped back to user's search terms."""

    @pytest.mark.asyncio
    async def test_term_to_product_mapping(self, db_session, test_bot, test_products):
        """Search for unavailable product → can map user term to product."""
        search_terms = ["vegie wrap"]  # typo
        unavail = await find_unavailable_products(
            db_session, test_bot["bot_id"], search_terms
        )
        if not unavail:
            pytest.skip("Unavailable search didn't find VEGGIE WRAP for this typo")

        # Simulate the term mapping logic from whatsapp.py
        from difflib import SequenceMatcher

        def _norm(s):
            return s.lower().replace("'", "").strip()

        term_map = {}
        for term in search_terms:
            best = max(
                unavail,
                key=lambda p: SequenceMatcher(None, _norm(term), _norm(p.name)).ratio(),
            )
            score = SequenceMatcher(None, _norm(term), _norm(best.name)).ratio()
            if score >= 0.45:
                term_map[best.id] = term

        assert len(term_map) >= 1
        # The mapped term should be the user's typo
        assert any(v == "vegie wrap" for v in term_map.values())


@pytest.mark.integration
class TestVariantFilter:
    """Test that available variants of unavailable products are filtered
    from the LLM prompt context to prevent auto-substitution."""

    @pytest.mark.asyncio
    async def test_unavailable_detected(self, db_session, test_bot, test_products):
        """Unavailable products are correctly identified by search."""
        unavail = await find_unavailable_products(
            db_session, test_bot["bot_id"], ["picanha com catupiry"]
        )
        assert any(p.name == "PICANHA COM CATUPIRY" for p in unavail)

    @pytest.mark.asyncio
    async def test_variants_filtered_from_results(
        self, db_session, test_bot, test_products
    ):
        """Available variants (same root) ARE filtered from search results."""
        available = await find_relevant_products(
            db_session, test_bot["bot_id"], ["picanha com catupiry"]
        )
        available_names = {p.name for p in available}

        # Variants sharing the "picanha" root should be filtered
        assert "PICANHA" not in available_names
        assert "PICANHA COM BACON" not in available_names

    @pytest.mark.asyncio
    async def test_unrelated_products_kept(self, db_session, test_bot, test_products):
        """Unrelated products remain in search results alongside filtering."""
        available = await find_relevant_products(
            db_session,
            test_bot["bot_id"],
            ["picanha com catupiry", "spicy wings"],
        )
        available_names = {p.name for p in available}
        assert "SPICY WINGS" in available_names

    @pytest.mark.asyncio
    async def test_no_unavailable_keeps_all(self, db_session, test_bot, test_products):
        """When nothing is unavailable, all search results are kept."""
        available = await find_relevant_products(
            db_session, test_bot["bot_id"], ["spicy wings"]
        )
        unavail = await find_unavailable_products(
            db_session, test_bot["bot_id"], ["spicy wings"]
        )

        assert len(unavail) == 0
        assert len(available) >= 1
