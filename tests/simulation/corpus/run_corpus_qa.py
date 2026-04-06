"""
Run QA tests against validated corpus images.

Picks 5 random images from different categories, creates bots with
cached product data, runs simulation tests, cleans up.

Run: python tests/simulation/corpus/run_corpus_qa.py
"""

import asyncio
import json
import random
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

CORPUS = Path(__file__).parent
PROJECT_ROOT = CORPUS.parent.parent.parent
MANIFEST = CORPUS / "manifest.json"

BASE_URL = "http://localhost:8000"
EMAIL = "test1@exemplo.com"
PASSWORD = "Test123$"

# Import phrase bank — handle both module and script execution
try:
    from tests.simulation.corpus.phrase_bank import (
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


def gen_scenarios(products, unavail_indices):
    available = [p for i, p in enumerate(products) if i not in unavail_indices]
    if len(available) < 5:
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
    }

    # Generate abbreviation for a suitable product
    for p in available:
        abbrevs = generate_abbreviations(p["name"])
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


def run_sql(sql):
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
        r = await client.post(
            "/auth/token", data={"username": EMAIL, "password": PASSWORD}
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

            bot_resp = await client.post(
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
                pr = await client.post(
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
                    await client.put(
                        f"/bots/{bot_id}/products/{pid}",
                        json={"is_available": False},
                        headers=headers,
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
        json.dumps(test_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n{len(bots)} bots ready. Running tests...\n")

    started_at = datetime.now(timezone.utc).isoformat()

    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "backend",
            "pytest",
            "tests/simulation/test_random_restaurants.py",
            "-v",
            "--tb=short",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        timeout=300,
        encoding="utf-8",
        errors="replace",
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr)

    finished_at = datetime.now(timezone.utc).isoformat()

    # Parse pytest output into structured report
    report = _parse_results(result.stdout, bots, started_at, finished_at)

    # Upload to S3
    _upload_report(report)

    # Cleanup
    print("\nCleaning up...")
    cleanup_bots([b["bot_id"] for b in bots.values()])
    print("Done!")


# --- Test scenario names mapped from test class names ---
_SCENARIO_MAP = {
    "TestRandAddSingle": "add_single",
    "TestRandAddMulti": "add_multi",
    "TestRandUnavailable": "unavailable",
    "TestRandRemove": "remove",
    "TestRandSuggestions": "suggestions",
    "TestRandCheckout": "checkout",
    "TestRandGreeting": "greeting",
    "TestRandAbbreviation": "abbreviation",
    "TestRandDoubleAdd": "double_add",
    "TestRandQuestion": "question",
    "TestRandAddRemove": "add_remove",
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
            }
            # Only count tests that belong to known restaurants
            # (avoids double-counting from failure summary section)
            if status == "PASSED":
                total_passed += 1
            elif status == "FAILED":
                total_failed += 1
            else:
                total_skipped += 1

    # Extract failure details
    failure_lines = []
    in_failures = False
    for line in output.splitlines():
        if "FAILURES" in line or "short test summary" in line:
            in_failures = True
        if in_failures:
            failure_lines.append(line)

    total = total_passed + total_failed + total_skipped
    return {
        "run_type": "corpus",
        "started_at": started_at,
        "finished_at": finished_at,
        "total_tests": total,
        "passed": total_passed,
        "failed": total_failed,
        "skipped": total_skipped,
        "pass_rate": round(total_passed / total * 100, 1) if total else 0,
        "restaurants": list(restaurants.values()),
        "failure_summary": "\n".join(failure_lines[-30:]) if failure_lines else None,
    }


def _upload_report(report: dict):
    """Upload report to S3 via Docker exec into the backend container, or save locally."""
    try:
        # Write report to a temp file, then docker exec reads it from the volume mount
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
