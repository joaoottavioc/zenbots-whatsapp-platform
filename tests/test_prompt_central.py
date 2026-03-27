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


def _static_system_content(messages: list) -> str:
    """Return the content of the first (static) system message."""
    return messages[0]["content"]


def _all_system_content(messages: list) -> str:
    """Return the combined content of all system messages."""
    return " ".join(m["content"] for m in messages if m.get("role") == "system")


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

        assert "2x Pizza" in _all_system_content(messages)

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

        system = _all_system_content(messages)
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

        assert "vazio" in _all_system_content(messages).lower()

    # 7 -----------------------------------------------------------------------
    # 8 -----------------------------------------------------------------------
    def test_menu_context_includes_all_items(self):
        """All products are included in the system message (no cap)."""
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
        system = _all_system_content(messages)
        for i in range(30):
            assert f"Product_{i}" in system

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
        system = _all_system_content(messages)
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

        assert "Nenhum item" in _all_system_content(messages)


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


# ---------------------------------------------------------------------------
# Phase 1 Token Reduction Tests
# ---------------------------------------------------------------------------


class TestPrefixCachingStructure:
    """T1-1: Verify prompt is structured for OpenAI automatic prefix caching."""

    def test_two_system_messages_present(self):
        """Prompt must contain exactly two system messages (static + dynamic)."""
        messages = create_central_prompt(
            user_query="oi",
            history=[],
            restaurant_name="Test",
            cart_items=[],
        )
        system_msgs = [m for m in messages if m.get("role") == "system"]
        assert len(system_msgs) == 2

    def test_static_system_before_examples(self):
        """Static system message must come before any example messages."""
        messages = create_central_prompt(
            user_query="oi",
            history=[],
            restaurant_name="Test",
            cart_items=[],
        )
        assert messages[0]["role"] == "system"
        # Second message should be the first example (user role)
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "quero 2 pizzas marguerita e 1 coca-cola"

    def test_dynamic_system_after_examples_before_history(self):
        """Dynamic system message must come after examples, before history and user query."""
        history = [
            {"role": "user", "content": "oi"},
            {"role": "assistant", "content": "ola"},
        ]
        messages = create_central_prompt(
            user_query="quero pizza",
            history=history,
            restaurant_name="Test",
            cart_items=[{"quantity": 1, "name": "Coca", "product_id": 1}],
        )
        # Find the second system message
        system_indices = [
            i for i, m in enumerate(messages) if m.get("role") == "system"
        ]
        assert len(system_indices) == 2
        dynamic_idx = system_indices[1]

        # Dynamic system should contain cart context
        assert "Coca" in messages[dynamic_idx]["content"]

        # History and user query should come after dynamic system
        last_msg = messages[-1]
        assert last_msg["role"] == "user"
        assert last_msg["content"] == "quero pizza"

        # History messages should be between dynamic system and user query
        history_start = dynamic_idx + 1
        assert messages[history_start]["content"] == "oi"

    def test_static_system_does_not_contain_dynamic_context(self):
        """Static system message must NOT contain menu/cart/suggestion context."""
        cart_items = [{"quantity": 2, "name": "Pizza", "product_id": 1}]
        products = [_make_product(product_id=5, name="Coca-Cola")]
        messages = create_central_prompt(
            user_query="oi",
            history=[],
            restaurant_name="Test",
            cart_items=cart_items,
            search_results=products,
        )
        static = messages[0]["content"]
        assert "2x Pizza" not in static
        assert "Coca-Cola" not in static

    def test_dynamic_system_contains_cart_and_menu(self):
        """Dynamic system message must contain menu and cart context."""
        cart_items = [{"quantity": 2, "name": "Pizza", "product_id": 1}]
        products = [_make_product(product_id=5, name="Coca-Cola")]
        messages = create_central_prompt(
            user_query="oi",
            history=[],
            restaurant_name="Test",
            cart_items=cart_items,
            search_results=products,
        )
        system_msgs = [m for m in messages if m.get("role") == "system"]
        dynamic = system_msgs[1]["content"]
        assert "2x Pizza" in dynamic
        assert "Coca-Cola" in dynamic

    def test_static_prefix_identical_across_requests(self):
        """Static system + examples prefix must be identical regardless of dynamic context."""
        messages_a = create_central_prompt(
            user_query="quero pizza",
            history=[],
            restaurant_name="Test",
            cart_items=[{"quantity": 1, "name": "Coca", "product_id": 1}],
        )
        messages_b = create_central_prompt(
            user_query="quero sushi",
            history=[{"role": "user", "content": "oi"}],
            restaurant_name="Test",
            cart_items=[{"quantity": 5, "name": "Cerveja", "product_id": 2}],
            search_results=[_make_product(99, "Sashimi")],
        )
        # Find second system message index
        idx_a = next(
            i for i, m in enumerate(messages_a) if m.get("role") == "system" and i > 0
        )
        idx_b = next(
            i for i, m in enumerate(messages_b) if m.get("role") == "system" and i > 0
        )

        # Everything before the dynamic system message should be identical
        prefix_a = messages_a[:idx_a]
        prefix_b = messages_b[:idx_b]
        assert prefix_a == prefix_b


