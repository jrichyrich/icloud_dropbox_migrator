# Codex Handoff

## Current State

- Repository: `icloud_dropbox_migrator`
- Status: initial implementation complete
- Tests: passing with `PYTHONPATH=src python3 -m unittest discover -s tests`
- Source root to migrate:
  - `/Users/jasricha/Library/Mobile Documents/com~apple~Keynote/Documents/Gospel Doctrine`
- Migration behavior:
  - scans the source tree into SQLite
  - downloads one iCloud file at a time
  - uploads directly to Dropbox
  - evicts the local copy after upload
  - preserves the relative folder structure in Dropbox

## Implemented Commands

- `scan`
- `run`
- `status`
- `retry-failed`
- `dropbox-auth-url`
- `dropbox-exchange-code`
- `dropbox-whoami`

Main code:

- `src/icloud_dropbox_migrator/cli.py`
- `src/icloud_dropbox_migrator/worker.py`
- `src/icloud_dropbox_migrator/db.py`
- `src/icloud_dropbox_migrator/dropbox_api.py`
- `src/icloud_dropbox_migrator/icloud.py`
- `src/icloud_dropbox_migrator/native_icloud.jxa`

## 1Password State

Verified 1Password item:

- Vault: `Private`
- Item title: `Dropbox iCloud to Dropbox Migrator`

Verified fields currently present:

- `App key`
- `App secret`

Exact secret references for those:

```bash
export DROPBOX_APP_KEY='op://Private/Dropbox iCloud to Dropbox Migrator/App key'
export DROPBOX_APP_SECRET='op://Private/Dropbox iCloud to Dropbox Migrator/App secret'
```

Not yet present in the item:

- `Refresh token`

Planned secret reference once added:

```bash
export DROPBOX_REFRESH_TOKEN='op://Private/Dropbox iCloud to Dropbox Migrator/Refresh token'
```

## Important Warning

The app key and app secret were displayed in terminal output during setup. They should be rotated in the Dropbox app console before continuing.

## Next Steps

1. Rotate the Dropbox app key and app secret in Dropbox.
2. Update the 1Password item with the new `App key` and `App secret`.
3. Generate the Dropbox OAuth URL:

```bash
cd /Users/jasricha/Documents/Github_Personal/icloud_dropbox_migrator
export PYTHONPATH=src
export DROPBOX_APP_KEY='op://Private/Dropbox iCloud to Dropbox Migrator/App key'
export DROPBOX_APP_SECRET='op://Private/Dropbox iCloud to Dropbox Migrator/App secret'
export DROPBOX_REDIRECT_URI='http://localhost:8765/dropbox/callback'

op run -- python3 -m icloud_dropbox_migrator.cli dropbox-auth-url --scope files.content.write
```

4. Open the printed Dropbox URL, approve the app, and copy the `code` value from the redirect URL.
5. Exchange the code for tokens:

```bash
op run -- python3 -m icloud_dropbox_migrator.cli dropbox-exchange-code --code 'PASTE_CODE_HERE'
```

6. Add a new field to the same 1Password item:
   - label: `Refresh token`
   - value: the refresh token returned by the exchange command

7. Set the runtime environment:

```bash
cd /Users/jasricha/Documents/Github_Personal/icloud_dropbox_migrator
export PYTHONPATH=src
export DROPBOX_APP_KEY='op://Private/Dropbox iCloud to Dropbox Migrator/App key'
export DROPBOX_APP_SECRET='op://Private/Dropbox iCloud to Dropbox Migrator/App secret'
export DROPBOX_REFRESH_TOKEN='op://Private/Dropbox iCloud to Dropbox Migrator/Refresh token'
```

8. Verify Dropbox auth:

```bash
op run -- python3 -m icloud_dropbox_migrator.cli dropbox-whoami
```

9. Run the first safe migration pass:

```bash
op run -- python3 -m icloud_dropbox_migrator.cli scan \
  --source-root "/Users/jasricha/Library/Mobile Documents/com~apple~Keynote/Documents/Gospel Doctrine"

op run -- python3 -m icloud_dropbox_migrator.cli run \
  --source-root "/Users/jasricha/Library/Mobile Documents/com~apple~Keynote/Documents/Gospel Doctrine" \
  --dropbox-root "/Migrated/Gospel Doctrine" \
  --max-files 1
```

10. If that succeeds, remove `--max-files 1` and run the full migration.

## Notes

- The worker uses SQLite state in `migration_state.sqlite3` by default.
- The CLI expects `--db` after the subcommand, not before it.
- Current Dropbox auth supports:
  - refresh token plus app key/app secret
  - fallback direct access token
- The 1Password CLI worked in the user's own terminal, but not reliably in the sandboxed Codex session.
