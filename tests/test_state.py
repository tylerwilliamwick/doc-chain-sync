"""Tests for SyncState tracking."""

import json
import os
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from state import SyncState


class TestSyncState(unittest.TestCase):
    """Unit tests for the sync state tracker."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmp, "test-state.json")

    def tearDown(self):
        if os.path.exists(self.state_file):
            os.unlink(self.state_file)
        os.rmdir(self.tmp)

    def test_empty_state_on_first_run(self):
        """State file is created empty when it doesn't exist."""
        state = SyncState(self.state_file)
        self.assertEqual(state.get_all_synced_files(), {})
        self.assertIsNone(state.last_run)

    def test_needs_sync_new_file(self):
        """A file not in state always needs syncing."""
        state = SyncState(self.state_file)
        self.assertTrue(state.needs_sync("Projects/test.md", 1000.0))

    def test_needs_sync_unchanged_file(self):
        """A file with same mtime does not need syncing."""
        state = SyncState(self.state_file)
        state.record_sync("Projects/test.md", 1000.0, notion_page_id="abc-123")
        state.save()

        # Reload
        state2 = SyncState(self.state_file)
        self.assertFalse(state2.needs_sync("Projects/test.md", 1000.0))

    def test_needs_sync_modified_file(self):
        """A file with newer mtime needs syncing."""
        state = SyncState(self.state_file)
        state.record_sync("Projects/test.md", 1000.0)
        state.save()

        state2 = SyncState(self.state_file)
        self.assertTrue(state2.needs_sync("Projects/test.md", 2000.0))

    def test_record_and_retrieve_notion_page_id(self):
        """Notion page IDs are persisted across loads."""
        state = SyncState(self.state_file)
        state.record_sync("Decisions/choice.md", 1500.0, notion_page_id="page-456")
        state.save()

        state2 = SyncState(self.state_file)
        self.assertEqual(state2.get_notion_page_id("Decisions/choice.md"), "page-456")

    def test_record_gdrive_path(self):
        """Google Drive paths are stored in state."""
        state = SyncState(self.state_file)
        state.record_sync("Plans/roadmap.md", 2000.0, gdrive_path="gdrive:NotebookLM-Vault/Plans/roadmap.md")
        state.save()

        state2 = SyncState(self.state_file)
        file_state = state2.get_file_state("Plans/roadmap.md")
        self.assertEqual(file_state["gdrive_path"], "gdrive:NotebookLM-Vault/Plans/roadmap.md")

    def test_record_failure_capped_at_five(self):
        """Error log is capped at 5 entries per file."""
        state = SyncState(self.state_file)
        for i in range(8):
            state.record_failure("Projects/broken.md", "notion", f"Error {i}")

        errors = state.get_file_state("Projects/broken.md")["errors"]
        self.assertEqual(len(errors), 5)
        self.assertEqual(errors[0]["error"], "Error 3")  # Oldest kept

    def test_remove_file(self):
        """Removing a file clears its state."""
        state = SyncState(self.state_file)
        state.record_sync("Plans/old.md", 500.0)
        state.remove_file("Plans/old.md")
        self.assertEqual(state.get_file_state("Plans/old.md"), {})

    def test_corrupt_state_file(self):
        """Corrupt JSON state file is handled gracefully."""
        with open(self.state_file, "w") as f:
            f.write("not valid json{{{")
        state = SyncState(self.state_file)
        self.assertEqual(state.get_all_synced_files(), {})

    def test_save_updates_last_run(self):
        """Saving state records the last run timestamp."""
        state = SyncState(self.state_file)
        state.save()

        state2 = SyncState(self.state_file)
        self.assertIsNotNone(state2.last_run)


if __name__ == "__main__":
    unittest.main()
