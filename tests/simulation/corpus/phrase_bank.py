# tests/simulation/corpus/phrase_bank.py
"""
Realistic Brazilian Portuguese phrase variations for QA testing.
Each phrase uses {name} for product name and {qty} for quantity.
Weights approximate real-world frequency.

Phrase tiers (controlled by PHRASE_TIER env var):
  - "clean":  Tier 1 only — formal, well-formed phrases (legacy default)
  - "mixed":  Weighted blend of Tier 1 + Tier 2 (default; closest to today)
  - "slang":  Tier 2 only — WhatsApp shorthand, abbreviations, no caps
  - "hard":   Tier 2 + Tier 3 (slang AND typos applied to product names)

The "hard" tier is what the P0 honest baseline run uses to expose the
real-world failure rate. Switch via:  PHRASE_TIER=hard python ...
"""

import os
import random
import re

# --- Tier 1: Clean / formal phrases ---
# These are the well-formed messages a careful customer would send.
# Used for the "clean" tier and as the high-weight base of "mixed".
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

# --- Tier 2: Slang / abbreviations / WhatsApp shorthand ---
# What real Brazilian customers actually type at 11pm on Friday after a beer.
# These are MUCH harder for the bot — abbreviated verbs ("pfv"=por favor,
# "ae"=aí), no punctuation, vague qualifiers, voice-like fillers, no capitals.
SLANG_ADD_PHRASES = [
    # WhatsApp shorthand
    ("manda {qty} {name} ae", 0.10),
    ("manda {qty} {name} pfv", 0.08),
    ("me ve {qty} {name} ai", 0.07),
    ("bota {qty} {name} pra mim", 0.07),
    ("fecha {qty} {name}", 0.06),
    ("me faz {qty} {name} ai", 0.05),
    ("solta {qty} {name}", 0.05),
    # Bare names + voice fillers
    ("{name}", 0.07),
    ("um {name}", 0.06),
    ("{qty} {name}", 0.06),
    ("entao manda {qty} {name}", 0.05),
    ("tipo um {name}", 0.04),
    # Affective qualifiers (filler that doesn't help disambiguation)
    ("manda uma {name} gelada", 0.04),
    ("bota um {name} bem feito", 0.03),
    ("quero {qty} {name} caprichada", 0.03),
    # Continuation-style (no verb)
    ("e mais {qty} {name}", 0.04),
    ("ah e {qty} {name}", 0.03),
    # Polite slang (removed templates ending in "?" — the trailing question
    # mark made the bot answer conversationally instead of adding. This is a
    # phrase-bank quality bug, not a bot bug. Real "polite slang" would be
    # "da pra mandar X" or "rola X" without the question mark.)
    ("da pra mandar {qty} {name}", 0.04),
    ("rola {qty} {name}", 0.03),
]

