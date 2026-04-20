"""Select scraped pending entries for validation with per-category quotas.

Applies tiered targets (primary launch categories get more, niche less)
and selects the best candidates using:
  1. Highest `_est_products` (richer menus)
  2. City diversity (spread across the 5 target cities)
  3. Format mix (images and PDFs)
  4. No duplicate restaurant names

Outputs a JSON file with the selected menu IDs for migration.

Usage:
  python tests/simulation/corpus/select_for_validation.py
  python tests/simulation/corpus/select_for_validation.py --dry-run
"""

import argparse
import json
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

CORPUS = Path(__file__).parent
PENDING = CORPUS / "pending_images.json"
SELECTION = CORPUS / ".selected_for_validation.json"

# Per-category validation targets (how many to ACCEPT into manifest)
# Expects ~25% validation failure rate → actual validated ~75% of these numbers.
CATEGORY_TARGETS = {
    # Primary launch segments — need baseline coverage + random pool variety
    "padaria": 10,
    "cafeteria": 10,
    "açaiteria": 8,
    "marmitaria": 5,  # only 5 candidates exist; take all
    "pastelaria": 8,
    "doceria": 8,
    "lanchonete": 10,
    "hamburgueria": 10,
    # Secondary — plausible future launch
    "pizzaria": 5,
    "espetaria": 5,
    # Niche — representation only
    "sushi": 3,
    "churrascaria": 3,
    "comida japonesa": 3,
    "comida italiana": 3,
    "comida árabe": 3,
    "comida mexicana": 3,
    "tapiocaria": 3,
    "hot dog": 3,
    "creperia": 3,
    "sorveteria": 3,
}


def _norm(s: str) -> str:
    """Normalize category/name for matching (accent-insensitive, lowercase)."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s.lower().strip()


def _clean_restaurant_name(alt: str, category: str, city: str, idx: int) -> str:
    """Extract a clean restaurant name from DDG alt text.

    Examples of alt text:
      "Menu em Pampa Burger restaurante, Porto Alegre, Rua da República"
      "PDF Cardápio Kitchin São Paulo"
      "Restaurant XYZ menu - TripAdvisor"
    """
    if not alt:
        return f"{category.title()} #{idx}"

    alt = alt.strip()

    # Strip common prefixes
    for prefix in (
        "Menu em ",
        "Menu at ",
        "PDF ",
        "Cardápio do ",
        "Cardápio da ",
        "Cardápio de ",
        "Cardápio - ",
        "Cardápio ",
    ):
        if alt.startswith(prefix):
            alt = alt[len(prefix) :]
            break

    # Cut at common separators to isolate the restaurant name
    for sep in [" - ", " | ", ", "]:
        if sep in alt:
            alt = alt.split(sep)[0]
            break

    # Strip trailing category/city suffixes
    for suffix in (" restaurante", " restaurant", " pub & bar", " delivery"):
        if alt.lower().endswith(suffix):
            alt = alt[: -len(suffix)]

    return alt.strip()[:80] or f"{category.title()} #{idx}"


def select_candidates(pending: list, manifest: list) -> dict:
    """Select scraped entries per category using quotas + ranking."""
    # Build set of restaurant names already in manifest (for dedup)
    existing_names = {_norm(e.get("restaurant_name", "")) for e in manifest}

    # Group scraped entries by normalized category
    by_cat = defaultdict(list)
    for e in pending:
        if not e.get("_source", "").startswith("scrape"):
            continue
        if e.get("_migrated"):
            continue
        cat_norm = _norm(e.get("_category", ""))
        by_cat[cat_norm].append(e)

    # Normalize targets keys for matching
    target_map = {_norm(k): (k, v) for k, v in CATEGORY_TARGETS.items()}

    selected = {}
    report = []
    for cat_norm, entries in sorted(by_cat.items()):
        if cat_norm not in target_map:
            report.append(
                {
                    "category": cat_norm,
                    "available": len(entries),
                    "target": 0,
                    "picked": 0,
                    "reason": "no target configured",
                }
            )
            continue

        orig_cat, target = target_map[cat_norm]

        # Score each entry:
        #   primary: _est_products (higher = better menu)
        #   tiebreakers: diversity (city spread) handled after sort
        ranked = sorted(entries, key=lambda e: e.get("_est_products", 0), reverse=True)

        # Pick with city diversity: round-robin through cities
        city_picks = defaultdict(int)
        picked_names = set()
        picked = []
        skipped_dupe = 0
        skipped_deadlink = 0

        for e in ranked:
            if len(picked) >= target:
                break

            # Dedup by restaurant name (vs manifest + within selection)
            idx = len(picked) + 1
            name = _clean_restaurant_name(
                e.get("alt", ""), orig_cat, e.get("_city", ""), idx
            )
            name_norm = _norm(name)
            if name_norm in existing_names or name_norm in picked_names:
                skipped_dupe += 1
                continue

            # Require local file to exist (dead links skipped)
            menu_id = e.get("_menu_id")
            if not menu_id:
                continue
            file_type = "pdf" if "pdf" in e.get("_source", "") else "jpg"
            if file_type == "pdf":
                local_rel = e.get("_local_file", "")
                local_path = CORPUS / local_rel if local_rel else None
            else:
                local_path = CORPUS / "images" / f"{menu_id}.jpg"
            if not local_path or not local_path.exists():
                skipped_deadlink += 1
                continue

            # Accept
            city = e.get("_city", "")
            city_picks[city] += 1
            picked_names.add(name_norm)
            picked.append(
                {
                    "menu_id": menu_id,
                    "category": orig_cat,
                    "city": city,
                    "file_type": file_type,
                    "est_products": e.get("_est_products", 0),
                    "restaurant_name": name,
                }
            )

        selected[orig_cat] = picked
        report.append(
            {
                "category": orig_cat,
                "available": len(entries),
                "target": target,
                "picked": len(picked),
                "skipped_dupes": skipped_dupe,
                "skipped_deadlinks": skipped_deadlink,
                "city_distribution": dict(city_picks),
            }
        )

    return {"selected": selected, "report": report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show plan without writing selection file",
    )
    args = parser.parse_args()

    if not PENDING.exists():
        print(f"ERROR: {PENDING} not found")
        sys.exit(1)

    pending = json.loads(PENDING.read_text(encoding="utf-8"))
    manifest_path = CORPUS / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else []
    )

    result = select_candidates(pending, manifest)

    # Report
    print(f"{'Category':<20} {'Avail':>6} {'Target':>7} {'Picked':>7} {'Cities':>30}")
    print("-" * 75)
    total_picked = 0
    total_target = 0
    for row in result["report"]:
        cities = row.get("city_distribution", {})
        city_str = ", ".join(f"{c[:3]}:{n}" for c, n in sorted(cities.items())[:4])
        print(
            f"{row['category']:<20} {row['available']:>6} {row['target']:>7} {row['picked']:>7}  {city_str[:30]}"
        )
        total_picked += row["picked"]
        total_target += row["target"]
    print("-" * 75)
    print(f"{'TOTAL':<20} {'':>6} {total_target:>7} {total_picked:>7}")
    print()
    print(f"Estimated validation cost: ~${total_picked * 0.02:.2f}")
    print(f"Estimated validation time: ~{(total_picked * 17) // 60} minutes")

    if args.dry_run:
        print("\n(--dry-run: selection file not written)")
        return

    SELECTION.write_text(
        json.dumps(
            {"selected": result["selected"], "report": result["report"]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nSelection written to: {SELECTION}")
    print("Next: python tests/simulation/corpus/migrate_to_manifest.py")


if __name__ == "__main__":
    main()
