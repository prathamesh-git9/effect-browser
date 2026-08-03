from __future__ import annotations

from effect_browser.domain import ActionKind
from effect_browser.engine import EffectBrowserService
from effect_browser.policy import ActionPolicy
from effect_browser.providers import DeterministicPlanner
from effect_browser.session import SessionStateProtector

from .conftest import BASE_URL, TENANT, FakeDriver, RemoteSystem


class SessionDriver(FakeDriver):
    def __init__(self, remote: RemoteSystem) -> None:
        super().__init__(remote)
        self.restored_checkpoint_ordinal = 0
        self.executed: list[ActionKind] = []

    def restore_storage_state(
        self,
        storage_state: dict[str, object],
        checkpoint_ordinal: int,
    ) -> None:
        self.restored_checkpoint_ordinal = checkpoint_ordinal
        self.url = str(storage_state["fake_url"])
        self.values = dict(storage_state["fake_values"])

    def export_storage_state(self) -> dict[str, object]:
        return {
            "cookies": [],
            "origins": [],
            "fake_url": self.url,
            "fake_values": self.values,
        }

    def execute(self, action):
        self.executed.append(action.kind)
        return super().execute(action)


def test_service_restores_encrypted_checkpoint_and_deletes_it_at_terminal_state(
    store,
) -> None:
    protector = SessionStateProtector(encryption_key=b"s" * 32)
    service = EffectBrowserService(
        store,
        ActionPolicy((BASE_URL,)),
        session_protector=protector,
    )
    remote = RemoteSystem()
    task = service.create_task(
        tenant_id=TENANT,
        instruction="Place one test order.",
        start_url=BASE_URL,
        planner=DeterministicPlanner(),
    )

    first = SessionDriver(remote)
    paused = service.run(tenant_id=TENANT, task_id=task.id, driver=first)
    checkpoint = store.load_task_session(TENANT, task.id)

    assert paused.next_action is not None
    assert checkpoint is not None
    assert checkpoint.checkpoint_ordinal == 4
    assert b"backup-drive" not in checkpoint.ciphertext

    approved = store.approve_action(
        tenant_id=TENANT,
        action_id=paused.next_action.id,
        expected_version=paused.next_action.version,
        actor_id="session-test",
    )
    second = SessionDriver(remote)
    completed = service.run(tenant_id=TENANT, task_id=task.id, driver=second)

    assert approved.ordinal == 4
    assert second.restored_checkpoint_ordinal == 4
    assert second.executed == [
        ActionKind.NAVIGATE,
        ActionKind.FILL,
        ActionKind.FILL,
        ActionKind.FILL,
        ActionKind.SUBMIT,
    ]
    assert completed.task.status.value == "succeeded"
    assert remote.commits == 1
    assert store.load_task_session(TENANT, task.id) is None
