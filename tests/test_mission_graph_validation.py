from __future__ import annotations

import pytest
from pydantic import ValidationError

from effect_browser.domain import (
    MAX_MISSION_READY_WIDTH,
    MissionPlan,
    MissionPlanStep,
    MissionStepKind,
)
from effect_browser.mission import _validate_authority
from effect_browser.providers import ProviderError


def research_step(
    key: str,
    *,
    depends_on: tuple[str, ...] = (),
) -> MissionPlanStep:
    return MissionPlanStep(
        key=key,
        kind=MissionStepKind.RESEARCH,
        instruction=f"Collect bounded evidence for {key}.",
        depends_on=depends_on,
    )


def test_two_node_cycle_is_rejected_before_the_graph_is_persisted() -> None:
    with pytest.raises(ValidationError, match="topologically ordered"):
        MissionPlan(
            summary="A cyclic plan must never reach the scheduler.",
            steps=(
                research_step("first", depends_on=("second",)),
                research_step("second", depends_on=("first",)),
            ),
        )


@pytest.mark.parametrize("shape", ["roots", "fanout"])
def test_overwide_ready_wave_is_rejected_deterministically(shape: str) -> None:
    keys = tuple(f"source_{index}" for index in range(MAX_MISSION_READY_WIDTH + 1))
    if shape == "roots":
        steps = tuple(research_step(key) for key in keys)
    else:
        steps = (research_step("root"),) + tuple(
            research_step(key, depends_on=("root",)) for key in keys
        )

    with pytest.raises(ValidationError, match="maximum width"):
        MissionPlan(summary="Reject an over-wide graph.", steps=steps)


def test_authority_validation_rejects_a_smuggled_second_browser_child() -> None:
    plan = MissionPlan(
        summary="A model cannot multiply the parent's single commit budget.",
        steps=(
            MissionPlanStep(
                key="primary_commit",
                kind=MissionStepKind.BROWSER,
                instruction="Submit the reviewed operation once.",
            ),
            MissionPlanStep(
                key="hidden_commit",
                kind=MissionStepKind.BROWSER,
                instruction="Submit it again under a different description.",
                depends_on=("primary_commit",),
            ),
        ),
    )

    with pytest.raises(ProviderError, match="browser step"):
        _validate_authority(plan, commits=True)


def test_non_browser_language_cannot_create_a_second_committing_child() -> None:
    plan = MissionPlan(
        summary="Step kind, not model prose, defines the executable boundary.",
        steps=(
            MissionPlanStep(
                key="commit",
                kind=MissionStepKind.BROWSER,
                instruction="Submit the reviewed operation once.",
            ),
            MissionPlanStep(
                key="summary",
                kind=MissionStepKind.SYNTHESIS,
                instruction="Say 'submit again' while summarizing persisted evidence.",
                depends_on=("commit",),
            ),
        ),
    )

    _validate_authority(plan, commits=True)
