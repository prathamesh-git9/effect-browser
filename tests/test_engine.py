from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from effect_browser.domain import (
    ActionKind,
    ActionState,
    AutonomyMode,
    AutonomyScope,
    BrowserAction,
    BrowserReceipt,
    ElementCandidate,
    Locator,
    Observation,
    OutgoingReview,
    PageSnapshot,
    PolicyDecision,
    ProposedAction,
    ReconciliationSpec,
    RiskClass,
    StepChoice,
    TaskStatus,
    digest,
    utc_now,
)
from effect_browser.engine import CrashAfterCommitDriver, SimulatedProcessCrash
from effect_browser.providers import DeterministicPlanner, ReactiveBootstrapPlanner
from effect_browser.providers.base import ProviderError
from effect_browser.store import ConflictError, DatabaseStore, NotFoundError
from effect_browser.transmission import (
    TransmissionBlocked,
    TransmissionReviewError,
    fingerprint_request,
)

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


def test_reversible_transmission_block_does_not_claim_approval_drift(service) -> None:
    class BlockingNavigationDriver(FakeDriver):
        def execute(self, _action: ProposedAction) -> BrowserReceipt:
            raise TransmissionBlocked("synthetic page-load telemetry was blocked")

    task = create(service)

    result = service.run(
        tenant_id=TENANT,
        task_id=task.id,
        driver=BlockingNavigationDriver(RemoteSystem()),
    )

    assert result.message == (
        "browser action triggered an unreviewed request and was blocked before "
        "transmission"
    )
    assert "approval" not in result.message


def test_normal_run_never_follows_off_origin_reconciliation(service) -> None:
    class OffOriginPlanner:
        name = "off-origin-reconciliation"

        @staticmethod
        def plan(request):
            reference = f"OFF-{str(request.task_id)[:8]}"
            return (
                ProposedAction(
                    kind=ActionKind.NAVIGATE,
                    url=BASE_URL,
                    description="Open the controlled target.",
                ),
                ProposedAction(
                    kind=ActionKind.SUBMIT,
                    locator=Locator(role="button", name="Commit once"),
                    description="Commit one controlled effect.",
                    effect_key=reference,
                    expected_outcome="One controlled effect.",
                    reconciliation=ReconciliationSpec(
                        url="https://untrusted.example.test/echo",
                        expected_text=reference,
                        external_reference=reference,
                    ),
                ),
                ProposedAction(
                    kind=ActionKind.FINISH,
                    description="Finish only with authoritative evidence.",
                ),
            )

    class ReconcileProbe(FakeDriver):
        reconciliations = 0

        def reconcile(self, spec):
            self.reconciliations += 1
            return super().reconcile(spec)

    remote = RemoteSystem()
    task = service.create_task(
        tenant_id=TENANT,
        instruction="Never leak a receipt lookup across the origin boundary.",
        start_url=BASE_URL,
        planner=OffOriginPlanner(),
    )
    first = ReconcileProbe(remote)
    paused = service.run(tenant_id=TENANT, task_id=task.id, driver=first)
    action = paused.next_action
    assert action is not None
    service.store.approve_action(
        tenant_id=TENANT,
        action_id=action.id,
        expected_version=action.version,
        actor_id="test-operator",
    )

    second = ReconcileProbe(remote)
    stopped = service.run(tenant_id=TENANT, task_id=task.id, driver=second)

    assert remote.commits == 1
    assert second.reconciliations == 0
    assert stopped.next_action is not None
    assert stopped.next_action.state is ActionState.OUTCOME_UNKNOWN
    assert "origin is not allowed" in stopped.message


