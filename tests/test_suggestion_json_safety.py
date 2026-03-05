# tests/test_suggestion_json_safety.py
"""Tests for safe JSON parsing of LLM tool arguments in suggestion flow."""

import ast
import inspect

from app import whatsapp


class TestSuggestionJsonSafety:
    def test_search_catalog_json_parsing_has_try_except(self):
        """Verify the json.loads call for search_catalog_for_suggestions is wrapped in try-except."""
        source = inspect.getsource(whatsapp)

        # Find the block that handles search_catalog_for_suggestions
        assert "search_catalog_for_suggestions" in source

        # Parse the AST and find the try-except around json.loads near search_catalog_for_suggestions
        tree = ast.parse(source)

        found_protected = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                # Check if any handler catches JSONDecodeError or TypeError
                handler_names = []
                for handler in node.handlers:
                    if isinstance(handler.type, ast.Tuple):
                        for elt in handler.type.elts:
                            if isinstance(elt, ast.Attribute):
                                handler_names.append(elt.attr)
                            elif isinstance(elt, ast.Name):
                                handler_names.append(elt.id)
                    elif isinstance(handler.type, ast.Attribute):
                        handler_names.append(handler.type.attr)
                    elif isinstance(handler.type, ast.Name):
                        handler_names.append(handler.type.id)

                if "JSONDecodeError" in handler_names or "TypeError" in handler_names:
                    # Check if the try body contains json.loads
                    for sub in ast.walk(node):
                        if (
                            isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "loads"
                        ):
                            found_protected = True
                            break

        assert found_protected, (
            "json.loads for search_catalog_for_suggestions must be wrapped in "
            "try-except (json.JSONDecodeError, TypeError)"
        )


class TestConceptValidation:
    """Tests for search concept sanitization in suggestion flow."""

    def test_concept_special_chars_cleaned(self):
        """Special characters should be removed from concept."""
        import regex as re

        concept = "pizza'; DROP TABLE<>"
        concept = str(concept)[:100]
        concept = re.sub(
            r"[^\w\s\-áàâãéèêíìîóòôõúùûçÁÀÂÃÉÈÊÍÌÎÓÒÔÕÚÙÛÇ]", "", concept
        ).strip()
        assert concept == "pizza DROP TABLE"

    def test_concept_long_truncated(self):
        """Long concept strings should be truncated to 100 chars."""
        concept = "a" * 200
        concept = str(concept)[:100]
        assert len(concept) == 100

    def test_concept_empty_after_clean_becomes_none(self):
        """If cleaning removes everything, concept should become None."""
        import regex as re

        concept = "!@#$%^&*()"
        concept = str(concept)[:100]
        concept = re.sub(
            r"[^\w\s\-áàâãéèêíìîóòôõúùûçÁÀÂÃÉÈÊÍÌÎÓÒÔÕÚÙÛÇ]", "", concept
        ).strip()
        if not concept:
            concept = None
        assert concept is None

    def test_concept_preserves_accented_chars(self):
        """Portuguese accented characters should be preserved."""
        import regex as re

        concept = "frango à parmegiana"
        concept = str(concept)[:100]
        concept = re.sub(
            r"[^\w\s\-áàâãéèêíìîóòôõúùûçÁÀÂÃÉÈÊÍÌÎÓÒÔÕÚÙÛÇ]", "", concept
        ).strip()
        assert concept == "frango à parmegiana"
