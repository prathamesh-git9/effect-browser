from __future__ import annotations

from typing import Protocol

from effect_browser.domain import (
    BrowserReceipt,
    Observation,
    ProposedAction,
    ReconciliationSpec,
)


class BrowserDriver(Protocol):
    def observe(self) -> Observation: ...

    def execute(self, action: ProposedAction) -> BrowserReceipt: ...

    def reconcile(self, spec: ReconciliationSpec) -> BrowserReceipt | None: ...

    def close(self) -> None: ...
