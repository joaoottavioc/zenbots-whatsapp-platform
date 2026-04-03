"""
Random Restaurant Test Runner (v2 — Image-Based)

Automated pipeline that:
1. Picks 5 random restaurant categories from 30+
2. Searches DuckDuckGo Images for real menu photos
3. Downloads menu images and uploads via /catalog/upload-from-file
   (uses the Cadastro Magico gpt-4o extraction pipeline)
4. Creates bots + products via ZenBots API
5. Flags 2 random products as unavailable per bot
6. Auto-generates test scenarios from extracted products
7. Writes test data file, runs pytest, reports results
8. Saves structured JSON for historical dashboard tracking
9. Cleans up bots from DB

Usage:
    export OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' .env | cut -d= -f2)
    python -m tests.simulation.random_restaurant_runner

    # Or via Claude Code command:
    /test-restaurants 5
"""

import argparse
import asyncio
import json
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import httpx
from ddgs import DDGS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:8000"
EMAIL = "test1@exemplo.com"
PASSWORD = "Test123$"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

CATEGORIES = [
    "pastelaria", "pizzaria", "hamburgueria", "sushi", "açaiteria",
    "tapiocaria", "espetaria", "creperia", "marmitaria", "padaria",
    "cafeteria", "sorveteria", "doceria", "casa de sucos", "petiscaria",
    "lanchonete", "comida árabe", "comida japonesa", "comida mexicana",
    "comida baiana", "comida italiana", "restaurante vegano", "food truck",
    "churrascaria", "temakeria", "poke", "hot dog gourmet", "comida chinesa",
    "confeitaria", "rotisseria", "comida nordestina", "comida mineira",
]

NEIGHBORHOODS = [
    "pinheiros", "vila mariana", "mooca", "tatuapé", "perdizes",
    "vila madalena", "itaim bibi", "brooklin", "santana", "lapa",
    "liberdade", "bela vista", "consolação", "jardins", "higienópolis",
]

TESTED_LOG = PROJECT_ROOT / ".claude" / "simulation-reports" / "tested_restaurants.txt"
REPORT_DIR = PROJECT_ROOT / ".claude" / "simulation-reports"
RUNS_DIR = REPORT_DIR / "runs"

# ---------------------------------------------------------------------------
# Step 1: Search for menu images
# ---------------------------------------------------------------------------


def _load_tested() -> set[str]:
    if TESTED_LOG.exists():
        return {line.strip().lower() for line in TESTED_LOG.read_text().splitlines() if line.strip()}
    return set()


def _save_tested(name: str):
    TESTED_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(TESTED_LOG, "a", encoding="utf-8") as f:
        f.write(f"{name}\n")


def search_menu_image(category: str) -> dict | None:
    """Search DuckDuckGo Images for a real menu photo."""
    neighborhood = random.choice(NEIGHBORHOODS)
    # Try multiple query patterns — ddgs images can be flaky
    queries = [
        f"cardapio {category} precos",
        f"menu {category} delivery sao paulo",
        f"cardapio {category} {neighborhood}",
        f"cardapio {category}",
    ]

    results = []
    ddgs = DDGS()
    for query in queries:
        try:
            hits = list(ddgs.images(query, max_results=10))
            results.extend(hits)
            if len(results) >= 5:
                break
        except Exception:
            continue
        # Small delay to avoid rate limiting
        time.sleep(0.5)

    if not results:
        print(f"  Image search: no results for '{category}'")
        return None

    # Shuffle to get variety across runs
    results = list(results)
    random.shuffle(results)

    for r in results:
        url = r.get("image", "")
        title = r.get("title", "")
        width = r.get("width", 0)
        height = r.get("height", 0)

        # Filter: need reasonable resolution and image format
        if width < 400 or height < 400:
            continue
        if not any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
            # Allow URLs without clear extension (many CDNs don't have them)
            if "svg" in url.lower() or "gif" in url.lower():
                continue

        # Extract restaurant name from title
        name = title.split(" - ")[0].split(" | ")[0].split(",")[0].strip()
        for suffix in ["Delivery", "delivery", "Cardápio", "cardápio",
                        "Menu", "menu", "PDF", "Preços", "preços"]:
            name = name.replace(suffix, "").strip()
        name = name.strip(" -|,.")

        if not name or len(name) < 3:
            name = f"{category.title()} {neighborhood.title()}"

        return {
            "name": name[:50],
            "category": category,
            "neighborhood": neighborhood,
            "image_url": url,
            "source": "image",
        }

    return None


