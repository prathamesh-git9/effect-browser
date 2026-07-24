from __future__ import annotations

from pathlib import Path

import pytest

from effect_browser.policy import ActionPolicy
from effect_browser.research import capture_research

from .test_job_harness_e2e import browser, start_harness


@pytest.mark.e2e
def test_real_browser_research_captures_hydrated_ats_page_without_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    driver = browser()
    try:
        report = capture_research(
            question="What role is advertised?",
            urls=(f"{base_url}/demo-jobs",),
            driver=driver,
            policy=ActionPolicy((base_url,)),
        )
        assert str(report.sources[0].observed_url) == f"{base_url}/demo-jobs"
        assert "Platform Reliability Engineer" in report.sources[0].excerpt
        assert report.sources[0].state_sha256
        assert report.sources[0].evidence_sha256
    finally:
        driver.close()
        server.should_exit = True
        thread.join(timeout=10)
