"""Checkout endpoint sends Plan.billing_cycle_months as MP auto_recurring.frequency.

Previously hard-coded to `1`, which caused annual plans (R$1068/yr) to be
billed as R$1068/month.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.billing_routes import router
from app.database import get_session


@pytest.fixture
def mock_user():
    user = MagicMock()
    user.id = 1
    user.email = "resto@example.com"
    return user


def _build_mock_session(plan_billing_cycle_months: int, plan_price: float):
    """Session that returns a bot and a Plan with the requested cycle."""
    bot = MagicMock()
    bot.id = 10
    bot.user_id = 1
    bot.restaurant_name = "Pizzaria Teste"

    plan = MagicMock()
    plan.key = "pro_annual" if plan_billing_cycle_months == 12 else "pro_monthly"
    plan.title = "Pro"
    plan.price = plan_price
    plan.billing_cycle_months = plan_billing_cycle_months

    session = AsyncMock()
    session.get = AsyncMock(return_value=bot)

    exec_result = MagicMock()
    exec_result.scalars.return_value.first.return_value = plan
    session.execute = AsyncMock(return_value=exec_result)

    return session


@pytest.fixture
def client_factory(mock_user):
    def _make(session):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_user] = lambda: mock_user
        return TestClient(app)

    return _make


def _invoke_checkout(client, plan_key: str):
    mock_sdk = MagicMock()
    mock_sdk.preapproval.return_value.create.return_value = {
        "status": 201,
        "response": {"init_point": "https://mp.example/checkout"},
    }

    with (
        patch("app.billing_routes._get_sdk", return_value=mock_sdk),
        patch(
            "app.billing_routes.crud.get_subscription_by_bot",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        resp = client.post(
            "/billing/checkout", json={"bot_id": 10, "plan_key": plan_key}
        )

    created_with = mock_sdk.preapproval.return_value.create.call_args[0][0]
    return resp, created_with


class TestCheckoutFrequency:
    def test_monthly_plan_sends_frequency_1(self, client_factory):
        session = _build_mock_session(plan_billing_cycle_months=1, plan_price=129.90)
        client = client_factory(session)

        resp, body = _invoke_checkout(client, "pro_monthly")

        assert resp.status_code == 200
        assert body["auto_recurring"]["frequency"] == 1
        assert body["auto_recurring"]["frequency_type"] == "months"
        assert body["auto_recurring"]["transaction_amount"] == 129.90

    def test_annual_plan_sends_frequency_12(self, client_factory):
        """Regression: annual plan must bill once every 12 months, not monthly."""
        session = _build_mock_session(plan_billing_cycle_months=12, plan_price=1068.00)
        client = client_factory(session)

        resp, body = _invoke_checkout(client, "pro_annual")

        assert resp.status_code == 200
        assert body["auto_recurring"]["frequency"] == 12
        assert body["auto_recurring"]["frequency_type"] == "months"
        assert body["auto_recurring"]["transaction_amount"] == 1068.00
