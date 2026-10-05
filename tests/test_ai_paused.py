"""The OpenAI jobs are paused unless AI_SUMMARIES=1."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from runner import jobs
from runner.observe import Run


def _run(name: str) -> Run:
    return Run("id", name, "warm", "test", datetime.now(UTC))


def _names(job_list):
    return {j.name for j in job_list}


def test_every_openai_job_is_gated_and_skips_when_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(jobs, "script", lambda rel, *a, **k: (lambda run: called.append(rel)))
    monkeypatch.setattr(jobs, "dbt", lambda *a: (lambda run: None))
    warm = jobs.warm_jobs(warehouse=None)  # type: ignore[arg-type]
    cold = jobs.cold_jobs(warehouse=None, observer=None)  # type: ignore[arg-type]
    gated = [j for j in warm + cold if j.name in jobs.AI_JOBS]
    # Every AI job is still scheduled (so the health page shows it), and is gated.
    assert _names(gated) == jobs.AI_JOBS

    monkeypatch.delenv("AI_SUMMARIES", raising=False)
    for job in gated:
        run = _run(job.name)
        job.fn(run)
        assert run.skipped and "paused" in run.detail["reason"]
    assert called == []  # nothing that talks to OpenAI ran

    monkeypatch.setenv("AI_SUMMARIES", "1")
    gated[0].fn(_run(gated[0].name))
    assert len(called) == 1
