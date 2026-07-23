from __future__ import annotations

from typing import Protocol

from effect_browser.domain import PlanRequest, ProposedAction, StepChoice, StepRequest


class Planner(Protocol):
    name: str

    def plan(self, request: PlanRequest) -> tuple[ProposedAction, ...]: ...


class StepPlanner(Protocol):
    name: str

    def choose(self, request: StepRequest) -> StepChoice: ...


class ProviderError(RuntimeError):
    """A planner provider rejected or failed to produce a plan."""
