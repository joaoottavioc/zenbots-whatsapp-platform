# tests/test_pii_error_handler.py
"""
Tests for PII-safe error handling in process_whatsapp_message.
Verifies send_whatsapp_message is NOT called when exception fires before
contact_number/current_token/current_phone_id are assigned.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


VALID_WEBHOOK_DATA = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "messages": [
                            {
                                "from": "5511999990000",
                                "text": {"body": "oi"},
                                "id": "wamid.test123",
                            }
                        ],
                        "metadata": {
                            "phone_number_id": "PHONE123",
                            "display_phone_number": "5511999991111",
                        },
                    }
                }
            ]
        }
    ],
}


def _make_bad_data_no_messages():
    """Data that has messages key but will fail on contact extraction."""
    return {"entry": [{"changes": [{"value": {"messages": [{}]}}]}]}


@pytest.mark.asyncio
async def test_no_send_when_variables_not_assigned():
    """When exception fires before contact_number is assigned, no message is sent."""
    bad_data = {"entry": [{"changes": [{"value": {"messages": "not_a_list"}}]}]}

    mock_session = AsyncMock()
    mock_session.is_active = True
    mock_session.rollback = AsyncMock()
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.whatsapp.async_session", return_value=mock_session_ctx),
        patch("app.whatsapp.send_whatsapp_message", new_callable=AsyncMock) as mock_send,
        patch("app.whatsapp.new_trace_id", return_value="aabbccdd"),
        patch("app.whatsapp.record_business_event", new_callable=AsyncMock),
        patch("app.whatsapp.record_error", new_callable=AsyncMock),
    ):
        from app.whatsapp import process_whatsapp_message

        await process_whatsapp_message({}, bad_data)
        mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_send_when_all_variables_assigned():
    """When exception fires after all 3 vars assigned, error message IS sent."""
    mock_session = AsyncMock()
    mock_session.is_active = True
    mock_session.rollback = AsyncMock()
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.whatsapp.async_session", return_value=mock_session_ctx),
        patch("app.whatsapp.send_whatsapp_message", new_callable=AsyncMock) as mock_send,
        patch("app.whatsapp.new_trace_id", return_value="aabbccdd"),
        patch("app.whatsapp.record_business_event", new_callable=AsyncMock),
        patch("app.whatsapp.record_error", new_callable=AsyncMock),
        patch("app.whatsapp.is_spamming", new_callable=AsyncMock, return_value=False),
        patch("app.whatsapp.is_rate_limited", new_callable=AsyncMock, return_value=False),
        patch("app.whatsapp._find_bot", new_callable=AsyncMock) as mock_find_bot,
        patch("app.whatsapp._check_subscription", new_callable=AsyncMock, return_value=False),
        patch("app.whatsapp._handle_dedup", new_callable=AsyncMock, side_effect=RuntimeError("test boom")),
        patch("app.whatsapp.decrypt_value", side_effect=lambda x: x),
        patch("app.whatsapp.mask_phone", side_effect=lambda x: "***"),
    ):
        mock_bot = MagicMock()
        mock_bot.id = 1
        mock_bot.whatsapp_token = "TOKEN123"
        mock_bot.phone_number_id = "PHONE123"
        mock_find_bot.return_value = mock_bot

        from app.whatsapp import process_whatsapp_message

        await process_whatsapp_message({}, VALID_WEBHOOK_DATA)

        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        assert call_kwargs[1]["to"] == "5511999990000"
        assert call_kwargs[1]["token"] == "TOKEN123"
        assert call_kwargs[1]["phone_id"] == "PHONE123"
