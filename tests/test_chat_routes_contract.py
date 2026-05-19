"""Contract tests for the /chat endpoints (plan/in_browser_bots.md Phase 2.3).

Phase 2.1 shipped the 501 stubs that pinned the schemas. Phase 2.3 wired
the real bodies (enqueue + SSE forwarder + per-IP rate limit + CORS).
These tests now exercise the real behavior:

  - Routing + Pydantic validation (preserved from Phase 2.1)
  - 404 / 403 when the bot doesn't exist or has the widget disabled
  - CORS allowlist + dev-convenience escape hatch
  - Per-IP rate limit enforcement
  - Successful enqueue produces 202 with echo
  - Session handshake returns welcome + theme + plan_tier

We DON'T cover the SSE endpoint here — that's a streaming test better
suited to the simulation suite (test_web_channel_e2e.py covers the
PubSub round-trip directly).
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def _fake_bot(
    bot_id: int = 1,
    web_widget_enabled: bool = True,
    allowed_origins: list[str] | None = None,
    theme: dict | None = None,
):
    """Build a minimal Bot stand-in (MagicMock) that satisfies the
    attributes _load_widget_bot and _check_origin read."""
    bot = MagicMock()
    bot.id = bot_id
    bot.web_widget_enabled = web_widget_enabled
    bot.web_widget_allowed_origins = allowed_origins or []
    bot.web_widget_theme = theme or {}
    bot.restaurant_name = "Sabor da Serra"
    return bot


# ── Routing + validation (preserved from Phase 2.1) ──────────────────


def test_post_message_rejects_missing_session_id(client):
    response = client.post(
        "/chat/1/message",
        json={"message_id": "m1", "text": "x"},
    )
    assert response.status_code == 422


def test_post_message_rejects_empty_text(client):
    response = client.post(
        "/chat/1/message",
        json={"session_id": "s", "message_id": "m1", "text": ""},
    )
    assert response.status_code == 422


def test_post_message_rejects_text_above_4096_chars(client):
    long_text = "a" * 4097
    response = client.post(
        "/chat/1/message",
        json={"session_id": "s", "message_id": "m1", "text": long_text},
    )
    assert response.status_code == 422


def test_post_message_rejects_bot_id_zero(client):
    response = client.post(
        "/chat/0/message",
        json={"session_id": "s", "message_id": "m1", "text": "hi"},
    )
    assert response.status_code == 422


# ── Bot lookup + enable gate ─────────────────────────────────────────


def test_post_message_returns_404_when_bot_not_found(client):
    """The bot lookup miss surfaces as a 404 with a structured detail."""
    with (
        patch("app.chat_routes.crud.get_bot_by_id", new=AsyncMock(return_value=None)),
    ):
        response = client.post(
            "/chat/999/message",
            json={"session_id": "s", "message_id": "m1", "text": "hi"},
        )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "bot_not_found"


def test_post_message_returns_403_when_widget_disabled(client):
    """A bot with web_widget_enabled=False rejects the request — the
    widget shouldn't even render, but a stale embed could try anyway."""
    bot = _fake_bot(web_widget_enabled=False)
    with patch("app.chat_routes.crud.get_bot_by_id", new=AsyncMock(return_value=bot)):
        response = client.post(
            "/chat/1/message",
            json={"session_id": "s", "message_id": "m1", "text": "hi"},
        )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "web_widget_disabled"


# ── CORS allowlist ───────────────────────────────────────────────────


def test_post_message_dev_allows_empty_allowlist(client):
    """In dev, an empty allowed_origins list permits any Origin — the
    bot owner hasn't configured anything yet and we don't want to block
    local testing. Production has a stricter posture (see next test)."""
    bot = _fake_bot(allowed_origins=[])
    fake_redis = MagicMock()
    fake_redis.enqueue_job = AsyncMock()

    with (
        patch("app.chat_routes.crud.get_bot_by_id", new=AsyncMock(return_value=bot)),
        patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=False),
    ):
        response = client.post(
            "/chat/1/message",
            json={"session_id": "s", "message_id": "m1", "text": "hi"},
            headers={"Origin": "https://random-site.com"},
        )
    # 202 OR 429 OR 500 — but NOT 403.
    assert response.status_code != 403, (
        f"Empty allowlist in dev shouldn't 403, got {response.status_code} {response.text}"
    )


def test_post_message_prod_rejects_unlisted_origin(client):
    bot = _fake_bot(allowed_origins=["https://meurestaurante.com.br"])
    with (
        patch("app.chat_routes.crud.get_bot_by_id", new=AsyncMock(return_value=bot)),
        patch.dict(os.environ, {"ENVIRONMENT": "production"}, clear=False),
    ):
        response = client.post(
            "/chat/1/message",
            json={"session_id": "s", "message_id": "m1", "text": "hi"},
            headers={"Origin": "https://attacker.com"},
        )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "origin_not_allowed"


