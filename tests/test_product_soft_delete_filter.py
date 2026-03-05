# tests/test_product_soft_delete_filter.py
"""Tests to ensure soft-deleted products are filtered from all product queries."""

import inspect
import re

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app import whatsapp
from app.crud import find_relevant_products


class TestFindRelevantProductsExcludesSoftDeleted:
    """Verify that all 4 queries in find_relevant_products include is_deleted filtering."""

    @pytest.mark.asyncio
    async def test_find_relevant_products_excludes_soft_deleted(self):
        """All 4 queries must contain is_deleted filter by inspecting compiled SQL."""
        session = AsyncMock()

        captured_stmts = []

        async def capture_execute(stmt, *args, **kwargs):
            captured_stmts.append(
                str(stmt.compile(compile_kwargs={"literal_binds": True}))
            )
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=capture_execute)

        with patch("app.crud.embed_async", new=AsyncMock(return_value=[[0.0] * 384])):
            await find_relevant_products(session, bot_id=1, extracted_items=["pizza"])

        assert len(captured_stmts) == 4, (
            f"Expected 4 queries, got {len(captured_stmts)}"
        )
        for i, stmt_str in enumerate(captured_stmts):
            assert "is_deleted" in stmt_str, (
                f"Query {i + 1} missing is_deleted filter: {stmt_str}"
            )


class TestWhatsappProductQueriesExcludeSoftDeleted:
    """Verify all select(Product) queries in whatsapp.py include is_deleted filter."""

    def test_all_product_selects_have_is_deleted_filter(self):
        """Source inspection: every select(Product) in whatsapp.py must filter is_deleted."""
        source = inspect.getsource(whatsapp)
        lines = source.split("\n")

        # Find all lines that contain select(Product) — these start query chains
        select_line_indices = []
        for i, line in enumerate(lines):
            if re.search(r"select\(Product(?:\.id)?\)", line):
                select_line_indices.append(i)

        assert len(select_line_indices) >= 4, (
            f"Expected at least 4 select(Product) queries, found {len(select_line_indices)}"
        )

        # For each select(Product), gather the surrounding context (next 5 lines)
        # and check that is_deleted appears in the query chain
        for idx in select_line_indices:
            context = "\n".join(lines[idx : idx + 6])
            assert "is_deleted" in context, (
                f"select(Product) query at source line {idx + 1} missing is_deleted filter.\n"
                f"Context:\n{context}"
            )
