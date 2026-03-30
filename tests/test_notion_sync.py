"""Tests for Notion sync module."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from notion_sync import NotionSync, NotionSyncError


class TestNotionSync(unittest.TestCase):
    """Unit tests for Notion API integration."""

    def setUp(self):
        self.config = {
            "enabled": True,
            "api_token_env": "TEST_NOTION_TOKEN",
            "database_id_env": "TEST_NOTION_DB",
            "properties": {
                "title": "Name",
                "source_folder": "Source",
                "synced_at": "Synced At",
                "file_name": "File Name",
                "vault_path": "Vault Path",
            }
        }

    def test_not_configured_without_env_vars(self):
        """NotionSync reports unconfigured when env vars are missing."""
        # Clear env vars if they happen to exist
        os.environ.pop("TEST_NOTION_TOKEN", None)
        os.environ.pop("TEST_NOTION_DB", None)
        notion = NotionSync(self.config)
        self.assertFalse(notion.is_configured())

    @patch.dict(os.environ, {"TEST_NOTION_TOKEN": "secret-token", "TEST_NOTION_DB": "db-123"})
    def test_configured_with_env_vars(self):
        """NotionSync reports configured when both env vars are set."""
        notion = NotionSync(self.config)
        self.assertTrue(notion.is_configured())

    def test_strip_frontmatter(self):
        """YAML frontmatter is removed from markdown content."""
        notion = NotionSync(self.config)
        content = "---\ntitle: Test\ntype: project\n---\n\n# Heading\nSome content."
        result = notion._strip_frontmatter(content)
        self.assertTrue(result.startswith("# Heading"))
        self.assertNotIn("title: Test", result)

    def test_strip_frontmatter_no_frontmatter(self):
        """Content without frontmatter passes through unchanged."""
        notion = NotionSync(self.config)
        content = "# Just a heading\nNo frontmatter here."
        result = notion._strip_frontmatter(content)
        self.assertEqual(result, content)

    def test_markdown_to_blocks_headings(self):
        """Markdown headings convert to Notion heading blocks."""
        notion = NotionSync(self.config)
        content = "# H1\n## H2\n### H3"
        blocks = notion._markdown_to_blocks(content)
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0]["type"], "heading_1")
        self.assertEqual(blocks[1]["type"], "heading_2")
        self.assertEqual(blocks[2]["type"], "heading_3")

    def test_markdown_to_blocks_lists(self):
        """Bullet and numbered lists convert to correct block types."""
        notion = NotionSync(self.config)
        content = "- Bullet one\n- Bullet two\n1. Number one\n2. Number two"
        blocks = notion._markdown_to_blocks(content)
        self.assertEqual(blocks[0]["type"], "bulleted_list_item")
        self.assertEqual(blocks[1]["type"], "bulleted_list_item")
        self.assertEqual(blocks[2]["type"], "numbered_list_item")
        self.assertEqual(blocks[3]["type"], "numbered_list_item")

    def test_markdown_to_blocks_paragraph(self):
        """Plain text becomes paragraph blocks."""
        notion = NotionSync(self.config)
        content = "This is a paragraph."
        blocks = notion._markdown_to_blocks(content)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "paragraph")

    def test_markdown_to_blocks_max_100(self):
        """Block output is capped at 100 (Notion API limit)."""
        notion = NotionSync(self.config)
        lines = "\n".join([f"Line {i}" for i in range(200)])
        blocks = notion._markdown_to_blocks(lines)
        self.assertEqual(len(blocks), 100)

    def test_rich_text_chunking(self):
        """Long text is chunked at 2000 char boundaries."""
        notion = NotionSync(self.config)
        long_text = "x" * 5000
        chunks = notion._rich_text(long_text)
        self.assertEqual(len(chunks), 3)  # 2000 + 2000 + 1000
        self.assertEqual(len(chunks[0]["text"]["content"]), 2000)
        self.assertEqual(len(chunks[2]["text"]["content"]), 1000)

    @patch.dict(os.environ, {"TEST_NOTION_TOKEN": "tok", "TEST_NOTION_DB": "db"})
    def test_create_page_raises_without_config(self):
        """Sync raises error when credentials missing."""
        os.environ.pop("TEST_NOTION_TOKEN", None)
        notion = NotionSync(self.config)
        with self.assertRaises(NotionSyncError):
            notion.create_page("Title", "Projects", "content", "file.md", "path")

    @patch.dict(os.environ, {"TEST_NOTION_TOKEN": "tok", "TEST_NOTION_DB": "db"})
    def test_sync_file_creates_page(self):
        """sync_file calls create_page for new files."""
        notion = NotionSync(self.config)

        # Create a temp markdown file
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        tmp.write("---\ntype: project\n---\n# Test\nContent here.")
        tmp.close()

        with patch.object(notion, "_request") as mock_req:
            mock_req.return_value = {"id": "new-page-id"}
            page_id = notion.sync_file(Path(tmp.name), "Projects")
            self.assertEqual(page_id, "new-page-id")
            mock_req.assert_called_once()

        os.unlink(tmp.name)

    @patch.dict(os.environ, {"TEST_NOTION_TOKEN": "tok", "TEST_NOTION_DB": "db"})
    def test_sync_file_updates_existing(self):
        """sync_file calls update_page when existing_page_id is provided."""
        notion = NotionSync(self.config)

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        tmp.write("# Updated\nNew content.")
        tmp.close()

        with patch.object(notion, "_request") as mock_req:
            mock_req.return_value = {"results": []}
            page_id = notion.sync_file(Path(tmp.name), "Projects",
                                       existing_page_id="existing-id")
            self.assertEqual(page_id, "existing-id")
            # Should call PATCH (properties), GET (existing blocks), PATCH (new blocks)
            self.assertGreaterEqual(mock_req.call_count, 2)

        os.unlink(tmp.name)

    def test_asterisk_bullets_handled(self):
        """Markdown with * bullets converts correctly."""
        notion = NotionSync(self.config)
        content = "* First item\n* Second item"
        blocks = notion._markdown_to_blocks(content)
        self.assertEqual(blocks[0]["type"], "bulleted_list_item")


if __name__ == "__main__":
    unittest.main()