def test_rehydration_replays_reversible_option_clicks(service) -> None:
    class OptionPlanner:
        name = "option-replay"

        @staticmethod
        def plan(request):
            reference = f"OPTION-{str(request.task_id)[:8]}"
            return (
                ProposedAction(
                    kind=ActionKind.NAVIGATE,
                    url=BASE_URL,
                    description="Open the controlled target.",
                ),
                ProposedAction(
                    kind=ActionKind.CLICK,
                    locator=Locator(role="option", name="Dublin"),
                    description="Select the observed reversible option.",
                    target_interaction="option",
                ),
                ProposedAction(
                    kind=ActionKind.SUBMIT,
                    locator=Locator(role="button", name="Commit once"),
                    description="Commit the option-bound effect.",
                    effect_key=reference,
                    expected_outcome="One controlled effect.",
                    reconciliation=ReconciliationSpec(
                        url=f"{BASE_URL}/receipt",
                        expected_text=reference,
                        external_reference=reference,
                    ),
                ),
                ProposedAction(
                    kind=ActionKind.FINISH,
                    description="Finish with authoritative evidence.",
                ),
            )

    class OptionDriver(FakeDriver):
        option_clicks = 0

        def execute(self, action):
            if action.kind is ActionKind.CLICK:
                self.option_clicks += 1
            return super().execute(action)

    remote = RemoteSystem()
    task = service.create_task(
        tenant_id=TENANT,
        instruction="Replay reversible option state after restart.",
        start_url=BASE_URL,
        planner=OptionPlanner(),
    )
    first = OptionDriver(remote)
    paused = service.run(tenant_id=TENANT, task_id=task.id, driver=first)
    assert first.option_clicks == 1
    action = paused.next_action
    assert action is not None
    service.store.approve_action(
        tenant_id=TENANT,
        action_id=action.id,
        expected_version=action.version,
        actor_id="test-operator",
    )

    restarted = OptionDriver(remote)
    result = service.run(tenant_id=TENANT, task_id=task.id, driver=restarted)

    assert restarted.option_clicks == 1
    assert result.task.status is TaskStatus.SUCCEEDED


def test_zero_request_submit_preview_can_be_audited_as_superseded(service) -> None:
    class NativeValidationDriver(FakeDriver):
        def preview_submit(self, action, observation_sha256):
            raise TransmissionReviewError(
                "exact review requires one outgoing request; the submit produced 0"
            )

    task = create(service)
    stopped = service.run(
        tenant_id=TENANT,
        task_id=task.id,
        driver=NativeValidationDriver(RemoteSystem()),
    )
    failed = stopped.next_action
    assert failed is not None
    assert failed.state is ActionState.FAILED

    superseded = service.store.supersede_failed_submit_preview(
        tenant_id=TENANT,
        action_id=failed.id,
        expected_version=failed.version,
        actor_id="test-operator",
        authorization_basis="Native validation produced no request.",
    )

    assert superseded.state is ActionState.SUPERSEDED
    resumed = service.store.get_task(TENANT, task.id)
    assert resumed.current_ordinal == failed.ordinal + 1
    assert resumed.status is TaskStatus.QUEUED
    with service.store.engine.connect() as connection:
        released = connection.execute(
            text("SELECT effect_key FROM actions WHERE id=:action_id"),
            {"action_id": str(failed.id)},
        ).scalar_one()
    assert released is None
    assert service.store.verify_audit(TENANT).valid


def test_store_atomically_supersedes_an_invalidated_hash_bound_proposal(store) -> None:
    task_id = uuid4()
    proposal = ProposedAction(
        kind=ActionKind.WAIT,
        wait_ms=100,
        description="Wait against one observed page state.",
        planned_from_sha256="a" * 64,
    )
    store.create_task(
        task_id=task_id,
        tenant_id=TENANT,
        instruction="Replan if the observed page changes.",
        start_url=BASE_URL,
        provider="reactive-store-test",
        actions=(proposal,),
    )
    action = store.current_action(TENANT, task_id)
    assert action is not None
    fresh = Observation(
        url=BASE_URL,
        title="Changed page",
        state_sha256="b" * 64,
        captured_at=utc_now(),
    )
    prepared = store.prepare_action(
        TENANT,
        action.id,
        Observation(
            url=BASE_URL,
            title="Original page",
            state_sha256="a" * 64,
            captured_at=utc_now(),
        ),
        PolicyDecision(
            allowed=True,
            risk=RiskClass.READ,
            requires_approval=False,
            reason="Synthetic reversible action.",
        ),
    )
    invalidated = store.invalidate_approval(
        TENANT,
        prepared.id,
        fresh.state_sha256,
    )
    assert invalidated.state is ActionState.INVALIDATED

    superseded = store.supersede_stale_proposal(
        tenant_id=TENANT,
        action_id=invalidated.id,
        expected_version=invalidated.version,
        observation=fresh,
    )

    advanced = store.get_task(TENANT, task_id)
    assert superseded.state is ActionState.SUPERSEDED
    assert superseded.failure == "page changed after reactive planning before dispatch"
    assert superseded.observation_sha256 == fresh.state_sha256
    assert advanced.current_ordinal == 1
    assert advanced.status is TaskStatus.QUEUED
    assert store.current_action(TENANT, task_id) is None
    event = store.events(TENANT, task_id)[-1]
    assert event.kind == "action.stale_proposal_superseded"
    assert event.payload["expected_observation_sha256"] == "a" * 64
    assert event.payload["actual_observation_sha256"] == "b" * 64
    assert event.payload["automatic_retry"] is False
    assert store.verify_audit(TENANT).valid


