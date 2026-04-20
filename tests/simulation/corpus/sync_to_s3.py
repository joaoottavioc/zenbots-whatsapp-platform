"""Sync the QA corpus to S3.

Protects against URL rot (DDG proxy links die) by downloading all scraped
image URLs locally + uploading everything to S3 in a structured layout:

  s3://{bucket}/qa-corpus/
  ├── menus/
  │   └── {menu_id}/
  │       ├── source/            # Immutable original files
  │       │   ├── original-1.{ext}
  │       │   └── ...
  │       ├── extractions/       # Versioned gpt-4o output
  │       │   ├── {ts}.json
  │       │   └── latest.json
  │       ├── enrichments/       # Manual edits
  │       └── metadata.json
  ├── pdfs/                      # PDF binaries (also mirrored in menus/)
  └── indexes/
      ├── manifest.json
      ├── baseline_pool.json
      └── prospect_pool.json

Usage:
  python tests/simulation/corpus/sync_to_s3.py             # full sync
  python tests/simulation/corpus/sync_to_s3.py --dry-run   # show plan only
  python tests/simulation/corpus/sync_to_s3.py --indexes-only  # just indexes

Env vars required:
  AWS_BUCKET_NAME, AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
"""

import argparse
import asyncio
import json
import mimetypes
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
import httpx
from botocore.exceptions import ClientError

CORPUS = Path(__file__).parent
PDFS_DIR = CORPUS / "pdfs"
IMAGES_DIR = CORPUS / "images"
EXTRACTIONS_DIR = CORPUS / "extractions"
MANIFEST = CORPUS / "manifest.json"
PENDING = CORPUS / "pending_images.json"
BASELINE = CORPUS / "baseline_pool.json"
PROSPECTS = CORPUS / "prospect_pool.json"

BUCKET = os.getenv("AWS_BUCKET_NAME", "")
REGION = os.getenv("AWS_REGION", "us-east-1")
S3_PREFIX = "qa-corpus"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def s3_client():
    if not BUCKET:
        print("ERROR: AWS_BUCKET_NAME env var not set")
        sys.exit(1)
    return boto3.client("s3", region_name=REGION)


def menu_key(menu_id: str, *parts: str) -> str:
    return f"{S3_PREFIX}/menus/{menu_id}/" + "/".join(parts)


def s3_url(key: str) -> str:
    return f"https://{BUCKET}.s3.{REGION}.amazonaws.com/{key}"


def guess_content_type(path: str) -> str:
    ct, _ = mimetypes.guess_type(path)
    return ct or "application/octet-stream"


def s3_object_exists(s3, key: str) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def upload_bytes(
    s3, data: bytes, key: str, content_type: str, skip_if_exists: bool = True
) -> bool:
    """Upload bytes to S3. Returns True if uploaded, False if skipped."""
    if skip_if_exists and s3_object_exists(s3, key):
        return False
    s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type)
    return True


def upload_json(s3, obj: dict, key: str, skip_if_exists: bool = False) -> bool:
    return upload_bytes(
        s3,
        json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
        key,
        "application/json",
        skip_if_exists,
    )


async def download_url(url: str, timeout: int = 20) -> bytes | None:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and len(r.content) > 2000:
                return r.content
    except Exception:
        pass
    return None


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Sync operations
# ---------------------------------------------------------------------------


def sync_manifest_entries(s3, manifest: list, dry_run: bool) -> dict:
    """Sync validated corpus entries (those with local image files)."""
    stats = {"uploaded": 0, "skipped": 0, "missing": 0}

    for entry in manifest:
        menu_id = entry["id"]
        local_file = CORPUS / entry.get("file", "")

        if not local_file.exists():
            stats["missing"] += 1
            continue

        ext = local_file.suffix.lstrip(".") or "jpg"
        source_key = menu_key(menu_id, "source", f"original.{ext}")

        if dry_run:
            print(f"  DRY {menu_id} -> {source_key}")
            stats["uploaded"] += 1
            continue

        data = local_file.read_bytes()
        uploaded = upload_bytes(
            s3, data, source_key, guess_content_type(str(local_file))
        )

        # Always write metadata (overwrites — small file, cheap)
        metadata = {
            "id": menu_id,
            "restaurant_name": entry.get("restaurant_name", ""),
            "category": entry.get("category", ""),
            "city": entry.get("city", ""),
            "menu_type": entry.get("menu_type", "template"),
            "source_url": entry.get("source_url", ""),
            "source_type": entry.get("source_type", ""),
            "added_date": entry.get("added_date", ""),
            "product_count": entry.get("product_count", 0),
            "validated": entry.get("validated", None),
            "s3_source": s3_url(source_key),
            "synced_at": now_iso(),
        }
        upload_json(
            s3, metadata, menu_key(menu_id, "metadata.json"), skip_if_exists=False
        )

        # Sync extraction if present
        extraction_file = EXTRACTIONS_DIR / f"{menu_id}.json"
        if extraction_file.exists():
            extraction_data = json.loads(extraction_file.read_text(encoding="utf-8"))
            ts_key = menu_key(menu_id, "extractions", f"{now_iso()}.json")
            latest_key = menu_key(menu_id, "extractions", "latest.json")
            if not s3_object_exists(s3, latest_key):
                upload_json(s3, extraction_data, ts_key)
                upload_json(s3, extraction_data, latest_key)

        if uploaded:
            stats["uploaded"] += 1
            print(f"  UP  {menu_id} ({ext}, {len(data) // 1024}KB)")
        else:
            stats["skipped"] += 1

    return stats


