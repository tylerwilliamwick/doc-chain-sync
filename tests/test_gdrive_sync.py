"""Tests for Google Drive sync module."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from gdrive_sync import GDriveSync, GDriveSyncError


class TestGDriveSyncDesktopMode(unittest.TestCase):
    """Tests for desktop_app sync mode."""

    def setUp(self):
        self.tmp_drive = tempfile.mkdtemp()
        self.tmp_vault = tempfile.mkdtemp()
        self.config = {
            "enabled": True,
            "mode": "desktop_app",
            "desktop_app_path": self.tmp_drive,
            "mirror_structure": True,
        }

    def tearDown(self):
        shutil.rmtree(self.tmp_drive, ignore_errors=True)
        shutil.rmtree(self.tmp_vault, ignore_errors=True)

    def _create_vault_file(self, name, content="# Test"):
        path = Path(self.tmp_vault) / name
        path.write_text(content)
        return path

    def test_is_configured_desktop_mode(self):
        """Desktop mode is configured when folder parent exists."""
        gdrive = GDriveSync(self.config)
        self.assertTrue(gdrive.is_configured())

    def test_sync_creates_subfolder(self):
        """Syncing creates the source subfolder in the Drive folder."""
        gdrive = GDriveSync(self.config)
        file_path = self._create_vault_file("roadmap.md", "# Roadmap\nQ2 plan")
        dest = gdrive.sync_file(file_path, "Plans")

        expected = Path(self.tmp_drive) / "Plans" / "roadmap.md"
        self.assertTrue(expected.exists())
        self.assertEqual(expected.read_text(), "# Roadmap\nQ2 plan")

    def test_sync_overwrites_existing(self):
        """Re-syncing overwrites the destination file."""
        gdrive = GDriveSync(self.config)
        file_path = self._create_vault_file("note.md", "Version 1")
        gdrive.sync_file(file_path, "Projects")

        file_path.write_text("Version 2")
        gdrive.sync_file(file_path, "Projects")

        dest = Path(self.tmp_drive) / "Projects" / "note.md"
        self.assertEqual(dest.read_text(), "Version 2")

    def test_no_mirror_structure(self):
        """Without mirror_structure, files go to root of drive folder."""
        self.config["mirror_structure"] = False
        gdrive = GDriveSync(self.config)
        file_path = self._create_vault_file("flat.md")
        gdrive.sync_file(file_path, "Decisions")

        expected = Path(self.tmp_drive) / "flat.md"
        self.assertTrue(expected.exists())

    def test_delete_file(self):
        """delete_file removes a file from the Drive folder."""
        gdrive = GDriveSync(self.config)
        file_path = self._create_vault_file("to-delete.md")
        gdrive.sync_file(file_path, "Plans")

        result = gdrive.delete_file("Plans", "to-delete.md")
        self.assertTrue(result)
        self.assertFalse((Path(self.tmp_drive) / "Plans" / "to-delete.md").exists())

    def test_delete_nonexistent_file(self):
        """delete_file returns False for a file that doesn't exist."""
        gdrive = GDriveSync(self.config)
        result = gdrive.delete_file("Plans", "ghost.md")
        self.assertFalse(result)


class TestGDriveSyncRcloneMode(unittest.TestCase):
    """Tests for rclone sync mode."""

    def setUp(self):
        self.config = {
            "enabled": True,
            "mode": "rclone",
            "rclone_remote": "gdrive",
            "rclone_dest": "NotebookLM-Vault",
            "mirror_structure": True,
        }

    @patch("gdrive_sync.subprocess.run")
    def test_rclone_available_check(self, mock_run):
        """Checks rclone availability by listing remotes."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="gdrive:\nbackup:\n"
        )
        gdrive = GDriveSync(self.config)
        self.assertTrue(gdrive.is_configured())

    @patch("gdrive_sync.subprocess.run")
    def test_rclone_not_available(self, mock_run):
        """Reports unconfigured when rclone remote is missing."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="backup:\n"
        )
        gdrive = GDriveSync(self.config)
        self.assertFalse(gdrive.is_configured())

    @patch("gdrive_sync.subprocess.run")
    def test_rclone_not_installed(self, mock_run):
        """Reports unconfigured when rclone binary is missing."""
        mock_run.side_effect = FileNotFoundError
        gdrive = GDriveSync(self.config)
        self.assertFalse(gdrive.is_configured())

    @patch("gdrive_sync.subprocess.run")
    def test_sync_file_rclone_success(self, mock_run):
        """Successful rclone sync returns remote path."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        gdrive = GDriveSync(self.config)
        # Bypass is_configured check
        gdrive.is_configured = lambda: True

        tmp = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
        tmp.write(b"# Test")
        tmp.close()

        result = gdrive.sync_file(Path(tmp.name), "Projects")
        self.assertIn("gdrive:", result)
        self.assertIn("Projects", result)

        os.unlink(tmp.name)

    @patch("gdrive_sync.subprocess.run")
    def test_sync_file_rclone_failure(self, mock_run):
        """Failed rclone sync raises GDriveSyncError."""
        # First call (mkdir) succeeds, second call (copyto) fails
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=1, stderr="permission denied"),
        ]

        gdrive = GDriveSync(self.config)
        gdrive.is_configured = lambda: True

        tmp = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
        tmp.write(b"# Test")
        tmp.close()

        with self.assertRaises(GDriveSyncError):
            gdrive.sync_file(Path(tmp.name), "Projects")

        os.unlink(tmp.name)


class TestGDriveSyncDisabled(unittest.TestCase):
    """Tests for disabled sync."""

    def test_disabled_raises_on_sync(self):
        """Syncing when not configured raises clear error."""
        config = {"enabled": True, "mode": "desktop_app",
                  "desktop_app_path": "/nonexistent/path"}
        gdrive = GDriveSync(config)
        self.assertFalse(gdrive.is_configured())

        tmp = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
        tmp.write(b"# Test")
        tmp.close()

        with self.assertRaises(GDriveSyncError):
            gdrive.sync_file(Path(tmp.name), "Projects")

        os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
