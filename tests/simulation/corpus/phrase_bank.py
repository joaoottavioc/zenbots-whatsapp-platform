# tests/simulation/corpus/phrase_bank.py
"""
Realistic Brazilian Portuguese phrase variations for QA testing.
Each phrase uses {name} for product name and {qty} for quantity.
Weights approximate real-world frequency.
"""

import random
import re

ADD_PHRASES = [
    # High frequency — universal
    ("quero {qty} {name}", 0.12),
    ("me vê {qty} {name}", 0.10),
    ("manda {qty} {name}", 0.10),
    # Informal — very common
    ("bota {qty} {name}", 0.07),
    ("coloca {qty} {name}", 0.07),
    ("me arruma {qty} {name}", 0.06),
    ("vou querer {qty} {name}", 0.06),
    ("pode mandar {qty} {name}", 0.05),
    # Ultra-short — WhatsApp typical
    ("{qty} {name}", 0.08),
    ("{name}", 0.04),
    # Slang/youth
    ("vou de {name}", 0.04),
    ("to querendo {qty} {name}", 0.04),
    ("me faz {qty} {name}", 0.03),
    ("joga {qty} {name} no pedido", 0.03),
    # Polite
    ("quero pedir {qty} {name}", 0.03),
    ("{qty} {name} por favor", 0.03),
    ("pode ser {qty} {name}", 0.03),
    # Regional
    ("tu manda {qty} {name}", 0.02),
]

REMOVE_PHRASES = [
    ("tira o {name}", 0.18),
    ("remove o {name}", 0.12),
    ("não quero mais o {name}", 0.12),
    ("pode tirar o {name}", 0.10),
    ("cancela o {name}", 0.08),
    ("tira {name} do pedido", 0.08),
    ("sem o {name}", 0.07),
    ("retira o {name}", 0.06),
    ("esquece o {name}", 0.05),
    ("tira fora o {name}", 0.04),
    ("não manda o {name} não", 0.04),
    ("deixa sem o {name}", 0.03),
    ("tira ae o {name}", 0.03),
]

SUGGESTION_PHRASES = [
    ("o que tem de bom?", 0.12),
    ("o que vocês recomendam?", 0.10),
    ("me indica alguma coisa", 0.08),
    ("qual o mais pedido?", 0.08),
    ("to indeciso, me ajuda", 0.07),
    ("o que tem aí?", 0.07),
    ("alguma sugestão?", 0.06),
    ("não sei o que pedir", 0.06),
    ("o que tá saindo mais?", 0.05),
    ("qual o carro chefe?", 0.05),
    ("me sugere aí", 0.05),
    ("quais as opções?", 0.05),
    ("sugestão?", 0.04),
    ("dicas?", 0.04),
    ("o que é bom aí?", 0.04),
    ("manda as opções aí", 0.04),
]

CHECKOUT_PHRASES = [
    ("só isso", 0.15),
    ("é só", 0.10),
    ("fecha", 0.08),
    ("finaliza", 0.08),
    ("pode fechar", 0.08),
    ("é isso aí", 0.07),
    ("tá bom assim", 0.06),
    ("pronto", 0.05),
    ("fecha o pedido", 0.05),
    ("só isso mesmo", 0.05),
    ("era só isso", 0.04),
    ("beleza, só isso", 0.04),
    ("já pode mandar", 0.04),
    ("manda o pedido", 0.04),
    ("não quero mais nada", 0.04),
    ("fechou", 0.03),
]

GREETING_PHRASES = [
    ("oi", 0.20),
    ("oi, tudo bem?", 0.12),
    ("boa noite", 0.10),
    ("boa tarde", 0.08),
    ("bom dia", 0.08),
    ("oi boa noite", 0.07),
    ("olá", 0.05),
    ("opa", 0.05),
    ("oii", 0.04),
    ("eae", 0.03),
    ("fala", 0.03),
    ("oi gente", 0.03),
    ("boa", 0.03),
    ("salve", 0.02),
    ("oiii", 0.02),
    ("oi, quero fazer um pedido", 0.03),
    ("voltei", 0.02),
]

QUESTION_PHRASES = [
    ("quanto custa o {name}?", 0.15),
    ("quanto é o {name}?", 0.12),
    ("qual o valor do {name}?", 0.10),
    ("tem {name}?", 0.10),
    ("quanto tá o {name}?", 0.08),
    ("o {name} vem com o quê?", 0.07),
    ("qual o tamanho do {name}?", 0.06),
    ("quanto fica o {name}?", 0.06),
    ("ainda tem {name}?", 0.05),
    ("quanto sai o {name}?", 0.05),
    ("o {name} tá no cardápio?", 0.05),
    ("aceita pix?", 0.04),
    ("qual a taxa de entrega?", 0.04),
    ("quanto tempo demora?", 0.03),
]

# --- Generic food words to exclude from abbreviation generation ---
GENERIC_FOOD_WORDS = frozenset(
    {
        "combo",
        "prato",
        "porção",
        "porcao",
        "tamanho",
        "grande",
        "pequeno",
        "medio",
        "especial",
        "simples",
        "duplo",
        "triplo",
        "mini",
        "super",
        "tradicional",
        "caseiro",
        "artesanal",
        "gourmet",
        "premium",
        "classic",
    }
)

CATEGORY_PREFIXES = [
    "cerveja",
    "refrigerante",
    "suco de",
    "suco",
    "água",
    "pizza de",
    "pizza",
    "hambúrguer",
    "hamburguer",
    "lanche",
    "pastel de",
    "pastel",
    "açaí",
    "acai",
    "crepe de",
    "crepe",
    "tapioca de",
    "tapioca",
    "espetinho de",
    "espetinho",
    "caldo de",
    "caldo",
]

