# tests/test_billing_subscription_guard.py
"""
Tests for subscription management in billing_routes.py:
- Checkout guards (duplicate, upgrade, pending cleanup, admin protection)
- Webhook guards (stale webhook skip, admin skip, upgrade confirmation, legacy fallback)
"""

from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing_routes import router
from app.auth import get_current_user
from app.database import get_session
from app.models import User, Bot, Plan, Subscription


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_user(user_id: int = 1) -> MagicMock:
    user = MagicMock(spec=User)
    user.id = user_id
    user.email = "owner@example.com"
    user.is_admin = False
    return user


def _make_bot(bot_id: int = 10, user_id: int = 1) -> MagicMock:
    bot = MagicMock(spec=Bot)
    bot.id = bot_id
    bot.user_id = user_id
    bot.restaurant_name = "Test Restaurant"
    return bot


def _make_plan(key: str = "pro", price: float = 199.0) -> MagicMock:
    plan = MagicMock(spec=Plan)
    plan.id = 1
    plan.key = key
    plan.title = f"ZenBotZ {key.capitalize()}"
    plan.price = price
    return plan


def _make_subscription(
    mp_id: str = "mp_sub_abc",
    status: str = "authorized",
    plan_type: str = "basic",
) -> MagicMock:
    sub = MagicMock(spec=Subscription)
    sub.mp_subscription_id = mp_id
    sub.status = status
    sub.plan_type = plan_type
    sub.current_period_end = datetime.now(timezone.utc) + timedelta(days=30)
    return sub


def _make_mp_sdk(create_result=None):
    sdk = MagicMock()
    if create_result is None:
        create_result = {
            "status": 201,
            "response": {"init_point": "https://mp.com/checkout/123"},
        }
    sdk.preapproval.return_value.create.return_value = create_result
    sdk.preapproval.return_value.update.return_value = {"status": 200, "response": {}}
    return sdk


def _setup_app(user=None, bot=None, plan=None, existing_sub=None):
    """Create a TestClient with mocked dependencies for checkout tests."""
    app = FastAPI()
    app.include_router(router)

    user = user or _make_user()
    mock_session = AsyncMock()

    # session.get(Bot, ...) returns the bot
    bot = bot or _make_bot()
    mock_session.get = AsyncMock(return_value=bot)

    # session.execute(...) returns the plan for the select query
    plan = plan or _make_plan()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = plan
    mock_session.execute = AsyncMock(return_value=mock_result)

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_session] = lambda: mock_session

    return TestClient(app), mock_session, existing_sub


# ── Checkout Guard Tests ─────────────────────────────────────────────────────


class TestCheckoutDuplicateGuard:
    """Checkout must block duplicate subscriptions for the same plan."""

    def test_authorized_same_plan_blocked(self):
        existing_sub = _make_subscription(status="authorized", plan_type="pro")
        client, session, _ = _setup_app(existing_sub=existing_sub)
        sdk = _make_mp_sdk()

        with (
            patch("app.billing_routes._get_sdk", return_value=sdk),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=existing_sub,
            ),
        ):
            resp = client.post(
                "/billing/checkout", json={"bot_id": 10, "plan_key": "pro"}
            )

        assert resp.status_code == 409
        assert "assinatura ativa" in resp.json()["detail"]
        sdk.preapproval.return_value.create.assert_not_called()


class TestCheckoutAdminGuard:
    """Checkout must block when subscription is admin-granted."""

    def test_admin_granted_blocked(self):
        existing_sub = _make_subscription(
            mp_id="admin_grant_10", status="authorized", plan_type="basic"
        )
        client, _, _ = _setup_app(existing_sub=existing_sub)
        sdk = _make_mp_sdk()

        with (
            patch("app.billing_routes._get_sdk", return_value=sdk),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=existing_sub,
            ),
        ):
            resp = client.post(
                "/billing/checkout", json={"bot_id": 10, "plan_key": "pro"}
            )

        assert resp.status_code == 409
        assert "administrador" in resp.json()["detail"]
        sdk.preapproval.return_value.create.assert_not_called()


