#!/bin/sh
# Blocks sensitive files (.env, local database, keys, dumps, logs ...) from being tracked.
#
#   check-files.sh --staged        files in the next commit
#   check-files.sh --tree <rev>    every file in a commit (used before pushing)
#   check-files.sh --tracked       every tracked file (used in CI)
#
# Prints file names only, never file contents.

set -u
. "$(dirname "$0")/lib.sh"

case "${1:-}" in
  --staged) files=$(git diff --cached --name-only --diff-filter=ACMR) ;;
  --tree) files=$(git ls-tree -r --name-only "${2:?missing revision}") ;;
  --tracked) files=$(git ls-files) ;;
  *) echo "usage: $0 --staged | --tree <rev> | --tracked" >&2; exit 2 ;;
esac

found=$(printf '%s\n' "$files" | filter_sensitive)
if [ -n "$found" ]; then
  echo "Sensitive files must not be committed:" >&2
  printf '%s\n' "$found" | sed 's/^/  - /' >&2
  block "remove them with: git rm --cached <file>  (then check .gitignore)"
fi
echo "check-files: ok"