DRINK_CATEGORIES = frozenset(
    {
        "bebidas",
        "drinks",
        "bebida",
        "cervejas",
        "cerveja",
        "refrigerantes",
        "refrigerante",
        "sucos",
        "suco",
    }
)


def pick_weighted(phrases: list[tuple[str, float]]) -> str:
    """Pick a phrase from a weighted list."""
    items, weights = zip(*phrases)
    return random.choices(items, weights=weights, k=1)[0]


def format_add(name: str, qty: int = 1) -> str:
    """Generate a random ADD phrase with product name and quantity."""
    phrase = pick_weighted(ADD_PHRASES)
    qty_str = str(qty) if qty > 1 else ("um" if random.random() < 0.5 else "1")
    result = phrase.format(name=name.lower(), qty=qty_str)
    # Clean up "um um" or "1 um" artifacts
    result = re.sub(r"\b(um|1)\s+\1\b", r"\1", result)
    return result.strip()


def format_remove(name: str) -> str:
    """Generate a random REMOVE phrase."""
    phrase = pick_weighted(REMOVE_PHRASES)
    return phrase.format(name=name.lower()).strip()


def format_suggestion() -> str:
    """Generate a random SUGGESTION phrase."""
    return pick_weighted(SUGGESTION_PHRASES)


def format_checkout() -> str:
    """Generate a random CHECKOUT phrase."""
    return pick_weighted(CHECKOUT_PHRASES)


def format_greeting() -> str:
    """Generate a random GREETING phrase."""
    return pick_weighted(GREETING_PHRASES)


def format_question(name: str) -> str:
    """Generate a random QUESTION phrase (should NOT add to cart)."""
    phrase = pick_weighted(QUESTION_PHRASES)
    return phrase.format(name=name.lower()).strip()


def generate_abbreviations(product_name: str) -> list[str]:
    """Generate natural abbreviations a customer would use for a product.

    Examples:
        "Cerveja Brahma 600ml" → ["brahma", "cerveja brahma", "brahma 600ml"]
        "Coca-Cola 2L" → ["coca", "coca-cola", "coca 2l"]
        "Pizza Calabresa" → ["calabresa"]
        "X-Tudo Completo" → ["x-tudo"]
    """
    name_lower = product_name.lower()
    abbreviations = []

    # 1. Without category prefix
    for prefix in CATEGORY_PREFIXES:
        if name_lower.startswith(prefix + " "):
            rest = product_name[len(prefix) :].strip()
            if len(rest) > 3:
                abbreviations.append(rest.lower())
        elif name_lower.startswith(prefix):
            rest = product_name[len(prefix) :].strip()
            if len(rest) > 3:
                abbreviations.append(rest.lower())

    # 2. Without size suffix
    no_size = re.sub(
        r"\s*\d+\s*(?:ml|l|g|kg|un|pç|pecas|peças)\b", "", product_name, flags=re.I
    ).strip()
    if no_size != product_name and len(no_size) > 3:
        abbreviations.append(no_size.lower())

    # 3. First significant word (often the brand)
    words = product_name.split()
    for w in words:
        clean = w.lower().strip(",-.()")
        if len(clean) > 3 and clean not in GENERIC_FOOD_WORDS:
            # Skip category prefixes themselves
            if clean not in {p.replace(" ", "") for p in CATEGORY_PREFIXES}:
                abbreviations.append(clean)
                break

    # 4. Common contractions
    if "coca-cola" in name_lower:
        abbreviations.extend(["coca", "coca cola"])
    if "guaraná" in name_lower or "guarana" in name_lower:
        abbreviations.append("guaraná")

    # Deduplicate and filter
    seen = set()
    result = []
    for a in abbreviations:
        a = a.strip()
        if a and a not in seen and a != name_lower and len(a) >= 3:
            seen.add(a)
            result.append(a)

    return result


def pick_random_products(
    products: list[dict], unavail_indices: set[int], count: int = 5
) -> dict:
    """Pick random products for test scenarios, avoiding selection bias.

    Returns a dict of role → product, picking from different parts of the menu.
    """
    available = [p for i, p in enumerate(products) if i not in unavail_indices]
    unavailable = [products[i] for i in unavail_indices if i < len(products)]

    main = [
        p for p in available if p.get("category", "").lower() not in DRINK_CATEGORIES
    ]
    drinks = [p for p in available if p.get("category", "").lower() in DRINK_CATEGORIES]

    if len(main) < 3:
        main = available[: max(5, len(available))]

    random.shuffle(main)
    random.shuffle(drinks)
    random.shuffle(available)

    def pick_unique(source: list, exclude_ids: set) -> dict | None:
        for p in source:
            pid = p.get("id") or id(p)
            if pid not in exclude_ids:
                exclude_ids.add(pid)
                return p
        return None

    used: set = set()

    result = {
        "single": pick_unique(main, used),
        "multi_1": pick_unique(main, used),
        "multi_2": pick_unique(drinks if drinks else available, used),
        "remove": pick_unique(main, used),
        "checkout": pick_unique(main, used),
        "question": pick_unique(available, used),
        "double_add": pick_unique(main, used),
        "avail_with_unavail": pick_unique(available, used),
    }

    result["unavail"] = unavailable[0] if unavailable else None

    # Fill None slots with any available product
    for key in result:
        if result[key] is None and key != "unavail":
            result[key] = random.choice(available) if available else None

    return result
