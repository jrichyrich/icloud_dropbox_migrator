from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from icloud_dropbox_migrator.keychain import (
    APP_KEY_ACCOUNT,
    APP_SECRET_ACCOUNT,
    KEYCHAIN_SERVICE,
    REFRESH_TOKEN_ACCOUNT,
    DropboxKeychainCredentials,
    KeychainError,
    load_dropbox_credentials,
    read_password,
    store_dropbox_credentials,
)


class KeychainTests(unittest.TestCase):
    @mock.patch("icloud_dropbox_migrator.keychain.subprocess.run")
    def test_read_password_returns_none_for_missing_item(self, run: mock.Mock) -> None:
        run.side_effect = subprocess.CalledProcessError(
            44,
            ["security"],
            stderr="security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.",
        )

        value = read_password(APP_KEY_ACCOUNT)

        self.assertIsNone(value)

    @mock.patch("icloud_dropbox_migrator.keychain.subprocess.run")
    def test_load_dropbox_credentials_reads_expected_accounts(self, run: mock.Mock) -> None:
        run.side_effect = [
            mock.Mock(stdout="app-key\n"),
            mock.Mock(stdout="app-secret\n"),
            mock.Mock(stdout="refresh-token\n"),
        ]

        credentials = load_dropbox_credentials()

        self.assertEqual(
            credentials,
            DropboxKeychainCredentials(
                app_key="app-key",
                app_secret="app-secret",
                refresh_token="refresh-token",
            ),
        )
        called_accounts = [call.args[0][3] for call in run.call_args_list]
        self.assertEqual(called_accounts, [APP_KEY_ACCOUNT, APP_SECRET_ACCOUNT, REFRESH_TOKEN_ACCOUNT])

    @mock.patch("icloud_dropbox_migrator.keychain.subprocess.run")
    def test_store_dropbox_credentials_updates_keychain_items(self, run: mock.Mock) -> None:
        stored = store_dropbox_credentials(
            app_key="app-key",
            app_secret="app-secret",
            refresh_token="refresh-token",
        )

        self.assertEqual(stored, ["app_key", "app_secret", "refresh_token"])
        first_call = run.call_args_list[0].args[0]
        self.assertEqual(first_call[:2], ["security", "add-generic-password"])
        self.assertIn(KEYCHAIN_SERVICE, first_call)
        self.assertIn("-U", first_call)

    @mock.patch("icloud_dropbox_migrator.keychain.subprocess.run")
    def test_store_dropbox_credentials_raises_keychain_error(self, run: mock.Mock) -> None:
        run.side_effect = subprocess.CalledProcessError(
            1,
            ["security"],
            stderr="write failed",
        )

        with self.assertRaises(KeychainError):
            store_dropbox_credentials(app_key="app-key")
