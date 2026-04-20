"""Generate `baseline_pool.json` — the stable 10-restaurant test pool.

The pool is the canonical "did this fix work?" measurement set. Every
Pillar 2+ fix gets a before/after on the SAME 10 restaurants so the
delta is real, not sampling lottery.

Composition rules (Phase 1C, 2026-04-09; updated 2026-04-13):
- 8 of 10 from "narrow launch" segments (bakeries, cafeterias, açaí,
  marmitarias, pastelarias, docerias, lanchonetes). These match the
  initial launch profile from the strategy doc — small simple menus,
  no customizations, daytime traffic.
- 2 of 10 from harder segments (hamburgueria + 1 pizzaria) to keep us
  honest about cross-restaurant variance and surface ambiguous-product
  failures.
- **Real menus preferred**: within each category, entries with
  menu_type="real_restaurant" are selected before "template" entries.
  This ensures launch gates measure against real-world menus as they
  are promoted from the prospect pipeline into the corpus.

The picker is DETERMINISTIC (no random.shuffle) so re-running it on the
same manifest produces the same pool. To rotate the pool intentionally,
either change the per-category counts below or pop entries from the
manifest before re-running.

Usage:
    python tests/simulation/corpus/pick_baseline_pool.py          # generate pool
    python tests/simulation/corpus/pick_baseline_pool.py --stats  # dry run, show composition
    git add tests/simulation/corpus/baseline_pool.json
    git commit -m "refresh baseline test pool"
"""

import json
from pathlib import Path

CORPUS = Path(__file__).parent
MANIFEST = CORPUS / "manifest.json"
POOL_FILE = CORPUS / "baseline_pool.json"

# Per-category counts. Weighted toward narrow-launch segments. Adjust to
# rotate the pool. Total should be 10 (or whatever you want pool size to be).
CATEGORY_COUNTS = {
    "padaria": 1,  # bakeries — primary launch segment
    "cafeteria": 1,  # cafés — primary launch segment
    "acaiteria": 1,  # açaí — primary launch segment (also handles "açaiteria" with cedilla)
    "marmitaria": 1,  # marmitas — primary launch segment
    "pastelaria": 1,  # pastéis — primary launch segment, Brazilian snack menus
    "doceria": 1,  # docerias — primary launch segment
    "lanchonete": 1,  # lanchonetes — borderline narrow
    "hamburgueria": 1,  # stress test for ambiguous abbreviations
    "pizzaria": 1,  # pizza menus — rich catalogs, tests long product names + size variants
    "sushi": 1,  # sushi — complex product naming (Japanese terms), size/piece variants
    "espetaria": 1,  # espetinhos — Brazilian-specific naming, informal product names
}
TARGET_POOL_SIZE = sum(CATEGORY_COUNTS.values())  # 10


def _normalize_category(cat: str) -> str:
    """Normalize category for matching (handle açaiteria vs acaiteria etc)."""
    return (
        cat.lower()
        .strip()
        .replace("á", "a")
        .replace("ã", "a")
        .replace("â", "a")
        .replace("é", "e")
        .replace("ê", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ô", "o")
        .replace("õ", "o")
        .replace("ú", "u")
        .replace("ç", "c")
    )


_MENU_TYPE_PRIORITY = {
    "real_restaurant": 0,  # promoted prospects — highest priority
    "tier2": 1,  # iFood/Rappi/Google Maps screenshots
    "prospect": 2,  # prospect entries (not yet promoted)
    "template": 3,  # design templates from Google Images
}


def _menu_type_sort_key(entry: dict) -> int:
    """Sort key: real menus before templates (lower = higher priority)."""
    return _MENU_TYPE_PRIORITY.get(entry.get("menu_type", "template"), 9)


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Dry run: show pool composition without writing the file",
    )
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    validated = [e for e in manifest if e.get("validated")]
    print(f"Loaded {len(validated)} validated entries from manifest.")

    # Count menu types
    type_counts: dict[str, int] = {}
    for e in validated:
        mt = e.get("menu_type", "template")
        type_counts[mt] = type_counts.get(mt, 0) + 1
    print(f"Menu types: {type_counts}")

    # Group by normalized category, preserving manifest order (deterministic)
    by_category: dict[str, list[dict]] = {}
    for entry in validated:
        cat = _normalize_category(entry.get("category", "outros"))
        by_category.setdefault(cat, []).append(entry)

    # Sort each category's entries: real menus first, templates last
    for cat in by_category:
        by_category[cat].sort(key=_menu_type_sort_key)

    print(f"Available categories: {sorted(by_category.keys())}")
    print()

    pool: list[dict] = []
    for category, want_count in CATEGORY_COUNTS.items():
        candidates = by_category.get(category, [])
        if not candidates:
            print(f"  WARNING: no '{category}' entries available, skipping")
            continue
        chosen = candidates[:want_count]
        for entry in chosen:
            pool.append(
                {
                    "id": entry["id"],
                    "category": category,
                    "restaurant_name": entry.get("restaurant_name", entry["id"]),
                    "menu_type": entry.get("menu_type", "unknown"),
                    "product_count": entry.get("product_count", 0),
                }
            )
        real = sum(1 for c in chosen if c.get("menu_type") != "template")
        tmpl = len(chosen) - real
        label = f"{real} real + {tmpl} template" if real else f"{tmpl} template"
        print(f"  picked {len(chosen)}/{want_count} from {category} ({label})")

    # Backfill if any category was short — pull from largest categories
    if len(pool) < TARGET_POOL_SIZE:
        already = {e["id"] for e in pool}
        for category, entries in sorted(
            by_category.items(), key=lambda kv: -len(kv[1])
        ):
            if len(pool) >= TARGET_POOL_SIZE:
                break
            for entry in entries:
                if entry["id"] in already:
                    continue
                pool.append(
                    {
                        "id": entry["id"],
                        "category": category,
                        "restaurant_name": entry.get("restaurant_name", entry["id"]),
                        "menu_type": entry.get("menu_type", "unknown"),
                        "product_count": entry.get("product_count", 0),
                    }
                )
                already.add(entry["id"])
                print(f"  backfilled from {category}")
                if len(pool) >= TARGET_POOL_SIZE:
                    break

    # Pool composition summary
    pool_real = sum(1 for e in pool if e.get("menu_type") != "template")
    pool_template = len(pool) - pool_real

    print()
    print(f"Final pool size: {len(pool)}")
    print(f"Composition: {pool_real} real / {pool_template} template")
    print()
    print("Pool entries:")
    for i, entry in enumerate(pool, 1):
        name = entry["restaurant_name"][:45]
        mt = entry.get("menu_type", "?")
        tag = " [REAL]" if mt != "template" else ""
        print(
            f"  {i:>2}. {entry['id']:>15} | {entry['category']:>15s} | "
            f"{entry['product_count']:>3} products | {name}{tag}"
        )

    if args.stats:
        print()
        print("(--stats mode: pool file not written)")
        return

    POOL_FILE.write_text(
        json.dumps(
            {
                "version": 1,
                "size": len(pool),
                "category_counts": CATEGORY_COUNTS,
                "entries": pool,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print()
    print(f"Wrote {POOL_FILE}")


if __name__ == "__main__":
    main()
