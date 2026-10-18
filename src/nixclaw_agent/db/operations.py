"""Database operations for nixclaw-agent."""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from ..config import config
from ..types import Event, Message as TypesMessage, ToolCall, ToolResult
from .models import CronJob, Message as DbMessage

LOG = logging.getLogger(__name__)


_connection: Optional[sqlite3.Connection] = None


def _get_connection() -> sqlite3.Connection:
    """Get or create the database connection."""
    global _connection
    if _connection is None:
        db_path = config.database.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _connection = sqlite3.connect(str(db_path), check_same_thread=False)
        _connection.row_factory = sqlite3.Row
        _connection.execute("PRAGMA journal_mode=WAL")
    return _connection


@contextmanager
def _db_connection():
    """Context manager for database connections."""
    conn = _get_connection()
    try:
        yield conn
    finally:
        pass


def init_database() -> None:
    """Initialize database schema."""
    conn = _get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            is_outgoing INTEGER NOT NULL,
            processed INTEGER NOT NULL DEFAULT 0,
            tool_calls TEXT,
            tool_results TEXT,
            is_summarized INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (id, channel_id)
        )
    """)
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN tool_results TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN is_summarized INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("SELECT is_summarized FROM messages LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE messages ADD COLUMN is_summarized INTEGER NOT NULL DEFAULT 0")

    try:
        conn.execute("ALTER TABLE messages ADD COLUMN source_type TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN source_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN is_discarded INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        # 0 = the text was later replaced on the user's screen (single-message
        # "replace" mode, see turn_output.py); the row itself stays as-is.
        conn.execute("ALTER TABLE messages ADD COLUMN user_visible INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass

    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel_timestamp ON messages(channel_id, timestamp)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS context_summaries (
            channel_id TEXT PRIMARY KEY,
            summary_content TEXT,
            keywords TEXT,
            version INTEGER DEFAULT 1,
            created_at INTEGER,
            token_count INTEGER,
            messages_before_count INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS cronjobs (
            id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            prompt TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            last_run TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            is_recurring INTEGER NOT NULL DEFAULT 0,
            interval_minutes INTEGER
        )
    """)
    try:
        conn.execute("ALTER TABLE cronjobs ADD COLUMN is_recurring INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE cronjobs ADD COLUMN interval_minutes INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE cronjobs ADD COLUMN cancelled INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE cronjobs ADD COLUMN creator_number TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE cronjobs ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE cronjobs ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cronjobs_scheduled_at ON cronjobs(scheduled_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cronjobs_status ON cronjobs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cronjobs_status_failure_count ON cronjobs(status, failure_count)")

    # Approval polls the agent posted and has not resolved yet (consent.py);
    # only what is needed to delete them after a restart.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS consent_polls (
            channel_id TEXT NOT NULL,
            poll_ts INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (channel_id, poll_ts)
        )
    """)

    # Web interface chat history (web/bus.py is the only writer). rev is a
    # global counter bumped on every insert/update; it is the SSE event id.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rev INTEGER NOT NULL,
            channel_id TEXT NOT NULL,
            sender TEXT,
            sender_name TEXT,
            origin TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            attachments TEXT NOT NULL DEFAULT '[]',
            poll TEXT,
            signal_ts INTEGER,
            edited INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_log_rev ON chat_log(rev)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_log_channel_id ON chat_log(channel_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_log_channel_ts ON chat_log(channel_id, signal_ts)")

    # Web interface logins (web/auth.py); only the token's sha256 is stored.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS web_endpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT NOT NULL,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL,
            last_used_at INTEGER,
            expires_at INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_web_endpoints_number ON web_endpoints(number)")

    # One-shot revival: reset any stale failed rows to pending with failure_count=0.
    # Naturally idempotent — after first run no rows match status='failed'.
    try:
        conn.execute("UPDATE cronjobs SET status='pending', failure_count=0 WHERE status='failed'")
    except sqlite3.OperationalError as e:
        LOG.warning(f"cronjob revival skipped: {e}")

    # Revival: rows stuck in 'running' were orphaned by a harness crash/restart mid-execution.
    # Single-process design — nothing can legitimately be 'running' at process start, so any
    # such row is stale by construction, not just "probably stale". Reset failure_count=0
    # (not counted as a failure) since the job itself may be fine; it was just interrupted
    # by an unrelated restart.
    try:
        conn.execute("UPDATE cronjobs SET status='pending', failure_count=0 WHERE status='running'")
    except sqlite3.OperationalError as e:
        LOG.warning(f"cronjob running-state revival skipped: {e}")

    conn.commit()


def store_message(message: TypesMessage, channel_id: str, source_type: Optional[str] = None, source_id: Optional[str] = None, is_discarded: bool = False) -> str:
    """Store a message. Returns its message id (unique per channel)."""
    message_id = f"{message.event.source}:{message.event.timestamp}"
    tool_calls_json = None
    if message.tool_calls:
        tool_calls_json = json.dumps([
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in message.tool_calls
        ])
    tool_results_json = None
    if message.tool_results:
        tool_results_json = json.dumps([
            {"name": tr.name, "success": tr.success, "output": tr.output, "error": tr.error}
            for tr in message.tool_results
        ])
    with _db_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO messages 
            (id, channel_id, sender, content, timestamp, is_outgoing, processed, tool_calls, tool_results, source_type, source_id, is_discarded)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                channel_id,
                message.event.source,
                message.event.message,
                int(message.event.timestamp),
                1 if message.is_outgoing else 0,
                1 if message.processed else 0,
                tool_calls_json,
                tool_results_json,
                source_type,
                source_id,
                1 if is_discarded else 0,
            ),
        )
        conn.commit()
    return message_id


def set_message_user_visible(message_id: str, channel_id: str, visible: bool) -> None:
    """Set the user_visible flag of one stored message."""
    with _db_connection() as conn:
        conn.execute(
            "UPDATE messages SET user_visible = ? WHERE id = ? AND channel_id = ?",
            (1 if visible else 0, message_id, channel_id),
        )
        conn.commit()


def get_messages(
    channel_id: str,
    limit: int = 10,
    include_summarized: bool = False,
    before_timestamp: Optional[int] = None,
    after_timestamp: Optional[int] = None,
    exclude_discarded: bool = True,
) -> list[TypesMessage]:
    """Get recent messages for a channel with optional time filtering."""
    with _db_connection() as conn:
        base_query = """
            SELECT id, channel_id, sender, content, timestamp, is_outgoing, processed, tool_calls, tool_results, source_type, source_id, is_discarded, user_visible
            FROM messages
            WHERE channel_id = ?
        """
        params: list = [channel_id]

        if not include_summarized:
            base_query += " AND is_summarized = 0"
        
        if exclude_discarded:
            base_query += " AND is_discarded = 0"
        
        if before_timestamp is not None:
            base_query += " AND timestamp <= ?"
            params.append(before_timestamp)
        
        if after_timestamp is not None:
            base_query += " AND timestamp >= ?"
            params.append(after_timestamp)

        base_query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(base_query, params).fetchall()
        result = []
        for row in rows:
            tool_calls = None
            if row["tool_calls"]:
                try:
                    tc_data = json.loads(row["tool_calls"])
                    tool_calls = [
                        ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                        for tc in tc_data
                    ]
                except (json.JSONDecodeError, KeyError):
                    tool_calls = None
            tool_results = None
            if row["tool_results"]:
                try:
                    tr_data = json.loads(row["tool_results"])
                    tool_results = [
                        ToolResult(name=tr["name"], success=tr["success"], output=tr["output"], error=tr["error"])
                        for tr in tr_data
                    ]
                except (json.JSONDecodeError, KeyError):
                    tool_results = None
            result.append(TypesMessage(
                event=Event(
                    source=row["sender"],
                    destination=row["channel_id"],
                    message=row["content"],
                    timestamp=int(row["timestamp"]),
                    group_id=None,
                    attachments=[],
                ),
                is_outgoing=bool(row["is_outgoing"]),
                processed=bool(row["processed"]),
                tool_calls=tool_calls,
                tool_results=tool_results,
                source_type=row["source_type"],
                source_id=row["source_id"],
                is_discarded=bool(row["is_discarded"]),
                user_visible=bool(row["user_visible"]),
            ))
        return result[::-1]


def get_discarded_messages(
    channel_id: str,
    source_type: str,
    seconds_ago: int = 3600,
    limit: int = 10,
) -> list[TypesMessage]:
    """Get discarded messages for a channel within a time window."""
    import time
    cutoff_timestamp = int(time.time()) - seconds_ago
    with _db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, channel_id, sender, content, timestamp, is_outgoing, processed, tool_calls, tool_results, source_type, source_id, is_discarded
            FROM messages
            WHERE channel_id = ? AND source_type = ? AND is_discarded = 1 AND timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (channel_id, source_type, cutoff_timestamp, limit),
        ).fetchall()
        result = []
        for row in rows:
            tool_calls = None
            if row["tool_calls"]:
                try:
                    tc_data = json.loads(row["tool_calls"])
                    tool_calls = [
                        ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                        for tc in tc_data
                    ]
                except (json.JSONDecodeError, KeyError):
                    tool_calls = None
            tool_results = None
            if row["tool_results"]:
                try:
                    tr_data = json.loads(row["tool_results"])
                    tool_results = [
                        ToolResult(name=tr["name"], success=tr["success"], output=tr["output"], error=tr["error"])
                        for tr in tr_data
                    ]
                except (json.JSONDecodeError, KeyError):
                    tool_results = None
            result.append(TypesMessage(
                event=Event(
                    source=row["sender"],
                    destination=row["channel_id"],
                    message=row["content"],
                    timestamp=int(row["timestamp"]),
                    group_id=None,
                    attachments=[],
                ),
                is_outgoing=bool(row["is_outgoing"]),
                processed=bool(row["processed"]),
                tool_calls=tool_calls,
                tool_results=tool_results,
                source_type=row["source_type"],
                source_id=row["source_id"],
                is_discarded=bool(row["is_discarded"]),
            ))
        return result[::-1]


def is_message_processed(message_id: str, channel_id: str) -> bool:
    """Check if a message has been processed (deduplication check)."""
    with _db_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE id = ? AND channel_id = ? AND processed = 1",
            (message_id, channel_id),
        ).fetchone()
        return row is not None


def mark_message_processed(message_id: str, channel_id: str) -> None:
    """Mark a message as processed."""
    with _db_connection() as conn:
        conn.execute(
            "UPDATE messages SET processed = 1 WHERE id = ? AND channel_id = ?",
            (message_id, channel_id),
        )
        conn.commit()


def clear_messages(channel_id: str) -> None:
    """Delete all messages for a channel."""
    with _db_connection() as conn:
        conn.execute("DELETE FROM messages WHERE channel_id = ?", (channel_id,))
        conn.commit()


def store_cronjob(cronjob: CronJob, failure_count: int | None = None) -> None:
    """Create a scheduled cron job."""
    _failure_count = failure_count if failure_count is not None else cronjob.failure_count
    with _db_connection() as conn:
        conn.execute(
            """
            INSERT INTO cronjobs (id, channel_id, creator_number, prompt, scheduled_at, last_run, status, is_recurring, interval_minutes, cancelled, failure_count, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cronjob.id,
                cronjob.channel_id,
                cronjob.creator_number,
                cronjob.prompt,
                cronjob.scheduled_at,
                cronjob.last_run,
                cronjob.status,
                1 if cronjob.is_recurring else 0,
                cronjob.interval_minutes,
                1 if cronjob.cancelled else 0,
                _failure_count,
                cronjob.summary,
            ),
        )
        conn.commit()


