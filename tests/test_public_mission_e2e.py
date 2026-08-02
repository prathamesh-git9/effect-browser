from __future__ import annotations

import os
from pathlib import Path

import pytest

from effect_browser.autopilot import AutopilotCoordinator
from effect_browser.browser.playwright import PlaywrightDriver
from effect_browser.config import Settings
from effect_browser.domain import (
    ActionKind,
    ActionState,
    MissionPlan,
    MissionPlanStep,
    MissionStepKind,
    MissionVerdict,
    PlanRequest,
    ProposedAction,
)
from effect_browser.engine import EffectBrowserService
from effect_browser.mission import MissionCoordinator
from effect_browser.policy import ActionPolicy

PUBLIC_FIXTURE_URL = "https://example.com/"


class PublicReadPlanner:
    """Produce a deterministic read-only plan while the real executor owns I/O."""

    name = "public-read-e2e"

    def plan(self, request: PlanRequest) -> tuple[ProposedAction, ...]:
        return (
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=request.start_url,
                description="Navigate to the stable public fixture.",
            ),
            ProposedAction(
                kind=ActionKind.FINISH,
                description="Persist evidence derived from the rendered page state.",
                expected_outcome="Example Domain",
            ),
        )


class PublicMissionPlanner:
    name = "public-mission-e2e"

    def plan(self, query: str, *, external_commit_authorized: bool) -> MissionPlan:
        assert PUBLIC_FIXTURE_URL in query
        assert external_commit_authorized is False
        return MissionPlan(
            summary="Inspect one stable public page in a real browser.",
            steps=(
                MissionPlanStep(
                    key="public_page",
                    kind=MissionStepKind.BROWSER,
                    instruction="Render the public page and retain hashed evidence.",
                ),
            ),
        )


@pytest.mark.e2e
def test_real_public_chromium_mission_persists_rendered_evidence(
    store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Runtime selection normally requires a remote planner for public targets. The
    # injected planner keeps this proof deterministic; no request uses this sentinel.
    monkeypatch.setenv("OPENAI_API_KEY", "unused-public-e2e-sentinel")
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'public-mission.db'}",
        artifacts_directory=tmp_path / "artifacts",
        browser_headless=True,
        browser_sandbox=os.getenv(
            "EFFECT_BROWSER_BROWSER_SANDBOX",
            "true",
        ).casefold()
        not in {"0", "false", "no", "off"},
    )
    service = EffectBrowserService(store, ActionPolicy(settings.allowed_origins))

    def real_browser(origins: tuple[str, ...]) -> PlaywrightDriver:
        return PlaywrightDriver(
            executable_path=settings.browser_executable,
            headless=True,
            sandbox=settings.browser_sandbox,
            artifacts_directory=settings.artifacts_directory,
            allowed_origins=origins,
        )

    autopilot = AutopilotCoordinator(
        service=service,
        settings=settings,
        planner_factory=lambda _runtime: PublicReadPlanner(),
        driver_factory=real_browser,
    )
    result = MissionCoordinator(
        store=store,
        autopilot=autopilot,
        settings=settings,
        plan_provider=PublicMissionPlanner(),
    ).execute(
        tenant_id=settings.default_tenant_id,
        query=(
            f"Inspect the rendered public fixture at {PUBLIC_FIXTURE_URL} and "
            "verify the exact phrase Example Domain."
        ),
    )

    browser_step = result.steps[0]
    assert result.verdict is MissionVerdict.COMPLETED
    assert browser_step.child_task_id is not None
    actions = store.list_actions(
        settings.default_tenant_id,
        browser_step.child_task_id,
    )
    assert [action.proposal.kind for action in actions] == [
        ActionKind.NAVIGATE,
        ActionKind.FINISH,
    ]
    assert all(action.state is ActionState.SUCCEEDED for action in actions)
    rendered = actions[1]
    assert rendered.observation_url == PUBLIC_FIXTURE_URL
    assert rendered.observation_sha256 is not None
    assert len(rendered.observation_sha256) == 64
    receipt = store.get_receipt(settings.default_tenant_id, rendered.id)
    assert receipt is not None
    assert receipt.url == PUBLIC_FIXTURE_URL
    assert len(receipt.evidence_sha256) == 64
    assert store.verify_audit(settings.default_tenant_id).valid is True
