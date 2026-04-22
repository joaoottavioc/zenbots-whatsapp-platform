"""Free-tier branding footer is appended inside send_whatsapp_message.

The footer is applied when the cached plan tier for the bot's
phone_number_id resolves to "free", and skipped for any other tier or
when the lookup fails open (None).
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.whatsapp import (
    FREE_TIER_FOOTER,
    _apply_free_tier_branding,
    send_whatsapp_message,
)


@pytest.mark.asyncio
async def test_apply_branding_free_tier_appends_footer():
    with patch(
        "app.whatsapp.get_tier_by_phone_id", new_callable=AsyncMock, return_value="free"
    ):
        out = await _apply_free_tier_branding("Olá!", "PHONE")
    assert out.endswith(FREE_TIER_FOOTER)
    assert out.startswith("Olá!")


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["pro", "founder", "plus", "enterprise", None])
async def test_apply_branding_non_free_tier_passes_through(tier):
    with patch(
        "app.whatsapp.get_tier_by_phone_id", new_callable=AsyncMock, return_value=tier
    ):
        out = await _apply_free_tier_branding("Olá!", "PHONE")
    assert out == "Olá!"


@pytest.mark.asyncio
async def test_apply_branding_idempotent():
    """Double-calling the helper must not stack footers."""
    with patch(
        "app.whatsapp.get_tier_by_phone_id", new_callable=AsyncMock, return_value="free"
    ):
        once = await _apply_free_tier_branding("Olá!", "PHONE")
        twice = await _apply_free_tier_branding(once, "PHONE")
    assert once == twice
    assert twice.count(FREE_TIER_FOOTER.strip()) == 1


@pytest.mark.asyncio
async def test_apply_branding_empty_text_noop():
    with patch(
        "app.whatsapp.get_tier_by_phone_id", new_callable=AsyncMock, return_value="free"
    ) as mock_tier:
        out = await _apply_free_tier_branding("", "PHONE")
    assert out == ""
    mock_tier.assert_not_called()


@pytest.mark.asyncio
async def test_send_whatsapp_message_brands_free_tier_body():
    captured: dict = {}

    class FakeResp:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResp()

    with (
        patch(
            "app.whatsapp.get_tier_by_phone_id",
            new_callable=AsyncMock,
            return_value="free",
        ),
        patch("app.whatsapp.httpx.AsyncClient", return_value=FakeClient()),
    ):
        await send_whatsapp_message(
            to="5511999990000",
            message="Pedido confirmado",
            token="TOKEN",
            phone_id="PHONE",
        )

    assert captured["json"]["text"]["body"] == "Pedido confirmado" + FREE_TIER_FOOTER


@pytest.mark.asyncio
async def test_send_whatsapp_message_brands_free_tier_media_caption():
    captured: dict = {}

    class FakeResp:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            return FakeResp()

    with (
        patch(
            "app.whatsapp.get_tier_by_phone_id",
            new_callable=AsyncMock,
            return_value="free",
        ),
        patch("app.whatsapp.httpx.AsyncClient", return_value=FakeClient()),
    ):
        await send_whatsapp_message(
            to="5511999990000",
            message="Cardápio anexo",
            token="TOKEN",
            phone_id="PHONE",
            media_url="https://example.com/menu.pdf",
            media_type="document",
        )

    assert (
        captured["json"]["document"]["caption"] == "Cardápio anexo" + FREE_TIER_FOOTER
    )


@pytest.mark.asyncio
async def test_send_whatsapp_message_no_brand_for_pro():
    captured: dict = {}

    class FakeResp:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            return FakeResp()

    with (
        patch(
            "app.whatsapp.get_tier_by_phone_id",
            new_callable=AsyncMock,
            return_value="pro",
        ),
        patch("app.whatsapp.httpx.AsyncClient", return_value=FakeClient()),
    ):
        await send_whatsapp_message(
            to="5511999990000",
            message="Pedido confirmado",
            token="TOKEN",
            phone_id="PHONE",
        )

    assert captured["json"]["text"]["body"] == "Pedido confirmado"
