"""Runtime feature flags for product-level gates.

These are *signup-flow* and *UI-level* gates, distinct from the
`channel_toggle` module which deals with per-channel kill switches at
the message-processing layer. The split is deliberate:

  - `channel_toggle.is_channel_enabled("whatsapp")` controls whether
    inbound WhatsApp webhooks are even processed.
  - `whatsapp_public_signup_enabled()` here controls whether the
    Embedded Signup flow accepts new connections from the UI.

A real restaurant already connected via WhatsApp keeps working when
`whatsapp_public_signup_enabled() == False` — only the
`POST /bots/whatsapp/complete-onboarding` endpoint refuses new linkings.
That separation is what lets us defer WhatsApp signup pending Meta App
Review without breaking restaurants that connected during the dev-mode
window.

All flags default to *closed* (the safer state) so a fresh deploy
without env vars set behaves like a production-cautious deploy. Override
in SSM / .env when ready to flip.
"""

from __future__ import annotations

import os


def _truthy(raw: str | None) -> bool:
    """Standard truthy parsing for env-var flags.

    Accepts: '1', 'true', 'yes', 'on' (case-insensitive). Everything
    else — including None, empty string, and the literal string 'false'
    — reads as False. Keeps misconfigured envs in the safe state.
    """
    if not raw:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def whatsapp_public_signup_enabled() -> bool:
    """Gates POST /bots/whatsapp/complete-onboarding.

    When False (default), the Embedded Signup endpoint returns 503 with
    a structured error the frontend can render as "Em breve". The
    underlying WhatsApp message-handling pipeline is unaffected — bots
    already connected continue to receive/send messages normally.

    Flip to True via SSM Parameter Store / .env on the day Meta App
    Review approves the app for production use.

    Env var: `WHATSAPP_PUBLIC_SIGNUP_ENABLED`. Defaults False.
    """
    return _truthy(os.getenv("WHATSAPP_PUBLIC_SIGNUP_ENABLED"))
