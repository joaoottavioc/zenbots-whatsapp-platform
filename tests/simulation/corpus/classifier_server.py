"""
Menu Classifier Game — Backend

Serves the game HTML and downloads accepted menu images to corpus/images/.

Run:  python tests/simulation/corpus/classifier_server.py
Open: http://localhost:8888
"""

import json
import os
from datetime import date
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import httpx

CORPUS_DIR = Path(__file__).parent
IMAGES_DIR = CORPUS_DIR / "images"
MANIFEST_PATH = CORPUS_DIR / "manifest.json"

IMAGES_DIR.mkdir(exist_ok=True)


def load_manifest() -> list:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return []


def save_manifest(manifest: list):
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/api/accept":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))

            image_url = body.get("src", "")
            alt = body.get("alt", "")

            if not image_url:
                self._json(400, {"error": "missing src"})
                return

            # Download image — use DDG proxy URL directly (original Bing URLs get blocked)
            try:
                resp = httpx.get(
                    image_url,
                    timeout=25,
                    follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                if resp.status_code != 200 or len(resp.content) < 2000:
                    self._json(
                        422,
                        {
                            "error": f"status={resp.status_code}, size={len(resp.content)}"
                        },
                    )
                    return
            except Exception as e:
                self._json(422, {"error": str(e)})
                return

            # Determine extension from content-type
            ct = resp.headers.get("content-type", "")
            if "png" in ct:
                ext = ".png"
            elif "webp" in ct:
                ext = ".webp"
            else:
                ext = ".jpg"

            # Generate sequential ID
            manifest = load_manifest()
            n = len(manifest) + 1
            image_id = f"menu_{n:04d}"
            filename = f"{image_id}{ext}"
            filepath = IMAGES_DIR / filename

            filepath.write_bytes(resp.content)

            entry = {
                "id": image_id,
                "file": f"images/{filename}",
                "category": "",
                "restaurant_name": (alt or "")[:60],
                "source_url": image_url[:500],
                "source_type": "classifier_game",
                "added_date": date.today().isoformat(),
                "file_size_kb": len(resp.content) // 1024,
                "product_count": 0,
                "validated": False,
                "last_used": None,
            }
            manifest.append(entry)
            save_manifest(manifest)

            size_kb = len(resp.content) // 1024
            print(f"  [{n}] {image_id} — {size_kb}KB — {alt[:40]}")

            self._json(
                200, {"id": image_id, "file": filename, "size_kb": size_kb, "total": n}
            )
            return

        elif self.path == "/api/stats":
            manifest = load_manifest()
            self._json(200, {"total": len(manifest)})
            return

        self._json(404, {"error": "not found"})

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt, *args):
        msg = fmt % args
        if "POST" in msg:
            super().log_message(fmt, *args)


def main():
    os.chdir(str(CORPUS_DIR))
    port = 8888
    server = HTTPServer(("0.0.0.0", port), Handler)
    manifest = load_manifest()
    print("Menu Classifier Server")
    print(f"  URL:      http://localhost:{port}")
    print(f"  Images:   {IMAGES_DIR}")
    print(f"  Corpus:   {len(manifest)} images already saved")
    print("\n  Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        manifest = load_manifest()
        print(f"\nDone! {len(manifest)} images in corpus.")


if __name__ == "__main__":
    main()
