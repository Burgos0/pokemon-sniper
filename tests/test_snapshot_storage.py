import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.snapshot_storage import (
    LocalDirSnapshotBackend,
    SnapshotError,
    build_manifest,
    compute_sha256,
    create_and_publish_snapshot,
    decompress_snapshot,
    restore_latest_snapshot,
    verify_local_snapshot,
)


from sniper import db as sniper_db

INSERT_PRICE = ("INSERT INTO printing_prices (date, product_id, printing, market_price) "
                "VALUES (?, ?, ?, ?)")


def make_databases(dest_dir, price_rows=None):
    prices_path = os.path.join(dest_dir, "prices.db")
    with sniper_db.init_db(prices_path) as conn:
        for row in (price_rows or [("2026-09-01", 1, "Holofoil", 10.0)]):
            conn.execute(INSERT_PRICE, row)
        # Pad past MIN_EXPECTED_BYTES so the size guard is not what fails.
        conn.execute("CREATE TABLE padding (x BLOB)")
        conn.execute("INSERT INTO padding VALUES (?)", (os.urandom(4096),))
    return prices_path


def paths(prices):
    return {"prices.db": prices}


class SnapshotStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.backend_root = os.path.join(self.root, "remote_releases")
        self.backend = LocalDirSnapshotBackend(self.backend_root)
        self.prices_db = make_databases(self.root)

    def test_round_trip_restoration(self):
        published = create_and_publish_snapshot(self.backend, paths(self.prices_db), "abc123")

        dest_dir = os.path.join(self.root, "restored")
        os.makedirs(dest_dir)
        dest_prices = os.path.join(dest_dir, "prices.db")
        restored = restore_latest_snapshot(self.backend, paths(dest_prices))

        self.assertEqual(restored["snapshot_id"], published["snapshot_id"])
        self.assertEqual(restored["source_commit"], "abc123")
        self.assertEqual(restored["latest_price_date"], "2026-09-01")
        # SQLite's backup API is a logical page copy, not guaranteed
        # byte-identical to the pre-backup source file -- so compare data
        # content, not raw bytes against the original.
        with sqlite3.connect(dest_prices) as conn:
            self.assertEqual(conn.execute("SELECT date, product_id, printing, market_price "
                                           "FROM printing_prices").fetchall(),
                              [("2026-09-01", 1, "Holofoil", 10.0)])

    def test_missing_snapshot_never_restores_empty_db(self):
        # No snapshot has ever been published -- no pointer exists.
        dest_prices = os.path.join(self.root, "dest_prices.db")
        with self.assertRaises(SnapshotError):
            restore_latest_snapshot(self.backend, paths(dest_prices))
        self.assertFalse(os.path.exists(dest_prices))

    def test_invalid_download_checksum_mismatch_never_installs(self):
        create_and_publish_snapshot(self.backend, paths(self.prices_db), "abc123")

        # Corrupt the uploaded snapshot after the fact (simulates a bad/incomplete download).
        tag = self.backend.get_pointer()
        corrupted_path = os.path.join(self.backend_root, tag, "prices.db.gz")
        with open(corrupted_path, "r+b") as f:
            f.seek(0)
            f.write(b"\x00\x00\x00\x00")

        dest_prices = os.path.join(self.root, "dest_prices.db")
        with self.assertRaises(SnapshotError):
            restore_latest_snapshot(self.backend, paths(dest_prices))
        self.assertFalse(os.path.exists(dest_prices))

    def test_interrupted_upload_never_advances_pointer(self):
        # First, a real good snapshot exists.
        first = create_and_publish_snapshot(self.backend, paths(self.prices_db), "commit-1")

        # Second publish attempt "uploads" successfully but the artifact is
        # corrupted in transit -- simulate by monkeypatching upload to write
        # a truncated prices.db, so post-upload verification must catch it.
        real_upload = self.backend.upload

        def flaky_upload(local_dir, tag):
            real_upload(local_dir, tag)
            # Simulate an interrupted/corrupted transfer after upload "succeeded".
            with open(os.path.join(self.backend_root, tag, "prices.db.gz"), "wb") as f:
                f.write(b"not a real db")

        self.backend.upload = flaky_upload
        with self.assertRaises(SnapshotError):
            create_and_publish_snapshot(self.backend, paths(self.prices_db), "commit-2")

        # Pointer must still reference the first, good snapshot.
        self.assertEqual(self.backend.get_pointer(), first["snapshot_id"])
        # The first snapshot's files must still be intact (never overwritten/deleted).
        check_dir = os.path.join(self.root, "check_first")
        os.makedirs(check_dir)
        self.backend.download(first["snapshot_id"], check_dir)
        decompress_snapshot(check_dir)
        ok, errors, _ = verify_local_snapshot(check_dir)
        self.assertTrue(ok, errors)

    def test_never_overwrites_or_deletes_previous_good_snapshot(self):
        first = create_and_publish_snapshot(self.backend, paths(self.prices_db), "commit-1")

        # Change the source data and publish again.
        with sqlite3.connect(self.prices_db) as conn:
            conn.execute(INSERT_PRICE, ("2026-09-02", 2, "Normal", 5.0))
        second = create_and_publish_snapshot(self.backend, paths(self.prices_db), "commit-2")

        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertTrue(self.backend.release_exists(first["snapshot_id"]))
        self.assertTrue(self.backend.release_exists(second["snapshot_id"]))
        self.assertEqual(self.backend.get_pointer(), second["snapshot_id"])

        # Republishing under an already-used tag must be refused, never silently overwritten.
        with self.assertRaises(SnapshotError):
            self.backend.upload(self.root, first["snapshot_id"])

    def test_pointer_failure_after_creation_before_asset_upload_leaves_draft_ignored(self):
        # A good, published pointer already exists.
        first = create_and_publish_snapshot(self.backend, paths(self.prices_db), "commit-1")

        # Simulate: the draft pointer release gets created successfully, but
        # the asset upload that would make it complete fails.
        with sqlite3.connect(self.prices_db) as conn:
            conn.execute(INSERT_PRICE, ("2026-09-02", 2, "Normal", 5.0))

        with patch.object(self.backend, "_upload_pointer_asset",
                          side_effect=SnapshotError("simulated upload failure")):
            with self.assertRaises(SnapshotError):
                create_and_publish_snapshot(self.backend, paths(self.prices_db), "commit-2")

        # A draft pointer now exists but must be invisible to restore.
        all_tags = self.backend.list_pointer_tags()
        self.assertGreaterEqual(len(all_tags), 2)  # first (published) + the new incomplete draft
        self.assertEqual(self.backend.get_pointer(), first["snapshot_id"])

        dest_prices = os.path.join(self.root, "dest_prices.db")
        restored = restore_latest_snapshot(self.backend, paths(dest_prices))
        self.assertEqual(restored["snapshot_id"], first["snapshot_id"])

    def test_draft_pointer_that_never_gets_published_is_ignored_forever(self):
        first = create_and_publish_snapshot(self.backend, paths(self.prices_db), "commit-1")

        # Directly create a draft pointer (as if _publish_pointer were never reached).
        self.backend._create_draft_pointer("data-pointer-99999999T999999Z")
        with self.assertRaises(SnapshotError):
            # It exists but is missing its asset entirely -- still must not
            # be treated as the current pointer.
            self.backend._download_and_check_pointer("data-pointer-99999999T999999Z", "anything")

        self.assertEqual(self.backend.get_pointer(), first["snapshot_id"])

    def test_bootstrap_then_independent_restore_round_trip(self):
        # Mirrors scripts.bootstrap_snapshot_storage's flow.
        manifest = create_and_publish_snapshot(self.backend, paths(self.prices_db), "bootstrap-commit")
        scratch_prices = os.path.join(self.root, "scratch_prices.db")
        restored = restore_latest_snapshot(self.backend, paths(scratch_prices))
        self.assertEqual(restored["snapshot_id"], manifest["snapshot_id"])
        self.assertTrue(os.path.exists(scratch_prices))

    def test_pointer_update_failure_preserves_previous_pointer(self):
        # A good snapshot + pointer already exist.
        first = create_and_publish_snapshot(self.backend, paths(self.prices_db), "commit-1")
        self.assertEqual(self.backend.get_pointer(), first["snapshot_id"])

        # The next publish's snapshot upload succeeds and verifies fine, but
        # the pointer update itself fails (e.g. a transient API error).
        real_set_pointer = self.backend.set_pointer
        self.backend.set_pointer = lambda tag: (_ for _ in ()).throw(SnapshotError("simulated pointer API failure"))

        with sqlite3.connect(self.prices_db) as conn:
            conn.execute(INSERT_PRICE, ("2026-09-02", 2, "Normal", 5.0))
        with self.assertRaises(SnapshotError):
            create_and_publish_snapshot(self.backend, paths(self.prices_db), "commit-2")

        # The previous pointer must still be intact and resolvable -- a
        # failed pointer update must never destroy access to it.
        self.assertEqual(self.backend.get_pointer(), first["snapshot_id"])
        self.backend.set_pointer = real_set_pointer

        # The new snapshot release itself was still uploaded (it just isn't
        # pointed at yet); restoring still gets the last GOOD, pointed-to snapshot.
        dest_prices = os.path.join(self.root, "dest_prices.db")
        restored = restore_latest_snapshot(self.backend, paths(dest_prices))
        self.assertEqual(restored["snapshot_id"], first["snapshot_id"])

    def test_pointer_history_is_append_only_never_mutated_in_place(self):
        first = create_and_publish_snapshot(self.backend, paths(self.prices_db), "commit-1")
        with sqlite3.connect(self.prices_db) as conn:
            conn.execute(INSERT_PRICE, ("2026-09-02", 2, "Normal", 5.0))
        second = create_and_publish_snapshot(self.backend, paths(self.prices_db), "commit-2")

        pointer_tags = self.backend.list_pointer_tags()
        self.assertGreaterEqual(len(pointer_tags), 2)  # one pointer release per publish, never reused
        self.assertEqual(self.backend.get_pointer(), second["snapshot_id"])

    def test_failure_between_installing_two_databases_never_leaves_mixed_set(self):
        # Milestone 4 adds sniper.db; the install must stay all-or-nothing.
        other_db = os.path.join(self.root, "sniper.db")
        with sqlite3.connect(other_db) as conn:
            conn.execute("CREATE TABLE trades (x INTEGER)")
            conn.execute("CREATE TABLE padding (x BLOB)")
            conn.execute("INSERT INTO padding VALUES (?)", (os.urandom(4096),))
        two = {"prices.db": self.prices_db, "sniper.db": other_db}
        create_and_publish_snapshot(self.backend, two, "commit-1")

        dest_dir = os.path.join(self.root, "installed")
        os.makedirs(dest_dir)
        dest = {"prices.db": os.path.join(dest_dir, "prices.db"),
                "sniper.db": os.path.join(dest_dir, "sniper.db")}
        restore_latest_snapshot(self.backend, dest)  # establish the "old" set
        old_bytes = {n: Path(p).read_bytes() for n, p in dest.items()}

        with sqlite3.connect(self.prices_db) as conn:
            conn.execute(INSERT_PRICE, ("2026-09-02", 2, "Normal", 5.0))
        create_and_publish_snapshot(self.backend, two, "commit-2")

        real_move = shutil.move
        call_count = {"n": 0}

        def flaky_move(src, dst):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise OSError("simulated disk failure during install")
            return real_move(src, dst)

        with patch("scripts.snapshot_storage.shutil.move", side_effect=flaky_move):
            with self.assertRaises(SnapshotError):
                restore_latest_snapshot(self.backend, dest)

        for name, path in dest.items():
            self.assertEqual(Path(path).read_bytes(), old_bytes[name])
        self.assertTrue(list(Path(dest_dir).glob("*.db.bak.*")))

    def test_snapshot_assets_are_gzip_compressed(self):
        manifest = create_and_publish_snapshot(self.backend, paths(self.prices_db), "abc")
        tag_dir = os.path.join(self.backend_root, manifest["snapshot_id"])
        self.assertEqual(sorted(os.listdir(tag_dir)), ["manifest.json", "prices.db.gz"])
        info = manifest["files"]["prices.db"]
        self.assertLess(info["gz_size_bytes"], info["size_bytes"] * 2)
        self.assertEqual(info["gz_sha256"], compute_sha256(os.path.join(tag_dir, "prices.db.gz")))


class VerifyLocalSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.staging = os.path.join(self.tmp.name, "staging")
        os.makedirs(self.staging)
        self.prices_db = make_databases(self.tmp.name)
        for name, src in (("prices.db", self.prices_db),):
            with open(src, "rb") as fsrc, open(os.path.join(self.staging, name), "wb") as fdst:
                fdst.write(fsrc.read())
        manifest = build_manifest("data-test-1", "commit-x", self.staging)
        with open(os.path.join(self.staging, "manifest.json"), "w") as f:
            json.dump(manifest, f)

    def test_valid_snapshot_passes(self):
        ok, errors, manifest = verify_local_snapshot(self.staging)
        self.assertTrue(ok, errors)
        self.assertEqual(manifest["latest_price_date"], "2026-09-01")

    def test_missing_manifest_fails(self):
        os.remove(os.path.join(self.staging, "manifest.json"))
        ok, errors, manifest = verify_local_snapshot(self.staging)
        self.assertFalse(ok)
        self.assertIsNone(manifest)

    def test_checksum_mismatch_detected(self):
        with open(os.path.join(self.staging, "prices.db"), "ab") as f:
            f.write(b"extra-bytes")
        ok, errors, _ = verify_local_snapshot(self.staging)
        self.assertFalse(ok)
        self.assertTrue(any("mismatch" in e for e in errors))

    def test_corrupt_sqlite_file_fails_integrity_check(self):
        with open(os.path.join(self.staging, "prices.db"), "wb") as f:
            f.write(os.urandom(2048))
        # Rebuild manifest to match the new (corrupt) file's checksum/size,
        # isolating the integrity-check failure from a checksum failure.
        manifest = build_manifest("data-test-2", "commit-x", self.staging)
        with open(os.path.join(self.staging, "manifest.json"), "w") as f:
            json.dump(manifest, f)
        ok, errors, _ = verify_local_snapshot(self.staging)
        self.assertFalse(ok)
        self.assertTrue(any("integrity_check" in e for e in errors))

    def test_missing_required_table_detected(self):
        os.remove(os.path.join(self.staging, "prices.db"))
        with sqlite3.connect(os.path.join(self.staging, "prices.db")) as conn:
            conn.execute("CREATE TABLE printing_prices (date TEXT)")
            conn.execute("CREATE TABLE padding (x BLOB)")
            conn.execute("INSERT INTO padding VALUES (?)", (os.urandom(4096),))
        manifest = build_manifest("data-test-3", "commit-x", self.staging)
        with open(os.path.join(self.staging, "manifest.json"), "w") as f:
            json.dump(manifest, f)
        ok, errors, _ = verify_local_snapshot(self.staging)
        self.assertFalse(ok)
        self.assertTrue(any("required table" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
