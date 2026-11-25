#!/bin/sh
# One-time setup per clone: point git at the versioned hooks in .githooks/.
#   sh scripts/security/install-hooks.sh

set -eu
root=$(git rev-parse --show-toplevel)
. "$root/scripts/security/lib.sh"

find_gitleaks
echo "gitleaks: $("$GITLEAKS" version)"

git -C "$root" config core.hooksPath .githooks
chmod +x "$root/.githooks/pre-commit" "$root/.githooks/pre-push" 2>/dev/null || true

echo "hooks installed: pre-commit and pre-push now scan for secrets"
