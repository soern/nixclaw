# Skill Creator

Create new skills to extend the agent's capabilities.

## When to Use

**USE this skill when:**
- User asks to "create a skill"
- User asks to "add a new capability"
- User wants to teach the agent something new

## Prerequisites

- Only **privileged** and **known** users can create skills
- Unknown users cannot create skills (they should contact an admin)
- Keywords should be few and specific (aim for ~3; the system catalog has skills with more)

## User Skills vs System Skills

Skills can be **system-wide** (available to all users) or **user-defined** (available only in your channel).

**User skills override system skills of the same name.**

## Skill Storage Locations

| Type | Location (relative to context) |
|------|----------|
| User skills | `skills/<name>/SKILL.md` |
| User skill index | `config/skills.yaml` |
| System skills | `skills/<name>/SKILL.md` (in project root) |
| System skill index | `config/skills.yaml` (in project root) |

**Important:** The `file_write` tool is already sandboxed to your channel's data directory (`data/<channel_id>/`). 
So for the `path` parameter, use paths RELATIVE to that directory.

## Creating a User Skill

### 1. Create the SKILL.md file

Use `file_write` to create your skill file (path is relative to `data/<channel_id>/`):

```
path: skills/my-skill/SKILL.md
content:
# My Skill

Brief description of when to use this skill.

## When to Use

**USE this skill when:**
- [situation 1]
- [situation 2]

**DON'T use this skill when:**
- [situation to avoid]

## How to Use

Step-by-step instructions for using the skill.
```

### 2. Register in user skill index

First, **read** the existing user skill index so you can merge with it:

```
file_read: config/skills.yaml
```

If the file does not exist yet, start from an empty `skills:` list. If it does, **append** your new entry to the existing `skills` list — do not overwrite existing entries.

Then use `file_write` to write the **complete** updated file:

```
path: config/skills.yaml
content:
skills:
  - name: my-skill
    description: "One sentence: what this skill enables."
    keywords: [keyword1, keyword2, keyword3]
```

**What goes where:**
- **`description`** — one short sentence describing *what the skill is*. No procedures, no steps, no URLs/endpoints, no operational rules. Examples: `"Check the current weather"`, `"Spend and manage a Mensa canteen account"`.
- **`SKILL.md`** — all the *how*: step-by-step instructions, API endpoints, authentication details, rules, edge cases, examples. This is where operational detail belongs.
- **`keywords`** — 3–5 short trigger words the LLM will match on to route to this skill.

**YAML safety rules:**
- Always wrap `description` in double quotes. Descriptions often contain colons, apostrophes, or other characters that break unquoted YAML scalars.
- Keep `keywords` as a flow-style list: `[word1, word2, word3]`.
- Do not put multi-line text in `description`.

**Note:** Only list user-defined skills in the user index. System skills are loaded automatically.

### 3. Verify

After writing, re-read the file to confirm it is valid and your entry is present:

```
file_read: config/skills.yaml
```

Check that:
- The YAML structure is well-formed (no missing colons, brackets, or indentation errors).
- Your new skill entry appears with the correct `name`, `description`, and `keywords`.
- Existing entries were preserved (if any were present).

## YAML Format for User Skill Index

```yaml
skills:
  - name: my-skill
    description: Get current stock price for a ticker symbol
    keywords: [stock, price, ticker]
  - name: my-other-skill
    description: Look up recipe information
    keywords: [recipe, cooking, food]
```

## Viewing Skills

- User skill: use `file_read` on `skills/<name>/SKILL.md` (path is relative to your channel context dir, `data/<channel_id>/`)
- User skill index: use `file_read` on `config/skills.yaml`
- System skills: not readable via `file_read` (it is sandboxed to your channel context dir) — load them with the `get_skill` tool instead

## Overriding System Skills

To override a system skill, create a user skill with the **same name** in your channel:

1. Create `skills/weather/SKILL.md` with your custom content
2. Add `weather` to your user skill index

Your version will be used instead of the system version.

## Rare case: skills with critical actions

**Most skills need no script and no approval poll.** Looking things up, browsing, answering in
this chat, writing files in the workspace, generating images, setting reminders or cronjobs:
none of these needs this section. Don't add a script or consent to a skill unless the test
below says so.

