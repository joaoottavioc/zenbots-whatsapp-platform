# tests/test_utils.py
"""
Tests for app/utils.py.

Covered:
- normalize_phone        (strip non-digits, strip "55" prefix, edge cases)
- calculate_distance     (Haversine, same point, None coords, invalid strings)
- check_delivery_radius  (within radius, outside radius, missing coords)
- get_address_from_cep   (valid CEP with mocked HTTP, invalid format, API error)
"""
import re
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.utils import (
    normalize_phone,
    calculate_distance,
    check_delivery_radius,
    get_address_from_cep,
    _fetch_coordinates,
)


# ===========================================================================
# TestNormalizePhone
# ===========================================================================

class TestNormalizePhone:
    """normalize_phone returns 10-11 digit Brazilian number or None."""

    def test_valid_mobile_with_country_code(self):
        """'+55 (11) 98765-4321' -> '11987654321'."""
        assert normalize_phone("+55 (11) 98765-4321") == "11987654321"

    def test_valid_mobile_without_country_code(self):
        """'11987654321' (11 digits, DDD 11) -> '11987654321'."""
        assert normalize_phone("11987654321") == "11987654321"

    def test_valid_landline(self):
        """'1134567890' (10 digits, DDD 11) -> '1134567890'."""
        assert normalize_phone("1134567890") == "1134567890"

    def test_strips_55_prefix_when_12_plus_digits(self):
        """'5511987654321' (13 digits) strips '55' -> '11987654321'."""
        assert normalize_phone("5511987654321") == "11987654321"

    def test_does_not_strip_55_when_short(self):
        """'5598765432' (10 digits starting with 55) -> valid DDD 55."""
        assert normalize_phone("5598765432") == "5598765432"

    def test_empty_string_returns_none(self):
        """Empty input returns None."""
        assert normalize_phone("") is None

    def test_too_short_returns_none(self):
        """A number with fewer than 10 digits returns None."""
        assert normalize_phone("123456789") is None

    def test_too_long_returns_none(self):
        """A number longer than 11 digits (after stripping) returns None."""
        assert normalize_phone("123456789012") is None

    def test_invalid_ddd_below_11_returns_none(self):
        """DDD below 11 (e.g., 10) is invalid."""
        assert normalize_phone("1098765432") is None

    def test_strips_formatting_characters(self):
        """Dashes, dots, spaces, parentheses are stripped."""
        assert normalize_phone("(21) 98765-4321") == "21987654321"

    def test_only_country_code_returns_none(self):
        """Input '55' alone is too short."""
        assert normalize_phone("55") is None

    def test_preserves_inner_55(self):
        """'11557654321' — DDD 11, valid 11-digit number."""
        assert normalize_phone("11557654321") == "11557654321"


# ===========================================================================
# TestCalculateDistance
# ===========================================================================

class TestCalculateDistance:
    # Approximate straight-line distance between São Paulo and Rio de Janeiro.
    SP_LAT, SP_LON = -23.5505, -46.6333
    RJ_LAT, RJ_LON = -22.9068, -43.1729

    def test_sp_to_rj_approximately_360km(self):
        """Haversine distance between SP and RJ should be ~360 km (±20 km)."""
        dist = calculate_distance(self.SP_LAT, self.SP_LON, self.RJ_LAT, self.RJ_LON)
        assert 340.0 <= dist <= 380.0, f"Expected ~360 km, got {dist:.2f} km"

    def test_same_point_returns_zero(self):
        """Distance from a point to itself is 0."""
        dist = calculate_distance(self.SP_LAT, self.SP_LON, self.SP_LAT, self.SP_LON)
        assert dist == pytest.approx(0.0, abs=1e-6)

    def test_none_coord_returns_9999(self):
        """Any None coordinate must return the sentinel value 9999.0."""
        assert calculate_distance(None, self.SP_LON, self.RJ_LAT, self.RJ_LON) == 9999.0
        assert calculate_distance(self.SP_LAT, None, self.RJ_LAT, self.RJ_LON) == 9999.0
        assert calculate_distance(self.SP_LAT, self.SP_LON, None, self.RJ_LON) == 9999.0
        assert calculate_distance(self.SP_LAT, self.SP_LON, self.RJ_LAT, None) == 9999.0
        assert calculate_distance(None, None, None, None) == 9999.0

    def test_invalid_string_returns_9999(self):
        """Non-numeric strings that cannot be converted to float return 9999.0."""
        assert calculate_distance("abc", self.SP_LON, self.RJ_LAT, self.RJ_LON) == 9999.0
        assert calculate_distance(self.SP_LAT, "xyz", self.RJ_LAT, self.RJ_LON) == 9999.0

    def test_string_numeric_values_are_accepted(self):
        """Numeric strings are coerced to float and produce a valid distance."""
        dist = calculate_distance(
            str(self.SP_LAT), str(self.SP_LON),
            str(self.RJ_LAT), str(self.RJ_LON),
        )
        assert 340.0 <= dist <= 380.0

    def test_symmetric(self):
        """Distance A→B equals distance B→A."""
        d1 = calculate_distance(self.SP_LAT, self.SP_LON, self.RJ_LAT, self.RJ_LON)
        d2 = calculate_distance(self.RJ_LAT, self.RJ_LON, self.SP_LAT, self.SP_LON)
        assert d1 == pytest.approx(d2, rel=1e-6)


