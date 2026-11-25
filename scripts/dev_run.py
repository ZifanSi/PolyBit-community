"""Run PolyBit locally without installing PostgreSQL.

Uses the PostgreSQL binaries bundled with the `pgserver` dev dependency to run a
local server on 127.0.0.1:54329. Data lives in ./.devdb/data and the server log in
./.devdb/postgres.log (kept outside the data directory so crash recovery is never
blocked by an open log file). The server keeps running after the bot exits, so
restarting the bot is fast; stop it with `stop-db`.

    python scripts/dev_run.py            # start DB if needed, then run the bot
    python scripts/dev_run.py audit      # any other polybit command (migrate, audit)
    python scripts/dev_run.py stop-db    # shut the local database down cleanly
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pgserver

ROOT = Path(__file__).resolve().parent.parent
DEV_DIR = ROOT / ".devdb"
DATA_DIR = DEV_DIR / "data"
LOG_FILE = DEV_DIR / "postgres.log"
PORT = 54329
BIN = Path(pgserver.__file__).resolve().parent / "pginstall" / "bin"
DATABASE_URL = f"postgresql://postgres@127.0.0.1:{PORT}/postgres"


def _exe(name: str) -> str:
    return str(BIN / (name + (".exe" if os.name == "nt" else "")))


def _pg_ctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_exe("pg_ctl"), "-D", str(DATA_DIR), *args],
        check=check, capture_output=True, text=True,
    )


def ensure_database() -> None:
    DEV_DIR.mkdir(exist_ok=True)
    if not (DATA_DIR / "PG_VERSION").exists():
        print("Initializing local database in", DATA_DIR)
        subprocess.run(
            [_exe("initdb"), "-D", str(DATA_DIR), "-U", "postgres", "--auth=trust", "-E", "UTF8"],
            check=True, capture_output=True, text=True,
        )
    if _pg_ctl("status", check=False).returncode == 0:
        return
    print(f"Starting local PostgreSQL on port {PORT} (log: {LOG_FILE})")
    # The postmaster inherits pg_ctl's stdio handles. Capturing them in pipes would make
    # us wait forever for EOF (on Windows), so discard them; errors go to LOG_FILE.
    # Generous timeout: after an unclean shutdown the server replays WAL first.
    result = subprocess.run(
        [_exe("pg_ctl"), "-D", str(DATA_DIR), "start", "-w", "-t", "180",
         "-l", str(LOG_FILE), "-o", f"-h 127.0.0.1 -p {PORT}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        sys.exit(f"Could not start PostgreSQL. See {LOG_FILE}")


def stop_database() -> None:
    if _pg_ctl("status", check=False).returncode != 0:
        print("Local database is not running.")
        return
    _pg_ctl("stop", "-m", "fast", "-w")
    print("Local database stopped.")


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["stop-db"]:
        stop_database()
        return 0
    ensure_database()
    os.environ["PB_DATABASE_URL"] = DATABASE_URL
    from polybit.__main__ import main as polybit_main

    return polybit_main(args)


if __name__ == "__main__":
    sys.exit(main())
