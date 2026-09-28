"""Turn a corpus QA run into a report, a verdict, and improvement proposals.

Reads the JSON written by tests/simulation/corpus/run_corpus_qa.py, the pytest
log of that run, usage_events (cost) from DATABASE_URL, and the CI baseline in
eval/ci_corpus_baseline.json. Writes to --out-dir:

  summary.json  headline numbers for this run (the input to --write-baseline)
  report.md     GitHub issue body
  verdict.txt   pass | regression | no-baseline | failed-run

Every number in the report is computed here. The optional Claude step
(--proposals, needs ANTHROPIC_API_KEY) writes only the "Proposals" section.

    python -m scripts.corpus_qa_report --report run.json --pytest-log corpus.log \
        --out-dir corpus-qa [--proposals] [--write-baseline]

    # Promote a CI run to the baseline (summary.json from the run's artifact):
    python -m scripts.corpus_qa_report --promote corpus-qa/summary.json

Acceptance rule (CLAUDE.md, "Conversation quality loop"): a run passes when
score >= baseline score - noise_pp AND cost per conversation <= baseline x 1.10.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CI_BASELINE = ROOT / "eval" / "ci_corpus_baseline.json"
PUBLIC_BASELINE = ROOT / "eval" / "latest.json"
COST_TOLERANCE = 1.10
DEFAULT_NOISE_PP = 5.0

# Failure modes, keyed by corpus scenario (see _SCENARIO_MAP in
# run_corpus_qa.py). Documented in CLAUDE.md; edit both together.
# F8 is not a scenario: it is "failed in some samples and passed in others",
# only detectable when the run used --samples > 1.
FAILURE_MODES: dict[str, tuple[str, set[str]]] = {
    "F7": (
        "Product matching (wrong/missing item or quantity when adding)",
        {"add_single", "add_multi", "abbreviation", "digit_in_name", "double_add"},
    ),
    "F9": (
        "Checkout and multi-turn state (flow broken, cart wiped, state lost)",
        {"checkout", "multiturn_flow", "trap_finish", "continuation"},
    ),
    "F10": (
        "Unavailable-item handling (substitution or wrong 'em falta')",
        {"unavailable"},
    ),
}
F8_LABEL = "Flaky (passed in some samples, failed in others)"
OTHER_LABEL = "Other (cart edits, suggestions, conversational)"

# Files the proposals may target (the conversation pipeline).
PROPOSAL_FILES = [
    "app/semantic_router.py",
    "app/prompt_central.py",
    "app/item_extraction.py",
]
PROPOSAL_CONTEXT_FILES = PROPOSAL_FILES + ["app/tools_definition.py"]
PROPOSAL_MODEL = "claude-opus-5"


def mode_for(scenario: str) -> str:
    for code, (_, scenarios) in FAILURE_MODES.items():
        if scenario in scenarios:
            return code
    return "Other"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def parse_failure_excerpts(log_text: str) -> dict[str, list[str]]:
    """Map scenario -> first assertion line of each failing test.

    Relies on pytest's `--tb=short` section headers:
    `____ TestRandAddSingle.test_add_single[Restaurant] ____`.
    """
    from tests.simulation.corpus.run_corpus_qa import _SCENARIO_MAP

    excerpts: dict[str, list[str]] = defaultdict(list)
    current: str | None = None
    taken = False
    for line in log_text.splitlines():
        # pytest pads headers to the terminal width, so a long test id gets a
        # single underscore on each side.
        m = re.match(r"^_+ (Test\w+)\.test_\w+\[(.*)\] _+$", line)
        if m:
            current = _SCENARIO_MAP.get(m.group(1))
            restaurant = m.group(2)
            taken = False
            continue
        if current and not taken and line.startswith("E "):
            text = line[1:].strip()
            if len(text) > 220:
                text = text[:217] + "..."
            excerpts[current].append(f"{restaurant}: {text}")
            taken = True
    return excerpts


async def query_cost(started_at: str, finished_at: str) -> dict:
    """Sum usage_events cost in the run window (CI DB holds only this run)."""
    from sqlalchemy import text

    from app.database import engine

    def naive_utc(ts: str) -> datetime:
        return datetime.fromisoformat(ts).astimezone(timezone.utc).replace(tzinfo=None)

    start = naive_utc(started_at)
    # Grace period: the last test's events are flushed during teardown.
    end = naive_utc(finished_at) + timedelta(minutes=2)
    sql = text(
        """
        SELECT service, operation, COUNT(*) AS calls,
               COALESCE(SUM(cost_usd), 0) AS cost,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens
        FROM usage_events
        WHERE created_at >= :start
          AND created_at <= :end
        GROUP BY service, operation
        ORDER BY cost DESC
        """
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(sql, {"start": start, "end": end})).mappings().all()
    await engine.dispose()
    by_op = [dict(r) for r in rows]
    return {
        "total_usd": float(sum(r["cost"] for r in by_op)),
        "llm_calls": int(sum(r["calls"] for r in by_op if r["service"] == "openai")),
        "input_tokens": int(sum(r["input_tokens"] for r in by_op)),
        "output_tokens": int(sum(r["output_tokens"] for r in by_op)),
        "by_operation": [
            {**r, "cost": float(r["cost"]), "calls": int(r["calls"])} for r in by_op
        ],
    }


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def group_failures(report: dict) -> dict[str, dict]:
    groups: dict[str, dict] = {}

    def add(code: str, label: str, scenario: str, restaurant: str) -> None:
        g = groups.setdefault(code, {"label": label, "count": 0, "scenarios": {}})
        g["count"] += 1
        g["scenarios"].setdefault(scenario, []).append(restaurant)

    for rest in report.get("restaurants", []):
        for scenario, cell in (rest.get("scenarios") or {}).items():
            passed = cell.get("passed_count", 0)
            total = cell.get("total_count", 0)
            if total == 0 or cell.get("status") == "skipped" or passed == total:
                continue
            if 0 < passed < total:
                add("F8", F8_LABEL, scenario, rest["name"])
                continue
            code = mode_for(scenario)
            label = FAILURE_MODES[code][0] if code in FAILURE_MODES else OTHER_LABEL
            add(code, label, scenario, rest["name"])
    order = ["F7", "F8", "F9", "F10", "Other"]
    return {k: groups[k] for k in order if k in groups}


def build_summary(report: dict, cost: dict | None) -> dict:
    conversations = int(report.get("total_tests", 0)) - int(report.get("skipped", 0))
    cpc = None
    if cost is not None and conversations > 0:
        cpc = cost["total_usd"] / conversations
    return {
        "measured_at": report.get("finished_at"),
        "score": report.get("comprehension_pass_rate"),
        "pass_rate": report.get("pass_rate"),
        "passed": report.get("passed"),
        "failed": report.get("failed"),
        "skipped": report.get("skipped"),
        "conversations": conversations,
        "restaurants": len(report.get("restaurants", [])),
        "samples": report.get("samples_per_restaurant", 1),
        "population": report.get("samples_by_tier", {}),
        "integrity": report.get("integrity"),
        "cost_total_usd": None if cost is None else round(cost["total_usd"], 6),
        "cost_per_conversation_usd": None if cpc is None else round(cpc, 7),
        "llm_calls": None if cost is None else cost["llm_calls"],
        "git_sha": os.getenv("GITHUB_SHA", "")[:8] or None,
        "ref": os.getenv("GITHUB_REF_NAME") or None,
        "run_url": run_url(),
    }


def run_url() -> str | None:
    server, repo, run_id = (
        os.getenv("GITHUB_SERVER_URL"),
        os.getenv("GITHUB_REPOSITORY"),
        os.getenv("GITHUB_RUN_ID"),
    )
    return (
        f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else None
    )


def verdict(summary: dict, baseline: dict | None) -> tuple[str, list[str]]:
    """Apply the acceptance rule. Returns (verdict, reasons)."""
    if summary["score"] is None or summary["conversations"] == 0:
        return "failed-run", ["The run produced no scored conversations."]
    if not baseline:
        return "no-baseline", [
            "No CI baseline yet (eval/ci_corpus_baseline.json). Commit this run's "
            "summary as the baseline: `python -m scripts.corpus_qa_report ... "
            "--write-baseline`."
        ]
    reasons = []
    noise = float(baseline.get("noise_pp", DEFAULT_NOISE_PP))
    floor = float(baseline["score"]) - noise
    if summary["score"] < floor:
        reasons.append(
            f"Score {summary['score']:.1f}% is below baseline "
            f"{baseline['score']:.1f}% - {noise:.0f}pp noise band = {floor:.1f}%."
        )
    base_cpc = baseline.get("cost_per_conversation_usd")
    cpc = summary.get("cost_per_conversation_usd")
    if base_cpc and cpc is not None and cpc > base_cpc * COST_TOLERANCE:
        reasons.append(
            f"Cost per conversation ${cpc:.6f} is {cpc / base_cpc - 1:+.0%} vs "
            f"baseline ${base_cpc:.6f} (limit +{COST_TOLERANCE - 1:.0%})."
        )
    return ("regression" if reasons else "pass"), reasons


# ---------------------------------------------------------------------------
# Proposals (Claude)
# ---------------------------------------------------------------------------


def generate_proposals(summary: dict, groups: dict, excerpts: dict) -> str:
    """One Messages API call; returns Markdown. Raises on API failure."""
    import anthropic

    sources = "\n\n".join(
        f'<file path="{p}">\n{(ROOT / p).read_text(encoding="utf-8")}\n</file>'
        for p in PROPOSAL_CONTEXT_FILES
    )
    evidence = {
        "summary": {
            k: summary[k]
            for k in ("score", "conversations", "restaurants", "samples", "population")
        },
        "failure_modes": {
            code: {
                "label": g["label"],
                "count": g["count"],
                "scenarios": g["scenarios"],
            }
            for code, g in groups.items()
        },
        "assertion_excerpts": {k: v[:6] for k, v in excerpts.items()},
    }
    system = (
        "You improve ZenBotZ, a Portuguese-language WhatsApp ordering bot for "
        "Brazilian restaurants. A weekly QA run simulates customer conversations "
        "against real restaurant menus and records which scenarios fail. You "
        "propose code changes that would fix the observed failures."
    )
    prompt = f"""Here is this week's QA evidence (JSON), then the source of the
