"""
Notion sync module for Doc Chain.

Creates and updates pages in a Notion database using the REST API.
Each vault file becomes a Notion page with:
  - Title matching the file name (without extension)
  - Source folder property (Projects, Decisions, Plans, Daily, Meetings, Retros)
  - Content type property (Daily Note, Meeting Notes, Retro, Project, Decision, Plan, Session Handoff)
  - Emoji icon and cover image keyed by content type
  - Full markdown content as page body (with code, callout, table, divider, inline formatting)
  - Synced timestamp
  - Original vault path for traceability

Requires:
  - NOTION_DOC_CHAIN_TOKEN env var (integration token)
  - NOTION_DOC_CHAIN_DB env var (target database ID)
  - Database shared with the integration
  - Database columns: Name (title), Source (select), Type (select), Synced At (date),
    File Name (rich text), Vault Path (rich text)
"""

import json
import os
import random
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"

# HTTP status codes that warrant a retry
_RETRY_STATUSES = {429, 502, 503}
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0  # seconds; doubles on each attempt


class NotionSyncError(Exception):
    """Raised when a Notion API call fails."""
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class NotionSync:
    """Syncs markdown files to a Notion database."""

    def __init__(self, config: dict):
        token_env = config.get("api_token_env", "NOTION_DOC_CHAIN_TOKEN")
        db_env = config.get("database_id_env", "NOTION_DOC_CHAIN_DB")

        self.token = os.environ.get(token_env, "")
        self.database_id = os.environ.get(db_env, "")
        self.properties = config.get("properties", {})
        self.enabled = config.get("enabled", True)
        self.icons = config.get("page_icons", {})
        self.covers = config.get("page_covers", {})

    def is_configured(self) -> bool:
        """Check if Notion credentials are available."""
        return bool(self.token and self.database_id)

    def _request(self, method: str, endpoint: str, body: dict = None) -> dict:
        """Make a Notion API request with retry on transient errors (429, 502, 503)."""
        url = f"{NOTION_BASE_URL}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_API_VERSION,
        }

        data = json.dumps(body).encode("utf-8") if body else None

        delay = _RETRY_BASE_DELAY
        for attempt in range(_MAX_RETRIES + 1):
            req = Request(url, data=data, headers=headers, method=method)
            try:
                with urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except HTTPError as e:
                if e.code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                    print(f"Notion API {method} {endpoint} returned {e.code}, "
                          f"retrying in {delay:.1f}s (attempt {attempt + 1}/{_MAX_RETRIES})")
                    time.sleep(delay + random.uniform(0, delay * 0.1))
                    delay *= 2
                    continue
                error_body = e.read().decode("utf-8", errors="replace")
                # Truncate error body to prevent leaking sensitive API response data
                safe_body = error_body[:200] if error_body else ""
                raise NotionSyncError(
                    f"Notion API {method} {endpoint} returned {e.code}: {safe_body}",
                    status_code=e.code
                )
            except URLError as e:
                raise NotionSyncError(f"Notion API connection failed: {e.reason}")

        raise NotionSyncError(f"Notion API {method} {endpoint} failed after {_MAX_RETRIES} retries")

    def _append_blocks_in_batches(self, page_id: str, blocks: list,
                                   batch_size: int = 100) -> None:
        """Append blocks to a page in batches of at most batch_size."""
        for i in range(0, len(blocks), batch_size):
            batch = blocks[i:i + batch_size]
            self._request("PATCH", f"blocks/{page_id}/children", {"children": batch})

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                return content[end + 3:].strip()
        return content

    _SAFE_URL_SCHEMES = ("https://", "http://", "mailto:", "notion://")

    def _sanitize_link(self, url: str):
        """Return url if scheme is safe, else None."""
        cleaned = url.strip()
        if any(cleaned.lower().startswith(s) for s in self._SAFE_URL_SCHEMES):
            return cleaned
        return None

    def _rich_text(self, text: str) -> list:
        """Create a Notion rich text array with tokenizer supporting combined annotations.

        Inline markers supported (precedence order):
          [text](url)  -> link (text.link field, not annotation)
          **text**     -> bold annotation
          *text*       -> italic annotation
          `text`       -> code annotation
          ~~text~~     -> strikethrough annotation (exactly 2 tildes)

        Multiple annotations can apply to the same span (e.g., bold + code).
        Splits text longer than 2000 chars into plain chunks.
        Nested inline markers (e.g., [**bold**](url)) are a known Tier 2 limitation.
        """
        if not text:
            return [{"type": "text", "text": {"content": ""}}]

        # Tokenize: find all inline markers and build (text, annotations, link) tuples.
        # Process in one pass with a combined regex. Order in alternation sets precedence.
        token_re = re.compile(
            r'(\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\))'  # [text](url) — link (balanced parens)
            r'|(\*\*`[^`]+`\*\*)'           # **`code`** — bold + code
            r'|(`[^`\n]+`)'                  # `code` (no newlines)
            r'|(~~(?:[^~]|~(?!~))+~~)'       # ~~strike~~ (allows interior single ~)
            r'|(\*\*[^*\n]+\*\*)'            # **bold** (no newlines)
            r'|(\*[^*\n]+\*)'                # *italic* (no newlines)
        )

        # Each part: {"text": str, "annotations": set, "link": str or None}
        parts = []
        pos = 0
        for m in token_re.finditer(text):
            start = m.start()
            if start > pos:
                parts.append({"text": text[pos:start], "annotations": set(), "link": None})

            if m.group(2) is not None:
                # Link: [text](url)
                parts.append({"text": m.group(2), "annotations": set(), "link": m.group(3)})
            elif m.group(4) is not None:
                # Bold + code: **`text`**
                inner = m.group(4)[3:-3]  # strip **` and `**
                parts.append({"text": inner, "annotations": {"bold", "code"}, "link": None})
            elif m.group(5) is not None:
                # Code: `text`
                inner = m.group(5)[1:-1]
                parts.append({"text": inner, "annotations": {"code"}, "link": None})
            elif m.group(6) is not None:
                # Strikethrough: ~~text~~
                inner = m.group(6)[2:-2]
                parts.append({"text": inner, "annotations": {"strikethrough"}, "link": None})
            elif m.group(7) is not None:
                # Bold: **text**
                inner = m.group(7)[2:-2]
                parts.append({"text": inner, "annotations": {"bold"}, "link": None})
            elif m.group(8) is not None:
                # Italic: *text*
                inner = m.group(8)[1:-1]
                parts.append({"text": inner, "annotations": {"italic"}, "link": None})

            pos = m.end()

        if pos < len(text):
            parts.append({"text": text[pos:], "annotations": set(), "link": None})

        # Convert parts to Notion rich text objects, chunking at 2000 chars
        result = []
        for part in parts:
            chunk_text = part["text"]
            while chunk_text:
                chunk = chunk_text[:2000]
                chunk_text = chunk_text[2000:]
                obj = {"type": "text", "text": {"content": chunk}}
                if part["link"]:
                    safe_url = self._sanitize_link(part["link"])
                    if safe_url:
                        obj["text"]["link"] = {"url": safe_url}
                ann = part["annotations"]
                if ann:
                    obj["annotations"] = {}
                    for a in ("bold", "italic", "code", "strikethrough"):
                        if a in ann:
                            obj["annotations"][a] = True
                result.append(obj)

        return result if result else [{"type": "text", "text": {"content": ""}}]

    def _heading_block(self, text: str, level: int) -> dict:
        key = f"heading_{level}"
        return {"object": "block", "type": key, key: {"rich_text": self._rich_text(text)}}

    def _paragraph_block(self, text: str) -> dict:
        return {"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": self._rich_text(text)}}

    def _list_block(self, text: str, block_type: str) -> dict:
        return {"object": "block", "type": block_type,
                block_type: {"rich_text": self._rich_text(text)}}

    def _todo_block(self, text: str, checked: bool) -> dict:
        return {"object": "block", "type": "to_do",
                "to_do": {"rich_text": self._rich_text(text), "checked": checked}}

    def _code_block(self, code: str, language: str = "plain text") -> dict:
        return {
            "object": "block",
            "type": "code",
            "code": {
                "rich_text": self._rich_text(code),
                "language": language or "plain text",
            },
        }

    def _callout_block(self, text: str) -> dict:
        return {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": self._rich_text(text),
                "icon": {"type": "emoji", "emoji": "💡"},
            },
        }

    def _divider_block(self) -> dict:
        return {"object": "block", "type": "divider", "divider": {}}

    def _table_blocks(self, rows: list) -> list:
        """Convert a list of row lists into a Notion table + table_row blocks.

        The first row is treated as the header row.
        Returns a list: [table_block] where table_block has table_rows as children.
        """
        if not rows:
            return []

        table_width = max(len(row) for row in rows)

        table_rows = []
        for row in rows:
            cells = []
            for i in range(table_width):
                cell_text = row[i].strip() if i < len(row) else ""
                cells.append(self._rich_text(cell_text))
            table_rows.append({
                "object": "block",
                "type": "table_row",
                "table_row": {"cells": cells},
            })

        table_block = {
            "object": "block",
            "type": "table",
            "table": {
                "table_width": table_width,
                "has_column_header": True,
                "has_row_header": False,
                "children": table_rows,
            },
        }
        return [table_block]

    def _prepend_toc(self, blocks: list) -> list:
        """Prepend a table of contents block if there are 3+ heading blocks.

        Counts heading blocks in the output list (not raw markdown) to avoid
        miscounting headings inside fenced code blocks.
        """
        heading_count = sum(
            1 for b in blocks
            if b.get("type", "").startswith("heading_")
        )
        if heading_count >= 3:
            toc = {"object": "block", "type": "table_of_contents",
                   "table_of_contents": {"color": "default"}}
            return [toc] + blocks
        return blocks

    def _markdown_to_blocks(self, content: str) -> list:
        """Convert markdown content to Notion block objects.

        State machine parser supporting:
          - Headings (h1-h3)
          - Bullet and numbered lists
          - Fenced code blocks (triple backtick with optional language hint)
          - Blockquotes (> prefix) as callout blocks
          - Horizontal rules (--- alone) as divider blocks
          - Pipe-delimited tables as Notion table blocks
          - Paragraphs
          - Inline bold (**text**) and italic (*text*) via _rich_text()

        Does NOT truncate at 100 blocks. Caller is responsible for batching.
        """
        blocks = []
        clean = self._strip_frontmatter(content)
        lines = clean.split("\n")

        # Parser states
        STATE_NORMAL = "normal"
        STATE_CODE = "code_fence"
        STATE_TABLE = "table"

        state = STATE_NORMAL
        code_lines = []
        code_lang = ""
        table_rows = []

        def flush_table():
            nonlocal table_rows
            if table_rows:
                blocks.extend(self._table_blocks(table_rows))
                table_rows = []

        for line in lines:
            stripped = line.strip()

            # --- Code fence state ---
            if state == STATE_CODE:
                if stripped.startswith("```"):
                    blocks.append(self._code_block("\n".join(code_lines), code_lang))
                    code_lines = []
                    code_lang = ""
                    state = STATE_NORMAL
                else:
                    code_lines.append(line)
                continue

            # --- Table state ---
            if state == STATE_TABLE:
                if stripped.startswith("|") and stripped.endswith("|"):
                    # Skip separator rows (e.g. |---|---|)
                    cells = [c.strip() for c in stripped.strip("|").split("|")]
                    if all(re.match(r'^[-:]+$', c) for c in cells):
                        continue
                    table_rows.append(cells)
                    continue
                else:
                    flush_table()
                    state = STATE_NORMAL
                    # Fall through to process this line normally

            # --- Normal state ---
            if not stripped:
                continue

            # Enter code fence
            if stripped.startswith("```"):
                lang = stripped[3:].strip()
                code_lang = lang
                code_lines = []
                state = STATE_CODE
                continue

            # Enter table
            if stripped.startswith("|") and stripped.endswith("|"):
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                table_rows.append(cells)
                state = STATE_TABLE
                continue

            # Horizontal rule (must not be confused with frontmatter delimiter already stripped)
            if stripped == "---" or stripped == "***" or stripped == "___":
                blocks.append(self._divider_block())
                continue

            # Blockquote
            if stripped.startswith("> "):
                blocks.append(self._callout_block(stripped[2:]))
                continue
            if stripped == ">":
                blocks.append(self._callout_block(""))
                continue

            # Headings
            if stripped.startswith("### "):
                blocks.append(self._heading_block(stripped[4:], 3))
            elif stripped.startswith("## "):
                blocks.append(self._heading_block(stripped[3:], 2))
            elif stripped.startswith("# "):
                blocks.append(self._heading_block(stripped[2:], 1))
            # Checkbox / to-do (must check BEFORE generic bullet)
            elif stripped.startswith("- [ ] "):
                blocks.append(self._todo_block(stripped[6:], checked=False))
            elif stripped.startswith("- [x] ") or stripped.startswith("- [X] "):
                blocks.append(self._todo_block(stripped[6:], checked=True))
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

        # Flush any open table at end of content
        if state == STATE_TABLE:
            flush_table()
        # Unclosed code fence: emit what we have
        if state == STATE_CODE and code_lines:
            blocks.append(self._code_block("\n".join(code_lines), code_lang))

        return blocks

    def create_page(self, title: str, source_folder: str,
                    content: str, file_name: str, vault_path: str,
                    content_type: str = None) -> str:
        """Create a new Notion page in the database.

        Returns the page ID for future updates.
        """
        if not self.is_configured():
            raise NotionSyncError("Notion is not configured. Set token and database ID env vars.")

        prop_title = self.properties.get("title", "Name")
        prop_source = self.properties.get("source_folder", "Source")
        prop_type = self.properties.get("type_field", "Type")
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
        }

        if content_type:
            body["properties"][prop_type] = {"select": {"name": content_type}}
            icon = self.icons.get(content_type)
            cover = self.covers.get(content_type)
            if icon:
                body["icon"] = {"type": "emoji", "emoji": icon}
            if cover:
                body["cover"] = {"type": "external", "external": {"url": cover}}

        all_blocks = self._prepend_toc(self._markdown_to_blocks(content))
        body["children"] = all_blocks[:100]

        result = self._request("POST", "pages", body)
        page_id = result.get("id", "")

        if page_id and len(all_blocks) > 100:
            self._append_blocks_in_batches(page_id, all_blocks[100:])

        return page_id

    def update_page(self, page_id: str, title: str, source_folder: str,
                    content: str, file_name: str, vault_path: str,
                    content_type: str = None) -> str:
        """Update an existing Notion page.

        Updates properties (including icon/cover if content_type supplied) and replaces
        all child blocks with new content.
        Returns the page ID.
        """
        if not self.is_configured():
            raise NotionSyncError("Notion is not configured.")

        prop_title = self.properties.get("title", "Name")
        prop_source = self.properties.get("source_folder", "Source")
        prop_type = self.properties.get("type_field", "Type")
        prop_synced = self.properties.get("synced_at", "Synced At")

        now = datetime.now(timezone.utc).isoformat()

        props_body = {
            "properties": {
                prop_title: {"title": self._rich_text(title)},
                prop_source: {"select": {"name": source_folder}},
                prop_synced: {"date": {"start": now}},
            }
        }

        if content_type:
            props_body["properties"][prop_type] = {"select": {"name": content_type}}
            icon = self.icons.get(content_type)
            cover = self.covers.get(content_type)
            if icon:
                props_body["icon"] = {"type": "emoji", "emoji": icon}
            if cover:
                props_body["cover"] = {"type": "external", "external": {"url": cover}}

        self._request("PATCH", f"pages/{page_id}", props_body)

        # Delete existing blocks (paginated — collect all IDs first, then delete)
        block_ids = []
        cursor = None
        while True:
            endpoint = f"blocks/{page_id}/children?page_size=100"
            if cursor:
                endpoint += f"&start_cursor={urllib.parse.quote(cursor, safe='')}"
            existing = self._request("GET", endpoint)
            block_ids.extend(b["id"] for b in existing.get("results", []))
            if not existing.get("has_more"):
                break
            cursor = existing.get("next_cursor")
            if not cursor:
                break  # Defensive: malformed API response, don't spin

        for bid in block_ids:
            try:
                self._request("DELETE", f"blocks/{bid}")
            except NotionSyncError as e:
                if e.status_code == 404:
                    pass  # Block already deleted
                else:
                    raise

        # Append new blocks in batches
        new_blocks = self._prepend_toc(self._markdown_to_blocks(content))
        if new_blocks:
            self._append_blocks_in_batches(page_id, new_blocks)

        return page_id

    def sync_file(self, file_path: Path, source_folder: str,
                  existing_page_id: str = None, vault_rel_path: str = None,
                  content_type: str = None) -> str:
        """Sync a single vault file to Notion.

        Creates a new page or updates an existing one.
        Returns the Notion page ID.
        """
        content = file_path.read_text(encoding="utf-8", errors="replace")
        title = file_path.stem.replace("-", " ").replace("_", " ").title()
        file_name = file_path.name
        vault_path = vault_rel_path or f"{source_folder}/{file_name}"

        if existing_page_id:
            return self.update_page(
                existing_page_id, title, source_folder,
                content, file_name, vault_path,
                content_type=content_type,
            )
        else:
            return self.create_page(
                title, source_folder, content, file_name, vault_path,
                content_type=content_type,
            )
