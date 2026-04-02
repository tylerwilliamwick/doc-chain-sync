"""Tests for the main dispatcher module."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from dispatcher import load_config, scan_vault, cleanup_deleted, run_sync, detect_content_type
from state import SyncState


class TestLoadConfig(unittest.TestCase):
    """Tests for config loading."""

    def test_load_valid_config(self):
        """Valid YAML config loads successfully."""
        config_path = Path(__file__).parent.parent / "config.yaml"
        if config_path.exists():
            config = load_config(str(config_path))
            self.assertIn("vault", config)
            self.assertIn("notion", config)
            self.assertIn("google_drive", config)
            self.assertIn("sync", config)

    def test_load_missing_config(self):
        """Missing config file raises FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            load_config("/nonexistent/config.yaml")


class TestScanVault(unittest.TestCase):
    """Tests for vault scanning logic."""

    def setUp(self):
        self.vault = tempfile.mkdtemp()
        # Create folder structure
        for folder in ["Projects", "Decisions", "Plans", "Daily"]:
            os.makedirs(os.path.join(self.vault, folder))

        # Create test files
        self._write(f"{self.vault}/Projects/roadmap.md", "# Roadmap")
        self._write(f"{self.vault}/Projects/sprint-plan.md", "# Sprint")
        self._write(f"{self.vault}/Decisions/api-choice.md", "# API Choice")
        self._write(f"{self.vault}/Plans/q2-plan.md", "# Q2")
        self._write(f"{self.vault}/Daily/2026-03-29.md", "# Daily")  # Should be excluded
        self._write(f"{self.vault}/Projects/_index.md", "# Index")  # Excluded by pattern
        self._write(f"{self.vault}/Projects/.DS_Store", "")  # Excluded by pattern

    def tearDown(self):
        shutil.rmtree(self.vault)

    def _write(self, path, content):
        Path(path).write_text(content)

    def test_scans_watched_folders_only(self):
        """Only files in watched folders are returned."""
        files = scan_vault(
            Path(self.vault),
            ["Projects", "Decisions", "Plans"],
            ["_index.md", ".DS_Store"]
        )
        names = [f[0].name for f in files]
        self.assertIn("roadmap.md", names)
        self.assertIn("sprint-plan.md", names)
        self.assertIn("api-choice.md", names)
        self.assertIn("q2-plan.md", names)
        self.assertNotIn("2026-03-29.md", names)  # Daily not watched

    def test_excludes_index_files(self):
        """Excluded patterns are filtered out."""
        files = scan_vault(
            Path(self.vault),
            ["Projects"],
            ["_index.md", ".DS_Store"]
        )
        names = [f[0].name for f in files]
        self.assertNotIn("_index.md", names)
        self.assertNotIn(".DS_Store", names)

    def test_returns_source_folder(self):
        """Each file is tagged with its source folder name."""
        files = scan_vault(Path(self.vault), ["Decisions"], [])
        self.assertEqual(files[0][1], "Decisions")

    def test_handles_missing_folder(self):
        """Missing folders are skipped without error."""
        files = scan_vault(Path(self.vault), ["NonExistent"], [])
        self.assertEqual(len(files), 0)

    def test_handles_nested_files(self):
        """Files in subfolders are included."""
        os.makedirs(f"{self.vault}/Projects/sub")
        self._write(f"{self.vault}/Projects/sub/nested.md", "# Nested")
        files = scan_vault(Path(self.vault), ["Projects"], ["_index.md", ".DS_Store"])
        names = [f[0].name for f in files]
        self.assertIn("nested.md", names)


