# NixClaw prompts

Every prompt the agent sends to the model, and every canned reply it sends to
users, lives in this directory. You can tune them without touching code.

## How it works

- `default/` is git-tracked and ships the standard text. Never edit it for
  instance-specific tuning (your change would conflict on upgrade).
- `custom/` is git-ignored (and excluded from the Docker image). Put your
  overrides here. Every file is optional; a missing `custom/` directory is fine.
- Loading rule:
  - `.md` files: one prompt per file. `custom/X.md` replaces `default/X.md`
    as a whole.
  - `.yaml` files: many short strings. `custom/X.yaml` is merged over
    `default/X.yaml` key by key. You only list the keys you want to change.
    A key that does not exist in the default is an error (catches typos).
- Activate changes without a restart: see "Reloading" below.
- Which file was loaded for each prompt is logged at DEBUG level.

## Directory layout

```
config/prompts/
  default/                    shipped text (tracked)
    system-prompt.md ...      one .md file per long prompt
    system-prompts.yaml       short model-facing strings
    user-messages.yaml        canned replies sent to humans
    tool-descriptions.yaml    tool and parameter descriptions
  custom/                     your overrides (git-ignored, optional)
    .gitkeep
```

Files in `custom/` whose names match no known prompt file are ignored (logged
at DEBUG only), so check the name if an override seems to have no effect.

## Prompt reference

