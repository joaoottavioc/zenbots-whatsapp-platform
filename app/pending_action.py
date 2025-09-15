from datetime import timedelta
from typing import Any, Dict
from app.time import utcnow

# se quiser parametrizar por env mais tarde, troque 10 por uma leitura de settings
_TTL_MINUTES = 10

def save_pending(cart, tool: str, args: Dict[str, Any], question: str) -> None:
    cart.pending_action_tool = tool
    cart.pending_action_args = args
    cart.pending_action_question = question
    cart.pending_action_expires_at = utcnow() + timedelta(minutes=_TTL_MINUTES)

def clear_pending(cart) -> None:
    cart.pending_action_tool = None
    cart.pending_action_args = None
    cart.pending_action_question = None
    cart.pending_action_expires_at = None

def has_valid_pending(cart) -> bool:
    return (
        cart.pending_action_tool is not None
        and cart.pending_action_expires_at is not None
        and utcnow() <= cart.pending_action_expires_at
    )

def expire_if_needed(cart) -> None:
    if cart.pending_action_expires_at and utcnow() > cart.pending_action_expires_at:
        clear_pending(cart)
