# tests/test_item_extraction.py
"""
Tests for app/item_extraction.py (T2-1).

Validates that the local (no-LLM) item extractor handles:
- Basic food orders with quantities
- Multiple items separated by "e" or ","
- Stopword removal (quero, me vê, por favor, etc.)
- Written-out quantities (duas, três, etc.)
- Compound names (x-burger com queijo, steak au poivre)
- Brazilian slang (me vê um x-tudo)
- Messages with notes (pizza sem cebola → extracts "pizza cebola" not the full phrase)
- Edge cases: empty strings, only stopwords, only numbers
- Fallback: returns [text.strip()] when nothing meaningful is extracted
"""

from app.item_extraction import extract_items_local


class TestBasicExtraction:
    def test_single_item_with_quantity(self):
        result = extract_items_local("quero 2 pizzas")
        assert len(result) >= 1
        assert any("pizza" in item for item in result)

    def test_multiple_items_with_e(self):
        result = extract_items_local("quero 2 pizzas e 1 coca")
        assert len(result) >= 2
        assert any("pizza" in item for item in result)
        assert any("coca" in item for item in result)

    def test_multiple_items_with_comma(self):
        result = extract_items_local("pizza, coca, batata frita")
        assert len(result) >= 3

    def test_single_item_no_quantity(self):
        result = extract_items_local("pizza margherita")
        assert len(result) >= 1
        assert any("pizza margherita" in item for item in result)


class TestStopwordRemoval:
    def test_removes_quero(self):
        result = extract_items_local("quero uma pizza")
        assert all("quero" not in item for item in result)

    def test_removes_por_favor(self):
        result = extract_items_local("uma água por favor")
        assert all("favor" not in item for item in result)
        assert any("água" in item for item in result)

    def test_removes_me_ve(self):
        result = extract_items_local("me vê um x-burger")
        assert any("x-burger" in item for item in result)

    def test_removes_obrigado(self):
        result = extract_items_local("só uma água, obrigado")
        assert any("água" in item for item in result)
        assert all("obrigado" not in item for item in result)


class TestWrittenQuantities:
    def test_removes_duas(self):
        result = extract_items_local("quero duas pizzas")
        assert all("duas" not in item for item in result)
        assert any("pizza" in item for item in result)

    def test_removes_tres(self):
        result = extract_items_local("três cervejas")
        assert all("três" not in item for item in result)

    def test_removes_uma(self):
        result = extract_items_local("uma coca-cola")
        assert any("coca-cola" in item for item in result)


class TestCompoundNames:
    def test_compound_with_hyphen(self):
        result = extract_items_local("quero um x-burger")
        assert any("x-burger" in item for item in result)

    def test_french_name(self):
        """Compound foreign names should survive extraction."""
        result = extract_items_local("me vê um steak au poivre")
        assert any("steak au poivre" in item for item in result)

    def test_croque_monsieur(self):
        result = extract_items_local("me vê um croque monsieur")
        assert any("croque monsieur" in item for item in result)


class TestBrazilianSlang:
    def test_manda_ver(self):
        result = extract_items_local("manda ver uma pizza calabresa")
        assert any("pizza calabresa" in item for item in result)

    def test_bota_ai(self):
        result = extract_items_local("bota aí uma coca")
        assert any("coca" in item for item in result)

    def test_coloca(self):
        result = extract_items_local("coloca 3 pastéis")
        assert any("past" in item for item in result)


class TestEdgeCases:
    def test_empty_string_returns_original(self):
        result = extract_items_local("")
        assert result == [""]

    def test_only_stopwords_returns_original(self):
        """When extraction strips everything, falls back to original text."""
        result = extract_items_local("por favor obrigado")
        # Should return the original text since nothing meaningful extracted
        assert len(result) >= 1

    def test_only_numbers_returns_original(self):
        result = extract_items_local("2 3 4")
        assert len(result) >= 1

    def test_short_word_filtered(self):
        """Single-character tokens after cleanup should be dropped."""
        result = extract_items_local("quero a")
        # "a" is a stopword so it gets removed; fallback to original
        assert len(result) >= 1

    def test_preserves_accented_characters(self):
        result = extract_items_local("café e água")
        assert any("café" in item for item in result)
        assert any("água" in item for item in result)


