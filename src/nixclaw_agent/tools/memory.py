"""Memory tools for NixClaw agent."""

import logging
from typing import Optional

from ..memory import MemoryManager
from ..types import ToolResult

LOG = logging.getLogger(__name__)

# Global cache of MemoryManager instances per channel
_memory_managers: dict[str, MemoryManager] = {}

def get_memory_manager(channel_id: str, memory_dir) -> MemoryManager:
    """Get or create MemoryManager for channel."""
    key = f"{channel_id}"
    if key not in _memory_managers:
        _memory_managers[key] = MemoryManager(channel_id, memory_dir)
    return _memory_managers[key]

def remember(channel_id: str, content: str, memory_type: str) -> ToolResult:
    """Store important information in long-term memory for this channel.
    
    Use when:
    - User expresses a preference (likes/dislikes)
    - User gives feedback on your behavior
    - User reveals a fact about themselves
    - You learn something context-specific worth preserving
    
    Types:
    - preference: User likes/dislikes, cultural context
    - feedback: "that was inappropriate", "stop doing X"
    - topic: Ongoing discussion subjects
    - fact: Factual info about users relevant to context
    """
    try:
        # Import here to avoid circular dependency issues at module load
        from ..config import config
        
        memory_dir = config.paths.data_dir / channel_id / "memories"
        manager = get_memory_manager(channel_id, memory_dir)
        
        success = manager.add_memory(
            content=content,
            memory_type=memory_type,
            source_msg_id=f"memory_{channel_id}"
        )
        
        if success:
            LOG.info(f"Stored memory in channel {channel_id}: type={memory_type}")
            return ToolResult(
                name="remember",
                success=True,
                output=f"Remembered: {content}",
                error=None,
            )
        else:
            return ToolResult(
                name="remember",
                success=False,
                output=None,
                error="Failed to store memory",
            )
    except Exception as e:
        LOG.error(f"remember tool failed: {e}")
        return ToolResult(
            name="remember",
            success=False,
            output=None,
            error=str(e),
        )

def recall_memories(channel_id: str, query: str, memory_type: Optional[str] = None) -> ToolResult:
    """Search long-term memory for relevant information.
    
    Use to recall:
    - User preferences from past conversations
    - Past feedback on your behavior
    - Context from earlier in the conversation
    - Topics that were discussed
    
    Returns formatted list of relevant memories.
    """
    try:
        from ..config import config
        
        memory_dir = config.paths.data_dir / channel_id / "memories"
        manager = get_memory_manager(channel_id, memory_dir)
        
        results = manager.search(query=query, limit=5, memory_type=memory_type)
        
        if not results:
            return ToolResult(
                name="recall_memories",
                success=True,
                output="No relevant memories found.",
                error=None,
            )
        
        output_lines = ["Relevant memories:"]
        for r in results:
            output_lines.append(f"- [{r['type']}] {r['content']} (relevance: {1-r['distance']:.2f})")
        
        return ToolResult(
            name="recall_memories",
            success=True,
            output="\n".join(output_lines),
            error=None,
        )
    except Exception as e:
        LOG.error(f"recall_memories tool failed: {e}")
        return ToolResult(
            name="recall_memories",
            success=False,
            output=None,
            error=str(e),
        )
