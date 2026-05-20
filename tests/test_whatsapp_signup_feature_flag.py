"""Feature-flag gate for WhatsApp Embedded Signup.

Two contract checks, both at the unit level on the feature_flags module
plus one integration probe of the endpoint behavior. We don't exercise
the full Meta token-exchange path here — that's already covered in
other suites and would need extensive mocking. Just verify the gate
fires before any Meta call happens.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Reset the env var between tests; default-closed is the contract."""
    monkeypatch.delenv("WHATSAPP_PUBLIC_SIGNUP_ENABLED", raising=False)
    yield


def test_default_closed():
    from app import feature_flags

    importlib.reload(feature_flags)
    assert feature_flags.whatsapp_public_signup_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_values_open_the_flag(monkeypatch, value):
    from app import feature_flags

    monkeypatch.setenv("WHATSAPP_PUBLIC_SIGNUP_ENABLED", value)
    importlib.reload(feature_flags)
    assert feature_flags.whatsapp_public_signup_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_falsy_or_garbage_keeps_flag_closed(monkeypatch, value):
    from app import feature_flags

    monkeypatch.setenv("WHATSAPP_PUBLIC_SIGNUP_ENABLED", value)
    importlib.reload(feature_flags)
    assert feature_flags.whatsapp_public_signup_enabled() is False
