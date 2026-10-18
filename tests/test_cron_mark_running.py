"""CronScheduler.check_due() must not mark every due row 'running' up
front - only mark_running(), called by the caller for a job it actually
dispatches, should do that.

Regression test for the bug where a job skipped because its channel's
background lane was busy (core.py's run() loop) got stuck in 'running'
forever: get_due_cronjobs() only re-selects 'pending'/'failed' rows, and
mark_complete() is never called for a job that was never dispatched.
"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "src")

import pytest

from nixclaw_agent.config import CronConfig
from nixclaw_agent.cron import CronScheduler
from nixclaw_agent.db.models import CronJob


def _config(threshold: int = 3) -> CronConfig:
    return CronConfig(max_failure_count=threshold)


class FakeDbOps:
    """Minimal in-memory stand-in, just enough for these two tests."""

    def __init__(self):
        self.jobs: dict[str, CronJob] = {}
        self.update_calls: list[tuple[str, dict]] = []

    def get_due_cronjobs(self, max_failure_count=None):
        now = datetime.now(timezone.utc)
        return [
            j for j in self.jobs.values()
            if not j.cancelled and j.status == "pending"
            and datetime.fromisoformat(j.scheduled_at) <= now
        ]

    def update_cronjob(self, job_id, status=None, last_run=None, scheduled_at=None,
                        cancelled=None, failure_count=None, summary=None):
        self.update_calls.append((job_id, {"status": status}))
        if job_id in self.jobs and status is not None:
            self.jobs[job_id].status = status

    def store_cronjob(self, cronjob, failure_count=None):
        self.jobs[cronjob.id] = cronjob

    def get_channel_cronjobs(self, channel_id, status=None):
        return list(self.jobs.values())

    def get_cronjob(self, job_id):
        return self.jobs.get(job_id)

    def delete_cronjob(self, job_id):
        return self.jobs.pop(job_id, None) is not None


def _due_job(job_id: str) -> CronJob:
    return CronJob(
        id=job_id,
        channel_id="+000000",
        creator_number="+000000",
        prompt="p",
        scheduled_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        status="pending",
        summary="s",
    )


class TestCheckDueDoesNotMutate:
    def test_check_due_leaves_status_pending(self):
        db = FakeDbOps()
        db.jobs["job-1"] = _due_job("job-1")
        scheduler = CronScheduler(_config(), db)

        due = scheduler.check_due()

        assert [j.id for j in due] == ["job-1"]
        assert db.jobs["job-1"].status == "pending"
        assert db.update_calls == []

    def test_check_due_called_twice_returns_same_job_when_skipped(self):
        """The whole point of the fix: a caller that skips a due job (never
        calls mark_running) sees it due again on the next check_due()."""
        db = FakeDbOps()
        db.jobs["job-1"] = _due_job("job-1")
        scheduler = CronScheduler(_config(), db)

        first = scheduler.check_due()
        # Simulate the caller skipping it (busy background lane) - no
        # mark_running() call.
        second = scheduler.check_due()

        assert [j.id for j in first] == ["job-1"]
        assert [j.id for j in second] == ["job-1"]


class TestMarkRunning:
    def test_mark_running_sets_status(self):
        db = FakeDbOps()
        db.jobs["job-1"] = _due_job("job-1")
        scheduler = CronScheduler(_config(), db)

        scheduler.mark_running("job-1")

        assert db.jobs["job-1"].status == "running"
        assert db.update_calls == [("job-1", {"status": "running"})]

    def test_dispatched_job_no_longer_due_after_mark_running(self):
        db = FakeDbOps()
        db.jobs["job-1"] = _due_job("job-1")
        scheduler = CronScheduler(_config(), db)

        scheduler.check_due()
        scheduler.mark_running("job-1")

        assert scheduler.check_due() == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
