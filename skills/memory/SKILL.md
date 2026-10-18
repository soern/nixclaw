# Memory

Store and recall important information about the user for long-term reference.

## When to Use

**USE remember when:**
- User expresses a preference (likes/dislikes)
- User gives feedback on your behavior
- User reveals a fact about themselves
- You learn something context-specific worth preserving

**USE recall_memories when:**
- User asks "do you remember...?"
- User asks about their preferences
- You need context from earlier in the conversation

## Memory Types

- `preference`: User likes/dislikes, habits, preferred communication style
- `feedback`: User's feedback on your responses or behavior
- `fact`: Factual information about the user (name, location, job, etc.)
- `topic`: Subjects the user is interested in or has discussed

## Examples

**To store:**
```
User: I prefer if you don't use emojis

Agent: I'll remember that you prefer no emojis.
(remember tool with content="User prefers no emojis", memory_type="preference")
```

**To recall:**
```
User: What do you know about me?

Agent: Let me recall what I've learned about you.
(recall_memories with query="user preferences and facts")
```

## Notes

- Memory persists across conversations
- Use recall_memories periodically to stay context-aware
- Store information proactively when users share preferences