# ===========================================================================
# TestCheckDeliveryRadius
# ===========================================================================

class TestCheckDeliveryRadius:
    """check_delivery_radius is async; pytest-asyncio handles coroutine execution."""

    def _make_bot(self, lat, lon, radius=10.0):
        bot = MagicMock()
        bot.latitude = lat
        bot.longitude = lon
        bot.max_delivery_radius = radius
        return bot

    # SP coords used as the bot location
    BOT_LAT, BOT_LON = -23.5505, -46.6333

    async def test_within_radius_returns_true(self):
        """A customer very close to the restaurant is within the delivery radius."""
        bot = self._make_bot(self.BOT_LAT, self.BOT_LON, radius=10.0)
        # Shift ~0.01 degrees (~1 km) — well within 10 km
        is_ok, dist = await check_delivery_radius(bot, self.BOT_LAT + 0.01, self.BOT_LON + 0.01)
        assert is_ok is True
        assert dist < 10.0

    async def test_outside_radius_returns_false(self):
        """A customer far away (RJ) is outside a 10 km delivery radius."""
        bot = self._make_bot(self.BOT_LAT, self.BOT_LON, radius=10.0)
        RJ_LAT, RJ_LON = -22.9068, -43.1729
        is_ok, dist = await check_delivery_radius(bot, RJ_LAT, RJ_LON)
        assert is_ok is False
        assert dist > 10.0

    async def test_missing_bot_coords_returns_false(self):
        """Bot without lat/lon always denies delivery (returns False, 0.0)."""
        bot = self._make_bot(None, None, radius=10.0)
        is_ok, dist = await check_delivery_radius(bot, -23.55, -46.63)
        assert is_ok is False
        assert dist == 0.0

    async def test_missing_customer_lat_returns_false(self):
        """None customer latitude returns (False, 9999.0)."""
        bot = self._make_bot(self.BOT_LAT, self.BOT_LON, radius=10.0)
        is_ok, dist = await check_delivery_radius(bot, None, -46.63)
        assert is_ok is False
        assert dist == 9999.0

    async def test_missing_customer_lon_returns_false(self):
        """None customer longitude returns (False, 9999.0)."""
        bot = self._make_bot(self.BOT_LAT, self.BOT_LON, radius=10.0)
        is_ok, dist = await check_delivery_radius(bot, -23.55, None)
        assert is_ok is False
        assert dist == 9999.0

    async def test_exactly_on_radius_boundary_is_ok(self):
        """A distance exactly equal to max_radius is accepted (<=)."""
        bot = self._make_bot(self.BOT_LAT, self.BOT_LON, radius=10.0)
        # Patch calculate_distance to return exactly 10.0
        with patch("app.utils.calculate_distance", return_value=10.0):
            is_ok, dist = await check_delivery_radius(bot, -23.60, -46.70)
        assert is_ok is True
        assert dist == 10.0


# ===========================================================================
# TestGetAddressFromCep
# ===========================================================================