class TestCheckoutUpgrade:
    """Checkout must allow upgrading to a different plan."""

    def test_authorized_different_plan_allowed(self):
        existing_sub = _make_subscription(
            mp_id="mp_old_basic", status="authorized", plan_type="basic"
        )
        client, _, _ = _setup_app(existing_sub=existing_sub)
        sdk = _make_mp_sdk()

        with (
            patch("app.billing_routes._get_sdk", return_value=sdk),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=existing_sub,
            ),
        ):
            resp = client.post(
                "/billing/checkout", json={"bot_id": 10, "plan_key": "pro"}
            )

        assert resp.status_code == 200
        assert "checkout_url" in resp.json()
        sdk.preapproval.return_value.create.assert_called_once()


class TestCheckoutPendingCleanup:
    """Checkout must cancel stale pending subscriptions on MP before creating new."""

    def test_pending_sub_cancelled_on_mp(self):
        existing_sub = _make_subscription(
            mp_id="mp_stale_pending", status="pending", plan_type="pro"
        )
        client, _, _ = _setup_app(existing_sub=existing_sub)
        sdk = _make_mp_sdk()

        with (
            patch("app.billing_routes._get_sdk", return_value=sdk),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=existing_sub,
            ),
        ):
            resp = client.post(
                "/billing/checkout", json={"bot_id": 10, "plan_key": "pro"}
            )

        assert resp.status_code == 200
        # Old pending sub should be cancelled on MP
        sdk.preapproval.return_value.update.assert_called_once_with(
            "mp_stale_pending", {"status": "cancelled"}
        )
        # New sub should be created
        sdk.preapproval.return_value.create.assert_called_once()

    def test_pending_cancel_failure_still_creates_new(self):
        """Even if cancelling old pending fails, checkout proceeds."""
        existing_sub = _make_subscription(
            mp_id="mp_stale_pending", status="pending", plan_type="pro"
        )
        client, _, _ = _setup_app(existing_sub=existing_sub)
        sdk = _make_mp_sdk()
        sdk.preapproval.return_value.update.side_effect = Exception("MP down")

        with (
            patch("app.billing_routes._get_sdk", return_value=sdk),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=existing_sub,
            ),
        ):
            resp = client.post(
                "/billing/checkout", json={"bot_id": 10, "plan_key": "pro"}
            )

        assert resp.status_code == 200
        sdk.preapproval.return_value.create.assert_called_once()


class TestCheckoutResubscribe:
    """Cancelled/paused subscriptions should allow re-subscribing."""

    @pytest.mark.parametrize("status", ["cancelled", "paused"])
    def test_cancelled_or_paused_allows_new_checkout(self, status):
        existing_sub = _make_subscription(
            mp_id="mp_old", status=status, plan_type="basic"
        )
        client, _, _ = _setup_app(existing_sub=existing_sub)
        sdk = _make_mp_sdk()

        with (
            patch("app.billing_routes._get_sdk", return_value=sdk),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=existing_sub,
            ),
        ):
            resp = client.post(
                "/billing/checkout", json={"bot_id": 10, "plan_key": "basic"}
            )

        assert resp.status_code == 200
        sdk.preapproval.return_value.create.assert_called_once()


