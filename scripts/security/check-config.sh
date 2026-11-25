#!/bin/sh
# Validates the security setup itself: .gitignore still covers secrets and the gitleaks
# config is present.

set -u
. "$(dirname "$0")/lib.sh"

root=$(git rev-parse --show-toplevel)
cd "$root" || block "cannot enter repository root"

[ -f .gitleaks.toml ] || block ".gitleaks.toml is missing"
[ -f .gitignore ] || block ".gitignore is missing"

for entry in '.env' '.devdb/' 'rules/' 'private/' '.gitleaks.local.toml' '*.pem' '*.key' '*.log'; do
  grep -qxF "$entry" .gitignore || block ".gitignore no longer ignores $entry"
done

# An ignore file would silently hide findings; it needs a deliberate review.
[ -e .gitleaksignore ] && block ".gitleaksignore exists; remove it or get it reviewed by the owner"

echo "check-config: ok"
