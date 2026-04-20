"""Scrape real restaurant menu images for the QA corpus.

Two modes:
  --mode=search   Search Google/DuckDuckGo for menu images, classify REAL vs
                  TEMPLATE via gpt-4o-mini vision, save real ones to
                  pending_images.json tagged as tier2.
  --mode=gmaps    Use Google Maps Places API to find restaurants, download
                  their photos, classify as menu vs non-menu, and create
                  prospect entries directly (with real name/category/city).

Usage:
  # Search mode — populate pending_images.json with real menu candidates
  python tests/simulation/corpus/scrape_real_menus.py --mode=search \\
      --categories=hamburgueria,pizzaria --cities="São Paulo,Curitiba"

  # Google Maps mode — create prospect entries from real restaurants
  python tests/simulation/corpus/scrape_real_menus.py --mode=gmaps \\
      --categories=hamburgueria --cities="São Paulo" --limit=5

  # Dry run — show what would be searched without downloading
  python tests/simulation/corpus/scrape_real_menus.py --mode=search --dry-run

Env vars:
  OPENAI_API_KEY          Required for classification (gpt-4o-mini vision)
  GOOGLE_MAPS_API_KEY     Required for --mode=gmaps
  GOOGLE_CSE_API_KEY      Optional, for Google Custom Search (search mode)
  GOOGLE_CSE_CX           Optional, Custom Search Engine ID
"""

import argparse
import asyncio
import base64
import json
import os
import random
import sys
import time
from pathlib import Path

import httpx

_project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

CORPUS = Path(__file__).parent
PENDING = CORPUS / "pending_images.json"

DEFAULT_CATEGORIES = [
    # Primary launch segments
    "hamburgueria",
    "pizzaria",
    "padaria",
    "cafeteria",
    "açaiteria",
    "marmitaria",
    "pastelaria",
    "doceria",
    "lanchonete",
    "espetaria",
    # Niche categories
    "sushi",
    "churrascaria",
    "comida japonesa",
    "comida italiana",
    "comida árabe",
    "comida mexicana",
    "tapiocaria",
    "hot dog",
    "creperia",
    "sorveteria",
]

DEFAULT_CITIES = [
    "São Paulo",
    "Rio de Janeiro",
    "Curitiba",
    "Belo Horizonte",
    "Porto Alegre",
]

# Query templates that favor real menus over design templates
SEARCH_TEMPLATES = [
    "cardápio do dia {category} {city}",
    "cardápio delivery {category} {city}",
    "cardápio {category} {city} whatsapp",
    "menu {category} {city} delivery",
    "{category} cardápio {city}",
]

# Instagram-targeted queries — finds IG posts indexed by search engines
INSTAGRAM_TEMPLATES = [
    "site:instagram.com cardápio {category} {city}",
    "site:instagram.com cardapio {category}",
    "site:instagram.com menu {category} {city} delivery",
    "site:instagram.com cardápio do dia {category}",
    "instagram.com {category} cardápio {city}",
]

# PDF/document menu queries — finds downloadable menu PDFs hosted on restaurant sites
PDF_TEMPLATES = [
    "cardápio {category} {city} filetype:pdf",
    "menu {category} {city} filetype:pdf",
    "cardápio {category} filetype:pdf",
    "{category} cardápio delivery filetype:pdf",
]

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
GOOGLE_CSE_API_KEY = os.getenv("GOOGLE_CSE_API_KEY", "")
GOOGLE_CSE_CX = os.getenv("GOOGLE_CSE_CX", "")


# ---------------------------------------------------------------------------
# Classification — gpt-4o-mini vision: REAL vs TEMPLATE
# ---------------------------------------------------------------------------

DEFAULT_MIN_PRODUCTS = 20  # Default scraper floor

CLASSIFY_PROMPT = (
    "Look at this image. Does it show a REAL restaurant menu that lists "
    "PRODUCT NAMES together with their PRICES (e.g. 'X-Burger R$25,00')? "
    "A valid menu must have multiple food/drink items each with a visible price. "
    "Reject if: no prices shown, only photos of food, design template/mockup, "
    "Canva template, stock photo, brochure, or fewer than 3 priced items. "
    "If VALID, count how many product+price pairs are visible. "
    "Answer ONLY: REAL:N (where N is the count of priced items) or REJECT"
)


async def classify_image(
    client: httpx.AsyncClient, image_bytes: bytes, mime: str
) -> tuple[str, int]:
    """Classify an image as REAL or TEMPLATE and estimate product count.

    Returns (label, estimated_count). Label is "REAL", "TEMPLATE", or "ERROR".
    estimated_count is 0 for TEMPLATE/ERROR.
    """
    import re as _re

    b64 = base64.b64encode(image_bytes).decode()
    resp = await client.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": CLASSIFY_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{b64}",
                                "detail": "low",
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 15,
            "temperature": 0,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        return "ERROR", 0
    text = resp.json()["choices"][0]["message"]["content"].strip().upper()

    # Parse "REAL:24" or "REAL: 24" or "REAL 24"
    m = _re.search(r"REAL\s*[:\s]\s*(\d+)", text)
    if m:
        return "REAL", int(m.group(1))
    if "REAL" in text:
        return "REAL", 0  # couldn't parse count but it's real
    return "REJECT", 0


MENU_CLASSIFY_PROMPT = (
    "Is this photo of a restaurant MENU (printed menu, menu board, "
    "cardápio, price list with food items) or something else (food photo, "
    "storefront, interior, people, logo)? "
    "Answer with ONLY one word: MENU or OTHER"
)


async def classify_is_menu(
    client: httpx.AsyncClient, image_bytes: bytes, mime: str
) -> bool:
    """Classify whether an image is a menu photo (vs food, storefront, etc.)."""
    b64 = base64.b64encode(image_bytes).decode()
    resp = await client.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": MENU_CLASSIFY_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{b64}",
                                "detail": "low",
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 10,
            "temperature": 0,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        return False
    text = resp.json()["choices"][0]["message"]["content"].strip().upper()
    return "MENU" in text


# ---------------------------------------------------------------------------
# Image search backends
# ---------------------------------------------------------------------------

