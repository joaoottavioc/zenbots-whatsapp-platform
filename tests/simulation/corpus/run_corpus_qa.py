"""
Run QA tests against validated corpus images.

Picks 5 random images from different categories, creates bots with
cached product data, runs simulation tests, cleans up.

Run: python tests/simulation/corpus/run_corpus_qa.py
"""

import asyncio
import json
import random
import subprocess
import time
from pathlib import Path

import httpx

CORPUS = Path(__file__).parent
PROJECT_ROOT = CORPUS.parent.parent.parent
MANIFEST = CORPUS / "manifest.json"

BASE_URL = "http://localhost:8000"
EMAIL = "test1@exemplo.com"
PASSWORD = "Test123$"

ADD_PHRASES = ["quero um {}", "me ve um {}", "manda um {}"]
REMOVE_PHRASES = ["tira o {}", "remove o {}"]


def load_extraction(image_id):
    f = CORPUS / "extractions" / f"{image_id}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return None


def gen_scenarios(products, unavail_indices):
    available = [p for i, p in enumerate(products) if i not in unavail_indices]
    unavailable = [products[i] for i in unavail_indices]

    main = [
        p for p in available
        if p.get("category", "").lower() not in
        ("bebidas", "drinks", "sobremesas", "adicionais", "complementos", "acompanhamentos")
    ]
    if len(main) < 2:
        main = available[:4]
    if len(available) < 5:
        return None

    single = main[0]
    multi_1 = main[1] if len(main) > 1 else available[1]
    multi_2 = available[2] if available[2] != multi_1 else available[3]
    remove_item = main[2] if len(main) > 2 else available[2]
    checkout_item = main[3] if len(main) > 3 else available[3]
    unavail_prod = unavailable[0] if unavailable else None
    avail_with = available[4] if len(available) > 4 else available[0]

    sc = {
        "add_single_msg": random.choice(ADD_PHRASES).format(single["name"].lower()),
        "add_single_expected": [(single["name"], 1)],
        "add_multi_msg": f"quero 2 {multi_1['name'].lower()} e 1 {multi_2['name'].lower()}",
        "add_multi_expected": [(multi_1["name"], 2), (multi_2["name"], 1)],
        "remove_add_msg": f"quero 2 {remove_item['name'].lower()}",
        "remove_add_expected": (remove_item["name"], 2),
        "remove_msg": random.choice(REMOVE_PHRASES).format(remove_item["name"].lower()),
        "suggestion_msg": "o que tem de bom?",
        "checkout_product_msg": f"quero 1 {checkout_item['name'].lower()}",
        "checkout_product_expected": (checkout_item["name"], 1),
    }
    if unavail_prod:
        sc["unavailable_msg"] = (
            f"quero um {unavail_prod['name'].lower()} e um {avail_with['name'].lower()}"
        )
        sc["unavailable_available"] = avail_with["name"]
        sc["unavailable_name"] = unavail_prod["name"]
    else:
        sc["unavailable_msg"] = None
    return sc


def run_sql(sql):
    subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "psql",
         "-U", "postgres", "-d", "botbuilder", "-c", sql],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True,
    )


def pre_cleanup():
    run_sql("""DO $$ DECLARE _bids int[]; _cids int[]; _carts int[];
    BEGIN
      SELECT ARRAY(SELECT id FROM bot WHERE (phone_number_id LIKE 'rand-test-%'
        OR phone_number_id LIKE 'corpus-build-%') AND id != 74) INTO _bids;
      IF array_length(_bids,1) IS NULL THEN RETURN; END IF;
      SELECT ARRAY(SELECT id FROM contact WHERE bot_id=ANY(_bids)) INTO _cids;
      SELECT ARRAY(SELECT id FROM shoppingcart WHERE contact_id=ANY(_cids)) INTO _carts;
      DELETE FROM orderitem WHERE order_id IN (SELECT id FROM "order" WHERE contact_id=ANY(_cids));
      DELETE FROM "order" WHERE contact_id=ANY(_cids);
      DELETE FROM cartitem WHERE cart_id=ANY(_carts);
      DELETE FROM shoppingcart WHERE id=ANY(_carts);
      DELETE FROM conversationhistory WHERE bot_id=ANY(_bids);
      DELETE FROM contact WHERE id=ANY(_cids);
      DELETE FROM product WHERE bot_id=ANY(_bids);
      DELETE FROM subscription WHERE bot_id=ANY(_bids);
      DELETE FROM bot WHERE id=ANY(_bids);
    END $$;""")


