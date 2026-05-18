# tests/test_upload_limit.py
"""
Tests for file upload size limits in bot_routes.py.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException


def test_max_upload_size_is_35mb():
    """Verify the MAX_UPLOAD_SIZE constant is 35 MB."""
    from app.bot_routes import MAX_UPLOAD_SIZE

    assert MAX_UPLOAD_SIZE == 35 * 1024 * 1024


def _make_request_with_arq():
    """Build a mock FastAPI Request whose app.state.arq_redis captures enqueue calls."""
    mock_request = MagicMock()
    mock_request.app.state.arq_redis.enqueue_job = AsyncMock()
    return mock_request


async def test_upload_within_limit_no_413():
    """An image upload within the size limit should NOT raise 413."""
    from app.bot_routes import upload_catalog_from_file_endpoint
    from app.menu_extraction_policy import MenuExtractionPolicy
    from fastapi import Response

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

    pro_policy = MenuExtractionPolicy(
        plan_key="pro_monthly",
        tier="pro",
        max_per_month=5,
        allows_pdf=True,
        max_images=None,
    )

    with (
        patch(
            "app.bot_routes.crud.get_bot_by_id", new=AsyncMock(return_value=mock_bot)
        ),
        patch(
            "app.bot_routes.consume_extraction_quota",
            new=AsyncMock(return_value=pro_policy),
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
            bot_id=1,
            request=_make_request_with_arq(),
            response=Response(),
            files=[mock_file],
            session=mock_session,
            current_user=mock_user,
        )

    assert "Sucesso" in result["message"]


async def test_pdf_upload_enqueues_worker_and_returns_202():
    """A PDF upload should enqueue the worker job and return a 202 status."""
    from app.bot_routes import upload_catalog_from_file_endpoint
    from app.menu_extraction_policy import MenuExtractionPolicy
    from fastapi import Response

    pdf_content = b"%PDF-1.4 fake"

    mock_file = MagicMock()
    mock_file.filename = "menu.pdf"
    mock_file.content_type = "application/pdf"
    mock_file.read = AsyncMock(side_effect=[pdf_content, b""])

    mock_session = AsyncMock()
    mock_user = MagicMock()
    mock_user.id = 1

    mock_bot = MagicMock()
    mock_bot.user_id = 1
    mock_bot.menu_url = None

    mock_request = _make_request_with_arq()
    response = Response()

    pro_policy = MenuExtractionPolicy(
        plan_key="pro_monthly",
        tier="pro",
        max_per_month=5,
        allows_pdf=True,
        max_images=None,
    )

    with (
        patch(
            "app.bot_routes.crud.get_bot_by_id", new=AsyncMock(return_value=mock_bot)
        ),
        patch(
            "app.bot_routes.consume_extraction_quota",
            new=AsyncMock(return_value=pro_policy),
        ),
        patch(
            "app.bot_routes.asyncio.to_thread",
            new=AsyncMock(return_value="https://s3.example.com/menu.pdf"),
        ),
        patch(
            "app.bot_routes._detect_file_type",
            return_value=(True, False, None),
        ),
    ):
        result = await upload_catalog_from_file_endpoint(
            bot_id=1,
            request=mock_request,
            response=response,
            files=[mock_file],
            session=mock_session,
            current_user=mock_user,
        )

    assert response.status_code == 202
    assert result["status"] == "processing"
    mock_request.app.state.arq_redis.enqueue_job.assert_awaited_once()
    args = mock_request.app.state.arq_redis.enqueue_job.await_args.args
    assert args[0] == "process_menu_extraction"
    assert args[1] == 1
    assert args[3] == "menu.pdf"


async def test_upload_exceeding_limit_raises_413():
    """An upload exceeding MAX_UPLOAD_SIZE must raise HTTP 413."""
    from app.bot_routes import upload_catalog_from_file_endpoint, MAX_UPLOAD_SIZE
    from fastapi import Response

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
                bot_id=1,
                request=_make_request_with_arq(),
                response=Response(),
                files=[mock_file],
                session=mock_session,
                current_user=mock_user,
            )
        assert exc_info.value.status_code == 413
