from app.sanitize import sanitize_llm_output


# ---------- URL stripping ----------


def test_strips_https_url():
    assert "[link removido]" in sanitize_llm_output(
        "Veja https://phishing.com/scam agora"
    )


def test_strips_http_url():
    assert "[link removido]" in sanitize_llm_output("Acesse http://evil.com")


def test_strips_www_url():
    assert "[link removido]" in sanitize_llm_output("Visite www.phishing.com")


# ---------- Email stripping ----------


def test_strips_email():
    result = sanitize_llm_output("Mande para victim@evil.com")
    assert "[email removido]" in result
    assert "victim@evil.com" not in result


# ---------- Phone number stripping ----------


def test_strips_domestic_phone():
    result = sanitize_llm_output("Ligue para 11987654321")
    assert "[numero removido]" in result
    assert "11987654321" not in result


def test_strips_international_phone():
    result = sanitize_llm_output("Ligue para +55 11 98765-4321")
    assert "[numero removido]" in result


def test_preserves_short_numbers():
    """Prices, order IDs, and short sequences should not be stripped."""
    result = sanitize_llm_output("Pedido #123 total R$ 25,90")
    assert "123" in result
    assert "25" in result


def test_preserves_order_id_like_numbers():
    result = sanitize_llm_output("Seu pedido 4567 foi confirmado")
    assert "4567" in result


# ---------- Dangerous Unicode ----------


def test_strips_rtl_override():
    text = "Hello\u202eevil"
    result = sanitize_llm_output(text)
    assert "\u202e" not in result


def test_strips_zero_width_chars():
    text = "normal\u200btext\u200cwith\u200dhidden"
    result = sanitize_llm_output(text)
    assert "\u200b" not in result
    assert "\u200c" not in result
    assert "\u200d" not in result


# ---------- Truncation ----------


def test_truncates_long_messages():
    long_text = "a" * 2000
    result = sanitize_llm_output(long_text)
    assert len(result) <= 1004  # 1000 + "..."


# ---------- Preserves normal formatting ----------


def test_preserves_whatsapp_bold():
    result = sanitize_llm_output("Seu pedido *total* ficou R$ 30")
    assert "*total*" in result


def test_preserves_whatsapp_italic():
    result = sanitize_llm_output("Item _adicionado_ com sucesso")
    assert "_adicionado_" in result


def test_preserves_normal_text():
    msg = "Obrigado pelo seu pedido! Ele esta sendo preparado."
    assert sanitize_llm_output(msg) == msg


# ---------- Edge cases ----------


def test_empty_string():
    assert sanitize_llm_output("") == ""


def test_none_input():
    assert sanitize_llm_output(None) == ""


def test_whitespace_collapse():
    result = sanitize_llm_output("Pedido   feito    com   sucesso")
    assert "  " not in result


# ---------- HTML entity escaping ----------


def test_escapes_html_script_tags():
    result = sanitize_llm_output("Olá <script>alert('xss')</script>")
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_escapes_html_ampersand():
    result = sanitize_llm_output("A & B")
    assert "&amp;" in result


def test_preserves_quotes():
    result = sanitize_llm_output('"obrigado"')
    assert '"obrigado"' in result
