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
            "entry": [{"changes": [{"value": {
                "metadata": {
                    "phone_number_id": self.phone_number_id,
                    "display_phone_number": self.contact_phone,
                },
                "messages": [{
                    "from": self.contact_phone,
                    "id": f"wamid.rd{uuid.uuid4().hex[:12]}",
                    "text": {"body": text_body},
                    "type": "text",
                }],
            }}]}],
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
                select(CartItem).where(CartItem.cart_id == cart.id).order_by(CartItem.id)
            )
            result = []
            for item in ir.scalars().all():
                pr = await fresh.execute(select(Product).where(Product.id == item.product_id))
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
        pytest.skip("No test data. Run: python -m tests.simulation.random_restaurant_runner")
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
    @pytest.mark.parametrize("label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x))
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
    @pytest.mark.parametrize("label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x))
    async def test_add_multi(self, db_check, label):
        c = await _ctx(label)
        sc = _sc(label)
        await c.send("oi")
        await c.send(sc["add_multi_msg"])
        cart = await c.get_cart_items()
        expected = sc["add_multi_expected"]
        assert len(cart) >= len(expected), f"Expected >= {len(expected)} items, got {cart}"
        for exp_name, exp_qty in expected:
            found = [(n, q) for n, q in cart if exp_name.lower() in n.lower()]
            assert found, f"'{exp_name}' not in cart: {cart}"
            assert found[0][1] == exp_qty


class TestRandUnavailable:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x))
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
        assert any(w in rl for w in ("falta", "indisponivel", "indisponível", "disponivel", "disponível")), (
            f"No em falta: {response[:200]}"
        )


class TestRandRemove:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x))
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
    @pytest.mark.parametrize("label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x))
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
    @pytest.mark.parametrize("label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x))
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
    @pytest.mark.parametrize("label", _LABELS, ids=lambda x: _TEST_DATA.get(x, {}).get("name", x))
    async def test_greeting(self, db_check, label):
        c = await _ctx(label)
        response = await c.send("oi, boa noite")
        cart = await c.get_cart_items()
        assert len(cart) == 0, f"Greeting added to cart: {cart}"
        assert response is not None
