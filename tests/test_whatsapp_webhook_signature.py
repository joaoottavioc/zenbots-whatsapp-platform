import hashlib
import hmac
import json
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.webhook_security import verify_whatsapp_signature

# Ensure fitz (PyMuPDF) doesn't block import of bot_routes in test env
if "fitz" not in sys.modules:
    sys.modules["fitz"] = MagicMock()

from app.bot_routes import receive_whatsapp_message  # noqa: E402


# ---------------------------------------------------------------------------
# Unit tests for verify_whatsapp_signature
# ---------------------------------------------------------------------------

SECRET = "test-fb-app-secret"

VALID_PAYLOAD = json.dumps(
    {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "123456"},
                            "messages": [
                                {
                                    "from": "5511999999999",
                                    "id": "wamid.test",
                                    "text": {"body": "Olá"},
                                    "type": "text",
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }
).encode("utf-8")


def _sign(body: bytes, secret: str) -> str:
    """Compute the X-Hub-Signature-256 header value like Meta does."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# --- verify_whatsapp_signature unit tests ---


def test_valid_signature():
    sig = _sign(VALID_PAYLOAD, SECRET)
    assert verify_whatsapp_signature(VALID_PAYLOAD, sig, SECRET) is True


def test_wrong_secret_returns_false():
    sig = _sign(VALID_PAYLOAD, SECRET)
    assert verify_whatsapp_signature(VALID_PAYLOAD, sig, "wrong-secret") is False


def test_tampered_body_returns_false():
    sig = _sign(VALID_PAYLOAD, SECRET)
    tampered = VALID_PAYLOAD + b"x"
    assert verify_whatsapp_signature(tampered, sig, SECRET) is False


def test_empty_signature_returns_false():
    assert verify_whatsapp_signature(VALID_PAYLOAD, "", SECRET) is False


def test_none_signature_returns_false():
    assert verify_whatsapp_signature(VALID_PAYLOAD, None, SECRET) is False


def test_missing_secret_returns_false():
    sig = _sign(VALID_PAYLOAD, SECRET)
    assert verify_whatsapp_signature(VALID_PAYLOAD, sig, "") is False


def test_missing_sha256_prefix_returns_false():
    digest = hmac.new(SECRET.encode(), VALID_PAYLOAD, hashlib.sha256).hexdigest()
    assert verify_whatsapp_signature(VALID_PAYLOAD, digest, SECRET) is False


def test_wrong_prefix_returns_false():
    digest = hmac.new(SECRET.encode(), VALID_PAYLOAD, hashlib.sha256).hexdigest()
    assert verify_whatsapp_signature(VALID_PAYLOAD, f"sha1={digest}", SECRET) is False


def test_empty_body_with_valid_signature():
    body = b""
    sig = _sign(body, SECRET)
    assert verify_whatsapp_signature(body, sig, SECRET) is True


# ---------------------------------------------------------------------------
# Integration tests for the POST /bots/whatsapp/webhook endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_arq_redis():
    redis = AsyncMock()
    redis.enqueue_job = AsyncMock()
    return redis


@pytest.fixture
def mock_request(mock_arq_redis):
    """Factory to build a mock Request with configurable headers and body."""

    def _build(body: bytes = VALID_PAYLOAD, headers: dict = None):
        request = AsyncMock()
        request.body = AsyncMock(return_value=body)
        request.headers = headers or {}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        app_state = MagicMock()
        app_state.arq_redis = mock_arq_redis
        request.app = MagicMock()
        request.app.state = app_state

        return request

    return _build


@pytest.mark.asyncio
async def test_endpoint_valid_signature_enqueues_job(mock_request, mock_arq_redis):
    """A correctly signed payload should be enqueued to Redis."""
    sig = _sign(VALID_PAYLOAD, SECRET)
    request = mock_request(
        body=VALID_PAYLOAD,
        headers={"X-Hub-Signature-256": sig},
    )

    with patch("app.bot_routes.FB_APP_SECRET", SECRET):
        result = await receive_whatsapp_message(request)

    assert result == {"status": "received"}
    mock_arq_redis.enqueue_job.assert_called_once()
    args = mock_arq_redis.enqueue_job.call_args
    assert args[0][0] == "process_whatsapp_message"


@pytest.mark.asyncio
async def test_endpoint_invalid_signature_rejects(mock_request, mock_arq_redis):
    """An invalid signature should return 200 but NOT enqueue the job."""
    request = mock_request(
        body=VALID_PAYLOAD,
        headers={"X-Hub-Signature-256": "sha256=invalid"},
    )

    with patch("app.bot_routes.FB_APP_SECRET", SECRET):
        result = await receive_whatsapp_message(request)

    assert result == {"status": "invalid_signature"}
    mock_arq_redis.enqueue_job.assert_not_called()


@pytest.mark.asyncio
async def test_endpoint_missing_signature_header_rejects(mock_request, mock_arq_redis):
    """Missing X-Hub-Signature-256 header should be rejected when secret is set."""
    request = mock_request(body=VALID_PAYLOAD, headers={})

    with patch("app.bot_routes.FB_APP_SECRET", SECRET):
        result = await receive_whatsapp_message(request)

    assert result == {"status": "invalid_signature"}
    mock_arq_redis.enqueue_job.assert_not_called()


@pytest.mark.asyncio
async def test_endpoint_no_secret_configured_still_processes(
    mock_request, mock_arq_redis
):
    """When FB_APP_SECRET is not set, webhook should still process (backwards compat)."""
    request = mock_request(body=VALID_PAYLOAD, headers={})

    with patch("app.bot_routes.FB_APP_SECRET", ""):
        result = await receive_whatsapp_message(request)

    assert result == {"status": "received"}
    mock_arq_redis.enqueue_job.assert_called_once()


@pytest.mark.asyncio
async def test_endpoint_status_event_enqueued(mock_request, mock_arq_redis):
    """Status events (delivery receipts) should also be enqueued when signature is valid."""
    status_payload = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "123456"},
                                "statuses": [
                                    {"id": "wamid.test", "status": "delivered"}
                                ],
                            }
                        }
                    ]
                }
            ],
        }
    ).encode("utf-8")

    sig = _sign(status_payload, SECRET)
    request = mock_request(
        body=status_payload,
        headers={"X-Hub-Signature-256": sig},
    )

    with patch("app.bot_routes.FB_APP_SECRET", SECRET):
        result = await receive_whatsapp_message(request)

    assert result == {"status": "received"}
    mock_arq_redis.enqueue_job.assert_called_once()


@pytest.mark.asyncio
async def test_endpoint_non_whatsapp_payload_ignored(mock_request, mock_arq_redis):
    """A signed but non-WhatsApp payload should not enqueue any job."""
    other_payload = json.dumps({"object": "page", "entry": []}).encode("utf-8")
    sig = _sign(other_payload, SECRET)
    request = mock_request(
        body=other_payload,
        headers={"X-Hub-Signature-256": sig},
    )

    with patch("app.bot_routes.FB_APP_SECRET", SECRET):
        result = await receive_whatsapp_message(request)

    assert result == {"status": "received"}
    mock_arq_redis.enqueue_job.assert_not_called()
