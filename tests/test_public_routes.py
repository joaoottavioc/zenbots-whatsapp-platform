"""Contract tests for app/public_routes.py."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_eval_latest_returns_snapshot_payload(client):
    """The /public/eval/latest endpoint returns the committed snapshot.

    Doesn't pin specific numbers — those drift as the corpus improves.
    Just asserts the schema contract so an accidental schema drift in
    scripts/snapshot_corpus_qa.py is caught by CI."""
    resp = client.get("/public/eval/latest")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Schema contract
    assert data["schema_version"] == "1"
    assert "generated_at" in data
    for key in ("headline", "comprehension", "consistency", "scenarios", "methodology"):
        assert key in data, f"missing top-level key: {key}"

    # Headline shape
    headline = data["headline"]
    for key in ("total_tests", "passed", "failed", "skipped", "pass_rate_pct"):
        assert key in headline, f"headline missing {key}"
    assert isinstance(headline["total_tests"], int)
    assert isinstance(headline["pass_rate_pct"], (int, float))


def test_eval_latest_sets_cache_headers(client):
    """The endpoint sets a short-but-non-trivial Cache-Control header so
    a busy /eval page doesn't re-fetch on every render."""
    resp = client.get("/public/eval/latest")
    cache_control = resp.headers.get("cache-control", "")
    assert "max-age=" in cache_control
    assert "public" in cache_control


def test_eval_latest_503_when_snapshot_missing(client, monkeypatch, tmp_path):
    """If the snapshot file is missing the endpoint returns 503 with a
    structured error, not a 500. Frontend can render an inline 'not
    yet seeded' message."""
    from app import public_routes

    missing = tmp_path / "nope.json"
    monkeypatch.setattr(public_routes, "_SNAPSHOT_PATH", missing)

    resp = client.get("/public/eval/latest")
    assert resp.status_code == 503
    body = resp.json()["detail"]
    assert body["error"] == "eval_snapshot_unavailable"


def test_eval_latest_500_when_snapshot_malformed(client, monkeypatch, tmp_path):
    """A corrupted snapshot is a real bug — 500 surfaces it loudly."""
    from app import public_routes

    bad = tmp_path / "broken.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(public_routes, "_SNAPSHOT_PATH", bad)

    resp = client.get("/public/eval/latest")
    assert resp.status_code == 500
    body = resp.json()["detail"]
    assert body["error"] == "eval_snapshot_invalid"


class _FakeBot:
    def __init__(self, slug):
        self.slug = slug


def _patch_demo_bot(monkeypatch, bot):
    """Stub the slug-independent demo-bot resolver with an async shim."""
    from app import public_routes

    async def _fake_resolve(_session):
        return bot

    monkeypatch.setattr(public_routes, "_resolve_demo_bot", _fake_resolve)


def test_demo_redirect_uses_current_slug(client, monkeypatch):
    """/demo 302s to the frontend widget at the demo bot's *current* slug,
    proving the link survives a dashboard rename."""
    monkeypatch.setenv("FRONTEND_URL", "https://dev.zenbotz.com.br")
    _patch_demo_bot(monkeypatch, _FakeBot("johns-hot-dog"))

    resp = client.get("/demo", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == (
        "https://dev.zenbotz.com.br/widget?slug=johns-hot-dog"
    )
    # Slug can change anytime — the redirect must never be cached.
    assert resp.headers.get("cache-control") == "no-store"


def test_demo_redirect_strips_trailing_slash_on_frontend_url(client, monkeypatch):
    """A trailing slash on FRONTEND_URL must not produce a double slash."""
    monkeypatch.setenv("FRONTEND_URL", "https://dev.zenbotz.com.br/")
    _patch_demo_bot(monkeypatch, _FakeBot("johns-hot-dog"))

    resp = client.get("/demo", follow_redirects=False)
    assert resp.headers["location"] == (
        "https://dev.zenbotz.com.br/widget?slug=johns-hot-dog"
    )


def test_demo_redirect_404_when_not_seeded(client, monkeypatch):
    """Fresh dev env with no demo bot returns a structured 404, not a 500."""
    _patch_demo_bot(monkeypatch, None)

    resp = client.get("/demo", follow_redirects=False)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "demo_bot_unavailable"
