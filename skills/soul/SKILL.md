# Soul

Customize the agent's persona and behavior for this channel.

## When to Use

**USE this skill when:**
- User asks to change the agent's personality
- User asks to update the agent's character
- User wants to modify how the agent responds
- User asks "what's your soul/persona?"

**DON'T use this skill when:**
- User wants to change core tool capabilities
- User wants to add new features (use skill-creator instead)

## How to Use

Use the `read_soul` tool to see the current persona:
- No parameters needed

Use the `update_soul` tool to modify the persona:
- `new_content`: The new soul/persona content (privileged users only)

## Soul Content Format

The soul is written in markdown and defines:
- Agent's personality traits
- Communication style
- Response patterns
- Any channel-specific behavior

Example soul content:
```markdown
# Soul

You are a helpful assistant with a friendly tone.
You enjoy coding and technical discussions.
You respond concisely and directly.
```

## Notes

- Only privileged users can update the soul
- Soul changes are channel-specific
- The soul file is stored as SOUL.md in the channel's context
