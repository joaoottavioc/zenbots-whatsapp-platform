"""
Tests for SSE ticket-based authentication (P0-2 fix).

Covers:
- Redis layer: create_sse_ticket / consume_sse_ticket
- POST /auth/sse-ticket endpoint
- GET /stream ticket integration
"""

import asyncio
import json
import pytest
from unittest.mock import ANY, AsyncMock, MagicMock, patch


# ===================================================================
# Redis layer tests
# ===================================================================


@pytest.mark.asyncio
async def test_create_stores_ticket_in_redis():
    """create_sse_ticket stores ticket in Redis with correct key, value, and TTL."""
    mock_redis = AsyncMock()

    with patch("app.rate_limiter._get_client", return_value=mock_redis):
        from app.rate_limiter import create_sse_ticket

        ticket = await create_sse_ticket(user_id=42)

    assert isinstance(ticket, str)
    assert len(ticket) > 20  # token_urlsafe(32) produces ~43 chars

    mock_redis.set.assert_called_once()
    call_args = mock_redis.set.call_args
    key = call_args.args[0]
    value = call_args.args[1]
    assert key == f"sse_ticket:{ticket}"
    assert json.loads(value) == {"user_id": 42}
    assert call_args.kwargs.get("ex") == 60


@pytest.mark.asyncio
async def test_consume_returns_user_id():
    """Round-trip: create ticket then consume returns the correct user_id."""
    store = {}

    async def fake_set(key, value, ex=None):
        store[key] = value

    async def fake_getdel(key):
        return store.pop(key, None)

    mock_redis = AsyncMock()
    mock_redis.set = fake_set
    mock_redis.getdel = fake_getdel

    with patch("app.rate_limiter._get_client", return_value=mock_redis):
        from app.rate_limiter import create_sse_ticket, consume_sse_ticket

        ticket = await create_sse_ticket(user_id=7)
        user_id = await consume_sse_ticket(ticket)

    assert user_id == 7


@pytest.mark.asyncio
async def test_consume_invalid_ticket_returns_none():
    """Consuming a nonexistent/expired ticket returns None."""
    mock_redis = AsyncMock()
    mock_redis.getdel.return_value = None

    with patch("app.rate_limiter._get_client", return_value=mock_redis):
        from app.rate_limiter import consume_sse_ticket

        result = await consume_sse_ticket("nonexistent-ticket")

    assert result is None


@pytest.mark.asyncio
async def test_consume_empty_ticket_returns_none():
    """Empty string short-circuits without hitting Redis."""
    mock_redis = AsyncMock()

    with patch("app.rate_limiter._get_client", return_value=mock_redis):
        from app.rate_limiter import consume_sse_ticket

        result = await consume_sse_ticket("")

    assert result is None
    mock_redis.getdel.assert_not_called()


@pytest.mark.asyncio
async def test_consume_corrupted_json_returns_none():
    """Malformed JSON in Redis returns None instead of crashing."""
    mock_redis = AsyncMock()
    mock_redis.getdel.return_value = "not-valid-json{{"

    with patch("app.rate_limiter._get_client", return_value=mock_redis):
        from app.rate_limiter import consume_sse_ticket

        result = await consume_sse_ticket("some-ticket")

    assert result is None


# ===================================================================
# POST /auth/sse-ticket endpoint tests
# ===================================================================

try:
    from app.main import app as _app

    _HAS_FULL_APP = True
except ImportError:
    _HAS_FULL_APP = False


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_FULL_APP, reason="Full app dependencies not installed")
async def test_sse_ticket_endpoint_requires_auth():
    """POST /auth/sse-ticket without JWT returns 401."""
    from httpx import AsyncClient, ASGITransport

    transport = ASGITransport(app=_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/auth/sse-ticket")

    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_FULL_APP, reason="Full app dependencies not installed")
