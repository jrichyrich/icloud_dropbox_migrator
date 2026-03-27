from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib import error, parse, request

from .keychain import KeychainError, load_dropbox_credentials


API_URL = "https://api.dropboxapi.com"
CONTENT_URL = "https://content.dropboxapi.com"
AUTHORIZE_URL = "https://www.dropbox.com/oauth2/authorize"
TOKEN_URL = "https://api.dropbox.com/oauth2/token"
CHUNK_SIZE = 8 * 1024 * 1024
SIMPLE_UPLOAD_LIMIT = 150 * 1024 * 1024
TOKEN_REFRESH_SKEW_SECONDS = 60


class DropboxError(RuntimeError):
    pass


@dataclass(slots=True)
class UploadResult:
    path_display: str
    path_lower: str
    dropbox_id: str


@dataclass(slots=True)
class DropboxTokenSet:
    access_token: str
    expires_in: int | None = None
    refresh_token: str | None = None
    scope: str | None = None
    account_id: str | None = None
    uid: str | None = None
    token_type: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DropboxTokenSet":
        return cls(
            access_token=str(payload["access_token"]),
            expires_in=int(payload["expires_in"]) if payload.get("expires_in") is not None else None,
            refresh_token=payload.get("refresh_token"),
            scope=payload.get("scope"),
            account_id=payload.get("account_id"),
            uid=str(payload["uid"]) if payload.get("uid") is not None else None,
            token_type=payload.get("token_type"),
        )


