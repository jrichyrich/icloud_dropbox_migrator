from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from icloud_dropbox_migrator.db import StateStore
from icloud_dropbox_migrator.icloud import ICloudError
from icloud_dropbox_migrator.dropbox_api import UploadResult
from icloud_dropbox_migrator.icloud import FileState
from icloud_dropbox_migrator.worker import MigrationWorker, scan_source_tree


class MigrationWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "state.sqlite3"
        self.store = StateStore(self.db_path)

    def test_scan_and_run_single_file(self) -> None:
        source_root = self.root / "source"
        source_root.mkdir()
        sample = source_root / "nested" / "example.txt"
        sample.parent.mkdir()
        sample.write_text("hello", encoding="utf-8")

        scan_result = scan_source_tree(self.store, source_root)
        self.assertEqual(scan_result.inserted, 1)

        dropbox_client = mock.Mock()
        dropbox_client.upload_file.return_value = UploadResult(
            path_display="/Root/nested/example.txt",
            path_lower="/root/nested/example.txt",
            dropbox_id="id:123",
        )
        icloud = mock.Mock()
        icloud.ensure_local_file.return_value = FileState(sample, sample.stat().st_size, set())

        worker = MigrationWorker(
            store=self.store,
            source_root=source_root,
            dropbox_root="/Root",
            dropbox_client=dropbox_client,
            icloud=icloud,
            retry_limit=1,
        )
        summary = worker.run(max_files=1)

        self.assertEqual(summary.processed, 1)
        self.assertEqual(summary.uploaded, 1)
        self.assertEqual(summary.evicted, 1)
        icloud.start_download.assert_not_called()
        icloud.ensure_local_file.assert_called_once()
        icloud.evict_local_copy.assert_called_once_with(sample.resolve())

        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT status, dropbox_path FROM items WHERE item_type = 'file'"
            ).fetchone()
        self.assertEqual(row["status"], "evicted")
        self.assertEqual(row["dropbox_path"], "/Root/nested/example.txt")

    def test_run_retries_evict_without_reuploading(self) -> None:
        source_root = self.root / "source"
        source_root.mkdir()
        sample = source_root / "nested" / "example.txt"
        sample.parent.mkdir()
        sample.write_text("hello", encoding="utf-8")

        scan_source_tree(self.store, source_root)

        dropbox_client = mock.Mock()
        dropbox_client.upload_file.return_value = UploadResult(
            path_display="/Root/nested/example.txt",
            path_lower="/root/nested/example.txt",
            dropbox_id="id:123",
        )
        icloud = mock.Mock()
        icloud.ensure_local_file.return_value = FileState(sample, sample.stat().st_size, set())
        icloud.evict_local_copy.side_effect = [ICloudError("evict failed"), None]

        worker = MigrationWorker(
            store=self.store,
            source_root=source_root,
            dropbox_root="/Root",
            dropbox_client=dropbox_client,
            icloud=icloud,
            retry_limit=2,
            retry_delay_seconds=0,
        )
        summary = worker.run(max_files=1)

        self.assertEqual(summary.processed, 1)
        self.assertEqual(summary.uploaded, 1)
        self.assertEqual(summary.evicted, 1)
        dropbox_client.upload_file.assert_called_once()
        self.assertEqual(icloud.evict_local_copy.call_count, 2)

        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT status, dropbox_path FROM items WHERE item_type = 'file'"
            ).fetchone()
        self.assertEqual(row["status"], "evicted")
        self.assertEqual(row["dropbox_path"], "/Root/nested/example.txt")

    def test_run_resumes_evict_only_for_pending_item_with_uploaded_state(self) -> None:
        source_root = self.root / "source"
        source_root.mkdir()
        sample = source_root / "nested" / "example.txt"
        sample.parent.mkdir()
        sample.write_text("hello", encoding="utf-8")

        scan_source_tree(self.store, source_root)
        with self.store.connection() as conn:
            conn.execute(
                """
                UPDATE items
                SET status = 'pending',
                    dropbox_path = '/Root/nested/example.txt',
                    uploaded_at = '2026-01-01T00:00:00+00:00'
                WHERE relative_path = 'nested/example.txt'
                """
            )

        dropbox_client = mock.Mock()
        icloud = mock.Mock()

        worker = MigrationWorker(
            store=self.store,
            source_root=source_root,
            dropbox_root="/Root",
            dropbox_client=dropbox_client,
            icloud=icloud,
            retry_limit=1,
        )
        summary = worker.run(max_files=1)

        self.assertEqual(summary.processed, 1)
        self.assertEqual(summary.uploaded, 0)
        self.assertEqual(summary.evicted, 1)
        dropbox_client.upload_file.assert_not_called()
        icloud.ensure_local_file.assert_not_called()
        icloud.evict_local_copy.assert_called_once_with(sample.resolve())

    def test_scan_skips_ds_store(self) -> None:
        source_root = self.root / "source"
        source_root.mkdir()
        (source_root / ".DS_Store").write_text("ignored", encoding="utf-8")
        sample = source_root / "example.txt"
        sample.write_text("hello", encoding="utf-8")

        scan_result = scan_source_tree(self.store, source_root)

        self.assertEqual(scan_result.inserted, 1)
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT relative_path, status FROM items ORDER BY relative_path"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["relative_path"], "example.txt")
        self.assertEqual(rows[0]["status"], "pending")

    def test_scan_marks_existing_ds_store_as_skipped(self) -> None:
        source_root = self.root / "source"
        source_root.mkdir()
        ds_store = source_root / ".DS_Store"
        ds_store.write_text("ignored", encoding="utf-8")

        self.store.upsert_path(ds_store.resolve(), ".DS_Store", "file", ds_store.stat().st_size, ds_store.stat().st_mtime)
        with self.store.connection() as conn:
            conn.execute("UPDATE items SET status = 'failed' WHERE relative_path = '.DS_Store'")

        scan_source_tree(self.store, source_root)

        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT status FROM items WHERE relative_path = '.DS_Store'"
            ).fetchone()
        self.assertEqual(row["status"], "skipped")
