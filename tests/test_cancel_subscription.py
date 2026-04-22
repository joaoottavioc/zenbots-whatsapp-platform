"""POST /billing/cancel + GET /billing/current-plan.

Covers:
- Happy path: cancel marks subscription, MP cancel called, cache invalidated
- Founder inside refund window → slot released
- Founder outside refund window → slot NOT released (consumed forever)
- Admin-granted subscription → 409
- Already-cancelled subscription → 409 (idempotency guard)
- current-plan returns paid plan while in grace period
- current-plan returns Free after grace period ends
- current-plan returns Founder snapshotted price, not current Plan.price
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import founder
from app.auth import get_current_user
from app.billing_routes import router
from app.database import get_session


class _FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, *args, **kwargs):
        self.kv[key] = str(value)

    async def incr(self, key):
        v = int(self.kv.get(key, "0")) + 1
        self.kv[key] = str(v)
        return v

    async def decr(self, key):
        v = int(self.kv.get(key, "0")) - 1
        self.kv[key] = str(v)
        return v


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(founder, "_client", fake)
    yield fake


@pytest.fixture
def mock_user():
    user = MagicMock()
    user.id = 1
    user.email = "resto@example.com"
    return user


def _make_bot():
    bot = MagicMock()
    bot.id = 10
    bot.user_id = 1
    return bot


def _make_session(bot):
    session = AsyncMock()
    session.get = AsyncMock(return_value=bot)
    session.add = MagicMock()
    session.commit = AsyncMock()
    return session


def _client(session, user):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _make_sub(
    *,
    plan_type: str = "pro_monthly",
    is_founder: bool = False,
    snapshotted_price_brl: float | None = None,
    cancel_at_period_end: bool = False,
    status: str = "authorized",
    mp_id: str = "PAP-XYZ",
    created_days_ago: int = 5,
    period_end_days_from_now: int = 25,
):
    sub = MagicMock()
    sub.mp_subscription_id = mp_id
    sub.plan_type = plan_type
    sub.is_founder = is_founder
    sub.snapshotted_price_brl = snapshotted_price_brl
    sub.cancel_at_period_end = cancel_at_period_end
    sub.cancelled_at = None
    sub.cancelled_reason = None
    sub.status = status
    sub.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=created_days_ago
    )
    sub.current_period_end = datetime.now(timezone.utc).replace(
        tzinfo=None
    ) + timedelta(days=period_end_days_from_now)
    return sub


class TestCancelSubscription:
    def test_happy_path_marks_cancel_at_period_end(self, mock_user):
        bot = _make_bot()
        session = _make_session(bot)
        sub = _make_sub()

        with (
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=sub,
            ),
            patch("app.billing_routes._get_sdk", return_value=MagicMock()) as mock_sdk,
            patch(
                "app.billing_routes.billing_cache.invalidate_by_bot_id",
                new_callable=AsyncMock,
            ) as mock_invalidate,
        ):
            resp = _client(session, mock_user).post(
                "/billing/cancel", json={"bot_id": 10, "reason": "Too expensive"}
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "cancelled_at_period_end"
        assert body["cancel_at_period_end"] is True
        assert body["plan_type"] == "pro_monthly"
        assert sub.cancel_at_period_end is True
        assert sub.cancelled_reason == "Too expensive"
        # MP cancellation was attempted
        mock_sdk.return_value.preapproval.return_value.update.assert_called_once()
        mock_invalidate.assert_awaited_once_with(10)

    def test_admin_granted_subscription_rejected(self, mock_user):
        bot = _make_bot()
        session = _make_session(bot)
        sub = _make_sub(mp_id="admin_grant_10")

        with patch(
            "app.billing_routes.crud.get_subscription_by_bot",
            new_callable=AsyncMock,
            return_value=sub,
        ):
            resp = _client(session, mock_user).post(
                "/billing/cancel", json={"bot_id": 10}
            )

        assert resp.status_code == 409
        assert "administrador" in resp.json()["detail"].lower()

    def test_already_cancelled_rejected(self, mock_user):
        bot = _make_bot()
        session = _make_session(bot)
        sub = _make_sub(cancel_at_period_end=True)

        with patch(
            "app.billing_routes.crud.get_subscription_by_bot",
            new_callable=AsyncMock,
            return_value=sub,
        ):
            resp = _client(session, mock_user).post(
                "/billing/cancel", json={"bot_id": 10}
            )

        assert resp.status_code == 409

    def test_no_subscription_404(self, mock_user):
        bot = _make_bot()
        session = _make_session(bot)

        with patch(
            "app.billing_routes.crud.get_subscription_by_bot",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = _client(session, mock_user).post(
                "/billing/cancel", json={"bot_id": 10}
            )

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_founder_inside_refund_window_releases_slot(self, mock_user):
        # Pre-load: 5 founders already in, this one is #6
        for _ in range(5):
            await founder.reserve_slot()
        await founder.reserve_slot()  # represents our cancelling user
        assert await founder.slots_remaining() == 24

        bot = _make_bot()
        session = _make_session(bot)
        sub = _make_sub(
            plan_type="founder",
            is_founder=True,
            created_days_ago=10,  # within 30-day refund window
            mp_id="PAP-FDR-1",
        )

        with (
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=sub,
            ),
            patch("app.billing_routes._get_sdk", return_value=MagicMock()),
            patch(
                "app.billing_routes.billing_cache.invalidate_by_bot_id",
                new_callable=AsyncMock,
            ),
        ):
            resp = _client(session, mock_user).post(
                "/billing/cancel", json={"bot_id": 10}
            )

        assert resp.status_code == 200
        assert await founder.slots_remaining() == 25

    @pytest.mark.asyncio
    async def test_founder_outside_refund_window_keeps_slot_consumed(self, mock_user):
        await founder.reserve_slot()
        assert await founder.slots_remaining() == 29

        bot = _make_bot()
        session = _make_session(bot)
        sub = _make_sub(
            plan_type="founder",
            is_founder=True,
            created_days_ago=45,  # outside refund window
            mp_id="PAP-FDR-2",
        )

        with (
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=sub,
            ),
            patch("app.billing_routes._get_sdk", return_value=MagicMock()),
            patch(
                "app.billing_routes.billing_cache.invalidate_by_bot_id",
                new_callable=AsyncMock,
            ),
        ):
            resp = _client(session, mock_user).post(
                "/billing/cancel", json={"bot_id": 10}
            )

        assert resp.status_code == 200
        # Slot is NOT released — consumed forever per 2.2 rules
        assert await founder.slots_remaining() == 29

    def test_mp_cancel_failure_does_not_block_local_cancel(self, mock_user):
        bot = _make_bot()
        session = _make_session(bot)
        sub = _make_sub()

        failing_sdk = MagicMock()
        failing_sdk.preapproval.return_value.update.side_effect = Exception("MP down")

        with (
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=sub,
            ),
            patch("app.billing_routes._get_sdk", return_value=failing_sdk),
            patch(
                "app.billing_routes.billing_cache.invalidate_by_bot_id",
                new_callable=AsyncMock,
            ),
        ):
            resp = _client(session, mock_user).post(
                "/billing/cancel", json={"bot_id": 10}
            )

        assert resp.status_code == 200
        assert sub.cancel_at_period_end is True


def _plan(
    key: str = "pro_monthly",
    tier: str = "pro",
    title: str = "Pro Mensal",
    price: float = 129.90,
    billing_cycle_months: int = 1,
    monthly_order_cap: int | None = None,
    fair_use_orders_cap: int | None = 5000,
    overage_per_order_brl: float | None = None,
):
    p = MagicMock()
    p.key = key
    p.tier = tier
    p.title = title
    p.price = price
    p.billing_cycle_months = billing_cycle_months
    p.monthly_order_cap = monthly_order_cap
    p.fair_use_orders_cap = fair_use_orders_cap
    p.overage_per_order_brl = overage_per_order_brl
    return p


class TestCurrentPlan:
    def test_no_subscription_returns_free(self, mock_user):
        bot = _make_bot()
        session = _make_session(bot)
        free = _plan(
            key="free",
            tier="free",
            title="Grátis",
            price=0.0,
            monthly_order_cap=15,
            overage_per_order_brl=1.39,
        )

        with (
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.billing_routes.crud.get_plan_by_key",
                new_callable=AsyncMock,
                return_value=free,
            ),
        ):
            resp = _client(session, mock_user).get("/billing/current-plan?bot_id=10")

        assert resp.status_code == 200
        body = resp.json()
        assert body["plan_key"] == "free"
        assert body["plan_tier"] == "free"
        assert body["cancel_at_period_end"] is False
        assert body["is_founder"] is False

    def test_graceful_cancel_within_period_shows_paid_plan(self, mock_user):
        bot = _make_bot()
        session = _make_session(bot)
        sub = _make_sub(cancel_at_period_end=True, period_end_days_from_now=10)

        with (
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=sub,
            ),
            patch(
                "app.billing_routes.crud.get_plan_by_key",
                new_callable=AsyncMock,
                return_value=_plan(),
            ),
        ):
            resp = _client(session, mock_user).get("/billing/current-plan?bot_id=10")

        body = resp.json()
        assert body["plan_key"] == "pro_monthly"
        assert body["cancel_at_period_end"] is True

    def test_graceful_cancel_after_period_shows_free(self, mock_user):
        bot = _make_bot()
        session = _make_session(bot)
        sub = _make_sub(
            cancel_at_period_end=True, period_end_days_from_now=-5, status="cancelled"
        )
        free = _plan(key="free", tier="free", title="Grátis", price=0.0)

        with (
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=sub,
            ),
            patch(
                "app.billing_routes.crud.get_plan_by_key",
                new_callable=AsyncMock,
                return_value=free,
            ),
        ):
            resp = _client(session, mock_user).get("/billing/current-plan?bot_id=10")

        body = resp.json()
        assert body["plan_key"] == "free"
        assert body["cancel_at_period_end"] is True

    def test_founder_shows_snapshotted_price_not_current(self, mock_user):
        """If Plan.price is bumped to 79.90 later, founder still sees 59.90."""
        bot = _make_bot()
        session = _make_session(bot)
        sub = _make_sub(
            plan_type="founder",
            is_founder=True,
            snapshotted_price_brl=59.90,
        )
        # Simulate a later price bump on the Plan row
        bumped_plan = _plan(
            key="founder", tier="founder", title="Founder Lifetime", price=79.90
        )

        with (
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=sub,
            ),
            patch(
                "app.billing_routes.crud.get_plan_by_key",
                new_callable=AsyncMock,
                return_value=bumped_plan,
            ),
        ):
            resp = _client(session, mock_user).get("/billing/current-plan?bot_id=10")

        body = resp.json()
        assert body["price"] == 59.90
        assert body["is_founder"] is True
