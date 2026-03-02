"""Tests for UsageEvent and DailyCostSummary models."""

from datetime import date, datetime, timezone

from app.models import UsageEvent, DailyCostSummary


class TestUsageEvent:
    def test_create_with_required_fields(self):
        event = UsageEvent(
            bot_id=1,
            service="openai",
            operation="get_ai_decision",
        )
        assert event.bot_id == 1
        assert event.service == "openai"
        assert event.operation == "get_ai_decision"

    def test_default_values(self):
        event = UsageEvent(
            bot_id=1,
            service="openai",
            operation="get_ai_decision",
        )
        assert event.model is None
        assert event.input_tokens == 0
        assert event.output_tokens == 0
        assert event.cached_tokens == 0
        assert event.cost_usd == 0.0
        assert event.quantity == 1
        assert event.duration_ms == 0
        assert event.success is True
        assert event.contact_id is None
        assert event.trace_id is None

    def test_create_with_all_fields(self):
        event = UsageEvent(
            bot_id=5,
            service="openai",
            operation="get_ai_decision",
            model="gpt-4o-mini",
            input_tokens=6500,
            output_tokens=150,
            cached_tokens=0,
            cost_usd=0.001065,
            quantity=1,
            duration_ms=350,
            success=True,
            contact_id=42,
            trace_id="abc123def456",
        )
        assert event.model == "gpt-4o-mini"
        assert event.input_tokens == 6500
        assert event.output_tokens == 150
        assert event.cost_usd == 0.001065
        assert event.duration_ms == 350
        assert event.contact_id == 42
        assert event.trace_id == "abc123def456"

    def test_failed_event(self):
        event = UsageEvent(
            bot_id=1,
            service="openai",
            operation="get_ai_decision",
            success=False,
            cost_usd=0.0,
            duration_ms=5000,
        )
        assert event.success is False
        assert event.cost_usd == 0.0

    def test_non_llm_service(self):
        event = UsageEvent(
            bot_id=1,
            service="google_maps",
            operation="geocode",
            model=None,
            cost_usd=0.005,
            duration_ms=200,
        )
        assert event.service == "google_maps"
        assert event.model is None

    def test_table_name(self):
        assert UsageEvent.__tablename__ == "usage_events"


class TestDailyCostSummary:
    def test_create_with_required_fields(self):
        summary = DailyCostSummary(
            bot_id=1,
            date=date(2026, 3, 1),
            service="openai",
        )
        assert summary.bot_id == 1
        assert summary.date == date(2026, 3, 1)
        assert summary.service == "openai"

    def test_default_values(self):
        summary = DailyCostSummary(
            bot_id=1,
            date=date(2026, 3, 1),
            service="openai",
        )
        assert summary.total_cost_usd == 0.0
        assert summary.total_input_tokens == 0
        assert summary.total_output_tokens == 0
        assert summary.total_api_calls == 0
        assert summary.total_failed_calls == 0
        assert summary.total_duration_ms == 0
        assert summary.avg_cost_per_call == 0.0
        assert summary.max_cost_single_call == 0.0

    def test_create_with_aggregated_data(self):
        summary = DailyCostSummary(
            bot_id=5,
            date=date(2026, 3, 1),
            service="openai",
            total_cost_usd=15.40,
            total_input_tokens=1_000_000,
            total_output_tokens=50_000,
            total_api_calls=8200,
            total_failed_calls=12,
            total_duration_ms=3_600_000,
            avg_cost_per_call=0.001878,
            max_cost_single_call=0.05,
        )
        assert summary.total_cost_usd == 15.40
        assert summary.total_api_calls == 8200
        assert summary.total_failed_calls == 12

    def test_table_name(self):
        assert DailyCostSummary.__tablename__ == "daily_cost_summary"
