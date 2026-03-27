# iCloud Dropbox Migrator

This project migrates an iCloud folder tree into Dropbox without requiring the entire source tree to be hydrated locally at once. It scans the source tree into SQLite, then processes one file at a time:

1. Trigger iCloud download for the next file.
2. Wait until the file is no longer marked `dataless`.
3. Upload the file directly to Dropbox over the API.
4. Evict the local copy from disk while leaving the iCloud item intact.
5. Continue with the next file.

## Requirements

- macOS with iCloud Drive
- Python 3.11+
- A Dropbox app with `files.content.write`
- Dropbox credentials via either:
  - `DROPBOX_REFRESH_TOKEN`, `DROPBOX_APP_KEY`, and `DROPBOX_APP_SECRET`
  - or the same three values stored in the macOS Keychain
  - or a fallback `DROPBOX_ACCESS_TOKEN`

## Dropbox Setup

Create a scoped Dropbox app, enable the permissions you need, and use offline access so Dropbox returns a refresh token. The CLI can help with the setup:

```bash
export PYTHONPATH=src
export DROPBOX_APP_KEY="..."
export DROPBOX_APP_SECRET="..."
export DROPBOX_REDIRECT_URI="http://localhost:8765/dropbox/callback"

python -m icloud_dropbox_migrator.cli dropbox-auth-url \
  --scope files.content.write
```

Open the printed URL, approve the app, and copy the returned `code` from the redirect URL. Then exchange it:

```bash
python -m icloud_dropbox_migrator.cli dropbox-exchange-code \
  --code "PASTE_AUTHORIZATION_CODE_HERE"
```

That command prints `export` lines for the access token and refresh token. Keep these set in your shell:

```bash
export DROPBOX_APP_KEY="..."
export DROPBOX_APP_SECRET="..."
export DROPBOX_REFRESH_TOKEN="..."
```

To stop depending on `op run` for every invocation, you can store the app key,
app secret, and refresh token in the macOS Keychain once:

```bash
export DROPBOX_APP_KEY="..."
export DROPBOX_APP_SECRET="..."
export DROPBOX_REFRESH_TOKEN="..."

python -m icloud_dropbox_migrator.cli dropbox-keychain-store
```

After that, runtime commands like `run` and `dropbox-whoami` can use the
stored Keychain values even when the corresponding environment variables are
unset.

You can verify the configured Dropbox account with:

```bash
python -m icloud_dropbox_migrator.cli dropbox-whoami
```

## Commands

```bash
export PYTHONPATH=src

python -m icloud_dropbox_migrator.cli scan \
  --source-root "$HOME/Library/Mobile Documents/iCloud~md~obsidian"

python -m icloud_dropbox_migrator.cli run \
  --source-root "$HOME/Library/Mobile Documents/iCloud~md~obsidian" \
  --dropbox-root "/Migrated/iCloud~md~obsidian"

python -m icloud_dropbox_migrator.cli status
python -m icloud_dropbox_migrator.cli retry-failed
```

By default the SQLite database is stored at `./migration_state.sqlite3`.
