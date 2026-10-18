"""Unit tests for cronjob three-strike resilience (failure revival + threshold semantics).

Provides its own FakeDbOps (pattern inspired by the now-merged test_cron_reschedule.py)
with an extended variant that tracks seeded rows for side-effect verification.

TestInitDatabaseRunningRevival below is the exception: it exercises the real
sqlite-backed init_database() (fix/stuck-cronjob-running-state) rather than
FakeDbOps, since FakeDbOps never calls init_database() and so can't observe
that revival block. Its fixture follows the same temp-DB pattern as
tests/test_channel_turn_serialization.py.

Also includes TestComputeNextScheduled (pure-function grid-alignment tests) and
TestMarkCompleteAnchorEdgeCases (anchor-parsing edge cases), merged in from
test_cron_reschedule.py - that file's other mark_complete tests were dropped as
redundant with the more rigorous equivalents in TestMarkComplete below (e.g. its
recurring-success-reschedule case didn't freeze time or assert an exact next
slot, where Case 6 here does both).
"""

import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "src")

import pytest

from nixclaw_agent.cron import CronScheduler, CronCompleteResult, compute_next_scheduled, _parse_iso_as_utc
from nixclaw_agent.config import CronConfig
from nixclaw_agent.config import config as config_module
from nixclaw_agent.db import operations as db_ops
from nixclaw_agent.db.models import CronJob


# ---------------------------------------------------------------------------
# FakeDbOps — extended from test_cron_reschedule.py
# ---------------------------------------------------------------------------