def _next_menu_id(existing_ids: set) -> str:
    n = 1
    while f"menu_{n:04d}" in existing_ids:
        n += 1
    return f"menu_{n:04d}"


async def sync_pending_scraped(
    s3, pending: list, manifest: list, dry_run: bool
) -> dict:
    """Download + sync pending scraped entries (images from URLs, PDFs already local)."""
    stats = {
        "downloaded": 0,
        "uploaded": 0,
        "dead_links": 0,
        "already_synced": 0,
        "assigned_ids": 0,
    }
    existing_ids = {e["id"] for e in manifest}

    IMAGES_DIR.mkdir(exist_ok=True)

    for entry in pending:
        src_tag = entry.get("_source", "")
        if not src_tag.startswith("scrape"):
            continue

        if entry.get("_s3_synced"):
            stats["already_synced"] += 1
            continue

        # Assign menu_id if missing
        if "_menu_id" not in entry:
            new_id = _next_menu_id(existing_ids)
            entry["_menu_id"] = new_id
            existing_ids.add(new_id)
            stats["assigned_ids"] += 1
        menu_id = entry["_menu_id"]

        # Determine local file
        is_pdf = src_tag.endswith("_pdf") or entry.get("file_type") == "pdf"
        if is_pdf:
            local_rel = entry.get("_local_file", "")
            local_path = CORPUS / local_rel if local_rel else None
            if not local_path or not local_path.exists():
                stats["dead_links"] += 1
                print(f"  MISS {menu_id} (PDF local file missing: {local_rel})")
                continue
            ext = "pdf"
        else:
            # Image: download if not already
            local_name = f"{menu_id}.jpg"
            local_path = IMAGES_DIR / local_name
            if not local_path.exists():
                if dry_run:
                    print(f"  DRY {menu_id} -> would download {entry['src'][:60]}...")
                    stats["downloaded"] += 1
                    continue
                data = await download_url(entry["src"])
                if not data:
                    stats["dead_links"] += 1
                    print(f"  DEAD {menu_id} ({entry['src'][:60]}...)")
                    continue
                local_path.write_bytes(data)
                stats["downloaded"] += 1
            ext = "jpg"

        if dry_run:
            print(
                f"  DRY {menu_id} -> {menu_key(menu_id, 'source', f'original.{ext}')}"
            )
            stats["uploaded"] += 1
            continue

        # Upload source file
        data = local_path.read_bytes()
        source_key = menu_key(menu_id, "source", f"original.{ext}")
        upload_bytes(s3, data, source_key, guess_content_type(str(local_path)))

        # Write metadata
        metadata = {
            "id": menu_id,
            "restaurant_name": entry.get("alt", "")[:100],
            "category": entry.get("_category", ""),
            "city": entry.get("_city", ""),
            "menu_type": entry.get("menu_type", "tier2"),
            "source_url": entry.get("src", ""),
            "source_type": src_tag,
            "file_type": "pdf" if is_pdf else "image",
            "est_products": entry.get("_est_products", 0),
            "validated": None,  # Not yet validated via Cadastro Mágico
            "s3_source": s3_url(source_key),
            "synced_at": now_iso(),
        }
        upload_json(s3, metadata, menu_key(menu_id, "metadata.json"))

        entry["_s3_synced"] = True
        entry["_s3_url"] = s3_url(source_key)
        stats["uploaded"] += 1
        print(f"  UP  {menu_id} ({ext}, {len(data) // 1024}KB)")

    return stats