conversation pipeline.

<evidence>
{json.dumps(evidence, ensure_ascii=False, indent=2)}
</evidence>

{sources}

Write 3 to 5 proposals, ordered by expected impact. Each must target exactly one
of: {", ".join(PROPOSAL_FILES)}. For each proposal use this Markdown shape:

### <n>. <short title>
- **File:** `<path>` (function or section)
- **Failure mode:** <F-code> — cite the scenarios and 1-2 excerpts above that motivate it
- **Change:** what to change, concretely enough for an engineer to implement
- **Test:** a test that fails today and passes after the change (file, name,
  input message, expected cart/reply); prefer a unit test, otherwise a
  tests/simulation case
- **Risk / cost:** what could regress, and whether it adds LLM calls or tokens

Rules: ground every proposal in the evidence; if the evidence is too thin for a
claim, say so instead of guessing. Do not restate the metrics. Prefer
deterministic fixes (routing rules, matching, prompt examples) over adding LLM
calls, because cost per conversation must not grow more than 10%. Output only
the proposals."""

    client = anthropic.Anthropic()
    # Streaming: long input + thinking can outlast a plain request's timeout.
    # fallbacks="default": if the model declines, the API re-runs the request
    # on a fallback model inside the same call.
    with client.beta.messages.stream(
        model=PROPOSAL_MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=system,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    text_out = "".join(b.text for b in message.content if b.type == "text").strip()
    if message.stop_reason == "max_tokens":
        text_out += "\n\n_(truncated: hit max_tokens)_"
    if not text_out:
        raise RuntimeError(f"empty response (stop_reason={message.stop_reason})")
    return text_out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def fmt_usd(v: float | None, digits: int = 6) -> str:
    return "n/a" if v is None else f"${v:.{digits}f}"


def render(
    summary: dict,
    baseline: dict | None,
    public: dict | None,
    verdict_value: str,
    reasons: list[str],
    groups: dict,
    excerpts: dict,
    cost: dict | None,
    proposals: str,
) -> str:
    icon = {"pass": "✅", "regression": "❌", "no-baseline": "⚪", "failed-run": "⚠️"}
    out = [
        f"**Verdict: {icon.get(verdict_value, '')} {verdict_value}**",
        "",
    ]
    out += [f"- {r}" for r in reasons] + ([""] if reasons else [])

    b_score = baseline.get("score") if baseline else None
    b_cpc = baseline.get("cost_per_conversation_usd") if baseline else None
    d_score = (
        f"{summary['score'] - b_score:+.1f}pp"
        if b_score is not None and summary["score"] is not None
        else "—"
    )
    cpc = summary["cost_per_conversation_usd"]
    d_cpc = f"{cpc / b_cpc - 1:+.0%}" if b_cpc and cpc is not None else "—"
    out += [
        "| Metric | This run | CI baseline | Δ |",
        "|---|---|---|---|",
        f"| Comprehension score | {summary['score']}% | "
        f"{'—' if b_score is None else f'{b_score}%'} | {d_score} |",
        f"| Cost per conversation | {fmt_usd(cpc, 7)} | {fmt_usd(b_cpc, 7)} | {d_cpc} |",
        f"| Conversations | {summary['conversations']} "
        f"({summary['restaurants']} restaurants × {summary['samples']} sample) | "
        f"{baseline.get('conversations', '—') if baseline else '—'} | |",
        f"| Passed / failed / skipped | {summary['passed']} / {summary['failed']} / "
        f"{summary['skipped']} | | |",
        f"| LLM calls / total cost | {summary['llm_calls']} / "
        f"{fmt_usd(summary['cost_total_usd'])} | | |",
        "",
        f"Population: `{json.dumps(summary['population'])}` "
        f"(integrity: {summary['integrity']}).",
    ]
    if public:
        head = public.get("headline", {})
        out.append(
            f"For reference only: the public eval (`eval/latest.json`) scores "
            f"{head.get('pass_rate_pct')}% over {public.get('methodology', {}).get('corpus_size')} "
            "menus including real-restaurant menus, which CI does not have. It is a "
            "different population and is not used for the verdict."
        )
    out += ["", "## Failures by mode", ""]
    if not groups:
        out.append("No failures. 🎉")
    for code, g in groups.items():
        out.append(f"### {code} — {g['label']} ({g['count']})")
        for scenario, rests in sorted(
            g["scenarios"].items(), key=lambda kv: -len(kv[1])
        ):
            out.append(f"- `{scenario}` ×{len(rests)}: {', '.join(sorted(set(rests)))}")
            for ex in excerpts.get(scenario, [])[:3]:
                out.append(f"  - {ex}")
        out.append("")
    if cost and cost["by_operation"]:
        out += [
            "<details><summary>Cost by operation</summary>",
            "",
            "| service | operation | calls | cost |",
            "|---|---|---|---|",
        ]
        out += [
            f"| {r['service']} | {r['operation']} | {r['calls']} | {fmt_usd(r['cost'])} |"
            for r in cost["by_operation"]
        ]
        out += ["", "</details>", ""]
    out += ["## Proposals", "", proposals, ""]
    footer = [
        f"Commit {summary['git_sha']}" if summary["git_sha"] else None,
        f"ref `{summary['ref']}`" if summary["ref"] else None,
        f"[workflow run]({summary['run_url']})" if summary["run_url"] else None,
    ]
    out.append("<sub>" + " · ".join(f for f in footer if f) + "</sub>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--report", type=Path)
    ap.add_argument("--pytest-log", type=Path)
    ap.add_argument("--baseline", type=Path, default=CI_BASELINE)
    ap.add_argument("--public-baseline", type=Path, default=PUBLIC_BASELINE)
    ap.add_argument("--out-dir", type=Path)
    ap.add_argument(
        "--promote",
        type=Path,
        metavar="SUMMARY_JSON",
        help="write a run's summary.json (CI artifact) to --baseline and exit",
    )
    ap.add_argument("--proposals", action="store_true", help="ask Claude for proposals")
    ap.add_argument(
        "--no-cost", action="store_true", help="skip the usage_events query"
    )
    ap.add_argument(
        "--write-baseline", action="store_true", help="write this run to --baseline"
    )
    ap.add_argument("--noise-pp", type=float, default=DEFAULT_NOISE_PP)
    args = ap.parse_args()

    if args.promote:
        summary = load_json(args.promote)
        if not summary or summary.get("score") is None:
            sys.exit(f"{args.promote} is not a valid summary.json")
        if summary.get("cost_per_conversation_usd") is None:
            sys.exit("Refusing to promote a run without cost data.")
        args.baseline.write_text(
            json.dumps({**summary, "noise_pp": args.noise_pp}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"Baseline written to {args.baseline}: score={summary['score']} "
            f"cost_per_conversation={summary['cost_per_conversation_usd']}"
        )
        return 0

    if not args.report or not args.out_dir:
        ap.error("--report and --out-dir are required (unless --promote)")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    report = load_json(args.report)
    if report is None:
        print(f"Report not found or invalid: {args.report}", file=sys.stderr)
        (args.out_dir / "verdict.txt").write_text("failed-run\n", encoding="utf-8")
        return 1

    cost = None
    if not args.no_cost:
        try:
            cost = asyncio.run(query_cost(report["started_at"], report["finished_at"]))
        except Exception as e:  # report still useful without cost
            print(f"WARNING: cost query failed: {e}", file=sys.stderr)

    log_text = ""
    if args.pytest_log and args.pytest_log.exists():
        log_text = args.pytest_log.read_text(encoding="utf-8", errors="replace")
    excerpts = parse_failure_excerpts(log_text) if log_text else {}

    summary = build_summary(report, cost)
    baseline = load_json(args.baseline)
    public = load_json(args.public_baseline)
    verdict_value, reasons = verdict(summary, baseline)
    groups = group_failures(report)

    proposals = "_Proposals were not generated for this run._"
    if args.proposals and groups:
        if not os.getenv("ANTHROPIC_API_KEY"):
            proposals = "_Proposals skipped: ANTHROPIC_API_KEY is not set._"
        else:
            try:
                proposals = generate_proposals(summary, groups, excerpts)
            except Exception as e:
                proposals = f"_Proposal generation failed ({type(e).__name__}: {e})._"
    elif args.proposals:
        proposals = "_No failures, so no proposals._"

    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    (args.out_dir / "verdict.txt").write_text(verdict_value + "\n", encoding="utf-8")
    (args.out_dir / "report.md").write_text(
        render(
            summary,
            baseline,
            public,
            verdict_value,
            reasons,
            groups,
            excerpts,
            cost,
            proposals,
        ),
        encoding="utf-8",
    )

    if args.write_baseline:
        if summary["cost_per_conversation_usd"] is None:
            print("Refusing to write a baseline without cost data.", file=sys.stderr)
            return 1
        args.baseline.write_text(
            json.dumps({**summary, "noise_pp": args.noise_pp}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Baseline written to {args.baseline}")

    print(
        f"verdict={verdict_value} score={summary['score']} "
        f"cost_per_conversation={summary['cost_per_conversation_usd']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
