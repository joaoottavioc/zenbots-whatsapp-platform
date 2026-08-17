"""Bot channel matrix defaults (plan/in_browser_bots.md Phase 2.5).

Pins the per-bot channel configuration shape:
  - whatsapp_enabled defaults true (every existing bot was WhatsApp-only)
  - web_widget_enabled defaults true (every bot ships with the widget on;
    owner can turn it off via dashboard)
  - web_widget_allowed_origins defaults [] (block all browser embeds)
  - web_widget_theme defaults {} (no customization)
  - web_widget_offline_message defaults None

The application enforces the channel matrix in Phase 2 ingress; these
tests just guarantee the column defaults match the migration so a future
seed/migration change doesn't accidentally flip a default and silently
re-shape every existing bot.
"""

from app.models import Bot


def test_new_bot_defaults_to_both_channels():
    """The default Bot config has WhatsApp on (preserves pre-Phase-2
    behavior of every existing row) and the web widget on out of the box."""
    bot = Bot(user_id=1, whatsapp_number="5511000000001")
    assert bot.whatsapp_enabled is True
    assert bot.web_widget_enabled is True


def test_new_bot_web_widget_config_defaults():
    bot = Bot(user_id=1, whatsapp_number="5511000000002")
    assert bot.web_widget_allowed_origins == []
    assert bot.web_widget_theme == {}
    assert bot.web_widget_offline_message is None


def test_bot_can_be_web_only():
    """Web-only config: whatsapp_enabled=False, web_widget_enabled=True.
    No phone_number_id or whatsapp_token required at the ORM level —
    the gate at ingress is what blocks WhatsApp messages from reaching
    this bot."""
    bot = Bot(
        user_id=1,
        whatsapp_number="5511000000003",
        whatsapp_enabled=False,
        web_widget_enabled=True,
        web_widget_allowed_origins=["https://restaurante.com.br"],
    )
    assert bot.whatsapp_enabled is False
    assert bot.web_widget_enabled is True
    assert bot.web_widget_allowed_origins == ["https://restaurante.com.br"]


def test_bot_can_have_both_channels():
    bot = Bot(
        user_id=1,
        whatsapp_number="5511000000004",
        whatsapp_enabled=True,
        web_widget_enabled=True,
    )
    assert bot.whatsapp_enabled is True
    assert bot.web_widget_enabled is True


def test_bot_can_be_in_draft_state():
    """Both channels disabled — staged onboarding before going live.
    No CHECK constraint blocks this; the application interprets it as
    'accept no messages from either channel'."""
    bot = Bot(
        user_id=1,
        whatsapp_number="5511000000005",
        whatsapp_enabled=False,
        web_widget_enabled=False,
    )
    assert bot.whatsapp_enabled is False
    assert bot.web_widget_enabled is False


def test_web_widget_theme_accepts_dict():
    bot = Bot(
        user_id=1,
        whatsapp_number="5511000000006",
        web_widget_theme={
            "primary_color": "#00B14F",
            "position": "br",
            "welcome_message": "Olá! Como posso ajudar?",
        },
    )
    assert bot.web_widget_theme["primary_color"] == "#00B14F"
    assert bot.web_widget_theme["position"] == "br"
