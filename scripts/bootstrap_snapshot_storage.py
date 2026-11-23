"""
One-time bootstrap: publish the very FIRST DB snapshot from the locally
backfilled database, so restore_snapshot.py has something to restore
before any daily run has published a snapshot of its own.

--prices-db lets you bootstrap from a copy elsewhere on disk.

Usage:
    python -m scripts.bootstrap_snapshot_storage --source-commit <sha>
    python -m scripts.bootstrap_snapshot_storage --source-commit <sha> --prices-db /path/prices.db
"""
import argparse
import os
import sys
import tempfile

from scripts.restore_snapshot import DB_PATHS
from sniper import engine
from scripts.snapshot_storage import (
    GhReleaseSnapshotBackend,
    SnapshotError,
    create_and_publish_snapshot,
    restore_latest_snapshot,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-commit", default=None,
                         help="Commit SHA the database was produced from.")
    parser.add_argument("--prices-db", default=DB_PATHS["prices.db"],
                         help=f"Path to the prices database (default: {DB_PATHS['prices.db']}).")
    args = parser.parse_args()

    db_paths = {**DB_PATHS, "prices.db": args.prices_db}
    if not os.path.exists(db_paths["sniper.db"]):
        # The live ledger starts empty: schema only, no account until the
        # first daily trading step opens it with $1,000.
        engine.init_ledger(db_paths["sniper.db"]).close()
        print(f"Created empty live ledger at {db_paths['sniper.db']}")
    backend = GhReleaseSnapshotBackend()

    try:
        manifest = create_and_publish_snapshot(backend, db_paths, args.source_commit)
    except SnapshotError as e:
        print(f"ERROR: Bootstrap snapshot failed: {e}")
        sys.exit(1)

    print("Bootstrap snapshot published and verified.")
    for name, path in db_paths.items():
        print(f"  source {name}: {path}")
    print(f"  snapshot_id:       {manifest['snapshot_id']}")
    print(f"  latest_price_date: {manifest['latest_price_date']}")

    # Extra end-to-end confirmation: actually restore it into a scratch
    # location (never over the real data/ files) before calling this done.
    with tempfile.TemporaryDirectory() as scratch:
        scratch_paths = {name: os.path.join(scratch, name) for name in db_paths}
        try:
            restore_latest_snapshot(backend, scratch_paths)
        except SnapshotError as e:
            print(f"ERROR: Bootstrap snapshot was published but failed an independent restore check: {e}")
            sys.exit(1)

    print("Independent restore check into a scratch directory succeeded.")


if __name__ == "__main__":
    main()
