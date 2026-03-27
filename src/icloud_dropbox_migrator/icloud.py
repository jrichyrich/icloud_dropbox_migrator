from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class ICloudError(RuntimeError):
    pass


@dataclass(slots=True)
class FileState:
    path: Path
    size: int
    flags: set[str]

    @property
    def is_dataless(self) -> bool:
        return "dataless" in self.flags


class ICloudManager:
    def __init__(self, helper_path: Path | None = None) -> None:
        self.helper_path = helper_path or Path(__file__).with_name("native_icloud.jxa")

    def start_download(self, path: Path) -> None:
        self._native_action("download", path)

    def evict_local_copy(self, path: Path) -> None:
        self._native_action("evict", path)

    def ensure_local_file(
        self,
        path: Path,
        *,
        timeout_seconds: int = 3600,
        poll_seconds: float = 2.0,
    ) -> FileState:
        self.start_download(path)
        deadline = time.monotonic() + timeout_seconds
        stable_polls = 0
        previous_state: FileState | None = None

        while time.monotonic() < deadline:
            state = self.inspect(path)
            if not state.is_dataless:
                if previous_state and previous_state.size == state.size:
                    stable_polls += 1
                else:
                    stable_polls = 0
                if stable_polls >= 1:
                    self._verify_readable(path)
                    return state
            previous_state = state
            time.sleep(poll_seconds)

        raise ICloudError(f"timed out waiting for iCloud download: {path}")

    def inspect(self, path: Path) -> FileState:
        if not path.exists():
            raise ICloudError(f"source path no longer exists: {path}")
        try:
            result = subprocess.run(
                ["stat", "-f", "%Sf|%z", str(path)],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise ICloudError(f"failed to inspect file state for {path}: {exc.stderr}") from exc
        flags_value, size_value = result.stdout.strip().split("|", maxsplit=1)
        flags = {flag for flag in flags_value.split(",") if flag}
        return FileState(path=path, size=int(size_value), flags=flags)

    def _verify_readable(self, path: Path) -> None:
        with path.open("rb") as handle:
            handle.read(64 * 1024)

    def _native_action(self, action: str, path: Path) -> None:
        try:
            result = subprocess.run(
                ["osascript", "-l", "JavaScript", str(self.helper_path), action, str(path)],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip()
            raise ICloudError(
                f"native iCloud action {action} failed for {path}: {stderr or exc.stdout.strip()}"
            ) from exc

        last_stdout_line = ""
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                last_stdout_line = line
        if not last_stdout_line:
            return

        payload = json.loads(last_stdout_line)
        if not payload.get("ok", False):
            raise ICloudError(payload.get("error", f"native action {action} failed for {path}"))
