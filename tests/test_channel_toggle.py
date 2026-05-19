"""Per-channel kill switches (plan/in_browser_bots.md Phase 5.6).

Verifies the env-var read semantics for is_channel_enabled. The HTTP
endpoints + worker entries that call this are exercised in their own
test suites; this file just pins the helper's contract.
"""

import os
from unittest.mock import patch

import pytest

from app.channel_toggle import is_channel_enabled


def test_defaults_true_when_env_var_missing():
    """Missing env var → enabled. Safe default: always on; you have to
    explicitly disable. Mirror behavior on both channels."""
    with patch.dict(os.environ, {}, clear=False):
        # Remove the keys if they happen to be set in this shell.
        for k in ("WHATSAPP_CHANNEL_ENABLED", "WEB_CHANNEL_ENABLED"):
            os.environ.pop(k, None)
        assert is_channel_enabled("whatsapp") is True
        assert is_channel_enabled("web") is True


@pytest.mark.parametrize(
    "raw_value, expected",
    [
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("anything-else", True),  # default-true posture for unknown values
        ("false", False),
        ("False", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("  false  ", False),  # whitespace tolerated
    ],
)
def test_env_var_truthiness(raw_value, expected):
    with patch.dict(os.environ, {"WEB_CHANNEL_ENABLED": raw_value}, clear=False):
        assert is_channel_enabled("web") is expected


def test_channels_are_independent():
    """Disabling one channel doesn't affect the other — the whole point
    of having two env vars."""
    with patch.dict(
        os.environ,
        {"WHATSAPP_CHANNEL_ENABLED": "false", "WEB_CHANNEL_ENABLED": "true"},
        clear=False,
    ):
        assert is_channel_enabled("whatsapp") is False
        assert is_channel_enabled("web") is True

    with patch.dict(
        os.environ,
        {"WHATSAPP_CHANNEL_ENABLED": "true", "WEB_CHANNEL_ENABLED": "false"},
        clear=False,
    ):
        assert is_channel_enabled("whatsapp") is True
        assert is_channel_enabled("web") is False


def test_lookup_uses_uppercase_channel_name():
    """Helper accepts lowercase channel names but reads UPPERCASE env vars."""
    with patch.dict(os.environ, {"WEB_CHANNEL_ENABLED": "false"}, clear=False):
        assert is_channel_enabled("web") is False
        assert is_channel_enabled("Web") is False
        assert is_channel_enabled("WEB") is False