# Shared Playwright browser instance — reused across searches so you only
# solve the CAPTCHA once. Launched visible (headless=False) so you can
# interact with it. Set via _ensure_google_browser().
_google_browser = None
_google_context = None
_google_page = None


GOOGLE_STATE_FILE = CORPUS / ".google_session.json"


async def _ensure_google_browser():
    """Launch (or reuse) a visible Playwright browser for Google searches.

    Persists cookies/localStorage so CAPTCHA solutions carry across runs.
    """
    global _google_browser, _google_context, _google_page

    if _google_page is not None:
        try:
            await _google_page.evaluate("1")
            return _google_page
        except Exception:
            _google_page = None

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("    playwright not installed, skipping Google web search")
        return None

    pw = await async_playwright().start()
    _google_browser = await pw.chromium.launch(
        headless=False,
        channel="chrome",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    )

    # Restore saved cookies/session if available
    storage_state = str(GOOGLE_STATE_FILE) if GOOGLE_STATE_FILE.exists() else None

    _google_context = await _google_browser.new_context(
        locale="pt-BR",
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        storage_state=storage_state,
    )
    _google_page = await _google_context.new_page()
    await _google_page.add_init_script(
        'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
    )

    await _google_page.goto(
        "https://www.google.com", wait_until="domcontentloaded", timeout=15000
    )
    await _google_page.wait_for_timeout(random.randint(2000, 4000))
    return _google_page


async def _save_google_session():
    """Save cookies/localStorage so CAPTCHA wins persist across runs."""
    if _google_context:
        try:
            await _google_context.storage_state(path=str(GOOGLE_STATE_FILE))
        except Exception as e:
            print(f"    Could not save Google session: {e}")


async def _google_search_with_captcha_wait(page, query: str) -> bool:
    """Navigate to Google search with human-like pacing.

    Waits randomly between searches, scrolls the page, and on CAPTCHA
    pauses for user interaction.
    """
    from urllib.parse import quote_plus

    # Human-like delay: 8-15 seconds between searches
    await page.wait_for_timeout(random.randint(8000, 15000))

    url = f"https://www.google.com/search?q={quote_plus(query)}&hl=pt-BR&gl=BR&num=10"
    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
    # Extra settle time
    await page.wait_for_timeout(random.randint(1500, 3000))

    # Light scroll to look less automated
    try:
        await page.evaluate("window.scrollBy(0, Math.random() * 200 + 100)")
        await page.wait_for_timeout(random.randint(800, 1500))
    except Exception:
        pass

    # Check for CAPTCHA
    if "sorry" in page.url or "/sorry/" in page.url:
        print("\n    >>> CAPTCHA detected -- solve it in the browser window <<<")
        print("    (After solving, wait 30s — cooldown — then press Enter)")
        await asyncio.to_thread(input, "    Press Enter after solving the CAPTCHA...")
        # Cooldown after CAPTCHA — gives Google time to trust us again
        await page.wait_for_timeout(random.randint(25000, 35000))
        # Save session so future runs skip this CAPTCHA
        await _save_google_session()
        # Re-navigate
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(random.randint(2000, 4000))
        if "sorry" in page.url:
            print("    Still blocked -- skipping this query, longer cooldown...")
            await page.wait_for_timeout(random.randint(45000, 60000))
            return False

    return True


async def search_google_web(query: str, max_results: int = 10) -> list[dict]:
    """Google web search via visible Playwright browser. Returns [{src, alt}].

    On CAPTCHA, pauses and asks the user to solve it manually.
    Reuses a single browser instance across all searches.
    """
    page = await _ensure_google_browser()
    if not page:
        return []

    results = []
    try:
        if not await _google_search_with_captcha_wait(page, query):
            return []

        # Extract result links (skip Google's own links)
        links = await page.eval_on_selector_all(
            "a[href]",
            """els => els.map(el => ({
                href: el.href,
                text: el.innerText || ''
            })).filter(l =>
                l.href.startsWith('http') &&
                !l.href.includes('google.com') &&
                !l.href.includes('youtube.com') &&
                !l.href.includes('accounts.google')
            )""",
        )
        for link in links:
            href = link.get("href", "")
            title = link.get("text", "").strip()
            if href and href.startswith("http"):
                results.append({"src": href, "alt": title})
                if len(results) >= max_results:
                    break

    except Exception as e:
        print(f"    Google web search error: {e}")

    return results


async def search_duckduckgo_web(query: str, max_results: int = 10) -> list[dict]:
    """Search DuckDuckGo Web for PDFs/documents. Returns [{src, alt}]."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            return []

        import re

        results = []
        # Parse result links from DDG HTML search
        for match in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            r.text,
            re.DOTALL,
        ):
            url = match.group(1)
            title = re.sub(r"<[^>]+>", "", match.group(2)).strip()

            # DDG wraps URLs in a redirect — extract the actual URL
            actual_match = re.search(r"uddg=([^&]+)", url)
            if actual_match:
                from urllib.parse import unquote

                url = unquote(actual_match.group(1))

            # Only keep PDF links
            if ".pdf" in url.lower():
                results.append({"src": url, "alt": title})
                if len(results) >= max_results:
                    break

        return results


async def search_duckduckgo(query: str, max_results: int = 20) -> list[dict]:
    """Search DuckDuckGo Images. Returns [{src, alt}]."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        # DuckDuckGo image search via the vqd token flow
        r = await client.get(
            "https://duckduckgo.com/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        # Extract vqd token
        import re

        vqd_match = re.search(r"vqd=['\"]([^'\"]+)['\"]", r.text)
        if not vqd_match:
            # Fallback: try the i.js endpoint directly
            vqd_match = re.search(r"vqd=([a-zA-Z0-9_-]+)", r.text)
        if not vqd_match:
            print(f"    DDG: no vqd token for '{query[:40]}'")
            return []

        vqd = vqd_match.group(1)
        img_resp = await client.get(
            "https://duckduckgo.com/i.js",
            params={
                "l": "br-pt",
                "o": "json",
                "q": query,
                "vqd": vqd,
                "f": ",,,,,",
                "p": "1",
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://duckduckgo.com/"},
        )
        if img_resp.status_code != 200:
            return []

        results = img_resp.json().get("results", [])
        return [
            {"src": r["image"], "alt": r.get("title", "")}
            for r in results[:max_results]
            if r.get("image")
        ]


async def search_google_cse(query: str, max_results: int = 10) -> list[dict]:
    """Search Google Custom Search Engine for images. Returns [{src, alt}]."""
    if not GOOGLE_CSE_API_KEY or not GOOGLE_CSE_CX:
        return []

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": GOOGLE_CSE_API_KEY,
                "cx": GOOGLE_CSE_CX,
                "q": query,
                "searchType": "image",
                "num": min(max_results, 10),
                "imgType": "photo",
                "lr": "lang_pt",
                "cr": "countryBR",
            },
        )
        if resp.status_code != 200:
            print(f"    Google CSE error: {resp.status_code}")
            return []

        items = resp.json().get("items", [])
        return [
            {"src": item["link"], "alt": item.get("title", "")}
            for item in items
            if item.get("link")
        ]


