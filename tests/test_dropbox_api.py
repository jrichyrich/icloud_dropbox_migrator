from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from icloud_dropbox_migrator.dropbox_api import DropboxClient, DropboxTokenSet, UploadResult


class DropboxClientTests(unittest.TestCase):
    def test_build_destination_preserves_tree(self) -> None:
        destination = DropboxClient._build_destination("/Root", "nested/file.txt")
        self.assertEqual(destination, "/Root/nested/file.txt")

    def test_build_authorize_url_requests_offline_access(self) -> None:
        url = DropboxClient.build_authorize_url(
            app_key="app-key",
            redirect_uri="http://localhost/callback",
            state="abc123",
            scopes=["files.content.write"],
        )
        self.assertIn("token_access_type=offline", url)
        self.assertIn("client_id=app-key", url)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%2Fcallback", url)
        self.assertIn("state=abc123", url)
        self.assertIn("scope=files.content.write", url)

    @mock.patch.dict(
        os.environ,
        {
            "DROPBOX_REFRESH_TOKEN": "refresh-token",
            "DROPBOX_APP_KEY": "app-key",
            "DROPBOX_APP_SECRET": "app-secret",
        },
        clear=True,
    )
    def test_from_env_prefers_refresh_token_auth(self) -> None:
        client = DropboxClient.from_env()
        self.assertEqual(client._refresh_token, "refresh-token")
        self.assertEqual(client._app_key, "app-key")
        self.assertEqual(client._app_secret, "app-secret")

    @mock.patch("icloud_dropbox_migrator.dropbox_api.DropboxClient._content_request")
    def test_simple_upload_returns_structured_result(self, content_request: mock.Mock) -> None:
        content_request.return_value = {
            "path_display": "/Root/file.txt",
            "path_lower": "/root/file.txt",
            "id": "id:abc",
        }
        client = DropboxClient(access_token="token")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "file.txt"
            path.write_text("hello", encoding="utf-8")
            result = client.upload_file(path, "/Root", "file.txt")
        self.assertEqual(
            result,
            UploadResult(path_display="/Root/file.txt", path_lower="/root/file.txt", dropbox_id="id:abc"),
        )
        self.assertEqual(content_request.call_args.args[0], "/2/files/upload")

    @mock.patch("icloud_dropbox_migrator.dropbox_api.DropboxClient._form_request")
    def test_exchange_code_for_tokens_returns_refresh_token(self, form_request: mock.Mock) -> None:
        form_request.return_value = {
            "access_token": "access-token",
            "expires_in": 14400,
            "refresh_token": "refresh-token",
            "token_type": "bearer",
            "scope": "files.content.write",
            "account_id": "dbid:123",
            "uid": "42",
        }
        tokens = DropboxClient.exchange_code_for_tokens(
            app_key="app-key",
            app_secret="app-secret",
            code="auth-code",
            redirect_uri="http://localhost/callback",
        )
        self.assertEqual(tokens.refresh_token, "refresh-token")
        self.assertEqual(tokens.access_token, "access-token")
        self.assertEqual(form_request.call_args.args[0], "https://api.dropbox.com/oauth2/token")
        self.assertEqual(form_request.call_args.args[1]["grant_type"], "authorization_code")

    @mock.patch("icloud_dropbox_migrator.dropbox_api.DropboxClient._form_request")
    def test_refresh_access_token_caches_short_lived_token(self, form_request: mock.Mock) -> None:
        form_request.return_value = {
            "access_token": "new-access-token",
            "expires_in": 3600,
            "token_type": "bearer",
            "scope": "files.content.write",
        }
        client = DropboxClient(
            refresh_token="refresh-token",
            app_key="app-key",
            app_secret="app-secret",
        )

        first = client._get_access_token()
        second = client._get_access_token()

        self.assertEqual(first, "new-access-token")
        self.assertEqual(second, "new-access-token")
        self.assertEqual(form_request.call_count, 1)
        self.assertIsNotNone(client._access_token_expires_at)

    @mock.patch("icloud_dropbox_migrator.dropbox_api.DropboxClient._json_response")
    @mock.patch("icloud_dropbox_migrator.dropbox_api.request.Request")
    def test_get_current_account_posts_null_body(
        self,
        request_ctor: mock.Mock,
        json_response: mock.Mock,
    ) -> None:
        request_ctor.return_value = mock.sentinel.request
        json_response.return_value = {"account_id": "dbid:123"}

        client = DropboxClient(access_token="token")
        result = client.get_current_account()

        self.assertEqual(result, {"account_id": "dbid:123"})
        self.assertEqual(request_ctor.call_args.kwargs["data"], b"null")
        self.assertEqual(request_ctor.call_args.kwargs["method"], "POST")
