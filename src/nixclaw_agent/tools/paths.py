"""Shared channel-scoped path helpers for tools.

The attachments directory is `{data_dir}/{channel_id}/attachments` and must
stay sandboxed to the channel's context dir - channel_id comes from the LLM
-adjacent tool layer and has to survive path traversal attempts.
"""

from pathlib import Path

from ..config import config
from ..path_safety import is_within_directory


def get_context_dir(channel_id: str) -> Path:
    """Get the context directory path for a channel, ensuring it's sandboxed."""
    context_base = config.paths.data_dir
    context_dir = (context_base / channel_id).resolve()
    if not is_within_directory(context_dir, context_base):
        raise ValueError("Path traversal detected")
    return context_dir


def get_attachments_dir(channel_id: str) -> Path:
    """Get the attachments directory path for a channel, ensuring it's sandboxed."""
    context_dir = get_context_dir(channel_id)
    attachments_dir = (context_dir / "attachments").resolve()
    if not is_within_directory(attachments_dir, context_dir):
        raise ValueError("Path traversal detected")
    return attachments_dir
