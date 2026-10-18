"""Skills system for nixclaw-agent."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from ..config import PROJECT_ROOT, config as agent_config

LOG = logging.getLogger(__name__)


@dataclass
class SkillEntry:
    name: str
    description: str
    keywords: list[str]
    file: Optional[str] = None


def load_skills_manifest() -> list[SkillEntry]:
    """Load skills manifest from config/skills.yaml (copy of config/skills.yaml.example)."""
    manifest_path = PROJECT_ROOT / "config" / "skills.yaml"
    if not manifest_path.exists():
        LOG.warning(f"Skills manifest not found: {manifest_path}")
        return []

    try:
        with open(manifest_path) as f:
            data = yaml.safe_load(f) or {}
        skills = data.get("skills", [])
        return [SkillEntry(**skill) for skill in skills]
    except Exception as e:
        LOG.error(f"Failed to load skills manifest: {e}")
        return []


def get_user_skill_index_path(channel_id: str) -> Path:
    """Get path to user skill index for a channel."""
    return agent_config.paths.data_dir / channel_id / "config" / "skills.yaml"


def load_user_skills_manifest(channel_id: str) -> list[SkillEntry]:
    """Load user skills manifest for a channel if it exists."""
    manifest_path = get_user_skill_index_path(channel_id)
    if not manifest_path.exists():
        return []

    try:
        with open(manifest_path) as f:
            data = yaml.safe_load(f) or {}
        skills = data.get("skills", [])
        return [SkillEntry(**skill) for skill in skills]
    except Exception as e:
        LOG.error(f"Failed to load user skills manifest for {channel_id}: {e}")
        return []


def get_skill_file_path(name: str, channel_id: Optional[str] = None) -> Optional[Path]:
    """Get the full path to a skill's SKILL.md file.
    
    If channel_id is provided, checks user skill index first for overrides,
    then falls back to system skill index.
    """
    if ".." in name or "/" in name or "\\" in name:
        LOG.warning(f"Invalid skill name attempted: {name}")
        return None
    
    if channel_id:
        user_manifest = load_user_skills_manifest(channel_id)
        for entry in user_manifest:
            if entry.name == name:
                return agent_config.paths.data_dir / channel_id / "skills" / name / "SKILL.md"
    
    manifest = load_skills_manifest()
    for entry in manifest:
        if entry.name == name and entry.file:
            return PROJECT_ROOT / entry.file
    return None
