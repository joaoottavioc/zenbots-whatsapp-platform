"""Tests for LLM tool argument validation schemas (V3)."""

import pytest
from pydantic import ValidationError

from app.tool_arg_schemas import (
    AddItemsArgs,
    AnswerArgs,
    CartItemInput,
    ModifyQuantityArgs,
    RemoveItemsArgs,
    SearchCatalogArgs,
    UpdateObservationArgs,
    TOOL_VALIDATORS,
)


class TestCartItemInput:
    def test_valid(self):
        item = CartItemInput(product_id=1, quantity=2, notes="Sem cebola")
        assert item.product_id == 1
        assert item.quantity == 2

    def test_quantity_zero_rejected(self):
        with pytest.raises(ValidationError):
            CartItemInput(product_id=1, quantity=0)

    def test_quantity_over_50_rejected(self):
        with pytest.raises(ValidationError):
            CartItemInput(product_id=1, quantity=51)

    def test_notes_too_long_rejected(self):
        with pytest.raises(ValidationError):
            CartItemInput(product_id=1, quantity=1, notes="x" * 201)

    def test_notes_optional(self):
        item = CartItemInput(product_id=1, quantity=1)
        assert item.notes is None


class TestAddItemsArgs:
    def test_valid(self):
        args = AddItemsArgs(items=[{"product_id": 1, "quantity": 2}])
        assert len(args.items) == 1

    def test_empty_items_rejected(self):
        with pytest.raises(ValidationError):
            AddItemsArgs(items=[])


class TestRemoveItemsArgs:
    def test_valid(self):
        args = RemoveItemsArgs(product_ids=[1, 2])
        assert args.product_ids == [1, 2]

    def test_empty_rejected(self):
        with pytest.raises(ValidationError):
            RemoveItemsArgs(product_ids=[])


class TestModifyQuantityArgs:
    def test_valid(self):
        args = ModifyQuantityArgs(product_id=5, new_quantity=3)
        assert args.new_quantity == 3

    def test_negative_quantity_rejected(self):
        with pytest.raises(ValidationError):
            ModifyQuantityArgs(product_id=5, new_quantity=-1)

    def test_zero_quantity_accepted(self):
        args = ModifyQuantityArgs(product_id=5, new_quantity=0)
        assert args.new_quantity == 0


class TestAnswerArgs:
    def test_valid(self):
        args = AnswerArgs(response_text="Olá!")
        assert args.response_text == "Olá!"

    def test_too_long_rejected(self):
        with pytest.raises(ValidationError):
            AnswerArgs(response_text="x" * 1501)


class TestSearchCatalogArgs:
    def test_valid(self):
        args = SearchCatalogArgs(search_concept="sobremesa")
        assert args.search_concept == "sobremesa"

    def test_too_long_rejected(self):
        with pytest.raises(ValidationError):
            SearchCatalogArgs(search_concept="x" * 201)


class TestUpdateObservationArgs:
    def test_valid(self):
        args = UpdateObservationArgs(product_id=1, notes="Sem maionese")
        assert args.notes == "Sem maionese"

    def test_notes_too_long_rejected(self):
        with pytest.raises(ValidationError):
            UpdateObservationArgs(product_id=1, notes="x" * 201)


class TestToolValidatorRegistry:
    def test_all_tools_have_validators(self):
        expected_tools = [
            "add_items_to_cart",
            "remove_items_from_cart",
            "modify_item_quantity",
            "bulk_modify_quantities",
            "answer_conversationally",
            "answer_with_found_products",
            "search_catalog_for_suggestions",
            "update_item_observation",
            "propose_and_confirm_action",
        ]
        for tool in expected_tools:
            assert tool in TOOL_VALIDATORS, f"Missing validator for {tool}"

    def test_validator_values_are_classes(self):
        for name, cls in TOOL_VALIDATORS.items():
            assert isinstance(cls, type), f"{name} validator is not a class"
