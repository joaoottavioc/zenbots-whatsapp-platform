import hashlib
import hmac

import pytest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.webhook_security import verify_mp_signature, require_mp_signature


# ---------- verify_mp_signature ----------

SECRET = "my-test-secret"


def _make_signature(data_id: str, request_id: str, ts: str, secret: str) -> str:
    manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
    h = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return f"ts={ts},v1={h}"


def test_valid_signature():
    sig = _make_signature("12345", "req-1", "1700000000", SECRET)
    assert verify_mp_signature(sig, "req-1", "12345", SECRET) is True


def test_wrong_secret_returns_false():
    sig = _make_signature("12345", "req-1", "1700000000", SECRET)
    assert verify_mp_signature(sig, "req-1", "12345", "wrong-secret") is False


def test_tampered_data_id_returns_false():
    sig = _make_signature("12345", "req-1", "1700000000", SECRET)
    assert verify_mp_signature(sig, "req-1", "99999", SECRET) is False


def test_empty_signature_returns_false():
    assert verify_mp_signature("", "req-1", "12345", SECRET) is False


def test_missing_v1_returns_false():
    assert verify_mp_signature("ts=123", "req-1", "12345", SECRET) is False


def test_missing_ts_returns_false():
    assert verify_mp_signature("v1=abc123", "req-1", "12345", SECRET) is False


def test_no_secret_returns_false():
    sig = _make_signature("12345", "req-1", "1700000000", SECRET)
    assert verify_mp_signature(sig, "req-1", "12345", "") is False


def test_malformed_header_returns_false():
    assert verify_mp_signature("garbage-header", "req-1", "12345", SECRET) is False


# ---------- require_mp_signature ----------


@pytest.mark.asyncio
async def test_require_raises_403_on_bad_signature():
    mock_request = AsyncMock()
    mock_request.headers = {"x-signature": "ts=123,v1=bad", "x-request-id": "req-1"}

    with patch("app.webhook_security.MP_WEBHOOK_SECRET", SECRET):
        with pytest.raises(HTTPException) as exc_info:
            await require_mp_signature(mock_request, "12345")
        assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_raises_403_when_secret_not_configured():
    mock_request = AsyncMock()
    mock_request.headers = {"x-signature": "ts=123,v1=abc", "x-request-id": "req-1"}

    with patch("app.webhook_security.MP_WEBHOOK_SECRET", ""):
        with pytest.raises(HTTPException) as exc_info:
            await require_mp_signature(mock_request, "12345")
        assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_passes_on_valid_signature():
    sig = _make_signature("12345", "req-1", "1700000000", SECRET)
    mock_request = AsyncMock()
    mock_request.headers = {"x-signature": sig, "x-request-id": "req-1"}

    with patch("app.webhook_security.MP_WEBHOOK_SECRET", SECRET):
        # Should not raise
        await require_mp_signature(mock_request, "12345")


@pytest.mark.asyncio
async def test_require_raises_403_on_missing_headers():
    mock_request = AsyncMock()
    mock_request.headers = {}

    with patch("app.webhook_security.MP_WEBHOOK_SECRET", SECRET):
        with pytest.raises(HTTPException) as exc_info:
            await require_mp_signature(mock_request, "12345")
        assert exc_info.value.status_code == 403
