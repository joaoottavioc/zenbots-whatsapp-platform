# app/menu_extraction.py
"""Async menu extraction worker job.

Processes uploaded menu files (PDF/image) in the ARQ worker,
extracts products via AI, saves to DB, and broadcasts progress via SSE.

Tiered extraction pipeline:
1. Text extraction (fitz → pdfplumber fallback) + quality check
2. gpt-4o-mini text-only (fast path, ~1-3s)
3. gpt-4o-mini vision (slow path, ~5-15s)
4. File hash caching via Redis DB 2 (skip re-processing identical files)
"""

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import re
from typing import List, Dict, Optional

import httpx
import redis.asyncio as redis

from app.broadcast import broadcast_order_update
from app.crud import bulk_create_products
from app.database import async_session
from app.openai_client import extract_products_from_image, get_forced_tool_call

logger = logging.getLogger(__name__)

# Redis DB 2 for file hash cache (same DB as monitoring counters)
_REDIS_HOST = os.getenv("REDIS_HOST", "redis")
_REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
_CACHE_TTL = 86400  # 24 hours

# Max image dimension before sending to vision API (reduces token count)
_MAX_IMAGE_DIM = 2048
# Render quality for PDF→image. 200 DPI + q=90 trades ~30% more tokens per call
# for materially better legibility of small text (sidebar prices, section
# footers) where the vision model was silently dropping items.
_PDF_RENDER_DPI = 200
_JPEG_QUALITY = 90

# Below this count, consolidation is a no-op (not enough variety to canonicalize).
_CONSOLIDATION_MIN_PRODUCTS = 3
# Ceiling on canonical categories the LLM is allowed to emit.
_CONSOLIDATION_MAX_CATEGORIES = 12

# Partial-failure guard: refuse to overwrite the current catalog if a new
# extraction returns drastically fewer products than what's already active.
# Catches the "some vision pages 429'd → half the menu silently evaporates"
# failure mode. Only applies when the current catalog is nontrivial.
_PARTIAL_FAILURE_MIN_EXISTING = 10
_PARTIAL_FAILURE_RATIO = 0.5

# Cap simultaneous vision calls. Each page at detail=high is ~25.5K tokens,
# so the real Tier-1 TPM constraint (200K/min) means we can only run ~7
# calls/min no matter what — concurrency just controls burst pattern.
# Setting this to 2 keeps peak burst ~51K tokens and gives space for
# consolidation + validator without blowing the window.
_VISION_CONCURRENCY = 2
# Pre-retry cooldown: after the first pass bursts most of the TPM budget,
# firing retries immediately just reproduces the 429s. Wait for the rolling
# window to age out before retrying dropped pages.
_RETRY_COOLDOWN_SECONDS = 20
_vision_semaphore: Optional[asyncio.Semaphore] = None


def _get_vision_semaphore() -> asyncio.Semaphore:
    """Lazy-init so the semaphore binds to the running event loop."""
    global _vision_semaphore
    if _vision_semaphore is None:
        _vision_semaphore = asyncio.Semaphore(_VISION_CONCURRENCY)
    return _vision_semaphore


def _count_price_patterns(text: str) -> int:
    """Count `\\d+[,\\.]\\d{2}` patterns — a strong proxy for item count.

    Same pattern used by `_is_text_quality_sufficient`. Over-counts on
    decorative text but under-counting is the worse failure mode here
    (we want to encourage the vision model to keep looking).
    """
    if not text:
        return 0
    return len(re.findall(r"\d+[,\.]\d{2}", text))


async def _extract_with_limit(
    image: bytes,
    page_text: Optional[str],
    retry_hint: bool = False,
    expected_item_count: Optional[int] = None,
):
    """Run one vision extraction under the concurrency semaphore."""
    sem = _get_vision_semaphore()
    async with sem:
        return await extract_products_from_image(
            image,
            "image/jpeg",
            page_text=page_text,
            retry_hint=retry_hint,
            expected_item_count=expected_item_count,
        )


# Tool schema for the consolidation call. Forcing a tool_call gets us
# API-side argument validation, which is strictly more reliable than
# response_format=json_object with prose instructions.
_CONSOLIDATION_TOOL = {
    "type": "function",
    "function": {
        "name": "apply_category_consolidation",
        "description": (
            "Consolida categorias duplicadas/inconsistentes de uma lista de "
            "produtos e atribui cada produto a exatamente uma categoria canônica."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "canonical_categories": {
                    "type": "array",
                    "description": (
                        "Lista final de categorias canônicas em Title Case "
                        "Português. Máximo 12 itens."
                    ),
                    "items": {"type": "string"},
                },
                "assignments": {
                    "type": "array",
                    "description": (
                        "Uma entrada por produto, na ordem recebida. Cada "
                        "category DEVE aparecer também em canonical_categories."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "category": {"type": "string"},
                        },
                        "required": ["index", "category"],
                    },
                },
            },
            "required": ["canonical_categories", "assignments"],
        },
    },
}


