from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from effect_browser.domain import (
    ActionKind,
    BrowserReceipt,
    Locator,
    MissionPlan,
    MissionPlanStep,
    MissionStepKind,
    Observation,
    PolicyDecision,
    ProposedAction,
    RiskClass,
    digest,
    utc_now,
)
from effect_browser.observability import OperationalMetrics
from effect_browser.store import ConflictError, DatabaseStore

TENANT = UUID("71000000-0000-0000-0000-000000000001")
BASE_URL = "https://secret-target.example/form"


@pytest.fixture
def metric_store(tmp_path: Path):
    registry = CollectorRegistry()
    metrics = OperationalMetrics(registry)
    store = DatabaseStore(
        f"sqlite:///{tmp_path / 'metrics.db'}",
        metrics=metrics,
    )
    store.initialize()
    yield store, registry
    store.close()


def test_committed_mission_and_browser_transitions_emit_bounded_metrics(
    metric_store,
) -> None:
    store, registry = metric_store
    mission = store.create_mission(
        mission_id=uuid4(),
        tenant_id=TENANT,
        query="secret mission text",
        provider="secret-provider",
        plan=MissionPlan(
            summary="secret summary",
            steps=(
                MissionPlanStep(
                    key="research",
                    kind=MissionStepKind.RESEARCH,
                    instruction="secret research text",
                ),
            ),
        ),
        external_commit_authorized=False,
    )
    step = store.list_mission_steps(TENANT, mission.id)[0]
    store.claim_mission(
        tenant_id=TENANT,
        mission_id=mission.id,
        owner="test-worker",
    )
    store.start_mission_steps(
        tenant_id=TENANT,
        mission_id=mission.id,
        step_ids=(step.id,),
    )
    store.complete_mission_step(
        tenant_id=TENANT,
        mission_id=mission.id,
        step_id=step.id,
        output={"summary": "secret evidence"},
    )

    read_action = _create_action(
        store,
        ProposedAction(
            kind=ActionKind.NAVIGATE,
            url=BASE_URL,
            description="secret navigation text",
        ),
    )
    prepared_read = store.prepare_action(
        TENANT,
        read_action.id,
        _observation(),
        PolicyDecision(
            allowed=True,
            risk=RiskClass.READ,
            requires_approval=False,
            reason="secret policy explanation",
        ),
    )
    store.start_dispatch(TENANT, prepared_read.id)
    store.complete_action(TENANT, prepared_read.id, _receipt("read"))

    commit_action = _create_action(
        store,
        ProposedAction(
            kind=ActionKind.CLICK,
            locator=Locator(label="Send"),
            description="secret ambiguous click",
            target_interaction="ambiguous",
        ),
    )
    awaiting_approval = store.prepare_action(
        TENANT,
        commit_action.id,
        _observation(),
        PolicyDecision(
            allowed=True,
            risk=RiskClass.EXTERNAL_COMMIT,
            requires_approval=True,
            reason="secret approval explanation",
        ),
    )
    approved = store.approve_action(
        tenant_id=TENANT,
        action_id=awaiting_approval.id,
        expected_version=awaiting_approval.version,
        actor_id="secret-operator",
    )
    store.start_dispatch(TENANT, approved.id)
    store.mark_outcome_unknown(TENANT, approved.id, "secret failure detail")

    metrics = generate_latest(registry).decode()
    assert (
        'effect_browser_mission_step_transitions_total{kind="research",'
        'status="succeeded"} 1.0'
    ) in metrics
    assert (
        'effect_browser_mission_step_duration_seconds_count{kind="research",'
        'status="succeeded"} 1.0'
    ) in metrics
    assert (
        'effect_browser_browser_action_transitions_total{kind="navigate",'
        'risk="read",status="succeeded"} 1.0'
    ) in metrics
    assert (
        'effect_browser_browser_action_duration_seconds_count{kind="click",'
        'risk="external_commit",status="outcome_unknown"} 1.0'
    ) in metrics
    assert (
        'effect_browser_external_commit_dispatch_attempts_total{kind="click"} 1.0'
    ) in metrics
    assert 'effect_browser_outcome_unknown_transitions_total{kind="click"} 1.0' in metrics
    for sensitive in (
        str(TENANT),
        BASE_URL,
        "secret",
        "secret-provider",
        "secret-operator",
    ):
        assert sensitive not in metrics


def test_metric_delivery_happens_only_after_commit_and_cannot_fail_the_domain(
    tmp_path: Path,
) -> None:
    class RecordingMetrics:
        def __init__(self) -> None:
            self.events = []

        def observe_committed(self, event) -> None:
            self.events.append(event)

    recorder = RecordingMetrics()
    store = DatabaseStore(
        f"sqlite:///{tmp_path / 'committed.db'}",
        metrics=recorder,
    )
    store.initialize()
    mission_id = uuid4()
    plan = MissionPlan(
        summary="One step",
        steps=(
            MissionPlanStep(
                key="research",
                kind=MissionStepKind.RESEARCH,
                instruction="Research",
            ),
        ),
    )
    store.create_mission(
        mission_id=mission_id,
        tenant_id=TENANT,
        query="Research",
        provider="test",
        plan=plan,
        external_commit_authorized=False,
    )
    assert [event.kind for event in recorder.events] == ["mission.created"]

    with pytest.raises(ConflictError):
        store.create_mission(
            mission_id=mission_id,
            tenant_id=TENANT,
            query="Duplicate",
            provider="test",
            plan=plan,
            external_commit_authorized=False,
        )
    assert [event.kind for event in recorder.events] == ["mission.created"]
    store.close()

    class BrokenMetrics:
        def observe_committed(self, _event) -> None:
            raise RuntimeError("collector unavailable")

    broken = DatabaseStore(
        f"sqlite:///{tmp_path / 'broken.db'}",
        metrics=BrokenMetrics(),
    )
    broken.initialize()
    created = broken.create_mission(
        mission_id=uuid4(),
        tenant_id=TENANT,
        query="Still commit the domain transaction",
        provider="test",
        plan=plan,
        external_commit_authorized=False,
    )
    assert broken.get_mission(TENANT, created.id).id == created.id
    broken.close()


def _create_action(store: DatabaseStore, proposal: ProposedAction):
    task = store.create_task(
        task_id=uuid4(),
        tenant_id=TENANT,
        instruction="secret task text",
        start_url=BASE_URL,
        provider="secret-provider",
        actions=(proposal,),
    )
    action = store.current_action(TENANT, task.id)
    assert action is not None
    return action


def _observation() -> Observation:
    return Observation(
        url=BASE_URL,
        title="secret title",
        state_sha256=digest({"secret": "page text"}),
        captured_at=utc_now(),
    )


def _receipt(external_id: str) -> BrowserReceipt:
    return BrowserReceipt(
        external_id=external_id,
        url=BASE_URL,
        evidence_sha256=digest({"receipt": external_id}),
        captured_at=utc_now(),
    )
