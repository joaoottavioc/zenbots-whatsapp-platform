"""Contract tests for the /chat endpoints (plan/in_browser_bots.md Phase 2.1).

Phase 2.1 ships the route signatures and Pydantic schemas — the bodies
return 501 until Phase 2.2-2.4 wire the actual ARQ task, SSE stream, and
Contact creation. These tests pin:

  1. The routes are mounted (no 404).
  2. Request validation fires BEFORE the 501 (so the frontend widget
     gets useful Pydantic errors during development).
  3. Schemas reject obviously bad payloads (text too long, missing fields).

When Phase 2.2+ lands, the 501 expectations flip to real status codes;
the validation tests stay green as regression coverage.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


# ── Routing ──────────────────────────────────────────────────────────


def test_post_message_is_mounted(client):
    """The endpoint exists; we get 501 (Phase 2.2 stub), not 404."""
    response = client.post(
        "/chat/1/message",
        json={
            "session_id": "abc-123",
            "message_id": "msg-1",
            "text": "olá",
        },
    )
    assert response.status_code == 501
    body = response.json()
    assert body["detail"]["error"] == "phase_2_not_implemented"


def test_get_stream_is_mounted(client):
    response = client.get("/chat/1/stream?session_id=abc-123")
    assert response.status_code == 501


def test_post_session_is_mounted(client):
    response = client.post("/chat/1/session")
    assert response.status_code == 501


# ── Pydantic validation fires BEFORE the 501 ─────────────────────────


def test_post_message_rejects_missing_session_id(client):
    response = client.post(
        "/chat/1/message",
        json={"message_id": "m1", "text": "x"},
    )
    assert response.status_code == 422  # FastAPI/Pydantic validation


def test_post_message_rejects_missing_text(client):
    response = client.post(
        "/chat/1/message",
        json={"session_id": "s", "message_id": "m1"},
    )
    assert response.status_code == 422


def test_post_message_rejects_empty_text(client):
    response = client.post(
        "/chat/1/message",
        json={"session_id": "s", "message_id": "m1", "text": ""},
    )
    assert response.status_code == 422


def test_post_message_rejects_text_above_4096_chars(client):
    """WhatsApp message limit is 4096; we match it to avoid the bot
    flooding the worker with megabyte payloads from a hostile widget."""
    long_text = "a" * 4097
    response = client.post(
        "/chat/1/message",
        json={
            "session_id": "s",
            "message_id": "m1",
            "text": long_text,
        },
    )
    assert response.status_code == 422


def test_post_message_accepts_text_at_4096_chars(client):
    """Boundary: exactly the max length should pass validation and
    reach the 501 stub (not 422)."""
    response = client.post(
        "/chat/1/message",
        json={
            "session_id": "s",
            "message_id": "m1",
            "text": "a" * 4096,
        },
    )
    assert response.status_code == 501


def test_post_message_rejects_bot_id_zero(client):
    """bot_id is constrained to >= 1 in the path."""
    response = client.post(
        "/chat/0/message",
        json={"session_id": "s", "message_id": "m1", "text": "hi"},
    )
    assert response.status_code == 422
