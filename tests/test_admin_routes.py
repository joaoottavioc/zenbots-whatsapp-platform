"""Tests for admin plan management and public GET /billing/plans."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin_routes import router as admin_router
from app.billing_routes import router as billing_router
from app.auth import get_current_user, require_admin
from app.database import get_session
from app.models import Bot, Plan, Subscription, User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_user(is_admin: bool = False) -> User:
    user = MagicMock(spec=User)
    user.id = 1
    user.email = "test@example.com"
    user.is_admin = is_admin
    return user


def _make_plan(**overrides) -> Plan:
    defaults = dict(
        id=1,
        key="pro",
        title="ZenBotZ Pro",
        description="Plan description",
        price=199.0,
        currency="BRL",
        frequency=1,
        allows_bot_usage=True,
    )
    defaults.update(overrides)
    plan = MagicMock(spec=Plan)
    for k, v in defaults.items():
        setattr(plan, k, v)
    return plan


@pytest.fixture
def admin_client():
    """TestClient with admin user and mocked session."""
    app = FastAPI()
    app.include_router(admin_router)

    admin_user = _make_user(is_admin=True)
    mock_session = AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    app.dependency_overrides[get_session] = lambda: mock_session

    client = TestClient(app)
    client._mock_session = mock_session  # expose for test assertions
    return client


@pytest.fixture
def non_admin_client():
    """TestClient with a non-admin user."""
    app = FastAPI()
    app.include_router(admin_router)

    regular_user = _make_user(is_admin=False)
    mock_session = AsyncMock()

    # Override get_current_user but NOT require_admin — let the real check run
    app.dependency_overrides[get_current_user] = lambda: regular_user
    app.dependency_overrides[get_session] = lambda: mock_session

    return TestClient(app)


@pytest.fixture
def public_client():
    """TestClient for public billing endpoints (no auth)."""
    app = FastAPI()
    app.include_router(billing_router)

    mock_session = AsyncMock()
    app.dependency_overrides[get_session] = lambda: mock_session

    client = TestClient(app)
    client._mock_session = mock_session
    return client


# ---------------------------------------------------------------------------
# Admin auth gate
# ---------------------------------------------------------------------------


class TestAdminAuthGate:
    def test_non_admin_gets_403(self, non_admin_client):
        resp = non_admin_client.get("/admin/plans")
        assert resp.status_code == 403

    def test_admin_gets_200(self, admin_client):
        with patch("app.crud.list_plans", new_callable=AsyncMock, return_value=[]):
            resp = admin_client.get("/admin/plans")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Admin plan CRUD
# ---------------------------------------------------------------------------


class TestAdminPlanCRUD:
    def test_list_plans(self, admin_client):
        plan = _make_plan()
        with patch("app.crud.list_plans", new_callable=AsyncMock, return_value=[plan]):
            resp = admin_client.get("/admin/plans")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["key"] == "pro"

    def test_create_plan_success(self, admin_client):
        plan = _make_plan()
        with (
            patch(
                "app.crud.get_plan_by_key", new_callable=AsyncMock, return_value=None
            ),
            patch("app.crud.create_plan", new_callable=AsyncMock, return_value=plan),
        ):
            resp = admin_client.post(
                "/admin/plans",
                json={
                    "key": "pro",
                    "title": "ZenBotZ Pro",
                    "description": "Plan description",
                    "price": 199.0,
                },
            )
        assert resp.status_code == 201
        assert resp.json()["key"] == "pro"

    def test_create_plan_duplicate_key_409(self, admin_client):
        existing = _make_plan()
        with patch(
            "app.crud.get_plan_by_key", new_callable=AsyncMock, return_value=existing
        ):
            resp = admin_client.post(
                "/admin/plans",
                json={
                    "key": "pro",
                    "title": "Dup",
                    "description": "Dup",
                    "price": 99.0,
                },
            )
        assert resp.status_code == 409

    def test_update_plan_success(self, admin_client):
        updated = _make_plan(title="Updated Pro")
        with patch(
            "app.crud.update_plan", new_callable=AsyncMock, return_value=updated
        ):
            resp = admin_client.put("/admin/plans/1", json={"title": "Updated Pro"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "Updated Pro"

    def test_update_plan_not_found_404(self, admin_client):
        with patch("app.crud.update_plan", new_callable=AsyncMock, return_value=None):
            resp = admin_client.put("/admin/plans/999", json={"title": "Nope"})
        assert resp.status_code == 404

    def test_update_plan_empty_body_400(self, admin_client):
        resp = admin_client.put("/admin/plans/1", json={})
        assert resp.status_code == 400

    def test_delete_plan_success(self, admin_client):
        with patch("app.crud.delete_plan", new_callable=AsyncMock, return_value=True):
            resp = admin_client.delete("/admin/plans/1")
        assert resp.status_code == 204

    def test_delete_plan_not_found_404(self, admin_client):
        with patch("app.crud.delete_plan", new_callable=AsyncMock, return_value=False):
            resp = admin_client.delete("/admin/plans/999")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Public GET /billing/plans
# ---------------------------------------------------------------------------


class TestPublicPlans:
    def test_list_plans_no_auth(self, public_client):
        plan = _make_plan()
        with patch("app.crud.list_plans", new_callable=AsyncMock, return_value=[plan]):
            resp = public_client.get("/billing/plans")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["price"] == 199.0

    def test_list_plans_empty(self, public_client):
        with patch("app.crud.list_plans", new_callable=AsyncMock, return_value=[]):
            resp = public_client.get("/billing/plans")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# Admin subscription upsert
# ---------------------------------------------------------------------------


def _make_bot(bot_id=1, user_id=1) -> Bot:
    bot = MagicMock(spec=Bot)
    bot.id = bot_id
    bot.user_id = user_id
    return bot


def _make_subscription(status="authorized", plan_type="pro", days_remaining=29):
    from datetime import datetime, timezone, timedelta

    sub = MagicMock(spec=Subscription)
    sub.status = status
    sub.plan_type = plan_type
    sub.current_period_end = datetime.now(timezone.utc) + timedelta(days=days_remaining)
    return sub


class TestAdminUpsertSubscription:
    def test_valid_plan_returns_200(self, admin_client):
        bot = _make_bot()
        plan = _make_plan(key="pro")
        sub = _make_subscription()

        with (
            patch.object(
                admin_client._mock_session,
                "get",
                new_callable=AsyncMock,
                return_value=bot,
            ),
            patch(
                "app.crud.get_plan_by_key", new_callable=AsyncMock, return_value=plan
            ),
            patch(
                "app.crud.upsert_subscription", new_callable=AsyncMock, return_value=sub
            ),
        ):
            resp = admin_client.put(
                "/admin/subscriptions/1",
                json={"status": "authorized", "plan_type": "pro"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "authorized"
        assert data["is_active"] is True

    def test_invalid_plan_returns_400(self, admin_client):
        bot = _make_bot()

        with (
            patch.object(
                admin_client._mock_session,
                "get",
                new_callable=AsyncMock,
                return_value=bot,
            ),
            patch(
                "app.crud.get_plan_by_key", new_callable=AsyncMock, return_value=None
            ),
        ):
            resp = admin_client.put(
                "/admin/subscriptions/1",
                json={"status": "authorized", "plan_type": "nonexistent"},
            )
        assert resp.status_code == 400
        assert "not found" in resp.json()["detail"]

    def test_bot_not_found_returns_404(self, admin_client):
        with patch.object(
            admin_client._mock_session, "get", new_callable=AsyncMock, return_value=None
        ):
            resp = admin_client.put(
                "/admin/subscriptions/999",
                json={"status": "authorized", "plan_type": "pro"},
            )
        assert resp.status_code == 404
