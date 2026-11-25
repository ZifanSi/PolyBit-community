# Security

PolyBit handles a Discord bot token, Discord user and server IDs, and a database of
balances. None of that may ever reach git. The repo enforces this in two layers:

| Layer | When | What |
|---|---|---|
| `pre-commit` hook | before each commit | scans staged changes, blocks sensitive files |
| `pre-push` hook | before each push | scans every outgoing commit |
| GitHub Actions `security` | after each push / PR | full-history scan, tracked-file check |

The hooks are the real protection. CI runs only after code is already on GitHub.
Everything is **fail-closed**: if a check fails or can't run, the commit/push is blocked.

## Setup (once per clone)

1. Install [gitleaks](https://github.com/gitleaks/gitleaks) 8.30 or newer:
   - Windows: `winget install Gitleaks.Gitleaks`
   - macOS: `brew install gitleaks`
   - Linux: download a release binary and verify its checksum
2. Enable the hooks:
   ```sh
   sh scripts/security/install-hooks.sh
   ```
   This sets `git config core.hooksPath .githooks`.
3. Copy the env template and fill it in locally. It is gitignored:
   ```sh
   cp .env.example .env
   ```

## What gets blocked

- Discord bot tokens, real Discord user/server IDs (17–20 digit snowflakes)
- Database passwords, including passwords inside connection strings
- Filled-in `PB_DISCORD_TOKEN`, `PB_DB_PASSWORD`, `POSTGRES_PASSWORD` values
- Everything in gitleaks' default rule set (API keys, private keys, cloud credentials …)
- Sensitive files: `.env*` (except `.env.example`), `.devdb/`, `rules/`, keys and
  certificates, database dumps and backups, `*.log`, compose override files

Rules live in [`.gitleaks.toml`](.gitleaks.toml); blocked paths in
[`scripts/security/lib.sh`](scripts/security/lib.sh). Treat changes to either as
security changes.

## Run the checks by hand

```sh
gitleaks git --pre-commit --staged --redact .   # what you're about to commit
gitleaks git --redact .                          # the whole history
sh scripts/security/check-files.sh --tracked     # sensitive files in the repo
sh scripts/security/check-config.sh              # .gitignore / template sanity
```

Always use `--redact` so secret values never appear in your terminal or logs.

## If a check blocks you

- **A secret in your changes:** remove it, put it in `.env`, read it via an environment
  variable. Unstage the file if needed: `git reset <file>`.
- **A sensitive file staged:** `git rm --cached <file>` and make sure `.gitignore` covers it.
- **A false positive:** don't add `gitleaks:allow` comments or a `.gitleaksignore`
  (both are rejected). Fix the code or ask the owner to review a rule change.
- **Never** use `git commit --no-verify` or `git push --no-verify`.

## If a secret was committed or pushed

1. Stop. Don't push further.
2. Rotate it immediately:
   - Discord token: Developer Portal → your app → Bot → **Reset Token**
   - Database: change the PostgreSQL password and update `.env`
3. Deleting the file in a new commit does **not** remove the secret from history.
   History is only rewritten with the owner's explicit approval.

## Handling dev credentials

- Secrets live only in `.env` (local) or your host's secret store (production).
- `.env.example` holds placeholders only. CI fails if a real value appears there.
- Tests use synthetic data: small fake IDs like `1`, `2`, `3`, never real Discord IDs.
- Use a separate bot token and database for development and production.
- Don't paste `.env` contents, tokens or logs with IDs into issues or chat.

## Reporting a vulnerability

Use GitHub's private reporting: **Security** tab → **Report a vulnerability**.
Don't open a public issue, PR or discussion for anything that could create, duplicate or
move PB outside the ledger rules, or that exposes tokens or user data.

Please include the affected version, steps to reproduce, and impact. Never include real
tokens, Discord IDs or database contents in a report.
