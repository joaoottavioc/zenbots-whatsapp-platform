# tests/test_safe_int_coercion.py
"""Tests for the _safe_int helper in app/whatsapp.py."""

import logging

from app.whatsapp import _safe_int


class TestSafeInt:
    def test_valid_int(self):
        assert _safe_int(42, "test") == 42
        assert _safe_int("7", "test") == 7
        assert _safe_int(0, "test") == 0

    def test_invalid_returns_none_and_logs(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.whatsapp"):
            result = _safe_int("abc", "product_id")
        assert result is None
        assert "Invalid product_id" in caplog.text
        assert "'abc'" in caplog.text

    def test_none_returns_none(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.whatsapp"):
            result = _safe_int(None, "value")
        assert result is None