async def test_sse_ticket_endpoint_returns_ticket():
    """POST /auth/sse-ticket with valid JWT returns a ticket."""
    from httpx import AsyncClient, ASGITransport
    from app.auth import get_current_user

    fake_user = MagicMock(id=1, email="test@example.com")

    _app.dependency_overrides[get_current_user] = lambda: fake_user
    try:
        with patch("app.auth.create_sse_ticket", return_value="fake-ticket-abc"):
            transport = ASGITransport(app=_app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.post(
                    "/auth/sse-ticket",
                    headers={"Authorization": "Bearer fake-jwt"},
                )
    finally:
        _app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert "ticket" in data
    assert data["ticket"] == "fake-ticket-abc"


# ===================================================================
# GET /stream ticket integration tests
# ===================================================================


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_FULL_APP, reason="Full app dependencies not installed")
async def test_stream_with_valid_ticket():
    """GET /stream?ticket=valid returns 200 SSE stream."""
    from httpx import AsyncClient, ASGITransport

    fake_user = MagicMock(id=1)
    fake_bot = MagicMock(id=10)

    # Mock the Redis pubsub used inside event_generator
    # get_message raises CancelledError so the generator exits after the initial ping
    mock_pubsub = MagicMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.get_message = AsyncMock(side_effect=asyncio.CancelledError())
    mock_pubsub.unsubscribe = AsyncMock()

    mock_redis_conn = MagicMock()
    mock_redis_conn.pubsub.return_value = mock_pubsub
    mock_redis_conn.aclose = AsyncMock()

    with (
        patch("app.main.consume_sse_ticket", return_value=1) as mock_consume,
        patch("app.main.crud.get_user_by_id", return_value=fake_user),
        patch("app.main.crud.list_user_bots", return_value=[fake_bot]),
        patch("app.main.redis.from_url", return_value=mock_redis_conn),
    ):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/stream?ticket=valid-ticket")

    assert response.status_code == 200
    mock_consume.assert_called_once_with("valid-ticket")


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_FULL_APP, reason="Full app dependencies not installed")
async def test_stream_with_invalid_ticket_returns_401():
    """GET /stream?ticket=invalid returns 401."""
    from httpx import AsyncClient, ASGITransport

    with patch("app.main.consume_sse_ticket", return_value=None):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/stream?ticket=bad-ticket")

    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_FULL_APP, reason="Full app dependencies not installed")
async def test_stream_no_credentials_returns_401():
    """GET /stream with neither ticket nor token returns 401."""
    from httpx import AsyncClient, ASGITransport

    transport = ASGITransport(app=_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/stream")

    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_FULL_APP, reason="Full app dependencies not installed")
async def test_stream_ticket_takes_priority_over_token():
    """When both ticket and token are provided, ticket path is used."""
    from httpx import AsyncClient, ASGITransport

    fake_user = MagicMock(id=1)
    fake_bot = MagicMock(id=10)

    # Mock the Redis pubsub used inside event_generator
    mock_pubsub = MagicMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.get_message = AsyncMock(side_effect=asyncio.CancelledError())
    mock_pubsub.unsubscribe = AsyncMock()

    mock_redis_conn = MagicMock()
    mock_redis_conn.pubsub.return_value = mock_pubsub
    mock_redis_conn.aclose = AsyncMock()

    with (
        patch("app.main.consume_sse_ticket", return_value=1) as mock_consume,
        patch("app.main.crud.get_user_by_id", return_value=fake_user),
        patch("app.main.crud.list_user_bots", return_value=[fake_bot]),
        patch("app.main.get_user_from_token") as mock_jwt,
        patch("app.main.redis.from_url", return_value=mock_redis_conn),
    ):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/stream?ticket=valid-ticket&token=some-jwt")

    assert response.status_code == 200
    mock_consume.assert_called_once_with("valid-ticket")
    mock_jwt.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_FULL_APP, reason="Full app dependencies not installed")
async def test_stream_with_authorization_header():
    """GET /stream with Authorization: Bearer <jwt> header returns 200."""
    from httpx import AsyncClient, ASGITransport

    fake_user = MagicMock(id=1)
    fake_bot = MagicMock(id=10)

    mock_pubsub = MagicMock()
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.get_message = AsyncMock(side_effect=asyncio.CancelledError())
    mock_pubsub.unsubscribe = AsyncMock()

    mock_redis_conn = MagicMock()
    mock_redis_conn.pubsub.return_value = mock_pubsub
    mock_redis_conn.aclose = AsyncMock()

    with (
        patch("app.main.get_user_from_token", return_value=fake_user) as mock_jwt,
        patch("app.main.crud.list_user_bots", return_value=[fake_bot]),
        patch("app.main.redis.from_url", return_value=mock_redis_conn),
    ):
        transport = ASGITransport(app=_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/stream",
                headers={"Authorization": "Bearer valid-jwt-token"},
            )

    assert response.status_code == 200
    mock_jwt.assert_called_once_with("valid-jwt-token", ANY)
