"""Tests for the PDF text-quality heuristic in app/menu_extraction.py."""

from app.menu_extraction import _is_text_quality_sufficient


def test_quality_ok_on_linear_single_column_menu():
    """Typical single-column menu where price sits on the same line as the name."""
    text = "\n".join(
        [
            "Pizza Margherita - R$ 25,00",
            "Molho de tomate, mussarela e manjericao fresco",
            "Pizza Pepperoni - R$ 30,00",
            "Molho de tomate, mussarela e pepperoni",
            "Pizza Calabresa - R$ 28,00",
            "Molho de tomate, mussarela, calabresa e cebola",
        ]
    )
    ok, reason = _is_text_quality_sufficient(text)
    assert ok, f"expected ok, got {reason}"


def test_quality_fails_on_multi_column_layout():
    """Menu with prices floating on their own lines — classic multi-column pattern."""
    text = "\n".join(
        [
            "ENTRADAS",
            "48.00",
            "Serve 3",
            "68.00",
            "COUVERT DA CASA",
            "Pao italiano com manteiga",
            "COUVERT COMPLETO",
            "Pao italiano com patê de cenoura",
            "22.00",
            "ABOBRINHA FILETADA",
            "28.00",
            "AZEITONA PRETA",
        ]
    )
    ok, reason = _is_text_quality_sufficient(text)
    assert not ok
    assert reason == "multi_column_layout"


def test_quality_fails_on_too_short_text():
    ok, reason = _is_text_quality_sufficient("hi")
    assert not ok
    assert reason == "too_short"


def test_quality_fails_when_no_prices():
    text = "Lorem ipsum dolor sit amet, consectetur adipiscing elit sed do eiusmod."
    ok, reason = _is_text_quality_sufficient(text)
    assert not ok
    assert reason == "no_prices"
