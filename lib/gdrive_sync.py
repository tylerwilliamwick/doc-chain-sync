"""
Google Drive sync module for Doc Chain.

Copies vault files to Google Drive so NotebookLM can auto-ingest them.
Supports two modes:

1. desktop_app: Copies files to the local Google Drive folder.
   Requires Google Drive for Desktop installed and syncing.

2. rclone: Uses rclone CLI to upload files to a configured remote.
   Requires rclone installed and configured with a remote named in config.

The destination folder structure mirrors the vault:
  NotebookLM-Vault/
    Projects/
    Decisions/
    Plans/
"""

import os
import shutil
import subprocess
from pathlib import Path


class GDriveSyncError(Exception):
    """Raised when Google Drive sync fails."""
    pass


class GDriveSync:
    """Syncs vault files to Google Drive for NotebookLM ingestion."""

    def __init__(self, config: dict):
        self.enabled = config.get("enabled", True)
        self.mode = config.get("mode", "rclone")
        self.desktop_app_path = Path(
            config.get("desktop_app_path", "~/Google Drive/My Drive/NotebookLM-Vault")
        ).expanduser()
        self.rclone_remote = config.get("rclone_remote", "gdrive")
        self.rclone_dest = config.get("rclone_dest", "NotebookLM-Vault")
        self.mirror_structure = config.get("mirror_structure", True)

    def is_configured(self) -> bool:
        """Check if the selected mode is available."""
        if self.mode == "desktop_app":
            return self.desktop_app_path.parent.exists()
        elif self.mode == "rclone":
            return self._rclone_available()
        return False

    def _rclone_available(self) -> bool:
        """Check if rclone is installed and the remote is configured."""
        try:
            result = subprocess.run(
                ["rclone", "listremotes"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return False
            remotes = result.stdout.strip().split("\n")
            return f"{self.rclone_remote}:" in remotes
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _get_dest_path(self, source_folder: str, file_name: str) -> str:
        """Build the destination path within the Drive folder."""
        if self.mirror_structure:
            return f"{source_folder}/{file_name}"
        return file_name

    def sync_file_desktop(self, file_path: Path, source_folder: str) -> str:
        """Copy a file to the local Google Drive folder.

        Returns the destination path.
        """
        rel_path = self._get_dest_path(source_folder, file_path.name)
        dest = self.desktop_app_path / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(file_path, dest)
        return str(dest)

    def sync_file_rclone(self, file_path: Path, source_folder: str) -> str:
        """Upload a file to Google Drive via rclone.

        Returns the remote path.
        """
        rel_path = self._get_dest_path(source_folder, file_path.name)
        remote_dest = f"{self.rclone_remote}:{self.rclone_dest}/{rel_path}"

        # Ensure remote directory exists
        remote_dir = f"{self.rclone_remote}:{self.rclone_dest}/{source_folder}"
        mkdir_result = subprocess.run(
            ["rclone", "mkdir", remote_dir],
            capture_output=True, text=True, timeout=30
        )
        if mkdir_result.returncode != 0:
            raise GDriveSyncError(
                f"rclone mkdir failed for {remote_dir}: {mkdir_result.stderr.strip()}"
            )

        # Copy the file
        result = subprocess.run(
            ["rclone", "copyto", str(file_path), remote_dest],
            capture_output=True, text=True, timeout=60
        )

        if result.returncode != 0:
            raise GDriveSyncError(
                f"rclone copy failed for {file_path.name}: {result.stderr.strip()}"
            )

        return remote_dest

    def sync_file(self, file_path: Path, source_folder: str) -> str:
        """Sync a single file to Google Drive using the configured mode.

        Returns the destination path (local or remote).
        """
        if not self.is_configured():
            raise GDriveSyncError(
                f"Google Drive sync not configured. Mode: {self.mode}. "
                f"{'Install Google Drive for Desktop' if self.mode == 'desktop_app' else 'Install and configure rclone'}."
            )

        if self.mode == "desktop_app":
            return self.sync_file_desktop(file_path, source_folder)
        elif self.mode == "rclone":
            return self.sync_file_rclone(file_path, source_folder)
        else:
            raise GDriveSyncError(f"Unknown sync mode: {self.mode}")

    def delete_file(self, source_folder: str, file_name: str) -> bool:
        """Remove a file from Drive (for cleanup of deleted vault files).

        Returns True if successful.
        """
        rel_path = self._get_dest_path(source_folder, file_name)

        if self.mode == "desktop_app":
            dest = self.desktop_app_path / rel_path
            if dest.exists():
                dest.unlink()
                return True
            return False
        elif self.mode == "rclone":
            remote_path = f"{self.rclone_remote}:{self.rclone_dest}/{rel_path}"
            result = subprocess.run(
                ["rclone", "deletefile", remote_path],
                capture_output=True, text=True, timeout=30
            )
            return result.returncode == 0
        return False
