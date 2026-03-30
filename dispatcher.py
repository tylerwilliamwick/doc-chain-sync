#!/usr/bin/env python3
"""
Doc Chain Sync Dispatcher

Main entry point for the documentation chain. Scans the Obsidian vault for
deliverables and decisions, then syncs changed files to Notion and Google Drive
(for NotebookLM ingestion).

Designed to run as a LaunchAgent on a polling interval (default 5 min).
Can also be run manually: python dispatcher.py [--dry-run] [--force] [--verbose]

Flow:
  1. Load config and sync state
  2. Scan watched vault folders for .md files
  3. Compare modification times against state to find changed files
  4. For each changed file:
     a. Sync to Notion (create or update page)
     b. Sync to Google Drive (copy or rclone upload)
     c. Update state with new timestamps and IDs
  5. Clean up state entries for deleted files
  6. Save state and exit
"""

import argparse
import os
import sys
from pathlib import Path

import yaml

# Add lib to path
sys.path.insert(0, str(Path(__file__).parent / "lib"))

from state import SyncState
from notion_sync import NotionSync, NotionSyncError
from gdrive_sync import GDriveSync, GDriveSyncError
from logger import SyncLogger


def load_config(config_path: str = None) -> dict:
    """Load configuration from YAML file."""
    if config_path is None:
        config_path = Path(__file__).parent / "config.yaml"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def scan_vault(vault_base: Path, watch_folders: list,
               exclude_patterns: list) -> list:
    """Scan vault folders for syncable files.

    Returns list of (file_path, source_folder) tuples.
    """
    files = []

    for folder_name in watch_folders:
        folder = vault_base / folder_name
        if not folder.exists():
            continue

        for md_file in folder.rglob("*.md"):
            name = md_file.name
            skip = False
            for pattern in exclude_patterns:
                if pattern.startswith("*"):
                    if name.endswith(pattern[1:]):
                        skip = True
                        break
                elif name == pattern:
                    skip = True
                    break
            if not skip:
                files.append((md_file, folder_name))

    return files


def cleanup_deleted(state: SyncState, vault_base: Path, logger: SyncLogger):
    """Remove state entries for files that no longer exist in the vault."""
    all_synced = state.get_all_synced_files()
    for vault_path in list(all_synced.keys()):
        full_path = vault_base / vault_path.replace("Claude Code/", "", 1)
        if not full_path.exists():
            state.remove_file(vault_path)
            logger.info(f"Removed state for deleted file: {vault_path}")


def run_sync(config: dict, dry_run: bool = False,
             force: bool = False, verbose: bool = False):
    """Execute the sync pipeline."""
    vault_cfg = config["vault"]
    vault_base = Path(vault_cfg["base_path"]).expanduser()
    watch_folders = vault_cfg["watch_folders"]
    exclude_patterns = vault_cfg.get("exclude_patterns", [])

    sync_cfg = config["sync"]
    logger = SyncLogger(sync_cfg["log_file"], sync_cfg.get("max_log_size", 1048576))
    state = SyncState(sync_cfg["state_file"])

    notion = NotionSync(config.get("notion", {}))
    gdrive = GDriveSync(config.get("google_drive", {}))

    logger.info("Doc Chain sync starting")

    if not vault_base.exists():
        logger.error(f"Vault base does not exist: {vault_base}")
        return {"synced": 0, "errors": 1, "skipped": 0}

    # Check service availability
    notion_available = notion.enabled and notion.is_configured()
    gdrive_available = gdrive.enabled and gdrive.is_configured()

    if not notion_available:
        logger.warn("Notion sync disabled or not configured")
    if not gdrive_available:
        logger.warn("Google Drive sync disabled or not configured")

    if not notion_available and not gdrive_available:
        logger.warn("No sync targets available. Nothing to do.")
        return {"synced": 0, "errors": 0, "skipped": 0}

    # Scan vault
    files = scan_vault(vault_base, watch_folders, exclude_patterns)
    logger.info(f"Found {len(files)} files in watched folders")

    stats = {"synced": 0, "errors": 0, "skipped": 0}

    for file_path, source_folder in files:
        # Use relative path from vault base to handle nested subdirectories
        try:
            rel = file_path.relative_to(vault_base)
        except ValueError:
            rel = Path(source_folder) / file_path.name
        vault_path = f"Claude Code/{rel}"
        mtime = file_path.stat().st_mtime

        if not force and not state.needs_sync(vault_path, mtime):
            stats["skipped"] += 1
            continue

        if dry_run:
            logger.info(f"[DRY RUN] Would sync: {file_path.name} ({source_folder})")
            stats["synced"] += 1
            continue

        if verbose:
            logger.info(f"Syncing: {file_path.name} from {source_folder}")

        file_synced = False
        notion_page_id = state.get_notion_page_id(vault_path)
        gdrive_path = None

        # Sync to Notion
        if notion_available:
            try:
                notion_page_id = notion.sync_file(
                    file_path, source_folder,
                    existing_page_id=notion_page_id,
                    vault_rel_path=vault_path,
                )
                logger.sync_event(file_path.name, "notion", "ok",
                                  "updated" if notion_page_id else "created")
                file_synced = True
            except Exception as e:
                logger.sync_event(file_path.name, "notion", "error", str(e))
                state.record_failure(vault_path, "notion", str(e))
                stats["errors"] += 1

        # Sync to Google Drive (use relative path for nested subfolder support)
        if gdrive_available:
            try:
                drive_subfolder = str(rel.parent) if rel.parent != Path(".") else source_folder
                gdrive_path = gdrive.sync_file(file_path, drive_subfolder)
                logger.sync_event(file_path.name, "gdrive", "ok", gdrive_path)
                file_synced = True
            except Exception as e:
                logger.sync_event(file_path.name, "gdrive", "error", str(e))
                state.record_failure(vault_path, "gdrive", str(e))
                stats["errors"] += 1

        if file_synced:
            state.record_sync(vault_path, mtime,
                              notion_page_id=notion_page_id,
                              gdrive_path=gdrive_path)
            stats["synced"] += 1

    # Clean up deleted files from state
    cleanup_deleted(state, vault_base, logger)

    # Save state
    state.save()

    logger.info(
        f"Sync complete: {stats['synced']} synced, "
        f"{stats['skipped']} unchanged, {stats['errors']} errors"
    )
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Doc Chain Sync: Vault -> Notion + Google Drive"
    )
    parser.add_argument("--config", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log what would sync without syncing")
    parser.add_argument("--force", action="store_true",
                        help="Sync all files regardless of modification time")
    parser.add_argument("--verbose", action="store_true",
                        help="Log each file as it syncs")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Override dry_run from config if CLI flag set
    if args.dry_run:
        config.setdefault("sync", {})["dry_run"] = True

    stats = run_sync(
        config,
        dry_run=args.dry_run or config.get("sync", {}).get("dry_run", False),
        force=args.force,
        verbose=args.verbose,
    )

    if stats["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
