"""Per-channel single-message mode (off/append/replace), see turn_output.py.

Stored in its own file (data/<channel>/message_mode.json) because
set_channel_mode overwrites the whole channel_mode.json. Missing file ->
the global chat.single_message.mode.
"""

import json
import logging
from pathlib import Path

from ..types import ToolResult
from ..config import config, MESSAGE_MODES
from ..path_safety import is_within_directory

LOG = logging.getLogger(__name__)

MESSAGE_MODE_CONFIG_PATH = Path("message_mode.json")


def _mode_file(channel_id: str) -> Path:
    context_dir = (config.paths.data_dir / channel_id).resolve()
    if not is_within_directory(context_dir, config.paths.data_dir):
        raise ValueError("Path traversal detected")
    mode_path = (context_dir / MESSAGE_MODE_CONFIG_PATH).resolve()
    if not is_within_directory(mode_path, context_dir):
        raise ValueError("Path traversal detected")
    return mode_path


def _channel_override(channel_id: str) -> str | None:
    try:
        mode_path = _mode_file(channel_id)
        if mode_path.exists():
            with open(mode_path) as f:
                mode = json.load(f).get("mode")
            if mode in MESSAGE_MODES:
                return mode
    except Exception as e:
        LOG.warning(f"Could not read message mode for {channel_id}: {e}")
    return None


def resolve_message_mode(channel_id: str) -> str:
    """Effective mode for a channel: its override, else the global default."""
    return _channel_override(channel_id) or config.chat.single_message.effective_mode


def set_message_mode(channel_id: str, mode: str) -> ToolResult:
    if mode not in MESSAGE_MODES:
        return ToolResult(
            name="set_message_mode",
            success=False,
            output=None,
            error=f"Invalid mode '{mode}'; use one of: {', '.join(MESSAGE_MODES)}",
        )
    try:
        mode_path = _mode_file(channel_id)
        mode_path.parent.mkdir(parents=True, exist_ok=True)
        with open(mode_path, "w") as f:
            json.dump({"mode": mode}, f, indent=2)
        return ToolResult(
            name="set_message_mode",
            success=True,
            output=f"Message mode set to '{mode}' (takes effect from the next turn)",
            error=None,
        )
    except Exception as e:
        return ToolResult(name="set_message_mode", success=False, output=None, error=str(e))


def get_message_mode(channel_id: str) -> ToolResult:
    try:
        override = _channel_override(channel_id)
        mode = override or config.chat.single_message.effective_mode
        return ToolResult(
            name="get_message_mode",
            success=True,
            output=json.dumps({"mode": mode, "source": "channel" if override else "global default"}, indent=2),
            error=None,
        )
    except Exception as e:
        return ToolResult(name="get_message_mode", success=False, output=None, error=str(e))