def test_stale_supersession_rechecks_the_stored_action_hash(store) -> None:
    task_id = uuid4()
    proposal = ProposedAction(
        kind=ActionKind.WAIT,
        wait_ms=100,
        description="Hash-bound reactive wait.",
        planned_from_sha256="a" * 64,
    )
    store.create_task(
        task_id=task_id,
        tenant_id=TENANT,
        instruction="Never supersede a tampered proposal.",
        start_url=BASE_URL,
        provider="reactive-store-hash-test",
        actions=(proposal,),
    )
    action = store.current_action(TENANT, task_id)
    assert action is not None
    with store.engine.begin() as connection:
        connection.execute(
            text("UPDATE actions SET action_sha256=:forged WHERE id=:action_id"),
            {"forged": "f" * 64, "action_id": str(action.id)},
        )

    with pytest.raises(ConflictError, match="stored action no longer matches"):
        store.supersede_stale_proposal(
            tenant_id=TENANT,
            action_id=action.id,
            expected_version=action.version,
            observation=Observation(
                url=BASE_URL,
                title="Changed page",
                state_sha256="b" * 64,
                captured_at=utc_now(),
            ),
        )

    unchanged = store.get_task(TENANT, task_id)
    assert unchanged.current_ordinal == 0
    assert store.get_action(TENANT, action.id).state is ActionState.PENDING


def test_bounded_autonomy_commits_without_per_action_intervention(service) -> None:
    remote = RemoteSystem()
    task = service.create_task(
        tenant_id=TENANT,
        instruction="Order once under an explicit unattended task scope.",
        start_url=BASE_URL,
        planner=DeterministicPlanner(),
        autonomy=AutonomyScope(
            mode=AutonomyMode.BOUNDED,
            allow_external_commits=True,
            max_external_commits=1,
        ),
    )

    reviewed = service.run(
        tenant_id=TENANT,
        task_id=task.id,
        driver=FakeDriver(remote),
    )
    assert reviewed.next_action is not None
    assert reviewed.next_action.state is ActionState.PREPARED

    result = service.run(
        tenant_id=TENANT,
        task_id=task.id,
        driver=FakeDriver(remote),
    )

    assert result.task.status is TaskStatus.SUCCEEDED
    assert remote.commits == 1
    submit = next(
        action
        for action in service.store.list_actions(TENANT, task.id)
        if action.proposal.kind is ActionKind.SUBMIT
    )
    approval = service.store.latest_approval(TENANT, submit.id)
    assert approval is not None
    assert approval.actor_id.startswith("bounded-autonomy:")
    assert approval.payload_sha256 == submit.proposal.outgoing_review.payload_sha256
    approved_event = next(
        event
        for event in service.store.events(TENANT, task.id)
        if event.kind == "action.approved"
    )
    assert "exact reviewed request" in approved_event.payload["authorization_basis"]
    assert service.store.verify_audit(TENANT).valid


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


