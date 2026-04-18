"""Tests for Phase 1 + 2 accuracy improvements in menu extraction.

Covers:
- Dedup by (name, price) tuple — size variants survive (Q)
- Empty-page retry with retry_hint=True (C)
- Page-text passed to vision call as hybrid context (D)
- Post-extraction validator logs concerns (M)
"""

from unittest.mock import AsyncMock, patch

import pytest


class TestDedupByNameAndPrice:
    def test_same_name_different_price_survive(self):
        """Salada Julienne Individual and Salada Julienne Serve 2-3 must
        NOT collapse: same name, different prices, different items."""
        from app.menu_extraction import _dedup_products

        products = [
            {"name": "Salada Julienne", "price": 48.0, "description": "individual"},
            {"name": "Salada Julienne", "price": 62.0, "description": "serve 2-3"},
        ]
        result = _dedup_products(products)
        assert len(result) == 2
        prices = sorted(p["price"] for p in result)
        assert prices == [48.0, 62.0]

    def test_same_name_same_price_collapse_case_and_accents(self):
        """Case and accent variants (normalized by NFKD+lower) at same price collapse."""
        from app.menu_extraction import _dedup_products

        products = [
            {"name": "QUEIJO COALHO", "price": 32.0, "description": ""},
            {"name": "Queijo Coalho", "price": 32.0, "description": "a detailed desc"},
            {"name": "Filé Mignon", "price": 138.0, "description": "short"},
            {"name": "File Mignon", "price": 138.0, "description": "longer desc"},
        ]
        result = _dedup_products(products)
        assert len(result) == 2
        # Keeps the richer description in both pairs
        coalho = next(p for p in result if "coalho" in p["name"].lower())
        assert coalho["description"] == "a detailed desc"
        file_m = next(p for p in result if "mignon" in p["name"].lower())
        assert file_m["description"] == "longer desc"

    def test_price_variation_tolerance(self):
        """Prices 68.00 and 68.00 hash to the same rounded key."""
        from app.menu_extraction import _dedup_products

        products = [
            {"name": "Foo", "price": 68.001, "description": ""},
            {"name": "Foo", "price": 68.0, "description": ""},
        ]
        result = _dedup_products(products)
        assert len(result) == 1

    def test_empty_name_skipped(self):
        from app.menu_extraction import _dedup_products

        products = [
            {"name": "", "price": 10.0},
            {"name": "Real Item", "price": 20.0},
        ]
        result = _dedup_products(products)
        assert len(result) == 1
        assert result[0]["name"] == "Real Item"

    def test_invalid_price_defaults_to_zero(self):
        """Non-numeric prices still work (all 0.0 collide on price key)."""
        from app.menu_extraction import _dedup_products

        products = [
            {"name": "A", "price": "not a number"},
            {"name": "A", "price": None},
        ]
        # Both normalize to price=0.0 → same key → collapse
        result = _dedup_products(products)
        assert len(result) == 1


class TestExtractProductsFromImagePrompt:
    """Smoke-test the prompt construction; avoids real OpenAI calls."""

    @pytest.mark.asyncio
    async def test_retry_hint_adds_look_again_framing(self):
        from app import openai_client

        captured = {}

        def fake_sync_call():
            # Inspect the prompt passed to the OpenAI client
            raise RuntimeError("short-circuit")

        def make_sync(messages, **kwargs):
            captured["messages"] = messages
            raise RuntimeError("short-circuit")

        # Patch the sync wrapper with something that captures then raises
        real_create = openai_client.client_openai_api.chat.completions.create

        def spy(**kwargs):
            captured["messages"] = kwargs["messages"]
            raise RuntimeError("stop")

        with patch.object(
            openai_client.client_openai_api.chat.completions,
            "create",
            side_effect=spy,
        ):
            result = await openai_client.extract_products_from_image(
                b"fakeimg", "image/jpeg", retry_hint=True
            )
        assert result == []  # graceful failure
        prompt = captured["messages"][0]["content"][0]["text"]
        assert "primeira extração desta página retornou 0 produtos" in prompt

        # Unused — silences "assigned but never used" tooling
        _ = (fake_sync_call, make_sync, real_create)

    @pytest.mark.asyncio
    async def test_page_text_is_included_in_prompt(self):
        from app import openai_client

        captured = {}

        def spy(**kwargs):
            captured["messages"] = kwargs["messages"]
            raise RuntimeError("stop")

        with patch.object(
            openai_client.client_openai_api.chat.completions,
            "create",
            side_effect=spy,
        ):
            await openai_client.extract_products_from_image(
                b"fakeimg",
                "image/jpeg",
                page_text="Picanha R$ 598,00\nBife Ancho R$ 198,00",
            )

        prompt = captured["messages"][0]["content"][0]["text"]
        assert "TEXTO DESTA PÁGINA" in prompt
        assert "Picanha R$ 598,00" in prompt

    @pytest.mark.asyncio
    async def test_default_prompt_has_chain_of_thought(self):
        from app import openai_client

        captured = {}

        def spy(**kwargs):
            captured["messages"] = kwargs["messages"]
            raise RuntimeError("stop")

        with patch.object(
            openai_client.client_openai_api.chat.completions,
            "create",
            side_effect=spy,
        ):
            await openai_client.extract_products_from_image(b"fakeimg", "image/jpeg")

        prompt = captured["messages"][0]["content"][0]["text"]
        assert "PROCESSO" in prompt
        assert "Liste TODOS os nomes" in prompt
        assert "Liste TODOS os preços" in prompt


