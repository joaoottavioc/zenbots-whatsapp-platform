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
        # Defensive: pin both pieces of env state this test depends on.
        # ENVIRONMENT controls the CORS dev-convenience escape hatch;
        # is_channel_enabled is patched directly because the kill-switch
        # tests at the end of this file flip WEB_CHANNEL_ENABLED in env,
        # and some pytest collection orderings leak that state here.
        patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=False),
        patch("app.chat_routes.is_channel_enabled", return_value=True),
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
        patch("app.chat_routes.is_channel_enabled", return_value=True),
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


# ── Phase 5.2 — per-session daily cap on Free tier ──────────────────


def test_free_tier_session_at_cap_gets_soft_refusal_not_429(client):
    """Free tier + per-session daily cap reached → 202 with no enqueue,
    soft refusal emitted via SSE. A 429 would look like a transport
    error to the widget and trigger client retry loops; the user must
    see the limit message, not a stale UI."""
    bot = _fake_bot()
    fake_redis = MagicMock()
    fake_redis.enqueue_job = AsyncMock()

    sub = MagicMock()
    sub.plan_type = "free"

    captured_broadcasts = []

    async def capture_broadcast(**kwargs):
        captured_broadcasts.append(kwargs)

    original_redis = client.app.state.arq_redis
    client.app.state.arq_redis = fake_redis
    try:
        with (
            patch(
                "app.chat_routes.crud.get_bot_by_id",
                new=AsyncMock(return_value=bot),
            ),
            patch(
                "app.chat_routes.crud.get_subscription_by_bot",
                new=AsyncMock(return_value=sub),
            ),
            # First call (per-IP) passes; second call (per-session cap) hits.
            patch(
                "app.chat_routes.is_rate_limited",
                new=AsyncMock(side_effect=[False, True]),
            ),
            patch("app.chat_routes.broadcast_web_reply", new=capture_broadcast),
        ):
            response = client.post(
                "/chat/1/message",
                json={
                    "session_id": "free-sess-1",
                    "message_id": "m1",
                    "text": "oi",
                },
            )

        assert response.status_code == 202, response.text
        assert response.json()["accepted"] is True
        # ARQ task NOT enqueued — the limit message ate the cost slot.
        fake_redis.enqueue_job.assert_not_awaited()
        # Soft refusal emitted via SSE channel.
        assert len(captured_broadcasts) == 1
        text = captured_broadcasts[0]["text"]
        assert "limite" in text.lower()
        assert "gratuito" in text.lower() or "grátis" in text.lower()
    finally:
        client.app.state.arq_redis = original_redis


def test_pro_tier_bypasses_daily_cap(client):
    """Pro/Founder plans should not be subject to the Free-tier daily
    cap — they pay marginal cost via the subscription."""
    bot = _fake_bot()
    fake_redis = MagicMock()
    fake_redis.enqueue_job = AsyncMock()

    sub = MagicMock()
    sub.plan_type = "pro_monthly"

    original_redis = client.app.state.arq_redis
    client.app.state.arq_redis = fake_redis
    try:
        with (
            patch(
                "app.chat_routes.crud.get_bot_by_id",
                new=AsyncMock(return_value=bot),
            ),
            patch(
                "app.chat_routes.crud.get_subscription_by_bot",
                new=AsyncMock(return_value=sub),
            ),
            # Only the per-IP call should happen; the per-session call
            # must be skipped entirely for Pro.
            patch(
                "app.chat_routes.is_rate_limited",
                new=AsyncMock(return_value=False),
            ) as mock_rl,
        ):
            response = client.post(
                "/chat/1/message",
                json={
                    "session_id": "pro-sess-1",
                    "message_id": "m1",
                    "text": "oi",
                },
            )

        assert response.status_code == 202
        # Exactly ONE is_rate_limited call (per-IP). Not two (per-IP + cap).
        assert mock_rl.await_count == 1
        fake_redis.enqueue_job.assert_awaited_once()
    finally:
        client.app.state.arq_redis = original_redis


# ── Phase 5.6 — web channel kill switch ─────────────────────────────


def test_post_message_returns_503_when_web_channel_disabled(client):
    """WEB_CHANNEL_ENABLED=false → every /chat endpoint 503s. Ops lever
    for incident response; doesn't touch the per-bot enable flag."""
    with patch.dict(os.environ, {"WEB_CHANNEL_ENABLED": "false"}, clear=False):
        response = client.post(
            "/chat/1/message",
            json={"session_id": "s", "message_id": "m1", "text": "hi"},
        )
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "web_channel_disabled"


def test_post_session_returns_503_when_web_channel_disabled(client):
    with patch.dict(os.environ, {"WEB_CHANNEL_ENABLED": "false"}, clear=False):
        response = client.post("/chat/1/session")
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "web_channel_disabled"
