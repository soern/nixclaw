"""Soul management tools for nixclaw-agent.

Provides tools for the agent to update its own soul/persona.
Only privileged users can modify souls.
"""

import logging

from .. import prompts
from ..types import ToolResult
from ..soul import soul_manager

LOG = logging.getLogger(__name__)


async def update_soul(new_content: str, channel_id: str) -> ToolResult:
    """Update the agent's soul/persona for this channel.

    Only channel-specific SOUL.md can be modified by the agent.
    The user will be notified of any changes.

    Args:
        new_content: The new SOUL.md content
        channel_id: The channel ID for this conversation

    Returns:
        ToolResult with success status or error
    """
    try:
        if not new_content.strip():
            return ToolResult(
                name="update_soul",
                success=False,
                output=None,
                error="Soul content cannot be empty",
            )

        success = soul_manager.write_channel_soul(channel_id, new_content)

        if success:
            return ToolResult(
                name="update_soul",
                success=True,
                output=prompts.current().render("soul-updated", channel_id=channel_id),
                error=None,
            )
        else:
            return ToolResult(
                name="update_soul",
                success=False,
                output=None,
                error="Failed to write soul file",
            )

    except Exception as e:
        LOG.error(f"Error updating soul for channel {channel_id}: {e}")
        return ToolResult(
            name="update_soul",
            success=False,
            output=None,
            error=str(e),
        )


async def read_soul(channel_id: str) -> ToolResult:
    """Read the current soul/persona for this channel.

    Args:
        channel_id: The channel ID for this conversation

    Returns:
        ToolResult with soul content or error
    """
    try:
        soul_content = soul_manager.get_soul_content(channel_id)
        soul_exists = soul_manager.soul_exists(channel_id)

        if soul_exists:
            return ToolResult(
                name="read_soul",
                success=True,
                output=f"Current soul for {channel_id}:\n\n{soul_content}",
                error=None,
            )
        else:
            return ToolResult(
                name="read_soul",
                success=True,
                output=f"No custom soul file exists for {channel_id}. Using default template:\n\n{soul_content}",
                error=None,
            )

    except Exception as e:
        LOG.error(f"Error reading soul for channel {channel_id}: {e}")
        return ToolResult(
            name="read_soul",
            success=False,
            output=None,
            error=str(e),
        )
