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

import redis.asyncio as redis

from app.broadcast import broadcast_order_update
from app.crud import bulk_create_products
from app.database import async_session
from app.openai_client import extract_products_from_image

logger = logging.getLogger(__name__)

# Redis DB 2 for file hash cache (same DB as monitoring counters)
_REDIS_HOST = os.getenv("REDIS_HOST", "redis")
_REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
_CACHE_TTL = 86400  # 24 hours

# Max image dimension before sending to vision API (reduces token count)
_MAX_IMAGE_DIM = 2048
_JPEG_QUALITY = 80


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

    return True, "ok"


def _extract_text_with_pdfplumber(contents: bytes) -> str:
    """Fallback PDF text extraction using pdfplumber (better at tables/columns)."""
    import pdfplumber

    text_parts = []
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        for i, page in enumerate(pdf.pages[:5]):
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
    import fitz

    doc = fitz.open(stream=contents, filetype="pdf")
    text_parts = []
    for i in range(min(len(doc), 5)):
        page_text = doc.load_page(i).get_text()
        if page_text.strip():
            text_parts.append(page_text.strip())
    doc.close()
    return "\n\n".join(text_parts)


def _pdf_to_images(contents: bytes) -> list[bytes]:
    """Convert PDF pages to JPEG images at 150 DPI, resized and compressed."""
    import fitz

    doc = fitz.open(stream=contents, filetype="pdf")
    images = []
    max_pages = 5
    for i in range(min(len(doc), max_pages)):
        page = doc.load_page(i)
        pix = page.get_pixmap(dpi=150)
        page_bytes = pix.tobytes("png")
        compressed, _ = _resize_and_compress(page_bytes)
        images.append(compressed)
    doc.close()
    return images


def _enrich_products(products: List[Dict]) -> List[Dict]:
    """Add keywords and is_available flag to extracted products."""
    enriched = []
    for product in products:
        name_words = product.get("name", "").lower()
        desc_words = product.get("description", "").lower()
        full_text = name_words + " " + desc_words
        words = set(re.findall(r"\b\w+\b", full_text))
        product["keywords"] = list(words)
        product["is_available"] = True
        enriched.append(product)
    return enriched


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
    """
    fitz_text = _extract_text_from_pdf(contents)
    is_good, reason = _is_text_quality_sufficient(fitz_text)
    if is_good:
        return fitz_text, "fitz"

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
                # Fast path: text-only gpt-4o-mini (~1-3s)
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

                all_products = await extract_products_from_text(pdf_text)
            else:
                # Slow path: vision API with optimized images
                logger.info("PDF text quality insufficient, using vision API")
                extraction_tier = "vision"
                await _broadcast_progress(
                    bot_id, "extracting", "Analisando imagens do cardápio..."
                )
                page_images = _pdf_to_images(contents)
                tasks = [
                    extract_products_from_image(img, "image/jpeg")
                    for img in page_images
                ]
                results = await asyncio.gather(*tasks)
                for page_products in results:
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
