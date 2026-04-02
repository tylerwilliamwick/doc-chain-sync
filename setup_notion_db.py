#!/usr/bin/env python3
"""
Idempotent setup script for the Notion unified database.

Checks whether the 'Type' select property exists in the target database.
If missing, adds it with all 7 content type options.
Safe to re-run: does nothing if the property already exists.

Usage:
    python3 setup_notion_db.py [--config path/to/config.yaml]

Prerequisites:
    - NOTION_DOC_CHAIN_TOKEN env var set (Notion integration token)
    - NOTION_DOC_CHAIN_DB env var set (target database ID)
    - Integration has access to the database
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import yaml

NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"

# Content type options with display colors
CONTENT_TYPE_OPTIONS = [
    {"name": "Daily Note",      "color": "blue"},
    {"name": "Meeting Notes",   "color": "purple"},
    {"name": "Retro",           "color": "orange"},
    {"name": "Project",         "color": "gray"},
    {"name": "Decision",        "color": "yellow"},
    {"name": "Plan",            "color": "green"},
    {"name": "Session Handoff", "color": "default"},
]


def notion_request(method: str, endpoint: str, token: str, body: dict = None) -> dict:
    """Make a Notion API request."""
    url = f"{NOTION_BASE_URL}/{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
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
        print(f"Notion API error {e.code}: {error_body}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"Notion API connection failed: {e.reason}", file=sys.stderr)
        sys.exit(1)


def load_config(config_path: str = None) -> dict:
    """Load configuration from YAML file."""
    if config_path is None:
        config_path = Path(__file__).parent / "config.yaml"
    else:
        config_path = Path(config_path)
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="Set up Notion unified database properties")
    parser.add_argument("--config", help="Path to config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    notion_cfg = config.get("notion", {})

    token_env = notion_cfg.get("api_token_env", "NOTION_DOC_CHAIN_TOKEN")
    db_env = notion_cfg.get("database_id_env", "NOTION_DOC_CHAIN_DB")

    token = os.environ.get(token_env, "")
    database_id = os.environ.get(db_env, "")

    if not token:
        print(f"Error: {token_env} environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    if not database_id:
        print(f"Error: {db_env} environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    type_prop_name = notion_cfg.get("properties", {}).get("type_field", "Type")

    print(f"Checking database {database_id} for '{type_prop_name}' property...")

    db = notion_request("GET", f"databases/{database_id}", token)
    existing_props = db.get("properties", {})

    if type_prop_name in existing_props:
        prop = existing_props[type_prop_name]
        if prop.get("type") == "select":
            existing_options = {o["name"] for o in prop.get("select", {}).get("options", [])}
            required_options = {o["name"] for o in CONTENT_TYPE_OPTIONS}
            if required_options.issubset(existing_options):
                print(f"'{type_prop_name}' property already exists with all required options. Nothing to do.")
                return
            else:
                missing = required_options - existing_options
                print(f"'{type_prop_name}' exists but is missing options: {missing}. Adding missing options...")
        else:
            print(f"Error: '{type_prop_name}' exists but is not a select property (type: {prop.get('type')}).",
                  file=sys.stderr)
            sys.exit(1)
    else:
        print(f"'{type_prop_name}' property not found. Adding it now...")

    patch_body = {
        "properties": {
            type_prop_name: {
                "select": {
                    "options": CONTENT_TYPE_OPTIONS,
                }
            }
        }
    }

    notion_request("PATCH", f"databases/{database_id}", token, patch_body)
    print(f"Successfully added '{type_prop_name}' select property with {len(CONTENT_TYPE_OPTIONS)} options:")
    for opt in CONTENT_TYPE_OPTIONS:
        print(f"  - {opt['name']} ({opt['color']})")
    print("\nDatabase is ready. Run 'python3 dispatcher.py --force --verbose' to migrate all content.")


if __name__ == "__main__":
    main()
