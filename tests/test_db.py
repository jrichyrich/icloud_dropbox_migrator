from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from icloud_dropbox_migrator.db import StateStore


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "state.sqlite3"
        self.store = StateStore(self.db_path)

    def test_upsert_inserts_and_updates(self) -> None:
        path = Path("/tmp/example.txt")
        self.assertEqual(self.store.upsert_path(path, "example.txt", "file", 10, 1.0), "inserted")
        self.assertEqual(self.store.upsert_path(path, "example.txt", "file", 10, 1.0), "updated")
        with self.store.connection() as conn:
            row = conn.execute("SELECT status FROM items WHERE source_path = ?", (str(path),)).fetchone()
        self.assertEqual(row["status"], "pending")

    def test_changed_file_resets_evicted_status(self) -> None:
        path = Path("/tmp/example.txt")
        self.store.upsert_path(path, "example.txt", "file", 10, 1.0)
        with self.store.connection() as conn:
            conn.execute(
                """
                UPDATE items
                SET status = 'evicted', dropbox_path = '/dest/example.txt',
                    uploaded_at = 'uploaded', evicted_at = 'evicted'
                WHERE source_path = ?
                """,
                (str(path),),
            )
        self.store.upsert_path(path, "example.txt", "file", 12, 2.0)
        with self.store.connection() as conn:
            row = conn.execute("SELECT status, dropbox_path, uploaded_at, evicted_at FROM items").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["dropbox_path"])
        self.assertIsNone(row["uploaded_at"])
        self.assertIsNone(row["evicted_at"])

    def test_retry_failed(self) -> None:
        path = Path("/tmp/example.txt")
        self.store.upsert_path(path, "example.txt", "file", 10, 1.0)
        run_id = self.store.start_run("run")
        self.store.mark_failed(1, "boom", run_id=run_id)
        count = self.store.retry_failed()
        self.assertEqual(count, 1)
        with self.store.connection() as conn:
            row = conn.execute("SELECT status, last_error FROM items").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["last_error"])