def test_block_task_records_terminal_handoff_and_keeps_audit_intact(service) -> None:
    task = create(service)

    blocked = service.store.block_task(
        TENANT,
        task.id,
        kind="captcha",
        reason="a CAPTCHA challenge requires a human to proceed",
        evidence="candidate 'turnstile' matched 'cf-turnstile'",
    )

    assert blocked.status is TaskStatus.BLOCKED
    events = service.store.events(TENANT, task.id)
    blocked_events = [event for event in events if event.kind == "task.blocked"]
    assert len(blocked_events) == 1
    payload = blocked_events[0].payload
    assert payload["challenge"] == "captcha"
    assert payload["automatic_progress"] is False
    assert service.store.verify_audit(TENANT).valid

    # A blocked task is terminal: it must not be blocked or re-run again.
    with pytest.raises(ConflictError):
        service.store.block_task(
            TENANT,
            task.id,
            kind="mfa",
            reason="second attempt",
            evidence="ignored",
        )
    rerun = service.run(
        tenant_id=TENANT, task_id=task.id, driver=FakeDriver(RemoteSystem())
    )
    assert rerun.task.status is TaskStatus.BLOCKED
    assert "already blocked" in rerun.message


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


def test_delayed_dispatch_false_negative_requires_external_receipt(service) -> None:
    task, approved = prepare_and_approve(service, RemoteSystem())
    service.store.start_dispatch(TENANT, approved.id)
    failed = service.store.fail_action(
        TENANT,
        approved.id,
        (
            "exact request was blocked before transmission: approved outgoing "
            "request was not produced; nothing was sent"
        ),
    )
    receipt = BrowserReceipt(
        external_id="confirmation-123",
        url=f"{BASE_URL}/confirmation",
        evidence_sha256=digest({"confirmation": "Application received"}),
        captured_at=utc_now(),
    )

    recovered = service.store.recover_false_pretransmission_failure(
        tenant_id=TENANT,
        action_id=failed.id,
        receipt=receipt,
        actor_id="test-reconciler",
        authorization_basis="A synthetic authoritative confirmation was observed.",
    )

    assert recovered.state is ActionState.SUCCEEDED
    assert service.store.get_receipt(TENANT, failed.id) == receipt
    assert service.store.get_task(TENANT, task.id).status in {
        TaskStatus.QUEUED,
        TaskStatus.SUCCEEDED,
    }
    assert service.store.verify_audit(TENANT).valid


def test_page_drift_invalidates_previous_approval(service) -> None:
    remote = RemoteSystem()
    task, approved = prepare_and_approve(service, remote)
    remote.page_revision += 1

    result = service.run(tenant_id=TENANT, task_id=task.id, driver=FakeDriver(remote))

    assert result.next_action is not None
    assert result.next_action.state is ActionState.INVALIDATED
    assert remote.commits == 0
    assert service.store.latest_approval(TENANT, approved.id) is not None

    reviewed_again = service.run(
        tenant_id=TENANT,
        task_id=task.id,
        driver=FakeDriver(remote),
    )
    assert reviewed_again.next_action is not None
    assert reviewed_again.next_action.state is ActionState.APPROVAL_REQUIRED
    assert remote.commits == 0


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


def test_payload_approval_is_persisted_and_rechecked_before_dispatch(service) -> None:
    observation_sha256 = "a" * 64
    review_body = {
        "fields": [],
        "document_sha256s": [],
        "observation_sha256": observation_sha256,
    }
    base_review = OutgoingReview(
        observation_sha256=observation_sha256,
        payload_sha256=digest(review_body),
    )
    review = base_review.bind_requests(
        (
            fingerprint_request(
                method="POST",
                url=f"{BASE_URL}/applications",
                headers={"content-type": "application/json"},
                body=b"{}",
            ),
        )
    )

    class ReviewedSubmitPlanner:
        name = "reviewed-submit"

        def plan(self, _request):
            return (
                ProposedAction(
                    kind=ActionKind.SUBMIT,
                    locator=Locator(role="button", name="Submit application"),
                    description="Submit the exact reviewed payload.",
                    effect_key="REVIEWED-PAYLOAD",
                    expected_outcome="One reviewed application.",
                    planned_from_sha256=observation_sha256,
                    outgoing_review=review,
                ),
            )

    task = service.create_task(
        tenant_id=TENANT,
        instruction="Bind approval to the reviewed payload.",
        start_url=BASE_URL,
        planner=ReviewedSubmitPlanner(),
    )
    action = service.store.current_action(TENANT, task.id)
    assert action is not None
    prepared = service.store.prepare_action(
        TENANT,
        action.id,
        Observation(
            url=BASE_URL,
            title="Synthetic review",
            state_sha256=observation_sha256,
            captured_at=utc_now(),
        ),
        PolicyDecision(
            allowed=True,
            risk=RiskClass.EXTERNAL_COMMIT,
            requires_approval=True,
            reason="reviewed payload requires approval",
        ),
    )
    service.store.approve_action(
        tenant_id=TENANT,
        action_id=prepared.id,
        expected_version=prepared.version,
        actor_id="test-operator",
    )

    approval = service.store.latest_approval(TENANT, action.id)
    assert approval is not None
    assert approval.action_sha256 == action.action_sha256
    assert approval.observation_sha256 == observation_sha256
    assert approval.payload_sha256 == review.payload_sha256
    approved_event = service.store.events(TENANT, task.id)[-1]
    assert approved_event.payload["payload_sha256"] == review.payload_sha256

    with service.store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE approvals SET payload_sha256=:forged WHERE action_id=:action_id"
            ),
            {"forged": "f" * 64, "action_id": str(action.id)},
        )

    with pytest.raises(ConflictError, match="exact action, payload, or page"):
        service.store.start_dispatch(TENANT, action.id)


