# tests/test_oauth_state.py
"""
Tests for OAuth CSRF state token management and payment route integration.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ===========================================================================
# store_oauth_state + consume_oauth_state
# ===========================================================================

@pytest.fixture()
def mock_redis_for_state():
    """Patch _get_client for OAuth state tests."""
    mock_client = MagicMock()
    mock_client.set = AsyncMock()
    mock_client.getdel = AsyncMock()

    with patch("app.rate_limiter._get_client", return_value=mock_client):
        yield mock_client


async def test_store_and_consume_round_trip(mock_redis_for_state):
    """store_oauth_state stores data; consume_oauth_state retrieves and deletes it."""
    from app.rate_limiter import store_oauth_state, consume_oauth_state

    mock_client = mock_redis_for_state

    # store_oauth_state should call set with JSON data and TTL
    token = await store_oauth_state(user_id=42, bot_id=7)
    assert isinstance(token, str)
    assert len(token) > 20  # token_urlsafe(32) produces ~43 chars

    # Verify Redis set was called with the right key pattern and TTL
    set_call = mock_client.set.call_args
    key = set_call.args[0]
    value = set_call.args[1]
    assert key == f"oauth_state:{token}"
    assert set_call.kwargs.get("ex") == 600

    # Simulate consume: Redis returns the stored value
    mock_client.getdel.return_value = value
    result = await consume_oauth_state(token)

    assert result == (42, 7)
    mock_client.getdel.assert_awaited_once_with(f"oauth_state:{token}")


async def test_consume_invalid_token_returns_none(mock_redis_for_state):
    """An invalid/expired token returns None."""
    from app.rate_limiter import consume_oauth_state

    mock_redis_for_state.getdel.return_value = None

    result = await consume_oauth_state("invalid-token-abc123")
    assert result is None


async def test_consume_empty_token_returns_none(mock_redis_for_state):
    """Empty string token returns None without hitting Redis."""
    from app.rate_limiter import consume_oauth_state

    result = await consume_oauth_state("")
    assert result is None


# ===========================================================================
# Auth URL includes state
# ===========================================================================

async def test_auth_url_includes_state():
    """GET /auth-url must include &state= in the returned URL."""
    from app.payment_routes import get_auth_url

    mock_user = MagicMock()
    mock_user.id = 1

    mock_bot = MagicMock()
    mock_bot.user_id = 1

    mock_session = AsyncMock()

    with patch("app.payment_routes.crud.get_bot_by_id", new=AsyncMock(return_value=mock_bot)), \
         patch("app.payment_routes.store_oauth_state", new=AsyncMock(return_value="test-state-token")), \
         patch("app.payment_routes.MP_CLIENT_ID", "test_client_id"):
        result = await get_auth_url(
            bot_id=1, current_user=mock_user, session=mock_session
        )

    assert "&state=test-state-token" in result["url"]


# ===========================================================================
# Callback rejects missing/invalid state
# ===========================================================================

async def test_callback_rejects_missing_state():
    """POST /callback with no state token must return 400."""
    from app.payment_routes import exchange_token
    from fastapi import HTTPException

    mock_user = MagicMock()
    mock_user.id = 1
    mock_session = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await exchange_token(
            body={"code": "test-code"},
            current_user=mock_user,
            session=mock_session,
        )
    assert exc_info.value.status_code == 400
    assert "state" in exc_info.value.detail.lower() or "State" in exc_info.value.detail


async def test_callback_rejects_invalid_state():
    """POST /callback with an invalid state token must return 400."""
    from app.payment_routes import exchange_token
    from fastapi import HTTPException

    mock_user = MagicMock()
    mock_user.id = 1
    mock_session = AsyncMock()

    with patch("app.payment_routes.consume_oauth_state", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await exchange_token(
                body={"code": "test-code", "state": "invalid-token"},
                current_user=mock_user,
                session=mock_session,
            )
        assert exc_info.value.status_code == 400
