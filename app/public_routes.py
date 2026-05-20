"""Public portfolio endpoints (no auth, no rate-limit by user).

P5 of plan/portfolio_pivot.md. These are read-only artifacts surfaced
for recruiters / curious visitors — separate from `chat_routes` (web
widget customer-facing) and `monitoring_routes` (owner-authenticated
cost data).

Endpoints:

  - GET /public/eval/latest  → docs/eval/latest.json

The snapshot is committed to the repo (see scripts/snapshot_corpus_qa.py
+ docs/eval/latest.json) so every deploy serves a deterministic value
without needing to run the corpus suite at request time. To refresh
the numbers, re-run the snapshot script, commit, redeploy.

CORS is wide-open for this router because the response is intentionally
public; the frontend `/eval` page may be fetched from anywhere
(including from a third-party blog post embedding it). The data itself
is aggregate-only — no per-bot, per-customer, or per-conversation
content.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/public", tags=["Public"])

# Snapshot lives at repo-root/docs/eval/latest.json. main.py runs from
# the repo root in the container (WORKDIR /code), so the relative path
# resolves correctly. Compute it once at import.
_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "eval" / "latest.json"

# Short browser cache so a hammered page doesn't re-fetch on every nav,
# but short enough that a deploy with refreshed numbers shows up
# promptly. The data only changes when scripts/snapshot_corpus_qa.py
# is re-run + committed, which is at most weekly in practice.
_CACHE_CONTROL = "public, max-age=300, s-maxage=300"


@router.get(
    "/eval/latest",
    summary="Latest corpus QA snapshot",
    response_class=JSONResponse,
)
async def get_eval_latest() -> JSONResponse:
    """Return the most recent corpus QA snapshot.

    Shape mirrors `scripts/snapshot_corpus_qa.py:build_snapshot` — keys:
    `schema_version`, `generated_at`, `headline`, `comprehension`,
    `consistency`, `scenarios`, `methodology`.

    503 if the snapshot file is missing (e.g., a fresh dev environment
    that hasn't been seeded yet). 500 if it's present but malformed
    (means someone committed a broken JSON, which is a real bug).
    """
    if not _SNAPSHOT_PATH.exists():
        logger.warning("Eval snapshot missing at %s", _SNAPSHOT_PATH)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "eval_snapshot_unavailable",
                "message": (
                    "Eval snapshot has not been generated yet. Run "
                    "scripts/snapshot_corpus_qa.py to seed it."
                ),
            },
        )

    try:
        data = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.error("Eval snapshot malformed at %s: %s", _SNAPSHOT_PATH, e)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "eval_snapshot_invalid",
                "message": "Snapshot file is not valid JSON.",
            },
        )

    return JSONResponse(
        content=data,
        headers={"Cache-Control": _CACHE_CONTROL},
    )
