from __future__ import annotations

from urllib.parse import quote, urljoin

from effect_browser.domain import (
    ActionKind,
    PageSnapshot,
    PlanRequest,
    ProposedAction,
    ReconciliationSpec,
    StepChoice,
)


class ReactiveBootstrapPlanner:
    """Persist only the initial navigation; every later action uses a fresh snapshot."""

    def __init__(self, name: str) -> None:
        self.name = name

    def plan(self, request: PlanRequest) -> tuple[ProposedAction, ...]:
        return (
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=request.start_url,
                description="Open the task start URL before observing the live page.",
            ),
        )


def bind_choice(
    choice: StepChoice,
    snapshot: PageSnapshot,
    *,
    effect_reference: str,
) -> ProposedAction:
    if choice.kind is ActionKind.NAVIGATE:
        return ProposedAction(
            kind=choice.kind,
            url=choice.url,
            description=choice.description,
            planned_from_sha256=snapshot.state_sha256,
        )
    if choice.kind is ActionKind.FINISH:
        return ProposedAction(
            kind=choice.kind,
            description=choice.description,
            planned_from_sha256=snapshot.state_sha256,
        )
    candidates = {
        candidate.id: candidate
        for candidate in snapshot.candidates
        if not candidate.disabled
    }
    candidate = candidates.get(choice.candidate_id or "")
    if candidate is None:
        raise ValueError("step planner selected a missing or disabled candidate")
    if choice.kind is ActionKind.FILL and candidate.interaction != "input":
        raise ValueError("fill choice must target an input candidate")
    if choice.kind is ActionKind.CLICK and candidate.interaction == "commit":
        raise ValueError("commit candidate must use submit, not click")
    if choice.kind is ActionKind.SUBMIT and candidate.interaction != "commit":
        raise ValueError("submit choice must target a commit candidate")
    reconciliation = None
    if choice.kind is ActionKind.SUBMIT and snapshot.submission_contract:
        contract = snapshot.submission_contract
        encoded_reference = quote(effect_reference, safe="")
        reconciliation = ReconciliationSpec(
            url=urljoin(
                snapshot.url,
                contract.url_template.replace("{effect_key}", encoded_reference),
            ),
            expected_text=contract.expected_text_template.replace(
                "{effect_key}", effect_reference
            ),
            external_reference=effect_reference,
            receipt_test_id=contract.receipt_test_id,
        )
    return ProposedAction(
        kind=choice.kind,
        locator=candidate.locator,
        value=choice.value,
        description=choice.description,
        effect_key=effect_reference if choice.kind is ActionKind.SUBMIT else None,
        expected_outcome=(
            choice.expected_outcome if choice.kind is ActionKind.SUBMIT else None
        ),
        reconciliation=reconciliation,
        planned_from_sha256=snapshot.state_sha256,
        target_interaction=candidate.interaction,
        target_name=candidate.name,
    )
