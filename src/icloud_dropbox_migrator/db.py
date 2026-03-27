from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


FILE_STATUSES = {
    "pending",
    "hydrating",
    "ready_local",
    "uploading",
    "uploaded",
    "evicted",
    "failed",
    "skipped",
}


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


@dataclass(slots=True)
class ScanResult:
    inserted: int = 0
    updated: int = 0
    skipped_dirs: int = 0


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    result TEXT,
                    summary_json TEXT
                );

                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL UNIQUE,
                    relative_path TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    dropbox_path TEXT,
                    uploaded_at TEXT,
                    evicted_at TEXT,
                    last_run_id INTEGER,
                    FOREIGN KEY(last_run_id) REFERENCES runs(id)
                );

                CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
                CREATE INDEX IF NOT EXISTS idx_items_relative_path ON items(relative_path);
                """
            )

    def start_run(self, command: str) -> int:
        with self.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO runs(command, started_at) VALUES (?, ?)",
                (command, utc_now()),
            )
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, result: str, summary: dict[str, Any]) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE runs
                SET finished_at = ?, result = ?, summary_json = ?
                WHERE id = ?
                """,
                (utc_now(), result, json.dumps(summary, sort_keys=True), run_id),
            )

    def upsert_path(
        self,
        source_path: Path,
        relative_path: str,
        item_type: str,
        size: int,
        mtime: float,
    ) -> str:
        now = utc_now()
        initial_status = "pending" if item_type == "file" else "skipped"
        with self.connection() as conn:
            existing = conn.execute(
                "SELECT * FROM items WHERE source_path = ?",
                (str(source_path),),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO items (
                        source_path, relative_path, item_type, size, mtime,
                        discovered_at, updated_at, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(source_path),
                        relative_path,
                        item_type,
                        size,
                        mtime,
                        now,
                        now,
                        initial_status,
                    ),
                )
                return "inserted"

            changed = (
                existing["relative_path"] != relative_path
                or existing["item_type"] != item_type
                or existing["size"] != size
                or float(existing["mtime"]) != float(mtime)
            )
            if not changed:
                conn.execute(
                    "UPDATE items SET updated_at = ? WHERE id = ?",
                    (now, existing["id"]),
                )
                return "updated"

            status = existing["status"]
            attempt_count = existing["attempt_count"]
            last_error = existing["last_error"]
            dropbox_path = existing["dropbox_path"]
            uploaded_at = existing["uploaded_at"]
            evicted_at = existing["evicted_at"]

            if item_type == "file":
                status = "pending"
                attempt_count = 0
                last_error = None
                dropbox_path = None
                uploaded_at = None
                evicted_at = None
            else:
                status = "skipped"

            conn.execute(
                """
                UPDATE items
                SET relative_path = ?, item_type = ?, size = ?, mtime = ?,
                    updated_at = ?, status = ?, attempt_count = ?, last_error = ?,
                    dropbox_path = ?, uploaded_at = ?, evicted_at = ?
                WHERE id = ?
                """,
                (
                    relative_path,
                    item_type,
                    size,
                    mtime,
                    now,
                    status,
                    attempt_count,
                    last_error,
                    dropbox_path,
                    uploaded_at,
                    evicted_at,
                    existing["id"],
                ),
            )
            return "updated"

    def recover_incomplete_items(self) -> int:
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE items
                SET status = 'pending'
                WHERE status IN ('hydrating', 'ready_local', 'uploading')
                """
            )
            return cursor.rowcount

    def next_work_item(self) -> sqlite3.Row | None:
        with self.connection() as conn:
            uploaded_row = conn.execute(
                """
                SELECT * FROM items
                WHERE item_type = 'file' AND status = 'uploaded'
                ORDER BY relative_path ASC
                LIMIT 1
                """
            ).fetchone()
            if uploaded_row is not None:
                return uploaded_row

            return conn.execute(
                """
                SELECT * FROM items
                WHERE item_type = 'file' AND status = 'pending'
                ORDER BY relative_path ASC
                LIMIT 1
                """
            ).fetchone()

    def update_item_status(
        self,
        item_id: int,
        status: str,
        *,
        run_id: int | None = None,
        last_error: str | None = None,
        dropbox_path: str | None = None,
        uploaded_at: str | None = None,
        evicted_at: str | None = None,
    ) -> None:
        if status not in FILE_STATUSES:
            raise ValueError(f"unsupported status: {status}")
        with self.connection() as conn:
            fields = ["status = ?", "updated_at = ?"]
            values: list[Any] = [status, utc_now()]
            if run_id is not None:
                fields.append("last_run_id = ?")
                values.append(run_id)
            if last_error is not None:
                fields.append("last_error = ?")
                values.append(last_error)
            if dropbox_path is not None:
                fields.append("dropbox_path = ?")
                values.append(dropbox_path)
            if uploaded_at is not None:
                fields.append("uploaded_at = ?")
                values.append(uploaded_at)
            if evicted_at is not None:
                fields.append("evicted_at = ?")
                values.append(evicted_at)
            values.append(item_id)
            conn.execute(
                f"UPDATE items SET {', '.join(fields)} WHERE id = ?",
                values,
            )

    def mark_failed(self, item_id: int, error: str, *, run_id: int) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE items
                SET status = 'failed',
                    attempt_count = attempt_count + 1,
                    last_error = ?,
                    updated_at = ?,
                    last_run_id = ?
                WHERE id = ?
                """,
                (error, utc_now(), run_id, item_id),
            )

    def retry_failed(self, path_prefix: str | None = None) -> int:
        query = "UPDATE items SET status = 'pending', last_error = NULL WHERE status = 'failed'"
        params: list[Any] = []
        if path_prefix:
            query += " AND relative_path LIKE ?"
            params.append(f"{path_prefix}%")
        with self.connection() as conn:
            cursor = conn.execute(query, params)
            return cursor.rowcount

    def status_counts(self) -> dict[str, int]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM items GROUP BY status ORDER BY status"
            ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def recent_failures(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute(
                """
                SELECT relative_path, attempt_count, last_error, updated_at
                FROM items
                WHERE status = 'failed'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