def download_image(url: str) -> bytes | None:
    """Download an image, return bytes."""
    try:
        resp = httpx.get(url, timeout=20, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200 and len(resp.content) > 5000:
            return resp.content
    except Exception as e:
        print(f"  Download failed: {e}")
    return None


# ---------------------------------------------------------------------------
# Step 2: Fallback — generate menu via gpt-4o-mini (if image extraction fails)
# ---------------------------------------------------------------------------


def generate_menu_fallback(restaurant: dict) -> list[dict]:
    """Fallback: use gpt-4o-mini to generate a realistic menu."""
    import openai

    client = openai.OpenAI()
    prompt = f"""Generate a realistic menu for a Brazilian {restaurant['category']} restaurant.

Name: {restaurant['name']}
Location: {restaurant['neighborhood']}, São Paulo

Rules:
- Return exactly 15 products as JSON
- Use realistic 2025-2026 São Paulo delivery prices in R$
- All names and descriptions in Brazilian Portuguese
- Include 3-5 categories
- Include variety: main items, sides/extras, drinks
- DO NOT include "R$" in the price field

Return ONLY valid JSON: {{"products": [{{"name": "...", "price": 00.00, "category": "...", "description": "..."}}]}}"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        products = data.get("products", data.get("menu", []))
        if isinstance(products, list) and len(products) >= 10:
            return products[:15]
    except Exception as e:
        print(f"  Fallback generation failed: {e}")
    return []


# ---------------------------------------------------------------------------
# Step 3: Generate test scenarios
# ---------------------------------------------------------------------------


def generate_test_scenarios(products: list[dict], unavailable_indices: list[int]) -> dict:
    """Auto-generate test scenarios from the product list."""
    available = [p for i, p in enumerate(products) if i not in unavailable_indices]
    unavailable = [products[i] for i in unavailable_indices]

    main_items = [p for p in available if p.get("category", "").lower() not in
                  ("bebidas", "drinks", "sobremesas", "adicionais", "complementos",
                   "acompanhamentos", "extras")]
    drinks = [p for p in available if "bebida" in p.get("category", "").lower()]
    sides = [p for p in available if p not in main_items and p not in drinks]

    if len(main_items) < 2:
        main_items = available[:4]

    if len(available) < 5:
        return {"add_single_msg": None}  # Not enough products

    single = main_items[0]
    multi_1 = main_items[1] if len(main_items) > 1 else available[1]
    multi_2 = (drinks[0] if drinks else sides[0] if sides else available[2])
    remove_item = main_items[2] if len(main_items) > 2 else available[2]
    checkout_item = main_items[3] if len(main_items) > 3 else available[3]

    unavail_product = unavailable[0] if unavailable else None
    avail_with_unavail = available[4] if len(available) > 4 else available[0]

    scenarios = {
        "add_single_msg": f"quero um {single['name'].lower()}",
        "add_single_expected": [(single["name"], 1)],
        "add_multi_msg": f"quero 2 {multi_1['name'].lower()} e 1 {multi_2['name'].lower()}",
        "add_multi_expected": [(multi_1["name"], 2), (multi_2["name"], 1)],
        "remove_add_msg": f"quero 2 {remove_item['name'].lower()}",
        "remove_add_expected": (remove_item["name"], 2),
        "remove_msg": f"tira o {remove_item['name'].lower()}",
        "suggestion_msg": "o que tem de bom?",
        "checkout_product_msg": f"quero 1 {checkout_item['name'].lower()}",
        "checkout_product_expected": (checkout_item["name"], 1),
    }

    if unavail_product:
        scenarios["unavailable_msg"] = (
            f"quero um {unavail_product['name'].lower()} e um {avail_with_unavail['name'].lower()}"
        )
        scenarios["unavailable_available"] = avail_with_unavail["name"]
        scenarios["unavailable_name"] = unavail_product["name"]
    else:
        scenarios["unavailable_msg"] = None

    return scenarios


# ---------------------------------------------------------------------------
# Step 4: Create bots via API (image upload or manual product creation)
# ---------------------------------------------------------------------------


async def create_bots(restaurants: list[dict]) -> dict:
    """Create bots + products via the ZenBots API."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60) as client:
        resp = await client.post("/auth/token", data={"username": EMAIL, "password": PASSWORD})
        if resp.status_code != 200:
            print(f"Login failed: {resp.status_code}")
            return {}
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        bots = {}
        for i, rest in enumerate(restaurants):
            label = f"rand{i}"
            phone_id = f"rand-test-{i}-{int(time.time())}"

            bot_resp = await client.post("/bots", json={
                "restaurant_name": rest["name"],
                "phone_number_id": phone_id,
                "whatsapp_token": "fake-token",
                "whatsapp_number": f"5511rand{i}{int(time.time()) % 100000:05d}",
            }, headers=headers)

            if bot_resp.status_code != 201:
                print(f"  Failed to create bot '{rest['name']}': {bot_resp.status_code}")
                continue

            bot_id = bot_resp.json()["id"]
            print(f"  Bot created: {rest['name']} (id={bot_id})")

            # --- Try image-based extraction first ---
            products = []
            if rest.get("image_data"):
                print("    Uploading menu image via Cadastro Magico...", end=" ", flush=True)
                # Determine file extension from URL
                img_url = rest.get("image_url", "")
                ext = ".jpg"
                for e in (".png", ".webp", ".jpeg"):
                    if e in img_url.lower():
                        ext = e
                        break

                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                        tmp.write(rest["image_data"])
                        tmp_path = tmp.name

                    mime = f"image/{'jpeg' if ext in ('.jpg', '.jpeg') else ext[1:]}"
                    with open(tmp_path, "rb") as fh:
                        upload_resp = await client.post(
                            f"/bots/{bot_id}/catalog/upload-from-file",
                            files={"file": (f"menu{ext}", fh, mime)},
                            headers=headers,
                            timeout=120,
                        )
                    if upload_resp.status_code == 201:
                        # Fetch created products
                        prod_resp = await client.get(
                            f"/bots/{bot_id}/products?limit=200", headers=headers)
                        if prod_resp.status_code == 200:
                            raw_products = prod_resp.json()
                            products = [
                                {"name": p["name"], "price": p["price"],
                                 "category": p.get("category", "Geral"),
                                 "description": p.get("description", ""),
                                 "id": p["id"]}
                                for p in raw_products
                            ]
                            print(f"{len(products)} products extracted")
                            rest["source"] = "image_extraction"
                    else:
                        print(f"FAILED ({upload_resp.status_code})")
                        body = upload_resp.text[:200]
                        print(f"    Response: {body}")
                except Exception as e:
                    print(f"FAILED: {e}")
                finally:
                    Path(tmp_path).unlink(missing_ok=True)

            # --- Fallback: add products manually ---
            if not products and rest.get("products"):
                print(f"    Adding {len(rest['products'])} products via API...")
                for prod in rest["products"]:
                    p_resp = await client.post(
                        f"/bots/{bot_id}/products", json=prod, headers=headers)
                    if p_resp.status_code == 201:
                        p_data = p_resp.json()
                        products.append({
                            "name": p_data["name"], "price": p_data["price"],
                            "category": p_data.get("category", "Geral"),
                            "description": p_data.get("description", ""),
                            "id": p_data["id"],
                        })
                rest["source"] = "llm_fallback"

            if len(products) < 5:
                print(f"    SKIP: Only {len(products)} products (need >= 5)")
                continue

            rest["products_created"] = products

            # Mark 2 random as unavailable
            non_drink = [j for j, p in enumerate(products)
                         if "bebida" not in p.get("category", "").lower()]
            unavail_indices = random.sample(
                non_drink[:10], min(2, len(non_drink))
            )

            for idx in unavail_indices:
                pid = products[idx].get("id")
                if pid:
                    await client.put(
                        f"/bots/{bot_id}/products/{pid}",
                        json={"is_available": False}, headers=headers)
                    print(f"    [UNAVAIL] {products[idx]['name']}")

            scenarios = generate_test_scenarios(products, unavail_indices)
            if not scenarios.get("add_single_msg"):
                print("    SKIP: Not enough products for test scenarios")
                continue

            bots[label] = {
                "bot_id": bot_id,
                "phone_number_id": phone_id,
                "name": rest["name"],
                "category": rest["category"],
                "source": rest.get("source", "unknown"),
                "product_count": len(products),
                "scenarios": scenarios,
            }

        return bots


# ---------------------------------------------------------------------------
# Step 5: Create subscriptions
# ---------------------------------------------------------------------------


def create_subscriptions(bots: dict):
    bot_ids = [b["bot_id"] for b in bots.values()]
    if not bot_ids:
        return
    values = ", ".join(
        f"({bid}, 1, 'rand-sub-{bid}', 'authorized', '2027-04-02', NOW(), NOW(), 'pro')"
        for bid in bot_ids
    )
    sql = (
        "INSERT INTO subscription (bot_id, user_id, mp_subscription_id, "
        "status, current_period_end, created_at, updated_at, plan_type) "
        f"VALUES {values};"
    )
    subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "psql", "-U", "postgres",
         "-d", "botbuilder", "-c", sql],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# Step 6: Write test data + run pytest
