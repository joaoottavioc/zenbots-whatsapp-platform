"""Snapshot the corpus QA suite into eval/latest.json.

P5 of plan/portfolio_pivot.md. Wraps the existing comprehension harness
at `tests/simulation/corpus/run_corpus_qa.py` and writes a public-safe
JSON snapshot the frontend `/eval` page renders. The snapshot is
versioned in-repo so:

  - Every deploy serves the current numbers (no runtime corpus run).
  - `git log -- docs/eval/latest.json` is the comprehension history.
  - Recruiters can read it cold without spinning up the backend.

What's *not* in the snapshot:
  - Failure transcripts (could leak prompt internals)
  - Per-bot business names if they look proprietary
  - The full corpus payload (just aggregated counts)

Usage:

    # Local: requires docker compose up so the worker can hit Postgres.
    docker compose exec backend python -m scripts.snapshot_corpus_qa

    # Or, with a representative manual override if you only have
    # headline numbers and want to refresh the public page:
    python -m scripts.snapshot_corpus_qa --headline-only \\
        --pass-rate 79.4 --total 442 --passed 351 --failed 91

The headline-only path skips the corpus run entirely — useful when the
full harness is slow or you just want to publish updated numbers from
a previous run.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("snapshot_corpus_qa")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = REPO_ROOT / "eval" / "latest.json"

SNAPSHOT_SCHEMA_VERSION = "1"


def _round_pct(passed: int, total: int) -> float:
    if total == 0:
        return 0.0
    return round((passed / total) * 100, 1)


def build_snapshot(
    *,
    total_tests: int,
    passed: int,
    failed: int,
    skipped: int = 0,
    comprehension_total: int | None = None,
    comprehension_passed: int | None = None,
    comprehension_failed: int | None = None,
    worst_restaurant_pct: float | None = None,
    cross_restaurant_spread_pp: float | None = None,
    scenarios: list[dict] | None = None,
    samples_per_restaurant: int | None = None,
    corpus_size: int | None = None,
) -> dict:
    """Assemble the public-safe snapshot payload.

    All optional fields fall back to sensible defaults so a headline-only
    refresh (just pass_rate + counts) still produces a valid file.
    """
    if comprehension_total is None:
        comprehension_total = total_tests
    if comprehension_passed is None:
        comprehension_passed = passed
    if comprehension_failed is None:
        comprehension_failed = failed

    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "headline": {
            "total_tests": total_tests,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "pass_rate_pct": _round_pct(passed, total_tests),
        },
        "comprehension": {
            "total": comprehension_total,
            "passed": comprehension_passed,
            "failed": comprehension_failed,
            "pass_rate_pct": _round_pct(comprehension_passed, comprehension_total),
        },
        "consistency": {
            "worst_restaurant_pct": worst_restaurant_pct,
            "cross_restaurant_spread_pp": cross_restaurant_spread_pp,
        },
        "scenarios": scenarios or [],
        "methodology": {
            "harness": "tests/simulation/corpus/run_corpus_qa.py",
            "corpus_size": corpus_size,
            "samples_per_restaurant": samples_per_restaurant,
            "description": (
                "Ground-truth conversation scenarios per restaurant: "
                "add/remove/modify items, multi-item orders, suggestions, "
                "checkout flow, ambiguity handling. Comprehension is "
                "scored as 'the bot understood the user's intent and "
                "produced the correct cart state' — not just 'no crash'."
            ),
            "repo_link": "https://github.com/joaoottavioc/zenbots-whatsapp-platform/tree/main/tests/simulation/corpus",
        },
    }


def _from_corpus_run() -> dict:
    """Run the full corpus QA suite via its existing CLI and convert the
    return dict into the public snapshot shape.

    Imports run_corpus_qa lazily so the headline-only path stays cheap
    (the import chain pulls in SQLAlchemy + the rest of app/).
    """
    # The corpus harness is built to be invoked from pytest. Running it
    # programmatically here without spinning up Docker would be fragile;
    # the CLI route is `python -m tests.simulation.corpus.run_corpus_qa`.
    # For v1 we only support headline-only mode in CI and document the
    # full-run path as a manual local step.
    raise NotImplementedError(
        "Full corpus run from snapshot script is a Phase 2 enhancement. "
        "For now, run the corpus suite via the existing pytest entrypoint "
        "and pass headline numbers with --headline-only, or paste the "
        "resulting report dict into a future --from-report PATH flag."
    )


def write_snapshot(snapshot: dict, path: Path = SNAPSHOT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Sort keys for deterministic diffs across runs — git history is
    # cleaner when the same numbers always produce the same bytes.
    payload = json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False)
    path.write_text(payload + "\n", encoding="utf-8")
    logger.info("Wrote snapshot %d bytes → %s", len(payload), path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--headline-only",
        action="store_true",
        help="Skip the corpus run; build the snapshot from CLI flags.",
    )
    parser.add_argument(
        "--pass-rate", type=float, default=None, help="Headline pass rate (percent)."
    )
    parser.add_argument("--total", type=int, default=None, help="Total tests.")
    parser.add_argument("--passed", type=int, default=None, help="Passed tests.")
    parser.add_argument("--failed", type=int, default=None, help="Failed tests.")
    parser.add_argument("--skipped", type=int, default=0, help="Skipped tests.")
    parser.add_argument(
        "--corpus-size",
        type=int,
        default=None,
        help="Number of restaurants in the corpus.",
    )
    parser.add_argument("--samples-per-restaurant", type=int, default=None)
    args = parser.parse_args()

    if args.headline_only:
        if args.total is None or args.passed is None or args.failed is None:
            parser.error("--headline-only requires --total --passed --failed")
        snapshot = build_snapshot(
            total_tests=args.total,
            passed=args.passed,
            failed=args.failed,
            skipped=args.skipped,
            corpus_size=args.corpus_size,
            samples_per_restaurant=args.samples_per_restaurant,
        )
    else:
        # Full corpus run not wired yet — see _from_corpus_run docstring.
        snapshot = _from_corpus_run()

    write_snapshot(snapshot)
    print(json.dumps(snapshot["headline"], indent=2))


if __name__ == "__main__":
    sys.exit(main())
