from __future__ import annotations

from typing import Protocol

from effect_browser.domain import PlanRequest, ProposedAction


class Planner(Protocol):
    name: str

    def plan(self, request: PlanRequest) -> tuple[ProposedAction, ...]: ...
