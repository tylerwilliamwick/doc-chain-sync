"""
Logging utility for Doc Chain.

Writes structured log entries to a rotating log file.
Also supports stdout for interactive debugging.
"""

import os
from datetime import datetime
from pathlib import Path


class SyncLogger:
    """Simple rotating file logger for the sync chain."""

    def __init__(self, log_file: str, max_size: int = 1048576):
        self.log_file = Path(log_file).expanduser()
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.max_size = max_size

    def _rotate_if_needed(self):
        """Rotate log file if it exceeds max size."""
        if self.log_file.exists() and self.log_file.stat().st_size > self.max_size:
            # Keep last half of the file
            content = self.log_file.read_text(errors="replace")
            lines = content.split("\n")
            half = len(lines) // 2
            self.log_file.write_text("\n".join(lines[half:]))

    def log(self, level: str, message: str):
        """Write a log entry."""
        self._rotate_if_needed()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{ts}] [{level.upper()}] {message}\n"
        with open(self.log_file, "a") as f:
            f.write(entry)

    def info(self, message: str):
        self.log("INFO", message)

    def error(self, message: str):
        self.log("ERROR", message)

    def warn(self, message: str):
        self.log("WARN", message)

    def sync_event(self, file_name: str, target: str, status: str, detail: str = ""):
        """Log a sync event with structured format."""
        msg = f"[{target}] {file_name}: {status}"
        if detail:
            msg += f" ({detail})"
        self.log("SYNC", msg)