def test_dispatch_recomputes_the_stored_action_hash(service) -> None:
    remote = RemoteSystem()
    _task, action = prepare_and_approve(service, remote)
    with service.store.engine.begin() as connection:
        connection.execute(
            text("UPDATE actions SET action_sha256=:forged WHERE id=:action_id"),
            {"forged": "f" * 64, "action_id": str(action.id)},
        )

    with pytest.raises(ConflictError, match="stored action no longer matches"):
        service.store.start_dispatch(TENANT, action.id)


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


def test_rehydration_never_replays_a_completed_upload(
    service, monkeypatch, tmp_path: Path
) -> None:
    task_id = uuid4()
    navigate = ProposedAction(
        kind=ActionKind.NAVIGATE,
        url=BASE_URL,
        description="Restore safe navigation.",
    )
    upload = ProposedAction(
        kind=ActionKind.UPLOAD,
        locator=Locator(label="Résumé"),
        file_path=(tmp_path / "resume.pdf").resolve(),
        document_sha256="a" * 64,
        description="Do not replay document transmission.",
    )
    history = [
        BrowserAction(
            id=uuid4(),
            tenant_id=TENANT,
            task_id=task_id,
            ordinal=0,
            proposal=navigate,
            state=ActionState.SUCCEEDED,
            action_sha256=navigate.action_hash(),
            version=1,
        ),
        BrowserAction(
            id=uuid4(),
            tenant_id=TENANT,
            task_id=task_id,
            ordinal=1,
            proposal=upload,
            state=ActionState.SUCCEEDED,
            action_sha256=upload.action_hash(),
            version=1,
        ),
    ]
    executed: list[ActionKind] = []

    class RecordingDriver:
        def execute(self, action: ProposedAction):
            executed.append(action.kind)
            return None

    monkeypatch.setattr(service.store, "list_actions", lambda *_args: history)

    service._rehydrate(TENANT, task_id, RecordingDriver(), current_ordinal=2)

    assert executed == [ActionKind.NAVIGATE]


def test_rehydration_skips_actions_covered_by_session_checkpoint(
    service, monkeypatch
) -> None:
    task_id = uuid4()
    navigate = ProposedAction(
        kind=ActionKind.NAVIGATE,
        url=BASE_URL,
        description="Restore the page using persisted browser state.",
    )
    consent = ProposedAction(
        kind=ActionKind.CLICK,
        locator=Locator(selector="#reject-all", adaptive_id="reject-all:0"),
        description="Reject optional cookies once.",
        target_interaction="consent",
        target_name="Reject all",
    )
    history = [
        BrowserAction(
            id=uuid4(),
            tenant_id=TENANT,
            task_id=task_id,
            ordinal=0,
            proposal=navigate,
            state=ActionState.SUCCEEDED,
            action_sha256=navigate.action_hash(),
            version=1,
        ),
        BrowserAction(
            id=uuid4(),
            tenant_id=TENANT,
            task_id=task_id,
            ordinal=1,
            proposal=consent,
            state=ActionState.SUCCEEDED,
            action_sha256=consent.action_hash(),
            version=1,
        ),
    ]
    executed: list[ActionKind] = []

    class RecordingDriver:
        restored_checkpoint_ordinal = 2

        def execute(self, action: ProposedAction):
            executed.append(action.kind)
            return None

    monkeypatch.setattr(service.store, "list_actions", lambda *_args: history)

    service._rehydrate(TENANT, task_id, RecordingDriver(), current_ordinal=2)

    assert executed == [ActionKind.NAVIGATE]


