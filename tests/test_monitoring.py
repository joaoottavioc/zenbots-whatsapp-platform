"""Tests for app/monitoring.py — core instrumentation module."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.monitoring import (
    PRICING,
    _event_buffer,
    _buffer_lock,
    _FLUSH_THRESHOLD,
    _BUFFER_HARD_CAP,
    record_llm_usage,
    record_api_usage,
    flush_buffer,
    _update_redis_counters,
    get_buffer_size,
)
from app.models import UsageEvent


@pytest.fixture(autouse=True)
async def clear_buffer():
    """Ensure buffer is empty before and after each test."""
    async with _buffer_lock:
        _event_buffer.clear()
    yield
    async with _buffer_lock:
        _event_buffer.clear()


class TestRecordLlmUsage:
    @pytest.mark.asyncio
    async def test_calculates_correct_cost_gpt4o_mini(self):
        """Cost should be (input * input_price) + (output * output_price)."""
        usage = MagicMock()
        usage.prompt_tokens = 1_000_000
        usage.completion_tokens = 1_000_000
        usage.cached_tokens = 0

        with patch("app.monitoring._update_redis_counters", new_callable=AsyncMock):
            await record_llm_usage(
                bot_id=1,
                operation="get_ai_decision",
                model="gpt-4o-mini",
                usage=usage,
                duration_ms=350,
            )

        assert len(_event_buffer) == 1
        event = _event_buffer[0]
        expected_cost = (
            1_000_000 * PRICING["gpt-4o-mini"]["input"]
            + 1_000_000 * PRICING["gpt-4o-mini"]["output"]
        )
        assert abs(event.cost_usd - expected_cost) < 0.0001

    @pytest.mark.asyncio
    async def test_calculates_correct_cost_gpt4o(self):
        usage = MagicMock()
        usage.prompt_tokens = 1000
        usage.completion_tokens = 500
        usage.cached_tokens = 0

        with patch("app.monitoring._update_redis_counters", new_callable=AsyncMock):
            await record_llm_usage(
                bot_id=1,
                operation="get_extraction_response",
                model="gpt-4o",
                usage=usage,
                duration_ms=800,
            )

        event = _event_buffer[0]
        expected = 1000 * PRICING["gpt-4o"]["input"] + 500 * PRICING["gpt-4o"]["output"]
        assert abs(event.cost_usd - expected) < 0.0001

    @pytest.mark.asyncio
    async def test_failed_call_records_zero_cost(self):
        usage = MagicMock()
        usage.prompt_tokens = 100
        usage.completion_tokens = 0
        usage.cached_tokens = 0

        with patch("app.monitoring._update_redis_counters", new_callable=AsyncMock):
            await record_llm_usage(
                bot_id=1,
                operation="get_ai_decision",
                model="gpt-4o-mini",
                usage=usage,
                duration_ms=5000,
                success=False,
            )

        event = _event_buffer[0]
        assert event.success is False
        assert event.cost_usd == 0.0

    @pytest.mark.asyncio
    async def test_records_token_counts(self):
        usage = MagicMock()
        usage.prompt_tokens = 6500
        usage.completion_tokens = 150
        usage.cached_tokens = 0

        with patch("app.monitoring._update_redis_counters", new_callable=AsyncMock):
            await record_llm_usage(
                bot_id=1,
                operation="test",
                model="gpt-4o-mini",
                usage=usage,
                duration_ms=100,
            )

        event = _event_buffer[0]
        assert event.input_tokens == 6500
        assert event.output_tokens == 150

    @pytest.mark.asyncio
    async def test_none_usage_defaults_to_zero(self):
        with patch("app.monitoring._update_redis_counters", new_callable=AsyncMock):
            await record_llm_usage(
                bot_id=1,
                operation="test",
                model="gpt-4o-mini",
                usage=None,
                duration_ms=100,
                success=False,
            )

        event = _event_buffer[0]
        assert event.input_tokens == 0
        assert event.output_tokens == 0


class TestRecordApiUsage:
    @pytest.mark.asyncio
    async def test_records_api_usage(self):
        with patch("app.monitoring._update_redis_counters", new_callable=AsyncMock):
            await record_api_usage(
                bot_id=1,
                service="google_maps",
                operation="geocode",
                cost_usd=0.005,
                duration_ms=200,
            )

        event = _event_buffer[0]
        assert event.service == "google_maps"
        assert event.operation == "geocode"
        assert event.cost_usd == 0.005
        assert event.model is None


class TestBufferManagement:
    @pytest.mark.asyncio
    async def test_buffer_flushes_at_threshold(self):
        """Buffer should trigger flush when reaching _FLUSH_THRESHOLD events."""
        mock_session = AsyncMock()
        mock_session.add_all = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("app.monitoring._update_redis_counters", new_callable=AsyncMock),
            patch("app.database.async_session", return_value=mock_session_ctx),
        ):
            for i in range(_FLUSH_THRESHOLD):
                await record_api_usage(
                    bot_id=1,
                    service="test",
                    operation=f"op_{i}",
                    duration_ms=1,
                )

        # After flush, buffer should be empty
        assert get_buffer_size() == 0
        mock_session.add_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_buffer_hard_cap_drops_oldest(self):
        """Buffer should not grow beyond _BUFFER_HARD_CAP."""
        with (
            patch("app.monitoring._update_redis_counters", new_callable=AsyncMock),
            patch("app.monitoring.flush_buffer", new_callable=AsyncMock),
        ):
            # Override _FLUSH_THRESHOLD temporarily by patching flush_buffer
            for i in range(_BUFFER_HARD_CAP + 10):
                event = UsageEvent(
                    bot_id=1,
                    service="test",
                    operation=f"op_{i}",
                    created_at=datetime.now(timezone.utc),
                )
                async with _buffer_lock:
                    if len(_event_buffer) >= _BUFFER_HARD_CAP:
                        del _event_buffer[:1]
                    _event_buffer.append(event)

        assert len(_event_buffer) <= _BUFFER_HARD_CAP


class TestFlushBuffer:
    @pytest.mark.asyncio
    async def test_flush_empty_buffer_returns_zero(self):
        result = await flush_buffer()
        assert result == 0

    @pytest.mark.asyncio
    async def test_flush_inserts_into_db(self):
        """flush_buffer should bulk insert buffered events."""
        # Populate buffer directly
        async with _buffer_lock:
            for i in range(5):
                _event_buffer.append(
                    UsageEvent(
                        bot_id=1,
                        service="test",
                        operation=f"op_{i}",
                        created_at=datetime.now(timezone.utc),
                    )
                )

        mock_session = AsyncMock()
        mock_session.add_all = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("app.database.async_session", return_value=mock_session_ctx):
            result = await flush_buffer()

        assert result == 5
        mock_session.add_all.assert_called_once()
        assert len(mock_session.add_all.call_args[0][0]) == 5
        assert get_buffer_size() == 0


class TestRedisCounters:
    @pytest.mark.asyncio
    async def test_update_redis_counters_calls_pipeline(self):
        """Should call INCR, INCRBYFLOAT, and ZINCRBY via pipeline."""
        mock_pipe = MagicMock()
        mock_pipe.incr = MagicMock(return_value=mock_pipe)
        mock_pipe.incrbyfloat = MagicMock(return_value=mock_pipe)
        mock_pipe.expire = MagicMock(return_value=mock_pipe)
        mock_pipe.zincrby = MagicMock(return_value=mock_pipe)
        mock_pipe.execute = AsyncMock(return_value=[])

        mock_redis = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        event = UsageEvent(
            bot_id=5,
            service="openai",
            operation="get_ai_decision",
            cost_usd=0.001,
            input_tokens=6500,
            output_tokens=150,
            created_at=datetime.now(timezone.utc),
        )

        async def mock_get_redis():
            return mock_redis

        with patch("app.monitoring._get_redis_monitor", side_effect=mock_get_redis):
            await _update_redis_counters(event)

        mock_pipe.incr.assert_called()
        mock_pipe.incrbyfloat.assert_called()
        mock_pipe.zincrby.assert_called()
        mock_pipe.execute.assert_awaited_once()
