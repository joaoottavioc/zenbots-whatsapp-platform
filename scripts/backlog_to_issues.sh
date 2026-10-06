#!/usr/bin/env bash
# Turn open items from the local tech_debt/ backlogs into GitHub issues.
#
# DRY RUN BY DEFAULT — prints what it would create. Pass --apply to create.
#
# Why the backlog is local: .gitignore excludes tech_debt/ and plan/ because
# this repo is public and those files enumerate fixed CVEs and strategy. So
# this script is deliberately conservative about what it publishes:
#   - Only files in ALLOWLIST are read (no vulnerabilities, pricing, plan/*).
#   - Issue bodies carry the title, the source file and a one-line summary
#     (first sentence, max 200 chars) — never the full backlog text.
#   - Struck-through items (~~...~~) and duplicate titles are skipped.
#   - Titles that already exist as issues (open or closed) are skipped.
#
# Labels:
#   area:infra      backlog_deploy, prod_readiness_* ; area:backend otherwise
#   priority:P0/P1/P2  from the nearest heading mentioning P0/P1/P2/P3 (P3 -> P2,
#                      default P2). prod_readiness "## P0-n —" headings are items.
#   agent-ready     only P1/P2 items from backlog_tests / backlog_architecture;
#                   everything else waits for human triage.
#
# Usage:
#   scripts/backlog_to_issues.sh                     # dry run, ./tech_debt
#   scripts/backlog_to_issues.sh --apply             # create issues
#   scripts/backlog_to_issues.sh --dir path/to/tech_debt --repo owner/name
#   scripts/backlog_to_issues.sh --limit 5 --apply   # create at most 5
set -euo pipefail

APPLY=0
BACKLOG_DIR="tech_debt"
REPO=""
LIMIT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --dir) BACKLOG_DIR="$2"; shift ;;
    --repo) REPO="$2"; shift ;;
    --limit) LIMIT="$2"; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

ALLOWLIST=(
  backlog_architecture.md
  backlog_tests.md
  backlog_production.md
  prod_readiness_2026-09.md
  backlog_mvp.md
  backlog_bot_intelligence.md
  backlog_soft_delete.md
  backlog_deploy.md
)

AGENT_READY_FILES=" backlog_tests.md backlog_architecture.md "
INFRA_FILES=" backlog_deploy.md prod_readiness_2026-09.md "

REPO_ARGS=()
[[ -n "$REPO" ]] && REPO_ARGS=(--repo "$REPO")

if [[ ! -d "$BACKLOG_DIR" ]]; then
  echo "Backlog dir '$BACKLOG_DIR' not found (it is gitignored — run from your main checkout)." >&2
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI not found." >&2
  exit 1
fi

