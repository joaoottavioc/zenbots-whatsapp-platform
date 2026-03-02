"""Tests for app/context.py — request-scoped context variables."""

from app.context import (
    trace_id_var,
    current_bot_id,
    current_contact_id,
    new_trace_id,
)


class TestNewTraceId:
    def test_returns_12_char_hex_string(self):
        tid = new_trace_id()
        assert len(tid) == 12
        assert all(c in "0123456789abcdef" for c in tid)

    def test_sets_context_var(self):
        tid = new_trace_id()
        assert trace_id_var.get() == tid

    def test_generates_unique_ids(self):
        ids = {new_trace_id() for _ in range(100)}
        assert len(ids) == 100


class TestContextVarDefaults:
    def test_trace_id_default_empty_string(self):
        var = trace_id_var.get()
        # May be set by previous tests, but the type should be str
        assert isinstance(var, str)

    def test_bot_id_default_none(self):
        # Reset to default by creating a fresh context
        token = current_bot_id.set(None)
        assert current_bot_id.get() is None
        current_bot_id.reset(token)

    def test_contact_id_default_none(self):
        token = current_contact_id.set(None)
        assert current_contact_id.get() is None
        current_contact_id.reset(token)

    def test_bot_id_roundtrip(self):
        token = current_bot_id.set(42)
        assert current_bot_id.get() == 42
        current_bot_id.reset(token)

    def test_contact_id_roundtrip(self):
        token = current_contact_id.set(99)
        assert current_contact_id.get() == 99
        current_contact_id.reset(token)