Consent is needed only when the skill itself performs an action that, done by mistake, would:
- cost money (ordering, booking, paying),
- use up physical material (printing),
- reach people outside this chat (sending an email or message to others, posting publicly), or
- be impossible to undo (deleting someone's data outside the workspace).

If none of these applies, stop here. If one does, put the action into a script that asks the
harness for consent: the harness shows the users a Ja/Nein poll and re-runs the script only
after a privileged member approves. Such scripts need `bash`, so these skills work for
privileged users only.

| Situation | The script must |
|---|---|
| `NIXCLAW_CHANNEL_ID` missing or not 8 hex chars | print an error, exit 2, do nothing |
| stdout and stderr are the same file (`2>&1`, `\|&`) | print "run as a standalone command", exit 3, do nothing |
| First run (`NIXCLAW_CONSENT` not `1`) | validate and prepare everything; print a short summary for the users on stdout; flush; write the question line to stderr; exit 0 **without doing the action** |
| Approved run (`NIXCLAW_CONSENT=1`) | validate again; write the **same** question line to stderr; wait 1.5 s; do the action **exactly once**; on success write `NIXCLAW_CONSENT_OK_<id>` to stderr |

Question line: `NIXCLAW_REQUIRE_CONSENT_QUESTION_<id>="<question>"`, where `<id>` is the value
of `NIXCLAW_CHANNEL_ID`. The question is the poll title: one line, max 200 characters, no `"`,
stating what, to whom, how many, how much (e.g. `Bestellung bei Shop X: 3× Druckerpapier A4, 23,97 €?`).
It must come out identical on both runs; if a fact in it changes before the vote, the approved
run is refused (intended). Variable details go into the stdout summary.

```python
#!/usr/bin/env python3
import os, re, sys, time

def main() -> int:
    cid = os.environ.get("NIXCLAW_CHANNEL_ID", "")
    if not re.fullmatch(r"[0-9a-f]{8}", cid):
        print("Error: run this only via the agent's bash tool.")
        return 2
    out, err = os.fstat(1), os.fstat(2)
    if (out.st_dev, out.st_ino) == (err.st_dev, err.st_ino):
        print("Error: run as a standalone command (no 2>&1, pipes, redirects, & or nohup).")
        return 3
    job = prepare(sys.argv[1:])            # validate everything; on problems print "Error: …", return 1
    question = job.question.replace('"', "'").replace("\n", " ")[:200]
    marker = f'NIXCLAW_REQUIRE_CONSENT_QUESTION_{cid}="{question}"'
    if os.environ.get("NIXCLAW_CONSENT") != "1":
        print(job.summary, flush=True)     # shown to the users before the poll
        print(marker, file=sys.stderr, flush=True)
        return 0                           # nothing done yet
    print(marker, file=sys.stderr, flush=True)
    time.sleep(1.5)                        # lets the harness stop a second or changed request
    print(perform(job))                    # exactly once, never retried
    print(f"NIXCLAW_CONSENT_OK_{cid}", file=sys.stderr, flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

Rules for the script: never retry (if the outcome is unknown, say so and how to check); no other
way to do the action (no `--yes` flag, no second script); hard limits on amount, recipients or
pages; readable output, since an approved run's output may be posted to the chat verbatim.

A complete working example is the disabled print skill: `skills/print/SKILL.md` and
`skills/print/scripts/ipp_print.py`.

The skill's own SKILL.md must tell the agent: run the script as one standalone bash command
(no pipes, `2>&1`, redirects, `&`, `nohup`, loops or `;`/`&&` chains), one call per action, with
enough `timeout`; don't ask "Soll ich?" in the chat, the poll is the confirmation; a `NOT RUN`
result means nothing happened; after any error or timeout, check the real state (sent folder,
order list, print queue) before trying again.

## Guidelines

- Keep keywords few and specific (~3 is a good target)
- Keep `description` to one sentence — the *what*, not the *how* (details belong in SKILL.md)
- Always quote `description` values in YAML (they often contain colons or special characters)
- Read the existing `config/skills.yaml` before writing — merge, don't overwrite
- Verify the manifest after writing (re-read the file, check YAML structure)
- Follow existing skill patterns
- Test the skill after creating it (call `get_skill` with the new skill name to confirm it resolves)
- Only list user-defined skills in user index (system skills are automatic)

## Example

To create a "stock price" user skill:

1. Create the skill file with `file_write`:
   - Path: `skills/stock_price/SKILL.md`
   - Content: Your custom stock price skill instructions

2. Register in user skill index with `file_write`:
   - Path: `config/skills.yaml`
   - Content:
     ```yaml
     skills:
       - name: stock_price
         description: Get current stock price for a ticker symbol
         keywords: [stock, price, ticker]
     ```