class TestCountPricePatterns:
    def test_counts_brazilian_prices(self):
        from app.menu_extraction import _count_price_patterns

        text = "BATATA AOS MURROS 42.00\nBATATA CASCÃO 38,00\nR$ 32.00"
        assert _count_price_patterns(text) == 3

    def test_empty_text_returns_zero(self):
        from app.menu_extraction import _count_price_patterns

        assert _count_price_patterns("") == 0
        assert _count_price_patterns(None) == 0


class TestExpectedItemCountHint:
    @pytest.mark.asyncio
    async def test_first_pass_soft_hint(self):
        from app import openai_client

        captured = {}

        def spy(**kwargs):
            captured["messages"] = kwargs["messages"]
            raise RuntimeError("stop")

        with patch.object(
            openai_client.client_openai_api.chat.completions,
            "create",
            side_effect=spy,
        ):
            await openai_client.extract_products_from_image(
                b"fakeimg", "image/jpeg", expected_item_count=9
            )

        prompt = captured["messages"][0]["content"][0]["text"]
        assert "9 padrões de preço" in prompt
        # Soft hint — no "ATENÇÃO" framing on first pass
        assert "ATENÇÃO" not in prompt

    @pytest.mark.asyncio
    async def test_retry_with_count_is_aggressive(self):
        from app import openai_client

        captured = {}

        def spy(**kwargs):
            captured["messages"] = kwargs["messages"]
            raise RuntimeError("stop")

        with patch.object(
            openai_client.client_openai_api.chat.completions,
            "create",
            side_effect=spy,
        ):
            await openai_client.extract_products_from_image(
                b"fakeimg",
                "image/jpeg",
                retry_hint=True,
                expected_item_count=9,
            )

        prompt = captured["messages"][0]["content"][0]["text"]
        assert "ATENÇÃO" in prompt
        assert "9 padrões de preço" in prompt
        assert "Os itens EXISTEM" in prompt
        # Aggressive version tells the model NOT to return empty, rather
        # than leaving the escape hatch "se realmente não há, retorne vazio".
        assert "se realmente não há" not in prompt.lower()
        assert "Não retorne lista vazia" in prompt

    @pytest.mark.asyncio
    async def test_retry_without_count_keeps_escape_hatch(self):
        from app import openai_client

        captured = {}

        def spy(**kwargs):
            captured["messages"] = kwargs["messages"]
            raise RuntimeError("stop")

        with patch.object(
            openai_client.client_openai_api.chat.completions,
            "create",
            side_effect=spy,
        ):
            await openai_client.extract_products_from_image(
                b"fakeimg",
                "image/jpeg",
                retry_hint=True,
                # no expected_item_count — model has no target
            )

        prompt = captured["messages"][0]["content"][0]["text"]
        assert "ATENÇÃO" in prompt
        # Without a count, keep the "maybe nothing here" safety net
        assert "retorne lista vazia" in prompt.lower()


class TestPostExtractionValidator:
    @pytest.mark.asyncio
    async def test_skipped_for_tiny_product_list(self):
        """Fewer than 3 products — skip the validator."""
        from app.menu_extraction import _validate_extraction

        with patch(
            "app.openai_client.get_extraction_response",
            new=AsyncMock(return_value='{"likely_missing":[],"suspicious_prices":[]}'),
        ) as mock_call:
            await _validate_extraction(
                products=[{"name": "A", "price": 10.0}],
                raw_text="some text with prices R$ 10.00",
                bot_id=1,
            )
        mock_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipped_for_empty_raw_text(self):
        from app.menu_extraction import _validate_extraction

        products = [{"name": f"Item {i}", "price": 10.0 + i} for i in range(5)]
        with patch(
            "app.openai_client.get_extraction_response",
            new=AsyncMock(return_value="{}"),
        ) as mock_call:
            await _validate_extraction(products=products, raw_text="   ", bot_id=1)
        mock_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_invokes_llm_and_does_not_raise_on_failure(self):
        from app.menu_extraction import _validate_extraction

        products = [{"name": f"Item {i}", "price": 10.0 + i} for i in range(5)]
        with patch(
            "app.openai_client.get_extraction_response",
            new=AsyncMock(side_effect=RuntimeError("API down")),
        ):
            # Must not raise — validator is best-effort
            await _validate_extraction(
                products=products,
                raw_text="Menu text with lots of prices R$ 12,90 R$ 15,00 R$ 22,00",
                bot_id=1,
            )

    @pytest.mark.asyncio
    async def test_logs_concerns_when_present(self, caplog):
        import logging

        from app.menu_extraction import _validate_extraction

        products = [{"name": f"Item {i}", "price": 10.0 + i} for i in range(5)]
        response = (
            '{"likely_missing":["Coca Zero"],"suspicious_prices":["Item 2: negative"]}'
        )

        with (
            caplog.at_level(logging.INFO, logger="app.menu_extraction"),
            patch(
                "app.openai_client.get_extraction_response",
                new=AsyncMock(return_value=response),
            ),
        ):
            await _validate_extraction(
                products=products,
                raw_text="Menu text with prices R$ 10,00 R$ 15,00 R$ 20,00",
                bot_id=42,
            )
        assert any("VALIDATION bot=42" in rec.message for rec in caplog.records)
        assert any("Coca Zero" in rec.message for rec in caplog.records)


class TestRenderConstants:
    def test_dpi_is_200(self):
        from app.menu_extraction import _PDF_RENDER_DPI

        assert _PDF_RENDER_DPI == 200

    def test_jpeg_quality_is_90(self):
        from app.menu_extraction import _JPEG_QUALITY

        assert _JPEG_QUALITY == 90