def get_due_cronjobs(max_failure_count: int | None = None) -> list[CronJob]:
    """Get jobs that are ready to run.

    Includes pending-due rows and, when max_failure_count is not None,
    failed rows whose failure_count is below the threshold (FastRetry).
    """
    now = datetime.now(timezone.utc).isoformat()
    with _db_connection() as conn:
        if max_failure_count is not None:
            rows = conn.execute(
                """
                SELECT id, channel_id, creator_number, prompt, scheduled_at, last_run, status, is_recurring, interval_minutes, cancelled, failure_count, summary
                FROM cronjobs
                WHERE cancelled = 0
                  AND (
                       (status = 'pending' AND scheduled_at <= ?)
                    OR (? IS NOT NULL AND status = 'failed' AND failure_count < ?)
                  )
                ORDER BY (status = 'pending') DESC, scheduled_at
                """,
                (now, max_failure_count, max_failure_count),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, channel_id, creator_number, prompt, scheduled_at, last_run, status, is_recurring, interval_minutes, cancelled, failure_count, summary
                FROM cronjobs
                WHERE status = 'pending' AND scheduled_at <= ? AND cancelled = 0
                ORDER BY scheduled_at
                """,
                (now,),
            ).fetchall()
        return [
            CronJob(
                id=row["id"],
                channel_id=row["channel_id"],
                creator_number=row["creator_number"],
                prompt=row["prompt"],
                scheduled_at=row["scheduled_at"],
                last_run=row["last_run"],
                status=row["status"],
                is_recurring=bool(row["is_recurring"]),
                interval_minutes=row["interval_minutes"],
                cancelled=bool(row["cancelled"]),
                failure_count=row["failure_count"],
                summary=row["summary"] or "",
            )
            for row in rows
        ]


def update_cronjob(job_id: str, status: Optional[str] = None, last_run: Optional[str] = None, scheduled_at: Optional[str] = None, cancelled: Optional[bool] = None, failure_count: Optional[int] = None, summary: Optional[str] = None) -> None:
    """Update cron job status and/or last_run time and/or scheduled_at and/or summary."""
    updates = []
    values: list[str | int] = []
    if status is not None:
        updates.append("status = ?")
        values.append(status)
    if last_run is not None:
        updates.append("last_run = ?")
        values.append(last_run)
    if scheduled_at is not None:
        updates.append("scheduled_at = ?")
        values.append(scheduled_at)
    if cancelled is not None:
        updates.append("cancelled = ?")
        values.append(1 if cancelled else 0)
    if failure_count is not None:
        updates.append("failure_count = ?")
        values.append(failure_count)
    if summary is not None:
        updates.append("summary = ?")
        values.append(summary)
    if not updates:
        return
    values.append(job_id)
    with _db_connection() as conn:
        conn.execute(
            f"UPDATE cronjobs SET {', '.join(updates)} WHERE id = ?",
            values,
        )
        conn.commit()


def get_channel_cronjobs(channel_id: str, status: Optional[str] = None) -> list[CronJob]:
    """Get all cron jobs for a channel, optionally filtered by status."""
    with _db_connection() as conn:
        if status:
            rows = conn.execute(
                """
                SELECT id, channel_id, creator_number, prompt, scheduled_at, last_run, status, is_recurring, interval_minutes, cancelled, failure_count, summary
                FROM cronjobs
                WHERE channel_id = ? AND status = ?
                ORDER BY scheduled_at DESC
                """,
                (channel_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, channel_id, creator_number, prompt, scheduled_at, last_run, status, is_recurring, interval_minutes, cancelled, failure_count, summary
                FROM cronjobs
                WHERE channel_id = ?
                ORDER BY scheduled_at DESC
                """,
                (channel_id,),
            ).fetchall()
        return [
            CronJob(
                id=row["id"],
                channel_id=row["channel_id"],
                creator_number=row["creator_number"],
                prompt=row["prompt"],
                scheduled_at=row["scheduled_at"],
                last_run=row["last_run"],
                status=row["status"],
                is_recurring=bool(row["is_recurring"]),
                interval_minutes=row["interval_minutes"],
                cancelled=bool(row["cancelled"]),
                failure_count=row["failure_count"],
                summary=row["summary"] or "",
            )
            for row in rows
        ]


