# tests/test_error_recovery.py
"""
Tests for _classify_error_message — ensures specific error types
produce user-friendly messages instead of a single generic one.
"""

import httpx

from app.whatsapp import _classify_error_message


class TestClassifyErrorMessage:
    def test_timeout_error_returns_connection_message(self):
        """httpx.TimeoutException → connection difficulty message."""
        exc = httpx.TimeoutException("read timed out")
        msg = _classify_error_message(exc)
        assert "conexão" in msg.lower()

    def test_connect_error_returns_connection_message(self):
        """httpx.ConnectError → connection difficulty message."""
        exc = httpx.ConnectError("connection refused")
        msg = _classify_error_message(exc)
        assert "conexão" in msg.lower()

    def test_builtin_timeout_returns_connection_message(self):
        """Python TimeoutError → connection difficulty message."""
        exc = TimeoutError("timed out")
        msg = _classify_error_message(exc)
        assert "conexão" in msg.lower()

    def test_connection_error_returns_connection_message(self):
        """ConnectionError → connection difficulty message."""
        exc = ConnectionError("connection reset")
        msg = _classify_error_message(exc)
        assert "conexão" in msg.lower()

    def test_database_error_returns_system_message(self):
        """Exception with 'database' in message → system unavailable message."""
        exc = Exception("database connection pool exhausted")
        msg = _classify_error_message(exc)
        assert "sistema" in msg.lower()

    def test_sqlalchemy_error_returns_system_message(self):
        """Exception whose type name contains 'sqlalchemy' → system unavailable message."""

        # Simulate a SQLAlchemy-like exception
        class SQLAlchemyOperationalError(Exception):
            pass

        exc = SQLAlchemyOperationalError("connection refused")
        msg = _classify_error_message(exc)
        assert "sistema" in msg.lower()

    def test_openai_error_returns_assistant_message(self):
        """Exception whose type name contains 'openai' → assistant difficulty message."""

        class OpenAIAPIError(Exception):
            pass

        exc = OpenAIAPIError("rate limit exceeded")
        msg = _classify_error_message(exc)
        assert "assistente" in msg.lower()

    def test_rate_limit_error_returns_assistant_message(self):
        """Exception with 'rate_limit' in message → assistant difficulty message."""
        exc = Exception("rate_limit exceeded for model")
        msg = _classify_error_message(exc)
        assert "assistente" in msg.lower()

    def test_payment_error_returns_payment_message(self):
        """Exception with 'mercadopago' in message → payment error message."""
        exc = Exception("mercadopago API returned 500")
        msg = _classify_error_message(exc)
        assert "pagamento" in msg.lower()

    def test_pix_error_returns_payment_message(self):
        """Exception with 'pix' in message → payment error message."""
        exc = Exception("PIX creation failed")
        msg = _classify_error_message(exc)
        assert "pagamento" in msg.lower()

    def test_generic_error_returns_fallback(self):
        """Unknown exception type → generic fallback message."""
        exc = ValueError("something unexpected")
        msg = _classify_error_message(exc)
        assert "erro" in msg.lower()
        assert "tente novamente" in msg.lower()

    def test_all_messages_are_in_portuguese(self):
        """All returned messages should be in Portuguese (contain common PT words)."""
        test_cases = [
            httpx.TimeoutException("t"),
            Exception("database error"),
            Exception("mercadopago"),
            ValueError("generic"),
        ]
        for exc in test_cases:
            msg = _classify_error_message(exc)
            assert any(
                w in msg.lower()
                for w in ["por favor", "tente", "no momento", "instantes"]
            ), (
                f"Message for {type(exc).__name__} doesn't seem to be in Portuguese: {msg}"
            )
