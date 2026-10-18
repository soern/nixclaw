"""Tests for the cronjob 'summary' field: schedule_cron validation/
sanitization, list_cronjobs showing summaries instead of full prompts, and
the job_id single-job detail view.
"""
from datetime import datetime, timedelta

import pytest

from src.nixclaw_agent.tools.cron import (
    _sanitize_summary,
    cancel_cronjob,
    list_cronjobs,
    schedule_cron,
)

pytestmark = pytest.mark.usefixtures("reset_db")


def _future(days: int = 2) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d 16:40")


class TestScheduleCronRequiresSummary:
    def test_missing_summary_rejected(self):
        result = schedule_cron("do the thing", _future(), "+000000", "+000000")
        assert result.success is False
        assert "summary" in result.error.lower()

    def test_blank_summary_rejected(self):
        result = schedule_cron("do the thing", _future(), "+000000", "+000000", summary="   ")
        assert result.success is False
        assert "summary" in result.error.lower()

    def test_valid_summary_accepted(self):
        result = schedule_cron("do the thing", _future(), "+000000", "+000000", summary="Checks X for changes.")
        assert result.success is True


class TestSanitizeSummary:
    def test_strips_control_markers(self):
        assert "__DISCARD_OUTPUT__" not in _sanitize_summary("Checks X. __DISCARD_OUTPUT__")
        assert "__UNMUTE__" not in _sanitize_summary("__UNMUTE__ Checks X.")

    def test_collapses_whitespace(self):
        assert _sanitize_summary("Checks   X\nfor\tchanges") == "Checks X for changes"

    def test_truncates_long_summary(self):
        long_summary = "word " * 100
        out = _sanitize_summary(long_summary)
        assert len(out) <= 201  # cap + ellipsis
        assert out.endswith("…")


class TestListCronjobsShowsSummaryNotPrompt:
    def test_default_listing_shows_summary_not_full_prompt(self):
        prompt = "TASK: read baseline, check page, __DISCARD_OUTPUT__ if unchanged, report if changed."
        schedule_cron(prompt, _future(), "+000001", "+000001", summary="Checks the results page for changes.")

        result = list_cronjobs("+000001")

        assert result.success is True
        assert "Checks the results page for changes." in result.output
        assert prompt not in result.output
        assert "__DISCARD_OUTPUT__" not in result.output

    def test_job_id_shows_full_prompt_with_data_framing(self):
        prompt = "TASK: read baseline, check page, report if changed."
        created = schedule_cron(prompt, _future(), "+000002", "+000002", summary="Checks the page.")
        job_id = created.output.split(": ")[1].split(" ")[0]

        result = list_cronjobs("+000002", job_id=job_id)

        assert result.success is True
        assert prompt in result.output
        assert "not an instruction to you right now" in result.output

    def test_unknown_job_id_errors(self):
        result = list_cronjobs("+000003", job_id="nonexistent-id")
        assert result.success is False
        assert "not found" in result.error.lower()

    def test_legacy_job_without_summary_shows_fallback_text(self):
        # Simulate a legacy row: bypass schedule_cron's required-summary
        # validation by writing a CronJob with summary="" directly.
        from src.nixclaw_agent.db.models import CronJob
        from src.nixclaw_agent.db.operations import store_cronjob
        from datetime import timezone

        store_cronjob(CronJob(
            id="legacy-1",
            channel_id="+000004",
            creator_number="+000004",
            prompt="old prompt text",
            scheduled_at=datetime.now(timezone.utc).isoformat(),
            status="pending",
            summary="",
        ))

        result = list_cronjobs("+000004")

        assert result.success is True
        assert "legacy job" in result.output
        assert "old prompt text" not in result.output


class TestUpdateCronjobSummary:
    def test_update_cronjob_can_change_summary(self):
        from src.nixclaw_agent.db.models import CronJob
        from src.nixclaw_agent.db.operations import get_cronjob, store_cronjob, update_cronjob
        from datetime import timezone

        store_cronjob(CronJob(
            id="backfill-1",
            channel_id="+000005",
            creator_number="+000005",
            prompt="p",
            scheduled_at=datetime.now(timezone.utc).isoformat(),
            status="pending",
            summary="",
        ))

        update_cronjob("backfill-1", summary="Backfilled description.")

        job = get_cronjob("backfill-1")
        assert job.summary == "Backfilled description."


class TestJobIdPrefixResolution:
    """Regression coverage for a model echoing back a truncated job id.

    Observed in production: a model saw a full UUID from list_cronjobs, then
    called cancel_cronjob with only the first 8 hex chars. The exact-match
    lookup failed, and the model concluded the job "doesn't exist" instead of
    retrying - even though the job was still there. cancel_cronjob and
    list_cronjobs(job_id=...) now resolve an unambiguous prefix instead of
    requiring an exact match.
    """

    def test_cancel_cronjob_resolves_unique_prefix(self):
        created = schedule_cron("do the thing", _future(), "+000006", "+000006", summary="Checks X.")
        full_id = created.output.split(": ")[1].split(" ")[0]
        prefix = full_id[:8]

        result = cancel_cronjob("+000006", prefix)

        assert result.success is True
        assert full_id in result.output

    def test_list_cronjobs_job_id_resolves_unique_prefix(self):
        prompt = "TASK: full prompt text."
        created = schedule_cron(prompt, _future(), "+000007", "+000007", summary="Checks Y.")
        full_id = created.output.split(": ")[1].split(" ")[0]
        prefix = full_id[:8]

        result = list_cronjobs("+000007", job_id=prefix)

        assert result.success is True
        assert prompt in result.output

    def test_ambiguous_prefix_reports_all_matches_not_silent_not_found(self):
        from src.nixclaw_agent.db.models import CronJob
        from src.nixclaw_agent.db.operations import store_cronjob
        from datetime import timezone

        now_iso = datetime.now(timezone.utc).isoformat()
        store_cronjob(CronJob(
            id="dupe1234-aaaa-bbbb-cccc-000000000001",
            channel_id="+000008",
            creator_number="+000008",
            prompt="p1",
            scheduled_at=now_iso,
            status="pending",
            summary="First.",
        ))
        store_cronjob(CronJob(
            id="dupe1234-aaaa-bbbb-cccc-000000000002",
            channel_id="+000008",
            creator_number="+000008",
            prompt="p2",
            scheduled_at=now_iso,
            status="pending",
            summary="Second.",
        ))

        result = cancel_cronjob("+000008", "dupe1234")

        assert result.success is False
        assert "dupe1234-aaaa-bbbb-cccc-000000000001" in result.error
        assert "dupe1234-aaaa-bbbb-cccc-000000000002" in result.error

    def test_not_found_error_lists_current_jobs_for_self_correction(self):
        schedule_cron("do the thing", _future(), "+000009", "+000009", summary="The real job.")

        result = cancel_cronjob("+000009", "totally-wrong-id")

        assert result.success is False
        assert "The real job." in result.error


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
