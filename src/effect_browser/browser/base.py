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
    restored_checkpoint_ordinal: int

    def restore_storage_state(
        self,
        storage_state: dict[str, object],
        checkpoint_ordinal: int,
    ) -> None: ...

    def export_storage_state(self) -> dict[str, object]: ...

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
