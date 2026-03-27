from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from .db import StateStore
from .dropbox_api import DropboxClient, DropboxError, DropboxTokenSet
from .keychain import KeychainError, load_dropbox_credentials, store_dropbox_credentials
from .worker import MigrationWorker, default_db_path, scan_source_tree


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="icloud-dropbox-migrator")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=default_db_path(), help="SQLite state file path")
    common.add_argument("--log-level", default="INFO", help="Python log level")

    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Scan an iCloud tree into SQLite", parents=[common])
    scan_parser.add_argument("--source-root", type=Path, required=True)

    run_parser = subparsers.add_parser("run", help="Run the migration worker", parents=[common])
    run_parser.add_argument("--source-root", type=Path, required=True)
    run_parser.add_argument("--dropbox-root", required=True)
    run_parser.add_argument("--max-files", type=int)
    run_parser.add_argument("--hydrate-timeout-seconds", type=int, default=3600)
    run_parser.add_argument("--retry-limit", type=int, default=3)
    run_parser.add_argument("--retry-delay-seconds", type=float, default=5.0)

    subparsers.add_parser("status", help="Show migration status", parents=[common])

    retry_parser = subparsers.add_parser(
        "retry-failed",
        help="Move failed items back to pending",
        parents=[common],
    )
    retry_parser.add_argument("--path-prefix")

    auth_url_parser = subparsers.add_parser(
        "dropbox-auth-url",
        help="Print the Dropbox OAuth URL for generating a refresh token",
    )
    auth_url_parser.add_argument("--app-key", default=os.environ.get("DROPBOX_APP_KEY"))
    auth_url_parser.add_argument("--redirect-uri", default=os.environ.get("DROPBOX_REDIRECT_URI"))
    auth_url_parser.add_argument("--state")
    auth_url_parser.add_argument(
        "--scope",
        action="append",
        default=[],
        help="Optional Dropbox scope. Can be passed multiple times.",
    )

    exchange_parser = subparsers.add_parser(
        "dropbox-exchange-code",
        help="Exchange a Dropbox authorization code for access and refresh tokens",
    )
    exchange_parser.add_argument("--app-key", default=os.environ.get("DROPBOX_APP_KEY"))
    exchange_parser.add_argument("--app-secret", default=os.environ.get("DROPBOX_APP_SECRET"))
    exchange_parser.add_argument("--redirect-uri", default=os.environ.get("DROPBOX_REDIRECT_URI"))
    exchange_parser.add_argument("--code", required=True)

    keychain_store_parser = subparsers.add_parser(
        "dropbox-keychain-store",
        help="Store Dropbox app credentials in the macOS Keychain",
    )
    keychain_store_parser.add_argument("--app-key", default=os.environ.get("DROPBOX_APP_KEY"))
    keychain_store_parser.add_argument("--app-secret", default=os.environ.get("DROPBOX_APP_SECRET"))
    keychain_store_parser.add_argument("--refresh-token", default=os.environ.get("DROPBOX_REFRESH_TOKEN"))

    subparsers.add_parser(
        "dropbox-whoami",
        help="Validate the configured Dropbox credentials and print the current account",
    )

    return parser


def require_value(parser: argparse.ArgumentParser, value: str | None, message: str) -> str:
    if value:
        return value
    parser.error(message)
    raise AssertionError("unreachable")


def resolve_dropbox_credential(
    parser: argparse.ArgumentParser,
    explicit_value: str | None,
    keychain_value: str | None,
    error_message: str,
) -> str:
    return require_value(parser, explicit_value or keychain_value, error_message)


