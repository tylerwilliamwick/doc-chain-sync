"""
Smoke tests for Doc Chain Sync.

These tests verify the system works end-to-end with real files
but mocked external services (Notion API, rclone).
They cover the critical path: vault file changes trigger sync
to both Notion and Google Drive.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from dispatcher import run_sync
from state import SyncState


class TestSmokeEndToEnd(unittest.TestCase):
    """End-to-end smoke tests with mocked services."""

    def setUp(self):
        """Set up a realistic vault structure with test files."""
        self.vault = tempfile.mkdtemp()
        self.tmp = tempfile.mkdtemp()
        self.drive_folder = tempfile.mkdtemp()

        # Create vault folders
        for folder in ["Projects", "Decisions", "Plans"]:
            os.makedirs(os.path.join(self.vault, folder))

        # Create realistic vault files with frontmatter
        self._write("Projects/gis-integration.md", """---
created: 2026-03-29
type: project
tags: [#product/cpcore]
status: active
---

# GIS Integration

## Current State
ArcGIS integration is live for parcel lookup.

## Next Steps
Expand to permit boundary validation.
""")

        self._write("Decisions/api-versioning-strategy.md", """---
created: 2026-03-28
type: decision
tags: [#product/platform]
---

# API Versioning Strategy

## Decision
We will use URL path versioning (v1, v2) for the public API.

## Rationale
Path versioning is the most visible and least error-prone approach for our customers.

## Alternatives Considered
Header versioning, query param versioning.
""")

        self._write("Plans/q2-roadmap.md", """---
created: 2026-03-27
type: plan
tags: [#roadmap]
---

# Q2 2026 Roadmap

## Themes
1. Platform stability
2. GIS expansion
3. Portal self-service

## Key Milestones
- April: ArcGIS boundary validation
- May: Portal redesign launch
- June: Reporting dashboard v2
""")

        self.config = {
            "vault": {
                "base_path": self.vault,
                "watch_folders": ["Projects", "Decisions", "Plans"],
                "exclude_patterns": ["_index.md", "_about.md", ".DS_Store"],
            },
            "notion": {
                "enabled": True,
                "api_token_env": "SMOKE_NOTION_TOKEN",
                "database_id_env": "SMOKE_NOTION_DB",
                "properties": {
                    "title": "Name",
                    "source_folder": "Source",
                    "synced_at": "Synced At",
                    "file_name": "File Name",
                    "vault_path": "Vault Path",
                },
            },
            "google_drive": {
                "enabled": True,
                "mode": "desktop_app",
                "desktop_app_path": self.drive_folder,
                "mirror_structure": True,
            },
            "sync": {
                "state_file": os.path.join(self.tmp, "smoke-state.json"),
                "log_file": os.path.join(self.tmp, "smoke.log"),
                "max_log_size": 1048576,
            },
        }

    def tearDown(self):
        shutil.rmtree(self.vault)
        shutil.rmtree(self.tmp)
        shutil.rmtree(self.drive_folder)

    def _write(self, rel_path, content):
        path = Path(self.vault) / rel_path
        path.write_text(content)

    @patch.dict(os.environ, {"SMOKE_NOTION_TOKEN": "test-tok", "SMOKE_NOTION_DB": "test-db"})
    @patch("notion_sync.NotionSync._request")
    def test_smoke_full_sync(self, mock_notion_request):
        """Full sync: 3 files to Notion + Drive, state persisted."""
        mock_notion_request.return_value = {"id": "page-123"}

        stats = run_sync(self.config, verbose=True)

        # All 3 files should sync
        self.assertEqual(stats["synced"], 3)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["skipped"], 0)

        # Verify Drive files exist
        drive = Path(self.drive_folder)
        self.assertTrue((drive / "Projects" / "gis-integration.md").exists())
        self.assertTrue((drive / "Decisions" / "api-versioning-strategy.md").exists())
        self.assertTrue((drive / "Plans" / "q2-roadmap.md").exists())

        # Verify state file was created
        state = SyncState(self.config["sync"]["state_file"])
        all_files = state.get_all_synced_files()
        self.assertEqual(len(all_files), 3)

        # Verify Notion was called for each file
        self.assertEqual(mock_notion_request.call_count, 3)  # 3 creates

    @patch.dict(os.environ, {"SMOKE_NOTION_TOKEN": "test-tok", "SMOKE_NOTION_DB": "test-db"})
    @patch("notion_sync.NotionSync._request")
    def test_smoke_incremental_sync(self, mock_notion_request):
        """Second run only syncs modified files."""
        mock_notion_request.return_value = {"id": "page-123"}

        # First run: sync everything
        run_sync(self.config, verbose=True)

        # Modify one file
        import time
        time.sleep(0.1)  # Ensure mtime changes
        self._write("Projects/gis-integration.md", "# GIS Integration\nUpdated content.")

        # Reset mock
        mock_notion_request.reset_mock()
        mock_notion_request.side_effect = [
            {"id": "page-123"},  # PATCH properties
            {"results": []},    # GET existing blocks
            {},                 # PATCH new blocks
        ]

        # Second run
        stats = run_sync(self.config, verbose=True)

        self.assertEqual(stats["synced"], 1)
        self.assertEqual(stats["skipped"], 2)

    @patch.dict(os.environ, {"SMOKE_NOTION_TOKEN": "test-tok", "SMOKE_NOTION_DB": "test-db"})
    @patch("notion_sync.NotionSync._request")
    def test_smoke_notion_failure_continues_drive(self, mock_notion_request):
        """Notion failure doesn't block Drive sync."""
        mock_notion_request.side_effect = Exception("Connection refused")

        stats = run_sync(self.config, verbose=True)

        # Errors from Notion, but Drive should still work
        self.assertGreater(stats["errors"], 0)

        # Drive files should still exist
        drive = Path(self.drive_folder)
        self.assertTrue((drive / "Projects" / "gis-integration.md").exists())

    @patch.dict(os.environ, {"SMOKE_NOTION_TOKEN": "test-tok", "SMOKE_NOTION_DB": "test-db"})
    @patch("notion_sync.NotionSync._request")
    def test_smoke_force_resyncs_everything(self, mock_notion_request):
        """Force flag re-syncs all files regardless of mtime."""
        mock_notion_request.return_value = {"id": "page-123"}

        # First run
        run_sync(self.config)

        # Second run with force
        mock_notion_request.reset_mock()
        mock_notion_request.return_value = {"id": "page-123"}

        stats = run_sync(self.config, force=True)
        self.assertEqual(stats["synced"], 3)
        self.assertEqual(stats["skipped"], 0)

    def test_smoke_drive_content_matches_source(self):
        """Drive copies are byte-identical to vault originals."""
        self.config["notion"]["enabled"] = False  # Only test Drive

        run_sync(self.config)

        vault_file = Path(self.vault) / "Plans" / "q2-roadmap.md"
        drive_file = Path(self.drive_folder) / "Plans" / "q2-roadmap.md"

        self.assertEqual(vault_file.read_text(), drive_file.read_text())

    @patch.dict(os.environ, {"SMOKE_NOTION_TOKEN": "test-tok", "SMOKE_NOTION_DB": "test-db"})
    @patch("notion_sync.NotionSync._request")
    def test_smoke_deleted_file_cleanup(self, mock_notion_request):
        """Deleted vault files are removed from state."""
        mock_notion_request.return_value = {"id": "page-123"}

        # Sync everything
        run_sync(self.config)

        # Delete a file
        os.unlink(os.path.join(self.vault, "Decisions", "api-versioning-strategy.md"))

        # Run again
        run_sync(self.config)

        # State should no longer track the deleted file
        state = SyncState(self.config["sync"]["state_file"])
        deleted_state = state.get_file_state("Claude Code/Decisions/api-versioning-strategy.md")
        self.assertEqual(deleted_state, {})


if __name__ == "__main__":
    unittest.main()
