"""Tool for channel mode management."""

import json
import logging
from pathlib import Path
from typing import Literal

from ..types import ToolResult
from ..config import config
from ..path_safety import is_within_directory

LOG = logging.getLogger(__name__)

CHANNEL_MODE_CONFIG_PATH = Path("channel_mode.json")

ChannelMode = Literal["mention_required", "always_process"]


def _get_context_dir(channel_id: str) -> Path:
    context_dir = (config.paths.data_dir / channel_id).resolve()
    if not is_within_directory(context_dir, config.paths.data_dir):
        raise ValueError("Path traversal detected")
    return context_dir


def is_channel_mode_always_process(channel_id: str) -> bool:
    try:
        context_dir = _get_context_dir(channel_id)
        mode_config_path = (context_dir / CHANNEL_MODE_CONFIG_PATH).resolve()
        if not is_within_directory(mode_config_path, context_dir):
            return False
        if mode_config_path.exists():
            with open(mode_config_path) as f:
                data = json.load(f)
                return data.get("mode") == "always_process"
    except Exception:
        pass  # Defensive: if error, assume not always_process
    return False


def set_channel_mode(channel_id: str, mode: ChannelMode) -> ToolResult:
    try:
        context_dir = _get_context_dir(channel_id)
        context_dir.mkdir(parents=True, exist_ok=True)
        mode_config_path = (context_dir / CHANNEL_MODE_CONFIG_PATH).resolve()
        if not is_within_directory(mode_config_path, context_dir):
            return ToolResult(
                name="set_channel_mode",
                success=False,
                output=None,
                error="Path traversal detected",
            )
        with open(mode_config_path, "w") as f:
            json.dump({"mode": mode}, f, indent=2)
        return ToolResult(
            name="set_channel_mode",
            success=True,
            output=f"Channel mode set to '{mode}' successfully",
            error=None,
        )
    except Exception as e:
        return ToolResult(
            name="set_channel_mode",
            success=False,
            output=None,
            error=str(e),
        )


def get_channel_mode(channel_id: str) -> ToolResult:
    try:
        context_dir = _get_context_dir(channel_id)
        mode_config_path = (context_dir / CHANNEL_MODE_CONFIG_PATH).resolve()
        if not is_within_directory(mode_config_path, context_dir):
            return ToolResult(
                name="get_channel_mode",
                success=False,
                output=None,
                error="Path traversal detected",
            )
        if mode_config_path.exists():
            with open(mode_config_path) as f:
                data = json.load(f)
                mode = data.get("mode", "mention_required")
        else:
            mode = "mention_required"
        return ToolResult(
            name="get_channel_mode",
            success=True,
            output=json.dumps({"mode": mode}, indent=2),
            error=None,
        )
    except Exception as e:
        return ToolResult(
            name="get_channel_mode",
            success=False,
            output=None,
            error=str(e),
        )