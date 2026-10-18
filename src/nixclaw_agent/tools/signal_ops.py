"""Signal operations tools for nixclaw-agent."""

import logging
from pathlib import Path
from typing import Optional

from ..config import config
from ..path_safety import is_within_directory
from ..signal import SignalClient
from ..types import ToolResult
from .paths import get_attachments_dir as _get_attachments_dir
from .registry import get_current_turn_output

LOG = logging.getLogger(__name__)


SIGNAL_CLIENT: SignalClient = SignalClient(
    api_url=config.signal.api_url,
    phone_number=config.signal.phone_number,
    webhook_port=config.signal.webhook_port,
)


async def send_signal_attachment(filename: str, caption: Optional[str], channel_id: str) -> ToolResult:
    """Send a file attachment via Signal to the user.

    Args:
        filename: Filename of the attachment in the attachments/ directory
        caption: Optional caption/text to send with the attachment
        channel_id: The channel ID to scope file operations to

    Returns:
        ToolResult with success status or error
    """
    try:
        if ".." in filename or "/" in filename:
            return ToolResult(
                name="send_signal_attachment",
                success=False,
                output=None,
                error="Invalid filename - path traversal not allowed",
            )

        attachments_dir = _get_attachments_dir(channel_id)
        file_path = (attachments_dir / filename).resolve()

        if not is_within_directory(file_path, attachments_dir):
            return ToolResult(
                name="send_signal_attachment",
                success=False,
                output=None,
                error="Invalid filename - path traversal not allowed",
            )

        if not file_path.exists():
            return ToolResult(
                name="send_signal_attachment",
                success=False,
                output=None,
                error=f"File not found: {filename}",
            )

        content_type = _get_content_type(filename)

        ts = await SIGNAL_CLIENT.send_attachment(
            recipient=channel_id,
            file_path=str(file_path),
            content_type=content_type,
            caption=caption if caption else None,
        )
        # Single-message mode: this attachment message becomes the turn's
        # target, so later texts edit its caption instead of older messages.
        turn_output = get_current_turn_output()
        if turn_output is not None:
            turn_output.note_attachment(ts, caption)

        return ToolResult(
            name="send_signal_attachment",
            success=True,
            output=f"Sent attachment: {filename}",
            error=None,
        )

    except Exception as e:
        LOG.error(f"Failed to send signal attachment: {e}")
        return ToolResult(
            name="send_signal_attachment",
            success=False,
            output=None,
            error=str(e),
        )


def _get_content_type(filename: str) -> str:
    """Determine content type from filename extension."""
    ext = Path(filename).suffix.lower()
    content_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".svg": "image/svg+xml",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".html": "text/html",
        ".xml": "application/xml",
        ".json": "application/json",
        ".zip": "application/zip",
        ".tar": "application/x-tar",
        ".gz": "application/gzip",
        ".aac": "audio/aac",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
    }
    return content_types.get(ext, "application/octet-stream")