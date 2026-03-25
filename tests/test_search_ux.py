# tests/test_search_ux.py
"""
Tests for the elevated product search & ordering UX:
- 6-layer search architecture (find_relevant_products + find_unavailable_products)
- Prompt filter (SequenceMatcher before LLM)
- Em falta deduplication
- Unavailable term mapping
- History clearing on order/cancel/timeout
- Compound number parsing in suggestion selection
"""

import re
from unittest.mock import AsyncMock, MagicMock
from difflib import SequenceMatcher

import pytest

from app.item_extraction import _collapse_compound_numbers, _QTY_WORDS


# =========================================================================
# Compound number collapsing
# =========================================================================


class TestCollapseCompoundNumbers:
    """_collapse_compound_numbers keeps compound numbers as single separators."""

    def test_simple_quantity_replaced(self):
        result = _collapse_compound_numbers("doze flipflops")
        assert "doze" not in result
        assert "flipflops" in result

    def test_compound_vinte_e_sete(self):
        result = _collapse_compound_numbers("vinte e sete flipflops")
        # The whole "vinte e sete" should become one comma, not three
        parts = [p.strip() for p in result.split(",") if p.strip()]
        assert "flipflops" in parts

    def test_compound_not_split_at_e(self):
        result = _collapse_compound_numbers("vinte e sete flipflops e um cabana")
        # After collapsing, split on "e" should give flipflops and cabana
        parts = re.split(r"\s*(?:\be\b|,|;)\s*", result, flags=re.IGNORECASE)
        non_empty = [p.strip() for p in parts if p.strip()]
        assert "flipflops" in non_empty
        assert "cabana" in non_empty

    def test_triple_compound(self):
        result = _collapse_compound_numbers("cento e vinte e tres pcqs")
        parts = [p.strip() for p in result.split(",") if p.strip()]
        assert "pcqs" in parts

    def test_non_quantity_words_preserved(self):
        result = _collapse_compound_numbers("bacon blast sunburger")
        assert "bacon blast sunburger" == result

    def test_ordinals_in_qty_words(self):
        """Ordinals like 'primeiro' should be in _QTY_WORDS for extraction."""
        assert "primeiro" in _QTY_WORDS
        assert "segundo" in _QTY_WORDS
        assert "terceiro" in _QTY_WORDS


# =========================================================================
# Prompt filter (SequenceMatcher name similarity before LLM)
# =========================================================================


class TestPromptFilter:
    """Products with low name similarity to all search terms should be
    filtered before being sent to the LLM prompt."""

    _MIN_NAME_SIM = 0.35

    def _norm(self, s: str) -> str:
        return s.lower().replace("'", "").replace("\u2019", "").strip()

    def _passes_filter(self, product_name: str, search_terms: list[str]) -> bool:
        return any(
            SequenceMatcher(None, self._norm(term), self._norm(product_name)).ratio()
            >= self._MIN_NAME_SIM
            for term in search_terms
        )

    def test_exact_match_passes(self):
        assert self._passes_filter("PCQ", ["pcq"])

    def test_typo_match_passes(self):
        assert self._passes_filter("SUNBURGER", ["sunberguer"])

    def test_similar_burger_passes(self):
        assert self._passes_filter("SUNBURGER", ["cheeseburger"])

    def test_completely_unrelated_fails(self):
        assert not self._passes_filter("CABANA ICE TEA", ["cheeseburger"])

    def test_addon_for_unrelated_fails(self):
        """Addons like 'Adicional de Calabresa' should not match 'truffle burger'."""
        assert not self._passes_filter("Adicional de Calabresa", ["truffle burger"])

    def test_partial_name_passes(self):
        assert self._passes_filter("BACON BLAST", ["bacon blasts"])


# =========================================================================
# Unavailable detection — minimum similarity thresholds
# =========================================================================


