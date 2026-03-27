from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from icloud_dropbox_migrator.icloud import FileState, ICloudManager


class ICloudManagerTests(unittest.TestCase):
    def test_icloud_action_path_uses_package_root(self) -> None:
        manager = ICloudManager(helper_path=Path("/tmp/fake.jxa"))
        path = Path("/tmp/example.key/Data/asset.mp4")

        self.assertEqual(manager._icloud_action_path(path), Path("/private/tmp/example.key"))

    def test_ensure_local_returns_immediately_when_file_is_already_local(self) -> None:
        manager = ICloudManager(helper_path=Path("/tmp/fake.jxa"))
        path = Path("/tmp/example.txt")
        state = FileState(path=path, size=10, flags=set())

        with mock.patch.object(manager, "inspect", return_value=state), mock.patch.object(
            manager, "start_download"
        ) as start_download, mock.patch.object(manager, "_verify_readable") as verify_readable:
            result = manager.ensure_local_file(path, timeout_seconds=10, poll_seconds=0)

        self.assertEqual(result, state)
        start_download.assert_not_called()
        verify_readable.assert_called_once_with(path)

    def test_ensure_local_uses_package_root_for_download(self) -> None:
        manager = ICloudManager(helper_path=Path("/tmp/fake.jxa"))
        path = Path("/tmp/example.key/Data/asset.mp4")
        package_root = Path("/tmp/example.key")

        states = [
            FileState(path=package_root, size=10, flags={"dataless"}),
            FileState(path=package_root, size=10, flags=set()),
            FileState(path=package_root, size=10, flags=set()),
            FileState(path=path, size=10, flags=set()),
        ]

        with mock.patch.object(manager, "start_download") as start_download, mock.patch.object(
            manager, "inspect", side_effect=states
        ), mock.patch.object(manager, "_verify_readable") as verify_readable, mock.patch(
            "icloud_dropbox_migrator.icloud.time.sleep"
        ):
            state = manager.ensure_local_file(path, timeout_seconds=10, poll_seconds=0)

        start_download.assert_called_once_with(path)
        verify_readable.assert_called_once_with(path)
        self.assertEqual(state.path, path)
        self.assertFalse(state.is_dataless)

    def test_ensure_local_waits_for_non_dataless_state(self) -> None:
        manager = ICloudManager(helper_path=Path("/tmp/fake.jxa"))
        path = Path("/tmp/example.txt")

        states = [
            FileState(path=path, size=10, flags={"dataless"}),
            FileState(path=path, size=10, flags=set()),
            FileState(path=path, size=10, flags=set()),
            FileState(path=path, size=10, flags=set()),
        ]

        with mock.patch.object(manager, "start_download") as start_download, mock.patch.object(
            manager, "inspect", side_effect=states
        ), mock.patch.object(manager, "_verify_readable") as verify_readable, mock.patch(
            "icloud_dropbox_migrator.icloud.time.sleep"
        ):
            state = manager.ensure_local_file(path, timeout_seconds=10, poll_seconds=0)

        start_download.assert_called_once_with(path)
        verify_readable.assert_called_once_with(path)
        self.assertFalse(state.is_dataless)
