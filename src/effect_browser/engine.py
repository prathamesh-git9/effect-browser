from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from effect_browser.autonomy import auto_approval_reason
from effect_browser.browser.base import BrowserDriver
from effect_browser.challenge import detect_challenge
from effect_browser.domain import (
    ActionKind,
    ActionState,
    AutonomyMode,
    AutonomyScope,
    BrowserReceipt,
    PlanRequest,
    PolicyDecision,
    Resolution,
    RiskClass,
    RunResult,
    StepRequest,
    Task,
    TaskStatus,
    digest,
    utc_now,
)
from effect_browser.factual import deterministic_required_choice, planning_facts
from effect_browser.policy import ActionPolicy
from effect_browser.providers.base import Planner, ProviderError, StepPlanner
from effect_browser.providers.reactive import bind_choice
from effect_browser.store import ConflictError, DatabaseStore
from effect_browser.transmission import TransmissionBlocked


class SimulatedProcessCrash(BaseException):
    """Test-only crash signal that deliberately bypasses normal exception handling."""


class CrashAfterCommitDriver:
    """Delegating driver that loses the process after one commit reaches the target."""

    def __init__(self, inner: BrowserDriver) -> None:
        self.inner = inner
        self.crashed = False

    def observe(self):
        return self.inner.observe()

    def snapshot(self):
        return self.inner.snapshot()

    def preview_submit(self, action, observation_sha256):
        return self.inner.preview_submit(action, observation_sha256)

    def arm_reviewed_submit(self, review, allowed_origin_url):
        return self.inner.arm_reviewed_submit(review, allowed_origin_url)

    def assert_rehydration_safe(self):
        return self.inner.assert_rehydration_safe()

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
        *,
        task_lease_seconds: int = 120,
    ) -> None:
        if task_lease_seconds < 1:
            raise ValueError("task_lease_seconds must be positive")
        self.store = store
        self.policy = policy
        self.step_planners = step_planners or {}
        # A dead worker cannot run its release finally block. Keeping the lease
        # duration injectable lets recovery wait for the same durable fencing rule
        # in production and in process-death tests without unsafe lease stealing.
        self.task_lease_seconds = task_lease_seconds

    def create_task(
        self,
        *,
        tenant_id: UUID,
        instruction: str,
        start_url: str,
        planner: Planner,
        task_id: UUID | None = None,
        profile_id: UUID | None = None,
        document_path: Path | None = None,
        document_sha256: str | None = None,
        autonomy: AutonomyScope | None = None,
    ) -> Task:
        if (document_path is None) != (document_sha256 is None):
            raise ValueError(
                "document_path and document_sha256 must be supplied together"
            )
        validated_document = None
        if document_path is not None and document_sha256 is not None:
            validated_document = self.policy.upload_guard.validate(
                document_path,
                document_sha256,
            )
        task_id = task_id or uuid4()
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
        for action in actions:
            _validate_finish_expectation(
                action.kind,
                action.expected_outcome,
                instruction,
            )
        return self.store.create_task(
            task_id=task_id,
            tenant_id=tenant_id,
            instruction=instruction,
            start_url=start_url,
            provider=planner.name,
            actions=actions,
            profile_id=profile_id,
            document_path=(
                validated_document.path if validated_document is not None else None
            ),
            document_sha256=document_sha256,
            autonomy=autonomy,
        )

    def run(
        self,
        *,
        tenant_id: UUID,
        task_id: UUID,
        driver: BrowserDriver,
    ) -> RunResult:
        task = self.store.get_task(tenant_id, task_id)
        if task.status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.REJECTED,
            TaskStatus.BLOCKED,
        }:
            return RunResult(task=task, message=f"task is already {task.status.value}")
        worker_id = f"worker-{uuid4()}"
        self.store.claim_task(
            tenant_id=tenant_id,
            task_id=task_id,
            owner=worker_id,
            lease_seconds=self.task_lease_seconds,
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
        current = self.store.current_action(tenant_id, task_id)
        replay_uploads = bool(
            current is not None
            and current.state is ActionState.PREPARED
            and current.proposal.kind is ActionKind.SUBMIT
            and current.proposal.outgoing_review is not None
            and len(current.proposal.outgoing_review.requests) == 1
        )
        if replay_uploads:
            driver.arm_reviewed_submit(
                current.proposal.outgoing_review,
                task.start_url,
            )
        self._rehydrate(
            tenant_id,
            task_id,
            driver,
            task.current_ordinal,
            replay_uploads=replay_uploads,
        )
        if replay_uploads:
            try:
                driver.assert_rehydration_safe()
                restored_observation = driver.observe()
            except TransmissionBlocked as exc:
                self.store.start_dispatch(tenant_id, current.id)
                failed = self.store.fail_action(
                    tenant_id,
                    current.id,
                    f"upload rehydration blocked before submission: {exc}",
                )
                return RunResult(
                    task=self.store.get_task(tenant_id, task_id),
                    next_action=failed,
                    message=(
                        "restoring the approved upload triggered an unexpected "
                        "request; it was blocked before transmission"
                    ),
                )
            if restored_observation.state_sha256 != current.observation_sha256:
                restored_review = driver.preview_submit(
                    current.proposal,
                    restored_observation.state_sha256,
                )
                payload_matches = (
                    restored_review.fields == current.proposal.outgoing_review.fields
                    and restored_review.document_sha256s
                    == current.proposal.outgoing_review.document_sha256s
                    and tuple(
                        request.request_sha256 for request in restored_review.requests
                    )
                    == tuple(
                        request.request_sha256
                        for request in current.proposal.outgoing_review.requests
                    )
                )
                if not payload_matches:
                    self.store.start_dispatch(tenant_id, current.id)
                    failed = self.store.fail_action(
                        tenant_id,
                        current.id,
                        (
                            "outgoing request changed after approval and was "
                            "blocked before transmission"
                        ),
                    )
                    return RunResult(
                        task=self.store.get_task(tenant_id, task_id),
                        next_action=failed,
                        message=(
                            "restored outgoing request differs from the approved "
                            "review and was blocked before transmission"
                        ),
                    )
                if (
                    restored_review.observation_sha256
                    != current.proposal.outgoing_review.observation_sha256
                ):
                    # Rehydration and preview sent no write. A real change therefore
                    # invalidates the approval and returns the action to the normal
                    # review path instead of permanently failing a durable task.
                    invalidated = self.store.invalidate_approval(
                        tenant_id,
                        current.id,
                        restored_review.observation_sha256,
                    )
                    return RunResult(
                        task=self.store.get_task(tenant_id, task_id),
                        next_action=invalidated,
                        message=(
                            "page changed during crash-safe restoration; the prior "
                            "approval was invalidated before transmission"
                        ),
                    )

        automatic_invalidations = 0
        while True:
            self.store.renew_task_lease(
                tenant_id=tenant_id,
                task_id=task_id,
                owner=worker_id,
                lease_seconds=self.task_lease_seconds,
            )
            task = self.store.get_task(tenant_id, task_id)
            query_origins = (
                (task.start_url,) if task.autonomy.allow_query_target_origin else ()
            )
            action = self.store.current_action(tenant_id, task_id)
            if action is None:
                step_planner = self.step_planners.get(task.provider)
                if step_planner is not None:
                    if task.current_ordinal >= 30:
                        blocked = self.store.block_task(
                            tenant_id,
                            task_id,
                            kind="step_budget_exhausted",
                            reason=(
                                "the browser reached its 30-step execution limit "
                                "without verified completion"
                            ),
                            evidence=f"current_ordinal={task.current_ordinal}",
                        )
                        return RunResult(
                            task=blocked,
                            message=("step budget exhausted; success is not claimed"),
                        )
                    snapshot = driver.snapshot()
                    challenge = detect_challenge(snapshot)
                    if challenge is not None:
                        blocked = self.store.block_task(
                            tenant_id,
                            task_id,
                            kind=challenge.kind.value,
                            reason=challenge.reason,
                            evidence=challenge.evidence,
                        )
                        return RunResult(
                            task=blocked,
                            message=(
                                f"{challenge.reason}; the task is blocked for human "
                                "handoff"
                            ),
                        )
                    history = self.store.list_actions(tenant_id, task_id)
                    answers = (
                        self.store.list_profile_answers(
                            tenant_id,
                            task.profile_id,
                        )
                        if task.profile_id is not None
                        else []
                    )
                    facts = planning_facts(answers)
                    document = self.store.task_document(tenant_id, task_id)
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
                        facts=facts,
                        snapshot=snapshot,
                    )
                    choice = deterministic_required_choice(
                        text_excerpt=snapshot.text_excerpt,
                        candidates=snapshot.candidates,
                        facts=facts,
                        document_path=document[0] if document else None,
                        document_sha256=document[1] if document else None,
                    )
                    if choice is None:
                        try:
                            choice = step_planner.choose(request)
                        except ProviderError as exc:
                            blocked = self.store.block_task(
                                tenant_id,
                                task_id,
                                kind="provider_error",
                                reason=str(exc),
                                evidence=task.provider,
                            )
                            return RunResult(
                                task=blocked,
                                message=(
                                    "the planning provider failed; success is not claimed"
                                ),
                            )
                    try:
                        _validate_finish_expectation(
                            choice.kind,
                            choice.expected_outcome,
                            task.instruction,
                        )
                    except ValueError as exc:
                        blocked = self.store.block_task(
                            tenant_id,
                            task_id,
                            kind="provider_error",
                            reason=str(exc),
                            evidence=task.provider,
                        )
                        return RunResult(
                            task=blocked,
                            message=(
                                "the planning provider proposed unbound finish "
                                "evidence; success is not claimed"
                            ),
                        )
                    action = self.store.append_action(
                        tenant_id=tenant_id,
                        task_id=task_id,
                        proposal=bind_choice(
                            choice,
                            snapshot,
                            effect_reference=request.effect_reference,
                            prior_actions=tuple(
                                item.proposal
                                for item in history
                                if item.state is ActionState.SUCCEEDED
                            ),
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
            if action.state is ActionState.INPUT_REQUIRED:
                return RunResult(
                    task=task,
                    next_action=action,
                    message=action.failure or "verified user input is required",
                )
            if action.state is ActionState.APPROVAL_REQUIRED:
                action = self._auto_approve(task, action)
                if (
                    action.state is ActionState.PREPARED
                    and action.proposal.kind is ActionKind.SUBMIT
                ):
                    return RunResult(
                        task=self.store.get_task(tenant_id, task_id),
                        next_action=action,
                        message=(
                            "exact submit review was authorized; resume in a fresh "
                            "browser session for crash-safe dispatch"
                        ),
                    )
            if action.state is ActionState.APPROVAL_REQUIRED:
                return RunResult(
                    task=task,
                    next_action=action,
                    message="external commit is awaiting operator approval",
                )
            if action.state in {ActionState.PENDING, ActionState.INVALIDATED}:
                observation = driver.observe()
                if action.proposal.kind is ActionKind.HANDOFF:
                    handoff = self.store.require_input(
                        tenant_id=tenant_id,
                        action_id=action.id,
                        observation=observation,
                        reason=action.proposal.description,
                    )
                    return RunResult(
                        task=self.store.get_task(tenant_id, task_id),
                        next_action=handoff,
                        message=handoff.failure or "verified user input is required",
                    )
                if (
                    action.proposal.planned_from_sha256
                    and action.proposal.planned_from_sha256 != observation.state_sha256
                ):
                    decision = self.policy.evaluate(
                        action.proposal,
                        observation.url,
                        query_origins,
                    )
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
                    if action.proposal.kind is ActionKind.SUBMIT:
                        try:
                            review = driver.preview_submit(
                                action.proposal,
                                observation.state_sha256,
                            )
                            post_preview_observation = driver.observe()
                            if (
                                review.observation_sha256
                                == post_preview_observation.state_sha256
                            ):
                                observation = post_preview_observation
                            elif review.observation_sha256 != observation.state_sha256:
                                raise ValueError(
                                    "page changed after outgoing request preview"
                                )
                            action = self.store.bind_outgoing_review(
                                tenant_id=tenant_id,
                                action_id=action.id,
                                expected_version=action.version,
                                review=review,
                            )
                        except ValueError as exc:
                            decision = PolicyDecision(
                                allowed=False,
                                risk=RiskClass.EXTERNAL_COMMIT,
                                requires_approval=False,
                                reason=(
                                    "exact outgoing request review failed: "
                                    f"{type(exc).__name__}: {exc}"
                                ),
                            )
                        else:
                            decision = self.policy.evaluate(
                                action.proposal,
                                observation.url,
                                query_origins,
                            )
                    else:
                        decision = self.policy.evaluate(
                            action.proposal,
                            observation.url,
                            query_origins,
                        )
                action = self.store.prepare_action(
                    tenant_id,
                    action.id,
                    observation,
                    decision,
                )
                task = self.store.get_task(tenant_id, task_id)
                if action.state is ActionState.APPROVAL_REQUIRED:
                    action = self._auto_approve(task, action)
                    if (
                        action.state is ActionState.PREPARED
                        and action.proposal.kind is ActionKind.SUBMIT
                    ):
                        return RunResult(
                            task=self.store.get_task(tenant_id, task_id),
                            next_action=action,
                            message=(
                                "exact submit review was authorized; resume in a "
                                "fresh browser session for crash-safe dispatch"
                            ),
                        )
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
                if (
                    task.autonomy.mode is AutonomyMode.BOUNDED
                    and action.proposal.planned_from_sha256 is None
                    and automatic_invalidations < 3
                ):
                    automatic_invalidations += 1
                    continue
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
                    if not self.policy.allows_url(spec.url, query_origins):
                        unknown = self.store.mark_outcome_unknown(
                            tenant_id,
                            action.id,
                            (
                                "submit completed but its reconciliation URL "
                                "crosses the configured origin boundary"
                            ),
                        )
                        return RunResult(
                            task=self.store.get_task(tenant_id, task_id),
                            next_action=unknown,
                            message=(
                                "submit is unverified; the reconciliation origin "
                                "is not allowed"
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
            except TransmissionBlocked as exc:
                failed = self.store.fail_action(
                    tenant_id,
                    action.id,
                    f"outgoing request blocked before transmission: {exc}",
                )
                return RunResult(
                    task=self.store.get_task(tenant_id, task_id),
                    next_action=failed,
                    message=(
                        "file selection triggered an unreviewed request and was "
                        "blocked before transmission"
                        if action.proposal.kind is ActionKind.UPLOAD
                        else (
                            "outgoing request changed after approval and was blocked "
                            "before transmission"
                        )
                    ),
                )
            except Exception as exc:
                if action.proposal.kind in {ActionKind.SUBMIT, ActionKind.UPLOAD}:
                    unknown = self.store.mark_outcome_unknown(
                        tenant_id,
                        action.id,
                        f"browser error after dispatch: {type(exc).__name__}: {exc}",
                    )
                    return RunResult(
                        task=self.store.get_task(tenant_id, task_id),
                        next_action=unknown,
                        message=(
                            "external transmission may have occurred; automatic retry "
                            "is disabled"
                        ),
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

    def _auto_approve(self, task: Task, action):
        reason = auto_approval_reason(
            task=task,
            action=action,
            task_actions=self.store.list_actions(task.tenant_id, task.id),
            task_document=self.store.task_document(task.tenant_id, task.id),
        )
        if reason is None:
            return action
        actor = (
            "bounded-autonomy:"
            + digest(
                {
                    "task_id": str(task.id),
                    "scope": task.autonomy.model_dump(mode="json"),
                }
            )[:16]
        )
        return self.store.approve_action(
            tenant_id=task.tenant_id,
            action_id=action.id,
            expected_version=action.version,
            actor_id=actor,
            authorization_basis=reason,
        )

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
        task = self.store.get_task(tenant_id, action.task_id)
        query_origins = (
            (task.start_url,) if task.autonomy.allow_query_target_origin else ()
        )
        if not self.policy.allows_url(spec.url, query_origins):
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
        *,
        replay_uploads: bool = False,
    ) -> None:
        if current_ordinal == 0:
            return
        for action in self.store.list_actions(tenant_id, task_id):
            if action.ordinal >= current_ordinal:
                break
            if action.state is not ActionState.SUCCEEDED:
                continue
            if (
                action.proposal.kind
                in {
                    ActionKind.NAVIGATE,
                    ActionKind.FILL,
                    ActionKind.CHECK,
                    ActionKind.PRESS,
                }
                or (
                    action.proposal.kind is ActionKind.CLICK
                    and action.proposal.target_interaction in {"navigation", "option"}
                )
                or (replay_uploads and action.proposal.kind is ActionKind.UPLOAD)
            ):
                driver.execute(action.proposal)

    @staticmethod
    def _execute(proposal, driver: BrowserDriver) -> BrowserReceipt:
        if proposal.kind is ActionKind.FINISH:
            expected_phrase = (proposal.expected_outcome or "").strip()
            if expected_phrase:
                # Rendered text is useful only at this verification boundary. The
                # durable receipt binds its hash and the page-state hash without
                # copying target-controlled content into the store or audit log.
                snapshot = driver.snapshot()
                searchable = f"{snapshot.title}\n{snapshot.text_excerpt}".casefold()
                if expected_phrase.casefold() not in searchable:
                    raise ValueError(
                        "rendered page did not contain the expected finish outcome"
                    )
                return BrowserReceipt(
                    external_id="local-finish",
                    url=snapshot.url,
                    evidence_sha256=digest(
                        {
                            "url": snapshot.url,
                            "state_sha256": snapshot.state_sha256,
                            "expected_phrase_sha256": digest(
                                {"expected_phrase": expected_phrase}
                            ),
                        }
                    ),
                    captured_at=utc_now(),
                )
            observation = driver.observe()
            now = utc_now()
            return BrowserReceipt(
                external_id="local-finish",
                url=observation.url,
                evidence_sha256=digest(
                    {
                        "url": observation.url,
                        "state_sha256": observation.state_sha256,
                    }
                ),
                captured_at=now,
            )
        return driver.execute(proposal)


def _validate_finish_expectation(
    kind: ActionKind,
    expected_outcome: str | None,
    instruction: str,
) -> None:
    """Keep durable finish evidence sourced from user intent, never page content."""
    phrase = (expected_outcome or "").strip()
    if (
        kind is ActionKind.FINISH
        and phrase
        and phrase.casefold() not in instruction.casefold()
    ):
        raise ValueError(
            "finish expected_outcome must be an exact phrase from the user instruction"
        )
