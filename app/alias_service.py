"""
Disambiguated product alias generation.

Generates short aliases (abbreviations) for products that uniquely identify
them within their restaurant's menu. Aliases are stored in the Product.keywords
field and are automatically searchable via Layer 3 (Keywords ILIKE) in
find_relevant_products().

Phase 0: Pure string manipulation, zero infrastructure cost.
"""

import logging
import re
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models import Product

logger = logging.getLogger(__name__)

# Category-style prefixes commonly used in Brazilian restaurant product names.
# These are stripped to get the "core" name customers actually say.
_PREFIXES = [
    "cerveja ",
    "refrigerante ",
    "suco de ",
    "suco natural de ",
    "suco ",
    "água ",
    "pizza de ",
    "pizza ",
    "hambúrguer ",
    "hamburguer ",
    "hamburger ",
    "lanche ",
    "pastel de ",
    "pastel ",
    "açaí ",
    "acai ",
    "crepe de ",
    "crepe ",
    "tapioca de ",
    "tapioca ",
    "espetinho de ",
    "espetinho ",
    "caldo de ",
    "caldo ",
    "porção de ",
    "porção ",
    "porcao de ",
    "porcao ",
    "mini ",
    "combo ",
]

# Generic words that don't help identify a product
_GENERIC = frozenset(
    {
        "combo",
        "prato",
        "porção",
        "porcao",
        "grande",
        "pequeno",
        "pequena",
        "medio",
        "média",
        "especial",
        "simples",
        "mini",
        "super",
        "tradicional",
        "caseiro",
        "caseira",
        "artesanal",
        "gourmet",
        "premium",
        "individual",
        "executivo",
        "completo",
        "completa",
        "duplo",
        "dupla",
        "triplo",
    }
)

# Well-known Brazilian contractions
_KNOWN_CONTRACTIONS = {
    "coca-cola": ["coca", "coca cola"],
    "guaraná": ["guarana"],
    "guarana": ["guaraná"],
    "x-burger": ["x burger"],
    "x-tudo": ["x tudo"],
    "x-salada": ["x salada"],
    "x-egg": ["x egg"],
}


def _generate_candidate_aliases(product_name: str) -> list[str]:
    """Generate candidate aliases from a product name (before uniqueness check)."""
    name_lower = product_name.lower().strip()
    candidates = []

    # 1. Without category prefix: "Cerveja Brahma 600ml" → "brahma 600ml"
    for prefix in _PREFIXES:
        if name_lower.startswith(prefix):
            rest = product_name[len(prefix) :].strip()
            if len(rest) > 3:
                candidates.append(rest.lower())
            break  # Only strip one prefix

    # 2. Without size suffix: "Coca-Cola 600ml" → "coca-cola"
    no_size = re.sub(
        r"\s*\d+\s*(?:ml|l|g|kg|un|pç|pecas|peças|unidades?)\b",
        "",
        product_name,
        flags=re.IGNORECASE,
    ).strip()
    if no_size and no_size.lower() != name_lower and len(no_size) > 3:
        candidates.append(no_size.lower())

    # 3. Without prefix AND size: "Cerveja Brahma 600ml" → "brahma"
    for prefix in _PREFIXES:
        if name_lower.startswith(prefix):
            rest = product_name[len(prefix) :].strip()
            rest_no_size = re.sub(
                r"\s*\d+\s*(?:ml|l|g|kg|un|pç|pecas|peças|unidades?)\b",
                "",
                rest,
                flags=re.IGNORECASE,
            ).strip()
            if (
                rest_no_size
                and len(rest_no_size) > 3
                and rest_no_size.lower() != name_lower
            ):
                candidates.append(rest_no_size.lower())
            break

    # 4. First significant word (brand): "Cerveja Brahma 600ml" → "brahma"
    prefix_first_words = {p.split()[0] for p in _PREFIXES}
    words = product_name.split()
    for w in words:
        clean = w.lower().strip(",-.()")
        if (
            len(clean) > 3
            and clean not in _GENERIC
            and clean not in prefix_first_words
            and not re.match(r"\d", clean)
        ):
            candidates.append(clean)
            break

    # 5. Known contractions
    for key, contractions in _KNOWN_CONTRACTIONS.items():
        if key in name_lower:
            candidates.extend(contractions)

    # 6. Without parenthetical: "Hot Roll (10 unidades)" → "hot roll"
    no_paren = re.sub(r"\s*\([^)]*\)", "", product_name).strip()
    if no_paren and no_paren.lower() != name_lower and len(no_paren) > 3:
        candidates.append(no_paren.lower())

    # Deduplicate, filter
    seen = set()
    result = []
    for alias in candidates:
        alias = alias.strip()
        if alias and alias not in seen and alias != name_lower and len(alias) >= 3:
            seen.add(alias)
            result.append(alias)
    return result


def generate_product_aliases(
    product_name: str,
    other_product_names: list[str],
) -> list[str]:
    """Generate aliases that uniquely identify a product within its menu.

    Only produces aliases that don't collide with other products.
    """
    candidates = _generate_candidate_aliases(product_name)
    other_names_lower = [n.lower() for n in other_product_names]

    unique = []
    for alias in candidates:
        # Check: does this alias appear as a substring in any OTHER product name?
        collides = any(alias in other_name for other_name in other_names_lower)
        if not collides:
            unique.append(alias)

    return unique


def merge_aliases_into_keywords(
    existing_keywords: Optional[str],
    aliases: list[str],
) -> str:
    """Merge new aliases into existing keywords string without duplicates."""
    existing = set()
    if existing_keywords:
        existing = {
            k.strip().lower() for k in existing_keywords.split(",") if k.strip()
        }

    new_parts = []
    if existing_keywords:
        new_parts.append(existing_keywords.rstrip(", "))

    for alias in aliases:
        if alias.lower() not in existing:
            new_parts.append(alias)
            existing.add(alias.lower())

    return ", ".join(new_parts) if new_parts else ""


async def regenerate_aliases_for_bot(
    session: AsyncSession,
    bot_id: int,
) -> int:
    """Regenerate aliases for ALL products of a bot.

    Should be called after:
    - Cadastro Mágico extraction (all products created)
    - Product creation/update/deletion (menu changed)

    Returns the number of products that got new aliases.
    """
    result = await session.execute(
        select(Product).where(
            Product.bot_id == bot_id,
            Product.is_deleted == False,  # noqa: E712
        )
    )
    products = list(result.scalars().all())

    if not products:
        return 0

    all_names = [p.name for p in products]
    updated = 0

    for product in products:
        other_names = [n for n in all_names if n != product.name]
        aliases = generate_product_aliases(product.name, other_names)

        if aliases:
            new_keywords = merge_aliases_into_keywords(product.keywords, aliases)
            if new_keywords != (product.keywords or ""):
                product.keywords = new_keywords
                session.add(product)
                updated += 1
                logger.info(
                    "ALIAS bot=%s product='%s' aliases=%s",
                    bot_id,
                    product.name,
                    aliases,
                )

    if updated:
        await session.flush()

    logger.info(
        "ALIAS_REGEN bot=%s total=%d updated=%d",
        bot_id,
        len(products),
        updated,
    )
    return updated
