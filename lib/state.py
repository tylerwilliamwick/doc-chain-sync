"""
Sync state tracking for Doc Chain.

Maintains a JSON file mapping vault file paths to their sync status:
- Last modified time seen
- Notion page ID (if synced)
- Google Drive file ID or path (if synced)
- Last sync timestamp

This prevents re-syncing unchanged files and enables incremental updates.
"""

import json
import os
from datetime import datetime
from pathlib import Path


class SyncState:
    """Tracks sync state for vault files across Notion and Google Drive."""

    def __init__(self, state_file: str):
        self.state_file = Path(state_file).expanduser()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        """Load state from disk. Returns empty dict if file missing or corrupt."""
        if not self.state_file.exists():
            return {"files": {}, "last_run": None}
        try:
            with open(self.state_file, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"files": {}, "last_run": None}

    def save(self):
        """Persist state to disk."""
        self._data["last_run"] = datetime.now().isoformat()
        with open(self.state_file, "w") as f:
            json.dump(self._data, f, indent=2)

    def get_file_state(self, vault_path: str) -> dict:
        """Get sync state for a specific vault file."""
        return self._data["files"].get(vault_path, {})

    def needs_sync(self, vault_path: str, current_mtime: float) -> bool:
        """Check if a file needs syncing based on modification time."""
        file_state = self.get_file_state(vault_path)
        if not file_state:
            return True
        last_mtime = file_state.get("last_mtime", 0)
        return current_mtime > last_mtime

    def record_sync(self, vault_path: str, mtime: float,
                    notion_page_id: str = None, gdrive_path: str = None):
        """Record a successful sync for a file."""
        if vault_path not in self._data["files"]:
            self._data["files"][vault_path] = {}

        entry = self._data["files"][vault_path]
        entry["last_mtime"] = mtime
        entry["last_synced"] = datetime.now().isoformat()

        if notion_page_id is not None:
            entry["notion_page_id"] = notion_page_id
        if gdrive_path is not None:
            entry["gdrive_path"] = gdrive_path

    def record_failure(self, vault_path: str, target: str, error: str):
        """Record a sync failure for debugging."""
        if vault_path not in self._data["files"]:
            self._data["files"][vault_path] = {}

        entry = self._data["files"][vault_path]
        if "errors" not in entry:
            entry["errors"] = []

        entry["errors"].append({
            "target": target,
            "error": error,
            "timestamp": datetime.now().isoformat()
        })
        # Keep only last 5 errors per file
        entry["errors"] = entry["errors"][-5:]

    def get_notion_page_id(self, vault_path: str) -> str:
        """Get the Notion page ID for a previously synced file (for updates)."""
        return self.get_file_state(vault_path).get("notion_page_id")

    def get_all_synced_files(self) -> dict:
        """Return all tracked files and their states."""
        return dict(self._data["files"])

    def remove_file(self, vault_path: str):
        """Remove tracking for a deleted file."""
        self._data["files"].pop(vault_path, None)

    @property
    def last_run(self) -> str:
        return self._data.get("last_run")
