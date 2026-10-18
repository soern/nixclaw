"""Soul management for nixclaw-agent.

Handles loading, caching, and persisting per-channel SOUL.md files.
Resolution order: channel data/<channel>/SOUL.md, then the configurable
prompt soul (config/prompts/custom/soul.md, else default/soul.md).
"""

import logging
from pathlib import Path
from typing import Optional

from . import prompts
from .config import config

LOG = logging.getLogger(__name__)

SOUL_FILENAME = "SOUL.md"


class SoulManager:
    def __init__(self):
        self._soul_cache: dict[str, tuple[str, float]] = {}
        self._config = config

    def _get_channel_soul_path(self, channel_id: str) -> Path:
        channel_dir = self._config.paths.data_dir / channel_id
        return channel_dir / SOUL_FILENAME

    def _get_mtime(self, path: Path) -> Optional[float]:
        try:
            return path.stat().st_mtime
        except (OSError, FileNotFoundError):
            return None

    def _read_soul_file(self, path: Path) -> Optional[str]:
        try:
            return path.read_text()
        except (OSError, FileNotFoundError):
            return None

    def load_soul(self, channel_id: str) -> Optional[str]:
        """Channel SOUL.md content (mtime-cached), or None if the channel has none."""
        channel_soul_path = self._get_channel_soul_path(channel_id)
        channel_mtime = self._get_mtime(channel_soul_path)

        cached = self._soul_cache.get(channel_id)
        if cached and channel_mtime == cached[1]:
            return cached[0]

        if channel_mtime is not None:
            content = self._read_soul_file(channel_soul_path)
            if content is not None:
                self._soul_cache[channel_id] = (content, channel_mtime)
                return content

        return None

    def get_soul_content(self, channel_id: str) -> str:
        soul = self.load_soul(channel_id)
        if soul:
            return soul

        # Not cached: a prompt reload must be visible immediately.
        return prompts.current().text("soul")

    def write_channel_soul(self, channel_id: str, content: str) -> bool:
        channel_soul_path = self._get_channel_soul_path(channel_id)
        channel_dir = channel_soul_path.parent

        try:
            channel_dir.mkdir(parents=True, exist_ok=True)
            channel_soul_path.write_text(content)
            mtime = self._get_mtime(channel_soul_path)
            if mtime is not None:
                self._soul_cache[channel_id] = (content, mtime)
            LOG.info(f"Updated soul for channel {channel_id}")
            return True
        except OSError as e:
            LOG.error(f"Failed to write soul for channel {channel_id}: {e}")
            return False

    def soul_exists(self, channel_id: str) -> bool:
        channel_soul_path = self._get_channel_soul_path(channel_id)
        return channel_soul_path.exists()

    def invalidate_cache(self, channel_id: str) -> None:
        self._soul_cache.pop(channel_id, None)


soul_manager = SoulManager()
