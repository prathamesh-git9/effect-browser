from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from effect_browser import api
from effect_browser.browser.playwright import PlaywrightDriver
from effect_browser.config import get_settings
from effect_browser.domain import (
    ActionKind,
    ActionState,
    ProposedAction,
    TaskStatus,
)
from effect_browser.engine import CrashAfterCommitDriver, SimulatedProcessCrash
from effect_browser.policy import ActionPolicy
from effect_browser.providers import DeterministicPlanner
from effect_browser.uploads import sha256_file


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def edge_executable() -> str | None:
    configured = os.getenv("EFFECT_BROWSER_BROWSER_EXECUTABLE")
    if configured:
        return configured
    candidates = (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    )
    return str(next((path for path in candidates if path.exists()), "")) or None


def wait_until_ready(base_url: str) -> None:
    deadline = time.monotonic() + 10
    last_response: httpx.Response | None = None
    while time.monotonic() < deadline:
        try:
            last_response = httpx.get(f"{base_url}/readyz", timeout=1)
            if last_response.status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.05)
    detail = last_response.text if last_response is not None else "no HTTP response"
    raise RuntimeError(f"test server did not become ready: {detail}")


@pytest.mark.e2e
def test_real_browser_upload_is_allowlisted_hash_bound_and_path_redacted(
    tmp_path: Path,
) -> None:
    upload_root = tmp_path / "approved"
    upload_root.mkdir()
    document = upload_root / "synthetic-resume.txt"
    document.write_bytes(b"synthetic resume fixture")
    form = tmp_path / "upload-form.html"
    form.write_text(
        """
        <!doctype html><title>Upload fixture</title>
        <label for="resume">Résumé</label>
        <input id="resume" name="resume" type="file" required>
        """,
        encoding="utf-8",
    )
    driver = PlaywrightDriver(
        executable_path=edge_executable(),
        headless=True,
        sandbox=os.getenv(
            "EFFECT_BROWSER_BROWSER_SANDBOX",
            "true",
        ).casefold()
        not in {"0", "false", "no", "off"},
        artifacts_directory=tmp_path / "artifacts",
        allowed_upload_roots=(upload_root,),
    )
    try:
        driver.execute(
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=form.resolve().as_uri(),
                description="Open the local synthetic upload fixture.",
            )
        )
        candidate = driver.snapshot().candidates[0]
        receipt = driver.execute(
            ProposedAction(
                kind=ActionKind.UPLOAD,
                locator=candidate.locator,
                file_path=document.resolve(),
                document_sha256=sha256_file(document),
                description="Attach the approved synthetic document.",
            )
        )
        attached = driver.snapshot().candidates[0]
    finally:
        driver.close()

    assert receipt.external_id == "local-upload"
    assert attached.filled is True
    assert attached.current_value is None
    assert document.name not in attached.model_dump_json()


@pytest.mark.e2e
def test_real_browser_crash_reconciles_one_order(tmp_path: Path, monkeypatch) -> None:
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv(
        "EFFECT_BROWSER_DATABASE_URL",
        f"sqlite:///{tmp_path / 'browser-e2e.db'}",
    )
    monkeypatch.setenv("EFFECT_BROWSER_ALLOWED_ORIGINS", base_url)
    monkeypatch.setenv("EFFECT_BROWSER_ARTIFACTS_DIRECTORY", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    api.get_store.cache_clear()
    server = uvicorn.Server(
        uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_until_ready(base_url)

    settings = get_settings()
    service = api.get_service()
    service.policy = ActionPolicy((base_url,))
    tenant = settings.default_tenant_id
    executable = edge_executable()

    def browser() -> PlaywrightDriver:
        return PlaywrightDriver(
            executable_path=executable,
            headless=True,
            sandbox=settings.browser_sandbox,
            artifacts_directory=settings.artifacts_directory,
        )

    try:
        task = service.create_task(
            tenant_id=tenant,
            instruction="Order once; never duplicate after a crash.",
            start_url=base_url,
            planner=DeterministicPlanner(),
        )
        first = browser()
        try:
            paused = service.run(tenant_id=tenant, task_id=task.id, driver=first)
        finally:
            first.close()
        action = paused.next_action
        assert action is not None
        assert action.state is ActionState.APPROVAL_REQUIRED
        service.store.approve_action(
            tenant_id=tenant,
            action_id=action.id,
            expected_version=action.version,
            actor_id="e2e-operator",
        )

        crashing = CrashAfterCommitDriver(browser())
        try:
            with pytest.raises(SimulatedProcessCrash):
                service.run(tenant_id=tenant, task_id=task.id, driver=crashing)
        finally:
            crashing.close()

        recovery = browser()
        try:
            stopped = service.run(tenant_id=tenant, task_id=task.id, driver=recovery)
            assert stopped.next_action is not None
            assert stopped.next_action.state is ActionState.OUTCOME_UNKNOWN
            receipt = service.reconcile(
                tenant_id=tenant,
                action_id=action.id,
                driver=recovery,
            )
        finally:
            recovery.close()
        assert receipt is not None

        final_browser = browser()
        try:
            final = service.run(
                tenant_id=tenant,
                task_id=task.id,
                driver=final_browser,
            )
        finally:
            final_browser.close()
        orders = httpx.get(f"{base_url}/demo-shop/api/orders", timeout=5).json()
        matching = [
            row for row in orders if row["reference"] == action.proposal.effect_key
        ]

        assert final.task.status is TaskStatus.SUCCEEDED
        assert len(matching) == 1
        assert matching[0]["duplicate_attempts"] == 0
        assert service.store.verify_audit(tenant).valid is True
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()
