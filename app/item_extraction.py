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


def extract_items_local(text: str) -> List[str]:
    """
    Extract food/drink item names from a user message without calling an LLM.

    Examples:
        "quero 2 pizzas e 1 coca" → ["pizzas", "coca"]
        "me vê um x-burger com queijo" → ["x-burger queijo"]
        "só uma água, obrigado" → ["água"]
        "quero duas nega maluca e um café" → ["nega maluca", "café"]

    Returns a list of cleaned item strings. Falls back to [text.strip()]
    if nothing meaningful is extracted (so the caller always has something
    to feed to pgvector search).
    """
    # 1. Lowercase and strip numbers
    cleaned = _NUM_RE.sub("", text.lower())

    # 2. Remove common written-out quantities (Portuguese)
    # Also treat quantity words as item separators by replacing with comma
    # so "truffle burguer tres pcq" becomes "truffle burguer , pcq"
    cleaned = re.sub(
        r"\b(duas?|três|tres|quatro|cinco|seis|sete|oito|nove|dez|"
        r"onze|doze|treze|quatorze|catorze|quinze|dezesseis|dezessete|"
        r"dezoito|dezenove|vinte|trinta|quarenta|cinquenta|cem|"
        r"uma?|primeiro|segunda?|terceir[ao]|porções?|porção|unidades?)\b",
        ",",
        cleaned,
    )

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
