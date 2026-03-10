"""Tests for S5: JWT in URL query parameter removed from /stream.

Verifies that the /stream SSE endpoint function signature no longer
accepts a `token` query parameter. Only ticket, cookie, and Bearer header remain.
"""

import inspect


class TestSSETokenQueryRemoved:
    """The /stream endpoint must not accept JWT via `token` query param."""

    def test_stream_function_has_no_token_parameter(self):
        """The stream_events function must not have a 'token' parameter."""
        # Import lazily to avoid fitz dependency
        from unittest.mock import MagicMock

        # Mock fitz before importing main
        import sys

        if "fitz" not in sys.modules:
            sys.modules["fitz"] = MagicMock()

        from app.main import stream_events

        sig = inspect.signature(stream_events)
        param_names = list(sig.parameters.keys())

        assert "token" not in param_names, (
            "S5 violation: 'token' query parameter still present in /stream. "
            "JWT must not be passed via URL query string."
        )

    def test_stream_function_still_has_ticket_parameter(self):
        """The stream_events function must still accept 'ticket' param."""
        import sys
        from unittest.mock import MagicMock

        if "fitz" not in sys.modules:
            sys.modules["fitz"] = MagicMock()

        from app.main import stream_events

        sig = inspect.signature(stream_events)
        param_names = list(sig.parameters.keys())

        assert "ticket" in param_names, (
            "Ticket-based auth must remain available on /stream."
        )

    def test_stream_function_has_request_parameter(self):
        """The stream_events function must still accept 'request' for Bearer header."""
        import sys
        from unittest.mock import MagicMock

        if "fitz" not in sys.modules:
            sys.modules["fitz"] = MagicMock()

        from app.main import stream_events

        sig = inspect.signature(stream_events)
        param_names = list(sig.parameters.keys())

        assert "request" in param_names
