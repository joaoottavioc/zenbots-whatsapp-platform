"""Tests for app/monitoring_routes.py — monitoring API endpoints."""

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.monitoring_routes import router, _get_user_bot_ids
from app.models import User, Bot, DailyCostSummary


@pytest.fixture
def mock_user():
    user = MagicMock(spec=User)
    user.id = 1
    user.email = "test@test.com"
    return user


@pytest.fixture
def client(mock_user):
    app = FastAPI()
    app.include_router(router)

    # Override auth dependency
    async def override_user():
        return mock_user

    from app.auth import get_current_user
    app.dependency_overrides[get_current_user] = override_user

    return TestClient(app)


class TestOverview:
    def test_overview_returns_json(self, client, mock_user):
        mock_session = AsyncMock()
        # Mock bot IDs query
        mock_bot_result = MagicMock()
        mock_bot_result.all.return_value = [(1,), (2,)]
        # Mock aggregation query - empty results
        mock_agg_result = MagicMock()
        mock_agg_result.all.return_value = []
        mock_session.execute = AsyncMock(side_effect=[mock_bot_result, mock_agg_result])

        from app.database import get_session
        async def override_session():
            yield mock_session

        client.app.dependency_overrides[get_session] = override_session

        resp = client.get("/monitoring/overview?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert "days" in data
        assert "total_cost_usd" in data
        assert "services" in data
        assert data["bots_count"] == 2

    def test_overview_no_bots(self, client, mock_user):
        mock_session = AsyncMock()
        mock_bot_result = MagicMock()
        mock_bot_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_bot_result)

        from app.database import get_session
        async def override_session():
            yield mock_session

        client.app.dependency_overrides[get_session] = override_session

        resp = client.get("/monitoring/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_cost_usd"] == 0
        assert data["bots_count"] == 0


class TestBotDetail:
    def test_bot_not_found_returns_404(self, client, mock_user):
        mock_session = AsyncMock()
        mock_bot_result = MagicMock()
        mock_bot_result.all.return_value = [(2,), (3,)]  # bot_id 1 not owned
        mock_session.execute = AsyncMock(return_value=mock_bot_result)

        from app.database import get_session
        async def override_session():
            yield mock_session

        client.app.dependency_overrides[get_session] = override_session

        resp = client.get("/monitoring/bot/1?days=7")
        assert resp.status_code == 404


class TestLeaderboard:
    def test_leaderboard_returns_json(self, client, mock_user):
        mock_session = AsyncMock()
        mock_bot_result = MagicMock()
        mock_bot_result.all.return_value = [(1,), (2,)]
        mock_agg_result = MagicMock()
        mock_agg_result.all.return_value = []
        mock_session.execute = AsyncMock(side_effect=[mock_bot_result, mock_agg_result])

        from app.database import get_session
        async def override_session():
            yield mock_session

        client.app.dependency_overrides[get_session] = override_session

        resp = client.get("/monitoring/leaderboard?days=1&limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert "days" in data
        assert "bots" in data

    def test_leaderboard_no_bots(self, client, mock_user):
        mock_session = AsyncMock()
        mock_bot_result = MagicMock()
        mock_bot_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_bot_result)

        from app.database import get_session
        async def override_session():
            yield mock_session

        client.app.dependency_overrides[get_session] = override_session

        resp = client.get("/monitoring/leaderboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["bots"] == []