# ---------------------------------------------------------------------------


def write_test_data(bots: dict):
    data_file = PROJECT_ROOT / "tests" / "simulation" / "_random_test_data.json"
    serializable = {}
    for label, bot in bots.items():
        serializable[label] = {
            "bot_id": bot["bot_id"],
            "phone_number_id": bot["phone_number_id"],
            "name": bot["name"],
            "category": bot["category"],
            "scenarios": bot["scenarios"],
        }
    data_file.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")


def run_tests() -> str:
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "backend",
         "pytest", "tests/simulation/test_random_restaurants.py", "-v", "--tb=short"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=300,
    )
    return result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Step 7: Cleanup
# ---------------------------------------------------------------------------


def cleanup_bots(bots: dict):
    bot_ids = [b["bot_id"] for b in bots.values()]
    if not bot_ids:
        return
    ids_str = ", ".join(str(bid) for bid in bot_ids)
    sql = f"""
    DO $$
    DECLARE
      _bot_ids int[] := ARRAY[{ids_str}];
      _contact_ids int[];
      _cart_ids int[];
    BEGIN
      SELECT ARRAY(SELECT id FROM contact WHERE bot_id = ANY(_bot_ids)) INTO _contact_ids;
      SELECT ARRAY(SELECT id FROM shoppingcart WHERE contact_id = ANY(_contact_ids)) INTO _cart_ids;
      DELETE FROM orderitem WHERE order_id IN (SELECT id FROM "order" WHERE contact_id = ANY(_contact_ids));
      DELETE FROM "order" WHERE contact_id = ANY(_contact_ids);
      DELETE FROM cartitem WHERE cart_id = ANY(_cart_ids);
      DELETE FROM shoppingcart WHERE id = ANY(_cart_ids);
      DELETE FROM conversationhistory WHERE bot_id = ANY(_bot_ids);
      DELETE FROM contact WHERE id = ANY(_contact_ids);
      DELETE FROM product WHERE bot_id = ANY(_bot_ids);
      DELETE FROM subscription WHERE bot_id = ANY(_bot_ids);
      DELETE FROM bot WHERE id = ANY(_bot_ids);
    END $$;
    """
    subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "psql", "-U", "postgres",
         "-d", "botbuilder", "-c", sql],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# Step 8: Report + JSON storage
