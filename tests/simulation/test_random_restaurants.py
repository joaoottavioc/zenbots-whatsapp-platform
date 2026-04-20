"""
Random restaurant simulation tests.

Reads test data from _random_test_data.json (written by random_restaurant_runner.py).
Runs the same 7 scenarios against dynamically created restaurant bots.

Do NOT run directly — use the runner:
  export OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' .env | cut -d= -f2)
  python -m tests.simulation.random_restaurant_runner
"""

import json
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlmodel import select
from sqlalchemy import text

from app.database import async_session
from app.models import Bot, Contact, ShoppingCart, CartItem, Product


# ---------------------------------------------------------------------------
# Load test data
# ---------------------------------------------------------------------------

_DATA_FILE = Path(__file__).parent / "_random_test_data.json"
_TEST_DATA: dict = {}
_LABELS: list[str] = []

if _DATA_FILE.exists():
    _TEST_DATA = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    _LABELS = list(_TEST_DATA.keys())


_bot_id_cache: dict[str, int] = {}


async def _get_bot_id(label: str) -> int:
    if label in _bot_id_cache:
        return _bot_id_cache[label]
    phone_id = _TEST_DATA[label]["phone_number_id"]
    async with async_session() as session:
        result = await session.execute(
            select(Bot.id).where(Bot.phone_number_id == phone_id)
        )
        row = result.first()
        if not row:
            pytest.skip(f"Bot not found for {label}. Run the runner script first.")
        _bot_id_cache[label] = row[0]
        return row[0]


# ---------------------------------------------------------------------------
# SimContext
# ---------------------------------------------------------------------------


class RandSimContext:
    def __init__(self, bot_id: int, phone_number_id: str, contact_phone: str):
        self.bot_id = bot_id
        self.contact_phone = contact_phone
        self.phone_number_id = phone_number_id

    def _build_payload(self, text_body: str) -> dict:
        return {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {
                                    "phone_number_id": self.phone_number_id,
                                    "display_phone_number": self.contact_phone,
                                },
                                "messages": [
                                    {
                                        "from": self.contact_phone,
                                        "id": f"wamid.rd{uuid.uuid4().hex[:12]}",
                                        "text": {"body": text_body},
                                        "type": "text",
                                    }
                                ],
                            }
                        }
                    ]
                }
            ],
        }

    async def send(self, text_body: str) -> str | None:
        import app.whatsapp as wh
        from app.worker import process_whatsapp_message
        import redis.asyncio as aioredis

        local_captured = []
        original_send = wh.send_whatsapp_message

        async def capture_send(*args, **kwargs):
            local_captured.append((args, kwargs))

        wh.send_whatsapp_message = capture_send
        try:
            pool = aioredis.from_url("redis://redis:6379/1")
            ctx = {"redis": pool}
            await process_whatsapp_message(ctx, self._build_payload(text_body))
            await pool.aclose()
        finally:
            wh.send_whatsapp_message = original_send

        if local_captured:
            for call_args, call_kwargs in local_captured:
                msg = call_kwargs.get("message", "")
                if not msg and len(call_args) > 1:
                    msg = call_args[1] if isinstance(call_args[1], str) else ""
                if msg and len(msg) > 5:
                    return msg
        return None

    async def get_cart_items(self) -> list[tuple[str, int]]:
        async with async_session() as fresh:
            cr = await fresh.execute(
                select(Contact).where(
                    Contact.phone_number == self.contact_phone,
                    Contact.bot_id == self.bot_id,
                )
            )
            contact = cr.scalars().first()
            if not contact:
                return []
            sr = await fresh.execute(
                select(ShoppingCart).where(ShoppingCart.contact_id == contact.id)
            )
            cart = sr.scalars().first()
            if not cart:
                return []
            ir = await fresh.execute(
                select(CartItem)
                .where(CartItem.cart_id == cart.id)
                .order_by(CartItem.id)
            )
            result = []
            for item in ir.scalars().all():
                pr = await fresh.execute(
                    select(Product).where(Product.id == item.product_id)
                )
                prod = pr.scalars().first()
                if prod:
                    result.append((prod.name, item.quantity))
            return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_check():
    if not _LABELS:
        pytest.skip(
            "No test data. Run: python -m tests.simulation.random_restaurant_runner"
        )
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("Database not reachable")


