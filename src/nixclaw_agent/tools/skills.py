"""Skills tool for nixclaw-agent."""

import logging
from pathlib import Path
from typing import Optional

from ..skills import get_skill_file_path, load_skills_manifest, load_user_skills_manifest
from ..types import ToolResult

LOG = logging.getLogger(__name__)


def get_skill(name: str, channel_id: Optional[str] = None) -> ToolResult:
    """Get a skill's SKILL.md content by name.

    Args:
        name: The name of the skill to retrieve
        channel_id: Optional channel ID for user skill override lookup

    Returns:
        ToolResult with skill content or error
    """
    try:
        manifest = load_skills_manifest()
        skill_names = [entry.name for entry in manifest]
        
        if channel_id:
            user_manifest = load_user_skills_manifest(channel_id)
            user_skill_names = [entry.name for entry in user_manifest]
            skill_names = list(set(skill_names) | set(user_skill_names))
        
        if name not in skill_names:
            available = ', '.join(skill_names) if skill_names else 'none'
            return ToolResult(
                name="get_skill",
                success=False,
                output=None,
                error=f"Skill not found: {name}. Available skills: {available}",
            )

        skill_path = get_skill_file_path(name, channel_id)
        if not skill_path:
            return ToolResult(
                name="get_skill",
                success=False,
                output=None,
                error=f"Skill file not found for: {name}",
            )

        if not skill_path.exists():
            return ToolResult(
                name="get_skill",
                success=False,
                output=None,
                error=f"Skill file does not exist: {skill_path}",
            )

        content = skill_path.read_text()
        return ToolResult(
            name="get_skill",
            success=True,
            output=content,
            error=None,
        )

    except Exception as e:
        LOG.error(f"Failed to get skill '{name}': {e}")
        return ToolResult(
            name="get_skill",
            success=False,
            output=None,
            error=str(e),
        )
