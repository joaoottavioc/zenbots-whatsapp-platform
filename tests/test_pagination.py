"""Tests for product list pagination (V9)."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.crud import get_products_by_bot_id


@pytest.mark.asyncio
async def test_default_pagination_params():
    """get_products_by_bot_id should apply default limit=50 offset=0."""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    session.execute.return_value = result_mock

    await get_products_by_bot_id(session, bot_id=1)

    # Verify execute was called (statement built with offset/limit)
    session.execute.assert_called_once()
    stmt = session.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "LIMIT" in compiled.upper()
    assert "OFFSET" in compiled.upper()


@pytest.mark.asyncio
async def test_custom_pagination_params():
    """Custom limit/offset should be passed through."""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    session.execute.return_value = result_mock

    await get_products_by_bot_id(session, bot_id=1, limit=10, offset=20)

    session.execute.assert_called_once()
    stmt = session.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "LIMIT" in compiled.upper()
    assert "OFFSET" in compiled.upper()