def test_rehydration_replays_consent_after_older_session_checkpoint(
    service, monkeypatch
) -> None:
    task_id = uuid4()
    consent = ProposedAction(
        kind=ActionKind.CLICK,
        locator=Locator(selector="#reject-all", adaptive_id="reject-all:0"),
        description="Restore consent recorded after the durable checkpoint.",
        target_interaction="consent",
        target_name="Reject all",
    )
    history = [
        BrowserAction(
            id=uuid4(),
            tenant_id=TENANT,
            task_id=task_id,
            ordinal=1,
            proposal=consent,
            state=ActionState.SUCCEEDED,
            action_sha256=consent.action_hash(),
            version=1,
        )
    ]
    executed: list[ActionKind] = []

    class RecordingDriver:
        restored_checkpoint_ordinal = 1

        def execute(self, action: ProposedAction):
            executed.append(action.kind)
            return None

    monkeypatch.setattr(service.store, "list_actions", lambda *_args: history)

    service._rehydrate(TENANT, task_id, RecordingDriver(), current_ordinal=2)

    assert executed == [ActionKind.CLICK]


def test_reactive_task_requires_verified_fact_before_planner_can_guess(
    service,
) -> None:
    class PlannerMustNotRun:
        name = "profile-test"

        def choose(self, _request):
            raise AssertionError("planner must not see an unresolved identity field")

    class ProfileDriver(FakeDriver):
        def snapshot(self) -> PageSnapshot:
            observation = self.observe()
            return PageSnapshot(
                url=self.url,
                title="Synthetic application",
                state_sha256=observation.state_sha256,
                text_excerpt="Application",
                candidates=(
                    ElementCandidate(
                        id="C001",
                        tag="input",
                        role="textbox",
                        name="Full name",
                        input_type="text",
                        required=True,
                        interaction="input",
                        locator=Locator(
                            selector="body > input",
                            adaptive_id="candidate-name:0",
                        ),
                    ),
                ),
                captured_at=utc_now(),
            )

    service.step_planners = {"profile-test": PlannerMustNotRun()}
    task = service.create_task(
        tenant_id=TENANT,
        instruction="Prepare a synthetic application without inventing identity.",
        start_url=BASE_URL,
        planner=ReactiveBootstrapPlanner("profile-test"),
    )

    result = service.run(
        tenant_id=TENANT,
        task_id=task.id,
        driver=ProfileDriver(RemoteSystem()),
    )

    assert result.task.status is TaskStatus.AWAITING_INPUT
    assert result.next_action is not None
    assert result.next_action.state is ActionState.INPUT_REQUIRED
    assert result.next_action.proposal.kind is ActionKind.HANDOFF
    assert "full_name" in result.message

    resumed = service.store.resolve_input(
        tenant_id=TENANT,
        action_id=result.next_action.id,
        expected_version=result.next_action.version,
        actor_id="test-operator",
    )
    assert resumed.state is ActionState.SUCCEEDED
    assert service.store.get_task(TENANT, task.id).status is TaskStatus.QUEUED


def test_reactive_provider_failure_becomes_truthful_blocker(service) -> None:
    class FailingPlanner:
        name = "failing-reactive"

        def choose(self, _request):
            raise ProviderError("synthetic provider outage")

    class SnapshotDriver(FakeDriver):
        def snapshot(self) -> PageSnapshot:
            observation = self.observe()
            return PageSnapshot(
                url=observation.url,
                title=observation.title,
                state_sha256=observation.state_sha256,
                text_excerpt="No challenge and no controls.",
                candidates=(),
                captured_at=observation.captured_at,
            )

    service.step_planners = {"failing-reactive": FailingPlanner()}
    task = service.create_task(
        tenant_id=TENANT,
        instruction="Stop honestly if planning is unavailable.",
        start_url=BASE_URL,
        planner=ReactiveBootstrapPlanner("failing-reactive"),
    )

    result = service.run(
        tenant_id=TENANT,
        task_id=task.id,
        driver=SnapshotDriver(RemoteSystem()),
    )

    assert result.task.status is TaskStatus.BLOCKED
    assert result.next_action is None
    assert "success is not claimed" in result.message