class TestCheckoutNoExistingSub:
    """First-time checkout (no existing subscription) should work normally."""

    def test_no_existing_sub_creates_new(self):
        client, _, _ = _setup_app()
        sdk = _make_mp_sdk()

        with (
            patch("app.billing_routes._get_sdk", return_value=sdk),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            resp = client.post(
                "/billing/checkout", json={"bot_id": 10, "plan_key": "pro"}
            )

        assert resp.status_code == 200
        assert "checkout_url" in resp.json()


# ── Webhook Guard Tests ──────────────────────────────────────────────────────


def _setup_webhook_app():
    """Create a TestClient for webhook tests (no auth needed)."""
    app = FastAPI()
    app.include_router(router)
    mock_session = AsyncMock()
    mock_session.commit = AsyncMock()
    app.dependency_overrides[get_session] = lambda: mock_session
    return TestClient(app), mock_session


def _webhook_payload(preapproval_id: str = "mp_new_123"):
    return {
        "topic": "subscription_preapproval",
        "data": {"id": preapproval_id},
    }


def _mp_sub_info(
    preapproval_id: str = "mp_new_123",
    status: str = "authorized",
    external_ref: str = "BOT_10_pro",
):
    return {
        "response": {
            "id": preapproval_id,
            "status": status,
            "external_reference": external_ref,
            "auto_recurring": {"frequency": 1, "frequency_type": "months"},
        }
    }


class TestWebhookAdminGuard:
    """Webhook must skip admin-granted subscriptions."""

    def test_admin_granted_skipped(self):
        client, session = _setup_webhook_app()
        bot = _make_bot()
        existing_sub = _make_subscription(mp_id="admin_grant_10")
        session.get = AsyncMock(return_value=bot)

        sdk = MagicMock()
        sdk.preapproval.return_value.get.return_value = _mp_sub_info()

        with (
            patch("app.billing_routes._get_sdk", return_value=sdk),
            patch("app.billing_routes.require_mp_signature", new_callable=AsyncMock),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=existing_sub,
            ),
            patch(
                "app.billing_routes.crud.upsert_subscription",
                new_callable=AsyncMock,
            ) as mock_upsert,
        ):
            resp = client.post("/billing/webhook", json=_webhook_payload())

        assert resp.json()["status"] == "ok"
        mock_upsert.assert_not_called()


class TestWebhookStaleSkip:
    """Webhook must skip stale webhooks from old/orphan subscriptions."""

    @pytest.mark.parametrize("stale_status", ["pending", "paused", "cancelled"])
    def test_mismatched_mp_id_non_authorized_skipped(self, stale_status):
        client, session = _setup_webhook_app()
        bot = _make_bot()
        existing_sub = _make_subscription(mp_id="mp_current_sub")
        session.get = AsyncMock(return_value=bot)

        sdk = MagicMock()
        sdk.preapproval.return_value.get.return_value = _mp_sub_info(
            preapproval_id="mp_orphan_old", status=stale_status
        )

        with (
            patch("app.billing_routes._get_sdk", return_value=sdk),
            patch("app.billing_routes.require_mp_signature", new_callable=AsyncMock),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=existing_sub,
            ),
            patch(
                "app.billing_routes.crud.upsert_subscription",
                new_callable=AsyncMock,
            ) as mock_upsert,
        ):
            resp = client.post(
                "/billing/webhook", json=_webhook_payload("mp_orphan_old")
            )

        assert resp.json()["status"] == "ok"
        mock_upsert.assert_not_called()


class TestWebhookUpgradeConfirmation:
    """When a new mp_id arrives with 'authorized', it's a confirmed upgrade."""

    def test_upgrade_cancels_old_and_upserts(self):
        client, session = _setup_webhook_app()
        bot = _make_bot()
        existing_sub = _make_subscription(
            mp_id="mp_old_basic", status="authorized", plan_type="basic"
        )
        session.get = AsyncMock(return_value=bot)

        sdk = MagicMock()
        sdk.preapproval.return_value.get.return_value = _mp_sub_info(
            preapproval_id="mp_new_pro", status="authorized", external_ref="BOT_10_pro"
        )
        sdk.preapproval.return_value.update.return_value = {
            "status": 200,
            "response": {},
        }

        mock_upsert_result = _make_subscription(
            mp_id="mp_new_pro", status="authorized", plan_type="pro"
        )

        with (
            patch("app.billing_routes._get_sdk", return_value=sdk),
            patch("app.billing_routes.require_mp_signature", new_callable=AsyncMock),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=existing_sub,
            ),
            patch(
                "app.billing_routes.crud.upsert_subscription",
                new_callable=AsyncMock,
                return_value=mock_upsert_result,
            ) as mock_upsert,
        ):
            resp = client.post("/billing/webhook", json=_webhook_payload("mp_new_pro"))

        assert resp.json()["status"] == "ok"
        # Old subscription should be cancelled on MP
        sdk.preapproval.return_value.update.assert_called_once_with(
            "mp_old_basic", {"status": "cancelled"}
        )
        # New subscription should be upserted
        mock_upsert.assert_called_once()
        call_kwargs = mock_upsert.call_args[1]
        assert call_kwargs["mp_id"] == "mp_new_pro"
        assert call_kwargs["status"] == "authorized"
        assert call_kwargs["plan_type"] == "pro"


