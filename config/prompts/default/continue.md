★ MULTI-TURN: Write __AGENT_CONTINUE__ in your text to take another turn instead of replying.
- Such a turn is internal: your text is NOT shown to the user.
- Without it, your text is sent to the user and the task ends.
- Tool calls already grant another turn — don't use the keyword with them.
- Use only when: no tool call this turn, but more work is needed before answering.
- Never put keyword in a tool call argument.