class FakeDbOps:
    """Fake database operations for testing three-strike resilience."""

    def __init__(self):
        self.update_calls: list[dict] = []
        self.delete_calls: list[str] = []
        self.seed_count = 0
        # Subclass sets this to pre-load rows
        self.seeded_cronjobs: dict[str, CronJob] = {}

    def store_cronjob(self, cronjob: CronJob, failure_count: int | None = None) -> None:
        pass

    def get_due_cronjobs(self, max_failure_count: int | None = None) -> list[CronJob]:
        """Return seeded jobs that match the broadened filter.

        pending + scheduled_at <= now
        failed + failure_count < max_failure_count (FastRetry)
        """
        now = datetime.now(timezone.utc)
        result = []
        for job in self.seeded_cronjobs.values():
            if job.cancelled:
                continue
            if job.status == "pending" and _parse_iso_as_utc(job.scheduled_at) <= now:
                result.append(job)
            elif (
                max_failure_count is not None
                and job.status == "failed"
                and job.failure_count < max_failure_count
            ):
                result.append(job)
        # Simulate the DB ORDER BY: pending rows first, then failed, all sorted by scheduled_at
        def sort_key(j: CronJob) -> tuple:
            return (0 if j.status == "pending" else 1, j.scheduled_at)

        return sorted(result, key=sort_key)

    def update_cronjob(
        self,
        job_id: str,
        status: str | None = None,
        last_run: str | None = None,
        scheduled_at: str | None = None,
        cancelled: bool | None = None,
        failure_count: int | None = None,
    ) -> None:
        self.update_calls.append(
            {
                "job_id": job_id,
                "status": status,
                "last_run": last_run,
                "scheduled_at": scheduled_at,
                "cancelled": cancelled,
                "failure_count": failure_count,
            }
        )
        # Mirror the actual side-effect: update the seeded row so get_cronjob reads fresh state.
        if job_id in self.seeded_cronjobs:
            j = self.seeded_cronjobs[job_id]
            if status is not None:
                j.status = status
            if last_run is not None:
                j.last_run = last_run
            if scheduled_at is not None:
                j.scheduled_at = scheduled_at
            if failure_count is not None:
                j.failure_count = failure_count

    def get_channel_cronjobs(self, channel_id: str, status: str | None = None) -> list[CronJob]:
        return [j for j in self.seeded_cronjobs.values() if j.channel_id == channel_id and (status is None or j.status == status)]

    def delete_cronjob(self, job_id: str) -> bool:
        self.delete_calls.append(job_id)
        return True

    def get_cronjob(self, job_id: str) -> CronJob | None:
        return self.seeded_cronjobs.get(job_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHANNEL = "+0000000001"
CREATOR = "+0000000001"


def make_recurring(
    job_id: str,
    scheduled_at: datetime,
    interval_minutes: int = 60,
    failure_count: int = 0,
    status: str = "pending",
) -> CronJob:
    return CronJob(
        id=job_id,
        channel_id=CHANNEL,
        creator_number=CREATOR,
        prompt="test prompt",
        scheduled_at=scheduled_at.isoformat(),
        status=status,
        is_recurring=True,
        interval_minutes=interval_minutes,
        cancelled=False,
        failure_count=failure_count,
    )


def make_one_time(
    job_id: str,
    scheduled_at: datetime,
    failure_count: int = 0,
    status: str = "pending",
) -> CronJob:
    return CronJob(
        id=job_id,
        channel_id=CHANNEL,
        creator_number=CREATOR,
        prompt="test prompt",
        scheduled_at=scheduled_at.isoformat(),
        status=status,
        is_recurring=False,
        interval_minutes=None,
        cancelled=False,
        failure_count=failure_count,
    )


def config_with_threshold(t: int = 3) -> CronConfig:
    return CronConfig(max_failure_count=t)


# ---------------------------------------------------------------------------
# compute_next_scheduled — pure-function grid-alignment tests
# (merged in from test_cron_reschedule.py)
# ---------------------------------------------------------------------------


class TestComputeNextScheduled:
    """Tests for the pure compute_next_scheduled function."""

    def test_completion_after_anchor_stays_on_grid(self):
        """Completing 5 minutes late on a daily job stays on the original grid."""
        anchor = datetime(2025, 1, 1, 6, 30, 0, tzinfo=timezone.utc)
        interval = timedelta(days=1)
        now = datetime(2025, 1, 1, 6, 35, 0, tzinfo=timezone.utc)  # 5 minutes late

        result = compute_next_scheduled(anchor, interval, now)

        assert result == datetime(2025, 1, 2, 6, 30, 0, tzinfo=timezone.utc)

    def test_completion_exactly_on_grid_advances_one_slot(self):
        """Completing exactly at a grid point advances to the strictly next slot."""
        anchor = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        interval = timedelta(hours=1)
        now = datetime(2025, 1, 1, 13, 0, 0, tzinfo=timezone.utc)  # Exactly anchor + 1h

        result = compute_next_scheduled(anchor, interval, now)

        assert result == datetime(2025, 1, 1, 14, 0, 0, tzinfo=timezone.utc)

    def test_downtime_single_next_slot(self):
        """After 3+ days downtime on a daily job, returns exactly one next-future slot."""
        anchor = datetime(2025, 1, 1, 6, 30, 0, tzinfo=timezone.utc)
        interval = timedelta(days=1)
        now = datetime(2025, 1, 4, 6, 37, 0, tzinfo=timezone.utc)  # 3 days + 7 minutes

        result = compute_next_scheduled(anchor, interval, now)

        assert result == datetime(2025, 1, 5, 6, 30, 0, tzinfo=timezone.utc)

    def test_anchor_in_future_returns_unchanged(self):
        """If the anchor is still in the future, return it unchanged."""
        anchor = datetime(2025, 2, 1, 6, 30, 0, tzinfo=timezone.utc)
        interval = timedelta(days=1)
        now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

        result = compute_next_scheduled(anchor, interval, now)

        assert result == anchor

    def test_multi_year_gap_constant_time(self):
        """A multi-year gap completes in O(1) and returns correct next slot."""
        anchor = datetime(2020, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        interval = timedelta(days=1)
        now = datetime(2025, 12, 31, 15, 0, 0, tzinfo=timezone.utc)

        result = compute_next_scheduled(anchor, interval, now)

        # Should be exactly 1 day after anchor, computed via modular arithmetic
        days_elapsed = (now - anchor).days
        expected_periods = days_elapsed + 1
        expected = anchor + expected_periods * interval
        assert result == expected
        # Verify it's the next future slot (not in the past)
        assert result > now

    def test_boundary_exact_multiple_of_interval(self):
        """Boundary: now lands exactly on anchor + k*interval → must advance one full interval."""
        anchor = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        interval = timedelta(hours=6)
        now = datetime(2025, 1, 2, 6, 0, 0, tzinfo=timezone.utc)  # anchor + 30 hours = anchor + 5*interval

        result = compute_next_scheduled(anchor, interval, now)

        # Must be strictly after now, so next slot is anchor + 6*interval
        assert result == datetime(2025, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
        assert result > now


# ---------------------------------------------------------------------------
# Cases 1-5: check_due / get_due_cronjobs
# ---------------------------------------------------------------------------


class TestCheckDue:
    """Cases 1-5: which rows get picked up."""

    def test_pending_past_is_picked_up(self):
        """Case 1: pending + scheduled_at past → picked up."""
        db = FakeDbOps()
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.seeded_cronjobs["job-1"] = make_recurring("job-1", past)
        scheduler = CronScheduler(config_with_threshold(), db)

        due = scheduler.check_due()

        assert len(due) == 1
        assert due[0].id == "job-1"

    def test_failed_under_threshold_is_picked_up(self):
        """Case 2: failed + failure_count < threshold → picked up (FastRetry)."""
        db = FakeDbOps()
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.seeded_cronjobs["job-2"] = make_recurring("job-2", past, failure_count=2)
        db.seeded_cronjobs["job-2"].status = "failed"
        scheduler = CronScheduler(config_with_threshold(t=3), db)

        due = scheduler.check_due()

        assert len(due) == 1
        assert due[0].id == "job-2"

    def test_failed_at_threshold_not_picked_up(self):
        """Case 3: failed + failure_count == threshold → NOT picked up."""
        db = FakeDbOps()
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.seeded_cronjobs["job-3"] = make_recurring("job-3", past, failure_count=3)
        db.seeded_cronjobs["job-3"].status = "failed"
        scheduler = CronScheduler(config_with_threshold(t=3), db)

        due = scheduler.check_due()

        assert len(due) == 0

    def test_pending_future_not_picked_up(self):
        """Case 4: pending + scheduled_at in future → NOT picked up."""
        db = FakeDbOps()
        future = datetime.now(timezone.utc) + timedelta(minutes=30)
        db.seeded_cronjobs["job-4"] = make_recurring("job-4", future)
        scheduler = CronScheduler(config_with_threshold(), db)

        due = scheduler.check_due()

        assert len(due) == 0

    def test_cancelled_not_picked_up(self):
        """Case 5: cancelled=1 → NOT picked up regardless of state."""
        db = FakeDbOps()
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        job = make_recurring("job-5", past)
        job.cancelled = True
        db.seeded_cronjobs["job-5"] = job
        scheduler = CronScheduler(config_with_threshold(), db)

        due = scheduler.check_due()

        assert len(due) == 0


# ---------------------------------------------------------------------------
# Cases 6-16: mark_complete paths
# ---------------------------------------------------------------------------


class TestMarkComplete:
    """Cases 6-16: mark_complete terminal actions and counter mechanics."""

    def test_recurring_success_reschedules_on_grid(self):
        """Case 6: recurring success → pending, failure_count=0, recurring_rescheduled."""
        from unittest.mock import patch
        import datetime as real_datetime_module

        db = FakeDbOps()
        scheduler = CronScheduler(config_with_threshold(), db)

        anchor = datetime(2025, 1, 1, 6, 30, 0, tzinfo=timezone.utc)
        # now is 5 minutes after the anchor; next slot must be anchor + 1 day
        now = datetime(2025, 1, 1, 6, 35, 0, tzinfo=timezone.utc)
        expected_next = compute_next_scheduled(anchor, timedelta(days=1), now)

        class _FrozenDatetime:
            def __init__(self, target_now: datetime):
                self._target_now = target_now
                self._real_datetime = real_datetime_module.datetime

            def now(self, tz=None):
                if tz is not None:
                    return self._target_now.astimezone(tz)
                return self._target_now

            def fromisoformat(self, s):
                return self._real_datetime.fromisoformat(s)

            def __getattr__(self, name):
                return getattr(self._real_datetime, name)

        frozen_dt = _FrozenDatetime(now)

        with patch("nixclaw_agent.cron.datetime", frozen_dt):
            result = scheduler.mark_complete(
                "job-6",
                success=True,
                is_recurring=True,
                interval_minutes=1440,
                scheduled_at=anchor.isoformat(),
            )

        assert result.terminal_action == "recurring_rescheduled"
        assert result.job_id == "job-6"
        call = db.update_calls[-1]
        assert call["status"] == "pending"
        assert call["failure_count"] == 0
        # Exact match against compute_next_scheduled to prevent silent now+interval regression
        assert call["scheduled_at"] == expected_next.isoformat()

    def test_non_recurring_success_terminal_complete(self):
        """Case 7: non-recurring success → complete, failure_count=0, none."""
        db = FakeDbOps()
        scheduler = CronScheduler(config_with_threshold(), db)

        result = scheduler.mark_complete(
            "job-7",
            success=True,
            is_recurring=False,
        )

        assert result.terminal_action == "none"
        call = db.update_calls[-1]
        assert call["status"] == "complete"
        assert call["failure_count"] == 0

    def test_failure_under_threshold_0_to_1(self):
        """Case 8: recurring failure, counter 0→1 → failed, failure_count=1, none."""
        db = FakeDbOps()
        db.seeded_cronjobs["job-8"] = make_recurring("job-8", datetime.now(timezone.utc), failure_count=0)
        scheduler = CronScheduler(config_with_threshold(), db)

        result = scheduler.mark_complete(
            "job-8",
            success=False,
            is_recurring=True,
            interval_minutes=60,
        )

        assert result.terminal_action == "none"
        call = db.update_calls[-1]
        assert call["status"] == "failed"
        assert call["failure_count"] == 1

    def test_recurring_threshold_trip_reschedules_on_grid(self):
        """Case 9: recurring failure 2→3 hits threshold → pending, failure_count=0, recurring_rescheduled."""
        db = FakeDbOps()
        anchor_dt = datetime.now(timezone.utc) - timedelta(hours=2)
        anchor = anchor_dt.isoformat()
        db.seeded_cronjobs["job-9"] = make_recurring(
            "job-9",
            anchor_dt,
            interval_minutes=60,
            failure_count=2,
            status="failed",
        )
        scheduler = CronScheduler(config_with_threshold(t=3), db)

        result = scheduler.mark_complete(
            "job-9",
            success=False,
            is_recurring=True,
            interval_minutes=60,
            scheduled_at=anchor,
        )

        assert result.terminal_action == "recurring_rescheduled"
        call = db.update_calls[-1]
        assert call["status"] == "pending"
        assert call["failure_count"] == 0
        # Must be strictly in the future
        next_dt = datetime.fromisoformat(call["scheduled_at"])
        assert next_dt > datetime.now(timezone.utc)

    def test_one_time_threshold_tripped(self):
        """Case 10: one-time failure at threshold → stays failed, one_time_threshold_tripped."""
        db = FakeDbOps()
        db.seeded_cronjobs["job-10"] = make_one_time(
            "job-10",
            datetime.now(timezone.utc),
            failure_count=2,
            status="failed",
        )
        scheduler = CronScheduler(config_with_threshold(t=3), db)

        result = scheduler.mark_complete(
            "job-10",
            success=False,
            is_recurring=False,
        )

        assert result.terminal_action == "one_time_threshold_tripped"
        call = db.update_calls[-1]
        assert call["status"] == "failed"  # row stays failed; orchestrator deletes
        assert call["failure_count"] == 3

    def test_success_resets_failure_count_from_streak(self):
        """Case 11: success after streak 0→2 → complete, failure_count=0, none."""
        db = FakeDbOps()
        db.seeded_cronjobs["job-11"] = make_one_time(
            "job-11",
            datetime.now(timezone.utc),
            failure_count=2,
            status="failed",
        )
        scheduler = CronScheduler(config_with_threshold(), db)

        result = scheduler.mark_complete(
            "job-11",
            success=True,
            is_recurring=False,
        )

        assert result.terminal_action == "none"
        call = db.update_calls[-1]
        assert call["status"] == "complete"
        assert call["failure_count"] == 0

    def test_recurring_corruption_guard_no_interval(self):
        """Case 12: recurring + interval_minutes=None at threshold → failed, none (no loop)."""
        db = FakeDbOps()
        db.seeded_cronjobs["job-12"] = CronJob(
            id="job-12",
            channel_id=CHANNEL,
            creator_number=CREATOR,
            prompt="test",
            scheduled_at=datetime.now(timezone.utc).isoformat(),
            status="failed",
            is_recurring=True,
            interval_minutes=None,
            cancelled=False,
            failure_count=2,
        )
        scheduler = CronScheduler(config_with_threshold(), db)

        result = scheduler.mark_complete(
            "job-12",
            success=False,
            is_recurring=True,
            interval_minutes=None,
        )

        assert result.terminal_action == "none"
        call = db.update_calls[-1]
        assert call["status"] == "failed"
        assert call["failure_count"] == 3  # incremented

    def test_cancelled_resets_failure_count(self):
        """Case 13: cancelled → terminal complete/failed, failure_count=0, none."""
        db = FakeDbOps()
        scheduler = CronScheduler(config_with_threshold(), db)

        result = scheduler.mark_complete(
            "job-13",
            success=False,
            is_recurring=True,
            is_cancelled=True,
        )

        assert result.terminal_action == "none"
        call = db.update_calls[-1]
        assert call["status"] == "failed"
        assert call["failure_count"] == 0

    def test_cancelled_success_resets_failure_count(self):
        """Case 13 mirror: success + cancelled → complete, failure_count=0, none."""
        db = FakeDbOps()
        scheduler = CronScheduler(config_with_threshold(), db)

        result = scheduler.mark_complete(
            "job-13-success",
            success=True,
            is_recurring=False,
            is_cancelled=True,
        )

        assert result.terminal_action == "none"
        call = db.update_calls[-1]
        assert call["status"] == "complete"
        assert call["failure_count"] == 0
        assert isinstance(result, CronCompleteResult)
        assert result.terminal_action == "none"

    def test_cancelled_after_threshold_streak_no_notification(self):
        """Case 15: cancelled after reaching threshold → failure_count=0, none (short-circuit)."""
        db = FakeDbOps()
        db.seeded_cronjobs["job-15"] = make_one_time(
            "job-15",
            datetime.now(timezone.utc),
            failure_count=3,
            status="failed",
        )
        scheduler = CronScheduler(config_with_threshold(), db)

        result = scheduler.mark_complete(
            "job-15",
            success=False,
            is_recurring=False,
            is_cancelled=True,
        )

        assert result.terminal_action == "none"
        call = db.update_calls[-1]
        assert call["status"] == "failed"
        assert call["failure_count"] == 0  # reset, not threshold_tripped

    def test_threshold_reschedule_strictly_after_now(self):
        """Case 16: rescheduled time is always strictly after now (even with long gap)."""
        db = FakeDbOps()
        # scheduled_at is 5 hours ago; interval=60min; next grid slot must be in the future
        anchor_dt = datetime.now(timezone.utc) - timedelta(hours=5)
        anchor = anchor_dt.isoformat()
        db.seeded_cronjobs["job-16"] = make_recurring(
            "job-16",
            anchor_dt,
            interval_minutes=60,
            failure_count=3,
            status="failed",
        )
        scheduler = CronScheduler(config_with_threshold(t=3), db)

        result = scheduler.mark_complete(
            "job-16",
            success=False,
            is_recurring=True,
            interval_minutes=60,
            scheduled_at=anchor,
        )

        call = db.update_calls[-1]
        next_dt = datetime.fromisoformat(call["scheduled_at"])
        now = datetime.now(timezone.utc)
        # Allow 1-second tolerance for test clock vs wall clock
        assert (next_dt - now).total_seconds() >= -1


class TestMarkCompleteAnchorEdgeCases:
    """Anchor-parsing edge cases for mark_complete's rescheduling path.

    Merged in from test_cron_reschedule.py; unique coverage not already exercised
    by TestMarkComplete above (which focuses on threshold/counter mechanics rather
    than malformed/missing anchor handling)."""

    def test_naive_iso_anchor_treated_as_utc(self):
        """A naive (no tzinfo) ISO anchor is treated as UTC, not crashed."""
        db = FakeDbOps()
        scheduler = CronScheduler(CronConfig(), db)

        naive_iso = "2025-01-01T06:30:00"  # No +00:00 suffix

        scheduler.mark_complete(
            "job-anchor-1",
            success=True,
            is_recurring=True,
            interval_minutes=60,
            scheduled_at=naive_iso,
        )

        assert len(db.update_calls) == 1
        assert db.update_calls[0]["status"] == "pending"
        assert db.update_calls[0]["scheduled_at"] is not None

    def test_unparseable_scheduled_at_falls_back_to_legacy(self):
        """An unparseable scheduled_at falls back to legacy now + interval behavior."""
        db = FakeDbOps()
        scheduler = CronScheduler(CronConfig(), db)

        scheduler.mark_complete(
            "job-anchor-2",
            success=True,
            is_recurring=True,
            interval_minutes=60,
            scheduled_at="invalid-iso-string",
        )

        assert len(db.update_calls) == 1
        call = db.update_calls[0]
        assert call["status"] == "pending"
        assert call["scheduled_at"] is not None
        # No crash; falls back to now + interval

    def test_missing_scheduled_at_falls_back_to_legacy(self):
        """No scheduled_at (None) falls back to legacy now + interval behavior."""
        db = FakeDbOps()
        scheduler = CronScheduler(CronConfig(), db)

        scheduler.mark_complete(
            "job-anchor-3",
            success=True,
            is_recurring=True,
            interval_minutes=60,
            scheduled_at=None,
        )

        assert len(db.update_calls) == 1
        call = db.update_calls[0]
        assert call["status"] == "pending"
        assert call["scheduled_at"] is not None

    def test_zero_or_negative_interval_not_rescheduled(self):
        """Zero interval_minutes does not trigger reschedule (treated as non-recurring)."""
        db = FakeDbOps()
        scheduler = CronScheduler(CronConfig(), db)

        scheduler.mark_complete(
            "job-anchor-4",
            success=True,
            is_recurring=True,
            interval_minutes=0,
            scheduled_at="2025-01-01T06:30:00+00:00",
        )

        assert len(db.update_calls) == 1
        call = db.update_calls[0]
        assert call["status"] == "complete"  # Treated as non-recurring
        assert call["scheduled_at"] is None  # Not rescheduled


class TestAntiRegression:
    """Regression tests: ensure the bug is fixed and existing behaviour preserved."""

    def test_recurring_success_never_produces_one_time_threshold_tripped(self):
        """Case 14: success → never one_time_threshold_tripped."""
        db = FakeDbOps()
        scheduler = CronScheduler(config_with_threshold(), db)

        result = scheduler.mark_complete(
            "job-14",
            success=True,
            is_recurring=True,
            interval_minutes=60,
            scheduled_at="2025-01-01T06:30:00+00:00",
        )

        assert result.terminal_action != "one_time_threshold_tripped"


# ---------------------------------------------------------------------------
# Case 17: _parse_iso_as_utc helper
# ---------------------------------------------------------------------------


class TestParseIsoAsUtc:
    """Unit tests for _parse_iso_as_utc."""

    def test_naive_parsed_as_utc(self):
        dt = _parse_iso_as_utc("2025-01-01T06:30:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_aware_preserved(self):
        dt = _parse_iso_as_utc("2025-01-01T06:30:00+05:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_none_returns_none(self):
        assert _parse_iso_as_utc(None) is None

    def test_invalid_returns_none(self):
        assert _parse_iso_as_utc("not-an-iso-date") is None


# ---------------------------------------------------------------------------
# compute_next_scheduled integration
# ---------------------------------------------------------------------------


class TestComputeNextScheduledGrid:
    """Ensure the threshold-driven reschedule path uses the correct grid."""

    def test_threshold_reschedule_matches_success_path_grid(self):
        """Threshold-tripped reschedule must produce the same next slot as success would."""
        from unittest.mock import patch

        anchor = datetime(2025, 1, 1, 6, 30, 0, tzinfo=timezone.utc)
        interval = timedelta(days=1)
        # now is 5 minutes late (relative to anchor)
        now = datetime(2025, 1, 1, 6, 35, 0, tzinfo=timezone.utc)
        next_from_compute = compute_next_scheduled(anchor, interval, now)
        assert next_from_compute == datetime(2025, 1, 2, 6, 30, 0, tzinfo=timezone.utc)

        # The mark_complete threshold path must produce the same result.
        # Patch datetime.now in the cron module while keeping fromisoformat intact.
        db = FakeDbOps()
        db.seeded_cronjobs["grid-test"] = make_recurring(
            "grid-test",
            anchor,
            interval_minutes=1440,
            failure_count=2,
            status="failed",
        )
        scheduler = CronScheduler(config_with_threshold(t=3), db)

        # Build a fake datetime class that delegates everything to the real datetime
        # but freezes datetime.now(timezone.utc) to our desired 'now' value.
        import datetime as real_datetime_module

        class _FrozenDatetime:
            """datetime substitute that replaces datetime.now() while keeping other methods."""
            def __init__(self, target_now: datetime):
                self._target_now = target_now
                self._real_datetime = real_datetime_module.datetime

            def now(self, tz=None):
                if tz is not None:
                    return self._target_now.astimezone(tz)
                return self._target_now

            def fromisoformat(self, s):
                return self._real_datetime.fromisoformat(s)

            def __getattr__(self, name):
                return getattr(self._real_datetime, name)

        frozen_dt = _FrozenDatetime(now)

        with patch("nixclaw_agent.cron.datetime", frozen_dt):
                result = scheduler.mark_complete(
                    "grid-test",
                    success=False,
                    is_recurring=True,
                    interval_minutes=1440,
                    scheduled_at=anchor.isoformat(),
                )

        assert result.terminal_action == "recurring_rescheduled"
        call = db.update_calls[-1]
        actual_next = datetime.fromisoformat(call["scheduled_at"])
        assert actual_next == next_from_compute


# ---------------------------------------------------------------------------
# init_database() 'running' -> 'pending' revival (fix/stuck-cronjob-running-state)
#
# Real sqlite, not FakeDbOps: FakeDbOps.get_due_cronjobs() reimplements the
# filtering logic in Python and never calls init_database(), so it can't
# observe this revival block. These tests instead point db_ops at a temp
# on-disk DB (same fixture pattern as tests/test_channel_turn_serialization.py)
# and exercise the real init_database() + get_due_cronjobs().
# ---------------------------------------------------------------------------


@pytest.fixture
def real_db_ops(monkeypatch, tmp_path):
    """Fresh temp sqlite DB, schema bootstrapped once (mirrors first process
    boot), yielding the db_ops module itself for direct use as the DB layer."""
    db_file = tmp_path / "nixclaw.db"
    monkeypatch.setattr(db_ops, "_connection", None)
    monkeypatch.setattr(config_module.paths, "data_dir", tmp_path)
    monkeypatch.setattr(config_module.database, "db_path", db_file)
    db_ops.init_database()
    yield db_ops
    monkeypatch.setattr(db_ops, "_connection", None)


class TestInitDatabaseRunningRevival:
    """Case 18: rows stuck in status='running' (set by CronScheduler.check_due()
    right before dispatch) must be revived back to 'pending' on the next
    init_database() call, i.e. the next process boot after a crash/restart
    mid-job -- mirroring the existing status='failed' revival directly above
    it in operations.py."""

    def test_running_row_reset_to_pending_with_failure_count_zero(self, real_db_ops):
        """A row inserted directly with status='running' is reset to
        pending/failure_count=0 the next time init_database() runs."""
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        job = make_recurring("job-running-1", past, failure_count=2, status="running")
        real_db_ops.store_cronjob(job)

        # Simulate the harness restarting: init_database() runs again on boot.
        real_db_ops.init_database()

        stored = real_db_ops.get_cronjob("job-running-1")
        assert stored is not None
        assert stored.status == "pending"
        assert stored.failure_count == 0

    def test_revived_row_is_re_picked_up_by_get_due_cronjobs(self, real_db_ops):
        """The fix is observably effective: get_due_cronjobs() re-selects the
        row after revival, not just a DB-state check."""
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        job = make_recurring("job-running-2", past, failure_count=1, status="running")
        real_db_ops.store_cronjob(job)

        # Before revival: 'running' is not selected by get_due_cronjobs() at all.
        assert real_db_ops.get_due_cronjobs() == []

        real_db_ops.init_database()

        due = real_db_ops.get_due_cronjobs()
        assert [j.id for j in due] == ["job-running-2"]
        assert due[0].status == "pending"

    def test_cancelled_running_row_revived_but_still_excluded_from_due(self, real_db_ops):
        """Regression check: a cancelled=1 row that was 'running' gets reset to
        'pending' (the UPDATE has no cancelled filter) but get_due_cronjobs()
        still correctly excludes it via its own cancelled=0 clause."""
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        job = make_recurring("job-running-cancelled", past, failure_count=1, status="running")
        job.cancelled = True
        real_db_ops.store_cronjob(job)

        real_db_ops.init_database()

        stored = real_db_ops.get_cronjob("job-running-cancelled")
        assert stored is not None
        assert stored.status == "pending"
        assert stored.failure_count == 0
        assert stored.cancelled is True

        assert real_db_ops.get_due_cronjobs() == []

    def test_init_database_idempotent_across_repeated_restarts(self, real_db_ops):
        """Repeated init_database() calls (simulating repeated restarts) must
        not error, and settle into a stable, non-flapping state."""
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        job = make_recurring("job-running-3", past, failure_count=1, status="running")
        real_db_ops.store_cronjob(job)

        # Several "restarts" in a row.
        real_db_ops.init_database()
        real_db_ops.init_database()
        real_db_ops.init_database()

        stored = real_db_ops.get_cronjob("job-running-3")
        assert stored is not None
        assert stored.status == "pending"
        assert stored.failure_count == 0

        # Still exactly one due row -- no duplication/corruption from repeated calls.
        assert [j.id for j in real_db_ops.get_due_cronjobs()] == ["job-running-3"]