def sync_prospects(s3, prospects: dict, dry_run: bool) -> dict:
    """Sync prospect entries (usually have image files + extraction)."""
    stats = {"uploaded": 0, "skipped": 0, "missing": 0}

    for entry in prospects.get("entries", []):
        menu_id = entry["id"]
        image_files = entry.get("image_files") or (
            [entry.get("image_file")] if entry.get("image_file") else []
        )

        uploaded_any = False
        for i, rel in enumerate(image_files, 1):
            if not rel:
                continue
            local = CORPUS / rel
            if not local.exists():
                stats["missing"] += 1
                continue

            ext = local.suffix.lstrip(".") or "jpg"
            suffix = f"-{i}" if len(image_files) > 1 else ""
            source_key = menu_key(menu_id, "source", f"original{suffix}.{ext}")

            if dry_run:
                print(f"  DRY {menu_id} -> {source_key}")
                uploaded_any = True
                continue

            data = local.read_bytes()
            if upload_bytes(s3, data, source_key, guess_content_type(str(local))):
                uploaded_any = True
                print(f"  UP  {menu_id} src[{i}] ({ext}, {len(data) // 1024}KB)")

        if not uploaded_any:
            continue

        if dry_run:
            stats["uploaded"] += 1
            continue

        # Metadata
        metadata = {
            "id": menu_id,
            "restaurant_name": entry.get("restaurant_name", ""),
            "category": entry.get("category", ""),
            "city": entry.get("city", ""),
            "menu_type": "prospect"
            if entry.get("status") != "promoted"
            else "real_restaurant",
            "source_type": "prospect",
            "status": entry.get("status"),
            "product_count": entry.get("product_count", 0),
            "added_date": entry.get("added_date", ""),
            "synced_at": now_iso(),
        }
        upload_json(s3, metadata, menu_key(menu_id, "metadata.json"))

        # Upload extraction
        ext_file_rel = entry.get("extraction_file", "")
        if ext_file_rel:
            ext_file = CORPUS / ext_file_rel
            if ext_file.exists():
                ext_data = json.loads(ext_file.read_text(encoding="utf-8"))
                ts_key = menu_key(menu_id, "extractions", f"{now_iso()}.json")
                latest_key = menu_key(menu_id, "extractions", "latest.json")
                if not s3_object_exists(s3, latest_key):
                    upload_json(s3, ext_data, ts_key)
                    upload_json(s3, ext_data, latest_key)

        stats["uploaded"] += 1

    return stats


def sync_indexes(s3, dry_run: bool) -> dict:
    """Upload the top-level JSON indexes."""
    stats = {"uploaded": 0, "missing": 0}

    for path in [MANIFEST, BASELINE, PROSPECTS, PENDING]:
        if not path.exists():
            stats["missing"] += 1
            continue
        key = f"{S3_PREFIX}/indexes/{path.name}"
        data = path.read_bytes()
        if dry_run:
            print(f"  DRY {path.name} -> {key} ({len(data) // 1024}KB)")
            stats["uploaded"] += 1
            continue
        s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType="application/json")
        print(f"  UP  indexes/{path.name} ({len(data) // 1024}KB)")
        stats["uploaded"] += 1

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be uploaded without uploading",
    )
    parser.add_argument(
        "--indexes-only", action="store_true", help="Only sync indexes (fast)"
    )
    parser.add_argument(
        "--skip-pending",
        action="store_true",
        help="Skip pending_images.json scraped entries (no download pass)",
    )
    args = parser.parse_args()

    print(f"Target: s3://{BUCKET}/{S3_PREFIX}/ (region: {REGION})")
    if args.dry_run:
        print("DRY RUN — no uploads will happen")
    print()

    s3 = s3_client() if not args.dry_run else None

    if args.indexes_only:
        print("=== Indexes only ===")
        stats = sync_indexes(s3, args.dry_run)
        print(f"Indexes: uploaded={stats['uploaded']}, missing={stats['missing']}")
        return

    manifest = load_json(MANIFEST, [])
    pending = load_json(PENDING, [])
    prospects = load_json(PROSPECTS, {"version": 1, "entries": []})

    print(
        f"Manifest entries: {len(manifest)} ({sum(1 for e in manifest if e.get('validated'))} validated)"
    )
    print(
        f"Pending entries: {len(pending)} ({sum(1 for e in pending if e.get('_source', '').startswith('scrape'))} scraped)"
    )
    print(f"Prospects: {len(prospects.get('entries', []))}")
    print()

    # 1. Sync manifest-validated entries
    print("=== Manifest entries ===")
    mstats = sync_manifest_entries(s3, manifest, args.dry_run)
    print(
        f"Manifest: uploaded={mstats['uploaded']}, skipped={mstats['skipped']}, missing={mstats['missing']}"
    )
    print()

    # 2. Sync prospects
    print("=== Prospects ===")
    pstats = sync_prospects(s3, prospects, args.dry_run)
    print(
        f"Prospects: uploaded={pstats['uploaded']}, skipped={pstats['skipped']}, missing={pstats['missing']}"
    )
    print()

    # 3. Sync pending scraped entries (download + upload)
    if not args.skip_pending:
        print("=== Pending scraped (download + upload) ===")
        sstats = await sync_pending_scraped(s3, pending, manifest, args.dry_run)
        print(
            f"Pending: uploaded={sstats['uploaded']}, downloaded={sstats['downloaded']}, "
            f"dead_links={sstats['dead_links']}, already_synced={sstats['already_synced']}, "
            f"new_ids={sstats['assigned_ids']}"
        )
        # Save updated pending (with _menu_id, _s3_synced flags)
        if not args.dry_run:
            save_json(PENDING, pending)
        print()

    # 4. Sync indexes last (they reference the uploaded content)
    print("=== Indexes ===")
    istats = sync_indexes(s3, args.dry_run)
    print(f"Indexes: uploaded={istats['uploaded']}")


if __name__ == "__main__":
    asyncio.run(main())
