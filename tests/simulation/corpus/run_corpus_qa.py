"""
Run QA tests against validated corpus images.

Picks 5 random images from different categories, creates bots with
cached product data, runs simulation tests, cleans up.

Run: python tests/simulation/corpus/run_corpus_qa.py
     python tests/simulation/corpus/run_corpus_qa.py --samples=3
     CORPUS_SAMPLES=3 python tests/simulation/corpus/run_corpus_qa.py

The --samples flag (Pillar 1, F8) re-runs pytest N times against the same
test data, then aggregates per-restaurant per-scenario results into one
report. Each pytest run uses fresh phone numbers, so samples are independent
attempts that capture LLM stochasticity. Default N=1 preserves the
historical single-attempt behavior. CORPUS_SAMPLES env var is respected
when no CLI flag is given (the frontend endpoint reads it from .env).
"""

import argparse
import asyncio
import gc
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Max retries for HTTP calls to the backend. The subprocess survives
# uvicorn --reload but the server is briefly unavailable during restarts
# (~10-20s for embedding model warm-up). Without retries, a single
# ReadTimeout during bot setup crashes the entire run with no report.
_HTTP_MAX_RETRIES = 3
_HTTP_RETRY_DELAY = 15  # seconds — enough for uvicorn to finish reloading


async def _http_with_retry(client, method, url, **kwargs):
    """HTTP request with retry on transient errors (ReadTimeout, ConnectError).

    Used during the bot setup phase where the backend might be restarting
    due to uvicorn --reload. The test execution phase (subprocess.run pytest)
    doesn't need this because pytest connects to the DB directly.
    """
    last_exc = None
    for attempt in range(1, _HTTP_MAX_RETRIES + 1):
        try:
            return await getattr(client, method)(url, **kwargs)
        except (
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
        ) as exc:
            last_exc = exc
            if attempt < _HTTP_MAX_RETRIES:
                print(
                    f"\n  [retry {attempt}/{_HTTP_MAX_RETRIES}] "
                    f"{type(exc).__name__} on {method.upper()} {url} "
                    f"— waiting {_HTTP_RETRY_DELAY}s for server restart...",
                    flush=True,
                )
                await asyncio.sleep(_HTTP_RETRY_DELAY)
    raise last_exc


# Ensure app module is importable when running as a script inside Docker
_project_root = Path(__file__).resolve().parent.parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

CORPUS = Path(__file__).parent
PROJECT_ROOT = CORPUS.parent.parent.parent
MANIFEST = CORPUS / "manifest.json"

BASE_URL = "http://localhost:8000"
EMAIL = "test1@exemplo.com"
PASSWORD = "Test123$"

# Import phrase bank — handle both module and script execution
try:
    from tests.simulation.corpus.phrase_bank import (
        DRINK_CATEGORIES,
        format_add,
        format_remove,
        format_suggestion,
        format_greeting,
        format_question,
        generate_abbreviations,
        pick_random_products,
    )
except ModuleNotFoundError:
    from phrase_bank import (
        DRINK_CATEGORIES,
        format_add,
        format_remove,
        format_suggestion,
        format_greeting,
        format_question,
        generate_abbreviations,
        pick_random_products,
    )


def load_extraction(image_id):
    f = CORPUS / "extractions" / f"{image_id}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return None