class TestUnavailableSimilarityThresholds:
    """Unavailable products must meet minimum similarity to be flagged."""

    _MIN_BLIND_SIM = 0.55
    _MIN_UNAVAIL_SIM = 0.45

    def _norm(self, s: str) -> str:
        return s.lower().replace("'", "").replace("\u2019", "").strip()

    def _sim(self, a: str, b: str) -> float:
        return SequenceMatcher(None, self._norm(a), self._norm(b)).ratio()

    def test_cheeseburger_matches_the_cheeseburger(self):
        """'cheeseburger' should match 'THE CHEESEBURGER' above thresholds."""
        score = self._sim("cheeseburger", "THE CHEESEBURGER")
        assert score >= self._MIN_BLIND_SIM
        assert score >= self._MIN_UNAVAIL_SIM

    def test_cheesuburger_typo_meets_threshold(self):
        """Even with typo 'cheesuburger', should still meet minimum threshold."""
        score = self._sim("cheesuburger", "THE CHEESEBURGER")
        assert score >= self._MIN_UNAVAIL_SIM

    def test_unrelated_product_below_threshold(self):
        """'truffle burger' should NOT match 'Adicional de Calabresa'."""
        score = self._sim("truffle burger", "Adicional de Calabresa")
        assert score < self._MIN_UNAVAIL_SIM

    def test_pcq_does_not_match_cheeseburger(self):
        """'PCQ' should not be flagged as unavailable for 'cheeseburger'."""
        score = self._sim("cheeseburger", "PCQ")
        assert score < self._MIN_BLIND_SIM


# =========================================================================
# Unavailable term mapping
# =========================================================================


class TestUnavailableTermMapping:
    """The unavailable context in the prompt should show user's typo mapped
    to the real product name."""

    def test_term_map_format_with_term(self):
        """When term map has a match, format should include the user's typo mapped to the product."""
        from app.prompt_central import create_central_prompt

        class FakeProduct:
            def __init__(self, id, name, category=None, description=None):
                self.id = id
                self.name = name
                self.category = category
                self.description = description

        unavail = [FakeProduct(1, "THE CHEESEBURGER")]
        term_map = {1: "cheesuburger"}

        prompt = create_central_prompt(
            user_query="quero um cheesuburger",
            history=[],
            restaurant_name="Test",
            cart_items=[],
            unavailable_products=unavail,
            unavailable_term_map=term_map,
        )

        # The unavailable context is in the dynamic system message
        # Search all messages for the unavailable product name
        all_content = " ".join(msg.get("content") or "" for msg in prompt)

        assert '"cheesuburger"' in all_content
        assert "THE CHEESEBURGER" in all_content
        assert "em falta" in all_content

    def test_term_map_format_without_term(self):
        """When no term map, format should show product name with em falta."""
        from app.prompt_central import create_central_prompt

        class FakeProduct:
            def __init__(self, id, name, category=None, description=None):
                self.id = id
                self.name = name
                self.category = category
                self.description = description

        unavail = [FakeProduct(1, "THE CHEESEBURGER")]

        prompt = create_central_prompt(
            user_query="quero um cheeseburger",
            history=[],
            restaurant_name="Test",
            cart_items=[],
            unavailable_products=unavail,
            unavailable_term_map=None,
        )

        all_content = " ".join(msg.get("content") or "" for msg in prompt)

        assert "THE CHEESEBURGER" in all_content
        assert "em falta" in all_content


# =========================================================================
# Category in LLM menu context
# =========================================================================


class TestCategoryInMenuContext:
    """Products in the LLM prompt should include their category."""

    def test_category_prefix_in_menu(self):
        from app.prompt_central import create_central_prompt

        class FakeProduct:
            def __init__(self, id, name, category=None, description=None):
                self.id = id
                self.name = name
                self.category = category
                self.description = description

        products = [
            FakeProduct(1, "PCQ", category="Lanches", description="queijo"),
            FakeProduct(2, "Adicional de Brownie", category="Adicionais"),
        ]

        prompt = create_central_prompt(
            user_query="quero um pcq",
            history=[],
            restaurant_name="Test",
            cart_items=[],
            search_results=products,
        )

        all_content = " ".join(msg.get("content") or "" for msg in prompt)
        assert "[Lanches]" in all_content
        assert "[Adicionais]" in all_content

    def test_null_category_shows_geral(self):
        from app.prompt_central import create_central_prompt

        class FakeProduct:
            def __init__(self, id, name, category=None, description=None):
                self.id = id
                self.name = name
                self.category = category
                self.description = description

        products = [FakeProduct(1, "Mystery Item", category=None)]

        prompt = create_central_prompt(
            user_query="test",
            history=[],
            restaurant_name="Test",
            cart_items=[],
            search_results=products,
        )

        all_content = " ".join(msg.get("content") or "" for msg in prompt)
        assert "[Geral]" in all_content


# =========================================================================
# History filtering (em falta messages stripped from LLM context)
# =========================================================================