def test_stale_reactive_proposal_is_never_executed_and_replanning_succeeds(
    service,
) -> None:
    class DriftThenFinishPlanner:
        name = "drift-then-finish"

        def __init__(self) -> None:
            self.calls = 0

        def choose(self, _request):
            self.calls += 1
            if self.calls == 1:
                return StepChoice(
                    kind=ActionKind.WAIT,
                    wait_ms=100,
                    description="Wait using the stale proposal.",
                )
            return StepChoice(
                kind=ActionKind.FINISH,
                description="Finish after planning from a stable observation.",
            )

    class DriftOnceDriver(FakeDriver):
        def __init__(self, remote: RemoteSystem) -> None:
            super().__init__(remote)
            self.snapshot_calls = 0
            self.executed: list[ActionKind] = []

        def snapshot(self) -> PageSnapshot:
            self.snapshot_calls += 1
            observation = self.observe()
            planned_sha256 = (
                digest({"synthetic_stale_snapshot": self.snapshot_calls})
                if self.snapshot_calls == 1
                else observation.state_sha256
            )
            return PageSnapshot(
                url=observation.url,
                title=observation.title,
                state_sha256=planned_sha256,
                text_excerpt="Stable synthetic page.",
                candidates=(),
                captured_at=observation.captured_at,
            )

        def execute(self, action: ProposedAction) -> BrowserReceipt:
            self.executed.append(action.kind)
            return super().execute(action)

    planner = DriftThenFinishPlanner()
    service.step_planners = {planner.name: planner}
    task = service.create_task(
        tenant_id=TENANT,
        instruction="Finish after safely replanning a stale browser action.",
        start_url=BASE_URL,
        planner=ReactiveBootstrapPlanner(planner.name),
    )
    driver = DriftOnceDriver(RemoteSystem())

    result = service.run(tenant_id=TENANT, task_id=task.id, driver=driver)

    actions = service.store.list_actions(TENANT, task.id)
    stale = next(action for action in actions if action.proposal.kind is ActionKind.WAIT)
    assert result.task.status is TaskStatus.SUCCEEDED
    assert planner.calls == 2
    assert driver.executed == [ActionKind.NAVIGATE]
    assert stale.state is ActionState.SUPERSEDED
    assert all(
        event.kind != "action.dispatching" or event.action_id != stale.id
        for event in service.store.events(TENANT, task.id)
    )
    assert any(
        event.kind == "action.stale_proposal_superseded"
        and event.action_id == stale.id
        and event.payload["automatic_retry"] is False
        for event in service.store.events(TENANT, task.id)
    )


