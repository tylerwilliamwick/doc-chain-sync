"""Tests for Notion sync module."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

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
                "type_field": "Type",
                "synced_at": "Synced At",
                "file_name": "File Name",
                "vault_path": "Vault Path",
            },
            "page_icons": {
                "Daily Note": "📅",
                "Meeting Notes": "💬",
                "Retro": "🔄",
                "Project": "🏗️",
                "Decision": "⚡",
                "Plan": "🗺️",
                "Session Handoff": "🤖",
            },
            "page_covers": {
                "Daily Note": "https://www.notion.so/images/page-cover/gradients_2.png",
                "Session Handoff": "https://www.notion.so/images/page-cover/gradients_5.png",
            },
        }

    def _notion(self, extra_config=None):
        cfg = dict(self.config)
        if extra_config:
            cfg.update(extra_config)
        return NotionSync(cfg)

    # ------------------------------------------------------------------ #
    # Basic configuration
    # ------------------------------------------------------------------ #

    def test_not_configured_without_env_vars(self):
        """NotionSync reports unconfigured when env vars are missing."""
        os.environ.pop("TEST_NOTION_TOKEN", None)
        os.environ.pop("TEST_NOTION_DB", None)
        notion = self._notion()
        self.assertFalse(notion.is_configured())

    @patch.dict(os.environ, {"TEST_NOTION_TOKEN": "secret-token", "TEST_NOTION_DB": "db-123"})
    def test_configured_with_env_vars(self):
        """NotionSync reports configured when both env vars are set."""
        notion = self._notion()
        self.assertTrue(notion.is_configured())

    def test_icons_and_covers_stored(self):
        """NotionSync stores icons and covers from config."""
        notion = self._notion()
        self.assertEqual(notion.icons["Daily Note"], "📅")
        self.assertEqual(notion.covers["Daily Note"], "https://www.notion.so/images/page-cover/gradients_2.png")

    # ------------------------------------------------------------------ #
    # Frontmatter stripping
    # ------------------------------------------------------------------ #

    def test_strip_frontmatter(self):
        """YAML frontmatter is removed from markdown content."""
        notion = self._notion()
        content = "---\ntitle: Test\ntype: project\n---\n\n# Heading\nSome content."
        result = notion._strip_frontmatter(content)
        self.assertTrue(result.startswith("# Heading"))
        self.assertNotIn("title: Test", result)

    def test_strip_frontmatter_no_frontmatter(self):
        """Content without frontmatter passes through unchanged."""
        notion = self._notion()
        content = "# Just a heading\nNo frontmatter here."
        result = notion._strip_frontmatter(content)
        self.assertEqual(result, content)

    # ------------------------------------------------------------------ #
    # _rich_text: chunking and inline annotations
    # ------------------------------------------------------------------ #

    def test_rich_text_chunking(self):
        """Long text is chunked at 2000 char boundaries."""
        notion = self._notion()
        long_text = "x" * 5000
        chunks = notion._rich_text(long_text)
        self.assertEqual(len(chunks), 3)  # 2000 + 2000 + 1000
        self.assertEqual(len(chunks[0]["text"]["content"]), 2000)
        self.assertEqual(len(chunks[2]["text"]["content"]), 1000)

    def test_rich_text_bold(self):
        """**bold** text produces a rich_text object with bold annotation."""
        notion = self._notion()
        rt = notion._rich_text("Hello **world** there")
        bold_parts = [p for p in rt if p.get("annotations", {}).get("bold")]
        self.assertEqual(len(bold_parts), 1)
        self.assertEqual(bold_parts[0]["text"]["content"], "world")

    def test_rich_text_italic(self):
        """*italic* text produces a rich_text object with italic annotation."""
        notion = self._notion()
        rt = notion._rich_text("Hello *world* there")
        italic_parts = [p for p in rt if p.get("annotations", {}).get("italic")]
        self.assertEqual(len(italic_parts), 1)
        self.assertEqual(italic_parts[0]["text"]["content"], "world")

    def test_rich_text_mixed_annotations(self):
        """Mixed bold and italic in one line produce separate annotated objects."""
        notion = self._notion()
        rt = notion._rich_text("**bold** and *italic*")
        types = [(p["text"]["content"], p.get("annotations")) for p in rt]
        bold_texts = [t for t, a in types if a and a.get("bold")]
        italic_texts = [t for t, a in types if a and a.get("italic")]
        self.assertIn("bold", bold_texts)
        self.assertIn("italic", italic_texts)

    def test_rich_text_plain_no_annotations(self):
        """Plain text with no markers has no annotations key."""
        notion = self._notion()
        rt = notion._rich_text("plain text")
        self.assertEqual(len(rt), 1)
        self.assertNotIn("annotations", rt[0])

    # ------------------------------------------------------------------ #
    # _markdown_to_blocks: standard block types
    # ------------------------------------------------------------------ #

    def test_blocks_headings(self):
        """Markdown headings convert to Notion heading blocks."""
        notion = self._notion()
        content = "# H1\n## H2\n### H3"
        blocks = notion._markdown_to_blocks(content)
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0]["type"], "heading_1")
        self.assertEqual(blocks[1]["type"], "heading_2")
        self.assertEqual(blocks[2]["type"], "heading_3")

    def test_blocks_bullet_list(self):
        """Bullet and numbered lists convert to correct block types."""
        notion = self._notion()
        content = "- Bullet one\n- Bullet two\n1. Number one\n2. Number two"
        blocks = notion._markdown_to_blocks(content)
        self.assertEqual(blocks[0]["type"], "bulleted_list_item")
        self.assertEqual(blocks[1]["type"], "bulleted_list_item")
        self.assertEqual(blocks[2]["type"], "numbered_list_item")
        self.assertEqual(blocks[3]["type"], "numbered_list_item")

    def test_blocks_asterisk_bullets(self):
        """Markdown with * bullets converts correctly."""
        notion = self._notion()
        content = "* First item\n* Second item"
        blocks = notion._markdown_to_blocks(content)
        self.assertEqual(blocks[0]["type"], "bulleted_list_item")

    def test_blocks_paragraph(self):
        """Plain text becomes paragraph blocks."""
        notion = self._notion()
        content = "This is a paragraph."
        blocks = notion._markdown_to_blocks(content)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "paragraph")

    def test_blocks_empty_lines_skipped(self):
        """Empty lines do not produce blocks."""
        notion = self._notion()
        content = "Para one\n\n\nPara two"
        blocks = notion._markdown_to_blocks(content)
        self.assertEqual(len(blocks), 2)

    # ------------------------------------------------------------------ #
    # New block types: code, callout, divider, table
    # ------------------------------------------------------------------ #

    def test_blocks_code_fence(self):
        """Triple-backtick fenced code produces a Notion code block."""
        notion = self._notion()
        content = "Before\n```python\nx = 1\ny = 2\n```\nAfter"
        blocks = notion._markdown_to_blocks(content)
        code_blocks = [b for b in blocks if b["type"] == "code"]
        self.assertEqual(len(code_blocks), 1)
        self.assertEqual(code_blocks[0]["code"]["language"], "python")
        self.assertIn("x = 1", code_blocks[0]["code"]["rich_text"][0]["text"]["content"])

    def test_blocks_code_fence_no_language(self):
        """Code fence without language hint defaults to 'plain text'."""
        notion = self._notion()
        content = "```\nsome code\n```"
        blocks = notion._markdown_to_blocks(content)
        self.assertEqual(blocks[0]["type"], "code")
        self.assertEqual(blocks[0]["code"]["language"], "plain text")

    def test_blocks_blockquote_becomes_callout(self):
        """Blockquote lines (> prefix) produce Notion callout blocks."""
        notion = self._notion()
        content = "> This is important"
        blocks = notion._markdown_to_blocks(content)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "callout")
        self.assertEqual(blocks[0]["callout"]["icon"]["emoji"], "💡")
        self.assertEqual(blocks[0]["callout"]["rich_text"][0]["text"]["content"], "This is important")

    def test_blocks_horizontal_rule_becomes_divider(self):
        """--- on its own line produces a Notion divider block."""
        notion = self._notion()
        content = "Before\n---\nAfter"
        blocks = notion._markdown_to_blocks(content)
        dividers = [b for b in blocks if b["type"] == "divider"]
        self.assertEqual(len(dividers), 1)

    def test_blocks_table(self):
        """Pipe-delimited table produces Notion table + table_row blocks."""
        notion = self._notion()
        content = "| Col1 | Col2 |\n|------|------|\n| A    | B    |\n| C    | D    |"
        blocks = notion._markdown_to_blocks(content)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "table")
        self.assertTrue(blocks[0]["table"]["has_column_header"])
        self.assertEqual(blocks[0]["table"]["table_width"], 2)
        rows = blocks[0]["table"]["children"]
        self.assertEqual(len(rows), 3)  # Header + 2 data rows (separator skipped)
        self.assertEqual(rows[0]["table_row"]["cells"][0][0]["text"]["content"], "Col1")
        self.assertEqual(rows[1]["table_row"]["cells"][0][0]["text"]["content"], "A")
        self.assertEqual(rows[2]["table_row"]["cells"][0][0]["text"]["content"], "C")

    # ------------------------------------------------------------------ #
    # No 100-block truncation
    # ------------------------------------------------------------------ #

    def test_blocks_no_truncation_at_100(self):
        """Block output is not capped at 100. All blocks are returned."""
        notion = self._notion()
        lines = "\n".join([f"Line {i}" for i in range(250)])
        blocks = notion._markdown_to_blocks(lines)
        self.assertEqual(len(blocks), 250)

    # ------------------------------------------------------------------ #
    # Batch append
    # ------------------------------------------------------------------ #

    @patch.dict(os.environ, {"TEST_NOTION_TOKEN": "tok", "TEST_NOTION_DB": "db"})
    def test_append_blocks_in_batches_splits_correctly(self):
        """250 blocks are split into 3 batches (100+100+50) via PATCH calls."""
        notion = self._notion()
        blocks = [{"type": "paragraph", "paragraph": {"rich_text": []}} for _ in range(250)]

        with patch.object(notion, "_request") as mock_req:
            mock_req.return_value = {}
            notion._append_blocks_in_batches("page-id", blocks, batch_size=100)
            self.assertEqual(mock_req.call_count, 3)
            # Verify batch sizes
            call_args = mock_req.call_args_list
            self.assertEqual(len(call_args[0][0][2]["children"]), 100)
            self.assertEqual(len(call_args[1][0][2]["children"]), 100)
            self.assertEqual(len(call_args[2][0][2]["children"]), 50)

    @patch.dict(os.environ, {"TEST_NOTION_TOKEN": "tok", "TEST_NOTION_DB": "db"})
    def test_create_page_with_250_blocks_appends_remainder(self):
        """create_page sends first 100 in create call, then batches the rest."""
        notion = self._notion()
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        tmp.write("\n".join([f"Line {i}" for i in range(250)]))
        tmp.close()

        with patch.object(notion, "_request") as mock_req:
            mock_req.return_value = {"id": "new-page-id"}
            page_id = notion.sync_file(Path(tmp.name), "Projects",
                                       content_type="Project")
            # First call: POST pages (creates with first 100 children)
            first_call = mock_req.call_args_list[0]
            self.assertEqual(first_call[0][0], "POST")
            self.assertEqual(len(first_call[0][2]["children"]), 100)
            # Subsequent calls: PATCH blocks/{page_id}/children for batches 2 and 3
            patch_calls = [c for c in mock_req.call_args_list
                           if c[0][0] == "PATCH" and "children" in c[0][1]]
            self.assertEqual(len(patch_calls), 2)

        os.unlink(tmp.name)

    # ------------------------------------------------------------------ #
    # Icons and covers on create/update
    # ------------------------------------------------------------------ #

    @patch.dict(os.environ, {"TEST_NOTION_TOKEN": "tok", "TEST_NOTION_DB": "db"})
    def test_create_page_includes_icon_and_cover(self):
        """create_page body includes icon and cover when content_type is supplied."""
        notion = self._notion()
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        tmp.write("# Hello")
        tmp.close()

        with patch.object(notion, "_request") as mock_req:
            mock_req.return_value = {"id": "page-id"}
            notion.sync_file(Path(tmp.name), "Daily",
                             content_type="Daily Note")
            body = mock_req.call_args_list[0][0][2]
            self.assertIn("icon", body)
            self.assertEqual(body["icon"]["emoji"], "📅")
            self.assertIn("cover", body)
            self.assertIn("gradients_2", body["cover"]["external"]["url"])

        os.unlink(tmp.name)

    @patch.dict(os.environ, {"TEST_NOTION_TOKEN": "tok", "TEST_NOTION_DB": "db"})
    def test_create_page_type_property_set(self):
        """create_page sets Type select property to content_type value."""
        notion = self._notion()
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        tmp.write("# Hello")
        tmp.close()

        with patch.object(notion, "_request") as mock_req:
            mock_req.return_value = {"id": "page-id"}
            notion.sync_file(Path(tmp.name), "Daily",
                             content_type="Session Handoff")
            body = mock_req.call_args_list[0][0][2]
            self.assertEqual(body["properties"]["Type"]["select"]["name"], "Session Handoff")
            self.assertEqual(body["icon"]["emoji"], "🤖")

        os.unlink(tmp.name)

    # ------------------------------------------------------------------ #
    # sync_file routing
    # ------------------------------------------------------------------ #

    @patch.dict(os.environ, {"TEST_NOTION_TOKEN": "tok", "TEST_NOTION_DB": "db"})
    def test_sync_file_creates_page(self):
        """sync_file calls create_page for new files."""
        notion = self._notion()
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
        notion = self._notion()
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        tmp.write("# Updated\nNew content.")
        tmp.close()

        with patch.object(notion, "_request") as mock_req:
            mock_req.return_value = {"results": []}
            page_id = notion.sync_file(Path(tmp.name), "Projects",
                                       existing_page_id="existing-id")
            self.assertEqual(page_id, "existing-id")
            self.assertGreaterEqual(mock_req.call_count, 2)

        os.unlink(tmp.name)

    @patch.dict(os.environ, {"TEST_NOTION_TOKEN": "tok", "TEST_NOTION_DB": "db"})
    def test_create_page_raises_without_config(self):
        """Sync raises error when credentials missing."""
        os.environ.pop("TEST_NOTION_TOKEN", None)
        notion = self._notion()
        with self.assertRaises(NotionSyncError):
            notion.create_page("Title", "Projects", "content", "file.md", "path")


    # ------------------------------------------------------------------ #
    # Tokenizer: inline code, links, strikethrough, combined annotations
    # ------------------------------------------------------------------ #

    def test_rich_text_inline_code(self):
        """Backtick-wrapped text produces code annotation."""
        notion = self._notion()
        result = notion._rich_text("Run `git status` now")
        texts = [(r["text"]["content"], r.get("annotations", {})) for r in result]
        self.assertIn(("git status", {"code": True}), texts)

    def test_rich_text_link(self):
        """Markdown link produces text with link field."""
        notion = self._notion()
        result = notion._rich_text("See [docs](https://example.com) here")
        link_parts = [r for r in result if r["text"].get("link")]
        self.assertEqual(len(link_parts), 1)
        self.assertEqual(link_parts[0]["text"]["content"], "docs")
        self.assertEqual(link_parts[0]["text"]["link"]["url"], "https://example.com")

    def test_rich_text_strikethrough(self):
        """Double-tilde text produces strikethrough annotation."""
        notion = self._notion()
        result = notion._rich_text("This is ~~deleted~~ text")
        texts = [(r["text"]["content"], r.get("annotations", {})) for r in result]
        self.assertIn(("deleted", {"strikethrough": True}), texts)

    def test_rich_text_combined_bold_code(self):
        """Bold + code combined on same span via **`text`** syntax."""
        notion = self._notion()
        result = notion._rich_text("Run **`git status`** now")
        combined = [r for r in result if r.get("annotations", {}).get("bold")
                    and r.get("annotations", {}).get("code")]
        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0]["text"]["content"], "git status")

    def test_rich_text_link_with_bold(self):
        """Link text is captured correctly (nested bold inside link is Tier 2)."""
        notion = self._notion()
        result = notion._rich_text("Click [here](https://example.com)")
        link_parts = [r for r in result if r["text"].get("link")]
        self.assertEqual(len(link_parts), 1)
        self.assertEqual(link_parts[0]["text"]["content"], "here")

    # ------------------------------------------------------------------ #
    # Checkbox / to-do blocks
    # ------------------------------------------------------------------ #

    def test_blocks_checkbox_unchecked(self):
        """- [ ] produces to_do block with checked=false."""
        notion = self._notion()
        blocks = notion._markdown_to_blocks("- [ ] Buy groceries")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "to_do")
        self.assertFalse(blocks[0]["to_do"]["checked"])
        self.assertEqual(blocks[0]["to_do"]["rich_text"][0]["text"]["content"], "Buy groceries")

    def test_blocks_checkbox_checked(self):
        """- [x] produces to_do block with checked=true."""
        notion = self._notion()
        blocks = notion._markdown_to_blocks("- [x] Done task")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "to_do")
        self.assertTrue(blocks[0]["to_do"]["checked"])

    def test_blocks_bullet_still_works(self):
        """Plain - item still produces bulleted_list_item after checkbox check."""
        notion = self._notion()
        blocks = notion._markdown_to_blocks("- Regular item")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "bulleted_list_item")

    # ------------------------------------------------------------------ #
    # Table of contents
    # ------------------------------------------------------------------ #

    def test_blocks_toc_prepended_at_3_headings(self):
        """Page with 3+ headings gets TOC block prepended."""
        notion = self._notion()
        md = "# One\n\nText\n\n## Two\n\nText\n\n### Three\n\nText"
        blocks = notion._prepend_toc(notion._markdown_to_blocks(md))
        self.assertEqual(blocks[0]["type"], "table_of_contents")

    def test_blocks_toc_not_prepended_below_threshold(self):
        """Page with fewer than 3 headings has no TOC."""
        notion = self._notion()
        md = "# One\n\nText\n\n## Two\n\nText"
        blocks = notion._prepend_toc(notion._markdown_to_blocks(md))
        self.assertNotEqual(blocks[0]["type"], "table_of_contents")

    def test_blocks_toc_ignores_headings_in_code(self):
        """Heading-like lines inside code blocks are not counted for TOC."""
        notion = self._notion()
        md = "# Real heading\n\n```bash\n# comment one\n# comment two\n```\n\n## Second heading\n\nText"
        blocks = notion._prepend_toc(notion._markdown_to_blocks(md))
        # Only 2 real headings, should NOT have TOC
        self.assertNotEqual(blocks[0]["type"], "table_of_contents")

    # ------------------------------------------------------------------ #
    # Pagination in update_page
    # ------------------------------------------------------------------ #

    @patch.dict(os.environ, {"TEST_NOTION_TOKEN": "tok", "TEST_NOTION_DB": "db"})
    def test_update_page_paginates_block_deletion(self):
        """update_page follows has_more/next_cursor to delete all blocks."""
        notion = self._notion()

        page1_blocks = [{"id": f"block-{i}"} for i in range(100)]
        page2_blocks = [{"id": f"block-{100 + i}"} for i in range(50)]

        call_count = {"get": 0}

        def mock_request(method, endpoint, body=None):
            if method == "GET" and "children" in endpoint:
                call_count["get"] += 1
                if call_count["get"] == 1:
                    return {"results": page1_blocks, "has_more": True,
                            "next_cursor": "cursor-abc"}
                else:
                    return {"results": page2_blocks, "has_more": False}
            if method == "PATCH" and "pages/" in endpoint:
                return {"id": "page-id"}
            if method == "DELETE":
                return {}
            if method == "PATCH" and "children" in endpoint:
                return {}
            return {}

        with patch.object(notion, "_request", side_effect=mock_request):
            notion.update_page("page-id", "Title", "Source", "# H\ntext",
                               "f.md", "path")

        # Should have made 2 GET calls (paginated)
        self.assertEqual(call_count["get"], 2)


if __name__ == "__main__":
    unittest.main()
