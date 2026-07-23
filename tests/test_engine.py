from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import text

from effect_browser.domain import (
    ActionKind,
    ActionState,
    Locator,
    ProposedAction,
    TaskStatus,
)
from effect_browser.engine import CrashAfterCommitDriver, SimulatedProcessCrash
from effect_browser.providers import DeterministicPlanner
from effect_browser.store import ConflictError, DatabaseStore, NotFoundError

from .conftest import BASE_URL, TENANT, FakeDriver, RemoteSystem


def create(service, tenant: UUID = TENANT, start_url: str = BASE_URL):
    return service.create_task(
        tenant_id=tenant,
        instruction="Order once without a duplicate.",
        start_url=start_url,
        planner=DeterministicPlanner(),
    )


def prepare_and_approve(service, remote: RemoteSystem):
    task = create(service)
    paused = service.run(tenant_id=TENANT, task_id=task.id, driver=FakeDriver(remote))
    action = paused.next_action
    assert action is not None
    assert action.state is ActionState.APPROVAL_REQUIRED
    approved = service.store.approve_action(
        tenant_id=TENANT,
        action_id=action.id,
        expected_version=action.version,
        actor_id="test-operator",
    )
    return task, approved


def test_external_commit_requires_bound_approval(service) -> None:
    remote = RemoteSystem()
    task = create(service)

    paused = service.run(tenant_id=TENANT, task_id=task.id, driver=FakeDriver(remote))

    assert paused.task.status is TaskStatus.AWAITING_APPROVAL
    assert paused.next_action is not None
    assert paused.next_action.state is ActionState.APPROVAL_REQUIRED
    assert paused.next_action.observation_sha256
    assert remote.commits == 0


def test_submit_without_independent_receipt_never_claims_success(service) -> None:
    class UnverifiablePlanner:
        name = "unverifiable"

        def plan(self, _request):
            return (
                ProposedAction(
                    kind=ActionKind.NAVIGATE,
                    url=f"{BASE_URL}/form",
                    description="Open a synthetic form.",
                ),
                ProposedAction(
                    kind=ActionKind.SUBMIT,
                    locator=Locator(role="button", name="Commit"),
                    description="Submit without a receipt lookup.",
                    effect_key="UNVERIFIABLE-EFFECT",
                    expected_outcome="One remote effect.",
                ),
            )

    remote = RemoteSystem()
    task = service.create_task(
        tenant_id=TENANT,
        instruction="Never trust visible success.",
        start_url=BASE_URL,
        planner=UnverifiablePlanner(),
    )
    paused = service.run(tenant_id=TENANT, task_id=task.id, driver=FakeDriver(remote))
    assert paused.next_action is not None
    service.store.approve_action(
        tenant_id=TENANT,
        action_id=paused.next_action.id,
        expected_version=paused.next_action.version,
        actor_id="test-operator",
    )

    result = service.run(tenant_id=TENANT, task_id=task.id, driver=FakeDriver(remote))

    assert remote.commits == 1
    assert result.task.status is TaskStatus.AWAITING_RECOVERY
    assert result.next_action is not None
    assert result.next_action.state is ActionState.OUTCOME_UNKNOWN
    assert "visible success is not accepted" in result.message


def test_crash_after_commit_is_reconciled_without_retry(service) -> None:
    remote = RemoteSystem()
    task, approved = prepare_and_approve(service, remote)

    with pytest.raises(SimulatedProcessCrash):
        service.run(
            tenant_id=TENANT,
            task_id=task.id,
            driver=CrashAfterCommitDriver(FakeDriver(remote)),
        )
    assert remote.commits == 1
    assert service.store.get_action(TENANT, approved.id).state is ActionState.DISPATCHING

    restarted = service.run(
        tenant_id=TENANT,
        task_id=task.id,
        driver=FakeDriver(remote),
    )
    assert restarted.next_action is not None
    assert restarted.next_action.state is ActionState.OUTCOME_UNKNOWN
    assert remote.commits == 1

    receipt = service.reconcile(
        tenant_id=TENANT,
        action_id=approved.id,
        driver=FakeDriver(remote),
    )
    assert receipt is not None
    final = service.run(tenant_id=TENANT, task_id=task.id, driver=FakeDriver(remote))
    assert final.task.status is TaskStatus.SUCCEEDED
    assert remote.commits == 1


