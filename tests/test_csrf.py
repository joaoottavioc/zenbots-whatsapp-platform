"""Tests for CSRFMiddleware (double-submit cookie pattern)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.csrf import CSRFMiddleware


def _make_app() -> FastAPI:
    """Minimal app with CSRF middleware and a few routes for testing."""
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.get("/items")
    async def get_items():
        return {"items": []}

    @app.post("/items")
    async def create_item():
        return {"created": True}

    @app.post("/auth/token")
    async def login():
        return {"ok": True}

    @app.post("/auth/google")
    async def google_login():
        return {"ok": True}

    @app.post("/auth/register")
    async def register():
        return {"ok": True}

    @app.post("/auth/logout")
    async def logout():
        return {"ok": True}

    @app.post("/webhook/whatsapp")
    async def webhook():
        return {"ok": True}

    @app.post("/payments/webhooks/confirm")
    async def payment_webhook():
        return {"ok": True}

    @app.post("/billing/webhook")
    async def billing_webhook():
        return {"ok": True}

    @app.post("/health/ready")
    async def health():
        return {"ok": True}

    return app


@pytest.fixture
def client():
    return TestClient(_make_app())


# ---------- Safe methods pass through ----------


def test_get_passes_without_csrf(client):
    resp = client.get("/items")
    assert resp.status_code == 200


# ---------- Exempt paths pass through ----------


def test_auth_token_exempt(client):
    resp = client.post(
        "/auth/token", cookies={"access_token": "jwt", "csrf_token": "abc"}
    )
    assert resp.status_code == 200


def test_auth_google_exempt(client):
    """A stale access_token cookie must not block re-authenticating via Google."""
    resp = client.post(
        "/auth/google", cookies={"access_token": "jwt", "csrf_token": "abc"}
    )
    assert resp.status_code == 200


def test_auth_register_exempt(client):
    resp = client.post("/auth/register", cookies={"access_token": "jwt"})
    assert resp.status_code == 200


def test_auth_logout_exempt(client):
    resp = client.post("/auth/logout", cookies={"access_token": "jwt"})
    assert resp.status_code == 200


def test_webhook_prefix_exempt(client):
    resp = client.post("/webhook/whatsapp", cookies={"access_token": "jwt"})
    assert resp.status_code == 200


def test_payment_webhook_exempt(client):
    resp = client.post("/payments/webhooks/confirm", cookies={"access_token": "jwt"})
    assert resp.status_code == 200


def test_billing_webhook_exempt(client):
    resp = client.post("/billing/webhook", cookies={"access_token": "jwt"})
    assert resp.status_code == 200


def test_health_prefix_exempt(client):
    resp = client.post("/health/ready", cookies={"access_token": "jwt"})
    assert resp.status_code == 200


# ---------- Bearer-only (no access_token cookie) passes ----------


def test_no_cookie_passes_without_csrf(client):
    """When no access_token cookie is present (Bearer-only client), skip CSRF."""
    resp = client.post("/items")
    assert resp.status_code == 200


# ---------- CSRF enforcement when cookie auth is active ----------


def test_matching_csrf_passes(client):
    resp = client.post(
        "/items",
        cookies={"access_token": "jwt", "csrf_token": "tok123"},
        headers={"X-CSRF-Token": "tok123"},
    )
    assert resp.status_code == 200


def test_missing_csrf_header_returns_403(client):
    resp = client.post(
        "/items",
        cookies={"access_token": "jwt", "csrf_token": "tok123"},
    )
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]


def test_mismatched_csrf_returns_403(client):
    resp = client.post(
        "/items",
        cookies={"access_token": "jwt", "csrf_token": "tok123"},
        headers={"X-CSRF-Token": "wrong"},
    )
    assert resp.status_code == 403


def test_missing_csrf_cookie_returns_403(client):
    """access_token cookie present but no csrf_token cookie."""
    resp = client.post(
        "/items",
        cookies={"access_token": "jwt"},
        headers={"X-CSRF-Token": "something"},
    )
    assert resp.status_code == 403
