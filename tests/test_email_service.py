# tests/test_email_service.py
"""
Tests for app/email_service.py:
- FRONTEND_URL env var is used in reset link
- Default fallback to localhost:3000
- Exception from send_message is logged and re-raised
"""

from unittest.mock import AsyncMock, patch, MagicMock
import pytest
import os


@pytest.mark.asyncio
class TestSendPasswordResetEmail:
    async def test_uses_frontend_url_env_var(self):
        """When FRONTEND_URL is set, the reset link uses it."""
        mock_fm = AsyncMock()
        mock_fm.send_message = AsyncMock()

        with patch.dict(os.environ, {"FRONTEND_URL": "https://app.zenbotz.com.br"}):
            with patch("app.email_service.FastMail", return_value=mock_fm):
                from app.email_service import send_password_reset_email

                await send_password_reset_email("user@test.com", "abc123")

        call_args = mock_fm.send_message.call_args
        message = call_args[0][0]
        assert "https://app.zenbotz.com.br/redefinir-senha?token=abc123" in message.body

    async def test_defaults_to_localhost(self):
        """When FRONTEND_URL is not set, falls back to http://localhost:3000."""
        mock_fm = AsyncMock()
        mock_fm.send_message = AsyncMock()

        # Remove FRONTEND_URL if present
        env_copy = {k: v for k, v in os.environ.items() if k != "FRONTEND_URL"}
        with patch.dict(os.environ, env_copy, clear=True):
            with patch("app.email_service.FastMail", return_value=mock_fm):
                from app.email_service import send_password_reset_email

                await send_password_reset_email("user@test.com", "abc123")

        call_args = mock_fm.send_message.call_args
        message = call_args[0][0]
        assert "http://localhost:3000/redefinir-senha?token=abc123" in message.body

    async def test_exception_logged_and_reraised(self):
        """When send_message raises, it is logged and re-raised."""
        mock_fm = AsyncMock()
        mock_fm.send_message = AsyncMock(side_effect=ConnectionError("SMTP down"))

        with patch("app.email_service.FastMail", return_value=mock_fm), patch(
            "app.email_service.logger"
        ) as mock_logger:
            from app.email_service import send_password_reset_email

            with pytest.raises(ConnectionError, match="SMTP down"):
                await send_password_reset_email("user@test.com", "token123")

            mock_logger.exception.assert_called_once()


class TestValidateEmailConfig:
    """Tests for validate_email_config() production guard."""

    def test_dev_environment_passes(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "development", "MAIL_SERVER": "mailhog"}):
            from app.email_service import validate_email_config

            validate_email_config()  # Should not raise

    def test_test_environment_passes(self):
        env = {k: v for k, v in os.environ.items()}
        env.pop("ENVIRONMENT", None)
        env.pop("MAIL_SERVER", None)
        with patch.dict(os.environ, env, clear=True):
            from app.email_service import validate_email_config

            validate_email_config()  # Defaults to development — should not raise

    def test_production_with_mailhog_raises(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "production", "MAIL_SERVER": "mailhog"}):
            from app.email_service import validate_email_config

            with pytest.raises(RuntimeError, match="MAIL_SERVER"):
                validate_email_config()

    def test_production_with_real_server_passes(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "production", "MAIL_SERVER": "smtp.gmail.com"}):
            from app.email_service import validate_email_config

            validate_email_config()  # Should not raise
