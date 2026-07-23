from __future__ import annotations

from urllib.parse import urlencode, urljoin

from effect_browser.domain import (
    ActionKind,
    Locator,
    PlanRequest,
    ProposedAction,
    ReconciliationSpec,
)


class DeterministicPlanner:
    name = "deterministic"

    def plan(self, request: PlanRequest) -> tuple[ProposedAction, ...]:
        reference = f"EB-{str(request.task_id)[:8].upper()}"
        order_url = urljoin(request.start_url.rstrip("/") + "/", "demo-shop")
        reconcile_url = (
            f"{urljoin(order_url + '/', 'orders')}?{urlencode({'reference': reference})}"
        )
        return (
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=order_url,
                description="Open the bundled order portal.",
            ),
            ProposedAction(
                kind=ActionKind.FILL,
                locator=Locator(label="Product"),
                value="backup-drive",
                description="Choose the backup-drive SKU.",
            ),
            ProposedAction(
                kind=ActionKind.FILL,
                locator=Locator(label="Quantity"),
                value="3",
                description="Set order quantity to three.",
            ),
            ProposedAction(
                kind=ActionKind.FILL,
                locator=Locator(label="Customer reference"),
                value=reference,
                description="Set the stable business reference used for reconciliation.",
            ),
            ProposedAction(
                kind=ActionKind.SUBMIT,
                locator=Locator(role="button", name="Place order"),
                description="Commit the order to the portal.",
                effect_key=reference,
                expected_outcome="One order for three backup drives.",
                reconciliation=ReconciliationSpec(
                    url=reconcile_url,
                    expected_text=reference,
                    external_reference=reference,
                    receipt_test_id="receipt",
                ),
            ),
            ProposedAction(
                kind=ActionKind.FINISH,
                description="Return the reconciled order receipt.",
            ),
        )
