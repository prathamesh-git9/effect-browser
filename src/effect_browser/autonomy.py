from __future__ import annotations

from pathlib import Path

from effect_browser.domain import (
    ActionKind,
    ActionState,
    AutonomyMode,
    BrowserAction,
    Task,
)


def auto_approval_reason(
    *,
    task: Task,
    action: BrowserAction,
    task_actions: list[BrowserAction],
    task_document: tuple[Path, str] | None,
) -> str | None:
    """Return the exact pre-authorization used, or fail closed with ``None``."""
    if task.autonomy.mode is not AutonomyMode.BOUNDED:
        return None
    if action.state is not ActionState.APPROVAL_REQUIRED:
        return None

    proposal = action.proposal
    if proposal.kind is ActionKind.UPLOAD:
        if not task.autonomy.allow_file_uploads or task_document is None:
            return None
        document_path, document_sha256 = task_document
        if (
            proposal.file_path != document_path
            or proposal.document_sha256 != document_sha256
            or task.document_sha256 != document_sha256
        ):
            return None
        return "task scope pre-authorized the exact allowlisted document hash"

    if proposal.kind is not ActionKind.SUBMIT:
        # Ambiguous clicks cannot be reviewed or reconciled, so bounded autonomy
        # deliberately refuses them even though supervised mode can approve them.
        return None
    if not task.autonomy.allow_external_commits:
        return None
    if proposal.reconciliation is None or proposal.outgoing_review is None:
        return None
    if len(proposal.outgoing_review.requests) != 1:
        return None
    if task_document is not None:
        _document_path, document_sha256 = task_document
        if proposal.outgoing_review.document_sha256s != (document_sha256,):
            return None

    committed_or_in_flight = sum(
        candidate.id != action.id
        and candidate.proposal.kind is ActionKind.SUBMIT
        and candidate.state
        in {
            ActionState.PREPARED,
            ActionState.DISPATCHING,
            ActionState.SUCCEEDED,
            ActionState.OUTCOME_UNKNOWN,
        }
        for candidate in task_actions
    )
    if committed_or_in_flight >= task.autonomy.max_external_commits:
        return None
    return (
        "task scope pre-authorized one exact reviewed request with "
        "authoritative reconciliation"
    )
