from __future__ import annotations

import subprocess
from dataclasses import dataclass


KEYCHAIN_SERVICE = "icloud-dropbox-migrator.dropbox"
APP_KEY_ACCOUNT = "dropbox-app-key"
APP_SECRET_ACCOUNT = "dropbox-app-secret"
REFRESH_TOKEN_ACCOUNT = "dropbox-refresh-token"


class KeychainError(RuntimeError):
    pass


@dataclass(slots=True)
class DropboxKeychainCredentials:
    app_key: str | None = None
    app_secret: str | None = None
    refresh_token: str | None = None


def load_dropbox_credentials() -> DropboxKeychainCredentials:
    return DropboxKeychainCredentials(
        app_key=read_password(APP_KEY_ACCOUNT),
        app_secret=read_password(APP_SECRET_ACCOUNT),
        refresh_token=read_password(REFRESH_TOKEN_ACCOUNT),
    )


def store_dropbox_credentials(
    *,
    app_key: str | None = None,
    app_secret: str | None = None,
    refresh_token: str | None = None,
) -> list[str]:
    stored: list[str] = []
    if app_key is not None:
        store_password(APP_KEY_ACCOUNT, app_key, label="Dropbox app key")
        stored.append("app_key")
    if app_secret is not None:
        store_password(APP_SECRET_ACCOUNT, app_secret, label="Dropbox app secret")
        stored.append("app_secret")
    if refresh_token is not None:
        store_password(REFRESH_TOKEN_ACCOUNT, refresh_token, label="Dropbox refresh token")
        stored.append("refresh_token")
    return stored


def read_password(account: str) -> str | None:
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise KeychainError("macOS security CLI is not available") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout).strip()
        if "could not be found" in detail.lower():
            return None
        raise KeychainError(f"failed to read keychain item {account!r}: {detail}") from exc
    return result.stdout.rstrip("\n")


def store_password(account: str, value: str, *, label: str) -> None:
    try:
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-a",
                account,
                "-s",
                KEYCHAIN_SERVICE,
                "-l",
                label,
                "-U",
                "-w",
                value,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise KeychainError("macOS security CLI is not available") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout).strip()
        raise KeychainError(f"failed to write keychain item {account!r}: {detail}") from exc
