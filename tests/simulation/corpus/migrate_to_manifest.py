"""Migrate selected pending entries into manifest.json as unvalidated real_restaurant entries.

Reads the selection file produced by select_for_validation.py and:
  - Moves PDF files from pdfs/ to images/ (manifest expects images/)
    (wait — actually, manifest entries can reference images/ or pdfs/ paths.
    Checking validate_corpus.py: it reads entry['file'] and uploads it.
    So we just need to set `file` correctly.)
  - Adds a manifest entry per selection with validated=null
  - Marks pending entries as _migrated=true (idempotent)

Usage:
  python tests/simulation/corpus/migrate_to_manifest.py
  python tests/simulation/corpus/migrate_to_manifest.py --dry-run
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

CORPUS = Path(__file__).parent
PENDING = CORPUS / "pending_images.json"
MANIFEST = CORPUS / "manifest.json"
SELECTION = CORPUS / ".selected_for_validation.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Show plan without modifying files"
    )
    args = parser.parse_args()

    if not SELECTION.exists():
        print(f"ERROR: {SELECTION} not found. Run select_for_validation.py first.")
        sys.exit(1)

    selection_data = json.loads(SELECTION.read_text(encoding="utf-8"))
    selected = selection_data["selected"]

    pending = json.loads(PENDING.read_text(encoding="utf-8"))
    pending_by_id = {e.get("_menu_id"): e for e in pending if e.get("_menu_id")}

    manifest = (
        json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else []
    )
    manifest_ids = {e["id"] for e in manifest}

    # Build migration plan
    plan = []
    for cat, entries in selected.items():
        for sel in entries:
            menu_id = sel["menu_id"]
            if menu_id in manifest_ids:
                continue  # Already in manifest
            src_entry = pending_by_id.get(menu_id)
            if not src_entry:
                print(f"  WARN: {menu_id} in selection but not in pending — skip")
                continue

            file_type = sel["file_type"]
            if file_type == "pdf":
                local_rel = src_entry.get("_local_file", "")
                file_path = local_rel  # keep pdfs/ prefix
            else:
                file_path = f"images/{menu_id}.jpg"

            abs_path = CORPUS / file_path
            if not abs_path.exists():
                print(f"  WARN: {menu_id} file missing at {abs_path} — skip")
                continue

            plan.append(
                {
                    "menu_id": menu_id,
                    "file": file_path,
                    "category": sel["category"],
                    "city": sel["city"],
                    "restaurant_name": sel["restaurant_name"],
                    "source_url": src_entry.get("src", "")[:500],
                    "source_type": src_entry.get("_source", ""),
                    "file_size_kb": abs_path.stat().st_size // 1024,
                    "est_products": sel.get("est_products", 0),
                    "s3_url": src_entry.get("_s3_url", ""),
                }
            )

    print(f"Migration plan: {len(plan)} entries")
    print()
    by_cat = {}
    for p in plan:
        by_cat[p["category"]] = by_cat.get(p["category"], 0) + 1
    for cat, n in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"  {cat:<20} {n}")
    print()

    if args.dry_run:
        print("(--dry-run: manifest not modified)")
        return

    # Apply: add to manifest, mark as _migrated in pending
    added = 0
    for p in plan:
        new_entry = {
            "id": p["menu_id"],
            "file": p["file"],
            "category": p["category"],
            "restaurant_name": p["restaurant_name"],
            "source_url": p["source_url"],
            "source_type": p["source_type"],
            "added_date": date.today().isoformat(),
            "file_size_kb": p["file_size_kb"],
            "product_count": 0,  # updated by validate_corpus.py
            "validated": None,  # unvalidated — triggers validate_corpus.py
            "last_used": None,
            "menu_type": "real_restaurant",
            "_est_products": p.get("est_products", 0),
            "_s3_url": p.get("s3_url", ""),
            "_city": p.get("city", ""),
        }
        manifest.append(new_entry)
        added += 1

        # Mark pending entry as migrated
        src = pending_by_id.get(p["menu_id"])
        if src:
            src["_migrated"] = True

    # Save
    MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    PENDING.write_text(
        json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Added {added} entries to manifest.json with validated=null")
    print(f"Marked {added} entries as _migrated=true in pending_images.json")
    print()
    print("Next: python tests/simulation/corpus/validate_corpus.py")
    print("  (Backend must be running — this validates each via Cadastro Mágico)")


if __name__ == "__main__":
    main()
