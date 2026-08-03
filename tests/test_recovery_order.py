from __future__ import annotations

from effect_browser.domain import ActionState, TaskStatus
from effect_browser.providers import DeterministicPlanner
from effect_browser.session import SessionStateProtector

from .conftest import BASE_URL, TENANT, FakeDriver, RemoteSystem


class NoBrowserTraffic:
    def __getattr__(self, name: str):
        raise AssertionError(f"recovery unexpectedly used browser method {name}")


def test_interrupted_dispatch_becomes_unknown_before_browser_rehydration(service) -> None:
    service.session_protector = SessionStateProtector(encryption_key=b"r" * 32)
    remote = RemoteSystem()
    task = service.create_task(
        tenant_id=TENANT,
        instruction="Submit once and never replay an ambiguous dispatch.",
        start_url=BASE_URL,
        planner=DeterministicPlanner(),
    )
    paused = service.run(
        tenant_id=TENANT,
        task_id=task.id,
        driver=FakeDriver(remote),
    )
    action = paused.next_action
    assert action is not None
    assert action.state is ActionState.APPROVAL_REQUIRED
    approved = service.store.approve_action(
        tenant_id=TENANT,
        action_id=action.id,
        expected_version=action.version,
        actor_id="recovery-order-test",
    )
    service.store.start_dispatch(TENANT, approved.id)

    recovered = service.run(
        tenant_id=TENANT,
        task_id=task.id,
        driver=NoBrowserTraffic(),
    )

    assert recovered.task.status is TaskStatus.AWAITING_RECOVERY
    assert recovered.next_action is not None
    assert recovered.next_action.state is ActionState.OUTCOME_UNKNOWN
    assert recovered.message == "outcome is unknown; automatic retry is disabled"
    assert remote.commits == 0
    assert service.store.get_receipt(TENANT, approved.id) is None

    repeated = service.run(
        tenant_id=TENANT,
        task_id=task.id,
        driver=NoBrowserTraffic(),
    )

    assert repeated.task.status is TaskStatus.AWAITING_RECOVERY
    assert repeated.next_action is not None
    assert repeated.next_action.state is ActionState.OUTCOME_UNKNOWN
    assert repeated.message == "outcome is unknown; reconcile or resolve it"
    assert remote.commits == 0
    unknown_events = [
        event
        for event in service.store.events(TENANT, task.id)
        if event.kind == "action.outcome_unknown"
    ]
    assert len(unknown_events) == 1
    assert unknown_events[0].payload["automatic_retry"] is False
    assert service.store.verify_audit(TENANT).valid
