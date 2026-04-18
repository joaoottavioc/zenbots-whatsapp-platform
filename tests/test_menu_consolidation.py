"""Tests for post-extraction category consolidation (option B)."""

import json
from unittest.mock import AsyncMock, patch

import pytest


SAMPLE_PRODUCTS = [
    {"name": "Bife Ancho", "price": 198.0, "category": "Cortes Clássicos"},
    {"name": "Picanha", "price": 598.0, "category": "Cortes Especiais"},
    {"name": "Fraldinha", "price": 388.0, "category": "Carnes"},
    {"name": "Leitão", "price": 798.0, "category": "Pratos Principais"},
    {"name": "Batata Rústica", "price": 38.0, "category": "Porções"},
]


def _llm_response(products, category_for_index):
    """Build a well-formed consolidation JSON response."""
    canonical = sorted(set(category_for_index.values()))
    return json.dumps(
        {
            "canonical_categories": canonical,
            "assignments": [
                {"index": i, "category": category_for_index[i]}
                for i in range(len(products))
            ],
        }
    )


class TestApplyConsolidation:
    def test_happy_path_rewrites_categories(self):
        from app.menu_extraction import _apply_consolidation

        raw = _llm_response(
            SAMPLE_PRODUCTS,
            {0: "Cortes", 1: "Cortes", 2: "Cortes", 3: "Cortes", 4: "Acompanhamentos"},
        )
        result = _apply_consolidation(SAMPLE_PRODUCTS, raw)
        assert result is not None
        assert [p["category"] for p in result[:4]] == ["Cortes"] * 4
        assert result[4]["category"] == "Acompanhamentos"
        # Names/prices untouched
        for original, rewritten in zip(SAMPLE_PRODUCTS, result):
            assert original["name"] == rewritten["name"]
            assert original["price"] == rewritten["price"]

    def test_returns_none_on_invalid_json(self):
        from app.menu_extraction import _apply_consolidation

        assert _apply_consolidation(SAMPLE_PRODUCTS, "not json at all") is None

    def test_returns_none_on_size_mismatch(self):
        from app.menu_extraction import _apply_consolidation

        raw = json.dumps(
            {
                "canonical_categories": ["Cortes"],
                "assignments": [{"index": 0, "category": "Cortes"}],
            }
        )
        assert _apply_consolidation(SAMPLE_PRODUCTS, raw) is None

    def test_returns_none_on_category_outside_canonical_set(self):
        from app.menu_extraction import _apply_consolidation

        raw = json.dumps(
            {
                "canonical_categories": ["Cortes", "Acompanhamentos"],
                "assignments": [
                    {"index": i, "category": "HallucinatedCategory"}
                    for i in range(len(SAMPLE_PRODUCTS))
                ],
            }
        )
        assert _apply_consolidation(SAMPLE_PRODUCTS, raw) is None

    def test_returns_none_on_duplicate_index(self):
        from app.menu_extraction import _apply_consolidation

        raw = json.dumps(
            {
                "canonical_categories": ["Cortes"],
                "assignments": [
                    {"index": 0, "category": "Cortes"},
                    {"index": 0, "category": "Cortes"},
                    {"index": 2, "category": "Cortes"},
                    {"index": 3, "category": "Cortes"},
                    {"index": 4, "category": "Cortes"},
                ],
            }
        )
        assert _apply_consolidation(SAMPLE_PRODUCTS, raw) is None

    def test_returns_none_on_out_of_range_index(self):
        from app.menu_extraction import _apply_consolidation

        raw = json.dumps(
            {
                "canonical_categories": ["Cortes"],
                "assignments": [{"index": i, "category": "Cortes"} for i in range(4)]
                + [{"index": 99, "category": "Cortes"}],
            }
        )
        assert _apply_consolidation(SAMPLE_PRODUCTS, raw) is None

    def test_returns_none_when_too_many_canonical_categories(self):
        from app.menu_extraction import _apply_consolidation

        too_many = [f"Cat{i}" for i in range(20)]
        raw = json.dumps(
            {
                "canonical_categories": too_many,
                "assignments": [
                    {"index": i, "category": too_many[0]}
                    for i in range(len(SAMPLE_PRODUCTS))
                ],
            }
        )
        assert _apply_consolidation(SAMPLE_PRODUCTS, raw) is None


class TestConsolidateCategories:
    @pytest.mark.asyncio
    async def test_noop_when_too_few_products(self):
        from app import menu_extraction

        tiny = SAMPLE_PRODUCTS[:2]
        result = await menu_extraction._consolidate_categories(tiny)
        assert result == tiny

    @pytest.mark.asyncio
    async def test_fallback_on_llm_error(self):
        from app import menu_extraction

        with patch(
            "app.menu_extraction.get_forced_tool_call",
            new=AsyncMock(side_effect=RuntimeError("API down")),
        ):
            result = await menu_extraction._consolidate_categories(SAMPLE_PRODUCTS)
        assert result == SAMPLE_PRODUCTS

    @pytest.mark.asyncio
    async def test_applies_consolidation_on_valid_response(self):
        from app import menu_extraction

        raw = _llm_response(
            SAMPLE_PRODUCTS,
            {0: "Cortes", 1: "Cortes", 2: "Cortes", 3: "Cortes", 4: "Acompanhamentos"},
        )
        with patch(
            "app.menu_extraction.get_forced_tool_call",
            new=AsyncMock(return_value=raw),
        ):
            result = await menu_extraction._consolidate_categories(SAMPLE_PRODUCTS)

        assert len(result) == len(SAMPLE_PRODUCTS)
        assert {p["category"] for p in result} == {"Cortes", "Acompanhamentos"}
        # Names & prices must be untouched
        for original, rewritten in zip(SAMPLE_PRODUCTS, result):
            assert original["name"] == rewritten["name"]
            assert original["price"] == rewritten["price"]

    @pytest.mark.asyncio
    async def test_fallback_on_invalid_llm_output(self):
        from app import menu_extraction

        with patch(
            "app.menu_extraction.get_forced_tool_call",
            new=AsyncMock(return_value="garbage response"),
        ):
            result = await menu_extraction._consolidate_categories(SAMPLE_PRODUCTS)
        assert result == SAMPLE_PRODUCTS

    @pytest.mark.asyncio
    async def test_existing_categories_are_passed_to_prompt(self):
        from app import menu_extraction

        captured = {}

        async def fake_call(messages, tool):
            captured["messages"] = messages
            return _llm_response(
                SAMPLE_PRODUCTS,
                {
                    0: "Cortes",
                    1: "Cortes",
                    2: "Cortes",
                    3: "Cortes",
                    4: "Acompanhamentos",
                },
            )

        with patch(
            "app.menu_extraction.get_forced_tool_call",
            new=fake_call,
        ):
            await menu_extraction._consolidate_categories(
                SAMPLE_PRODUCTS, existing_categories=["Cortes", "Acompanhamentos"]
            )

        user_prompt = captured["messages"][-1]["content"]
        assert "CATEGORIAS JÁ USADAS" in user_prompt
        assert "- Cortes" in user_prompt
        assert "- Acompanhamentos" in user_prompt