# parse_file FILE -> TSV lines: priority<TAB>title<TAB>summary
parse_file() {
  local fixed=""
  # This file uses P0/P1/P2 as *phase* names, not priorities.
  [[ "$(basename "$1")" == "backlog_bot_intelligence.md" ]] && fixed="P2"
  awk -v fixed="$fixed" '
    function trim(s) { gsub(/^[ \t]+|[ \t]+$/, "", s); return s }
    function clean(s) {
      gsub(/\*\*|`|~~/, "", s)
      gsub(/\t/, " ", s)
      return trim(s)
    }
    function first_sentence(s,   i) {
      s = clean(s)
      sub(/^[—–:-]+ */, "", s)
      i = index(s, ". ")
      if (i > 0) s = substr(s, 1, i)
      if (length(s) > 200) s = substr(s, 1, 197) "..."
      return s
    }
    BEGIN { prio = (fixed != "" ? fixed : "P2") }
    /^#/ {
      line = $0
      # prod_readiness style: "## P0-1 — Title" is itself an item.
      if (match(line, /^##+ +P[0-2]-[0-9]+ *[—–-] */)) {
        p = substr(line, index(line, "P"), 2)
        title = clean(substr(line, RSTART + RLENGTH))
        print p "\t" title "\t" ""
        next
      }
      # Only "## P1 — ..." / "## 2. Blockers (P0)" style headings set the
      # priority; a P-number mentioned mid-sentence does not.
      if (fixed != "") next
      h = line
      sub(/^#+ +/, "", h)
      sub(/^[0-9]+\. +/, "", h)
      tag = ""
      if (match(h, /^P[0-3]([^.0-9A-Za-z]|$)/)) tag = substr(h, 2, 1)
      else if (match(h, /\(P[0-3]\) *$/)) tag = substr(h, RSTART + 2, 1)
      if (tag == "0") prio = "P0"
      else if (tag == "1") prio = "P1"
      else if (tag == "2" || tag == "3") prio = "P2"
      next
    }
    /^- \[ \] / {
      rest = substr($0, 7)
      if (rest ~ /^~~/) next
      if (match(rest, /^\*\*[^*]+\*\*/)) {
        title = clean(substr(rest, 3, RLENGTH - 4))
        summary = first_sentence(substr(rest, RLENGTH + 1))
      } else {
        title = clean(rest)
        summary = ""
      }
      sub(/[:.]$/, "", title)
      # backlog_tests lists coverage gaps by bare filename.
      if (title ~ /^[A-Za-z0-9_\/]+\.py$/) title = "Add test coverage for " title
      if (length(title) > 100) title = substr(title, 1, 97) "..."
      if (title == "") next
      print prio "\t" title "\t" summary
    }
  ' "$1"
}

ensure_labels() {
  local specs=(
    "area:backend|1d76db|Backend (app/, tests/)"
    "area:infra|5319e7|Infrastructure, deploy, AWS"
    "priority:P0|b60205|Blocker"
    "priority:P1|d93f0b|High priority"
    "priority:P2|fbca04|Normal priority"
    "agent-ready|0e8a16|Scoped and safe for an AI agent to pick up"
  )
  for spec in "${specs[@]}"; do
    IFS='|' read -r name color desc <<<"$spec"
    if [[ $APPLY -eq 1 ]]; then
      gh label create "$name" --color "$color" --description "$desc" --force "${REPO_ARGS[@]}" >/dev/null
    else
      echo "[dry-run] gh label create \"$name\" --color $color --force"
    fi
  done
}

issue_exists() {
  local title="$1"
  local found
  found=$(gh issue list "${REPO_ARGS[@]}" --state all --limit 20 \
    --search "\"${title//\"/}\" in:title" --json title --jq '.[].title' 2>/dev/null || true)
  grep -Fxq -- "$title" <<<"$found"
}

ensure_labels

declare -A SEEN=()
created=0
skipped=0
total=0

for file in "${ALLOWLIST[@]}"; do
  path="$BACKLOG_DIR/$file"
  [[ -f "$path" ]] || { echo "skip: $path not found" >&2; continue; }

  area="area:backend"
  [[ "$INFRA_FILES" == *" $file "* ]] && area="area:infra"

  while IFS=$'\t' read -r prio title summary; do
    key=$(tr '[:upper:]' '[:lower:]' <<<"$title")
    if [[ -n "${SEEN[$key]:-}" ]]; then
      continue
    fi
    SEEN[$key]=1
    total=$((total + 1))

    labels="$area,priority:$prio"
    if [[ "$AGENT_READY_FILES" == *" $file "* && "$prio" != "P0" ]]; then
      labels="$labels,agent-ready"
    fi

    body="$title"$'\n\n'
    [[ -n "$summary" ]] && body+="$summary"$'\n\n'
    body+="Source: \`tech_debt/$file\` (local backlog, not in the public repo)."

    if [[ $APPLY -eq 0 ]]; then
      printf '[dry-run] %-11s %-40s %s\n' "$prio" "$labels" "$title"
      continue
    fi

    if [[ $LIMIT -gt 0 && $created -ge $LIMIT ]]; then
      break 2
    fi

    if issue_exists "$title"; then
      echo "exists:  $title"
      skipped=$((skipped + 1))
      continue
    fi

    gh issue create "${REPO_ARGS[@]}" --title "$title" --body "$body" --label "$labels" >/dev/null
    echo "created: $title"
    created=$((created + 1))
  done < <(parse_file "$path")
done

if [[ $APPLY -eq 0 ]]; then
  echo
  echo "Dry run: $total issues would be created (minus any that already exist)."
  echo "Re-run with --apply to create them (optionally --limit N first)."
else
  echo
  echo "Created $created, skipped $skipped existing."
fi
