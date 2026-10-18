"""Debug tools for viewing discarded cronjob outputs."""

from typing import Optional

from ..db.operations import get_discarded_messages
from ..types import ToolResult


def debug_cron(
    seconds: int = 3600,
    limit: int = 10,
    channel_id: Optional[str] = None,
) -> ToolResult:
    """View discarded cronjob outputs for debugging.

    Args:
        seconds: How far back to search in seconds (default: 3600 = 1 hour)
        limit: Max messages to return (default: 10)
        channel_id: The channel ID (auto-injected)

    Returns:
        ToolResult with discarded cronjob outputs or error.
    """
    try:
        if not channel_id:
            return ToolResult(
                name="debug_cron",
                success=False,
                output=None,
                error="channel_id is required",
            )

        messages = get_discarded_messages(channel_id, "cronjob", seconds, limit)

        if not messages:
            return ToolResult(
                name="debug_cron",
                success=True,
                output="No discarded cronjob messages found in the specified time window.",
                error=None,
            )

        lines = []
        for i, msg in enumerate(messages, 1):
            from datetime import datetime, timezone
            ts = datetime.fromtimestamp(msg.event.timestamp, tz=timezone.utc)
            lines.append(f"--- Discarded Cronjob #{i} [{ts.strftime('%Y-%m-%d %H:%M:%S')}] ---")
            if msg.source_id:
                lines.append(f"Job ID: {msg.source_id}")
            lines.append(f"Output: {msg.event.message}")
            lines.append("")

        return ToolResult(
            name="debug_cron",
            success=True,
            output="\n".join(lines),
            error=None,
        )

    except Exception as e:
        return ToolResult(
            name="debug_cron",
            success=False,
            output=None,
            error=str(e),
        )