def cleanup_bots(bot_ids):
    if not bot_ids:
        return
    ids = ",".join(str(x) for x in bot_ids)
    run_sql(f"""DO $$ DECLARE _bids int[]:=ARRAY[{ids}]; _cids int[]; _carts int[];
    BEGIN
      SELECT ARRAY(SELECT id FROM contact WHERE bot_id=ANY(_bids)) INTO _cids;
      SELECT ARRAY(SELECT id FROM shoppingcart WHERE contact_id=ANY(_cids)) INTO _carts;
      DELETE FROM orderitem WHERE order_id IN (SELECT id FROM "order" WHERE contact_id=ANY(_cids));
      DELETE FROM "order" WHERE contact_id=ANY(_cids);
      DELETE FROM cartitem WHERE cart_id=ANY(_carts);
      DELETE FROM shoppingcart WHERE id=ANY(_carts);
      DELETE FROM conversationhistory WHERE bot_id=ANY(_bids);
      DELETE FROM contact WHERE id=ANY(_cids);
      DELETE FROM product WHERE bot_id=ANY(_bids);
      DELETE FROM subscription WHERE bot_id=ANY(_bids);
      DELETE FROM bot WHERE id=ANY(_bids);
    END $$;""")


async def main():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    validated = [e for e in manifest if e.get("validated")]

    if len(validated) < 5:
        print(f"Only {len(validated)} validated images. Need at least 5.")
        return

    # Pick 5 from different categories
    random.shuffle(validated)
    selected = []
    seen_cats = set()
    for e in validated:
        cat = e.get("category", "outros")
        if cat not in seen_cats:
            selected.append(e)
            seen_cats.add(cat)
        if len(selected) >= 5:
            break
    while len(selected) < 5:
        for e in validated:
            if e not in selected:
                selected.append(e)
                break

    print("=" * 60)
    print("CORPUS-BASED QA RUN")
    print("=" * 60)
    print()
    for s in selected:
        print(f"  {s['id']} -- {s['category']} -- {s['product_count']} products")
    print()

    print("Pre-run cleanup...")
    pre_cleanup()

    bots = {}

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60) as client:
        r = await client.post("/auth/token", data={"username": EMAIL, "password": PASSWORD})
        if r.status_code != 200:
            print(f"Login failed: {r.status_code}")
            return
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        for i, entry in enumerate(selected):
            label = f"rand{i}"
            phone_id = f"rand-test-{i}-{int(time.time())}"
            extraction = load_extraction(entry["id"])
            if not extraction:
                print(f"  SKIP {entry['id']}: no extraction")
                continue

            products = extraction["products"]
            name = entry.get("restaurant_name", entry["id"])[:50]
            print(f"  Creating: {name} ({entry['category']})...", end=" ", flush=True)

            bot_resp = await client.post("/bots", json={
                "restaurant_name": name,
                "phone_number_id": phone_id,
                "whatsapp_token": "fake-token",
                "whatsapp_number": f"5511r{i}{int(time.time()) % 99999:05d}",
            }, headers=headers)

            if bot_resp.status_code != 201:
                print(f"FAIL ({bot_resp.status_code})")
                continue
            bot_id = bot_resp.json()["id"]

            prod_ids = []
            for p in products:
                pr = await client.post(f"/bots/{bot_id}/products", json={
                    "name": p["name"], "price": p["price"],
                    "category": p.get("category", "Geral"),
                    "description": p.get("description", ""),
                }, headers=headers)
                prod_ids.append(pr.json()["id"] if pr.status_code == 201 else None)

            non_drink = [
                j for j, p in enumerate(products)
                if "bebida" not in p.get("category", "").lower()
            ]
            unavail_idx = random.sample(non_drink[:10], min(2, len(non_drink)))
            for idx in unavail_idx:
                pid = prod_ids[idx]
                if pid:
                    await client.put(
                        f"/bots/{bot_id}/products/{pid}",
                        json={"is_available": False}, headers=headers,
                    )

            scenarios = gen_scenarios(products, unavail_idx)
            if not scenarios:
                print("SKIP (not enough products)")
                continue

            bots[label] = {
                "bot_id": bot_id,
                "phone_number_id": phone_id,
                "name": name,
                "category": entry.get("category", "outros"),
                "scenarios": scenarios,
            }
            print(f"OK (id={bot_id})")

    if not bots:
        print("No bots created!")
        return

    # Subscriptions
    vals = ",".join(
        f"({b['bot_id']},1,'rand-sub-{b['bot_id']}','authorized',"
        f"'2027-04-02',NOW(),NOW(),'pro')"
        for b in bots.values()
    )
    run_sql(
        "INSERT INTO subscription (bot_id,user_id,mp_subscription_id,"
        f"status,current_period_end,created_at,updated_at,plan_type) VALUES {vals};"
    )

    # Write test data
    test_data = {
        label: {
            "bot_id": b["bot_id"],
            "phone_number_id": b["phone_number_id"],
            "name": b["name"],
            "category": b["category"],
            "scenarios": b["scenarios"],
        }
        for label, b in bots.items()
    }
    Path(PROJECT_ROOT / "tests/simulation/_random_test_data.json").write_text(
        json.dumps(test_data, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    print(f"\n{len(bots)} bots ready. Running tests...\n")

    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "backend",
         "pytest", "tests/simulation/test_random_restaurants.py", "-v", "--tb=short"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=300,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr)

    # Cleanup
    print("\nCleaning up...")
    cleanup_bots([b["bot_id"] for b in bots.values()])
    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
