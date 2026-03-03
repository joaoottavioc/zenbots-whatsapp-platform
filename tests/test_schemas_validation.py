"""Tests for schema validation: price, fee, and max-length constraints."""

import pytest
from pydantic import ValidationError

from app.schemas import ProductBase, ProductUpdate, BotCreate, BotUpdate


# ---------------------------------------------------------------------------
# V1: Price and fee validation
# ---------------------------------------------------------------------------


class TestProductPriceValidation:
    def test_positive_price_accepted(self):
        p = ProductBase(name="Pizza", price=10.0, category="Food")
        assert p.price == 10.0

    def test_zero_price_rejected(self):
        with pytest.raises(ValidationError):
            ProductBase(name="Pizza", price=0, category="Food")

    def test_negative_price_rejected(self):
        with pytest.raises(ValidationError):
            ProductBase(name="Pizza", price=-5.0, category="Food")

    def test_update_positive_price_accepted(self):
        p = ProductUpdate(price=15.0)
        assert p.price == 15.0

    def test_update_none_price_accepted(self):
        p = ProductUpdate()
        assert p.price is None

    def test_update_zero_price_rejected(self):
        with pytest.raises(ValidationError):
            ProductUpdate(price=0)

    def test_update_negative_price_rejected(self):
        with pytest.raises(ValidationError):
            ProductUpdate(price=-1.0)


class TestBotFeeValidation:
    def test_zero_delivery_fee_accepted(self):
        b = BotCreate(
            restaurant_name="Test",
            whatsapp_number="123",
            delivery_fee=0.0,
            min_order_value=0.0,
            whatsapp_token="tok",
            phone_number_id="pid",
        )
        assert b.delivery_fee == 0.0

    def test_positive_delivery_fee_accepted(self):
        b = BotCreate(
            restaurant_name="Test",
            whatsapp_number="123",
            delivery_fee=5.0,
            min_order_value=10.0,
            whatsapp_token="tok",
            phone_number_id="pid",
        )
        assert b.delivery_fee == 5.0

    def test_negative_delivery_fee_rejected(self):
        with pytest.raises(ValidationError):
            BotCreate(
                restaurant_name="Test",
                whatsapp_number="123",
                delivery_fee=-1.0,
                whatsapp_token="tok",
                phone_number_id="pid",
            )

    def test_negative_min_order_value_rejected(self):
        with pytest.raises(ValidationError):
            BotCreate(
                restaurant_name="Test",
                whatsapp_number="123",
                min_order_value=-1.0,
                whatsapp_token="tok",
                phone_number_id="pid",
            )

    def test_update_negative_fee_rejected(self):
        with pytest.raises(ValidationError):
            BotUpdate(delivery_fee=-0.01)

    def test_update_none_fee_accepted(self):
        b = BotUpdate()
        assert b.delivery_fee is None

    def test_update_zero_fee_accepted(self):
        b = BotUpdate(delivery_fee=0.0)
        assert b.delivery_fee == 0.0


# ---------------------------------------------------------------------------
# V3: Timezone validation
# ---------------------------------------------------------------------------


class TestTimezoneValidation:
    def test_valid_timezone_accepted_on_create(self):
        b = BotCreate(
            restaurant_name="Test",
            whatsapp_number="123",
            whatsapp_token="tok",
            phone_number_id="pid",
            timezone="America/Sao_Paulo",
        )
        assert b.timezone == "America/Sao_Paulo"

    def test_invalid_timezone_rejected_on_create(self):
        with pytest.raises(ValidationError):
            BotCreate(
                restaurant_name="Test",
                whatsapp_number="123",
                whatsapp_token="tok",
                phone_number_id="pid",
                timezone="Invalid/Zone",
            )

    def test_default_timezone_on_create(self):
        b = BotCreate(
            restaurant_name="Test",
            whatsapp_number="123",
            whatsapp_token="tok",
            phone_number_id="pid",
        )
        assert b.timezone == "America/Sao_Paulo"

    def test_valid_timezone_accepted_on_update(self):
        b = BotUpdate(timezone="US/Eastern")
        assert b.timezone == "US/Eastern"

    def test_invalid_timezone_rejected_on_update(self):
        with pytest.raises(ValidationError):
            BotUpdate(timezone="Not/A/Timezone")

    def test_none_timezone_accepted_on_update(self):
        b = BotUpdate(timezone=None)
        assert b.timezone is None

    def test_utc_timezone_accepted(self):
        b = BotUpdate(timezone="UTC")
        assert b.timezone == "UTC"


# ---------------------------------------------------------------------------
# V2: Max-length validation (added in Step 5, tests here for cohesion)
# ---------------------------------------------------------------------------


class TestMaxLengthValidation:
    def test_product_name_too_long_rejected(self):
        with pytest.raises(ValidationError):
            ProductBase(name="x" * 151, price=10.0, category="Food")

    def test_product_name_at_limit_accepted(self):
        p = ProductBase(name="x" * 150, price=10.0, category="Food")
        assert len(p.name) == 150

    def test_product_description_too_long_rejected(self):
        with pytest.raises(ValidationError):
            ProductBase(
                name="Pizza", price=10.0, category="Food", description="x" * 501
            )

    def test_product_category_too_long_rejected(self):
        with pytest.raises(ValidationError):
            ProductBase(name="Pizza", price=10.0, category="x" * 101)

    def test_bot_restaurant_name_too_long_rejected(self):
        with pytest.raises(ValidationError):
            BotCreate(
                restaurant_name="x" * 151,
                whatsapp_number="123",
                whatsapp_token="tok",
                phone_number_id="pid",
            )

    def test_bot_update_restaurant_name_too_long_rejected(self):
        with pytest.raises(ValidationError):
            BotUpdate(restaurant_name="x" * 151)
