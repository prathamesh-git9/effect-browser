from __future__ import annotations

from urllib.parse import quote, urljoin

from effect_browser.domain import (
    ActionKind,
    OutgoingReview,
    PageSnapshot,
    PlanRequest,
    ProposedAction,
    ReconciliationSpec,
    ReviewField,
    StepChoice,
    digest,
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
    prior_actions: tuple[ProposedAction, ...] = (),
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
    if choice.kind is ActionKind.UPLOAD and candidate.interaction != "upload":
        raise ValueError("upload choice must target a file input candidate")
    if choice.kind is ActionKind.CLICK and candidate.interaction == "commit":
        raise ValueError("commit candidate must use submit, not click")
    if choice.kind is ActionKind.SUBMIT and candidate.interaction != "commit":
        raise ValueError("submit choice must target a commit candidate")
    reconciliation = None
    outgoing_review = None
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
    if choice.kind is ActionKind.SUBMIT:
        latest_fill_by_name = {
            action.target_name: action
            for action in prior_actions
            if action.kind is ActionKind.FILL and action.target_name is not None
        }
        latest_upload_by_name = {
            action.target_name: action
            for action in prior_actions
            if action.kind is ActionKind.UPLOAD and action.target_name is not None
        }
        fields = [
            ReviewField(
                candidate_id=candidate.id,
                label=candidate.name,
                value=candidate.current_value,
                source_action_sha256=(
                    source.action_hash()
                    if (
                        (source := latest_fill_by_name.get(candidate.name)) is not None
                        and source.value == candidate.current_value
                    )
                    else None
                ),
            )
            for candidate in snapshot.candidates
            if candidate.interaction == "input" and candidate.current_value is not None
        ]
        visible_names = {field.label for field in fields}
        for name, source in latest_fill_by_name.items():
            if name in visible_names or source.value is None:
                continue
            fields.append(
                ReviewField(
                    candidate_id=(
                        source.locator.adaptive_id
                        if source.locator and source.locator.adaptive_id
                        else f"prior-{source.action_hash()[:20]}"
                    ),
                    label=name,
                    value=source.value,
                    source_action_sha256=source.action_hash(),
                )
            )
        document_sha256s = tuple(
            source.document_sha256
            for candidate in snapshot.candidates
            if candidate.interaction == "upload"
            and candidate.filled
            and (source := latest_upload_by_name.get(candidate.name)) is not None
            and source.document_sha256 is not None
        )
        review_body = {
            "fields": [field.model_dump(mode="json") for field in fields],
            "document_sha256s": list(document_sha256s),
            "observation_sha256": snapshot.state_sha256,
        }
        outgoing_review = OutgoingReview(
            fields=tuple(fields),
            document_sha256s=document_sha256s,
            observation_sha256=snapshot.state_sha256,
            payload_sha256=digest(review_body),
        )
    return ProposedAction(
        kind=choice.kind,
        locator=candidate.locator,
        value=choice.value,
        file_path=choice.file_path,
        document_sha256=choice.document_sha256,
        description=choice.description,
        effect_key=effect_reference if choice.kind is ActionKind.SUBMIT else None,
        expected_outcome=(
            choice.expected_outcome if choice.kind is ActionKind.SUBMIT else None
        ),
        reconciliation=reconciliation,
        planned_from_sha256=snapshot.state_sha256,
        target_interaction=candidate.interaction,
        target_name=candidate.name,
        outgoing_review=outgoing_review,
    )
