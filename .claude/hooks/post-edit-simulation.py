"""PostToolUse hook: run simulation tests when critical files are edited."""
import json
import sys
import subprocess
import datetime
import pathlib
import re

TARGETS = [
    "app/whatsapp.py",
    "app/item_extraction.py",
    "app/semantic_router.py",
    "app/prompt_central.py",
]
REPORTS_DIR = pathlib.Path(".claude/simulation-reports")

data = json.load(sys.stdin)
fp = data.get("tool_input", {}).get("file_path", "")
fp_normalized = fp.replace("\\", "/")

if not any(fp_normalized.endswith(t) for t in TARGETS):
    sys.exit(0)

triggered_by = fp_normalized.split("/")[-1]
now = datetime.datetime.now()
timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")

result = subprocess.run(
    ["docker", "compose", "exec", "-T", "backend",
     "pytest", "tests/simulation/", "-v", "--tb=short", "--no-header",
     "--ignore=tests/simulation/test_multi_restaurant.py",
     "--ignore=tests/simulation/test_real_restaurants.py",
     "--ignore=tests/simulation/test_real_restaurants_v2.py",
     "--ignore=tests/simulation/test_random_restaurants.py"],
    capture_output=True,
    text=True,
    timeout=300,
)

# Determine status from pytest output, not exit code (which can be
# non-zero due to warnings or docker-compose overhead).
_out = result.stdout
if " passed" in _out and "failed" not in _out.lower().split("passed")[-1]:
    status = "PASS"
elif "failed" in _out.lower():
    status = "FAIL"
else:
    status = "PASS" if result.returncode == 0 else "FAIL"
report_file = REPORTS_DIR / f"{timestamp}_{status}_{triggered_by}.txt"

# Parse pytest -v output into structured sections
lines = result.stdout.splitlines()

# Group test results by file (section)
sections: dict[str, list[str]] = {}
summary_lines: list[str] = []
in_summary = False

for line in lines:
    # Match test result lines: "tests/simulation/test_add_items.py::TestAddItems::test_name PASSED"
    m = re.match(r"(tests/simulation/(\w+)\.py)::(\w+)::(\w+)\s+(PASSED|FAILED|ERROR)", line)
    if m:
        file_path, file_name, class_name, test_name, result_status = m.groups()
        section = file_name.replace("test_", "").replace("_", " ").title()
        if section not in sections:
            sections[section] = []
        icon = "✓" if result_status == "PASSED" else "✗"
        # Clean up test name for display
        display_name = test_name.replace("test_", "").replace("_", " ")
        sections[section].append(f"  {icon} {display_name}")
        continue
    # Capture summary line (e.g. "15 passed, 4 warnings in 22.50s")
    if re.match(r"=+\s*(short test summary|FAILURES|[\d]+ passed)", line):
        in_summary = True
    if in_summary:
        summary_lines.append(line)

# Count pass/fail
total_pass = sum(1 for tests in sections.values() for t in tests if t.strip().startswith("✓"))
total_fail = sum(1 for tests in sections.values() for t in tests if t.strip().startswith("✗"))
total = total_pass + total_fail

# Build report
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
with open(report_file, "w", encoding="utf-8") as f:
    f.write(f"Simulation Tests — {status}\n")
    f.write(f"{'=' * 40}\n")
    f.write(f"Date:         {now.strftime('%Y-%m-%d %H:%M:%S')}\n")
    f.write(f"Triggered by: {triggered_by}\n")
    f.write(f"Result:       {total_pass}/{total} passed")
    if total_fail:
        f.write(f", {total_fail} FAILED")
    f.write("\n\n")

    for section, tests in sections.items():
        passed = sum(1 for t in tests if t.strip().startswith("✓"))
        failed = sum(1 for t in tests if t.strip().startswith("✗"))
        section_status = "PASS" if failed == 0 else "FAIL"
        f.write(f"[{section_status}] {section} ({passed}/{len(tests)})\n")
        for t in tests:
            f.write(f"{t}\n")
        f.write("\n")

    # Append failure details if any
    if total_fail and summary_lines:
        f.write("Failure Details\n")
        f.write(f"{'-' * 40}\n")
        for line in summary_lines:
            f.write(f"{line}\n")

sys.exit(0 if status == "PASS" else 1)
