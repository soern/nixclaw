"""MemoryManager.get_recent / prune ordering, against a fake collection
(a real chromadb collection would need the embedding model)."""
from unittest.mock import MagicMock

from src.nixclaw_agent.memory import MemoryManager


def _manager(timestamps: list[str]) -> MemoryManager:
    mgr = MemoryManager.__new__(MemoryManager)
    mgr._channel_id = "+000000"
    ids = [f"m{i}" for i in range(len(timestamps))]
    mgr._collection = MagicMock()
    mgr._collection.get.side_effect = lambda where=None, limit=None: {
        "ids": ids[:limit],
        "documents": [f"doc {i}" for i in ids][:limit],
        "metadatas": [{"type": "fact", "timestamp": ts} for ts in timestamps][:limit],
    }
    return mgr


# Insertion order differs from time order: the newest are stored first and last.
TIMESTAMPS = [
    "2026-10-03T00:00:00",
    "2026-10-01T00:00:00",
    "2026-10-02T00:00:00",
    "2026-10-04T00:00:00",
]


def test_get_recent_returns_newest_not_first_stored():
    recent = _manager(TIMESTAMPS).get_recent(limit=2)
    assert [m["id"] for m in recent] == ["m3", "m0"]


def test_prune_deletes_oldest():
    mgr = _manager(TIMESTAMPS)
    assert mgr.prune(max_memories=2) == 2
    mgr._collection.delete.assert_called_once_with(ids=["m1", "m2"])