def _resize_and_compress(image_bytes: bytes) -> tuple[bytes, str]:
    """Resize image to max _MAX_IMAGE_DIM px and compress to JPEG.

    Returns (compressed_bytes, media_type).
    """
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes))

    # Resize if larger than _MAX_IMAGE_DIM on any side
    if max(img.size) > _MAX_IMAGE_DIM:
        img.thumbnail((_MAX_IMAGE_DIM, _MAX_IMAGE_DIM), Image.LANCZOS)

    # Convert to RGB (JPEG doesn't support alpha)
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return buf.getvalue(), "image/jpeg"


# Lines that are nothing but a price, e.g. "48.00" or "R$ 128,00".
_STANDALONE_PRICE_LINE = re.compile(r"^\s*R?\$?\s*\d+[,.]\d{2}\s*$")

# When this fraction of non-empty lines is a standalone price, the PDF is
# almost certainly a multi-column visual layout that fitz/pdfplumber have
# flattened into jumbled text. Vision handles these far more reliably.
_MULTI_COLUMN_PRICE_LINE_RATIO = 0.08


def _is_text_quality_sufficient(text: str) -> tuple[bool, str]:
    """Check if extracted text is good enough for text-only LLM extraction.

    Returns (is_sufficient, reason) where reason explains why it failed.
    A restaurant menu should have prices and readable words.
    """
    stripped = text.strip()

    if len(stripped) < 50:
        return False, "too_short"

    # Menus should have prices (e.g., 12,90 or 12.90 or R$ 15)
    price_pattern = re.compile(r"\d+[,\.]\d{2}")
    price_count = len(price_pattern.findall(stripped))
    if price_count < 2:
        return False, "no_prices"

    # Should have enough distinct words (not garbled OCR noise)
    words = set(stripped.lower().split())
    if len(words) < 10:
        return False, "too_few_words"

    # Character quality: mostly readable characters (not binary/garbled)
    alphanumeric_chars = sum(1 for c in stripped if c.isalnum() or c.isspace())
    ratio = alphanumeric_chars / len(stripped) if stripped else 0
    if ratio < 0.65:
        return False, "garbled_text"

    # Multi-column layout detection: when prices float on their own lines
    # the text path produces product/price/description mis-associations the
    # LLM can't recover from. Fall through to vision for these menus.
    non_empty_lines = [line for line in stripped.split("\n") if line.strip()]
    if non_empty_lines:
        standalone_prices = sum(
            1 for line in non_empty_lines if _STANDALONE_PRICE_LINE.match(line)
        )
        if standalone_prices / len(non_empty_lines) > _MULTI_COLUMN_PRICE_LINE_RATIO:
            return False, "multi_column_layout"

    return True, "ok"


def _extract_text_with_pdfplumber(contents: bytes) -> str:
    """Fallback PDF text extraction using pdfplumber (better at tables/columns)."""
    import pdfplumber

    text_parts = []
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        for i, page in enumerate(pdf.pages[:15]):
            page_text = page.extract_text()
            if page_text and page_text.strip():
                text_parts.append(page_text.strip())
    return "\n\n".join(text_parts)


def _compute_file_hash(contents: bytes) -> str:
    """SHA-256 hash of file contents for dedup caching."""
    return hashlib.sha256(contents).hexdigest()


async def _get_cached_products(bot_id: int, file_hash: str) -> Optional[List[Dict]]:
    """Check Redis for cached extraction results."""
    try:
        r = redis.from_url(
            f"redis://{_REDIS_HOST}:{_REDIS_PORT}/2",
            decode_responses=True,
            socket_timeout=2,
        )
        cached = await r.get(f"menu_cache:{bot_id}:{file_hash}")
        await r.aclose()
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.debug("Cache lookup failed: %s", e)
    return None


async def _set_cached_products(
    bot_id: int, file_hash: str, products: List[Dict]
) -> None:
    """Store extraction results in Redis cache."""
    try:
        r = redis.from_url(
            f"redis://{_REDIS_HOST}:{_REDIS_PORT}/2",
            decode_responses=True,
            socket_timeout=2,
        )
        await r.setex(
            f"menu_cache:{bot_id}:{file_hash}",
            _CACHE_TTL,
            json.dumps(products, ensure_ascii=False),
        )
        await r.aclose()
    except Exception as e:
        logger.debug("Cache write failed: %s", e)


def _detect_file_type(
    contents: bytes, filename: str | None, content_type: str | None
) -> tuple[bool, bool, str | None]:
    """Detect whether file is PDF or image. Returns (is_pdf, is_image, mime)."""
    if contents[:4] == b"%PDF":
        return True, False, None
    if contents[:3] == b"\xff\xd8\xff":
        return False, True, "image/jpeg"
    if contents[:8] == b"\x89PNG\r\n\x1a\n":
        return False, True, "image/png"
    if len(contents) >= 12 and contents[:4] == b"RIFF" and contents[8:12] == b"WEBP":
        return False, True, "image/webp"

    ext = (filename or "").lower().rsplit(".", 1)[-1] if filename else ""
    ct = (content_type or "").lower()
    if ext == "pdf" or "pdf" in ct:
        return True, False, None
    if ext in ("jpg", "jpeg", "png", "webp") or ct.startswith("image/"):
        return False, True, content_type
    return False, False, None