# ---------------------------------------------------------------------------
# Google Maps mode
# ---------------------------------------------------------------------------


async def gmaps_find_restaurants(
    category: str, city: str, limit: int = 5
) -> list[dict]:
    """Find restaurants via Google Maps Text Search. Returns place details."""
    if not GOOGLE_MAPS_API_KEY:
        print("  ERROR: GOOGLE_MAPS_API_KEY not set")
        return []

    query = f"{category} em {city}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={
                "query": query,
                "key": GOOGLE_MAPS_API_KEY,
                "language": "pt-BR",
                "type": "restaurant",
            },
        )
        if resp.status_code != 200:
            print(f"  Google Maps error: {resp.status_code}")
            return []

        results = resp.json().get("results", [])
        places = []
        for r in results[:limit]:
            places.append(
                {
                    "place_id": r["place_id"],
                    "name": r.get("name", ""),
                    "address": r.get("formatted_address", ""),
                    "photos": r.get("photos", []),
                    "rating": r.get("rating"),
                    "types": r.get("types", []),
                }
            )
        return places


async def gmaps_download_photo(photo_ref: str, max_width: int = 1600) -> bytes | None:
    """Download a Google Maps place photo by reference."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(
            "https://maps.googleapis.com/maps/api/place/photo",
            params={
                "maxwidth": max_width,
                "photoreference": photo_ref,
                "key": GOOGLE_MAPS_API_KEY,
            },
        )
        if resp.status_code == 200 and len(resp.content) > 2000:
            return resp.content
    return None


# ---------------------------------------------------------------------------
# Main modes
# ---------------------------------------------------------------------------


async def run_search_mode(
    categories: list[str],
    cities: list[str],
    dry_run: bool = False,
    max_per_query: int = 15,
    engine: str = "auto",
    instagram: bool = False,
    pdfs: bool = False,
    min_products: int = DEFAULT_MIN_PRODUCTS,
):
    """Search for real menu images (and optionally PDFs) and add to pending_images.json."""
    # Build queries
    templates = list(SEARCH_TEMPLATES)
    if instagram:
        templates.extend(INSTAGRAM_TEMPLATES)
    if pdfs:
        templates.extend(PDF_TEMPLATES)

    queries = []
    for cat in categories:
        for city in cities:
            for tmpl in templates:
                queries.append((tmpl.format(category=cat, city=city), cat, city))

    print(
        f"Generated {len(queries)} search queries across {len(categories)} categories × {len(cities)} cities"
    )
    print()

    if dry_run:
        for q, cat, city in queries[:20]:
            print(f"  [{cat}/{city}] {q}")
        if len(queries) > 20:
            print(f"  ... and {len(queries) - 20} more")
        return

    # Load existing pending images to avoid duplicates
    existing_urls = set()
    pending = []
    if PENDING.exists():
        pending = json.loads(PENDING.read_text(encoding="utf-8"))
        existing_urls = {e["src"] for e in pending}
    print(f"Existing pending images: {len(existing_urls)}")

    all_candidates: list[dict] = []
    # DDG for images (works), Google Playwright for web search (PDFs/iFood)
    search_fn = search_duckduckgo
    print("Image search: DuckDuckGo | Web search (PDFs/iFood): Google (Playwright)")
    print()

    for i, (query, cat, city) in enumerate(queries):
        print(f"  [{i + 1}/{len(queries)}] {query[:60]}...", end=" ", flush=True)
        try:
            # Use DDG web search for filetype:pdf queries (works without CAPTCHAs),
            # image search for everything else
            is_pdf_query = "filetype:pdf" in query
            fn = search_duckduckgo_web if is_pdf_query else search_fn
            results = await fn(query, max_results=max_per_query)
            new = [r for r in results if r["src"] not in existing_urls]
            for r in new:
                r["_category"] = cat
                r["_city"] = city
                if is_pdf_query:
                    r["_is_pdf_query"] = True
                existing_urls.add(r["src"])
            all_candidates.extend(new)
            print(f"{len(new)} new" + (" (web/pdf)" if is_pdf_query and new else ""))
        except Exception as e:
            print(f"ERROR: {e}")

        # Rate limit
        await asyncio.sleep(0.5)

    print()
    print(f"Total unique candidates: {len(all_candidates)}")

    if not all_candidates:
        print("No candidates found.")
        return

    # Classify each candidate
    print()
    print(f"Classifying candidates (REAL >={min_products} items vs TEMPLATE)...")
    real_count = 0
    too_small_count = 0
    pdf_count = 0
    template_count = 0
    error_count = 0

    pdf_dir = CORPUS / "pdfs"
    if pdfs:
        pdf_dir.mkdir(exist_ok=True)

    async with httpx.AsyncClient(timeout=30) as client:
        for i, candidate in enumerate(all_candidates):
            alt_safe = candidate["alt"][:50].encode("ascii", "replace").decode()
            print(
                f"  [{i + 1}/{len(all_candidates)}] {alt_safe}...", end=" ", flush=True
            )

            # Download file
            try:
                dl_resp = await client.get(
                    candidate["src"],
                    headers={"User-Agent": "Mozilla/5.0"},
                    follow_redirects=True,
                    timeout=20,
                )
                if dl_resp.status_code != 200 or len(dl_resp.content) < 3000:
                    print("SKIP (download failed)")
                    error_count += 1
                    continue
            except Exception:
                print("SKIP (download error)")
                error_count += 1
                continue

            content = dl_resp.content
            ct = dl_resp.headers.get("content-type", "")

            # Detect if this is a PDF (by content-type or magic bytes)
            is_pdf = "pdf" in ct.lower() or content[:5] == b"%PDF-"

            if is_pdf:
                # Extract first page as image for classification
                try:
                    import fitz  # PyMuPDF

                    doc = fitz.open(stream=content, filetype="pdf")
                    page = doc[0]
                    pix = page.get_pixmap(dpi=150)
                    page_bytes = pix.tobytes("jpeg")
                    doc.close()

                    label, est_count = await classify_image(
                        client, page_bytes, "image/jpeg"
                    )
                except Exception as pdf_err:
                    print(f"SKIP (PDF parse error: {type(pdf_err).__name__})")
                    error_count += 1
                    continue

                if label != "REAL" or est_count < min_products:
                    if label == "REAL":
                        print(f"PDF REAL:{est_count} (< {min_products}, skipped)")
                        too_small_count += 1
                    else:
                        print(f"PDF REJECT ({len(content) // 1024}KB)")
                        template_count += 1
                    continue

                # Sanitize filename — strip Windows-forbidden chars and non-ASCII
                import re as _re_fn

                raw_slug = candidate.get("alt", "menu")[:40]
                slug = _re_fn.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw_slug)
                slug = _re_fn.sub(
                    r"[^\w\-_.]", "_", slug.encode("ascii", "ignore").decode()
                )
                slug = _re_fn.sub(r"_+", "_", slug).strip("_") or "menu"
                pdf_name = f"pdf_{int(time.time())}_{i}_{slug}.pdf"
                pdf_path = pdf_dir / pdf_name
                pdf_path.write_bytes(content)
                pdf_count += 1

                pending.append(
                    {
                        "src": candidate["src"],
                        "alt": candidate.get("alt", ""),
                        "menu_type": "tier2",
                        "file_type": "pdf",
                        "_local_file": f"pdfs/{pdf_name}",
                        "_category": candidate.get("_category", ""),
                        "_city": candidate.get("_city", ""),
                        "_est_products": est_count,
                        "_source": "scrape_real_menus_pdf",
                    }
                )
                print(f"PDF REAL:{est_count} ({len(content) // 1024}KB)")
                continue

            # Image — classify REAL vs TEMPLATE + estimate product count
            mime = (
                "image/png"
                if "png" in ct
                else "image/webp"
                if "webp" in ct
                else "image/jpeg"
            )
            try:
                label, est_count = await classify_image(client, content, mime)
            except Exception as cls_err:
                print(f"SKIP (classify error: {type(cls_err).__name__})")
                error_count += 1
                await asyncio.sleep(1)
                continue

            if label == "REAL" and est_count >= min_products:
                print(f"REAL:{est_count}")
                real_count += 1
                pending.append(
                    {
                        "src": candidate["src"],
                        "alt": candidate.get("alt", ""),
                        "menu_type": "tier2",
                        "_category": candidate.get("_category", ""),
                        "_city": candidate.get("_city", ""),
                        "_est_products": est_count,
                        "_source": "scrape_real_menus",
                    }
                )
            elif label == "REAL":
                print(f"REAL:{est_count} (< {min_products}, skipped)")
                too_small_count += 1
            elif label == "REJECT":
                print("REJECT")
                template_count += 1
            else:
                print(f"ERROR ({label})")
                error_count += 1

            # Periodic save every 25 classified images so crashes don't lose progress
            classified = real_count + too_small_count + template_count + error_count
            if classified > 0 and classified % 25 == 0:
                PENDING.write_text(
                    json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(f"    [checkpoint: {len(pending)} entries saved]")

            # Rate limit OpenAI
            await asyncio.sleep(0.3)

    # Save
    PENDING.write_text(
        json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    added = real_count + pdf_count
    print()
    print(
        f"Results: {real_count} REAL images + {pdf_count} REAL PDFs (>={min_products} priced items), {too_small_count} too small, {template_count} rejected, {error_count} errors"
    )
    print(f"Pending total: {len(pending)} (added {added} new tier2 entries)")
    if pdf_count:
        print(f"PDFs saved to: {pdf_dir}/")
    print()
    print("Next steps:")
    print("  1. Run the classifier game to accept/reject the new tier2 entries")
    print("  2. Run validate_corpus.py to extract products")
    print("  3. Promote validated entries to the baseline")


async def run_gmaps_mode(
    categories: list[str],
    cities: list[str],
    limit: int = 5,
    dry_run: bool = False,
):
    """Find restaurants on Google Maps, download menu photos, create prospects."""
    print(
        f"Google Maps mode: {len(categories)} categories × {len(cities)} cities, limit={limit}/combo"
    )
    print()

    if dry_run:
        for cat in categories:
            for city in cities:
                print(f"  Would search: '{cat} em {city}' (limit {limit})")
        return

    if not GOOGLE_MAPS_API_KEY:
        print("ERROR: GOOGLE_MAPS_API_KEY env var required for --mode=gmaps")
        return

    # Load existing prospect pool to avoid duplicates
    prospect_pool_path = CORPUS / "prospect_pool.json"
    prospect_pool = {"version": 1, "entries": []}
    if prospect_pool_path.exists():
        prospect_pool = json.loads(prospect_pool_path.read_text(encoding="utf-8"))
    existing_names = {e["restaurant_name"].lower() for e in prospect_pool["entries"]}

    images_dir = CORPUS / "images"
    images_dir.mkdir(exist_ok=True)

    total_found = 0
    total_menus = 0
    total_created = 0

    async with httpx.AsyncClient(timeout=30) as classify_client:
        for cat in categories:
            for city in cities:
                print(f"  Searching: {cat} em {city}...")
                places = await gmaps_find_restaurants(cat, city, limit=limit)
                print(f"    Found {len(places)} restaurants")

                for place in places:
                    name = place["name"]
                    total_found += 1

                    if name.lower() in existing_names:
                        print(f"    SKIP {name} (already in prospects)")
                        continue

                    if not place["photos"]:
                        print(f"    SKIP {name} (no photos)")
                        continue

                    print(
                        f"    {name} — {len(place['photos'])} photos...",
                        end=" ",
                        flush=True,
                    )

                    # Try each photo until we find a menu
                    menu_bytes = None
                    for photo in place["photos"][:5]:  # Check up to 5 photos
                        photo_ref = photo.get("photo_reference")
                        if not photo_ref:
                            continue

                        img_bytes = await gmaps_download_photo(photo_ref)
                        if not img_bytes:
                            continue

                        is_menu = await classify_is_menu(
                            classify_client, img_bytes, "image/jpeg"
                        )
                        if is_menu:
                            menu_bytes = img_bytes
                            break

                        await asyncio.sleep(0.2)

                    if not menu_bytes:
                        print("no menu photo found")
                        continue

                    total_menus += 1

                    # Generate prospect ID
                    n = len(prospect_pool["entries"]) + 1
                    while any(
                        e["id"] == f"prospect_{n:04d}" for e in prospect_pool["entries"]
                    ):
                        n += 1
                    prospect_id = f"prospect_{n:04d}"

                    # Save image
                    filename = f"{prospect_id}.jpg"
                    filepath = images_dir / filename
                    filepath.write_bytes(menu_bytes)

                    # Create prospect entry
                    prospect_entry = {
                        "id": prospect_id,
                        "restaurant_name": name[:60],
                        "category": cat,
                        "city": city[:60],
                        "image_file": f"images/{filename}",
                        "image_files": [f"images/{filename}"],
                        "extraction_file": f"extractions/{prospect_id}.json",
                        "product_count": 0,  # not yet extracted
                        "demo_bot_id": None,
                        "status": "pending_extraction",
                        "added_date": __import__("datetime").date.today().isoformat(),
                        "_source": "google_maps",
                        "_place_id": place["place_id"],
                        "_address": place.get("address", ""),
                    }
                    prospect_pool["entries"].append(prospect_entry)
                    existing_names.add(name.lower())
                    total_created += 1
                    print(f"SAVED as {prospect_id}")

                    await asyncio.sleep(0.3)

    # Save prospect pool
    prospect_pool_path.write_text(
        json.dumps(prospect_pool, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print(
        f"Results: {total_found} restaurants found, {total_menus} had menu photos, {total_created} prospects created"
    )
    print()
    if total_created > 0:
        print("Next steps:")
        print("  1. Start the backend: docker compose up")
        print("  2. For each prospect, extract products via:")
        print(
            "     POST /monitoring/admin/prospect/{id}/add  (or use the Prospects tab)"
        )
        print("  3. Review extracted products")
        print("  4. Promote to corpus + baseline")


# ---------------------------------------------------------------------------
# iFood structured scrape mode
# ---------------------------------------------------------------------------

# Map our categories to iFood search terms
IFOOD_CATEGORY_MAP = {
    "hamburgueria": "hamburgueria",
    "pizzaria": "pizzaria",
    "padaria": "padaria",
    "cafeteria": "cafeteria",
    "açaiteria": "açaí",
    "marmitaria": "marmita",
    "pastelaria": "pastelaria",
    "doceria": "doceria",
    "lanchonete": "lanchonete",
    "espetaria": "espetinho",
    "sushi": "sushi",
    "churrascaria": "churrascaria",
    "comida japonesa": "japonesa",
    "comida italiana": "italiana",
    "comida árabe": "árabe",
    "comida mexicana": "mexicana",
    "tapiocaria": "tapioca",
    "hot dog": "hot dog",
    "creperia": "crepe",
    "sorveteria": "sorveteria",
}


_ifood_address_set = False

# Default address for iFood — central São Paulo, delivers everywhere
IFOOD_DEFAULT_ADDRESS = "Avenida Paulista, 1000"


async def _ensure_ifood_address(page) -> bool:
    """Set a delivery address on iFood programmatically if needed."""
    global _ifood_address_set

    if _ifood_address_set:
        return True

    needs_address = await page.evaluate("""() => {
        const text = document.body.innerText.toLowerCase();
        return text.includes("informe seu endere") || text.includes("informar endere");
    }""")

    if not needs_address:
        _ifood_address_set = True
        return True

    # Navigate to iFood homepage where the address input is
    await page.goto("https://www.ifood.com.br", wait_until="networkidle", timeout=20000)
    await page.wait_for_timeout(2000)

    print("")
    print("    " + "=" * 60)
    print("    >>> CHROME WINDOW IS OPEN — set the address there <<<")
    print(f"    1. Type: {IFOOD_DEFAULT_ADDRESS}")
    print("    2. Click a suggestion from the dropdown")
    print("    3. Wait — I'll detect when address is set automatically")
    print("    " + "=" * 60)
    print("")

    # Poll for address to be set — detect by checking for "Informe seu endereço"
    # going away, or the URL changing to a delivery page
    max_wait_seconds = 180  # 3 minutes
    poll_interval = 2
    elapsed = 0
    while elapsed < max_wait_seconds:
        await page.wait_for_timeout(poll_interval * 1000)
        elapsed += poll_interval

        try:
            still_needs = await page.evaluate("""() => {
                const text = document.body.innerText.toLowerCase();
                return text.includes("informe seu endere");
            }""")
            current_url = page.url
            is_on_delivery = (
                "/delivery/" in current_url or "/restaurantes/" in current_url
            )

            if not still_needs or is_on_delivery:
                print(f"    Address detected as set (after {elapsed}s). Continuing...")
                _ifood_address_set = True
                return True
        except Exception:
            pass

        if elapsed % 30 == 0:
            print(
                f"    ... still waiting for address ({elapsed}s elapsed, max {max_wait_seconds}s)"
            )

    print(
        "    Timeout waiting for address. Continuing anyway — may skip closed/no-address pages."
    )
    _ifood_address_set = True
    return False


async def _fetch_ifood_menu_playwright(url: str) -> tuple[str, list[dict]]:
    """Visit an iFood restaurant page with Playwright, wait for menu to render,
    and extract products from the DOM.

    Returns (restaurant_name, products_list).
    """
    page = await _ensure_google_browser()
    if not page:
        return "", []

    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        # Set address if needed (only prompts once)
        await _ensure_ifood_address(page)

        # Reload after address set to get the menu
        if _ifood_address_set:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)

        # Check if restaurant is closed (no menu rendered)
        is_closed = await page.evaluate("""() => {
            const text = document.body.innerText.toLowerCase();
            return text.includes("loja fechada") || text.includes("restaurante fechado");
        }""")
        if is_closed:
            return "", []

        # Extract restaurant name
        name = await page.evaluate("""() => {
            const el = document.querySelector('h1, [class*="merchant-name"], [class*="restaurant-name"]');
            return el ? el.innerText.trim() : "";
        }""")

        # Extract products: find all elements that have a name + price pattern
        # iFood renders menu items with product name and price in nearby elements
        products = await page.evaluate(r"""() => {
            const results = [];
            const seen = new Set();

            // Strategy 1: Find price elements (R$ pattern) and grab nearby name
            const allElements = document.querySelectorAll('*');
            for (const el of allElements) {
                const text = el.innerText || '';
                // Look for price pattern: R$ XX,XX
                const priceMatch = text.match(/R\$\s*([\d]+[,.][\d]{2})/);
                if (!priceMatch) continue;

                // Skip if this element has too many children (it's a container, not a leaf)
                if (el.children.length > 5) continue;

                const price = parseFloat(priceMatch[1].replace(',', '.'));
                if (price <= 0 || price > 999) continue;

                // Look for the product name: walk up to parent, find a heading or strong text
                let nameEl = el.closest('[class*="item"], [class*="product"], [class*="dish"], li, article');
                if (!nameEl) nameEl = el.parentElement;
                if (!nameEl) continue;

                // Find name: first h3, h4, strong, or span with short text
                const candidates = nameEl.querySelectorAll('h3, h4, strong, span, div');
                let productName = "";
                for (const c of candidates) {
                    const t = c.innerText.trim();
                    if (t.length >= 3 && t.length <= 80 && !t.includes('R$') && !t.match(/^\d/)) {
                        productName = t;
                        break;
                    }
                }

                if (!productName || seen.has(productName.toLowerCase())) continue;
                seen.add(productName.toLowerCase());

                // Try to find description
                let desc = "";
                const descEl = nameEl.querySelector('[class*="desc"], [class*="detail"], p');
                if (descEl) {
                    const d = descEl.innerText.trim();
                    if (d.length > 10 && d.length < 200 && d !== productName) desc = d;
                }

                results.push({
                    name: productName,
                    price: price,
                    description: desc,
                    category: "Geral"
                });
            }

            return results;
        }""")

        return name or "", products or []

    except Exception as e:
        print(f"    iFood page error: {e}")
        return "", []


async def run_ifood_mode(
    categories: list[str],
    cities: list[str],
    limit: int = 5,
    min_products: int = DEFAULT_MIN_PRODUCTS,
    dry_run: bool = False,
):
    """Scrape structured menu data from iFood restaurant pages."""
    print(
        f"iFood mode: {len(categories)} categories × {len(cities)} cities, limit={limit}/combo"
    )
    print(f"Min products: {min_products}")
    print()

    if dry_run:
        for cat in categories:
            search_term = IFOOD_CATEGORY_MAP.get(cat, cat)
            for city in cities:
                print(f"  Would search DDG: 'site:ifood.com.br {search_term} {city}'")
        return

    if not OPENAI_API_KEY:
        print(
            "ERROR: OPENAI_API_KEY needed (not for extraction, but for fallback classification)"
        )

    # Load prospect pool
    prospect_pool_path = CORPUS / "prospect_pool.json"
    prospect_pool = {"version": 1, "entries": []}
    if prospect_pool_path.exists():
        prospect_pool = json.loads(prospect_pool_path.read_text(encoding="utf-8"))
    existing_names = {e["restaurant_name"].lower() for e in prospect_pool["entries"]}

    extractions_dir = CORPUS / "extractions"
    extractions_dir.mkdir(exist_ok=True)

    total_pages = 0
    total_extracted = 0
    total_saved = 0

    for cat in categories:
        search_term = IFOOD_CATEGORY_MAP.get(cat, cat)
        for city in cities:
            query = f"site:ifood.com.br {search_term} {city}"
            print(f"\n  Searching: {query}")

            # Find iFood URLs via Google Playwright
            try:
                results = await search_google_web(query, max_results=limit * 3)
                ifood_urls = []
                for r in results:
                    url = r["src"]
                    if "ifood.com.br" in url and "/delivery/" in url:
                        ifood_urls.append((url, r.get("alt", "")))
                ifood_urls = ifood_urls[:limit]
            except Exception as e:
                print(f"    Search error: {e}")
                continue

            if not ifood_urls:
                try:
                    results = await search_google_web(
                        f"ifood.com.br {search_term} {city} cardapio",
                        max_results=limit * 2,
                    )
                    ifood_urls = [
                        (r["src"], r.get("alt", ""))
                        for r in results
                        if "ifood.com.br" in r["src"]
                    ][:limit]
                except Exception:
                    pass

            print(f"    Found {len(ifood_urls)} iFood pages")

            for url, alt in ifood_urls:
                total_pages += 1
                short_url = url.split("?")[0][-60:]
                print(f"    {short_url}...", end=" ", flush=True)

                # Visit page with Playwright and extract from rendered DOM
                name, products = await _fetch_ifood_menu_playwright(url)
                if not products:
                    print("no menu (closed or empty)")
                    continue

                total_extracted += 1

                if not name:
                    name = alt.split("-")[0].strip() if alt else f"iFood {cat}"

                if len(products) < min_products:
                    print(
                        f"{name[:30]} -- {len(products)} products (< {min_products}, skip)"
                    )
                    continue

                if name.lower() in existing_names:
                    print(f"{name[:30]} -- already in prospects")
                    continue

                # Generate prospect ID
                n = len(prospect_pool["entries"]) + 1
                while any(
                    e["id"] == f"prospect_{n:04d}" for e in prospect_pool["entries"]
                ):
                    n += 1
                prospect_id = f"prospect_{n:04d}"

                # Save extraction JSON directly (no image needed)
                extraction_path = extractions_dir / f"{prospect_id}.json"
                extraction_path.write_text(
                    json.dumps(
                        {"products": products, "category": cat},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

                # Create prospect entry
                prospect_pool["entries"].append(
                    {
                        "id": prospect_id,
                        "restaurant_name": name[:60],
                        "category": cat,
                        "city": city[:60],
                        "image_file": "",
                        "image_files": [],
                        "extraction_file": f"extractions/{prospect_id}.json",
                        "product_count": len(products),
                        "demo_bot_id": None,
                        "status": "extracted",
                        "added_date": __import__("datetime").date.today().isoformat(),
                        "_source": "ifood",
                        "_ifood_url": url,
                    }
                )
                existing_names.add(name.lower())
                total_saved += 1
                print(f"{name[:30]} -- {len(products)} products SAVED as {prospect_id}")

                await asyncio.sleep(1)  # Rate limit iFood

    # Save
    prospect_pool_path.write_text(
        json.dumps(prospect_pool, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print(
        f"Results: {total_pages} pages fetched, {total_extracted} had menu data, {total_saved} prospects created"
    )
    print()
    if total_saved > 0:
        print("Next steps:")
        print("  These are already extracted (structured data, no vision AI needed).")
        print("  1. Review products in the Prospects tab")
        print("  2. Promote to corpus + baseline")


# ---------------------------------------------------------------------------
# Full mode — all sources, per-category constraints
# ---------------------------------------------------------------------------

# Per-category scrape plan: min_products threshold and target count.
# Primary launch segments get lower thresholds (smaller real menus are OK)
# and higher targets (need more samples for baseline + random pool).
CATEGORY_PLAN = {
    # Primary launch — low threshold, high target
    "padaria": {"min": 8, "target": 5},
    "cafeteria": {"min": 10, "target": 5},
    "açaiteria": {"min": 8, "target": 3},
    "marmitaria": {"min": 8, "target": 3},
    "pastelaria": {"min": 8, "target": 3},
    "doceria": {"min": 8, "target": 3},
    "lanchonete": {"min": 10, "target": 5},
    "hamburgueria": {"min": 12, "target": 5},
    # Secondary — medium threshold
    "pizzaria": {"min": 15, "target": 3},
    "espetaria": {"min": 8, "target": 3},
    # Niche — representation
    "sushi": {"min": 10, "target": 2},
    "churrascaria": {"min": 10, "target": 2},
    "comida japonesa": {"min": 10, "target": 2},
    "comida italiana": {"min": 10, "target": 2},
    "comida árabe": {"min": 8, "target": 2},
    "comida mexicana": {"min": 8, "target": 2},
    "tapiocaria": {"min": 8, "target": 2},
    "hot dog": {"min": 8, "target": 2},
    "creperia": {"min": 8, "target": 2},
    "sorveteria": {"min": 8, "target": 2},
}


def _count_corpus_by_cat() -> dict[str, dict[str, int]]:
    """Count existing scraped entries per category per source. Re-read from disk each time."""
    counts: dict[str, dict[str, int]] = {}  # {cat: {source: count}}

    if PENDING.exists():
        pending = json.loads(PENDING.read_text(encoding="utf-8"))
        for e in pending:
            src = e.get("_source", "")
            if not src.startswith("scrape"):
                continue
            cat = e.get("_category", "")
            if cat not in counts:
                counts[cat] = {}
            bucket = "pdfs" if "pdf" in src else "images"
            counts[cat][bucket] = counts[cat].get(bucket, 0) + 1

    prospect_pool_path = CORPUS / "prospect_pool.json"
    if prospect_pool_path.exists():
        pp = json.loads(prospect_pool_path.read_text(encoding="utf-8"))
        for e in pp.get("entries", []):
            src = e.get("_source", "")
            cat = e.get("category", "")
            if src == "ifood":
                if cat not in counts:
                    counts[cat] = {}
                counts[cat]["ifood"] = counts[cat].get("ifood", 0) + 1

    return counts


def _cat_total(counts: dict[str, dict[str, int]], cat: str) -> int:
    return sum(counts.get(cat, {}).values())


async def run_full_mode(
    categories: list[str],
    cities: list[str],
    dry_run: bool = False,
):
    """Run all sources (search + iFood + PDFs) per category with tailored thresholds.

    For each category:
      1. iFood structured scrape (fastest, no AI extraction needed)
      2. Image search + Instagram (with gpt-4o-mini classification)
      3. PDF web search (with page-1 classification)
    Re-counts after each source so the next source sees accurate state.
    """
    print("=" * 60)
    print("FULL SCRAPE — all sources, per-category constraints")
    print("=" * 60)
    print(f"Categories: {len(categories)}")
    print(f"Cities: {cities}")
    print()

    counts = _count_corpus_by_cat()

    if dry_run:
        print(
            f"{'Category':<20} {'Have':>5} {'Target':>7} {'Need':>5} {'Min':>4}  {'Breakdown'}"
        )
        print("-" * 70)
        total_need = 0
        for cat in categories:
            plan = CATEGORY_PLAN.get(cat, {"min": 10, "target": 2})
            have = _cat_total(counts, cat)
            need = max(0, plan["target"] - have)
            total_need += need
            breakdown = counts.get(cat, {})
            parts = (
                ", ".join(f"{v} {k}" for k, v in breakdown.items())
                if breakdown
                else "none"
            )
            status = "DONE" if need == 0 else ""
            print(
                f"{cat:<20} {have:>5} {plan['target']:>7} {need:>5} {plan['min']:>4}  ({parts}) {status}"
            )
        print("-" * 70)
        print(f"Total still needed: {total_need}")
        print()
        print("Sources per category (in order):")
        print("  1. iFood (structured, no AI cost)")
        print("  2. Image search + Instagram (gpt-4o-mini classification)")
        print("  3. PDF web search (page-1 classification)")
        return

    # Run per category
    summary: dict[str, dict] = {}

    for cat in categories:
        plan = CATEGORY_PLAN.get(cat, {"min": 10, "target": 2})
        min_p = plan["min"]

        # Re-count from disk before each category (captures additions from prior categories)
        counts = _count_corpus_by_cat()
        have = _cat_total(counts, cat)
        need = max(0, plan["target"] - have)

        print()
        print(f"{'=' * 50}")
        print(f"  {cat.upper()} — have {have}, need {need}, min_products={min_p}")
        print(f"{'=' * 50}")

        if need == 0:
            print("  Already at target, skipping.")
            summary[cat] = {"have": have, "added": 0, "sources": {}}
            continue

        added = {"images": 0, "pdfs": 0}

        # --- Source 1: Image search + Instagram ---
        still_need = need
        if still_need > 0:
            print(
                f"\n  [Images] Searching images + Instagram (need {still_need} more)..."
            )
            before = _cat_total(_count_corpus_by_cat(), cat)
            try:
                await run_search_mode(
                    [cat],
                    cities,
                    dry_run=False,
                    max_per_query=min(10, still_need * 3),
                    engine="google",
                    instagram=True,
                    pdfs=False,
                    min_products=min_p,
                )
            except Exception as e:
                print(f"  [Images] Error: {e}")
            after = _cat_total(_count_corpus_by_cat(), cat)
            added["images"] = max(0, after - before)
            still_need = need - added["images"]

        # --- Source 3: PDF web search ---
        if still_need > 0:
            print(f"\n  [PDFs] Searching PDF menus (need {still_need} more)...")
            before = _cat_total(_count_corpus_by_cat(), cat)
            try:
                await run_search_mode(
                    [cat],
                    cities,
                    dry_run=False,
                    max_per_query=min(10, still_need * 3),
                    engine="google",
                    instagram=False,
                    pdfs=True,
                    min_products=min_p,
                )
            except Exception as e:
                print(f"  [PDFs] Error: {e}")
            after = _cat_total(_count_corpus_by_cat(), cat)
            added["pdfs"] = max(0, after - before)

        total_added = added["images"] + added["pdfs"]
        summary[cat] = {"have": have, "added": total_added, "sources": added}

    # Final report
    print()
    print("=" * 60)
    print("FULL SCRAPE RESULTS")
    print("=" * 60)
    print(
        f"{'Category':<20} {'Had':>4} {'Images':>7} {'PDFs':>5} {'Total':>6} {'Target':>7}"
    )
    print("-" * 55)
    grand_total = 0
    for cat in categories:
        s = summary.get(cat, {"have": 0, "added": 0, "sources": {}})
        plan = CATEGORY_PLAN.get(cat, {"target": 2})
        src = s.get("sources", {})
        total = s["have"] + s["added"]
        grand_total += s["added"]
        met = "OK" if total >= plan["target"] else ""
        print(
            f"{cat:<20} {s['have']:>4} "
            f"{src.get('images', 0):>7} {src.get('pdfs', 0):>5} "
            f"{total:>6} /{plan['target']:<5} {met}"
        )
    print("-" * 55)
    print(f"Total new entries: {grand_total}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Scrape real restaurant menu images for the QA corpus.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        choices=["search", "gmaps", "ifood", "full"],
        default="search",
        help=(
            "search = image/PDF search + classify; "
            "gmaps = Google Maps places + photos; "
            "ifood = structured scrape from iFood pages; "
            "full = all sources per category with tailored thresholds"
        ),
    )
    parser.add_argument(
        "--categories",
        type=str,
        default=None,
        help=f"Comma-separated categories (default: all {len(DEFAULT_CATEGORIES)})",
    )
    parser.add_argument(
        "--cities",
        type=str,
        default=None,
        help=f"Comma-separated cities (default: {len(DEFAULT_CITIES)} major BR cities)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="For gmaps mode: restaurants per category×city combo (default: 5)",
    )
    parser.add_argument(
        "--max-per-query",
        type=int,
        default=15,
        help="For search mode: max images per search query (default: 15)",
    )
    parser.add_argument(
        "--engine",
        choices=["auto", "ddg", "google"],
        default="auto",
        help="Search engine for --mode=search (default: auto — Google CSE if keys set, else DuckDuckGo)",
    )
    parser.add_argument(
        "--instagram",
        action="store_true",
        help="Include Instagram-targeted search queries (site:instagram.com)",
    )
    parser.add_argument(
        "--pdfs",
        action="store_true",
        help="Include PDF menu search queries (filetype:pdf). PDFs are saved to corpus/pdfs/",
    )
    parser.add_argument(
        "--min-products",
        type=int,
        default=DEFAULT_MIN_PRODUCTS,
        help=f"Minimum estimated product count for a REAL menu to be accepted (default: {DEFAULT_MIN_PRODUCTS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be searched without downloading",
    )
    args = parser.parse_args()

    categories = args.categories.split(",") if args.categories else DEFAULT_CATEGORIES
    cities = (
        [c.strip() for c in args.cities.split(",")] if args.cities else DEFAULT_CITIES
    )

    if not OPENAI_API_KEY and not args.dry_run:
        print("ERROR: OPENAI_API_KEY env var required for image classification")
        sys.exit(1)

    print(f"Mode: {args.mode}")
    print(f"Categories: {categories}")
    print(f"Cities: {cities}")
    print()

    if args.mode == "search":
        asyncio.run(
            run_search_mode(
                categories,
                cities,
                args.dry_run,
                args.max_per_query,
                args.engine,
                args.instagram,
                args.pdfs,
                args.min_products,
            )
        )
    elif args.mode == "gmaps":
        asyncio.run(run_gmaps_mode(categories, cities, args.limit, args.dry_run))
    elif args.mode == "ifood":
        asyncio.run(
            run_ifood_mode(
                categories, cities, args.limit, args.min_products, args.dry_run
            )
        )
    elif args.mode == "full":
        asyncio.run(run_full_mode(categories, cities, args.dry_run))


if __name__ == "__main__":
    main()
