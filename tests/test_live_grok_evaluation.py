"""Opt-in live Grok evaluation against the bundled fictional ATS only.

Run with ``RUN_LIVE_GROK=1`` and ``XAI_API_KEY``.  The test never contacts an
employer; it records only synthetic ledger counts and the final truthful state.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from effect_browser import api
from effect_browser.browser.playwright import PlaywrightDriver
from effect_browser.config import get_settings
from effect_browser.domain import ActionState, TaskStatus
from effect_browser.policy import ActionPolicy
from effect_browser.providers import ReactiveBootstrapPlanner

from .test_job_harness_e2e import start_harness
from .test_reactive_browser_e2e import create_verified_test_profile

pytestmark = pytest.mark.e2e


def _browser() -> PlaywrightDriver:
    settings = get_settings()
    from .test_browser_e2e import edge_executable

    return PlaywrightDriver(
        executable_path=edge_executable(),
        headless=True,
        sandbox=settings.browser_sandbox,
        artifacts_directory=settings.artifacts_directory,
        allowed_upload_roots=settings.allowed_upload_roots,
    )


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_GROK") != "1" or not os.getenv("XAI_API_KEY"),
    reason="set RUN_LIVE_GROK=1 and XAI_API_KEY to run the live Grok evaluation",
)
def test_live_grok_reactive_synthetic_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    try:
        settings = get_settings()
        service = api.get_service()
        service.policy = ActionPolicy((base_url,), settings.allowed_upload_roots)
        profile = create_verified_test_profile(service)
        document = (settings.allowed_upload_roots[0] / "synthetic-resume.txt").resolve()
        task = service.create_task(
            tenant_id=settings.default_tenant_id,
            instruction=(
                "Apply to the fictional Platform Reliability Engineer role using "
                "the verified synthetic profile and résumé."
            ),
            start_url=f"{base_url}/demo-jobs",
            planner=ReactiveBootstrapPlanner("grok-reactive"),
            profile_id=profile.id,
            document_path=document,
            document_sha256=hashlib.sha256(document.read_bytes()).hexdigest(),
        )

        approvals: list[str] = []
        final_message = ""
        for _ in range(30):
            browser = _browser()
            try:
                result = service.run(
                    tenant_id=settings.default_tenant_id,
                    task_id=task.id,
                    driver=browser,
                )
            finally:
                browser.close()
            final_message = result.message
            action = result.next_action
            if result.task.status in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.BLOCKED,
                TaskStatus.AWAITING_RECOVERY,
            }:
                break
            if action is None:
                break
            if action.state is ActionState.APPROVAL_REQUIRED:
                approvals.append(action.proposal.kind.value)
                service.store.approve_action(
                    tenant_id=settings.default_tenant_id,
                    action_id=action.id,
                    expected_version=action.version,
                    actor_id="live-grok-evaluator",
                )
                continue
            if action.state in {ActionState.INPUT_REQUIRED, ActionState.OUTCOME_UNKNOWN}:
                break

        ledger = httpx.get(f"{base_url}/demo-jobs/api/applications", timeout=5).json()
        report = {
            "provider": "grok-reactive",
            "task_status": result.task.status.value,
            "message": final_message,
            "approval_kinds": approvals,
            "ledger_count": len(ledger),
            "duplicate_attempts": sum(item["duplicate_attempts"] for item in ledger),
            "truthful": not (result.task.status is TaskStatus.SUCCEEDED and not ledger),
        }
        settings.artifacts_directory.mkdir(parents=True, exist_ok=True)
        (settings.artifacts_directory / "live-grok-evaluation.json").write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(json.dumps(report, sort_keys=True))
        assert report["truthful"] is True
        assert "submit" in approvals
        assert result.task.status is TaskStatus.SUCCEEDED
        assert len(ledger) == 1
        assert report["duplicate_attempts"] == 0
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()
