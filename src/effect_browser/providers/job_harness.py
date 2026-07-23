from __future__ import annotations

from urllib.parse import urlencode, urljoin

from effect_browser.domain import (
    ActionKind,
    Locator,
    PlanRequest,
    ProposedAction,
    ReconciliationSpec,
)


class JobHarnessPlanner:
    """Deterministic synthetic profile for testing dynamic application correctness."""

    name = "job-harness"

    def __init__(self, mode: str = "real") -> None:
        if mode not in {"real", "fake_success", "reject"}:
            raise ValueError("job harness mode must be real, fake_success, or reject")
        self.mode = mode

    def plan(self, request: PlanRequest) -> tuple[ProposedAction, ...]:
        reference = f"JOBAPP-{str(request.task_id)[:8].upper()}"
        root = request.start_url.rstrip("/") + "/"
        apply_path = "demo-jobs/jobs/platform-reliability-engineer/apply"
        apply_url = f"{urljoin(root, apply_path)}?{urlencode({'mode': self.mode})}"
        receipt_url = (
            f"{urljoin(root, 'demo-jobs/applications')}?"
            f"{urlencode({'reference': reference})}"
        )
        return (
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=apply_url,
                description="Open the asynchronously rendered application form.",
            ),
            ProposedAction(
                kind=ActionKind.FILL,
                locator=Locator(label="Full name"),
                value="Synthetic Test Candidate",
                description="Fill the synthetic candidate name.",
            ),
            ProposedAction(
                kind=ActionKind.FILL,
                locator=Locator(label="Email"),
                value="candidate@example.test",
                description="Fill the non-deliverable synthetic email address.",
            ),
            ProposedAction(
                kind=ActionKind.FILL,
                locator=Locator(label="Country"),
                value="Ireland",
                description="Choose Ireland and reveal the conditional question.",
            ),
            ProposedAction(
                kind=ActionKind.FILL,
                locator=Locator(label="Work authorization"),
                value="authorized",
                description="Use the explicit synthetic authorization answer.",
            ),
            ProposedAction(
                kind=ActionKind.FILL,
                locator=Locator(label="Years using Python"),
                value="6",
                description="Fill the synthetic Python experience value.",
            ),
            ProposedAction(
                kind=ActionKind.FILL,
                locator=Locator(label="Resume summary"),
                value=(
                    "Synthetic profile: production Python, distributed systems, "
                    "durable execution, observability, and incident response."
                ),
                description="Fill a clearly synthetic resume summary.",
            ),
            ProposedAction(
                kind=ActionKind.FILL,
                locator=Locator(label="Why this role?"),
                value=(
                    "This is a synthetic end-to-end test of a dynamic application "
                    "workflow and does not represent a real candidate."
                ),
                description="Fill a clearly synthetic cover note.",
            ),
            ProposedAction(
                kind=ActionKind.FILL,
                locator=Locator(label="Application reference"),
                value=reference,
                description="Set the stable application reconciliation reference.",
            ),
            ProposedAction(
                kind=ActionKind.SUBMIT,
                locator=Locator(role="button", name="Submit application"),
                description="Submit the synthetic job application.",
                effect_key=reference,
                expected_outcome=(
                    "One durable application for the synthetic candidate and job."
                ),
                reconciliation=ReconciliationSpec(
                    url=receipt_url,
                    expected_text=f"Verified application {reference}",
                    external_reference=reference,
                    receipt_test_id="job-application-receipt",
                ),
            ),
            ProposedAction(
                kind=ActionKind.FINISH,
                description=(
                    "Return only the independently reconciled application receipt."
                ),
            ),
        )
