"""Cron job scheduler for nixclaw-agent."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol

from .config import CronConfig
from .db.models import CronJob

LOG = logging.getLogger(__name__)


def compute_next_scheduled(anchor: datetime, interval: timedelta, now: datetime) -> datetime:
    """Return the next anchor + k*interval grid slot strictly after `now`."""
    if now < anchor:
        return anchor
    periods = (now - anchor) // interval + 1
    return anchor + periods * interval


def _parse_iso_as_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string as UTC. Naive strings are assumed UTC. Returns None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class CronCompleteResult:
    """Result returned from CronScheduler.mark_complete.

    Lets the orchestrator decide whether to take terminal follow-up action
    (e.g., send a CronJobNotification and delete a one-time row).
    """

    terminal_action: Literal["none", "recurring_rescheduled", "one_time_threshold_tripped"]
    job_id: str


class DatabaseOperations(Protocol):
    """Protocol for database operations."""

    def store_cronjob(self, cronjob: CronJob, failure_count: int | None = None) -> None: ...
    def get_due_cronjobs(self, max_failure_count: int | None = None) -> list[CronJob]: ...
    def update_cronjob(
        self,
        job_id: str,
        status: str | None = None,
        last_run: str | None = None,
        scheduled_at: str | None = None,
        cancelled: bool | None = None,
        failure_count: int | None = None,
        summary: str | None = None,
    ) -> None: ...
    def get_channel_cronjobs(self, channel_id: str, status: str | None = None) -> list[CronJob]: ...
    def delete_cronjob(self, job_id: str) -> bool: ...
    def get_cronjob(self, job_id: str) -> CronJob | None: ...


class CronScheduler:
    def __init__(self, config: CronConfig, db_ops: DatabaseOperations) -> None:
        self.config = config
        self.db_ops = db_ops

    def schedule(
        self,
        when: datetime,
        prompt: str,
        channel_id: str,
        creator_number: str,
        is_recurring: bool = False,
        interval_minutes: int | None = None,
        summary: str = "",
    ) -> str:
        """Schedule a cronjob, return job_id."""
        job_id = str(uuid.uuid4())
        cronjob = CronJob(
            id=job_id,
            channel_id=channel_id,
            creator_number=creator_number,
            prompt=prompt,
            scheduled_at=when.isoformat(),
            status="pending",
            is_recurring=is_recurring,
            interval_minutes=interval_minutes,
            summary=summary,
        )
        self.db_ops.store_cronjob(cronjob)
        return job_id

    def check_due(self) -> list[CronJob]:
        """Get jobs that are ready to run. Does NOT mark them 'running'.

        Marking used to happen here for every due row, before the caller
        decided whether it could actually dispatch each one. A job skipped
        because its channel's background lane was busy (core.py's run()
        loop) was left stuck in 'running' forever, since get_due_cronjobs()
        only re-selects 'pending'/'failed' rows and mark_complete() is never
        called for a job that was never dispatched. Callers must now call
        mark_running() themselves, only for jobs they actually dispatch.
        """
        return self.db_ops.get_due_cronjobs(
            max_failure_count=self.config.max_failure_count
        )

    def mark_running(self, job_id: str) -> None:
        """Flip a due job to 'running' right before actually dispatching it.

        Call this only for a job the caller is about to process - see
        check_due()'s docstring for why marking happens here instead of
        for every due row up front.
        """
        self.db_ops.update_cronjob(job_id, status="running")

    def mark_complete(
        self,
        job_id: str,
        success: bool,
        is_recurring: bool = False,
        interval_minutes: int | None = None,
        is_cancelled: bool = False,
        scheduled_at: str | None = None,
    ) -> CronCompleteResult:
        """Mark job as complete or failed. If recurring, reschedule instead.

        Returns a CronCompleteResult so the caller (orchestrator) can react to
        terminal actions.
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # ---- Cancelled branch: terminal, reset counter, never reschedule. ----
        if is_cancelled:
            status = "complete" if success else "failed"
            self.db_ops.update_cronjob(
                job_id,
                status=status,
                last_run=now_iso,
                failure_count=0,
            )
            return CronCompleteResult("none", job_id)

        threshold = self.config.max_failure_count

        if success:
            # ---- Success path ----
            if is_recurring and interval_minutes is not None and interval_minutes > 0:
                now_dt = datetime.now(timezone.utc)
                interval = timedelta(minutes=interval_minutes)
                anchor = _parse_iso_as_utc(scheduled_at)
                if anchor is not None:
                    next_scheduled = compute_next_scheduled(anchor, interval, now_dt)
                else:
                    # Anchor unparseable: legacy fallback (now + interval).
                    next_scheduled = now_dt + interval
                self.db_ops.update_cronjob(
                    job_id,
                    status="pending",
                    last_run=now_iso,
                    scheduled_at=next_scheduled.isoformat(),
                    failure_count=0,
                )
                return CronCompleteResult("recurring_rescheduled", job_id)
            # Non-recurring success → terminal complete.
            self.db_ops.update_cronjob(
                job_id,
                status="complete",
                last_run=now_iso,
                failure_count=0,
            )
            return CronCompleteResult("none", job_id)

        # ---- Failure path ----
        # Read current state to increment FailureCount and (later) anchor reschedule.
        #
        # Single-task assumption: the dispatcher flips the row to status='running'
        # before this method is called, so concurrent invocations for the same
        # job_id are not expected.  If they did occur the broadened query
        # (pending + failed-with-headroom) would never re-pick a 'running' row
        # anyway, so a race here is structurally harmless.
        current = self.db_ops.get_cronjob(job_id)
        if current is None:
            # Row vanished; nothing to do.
            return CronCompleteResult("none", job_id)
        prev_count = current.failure_count or 0
        new_count = prev_count + 1
        self.db_ops.update_cronjob(
            job_id,
            status="failed",
            last_run=now_iso,
            failure_count=new_count,
        )

        if new_count < threshold:
            LOG.warning(
                f"Cronjob {job_id} failed ({new_count}/{threshold}); "
                f"awaiting FastRetry on next iteration."
            )
            return CronCompleteResult("none", job_id)

        # ---- Threshold-tripped ----
        if is_recurring:
            if interval_minutes is None or interval_minutes <= 0:
                # Data-corruption guard: don't loop forever; leave failed.
                LOG.warning(
                    f"Recurring cronjob {job_id} hit threshold but has no usable "
                    f"interval_minutes; leaving in failed state."
                )
                return CronCompleteResult("none", job_id)
            # Anchor reschedule to the original grid using the same helper as success.
            anchor = _parse_iso_as_utc(current.scheduled_at)
            now_dt = datetime.now(timezone.utc)
            interval = timedelta(minutes=interval_minutes)
            if anchor is not None:
                next_scheduled = compute_next_scheduled(anchor, interval, now_dt)
            else:
                next_scheduled = now_dt + interval
            self.db_ops.update_cronjob(
                job_id,
                status="pending",
                last_run=now_iso,
                scheduled_at=next_scheduled.isoformat(),
                failure_count=0,
            )
            LOG.warning(
                f"Cronjob {job_id} hit failure threshold; rescheduled for "
                f"{next_scheduled.isoformat()} on original grid."
            )
            return CronCompleteResult("recurring_rescheduled", job_id)

        # One-time at threshold → orchestrator deletes + notifies.
        LOG.warning(
            f"One-time cronjob {job_id} hit failure threshold; "
            f"caller must emit CronJobNotification and delete the row."
        )
        return CronCompleteResult("one_time_threshold_tripped", job_id)
