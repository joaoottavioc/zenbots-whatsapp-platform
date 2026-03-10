# app/menu_extraction.py
"""Async menu extraction worker job.

Processes uploaded menu files (PDF/image) in the ARQ worker,
extracts products via AI, saves to DB, and broadcasts progress via SSE.
"""

import asyncio
import base64
import io
import logging
import re
from typing import List, Dict

from app.broadcast import broadcast_order_update
from app.crud import bulk_create_products
from app.database import async_session
from app.openai_client import extract_products_from_image

logger = logging.getLogger(__name__)

# Max image dimension before sending to vision API (reduces token count)
_MAX_IMAGE_DIM = 1024
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


async def process_menu_extraction(
    ctx, bot_id: int, file_b64: str, filename: str, content_type: str
):
    """ARQ worker job: extract products from uploaded menu file.

    Optimizations applied:
    - Text-first PDF extraction (skips vision API for text-selectable PDFs)
    - 150 DPI + JPEG compression (smaller payload, fewer vision tokens)
    - Image resize to max 1024px (reduces tile count for vision API)
    - gpt-4o-mini instead of gpt-4o (3-5x faster)
    """
    logger.info("Starting menu extraction job for bot_id=%d file=%s", bot_id, filename)
    await _broadcast_progress(bot_id, "started", "Processando cardápio...")

    contents = base64.b64decode(file_b64)
    is_pdf, is_image, detected_mime = _detect_file_type(
        contents, filename, content_type
    )

    all_products: List[Dict] = []

    try:
        if is_pdf:
            # Tier 1 optimization: try text extraction first (skips vision API)
            pdf_text = _extract_text_from_pdf(contents)

            if len(pdf_text) > 100:
                # PDF has selectable text — use fast text extraction
                logger.info(
                    "PDF has selectable text (%d chars), using text extraction",
                    len(pdf_text),
                )
                await _broadcast_progress(
                    bot_id, "extracting", "Extraindo produtos do texto..."
                )
                from app.data_extractor import extract_products_from_text

                all_products = await extract_products_from_text(pdf_text)
            else:
                # Scanned PDF — fall back to vision API with optimized images
                logger.info("PDF is scanned/image-based, using vision API")
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

        # Save to DB
        enriched = _enrich_products(all_products)
        async with async_session() as session:
            count = await bulk_create_products(
                session=session, bot_id=bot_id, products_data=enriched
            )

        logger.info(
            "Menu extraction complete: %d products for bot_id=%d", count, bot_id
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