def gen_scenarios(products, unavail_indices, seed: str | int | None = None):
    """Generate test scenarios for a restaurant.

    When `seed` is provided, product selection is deterministic — the same
    restaurant always produces the same scenarios, regardless of run order or
    Python session. This makes baseline comparisons meaningful: a change in
    test infrastructure (e.g., smarter abbreviation filter) produces clean
    before/after deltas instead of noise from different product picks.

    Without a seed, behavior is fully random (legacy).
    """
    if seed is not None:
        # Use a dedicated Random instance so we don't pollute global random state
        # and so changes here don't affect other modules relying on random.
        import hashlib as _hashlib

        if isinstance(seed, str):
            seed = int(_hashlib.md5(seed.encode()).hexdigest()[:8], 16)
        random.seed(seed)

    # A3 (2026-04-15): filter out products with non-verbalizable names
    # (3+ commas, parenthetical variants, very long, digit-prefixed).
    # Real customers wouldn't order "quero um Carne, Queijo, Bacon e Catupiry"
    # verbatim — they'd say "o de carne" or point at a picture.
    try:
        from phrase_bank import _is_verbalizable
    except ImportError:
        from .phrase_bank import _is_verbalizable

    available = [
        p
        for i, p in enumerate(products)
        if i not in unavail_indices and _is_verbalizable(p)
    ]
    if len(available) < 6:
        # Fallback: allow less-verbalizable products if the menu has too few
        # clean ones (niche menus might not have any simple names)
        available = [p for i, p in enumerate(products) if i not in unavail_indices]
        if len(available) < 6:
            return None

    picks = pick_random_products(products, set(unavail_indices))

    single = picks["single"]
    multi_1 = picks["multi_1"]
    multi_2 = picks["multi_2"]
    remove_item = picks["remove"]
    checkout_item = picks["checkout"]
    question_item = picks["question"]
    double_add_item = picks["double_add"]
    unavail_prod = picks["unavail"]
    avail_with = picks["avail_with_unavail"]

    sc = {
        # --- Original 7 scenarios (now with varied phrasing) ---
        "add_single_msg": format_add(single["name"], qty=1),
        "add_single_expected": [(single["name"], 1)],
        "add_multi_msg": f"{format_add(multi_1['name'], qty=2)} e {format_add(multi_2['name'], qty=1)}",
        "add_multi_expected": [(multi_1["name"], 2), (multi_2["name"], 1)],
        "remove_add_msg": format_add(remove_item["name"], qty=2),
        "remove_add_expected": (remove_item["name"], 2),
        "remove_msg": format_remove(remove_item["name"]),
        "suggestion_msg": format_suggestion(),
        "checkout_product_msg": format_add(checkout_item["name"], qty=1),
        "checkout_product_expected": (checkout_item["name"], 1),
        # --- New scenarios ---
        # 8. Abbreviation test: order by abbreviated name
        "abbreviation_product": None,
        "abbreviation_msg": None,
        # 9. Double add: same product twice → qty should increase
        "double_add_product": double_add_item["name"],
        "double_add_msg_1": format_add(double_add_item["name"], qty=1),
        "double_add_msg_2": format_add(double_add_item["name"], qty=1),
        # 10. Question mid-order: should NOT add to cart
        "question_product": question_item["name"],
        "question_msg": format_question(question_item["name"]),
        # 11. Add then remove: cart should be empty after
        "add_remove_product": single["name"],
        "add_remove_add_msg": format_add(single["name"], qty=1),
        "add_remove_remove_msg": format_remove(single["name"]),
        # 12. Greeting (varied)
        "greeting_msg": format_greeting(),
        # 13. (S8) Continuation: after adding `single`, send a verb-less
        # follow-up like "e uma {multi_2}". Tests whether the bot handles
        # the most common production pattern: customer adds an item, then
        # tags on more without repeating "quero".
        "continuation_first_msg": format_add(single["name"], qty=1),
        "continuation_first_product": single["name"],
        "continuation_followup_msg": f"e uma {multi_2['name'].lower()}",
        "continuation_followup_product": multi_2["name"],
        # --- P1 Day 1.5 scenarios ---
        # 14. Suggestion-trap → QUESTION (price ask while suggestions are active)
        # Reuses the existing question_msg/question_product pair.
        "trap_question_msg": format_question(question_item["name"]),
        "trap_question_product": question_item["name"],
        # 15. Suggestion-trap → unrelated ADD
        # The drink (multi_2) is intentionally chosen here because the
        # suggestion handler filters drinks out of its candidate list, so
        # the unrelated_add product is guaranteed not to be in the bot's
        # numbered suggestion response. That makes the test isolate
        # "did the bot correctly route an explicit ADD instead of treating
        # the message as a numbered selection".
        "trap_unrelated_add_msg": f"vou querer um {multi_2['name'].lower()}",
        "trap_unrelated_add_product": multi_2["name"],
        # 16. Subset-remove (uses add_multi_msg as setup)
        # Removes multi_2 (the second item in add_multi), keeps multi_1 at qty 2.
        "subset_remove_msg": format_remove(multi_2["name"]),
        "subset_remove_target_product": multi_2["name"],
        "subset_remove_keeper": (multi_1["name"], 2),
        # 17. Multi-target remove in one message
        # Setup adds 3 distinct items: multi_1 + multi_2 + remove_item.
        # Then removes multi_1 + remove_item, keeps multi_2.
        "multi_remove_setup_msg": (
            f"quero um {multi_1['name'].lower()}, "
            f"um {multi_2['name'].lower()} "
            f"e um {remove_item['name'].lower()}"
        ),
        "multi_remove_msg": (
            f"tira o {multi_1['name'].lower()} e o {remove_item['name'].lower()}"
        ),
        "multi_remove_targets": [multi_1["name"], remove_item["name"]],
        "multi_remove_keeper": multi_2["name"],
        # 18. Quantity reduction (NOT a full removal)
        # Sets up X×3 + Y×2, then asks to reduce X to 1. Y should stay at 2.
        # Expected to fail today — surfaces a tool-shape gap, not a regex gap.
        "qty_reduce_setup_msg": (
            f"quero 3 {multi_1['name'].lower()} e 2 {multi_2['name'].lower()}"
        ),
        "qty_reduce_msg": f"deixa só 1 {multi_1['name'].lower()}",
        "qty_reduce_expected": [(multi_1["name"], 1), (multi_2["name"], 2)],
        # 19. Multi-turn flow (S11) — composite, no new fields needed; reuses
        # add_single_msg + suggestion_msg + continuation_followup_msg + question_msg.
    }

    # 20. Number-in-product-name extractor regression — only populated when
    # the bot's menu actually contains a product whose name has a digit
    # ("Casquinha 3 bolas", "Pizza 4 queijos", "Coca 2L", etc). When no such
    # product exists, the test skips. Drinks are excluded because "Coca 2L"
    # has a digit but isn't subject to the bug pattern (size, not quantity).
    # A2 (2026-04-15): pick only products where the digit has CLEAR semantic
    # meaning (size, piece count, variant version). Plain standalone numbers
    # like "Combo 2" are genuinely ambiguous with quantity — not a bot bug,
    # just an impossible test.
    #
    # Accepted patterns:
    #   - "N queijos", "N sabores", "N peças", "N pçs", "N unidades", "N un"
    #   - "Pizza 4 Queijos", "Combinado 8 peças"
    #   - "X-Burger 2.0" (version number)
    #   - Sizes: "N ml", "N l", "N g", "N kg" (though these are drinks usually)
    _CLEAR_DIGIT_RE = re.compile(
        r"\b\d+[.,]?\d*\s*(queijos?|sabores?|peças?|pçs?|pecas?|unidades?|un|fatias)\b|"
        r"\b\d+\.\d+\b",
        re.IGNORECASE,
    )

    digit_product = None
    for p in available:
        name = p.get("name", "")
        category = p.get("category", "").lower()
        if category in DRINK_CATEGORIES:
            continue
        if _CLEAR_DIGIT_RE.search(name):
            digit_product = p
            break
    if digit_product:
        sc["digit_in_name_product"] = digit_product["name"]
        sc["digit_in_name_msg"] = f"quero um {digit_product['name'].lower()}"
    else:
        sc["digit_in_name_product"] = None
        sc["digit_in_name_msg"] = None

    # Generate abbreviation for a suitable product.
    # Pass all product names so generate_abbreviations() can filter out
    # ambiguous abbreviations (ones matching 2+ products in this menu).
    # An abbreviation that matches multiple products is unsolvable by
    # construction — a real customer would also need clarification.
    all_product_names = [prd.get("name", "") for prd in products]
    sc["abbreviation_product"] = None
    sc["abbreviation_msg"] = None
    for p in available:
        abbrevs = generate_abbreviations(p["name"], all_product_names=all_product_names)
        if abbrevs:
            chosen_abbrev = random.choice(abbrevs)
            sc["abbreviation_product"] = p["name"]
            sc["abbreviation_msg"] = format_add(chosen_abbrev, qty=1)
            break

    # Unavailable scenario
    if unavail_prod and avail_with:
        sc["unavailable_msg"] = (
            f"{format_add(unavail_prod['name'], qty=1)} "
            f"e {format_add(avail_with['name'], qty=1)}"
        )
        sc["unavailable_available"] = avail_with["name"]
        sc["unavailable_name"] = unavail_prod["name"]
    else:
        sc["unavailable_msg"] = None

    return sc