def test_post_message_allows_listed_origin(client):
    bot = _fake_bot(allowed_origins=["https://meurestaurante.com.br"])
    fake_redis = MagicMock()
    fake_redis.enqueue_job = AsyncMock()

    original_redis = client.app.state.arq_redis
    client.app.state.arq_redis = fake_redis
    try:
        with (
            patch(
                "app.chat_routes.crud.get_bot_by_id",
                new=AsyncMock(return_value=bot),
            ),
            patch(
                "app.chat_routes.is_rate_limited",
                new=AsyncMock(return_value=False),
            ),
        ):
            response = client.post(
                "/chat/1/message",
                json={"session_id": "s", "message_id": "m1", "text": "hi"},
                headers={"Origin": "https://meurestaurante.com.br"},
            )
        assert response.status_code == 202
        fake_redis.enqueue_job.assert_awaited_once()
    finally:
        # Restore so TestClient teardown awaits the real ArqRedis.close().
        client.app.state.arq_redis = original_redis


# ── Rate limit ───────────────────────────────────────────────────────


def test_post_message_returns_429_when_rate_limited(client):
    bot = _fake_bot()
    with (
        patch("app.chat_routes.crud.get_bot_by_id", new=AsyncMock(return_value=bot)),
        patch("app.chat_routes.is_rate_limited", new=AsyncMock(return_value=True)),
    ):
        response = client.post(
            "/chat/1/message",
            json={"session_id": "s", "message_id": "m1", "text": "hi"},
        )
    assert response.status_code == 429
    assert response.json()["detail"]["error"] == "rate_limited"
    # Retry-After header tells the widget when to retry.
    assert "retry-after" in {h.lower() for h in response.headers}


# ── Enqueue path ─────────────────────────────────────────────────────


def test_post_message_enqueues_process_chat_message(client):
    """Happy path: 202 + echoed message_id, ARQ task enqueued with the
    right positional args in the right order."""
    bot = _fake_bot()
    fake_redis = MagicMock()
    fake_redis.enqueue_job = AsyncMock()

    original_redis = client.app.state.arq_redis
    client.app.state.arq_redis = fake_redis
    try:
        with (
            patch(
                "app.chat_routes.crud.get_bot_by_id",
                new=AsyncMock(return_value=bot),
            ),
            patch(
                "app.chat_routes.is_rate_limited",
                new=AsyncMock(return_value=False),
            ),
        ):
            response = client.post(
                "/chat/1/message",
                json={
                    "session_id": "sess-uuid-abc",
                    "message_id": "msg-uuid-1",
                    "text": "quero uma coca",
                },
            )

        assert response.status_code == 202
        body = response.json()
        assert body["accepted"] is True
        assert body["message_id"] == "msg-uuid-1"

        fake_redis.enqueue_job.assert_awaited_once_with(
            "process_chat_message",
            1,
            "sess-uuid-abc",
            "msg-uuid-1",
            "quero uma coca",
        )
    finally:
        client.app.state.arq_redis = original_redis


# ── Session handshake ────────────────────────────────────────────────


def test_post_session_returns_handshake_payload(client):
    bot = _fake_bot(
        theme={
            "primary_color": "#00B14F",
            "position": "br",
            "welcome_message": "Bem-vindo!",
        },
    )
    fake_sub = MagicMock()
    fake_sub.plan_type = "pro_monthly"
    with (
        patch("app.chat_routes.crud.get_bot_by_id", new=AsyncMock(return_value=bot)),
        patch(
            "app.chat_routes.crud.get_subscription_by_bot",
            new=AsyncMock(return_value=fake_sub),
        ),
    ):
        response = client.post("/chat/1/session")

    assert response.status_code == 200
    body = response.json()
    assert body["bot_display_name"] == "Sabor da Serra"
    assert body["welcome_message"] == "Bem-vindo!"
    assert body["theme"]["primary_color"] == "#00B14F"
    assert body["plan_tier"] == "pro_monthly"
    # session_id is a fresh UUID
    assert len(body["session_id"]) >= 16


def test_post_session_defaults_welcome_when_theme_missing_one(client):
    bot = _fake_bot(theme={})
    with (
        patch("app.chat_routes.crud.get_bot_by_id", new=AsyncMock(return_value=bot)),
        patch(
            "app.chat_routes.crud.get_subscription_by_bot",
            new=AsyncMock(return_value=None),
        ),
    ):
        response = client.post("/chat/1/session")

    assert response.status_code == 200
    body = response.json()
    assert "Sabor da Serra" in body["welcome_message"]
    assert body["plan_tier"] == "free"  # no subscription → free tier


def test_post_session_returns_404_for_unknown_bot(client):
    with patch("app.chat_routes.crud.get_bot_by_id", new=AsyncMock(return_value=None)):
        response = client.post("/chat/999/session")
    assert response.status_code == 404