async def _ctx(label: str) -> RandSimContext:
    bot_id = await _get_bot_id(label)
    phone = f"5500{str(uuid.uuid4().int)[:10]}"
    return RandSimContext(bot_id, _TEST_DATA[label]["phone_number_id"], phone)


def _sc(label: str) -> dict:
    return _TEST_DATA[label]["scenarios"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


if not _LABELS:
    _LABELS = ["__skip__"]


class TestRandAddSingle:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_add_single(self, db_check, label):
        c = await _ctx(label)
        sc = _sc(label)
        await c.send("oi")
        await c.send(sc["add_single_msg"])
        cart = await c.get_cart_items()
        assert len(cart) >= 1, f"Expected >= 1 item, got {cart}"
        exp_name, exp_qty = sc["add_single_expected"][0]
        found = [(n, q) for n, q in cart if exp_name.lower() in n.lower()]
        assert found, f"'{exp_name}' not in cart: {cart}"
        assert found[0][1] == exp_qty


class TestRandAddMulti:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_add_multi(self, db_check, label):
        c = await _ctx(label)
        sc = _sc(label)
        await c.send("oi")
        await c.send(sc["add_multi_msg"])
        cart = await c.get_cart_items()
        expected = sc["add_multi_expected"]
        assert len(cart) >= len(expected), (
            f"Expected >= {len(expected)} items, got {cart}"
        )
        for exp_name, exp_qty in expected:
            found = [(n, q) for n, q in cart if exp_name.lower() in n.lower()]
            assert found, f"'{exp_name}' not in cart: {cart}"
            assert found[0][1] == exp_qty


class TestRandContinuation:
    """S8: Continuation pattern.

    The most common real-world WhatsApp pattern: customer adds an item, then
    sends a follow-up message starting with "e " (no shopping verb). Today the
    bot misses these because the pre-router ADD guard requires a verb.

    Sequence:
      1. "oi"                       → greeting
      2. format_add(single)         → cart has [single]
      3. "e uma {multi_2}"          → cart should have [single, multi_2]
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_continuation(self, db_check, label):
        sc = _sc(label)
        if not sc.get("continuation_followup_msg"):
            pytest.skip("No continuation scenario")
        c = await _ctx(label)
        await c.send("oi")
        await c.send(sc["continuation_first_msg"])
        cart1 = await c.get_cart_items()
        first_name = sc["continuation_first_product"]
        found1 = [n for n, q in cart1 if first_name.lower() in n.lower()]
        assert found1, f"Setup: '{first_name}' not in cart: {cart1}"

        await c.send(sc["continuation_followup_msg"])
        cart2 = await c.get_cart_items()
        # First product must still be there (continuation must not wipe it)
        still_first = [n for n, q in cart2 if first_name.lower() in n.lower()]
        assert still_first, f"Continuation wiped first product '{first_name}': {cart2}"
        # Second product must have been added
        second_name = sc["continuation_followup_product"]
        found2 = [n for n, q in cart2 if second_name.lower() in n.lower()]
        assert found2, f"Continuation didn't add '{second_name}': {cart2}"


class TestRandUnavailable:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_unavailable(self, db_check, label):
        sc = _sc(label)
        if not sc.get("unavailable_msg"):
            pytest.skip("No unavailable scenario")
        c = await _ctx(label)
        await c.send("oi")
        response = await c.send(sc["unavailable_msg"])
        cart = await c.get_cart_items()
        avail = sc["unavailable_available"]
        unavail = sc["unavailable_name"]
        found = [n for n, q in cart if avail.lower() in n.lower()]
        assert found, f"'{avail}' not in cart: {cart}"
        bad = [n for n, q in cart if unavail.lower() in n.lower()]
        assert not bad, f"'{unavail}' should NOT be in cart: {cart}"
        assert response is not None
        rl = response.lower()
        assert any(
            w in rl
            for w in (
                "falta",
                "indisponivel",
                "indisponível",
                "disponivel",
                "disponível",
            )
        ), f"No em falta: {response[:200]}"


class TestRandRemove:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_remove(self, db_check, label):
        c = await _ctx(label)
        sc = _sc(label)
        await c.send("oi")
        await c.send(sc["remove_add_msg"])
        cart = await c.get_cart_items()
        exp_name = sc["remove_add_expected"][0]
        found = [n for n, q in cart if exp_name.lower() in n.lower()]
        assert found, f"Setup: '{exp_name}' not in cart: {cart}"
        await c.send(sc["remove_msg"])
        cart2 = await c.get_cart_items()
        still = [n for n, q in cart2 if exp_name.lower() in n.lower()]
        assert not still, f"'{exp_name}' still in cart: {cart2}"


class TestRandSuggestions:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_suggestions(self, db_check, label):
        c = await _ctx(label)
        sc = _sc(label)
        await c.send("oi")
        response = await c.send(sc["suggestion_msg"])
        cart = await c.get_cart_items()
        assert len(cart) == 0, f"Suggestions added to cart: {cart}"
        assert response is not None


class TestRandCheckout:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_checkout_protection(self, db_check, label):
        c = await _ctx(label)
        sc = _sc(label)
        await c.send("oi")
        await c.send(sc["checkout_product_msg"])
        await c.send("finalizar")
        await c.send("entrega")
        await c.send("hmm deixa eu pensar")
        cart = await c.get_cart_items()
        exp_name = sc["checkout_product_expected"][0]
        found = [n for n, q in cart if exp_name.lower() in n.lower()]
        assert found, f"Checkout destroyed! '{exp_name}' not in cart: {cart}"


class TestRandGreeting:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_greeting(self, db_check, label):
        sc = _sc(label)
        c = await _ctx(label)
        msg = sc.get("greeting_msg", "oi, boa noite")
        response = await c.send(msg)
        cart = await c.get_cart_items()
        assert len(cart) == 0, f"Greeting added to cart: {cart}"
        assert response is not None


class TestRandAbbreviation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_abbreviation(self, db_check, label):
        """Order by abbreviated product name — should match the full product."""
        sc = _sc(label)
        if not sc.get("abbreviation_msg"):
            pytest.skip("No abbreviation scenario")
        c = await _ctx(label)
        await c.send("oi")
        await c.send(sc["abbreviation_msg"])
        cart = await c.get_cart_items()
        exp_name = sc["abbreviation_product"]
        assert len(cart) >= 1, f"Abbreviation didn't add anything: {cart}"
        found = [n for n, q in cart if exp_name.lower() in n.lower()]
        assert found, f"'{exp_name}' not in cart (ordered by abbreviation): {cart}"


class TestRandDoubleAdd:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_double_add(self, db_check, label):
        """Adding the same product twice should increase quantity, not duplicate."""
        sc = _sc(label)
        if not sc.get("double_add_msg_1"):
            pytest.skip("No double add scenario")
        c = await _ctx(label)
        await c.send("oi")
        await c.send(sc["double_add_msg_1"])
        cart1 = await c.get_cart_items()
        exp_name = sc["double_add_product"]
        found1 = [(n, q) for n, q in cart1 if exp_name.lower() in n.lower()]
        assert found1, f"First add failed: '{exp_name}' not in cart: {cart1}"

        await c.send(sc["double_add_msg_2"])
        cart2 = await c.get_cart_items()
        found2 = [(n, q) for n, q in cart2 if exp_name.lower() in n.lower()]
        assert found2, f"Second add lost product: '{exp_name}' not in cart: {cart2}"
        assert found2[0][1] >= 2, f"Qty didn't increase: {found2}"


class TestRandQuestion:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_question_no_cart(self, db_check, label):
        """Asking a question about a product should NOT add it to cart."""
        sc = _sc(label)
        if not sc.get("question_msg"):
            pytest.skip("No question scenario")
        c = await _ctx(label)
        await c.send("oi")
        await c.send(sc["question_msg"])
        cart = await c.get_cart_items()
        assert len(cart) == 0, f"Question added to cart: {cart}"


class TestRandAddRemove:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_add_then_remove(self, db_check, label):
        """Add a product then remove it — cart should be empty."""
        sc = _sc(label)
        if not sc.get("add_remove_add_msg"):
            pytest.skip("No add-remove scenario")
        c = await _ctx(label)
        await c.send("oi")
        await c.send(sc["add_remove_add_msg"])
        cart1 = await c.get_cart_items()
        exp_name = sc["add_remove_product"]
        assert len(cart1) >= 1, f"Add failed: {cart1}"

        await c.send(sc["add_remove_remove_msg"])
        cart2 = await c.get_cart_items()
        still = [n for n, q in cart2 if exp_name.lower() in n.lower()]
        assert not still, f"'{exp_name}' still in cart after remove: {cart2}"


# ---------------------------------------------------------------------------
# P1 Day 1.5 — new scenarios
#
# These test classes were added to close measurement gaps that the headline
# 87.2% / 84.4% baseline was hiding. Each is intentionally a separate class
# (not a parametrized method) so the report parser in run_corpus_qa.py maps
# every variant to its own scenario column in the frontend matrix.
# ---------------------------------------------------------------------------


class TestRandSuggestionTrapQuestion:
    """Bot offers suggestions, customer asks a price question.

    Today the suggestion handler may misread the question as a numbered
    selection. Expected: bot answers the question, cart stays empty.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_trap_question(self, db_check, label):
        sc = _sc(label)
        if not sc.get("trap_question_msg"):
            pytest.skip("No trap_question scenario")
        c = await _ctx(label)
        await c.send("oi")
        # Step 1: trigger suggestion handler — populates last_suggestions
        await c.send(sc["suggestion_msg"])
        # Step 2: send a price question — should answer, NOT add anything
        await c.send(sc["trap_question_msg"])
        cart = await c.get_cart_items()
        assert len(cart) == 0, f"Trap question added to cart: {cart}"


class TestRandSuggestionTrapClear:
    """Bot offers suggestions while cart has items, customer says 'limpa tudo'.

    Today CLEAR has no pre-router guard, so the suggestion handler sees the
    message first. If it falls through cleanly, the LLM should call clear_cart.
    Expected: cart becomes empty.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_trap_clear(self, db_check, label):
        sc = _sc(label)
        if not sc.get("add_single_msg"):
            pytest.skip("No add_single scenario")
        c = await _ctx(label)
        await c.send("oi")
        # Step 1: add an item so CLEAR has something to do
        await c.send(sc["add_single_msg"])
        cart_before = await c.get_cart_items()
        assert len(cart_before) >= 1, f"Setup add failed: {cart_before}"
        # Step 2: trigger suggestion handler
        await c.send(sc["suggestion_msg"])
        # Step 3: clear command
        await c.send("limpa tudo")
        cart_after = await c.get_cart_items()
        assert len(cart_after) == 0, f"Cart not cleared: {cart_after}"


class TestRandSuggestionTrapUnrelatedAdd:
    """Bot offers suggestions, customer adds a product NOT in the suggested
    list. Expected: the product is added (no ordinal misread).

    The runner picks a drink for this test because the suggestion filter
    excludes drinks — so the unrelated_add_product is guaranteed to not be
    in the bot's suggestion list.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_trap_unrelated_add(self, db_check, label):
        sc = _sc(label)
        if not sc.get("trap_unrelated_add_msg"):
            pytest.skip("No trap_unrelated_add scenario")
        c = await _ctx(label)
        await c.send("oi")
        # Step 1: trigger suggestion handler
        await c.send(sc["suggestion_msg"])
        # Step 2: explicit add for an unrelated product
        await c.send(sc["trap_unrelated_add_msg"])
        cart = await c.get_cart_items()
        exp_name = sc["trap_unrelated_add_product"]
        found = [n for n, q in cart if exp_name.lower() in n.lower()]
        assert found, f"'{exp_name}' not in cart after unrelated add: {cart}"


class TestRandSuggestionTrapFinish:
    """Regression: Option E + Option H, shipped 2026-04-07.

    Cart has items, bot offers a suggestion list, customer says
    'to com fome vamo finalizar pode mandar'. Expected: FINISH path runs,
    cart preserved, bot asks how to deliver.

    Day 1.5 finding F1: previous version of this test sent
    'to com fome vamo fechar pode mandar' which never matched
    `_FINISH_KEYWORD_RE` because the regex requires `fechar` to be followed by
    `pedido` / `a conta`. Bare `fechar` was not in the regex. The Option E + H
    fix shipped 2026-04-07 was specifically for slang containing `finalizar`,
    so the regression test now uses that variant. F1b separately extends
    `_FINISH_KEYWORD_RE` to also accept `vamo fechar` etc.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_trap_finish(self, db_check, label):
        sc = _sc(label)
        if not sc.get("add_single_msg"):
            pytest.skip("No add_single scenario")
        c = await _ctx(label)
        await c.send("oi")
        # Step 1: add an item so FINISH has something to finalize
        await c.send(sc["add_single_msg"])
        # Step 2: trigger suggestion handler
        await c.send(sc["suggestion_msg"])
        # Step 3: slang FINISH while in suggestion-trap state
        response = await c.send("to com fome vamo finalizar pode mandar")
        cart = await c.get_cart_items()
        exp_name = sc["add_single_expected"][0][0]
        # Original item must survive
        still = [n for n, q in cart if exp_name.lower() in n.lower()]
        assert still, f"FINISH wiped the cart item '{exp_name}': {cart}"
        # Bot must have asked the delivery question — proof FINISH ran
        assert response is not None, "No bot response captured"
        rl = response.lower()
        assert any(
            w in rl
            for w in (
                "entrega",
                "retirada",
                "delivery",
                "buscar",
                "endereço",
                "endereco",
                "como você quer",
                "como voce quer",
                "como deseja",
                "quer que seja",
                "receber",
                "vai querer",
            )
        ), f"FINISH didn't trigger delivery question: {response[:200]}"


class TestRandRemoveSubsetByName:
    """Add 2 items, remove 1 by name. Cart should keep the other untouched.

    Today's remove tests only ever go from [X] → []. This catches the case
    where a remove call drops the wrong item or cascades into the rest of
    the cart.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_subset_remove(self, db_check, label):
        sc = _sc(label)
        if not sc.get("subset_remove_msg") or not sc.get("add_multi_msg"):
            pytest.skip("No subset_remove scenario")
        c = await _ctx(label)
        await c.send("oi")
        # Setup: add multiple products
        await c.send(sc["add_multi_msg"])
        cart_before = await c.get_cart_items()
        expected = sc["add_multi_expected"]
        if len(cart_before) < len(expected):
            pytest.skip(
                f"Setup add_multi didn't seed enough items "
                f"(got {len(cart_before)}, need {len(expected)})"
            )
        # Remove one by name
        await c.send(sc["subset_remove_msg"])
        cart_after = await c.get_cart_items()
        # The removed product must be gone
        removed_name = sc["subset_remove_target_product"]
        gone = [n for n, q in cart_after if removed_name.lower() in n.lower()]
        assert not gone, (
            f"'{removed_name}' still in cart after subset remove: {cart_after}"
        )
        # The keeper must still be there at original quantity
        keeper_name, keeper_qty = sc["subset_remove_keeper"]
        keeper = [(n, q) for n, q in cart_after if keeper_name.lower() in n.lower()]
        assert keeper, f"Subset remove wiped keeper '{keeper_name}': {cart_after}"
        assert keeper[0][1] == keeper_qty, (
            f"Subset remove changed keeper qty: expected {keeper_qty}, got {keeper[0][1]}"
        )


class TestRandRemoveMultipleAtOnce:
    """Add 3 items, remove 2 in one message. Cart should keep exactly the
    third item. Tests multi-target removal in a single tool call."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_multi_remove(self, db_check, label):
        sc = _sc(label)
        if not sc.get("multi_remove_setup_msg"):
            pytest.skip("No multi_remove scenario")
        c = await _ctx(label)
        await c.send("oi")
        # Setup: add 3 distinct items
        await c.send(sc["multi_remove_setup_msg"])
        cart_before = await c.get_cart_items()
        if len(cart_before) < 3:
            pytest.skip(f"Setup couldn't seed 3 items (got {len(cart_before)})")
        # Remove two of them in one message
        await c.send(sc["multi_remove_msg"])
        cart_after = await c.get_cart_items()
        # The two removed must be gone
        for removed_name in sc["multi_remove_targets"]:
            gone = [n for n, q in cart_after if removed_name.lower() in n.lower()]
            assert not gone, (
                f"'{removed_name}' still in cart after multi remove: {cart_after}"
            )
        # The keeper must still be there
        keeper_name = sc["multi_remove_keeper"]
        keeper = [n for n, q in cart_after if keeper_name.lower() in n.lower()]
        assert keeper, f"Multi remove wiped keeper '{keeper_name}': {cart_after}"


class TestRandQtyReduction:
    """Add X×3 + Y×2, then 'deixa só 1 X'. Expected: X×1, Y untouched.

    Almost certainly fails today — remove_from_cart is suspected to be a
    line-removal operation, not a partial-quantity reduction. The failure
    is the point: it surfaces a tool-shape gap, not a regex gap, and it
    informs the design of P2.5 (item swap) which has the same shape.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_qty_reduction(self, db_check, label):
        sc = _sc(label)
        if not sc.get("qty_reduce_setup_msg"):
            pytest.skip("No qty_reduce scenario")
        c = await _ctx(label)
        await c.send("oi")
        # Setup: add X×3 + Y×2
        await c.send(sc["qty_reduce_setup_msg"])
        cart_before = await c.get_cart_items()
        if len(cart_before) < 2:
            pytest.skip(f"Setup didn't seed both products (got {cart_before})")
        # Reduce X to 1
        await c.send(sc["qty_reduce_msg"])
        cart_after = await c.get_cart_items()
        # Both products must still be present
        for exp_name, exp_qty in sc["qty_reduce_expected"]:
            found = [(n, q) for n, q in cart_after if exp_name.lower() in n.lower()]
            assert found, f"Qty reduce lost '{exp_name}': {cart_after}"
            assert found[0][1] == exp_qty, (
                f"Qty reduce wrong qty for '{exp_name}': "
                f"expected {exp_qty}, got {found[0][1]}"
            )


class TestRandMultiTurnFlow:
    """End-to-end happy path covering ~80% of a real customer interaction.

    greeting → suggest → add → continue → question (no add) → remove →
    finalize → entrega. Catches integration regressions that single-turn
    scenarios miss.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_multiturn_flow(self, db_check, label):
        sc = _sc(label)
        required = (
            "add_single_msg",
            "continuation_followup_msg",
            "question_msg",
            "suggestion_msg",
        )
        if not all(sc.get(k) for k in required):
            pytest.skip("Multi-turn flow needs full scenario data")
        c = await _ctx(label)

        # 1. Greeting
        await c.send("oi")

        # 2. Ask for suggestions (do not select)
        await c.send(sc["suggestion_msg"])
        cart = await c.get_cart_items()
        assert len(cart) == 0, f"Step 2 (suggest) added to cart: {cart}"

        # 3. Add a product
        await c.send(sc["add_single_msg"])
        cart = await c.get_cart_items()
        single_name = sc["add_single_expected"][0][0]
        assert any(single_name.lower() in n.lower() for n, q in cart), (
            f"Step 3 (add) failed: {single_name} not in {cart}"
        )

        # 4. Continuation: tag on a second product
        await c.send(sc["continuation_followup_msg"])
        cart = await c.get_cart_items()
        cont_name = sc["continuation_followup_product"]
        assert any(cont_name.lower() in n.lower() for n, q in cart), (
            f"Step 4 (continuation) failed: {cont_name} not in {cart}"
        )
        cart_size_after_cont = len(cart)

        # 5. Ask a price question — must NOT add to cart
        await c.send(sc["question_msg"])
        cart = await c.get_cart_items()
        assert len(cart) == cart_size_after_cont, (
            f"Step 5 (question) added to cart: {cart}"
        )

        # 6. Remove the continuation item by name
        await c.send(f"tira o {cont_name.lower()}")
        cart = await c.get_cart_items()
        still = [n for n, q in cart if cont_name.lower() in n.lower()]
        assert not still, f"Step 6 (remove) failed: '{cont_name}' still in {cart}"

        # 7. Finalize — bot should ask delivery method
        response = await c.send("finalizar")
        assert response is not None, "Step 7: no bot response"
        rl = response.lower()
        assert any(
            w in rl
            for w in (
                "entrega",
                "retirada",
                "delivery",
                "buscar",
                "endereço",
                "endereco",
                "como você quer",
                "como voce quer",
                "como deseja",
                "quer que seja",
                "receber",
                "vai querer",
            )
        ), f"Step 7 (finalize) didn't ask delivery: {response[:200]}"

        # 8. Pick delivery
        await c.send("entrega")

        # 9. Original item still in cart at the end of the flow
        cart = await c.get_cart_items()
        assert any(single_name.lower() in n.lower() for n, q in cart), (
            f"Step 9: original item '{single_name}' lost during checkout flow: {cart}"
        )


class TestRandQuantityInProductName:
    """Order a product whose name contains a digit (e.g., 'Casquinha 3 bolas',
    'Pizza 4 queijos'). Today the extractor misreads the digit as a quantity:
    'um casquinha 3 bolas' → (1, casquinha) + (3, bolas).

    Skipped if no product in the bot's menu has a digit in its name. When the
    runner finds one, the test runs and surfaces the known P0 extractor bug.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x)
    )
    async def test_digit_in_name(self, db_check, label):
        sc = _sc(label)
        if not sc.get("digit_in_name_msg"):
            pytest.skip("No product with digit in name available")
        c = await _ctx(label)
        await c.send("oi")
        await c.send(sc["digit_in_name_msg"])
        cart = await c.get_cart_items()
        exp_name = sc["digit_in_name_product"]
        # The product must be in the cart with quantity 1 (not split into
        # qty=N + bogus name fragment)
        found = [(n, q) for n, q in cart if exp_name.lower() in n.lower()]
        assert found, f"'{exp_name}' not in cart: {cart}"
        assert found[0][1] == 1, (
            f"Quantity for '{exp_name}' wrong: expected 1, got {found[0][1]} — "
            f"likely the extractor read a digit in the name as quantity. Cart: {cart}"
        )
        # Cart should have exactly one line — anything else means the
        # extractor split the product name into a phantom second item
        assert len(cart) == 1, (
            f"Expected exactly 1 cart line, got {len(cart)} — extractor likely "
            f"split a digit-in-name product. Cart: {cart}"
        )
