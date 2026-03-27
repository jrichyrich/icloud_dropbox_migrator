from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .db import ScanResult, StateStore, utc_now
from .dropbox_api import DropboxClient
from .icloud import ICloudError, ICloudManager


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RunSummary:
    processed: int = 0
    uploaded: int = 0
    evicted: int = 0
    failed: int = 0
    recovered: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "processed": self.processed,
            "uploaded": self.uploaded,
            "evicted": self.evicted,
            "failed": self.failed,
            "recovered": self.recovered,
        }


def scan_source_tree(store: StateStore, source_root: Path) -> ScanResult:
    source_root = source_root.expanduser().resolve()
    if not source_root.exists():
        raise FileNotFoundError(f"source root does not exist: {source_root}")

    result = ScanResult()
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink():
            continue
        relative_path = path.relative_to(source_root).as_posix()
        stat_info = path.stat()
        if path.is_dir():
            outcome = store.upsert_path(path, relative_path, "dir", 0, stat_info.st_mtime)
            if outcome == "inserted":
                result.skipped_dirs += 1
            continue

        outcome = store.upsert_path(
            path,
            relative_path,
            "file",
            stat_info.st_size,
            stat_info.st_mtime,
        )
        if outcome == "inserted":
            result.inserted += 1
        else:
            result.updated += 1

    return result


class MigrationWorker:
    def __init__(
        self,
        *,
        store: StateStore,
        source_root: Path,
        dropbox_root: str,
        dropbox_client: DropboxClient,
        icloud: ICloudManager | None = None,
        hydrate_timeout_seconds: int = 3600,
        retry_limit: int = 3,
        retry_delay_seconds: float = 5.0,
    ) -> None:
        self.store = store
        self.source_root = source_root.expanduser().resolve()
        self.dropbox_root = dropbox_root
        self.dropbox_client = dropbox_client
        self.icloud = icloud or ICloudManager()
        self.hydrate_timeout_seconds = hydrate_timeout_seconds
        self.retry_limit = retry_limit
        self.retry_delay_seconds = retry_delay_seconds

    def run(self, *, max_files: int | None = None) -> RunSummary:
        run_id = self.store.start_run("run")
        summary = RunSummary(recovered=self.store.recover_incomplete_items())
        LOGGER.info("recovered %s incomplete items", summary.recovered)
        try:
            while max_files is None or summary.processed < max_files:
                item = self.store.next_work_item()
                if item is None:
                    break
                summary.processed += 1
                self._process_item(item, run_id, summary)
            self.store.finish_run(run_id, "ok", summary.to_dict())
        except Exception:
            self.store.finish_run(run_id, "error", summary.to_dict())
            raise
        return summary

    def _process_item(self, item, run_id: int, summary: RunSummary) -> None:
        path = Path(item["source_path"])
        LOGGER.info("processing %s", item["relative_path"])

        if item["status"] == "uploaded":
            self._evict_only(item, path, run_id, summary)
            return

        for attempt in range(1, self.retry_limit + 1):
            try:
                self.store.update_item_status(item["id"], "hydrating", run_id=run_id, last_error="")
                state = self.icloud.ensure_local_file(
                    path,
                    timeout_seconds=self.hydrate_timeout_seconds,
                )
                LOGGER.info("hydrated %s (%s bytes)", item["relative_path"], state.size)
                self.store.update_item_status(item["id"], "ready_local", run_id=run_id)

                self.store.update_item_status(item["id"], "uploading", run_id=run_id)
                upload_result = self.dropbox_client.upload_file(
                    path,
                    self.dropbox_root,
                    item["relative_path"],
                )
                summary.uploaded += 1
                self.store.update_item_status(
                    item["id"],
                    "uploaded",
                    run_id=run_id,
                    dropbox_path=upload_result.path_display,
                    uploaded_at=utc_now(),
                )

                self.icloud.evict_local_copy(path)
                summary.evicted += 1
                self.store.update_item_status(
                    item["id"],
                    "evicted",
                    run_id=run_id,
                    evicted_at=utc_now(),
                )
                LOGGER.info("evicted local copy for %s", item["relative_path"])
                return
            except Exception as exc:
                LOGGER.warning("attempt %s failed for %s: %s", attempt, item["relative_path"], exc)
                if attempt >= self.retry_limit:
                    summary.failed += 1
                    self.store.mark_failed(item["id"], str(exc), run_id=run_id)
                    return
                time.sleep(self.retry_delay_seconds)

    def _evict_only(self, item, path: Path, run_id: int, summary: RunSummary) -> None:
        try:
            self.icloud.evict_local_copy(path)
        except ICloudError as exc:
            summary.failed += 1
            self.store.mark_failed(item["id"], str(exc), run_id=run_id)
            return
        summary.evicted += 1
        self.store.update_item_status(
            item["id"],
            "evicted",
            run_id=run_id,
            evicted_at=utc_now(),
        )


def default_db_path() -> Path:
    return Path(os.environ.get("MIGRATOR_DB_PATH", "migration_state.sqlite3"))
