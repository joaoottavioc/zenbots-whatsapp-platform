"""Tests for MP token refresh logic (V8)."""
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.encryption import encrypt_value, decrypt_value
from app.payment_service import refresh_mp_token, get_valid_access_token


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.id = 1
    cfg.refresh_token = encrypt_value("old-refresh-token")
    cfg.access_token = encrypt_value("old-access-token")
    cfg.token_expires_at = datetime(2026, 1, 1, 0, 0, 0)  # expired
    cfg.updated_at = None
    return cfg


@pytest.fixture
def mock_session():
    s = AsyncMock()
    s.add = MagicMock()
    s.commit = AsyncMock()
    return s


@pytest.mark.asyncio
async def test_refresh_mp_token_success(mock_config, mock_session):
    """Successful refresh updates config with new tokens."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "new-access-token",
        "refresh_token": "new-refresh-token",
        "expires_in": 21600,
    }
    mock_resp.text = ""

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.payment_service.httpx.AsyncClient", return_value=mock_client):
        result = await refresh_mp_token(mock_config, mock_session)

    assert result == "new-access-token"
    assert decrypt_value(mock_config.access_token) == "new-access-token"
    assert decrypt_value(mock_config.refresh_token) == "new-refresh-token"
    assert mock_config.token_expires_at is not None
    mock_session.add.assert_called_once_with(mock_config)
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_refresh_mp_token_no_refresh_token(mock_session):
    """Should return None when no refresh_token is stored."""
    cfg = MagicMock()
    cfg.id = 2
    cfg.refresh_token = None

    result = await refresh_mp_token(cfg, mock_session)
    assert result is None
    mock_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_mp_token_http_error(mock_config, mock_session):
    """Should return None on HTTP error from MP."""
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Bad Request"
    mock_resp.json.return_value = {}

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.payment_service.httpx.AsyncClient", return_value=mock_client):
        result = await refresh_mp_token(mock_config, mock_session)

    assert result is None


@pytest.mark.asyncio
async def test_get_valid_access_token_not_expired(mock_config, mock_session):
    """Should return existing token when not expired."""
    from app.time import utcnow
    mock_config.token_expires_at = utcnow() + timedelta(hours=1)

    result = await get_valid_access_token(mock_config, mock_session)
    assert result == "old-access-token"


@pytest.mark.asyncio
async def test_get_valid_access_token_expired_triggers_refresh(mock_config, mock_session):
    """Should call refresh when token is expired."""
    from app.time import utcnow
    mock_config.token_expires_at = utcnow() - timedelta(hours=1)

    with patch("app.payment_service.refresh_mp_token", new_callable=AsyncMock, return_value="refreshed-token") as mock_refresh:
        result = await get_valid_access_token(mock_config, mock_session)

    assert result == "refreshed-token"
    mock_refresh.assert_called_once_with(mock_config, mock_session)


@pytest.mark.asyncio
async def test_get_valid_access_token_no_expiry(mock_config, mock_session):
    """Should return existing token when token_expires_at is None."""
    mock_config.token_expires_at = None

    result = await get_valid_access_token(mock_config, mock_session)
    assert result == "old-access-token"