class TestWebhookMatchingMpId:
    """When mp_id matches, webhook updates normally."""

    def test_matching_mp_id_updates(self):
        client, session = _setup_webhook_app()
        bot = _make_bot()
        existing_sub = _make_subscription(mp_id="mp_current")
        session.get = AsyncMock(return_value=bot)

        sdk = MagicMock()
        sdk.preapproval.return_value.get.return_value = _mp_sub_info(
            preapproval_id="mp_current", status="authorized"
        )

        mock_upsert_result = _make_subscription(mp_id="mp_current")

        with (
            patch("app.billing_routes._get_sdk", return_value=sdk),
            patch("app.billing_routes.require_mp_signature", new_callable=AsyncMock),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=existing_sub,
            ),
            patch(
                "app.billing_routes.crud.upsert_subscription",
                new_callable=AsyncMock,
                return_value=mock_upsert_result,
            ) as mock_upsert,
        ):
            resp = client.post("/billing/webhook", json=_webhook_payload("mp_current"))

        assert resp.json()["status"] == "ok"
        mock_upsert.assert_called_once()


class TestWebhookNoExistingSub:
    """When no subscription exists, webhook creates one (first-time flow)."""

    def test_no_existing_sub_creates(self):
        client, session = _setup_webhook_app()
        bot = _make_bot()
        session.get = AsyncMock(return_value=bot)

        sdk = MagicMock()
        sdk.preapproval.return_value.get.return_value = _mp_sub_info(
            preapproval_id="mp_first", status="authorized"
        )

        mock_upsert_result = _make_subscription(mp_id="mp_first")

        with (
            patch("app.billing_routes._get_sdk", return_value=sdk),
            patch("app.billing_routes.require_mp_signature", new_callable=AsyncMock),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.billing_routes.crud.upsert_subscription",
                new_callable=AsyncMock,
                return_value=mock_upsert_result,
            ) as mock_upsert,
        ):
            resp = client.post("/billing/webhook", json=_webhook_payload("mp_first"))

        assert resp.json()["status"] == "ok"
        mock_upsert.assert_called_once()


class TestWebhookLegacyFallback:
    """Legacy external_reference (BOT_10 without plan) should preserve existing plan_type."""

    def test_legacy_ref_preserves_existing_plan(self):
        client, session = _setup_webhook_app()
        bot = _make_bot()
        existing_sub = _make_subscription(
            mp_id="mp_legacy", status="authorized", plan_type="basic"
        )
        session.get = AsyncMock(return_value=bot)

        sdk = MagicMock()
        # Legacy ref: "BOT_10" without plan key
        sdk.preapproval.return_value.get.return_value = _mp_sub_info(
            preapproval_id="mp_legacy",
            status="authorized",
            external_ref="BOT_10",
        )

        mock_upsert_result = _make_subscription(mp_id="mp_legacy", plan_type="basic")

        with (
            patch("app.billing_routes._get_sdk", return_value=sdk),
            patch("app.billing_routes.require_mp_signature", new_callable=AsyncMock),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=existing_sub,
            ),
            patch(
                "app.billing_routes.crud.upsert_subscription",
                new_callable=AsyncMock,
                return_value=mock_upsert_result,
            ) as mock_upsert,
        ):
            resp = client.post("/billing/webhook", json=_webhook_payload("mp_legacy"))

        assert resp.json()["status"] == "ok"
        call_kwargs = mock_upsert.call_args[1]
        # Must use existing plan_type "basic", not default "pro"
        assert call_kwargs["plan_type"] == "basic"