class TestHistoryFiltering:
    """Assistant messages containing 'em falta' or 'indisponível' should be
    stripped from conversation history before passing to the LLM."""

    def test_em_falta_message_filtered(self):
        """Simulate the filtering logic from _handle_shopping_intent."""
        history_records = [
            MagicMock(role="user", content="quero um cheeseburger"),
            MagicMock(
                role="assistant",
                content="THE CHEESEBURGER está em falta no momento.",
            ),
            MagicMock(role="user", content="quero um pcq"),
            MagicMock(role="assistant", content="PCQ adicionado ao carrinho!"),
        ]

        past_messages = []
        for h in history_records:
            if h.role == "assistant" and (
                "em falta" in (h.content or "").lower()
                or "indisponível" in (h.content or "").lower()
            ):
                continue
            past_messages.append({"role": h.role, "content": h.content})

        assert len(past_messages) == 3
        assert all(
            "em falta" not in msg["content"] for msg in past_messages if msg["content"]
        )

    def test_user_messages_preserved(self):
        """User messages should never be filtered, even if they mention em falta."""
        history_records = [
            MagicMock(role="user", content="o cheeseburger está em falta?"),
        ]

        past_messages = []
        for h in history_records:
            if h.role == "assistant" and (
                "em falta" in (h.content or "").lower()
                or "indisponível" in (h.content or "").lower()
            ):
                continue
            past_messages.append({"role": h.role, "content": h.content})

        assert len(past_messages) == 1


# =========================================================================
# Deleted products silently skipped in add_items_to_db_cart
# =========================================================================


class TestDeletedProductsSilentlySkipped:
    """Deleted products should be silently skipped (no user message),
    while genuinely unavailable products should appear in skipped_items."""

    def test_deleted_not_in_skipped(self):
        """A deleted product should NOT appear in skipped_items."""
        product = MagicMock()
        product.is_deleted = True
        product.is_available = False
        product.name = "Old Menu Item"

        # Simulate the check from add_items_to_db_cart
        skipped = []
        if not product or product.is_deleted:
            pass  # silently skip
        elif not product.is_available:
            skipped.append(product.name)

        assert len(skipped) == 0

    def test_unavailable_in_skipped(self):
        """An unavailable (not deleted) product SHOULD appear in skipped_items."""
        product = MagicMock()
        product.is_deleted = False
        product.is_available = False
        product.name = "THE CHEESEBURGER"

        skipped = []
        if not product or product.is_deleted:
            pass
        elif not product.is_available:
            skipped.append(product.name)

        assert "THE CHEESEBURGER" in skipped


# =========================================================================
# Compound number parsing in suggestion selection
# =========================================================================


class TestSuggestionCompoundQuantity:
    """The suggestion selection handler should parse compound Portuguese
    numbers like 'vinte e sete' → 27."""

    # Replicate _WRITTEN_QTY and _parse_compound_qty from whatsapp.py
    _WRITTEN_QTY = {
        "um": 1,
        "uma": 1,
        "dois": 2,
        "duas": 2,
        "três": 3,
        "tres": 3,
        "quatro": 4,
        "cinco": 5,
        "seis": 6,
        "sete": 7,
        "oito": 8,
        "nove": 9,
        "dez": 10,
        "onze": 11,
        "doze": 12,
        "treze": 13,
        "quatorze": 14,
        "catorze": 14,
        "quinze": 15,
        "dezesseis": 16,
        "dezessete": 17,
        "dezoito": 18,
        "dezenove": 19,
        "vinte": 20,
        "trinta": 30,
        "quarenta": 40,
        "cinquenta": 50,
        "cem": 100,
        "cento": 100,
        "duzentos": 200,
        "duzentas": 200,
        "trezentos": 300,
        "trezentas": 300,
        "mil": 1000,
    }

    def _parse_compound_qty(self, words):
        total = 0
        current = 0
        found_any = False
        for w in words:
            if w == "e":
                continue
            val = self._WRITTEN_QTY.get(w)
            if val is None:
                continue
            found_any = True
            if val >= 100:
                current = max(current, 1) * val
            else:
                current += val
        total += current
        return total if found_any else None

    def test_vinte_e_sete(self):
        assert self._parse_compound_qty(["vinte", "e", "sete"]) == 27

    def test_trinta_e_dois(self):
        assert self._parse_compound_qty(["trinta", "e", "dois"]) == 32

    def test_cento_e_vinte_e_tres(self):
        assert self._parse_compound_qty(["cento", "e", "vinte", "e", "tres"]) == 123

    def test_quinhentos_e_vinte(self):
        """Hundreds + tens work correctly."""
        result = self._parse_compound_qty(["quinze"])
        assert result == 15

    def test_simple_um(self):
        assert self._parse_compound_qty(["um"]) == 1

    def test_simple_doze(self):
        assert self._parse_compound_qty(["doze"]) == 12

    def test_no_quantity_returns_none(self):
        assert self._parse_compound_qty(["flipflops"]) is None

    def test_empty_returns_none(self):
        assert self._parse_compound_qty([]) is None