def test_reactive_instability_is_bounded_to_three_automatic_replans(service) -> None:
    class AlwaysWaitPlanner:
        name = "always-drifting"

        def __init__(self) -> None:
            self.calls = 0

        def choose(self, _request):
            self.calls += 1
            return StepChoice(
                kind=ActionKind.WAIT,
                wait_ms=100,
                description=f"Stale wait proposal {self.calls}.",
            )

    class AlwaysDriftingDriver(FakeDriver):
        def __init__(self, remote: RemoteSystem) -> None:
            super().__init__(remote)
            self.snapshot_calls = 0
            self.executed: list[ActionKind] = []

        def snapshot(self) -> PageSnapshot:
            self.snapshot_calls += 1
            observation = self.observe()
            return PageSnapshot(
                url=observation.url,
                title=observation.title,
                state_sha256=digest({"synthetic_stale_snapshot": self.snapshot_calls}),
                text_excerpt="Continuously changing synthetic page.",
                candidates=(),
                captured_at=observation.captured_at,
            )

        def execute(self, action: ProposedAction) -> BrowserReceipt:
            self.executed.append(action.kind)
            return super().execute(action)

    planner = AlwaysWaitPlanner()
    service.step_planners = {planner.name: planner}
    task = service.create_task(
        tenant_id=TENANT,
        instruction="Stop after bounded replanning on an unstable page.",
        start_url=BASE_URL,
        planner=ReactiveBootstrapPlanner(planner.name),
    )
    driver = AlwaysDriftingDriver(RemoteSystem())

    result = service.run(tenant_id=TENANT, task_id=task.id, driver=driver)

    actions = service.store.list_actions(TENANT, task.id)
    stale_actions = [
        action
        for action in actions
        if action.failure == "page changed after reactive planning before dispatch"
    ]
    events = service.store.events(TENANT, task.id)
    assert result.task.status is TaskStatus.BLOCKED
    assert planner.calls == 4
    assert len(stale_actions) == 4
    assert all(action.state is ActionState.SUPERSEDED for action in stale_actions)
    assert driver.executed == [ActionKind.NAVIGATE]
    assert sum(event.kind == "action.stale_proposal_superseded" for event in events) == 4
    assert not any(
        event.kind == "action.dispatching"
        and event.action_id in {action.id for action in stale_actions}
        for event in events
    )
    blocked = next(event for event in events if event.kind == "task.blocked")
    assert blocked.payload["challenge"] == "reactive_page_instability"
    assert "no stale proposal was dispatched" in result.message


def test_reactive_engine_stops_before_planning_on_payment_fields(service) -> None:
    class MustNotPlanPayment:
        name = "payment-stop"

        def choose(self, _request):
            raise AssertionError("payment fields must stop before a provider call")

    class PaymentDriver(FakeDriver):
        def snapshot(self) -> PageSnapshot:
            observation = self.observe()
            return PageSnapshot(
                url=observation.url,
                title="Payment",
                state_sha256=observation.state_sha256,
                text_excerpt="Enter payment details to continue.",
                candidates=(
                    ElementCandidate(
                        id="C001",
                        tag="input",
                        role="textbox",
                        name="Card number",
                        input_type="text",
                        interaction="input",
                        locator=Locator(
                            selector="#generated-field",
                            adaptive_id="generated-field:0",
                        ),
                    ),
                ),
                captured_at=observation.captured_at,
            )

    service.step_planners = {"payment-stop": MustNotPlanPayment()}
    task = service.create_task(
        tenant_id=TENANT,
        instruction="Prepare the journey only until payment is requested.",
        start_url=BASE_URL,
        planner=ReactiveBootstrapPlanner("payment-stop"),
    )

    result = service.run(
        tenant_id=TENANT,
        task_id=task.id,
        driver=PaymentDriver(RemoteSystem()),
    )

    assert result.task.status is TaskStatus.BLOCKED
    assert "payment boundary" in result.message
    event = next(
        event
        for event in service.store.events(TENANT, task.id)
        if event.kind == "task.blocked"
    )
    assert event.kind == "task.blocked"
    assert event.payload["challenge"] == "payment"


def test_reactive_step_budget_blocks_instead_of_overflowing_schema(service) -> None:
    class ThirtyStepPlanner:
        name = "step-budget"

        def plan(self, _request):
            return (
                ProposedAction(
                    kind=ActionKind.NAVIGATE,
                    url=BASE_URL,
                    description="Open the bounded target.",
                ),
                *(
                    ProposedAction(
                        kind=ActionKind.WAIT,
                        wait_ms=100,
                        description=f"Bounded wait {number}.",
                    )
                    for number in range(1, 30)
                ),
            )

    class MustNotChoose:
        name = "step-budget"

        def choose(self, _request):
            raise AssertionError("provider must not run after the 30-step budget")

    service.step_planners = {"step-budget": MustNotChoose()}
    task = service.create_task(
        tenant_id=TENANT,
        instruction="Never run more than thirty browser steps.",
        start_url=BASE_URL,
        planner=ThirtyStepPlanner(),
    )

    result = service.run(
        tenant_id=TENANT,
        task_id=task.id,
        driver=FakeDriver(RemoteSystem()),
    )

    assert result.task.status is TaskStatus.BLOCKED
    assert "step budget exhausted" in result.message
