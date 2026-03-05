# tests/test_prompt_central.py
"""
Tests for create_central_prompt in app/prompt_central.py.

The function is pure (no I/O, no DB, no async) so no fixtures or mocks for
external services are needed — only MagicMock Product objects.

Covered:
- system prompt contains the restaurant name
- user query appears as the last message
- conversation history is included in the prompt
- cart items are rendered as "NxName" inside the system message
- search_results (RAG) are rendered inside the system message
- empty cart displays an "empty" indicator
- absent search_results displays a "no items found" indicator
"""

from unittest.mock import MagicMock

from app.prompt_central import create_central_prompt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_product(product_id: int, name: str, description: str = "") -> MagicMock:
    """Create a lightweight Product mock with the attributes read by create_central_prompt."""
    p = MagicMock()
    p.id = product_id
    p.name = name
    p.description = description
    return p


def _system_content(messages: list) -> str:
    """Return the content of the first (system) message."""
    return messages[0]["content"]


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestCreateCentralPrompt:
    # 1 -----------------------------------------------------------------------
    def test_system_prompt_contains_restaurant_name(self):
        """The first message must be the system role and include the restaurant name."""
        messages = create_central_prompt(
            user_query="oi",
            history=[],
            restaurant_name="Pizza Palace",
            cart_items=[],
        )

        first = messages[0]
        assert first["role"] == "system"
        assert "Pizza Palace" in first["content"]

    # 2 -----------------------------------------------------------------------
    def test_user_query_in_last_message(self):
        """The final message must be role='user' with the exact query text."""
        messages = create_central_prompt(
            user_query="quero pizza",
            history=[],
            restaurant_name="Restaurante Teste",
            cart_items=[],
        )

        last = messages[-1]
        assert last["role"] == "user"
        assert "quero pizza" in last["content"]

    # 3 -----------------------------------------------------------------------
    def test_history_included(self):
        """Conversation history messages must appear somewhere in the returned list."""
        history = [
            {"role": "user", "content": "oi"},
            {"role": "assistant", "content": "olá"},
        ]
        messages = create_central_prompt(
            user_query="quero algo",
            history=history,
            restaurant_name="Restaurante Teste",
            cart_items=[],
        )

        # Collect all (role, content) pairs from the prompt
        pairs = [(m.get("role"), m.get("content")) for m in messages]

        assert ("user", "oi") in pairs
        assert ("assistant", "olá") in pairs

    # 4 -----------------------------------------------------------------------
    def test_cart_items_in_system_prompt(self):
        """Cart items must be rendered as 'NxName' inside the system message."""
        cart_items = [{"quantity": 2, "name": "Pizza", "product_id": 1}]
        messages = create_central_prompt(
            user_query="ver carrinho",
            history=[],
            restaurant_name="Restaurante Teste",
            cart_items=cart_items,
        )

        assert "2x Pizza" in _system_content(messages)

    # 5 -----------------------------------------------------------------------
    def test_search_results_in_system_prompt(self):
        """Product names from search_results (RAG) must appear in the system message."""
        products = [
            _make_product(product_id=5, name="Coca-Cola", description="Refrigerante")
        ]
        messages = create_central_prompt(
            user_query="quero uma coca",
            history=[],
            restaurant_name="Restaurante Teste",
            cart_items=[],
            search_results=products,
        )

        system = _system_content(messages)
        assert "Coca-Cola" in system

    # 6 -----------------------------------------------------------------------
    def test_empty_cart_shows_empty_message(self):
        """When cart_items is empty the system message must mention 'vazio'."""
        messages = create_central_prompt(
            user_query="ver carrinho",
            history=[],
            restaurant_name="Restaurante Teste",
            cart_items=[],
        )

        assert "vazio" in _system_content(messages).lower()

    # 7 -----------------------------------------------------------------------
    # 8 -----------------------------------------------------------------------
    def test_menu_context_capped_at_15_items(self):
        """When more than 15 products are passed, only the first 15 appear in the system message."""
        products = [
            _make_product(product_id=i, name=f"Product_{i}", description=f"Desc {i}")
            for i in range(30)
        ]
        messages = create_central_prompt(
            user_query="quero algo",
            history=[],
            restaurant_name="Restaurante Teste",
            cart_items=[],
            search_results=products,
        )
        system = _system_content(messages)
        for i in range(15):
            assert f"Product_{i}" in system
        for i in range(15, 30):
            assert f"Product_{i}" not in system

    # 9 -----------------------------------------------------------------------
    def test_description_truncated_at_80_chars(self):
        """Product descriptions longer than 80 chars should be truncated."""
        long_desc = "A" * 200
        products = [_make_product(product_id=1, name="Pizza", description=long_desc)]
        messages = create_central_prompt(
            user_query="quero pizza",
            history=[],
            restaurant_name="Restaurante Teste",
            cart_items=[],
            search_results=products,
        )
        system = _system_content(messages)
        assert long_desc not in system
        assert "A" * 80 in system

    # 7 -----------------------------------------------------------------------
    def test_empty_search_results_shows_no_items_message(self):
        """When search_results is not provided the system message must mention 'Nenhum item'."""
        messages = create_central_prompt(
            user_query="quero algo",
            history=[],
            restaurant_name="Restaurante Teste",
            cart_items=[],
            search_results=None,
        )

        assert "Nenhum item" in _system_content(messages)


# ---------------------------------------------------------------------------
# Phantom tool removal tests
# ---------------------------------------------------------------------------


class TestPhantomToolsRemoved:
    PHANTOM_NAMES = {
        "request_customer_address",
        "process_order_with_address",
        "process_payment_choice",
    }

    def test_phantom_tools_absent_from_schema(self):
        """Phantom tool names must not appear in the tools_schema list."""
        from app.tools_definition import tools_schema

        schema_names = {t["function"]["name"] for t in tools_schema}
        assert self.PHANTOM_NAMES.isdisjoint(schema_names), (
            f"Phantom tools still in schema: {self.PHANTOM_NAMES & schema_names}"
        )

    def test_phantom_tools_absent_from_prompt(self):
        """Phantom tool names must not appear anywhere in the assembled prompt."""
        messages = create_central_prompt(
            user_query="oi",
            history=[],
            restaurant_name="Test",
            cart_items=[],
        )
        full_text = " ".join(
            m.get("content") or ""
            for m in messages
            if isinstance(m.get("content"), str)
        )
        for name in self.PHANTOM_NAMES:
            assert name not in full_text, f"Phantom tool '{name}' found in prompt"
