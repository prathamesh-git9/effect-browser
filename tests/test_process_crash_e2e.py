from __future__ import annotations

import multiprocessing
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import uvicorn

from effect_browser import api
from effect_browser.browser.playwright import PlaywrightDriver
from effect_browser.config import get_settings
from effect_browser.domain import (
    ActionKind,
    ActionState,
    BrowserAction,
    PageSnapshot,
    PlanRequest,
    ProposedAction,
    TaskStatus,
    digest,
    utc_now,
)
from effect_browser.engine import EffectBrowserService
from effect_browser.policy import ActionPolicy
from effect_browser.providers import DeterministicPlanner
from effect_browser.store import ConflictError, DatabaseStore

from .test_browser_e2e import free_port, wait_until_ready

_HARD_EXIT_CODE = 86
_TASK_LEASE_SECONDS = 8


class _UnsafeOrderPlanner:
    """Create the same reviewed POST without granting a receipt lookup contract."""

    name = "unsafe-process-crash"

    def plan(self, request: PlanRequest) -> tuple[ProposedAction, ...]:
        return tuple(
            action.model_copy(update={"reconciliation": None})
            if action.kind is ActionKind.SUBMIT
            else action
            for action in DeterministicPlanner().plan(request)
        )


class _HardExitAfterSubmit:
    """Kill the worker only after Chromium returns from transmitting the submit."""

    def __init__(self, inner: PlaywrightDriver) -> None:
        self.inner = inner

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    def execute(self, action: ProposedAction):
        receipt = self.inner.execute(action)
        if action.kind is ActionKind.SUBMIT:
            # os._exit deliberately bypasses service and driver finally blocks. The
            # parent can therefore prove recovery from durable state, not an exception.
            os._exit(_HARD_EXIT_CODE)
        return receipt


class _UnusedDriver:
    def __getattr__(self, name: str):
        raise AssertionError(f"fenced recovery unexpectedly used driver method {name}")


@dataclass
class _CrashedOrder:
    base_url: str
    tenant_id: UUID
    task_id: UUID
    action: BrowserAction
    store: DatabaseStore
    service: EffectBrowserService
    artifacts_directory: Path
    browser_sandbox: bool

    def browser(self) -> PlaywrightDriver:
        return _browser(
            self.base_url,
            self.artifacts_directory,
            self.browser_sandbox,
        )

    def matching_orders(self) -> list[dict]:
        orders = httpx.get(
            f"{self.base_url}/demo-shop/api/orders",
            timeout=5,
        ).json()
        return [
            row for row in orders if row["reference"] == self.action.proposal.effect_key
        ]


def _browser(
    base_url: str,
    artifacts_directory: Path,
    browser_sandbox: bool,
) -> PlaywrightDriver:
    return PlaywrightDriver(
        headless=True,
        sandbox=browser_sandbox,
        artifacts_directory=artifacts_directory,
        allowed_origins=(base_url,),
    )


def _hard_crash_worker(
    database_url: str,
    base_url: str,
    tenant_id: UUID,
    task_id: UUID,
    artifacts_directory: Path,
    browser_sandbox: bool,
) -> None:
    store = DatabaseStore(database_url)
    store.initialize()
    service = EffectBrowserService(
        store,
        ActionPolicy((base_url,)),
        task_lease_seconds=_TASK_LEASE_SECONDS,
    )
    browser = _browser(base_url, artifacts_directory, browser_sandbox)
    try:
        service.run(
            tenant_id=tenant_id,
            task_id=task_id,
            driver=_HardExitAfterSubmit(browser),
        )
    finally:
        # A correct run never reaches cleanup because os._exit bypasses finally.
        browser.close()
        store.close()
    raise AssertionError("worker returned without reaching the submit crash point")


def _spawn_crashing_worker(case: _CrashedOrder, database_url: str) -> None:
    process = multiprocessing.get_context("spawn").Process(
        target=_hard_crash_worker,
        args=(
            database_url,
            case.base_url,
            case.tenant_id,
            case.task_id,
            case.artifacts_directory / "crashed-worker",
            case.browser_sandbox,
        ),
    )
    process.start()
    process.join(timeout=60)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        process.close()
        pytest.fail("hard-crash worker did not reach the submit within 60 seconds")
    exit_code = process.exitcode
    process.close()
    assert exit_code == _HARD_EXIT_CODE


