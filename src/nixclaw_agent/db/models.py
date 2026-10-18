"""Database models for nixclaw-agent."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Message:
    """Represents a message in a Signal channel."""
    id: str
    channel_id: str
    sender: str
    content: str
    timestamp: str
    is_outgoing: bool
    processed: bool = False


@dataclass
class CronJob:
    """Represents a scheduled cron job."""
    id: str
    channel_id: str
    creator_number: str
    prompt: str
    scheduled_at: str
    last_run: Optional[str] = None
    status: str = "pending"
    is_recurring: bool = False
    interval_minutes: Optional[int] = None
    cancelled: bool = False
    failure_count: int = 0
    # Short (~20 words), human-readable description of what this job does,
    # written by the LLM when it calls schedule_cron. list_cronjobs shows
    # this instead of the full prompt, so the model summarizing/answering
    # about the job's own cronjobs isn't handed the raw prompt text (which
    # for research/monitoring jobs is full of its own embedded instructions
    # - see tools/cron.py list_cronjobs). Empty string for legacy rows
    # created before this field existed.
    summary: str = ""
