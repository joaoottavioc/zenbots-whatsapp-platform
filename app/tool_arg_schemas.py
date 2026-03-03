"""Pydantic models for validating LLM tool-call arguments."""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class CartItemInput(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=50)
    notes: Optional[str] = Field(default=None, max_length=200)


class AddItemsArgs(BaseModel):
    items: List[CartItemInput] = Field(min_length=1)


class RemoveItemsArgs(BaseModel):
    product_ids: List[int] = Field(min_length=1)


class ModifyQuantityArgs(BaseModel):
    product_id: int
    new_quantity: int = Field(ge=0, le=50)


class BulkModifyArgs(BaseModel):
    updates: List[ModifyQuantityArgs]


class AnswerArgs(BaseModel):
    response_text: str = Field(max_length=1500)


class FoundProductsArgs(BaseModel):
    product_names: List[str]


class SearchCatalogArgs(BaseModel):
    search_concept: str = Field(max_length=200)


class UpdateObservationArgs(BaseModel):
    product_id: int
    notes: str = Field(max_length=200)


class ProposeConfirmArgs(BaseModel):
    confirmation_question: str
    proposed_action: dict


TOOL_VALIDATORS: Dict[str, type] = {
    "add_items_to_cart": AddItemsArgs,
    "remove_items_from_cart": RemoveItemsArgs,
    "modify_item_quantity": ModifyQuantityArgs,
    "bulk_modify_quantities": BulkModifyArgs,
    "answer_conversationally": AnswerArgs,
    "answer_with_found_products": FoundProductsArgs,
    "search_catalog_for_suggestions": SearchCatalogArgs,
    "update_item_observation": UpdateObservationArgs,
    "propose_and_confirm_action": ProposeConfirmArgs,
}
