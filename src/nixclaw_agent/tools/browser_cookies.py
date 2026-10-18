"""Browser cookie and storage state persistence for per-channel isolation."""

import json
from pathlib import Path
from typing import Optional

from ..config import config
from ..path_safety import is_within_directory


def get_browser_dir(channel_id: str) -> Path:
    """Get browser data directory for channel (e.g., data/+000000/browser/)."""
    context_base = config.paths.data_dir
    context_dir = (context_base / channel_id).resolve()
    if not is_within_directory(context_dir, context_base):
        raise ValueError("Path traversal detected")
    browser_dir = (context_dir / "browser").resolve()
    if not is_within_directory(browser_dir, context_dir):
        raise ValueError("Path traversal detected")
    return browser_dir


def save_storage_state(channel_id: str, state: dict) -> None:
    """Save storage state to channel's browser directory."""
    browser_dir = get_browser_dir(channel_id)
    browser_dir.mkdir(parents=True, exist_ok=True)
    state_file = browser_dir / "storage_state.json"
    temp_file = browser_dir / "storage_state.json.tmp"
    with open(temp_file, "w") as f:
        json.dump(state, f)
    temp_file.replace(state_file)


def load_storage_state(channel_id: str) -> Optional[dict]:
    """Load storage state from channel's browser directory."""
    browser_dir = get_browser_dir(channel_id)
    state_file = browser_dir / "storage_state.json"
    if not state_file.exists():
        return None
    resolved = state_file.resolve()
    if not is_within_directory(resolved, browser_dir):
        raise ValueError("Symlink race condition detected")
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def clear_storage_state(channel_id: str) -> None:
    """Clear stored state for channel."""
    browser_dir = get_browser_dir(channel_id)
    state_file = browser_dir / "storage_state.json"
    if state_file.exists():
        state_file.unlink()
