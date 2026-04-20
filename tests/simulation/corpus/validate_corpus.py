"""
Validate corpus images via Cadastro Mágico.

Uploads each unvalidated image to the API, extracts products,
auto-detects category, saves extraction cache.

Run: python tests/simulation/corpus/validate_corpus.py
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

# Ensure app module is importable when running as a script inside Docker
_project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

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


async def delete_bot(bot_id: int):
    import shutil

    if shutil.which("docker"):
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
    else:
        from sqlalchemy import text as sa_text
        from app.database import async_session

        async with async_session() as session:
            r = await session.execute(
                sa_text(f"SELECT id FROM contact WHERE bot_id = {bot_id}")
            )
            cids = [row[0] for row in r.fetchall()]
            if cids:
                cids_str = ",".join(str(x) for x in cids)
                r2 = await session.execute(
                    sa_text(
                        f"SELECT id FROM shoppingcart WHERE contact_id IN ({cids_str})"
                    )
                )
                cart_ids = [row[0] for row in r2.fetchall()]
                if cart_ids:
                    carts_str = ",".join(str(x) for x in cart_ids)
                    await session.execute(
                        sa_text(f"DELETE FROM cartitem WHERE cart_id IN ({carts_str})")
                    )
                    await session.execute(
                        sa_text(f"DELETE FROM shoppingcart WHERE id IN ({carts_str})")
                    )
                await session.execute(
                    sa_text(
                        f"DELETE FROM conversationhistory WHERE contact_id IN ({cids_str})"
                    )
                )
                await session.execute(
                    sa_text(f"DELETE FROM contact WHERE id IN ({cids_str})")
                )
            await session.execute(
                sa_text(f"DELETE FROM conversationhistory WHERE bot_id = {bot_id}")
            )
            await session.execute(
                sa_text(f"DELETE FROM product WHERE bot_id = {bot_id}")
            )
            await session.execute(
                sa_text(f"DELETE FROM subscription WHERE bot_id = {bot_id}")
            )
            await session.execute(sa_text(f"DELETE FROM bot WHERE id = {bot_id}"))
            await session.commit()


def _get_cli_args():
    import argparse

    parser = argparse.ArgumentParser(
        description="Validate corpus images via Cadastro Mágico."
    )
    parser.add_argument(
        "--entry",
        type=str,
        default=None,
        help="Validate a single entry by ID (e.g., menu_0045 or prospect_0001)",
    )
    args, _ = parser.parse_known_args()
    return args


async def validate_all():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    # An image needs validation if either:
    #   - validated is None (clean "never tried" marker), OR
    #   - validated is False AND the file is still in images/ (new entries
    #     from the classifier game which historically wrote False).
    # Entries with validated == True are done. Entries with validated == False
    # AND file in rejected/ have already been rejected and should not retry.
    unvalidated = [
        e
        for e in manifest
        if e.get("validated") is not True
        and not e.get("file", "").startswith("rejected/")
    ]

    # --entry flag: process a single entry instead of all unvalidated
    args = _get_cli_args()
    if args.entry:
        unvalidated = [e for e in unvalidated if e["id"] == args.entry]
        if not unvalidated:
            # Also check if the entry exists but is already validated
            exists = any(e["id"] == args.entry for e in manifest)
            if exists:
                print(f"Entry {args.entry} is already validated.")
            else:
                print(f"Entry {args.entry} not found in manifest.")
            return

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

                try:
                    with open(image_path, "rb") as f:
                        upload_resp = await client.post(
                            f"/bots/{bot_id}/catalog/upload-from-file",
                            files=[("files", (f"menu{ext}", f, mime))],
                            headers=headers,
                            timeout=240,
                        )
                except httpx.ReadTimeout:
                    print("TIMEOUT (extraction >240s) — leaving as unvalidated")
                    # Don't mark as rejected — let it retry in a future run
                    continue
                except httpx.HTTPError as http_err:
                    print(f"HTTP ERROR ({type(http_err).__name__})")
                    continue

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

                if len(products) >= 8:
                    # Preserve pre-set category (from scrape-time search query) —
                    # auto-detection is unreliable for mixed menus (a pizzaria
                    # with Arabic sides gets flagged "comida árabe", etc.).
                    # Only infer if no category was set yet.
                    if entry.get("category"):
                        category = entry["category"]
                    else:
                        category = detect_category(products)
                        entry["category"] = category
                    entry["validated"] = True
                    entry["product_count"] = len(products)

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
                await delete_bot(bot_id)
                # Save after every entry — crash/timeout preserves progress
                MANIFEST.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
                )

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