def _extract_text_from_pdf(contents: bytes) -> str:
    """Try to extract selectable text from PDF using fitz."""
    return "\n\n".join(_extract_pages_text_from_pdf(contents))


def _extract_pages_text_from_pdf(contents: bytes) -> list[str]:
    """Extract selectable text from each PDF page using fitz.

    Returns one entry per non-empty page (up to 15 pages). Used to parallelize
    the text-only extraction LLM calls, one per page.
    """
    import fitz

    doc = fitz.open(stream=contents, filetype="pdf")
    pages: list[str] = []
    for i in range(min(len(doc), 15)):
        page_text = doc.load_page(i).get_text()
        if page_text.strip():
            pages.append(page_text.strip())
    doc.close()
    return pages


def _pdf_to_images(contents: bytes) -> list[bytes]:
    """Convert PDF pages to JPEG images at `_PDF_RENDER_DPI`, resized and compressed."""
    import fitz

    doc = fitz.open(stream=contents, filetype="pdf")
    images = []
    max_pages = 15
    for i in range(min(len(doc), max_pages)):
        page = doc.load_page(i)
        pix = page.get_pixmap(dpi=_PDF_RENDER_DPI)
        page_bytes = pix.tobytes("png")
        compressed, _ = _resize_and_compress(page_bytes)
        images.append(compressed)
    doc.close()
    return images


def _extract_all_pages_text_from_pdf(contents: bytes) -> list[str]:
    """Return fitz-extracted text for EVERY page (empty string if none).

    Differs from `_extract_pages_text_from_pdf`, which skips empty pages for
    the text-only LLM path. Here the list must align 1:1 with `_pdf_to_images`
    so the vision path can pass matching per-page text as extra context.
    """
    import fitz

    doc = fitz.open(stream=contents, filetype="pdf")
    pages: list[str] = []
    for i in range(min(len(doc), 15)):
        page_text = doc.load_page(i).get_text() or ""
        pages.append(page_text.strip())
    doc.close()
    return pages


def _dedup_products(products: List[Dict]) -> List[Dict]:
    """Deduplicate by (normalized_name, price) so size variants survive.

    Keyed on (name, price): two products with the same name and same price
    collapse (keeping the richer description); same name at different prices
    — e.g. "Salada Julienne (Individual)" R$48 vs "Salada Julienne (Serve 2 a 3)"
    R$62 — stay distinct.
    """
    import unicodedata

    seen: Dict[tuple, Dict] = {}
    for p in products:
        name = p.get("name", "")
        name_key = (
            unicodedata.normalize("NFKD", name)
            .encode("ascii", "ignore")
            .decode()
            .lower()
            .strip()
        )
        if not name_key:
            continue
        try:
            price_key = round(float(p.get("price") or 0.0), 2)
        except (TypeError, ValueError):
            price_key = 0.0
        key = (name_key, price_key)
        existing = seen.get(key)
        if existing is None:
            seen[key] = p
        else:
            if len(p.get("description", "") or "") > len(
                existing.get("description", "") or ""
            ):
                seen[key] = p
    return list(seen.values())


def _enrich_products(products: List[Dict]) -> List[Dict]:
    """Add keywords and is_available flag to extracted products."""
    enriched = []
    for product in products:
        # LLMs sometimes emit explicit JSON null instead of omitting the key,
        # so `.get(k, "")` isn't safe — fall through to "" on None too.
        name_words = (product.get("name") or "").lower()
        desc_words = (product.get("description") or "").lower()
        full_text = name_words + " " + desc_words
        words = set(re.findall(r"\b\w+\b", full_text))
        product["keywords"] = list(words)
        product["is_available"] = True
        enriched.append(product)
    return enriched


