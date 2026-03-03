"""Tests for app/health_routes.py — liveness and readiness probes."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.health_routes import router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestLiveness:
    def test_live_returns_200(self, client):
        resp = client.get("/health/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "alive"


class TestReadiness:
    def test_ready_returns_200_when_all_up(self, client):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock()

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock()
        mock_redis.zcard = AsyncMock(return_value=5)
        mock_redis.aclose = AsyncMock()

        with (
            patch("app.health_routes.async_session", return_value=mock_session),
            patch("app.health_routes.redis") as mock_redis_module,
        ):
            mock_redis_module.from_url.return_value = mock_redis
            resp = client.get("/health/ready")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["checks"]["postgresql"]["status"] == "up"
        assert data["checks"]["redis_db0"]["status"] == "up"
        assert data["checks"]["redis_db1_arq"]["status"] == "up"
        assert data["checks"]["redis_db1_arq"]["queue_depth"] == 5

    def test_ready_returns_503_when_db_down(self, client):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(side_effect=ConnectionError("DB down"))

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock()
        mock_redis.zcard = AsyncMock(return_value=0)
        mock_redis.aclose = AsyncMock()

        with (
            patch("app.health_routes.async_session", return_value=mock_session),
            patch("app.health_routes.redis") as mock_redis_module,
        ):
            mock_redis_module.from_url.return_value = mock_redis
            resp = client.get("/health/ready")

        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["checks"]["postgresql"]["status"] == "down"
        # Redis should still be up
        assert data["checks"]["redis_db0"]["status"] == "up"

    def test_ready_returns_503_when_redis_down(self, client):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock()

        with (
            patch("app.health_routes.async_session", return_value=mock_session),
            patch("app.health_routes.redis") as mock_redis_module,
        ):
            mock_redis_module.from_url.side_effect = ConnectionError("Redis down")
            resp = client.get("/health/ready")

        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"
        # PostgreSQL should still be up
        assert data["checks"]["postgresql"]["status"] == "up"

    def test_ready_includes_latency(self, client):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock()

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock()
        mock_redis.zcard = AsyncMock(return_value=0)
        mock_redis.aclose = AsyncMock()

        with (
            patch("app.health_routes.async_session", return_value=mock_session),
            patch("app.health_routes.redis") as mock_redis_module,
        ):
            mock_redis_module.from_url.return_value = mock_redis
            resp = client.get("/health/ready")

        data = resp.json()
        assert "latency_ms" in data["checks"]["postgresql"]
        assert "latency_ms" in data["checks"]["redis_db0"]
        assert "latency_ms" in data["checks"]["redis_db1_arq"]
