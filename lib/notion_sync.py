"""
Notion sync module for Doc Chain.

Creates and updates pages in a Notion database using the REST API.
Each vault file becomes a Notion page with:
  - Title matching the file name (without extension)
  - Source folder property (Projects, Decisions, Plans)
  - Full markdown content as page body
  - Synced timestamp
  - Original vault path for traceability

Requires:
  - NOTION_DOC_CHAIN_TOKEN env var (integration token)
  - NOTION_DOC_CHAIN_DB env var (target database ID)
  - Database shared with the integration
  - Database columns: Name (title), Source (select), Synced At (date),
    File Name (rich text), Vault Path (rich text)
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"


class NotionSyncError(Exception):
    """Raised when a Notion API call fails."""
    pass


class NotionSync:
    """Syncs markdown files to a Notion database."""

    def __init__(self, config: dict):
        token_env = config.get("api_token_env", "NOTION_DOC_CHAIN_TOKEN")
        db_env = config.get("database_id_env", "NOTION_DOC_CHAIN_DB")

        self.token = os.environ.get(token_env, "")
        self.database_id = os.environ.get(db_env, "")
        self.properties = config.get("properties", {})
        self.enabled = config.get("enabled", True)

    def is_configured(self) -> bool:
        """Check if Notion credentials are available."""
        return bool(self.token and self.database_id)

    def _request(self, method: str, endpoint: str, body: dict = None) -> dict:
        """Make a Notion API request."""
        url = f"{NOTION_BASE_URL}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_API_VERSION,
        }

        data = json.dumps(body).encode("utf-8") if body else None
        req = Request(url, data=data, headers=headers, method=method)

        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            raise NotionSyncError(
                f"Notion API {method} {endpoint} returned {e.code}: {error_body}"
            )
        except URLError as e:
            raise NotionSyncError(f"Notion API connection failed: {e.reason}")

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                return content[end + 3:].strip()
        return content

    def _markdown_to_blocks(self, content: str) -> list:
        """Convert markdown content to Notion block objects.

        Handles: headings (h1-h3), bullet lists, numbered lists, paragraphs.
        Notion API limits: max 100 blocks per request, max 2000 chars per rich text.
        """
        blocks = []
        clean = self._strip_frontmatter(content)
        lines = clean.split("\n")

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # Headings
            if stripped.startswith("### "):
                blocks.append(self._heading_block(stripped[4:], 3))
            elif stripped.startswith("## "):
                blocks.append(self._heading_block(stripped[3:], 2))
            elif stripped.startswith("# "):
                blocks.append(self._heading_block(stripped[2:], 1))
            # Bullet list
            elif stripped.startswith("- ") or stripped.startswith("* "):
                blocks.append(self._list_block(stripped[2:], "bulleted_list_item"))
            # Numbered list
            elif re.match(r"^\d+\.\s", stripped):
                text = re.sub(r"^\d+\.\s", "", stripped)
                blocks.append(self._list_block(text, "numbered_list_item"))
            # Paragraph
            else:
                blocks.append(self._paragraph_block(stripped))

        # Notion limit: 100 blocks per request
        return blocks[:100]

    def _rich_text(self, text: str) -> list:
        """Create a Notion rich text array, chunking at 2000 chars."""
        chunks = []
        while text:
            chunk = text[:2000]
            text = text[2000:]
            chunks.append({"type": "text", "text": {"content": chunk}})
        return chunks if chunks else [{"type": "text", "text": {"content": ""}}]

    def _heading_block(self, text: str, level: int) -> dict:
        key = f"heading_{level}"
        return {"object": "block", "type": key, key: {"rich_text": self._rich_text(text)}}

    def _paragraph_block(self, text: str) -> dict:
        return {"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": self._rich_text(text)}}

    def _list_block(self, text: str, block_type: str) -> dict:
        return {"object": "block", "type": block_type,
                block_type: {"rich_text": self._rich_text(text)}}

    def create_page(self, title: str, source_folder: str,
                    content: str, file_name: str, vault_path: str) -> str:
        """Create a new Notion page in the database.

        Returns the page ID for future updates.
        """
        if not self.is_configured():
            raise NotionSyncError("Notion is not configured. Set token and database ID env vars.")

        prop_title = self.properties.get("title", "Name")
        prop_source = self.properties.get("source_folder", "Source")
        prop_synced = self.properties.get("synced_at", "Synced At")
        prop_file = self.properties.get("file_name", "File Name")
        prop_vault = self.properties.get("vault_path", "Vault Path")

        now = datetime.now(timezone.utc).isoformat()

        body = {
            "parent": {"database_id": self.database_id},
            "properties": {
                prop_title: {"title": self._rich_text(title)},
                prop_source: {"select": {"name": source_folder}},
                prop_synced: {"date": {"start": now}},
                prop_file: {"rich_text": self._rich_text(file_name)},
                prop_vault: {"rich_text": self._rich_text(vault_path)},
            },
            "children": self._markdown_to_blocks(content),
        }

        result = self._request("POST", "pages", body)
        return result.get("id", "")

    def update_page(self, page_id: str, title: str, source_folder: str,
                    content: str, file_name: str, vault_path: str) -> str:
        """Update an existing Notion page.

        Updates properties and replaces all child blocks with new content.
        Returns the page ID.
        """
        if not self.is_configured():
            raise NotionSyncError("Notion is not configured.")

        prop_title = self.properties.get("title", "Name")
        prop_source = self.properties.get("source_folder", "Source")
        prop_synced = self.properties.get("synced_at", "Synced At")

        now = datetime.now(timezone.utc).isoformat()

        # Update properties
        props_body = {
            "properties": {
                prop_title: {"title": self._rich_text(title)},
                prop_source: {"select": {"name": source_folder}},
                prop_synced: {"date": {"start": now}},
            }
        }
        self._request("PATCH", f"pages/{page_id}", props_body)

        # Delete existing blocks
        existing = self._request("GET", f"blocks/{page_id}/children?page_size=100")
        for block in existing.get("results", []):
            try:
                self._request("DELETE", f"blocks/{block['id']}")
            except NotionSyncError:
                pass  # Block may already be gone

        # Add new blocks
        new_blocks = self._markdown_to_blocks(content)
        if new_blocks:
            self._request("PATCH", f"blocks/{page_id}/children",
                          {"children": new_blocks})

        return page_id

    def sync_file(self, file_path: Path, source_folder: str,
                  existing_page_id: str = None, vault_rel_path: str = None) -> str:
        """Sync a single vault file to Notion.

        Creates a new page or updates an existing one.
        Returns the Notion page ID.
        """
        content = file_path.read_text(encoding="utf-8", errors="replace")
        title = file_path.stem.replace("-", " ").replace("_", " ").title()
        file_name = file_path.name
        vault_path = vault_rel_path or f"Claude Code/{source_folder}/{file_name}"

        if existing_page_id:
            return self.update_page(
                existing_page_id, title, source_folder,
                content, file_name, vault_path
            )
        else:
            return self.create_page(
                title, source_folder, content, file_name, vault_path
            )
