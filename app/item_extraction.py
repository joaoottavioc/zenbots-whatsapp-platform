# app/item_extraction.py
"""
Lightweight local item extraction for food orders.

Replaces the LLM-based extract_potential_items() on the hot path (T2-1).
Strips quantities, stopwords, and splits on conjunctions to isolate
food/drink names for pgvector semantic search.
"""

import re
from typing import List

# Common Portuguese stopwords in restaurant ordering context.
# These are words that appear around food names but are not food names.
_STOP = frozenset(
    {
        "quero",
        "gostaria",
        "me",
        "ve",
        "vê",
        "por",
        "favor",
        "um",
        "uma",
        "uns",
        "umas",
        "de",
        "do",
        "da",
        "dos",
        "das",
        "pra",
        "para",
        "pode",
        "ser",
        "só",
        "so",
        "também",
        "mais",
        "aí",
        "ai",
        "manda",
        "coloca",
        "bota",
        "meu",
        "minha",
        "o",
        "a",
        "os",
        "as",
        "no",
        "na",
        "nos",
        "nas",
        "esse",
        "essa",
        "esses",
        "essas",
        "este",
        "esta",
        "com",
        "sem",
        "queria",
        "preciso",
        "vou",
        "vai",
        "mandar",
        "adicionar",
        "adiciona",
        "colocar",
        "botar",
        "pedir",
        "pedido",
        "obrigado",
        "obrigada",
        "brigado",
        "brigada",
        "valeu",
        "please",
        "pfv",
        "pfvr",
        "pf",
        "já",
        "ja",
        "la",
        "lá",
        "né",
        "ne",
        "tá",
        "ta",
        "então",
        "entao",
        "bem",
        "bom",
        "boa",
        "aqui",
        "ali",
        "leva",
        "traz",
        "traga",
        "viagem",
        "comer",
        "beber",
    }
)

# Matches pure numbers (including decimals like "2,5")
_NUM_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")

# Splits on "e", ",", ";" — common conjunctions in Brazilian Portuguese orders
_SPLIT_RE = re.compile(r"\s*(?:\be\b|,|;)\s*", re.IGNORECASE)

# Quantity words that act as item separators
_QTY_WORDS = frozenset(
    {
        "um",
        "uma",
        "duas",
        "dois",
        "três",
        "tres",
        "quatro",
        "cinco",
        "seis",
        "sete",
        "oito",
        "nove",
        "dez",
        "onze",
        "doze",
        "treze",
        "quatorze",
        "catorze",
        "quinze",
        "dezesseis",
        "dezessete",
        "dezoito",
        "dezenove",
        "vinte",
        "trinta",
        "quarenta",
        "cinquenta",
        "cem",
        "cento",
        "duzentos",
        "duzentas",
        "trezentos",
        "trezentas",
        "mil",
        "primeiro",
        "segunda",
        "segundo",
        "terceiro",
        "terceira",
    }
)

# Words that are only quantity-related (not compound number parts)
_QTY_ONLY_WORDS = frozenset({"porção", "porções", "unidade", "unidades"})


def _collapse_compound_numbers(text: str) -> str:
    """Replace quantity words with commas, but keep compound numbers together.

    "vinte e sete flipflops e um cabana"
    → "vinte e sete" is a compound number → single comma
    → ", flipflops e , cabana"

    "cento e vinte e três pcqs e doze flipflops"
    → "cento e vinte e três" is one compound number → single comma
    → ", pcqs e , flipflops"
    """
    words = text.split()
    result = []
    i = 0
    while i < len(words):
        word = words[i]
        if word in _QTY_WORDS:
            # Look ahead: is this part of a compound number?
            # Pattern: qty "e" qty ["e" qty ...]
            j = i
            while (
                j + 2 < len(words)
                and words[j + 1] == "e"
                and words[j + 2] in _QTY_WORDS
            ):
                j += 2  # skip "e" + next qty word
            # Replace the entire compound number span with a single comma
            result.append(",")
            i = j + 1
        elif word in _QTY_ONLY_WORDS:
            result.append(",")
            i += 1
        else:
            result.append(word)
            i += 1
    return " ".join(result)


def extract_items_local(text: str) -> List[str]:
    """
    Extract food/drink item names from a user message without calling an LLM.

    Examples:
        "quero 2 pizzas e 1 coca" → ["pizzas", "coca"]
        "me vê um x-burger com queijo" → ["x-burger queijo"]
        "só uma água, obrigado" → ["água"]
        "quero duas nega maluca e um café" → ["nega maluca", "café"]
        "vinte e sete flipflops e um cabana" → ["flipflops", "cabana"]

    Returns a list of cleaned item strings. Falls back to [text.strip()]
    if nothing meaningful is extracted (so the caller always has something
    to feed to pgvector search).
    """
    # 1. Lowercase and strip numbers
    cleaned = _NUM_RE.sub("", text.lower())

    # 2. Replace quantity words with commas (item separators),
    # but keep compound numbers like "vinte e sete" together as one separator.
    cleaned = _collapse_compound_numbers(cleaned)

    # 3. Split on conjunctions/commas
    parts = _SPLIT_RE.split(cleaned)

    items = []
    for part in parts:
        # Remove stopwords
        words = [w for w in part.split() if w not in _STOP]
        cleaned_part = " ".join(words).strip()

        # Remove leading/trailing punctuation
        cleaned_part = cleaned_part.strip(".,;!?")

        if cleaned_part and len(cleaned_part) >= 2:
            items.append(cleaned_part)

    # If extraction yielded nothing, return the original text so pgvector
    # can still attempt a semantic match.
    return items if items else [text.strip()]