# ---------------------------------------------------------------------------


def parse_test_results(test_output: str) -> dict:
    """Parse pytest output into structured results."""
    lines = test_output.split("\n")
    results = {"passed": [], "failed": []}

    for ln in lines:
        if "PASSED" in ln:
            results["passed"].append(ln.strip())
        elif "FAILED" in ln:
            results["failed"].append(ln.strip())

    # Extract failure details
    failures = []
    for ln in lines:
        if "AssertionError" in ln or "AssertionError" in ln:
            failures.append(ln.strip())

    results["failure_details"] = failures
    results["total_passed"] = len(results["passed"])
    results["total_failed"] = len(results["failed"])
    results["total"] = results["total_passed"] + results["total_failed"]
    results["pass_rate"] = (
        round(100 * results["total_passed"] / results["total"], 1)
        if results["total"] > 0 else 0
    )
    return results


def save_run_json(bots: dict, results: dict, timestamp: str):
    """Save structured JSON for dashboard tracking."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    run_data = {
        "timestamp": timestamp,
        "pass_rate": results["pass_rate"],
        "total_passed": results["total_passed"],
        "total_failed": results["total_failed"],
        "total": results["total"],
        "restaurants": [],
    }

    for label, bot in bots.items():
        run_data["restaurants"].append({
            "name": bot["name"],
            "category": bot["category"],
            "source": bot.get("source", "unknown"),
            "product_count": bot.get("product_count", 0),
        })

    # Per-scenario results
    scenario_types = ["AddSingle", "AddMulti", "Unavailable", "Remove",
                      "Suggestions", "Checkout", "Greeting"]
    scenario_stats = {}
    for st in scenario_types:
        p = sum(1 for ln in results["passed"] if st.lower() in ln.lower()
                or st.replace("Add", "add_") in ln)
        f = sum(1 for ln in results["failed"] if st.lower() in ln.lower()
                or st.replace("Add", "add_") in ln)
        scenario_stats[st] = {"passed": p, "failed": f, "total": p + f}
    run_data["scenarios"] = scenario_stats

    json_file = RUNS_DIR / f"{timestamp}.json"
    json_file.write_text(json.dumps(run_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return json_file


def generate_report(bots: dict, test_output: str) -> str:
    """Generate markdown report + save JSON."""
    results = parse_test_results(test_output)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    # Save JSON for dashboard
    json_file = save_run_json(bots, results, timestamp)

    report = f"# Random Restaurant Test Run -- {timestamp}\n\n"
    report += f"> Score: **{results['total_passed']}/{results['total']} ({results['pass_rate']}%)**\n\n"

    report += "## Restaurants Tested\n\n"
    report += "| # | Restaurant | Type | Source | Products |\n"
    report += "|---|---|---|---|---|\n"
    for i, (label, bot) in enumerate(bots.items(), 1):
        source = bot.get("source", "?")
        pcount = bot.get("product_count", "?")
        report += f"| {i} | {bot['name']} | {bot['category']} | {source} | {pcount} |\n"

    report += "\n## Results\n\n```\n"
    for ln in results["passed"] + results["failed"]:
        report += ln + "\n"
    report += "```\n"

    if results["failure_details"]:
        report += "\n## Failure Details\n\n```\n"
        for ln in results["failure_details"]:
            report += ln + "\n"
        report += "```\n"

    report_file = REPORT_DIR / f"random_run_{timestamp}.md"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(report, encoding="utf-8")
    print(f"\nReport: {report_file}")
    print(f"JSON:   {json_file}")

    return report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Random Restaurant Test Runner")
    parser.add_argument("--count", type=int, default=5,
                        help="Number of restaurants to test")
    parser.add_argument("--no-cleanup", action="store_true",
                        help="Don't delete bots after test")
    parser.add_argument("--no-images", action="store_true",
                        help="Skip image search, use LLM-generated menus only")
    args = parser.parse_args()

    print("=" * 60)
    print("RANDOM RESTAURANT TEST RUNNER (v2 - Image Extraction)")
    print("=" * 60)

    # Step 1: Pick random categories
    _load_tested()  # populate log if needed
    categories = random.sample(CATEGORIES, min(args.count, len(CATEGORIES)))
    print(f"\nCategories: {', '.join(categories)}")

    restaurants = []
    for cat in categories:
        print(f"\nSearching for {cat}...")

        if not args.no_images:
            # Try image search first
            info = search_menu_image(cat)
            if info:
                print(f"  Found image: {info['name']}")
                print("  Downloading...", end=" ", flush=True)
                img_data = download_image(info["image_url"])
                if img_data:
                    info["image_data"] = img_data
                    print(f"{len(img_data) // 1024}KB")
                    restaurants.append(info)
                    continue
                else:
                    print("FAILED")

        # Fallback: LLM-generated menu
        print("  Falling back to LLM menu generation...")
        neighborhood = random.choice(NEIGHBORHOODS)
        fallback_name = f"{cat.title()} {neighborhood.title()}"
        rest_info = {
            "name": fallback_name,
            "category": cat,
            "neighborhood": neighborhood,
            "source": "llm_fallback",
        }
        products = generate_menu_fallback(rest_info)
        if products:
            rest_info["products"] = products
            print(f"  Generated: {fallback_name} ({len(products)} products)")
            restaurants.append(rest_info)
        else:
            print(f"  FAILED: No menu for {cat}")

    if not restaurants:
        print("ERROR: No restaurants found.")
        sys.exit(1)

    print(f"\n--- Creating {len(restaurants)} bots ---")
    bots = asyncio.run(create_bots(restaurants))

    if not bots:
        print("ERROR: No bots created.")
        sys.exit(1)

    print("\nCreating subscriptions...")
    create_subscriptions(bots)

    print("\nWriting test data...")
    write_test_data(bots)

    print("\nRunning tests...\n")
    output = run_tests()
    print(output)

    # Log tested restaurants
    for rest in restaurants:
        _save_tested(rest["name"])

    # Report + JSON
    report = generate_report(bots, output)
    print(report)

    # Cleanup
    if not args.no_cleanup:
        print("\nCleaning up bots...")
        cleanup_bots(bots)
        print("Done.")
    else:
        print("\nSkipping cleanup (--no-cleanup)")


if __name__ == "__main__":
    main()
