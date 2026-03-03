"""Tests for Step 0.5 — verify formerly-silent error paths now produce log output."""

import logging
from unittest.mock import AsyncMock, patch

import pytest

from app.encryption import decrypt_value
from app.broadcast import broadcast_order_update


class TestEncryptionLogging:
    def test_decrypt_with_invalid_ciphertext_logs_warning(self, caplog):
        """Decrypting a non-Fernet string should log a warning and return plaintext."""
        with caplog.at_level(logging.WARNING, logger="app.encryption"):
            result = decrypt_value("not-a-real-encrypted-value")
        assert result == "not-a-real-encrypted-value"
        assert any("Decryption failed" in r.message for r in caplog.records)

    def test_decrypt_empty_string_no_warning(self, caplog):
        """Empty input should return empty string without logging."""
        with caplog.at_level(logging.WARNING, logger="app.encryption"):
            result = decrypt_value("")
        assert result == ""
        assert not any("Decryption failed" in r.message for r in caplog.records)


class TestBroadcastErrorHandling:
    @pytest.mark.asyncio
    async def test_aclose_called_even_on_publish_error(self):
        """Redis connection must be cleaned up even when publish() raises."""
        mock_redis = AsyncMock()
        mock_redis.publish.side_effect = ConnectionError("Redis down")
        mock_redis.aclose = AsyncMock()

        with patch("app.broadcast.redis") as mock_redis_module:
            mock_redis_module.from_url.return_value = mock_redis
            await broadcast_order_update("test", {"key": "value"}, bot_id=1)

        mock_redis.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aclose_called_on_success(self):
        """Redis connection must be cleaned up on successful publish too."""
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock()
        mock_redis.aclose = AsyncMock()

        with patch("app.broadcast.redis") as mock_redis_module:
            mock_redis_module.from_url.return_value = mock_redis
            await broadcast_order_update("test", {"key": "value"}, bot_id=1)

        mock_redis.aclose.assert_awaited_once()
        mock_redis.publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_broadcast_failure_logs_error(self, caplog):
        """Failed broadcast should log at ERROR level with bot_id context."""
        mock_redis = AsyncMock()
        mock_redis.publish.side_effect = ConnectionError("Redis down")
        mock_redis.aclose = AsyncMock()

        with patch("app.broadcast.redis") as mock_redis_module:
            mock_redis_module.from_url.return_value = mock_redis
            with caplog.at_level(logging.ERROR, logger="app.broadcast"):
                await broadcast_order_update("test", {"key": "value"}, bot_id=42)

        assert any("bot_id=42" in r.message for r in caplog.records)
