"""Tests for app/logging_config.py — structured logging with context injection."""

import logging

from unittest.mock import patch

from app.context import trace_id_var, current_bot_id, current_contact_id, new_trace_id
from app.logging_config import ContextFilter, setup_logging


class TestContextFilter:
    def test_injects_trace_id(self):
        filt = ContextFilter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)

        tid = new_trace_id()
        filt.filter(record)

        assert record.trace_id == tid  # type: ignore[attr-defined]

    def test_injects_bot_id(self):
        filt = ContextFilter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)

        token = current_bot_id.set(7)
        try:
            filt.filter(record)
            assert record.bot_id == 7  # type: ignore[attr-defined]
        finally:
            current_bot_id.reset(token)

    def test_injects_contact_id(self):
        filt = ContextFilter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)

        token = current_contact_id.set(42)
        try:
            filt.filter(record)
            assert record.contact_id == 42  # type: ignore[attr-defined]
        finally:
            current_contact_id.reset(token)

    def test_defaults_when_context_not_set(self):
        filt = ContextFilter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)

        # Reset context vars
        t1 = trace_id_var.set("")
        t2 = current_bot_id.set(None)
        t3 = current_contact_id.set(None)
        try:
            filt.filter(record)
            assert record.trace_id == ""  # type: ignore[attr-defined]
            assert record.bot_id is None  # type: ignore[attr-defined]
            assert record.contact_id is None  # type: ignore[attr-defined]
        finally:
            trace_id_var.reset(t1)
            current_bot_id.reset(t2)
            current_contact_id.reset(t3)

    def test_always_returns_true(self):
        filt = ContextFilter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        assert filt.filter(record) is True


class TestSetupLogging:
    def setup_method(self):
        """Clean up root logger before each test."""
        root = logging.getLogger()
        root.handlers.clear()
        root.filters = [f for f in root.filters if not isinstance(f, ContextFilter)]

    def test_adds_context_filter_to_root(self):
        with patch.dict("os.environ", {"LOG_FORMAT": "json"}, clear=False):
            setup_logging()
        root = logging.getLogger()
        assert any(isinstance(f, ContextFilter) for f in root.filters)

    def test_adds_handler_to_root(self):
        with patch.dict("os.environ", {"LOG_FORMAT": "json"}, clear=False):
            setup_logging()
        root = logging.getLogger()
        assert len(root.handlers) >= 1

    def test_idempotent_multiple_calls(self):
        with patch.dict("os.environ", {"LOG_FORMAT": "json"}, clear=False):
            setup_logging()
            setup_logging()
            setup_logging()
        root = logging.getLogger()
        context_filters = [f for f in root.filters if isinstance(f, ContextFilter)]
        assert len(context_filters) == 1

    def test_text_format_fallback(self):
        with patch.dict("os.environ", {"LOG_FORMAT": "text"}, clear=False):
            setup_logging()
        root = logging.getLogger()
        handler = root.handlers[0]
        # Should NOT be JsonFormatter
        from pythonjsonlogger import jsonlogger

        assert not isinstance(handler.formatter, jsonlogger.JsonFormatter)

    def test_suppresses_noisy_loggers(self):
        with patch.dict("os.environ", {"LOG_FORMAT": "json"}, clear=False):
            setup_logging()
        assert logging.getLogger("httpx").level >= logging.WARNING
        assert logging.getLogger("httpcore").level >= logging.WARNING
        assert logging.getLogger("sqlalchemy.engine").level >= logging.WARNING

    def _capture(self, log_format: str):
        import io

        with patch.dict("os.environ", {"LOG_FORMAT": log_format}, clear=False):
            setup_logging()
        stream = io.StringIO()
        logging.getLogger().handlers[0].stream = stream
        return stream

    def test_child_logger_records_carry_context_json(self):
        """Regression: the filter sat only on the root *logger*, which Python
        does not consult for records propagated from child loggers — so every
        `logging.getLogger(__name__)` line shipped trace_id/bot_id = null."""
        import json

        stream = self._capture("json")
        t1 = trace_id_var.set("abc123abc123")
        t2 = current_bot_id.set(42)
        try:
            logging.getLogger("app.whatsapp").warning("child line")
        finally:
            trace_id_var.reset(t1)
            current_bot_id.reset(t2)

        record = json.loads(stream.getvalue().strip().splitlines()[-1])
        assert record["name"] == "app.whatsapp"
        assert record["trace_id"] == "abc123abc123"
        assert record["bot_id"] == 42

    def test_child_logger_text_format_does_not_error(self):
        """In text mode the missing attribute raised a formatting error and
        the line was lost."""
        stream = self._capture("text")
        t1 = trace_id_var.set("feedfeedfeed")
        try:
            with patch("logging.Handler.handleError") as handle_error:
                logging.getLogger("app.worker").warning("child line")
        finally:
            trace_id_var.reset(t1)

        handle_error.assert_not_called()
        assert "trace=feedfeedfeed" in stream.getvalue()

    def teardown_method(self):
        """Restore root logger to avoid polluting other tests."""
        root = logging.getLogger()
        root.handlers.clear()
        root.filters = [f for f in root.filters if not isinstance(f, ContextFilter)]
        root.setLevel(logging.WARNING)
