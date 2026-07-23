from __future__ import annotations

from uuid import UUID, uuid4

from effect_browser.browser.base import BrowserDriver
from effect_browser.domain import (
    ActionKind,
    ActionState,
    BrowserReceipt,
    PlanRequest,
    Resolution,
    RunResult,
    StepRequest,
    Task,
    TaskStatus,
    digest,
    utc_now,
)
from effect_browser.policy import ActionPolicy
from effect_browser.providers.base import Planner, StepPlanner
from effect_browser.providers.reactive import bind_choice
from effect_browser.store import ConflictError, DatabaseStore


class SimulatedProcessCrash(BaseException):
    """Test-only crash signal that deliberately bypasses normal exception handling."""


class CrashAfterCommitDriver:
    """Delegating driver that loses the process after one commit reaches the target."""

    def __init__(self, inner: BrowserDriver) -> None:
        self.inner = inner
        self.crashed = False

    def observe(self):
        return self.inner.observe()

    def execute(self, action):
        receipt = self.inner.execute(action)
        if action.kind is ActionKind.SUBMIT and not self.crashed:
            self.crashed = True
            raise SimulatedProcessCrash("process lost after remote commit")
        return receipt

    def reconcile(self, spec):
        return self.inner.reconcile(spec)

    def close(self) -> None:
        self.inner.close()


