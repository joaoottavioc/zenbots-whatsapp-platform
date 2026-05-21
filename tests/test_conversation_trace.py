"""Contract tests for the behind-the-scenes conversation trace.

Covers `crud.get_conversation_trace` shape — the route-level test is
covered separately because the test infrastructure already struggles
with authenticated TestClient routes (see CLAUDE.md / pre-existing
test_chat_routes_contract failures).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import crud
from app.models import ConversationHistory, UsageEvent


def _hist(role: str, content: str, trace_id: str | None, *, hid: int, when: datetime):
    h = ConversationHistory(
        bot_id=1,
        contact_id=2,
        role=role,
        content=content,
        trace_id=trace_id,
    )
    h.id = hid
    h.created_at = when
    return h


def _event(
    trace_id: str, *, model="gpt-4o-mini", input_t=1000, output_t=50, cost=0.001
):
    e = UsageEvent(
        bot_id=1,
        service="openai",
        operation="get_ai_decision",
        model=model,
        input_tokens=input_t,
        output_tokens=output_t,
        cached_tokens=0,
        cost_usd=cost,
        duration_ms=900,
        success=True,
        trace_id=trace_id,
        channel="whatsapp",
    )
    return e


def _patch_session(history_rows: list, event_rows: list) -> MagicMock:
    """Fabricate an AsyncSession whose .execute returns history first, then events.

    crud.get_conversation_trace makes exactly two queries in this order:
    1. ConversationHistory rows for (bot_id, contact_id)
    2. UsageEvent rows for the resulting trace_ids — *unless* no trace_ids
       were found, in which case the second query is skipped.

    The mock returns history then events for each .execute call.
    """
    session = MagicMock()
    session.execute = AsyncMock()

    # The real query is `ORDER BY created_at DESC`; crud reverses the
    # list to get chronological order. Pre-sort the mock rows DESC so
    # the round-trip matches what production sees.
    history_rows_desc = sorted(history_rows, key=lambda r: r.created_at, reverse=True)
    history_result = MagicMock()
    history_scalars = MagicMock()
    history_scalars.all.return_value = history_rows_desc
    history_result.scalars.return_value = history_scalars

    events_result = MagicMock()
    events_scalars = MagicMock()
    events_scalars.all.return_value = event_rows
    events_result.scalars.return_value = events_scalars

    # If no rows have trace_ids, only the history query runs — match
    # both shapes by returning history first, events second, then
    # history again as fallback (the mock library cycles through).
    session.execute.side_effect = [history_result, events_result, history_result]
    return session


@pytest.mark.asyncio
async def test_trace_attaches_to_user_message_only():
    """The trace block belongs on the USER message — it represents the
    work to process that message. The assistant reply that shares the
    same trace_id is the *output* and gets no trace block."""
    t0 = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _hist("user", "quero uma pizza", "tracex001", hid=1, when=t0),
        _hist(
            "assistant",
            "Qual pizza?",
            "tracex001",
            hid=2,
            when=t0 + timedelta(seconds=1),
        ),
    ]
    events = [_event("tracex001")]
    session = _patch_session(rows, events)

    result = await crud.get_conversation_trace(session, bot_id=1, contact_id=2)

    assert len(result["messages"]) == 2
    user_msg, asst_msg = result["messages"]
    assert user_msg["role"] == "user"
    assert user_msg["trace"] is not None
    assert user_msg["trace"]["cost_usd"] == 0.001
    assert user_msg["trace"]["tokens"]["input"] == 1000
    assert user_msg["trace"]["tokens"]["output"] == 50

    assert asst_msg["role"] == "assistant"
    assert asst_msg["trace"] is None  # output, not work


@pytest.mark.asyncio
async def test_trace_aggregates_multiple_events_per_message():
    """A single user message can produce multiple UsageEvent rows (e.g.
    semantic_router miss → LLM intent call + tool dispatch). The trace
    should sum cost + tokens + duration across all of them and expose
    each operation in the `operations` list."""
    t0 = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _hist("user", "pode tirar a calabresa", "tracex002", hid=1, when=t0),
        _hist(
            "assistant", "Pronto!", "tracex002", hid=2, when=t0 + timedelta(seconds=1)
        ),
    ]
    events = [
        _event("tracex002", input_t=2000, output_t=80, cost=0.002),
        _event("tracex002", input_t=300, output_t=20, cost=0.0003),
    ]
    session = _patch_session(rows, events)

    result = await crud.get_conversation_trace(session, bot_id=1, contact_id=2)

    trace = result["messages"][0]["trace"]
    assert trace["tokens"]["input"] == 2300
    assert trace["tokens"]["output"] == 100
    assert trace["cost_usd"] == round(0.002 + 0.0003, 6)
    assert len(trace["operations"]) == 2


@pytest.mark.asyncio
async def test_messages_without_trace_id_render_with_null_trace():
    """Old messages predating the trace_id column return content without
    a trace block. The viewer renders them as plain text — graceful
    degradation, not an error."""
    t0 = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _hist("user", "oi", None, hid=1, when=t0),
        _hist("assistant", "Olá!", None, hid=2, when=t0 + timedelta(seconds=1)),
    ]
    session = _patch_session(rows, [])

    result = await crud.get_conversation_trace(session, bot_id=1, contact_id=2)

    for m in result["messages"]:
        assert m["trace"] is None


@pytest.mark.asyncio
async def test_trace_with_no_matching_events_keeps_trace_null():
    """If the history row has a trace_id but UsageEvent has no rows
    with that trace_id (e.g. monitoring buffer dropped the events or
    the trace was a no-op), the user message gets `trace: null` rather
    than an empty `{tokens: 0, cost: 0}` block. Honest > misleading."""
    t0 = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
    rows = [_hist("user", "oi", "tracex003", hid=1, when=t0)]
    session = _patch_session(rows, [])

    result = await crud.get_conversation_trace(session, bot_id=1, contact_id=2)

    assert result["messages"][0]["trace"] is None


@pytest.mark.asyncio
async def test_totals_aggregates_across_all_traces():
    """The top-level `totals` block sums every event across every trace
    so the frontend can render a 'conversation cost so far' headline
    without doing it client-side."""
    t0 = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _hist("user", "1", "ta", hid=1, when=t0),
        _hist("assistant", "2", "ta", hid=2, when=t0 + timedelta(seconds=1)),
        _hist("user", "3", "tb", hid=3, when=t0 + timedelta(seconds=2)),
        _hist("assistant", "4", "tb", hid=4, when=t0 + timedelta(seconds=3)),
    ]
    events = [
        _event("ta", input_t=1000, output_t=50, cost=0.001),
        _event("tb", input_t=2000, output_t=70, cost=0.002),
    ]
    session = _patch_session(rows, events)

    result = await crud.get_conversation_trace(session, bot_id=1, contact_id=2)
    totals = result["totals"]

    assert totals is not None
    assert totals["tokens"]["input"] == 3000
    assert totals["tokens"]["output"] == 120
    assert totals["cost_usd"] == round(0.001 + 0.002, 6)
