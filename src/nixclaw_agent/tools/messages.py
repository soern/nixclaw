"""Message search tools for nixclaw-agent."""

import re
from datetime import datetime, timedelta
from typing import Optional

from rapidfuzz import fuzz

from ..types import ToolResult
from ..db.operations import get_messages


def _parse_time_range(text: str) -> Optional[tuple[int, int]]:
    """Parse approximate time string into (start_timestamp, end_timestamp).

    Returns (start_ts, end_ts) as epoch seconds, or None if parsing failed.
    Day boundaries are in the agent's local time zone (the one the system
    prompt states), not UTC.

    Supported patterns:
    - "today" -> today, full day
    - "yesterday" -> previous day, full day
    - "last week" -> 7 days ago, full day
    - "Monday" / "Monday morning" / "Monday afternoon" / "Monday evening"
    - "3 days ago" -> 3 days ago, full day
    - "March 15th" -> March 15 of current year, full day
    """
    text = text.strip().lower()
    # Naive local time: .timestamp() then interprets boundaries as local.
    now = datetime.now()

    if text == "last week":
        target = now - timedelta(days=7)
        start = target.replace(hour=0, minute=0, second=0, microsecond=0)
        end = target.replace(hour=23, minute=59, second=59, microsecond=999999)
        return (int(start.timestamp()), int(end.timestamp()))

    if text == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        return (int(start.timestamp()), int(end.timestamp()))

    if text == "yesterday":
        target = now - timedelta(days=1)
        start = target.replace(hour=0, minute=0, second=0, microsecond=0)
        end = target.replace(hour=23, minute=59, second=59, microsecond=999999)
        return (int(start.timestamp()), int(end.timestamp()))

    match = re.match(r"(\d+)\s+days?\s+ago$", text)
    if match:
        days = int(match.group(1))
        target = now - timedelta(days=days)
        start = target.replace(hour=0, minute=0, second=0, microsecond=0)
        end = target.replace(hour=23, minute=59, second=59, microsecond=999999)
        return (int(start.timestamp()), int(end.timestamp()))

    day_times = {
        "morning": (6, 12),
        "afternoon": (12, 18),
        "evening": (18, 24),
    }

    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    for day_name in day_names:
        if text == day_name or text.startswith(day_name + " "):
            parts = text.split()
            time_of_day = None
            if len(parts) > 1 and parts[1] in day_times:
                time_of_day = parts[1]
            elif len(parts) > 1 and parts[1] + " " + parts[2] in ["monday morning", "monday afternoon", "monday evening"]:
                time_of_day = parts[1]
                day_name = parts[0]

            target_weekday = day_names.index(day_name)
            weekday_diff = now.weekday() - target_weekday
            if weekday_diff == 0:
                days_ahead = 7
            else:
                days_ahead = weekday_diff % 7
            target = now - timedelta(days=days_ahead)

            if time_of_day:
                start_hour, end_hour = day_times[time_of_day]
                start = target.replace(hour=start_hour, minute=0, second=0, microsecond=0)
                end = target.replace(hour=end_hour - 1, minute=59, second=59, microsecond=999999)
            else:
                start = target.replace(hour=0, minute=0, second=0, microsecond=0)
                end = target.replace(hour=23, minute=59, second=59, microsecond=999999)
            return (int(start.timestamp()), int(end.timestamp()))

    match = re.match(r"(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?$", text)
    if match:
        month_name = match.group(1).lower()
        day_num = int(match.group(2))
        month_map = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }
        if month_name in month_map:
            month = month_map[month_name]
            year = now.year if now.month > month or (now.month == month and now.day >= day_num) else now.year - 1
            try:
                target = datetime(year, month, day_num, 0, 0, 0, 0)
                start = target
                end = target.replace(hour=23, minute=59, second=59, microsecond=999999)
                return (int(start.timestamp()), int(end.timestamp()))
            except ValueError:
                pass

    return None


def _format_ts(timestamp: int) -> str:
    """Local time with explicit UTC offset, e.g. '2026-10-01 22:51 +0200'."""
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M %z")


def _format_message(msg, show_context_marker: bool = False, is_match: bool = False) -> str:
    """Format a message for display."""
    direction = "→" if msg.is_outgoing else "←"
    ts_str = _format_ts(msg.event.timestamp)
    source = msg.event.source
    marker = ">>> [MATCH]" if is_match else "    "

    source_prefix = ""
    if msg.source_type == "cronjob" and msg.source_id:
        source_prefix = f"[Cronjob {msg.source_id[:8]}...] "
    elif msg.source_type == "subagent" and msg.source_id:
        source_prefix = f"[Subagent {msg.source_id[:8]}...] "

    return f"{marker} {direction} [{ts_str}] {source_prefix}{source}: {msg.event.message}"


