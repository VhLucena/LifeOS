#!/usr/bin/env python3
"""Export Granola meetings to the configured vault and request local indexing."""

import argparse
import fcntl
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import httpx
from dotenv import dotenv_values

from api.services.granola_sync import GranolaSync
from config.settings import settings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=ROOT / "data/granola-sync")
    args = parser.parse_args(argv)
    token = os.environ.get("GRANOLA_API_TOKEN") or dotenv_values(ROOT / ".env").get(
        "GRANOLA_API_TOKEN"
    )
    if not token:
        print("Granola API key missing: set GRANOLA_API_TOKEN in .env.")
        return 2
    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    with (args.state_dir / "sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Granola sync already running.")
            return 0
        try:
            with httpx.Client(timeout=60) as client:
                sync = GranolaSync(
                    token,
                    settings.vault_path / "Granola",
                    args.state_dir / "state.json",
                    client,
                )
                result = sync.run()
                print("Granola notes exported:", result["exported"])
                indexed = sync.request_index("http://127.0.0.1:8000")
                print("LifeOS indexing request:", "accepted" if indexed else "pending")
                return 0 if indexed else 1
        except Exception as error:  # noqa: BLE001 — sanitize failures at the CLI boundary
            # Exception messages and API bodies may contain private note data.
            print("Granola sync failed:", type(error).__name__)
            return 1


if __name__ == "__main__":
    sys.exit(main())