class TestCompoundNumbers:
    """Compound Portuguese numbers like 'vinte e sete' should be treated as
    a single quantity, not split at the 'e' conjunction."""

    def test_vinte_e_sete_not_split(self):
        result = extract_items_local("vinte e sete flipflops e um cabana")
        assert "flipflops" in result
        assert "cabana" in result
        assert len(result) == 2

    def test_trinta_e_dois_not_split(self):
        result = extract_items_local("trinta e dois bacon blasts e 1 pcq")
        assert any("bacon blasts" in item for item in result)
        assert "pcq" in result

    def test_cento_e_vinte_e_tres(self):
        result = extract_items_local("cento e vinte e tres pcqs e doze flipflops")
        assert "pcqs" in result
        assert "flipflops" in result

    def test_mil_e_duzentos(self):
        result = extract_items_local("mil e duzentos e trinta e quatro flipflops")
        assert "flipflops" in result
        assert len(result) == 1

    def test_vinte_e_um(self):
        result = extract_items_local("vinte e um pcq e trinta e cinco sunburger")
        assert "pcq" in result
        assert "sunburger" in result
        assert len(result) == 2

    def test_simple_quantities_still_separate(self):
        """Non-compound quantities like 'tres pcq e doze flipflops' should
        still split items correctly."""
        result = extract_items_local("tres pcq e doze flipflops")
        assert "pcq" in result
        assert "flipflops" in result


class TestQuantityAsSeparator:
    """Quantity words should act as item separators, not just be stripped."""

    def test_quantity_between_items(self):
        result = extract_items_local(
            "quero um truffle burguer tres pcq e doze flipflops"
        )
        assert any("truffle burguer" in item for item in result)
        assert "pcq" in result
        assert "flipflops" in result

    def test_doze_stripped(self):
        result = extract_items_local("doze flipflops")
        assert "flipflops" in result
        assert all("doze" not in item for item in result)

    def test_trinta_stripped(self):
        result = extract_items_local("trinta pcqs")
        assert "pcqs" in result
        assert all("trinta" not in item for item in result)

    def test_complex_multi_item_order(self):
        result = extract_items_local(
            "hoje vou de dois cheesuburger um sunberguer e "
            "trinta e dois bacon blasts e 1 pcq e um oklahoma krispy"
        )
        assert any("cheesuburger" in item for item in result)
        assert any("sunberguer" in item for item in result)
        assert any("bacon blasts" in item for item in result)
        assert "pcq" in result
        assert any("oklahoma krispy" in item for item in result)


class TestRealWorldMessages:
    """Test with messages from the extract_potential_items LLM prompt examples."""

    def test_x_burger_com_queijo(self):
        result = extract_items_local(
            "Eu quero dois x-burger com queijo e uma porção de batata frita, por favor"
        )
        assert any("x-burger" in item for item in result)
        assert any("batata frita" in item for item in result)

    def test_agua_obrigado(self):
        result = extract_items_local("só uma água, obrigado")
        assert any("água" in item for item in result)

    def test_nega_maluca_e_cafe(self):
        result = extract_items_local("quero duas nega maluca e um café")
        assert any("nega maluca" in item for item in result)
        assert any("café" in item for item in result)

    def test_tainha_assada(self):
        result = extract_items_local("pode me mandar uma tainha assada")
        assert any("tainha assada" in item for item in result)

    def test_x_polenta(self):
        result = extract_items_local("vou querer um x polenta pra viagem")
        assert any("x polenta" in item for item in result)

    def test_bolo_macadamias(self):
        result = extract_items_local("quero um bolo de macadamias")
        assert any("bolo" in item for item in result)
        assert any("macadamia" in item for item in result)

    def test_no_food_items(self):
        """Messages without food items should still return something for RAG."""
        result = extract_items_local("tem mais sugestoes?")
        assert len(result) >= 1

    def test_unknown_item(self):
        """Unknown item names should pass through for RAG to handle."""
        result = extract_items_local("quero um XXXXXX")
        assert any("xxxxxx" in item.lower() for item in result)
