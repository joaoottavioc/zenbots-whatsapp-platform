# tests/test_ifood_extraction.py
"""Tests for iFood URL extraction (Cadastro Mágico from iFood link)."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Unit tests: URL validation
# ---------------------------------------------------------------------------


class TestIsValidIfoodUrl:
    def test_valid_restaurant_url(self):
        from app.menu_extraction import is_valid_ifood_url

        assert is_valid_ifood_url(
            "https://www.ifood.com.br/delivery/curitiba-pr/pizzaria-batel/abc-123"
        )

    def test_valid_without_www(self):
        from app.menu_extraction import is_valid_ifood_url

        assert is_valid_ifood_url(
            "https://ifood.com.br/delivery/sao-paulo-sp/burger-king/def-456"
        )

    def test_valid_http(self):
        from app.menu_extraction import is_valid_ifood_url

        assert is_valid_ifood_url(
            "http://www.ifood.com.br/delivery/curitiba-pr/restaurante/slug"
        )

    def test_invalid_not_ifood(self):
        from app.menu_extraction import is_valid_ifood_url

        assert not is_valid_ifood_url("https://google.com")

    def test_invalid_ifood_home(self):
        from app.menu_extraction import is_valid_ifood_url

        assert not is_valid_ifood_url("https://www.ifood.com.br")

    def test_invalid_ifood_no_restaurant(self):
        from app.menu_extraction import is_valid_ifood_url

        assert not is_valid_ifood_url("https://www.ifood.com.br/delivery/curitiba-pr")

    def test_empty_string(self):
        from app.menu_extraction import is_valid_ifood_url

        assert not is_valid_ifood_url("")

    def test_whitespace_trimmed(self):
        from app.menu_extraction import is_valid_ifood_url

        assert is_valid_ifood_url(
            "  https://www.ifood.com.br/delivery/city/restaurant/slug  "
        )


# ---------------------------------------------------------------------------
# Unit tests: __NEXT_DATA__ parsing
# ---------------------------------------------------------------------------


class TestExtractMenuFromNextData:
    def test_extracts_products_with_standard_fields(self):
        from app.menu_extraction import _extract_menu_from_next_data

        data = {
            "props": {
                "pageProps": {
                    "name": "Pizzaria Batel",
                    "catalog": [
                        {
                            "name": "Pizza Margherita",
                            "price": 39.90,
                            "description": "Molho, mussarela e manjericão",
                            "category": "Pizzas",
                        },
                        {
                            "name": "Pizza Calabresa",
                            "price": 42.00,
                            "description": "Calabresa e cebola",
                            "category": "Pizzas",
                        },
                    ],
                }
            }
        }

        name, products = _extract_menu_from_next_data(data)
        assert name == "Pizzaria Batel"
        assert len(products) == 2
        assert products[0]["name"] == "Pizza Margherita"
        assert products[0]["price"] == 39.90

    def test_handles_centavo_prices(self):
        from app.menu_extraction import _extract_menu_from_next_data

        data = {
            "items": [
                {"name": "X-Burger", "price": 2590, "description": "Com queijo"},
            ]
        }

        _, products = _extract_menu_from_next_data(data)
        assert len(products) == 1
        assert products[0]["price"] == 25.90

    def test_handles_unitPrice_field(self):
        from app.menu_extraction import _extract_menu_from_next_data

        data = {
            "menu": [
                {"name": "Açaí 500ml", "unitPrice": 18.50, "details": "Com granola"},
            ]
        }

        _, products = _extract_menu_from_next_data(data)
        assert len(products) == 1
        assert products[0]["price"] == 18.50

    def test_deduplicates_by_name(self):
        from app.menu_extraction import _extract_menu_from_next_data

        data = {
            "a": [
                {"name": "Coca-Cola", "price": 8.0, "description": "Lata"},
            ],
            "b": [
                {"name": "Coca-Cola", "price": 8.0, "description": "Lata 350ml"},
            ],
        }

        _, products = _extract_menu_from_next_data(data)
        assert len(products) == 1

    def test_skips_zero_price(self):
        from app.menu_extraction import _extract_menu_from_next_data

        data = {
            "items": [
                {"name": "Free Sample", "price": 0, "description": "Grátis"},
                {"name": "Paid Item", "price": 15.0, "description": "Pago"},
            ]
        }

        _, products = _extract_menu_from_next_data(data)
        assert len(products) == 1
        assert products[0]["name"] == "Paid Item"

    def test_empty_data_returns_nothing(self):
        from app.menu_extraction import _extract_menu_from_next_data

        _, products = _extract_menu_from_next_data({})
        assert products == []

    def test_default_category_is_geral(self):
        from app.menu_extraction import _extract_menu_from_next_data

        data = {
            "items": [
                {"name": "Teste", "price": 10.0, "description": "Desc"},
            ]
        }

        _, products = _extract_menu_from_next_data(data)
        assert products[0]["category"] == "Geral"


# ---------------------------------------------------------------------------
# Unit tests: fetch_ifood_menu
# ---------------------------------------------------------------------------


class TestFetchIfoodMenu:
    @pytest.mark.asyncio
    async def test_invalid_url_raises_value_error(self):
        from app.menu_extraction import fetch_ifood_menu

        with pytest.raises(ValueError, match="URL inválida"):
            await fetch_ifood_menu("https://google.com")

    @pytest.mark.asyncio
    async def test_successful_extraction(self):
        from app.menu_extraction import fetch_ifood_menu

        next_data = {
            "props": {
                "pageProps": {
                    "name": "Burger House",
                    "items": [
                        {"name": "X-Tudo", "price": 28.0, "description": "Completo"},
                        {"name": "X-Salada", "price": 22.0, "description": "Leve"},
                    ],
                }
            }
        }
        html = (
            '<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(next_data)
            + "</script></html>"
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            name, products = await fetch_ifood_menu(
                "https://www.ifood.com.br/delivery/city/burger-house/slug-123"
            )

        assert name == "Burger House"
        assert len(products) == 2

    @pytest.mark.asyncio
    async def test_no_products_raises_value_error(self):
        from app.menu_extraction import fetch_ifood_menu

        html = (
            '<html><script id="__NEXT_DATA__" type="application/json">'
            '{"props":{}}'
            "</script></html>"
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="Nenhum produto"):
                await fetch_ifood_menu(
                    "https://www.ifood.com.br/delivery/city/empty-place/slug"
                )

    @pytest.mark.asyncio
    async def test_http_error_raises_value_error(self):
        from app.menu_extraction import fetch_ifood_menu

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = ""

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="HTTP 404"):
                await fetch_ifood_menu(
                    "https://www.ifood.com.br/delivery/city/gone/slug"
                )

    @pytest.mark.asyncio
    async def test_no_next_data_raises_value_error(self):
        from app.menu_extraction import fetch_ifood_menu

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<html><body>No script here</body></html>"

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="extrair dados"):
                await fetch_ifood_menu(
                    "https://www.ifood.com.br/delivery/city/bad/slug"
                )


# ---------------------------------------------------------------------------
# Endpoint integration test
# ---------------------------------------------------------------------------


class TestUploadFromUrlEndpoint:
    @pytest.mark.asyncio
    async def test_invalid_url_returns_400(self):
        from app.bot_routes import upload_catalog_from_url

        mock_session = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = 1
        mock_bot = MagicMock()
        mock_bot.user_id = 1

        request = MagicMock()
        request.url = "https://google.com"

        with patch(
            "app.bot_routes.crud.get_bot_by_id",
            new=AsyncMock(return_value=mock_bot),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await upload_catalog_from_url(
                    bot_id=1,
                    request_data=request,
                    session=mock_session,
                    current_user=mock_user,
                )
            assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_successful_import(self):
        from app.bot_routes import upload_catalog_from_url

        mock_session = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = 1
        mock_bot = MagicMock()
        mock_bot.user_id = 1

        request = MagicMock()
        request.url = "https://www.ifood.com.br/delivery/city/restaurant/slug"

        products = [
            {
                "name": "Hambúrguer",
                "price": 25.0,
                "description": "Artesanal",
                "category": "Lanches",
            },
            {
                "name": "Batata Frita",
                "price": 15.0,
                "description": "Crocante",
                "category": "Acompanhamentos",
            },
        ]

        with (
            patch(
                "app.bot_routes.crud.get_bot_by_id",
                new=AsyncMock(return_value=mock_bot),
            ),
            patch(
                "httpx.AsyncClient",
            ) as mock_client_cls,
            patch(
                "app.bot_routes.crud.bulk_create_products",
                new=AsyncMock(return_value=2),
            ),
        ):
            next_data = {
                "props": {
                    "pageProps": {
                        "name": "Burger Place",
                        "items": products,
                    }
                }
            }
            html = (
                '<html><script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(next_data)
                + "</script></html>"
            )
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = html

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await upload_catalog_from_url(
                bot_id=1,
                request_data=request,
                session=mock_session,
                current_user=mock_user,
            )

        assert "2 produtos importados" in result["message"]
        assert "Burger Place" in result["message"]

    @pytest.mark.asyncio
    async def test_access_denied_for_wrong_user(self):
        from app.bot_routes import upload_catalog_from_url

        mock_session = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = 1
        mock_bot = MagicMock()
        mock_bot.user_id = 999  # Different user

        request = MagicMock()
        request.url = "https://www.ifood.com.br/delivery/city/restaurant/slug"

        with patch(
            "app.bot_routes.crud.get_bot_by_id",
            new=AsyncMock(return_value=mock_bot),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await upload_catalog_from_url(
                    bot_id=1,
                    request_data=request,
                    session=mock_session,
                    current_user=mock_user,
                )
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_ifood_error_returns_400(self):
        from app.bot_routes import upload_catalog_from_url

        mock_session = AsyncMock()
        mock_user = MagicMock()
        mock_user.id = 1
        mock_bot = MagicMock()
        mock_bot.user_id = 1

        request = MagicMock()
        request.url = "https://www.ifood.com.br/delivery/city/restaurant/slug"

        with (
            patch(
                "app.bot_routes.crud.get_bot_by_id",
                new=AsyncMock(return_value=mock_bot),
            ),
            patch(
                "httpx.AsyncClient",
            ) as mock_client_cls,
        ):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = '<html><script id="__NEXT_DATA__" type="application/json">{"props":{}}</script></html>'

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(HTTPException) as exc_info:
                await upload_catalog_from_url(
                    bot_id=1,
                    request_data=request,
                    session=mock_session,
                    current_user=mock_user,
                )
            assert exc_info.value.status_code == 400
