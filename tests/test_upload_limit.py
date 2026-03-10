# tests/test_upload_limit.py
"""
Tests for file upload size limits in bot_routes.py.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException


def test_max_upload_size_is_10mb():
    """Verify the MAX_UPLOAD_SIZE constant is 10 MB."""
    from app.bot_routes import MAX_UPLOAD_SIZE

    assert MAX_UPLOAD_SIZE == 10 * 1024 * 1024


async def test_upload_within_limit_no_413():
    """An upload within the size limit should NOT raise 413."""
    from app.bot_routes import upload_catalog_from_file_endpoint

    # 1 KB file — well within limits
    small_content = b"x" * 1024

    mock_file = MagicMock()
    mock_file.filename = "menu.png"
    mock_file.content_type = "image/png"
    mock_file.read = AsyncMock(side_effect=[small_content, b""])

    mock_session = AsyncMock()
    mock_user = MagicMock()
    mock_user.id = 1

    mock_bot = MagicMock()
    mock_bot.user_id = 1
    mock_bot.menu_url = None

    with (
        patch(
            "app.bot_routes.crud.get_bot_by_id", new=AsyncMock(return_value=mock_bot)
        ),
        patch(
            "app.bot_routes.asyncio.to_thread",
            new=AsyncMock(return_value="https://s3.example.com/menu.png"),
        ),
        patch(
            "app.bot_routes._detect_file_type",
            return_value=(False, True, "image/png"),
        ),
        patch(
            "app.bot_routes._resize_and_compress",
            return_value=(small_content, "image/jpeg"),
        ),
        patch(
            "app.bot_routes.extract_products_from_image",
            new=AsyncMock(return_value=[{"name": "Pizza", "price": 10.0}]),
        ),
        patch(
            "app.bot_routes._enrich_products",
            return_value=[
                {
                    "name": "Pizza",
                    "price": 10.0,
                    "keywords": ["pizza"],
                    "is_available": True,
                }
            ],
        ),
        patch(
            "app.bot_routes.crud.bulk_create_products",
            new=AsyncMock(return_value=1),
        ),
    ):
        result = await upload_catalog_from_file_endpoint(
            bot_id=1, file=mock_file, session=mock_session, current_user=mock_user
        )

    assert "Sucesso" in result["message"]


async def test_upload_exceeding_limit_raises_413():
    """An upload exceeding MAX_UPLOAD_SIZE must raise HTTP 413."""
    from app.bot_routes import upload_catalog_from_file_endpoint, MAX_UPLOAD_SIZE

    chunk_size = 8192
    oversized_chunk = b"x" * chunk_size
    call_count = 0
    needed_chunks = (MAX_UPLOAD_SIZE // chunk_size) + 2

    async def fake_read(size=None):
        nonlocal call_count
        call_count += 1
        if call_count <= needed_chunks:
            return oversized_chunk
        return b""

    mock_file = MagicMock()
    mock_file.filename = "huge.pdf"
    mock_file.content_type = "application/pdf"
    mock_file.read = fake_read

    mock_session = AsyncMock()
    mock_user = MagicMock()
    mock_user.id = 1

    mock_bot = MagicMock()
    mock_bot.user_id = 1

    with patch(
        "app.bot_routes.crud.get_bot_by_id", new=AsyncMock(return_value=mock_bot)
    ):
        with pytest.raises(HTTPException) as exc_info:
            await upload_catalog_from_file_endpoint(
                bot_id=1, file=mock_file, session=mock_session, current_user=mock_user
            )
        assert exc_info.value.status_code == 413
