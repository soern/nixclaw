"""Tool for mute/silent mode management."""

import json
import logging
from pathlib import Path
from typing import Optional

from ..types import ToolResult
from ..config import config
from ..path_safety import is_within_directory

LOG = logging.getLogger(__name__)

MUTE_CONFIG_PATH = Path("mute.json")


def _get_context_dir(channel_id: str) -> Path:
    context_dir = (config.paths.data_dir / channel_id).resolve()
    if not is_within_directory(context_dir, config.paths.data_dir):
        raise ValueError("Path traversal detected")
    return context_dir


def is_channel_muted(channel_id: str) -> bool:
    context_dir = _get_context_dir(channel_id)
    mute_config_path = (context_dir / MUTE_CONFIG_PATH).resolve()
    if not is_within_directory(mute_config_path, context_dir):
        return False
    if mute_config_path.exists():
        with open(mute_config_path) as f:
            data = json.load(f)
            return data.get("muted", False)
    return False


def set_channel_muted(channel_id: str, muted: bool) -> ToolResult:
    try:
        context_dir = _get_context_dir(channel_id)
        context_dir.mkdir(parents=True, exist_ok=True)
        mute_config_path = (context_dir / MUTE_CONFIG_PATH).resolve()
        if not is_within_directory(mute_config_path, context_dir):
            return ToolResult(
                name="mute_channel",
                success=False,
                output=None,
                error="Path traversal detected",
            )
        with open(mute_config_path, "w") as f:
            json.dump({"muted": muted}, f, indent=2)
        return ToolResult(
            name="mute_channel",
            success=True,
            output=f"Channel {'muted' if muted else 'unmuted'} successfully",
            error=None,
        )
    except Exception as e:
        return ToolResult(
            name="mute_channel",
            success=False,
            output=None,
            error=str(e),
        )


def mute_channel(channel_id: str) -> ToolResult:
    """Mute the channel - agent will stop responding to non-trigger messages."""
    return set_channel_muted(channel_id, True)


def unmute_channel(channel_id: str) -> ToolResult:
    """Unmute the channel - agent will resume normal responses."""
    return set_channel_muted(channel_id, False)


def read_mute_status(channel_id: str) -> ToolResult:
    """Read current mute status for this channel."""
    try:
        muted = is_channel_muted(channel_id)
        return ToolResult(
            name="read_mute_status",
            success=True,
            output=json.dumps({"muted": muted}, indent=2),
            error=None,
        )
    except Exception as e:
        return ToolResult(
            name="read_mute_status",
            success=False,
            output=None,
            error=str(e),
        )


MUTE_KEYWORDS = [
    "be quiet",
    "be silent",
    "stay silent",
    "don't respond",
    "don't say anything",
    "stop talking",
    "do not respond",
    "hush",
]


def contains_mute_request(message: str) -> bool:
    """Check if message contains a mute/silence request."""
    import re
    lower_msg = message.lower()
    for keyword in MUTE_KEYWORDS:
        pattern = r'\b' + re.escape(keyword) + r'(?:\s|$)'
        if re.search(pattern, lower_msg):
            return True
    if re.search(r'^\s*mute[d]?\s*$', lower_msg):
        return True
    if re.search(r'\bmute[d]?\s+(yourself|me|this|channel|on|off)\b', lower_msg):
        return True
    if re.search(r'\bmute[d]?\s*[.,!?]$', lower_msg):
        return True
    if re.search(r'\bquiet\b', lower_msg) and not re.search(r'\bquietly\b', lower_msg):
        return True
    if re.search(r'\bsilent\b', lower_msg):
        return True
    return False