def message_search(
    query: Optional[str] = None,
    approximate_time: Optional[str] = None,
    before: Optional[str] = None,
    after: Optional[str] = None,
    context_lines: int = 3,
    limit: int = 20,
    channel_id: Optional[str] = None,
) -> ToolResult:
    """Search message history with time-based and fuzzy keyword search.

    Args:
        query: Optional keyword to search (fuzzy match with 80% threshold).
        approximate_time: Natural time like 'last week', 'Monday morning', 'March 15th'.
        before: ISO datetime - search before this time.
        after: ISO datetime - search after this time.
        context_lines: Number of messages before/after to include (default: 3).
        limit: Maximum results to return (default: 20).
        channel_id: The channel ID to search (auto-injected).

    Returns:
        ToolResult with matching messages and context, or error.
    """
    try:
        if not channel_id:
            return ToolResult(
                name="message_search",
                success=False,
                output=None,
                error="No channel to search.",
            )
        if not query and not approximate_time and not before and not after:
            return ToolResult(
                name="message_search",
                success=False,
                output=None,
                error="At least one of query or time filter (approximate_time/before/after) is required",
            )

        before_ts: Optional[int] = None
        after_ts: Optional[int] = None

        if approximate_time:
            time_range = _parse_time_range(approximate_time)
            if time_range:
                after_ts, before_ts = time_range
            else:
                return ToolResult(
                    name="message_search",
                    success=False,
                    output=None,
                    error=f"Could not parse approximate_time: '{approximate_time}'. "
                          "Try formats like 'last week', 'Monday morning', 'March 15th'",
                )

        if before:
            try:
                dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
                before_ts = int(dt.timestamp())
            except ValueError:
                return ToolResult(
                    name="message_search",
                    success=False,
                    output=None,
                    error=f"Invalid before datetime: {before}. Use ISO format.",
                )

        if after:
            try:
                dt = datetime.fromisoformat(after.replace("Z", "+00:00"))
                after_ts = int(dt.timestamp())
            except ValueError:
                return ToolResult(
                    name="message_search",
                    success=False,
                    output=None,
                    error=f"Invalid after datetime: {after}. Use ISO format.",
                )

        # Summarized rows are exactly the older history this tool exists to
        # find, so they are included.
        messages = get_messages(
            channel_id=channel_id,
            limit=500,
            include_summarized=True,
            before_timestamp=before_ts,
            after_timestamp=after_ts,
        )

        if not messages:
            return ToolResult(
                name="message_search",
                success=True,
                output="No messages found in the specified time range",
                error=None,
            )

        if query:
            query_lower = query.lower()
            matching_indices = []
            for i, msg in enumerate(messages):
                if fuzz.partial_ratio(query_lower, msg.event.message.lower()) >= 80:
                    matching_indices.append(i)

            if not matching_indices:
                return ToolResult(
                    name="message_search",
                    success=True,
                    output=f"No messages found matching '{query}'",
                    error=None,
                )

            results = []
            for idx in matching_indices[:limit]:
                match_msg = messages[idx]

                result_lines = []
                result_lines.append(f"=== [{_format_ts(match_msg.event.timestamp)}] "
                                   f"{'→' if match_msg.is_outgoing else '←'} {match_msg.event.source} ===")

                start_ctx = max(0, idx - context_lines)
                for i in range(start_ctx, idx):
                    ctx_msg = messages[i]
                    result_lines.append(_format_message(ctx_msg))

                result_lines.append(_format_message(match_msg, is_match=True))

                end_ctx = min(len(messages) - 1, idx + context_lines)
                for i in range(idx + 1, end_ctx + 1):
                    ctx_msg = messages[i]
                    result_lines.append(_format_message(ctx_msg))

                results.append("\n".join(result_lines))

            output = f"Found {len(matching_indices)} matches:\n\n" + "\n---\n".join(results)

        else:
            results = []
            for i, msg in enumerate(messages[:limit]):
                result_lines = []
                result_lines.append(f"=== [{_format_ts(msg.event.timestamp)}] "
                                   f"{'→' if msg.is_outgoing else '←'} {msg.event.source} ===")

                start_ctx = max(0, i - context_lines)
                for j in range(start_ctx, i):
                    ctx_msg = messages[j]
                    result_lines.append(_format_message(ctx_msg))

                result_lines.append(_format_message(msg, is_match=True))

                end_ctx = min(len(messages) - 1, i + context_lines)
                for j in range(i + 1, end_ctx + 1):
                    ctx_msg = messages[j]
                    result_lines.append(_format_message(ctx_msg))

                results.append("\n".join(result_lines))

            output = f"Found {len(messages)} messages in time range:\n\n" + "\n---\n".join(results)

        return ToolResult(
            name="message_search",
            success=True,
            output=output,
            error=None,
        )

    except Exception as e:
        return ToolResult(
            name="message_search",
            success=False,
            output=None,
            error=str(e),
        )