def test_page_drift_invalidates_previous_approval(service) -> None:
    remote = RemoteSystem()
    task, approved = prepare_and_approve(service, remote)
    remote.page_revision += 1

    result = service.run(tenant_id=TENANT, task_id=task.id, driver=FakeDriver(remote))

    assert result.next_action is not None
    assert result.next_action.state is ActionState.INVALIDATED
    assert remote.commits == 0
    assert service.store.latest_approval(TENANT, approved.id) is not None


def test_stale_approval_version_is_rejected(service) -> None:
    remote = RemoteSystem()
    _task, action = prepare_and_approve(service, remote)

    with pytest.raises(ConflictError, match="version changed"):
        service.store.approve_action(
            tenant_id=TENANT,
            action_id=action.id,
            expected_version=action.version - 1,
            actor_id="stale-operator",
        )


def test_disallowed_origin_fails_closed(service) -> None:
    task = create(service, start_url="https://not-allowed.example")
    result = service.run(
        tenant_id=TENANT, task_id=task.id, driver=FakeDriver(RemoteSystem())
    )

    assert result.task.status is TaskStatus.FAILED
    assert result.next_action is not None
    assert "origin is not allowed" in (result.next_action.failure or "")


def test_tenant_scope_is_enforced(service) -> None:
    task = create(service)
    stranger = UUID("20000000-0000-0000-0000-000000000002")

    with pytest.raises(NotFoundError):
        service.store.get_task(stranger, task.id)


def test_second_worker_cannot_claim_active_task(service) -> None:
    task = create(service)
    claimed = service.store.claim_task(
        tenant_id=TENANT,
        task_id=task.id,
        owner="worker-one",
    )

    assert claimed.lease_owner == "worker-one"
    with pytest.raises(ConflictError, match="leased by another worker"):
        service.store.claim_task(
            tenant_id=TENANT,
            task_id=task.id,
            owner="worker-two",
        )

    service.store.release_task(
        tenant_id=TENANT,
        task_id=task.id,
        owner="worker-one",
    )
    reclaimed = service.store.claim_task(
        tenant_id=TENANT,
        task_id=task.id,
        owner="worker-two",
    )
    assert reclaimed.lease_owner == "worker-two"


def test_duplicate_effect_key_is_rejected(service) -> None:
    class FixedEffectPlanner:
        name = "fixed"

        def plan(self, _request):
            return (
                ProposedAction(
                    kind=ActionKind.SUBMIT,
                    locator=Locator(role="button", name="Commit"),
                    description="Commit one fixed business operation.",
                    effect_key="FIXED-BUSINESS-KEY",
                    expected_outcome="One external operation.",
                ),
            )

    service.create_task(
        tenant_id=TENANT,
        instruction="First request.",
        start_url=BASE_URL,
        planner=FixedEffectPlanner(),
    )

    with pytest.raises(ConflictError, match="uniqueness"):
        service.create_task(
            tenant_id=TENANT,
            instruction="Accidental duplicate request.",
            start_url=BASE_URL,
            planner=FixedEffectPlanner(),
        )
    assert service.store.verify_audit(TENANT).valid is True


def test_audit_chain_detects_tampering(service, store: DatabaseStore) -> None:
    create(service)
    assert store.verify_audit(TENANT).valid is True
    with store.engine.begin() as connection:
        connection.execute(
            text("UPDATE audit_events SET kind='tampered' WHERE sequence=1")
        )

    verification = store.verify_audit(TENANT)
    assert verification.valid is False
    assert verification.first_invalid_sequence == 1


def test_audit_chain_detects_head_tampering(service, store: DatabaseStore) -> None:
    create(service)
    with store.engine.begin() as connection:
        connection.execute(text("UPDATE tenant_ledgers SET head_hash='forged'"))

    verification = store.verify_audit(TENANT)
    assert verification.valid is False
    assert verification.first_invalid_sequence == verification.event_count + 1