async def run_sql(sql):
    """Execute SQL — tries docker psql first, falls back to app DB engine."""
    import shutil

    if shutil.which("docker"):
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
        await _run_sql_direct(sql)


async def _run_sql_direct(sql):
    """Execute SQL via app DB engine (for running inside Docker)."""
    from sqlalchemy import text as sa_text
    from app.database import async_session

    async with async_session() as session:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt and not stmt.startswith("--"):
                await session.execute(sa_text(stmt))
        await session.commit()


async def _run_cleanup_direct(bot_ids: list[int]):
    """Clean up bots via app DB engine using simple DELETEs (no PL/pgSQL)."""
    from sqlalchemy import text as sa_text
    from app.database import async_session

    if not bot_ids:
        return

    ids_str = ",".join(str(x) for x in bot_ids)

    async with async_session() as session:
        r = await session.execute(
            sa_text(f"SELECT id FROM contact WHERE bot_id IN ({ids_str})")
        )
        contact_ids = [row[0] for row in r.fetchall()]

        if contact_ids:
            cids = ",".join(str(x) for x in contact_ids)
            r2 = await session.execute(
                sa_text(f"SELECT id FROM shoppingcart WHERE contact_id IN ({cids})")
            )
            cart_ids = [row[0] for row in r2.fetchall()]

            if cart_ids:
                cartids = ",".join(str(x) for x in cart_ids)
                await session.execute(
                    sa_text(f"DELETE FROM cartitem WHERE cart_id IN ({cartids})")
                )
                await session.execute(
                    sa_text(f"DELETE FROM shoppingcart WHERE id IN ({cartids})")
                )

            await session.execute(
                sa_text(
                    f'DELETE FROM orderitem WHERE order_id IN (SELECT id FROM "order" WHERE contact_id IN ({cids}))'
                )
            )
            await session.execute(
                sa_text(f'DELETE FROM "order" WHERE contact_id IN ({cids})')
            )
            # Delete conversation history by contact_id BEFORE deleting contacts
            await session.execute(
                sa_text(f"DELETE FROM conversationhistory WHERE contact_id IN ({cids})")
            )
            await session.execute(sa_text(f"DELETE FROM contact WHERE id IN ({cids})"))

        # Also clean any orphaned conversation history by bot_id
        await session.execute(
            sa_text(f"DELETE FROM conversationhistory WHERE bot_id IN ({ids_str})")
        )
        await session.execute(
            sa_text(f"DELETE FROM product WHERE bot_id IN ({ids_str})")
        )
        await session.execute(
            sa_text(f"DELETE FROM subscription WHERE bot_id IN ({ids_str})")
        )
        await session.execute(sa_text(f"DELETE FROM bot WHERE id IN ({ids_str})"))
        await session.commit()