def _wait_for_dead_worker_lease(case: _CrashedOrder) -> None:
    task = case.store.get_task(case.tenant_id, case.task_id)
    assert task.lease_owner is not None
    assert task.lease_expires_at is not None
    remaining = (task.lease_expires_at - utc_now()).total_seconds()
    time.sleep(max(0.0, remaining) + 0.2)


@contextmanager
def _crashed_order(tmp_path: Path, monkeypatch, *, unsafe: bool):
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    database_url = f"sqlite:///{tmp_path / 'process-crash.db'}"
    artifacts_directory = tmp_path / "artifacts"
    monkeypatch.setenv("EFFECT_BROWSER_DATABASE_URL", database_url)
    monkeypatch.setenv("EFFECT_BROWSER_ALLOWED_ORIGINS", base_url)
    monkeypatch.setenv(
        "EFFECT_BROWSER_ARTIFACTS_DIRECTORY",
        str(artifacts_directory),
    )
    get_settings.cache_clear()
    api.get_store.cache_clear()
    settings = get_settings()
    server = uvicorn.Server(
        uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_until_ready(base_url)

    preparation_store = DatabaseStore(database_url)
    preparation_store.initialize()
    fresh_store: DatabaseStore | None = None
    try:
        preparation_service = EffectBrowserService(
            preparation_store,
            ActionPolicy((base_url,)),
        )
        task = preparation_service.create_task(
            tenant_id=settings.default_tenant_id,
            instruction="Commit one order and never retry an ambiguous dispatch.",
            start_url=base_url,
            planner=_UnsafeOrderPlanner() if unsafe else DeterministicPlanner(),
        )
        preview_browser = _browser(
            base_url,
            artifacts_directory / "preview",
            settings.browser_sandbox,
        )
        try:
            paused = preparation_service.run(
                tenant_id=settings.default_tenant_id,
                task_id=task.id,
                driver=preview_browser,
            )
        finally:
            preview_browser.close()
        action = paused.next_action
        assert action is not None
        assert action.proposal.kind is ActionKind.SUBMIT
        assert action.state is ActionState.APPROVAL_REQUIRED
        preparation_service.store.approve_action(
            tenant_id=settings.default_tenant_id,
            action_id=action.id,
            expected_version=action.version,
            actor_id="process-crash-operator",
        )
        preparation_store.close()

        placeholder_store = DatabaseStore(database_url)
        case = _CrashedOrder(
            base_url=base_url,
            tenant_id=settings.default_tenant_id,
            task_id=task.id,
            action=action,
            store=placeholder_store,
            service=EffectBrowserService(
                placeholder_store,
                ActionPolicy((base_url,)),
                task_lease_seconds=_TASK_LEASE_SECONDS,
            ),
            artifacts_directory=artifacts_directory,
            browser_sandbox=settings.browser_sandbox,
        )
        _spawn_crashing_worker(case, database_url)
        placeholder_store.close()

        # Reopen every worker-side object. Reusing the preparation service would
        # hide stale connections and would not demonstrate restart recovery.
        fresh_store = DatabaseStore(database_url)
        fresh_store.initialize()
        case.store = fresh_store
        case.service = EffectBrowserService(
            fresh_store,
            ActionPolicy((base_url,)),
            task_lease_seconds=_TASK_LEASE_SECONDS,
        )
        crashed = fresh_store.get_action(case.tenant_id, action.id)
        assert crashed.state is ActionState.DISPATCHING
        assert fresh_store.get_receipt(case.tenant_id, action.id) is None
        matching = case.matching_orders()
        assert len(matching) == 1
        assert matching[0]["duplicate_attempts"] == 0

        # The dead owner remains authoritative until its lease expires. Recovery
        # must wait for that fence rather than stealing a possibly live dispatch.
        with pytest.raises(ConflictError, match="leased by another worker"):
            case.service.run(
                tenant_id=case.tenant_id,
                task_id=case.task_id,
                driver=_UnusedDriver(),
            )
        _wait_for_dead_worker_lease(case)
        yield case
    finally:
        if fresh_store is not None:
            fresh_store.close()
        else:
            preparation_store.close()
        server.should_exit = True
        thread.join(timeout=10)
        if api.get_store.cache_info().currsize:
            api.get_store().close()
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_process_death_reconciles_one_real_chromium_submit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with _crashed_order(tmp_path, monkeypatch, unsafe=False) as case:
        recovery = case.browser()
        try:
            stopped = case.service.run(
                tenant_id=case.tenant_id,
                task_id=case.task_id,
                driver=recovery,
            )
            assert stopped.next_action is not None
            assert stopped.next_action.state is ActionState.OUTCOME_UNKNOWN
            assert case.store.get_receipt(case.tenant_id, case.action.id) is None
            receipt = case.service.reconcile(
                tenant_id=case.tenant_id,
                action_id=case.action.id,
                driver=recovery,
            )
        finally:
            recovery.close()

        assert receipt is not None
        final_browser = case.browser()
        try:
            final = case.service.run(
                tenant_id=case.tenant_id,
                task_id=case.task_id,
                driver=final_browser,
            )
        finally:
            final_browser.close()
        assert final.task.status is TaskStatus.SUCCEEDED
        matching = case.matching_orders()
        assert len(matching) == 1
        assert matching[0]["duplicate_attempts"] == 0
        assert case.store.verify_audit(case.tenant_id).valid


@pytest.mark.e2e
def test_process_death_without_receipt_contract_requires_manual_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with _crashed_order(tmp_path, monkeypatch, unsafe=True) as case:
        recovery = case.browser()
        try:
            stopped = case.service.run(
                tenant_id=case.tenant_id,
                task_id=case.task_id,
                driver=recovery,
            )
            assert stopped.task.status is TaskStatus.AWAITING_RECOVERY
            assert stopped.next_action is not None
            assert stopped.next_action.state is ActionState.OUTCOME_UNKNOWN
            assert (
                case.service.reconcile(
                    tenant_id=case.tenant_id,
                    action_id=case.action.id,
                    driver=recovery,
                )
                is None
            )
            repeated = case.service.run(
                tenant_id=case.tenant_id,
                task_id=case.task_id,
                driver=recovery,
            )
        finally:
            recovery.close()

        assert repeated.task.status is TaskStatus.AWAITING_RECOVERY
        assert repeated.next_action is not None
        assert repeated.next_action.state is ActionState.OUTCOME_UNKNOWN
        assert case.store.get_receipt(case.tenant_id, case.action.id) is None
        matching = case.matching_orders()
        assert len(matching) == 1
        assert matching[0]["duplicate_attempts"] == 0
        unknown_events = [
            event
            for event in case.store.events(case.tenant_id, case.task_id)
            if event.kind == "action.outcome_unknown"
        ]
        assert len(unknown_events) == 1
        assert unknown_events[0].payload["automatic_retry"] is False
        assert case.store.verify_audit(case.tenant_id).valid


def test_finish_expected_outcome_hashes_ephemeral_rendered_evidence() -> None:
    expected_phrase = "Application received"
    snapshot = PageSnapshot(
        url="https://portal.example/complete",
        title="Submission complete",
        state_sha256="a" * 64,
        text_excerpt="Your APPLICATION RECEIVED confirmation is ready.",
        candidates=(),
        captured_at=utc_now(),
    )

    class SnapshotDriver:
        def snapshot(self) -> PageSnapshot:
            return snapshot

        def observe(self):
            raise AssertionError("verified FINISH must use the rendered snapshot")

    proposal = ProposedAction(
        kind=ActionKind.FINISH,
        description="Verify the rendered confirmation.",
        expected_outcome=expected_phrase,
    )
    receipt = EffectBrowserService._execute(proposal, SnapshotDriver())

    assert receipt.evidence_sha256 == digest(
        {
            "url": snapshot.url,
            "state_sha256": snapshot.state_sha256,
            "expected_phrase_sha256": digest({"expected_phrase": expected_phrase}),
        }
    )
    assert snapshot.text_excerpt not in receipt.model_dump_json()

    missing = snapshot.model_copy(
        update={
            "title": "Still processing",
            "text_excerpt": "No confirmation is visible.",
        }
    )

    class MissingSnapshotDriver:
        def snapshot(self) -> PageSnapshot:
            return missing

    with pytest.raises(ValueError, match="expected finish outcome"):
        EffectBrowserService._execute(proposal, MissingSnapshotDriver())
