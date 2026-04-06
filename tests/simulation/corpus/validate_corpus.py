"""
Validate corpus images via Cadastro Mágico.

Uploads each unvalidated image to the API, extracts products,
auto-detects category, saves extraction cache.

Run: python tests/simulation/corpus/validate_corpus.py
"""

import asyncio
import json
import subprocess
import time
from pathlib import Path

import httpx

CORPUS = Path(__file__).parent
MANIFEST = CORPUS / "manifest.json"
EXTRACTIONS = CORPUS / "extractions"
REJECTED = CORPUS / "rejected"
PROJECT_ROOT = CORPUS.parent.parent.parent

EXTRACTIONS.mkdir(exist_ok=True)
REJECTED.mkdir(exist_ok=True)

BASE_URL = "http://localhost:8000"
EMAIL = "test1@exemplo.com"
PASSWORD = "Test123$"

CATEGORY_KEYWORDS = {
    "pizzaria": [
        "pizza",
        "margherita",
        "calabresa",
        "pepperoni",
        "mussarela",
        "napolitana",
        "portuguesa",
    ],
    "hamburgueria": [
        "burger",
        "hambúrguer",
        "hamburger",
        "smash",
        "cheeseburger",
        "artesanal",
    ],
    "sushi": [
        "temaki",
        "sashimi",
        "sushi",
        "uramaki",
        "niguiri",
        "hot roll",
        "japa",
        "combinado",
    ],
    "pastelaria": ["pastel"],
    "açaiteria": ["açaí", "acai", "tigela"],
    "lanchonete": ["x-burger", "x-tudo", "x-salada", "lanche", "misto quente"],
    "padaria": ["pão", "croissant", "bolo", "focaccia", "brioche"],
    "marmitaria": [
        "marmita",
        "parmegiana",
        "executivo",
        "prato feito",
        "feijoada",
        "virado",
    ],
    "espetaria": ["espetinho", "espeto", "churrasco", "churrasquinho"],
    "tapiocaria": ["tapioca", "crepioca"],
    "hot dog": ["hot dog", "cachorro quente", "dog gourmet"],
    "creperia": ["crepe", "crêpe"],
    "sorveteria": ["sorvete", "gelato", "sundae", "picolé"],
    "cafeteria": ["café", "espresso", "latte", "cappuccino", "mocha"],
    "comida japonesa": ["yakisoba", "gyoza", "ramen", "tempurá", "katsu"],
    "churrascaria": ["picanha", "costela", "alcatra", "rodízio", "maminha"],
    "comida árabe": [
        "esfiha",
        "kibe",
        "falafel",
        "shawarma",
        "homus",
        "esfirra",
        "árabe",
    ],
    "doceria": ["brigadeiro", "trufa", "doce", "brownie", "torta", "bolo"],
    "comida italiana": [
        "massa",
        "lasanha",
        "ravioli",
        "gnocchi",
        "risoto",
        "spaghetti",
    ],
    "comida mexicana": ["burrito", "taco", "nachos", "quesadilla", "guacamole"],
}


def detect_category(products: list) -> str:
    text = " ".join(
        f"{p.get('name', '')} {p.get('category', '')} {p.get('description', '')}"
        for p in products
    ).lower()

    scores = {}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        scores[cat] = sum(1 for kw in keywords if kw in text)

    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "outros"


def delete_bot(bot_id: int):
    sql = f"""DO $$ DECLARE _bid int := {bot_id}; _cids int[]; _carts int[];
    BEGIN
      SELECT ARRAY(SELECT id FROM contact WHERE bot_id=_bid) INTO _cids;
      SELECT ARRAY(SELECT id FROM shoppingcart WHERE contact_id=ANY(_cids)) INTO _carts;
      DELETE FROM cartitem WHERE cart_id=ANY(_carts);
      DELETE FROM shoppingcart WHERE id=ANY(_carts);
      DELETE FROM conversationhistory WHERE bot_id=_bid;
      DELETE FROM contact WHERE id=ANY(_cids);
      DELETE FROM product WHERE bot_id=_bid;
      DELETE FROM subscription WHERE bot_id=_bid;
      DELETE FROM bot WHERE id=_bid;
    END $$;"""
    subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "postgres",
            "-d",
            "botbuilder",
            "-c",
            sql,
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )


async def validate_all():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    unvalidated = [e for e in manifest if not e.get("validated")]

    if not unvalidated:
        print("All images already validated!")
        return

    print(f"Validating {len(unvalidated)} images via Cadastro Mágico...")
    print()

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=120) as client:
        r = await client.post(
            "/auth/token", data={"username": EMAIL, "password": PASSWORD}
        )
        if r.status_code != 200:
            print(f"Login failed: {r.status_code}")
            return
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        validated = 0
        rejected = 0

        for entry in unvalidated:
            image_path = CORPUS / entry["file"]
            if not image_path.exists():
                print(f"  SKIP {entry['id']}: file missing")
                continue

            print(f"  {entry['id']}...", end=" ", flush=True)

            bot_resp = await client.post(
                "/bots",
                json={
                    "restaurant_name": f"Validate {entry['id']}",
                    "phone_number_id": f"corpus-build-{entry['id']}",
                    "whatsapp_token": "fake-token",
                    "whatsapp_number": f"5511val{int(time.time()) % 99999:05d}",
                },
                headers=headers,
            )

            if bot_resp.status_code != 201:
                print(f"FAIL (bot: {bot_resp.status_code})")
                continue

            bot_id = bot_resp.json()["id"]

            try:
                ext = image_path.suffix
                mime = "image/jpeg" if ext in (".jpg", ".jpeg") else f"image/{ext[1:]}"

                with open(image_path, "rb") as f:
                    upload_resp = await client.post(
                        f"/bots/{bot_id}/catalog/upload-from-file",
                        files={"file": (f"menu{ext}", f, mime)},
                        headers=headers,
                        timeout=120,
                    )

                if upload_resp.status_code != 201:
                    print(f"FAIL (extraction: {upload_resp.status_code})")
                    entry["validated"] = False
                    rejected += 1
                    rej_path = REJECTED / image_path.name
                    if image_path.exists():
                        image_path.rename(rej_path)
                    entry["file"] = f"rejected/{image_path.name}"
                    continue

                prod_resp = await client.get(
                    f"/bots/{bot_id}/products?limit=200", headers=headers
                )
                products = prod_resp.json() if prod_resp.status_code == 200 else []

                if len(products) >= 5:
                    category = detect_category(products)
                    entry["validated"] = True
                    entry["product_count"] = len(products)
                    entry["category"] = category

                    extraction_file = EXTRACTIONS / f"{entry['id']}.json"
                    extraction_file.write_text(
                        json.dumps(
                            {
                                "products": products,
                                "category": category,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )

                    validated += 1
                    print(f"OK -- {len(products)} products -- {category}")
                else:
                    entry["validated"] = False
                    rejected += 1
                    rej_path = REJECTED / image_path.name
                    if image_path.exists():
                        image_path.rename(rej_path)
                    entry["file"] = f"rejected/{image_path.name}"
                    print(f"REJECTED -- only {len(products)} products")

            finally:
                delete_bot(bot_id)

        MANIFEST.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        print(f"\nDone! Validated: {validated}, Rejected: {rejected}")
        total_valid = sum(1 for e in manifest if e.get("validated"))
        print(f"Corpus: {total_valid} validated images")

        cats = {}
        for e in manifest:
            if e.get("validated"):
                c = e.get("category", "unknown")
                cats[c] = cats.get(c, 0) + 1
        print("\nCategories:")
        for c, n in sorted(cats.items(), key=lambda x: -x[1]):
            print(f"  {c}: {n}")


if __name__ == "__main__":
    asyncio.run(validate_all())
