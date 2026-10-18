"""Context management for nixclaw-agent."""

from pathlib import Path
from typing import Optional

from src.nixclaw_agent.config import PathsConfig
from src.nixclaw_agent.types import Context, Message
from src.nixclaw_agent.db.operations import DatabaseOperations
from src.nixclaw_agent.soul import soul_manager, SOUL_FILENAME

# Maximum number of messages to load for summarization.
# Known edge: a >500-message backlog will have mark_summarized applied by
# timestamp to rows never summarized; this is acceptable.
HISTORY_LOAD_LIMIT = 500


class ContextManager:
    """Manages context per Signal channel."""

    def __init__(self, config: PathsConfig, db_ops: DatabaseOperations):
        self._config = config
        self._db_ops = db_ops

    def get_context(self, channel_id: str, lane: str = "interactive") -> Context:
        """Get or create context for a channel.

        lane: "interactive" for chat turns, "background" for cronjob/subagent
        runs (gives the run its own ephemeral browser scope, see
        Context.browser_scope).
        """
        channel_dir = self._config.data_dir / channel_id
        channel_dir.mkdir(parents=True, exist_ok=True)

        messages_db = channel_dir / "messages.db"
        attachments_dir = channel_dir / "attachments"
        attachments_dir.mkdir(parents=True, exist_ok=True)

        memory_dir = channel_dir / "memories"
        memory_dir.mkdir(parents=True, exist_ok=True)

        soul_path = channel_dir / SOUL_FILENAME

        return Context(
            channel_id=channel_id,
            messages_db=messages_db,
            attachments_dir=attachments_dir,
            soul_path=soul_path if soul_path.exists() else None,
            memory_dir=memory_dir,
            lane=lane,
        )

    def get_soul_content(self, channel_id: str) -> str:
        """Get soul content for a channel using fallback chain.

        Priority: channel soul -> custom/soul.md -> default/soul.md
        """
        return soul_manager.get_soul_content(channel_id)

    def load_history(self, channel_id: str, limit: int = 10) -> list[Message]:
        """Load last n messages for a channel."""
        return self._db_ops.get_messages(channel_id, limit)

    def load_extended_history(self, channel_id: str, limit: int = 50) -> list[Message]:
        """Load last n messages for privileged users to recover context."""
        return self._db_ops.get_messages(channel_id, limit)

    def get_message_count(self, channel_id: str) -> int:
        """Get total count of non-summarized messages for a channel."""
        return self._db_ops.get_total_message_count(channel_id)

    def get_messages_for_llm(self, channel_id: str, before_timestamp: int | None = None) -> list[Message]:
        """Get non-summarized, non-discarded messages for a channel (up to HISTORY_LOAD_LIMIT).

        If before_timestamp is given, only messages with timestamp <=
        before_timestamp are returned. Pass event.timestamp - 1 to exclude
        the just-stored current event (timestamps are whole seconds and the
        underlying predicate is inclusive).
        """
        return self._db_ops.get_messages(
            channel_id,
            limit=HISTORY_LOAD_LIMIT,
            include_summarized=False,
            exclude_discarded=True,
            before_timestamp=before_timestamp,
        )

    def mark_summarized(self, channel_id: str, before_timestamp: int) -> None:
        """Mark messages as summarized up to the given timestamp."""
        self._db_ops.mark_messages_summarized(channel_id, before_timestamp)

    def get_summary(self, channel_id: str) -> tuple[str, list[str]] | None:
        """Get summary and keywords for a channel. Returns None if no summary exists."""
        return self._db_ops.get_summary(channel_id)

    def store_summary(
        self,
        channel_id: str,
        content: str,
        keywords: list[str],
        version: int,
        token_count: int,
        messages_before_count: int,
    ) -> None:
        """Store or update summary for a channel."""
        self._db_ops.upsert_summary(
            channel_id, content, keywords, version, token_count, messages_before_count
        )
