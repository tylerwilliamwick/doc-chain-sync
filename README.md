# Doc Chain Sync

Automated pipeline that syncs Obsidian vault deliverables to Notion and Google Drive. Designed for NotebookLM ingestion from the Drive folder.

## What it does

Watches configured vault folders and syncs changed `.md` files to:

- **Notion**: Creates/updates pages in a database with metadata (source folder, sync timestamp, vault path)
- **Google Drive**: Mirrors the folder structure via rclone for NotebookLM auto-ingestion

No LaunchAgent is installed by this checkout. After local paths, credentials, and destinations are configured, it supports 5-minute polling. Incremental: only syncs files that changed since the last run.

Current checkout status: runtime is inactive until Notion and Google Drive targets are configured. The latest local log reports both targets disabled or unconfigured and no sync state. Passing tests cover mocked integrations, not live delivery.

## Setup

### Prerequisites

- Python 3.9+
- [rclone](https://rclone.org/) (`brew install rclone`)
- A [Notion internal integration](https://www.notion.so/my-integrations) with read/insert/update capabilities
- A Notion database with these properties:
  - **Name** (title)
  - **Source** (select: Projects, Decisions, Plans)
  - **Synced At** (date)
  - **File Name** (rich text)
  - **Vault Path** (rich text)

### Configuration

1. Copy `config.yaml` and adjust paths if needed
2. Configure rclone with a Google Drive remote: `rclone config`
3. Set environment variables:
   ```bash
   export NOTION_DOC_CHAIN_TOKEN="your-notion-integration-token"
   export NOTION_DOC_CHAIN_DB="your-notion-database-id"
   ```

### Running

**Manual run:**
```bash
python3 dispatcher.py --verbose
```

**Dry run (logs what would sync without syncing):**
```bash
python3 dispatcher.py --dry-run --verbose
```

**Force re-sync all files:**
```bash
python3 dispatcher.py --force --verbose
```

**Require at least one configured target (useful for automation):**
```bash
python3 dispatcher.py --require-target
```

Without `--require-target`, unavailable targets remain a successful no-op for backward compatibility. With it, the process exits 1 when both Notion and Google Drive are unavailable.

**As a LaunchAgent (macOS):**

Create a plist at `~/Library/LaunchAgents/com.tylerwick.doc-chain-sync.plist` pointing to `dispatcher.py` with the Notion env vars in `EnvironmentVariables`. See the SDLC package for the full plist template.

## Architecture

```
dispatcher.py          # Entry point: scans vault, orchestrates sync
lib/
  notion_sync.py       # Notion REST API client (stdlib only, no SDK)
  gdrive_sync.py       # Google Drive sync via rclone CLI or Desktop app
  state.py             # JSON-based incremental sync state tracker
  logger.py            # Rotating file logger
config.yaml            # All configuration (paths, remotes, property names)
tests/                 # 98 automated tests
```

**No external dependencies** beyond PyYAML. The Notion client uses `urllib` directly. Google Drive sync shells out to `rclone`.

## Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

## Config reference

See `config.yaml` for all options. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `vault.base_path` | `~/Ty` | Root of the Obsidian vault |
| `vault.watch_folders` | Configured in `config.yaml` | Subdirectories to sync |
| `google_drive.mode` | `rclone` | `rclone` (headless) or `desktop_app` (local folder copy) |
| `google_drive.rclone_remote` | `gdrive` | Name of the rclone remote |
| `google_drive.rclone_dest` | `NotebookLM-Vault` | Destination folder on Drive |
| `sync.poll_interval` | 300 | Seconds between polls (LaunchAgent) |

## License

MIT