def _build_consolidation_prompt(
    products: List[Dict], existing_categories: Optional[List[str]]
) -> List[Dict]:
    """Build the messages for the consolidation LLM call.

    Only sends index + name + current category + price per product (no
    description) to keep token count down — the model doesn't need the full
    description to decide canonical category membership.
    """
    lines = []
    for i, p in enumerate(products):
        name = (p.get("name") or "").strip()[:120]
        cat = (p.get("category") or "Geral").strip()[:80]
        try:
            price = float(p.get("price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        lines.append(f"{i}. [{cat}] {name} — R$ {price:.2f}")
    product_block = "\n".join(lines)

    reuse_hint = ""
    if existing_categories:
        reuse_hint = (
            "\nCATEGORIAS JÁ USADAS POR ESTE RESTAURANTE (reutilize quando aplicável "
            "para manter consistência entre uploads):\n- "
            + "\n- ".join(existing_categories[:_CONSOLIDATION_MAX_CATEGORIES])
            + "\n"
        )

    user_prompt = f"""Você receberá uma lista de {len(products)} produtos de um cardápio de restaurante. Como foram extraídos página por página, categorias semanticamente equivalentes aparecem com nomes diferentes ("Cortes Clássicos", "Carnes", "Pratos Principais" etc).

Sua saída DEVE ser um JSON neste formato EXATO:

{{
  "canonical_categories": ["Entradas", "Cortes", "Saladas", "Acompanhamentos"],
  "assignments": [
    {{"index": 0, "category": "Entradas"}},
    {{"index": 1, "category": "Cortes"}}
  ]
}}

REGRAS:
1. "canonical_categories" tem no máximo {_CONSOLIDATION_MAX_CATEGORIES} entradas, em Title Case Português.
2. "assignments" tem EXATAMENTE {len(products)} entradas — uma para cada índice de 0 a {len(products) - 1}, SEM exceção.
3. Todo valor de "category" em "assignments" DEVE estar em "canonical_categories".
4. NÃO inclua nomes, preços ou descrições dos produtos na saída. Apenas index + category.
5. Preserve distinções reais do cardápio: individual vs. compartilhar, frio vs. quente. Não funda Bebidas, Sobremesas e Adicionais.
{reuse_hint}
LISTA DE PRODUTOS (formato: índice. [categoria_atual] nome — preço):
{product_block}

Gere o JSON agora."""

    return [
        {
            "role": "system",
            "content": (
                "Você padroniza categorias de cardápios de restaurantes. "
                "Sua saída é SEMPRE um objeto JSON com as chaves "
                '"canonical_categories" e "assignments". Nunca retorne um '
                "array na raiz. Nunca retorne um objeto vazio. Nunca altere "
                "nomes, descrições ou preços dos produtos."
            ),
        },
        {"role": "user", "content": user_prompt},
    ]


def _apply_consolidation(products: List[Dict], raw_json: str) -> Optional[List[Dict]]:
    """Validate consolidation LLM output and apply it to products.

    Returns a new list with rewritten categories on success, or None if any
    validation check fails — in which case the caller should keep the
    original list unchanged.
    """
    # First 200 chars of the raw response — included in every skip log so
    # we can diagnose what the LLM actually returned when validation fails.
    preview = (raw_json or "")[:200].replace("\n", " ")

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        logger.warning("CONSOLIDATION skipped: invalid JSON | raw=%r", preview)
        return None

    if not isinstance(data, dict):
        logger.warning(
            "CONSOLIDATION skipped: response is %s, not an object | raw=%r",
            type(data).__name__,
            preview,
        )
        return None

    canonical = data.get("canonical_categories")
    assignments = data.get("assignments")

    if not isinstance(canonical, list) or not canonical:
        logger.warning(
            "CONSOLIDATION skipped: canonical_categories missing/empty | keys=%s | raw=%r",
            list(data.keys()),
            preview,
        )
        return None
    if not all(isinstance(c, str) and c.strip() for c in canonical):
        logger.warning(
            "CONSOLIDATION skipped: canonical_categories has non-strings | raw=%r",
            preview,
        )
        return None
    if len(canonical) > _CONSOLIDATION_MAX_CATEGORIES:
        logger.warning(
            "CONSOLIDATION skipped: %d canonical categories exceeds cap %d | raw=%r",
            len(canonical),
            _CONSOLIDATION_MAX_CATEGORIES,
            preview,
        )
        return None

    canonical_set = {c.strip() for c in canonical}

    if not isinstance(assignments, list) or len(assignments) != len(products):
        logger.warning(
            "CONSOLIDATION skipped: %s assignments for %d products | raw=%r",
            len(assignments) if isinstance(assignments, list) else "non-list",
            len(products),
            preview,
        )
        return None

    by_index: Dict[int, str] = {}
    for entry in assignments:
        if not isinstance(entry, dict):
            logger.warning(
                "CONSOLIDATION skipped: assignment is not an object | raw=%r", preview
            )
            return None
        idx = entry.get("index")
        cat = entry.get("category")
        if not isinstance(idx, int) or idx < 0 or idx >= len(products):
            logger.warning("CONSOLIDATION skipped: bad index %r | raw=%r", idx, preview)
            return None
        if idx in by_index:
            logger.warning(
                "CONSOLIDATION skipped: duplicate index %d | raw=%r", idx, preview
            )
            return None
        if not isinstance(cat, str) or cat.strip() not in canonical_set:
            logger.warning(
                "CONSOLIDATION skipped: category %r not in canonical set | raw=%r",
                cat,
                preview,
            )
            return None
        by_index[idx] = cat.strip()

    if len(by_index) != len(products):
        logger.warning(
            "CONSOLIDATION skipped: coverage %d/%d | raw=%r",
            len(by_index),
            len(products),
            preview,
        )
        return None

    rewritten = []
    for i, p in enumerate(products):
        new_p = dict(p)
        new_p["category"] = by_index[i]
        rewritten.append(new_p)
    return rewritten


async def _count_active_products(bot_id: int) -> int:
    """Return how many products are currently active for this bot. Used by
    the partial-failure guard; returns 0 on DB error so we fail open rather
    than block a legitimate first upload.
    """
    try:
        from sqlalchemy import func, select

        from app.models import Product

        async with async_session() as session:
            stmt = select(func.count(Product.id)).where(
                Product.bot_id == bot_id,
                Product.is_available == True,  # noqa: E712
                Product.is_deleted == False,  # noqa: E712
            )
            result = await session.execute(stmt)
            return int(result.scalar() or 0)
    except Exception as e:
        logger.debug("Active-product count lookup failed: %s", e)
        return 0


async def _fetch_existing_categories(bot_id: int) -> List[str]:
    """Return the bot's currently active canonical categories.

    Used to seed the consolidation prompt so re-uploads don't drift into a
    new set of category names on each run. Silent on error — drift is a
    minor UX issue, not worth failing extraction over.
    """
    try:
        from sqlalchemy import select

        from app.models import Product

        async with async_session() as session:
            stmt = (
                select(Product.category)
                .where(
                    Product.bot_id == bot_id,
                    Product.is_available == True,  # noqa: E712
                    Product.is_deleted == False,  # noqa: E712
                )
                .distinct()
            )
            result = await session.execute(stmt)
            cats = [c.strip() for c in result.scalars().all() if c and c.strip()]
        return sorted(set(cats))
    except Exception as e:
        logger.debug("Existing-categories lookup failed: %s", e)
        return []


async def _consolidate_categories(
    products: List[Dict], existing_categories: Optional[List[str]] = None
) -> List[Dict]:
    """Normalize categories across a product list via one LLM pass.

    Per-page vision extraction produces inconsistent category names for the
    same semantic group. This pass rewrites each product's `category` field
    against a small canonical set, leaving name/description/price untouched.

    Returns the original list unchanged if:
      - the list is too small to warrant consolidation
      - the LLM output fails any validation check
    """
    if len(products) < _CONSOLIDATION_MIN_PRODUCTS:
        return products

    try:
        messages = _build_consolidation_prompt(products, existing_categories)
        raw = await get_forced_tool_call(messages, _CONSOLIDATION_TOOL)
    except Exception as e:
        logger.warning("CONSOLIDATION call failed: %s", e)
        return products

    applied = _apply_consolidation(products, raw)
    if applied is None:
        return products

    # Log the category diff so we can audit consolidation decisions.
    before = sorted({(p.get("category") or "Geral").strip() for p in products})
    after = sorted({p["category"] for p in applied})
    logger.info(
        "CONSOLIDATION ok: %d → %d categories (%s → %s)",
        len(before),
        len(after),
        before,
        after,
    )
    return applied


async def _validate_extraction(
    products: List[Dict], raw_text: str, bot_id: int
) -> None:
    """Log-only post-extraction sanity check.

    Asks gpt-4o-mini to compare the extracted list against the raw PDF
    text and report likely missing items or suspicious prices. Output is
    logged (not used for gating) so we can audit extraction quality over
    time and plan future retry logic without risking false aborts now.
    """
    if not raw_text.strip() or len(products) < 3:
        return

    # Cap input size — validator is best-effort observability.
    text_snippet = raw_text[:8000]
    product_summary = "\n".join(
        f"- {p.get('name', '')} — R$ {float(p.get('price') or 0):.2f}" for p in products
    )[:4000]

    messages = [
        {
            "role": "system",
            "content": (
                "Você audita extrações de cardápios. Dado o texto bruto do PDF "
                "e a lista de produtos extraídos, identifique itens provavelmente "
                "faltando ou preços que parecem incorretos. Seja conciso. "
                "Sempre responda em JSON."
            ),
        },
        {
            "role": "user",
            "content": (
                f"TEXTO DO PDF:\n---\n{text_snippet}\n---\n\n"
                f"PRODUTOS EXTRAÍDOS ({len(products)}):\n{product_summary}\n\n"
                "Responda em JSON: "
                '{"likely_missing": ["nome1", ...], "suspicious_prices": ["nome: motivo", ...]}. '
                "Se tudo parece OK, retorne listas vazias."
            ),
        },
    ]

    try:
        from app.openai_client import get_extraction_response

        raw = await get_extraction_response(messages)
        data = json.loads(raw) if raw else {}
        missing = data.get("likely_missing") or []
        suspicious = data.get("suspicious_prices") or []
        if missing or suspicious:
            logger.info(
                "VALIDATION bot=%d extracted=%d missing=%s suspicious=%s",
                bot_id,
                len(products),
                missing[:10],
                suspicious[:10],
            )
        else:
            logger.info(
                "VALIDATION bot=%d extracted=%d — no concerns flagged",
                bot_id,
                len(products),
            )
    except Exception as e:
        logger.debug("Validator pass failed (non-fatal): %s", e)


async def _broadcast_progress(bot_id: int, status: str, message: str, count: int = 0):
    """Send extraction progress update via SSE."""
    await broadcast_order_update(
        "menu_extraction",
        {
            "bot_id": bot_id,
            "status": status,
            "message": message,
            "product_count": count,
        },
        bot_id=bot_id,
    )


def _extract_pdf_text_tiered(contents: bytes) -> tuple[str, str]:
    """Try fitz first, then pdfplumber if quality is insufficient.

    Returns (text, method) where method is 'fitz', 'pdfplumber', or 'none'.
    Multi-column layouts skip pdfplumber entirely — its default extract_text
    also linearizes, so it produces the same jumbled output but can take
    minutes on image-heavy PDFs. Vision is the right fallback here.
    """
    fitz_text = _extract_text_from_pdf(contents)
    is_good, reason = _is_text_quality_sufficient(fitz_text)
    if is_good:
        return fitz_text, "fitz"

    if reason == "multi_column_layout":
        logger.info(
            "fitz reports multi-column layout; skipping pdfplumber, using vision"
        )
        return "", "none"

    logger.info("fitz text failed quality check (%s), trying pdfplumber", reason)

    try:
        plumber_text = _extract_text_with_pdfplumber(contents)
        is_good, reason = _is_text_quality_sufficient(plumber_text)
        if is_good:
            return plumber_text, "pdfplumber"
        logger.info("pdfplumber text also failed quality check (%s)", reason)
    except Exception as e:
        logger.warning("pdfplumber extraction failed: %s", e)

    return "", "none"


async def process_menu_extraction(
    ctx, bot_id: int, file_b64: str, filename: str, content_type: str
):
    """ARQ worker job: extract products from uploaded menu file.

    Tiered extraction pipeline:
    1. File hash cache check (instant return for identical files)
    2. PDF text extraction: fitz → pdfplumber fallback → quality heuristic
    3. Text-only gpt-4o-mini (fast path, ~1-3s) if text quality is sufficient
    4. Vision gpt-4o-mini (slow path, ~5-15s) as final fallback
    """
    logger.info("Starting menu extraction job for bot_id=%d file=%s", bot_id, filename)
    await _broadcast_progress(bot_id, "started", "Processando cardápio...")

    contents = base64.b64decode(file_b64)
    file_hash = _compute_file_hash(contents)

    # Check cache first
    cached = await _get_cached_products(bot_id, file_hash)
    if cached:
        logger.info(
            "Cache hit for bot_id=%d hash=%s, %d products",
            bot_id,
            file_hash[:12],
            len(cached),
        )
        # Partial-failure guard also applies to cache-hit: a cache entry
        # written during an earlier degraded run would otherwise wipe the
        # recovered catalog on every re-upload of the same file.
        existing_count = await _count_active_products(bot_id)
        if (
            existing_count >= _PARTIAL_FAILURE_MIN_EXISTING
            and len(cached) < existing_count * _PARTIAL_FAILURE_RATIO
        ):
            logger.error(
                "Cache-hit suppressed: cached %d products vs %d existing active. "
                "Keeping current catalog for bot_id=%d.",
                len(cached),
                existing_count,
                bot_id,
            )
            await _broadcast_progress(
                bot_id,
                "error",
                "Cache de extração parece incompleto. "
                "O cardápio atual foi preservado. "
                "Apague o cache ou faça upload de um arquivo diferente.",
            )
            return
        enriched = _enrich_products(cached)
        async with async_session() as session:
            count = await bulk_create_products(
                session=session, bot_id=bot_id, products_data=enriched
            )
        await _broadcast_progress(
            bot_id,
            "completed",
            f"Sucesso! {count} produtos cadastrados a partir do cardápio.",
            count=count,
        )
        return

    is_pdf, is_image, detected_mime = _detect_file_type(
        contents, filename, content_type
    )

    all_products: List[Dict] = []
    extraction_tier = "unknown"

    try:
        if is_pdf:
            # Tiered text extraction: fitz → pdfplumber → quality check
            pdf_text, text_method = _extract_pdf_text_tiered(contents)

            if text_method != "none":
                # Fast path: text-only gpt-4o-mini per page in parallel (~10-20s for 15 pages
                # vs ~100s for a single call on the joined text).
                logger.info(
                    "PDF text quality OK via %s (%d chars), using text extraction",
                    text_method,
                    len(pdf_text),
                )
                extraction_tier = f"text-{text_method}"
                await _broadcast_progress(
                    bot_id, "extracting", "Extraindo produtos do texto..."
                )
                from app.data_extractor import extract_products_from_text

                # fitz per-page chunking enables parallelism; pdfplumber joined text
                # is a single chunk (falls through to the original behavior).
                if text_method == "fitz":
                    pages = _extract_pages_text_from_pdf(contents)
                else:
                    pages = [pdf_text]

                results = await asyncio.gather(
                    *(extract_products_from_text(p) for p in pages),
                    return_exceptions=True,
                )
                for idx, page_products in enumerate(results):
                    if isinstance(page_products, Exception):
                        logger.warning(
                            "Page %d text extraction failed: %s",
                            idx + 1,
                            page_products,
                        )
                        continue
                    if page_products:
                        all_products.extend(page_products)

                if len(pages) > 1:
                    before = len(all_products)
                    all_products = _dedup_products(all_products)
                    logger.info(
                        "Per-page dedup: %d → %d products", before, len(all_products)
                    )
            else:
                # Slow path: vision API with optimized images. Pass fitz
                # per-page text alongside each image (hybrid text+vision) so
                # the model can cross-reference prices/names it would
                # otherwise need to OCR from scratch.
                logger.info("PDF text quality insufficient, using vision API")
                extraction_tier = "vision"
                await _broadcast_progress(
                    bot_id, "extracting", "Analisando imagens do cardápio..."
                )
                page_images = _pdf_to_images(contents)
                page_texts = _extract_all_pages_text_from_pdf(contents)
                # Pad in case page counts disagree (image count is authoritative).
                while len(page_texts) < len(page_images):
                    page_texts.append("")

                # Pre-compute per-page price counts from the fitz text — used
                # both as a first-pass soft hint and as a retry target.
                page_price_counts = [_count_price_patterns(t or "") for t in page_texts]
                tasks = [
                    _extract_with_limit(
                        img,
                        page_texts[i] or None,
                        expected_item_count=page_price_counts[i] or None,
                    )
                    for i, img in enumerate(page_images)
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # Retry pages that returned 0 products (or raised). A silent
                # empty page is the single biggest source of coverage loss.
                # Only retry pages where fitz actually found prices — if
                # fitz saw nothing (e.g. a cover page or divider), the first
                # empty result is probably correct.
                retry_indices = [
                    i
                    for i, r in enumerate(results)
                    if (isinstance(r, Exception) or not r) and page_price_counts[i] > 0
                ]
                if retry_indices:
                    logger.info(
                        "Retrying %d empty/failed pages: %s (price counts: %s) "
                        "after %ds cooldown",
                        len(retry_indices),
                        [i + 1 for i in retry_indices],
                        [page_price_counts[i] for i in retry_indices],
                        _RETRY_COOLDOWN_SECONDS,
                    )
                    # Wait for TPM to age out — firing retries immediately
                    # after a burst run just reproduces the 429 storm.
                    await asyncio.sleep(_RETRY_COOLDOWN_SECONDS)
                    retry_tasks = [
                        _extract_with_limit(
                            page_images[i],
                            page_texts[i] or None,
                            retry_hint=True,
                            expected_item_count=page_price_counts[i],
                        )
                        for i in retry_indices
                    ]
                    retry_results = await asyncio.gather(
                        *retry_tasks, return_exceptions=True
                    )
                    for orig_idx, r in zip(retry_indices, retry_results):
                        if not isinstance(r, Exception) and r:
                            results[orig_idx] = r
                            logger.info(
                                "Retry recovered page %d: %d products",
                                orig_idx + 1,
                                len(r),
                            )

                for idx, page_products in enumerate(results):
                    if isinstance(page_products, Exception):
                        logger.warning(
                            "Page %d vision call failed: %s", idx + 1, page_products
                        )
                        continue
                    if page_products:
                        all_products.extend(page_products)

        elif is_image:
            extraction_tier = "vision"
            await _broadcast_progress(
                bot_id, "extracting", "Analisando imagem do cardápio..."
            )
            compressed, mime = _resize_and_compress(contents)
            all_products = await extract_products_from_image(compressed, mime)

        if not all_products:
            logger.warning("No products extracted for bot_id=%d", bot_id)
            await _broadcast_progress(
                bot_id,
                "error",
                "A IA não identificou nenhum produto. Verifique se o arquivo está legível.",
            )
            return

        # Partial-failure guard: if the bot already has a substantial menu
        # and this extraction came back with less than half of it, treat the
        # run as degraded (some vision pages probably 429'd or timed out)
        # and keep the existing catalog. Without this, bulk_create_products
        # would soft-delete every product missing from the partial result.
        existing_count = await _count_active_products(bot_id)
        if (
            existing_count >= _PARTIAL_FAILURE_MIN_EXISTING
            and len(all_products) < existing_count * _PARTIAL_FAILURE_RATIO
        ):
            logger.error(
                "Extraction degraded: %d new products vs %d existing active. "
                "Keeping current catalog for bot_id=%d.",
                len(all_products),
                existing_count,
                bot_id,
            )
            await _broadcast_progress(
                bot_id,
                "error",
                "Extração incompleta (possível limite de API). "
                "O cardápio atual foi preservado. Tente novamente em alguns minutos.",
            )
            return

        # Post-extraction category consolidation (option B).
        # Runs on the in-memory list before caching, so cache reflects
        # canonical categories and re-uploads get stable results.
        #
        # Cooldown: a 15-page vision run bursts ~75K tokens in 10-15s, which
        # parks us at the top of OpenAI's 200K TPM window on Tier 1. Without
        # a pause, the consolidation + validator calls that immediately
        # follow reliably 429. A brief sleep lets the rolling window drain
        # enough to admit ~5K more tokens (consolidation + validator combined).
        if extraction_tier == "vision":
            await asyncio.sleep(5)

        existing_categories = await _fetch_existing_categories(bot_id)
        all_products = await _consolidate_categories(all_products, existing_categories)

        # Log-only sanity check: asks an LLM whether any obvious items are
        # missing compared to the raw text. Pure observability — does not
        # gate the save. Only runs for PDFs (we have raw text for those).
        if is_pdf:
            try:
                raw_pdf_text = _extract_text_from_pdf(contents)
                if raw_pdf_text:
                    await _validate_extraction(all_products, raw_pdf_text, bot_id)
            except Exception as e:
                logger.debug("Validator setup failed: %s", e)

        # Cache results for dedup
        await _set_cached_products(bot_id, file_hash, all_products)

        # Save to DB
        enriched = _enrich_products(all_products)
        async with async_session() as session:
            count = await bulk_create_products(
                session=session, bot_id=bot_id, products_data=enriched
            )

        logger.info(
            "Menu extraction complete: %d products for bot_id=%d (tier=%s)",
            count,
            bot_id,
            extraction_tier,
        )
        await _broadcast_progress(
            bot_id,
            "completed",
            f"Sucesso! {count} produtos cadastrados a partir do cardápio.",
            count=count,
        )

    except Exception as e:
        logger.error("Menu extraction failed for bot_id=%d: %s", bot_id, e)
        await _broadcast_progress(
            bot_id,
            "error",
            f"Erro ao processar cardápio: {str(e)[:200]}",
        )
        raise


# ---------------------------------------------------------------------------
# iFood URL extraction
# ---------------------------------------------------------------------------

_IFOOD_URL_PATTERN = re.compile(r"^https?://(www\.)?ifood\.com\.br/delivery/.+/.+")


def is_valid_ifood_url(url: str) -> bool:
    """Check if the URL looks like a valid iFood restaurant page."""
    return bool(_IFOOD_URL_PATTERN.match(url.strip()))


async def fetch_ifood_menu(url: str) -> tuple[str, list[dict]]:
    """Fetch an iFood restaurant page and extract menu products.

    Returns (restaurant_name, products) where products is a list of dicts
    with keys: name, price, description, category.

    Raises ValueError if the URL is invalid or the page can't be parsed.
    """
    url = url.strip()
    if not is_valid_ifood_url(url):
        raise ValueError("URL inválida. Envie um link de restaurante do iFood.")

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "pt-BR,pt;q=0.9",
                },
            )
            if resp.status_code != 200:
                raise ValueError(
                    f"Não foi possível acessar a página do iFood (HTTP {resp.status_code})."
                )
    except httpx.TimeoutException:
        raise ValueError("Tempo limite atingido ao acessar o iFood. Tente novamente.")
    except httpx.RequestError as exc:
        raise ValueError(f"Erro de conexão com o iFood: {exc}")

    html = resp.text

    # Extract __NEXT_DATA__ JSON blob embedded by Next.js
    m = re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    if not m:
        raise ValueError(
            "Não foi possível extrair dados do cardápio. "
            "Verifique se o link é de um restaurante válido no iFood."
        )

    try:
        next_data = json.loads(m.group(1))
    except json.JSONDecodeError:
        raise ValueError("Dados do iFood estão em formato inválido.")

    restaurant_name, products = _extract_menu_from_next_data(next_data)

    if not products:
        raise ValueError(
            "Nenhum produto encontrado na página do iFood. "
            "Verifique se o restaurante possui cardápio disponível."
        )

    logger.info(
        "iFood extraction: %d products from '%s'",
        len(products),
        restaurant_name[:40],
    )
    return restaurant_name, products


def _extract_menu_from_next_data(data: dict) -> tuple[str, list[dict]]:
    """Walk the __NEXT_DATA__ tree to find restaurant name and menu items.

    iFood's page structure varies, so we search recursively for anything
    that looks like a menu catalog (objects with name + price).
    """
    restaurant_name = ""
    products: list[dict] = []

    def _find_key(obj, key, depth=0):
        if depth > 15:
            return None
        if isinstance(obj, dict):
            if key in obj and isinstance(obj[key], str):
                return obj[key]
            for v in obj.values():
                r = _find_key(v, key, depth + 1)
                if r:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = _find_key(item, key, depth + 1)
                if r:
                    return r
        return None

    restaurant_name = _find_key(data, "name") or ""

    def _find_products(obj, depth=0):
        if depth > 15:
            return
        if isinstance(obj, dict):
            has_name = "name" in obj or "description" in obj
            has_price = "price" in obj or "unitPrice" in obj or "originalPrice" in obj
            if has_name and has_price:
                name = obj.get("name", obj.get("description", ""))
                price_raw = obj.get(
                    "price", obj.get("unitPrice", obj.get("originalPrice", 0))
                )
                # iFood prices are often in centavos (integer)
                if isinstance(price_raw, (int, float)) and price_raw > 1000:
                    price = price_raw / 100
                else:
                    price = price_raw
                if name and price and price > 0:
                    products.append(
                        {
                            "name": str(name).strip(),
                            "price": round(float(price), 2),
                            "description": str(
                                obj.get("description", obj.get("details", ""))
                            ).strip(),
                            "category": str(
                                obj.get("category", obj.get("section", "Geral"))
                            ).strip(),
                        }
                    )
            for v in obj.values():
                _find_products(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                _find_products(item, depth + 1)

    _find_products(data)

    # Deduplicate by name
    seen: set[str] = set()
    unique: list[dict] = []
    for p in products:
        key = p["name"].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(p)

    return restaurant_name, unique