class TestReducedExamples:
    """T1-2: Verify example count reduced from 13 to 10."""

    def test_example_count_is_12(self):
        """Prompt must contain exactly 12 example groups (36 messages: user+assistant+tool each)."""
        messages = create_central_prompt(
            user_query="oi",
            history=[],
            restaurant_name="Test",
            cart_items=[],
        )
        # Count example groups: triplets of (user, assistant, tool) between system messages
        system_indices = [
            i for i, m in enumerate(messages) if m.get("role") == "system"
        ]
        example_msgs = messages[system_indices[0] + 1 : system_indices[1]]
        # Each example is a triplet (user, assistant, tool)
        assert len(example_msgs) == 36  # 12 examples * 3 messages each

    def test_removed_examples_not_present(self):
        """Removed examples (ex7, ex_ordinals, ex_notes_simple) must not appear."""
        messages = create_central_prompt(
            user_query="oi",
            history=[],
            restaurant_name="Test",
            cart_items=[],
        )
        all_content = " ".join(
            m.get("content") or ""
            for m in messages
            if isinstance(m.get("content"), str)
        )
        # ex7 user message
        assert "quero cinco do primeiro e tr\u00eas do segundo" not in all_content
        # ex_ordinals user message
        assert "quero 2 do segundo e 7 do quarto" not in all_content
        # ex_notes_simple user message
        assert "Quero um X-Salada sem tomate" not in all_content

    def test_kept_examples_cover_all_tools(self):
        """Every tool must still have at least one example."""
        messages = create_central_prompt(
            user_query="oi",
            history=[],
            restaurant_name="Test",
            cart_items=[],
        )
        tool_names_in_examples = set()
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    tool_names_in_examples.add(tc["function"]["name"])

        expected_tools = {
            "add_items_to_cart",
            "remove_items_from_cart",
            "modify_item_quantity",
            "bulk_modify_quantities",
            "answer_conversationally",
            "answer_with_found_products",
            "search_catalog_for_suggestions",
            "update_item_observation",
        }
        assert expected_tools.issubset(tool_names_in_examples), (
            f"Missing tools in examples: {expected_tools - tool_names_in_examples}"
        )

    def test_ordinal_extenso_example_still_present(self):
        """The comprehensive ordinal+extenso example must still be present."""
        messages = create_central_prompt(
            user_query="oi",
            history=[],
            restaurant_name="Test",
            cart_items=[],
        )
        all_content = " ".join(
            m.get("content") or ""
            for m in messages
            if isinstance(m.get("content"), str)
        )
        assert "quero treze do terceiro e quinze do primeiro" in all_content

    def test_notes_complex_example_still_present(self):
        """The complex notes example must still be present (subsumes simple notes)."""
        messages = create_central_prompt(
            user_query="oi",
            history=[],
            restaurant_name="Test",
            cart_items=[],
        )
        all_content = " ".join(
            m.get("content") or ""
            for m in messages
            if isinstance(m.get("content"), str)
        )
        assert "X-Tudo" in all_content


class TestTrimmedToolDescriptions:
    """T1-3: Verify tool descriptions are concise."""

    def test_all_descriptions_under_80_chars(self):
        """Every tool description must be under 80 characters."""
        from app.tools_definition import tools_schema

        for tool in tools_schema:
            desc = tool["function"]["description"]
            name = tool["function"]["name"]
            assert len(desc) <= 80, (
                f"Tool {name!r} description too long ({len(desc)} chars): {desc}"
            )

    def test_tool_names_unchanged(self):
        """Tool names must not change (they are referenced in code)."""
        from app.tools_definition import tools_schema

        expected = {
            "add_items_to_cart",
            "answer_conversationally",
            "remove_items_from_cart",
            "modify_item_quantity",
            "propose_and_confirm_action",
            "answer_with_found_products",
            "bulk_modify_quantities",
            "search_catalog_for_suggestions",
            "update_item_observation",
        }
        actual = {t["function"]["name"] for t in tools_schema}
        assert actual == expected

    def test_required_fields_unchanged(self):
        """Required parameters for each tool must not change."""
        from app.tools_definition import tools_schema

        expected_required = {
            "add_items_to_cart": ["items"],
            "answer_conversationally": ["response_text"],
            "remove_items_from_cart": ["product_ids"],
            "modify_item_quantity": ["product_id", "new_quantity"],
            "propose_and_confirm_action": ["confirmation_question", "proposed_action"],
            "answer_with_found_products": ["product_names"],
            "bulk_modify_quantities": ["updates"],
            "search_catalog_for_suggestions": ["search_concept"],
            "update_item_observation": ["product_id", "notes"],
        }
        for tool in tools_schema:
            name = tool["function"]["name"]
            required = tool["function"]["parameters"]["required"]
            assert sorted(required) == sorted(expected_required[name]), (
                f"Required fields changed for {name}: {required}"
            )

    def test_schema_count_unchanged(self):
        """Number of tools must remain 9."""
        from app.tools_definition import tools_schema

        assert len(tools_schema) == 9