class EffectBrowserService:
    def __init__(
        self,
        store: DatabaseStore,
        policy: ActionPolicy,
        step_planners: dict[str, StepPlanner] | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.step_planners = step_planners or {}

    def create_task(
        self,
        *,
        tenant_id: UUID,
        instruction: str,
        start_url: str,
        planner: Planner,
    ) -> Task:
        task_id = uuid4()
        request = PlanRequest(
            task_id=task_id,
            instruction=instruction,
            start_url=start_url,
        )
        actions = planner.plan(request)
        if not actions:
            raise ValueError("planner returned an empty action list")
        if len(actions) > 30:
            raise ValueError("planner returned more than 30 actions")
        return self.store.create_task(
            task_id=task_id,
            tenant_id=tenant_id,
            instruction=instruction,
            start_url=start_url,
            provider=planner.name,
            actions=actions,
        )

    def run(
        self,
        *,
        tenant_id: UUID,
        task_id: UUID,
        driver: BrowserDriver,
    ) -> RunResult:
        task = self.store.get_task(tenant_id, task_id)
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.REJECTED}:
            return RunResult(task=task, message=f"task is already {task.status.value}")
        worker_id = f"worker-{uuid4()}"
        self.store.claim_task(
            tenant_id=tenant_id,
            task_id=task_id,
            owner=worker_id,
        )
        try:
            return self._run_claimed(
                tenant_id=tenant_id,
                task_id=task_id,
                driver=driver,
                worker_id=worker_id,
            )
        finally:
            self.store.release_task(
                tenant_id=tenant_id,
                task_id=task_id,
                owner=worker_id,
            )

    def _run_claimed(
        self,
        *,
        tenant_id: UUID,
        task_id: UUID,
        driver: BrowserDriver,
        worker_id: str,
    ) -> RunResult:
        task = self.store.get_task(tenant_id, task_id)
        self._rehydrate(tenant_id, task_id, driver, task.current_ordinal)

        while True:
            self.store.renew_task_lease(
                tenant_id=tenant_id,
                task_id=task_id,
                owner=worker_id,
            )
            task = self.store.get_task(tenant_id, task_id)
            action = self.store.current_action(tenant_id, task_id)
            if action is None:
                step_planner = self.step_planners.get(task.provider)
                if step_planner is not None:
                    snapshot = driver.snapshot()
                    history = self.store.list_actions(tenant_id, task_id)
                    request = StepRequest(
                        task_id=task.id,
                        instruction=task.instruction,
                        start_url=task.start_url,
                        step_number=task.current_ordinal + 1,
                        effect_reference=f"EB-{str(task.id)[:8].upper()}",
                        previous_actions=tuple(
                            f"{item.proposal.kind.value}: {item.proposal.description}"
                            for item in history
                        ),
                        snapshot=snapshot,
                    )
                    choice = step_planner.choose(request)
                    action = self.store.append_action(
                        tenant_id=tenant_id,
                        task_id=task_id,
                        proposal=bind_choice(
                            choice,
                            snapshot,
                            effect_reference=request.effect_reference,
                        ),
                    )
                    task = self.store.get_task(tenant_id, task_id)
                else:
                    return RunResult(
                        task=task,
                        message="task has no remaining action",
                    )
            if action.state is ActionState.DISPATCHING:
                action = self.store.mark_outcome_unknown(
                    tenant_id,
                    action.id,
                    "worker restarted while the external action was dispatching",
                )
                task = self.store.get_task(tenant_id, task_id)
                return RunResult(
                    task=task,
                    next_action=action,
                    message="outcome is unknown; automatic retry is disabled",
                )
            if action.state is ActionState.OUTCOME_UNKNOWN:
                return RunResult(
                    task=task,
                    next_action=action,
                    message="outcome is unknown; reconcile or resolve it",
                )
            if action.state is ActionState.APPROVAL_REQUIRED:
                return RunResult(
                    task=task,
                    next_action=action,
                    message="external commit is awaiting operator approval",
                )
            if action.state in {ActionState.PENDING, ActionState.INVALIDATED}:
                observation = driver.observe()
                if (
                    action.proposal.planned_from_sha256
                    and action.proposal.planned_from_sha256 != observation.state_sha256
                ):
                    decision = self.policy.evaluate(action.proposal, observation.url)
                    decision = decision.model_copy(
                        update={
                            "allowed": False,
                            "requires_approval": False,
                            "reason": (
                                "page changed after reactive planning; re-planning is "
                                "required"
                            ),
                        }
                    )
                else:
                    decision = self.policy.evaluate(action.proposal, observation.url)
                action = self.store.prepare_action(
                    tenant_id,
                    action.id,
                    observation,
                    decision,
                )
                task = self.store.get_task(tenant_id, task_id)
                if action.state is ActionState.APPROVAL_REQUIRED:
                    return RunResult(
                        task=task,
                        next_action=action,
                        message="external commit is awaiting operator approval",
                    )
                if action.state is ActionState.FAILED:
                    return RunResult(
                        task=task,
                        next_action=action,
                        message=action.failure or "policy denied action",
                    )
            if action.state is not ActionState.PREPARED:
                raise ConflictError(f"cannot run action in state {action.state.value}")

            current_observation = driver.observe()
            if current_observation.state_sha256 != action.observation_sha256:
                invalidated = self.store.invalidate_approval(
                    tenant_id,
                    action.id,
                    current_observation.state_sha256,
                )
                return RunResult(
                    task=self.store.get_task(tenant_id, task_id),
                    next_action=invalidated,
                    message="page changed; prior preparation or approval was invalidated",
                )

            self.store.start_dispatch(tenant_id, action.id)
            try:
                receipt = self._execute(action.proposal, driver)
                if action.proposal.kind is ActionKind.SUBMIT:
                    spec = action.proposal.reconciliation
                    if spec is None:
                        unknown = self.store.mark_outcome_unknown(
                            tenant_id,
                            action.id,
                            (
                                "submit returned UI state but has no independent "
                                "receipt lookup"
                            ),
                        )
                        return RunResult(
                            task=self.store.get_task(tenant_id, task_id),
                            next_action=unknown,
                            message=(
                                "submit is unverified; visible success is not accepted "
                                "as proof"
                            ),
                        )
                    receipt = driver.reconcile(spec)
                    if receipt is None:
                        unknown = self.store.mark_outcome_unknown(
                            tenant_id,
                            action.id,
                            "submit UI completed but authoritative receipt was not found",
                        )
                        return RunResult(
                            task=self.store.get_task(tenant_id, task_id),
                            next_action=unknown,
                            message=(
                                "submit is unverified; authoritative receipt was not "
                                "found"
                            ),
                        )
            except Exception as exc:
                if action.proposal.kind is ActionKind.SUBMIT:
                    unknown = self.store.mark_outcome_unknown(
                        tenant_id,
                        action.id,
                        f"browser error after dispatch: {type(exc).__name__}: {exc}",
                    )
                    return RunResult(
                        task=self.store.get_task(tenant_id, task_id),
                        next_action=unknown,
                        message="submit may have committed; automatic retry is disabled",
                    )
                failed = self.store.fail_action(
                    tenant_id,
                    action.id,
                    f"{type(exc).__name__}: {exc}",
                )
                return RunResult(
                    task=self.store.get_task(tenant_id, task_id),
                    next_action=failed,
                    message="browser action failed",
                )
            continues = (
                task.provider in self.step_planners
                and action.proposal.kind is not ActionKind.FINISH
            )
            self.store.complete_action(
                tenant_id,
                action.id,
                receipt,
                task_continues=continues,
            )
            completed = self.store.get_task(tenant_id, task_id)
            if completed.status is TaskStatus.SUCCEEDED:
                return RunResult(task=completed, message="task completed with receipts")

    def reconcile(
        self,
        *,
        tenant_id: UUID,
        action_id: UUID,
        driver: BrowserDriver,
    ) -> BrowserReceipt | None:
        action = self.store.get_action(tenant_id, action_id)
        if action.state is ActionState.DISPATCHING:
            action = self.store.mark_outcome_unknown(
                tenant_id,
                action_id,
                "recovery found an interrupted dispatch",
            )
        if action.state is not ActionState.OUTCOME_UNKNOWN:
            raise ConflictError("action is not awaiting recovery")
        spec = action.proposal.reconciliation
        if spec is None:
            return None
        if not self.policy.allows_url(spec.url):
            raise ValueError("reconciliation URL origin is not allowed")
        receipt = driver.reconcile(spec)
        if receipt is not None:
            self.store.complete_action(tenant_id, action_id, receipt)
        return receipt

    def resolve_not_committed(
        self,
        *,
        tenant_id: UUID,
        action_id: UUID,
        expected_version: int,
        actor_id: str,
        resolution: Resolution,
        receipt: BrowserReceipt | None = None,
    ):
        if resolution is Resolution.SUCCEEDED:
            if receipt is None:
                raise ValueError("succeeded resolution requires a receipt")
            return self.store.complete_action(tenant_id, action_id, receipt)
        return self.store.reset_not_committed(
            tenant_id=tenant_id,
            action_id=action_id,
            expected_version=expected_version,
            actor_id=actor_id,
        )

    def _rehydrate(
        self,
        tenant_id: UUID,
        task_id: UUID,
        driver: BrowserDriver,
        current_ordinal: int,
    ) -> None:
        if current_ordinal == 0:
            return
        for action in self.store.list_actions(tenant_id, task_id):
            if action.ordinal >= current_ordinal:
                break
            if action.state is not ActionState.SUCCEEDED:
                continue
            if action.proposal.kind in {
                ActionKind.NAVIGATE,
                ActionKind.FILL,
                ActionKind.CLICK,
            }:
                driver.execute(action.proposal)

    @staticmethod
    def _execute(proposal, driver: BrowserDriver) -> BrowserReceipt:
        if proposal.kind is ActionKind.FINISH:
            now = utc_now()
            return BrowserReceipt(
                external_id="local-finish",
                url="about:blank",
                evidence_sha256=digest({"finished_at": now.isoformat()}),
                captured_at=now,
            )
        return driver.execute(proposal)
