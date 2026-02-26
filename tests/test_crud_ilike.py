"""Tests for the escape_ilike helper in crud.py."""
from app.crud import escape_ilike


class TestEscapeIlike:
    def test_escapes_percent(self):
        assert escape_ilike("100%") == "100\\%"

    def test_escapes_underscore(self):
        assert escape_ilike("a_b") == "a\\_b"

    def test_escapes_backslash(self):
        assert escape_ilike("a\\b") == "a\\\\b"

    def test_normal_string_unchanged(self):
        assert escape_ilike("pizza") == "pizza"

    def test_all_special_chars(self):
        assert escape_ilike("%_\\") == "\\%\\_\\\\"

    def test_empty_string(self):
        assert escape_ilike("") == ""

    def test_mixed_content(self):
        assert escape_ilike("50% off_sale") == "50\\% off\\_sale"
