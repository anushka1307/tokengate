#!/usr/bin/env bash
# Fail if a commit message (or PR description) credits an AI assistant.
#
#   scripts/check_attribution.sh MESSAGE_FILE [MESSAGE_FILE...]
#
# Used by the pre-commit commit-msg hook (locally) and the CI "attribution" job (for every
# commit in a PR and the PR description). Commits in this repo are authored by the owner only.
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "usage: $0 MESSAGE_FILE..." >&2
  exit 2
fi

# Case-insensitive. "claude" as a whole word also catches a co-author name or a mention of
# the assistant's config file; "generated with" catches tool footers.
pattern='co-authored-by|\bclaude\b|anthropic|generated with'

status=0
for file in "$@"; do
  # Ignore git's comment lines and everything below the "git commit -v" scissors line.
  hits=$(sed -e '/^# -\{24\} >8 -\{24\}$/,$d' -e '/^#/d' "$file" | grep -niE "$pattern" || true)
  if [[ -n "$hits" ]]; then
    echo "AI attribution is not allowed in commit messages or PR descriptions:" >&2
    sed 's/^/    line /' <<<"$hits" >&2
    status=1
  fi
done
exit "$status"