class DropboxClient:
    def __init__(
        self,
        *,
        access_token: str | None = None,
        refresh_token: str | None = None,
        app_key: str | None = None,
        app_secret: str | None = None,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._app_key = app_key
        self._app_secret = app_secret
        self._access_token_expires_at: float | None = None

    @classmethod
    def from_env(cls) -> "DropboxClient":
        access_token = os.environ.get("DROPBOX_ACCESS_TOKEN")
        refresh_token = os.environ.get("DROPBOX_REFRESH_TOKEN")
        app_key = os.environ.get("DROPBOX_APP_KEY")
        app_secret = os.environ.get("DROPBOX_APP_SECRET")
        if not refresh_token or not app_key or not app_secret:
            try:
                keychain_credentials = load_dropbox_credentials()
            except KeychainError as exc:
                raise DropboxError(str(exc)) from exc
            refresh_token = refresh_token or keychain_credentials.refresh_token
            app_key = app_key or keychain_credentials.app_key
            app_secret = app_secret or keychain_credentials.app_secret

        if refresh_token:
            if not app_key or not app_secret:
                raise DropboxError(
                    "DROPBOX_REFRESH_TOKEN requires DROPBOX_APP_KEY and DROPBOX_APP_SECRET"
                )
            return cls(
                access_token=access_token,
                refresh_token=refresh_token,
                app_key=app_key,
                app_secret=app_secret,
            )

        if not access_token:
            raise DropboxError(
                "set DROPBOX_ACCESS_TOKEN, or set DROPBOX_REFRESH_TOKEN with "
                "DROPBOX_APP_KEY and DROPBOX_APP_SECRET in the environment or macOS Keychain"
            )
        return cls(access_token=access_token)

    @staticmethod
    def build_authorize_url(
        *,
        app_key: str,
        redirect_uri: str | None = None,
        state: str | None = None,
        scopes: list[str] | None = None,
    ) -> str:
        query: dict[str, str] = {
            "client_id": app_key,
            "response_type": "code",
            "token_access_type": "offline",
        }
        if redirect_uri:
            query["redirect_uri"] = redirect_uri
        if state:
            query["state"] = state
        if scopes:
            query["scope"] = " ".join(scopes)
        return f"{AUTHORIZE_URL}?{parse.urlencode(query)}"

    @classmethod
    def exchange_code_for_tokens(
        cls,
        *,
        app_key: str,
        app_secret: str,
        code: str,
        redirect_uri: str | None = None,
    ) -> DropboxTokenSet:
        form = {
            "code": code,
            "grant_type": "authorization_code",
            "client_id": app_key,
            "client_secret": app_secret,
        }
        if redirect_uri:
            form["redirect_uri"] = redirect_uri
        payload = cls._form_request(TOKEN_URL, form)
        return DropboxTokenSet.from_dict(payload)

    def upload_file(self, local_path: Path, dropbox_root: str, relative_path: str) -> UploadResult:
        destination = self._build_destination(dropbox_root, relative_path)
        file_size = local_path.stat().st_size
        if file_size <= SIMPLE_UPLOAD_LIMIT:
            return self._simple_upload(local_path, destination)
        return self._session_upload(local_path, destination)

    def get_current_account(self) -> dict[str, Any]:
        return self._api_request("/2/users/get_current_account", None)

    def _simple_upload(self, local_path: Path, destination: str) -> UploadResult:
        with local_path.open("rb") as handle:
            payload = handle.read()
        response = self._content_request(
            "/2/files/upload",
            payload,
            {
                "path": destination,
                "mode": "add",
                "autorename": False,
                "mute": False,
                "strict_conflict": True,
            },
        )
        return UploadResult(
            path_display=str(response["path_display"]),
            path_lower=str(response["path_lower"]),
            dropbox_id=str(response["id"]),
        )

    def _session_upload(self, local_path: Path, destination: str) -> UploadResult:
        file_size = local_path.stat().st_size
        with local_path.open("rb") as handle:
            first_chunk = handle.read(CHUNK_SIZE)
            start_response = self._content_request(
                "/2/files/upload_session/start",
                first_chunk,
                {"close": False},
            )
            session_id = str(start_response["session_id"])
            offset = len(first_chunk)

            while True:
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                is_last = offset + len(chunk) >= file_size
                cursor = {"session_id": session_id, "offset": offset}
                if is_last:
                    finish_response = self._content_request(
                        "/2/files/upload_session/finish",
                        chunk,
                        {
                            "cursor": cursor,
                            "commit": {
                                "path": destination,
                                "mode": "add",
                                "autorename": False,
                                "mute": False,
                                "strict_conflict": True,
                            },
                        },
                    )
                    return UploadResult(
                        path_display=str(finish_response["path_display"]),
                        path_lower=str(finish_response["path_lower"]),
                        dropbox_id=str(finish_response["id"]),
                    )
                self._content_request(
                    "/2/files/upload_session/append_v2",
                    chunk,
                    {"cursor": cursor, "close": False},
                )
                offset += len(chunk)

        raise DropboxError(f"upload session for {local_path} did not finish")

    def _content_request(self, path: str, body: bytes, api_arg: dict[str, object]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Content-Type": "application/octet-stream",
            "Dropbox-API-Arg": json.dumps(api_arg, separators=(",", ":")),
        }
        req = request.Request(
            f"{CONTENT_URL}{path}",
            data=body,
            headers=headers,
            method="POST",
        )
        return self._json_response(req)

    def _api_request(self, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        if payload is None:
            body = b"null"
        else:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Content-Type": "application/json",
        }
        req = request.Request(
            f"{API_URL}{path}",
            data=body,
            headers=headers,
            method="POST",
        )
        return self._json_response(req)

    def _get_access_token(self) -> str:
        if self._access_token and self._token_is_fresh():
            return self._access_token
        if self._refresh_token:
            token_set = self.refresh_access_token()
            return token_set.access_token
        if self._access_token:
            return self._access_token
        raise DropboxError("no Dropbox access token available")

    def _token_is_fresh(self) -> bool:
        if self._access_token is None:
            return False
        if self._access_token_expires_at is None:
            return True
        return time.time() + TOKEN_REFRESH_SKEW_SECONDS < self._access_token_expires_at

    def refresh_access_token(self) -> DropboxTokenSet:
        if not self._refresh_token or not self._app_key or not self._app_secret:
            raise DropboxError("refresh token auth requires refresh token, app key, and app secret")
        payload = self._form_request(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._app_key,
                "client_secret": self._app_secret,
            },
        )
        token_set = DropboxTokenSet.from_dict(payload)
        self._access_token = token_set.access_token
        if token_set.expires_in is not None:
            self._access_token_expires_at = time.time() + token_set.expires_in
        else:
            self._access_token_expires_at = None
        return token_set

    @classmethod
    def _form_request(cls, url: str, form: dict[str, str]) -> dict[str, Any]:
        encoded = parse.urlencode(form).encode("utf-8")
        req = request.Request(
            url,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return cls._json_response(req)

    @staticmethod
    def _json_response(req: request.Request) -> dict[str, Any]:
        try:
            with request.urlopen(req) as response:
                payload = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise DropboxError(f"Dropbox API returned {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise DropboxError(f"Dropbox request failed: {exc.reason}") from exc

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise DropboxError(f"invalid Dropbox JSON response: {payload!r}") from exc
        if not isinstance(data, dict):
            raise DropboxError(f"unexpected Dropbox response: {payload!r}")
        return data

    @staticmethod
    def _build_destination(dropbox_root: str, relative_path: str) -> str:
        clean_root = "/" + dropbox_root.strip("/")
        relative = PurePosixPath(relative_path)
        return str(PurePosixPath(clean_root) / relative)
