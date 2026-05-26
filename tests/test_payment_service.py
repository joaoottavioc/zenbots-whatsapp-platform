import os
import re
import pytest
from unittest.mock import patch, MagicMock

from app.payment_service import create_pix_payment

# RFC-pragmatic local-part check: no ":", whitespace, or empty local part.
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._-]+@zenbotz\.com\.br$")


@pytest.mark.asyncio
@patch.dict(os.environ, {"BASE_URL": "https://test.com"})
async def test_empty_token_returns_none():
    with patch("mercadopago.SDK") as mock_sdk_class:
        result = await create_pix_payment(
            order_id=1,
            total_amount=50.0,
            bot_name="TestBot",
            contact_phone="5511999999999",
            access_token_cliente="",
        )
        assert result is None
        mock_sdk_class.assert_not_called()


@pytest.mark.asyncio
@patch.dict(os.environ, {"BASE_URL": "https://test.com"})
async def test_successful_pix_creation():
    mock_pix_data = {
        "qr_code_base64": "base64encodedstring==",
        "ticket_url": "https://mercadopago.com/pix/ticket/123",
        "qr_code": "00020101021226870014br.gov.bcb.pix",
    }
    mock_response = {
        "status": 201,
        "response": {"point_of_interaction": {"transaction_data": mock_pix_data}},
    }

    mock_payment_instance = MagicMock()
    mock_payment_instance.create.return_value = mock_response

    mock_sdk_instance = MagicMock()
    mock_sdk_instance.payment.return_value = mock_payment_instance

    with (
        patch("mercadopago.SDK", return_value=mock_sdk_instance),
        patch("mercadopago.config.RequestOptions", return_value=MagicMock()),
    ):
        result = await create_pix_payment(
            order_id=42,
            total_amount=99.90,
            bot_name="TestBot",
            contact_phone="5511999999999",
            access_token_cliente="valid_access_token",
        )

    assert result is not None
    assert result["qr_code_base64"] == "base64encodedstring=="
    assert result["qr_code_url"] == "https://mercadopago.com/pix/ticket/123"
    assert result["pix_copy_paste"] == "00020101021226870014br.gov.bcb.pix"


@pytest.mark.asyncio
@patch.dict(os.environ, {"BASE_URL": "https://test.com"})
@pytest.mark.parametrize(
    "contact_phone",
    [
        "web:816101a5-d46b-4849-af34-b38924a02ac5",  # web widget identity key
        "+55 11 99999-9999",  # formatted phone with spaces/+/-
        "",  # empty → falls back to order id
        ":::",  # all stripped → falls back to order id
    ],
)
async def test_payer_email_is_always_valid(contact_phone):
    """MP rejects an invalid payer.email with a 400. Web contacts carry a
    synthesized "web:{uuid}" phone, so the email local-part must be
    sanitized regardless of channel (regression for the web-widget PIX bug).
    """
    mock_pix_data = {
        "qr_code_base64": "x==",
        "ticket_url": "https://mp/t",
        "qr_code": "pixcode",
    }
    mock_payment_instance = MagicMock()
    mock_payment_instance.create.return_value = {
        "status": 201,
        "response": {"point_of_interaction": {"transaction_data": mock_pix_data}},
    }
    mock_sdk_instance = MagicMock()
    mock_sdk_instance.payment.return_value = mock_payment_instance

    with (
        patch("mercadopago.SDK", return_value=mock_sdk_instance),
        patch("mercadopago.config.RequestOptions", return_value=MagicMock()),
    ):
        result = await create_pix_payment(
            order_id=99,
            total_amount=50.0,
            bot_name="TestBot",
            contact_phone=contact_phone,
            access_token_cliente="valid_access_token",
        )

    assert result is not None
    sent_payment_data = mock_payment_instance.create.call_args[0][0]
    payer_email = sent_payment_data["payer"]["email"]
    assert _EMAIL_RE.match(payer_email), f"invalid payer email: {payer_email!r}"


@pytest.mark.asyncio
@patch.dict(os.environ, {"BASE_URL": "https://test.com"})
async def test_api_error_returns_none():
    mock_response = {"status": 400, "response": {"message": "Bad Request"}}

    mock_payment_instance = MagicMock()
    mock_payment_instance.create.return_value = mock_response

    mock_sdk_instance = MagicMock()
    mock_sdk_instance.payment.return_value = mock_payment_instance

    with (
        patch("mercadopago.SDK", return_value=mock_sdk_instance),
        patch("mercadopago.config.RequestOptions", return_value=MagicMock()),
    ):
        result = await create_pix_payment(
            order_id=10,
            total_amount=30.0,
            bot_name="TestBot",
            contact_phone="5511999999999",
            access_token_cliente="valid_access_token",
        )

    assert result is None


@pytest.mark.asyncio
@patch.dict(os.environ, {"BASE_URL": "https://test.com"})
async def test_no_pix_data_in_response_returns_none():
    mock_response = {"status": 201, "response": {}}

    mock_payment_instance = MagicMock()
    mock_payment_instance.create.return_value = mock_response

    mock_sdk_instance = MagicMock()
    mock_sdk_instance.payment.return_value = mock_payment_instance

    with (
        patch("mercadopago.SDK", return_value=mock_sdk_instance),
        patch("mercadopago.config.RequestOptions", return_value=MagicMock()),
    ):
        result = await create_pix_payment(
            order_id=7,
            total_amount=20.0,
            bot_name="TestBot",
            contact_phone="5511999999999",
            access_token_cliente="valid_access_token",
        )

    assert result is None


@pytest.mark.asyncio
@patch.dict(os.environ, {"BASE_URL": "https://test.com"})
async def test_sdk_init_exception_returns_none():
    with patch("mercadopago.SDK", side_effect=Exception("SDK init failed")):
        result = await create_pix_payment(
            order_id=5,
            total_amount=15.0,
            bot_name="TestBot",
            contact_phone="5511999999999",
            access_token_cliente="bad_token",
        )

    assert result is None


@pytest.mark.asyncio
@patch.dict(os.environ, {"BASE_URL": "https://test.com"})
async def test_payment_create_exception_returns_none():
    mock_payment_instance = MagicMock()
    mock_payment_instance.create.side_effect = Exception("Network error")

    mock_sdk_instance = MagicMock()
    mock_sdk_instance.payment.return_value = mock_payment_instance

    with (
        patch("mercadopago.SDK", return_value=mock_sdk_instance),
        patch("mercadopago.config.RequestOptions", return_value=MagicMock()),
    ):
        result = await create_pix_payment(
            order_id=3,
            total_amount=75.0,
            bot_name="TestBot",
            contact_phone="5511999999999",
            access_token_cliente="valid_access_token",
        )

    assert result is None
