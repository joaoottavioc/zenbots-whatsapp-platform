# tests/test_webhook_token.py
"""Tests for the webhook token on orders (P3 fix)."""

import inspect
import re
from app.models import Order


class TestWebhookTokenGeneration:
    """Verify that Order.webhook_token is auto-generated."""

    def test_order_has_webhook_token_field(self):
        """Order model must have a webhook_token field."""
        assert hasattr(Order, "webhook_token")

    def test_auto_generates_token(self):
        """Each Order instance gets a unique non-empty webhook_token."""
        o1 = Order(total_amount=10.0, bot_id=1)
        o2 = Order(total_amount=20.0, bot_id=1)
        assert o1.webhook_token
        assert o2.webhook_token
        assert len(o1.webhook_token) >= 32
        assert o1.webhook_token != o2.webhook_token

    def test_token_is_url_safe(self):
        """Token must contain only URL-safe characters."""
        o = Order(total_amount=10.0, bot_id=1)
        assert re.match(r"^[A-Za-z0-9_-]+$", o.webhook_token)


class TestPaymentServiceWebhookUrl:
    """Verify the notification URL includes the webhook token."""

    def test_notification_url_includes_token_param(self):
        """payment_service.create_pix_payment must build URL with ?token=."""
        source = inspect.getsource(
            __import__("app.payment_service", fromlist=["create_pix_payment"])
        )
        assert "?token={webhook_token}" in source or "token=" in source

    def test_create_pix_payment_accepts_webhook_token_param(self):
        """create_pix_payment signature must include webhook_token."""
        from app.payment_service import create_pix_payment

        sig = inspect.signature(create_pix_payment)
        assert "webhook_token" in sig.parameters


class TestWebhookTokenValidation:
    """Verify the payment webhook validates the token."""

    def test_handler_checks_token_query_param(self):
        """handle_payment_notification source must use compare_digest on token."""
        import app.whatsapp as mod

        source = inspect.getsource(mod.handle_payment_notification)
        assert "compare_digest" in source, (
            "Webhook handler must use compare_digest for token validation"
        )
        assert "webhook_token" in source, (
            "Webhook handler must reference order.webhook_token"
        )
