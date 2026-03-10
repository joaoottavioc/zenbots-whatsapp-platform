"""Tests for S11: Billing error messages must not leak internal details.

Verifies that /billing/checkout returns generic error messages when
Mercado Pago API calls fail, without exposing MP error details or
Python exception messages to the client.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing_routes import router
from app.auth import get_current_user
from app.database import get_session
from app.models import User, Bot, Plan


def _make_user() -> MagicMock:
    user = MagicMock(spec=User)
    user.id = 1
    user.email = "owner@example.com"
    user.is_admin = False
    return user


def _make_bot(user_id: int = 1) -> MagicMock:
    bot = MagicMock(spec=Bot)
    bot.id = 10
    bot.user_id = user_id
    bot.restaurant_name = "Test Restaurant"
    return bot


def _make_plan() -> MagicMock:
    plan = MagicMock(spec=Plan)
    plan.id = 1
    plan.key = "pro"
    plan.title = "ZenBotZ Pro"
    plan.price = 199.0
    return plan


@pytest.fixture
def setup():
    """Create a TestClient with mocked dependencies."""
    app = FastAPI()
    app.include_router(router)

    user = _make_user()
    mock_session = AsyncMock()

    # session.get(Bot, ...) returns the bot
    bot = _make_bot()
    mock_session.get = AsyncMock(return_value=bot)

    # session.execute(...) returns the plan
    plan = _make_plan()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = plan
    mock_session.execute = AsyncMock(return_value=mock_result)

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_session] = lambda: mock_session

    client = TestClient(app)
    # Patch crud.get_subscription_by_bot so checkout guard passes (no existing sub)
    patcher = patch(
        "app.billing_routes.crud.get_subscription_by_bot",
        new_callable=AsyncMock,
        return_value=None,
    )
    patcher.start()
    yield client, mock_session
    patcher.stop()


CHECKOUT_PAYLOAD = {"bot_id": 10, "plan_key": "pro"}


class TestBillingErrorLeak:
    """Error responses from /billing/checkout must not expose internal details."""

    def test_mp_api_error_returns_generic_message(self, setup):
        """When MP returns non-201, the response must use a generic message."""
        client, _ = setup
        mock_sdk = MagicMock()
        mock_sdk.preapproval.return_value.create.return_value = {
            "status": 400,
            "response": {"message": "Invalid access_token: APP_USR-xxx-yyy"},
        }

        with patch("app.billing_routes._get_sdk", return_value=mock_sdk):
            resp = client.post("/billing/checkout", json=CHECKOUT_PAYLOAD)

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "Erro ao processar pagamento" in detail
        # Must NOT contain the raw MP error
        assert "access_token" not in detail
        assert "APP_USR" not in detail

    def test_mp_exception_returns_generic_message(self, setup):
        """When MP SDK throws an exception, the response must not leak it."""
        client, _ = setup
        mock_sdk = MagicMock()
        mock_sdk.preapproval.return_value.create.side_effect = ConnectionError(
            "Connection refused: api.mercadopago.com:443"
        )

        with patch("app.billing_routes._get_sdk", return_value=mock_sdk):
            resp = client.post("/billing/checkout", json=CHECKOUT_PAYLOAD)

        assert resp.status_code == 500
        detail = resp.json()["detail"]
        # Must NOT contain the raw exception
        assert "mercadopago" not in detail.lower()
        assert "Connection refused" not in detail
        assert "443" not in detail
        # Must contain a user-friendly message
        assert "Erro" in detail

    def test_mp_error_detail_field_never_contains_mp_prefix(self, setup):
        """The old 'Erro MP: ...' prefix pattern must not be present."""
        client, _ = setup
        mock_sdk = MagicMock()
        mock_sdk.preapproval.return_value.create.return_value = {
            "status": 500,
            "response": {"message": "Internal server error"},
        }

        with patch("app.billing_routes._get_sdk", return_value=mock_sdk):
            resp = client.post("/billing/checkout", json=CHECKOUT_PAYLOAD)

        detail = resp.json()["detail"]
        assert not detail.startswith("Erro MP:")
