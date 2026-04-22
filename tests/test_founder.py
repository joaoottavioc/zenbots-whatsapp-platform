"""Founder slot counter: atomic reserve/release + sunset + snapshotting."""

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import founder
from app.auth import get_current_user
from app.billing_routes import router
from app.database import get_session


class _FakeRedis:
    """In-memory stand-in for the founder counter client."""

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
def _reset_founder_state(monkeypatch):
    """Give every test a fresh Redis and a predictable sunset."""
    fake = _FakeRedis()
    monkeypatch.setattr(founder, "_client", fake)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    monkeypatch.setenv("FOUNDER_SUNSET_AT", tomorrow)
    yield fake


class TestFounderCounter:
    @pytest.mark.asyncio
    async def test_initial_state_is_30_slots_available(self):
        assert await founder.slots_remaining() == 30
        assert await founder.is_available() is True

    @pytest.mark.asyncio
    async def test_reserve_decrements_remaining(self):
        assert await founder.reserve_slot() is True
        assert await founder.slots_remaining() == 29

    @pytest.mark.asyncio
    async def test_reserve_31st_slot_fails_and_does_not_leak(self):
        for _ in range(30):
            assert await founder.reserve_slot() is True
        assert await founder.slots_remaining() == 0
        assert await founder.reserve_slot() is False
        # Remaining must still be 0 — the failed INCR was rolled back
        assert await founder.slots_remaining() == 0

    @pytest.mark.asyncio
    async def test_release_restores_slot(self):
        await founder.reserve_slot()
        await founder.release_slot()
        assert await founder.slots_remaining() == 30

    @pytest.mark.asyncio
    async def test_release_clamps_at_zero(self):
        await founder.release_slot()  # underflow attempt on fresh state
        assert await founder.slots_remaining() == 30

    @pytest.mark.asyncio
    async def test_sunset_blocks_reservation(self, monkeypatch):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        monkeypatch.setenv("FOUNDER_SUNSET_AT", yesterday)
        assert founder.sunset_passed() is True
        assert await founder.is_available() is False
        assert await founder.reserve_slot() is False


@pytest.fixture
def mock_user():
    user = MagicMock()
    user.id = 1
    user.email = "founder@example.com"
    return user


def _session_with_founder_plan():
    bot = MagicMock()
    bot.id = 10
    bot.user_id = 1
    bot.restaurant_name = "Pizzaria Fundador"

    plan = MagicMock()
    plan.key = "founder"
    plan.title = "Founder Lifetime"
    plan.price = 59.90
    plan.billing_cycle_months = 1

    session = AsyncMock()
    session.get = AsyncMock(return_value=bot)
    res = MagicMock()
    res.scalars.return_value.first.return_value = plan
    session.execute = AsyncMock(return_value=res)
    return session


def _make_client(session, user):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _mp_success_sdk():
    sdk = MagicMock()
    sdk.preapproval.return_value.create.return_value = {
        "status": 201,
        "response": {"init_point": "https://mp.example/checkout"},
    }
    return sdk


class TestFounderCheckoutGate:
    @pytest.mark.asyncio
    async def test_checkout_rejects_when_slots_exhausted(self, mock_user):
        # Burn all 30 slots first
        for _ in range(30):
            await founder.reserve_slot()

        session = _session_with_founder_plan()
        client = _make_client(session, mock_user)

        with (
            patch("app.billing_routes._get_sdk", return_value=_mp_success_sdk()),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            resp = client.post(
                "/billing/checkout", json={"bot_id": 10, "plan_key": "founder"}
            )

        assert resp.status_code == 409
        assert "Fundador" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_checkout_rejects_after_sunset(self, mock_user, monkeypatch):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        monkeypatch.setenv("FOUNDER_SUNSET_AT", yesterday)

        session = _session_with_founder_plan()
        client = _make_client(session, mock_user)

        with (
            patch("app.billing_routes._get_sdk", return_value=_mp_success_sdk()),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            resp = client.post(
                "/billing/checkout", json={"bot_id": 10, "plan_key": "founder"}
            )

        assert resp.status_code == 410
        assert "encerrada" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_successful_founder_checkout_reserves_one_slot(self, mock_user):
        session = _session_with_founder_plan()
        client = _make_client(session, mock_user)

        with (
            patch("app.billing_routes._get_sdk", return_value=_mp_success_sdk()),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            resp = client.post(
                "/billing/checkout", json={"bot_id": 10, "plan_key": "founder"}
            )

        assert resp.status_code == 200
        assert await founder.slots_remaining() == 29

    @pytest.mark.asyncio
    async def test_mp_failure_releases_slot(self, mock_user):
        session = _session_with_founder_plan()
        client = _make_client(session, mock_user)

        sdk = MagicMock()
        sdk.preapproval.return_value.create.return_value = {
            "status": 400,
            "response": {"message": "simulated MP failure"},
        }

        with (
            patch("app.billing_routes._get_sdk", return_value=sdk),
            patch(
                "app.billing_routes.crud.get_subscription_by_bot",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            resp = client.post(
                "/billing/checkout", json={"bot_id": 10, "plan_key": "founder"}
            )

        assert resp.status_code == 400
        # Slot must be released so the promo isn't silently drained by
        # upstream outages.
        assert await founder.slots_remaining() == 30


class TestFounderRemainingEndpoint:
    def test_endpoint_returns_live_state(self, mock_user):
        session = AsyncMock()
        client = _make_client(session, mock_user)
        resp = client.get("/billing/founder-remaining")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_slots"] == 30
        assert body["remaining"] == 30
        assert body["price_brl"] == 59.90
        assert body["available"] is True
        assert "sunset_at" in body
