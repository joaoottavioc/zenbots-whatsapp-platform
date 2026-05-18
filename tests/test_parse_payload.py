"""Channel-neutral ingress parsers (plan/in_browser_bots.md Phase 1.3).

Pins the contract that `_parse_whatsapp_payload` produces from a real Meta
Cloud API webhook payload, and confirms `_parse_web_payload` fails loudly
until Phase 2 implements it.

This is the seam that lets the same downstream pipeline serve both
WhatsApp and the future `/chat` endpoint — once Phase 2 fills in
_parse_web_payload, the rest of process_whatsapp_message can be hoisted
into a shared `_run_message_pipeline(ctx, parsed, channel)` without
touching channel-specific envelope handling.
"""

import pytest

from app.whatsapp import (
    ParsedIngress,
    _parse_web_payload,
    _parse_whatsapp_payload,
)


def _wa_payload(messages: list, **value_overrides) -> dict:
    """Build a minimal Meta Cloud API webhook payload shape."""
    value = {
        "metadata": {
            "phone_number_id": "881274981741368",
            "display_phone_number": "+15551312070",
        },
        "messages": messages,
    }
    value.update(value_overrides)
    return {
        "entry": [{"changes": [{"value": value}]}],
    }


def test_parses_text_message():
    payload = _wa_payload(
        [
            {
                "from": "5511999999999",
                "id": "wamid.abc",
                "type": "text",
                "text": {"body": "quero uma coca"},
            }
        ]
    )
    parsed = _parse_whatsapp_payload(payload)
    assert isinstance(parsed, ParsedIngress)
    assert parsed.contact_identity == "5511999999999"
    assert parsed.message_id == "wamid.abc"
    assert parsed.text_body == "quero uma coca"
    assert parsed.msg_type == "text"
    assert parsed.audio_media_id is None
    assert parsed.incoming_phone_id == "881274981741368"
    assert parsed.bot_display_phone == "+15551312070"
    # Web-specific fields stay None.
    assert parsed.bot_id is None
    assert parsed.session_id is None


def test_parses_audio_message():
    """For audio, text_body starts empty and audio_media_id is populated;
    downstream transcription replaces text_body with the Whisper output."""
    payload = _wa_payload(
        [
            {
                "from": "5511999999999",
                "id": "wamid.audio1",
                "type": "audio",
                "audio": {"id": "media-id-123"},
            }
        ]
    )
    parsed = _parse_whatsapp_payload(payload)
    assert parsed.msg_type == "audio"
    assert parsed.audio_media_id == "media-id-123"
    assert parsed.text_body == ""


def test_parses_voice_message_same_as_audio():
    """Meta sometimes uses 'voice' instead of 'audio' for the same shape."""
    payload = _wa_payload(
        [
            {
                "from": "5511999999999",
                "id": "wamid.voice1",
                "type": "voice",
                "voice": {"id": "voice-media-id"},
            }
        ]
    )
    parsed = _parse_whatsapp_payload(payload)
    assert parsed.audio_media_id == "voice-media-id"
    assert parsed.text_body == ""


def test_status_update_returns_none():
    """Non-message events (delivery receipts, read receipts) must return
    None so the caller can drop them without entering the pipeline."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "phone_number_id": "x",
                                "display_phone_number": "y",
                            },
                            "statuses": [
                                {
                                    "id": "wamid.delivered",
                                    "status": "delivered",
                                    "recipient_id": "5511999999999",
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    assert _parse_whatsapp_payload(payload) is None


def test_text_message_with_missing_text_body_returns_empty_string():
    """Defensive: malformed text messages (text key missing) shouldn't
    explode the parser — return empty text_body and let downstream
    handlers respond with a generic 'didn't understand' message."""
    payload = _wa_payload([{"from": "5511999999999", "id": "wamid.x", "type": "text"}])
    parsed = _parse_whatsapp_payload(payload)
    assert parsed is not None
    assert parsed.text_body == ""


def test_web_payload_raises_until_phase2():
    with pytest.raises(NotImplementedError, match="Phase 2"):
        _parse_web_payload(
            {
                "bot_id": 1,
                "session_id": "abc",
                "message_id": "msg-1",
                "text": "ignored",
            }
        )