def print_token_exports(token_set: DropboxTokenSet) -> None:
    print("token exchange complete")
    print(f"export DROPBOX_ACCESS_TOKEN='{token_set.access_token}'")
    if token_set.refresh_token:
        print(f"export DROPBOX_REFRESH_TOKEN='{token_set.refresh_token}'")
    if token_set.account_id:
        print(f"# account_id: {token_set.account_id}")
    if token_set.uid:
        print(f"# uid: {token_set.uid}")
    if token_set.scope:
        print(f"# scope: {token_set.scope}")
    if token_set.expires_in is not None:
        print(f"# access token expires in {token_set.expires_in} seconds")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        keychain_credentials = load_dropbox_credentials()
    except KeychainError as exc:
        parser.error(str(exc))

    logging.basicConfig(
        level=getattr(logging, getattr(args, "log_level", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.command == "scan":
        store = StateStore(args.db)
        result = scan_source_tree(store, args.source_root)
        print(
            f"scan complete: inserted={result.inserted} "
            f"updated={result.updated} skipped_dirs={result.skipped_dirs}"
        )
        return 0

    if args.command == "run":
        store = StateStore(args.db)
        try:
            client = DropboxClient.from_env()
        except DropboxError as exc:
            parser.error(str(exc))
        worker = MigrationWorker(
            store=store,
            source_root=args.source_root,
            dropbox_root=args.dropbox_root,
            dropbox_client=client,
            hydrate_timeout_seconds=args.hydrate_timeout_seconds,
            retry_limit=args.retry_limit,
            retry_delay_seconds=args.retry_delay_seconds,
        )
        summary = worker.run(max_files=args.max_files)
        print(
            "run complete: "
            f"processed={summary.processed} uploaded={summary.uploaded} "
            f"evicted={summary.evicted} failed={summary.failed} "
            f"recovered={summary.recovered}"
        )
        return 0

    if args.command == "status":
        store = StateStore(args.db)
        counts = store.status_counts()
        if not counts:
            print("no items in manifest")
            return 0
        for status, count in sorted(counts.items()):
            print(f"{status}: {count}")
        failures = store.recent_failures()
        if failures:
            print("")
            print("recent failures:")
            for failure in failures:
                print(
                    f"- {failure['relative_path']} "
                    f"(attempts={failure['attempt_count']}): {failure['last_error']}"
                )
        return 0

    if args.command == "retry-failed":
        store = StateStore(args.db)
        count = store.retry_failed(args.path_prefix)
        print(f"moved {count} failed items back to pending")
        return 0

    if args.command == "dropbox-auth-url":
        app_key = resolve_dropbox_credential(
            parser,
            args.app_key,
            keychain_credentials.app_key,
            "--app-key, DROPBOX_APP_KEY, or a stored macOS Keychain app key is required",
        )
        url = DropboxClient.build_authorize_url(
            app_key=app_key,
            redirect_uri=args.redirect_uri,
            state=args.state,
            scopes=args.scope or None,
        )
        print(url)
        return 0

    if args.command == "dropbox-exchange-code":
        app_key = resolve_dropbox_credential(
            parser,
            args.app_key,
            keychain_credentials.app_key,
            "--app-key, DROPBOX_APP_KEY, or a stored macOS Keychain app key is required",
        )
        app_secret = resolve_dropbox_credential(
            parser,
            args.app_secret,
            keychain_credentials.app_secret,
            "--app-secret, DROPBOX_APP_SECRET, or a stored macOS Keychain app secret is required",
        )
        token_set = DropboxClient.exchange_code_for_tokens(
            app_key=app_key,
            app_secret=app_secret,
            code=args.code,
            redirect_uri=args.redirect_uri,
        )
        print_token_exports(token_set)
        return 0

    if args.command == "dropbox-keychain-store":
        stored = store_dropbox_credentials(
            app_key=args.app_key,
            app_secret=args.app_secret,
            refresh_token=args.refresh_token,
        )
        if not stored:
            parser.error(
                "provide at least one of --app-key, --app-secret, or --refresh-token "
                "(or set the matching environment variables)"
            )
        print("stored Dropbox credentials in macOS Keychain: " + ", ".join(stored))
        return 0

    if args.command == "dropbox-whoami":
        try:
            client = DropboxClient.from_env()
        except DropboxError as exc:
            parser.error(str(exc))
        account = client.get_current_account()
        email = account.get("email", "")
        name = account.get("name", {}).get("display_name", "")
        account_id = account.get("account_id", "")
        print(f"name: {name}")
        print(f"email: {email}")
        print(f"account_id: {account_id}")
        return 0

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
