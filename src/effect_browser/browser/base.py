from __future__ import annotations

from typing import Protocol

from effect_browser.domain import (
    BrowserReceipt,
    Observation,
    OutgoingReview,
    PageSnapshot,
    ProposedAction,
    ReconciliationSpec,
)


class BrowserDriver(Protocol):
    def observe(self) -> Observation: ...

    def snapshot(self) -> PageSnapshot: ...

    def preview_submit(
        self,
        action: ProposedAction,
        observation_sha256: str,
    ) -> OutgoingReview: ...

    def arm_reviewed_submit(
        self,
        review: OutgoingReview,
        allowed_origin_url: str,
    ) -> None: ...

    def assert_rehydration_safe(self) -> None: ...

    def execute(self, action: ProposedAction) -> BrowserReceipt: ...

    def reconcile(self, spec: ReconciliationSpec) -> BrowserReceipt | None: ...

    def close(self) -> None: ...
