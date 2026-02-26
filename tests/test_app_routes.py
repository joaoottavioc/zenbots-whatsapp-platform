# tests/test_app_routes.py
"""
Verify that no (path, method) pair is registered more than once on the FastAPI app.
"""
import sys
from unittest.mock import MagicMock

# Stub heavy optional dependencies that aren't installed in the test env
for mod in ("fitz",):
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from app.main import app


def test_no_duplicate_route_paths():
    """Iterate app.routes and assert no (path, method) pair appears twice."""
    seen = set()
    duplicates = []

    for route in app.routes:
        methods = getattr(route, "methods", None)
        if not methods:
            continue
        for method in methods:
            key = (route.path, method)
            if key in seen:
                duplicates.append(key)
            seen.add(key)

    assert duplicates == [], f"Duplicate routes found: {duplicates}"