class TestGetAddressFromCep:
    """get_address_from_cep makes async HTTP calls; httpx is mocked."""

    _VALID_API_RESPONSE = {
        "cep": "01310100",
        "street": "Avenida Paulista",
        "neighborhood": "Bela Vista",
        "city": "São Paulo",
        "state": "SP",
        "location": {
            "coordinates": {"latitude": "-23.5630994", "longitude": "-46.6542719"}
        },
    }

    def _make_httpx_response(self, status_code: int, json_data: dict):
        """Build a MagicMock that mimics an httpx.Response."""
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_data
        return resp

    async def test_valid_cep_returns_address_dict(self):
        """A well-formed 8-digit CEP with a 200 response returns the expected dict."""
        mock_resp = self._make_httpx_response(200, self._VALID_API_RESPONSE)

        # Mock the internal _fetch_coordinates helper to avoid a second HTTP call
        with patch("app.utils._fetch_coordinates", new=AsyncMock(return_value=("-23.563", "-46.654"))), \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await get_address_from_cep("01310-100")

        assert result is not None
        assert result["cep"] == "01310100"
        assert result["street"] == "Avenida Paulista"
        assert result["city"] == "São Paulo"
        assert result["state"] == "SP"
        assert "lat" in result
        assert "lng" in result

    async def test_invalid_cep_too_short_returns_none(self):
        """A CEP with fewer than 8 digits (after stripping) returns None immediately."""
        result = await get_address_from_cep("1234")
        assert result is None

    async def test_invalid_cep_with_letters_returns_none(self):
        """A CEP that resolves to fewer than 8 digits after stripping returns None."""
        result = await get_address_from_cep("ABC-DEF")
        assert result is None

    async def test_api_non_200_returns_none(self):
        """A non-200 HTTP status code from BrasilAPI returns None."""
        mock_resp = self._make_httpx_response(404, {})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await get_address_from_cep("01310100")

        assert result is None

    async def test_api_network_error_returns_none(self):
        """A network exception during the HTTP call returns None instead of raising."""
        import httpx as _httpx

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=_httpx.RequestError("timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await get_address_from_cep("01310100")

        assert result is None

    async def test_all_zeros_cep_returns_none(self):
        """CEP 00000000 is not a valid CEP and should return None."""
        result = await get_address_from_cep("00000000")
        assert result is None

    async def test_below_range_cep_returns_none(self):
        """CEPs below 01000-000 (1000000) don't exist and should return None."""
        result = await get_address_from_cep("00500000")
        assert result is None

    async def test_at_minimum_range_cep_proceeds(self):
        """CEP 01000-000 (1000000) is the minimum valid CEP and should proceed to API call."""
        mock_resp = self._make_httpx_response(404, {})
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client
            result = await get_address_from_cep("01000000")
        # API returned 404 so result is None, but importantly the API was called
        assert result is None
        mock_client.get.assert_called_once()

    async def test_cep_with_formatting_characters_is_cleaned(self):
        """CEPs like "01310-100" have non-digits stripped before the request."""
        mock_resp = self._make_httpx_response(200, self._VALID_API_RESPONSE)

        with patch("app.utils._fetch_coordinates", new=AsyncMock(return_value=(None, None))), \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await get_address_from_cep("01310-100")

        # Request must have been made with the clean CEP
        call_url = mock_client.get.call_args[0][0]
        assert "01310100" in call_url
        assert "-" not in call_url.split("/")[-1]


# ===========================================================================
# TestFetchCoordinatesNoKeyLeak
# ===========================================================================

class TestFetchCoordinatesNoKeyLeak:
    """Ensure _fetch_coordinates never prints the API key to stdout."""

    async def test_does_not_print_api_key(self, capsys):
        """The Google Maps API key must never appear in stdout."""
        fake_key = "SUPER_SECRET_KEY_12345"
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"status": "REQUEST_DENIED"}
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch.dict("os.environ", {"GOOGLE_MAPS_API_KEY": fake_key}):
            await _fetch_coordinates(mock_client, "01310100", "Rua X", "Cidade Y", "SP")

        captured = capsys.readouterr()
        assert fake_key not in captured.out, "API key was leaked to stdout!"


# ===========================================================================
# TestLookupCepEndpointAuth
# ===========================================================================

class TestLookupCepEndpointAuth:
    """Verify the CEP lookup endpoint requires authentication."""

    def test_endpoint_requires_current_user_dependency(self):
        """lookup_cep_endpoint must have a Depends(get_current_user) parameter."""
        import inspect
        from app.utils import lookup_cep_endpoint

        sig = inspect.signature(lookup_cep_endpoint)
        params = sig.parameters

        assert "current_user" in params, (
            "lookup_cep_endpoint is missing the 'current_user' parameter"
        )
