"""PostToolUse hook: run simulation tests when critical files are edited.

Prefers `docker compose exec backend pytest` (the dev stack). When Docker or
the backend container isn't available (CI, headless/cloud agents), it falls
back to running pytest directly with the current interpreter. If the DB is
unreachable there, the simulation conftest skips every test and the report
says SKIP.

Exit codes: 1 only when tests actually FAIL. Anything else — no Docker, no
DB, timeout, bad hook input, unexpected error — writes a report (when
possible) and exits 0 so the agent's edit loop is never blocked by tooling.
"""

import datetime
import json
import pathlib
import re
import shutil
import subprocess
import sys

TARGETS = [
    "app/whatsapp.py",
    "app/item_extraction.py",
    "app/semantic_router.py",
    "app/prompt_central.py",
]
REPORTS_DIR = pathlib.Path(".claude/simulation-reports")
TIMEOUT_S = 300

PYTEST_ARGS = [
    "tests/simulation/",
    "-v",
    "--tb=short",
    "--no-header",
    "--ignore=tests/simulation/test_multi_restaurant.py",
    "--ignore=tests/simulation/test_real_restaurants.py",
    "--ignore=tests/simulation/test_real_restaurants_v2.py",
    "--ignore=tests/simulation/test_random_restaurants.py",
]


def _docker_backend_running() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        ps = subprocess.run(
            ["docker", "compose", "ps", "--status", "running", "--services"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return ps.returncode == 0 and "backend" in ps.stdout.split()


def _build_command() -> tuple[list[str], str]:
    if _docker_backend_running():
        return [
            "docker",
            "compose",
            "exec",
            "-T",
            "backend",
            "pytest",
            *PYTEST_ARGS,
        ], "docker"
    return [sys.executable, "-m", "pytest", *PYTEST_ARGS], "local"


def _count(pattern: str, text: str) -> int:
    m = re.search(rf"(\d+) {pattern}", text)
    return int(m.group(1)) if m else 0


def _status_from_output(out: str, returncode: int, runner: str) -> str:
    # Status comes from the pytest summary, not the exit code (docker compose
    # overhead and warnings can make the exit code unreliable).
    failed = _count("failed", out)
    errors = _count("errors?", out)
    passed = _count("passed", out)
    skipped = _count("skipped", out)
    if failed:
        return "FAIL"
    if errors:
        # Outside Docker, errors with nothing passing are almost always an
        # environment problem (missing deps/env vars at import), not a bug
        # the edit introduced.
        return "SKIP" if runner == "local" and not passed else "FAIL"
    if passed:
        return "PASS"
    if skipped or "no tests ran" in out:
        return "SKIP"
    if returncode == 0:
        return "PASS"
    # pytest exit codes 2-5 (interrupted, internal/usage error such as a
    # conftest ImportError, nothing collected) with no summary line mean the
    # suite never ran. Locally that's the environment, not the edit.
    if runner == "local" and returncode in (2, 3, 4, 5):
        return "SKIP"
    return "FAIL"


def _write_report(
    status: str, triggered_by: str, runner: str, now, stdout: str, note: str = ""
):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    report_file = REPORTS_DIR / f"{timestamp}_{status}_{triggered_by}.txt"

    sections: dict[str, list[str]] = {}
    summary_lines: list[str] = []
    in_summary = False
    for line in stdout.splitlines():
        # "tests/simulation/test_add_items.py::TestAddItems::test_name PASSED"
        m = re.match(
            r"(tests/simulation/(\w+)\.py)::(\w+)::(\w+)\s+(PASSED|FAILED|ERROR|SKIPPED)",
            line,
        )
        if m:
            _, file_name, _, test_name, result_status = m.groups()
            section = file_name.replace("test_", "").replace("_", " ").title()
            icon = {"PASSED": "✓", "SKIPPED": "-"}.get(result_status, "✗")
            display_name = test_name.replace("test_", "").replace("_", " ")
            sections.setdefault(section, []).append(f"  {icon} {display_name}")
            continue
        if re.match(
            r"=+\s*(short test summary|FAILURES|\d+ (passed|failed|skipped))", line
        ):
            in_summary = True
        if in_summary:
            summary_lines.append(line)

    def _n(icon: str) -> int:
        return sum(
            1 for ts in sections.values() for t in ts if t.strip().startswith(icon)
        )

    total_pass, total_fail, total_skip = _n("✓"), _n("✗"), _n("-")
    total = total_pass + total_fail + total_skip

    with open(report_file, "w", encoding="utf-8") as f:
        f.write(f"Simulation Tests — {status}\n")
        f.write(f"{'=' * 40}\n")
        f.write(f"Date:         {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Triggered by: {triggered_by}\n")
        f.write(f"Runner:       {runner}\n")
        f.write(f"Result:       {total_pass}/{total} passed")
        if total_fail:
            f.write(f", {total_fail} FAILED")
        if total_skip:
            f.write(f", {total_skip} skipped")
        f.write("\n")
        if note:
            f.write(f"Note:         {note}\n")
        f.write("\n")

        for section, tests in sections.items():
            failed = sum(1 for t in tests if t.strip().startswith("✗"))
            passed = sum(1 for t in tests if t.strip().startswith("✓"))
            section_status = "PASS" if failed == 0 else "FAIL"
            f.write(f"[{section_status}] {section} ({passed}/{len(tests)})\n")
            for t in tests:
                f.write(f"{t}\n")
            f.write("\n")

        if total_fail and summary_lines:
            f.write("Failure Details\n")
            f.write(f"{'-' * 40}\n")
            for line in summary_lines:
                f.write(f"{line}\n")


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    fp = (data.get("tool_input") or {}).get("file_path", "") or ""
    fp_normalized = fp.replace("\\", "/")
    if not any(fp_normalized.endswith(t) for t in TARGETS):
        return 0

    triggered_by = fp_normalized.split("/")[-1]
    now = datetime.datetime.now()
    cmd, runner = _build_command()

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        _write_report(
            "SKIP", triggered_by, runner, now, out, f"timed out after {TIMEOUT_S}s"
        )
        return 0
    except OSError as exc:
        _write_report(
            "SKIP", triggered_by, runner, now, "", f"could not start pytest: {exc}"
        )
        return 0

    status = _status_from_output(result.stdout, result.returncode, runner)
    note = ""
    if status == "SKIP":
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
        note = "tests did not run (DB/Redis/env unavailable) — start `docker compose up` to run them"
        if tail:
            note += f"; last output: {tail[0][:200]}"
    _write_report(status, triggered_by, runner, now, result.stdout, note)
    return 1 if status == "FAIL" else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # never block the edit loop on hook bugs
        print(f"post-edit-simulation hook error (ignored): {exc}", file=sys.stderr)
        sys.exit(0)
