# app/webhook_security.py
"""
Webhook signature verification for Mercado Pago and WhatsApp (Meta).

MP sends an `x-signature` header with format: `ts=<timestamp>,v1=<hmac>`.
We reconstruct the signed template and compare HMAC-SHA256 digests.

Meta signs WhatsApp webhook payloads with HMAC-SHA256 using the Facebook
App Secret, sent in the `X-Hub-Signature-256` header as `sha256=<hex>`.

Ref (MP): https://www.mercadopago.com.br/developers/en/docs/your-integrations/notifications/webhooks
Ref (Meta): https://developers.facebook.com/docs/graph-api/webhooks/getting-started#verification-requests
"""

import hashlib
import hmac
import logging
import os

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "")


def verify_mp_signature(
    x_signature: str,
    x_request_id: str,
    data_id: str,
    secret: str,
) -> bool:
    """
    Validate the HMAC-SHA256 signature sent by Mercado Pago.

    Returns True if the signature is valid, False otherwise.
    """
    if not x_signature or not secret:
        return False

    # Parse ts and v1 from header: "ts=1234567890,v1=abc123..."
    parts = {}
    for part in x_signature.split(","):
        kv = part.strip().split("=", 1)
        if len(kv) == 2:
            parts[kv[0].strip()] = kv[1].strip()

    ts = parts.get("ts")
    received_hash = parts.get("v1")

    if not ts or not received_hash:
        return False

    # Build the manifest string that MP signed
    manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"

    # Compute expected HMAC-SHA256
    expected_hash = hmac.new(
        secret.encode("utf-8"),
        manifest.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected_hash, received_hash)


async def require_mp_signature(request: Request, data_id: str) -> None:
    """
    FastAPI helper — raises HTTP 403 when the MP signature is invalid.
    Call this at the top of any webhook handler before doing DB work.
    """
    secret = MP_WEBHOOK_SECRET
    if not secret:
        # If the secret is not configured, reject all webhooks to be safe
        raise HTTPException(status_code=403, detail="Webhook secret not configured")

    x_signature = request.headers.get("x-signature", "")
    x_request_id = request.headers.get("x-request-id", "")

    if not verify_mp_signature(x_signature, x_request_id, data_id, secret):
        logger.warning(
            "Invalid MP webhook signature from IP %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=403, detail="Invalid webhook signature")


# ---------------------------------------------------------------------------
# WhatsApp (Meta) webhook signature verification
# ---------------------------------------------------------------------------

FB_APP_SECRET = os.getenv("FB_APP_SECRET", "")


def verify_whatsapp_signature(
    raw_body: bytes, signature_header: str, secret: str
) -> bool:
    """
    Validate the HMAC-SHA256 signature sent by Meta on WhatsApp webhook POSTs.

    Meta sends an `X-Hub-Signature-256` header with format `sha256=<hex_digest>`.
    The HMAC is computed over the raw request body using the Facebook App Secret.

    Returns True if the signature is valid, False otherwise.
    """
    if not signature_header or not secret:
        return False

    if not signature_header.startswith("sha256="):
        return False

    received_hash = signature_header[7:]  # strip "sha256=" prefix

    expected_hash = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected_hash, received_hash)