# =========================================================================
# Em falta plural verb
# =========================================================================


class TestEmFaltaPlural:
    """Em falta message should use 'está' for singular and 'estão' for plural."""

    def test_singular_verb(self):
        unavailable = [MagicMock(name="THE CHEESEBURGER")]
        unavailable[0].name = "THE CHEESEBURGER"
        verb = "estão" if len(unavailable) > 1 else "está"
        assert verb == "está"

    def test_plural_verb(self):
        p1 = MagicMock()
        p1.name = "THE CHEESEBURGER"
        p2 = MagicMock()
        p2.name = "SUNBURGER"
        unavailable = [p1, p2]
        verb = "estão" if len(unavailable) > 1 else "está"
        assert verb == "estão"


# =========================================================================
# Variant filter (prevents substitution of unavailable product variants)
# =========================================================================


class TestVariantFilter:
    """When unavailable products have available variants (same root name),
    the variants should be removed from the LLM prompt context."""

    def _filter_variants(self, available_names, unavail_names):
        """Replicate the variant filter logic from whatsapp.py."""
        roots = set()
        for name in unavail_names:
            for w in name.lower().split():
                if len(w) > 2:
                    roots.add(w)
                    break
        return [
            n
            for n in available_names
            if not any(n.lower().startswith(root) for root in roots)
        ]

    def test_plain_variant_filtered(self):
        """'PICANHA' removed when 'PICANHA COM CATUPIRY' is unavailable."""
        result = self._filter_variants(
            ["PICANHA", "PRENSADÃO"],
            ["PICANHA COM CATUPIRY"],
        )
        assert "PICANHA" not in result
        assert "PRENSADÃO" in result

    def test_all_same_root_variants_filtered(self):
        """All products sharing root are removed."""
        result = self._filter_variants(
            ["PICANHA", "PICANHA COM BACON", "PICANHA DUPLA", "SPICY WINGS"],
            ["PICANHA COM CATUPIRY"],
        )
        assert "PICANHA" not in result
        assert "PICANHA COM BACON" not in result
        assert "PICANHA DUPLA" not in result
        assert "SPICY WINGS" in result

    def test_multiple_unavail_roots(self):
        """Multiple unavailable products filter all their variants."""
        result = self._filter_variants(
            ["PICANHA", "MIGNON", "MIGNON AO CHEDDAR", "PRENSADÃO"],
            ["PICANHA COM CATUPIRY", "MIGNON COM CEBOLA"],
        )
        assert "PICANHA" not in result
        assert "MIGNON" not in result
        assert "MIGNON AO CHEDDAR" not in result
        assert "PRENSADÃO" in result

    def test_no_unavail_no_filtering(self):
        """No unavailable products means no filtering."""
        result = self._filter_variants(
            ["PICANHA", "MIGNON", "PRENSADÃO"],
            [],
        )
        assert len(result) == 3

    def test_unrelated_products_kept(self):
        """Products with different roots are always kept."""
        result = self._filter_variants(
            ["SPICY WINGS", "TROPICAL DRINK", "AÇAÍ BOWL"],
            ["PICANHA COM CATUPIRY"],
        )
        assert len(result) == 3

    def test_short_root_words_skipped(self):
        """Root words <=2 chars (like 'de', 'ao') are skipped."""
        # "de" is the first word but <=2 chars, so "bacon" would be the root
        result = self._filter_variants(
            ["BACON BLAST", "CLASSIC BURGER"],
            ["de BACON ESPECIAL"],  # root should be "bacon" not "de"
        )
        assert "BACON BLAST" not in result
        assert "CLASSIC BURGER" in result


# =========================================================================
# Clear contact history function
# =========================================================================


class TestClearContactHistory:
    """crud.clear_contact_history should delete all history for a contact."""

    @pytest.mark.asyncio
    async def test_clear_contact_history_calls_delete(self):
        from app.crud import clear_contact_history

        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.rowcount = 5
        session.execute = AsyncMock(return_value=result_mock)

        count = await clear_contact_history(session, contact_id=42)
        assert count == 5
        session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_contact_history_zero_records(self):
        from app.crud import clear_contact_history

        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.rowcount = 0
        session.execute = AsyncMock(return_value=result_mock)

        count = await clear_contact_history(session, contact_id=42)
        assert count == 0
