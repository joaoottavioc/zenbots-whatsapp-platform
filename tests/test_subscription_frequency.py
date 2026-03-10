"""Tests for D10: Subscription period_end respects plan frequency.

Verifies that the billing webhook extracts frequency from MP's auto_recurring
and passes the correct plan_frequency_months to upsert_subscription.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing_routes import router
from app.database import get_session


@pytest.fixture
def mock_session():
    session = AsyncMock()
    bot = MagicMock()
    bot.id = 10
    bot.user_id = 1
    session.get = AsyncMock(return_value=bot)
    session.commit = AsyncMock()
    return session


@pytest.fixture
def client(mock_session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: mock_session
    return TestClient(app)


def _mp_webhook_payload(preapproval_id="PAP-123"):
    return {
        "type": "subscription_preapproval",
        "data": {"id": preapproval_id},
    }


class TestSubscriptionFrequency:
    """Billing webhook must extract frequency from MP and pass to upsert."""

    def _call_webhook(self, client, mp_response):
        mock_sdk = MagicMock()
        mock_sdk.preapproval.return_value.get.return_value = {"response": mp_response}

        with (
            patch("app.billing_routes._get_sdk", return_value=mock_sdk),
            patch("app.billing_routes.require_mp_signature", return_value=None),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.billing_routes.crud.upsert_subscription", new_callable=AsyncMock
            ) as mock_upsert,
        ):
            resp = client.post(
                "/billing/webhook",
                json=_mp_webhook_payload(),
            )
            return resp, mock_upsert

    def test_monthly_plan_passes_1_month(self, client):
        """A monthly MP subscription should pass plan_frequency_months=1."""
        mp_resp = {
            "status": "authorized",
            "external_reference": "BOT_10_pro",
            "auto_recurring": {"frequency": 1, "frequency_type": "months"},
        }
        resp, mock_upsert = self._call_webhook(client, mp_resp)

        assert resp.status_code == 200
        mock_upsert.assert_called_once()
        call_kwargs = mock_upsert.call_args.kwargs
        assert call_kwargs["plan_frequency_months"] == 1

    def test_annual_plan_passes_12_months(self, client):
        """A 12-month MP subscription should pass plan_frequency_months=12."""
        mp_resp = {
            "status": "authorized",
            "external_reference": "BOT_10_pro",
            "auto_recurring": {"frequency": 12, "frequency_type": "months"},
        }
        resp, mock_upsert = self._call_webhook(client, mp_resp)

        assert resp.status_code == 200
        call_kwargs = mock_upsert.call_args.kwargs
        assert call_kwargs["plan_frequency_months"] == 12

    def test_quarterly_plan_passes_3_months(self, client):
        """A 3-month MP subscription should pass plan_frequency_months=3."""
        mp_resp = {
            "status": "authorized",
            "external_reference": "BOT_10_pro",
            "auto_recurring": {"frequency": 3, "frequency_type": "months"},
        }
        resp, mock_upsert = self._call_webhook(client, mp_resp)

        call_kwargs = mock_upsert.call_args.kwargs
        assert call_kwargs["plan_frequency_months"] == 3

    def test_missing_auto_recurring_defaults_to_1(self, client):
        """If auto_recurring is missing, default to 1 month."""
        mp_resp = {
            "status": "authorized",
            "external_reference": "BOT_10_pro",
        }
        resp, mock_upsert = self._call_webhook(client, mp_resp)

        call_kwargs = mock_upsert.call_args.kwargs
        assert call_kwargs["plan_frequency_months"] == 1

    def test_days_frequency_converts_to_months(self, client):
        """A 365-day subscription should convert to ~12 months."""
        mp_resp = {
            "status": "authorized",
            "external_reference": "BOT_10_pro",
            "auto_recurring": {"frequency": 365, "frequency_type": "days"},
        }
        resp, mock_upsert = self._call_webhook(client, mp_resp)

        call_kwargs = mock_upsert.call_args.kwargs
        assert call_kwargs["plan_frequency_months"] == 12