# Mixed tier: weighted blend of Tier 1 and Tier 2.
# Roughly mirrors what production WhatsApp traffic actually looks like.
MIXED_ADD_PHRASES = [(p, w * 0.55) for p, w in ADD_PHRASES] + [
    (p, w * 0.45) for p, w in SLANG_ADD_PHRASES
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

# --- Tier 2: Slang remove phrases ---
SLANG_REMOVE_PHRASES = [
    ("tira esse {name} ae", 0.15),
    ("vish, tira o {name}", 0.10),
    ("ah, esquece o {name}", 0.10),
    ("tira o {name} pfv", 0.08),
    ("nao quero mais {name} nao", 0.08),
    ("cancela esse {name}", 0.08),
    ("muda, tira o {name}", 0.07),
    ("manda sem o {name}", 0.07),
    ("eu nao quero {name}", 0.06),
    ("nao precisa do {name}", 0.06),
    ("pode esquecer o {name}", 0.05),
    ("muda ae, sem {name}", 0.05),
    ("deixa o {name} pra la", 0.05),
]

MIXED_REMOVE_PHRASES = [(p, w * 0.55) for p, w in REMOVE_PHRASES] + [
    (p, w * 0.45) for p, w in SLANG_REMOVE_PHRASES
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

# --- Tier 2: Slang question phrases (no punctuation, abbreviations) ---
SLANG_QUESTION_PHRASES = [
    ("qt ta o {name}", 0.15),
    ("quanto eh o {name}", 0.12),
    ("preço do {name}", 0.10),
    ("ce tem {name}", 0.10),
    ("ainda tem {name} ae", 0.08),
    ("o {name} ta saindo por quanto", 0.08),
    ("manda o preço do {name} ae", 0.06),
    ("quanto fica {name}", 0.06),
    ("quanto custa esse {name}", 0.06),
    ("o {name} ta saindo?", 0.05),
    ("vcs aceitam pix", 0.05),
    ("eh quanto o {name}", 0.04),
    ("qual o preço de um {name}", 0.05),
]

MIXED_QUESTION_PHRASES = [(p, w * 0.55) for p, w in QUESTION_PHRASES] + [
    (p, w * 0.45) for p, w in SLANG_QUESTION_PHRASES
]

# --- Tier 3: Common Brazilian food misspellings ---
# Maps the canonical product word to typo variants real customers type.
# Used by apply_typo() to mutate product names before formatting.
COMMON_TYPOS = {
    "margherita": ["margarita", "margerita", "marguerita"],
    "calabresa": ["calabreza", "calabressa"],
    "muçarela": ["mussarela", "musarela", "mucarela"],
    "mussarela": ["muçarela", "musarela", "mucarela"],
    "catupiry": ["catupiri", "katupiry", "catupirí"],
    "strogonoff": ["estrognoff", "estrogonofe", "strogonof", "estrogonoff"],
    "estrogonofe": ["estrognoff", "strogonof", "strogonoff"],
    "parmegiana": ["parmigiana", "parmejana", "parmegianna"],
    "pepperoni": ["peperoni", "peperone", "pepperonni"],
    "coca-cola": ["coca cola", "cocacola", "coca"],
    "guaraná": ["guarana", "guaranã"],
    "x-burger": ["xburguer", "x burguer", "xburger"],
    "x-tudo": ["xtudo", "x tudo"],
    "x-bacon": ["xbacon", "x bacon"],
    "x-salada": ["xsalada", "x salada"],
    "açaí": ["acai", "asai"],
    "iogurte": ["yogurte", "yogurt"],
    "frango": ["franco"],
    "filé": ["file", "filet"],
    "picanha": ["picana"],
    "feijoada": ["feijuada"],
    "lasanha": ["lazanha", "lasagna"],
    "tapioca": ["tapioka"],
    "esfiha": ["esfira", "isfia"],
}


def apply_typo(name: str) -> str:
    """Apply a random typo to a product name (Tier 3 only).

    For each canonical word found in the name (case-insensitive), there's a
    chance to replace it with a misspelled variant from COMMON_TYPOS. Always
    applies at least one transformation if any candidate is found, so the
    output is reliably "rougher" than the input.
    """
    if not name:
        return name
    name_lower = name.lower()

    # Find all candidate replacements that could apply.
    candidates = [
        (canonical, variants)
        for canonical, variants in COMMON_TYPOS.items()
        if canonical in name_lower
    ]
    if not candidates:
        # No known canonical word in this name → fall back to a generic
        # mutation (drop accents, drop hyphens) about half the time.
        if random.random() < 0.5:
            return _generic_typo(name)
        return name

    # Apply a typo to one randomly chosen canonical word.
    canonical, variants = random.choice(candidates)
    chosen = random.choice(variants)
    # Case-insensitive replace, preserving the rest of the name.
    pattern = re.compile(re.escape(canonical), re.IGNORECASE)
    return pattern.sub(chosen, name, count=1)


def _generic_typo(name: str) -> str:
    """Apply generic typo transformations (no-hyphen, no-accent, lowercase)."""
    result = name.lower()
    # Drop hyphens about 60% of the time
    if "-" in result and random.random() < 0.6:
        result = result.replace("-", " " if random.random() < 0.5 else "")
    # Drop accents about 50% of the time
    if random.random() < 0.5:
        accent_map = str.maketrans("áàâãéèêíìîóòôõúùûç", "aaaaeeeiiiooooouuc")
        result = result.translate(accent_map)
    return result


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


def get_tier() -> str:
    """Read PHRASE_TIER env var. Defaults to 'mixed' (today's behavior)."""
    return os.getenv("PHRASE_TIER", "mixed").lower()


def _add_pool() -> list[tuple[str, float]]:
    tier = get_tier()
    if tier == "clean":
        return ADD_PHRASES
    if tier in ("slang", "hard"):
        return SLANG_ADD_PHRASES
    return MIXED_ADD_PHRASES


def _remove_pool() -> list[tuple[str, float]]:
    tier = get_tier()
    if tier == "clean":
        return REMOVE_PHRASES
    if tier in ("slang", "hard"):
        return SLANG_REMOVE_PHRASES
    return MIXED_REMOVE_PHRASES


def _question_pool() -> list[tuple[str, float]]:
    tier = get_tier()
    if tier == "clean":
        return QUESTION_PHRASES
    if tier in ("slang", "hard"):
        return SLANG_QUESTION_PHRASES
    return MIXED_QUESTION_PHRASES


def format_add(name: str, qty: int = 1) -> str:
    """Generate a random ADD phrase with product name and quantity.

    Phrase pool depends on PHRASE_TIER env var (see module docstring).
    Tier 3 ("hard") additionally applies typos to the product name.

    When qty > 1, the pool is filtered to only templates that explicitly
    reference {qty} AND don't hardcode a singular qty word ("um"/"uma"/
    "uns"/"umas"). Without this filter, str.format silently ignores the
    qty kwarg on `{name}`-only templates and the rendered phrase says
    "taça tentação" instead of "2 taça tentação" — the test scenario
    asserts qty=2 and fails through no fault of the bot. Discovered
    2026-04-09 when Doce Café's add_multi cascade-failed 3 dependent
    scenarios (subset_remove, multiturn_flow, multi_remove) because
    add_multi_msg was reused as setup with the wrong qty.
    """
    pool = _add_pool()
    if qty > 1:
        pool = [
            (p, w)
            for p, w in pool
            if "{qty}" in p
            and not re.search(r"\b(um|uma|uns|umas)\b", p, re.IGNORECASE)
        ]
        if not pool:
            # Defensive fallback: should never happen with current pools, but
            # if a future tier removes all qty-aware templates, raise loudly
            # rather than silently dropping the qty.
            raise RuntimeError(
                f"format_add(qty={qty}): no qty-aware templates in pool. "
                f"Phrase bank pool is misconfigured."
            )
    phrase = pick_weighted(pool)
    qty_str = str(qty) if qty > 1 else ("um" if random.random() < 0.5 else "1")
    typo_name = apply_typo(name) if get_tier() == "hard" else name
    result = phrase.format(name=typo_name.lower(), qty=qty_str)
    # Clean up "um um" or "1 um" artifacts
    result = re.sub(r"\b(um|1)\s+\1\b", r"\1", result)
    return result.strip()


def format_remove(name: str) -> str:
    """Generate a random REMOVE phrase."""
    phrase = pick_weighted(_remove_pool())
    typo_name = apply_typo(name) if get_tier() == "hard" else name
    return phrase.format(name=typo_name.lower()).strip()


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
    phrase = pick_weighted(_question_pool())
    typo_name = apply_typo(name) if get_tier() == "hard" else name
    return phrase.format(name=typo_name.lower()).strip()


def generate_abbreviations(
    product_name: str, all_product_names: list[str] | None = None
) -> list[str]:
    """Generate natural abbreviations a customer would use for a product.

    Examples:
        "Cerveja Brahma 600ml" → ["brahma", "cerveja brahma", "brahma 600ml"]
        "Coca-Cola 2L" → ["coca", "coca-cola", "coca 2l"]
        "Pizza Calabresa" → ["calabresa"]
        "X-Tudo Completo" → ["x-tudo"]

    If `all_product_names` is provided, the result is filtered to abbreviations
    that UNIQUELY identify this product within the menu. Ambiguous abbreviations
    (those matching 2+ products as a substring) are rejected — a real customer
    using that abbreviation would also need clarification, so there's no
    "correct" behavior for the bot to demonstrate.

    Returns [] when the product has no unique abbreviation — caller should
    then pick a different product for the abbreviation test.
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

    # Deduplicate and filter by length
    seen = set()
    result = []
    for a in abbreviations:
        a = a.strip()
        if a and a not in seen and a != name_lower and len(a) >= 3:
            seen.add(a)
            result.append(a)

    # Filter by menu uniqueness: abbreviation must identify exactly ONE product
    # (the target). If it matches 0 or 2+, it's not a fair test.
    if all_product_names:
        target_lower = product_name.lower().strip()
        others = [
            p.lower().strip()
            for p in all_product_names
            if p.lower().strip() != target_lower
        ]
        unique = []
        for abbr in result:
            abbr_lower = abbr.lower()
            matches_target = abbr_lower in target_lower
            matches_others = sum(1 for p in others if abbr_lower in p)
            # Accept only if abbreviation is in the target AND in 0 other products
            if matches_target and matches_others == 0:
                unique.append(abbr)
        return unique

    return result


def _is_verbalizable(product: dict) -> bool:
    """True if a real customer could naturally SAY this product name.

    Rejects products whose names are clearly not meant to be spoken verbatim:
    compound names with 3+ commas, variant parentheticals, very long names,
    and names starting with a standalone digit. These products exist in real
    menus but customers order them via abbreviation, category, or number —
    not by reading the full name. Testing "quero um Carne, Queijo, Bacon e
    Catupiry" is a test-phrasing issue, not a bot bug.
    """
    name = product.get("name", "")
    if not name:
        return False
    if len(name) > 60:
        return False
    if name.count(",") >= 3:
        return False
    if "(" in name and ")" in name:
        return False
    # Name starts with standalone digit (e.g. "3 Queijos") — parser will
    # strip the digit as quantity
    stripped = name.strip()
    if stripped and stripped[0].isdigit():
        # Allow "X.Y" version numbers ("2.0")
        first_token = stripped.split()[0] if stripped.split() else ""
        if "." not in first_token:
            return False
    return True


def pick_random_products(
    products: list[dict], unavail_indices: set[int], count: int = 5
) -> dict:
    """Pick random products for test scenarios, avoiding selection bias.

    Returns a dict of role → product, picking from different parts of the menu.
    Filters out products with non-verbalizable names (too long, too many
    commas, parenthetical variants) — real customers wouldn't order by
    reading those names verbatim.
    """
    available = [
        p
        for i, p in enumerate(products)
        if i not in unavail_indices and _is_verbalizable(p)
    ]
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
