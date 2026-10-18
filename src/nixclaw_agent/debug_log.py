"""Debug logging for nixclaw-agent."""

import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Optional

LOG = logging.getLogger(__name__)

_debug_logger: Optional["DebugLogger"] = None


def get_debug_logger() -> "DebugLogger":
    """Get the configured debug logger instance."""
    global _debug_logger
    if _debug_logger is None:
        raise RuntimeError("Debug logger not configured. Call configure_debug_logging() first.")
    return _debug_logger


def safe_get_debug_logger() -> Optional["DebugLogger"]:
    """Get the debug logger instance, or None if not configured."""
    global _debug_logger
    return _debug_logger


def configure_debug_logging(mode: str, file_path: str, buffer_size: int = 400) -> "DebugLogger":
    """Configure and return the debug logger singleton."""
    global _debug_logger
    _debug_logger = DebugLogger(mode, file_path, buffer_size)
    return _debug_logger


class DebugLogger:
    VALID_MODES = ("off", "auto", "on")
    VALID_EVENT_TYPES = (
        "incoming_event", "llm_input", "llm_output",
        "tool_call", "tool_result", "outgoing_message"
    )

    def __init__(self, mode: str, file_path: str, buffer_size: int = 400):
        if mode not in self.VALID_MODES:
            raise ValueError(f"Invalid mode: {mode}. Must be one of {self.VALID_MODES}")

        self.mode = mode
        self.file_path = file_path
        self.buffer: deque[dict] = deque(maxlen=buffer_size)
        self.buffer_size = buffer_size

    def log(self, event_type: str, channel_id: str, data: dict) -> None:
        if self.mode == "off":
            return

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "channel": channel_id,
            "data": data,
        }

        if self.mode == "on":
            self._write_entry(entry)
        else:
            self.buffer.append(entry)

    def _write_entry(self, entry: dict) -> None:
        with open(self.file_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def flush_buffer(self, trigger: str) -> None:
        if self.mode not in ("auto", "on"):
            return

        marker = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "buffer_flush",
            "trigger": trigger,
            "entries_follow": len(self.buffer),
        }

        with open(self.file_path, "a") as f:
            f.write(json.dumps(marker) + "\n")
            for entry in self.buffer:
                f.write(json.dumps(entry) + "\n")

        self.buffer.clear()

    def record_error(self, error_context: str) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "error",
            "channel": "",
            "data": {"error_context": error_context},
        }

        if self.mode == "on":
            self._write_entry(entry)
        elif self.mode == "auto":
            self.buffer.append(entry)
            self.flush_buffer("error")
