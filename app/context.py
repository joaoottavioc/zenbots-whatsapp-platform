"""
Request-scoped context variables for trace correlation.

Every incoming WhatsApp message gets a unique trace_id that propagates
through all log lines, enabling end-to-end tracing across the
webhook -> worker -> LLM -> response pipeline.
"""

from contextvars import ContextVar
from uuid import uuid4

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
current_bot_id: ContextVar[int | None] = ContextVar("current_bot_id", default=None)
current_contact_id: ContextVar[int | None] = ContextVar(
    "current_contact_id", default=None
)
# Plan/in_browser_bots.md Phase 5.4 — which channel is processing the
# current message. Set at the top of process_whatsapp_message /
# process_chat_message; read by monitoring.py when recording UsageEvent
# rows so cost can be attributed per channel.
current_channel: ContextVar[str | None] = ContextVar("current_channel", default=None)


def new_trace_id() -> str:
    """Generate a 12-char hex trace ID, set it in context, and return it."""
    tid = uuid4().hex[:12]
    trace_id_var.set(tid)
    return tid
