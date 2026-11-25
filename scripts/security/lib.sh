#!/bin/sh
# Shared helpers for the security hooks and CI. POSIX sh (works in Git Bash on Windows).

block() {
  echo "" >&2
  echo "  BLOCKED: $1" >&2
  echo "  See SECURITY.md. Do not bypass this check (no --no-verify)." >&2
  echo "" >&2
  exit 1
}

# Find gitleaks on PATH, or where winget installs it on Windows. Fail closed if missing.
find_gitleaks() {
  if command -v gitleaks >/dev/null 2>&1; then
    GITLEAKS=gitleaks
    return 0
  fi
  if [ -n "${LOCALAPPDATA:-}" ] && command -v cygpath >/dev/null 2>&1; then
    for candidate in "$(cygpath -u "$LOCALAPPDATA")"/Microsoft/WinGet/Packages/Gitleaks.Gitleaks_*/gitleaks.exe; do
      if [ -x "$candidate" ]; then
        GITLEAKS="$candidate"
        return 0
      fi
    done
  fi
  block "gitleaks is not installed, so secrets cannot be checked"
}

# Files that must never be tracked, as an extended regex over repo-relative paths.
SENSITIVE_PATHS='(^|/)private/|(^|/)backups/|(^|/)\.gitleaks\.local\.toml$|(^|/)\.env$|(^|/)\.env\.|(^|/)[^/]*\.env$|(^|/)\.devdb/|(^|/)rules/|(^|/)secrets/|(^|/)\.claude/|\.(pem|key|crt|p12|pfx|jks|db|sqlite|sqlite3|dump|backup|log)$|\.sql\.gz$|(^|/)id_(rsa|ed25519)|credentials[^/]*\.json$|service-account[^/]*\.json$|(^|/)(docker-)?compose\.override\.ya?ml$'

# Reads paths on stdin, prints the sensitive ones. No exceptions: every .env* file is blocked.
filter_sensitive() {
  grep -E "$SENSITIVE_PATHS" || true
}