def get_cronjob(job_id: str) -> CronJob | None:
    """Get a single cronjob by id, or None if not found."""
    with _db_connection() as conn:
        row = conn.execute(
            """
            SELECT id, channel_id, creator_number, prompt, scheduled_at, last_run, status, is_recurring, interval_minutes, cancelled, failure_count, summary
            FROM cronjobs
            WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return CronJob(
            id=row["id"],
            channel_id=row["channel_id"],
            creator_number=row["creator_number"],
            prompt=row["prompt"],
            scheduled_at=row["scheduled_at"],
            last_run=row["last_run"],
            status=row["status"],
            is_recurring=bool(row["is_recurring"]),
            interval_minutes=row["interval_minutes"],
            cancelled=bool(row["cancelled"]),
            failure_count=row["failure_count"],
            summary=row["summary"] or "",
        )


def delete_cronjob(job_id: str) -> bool:
    """Delete a cron job. Returns True if deleted, False if not found."""
    with _db_connection() as conn:
        cursor = conn.execute("DELETE FROM cronjobs WHERE id = ?", (job_id,))
        conn.commit()
        return cursor.rowcount > 0


def add_consent_poll(channel_id: str, poll_ts: int) -> None:
    """Record an open approval poll."""
    with _db_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO consent_polls (channel_id, poll_ts, created_at) VALUES (?, ?, ?)",
            (channel_id, int(poll_ts), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def delete_consent_poll(channel_id: str, poll_ts: int) -> None:
    """Forget an approval poll (resolved, expired or deleted)."""
    with _db_connection() as conn:
        conn.execute(
            "DELETE FROM consent_polls WHERE channel_id = ? AND poll_ts = ?",
            (channel_id, int(poll_ts)),
        )
        conn.commit()


def get_consent_polls() -> list[tuple[str, int]]:
    """All recorded approval polls as (channel_id, poll_ts), oldest first."""
    with _db_connection() as conn:
        rows = conn.execute(
            "SELECT channel_id, poll_ts FROM consent_polls ORDER BY created_at"
        ).fetchall()
        return [(row["channel_id"], row["poll_ts"]) for row in rows]


def get_total_message_count(channel_id: str) -> int:
    """Get total count of non-summarized messages for a channel."""
    with _db_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE channel_id = ? AND is_summarized = 0",
            (channel_id,),
        ).fetchone()
        return row["cnt"] if row else 0


def mark_messages_summarized(channel_id: str, before_timestamp: int) -> None:
    """Mark all messages in channel before timestamp as summarized."""
    with _db_connection() as conn:
        conn.execute(
            "UPDATE messages SET is_summarized = 1 WHERE channel_id = ? AND timestamp <= ?",
            (channel_id, before_timestamp),
        )
        conn.commit()


def upsert_summary(channel_id: str, summary_content: str, keywords: list[str],
                    version: int, token_count: int, messages_before_count: int) -> None:
    """Insert or replace context summary for a channel."""
    import time
    with _db_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO context_summaries 
            (channel_id, summary_content, keywords, version, created_at, token_count, messages_before_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (channel_id, summary_content, json.dumps(keywords), version, int(time.time()), token_count, messages_before_count),
        )
        conn.commit()


def get_summary(channel_id: str) -> tuple[str, list[str]] | None:
    """Get context summary for a channel. Returns (summary_content, keywords) or None."""
    with _db_connection() as conn:
        row = conn.execute(
            "SELECT summary_content, keywords, version FROM context_summaries WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        if row and row["summary_content"]:
            keywords = []
            if row["keywords"]:
                try:
                    keywords = json.loads(row["keywords"])
                except json.JSONDecodeError:
                    pass
            return row["summary_content"], keywords
        return None


# --- chat_log (web interface history, written only by web/bus.py) ----------

CHAT_LOG_COLUMNS = ("channel_id", "sender", "sender_name", "origin", "text", "attachments",
                    "poll", "signal_ts", "edited", "deleted", "created_at")


def _next_chat_rev(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(rev), 0) + 1 AS r FROM chat_log").fetchone()
    return int(row["r"])


def chat_log_insert(values: dict) -> sqlite3.Row:
    """Insert one chat_log row (keys from CHAT_LOG_COLUMNS) with a fresh rev."""
    cols = [c for c in CHAT_LOG_COLUMNS if c in values]
    with _db_connection() as conn:
        rev = _next_chat_rev(conn)
        cur = conn.execute(
            f"INSERT INTO chat_log (rev, {', '.join(cols)}) VALUES (?, {', '.join('?' for _ in cols)})",
            (rev, *(values[c] for c in cols)),
        )
        conn.commit()
        return conn.execute("SELECT * FROM chat_log WHERE id = ?", (cur.lastrowid,)).fetchone()


def chat_log_update(row_id: int, values: dict) -> Optional[sqlite3.Row]:
    """Update columns of one chat_log row and bump its rev. None if missing."""
    cols = [c for c in CHAT_LOG_COLUMNS if c in values]
    with _db_connection() as conn:
        rev = _next_chat_rev(conn)
        assignments = ", ".join(f"{c} = ?" for c in ["rev", *cols])
        conn.execute(
            f"UPDATE chat_log SET {assignments} WHERE id = ?",
            (rev, *(values[c] for c in cols), row_id),
        )
        conn.commit()
        return conn.execute("SELECT * FROM chat_log WHERE id = ?", (row_id,)).fetchone()


def chat_log_get(row_id: int) -> Optional[sqlite3.Row]:
    with _db_connection() as conn:
        return conn.execute("SELECT * FROM chat_log WHERE id = ?", (row_id,)).fetchone()


def chat_log_find_by_ts(channel_id: str, signal_ts: int) -> Optional[sqlite3.Row]:
    """Latest row of a channel carrying this Signal timestamp."""
    with _db_connection() as conn:
        return conn.execute(
            "SELECT * FROM chat_log WHERE channel_id = ? AND signal_ts = ? ORDER BY id DESC LIMIT 1",
            (channel_id, int(signal_ts)),
        ).fetchone()


def chat_log_history(channel_id: str, before_id: Optional[int], limit: int) -> list[sqlite3.Row]:
    """Up to `limit` rows of a channel with id < before_id, ascending by id."""
    with _db_connection() as conn:
        if before_id is None:
            rows = conn.execute(
                "SELECT * FROM chat_log WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
                (channel_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM chat_log WHERE channel_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
                (channel_id, before_id, limit),
            ).fetchall()
        return list(reversed(rows))


def chat_log_max_rev() -> int:
    """Current global chat_log rev (0 if empty)."""
    with _db_connection() as conn:
        row = conn.execute("SELECT COALESCE(MAX(rev), 0) AS r FROM chat_log").fetchone()
        return int(row["r"])


def chat_log_since(rev: int, channel_ids: list[str], limit: int = 500) -> list[sqlite3.Row]:
    """Rows of the given channels changed after `rev`, ascending by rev, at
    most `limit` (page on with the last row's rev)."""
    if not channel_ids:
        return []
    with _db_connection() as conn:
        return conn.execute(
            f"SELECT * FROM chat_log WHERE rev > ? AND channel_id IN ({', '.join('?' for _ in channel_ids)}) "
            "ORDER BY rev LIMIT ?",
            (rev, *channel_ids, limit),
        ).fetchall()


# --- web_endpoints (web interface logins, web/auth.py) ----------------------

def web_endpoint_create(number: str, name: str, token_hash: str, created_at: int,
                        expires_at: Optional[int]) -> int:
    with _db_connection() as conn:
        cur = conn.execute(
            "INSERT INTO web_endpoints (number, name, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (number, name, token_hash, created_at, expires_at),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def web_endpoint_by_hash(token_hash: str) -> Optional[sqlite3.Row]:
    with _db_connection() as conn:
        return conn.execute("SELECT * FROM web_endpoints WHERE token_hash = ?", (token_hash,)).fetchone()


def web_endpoints_for(number: str) -> list[sqlite3.Row]:
    """All endpoints of a number, oldest first."""
    with _db_connection() as conn:
        return conn.execute(
            "SELECT * FROM web_endpoints WHERE number = ? ORDER BY id", (number,)
        ).fetchall()


def web_endpoint_touch(endpoint_id: int, when: int) -> None:
    with _db_connection() as conn:
        conn.execute("UPDATE web_endpoints SET last_used_at = ? WHERE id = ?", (when, endpoint_id))
        conn.commit()


def web_endpoint_delete(endpoint_id: int) -> None:
    with _db_connection() as conn:
        conn.execute("DELETE FROM web_endpoints WHERE id = ?", (endpoint_id,))
        conn.commit()


class DatabaseOperations:
    """Wrapper class for database operations returning proper types."""

    def get_messages(self, channel_id: str, limit: int = 10, include_summarized: bool = False, before_timestamp: Optional[int] = None, after_timestamp: Optional[int] = None, exclude_discarded: bool = True) -> list[TypesMessage]:
        """Get recent messages for a channel."""
        return get_messages(channel_id, limit, include_summarized, before_timestamp=before_timestamp, after_timestamp=after_timestamp, exclude_discarded=exclude_discarded)

    def store_message(self, message: TypesMessage, channel_id: str, source_type: Optional[str] = None, source_id: Optional[str] = None, is_discarded: bool = False) -> str:
        """Store a message. Returns its message id."""
        return store_message(message, channel_id, source_type, source_id, is_discarded)

    def set_message_user_visible(self, message_id: str, channel_id: str, visible: bool) -> None:
        """Set the user_visible flag of one stored message."""
        set_message_user_visible(message_id, channel_id, visible)

    def is_message_processed(self, message_id: str, channel_id: str) -> bool:
        """Check if a message has been processed."""
        return is_message_processed(message_id, channel_id)

    def mark_message_processed(self, message_id: str, channel_id: str) -> None:
        """Mark a message as processed."""
        mark_message_processed(message_id, channel_id)

    def clear_messages(self, channel_id: str) -> None:
        """Delete all messages for a channel."""
        clear_messages(channel_id)

    def store_cronjob(self, cronjob: CronJob, failure_count: int | None = None) -> None:
        """Store a cronjob."""
        store_cronjob(cronjob, failure_count)

    def get_due_cronjobs(self, max_failure_count: int | None = None) -> list[CronJob]:
        """Get due cronjobs."""
        return get_due_cronjobs(max_failure_count)

    def get_channel_cronjobs(self, channel_id: str, status: str | None = None) -> list[CronJob]:
        """Get all cronjobs for a channel."""
        return get_channel_cronjobs(channel_id, status)

    def get_cronjob(self, job_id: str) -> CronJob | None:
        """Get a single cronjob by id."""
        return get_cronjob(job_id)

    def delete_cronjob(self, job_id: str) -> bool:
        """Delete a cronjob."""
        return delete_cronjob(job_id)

    def update_cronjob(self, job_id: str, status: str | None = None, last_run: str | None = None, scheduled_at: str | None = None, cancelled: bool | None = None, failure_count: int | None = None, summary: str | None = None) -> None:
        """Update cronjob status."""
        update_cronjob(job_id, status, last_run, scheduled_at, cancelled, failure_count, summary)

    def add_consent_poll(self, channel_id: str, poll_ts: int) -> None:
        """Record an open approval poll."""
        add_consent_poll(channel_id, poll_ts)

    def delete_consent_poll(self, channel_id: str, poll_ts: int) -> None:
        """Forget an approval poll."""
        delete_consent_poll(channel_id, poll_ts)

    def get_consent_polls(self) -> list[tuple[str, int]]:
        """All recorded approval polls as (channel_id, poll_ts)."""
        return get_consent_polls()

    def get_total_message_count(self, channel_id: str) -> int:
        """Get total count of non-summarized messages for a channel."""
        return get_total_message_count(channel_id)

    def mark_messages_summarized(self, channel_id: str, before_timestamp: int) -> None:
        """Mark all messages in channel before timestamp as summarized."""
        mark_messages_summarized(channel_id, before_timestamp)

    def upsert_summary(self, channel_id: str, summary_content: str, keywords: list[str],
                      version: int, token_count: int, messages_before_count: int) -> None:
        """Insert or replace context summary for a channel."""
        upsert_summary(channel_id, summary_content, keywords, version, token_count, messages_before_count)

    def get_summary(self, channel_id: str) -> tuple[str, list[str]] | None:
        """Get context summary for a channel."""
        return get_summary(channel_id)

    def get_discarded_messages(self, channel_id: str, source_type: str, seconds_ago: int = 3600, limit: int = 10) -> list[TypesMessage]:
        """Get discarded messages for debugging purposes."""
        return get_discarded_messages(channel_id, source_type, seconds_ago, limit)
