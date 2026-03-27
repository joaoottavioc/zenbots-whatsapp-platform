# app/item_extraction.py
"""
Lightweight local item extraction for food orders.

Replaces the LLM-based extract_potential_items() on the hot path (T2-1).
Strips quantities, stopwords, and splits on conjunctions to isolate
food/drink names for pgvector semantic search.
"""

import re
from difflib import SequenceMatcher
from typing import List, Tuple

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
        "hoje",
        "querer",
        "quer",
        "vou",
        "vamos",
        "dia",
        "noite",
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
        "sessenta",
        "setenta",
        "oitenta",
        "noventa",
        "cem",
        "cento",
        "duzentos",
        "duzentas",
        "trezentos",
        "trezentas",
        "quatrocentos",
        "quatrocentas",
        "quinhentos",
        "quinhentas",
        "seiscentos",
        "seiscentas",
        "setecentos",
        "setecentas",
        "oitocentos",
        "oitocentas",
        "novecentos",
        "novecentas",
        "mil",
        # Common typos / Spanishisms / informal
        "cuatro",
        "cuarenta",
        "sinco",
        "sinquenta",
        "ceis",  # informal "seis"
        "tresentos",  # typo for "trezentos"
        "tresentas",
        "dusentos",  # typo for "duzentos"
        "dusentas",
        "primeiro",
        "segunda",
        "segundo",
        "terceiro",
        "terceira",
    }
)

# Words that are only quantity-related (not compound number parts)
_QTY_ONLY_WORDS = frozenset({"porção", "porções", "unidade", "unidades"})

# Mapping of Portuguese quantity words to their numeric values
_QTY_VALUES: dict[str, int] = {
    "um": 1,
    "uma": 1,
    "duas": 2,
    "dois": 2,
    "três": 3,
    "tres": 3,
    "quatro": 4,
    "cuatro": 4,  # Spanish typo
    "cinco": 5,
    "sinco": 5,  # common typo
    "ceis": 6,  # informal "seis"
    "seis": 6,
    "sete": 7,
    "oito": 8,
    "nove": 9,
    "dez": 10,
    "onze": 11,
    "doze": 12,
    "treze": 13,
    "quatorze": 14,
    "catorze": 14,
    "quinze": 15,
    "dezesseis": 16,
    "dezessete": 17,
    "dezoito": 18,
    "dezenove": 19,
    "vinte": 20,
    "trinta": 30,
    "quarenta": 40,
    "cuarenta": 40,  # Spanish typo
    "cinquenta": 50,
    "sinquenta": 50,  # common typo
    "sessenta": 60,
    "setenta": 70,
    "oitenta": 80,
    "noventa": 90,
    "cem": 100,
    "cento": 100,
    "duzentos": 200,
    "duzentas": 200,
    "trezentos": 300,
    "trezentas": 300,
    "tresentos": 300,  # common typo
    "tresentas": 300,
    "dusentos": 200,  # common typo
    "dusentas": 200,
    "quatrocentos": 400,
    "quatrocentas": 400,
    "quinhentos": 500,
    "quinhentas": 500,
    "seiscentos": 600,
    "seiscentas": 600,
    "setecentos": 700,
    "setecentas": 700,
    "oitocentos": 800,
    "oitocentas": 800,
    "novecentos": 900,
    "novecentas": 900,
    "mil": 1000,
}


def _is_qty_word(word: str) -> bool:
    """Check if word is a quantity word, with fuzzy matching for typos."""
    if word in _QTY_WORDS:
        return True
    if len(word) < 4:
        return False
    best = max(
        (
            SequenceMatcher(None, word, k).ratio()
            for k in _QTY_WORDS
            if abs(len(k) - len(word)) <= 3
        ),
        default=0.0,
    )
    return best >= 0.8


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
        if _is_qty_word(word):
            # Look ahead: is this part of a compound number?
            # Pattern: qty [qty | "e" qty] ...  (handles "mil duzentos" and "vinte e sete")
            j = i
            while j + 1 < len(words):
                nxt = words[j + 1]
                if _is_qty_word(nxt):
                    # Adjacent qty word: "mil duzentos"
                    j += 1
                elif nxt == "e" and j + 2 < len(words) and _is_qty_word(words[j + 2]):
                    # "e" + qty word: "vinte e sete"
                    j += 2
                else:
                    break
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


def _resolve_compound_qty(words: List[str], start: int) -> Tuple[int, int]:
    """Resolve a compound number starting at index `start`.

    Handles patterns like "vinte e três" (23), "cento e vinte" (120).
    Returns (numeric_value, end_index_exclusive).
    """
    total = _QTY_VALUES.get(words[start], 0)
    j = start + 1
    while j + 1 < len(words) and words[j] == "e" and words[j + 1] in _QTY_VALUES:
        total += _QTY_VALUES[words[j + 1]]
        j += 2
    return total, j


def extract_items_with_quantities(text: str) -> List[Tuple[int, str]]:
    """Extract (quantity, item_name) pairs from a user message.

    Examples:
        "um picanha dois prensadão treze supremo x"
        → [(1, "picanha"), (2, "prensadão"), (13, "supremo x")]

        "quero 3 pizzas e 1 coca"
        → [(3, "pizzas"), (1, "coca")]

        "vinte e sete classic burger e um cabana"
        → [(27, "classic burger"), (1, "cabana")]

    Returns empty list if no quantity-item pairs found.
    """
    cleaned = text.lower().strip()
    words = cleaned.split()

    pairs: List[Tuple[int, str]] = []
    current_qty: int | None = None
    current_words: List[str] = []

    i = 0
    while i < len(words):
        word = words[i]

        # Check for digit quantity (e.g., "3", "20")
        digit_match = re.match(r"^(\d+)$", word)

        if word in _QTY_VALUES:
            # Flush previous item if any
            if current_words:
                item = _clean_item_words(current_words)
                if item:
                    pairs.append((current_qty or 1, item))
                current_words = []

            # Resolve compound number
            qty, end = _resolve_compound_qty(words, i)
            current_qty = qty
            i = end
            continue

        elif digit_match:
            # Flush previous item
            if current_words:
                item = _clean_item_words(current_words)
                if item:
                    pairs.append((current_qty or 1, item))
                current_words = []

            current_qty = int(digit_match.group(1))
            i += 1
            continue

        elif word in _QTY_ONLY_WORDS:
            i += 1
            continue

        # Conjunction/separator — flush if we have words
        elif word == "e" or word in (",", ";"):
            if current_words:
                item = _clean_item_words(current_words)
                if item:
                    pairs.append((current_qty or 1, item))
                    current_qty = None
                current_words = []
            i += 1
            continue

        # Regular word — skip stopwords, collect item words
        elif word not in _STOP:
            current_words.append(word)

        i += 1

    # Flush last item
    if current_words:
        item = _clean_item_words(current_words)
        if item:
            pairs.append((current_qty or 1, item))

    return pairs


def _clean_item_words(words: List[str]) -> str:
    """Clean and join item words, removing punctuation."""
    cleaned = " ".join(words).strip(".,;!?")
    return cleaned if len(cleaned) >= 2 else ""


def rewrite_as_structured_order(pairs: List[Tuple[int, str]]) -> str | None:
    """Convert quantity-item pairs into a structured order string for the LLM.

    Returns None if pairs is empty (caller should use original message).
    """
    if not pairs:
        return None
    parts = [f"{qty}x {name}" for qty, name in pairs]
    return "Adicionar ao carrinho: " + ", ".join(parts)
