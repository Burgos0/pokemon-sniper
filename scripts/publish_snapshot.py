"""
Publish a new DB snapshot after a daily run. Used by the daily workflow's
persist step.

Stages a consistent copy of every database, uploads it gzip-compressed as a
brand-new immutable release, downloads that release back down to verify it
is complete and uncorrupted, and only then advances the "latest" pointer.
The previous good snapshot is never overwritten or deleted. Any failure
raises a hard error (nonzero exit) rather than silently leaving the
pointer on a stale or partially-uploaded snapshot.

Usage:
    python -m scripts.publish_snapshot --source-commit <sha>
"""
import argparse
import sys

from scripts.restore_snapshot import DB_PATHS
from scripts.snapshot_storage import (
    GhReleaseSnapshotBackend,
    SnapshotError,
    create_and_publish_snapshot,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-commit", default=None,
                         help="Commit SHA the snapshot was produced from (recorded in the manifest).")
    args = parser.parse_args()

    backend = GhReleaseSnapshotBackend()
    try:
        manifest = create_and_publish_snapshot(backend, DB_PATHS, args.source_commit)
    except SnapshotError as e:
        print(f"ERROR: Failed to publish database snapshot: {e}")
        sys.exit(1)

    print("Snapshot published and verified.")
    print(f"  snapshot_id:       {manifest['snapshot_id']}")
    print(f"  source_commit:     {manifest['source_commit']}")
    print(f"  latest_price_date: {manifest['latest_price_date']}")
    for name, info in manifest["files"].items():
        print(f"  {name}: {info['size_bytes']:,} bytes ({info['gz_size_bytes']:,} gzipped), "
              f"sha256={info['sha256'][:12]}...")


if __name__ == "__main__":
    main()
