"""Per-channel kill switches (plan/in_browser_bots.md Phase 5.6).

Ops lever: when one channel misbehaves (LLM-cost spike on web, Meta API
outage on WhatsApp, etc.), an operator flips an env var to disable that
channel WITHOUT touching the other.

The env vars are read on every check so a `kubectl set env` style toggle
takes effect on the next request — no process restart needed. The
per-request cost is one os.environ lookup (microseconds), negligible
compared to the DB and Redis round-trips that follow.

Behaviors enforced by callers:

  WEB_CHANNEL_ENABLED=false:
    - POST /chat/{bot_id}/message  → 503 Service Unavailable
    - POST /chat/{bot_id}/session  → 503 Service Unavailable
    - GET  /chat/{bot_id}/stream   → 503 Service Unavailable
    - process_chat_message worker  → no-op (defensive; HTTP layer
      should catch it first, but a job already in flight when the kill
      switch flips must not crash the worker)

  WHATSAPP_CHANNEL_ENABLED=false:
    - POST /webhook (WhatsApp inbound)  → 200 OK, message dropped.
      Returning 200 keeps Meta from retry-storming us; the message is
      lost on purpose during the outage window.
    - GET /webhook (WhatsApp verify)    → unchanged. Meta needs the
      verify endpoint live to keep the webhook registered. If we 503
      this, Meta deregisters us and we need to re-verify after the
      kill switch flips back.
    - process_whatsapp_message worker   → no-op (same defense-in-depth
      as web).
"""

from __future__ import annotations

import os


def is_channel_enabled(channel: str) -> bool:
    """Return True iff the channel is enabled via env var.

    Channel names: 'whatsapp' or 'web'. Env vars:
      WHATSAPP_CHANNEL_ENABLED=true (default)
      WEB_CHANNEL_ENABLED=true (default)

    Falsy values that disable: 'false', '0', 'no', 'off' (case insensitive).
    Anything else (including missing var) is treated as enabled — so the
    safe default is always-on; you have to explicitly disable.
    """
    var = f"{channel.upper()}_CHANNEL_ENABLED"
    value = os.environ.get(var, "true").strip().lower()
    return value not in {"false", "0", "no", "off"}