Generated from the registry in `src/nixclaw_agent/prompts.py`. "Placeholders"
are the `{{names}}` the text may contain. "Required tokens" are literal strings
that must still appear in your text (the agent's control logic looks for them).
`soul` and `tool-descriptions.yaml` are described in their own sections below.

| Name | File / key | When used | Placeholders | Required tokens |
|---|---|---|---|---|
| `soul` | `soul.md` | Default persona when a channel has no SOUL.md of its own (see soul hierarchy) | - | - |
| `system-prompt` | `system-prompt.md` | Main chat system prompt, built for every LLM call | `{{agent_name}}`, `{{current_datetime}}`, `{{memory_section}}`, `{{skill_routing}}`, `{{soul_section}}`, `{{timezone}}`, `{{tools_description}}`, `{{user_phone_number}}`, `{{user_tier}}` | - |
| `mute` | `mute.md` | Appended to the system prompt while the channel is muted | - | `__MUTE__` |
| `unmute` | `unmute.md` | Appended to the system prompt when the user may be asking to unmute | `{{agent_name}}` | `__UNMUTE__` |
| `continue` | `continue.md` | Appended on internal continuation turns | - | `__AGENT_CONTINUE__` |
| `personal-info` | `personal-info.md` | Section of the system prompt on handling personal information | - | - |
| `information-limitations` | `information-limitations.md` | Section of the system prompt on what the agent cannot know | - | - |
| `cron-run-preamble` | `cron-run-preamble.md` | Prefixed to every cronjob run's prompt (unattended run) | - | `__DISCARD_OUTPUT__` |
| `memory-classifier` | `memory-classifier.md` | Asks the model whether a message is worth memorizing | - | `should_remember`, `memory_type`, `content` |
| `summarize` | `summarize.md` | Cross-turn conversation summarization | `{{conversation_text}}`, `{{max_keywords}}`, `{{prior_section}}` | - |
| `distill-keywords` | `distill-keywords.md` | Condenses accumulated summary keywords | `{{max_keywords}}`, `{{prior_keywords}}`, `{{summary}}` | - |
| `debug-mode` | `debug-mode.md` | Added to the system prompt when debug logging is on; tells the model how to report issues | - | `__ISSUE_DETECTED__` |
| `background-activity` | `background-activity.md` | Status block in the chat prompt while a cronjob/subagent runs on the channel | `{{source_id}}`, `{{source_type}}` | - |
| `single-message-replace` | `single-message-replace.md` | Section of the system prompt when the channel's message mode is `replace` (later texts replace earlier ones on screen) | `{{replaced_tag}}` | - |
| `skill-routing` | `skill-routing.md` | Skill list section of the system prompt | `{{skill_list}}` | - |
| `captcha-guidance` | `captcha-guidance.md` | Hint returned by browser tools when a captcha is detected | - | - |
| `markup-corrective` | `markup-corrective.md` | Re-prompt after the model leaked tool-call markup in text | - | - |
| `markup-reasoning-corrective` | `markup-reasoning-corrective.md` | Re-prompt after tool-call markup leaked in reasoning | - | - |
| `precommit-privacy-check` | `precommit-privacy-check.md` | Not used by the agent: the git pre-commit hook (`scripts/precommit_privacy_check.py`, config in `config/dev.yaml`) sends it to an LLM with the staged diff attached | `{{file_list}}`, `{{repo_name}}` | `VERDICT: PASS`, `VERDICT: FAIL` |
| `soul-section` | `system-prompts.yaml` : `soul_section` | Wraps the soul text in the system prompt | `{{soul_content}}` | - |
| `memory-section` | `system-prompts.yaml` : `memory_section` | Wraps recalled memories in the system prompt | `{{memory_context}}` | - |
| `skill-routing-empty` | `system-prompts.yaml` : `skill_routing_empty` | Used when no skills are available | - | - |
| `summarize-prior-block` | `system-prompts.yaml` : `summarize_prior_block` | Prior-summary block inside the summarize prompt | `{{prior_keywords}}`, `{{prior_summary}}` | - |
| `history-keywords` | `system-prompts.yaml` : `history_keywords` | Keywords line prepended to the chat history when the channel has stored summary keywords | `{{keywords}}` | - |
| `conversation-summary` | `system-prompts.yaml` : `conversation_summary` | Wrapper for the stored conversation summary, in chat history and at the start of a subagent run | `{{summary_text}}` | - |
| `progress-summary` | `system-prompts.yaml` : `progress_summary` | Wrapper for in-run progress summary (session compaction) | `{{summary_text}}` | - |
| `context-recovery` | `system-prompts.yaml` : `context_recovery` | Wraps the user's message when it implies prior context (e.g. "as before"); tells the model to use message_search first | `{{message}}` | - |
| `empty-response-nudge` | `system-prompts.yaml` : `empty_response_nudge` | Nudge sent to the model (up to 3×) when the final response after tool calls is empty | - | - |
| `tool-failure-nudge` | `system-prompts.yaml` : `tool_failure_nudge` | Same, but when the last tool call failed; the model keeps its tools so it can retry/re-plan | `{{tool_names}}`, `{{error}}` | - |
| `repeated-tool-call-hint` | `system-prompts.yaml` : `repeated_tool_call_hint` | Tool result returned (tool not run) when an identical call repeats too often in a turn | `{{count}}`, `{{tool_name}}` | - |
| `inbound-drain` | `system-prompts.yaml` : `inbound_drain` | Synthetic user turn for a message that arrived mid-turn | `{{message}}` | - |
| `context-exceeded` | `system-prompts.yaml` : `context_exceeded` | System note for the final tool-less call when the context still overflows after compaction | - | - |
| `analyze-image` | `system-prompts.yaml` : `analyze_image` | Prompt sent to the vision model by analyze_image | `{{query}}` | - |
| `selector-help` | `system-prompts.yaml` : `selector_help` | Tool-error hint text for bad browser selectors | - | - |
| `no-page-error` | `system-prompts.yaml` : `no_page_error` | Tool-error text when the browser has no open page | - | - |
| `soul-updated` | `system-prompts.yaml` : `soul_updated` | Tool result after update_soul succeeds | `{{channel_id}}` | - |
| `consent-not-run-message` | `system-prompts.yaml` : `consent_not_run_message` | Tool result when the user wrote a message instead of answering an approval poll | `{{message}}`, `{{question}}` | - |
| `consent-rejected` | `system-prompts.yaml` : `consent_rejected` | Tool result when an approval poll was answered "no" | `{{question}}`, `{{voter}}` | - |
| `consent-expired` | `system-prompts.yaml` : `consent_expired` | Tool result when nobody answered an approval poll in time | `{{question}}` | - |
| `consent-skipped` | `system-prompts.yaml` : `consent_skipped` | Tool result for the remaining calls of a step after a command was not approved | - | - |
| `consent-approved-suffix` | `system-prompts.yaml` : `consent_approved_suffix` | Appended to the result of an approved command | `{{voter}}` | - |
| `consent-background-pending` | `system-prompts.yaml` : `consent_background_pending` | Tool result in a cronjob/subagent when an approval poll was posted | - | - |
| `consent-inspection` | `system-prompts.yaml` : `consent_inspection` | Prompt of the one-time cronjob after an approved background command failed | `{{command}}`, `{{output}}` | - |
| `operation-halted` | `user-messages.yaml` : `operation_halted` | Reply to a stop/halt/cancel message | - | - |
| `context-reset` | `user-messages.yaml` : `context_reset` | Reply to a context-reset request | - | - |
| `voice-transcription-failed` | `user-messages.yaml` : `voice_transcription_failed` | Reply/placeholder when voice transcription fails | - | - |
| `llm-unavailable` | `user-messages.yaml` : `llm_unavailable` | Reply when the LLM call fails (chat turn, subagent) | - | - |
| `empty-response` | `user-messages.yaml` : `empty_response` | Reply when the model returns neither text nor a tool call | - | - |
| `too-many-messages` | `user-messages.yaml` : `too_many_messages` | Reply when the per-session cap on messages sent in a channel is reached | - | - |
| `internal-limit` | `user-messages.yaml` : `internal_limit` | Reply when the chat turn hits the maximum tool iterations | - | - |
| `no-summary-fallback` | `user-messages.yaml` : `no_summary_fallback` | Reply when the model is still empty after the empty-response nudge | - | - |
| `cron-failed` | `user-messages.yaml` : `cron_failed` | Message to the user when a cronjob keeps failing | `{{count}}`, `{{time}}` | - |
| `task-completed` | `user-messages.yaml` : `task_completed` | Default result text of a subagent run, kept if the model produces none | - | - |
| `context-limit-fallback` | `user-messages.yaml` : `context_limit_fallback` | Fallback text when that final call also fails (tool loop) | - | - |
| `consent-answer-yes` | `user-messages.yaml` : `consent_answer_yes` | Approve answer of an approval poll | - | - |
| `consent-answer-no` | `user-messages.yaml` : `consent_answer_no` | Reject answer of an approval poll | - | - |
| `consent-expired-restart` | `user-messages.yaml` : `consent_expired_restart` | Posted after a restart for each approval poll that was still open | - | - |

`tool-descriptions.yaml` is not in the table: it holds a `description` and
per-parameter descriptions for every tool (see below).

## YAML override format

Example `custom/system-prompts.yaml` changing one string:

```yaml
empty_response_nudge: "You returned nothing. Answer the user now."
```

All other keys keep their default text. Nested keys are merged the same way.
Values must be strings.

Example `custom/tool-descriptions.yaml` overriding one parameter description:

```yaml
web_search:
  params:
    query: "Search terms. Prefer English keywords."
```

Only the tool name, `description`, and `params.<name>` keys that exist in the
default file are accepted. An empty string (`""`) is allowed and means the
parameter has no description (nothing is sent to the model). Tool schemas
(types, enums, required flags) stay in code and cannot be overridden here.

## Placeholders

- Syntax: `{{name}}`, letters/digits/underscore only.
- Only the placeholders listed for that prompt in the table are valid. An
  unknown placeholder is an error. Leaving a valid placeholder out is fine.
- Rendering is single pass over your template: inserted values (soul text,
  memories, user messages) are never scanned again, so text containing `{{x}}`
  in user input is safe.
- Anything else in braces (e.g. JSON examples with single `{ }`) is literal.

## Errors

- At startup: all problems are collected and the agent refuses to start with
  one error listing them. A missing default file is always fatal.
- On reload: the new prompts and config are validated first. On any error the
  previous config and prompts stay active and the error list is reported.
- Typical errors: unknown yaml key, unknown placeholder, a required token
  removed (e.g. `__MUTE__` missing from `mute.md`), invalid yaml, a tool or
  tool parameter missing from `tool-descriptions.yaml`.

## Soul (persona) hierarchy

The persona text used for a channel is the first that exists:

1. `data/<channel>/SOUL.md` (per channel, written by the `update_soul` tool or by hand)
2. `config/prompts/custom/soul.md`
3. `config/prompts/default/soul.md`

Release note: the old `/workspace/SOUL.md` layer (and `paths.workspace_dir`) was
removed. Move any `/workspace/SOUL.md` to `config/prompts/custom/soul.md`.

A channel `SOUL.md` is plain text, not a template: `{{...}}` in it is not
substituted.

## What deliberately stays in code

- Structural tags in model input such as `[Voice message: ...]` and
  `[New message from user while you were working: ...]` style framing: code
  parses or relies on their exact shape.
- Keyword lists for stop/reset/mute detection: they are matching logic, not
  prompt text.
- Tool error messages generated by code paths (validation, permission, timeouts).
- Tool schemas (parameter types, enums, required lists).

## Notes

- Custom prompts must refer to tool names that actually exist; the agent does
  not check prose for tool names.
- The bash tool description mentions a "30s timeout". This is documentation
  only; the real timeout is enforced in code. Editing the text does not change it.
- Keep control markers (`__MUTE__`, `__UNMUTE__`, `__AGENT_CONTINUE__`,
  `__DISCARD_OUTPUT__`, `__ISSUE_DETECTED__`) intact in the prompts that require
  them (see the table).

## Reloading

- Chat: a PRIVILEGED user sends exactly `/reload` in a 1:1 chat.
- Signal: `docker kill -s HUP nixclaw`.

A reload re-reads `config/agent.yaml` and all prompts. It is all-or-nothing.
Hot-applied config: `auth` numbers, `chat.*`, `agent.name`, `consent.*`, `logging.level`,
plus prompts and tool descriptions. Other changed sections are reported as
"restart required" and not applied. A reload is refused if `agent.yaml` is
missing/empty or `auth.privileged_numbers` would be empty.
