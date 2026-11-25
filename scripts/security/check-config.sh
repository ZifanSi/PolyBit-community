#!/bin/sh
# Validates the security setup itself: .gitignore still covers secrets, the env template
# holds no real values, and the gitleaks config is present.

set -u
. "$(dirname "$0")/lib.sh"

root=$(git rev-parse --show-toplevel)
cd "$root" || block "cannot enter repository root"

[ -f .gitleaks.toml ] || block ".gitleaks.toml is missing"
[ -f .gitignore ] || block ".gitignore is missing"

for entry in '.env' '.devdb/' 'rules/' '*.pem' '*.key' '*.log'; do
  grep -qxF "$entry" .gitignore || block ".gitignore no longer ignores $entry"
done

# Every secret key in the template must be empty or an obvious placeholder.
if [ -f .env.example ]; then
  for key in PB_DISCORD_TOKEN PB_DB_PASSWORD PB_ADMIN_IDS PB_DEV_GUILD_ID; do
    value=$(grep -E "^$key=" .env.example | head -n 1 | cut -d= -f2-)
    case "$value" in
      "" | CHANGE_ME) ;;
      *) block ".env.example must not contain a real value for $key" ;;
    esac
  done
fi

# An ignore file would silently hide findings; it needs a deliberate review.
[ -e .gitleaksignore ] && block ".gitleaksignore exists; remove it or get it reviewed by the owner"

echo "check-config: ok"