async def pre_cleanup():
    """Delete leftover test bots from prior runs.

    SAFETY (added 2026-04-08, Pillar 1): only deletes bots older than 15
    minutes. An in-progress concurrent run keeps its bots safe — this lets
    a user accidentally double-click "Run QA Tests" without the second
    invocation kneecapping the first one's pytest sample loop. Bots that
    legitimately leaked from a crashed run (>15 min old) are still cleaned.
    """
    import shutil

    if shutil.which("docker"):
        await run_sql("""DO $$ DECLARE _bids int[]; _cids int[]; _carts int[];
        BEGIN
          SELECT ARRAY(SELECT id FROM bot WHERE (phone_number_id LIKE 'rand-test-%'
            OR phone_number_id LIKE 'corpus-build-%') AND id != 74
            AND created_at < NOW() - INTERVAL '15 minutes') INTO _bids;
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
    else:
        from sqlalchemy import text as sa_text
        from app.database import async_session

        async with async_session() as session:
            r = await session.execute(
                sa_text(
                    "SELECT id FROM bot WHERE (phone_number_id LIKE 'rand-test-%' "
                    "OR phone_number_id LIKE 'corpus-build-%') AND id != 74 "
                    "AND created_at < NOW() - INTERVAL '15 minutes'"
                )
            )
            orphan_ids = [row[0] for row in r.fetchall()]

        if orphan_ids:
            await _run_cleanup_direct(orphan_ids)


async def cleanup_bots(bot_ids):
    if not bot_ids:
        return
    import shutil

    if shutil.which("docker"):
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
    else:
        await _run_cleanup_direct(bot_ids)


async def main():
    # Phase 1A — single-run lock. Two modes:
    # 1. CLI invocation: acquire our own lock, release when done.
    # 2. API-spawned (CORPUS_LOCK_OWNED=1): the endpoint already acquired
    #    the lock. We adopt the owner_id from the env and release it when
    #    done. The endpoint returns 202 immediately and does NOT release —
    #    the subprocess is the sole owner of the lock lifecycle.
    from app import qa_lock as _qa_lock

    _api_owned_lock = os.environ.get("CORPUS_LOCK_OWNED") == "1"
    _lock_owner_id: str | None = None

    if _api_owned_lock:
        # Adopt the lock the API endpoint created for us.
        _lock_owner_id = os.environ.get("CORPUS_LOCK_OWNER_ID")
        if _lock_owner_id:
            print(f"[QA_LOCK] Adopted API lock owner={_lock_owner_id}")
        else:
            print("WARNING: CORPUS_LOCK_OWNED=1 but no CORPUS_LOCK_OWNER_ID set")
    else:
        _acquired, _holder = await _qa_lock.try_acquire()
        if not _acquired:
            if _holder is None:
                print("ERROR: QA lock unavailable (Redis unreachable). Aborting.")
            else:
                from datetime import datetime as _dt

                _started = _dt.fromtimestamp(_holder.get("started_at", 0))
                print(
                    f"ERROR: A QA run is already in progress "
                    f"(owner={_holder.get('owner_id')}, started {_started.isoformat()}). "
                    f"Aborting."
                )
            return
        _lock_owner_id = _holder["owner_id"]
        print(f"[QA_LOCK] Acquired direct-CLI lock owner={_lock_owner_id}")

    try:
        await _main_locked()
    finally:
        if _lock_owner_id is not None:
            await _qa_lock.release(owner_id=_lock_owner_id)
            print(f"[QA_LOCK] Released lock owner={_lock_owner_id}")


async def _main_locked():
    from collections import Counter

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    validated = [e for e in manifest if e.get("validated")]

    if len(validated) < 5:
        print(f"Only {len(validated)} validated images. Need at least 5.")
        return

    # --- Honesty check: report what tiers exist in the corpus ---
    # P0 tagged 124 entries as menu_type="template" (synthetic-looking
    # cardapio screenshots from Google Image Search). Real-world readiness
    # requires Tier 2 menus (iFood / Rappi / Google Maps screenshots) tagged
    # menu_type="tier2". Until those exist in the corpus, runs against this
    # set OVERSTATE production-readiness — log it loudly so the metric
    # cannot quietly inflate again.
    tier_counts = Counter(e.get("menu_type", "unknown") for e in validated)
    print(f"Validated corpus by menu_type: {dict(tier_counts)}")
    if tier_counts.get("template", 0) == len(validated):
        print()
        print("=" * 60)
        print("WARNING: 100% of validated corpus entries are 'template'.")
        print("This run measures against synthetic-looking menus, NOT real")
        print("iFood / Rappi / Google Maps screenshots. Headline numbers")
        print("OVERSTATE real-world readiness. Collect Tier 2 menus via the")
        print("corpus game and tag them menu_type='tier2' to fix the metric.")
        print("=" * 60)
        print()

    # Phase 1C — restaurant selection mode. 'baseline' uses the locked
    # 10-restaurant pool from baseline_pool.json (clean per-fix attribution).
    # 'random' is the historical behavior (5 random validated entries).
    pool_mode = _get_pool()
    selected: list[dict] = []

    if pool_mode == "baseline":
        pool_file = CORPUS / "baseline_pool.json"
        if not pool_file.exists():
            print(
                f"ERROR: pool=baseline requested but {pool_file} does not exist. "
                f"Run `python tests/simulation/corpus/pick_baseline_pool.py` to generate it."
            )
            return
        pool_data = json.loads(pool_file.read_text(encoding="utf-8"))
        # Match pool IDs against validated manifest entries; preserve pool
        # ordering so the same restaurants test in the same order each run.
        validated_by_id = {e["id"]: e for e in validated}
        for pool_entry in pool_data.get("entries", []):
            entry = validated_by_id.get(pool_entry["id"])
            if entry is not None:
                selected.append(entry)
            else:
                print(
                    f"  WARNING: pool entry {pool_entry['id']} not in validated "
                    f"manifest — was it removed? Skipping."
                )
        if not selected:
            print(
                "ERROR: no pool entries matched validated manifest. "
                "Re-run pick_baseline_pool.py to refresh the pool."
            )
            return
    elif pool_mode == "prospect":
        pool_file = CORPUS / "prospect_pool.json"
        if not pool_file.exists():
            print(
                "ERROR: pool=prospect requested but prospect_pool.json does not exist."
            )
            return
        pool_data = json.loads(pool_file.read_text(encoding="utf-8"))
        for pool_entry in pool_data.get("entries", []):
            if pool_entry.get("status") not in ("extracted", "demo_active"):
                continue
            extraction_file = CORPUS / pool_entry.get("extraction_file", "")
            if not extraction_file.exists():
                print(
                    f"  WARNING: extraction missing for {pool_entry['id']} — skipping."
                )
                continue
            # Build a manifest-compatible entry for the runner
            selected.append(
                {
                    "id": pool_entry["id"],
                    "category": pool_entry.get("category", "outros"),
                    "restaurant_name": pool_entry.get(
                        "restaurant_name", pool_entry["id"]
                    ),
                    "product_count": pool_entry.get("product_count", 0),
                    "menu_type": "prospect",
                    # Carry demo_bot_id so the runner can reuse existing bots
                    "_demo_bot_id": pool_entry.get("demo_bot_id"),
                }
            )
        if not selected:
            print(
                "ERROR: no eligible prospect entries found (need status=extracted or demo_active)."
            )
            return
    else:
        # Random mode (historical): prefer non-template entries when picking
        # — fall back to templates only if there aren't enough Tier 2.
        non_template = [e for e in validated if e.get("menu_type") != "template"]
        template_only = [e for e in validated if e.get("menu_type") == "template"]
        random.shuffle(non_template)
        random.shuffle(template_only)
        pick_pool = non_template + template_only

        seen_cats = set()
        for e in pick_pool:
            cat = e.get("category", "outros")
            if cat not in seen_cats:
                selected.append(e)
                seen_cats.add(cat)
            if len(selected) >= 5:
                break
        while len(selected) < 5:
            for e in pick_pool:
                if e not in selected:
                    selected.append(e)
                    break

    print("=" * 60)
    print(f"CORPUS-BASED QA RUN  (pool={pool_mode}, n={len(selected)})")
    print("=" * 60)
    print()
    for s in selected:
        tier = s.get("menu_type", "unknown")
        print(
            f"  {s['id']} -- {s['category']} -- {s['product_count']} products -- {tier}"
        )
    print()

    print("Pre-run cleanup...")
    await pre_cleanup()

    bots = {}

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60) as client:
        r = await _http_with_retry(
            client,
            "post",
            "/auth/token",
            data={"username": EMAIL, "password": PASSWORD},
        )
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

            bot_resp = await _http_with_retry(
                client,
                "post",
                "/bots",
                json={
                    "restaurant_name": name,
                    "phone_number_id": phone_id,
                    "whatsapp_token": "fake-token",
                    "whatsapp_number": f"5511r{i}{int(time.time()) % 99999:05d}",
                },
                headers=headers,
            )

            if bot_resp.status_code != 201:
                print(f"FAIL ({bot_resp.status_code})")
                continue
            bot_id = bot_resp.json()["id"]

            prod_ids = []
            for p in products:
                pr = await _http_with_retry(
                    client,
                    "post",
                    f"/bots/{bot_id}/products",
                    json={
                        "name": p["name"],
                        "price": p["price"],
                        "category": p.get("category", "Geral"),
                        "description": p.get("description", ""),
                    },
                    headers=headers,
                )
                prod_ids.append(pr.json()["id"] if pr.status_code == 201 else None)

            non_drink = [
                j
                for j, p in enumerate(products)
                if "bebida" not in p.get("category", "").lower()
            ]
            unavail_idx = random.sample(non_drink[:10], min(2, len(non_drink)))
            for idx in unavail_idx:
                pid = prod_ids[idx]
                if pid:
                    await _http_with_retry(
                        client,
                        "put",
                        f"/bots/{bot_id}/products/{pid}",
                        json={"is_available": False},
                        headers=headers,
                    )

            # Seed with the entry ID so the same restaurant always produces the
            # same scenarios — enables clean A/B comparison of test infra changes.
            scenarios = gen_scenarios(products, unavail_idx, seed=entry["id"])
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
        f"'2027-04-02',NOW(),NOW(),'pro_monthly')"
        for b in bots.values()
    )
    await run_sql(
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
        json.dumps(test_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    samples = _get_samples()
    if samples > 1:
        print(
            f"\n{len(bots)} bots ready. Running tests {samples}x for "
            f"variance reduction (Pillar 1, F8)...\n"
        )
    else:
        print(f"\n{len(bots)} bots ready. Running tests...\n")

    started_at = datetime.now(timezone.utc).isoformat()

    import shutil

    if shutil.which("docker"):
        test_cmd = [
            "docker",
            "compose",
            "exec",
            "-T",
            "backend",
            "pytest",
            "tests/simulation/test_random_restaurants.py",
            "-v",
            "--tb=short",
        ]
    else:
        # Running inside Docker — call pytest directly
        test_cmd = [
            "pytest",
            "tests/simulation/test_random_restaurants.py",
            "-v",
            "--tb=short",
        ]

    # Pillar 1 (F8): loop pytest N times. Each invocation re-imports
    # _random_test_data.json (same product/message data) but every test uses
    # a fresh phone number, so each sample is an independent attempt against
    # the same scenarios. Captures LLM stochasticity, which is the dominant
    # within-run noise source. Phrase-variant noise is captured by running
    # multiple separate corpus runs (which the user does anyway: 3x baseline).
    #
    # Per-pytest timeout scales with restaurant count: each test takes ~1-3
    # seconds against gpt-4o-mini, and pytest runs them sequentially. With
    # 10 restaurants × 21 scenarios = 210 tests, the previous hardcoded 600s
    # ceiling was insufficient (2.85s/test budget) and the first sample
    # would silently time out, crashing the script before merge/upload.
    # Budget: ~6s/test × N tests + 60s overhead. For 5 restaurants → 690s,
    # for 10 → 1320s, for 30 → 3840s.
    n_tests_per_sample = len(bots) * len(_SCENARIO_MAP)
    pytest_timeout = max(600, n_tests_per_sample * 6 + 60)
    print(
        f"\nPer-pytest timeout: {pytest_timeout}s "
        f"({n_tests_per_sample} tests × 6s/test + 60s overhead)"
    )

    sample_reports: list[dict] = []
    for sample_idx in range(samples):
        if samples > 1:
            print(f"\n  --- Sample {sample_idx + 1}/{samples} ---")

        sample_started = datetime.now(timezone.utc).isoformat()
        try:
            result = subprocess.run(
                test_cmd,
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                timeout=pytest_timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            # Don't crash the whole run on a timeout — log it loudly and
            # synthesize an empty sample so the merge step still produces a
            # report (which Phase 1A integrity validation will then flag as
            # 'partial' so we know one sample was lost).
            print(
                f"\nERROR: pytest sample {sample_idx + 1}/{samples} timed out "
                f"after {pytest_timeout}s. Captured output (last 2000 chars):\n"
                f"{(exc.stdout or b'').decode('utf-8', errors='replace')[-2000:]}\n"
                f"---\n"
                f"{(exc.stderr or b'').decode('utf-8', errors='replace')[-2000:]}"
            )
            # Build an empty parsed report so the merge can still proceed
            sample_finished = datetime.now(timezone.utc).isoformat()
            sample_reports.append(
                _parse_results("", bots, sample_started, sample_finished)
            )
            gc.collect()
            continue
        print(result.stdout)
        if result.stderr:
            print(result.stderr)
        sample_finished = datetime.now(timezone.utc).isoformat()

        sample_reports.append(
            _parse_results(result.stdout, bots, sample_started, sample_finished)
        )

        # Phase 1A (OOM mitigation): force garbage collection between
        # samples so pytest's per-invocation memory churn (transformer
        # caches, httpx connection pools, sqlalchemy session leaks) doesn't
        # accumulate across the loop. Defense in depth alongside the
        # uvicorn --reload-exclude tweaks in docker-compose.yml.
        gc.collect()

    finished_at = datetime.now(timezone.utc).isoformat()

    # Merge per-sample reports into a single aggregated report.
    # Always go through the merge path so the integrity validation
    # (Phase 1A) runs uniformly for samples=1 and samples>1, and so the
    # frontend always receives the same per-cell shape (passed_count /
    # total_count / samples list).
    report = _merge_sample_reports(
        sample_reports, bots, started_at, finished_at, samples
    )

    # Honesty: record the menu_type distribution of the sampled corpus so the
    # frontend can flag a run as "100% template" instead of silently inflating.
    report["samples_by_tier"] = dict(
        Counter(e.get("menu_type", "unknown") for e in selected)
    )

    # Phase 1C: stamp the selection mode onto the report so the frontend
    # can distinguish baseline-pool runs (clean per-fix attribution) from
    # random-sample runs (broad-baseline checks).
    report["pool"] = pool_mode

    # Upload to S3
    _upload_report(report)

    # Cleanup
    print("\nCleaning up...")
    await cleanup_bots([b["bot_id"] for b in bots.values()])
    print("Done!")


# Scenarios that don't reflect comprehension difficulty:
#  - greeting: trivial intent ("oi") that always passes
#  - suggestions: tests the bot path that fires when comprehension fails
#  - checkout: tests state machine, not natural-language understanding
# These three account for 27% of the test count and inflate the headline
# pass_rate. The "comprehension_pass_rate" metric excludes them so the
# number reflects how well the bot actually understands customer messages.
_EASY_SCENARIOS = frozenset({"greeting", "suggestions", "checkout"})


# --- Test scenario names mapped from test class names ---
_SCENARIO_MAP = {
    "TestRandAddSingle": "add_single",
    "TestRandAddMulti": "add_multi",
    "TestRandContinuation": "continuation",
    "TestRandUnavailable": "unavailable",
    "TestRandRemove": "remove",
    "TestRandSuggestions": "suggestions",
    "TestRandCheckout": "checkout",
    "TestRandGreeting": "greeting",
    "TestRandAbbreviation": "abbreviation",
    "TestRandDoubleAdd": "double_add",
    "TestRandQuestion": "question",
    "TestRandAddRemove": "add_remove",
    # P1 Day 1.5 — added 2026-04-08
    "TestRandSuggestionTrapQuestion": "trap_question",
    "TestRandSuggestionTrapClear": "trap_clear",
    "TestRandSuggestionTrapUnrelatedAdd": "trap_unrelated_add",
    "TestRandSuggestionTrapFinish": "trap_finish",
    "TestRandRemoveSubsetByName": "subset_remove",
    "TestRandRemoveMultipleAtOnce": "multi_remove",
    "TestRandQtyReduction": "qty_reduction",
    "TestRandMultiTurnFlow": "multiturn_flow",
    "TestRandQuantityInProductName": "digit_in_name",
}


def _get_cli_args():
    """Parse the corpus runner CLI flags once and return the namespace.

    Centralized so adding flags doesn't require multiple ArgumentParser
    instances. Uses parse_known_args so the script doesn't fail if invoked
    with extra flags from the API endpoint or test harness.
    """
    parser = argparse.ArgumentParser(description="Run corpus QA tests")
    parser.add_argument(
        "--samples",
        type=int,
        default=None,
        help="Pytest invocations per run (default: 1, or CORPUS_SAMPLES env var)",
    )
    parser.add_argument(
        "--pool",
        type=str,
        default=None,
        choices=["random", "baseline", "prospect"],
        help=(
            "Restaurant selection mode (default: random, or CORPUS_POOL env var). "
            "random = pick 5 random validated restaurants; "
            "baseline = use the locked 10-restaurant pool from baseline_pool.json "
            "for clean per-fix attribution (Phase 1C); "
            "prospect = use prospect_pool.json entries for demo verification."
        ),
    )
    args, _ = parser.parse_known_args()
    return args


def _get_samples() -> int:
    """Return the per-restaurant sample count.

    Precedence: --samples CLI flag > CORPUS_SAMPLES env var > 1 (default).
    Used by Pillar 1 (F8) to reduce per-scenario stddev. Each sample re-runs
    pytest against the same test data — fresh phone numbers, fresh contacts,
    fresh LLM calls — so independent attempts capture LLM stochasticity.
    """
    args = _get_cli_args()
    if args.samples is not None:
        return max(1, args.samples)

    env_val = os.environ.get("CORPUS_SAMPLES", "").strip()
    if env_val.isdigit():
        return max(1, int(env_val))

    return 1


def _get_pool() -> str:
    """Return the restaurant selection mode: 'random', 'baseline', or 'prospect'.

    Precedence: --pool CLI flag > CORPUS_POOL env var > 'random' (default).
    Phase 1C: 'baseline' uses the locked 10-restaurant pool committed at
    ``tests/simulation/corpus/baseline_pool.json``. Same restaurants every
    run → clean per-fix attribution (no sampling lottery). 'random' is the
    historical behavior (pick 5 random validated entries) — used for the
    weekly broad-baseline check that catches population drift. 'prospect'
    uses entries from ``prospect_pool.json`` for pre-visit demo verification.
    """
    args = _get_cli_args()
    if args.pool is not None:
        return args.pool

    env_val = os.environ.get("CORPUS_POOL", "").strip().lower()
    if env_val in ("random", "baseline", "prospect"):
        return env_val

    return "random"


def _merge_sample_reports(
    sample_reports: list[dict],
    bots: dict,
    started_at: str,
    finished_at: str,
    samples: int,
) -> dict:
    """Merge N per-sample reports into one aggregated report.

    Aggregation rules:
    - Headline counts (passed/failed/skipped/comprehension_*) are SUMMED across
      samples. So a 3-run aggregate of 5 restaurants × 21 scenarios = 315
      attempts per sample → 945 total attempts after merge.
    - Per-restaurant per-scenario cells gain `passed_count` / `total_count` /
      `pass_rate` / `samples` (list of statuses), and the convenience fields
      `passed` / `status` are derived: `passed=True` iff ALL samples passed
      (conservative — frontend cells stay green only when stable).
    - `failure_summary` concatenates per-sample failures, capped at 3000 chars.
    """
    rests: dict[str, dict] = {}
    for b in bots.values():
        rests[b["name"]] = {
            "name": b["name"],
            "category": b.get("category", "outros"),
            "scenarios": {},
        }

    total_passed = 0
    total_failed = 0
    total_skipped = 0
    comp_passed = 0
    comp_failed = 0
    comp_skipped = 0
    failure_lines: list[str] = []

    for r in sample_reports:
        total_passed += r.get("passed", 0)
        total_failed += r.get("failed", 0)
        total_skipped += r.get("skipped", 0)
        comp_passed += r.get("comprehension_passed", 0)
        comp_failed += r.get("comprehension_failed", 0)
        comp_skipped += r.get("comprehension_skipped", 0)
        if r.get("failure_summary"):
            failure_lines.append(r["failure_summary"])

        for rest in r.get("restaurants", []):
            rname = rest["name"]
            if rname not in rests:
                rests[rname] = {
                    "name": rname,
                    "category": rest.get("category", "outros"),
                    "scenarios": {},
                }
            for scen, res in rest.get("scenarios", {}).items():
                cell = rests[rname]["scenarios"].setdefault(
                    scen,
                    {"passed_count": 0, "total_count": 0, "samples": []},
                )
                status = res.get("status", "unknown")
                cell["samples"].append(status)
                if status == "passed":
                    cell["passed_count"] += 1
                    cell["total_count"] += 1
                elif status == "failed":
                    cell["total_count"] += 1
                # 'skipped' is excluded from total — it never ran

    # Derive convenience fields per cell.
    for rdata in rests.values():
        for cell in rdata["scenarios"].values():
            if cell["total_count"] > 0:
                cell["pass_rate"] = round(cell["passed_count"] / cell["total_count"], 3)
                # Conservative: only green when ALL samples passed
                cell["passed"] = cell["passed_count"] == cell["total_count"]
                cell["status"] = "passed" if cell["passed"] else "failed"
                # Frontend display label: "3/3", "2/5", etc.
                cell["label"] = f"{cell['passed_count']}/{cell['total_count']}"
            else:
                cell["pass_rate"] = 0.0
                cell["passed"] = False
                cell["status"] = "skipped"
                cell["label"] = "skip"

    # --- Phase 1B per-restaurant comprehension stats ---
    # The launch goal is "the bot performs CONSISTENTLY across restaurants",
    # not "the average is high". A bot at 80% mean ± 25pp spread (some
    # restaurants at 95%, others at 50%) is fundamentally different from a
    # bot at 78% mean ± 4pp spread. The first is unshippable; the second is
    # launch-ready. Mean alone hides this. Track three numbers:
    #
    # - worst_restaurant_comp_rate: the lowest per-restaurant comprehension
    #   pass rate. The "no customer is left behind" metric. Restaurants with
    #   zero comprehension attempts (everything skipped) are excluded — they
    #   can't pin worst-case to 0% just because of test data quirks.
    # - worst_restaurant_name: which restaurant has the worst score, so we
    #   can investigate it directly.
    # - cross_restaurant_spread_pp: standard deviation across per-restaurant
    #   pass rates. Spread > 12pp means the bot's quality depends heavily on
    #   the menu it's running for — that's a product issue, not noise.
    per_restaurant_comp_rates: list[float] = []
    per_restaurant_names: list[str] = []
    for rdata in rests.values():
        rest_passed = 0
        rest_total = 0
        for scen, cell in rdata["scenarios"].items():
            if scen in _EASY_SCENARIOS:
                continue  # match the headline comprehension exclusion
            rest_passed += cell["passed_count"]
            rest_total += cell["total_count"]
        if rest_total > 0:
            per_restaurant_comp_rates.append(rest_passed / rest_total * 100)
            per_restaurant_names.append(rdata["name"])

    if per_restaurant_comp_rates:
        worst_idx = min(
            range(len(per_restaurant_comp_rates)),
            key=lambda i: per_restaurant_comp_rates[i],
        )
        worst_restaurant_comp_rate = round(per_restaurant_comp_rates[worst_idx], 1)
        worst_restaurant_name = per_restaurant_names[worst_idx]
        if len(per_restaurant_comp_rates) >= 2:
            import statistics as _stats

            cross_restaurant_spread_pp = round(
                _stats.stdev(per_restaurant_comp_rates), 1
            )
        else:
            cross_restaurant_spread_pp = 0.0
    else:
        worst_restaurant_comp_rate = 0.0
        worst_restaurant_name = None
        cross_restaurant_spread_pp = 0.0

    total = total_passed + total_failed + total_skipped
    comp_total = comp_passed + comp_failed + comp_skipped

    # --- Phase 1A integrity validation ---
    # Detect runs that produced fewer attempts than expected — usually a sign
    # of partial pytest output (OOM-killed mid-execution, container memory
    # pressure, broken bot creation, parser dropping lines because the test
    # data file was overwritten mid-run, etc.). Compare actual attempts to
    # the theoretical max:
    #   expected = samples × restaurants × scenarios
    # Several scenarios LEGITIMATELY skip on certain restaurants (e.g.
    # `digit_in_name` skips when the menu has no digit-in-name product).
    # We allow up to 15% legitimate-skip slack before flagging the run.
    n_restaurants = len(rests)
    n_scenarios = len(_SCENARIO_MAP)
    expected_attempts = samples * n_restaurants * n_scenarios
    integrity = "ok"
    integrity_warning: str | None = None
    if expected_attempts > 0:
        attempt_ratio = total / expected_attempts
        if attempt_ratio < 0.85:
            integrity = "partial"
            integrity_warning = (
                f"Only {total}/{expected_attempts} expected attempts captured "
                f"({attempt_ratio * 100:.0f}%). Run is likely corrupted by "
                f"partial pytest output, container OOM, or interrupted "
                f"execution. Headline numbers should NOT be trusted as a "
                f"baseline data point."
            )

    return {
        "run_type": "corpus",
        "started_at": started_at,
        "finished_at": finished_at,
        "samples_per_restaurant": samples,
        "total_tests": total,
        "expected_tests": expected_attempts,
        "passed": total_passed,
        "failed": total_failed,
        "skipped": total_skipped,
        "pass_rate": round(total_passed / total * 100, 1) if total else 0,
        "comprehension_total": comp_total,
        "comprehension_passed": comp_passed,
        "comprehension_failed": comp_failed,
        "comprehension_skipped": comp_skipped,
        "comprehension_pass_rate": (
            round(comp_passed / comp_total * 100, 1) if comp_total else 0
        ),
        # Phase 1B: per-restaurant consistency metrics
        "worst_restaurant_comp_rate": worst_restaurant_comp_rate,
        "worst_restaurant_name": worst_restaurant_name,
        "cross_restaurant_spread_pp": cross_restaurant_spread_pp,
        "integrity": integrity,
        "integrity_warning": integrity_warning,
        "restaurants": list(rests.values()),
        "failure_summary": (
            "\n".join(failure_lines)[-3000:] if failure_lines else None
        ),
    }


def _parse_results(output: str, bots: dict, started_at: str, finished_at: str) -> dict:
    """Parse pytest -v output into a structured report dict."""
    # Match lines like: test_file.py::TestRandAddSingle::test_add_single[Restaurant Name] PASSED
    pattern = re.compile(r"::(\w+)::\w+\[(.+?)\]\s+(PASSED|FAILED|SKIPPED)")

    # Build restaurant results with a lookup that handles pytest's
    # escaped unicode (e.g. \xe1 for á) vs the real UTF-8 name.
    restaurants: dict[str, dict] = {}
    _name_lookup: dict[str, str] = {}  # normalized → original name
    for label, b in bots.items():
        restaurants[b["name"]] = {
            "name": b["name"],
            "category": b["category"],
            "scenarios": {},
        }
        # pytest -v escapes non-ASCII as \xNN — decode to match
        _name_lookup[b["name"]] = b["name"]
        # Also store the ASCII-escaped version for matching
        try:
            ascii_name = b["name"].encode("unicode_escape").decode("ascii")
            _name_lookup[ascii_name] = b["name"]
        except Exception:
            pass

    def _resolve_name(raw: str) -> str | None:
        """Resolve a pytest output name to the original restaurant name."""
        if raw in _name_lookup:
            return _name_lookup[raw]
        # Try decoding pytest's \xNN escapes
        try:
            decoded = raw.encode("utf-8").decode("unicode_escape")
            if decoded in _name_lookup:
                return _name_lookup[decoded]
        except Exception:
            pass
        # Fallback: prefix match (pytest truncates long names at ~50 chars)
        for key, orig in _name_lookup.items():
            if len(raw) >= 30 and (
                raw.startswith(key[:30]) or key.startswith(raw[:30])
            ):
                return orig
        return None

    total_passed = 0
    total_failed = 0
    total_skipped = 0
    comp_passed = 0  # comprehension-only (excludes easy scenarios)
    comp_failed = 0
    comp_skipped = 0

    # Only parse lines before the FAILURES/warnings section —
    # the failure summary repeats test names and causes double-counting.
    _test_output = (
        output.split("= FAILURES =")[0] if "= FAILURES =" in output else output
    )
    _test_output = (
        _test_output.split("= warnings summary =")[0]
        if "= warnings summary =" in _test_output
        else _test_output
    )
    _test_output = (
        _test_output.split("= short test summary")[0]
        if "= short test summary" in _test_output
        else _test_output
    )

    for match in pattern.finditer(_test_output):
        test_class, restaurant_name, status = match.groups()
        scenario = _SCENARIO_MAP.get(test_class, test_class)

        resolved = _resolve_name(restaurant_name)
        if resolved and resolved in restaurants:
            passed = status == "PASSED"
            restaurants[resolved]["scenarios"][scenario] = {
                "passed": passed,
                "status": status.lower(),
                "label": "1/1" if passed else "0/1",
            }
            # Only count tests that belong to known restaurants
            # (avoids double-counting from failure summary section)
            is_comp = scenario not in _EASY_SCENARIOS
            if status == "PASSED":
                total_passed += 1
                if is_comp:
                    comp_passed += 1
            elif status == "FAILED":
                total_failed += 1
                if is_comp:
                    comp_failed += 1
            else:
                total_skipped += 1
                if is_comp:
                    comp_skipped += 1

    # Extract failure details (only FAILED lines, not warnings)
    failure_lines = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            failure_lines.append(stripped)

    total = total_passed + total_failed + total_skipped
    comp_total = comp_passed + comp_failed + comp_skipped
    return {
        "run_type": "corpus",
        "started_at": started_at,
        "finished_at": finished_at,
        "total_tests": total,
        "passed": total_passed,
        "failed": total_failed,
        "skipped": total_skipped,
        "pass_rate": round(total_passed / total * 100, 1) if total else 0,
        # Honest comprehension metric: excludes greeting/suggestions/checkout
        # which inflate the headline number with trivial passes.
        "comprehension_total": comp_total,
        "comprehension_passed": comp_passed,
        "comprehension_failed": comp_failed,
        "comprehension_skipped": comp_skipped,
        "comprehension_pass_rate": (
            round(comp_passed / comp_total * 100, 1) if comp_total else 0
        ),
        "restaurants": list(restaurants.values()),
        "failure_summary": "\n".join(failure_lines[-30:]) if failure_lines else None,
    }


def _upload_report(report: dict):
    """Upload report to S3 — direct import when running inside Docker, falls
    back to `docker compose exec` when running on the host. Either path that
    succeeds returns early; only a true failure falls through to local save.
    """
    import shutil

    try:
        if not shutil.which("docker"):
            # Inside Docker (e.g. invoked via the /admin/corpus/run-tests
            # endpoint as an in-container subprocess) — import and call directly.
            from app.qa_reports import upload_qa_report

            key = upload_qa_report(report)
            if key:
                print(f"\nReport uploaded to S3: {key}")
                return
            print("\nS3 upload returned None (bucket not configured?)")
        else:
            # Outside Docker — proxy through `docker compose exec backend`
            tmp_path = PROJECT_ROOT / ".qa_report_tmp.json"
            tmp_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            cmd = (
                "from app.qa_reports import upload_qa_report; "
                "import json; "
                "report = json.loads(open('/code/.qa_report_tmp.json', encoding='utf-8').read()); "
                "key = upload_qa_report(report); "
                "print(f'S3_KEY:{key}' if key else 'S3_FAIL')"
            )
            result = subprocess.run(
                ["docker", "compose", "exec", "-T", "backend", "python", "-c", cmd],
                capture_output=True,
                text=True,
                cwd=str(PROJECT_ROOT),
                timeout=30,
            )
            tmp_path.unlink(missing_ok=True)
            combined = result.stdout + result.stderr
            if "S3_KEY:" in combined:
                key = combined.split("S3_KEY:")[1].strip().split("\n")[0]
                print(f"\nReport uploaded to S3: {key}")
                return
            print(f"\nS3 upload output: {combined[:300]}")
    except Exception as e:
        print(f"\nS3 upload failed ({e})")

    # Fallback: save to local reports directory
    reports_dir = PROJECT_ROOT / ".claude" / "simulation-reports" / "runs"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = report["started_at"].replace(":", "-")
    path = reports_dir / f"corpus_{ts}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nReport saved locally: {path}")


if __name__ == "__main__":
    asyncio.run(main())
