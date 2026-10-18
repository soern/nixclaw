"""Memory system using ChromaDB for vector storage."""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import chromadb
from chromadb.config import Settings

if TYPE_CHECKING:
    from chromadb.api import ClientAPI

from src.nixclaw_agent.config import config
from src.nixclaw_agent.path_safety import is_within_directory

LOG = logging.getLogger(__name__)


class MemoryManager:
    """Manages per-channel ChromaDB vector memory."""

    def __init__(self, channel_id: str, persist_dir: Path) -> None:
        self._channel_id = channel_id
        self._persist_dir = self._validate_path(persist_dir)
        # Both set by _init_client, which raises on failure. The collection
        # is Any: chromadb types every get()/query() result field as Optional
        # (depends on `include`), and all callers already catch exceptions.
        self._client: "ClientAPI"
        self._collection: Any
        self._init_client()

    def _init_client(self) -> None:
        """Initialize ChromaDB client with proper directory setup."""
        try:
            self._persist_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self._persist_dir, 0o755)
            for item in self._persist_dir.iterdir():
                if item.is_dir():
                    os.chmod(item, 0o755)
                else:
                    os.chmod(item, 0o644)
            self._client = chromadb.PersistentClient(
                path=str(self._persist_dir),
                settings=Settings(anonymized_telemetry=False, allow_reset=True),
            )
            self._collection = self._client.get_or_create_collection(
                name="memories",
            )
            LOG.info(
                "MemoryManager initialized for channel %s at %s",
                self._channel_id,
                self._persist_dir,
            )
        except Exception as e:
            LOG.error(
                "Failed to initialize ChromaDB for channel %s: %s",
                self._channel_id,
                e,
            )
            raise

    def _validate_path(self, persist_dir: Path) -> Path:
        base = config.paths.data_dir
        resolved = persist_dir.resolve()
        if not is_within_directory(resolved, base):
            raise ValueError("Path traversal detected")
        return resolved

    def add_memory(
        self, content: str, memory_type: str, source_msg_id: str
    ) -> bool:
        """Add a memory to the collection."""
        try:
            memory_id = f"{source_msg_id}_{memory_type}_{datetime.now(timezone.utc).timestamp()}"
            metadata = {
                "type": memory_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source_msg_id": source_msg_id,
            }
            self._collection.add(
                documents=[content],
                metadatas=[metadata],
                ids=[memory_id],
            )
            return True
        except Exception as e:
            LOG.error(
                "Failed to add memory for channel %s: %s",
                self._channel_id,
                e,
            )
            return False

    def search(
        self,
        query: str,
        limit: int = 5,
        memory_type: Optional[str] = None,
    ) -> list[dict]:
        """Search memories by vector similarity."""
        try:
            where_filter = {"type": memory_type} if memory_type else None
            results = self._collection.query(
                query_texts=[query],
                n_results=limit,
                where=where_filter,
            )
            memories = []
            if results["ids"]:
                for i, mem_id in enumerate(results["ids"][0]):
                    memories.append({
                        "id": mem_id,
                        "content": results["documents"][0][i],
                        "type": results["metadatas"][0][i]["type"],
                        "timestamp": results["metadatas"][0][i]["timestamp"],
                        "distance": results["distances"][0][i],
                    })
            return memories
        except Exception as e:
            LOG.error(
                "Failed to search memories for channel %s: %s",
                self._channel_id,
                e,
            )
            return []

    def get_recent(self, limit: int = 10) -> list[dict]:
        """Get most recent memories by timestamp."""
        try:
            # Fetch all, then sort: chromadb's get(limit=) returns an
            # arbitrary (insertion-order) subset, not the newest entries.
            results = self._collection.get(
                where=None,
            )
            if not results["ids"]:
                return []
            memories = []
            for i, mem_id in enumerate(results["ids"]):
                memories.append({
                    "id": mem_id,
                    "content": results["documents"][i],
                    "type": results["metadatas"][i]["type"],
                    "timestamp": results["metadatas"][i]["timestamp"],
                })
            memories.sort(
                key=lambda m: m["timestamp"],
                reverse=True,
            )
            return memories[:limit]
        except Exception as e:
            LOG.error(
                "Failed to get recent memories for channel %s: %s",
                self._channel_id,
                e,
            )
            return []

    def prune(self, max_memories: int = 100) -> int:
        """Remove oldest memories beyond max_memories limit."""
        try:
            all_memories = self._collection.get(
                where=None,
            )
            if not all_memories["ids"]:
                return 0
            count = len(all_memories["ids"])
            if count <= max_memories:
                return 0
            to_delete = count - max_memories
            memories_with_ts = []
            for i, mem_id in enumerate(all_memories["ids"]):
                memories_with_ts.append({
                    "id": mem_id,
                    "timestamp": all_memories["metadatas"][i]["timestamp"],
                })
            memories_with_ts.sort(key=lambda m: m["timestamp"])
            ids_to_delete = [m["id"] for m in memories_with_ts[:to_delete]]
            self._collection.delete(ids=ids_to_delete)
            return to_delete
        except Exception as e:
            LOG.error(
                "Failed to prune memories for channel %s: %s",
                self._channel_id,
                e,
            )
            return 0

    def reset(self) -> bool:
        """Delete all memories for this channel."""
        try:
            self._collection.delete(where=None)
            LOG.info(f"Reset all memories for channel {self._channel_id}")
            return True
        except Exception as e:
            LOG.error(
                "Failed to reset memories for channel %s: %s",
                self._channel_id,
                e,
            )
            return False

    def close(self) -> None:
        """Close the ChromaDB client."""
        try:
            # close() exists on the concrete Client, not on the ClientAPI
            # interface PersistentClient is declared to return.
            self._client.close()  # type: ignore[attr-defined]
        except Exception as e:
            LOG.error(
                "Failed to close memory client for channel %s: %s",
                self._channel_id,
                e,
            )
