# Recurring Research Skill

This skill provides a standardized framework for performing automated, periodic research on various topics, ensuring consistency and preventing redundant notifications.

## When to Use

**USE this skill when:**
- The user requests a recurring monitoring task (e.g., news, price tracking, project updates).
- A cronjob is scheduled to poll for information on a specific topic.
- You need to track changes over time for a particular subject.

**DON'T use this skill when:**
- A one-time search is sufficient.
- The data is static and does not require periodic updates.

## How to Use

### 1. Baseline Initialization
For every unique research topic, maintain a dedicated baseline document: `data/research_[topic]_baseline.md`.
- If the file doesn't exist, create it upon the first execution.
- The baseline acts as the "memory" of what has already been reported to the user.

### 2. Execution Loop
At every single iteration of the research task:
1. **Read Baseline:** Load the current `[topic]_baseline.md`.
2. **Gather Data:** Perform the research (web search, API calls, etc.).
3. **Compact Memory Storage:**
   - For news or high-volume data, record findings in a **highly condensed bullet list**.
   - **Maximize Surface, Minimize Tokens:** Capture as many distinct items as possible, but use extremely concise phrasing (e.g., "Iran: New sanctions on oil exports" instead of a full paragraph).
   - Prioritize remembering *too many* items over *too few* to ensure no critical update is missed.
4. **Differential Analysis:**
   - Compare the new findings against the baseline.
   - Only notify the user if there is a **significant change** or a **new item** that isn't in the baseline.
5. **Update Baseline:**
   - Append new items to the baseline.
   - Periodically prune old or obsolete entries to prevent the file from cluttering the context.
   - Save the updated baseline for the next iteration.

## Output control is automatic - don't repeat it in the cron prompt

Every cronjob run is automatically prefixed with an unattended-run notice that already covers: using `__DISCARD_OUTPUT__` when there's nothing new to report, never using `__UNMUTE__` from a cron run, and writing only a plain final message (no plans, no JSON). **Do not paste a "CRITICAL OUTPUT CONTROL" block into the prompt you pass to `schedule_cron`** - it's redundant, and it bloats every future run of the job (that text gets read on every single tick, forever). Write the prompt as just the task itself: what to check, against which baseline file, and what counts as a change worth reporting.

## Always set `summary` on schedule_cron

`schedule_cron` requires a `summary` argument: a short (~20 words), plain-language description of what the job does, with no instructions inside it (e.g. `"Checks the Stade Kreistagswahl results page for new numbers."`). `list_cronjobs` shows this to the user instead of the full prompt - a vague or missing summary makes the job's purpose invisible to both the user and to you in a later turn.

### EXAMPLE

```
schedule_cron(
  prompt="Read data/research_stade_kommunalwahl_baseline.md. Navigate to <url>. "
         "Compare the current party percentages/seats against the baseline. If a party's "
         "numbers changed from the baseline, update the baseline and report the new numbers. "
         "If nothing changed, just update the 'last checked' line in the baseline.",
  summary="Checks the Stade Kreistagswahl results page for new numbers.",
  when="+20minutes",
  recurring=true,
  interval_minutes=20,
)
```
