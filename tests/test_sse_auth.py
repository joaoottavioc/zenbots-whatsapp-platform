import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.auth import get_user_from_token


# ---------- get_user_from_token ----------

@pytest.mark.asyncio
async def test_valid_token_returns_user():
    mock_session = AsyncMock()
    fake_user = MagicMock(id=1, email="test@example.com")

    with patch("app.auth.jwt.decode", return_value={"sub": "test@example.com"}), \
         patch("app.auth.crud.get_user_by_email", return_value=fake_user):
        user = await get_user_from_token("valid.jwt.token", mock_session)

    assert user is not None
    assert user.email == "test@example.com"


@pytest.mark.asyncio
async def test_invalid_token_returns_none():
    from jose import JWTError

    mock_session = AsyncMock()

    with patch("app.auth.jwt.decode", side_effect=JWTError("bad")):
        user = await get_user_from_token("bad.token", mock_session)

    assert user is None


@pytest.mark.asyncio
async def test_token_missing_sub_returns_none():
    mock_session = AsyncMock()

    with patch("app.auth.jwt.decode", return_value={}):
        user = await get_user_from_token("no-sub.token", mock_session)

    assert user is None


@pytest.mark.asyncio
async def test_token_user_not_found_returns_none():
    mock_session = AsyncMock()

    with patch("app.auth.jwt.decode", return_value={"sub": "ghost@example.com"}), \
         patch("app.auth.crud.get_user_by_email", return_value=None):
        user = await get_user_from_token("valid.jwt.token", mock_session)

    assert user is None


# ---------- /stream endpoint auth ----------

try:
    from app.main import app as _app
    _HAS_FULL_APP = True
except ImportError:
    _HAS_FULL_APP = False


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_FULL_APP, reason="Full app dependencies not installed (e.g. fitz)")
async def test_stream_no_token_returns_401():
    """GET /stream without token query parameter returns 401."""
    from httpx import AsyncClient, ASGITransport

    transport = ASGITransport(app=_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/stream")

    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_FULL_APP, reason="Full app dependencies not installed (e.g. fitz)")
async def test_stream_invalid_token_returns_401():
    """GET /stream with an invalid JWT returns 401."""
    from httpx import AsyncClient, ASGITransport

    transport = ASGITransport(app=_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/stream?token=invalid.jwt.here")

    assert response.status_code == 401
