from __future__ import annotations

import threading
from pathlib import Path

import httpx
import pytest
import uvicorn

from effect_browser import api
from effect_browser.browser.playwright import PlaywrightDriver
from effect_browser.config import get_settings
from effect_browser.domain import ActionState, TaskStatus
from effect_browser.engine import CrashAfterCommitDriver, SimulatedProcessCrash
from effect_browser.policy import ActionPolicy
from effect_browser.providers import JobHarnessPlanner

from .test_browser_e2e import edge_executable, free_port, wait_until_ready


def start_harness(tmp_path: Path, monkeypatch):
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv(
        "EFFECT_BROWSER_DATABASE_URL",
        f"sqlite:///{tmp_path / 'job-harness.db'}",
    )
    monkeypatch.setenv("EFFECT_BROWSER_ALLOWED_ORIGINS", base_url)
    monkeypatch.setenv(
        "EFFECT_BROWSER_ARTIFACTS_DIRECTORY",
        str(tmp_path / "artifacts"),
    )
    resume_path = tmp_path / "synthetic-resume.txt"
    resume_path.write_text(
        "Synthetic résumé fixture. This is not a real person's document.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EFFECT_BROWSER_ALLOWED_UPLOAD_ROOTS", str(tmp_path))
    get_settings.cache_clear()
    api.get_store.cache_clear()
    server = uvicorn.Server(
        uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_until_ready(base_url)
    return base_url, server, thread


def browser() -> PlaywrightDriver:
    settings = get_settings()
    return PlaywrightDriver(
        executable_path=edge_executable(),
        headless=True,
        sandbox=settings.browser_sandbox,
        artifacts_directory=settings.artifacts_directory,
        allowed_upload_roots=settings.allowed_upload_roots,
    )


def prepare_application(base_url: str, mode: str):
    settings = get_settings()
    service = api.get_service()
    service.policy = ActionPolicy((base_url,), settings.allowed_upload_roots)
    task = service.create_task(
        tenant_id=settings.default_tenant_id,
        instruction="Submit one synthetic application and prove the ATS stored it.",
        start_url=base_url,
        planner=JobHarnessPlanner(
            mode,
            get_settings().allowed_upload_roots[0] / "synthetic-resume.txt",
        ),
    )
    first = browser()
    try:
        paused = service.run(
            tenant_id=settings.default_tenant_id,
            task_id=task.id,
            driver=first,
        )
    finally:
        first.close()
    action = paused.next_action
    assert action is not None
    assert action.state is ActionState.APPROVAL_REQUIRED
    assert action.proposal.kind.value == "upload"
    service.store.approve_action(
        tenant_id=settings.default_tenant_id,
        action_id=action.id,
        expected_version=action.version,
        actor_id="job-harness-operator",
    )
    upload_runner = browser()
    try:
        paused = service.run(
            tenant_id=settings.default_tenant_id,
            task_id=task.id,
            driver=upload_runner,
        )
    finally:
        upload_runner.close()
    submit = paused.next_action
    assert submit is not None
    assert submit.state is ActionState.APPROVAL_REQUIRED
    assert submit.proposal.kind.value == "submit"
    service.store.approve_action(
        tenant_id=settings.default_tenant_id,
        action_id=submit.id,
        expected_version=submit.version,
        actor_id="job-harness-operator",
    )
    return service, task, submit


@pytest.mark.e2e
def test_dynamic_job_application_requires_authoritative_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    try:
        service, task, submit = prepare_application(base_url, "real")
        runner = browser()
        try:
            result = service.run(
                tenant_id=get_settings().default_tenant_id,
                task_id=task.id,
                driver=runner,
            )
        finally:
            runner.close()
        ledger = httpx.get(
            f"{base_url}/demo-jobs/api/applications",
            timeout=5,
        ).json()
        receipt = service.store.get_receipt(
            get_settings().default_tenant_id,
            submit.id,
        )

        assert result.task.status is TaskStatus.SUCCEEDED
        assert len(ledger) == 1
        assert ledger[0]["reference"] == submit.proposal.effect_key
        assert ledger[0]["duplicate_attempts"] == 0
        assert receipt is not None
        assert receipt.external_id == ledger[0]["id"]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_fake_success_page_never_becomes_application_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    try:
        service, task, submit = prepare_application(base_url, "fake_success")
        runner = browser()
        try:
            result = service.run(
                tenant_id=get_settings().default_tenant_id,
                task_id=task.id,
                driver=runner,
            )
        finally:
            runner.close()
        ledger = httpx.get(
            f"{base_url}/demo-jobs/api/applications",
            timeout=5,
        ).json()

        assert result.task.status is TaskStatus.AWAITING_RECOVERY
        assert result.next_action is not None
        assert result.next_action.state is ActionState.OUTCOME_UNKNOWN
        assert "unverified" in result.message
        assert ledger == []
        assert (
            service.store.get_receipt(get_settings().default_tenant_id, submit.id) is None
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_dynamic_application_crash_reconciles_without_duplicate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    try:
        service, task, submit = prepare_application(base_url, "real")
        crashing = CrashAfterCommitDriver(browser())
        try:
            with pytest.raises(SimulatedProcessCrash):
                service.run(
                    tenant_id=get_settings().default_tenant_id,
                    task_id=task.id,
                    driver=crashing,
                )
        finally:
            crashing.close()

        recovery = browser()
        try:
            stopped = service.run(
                tenant_id=get_settings().default_tenant_id,
                task_id=task.id,
                driver=recovery,
            )
            assert stopped.next_action is not None
            assert stopped.next_action.state is ActionState.OUTCOME_UNKNOWN
            receipt = service.reconcile(
                tenant_id=get_settings().default_tenant_id,
                action_id=submit.id,
                driver=recovery,
            )
        finally:
            recovery.close()
        assert receipt is not None

        final_browser = browser()
        try:
            final = service.run(
                tenant_id=get_settings().default_tenant_id,
                task_id=task.id,
                driver=final_browser,
            )
        finally:
            final_browser.close()
        ledger = httpx.get(
            f"{base_url}/demo-jobs/api/applications",
            timeout=5,
        ).json()

        assert final.task.status is TaskStatus.SUCCEEDED
        assert len(ledger) == 1
        assert ledger[0]["reference"] == submit.proposal.effect_key
        assert ledger[0]["duplicate_attempts"] == 0
        assert receipt.external_id == ledger[0]["id"]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()