class TestCleanupDeleted(unittest.TestCase):
    """Tests for state cleanup of deleted files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.vault = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmp, "state.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)
        shutil.rmtree(self.vault)

    def test_removes_deleted_file_from_state(self):
        """Files no longer on disk are removed from state."""
        state = SyncState(self.state_file)
        state.record_sync("Claude Code/Projects/gone.md", 1000.0)

        logger = MagicMock()
        cleanup_deleted(state, Path(self.vault), logger)

        self.assertEqual(state.get_file_state("Claude Code/Projects/gone.md"), {})


class TestDetectContentType(unittest.TestCase):
    """Tests for content type detection logic."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = {
            "notion": {
                "type_detection": {
                    "folder_map": {
                        "Projects": "Project",
                        "Decisions": "Decision",
                        "Plans": "Plan",
                        "Daily": "Daily Note",
                        "Meetings": "Meeting Notes",
                        "Retros": "Retro",
                    },
                    "handoff_heuristics": {
                        "filename_contains": ["handoff"],
                        "content_headers": ["## Task State", "## Decisions Made", "## TODOS"],
                    },
                }
            }
        }

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _make_file(self, name, content="# Note"):
        p = Path(self.tmp) / name
        p.write_text(content)
        return p

    def test_handoff_filename_returns_session_handoff(self):
        """File named handoff-2026-04-01.md in Daily/ returns Session Handoff."""
        f = self._make_file("handoff-2026-04-01.md")
        result = detect_content_type(f, "Daily", self.config)
        self.assertEqual(result, "Session Handoff")

    def test_handoff_uppercase_in_filename(self):
        """Filename check is case-insensitive."""
        f = self._make_file("Session-Handoff-2026-04-01.md")
        result = detect_content_type(f, "Daily", self.config)
        self.assertEqual(result, "Session Handoff")

    def test_daily_non_handoff_returns_daily_note(self):
        """Daily note without handoff markers returns Daily Note."""
        f = self._make_file("2026-04-01.md", "# Daily Note\nToday I did things.")
        result = detect_content_type(f, "Daily", self.config)
        self.assertEqual(result, "Daily Note")

    def test_content_marker_task_state(self):
        """File in Daily/ with ## Task State header returns Session Handoff."""
        f = self._make_file("2026-04-01.md",
                            "# Notes\n## Task State\nWorking on X.")
        result = detect_content_type(f, "Daily", self.config)
        self.assertEqual(result, "Session Handoff")

    def test_content_marker_todos(self):
        """File in Daily/ with ## TODOS header returns Session Handoff."""
        f = self._make_file("2026-04-01.md", "## TODOS\n- Fix bug")
        result = detect_content_type(f, "Daily", self.config)
        self.assertEqual(result, "Session Handoff")

    def test_meetings_folder_returns_meeting_notes(self):
        """Any file in Meetings/ folder returns Meeting Notes."""
        f = self._make_file("standup.md")
        result = detect_content_type(f, "Meetings", self.config)
        self.assertEqual(result, "Meeting Notes")

    def test_plans_folder_returns_plan(self):
        """Any file in Plans/ folder returns Plan."""
        f = self._make_file("q2-plan.md")
        result = detect_content_type(f, "Plans", self.config)
        self.assertEqual(result, "Plan")

    def test_projects_folder_returns_project(self):
        """Any file in Projects/ folder returns Project."""
        f = self._make_file("my-project.md")
        result = detect_content_type(f, "Projects", self.config)
        self.assertEqual(result, "Project")

    def test_decisions_folder_returns_decision(self):
        """Any file in Decisions/ folder returns Decision."""
        f = self._make_file("arch-decision.md")
        result = detect_content_type(f, "Decisions", self.config)
        self.assertEqual(result, "Decision")

    def test_retros_folder_returns_retro(self):
        """Any file in Retros/ folder returns Retro."""
        f = self._make_file("sprint-retro.md")
        result = detect_content_type(f, "Retros", self.config)
        self.assertEqual(result, "Retro")


class TestRunSync(unittest.TestCase):
    """Integration tests for the full sync pipeline."""

    def setUp(self):
        self.vault = tempfile.mkdtemp()
        self.tmp = tempfile.mkdtemp()

        for folder in ["Projects", "Decisions", "Plans"]:
            os.makedirs(os.path.join(self.vault, folder))

        Path(f"{self.vault}/Projects/test.md").write_text("# Test\nContent")
        Path(f"{self.vault}/Decisions/choice.md").write_text("# Choice\nWe chose X")

        self.config = {
            "vault": {
                "base_path": self.vault,
                "watch_folders": ["Projects", "Decisions", "Plans"],
                "exclude_patterns": ["_index.md"],
            },
            "notion": {"enabled": False},
            "google_drive": {"enabled": False},
            "sync": {
                "state_file": os.path.join(self.tmp, "state.json"),
                "log_file": os.path.join(self.tmp, "test.log"),
                "max_log_size": 1048576,
            },
        }

    def tearDown(self):
        shutil.rmtree(self.vault)
        shutil.rmtree(self.tmp)

    def test_dry_run_no_side_effects(self):
        """Dry run logs actions without syncing."""
        self.config["notion"]["enabled"] = True
        stats = run_sync(self.config, dry_run=True)
        # With no targets configured, it should warn and return 0
        self.assertEqual(stats["errors"], 0)

    def test_no_targets_returns_zero(self):
        """With both targets disabled, nothing syncs."""
        stats = run_sync(self.config)
        self.assertEqual(stats["synced"], 0)
        self.assertEqual(stats["errors"], 0)

    def test_missing_vault_returns_error(self):
        """Missing vault directory is logged as error."""
        self.config["vault"]["base_path"] = "/nonexistent/vault"
        stats = run_sync(self.config)
        self.assertEqual(stats["errors"], 1)


if __name__ == "__main__":
    unittest.main()
