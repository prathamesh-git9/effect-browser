from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from effect_browser.autopilot import AutopilotVerdict, ProviderRuntime
from effect_browser.config import Settings
from effect_browser.domain import (
    ActionKind,
    MissionPlan,
    MissionPlanStep,
    MissionStatus,
    MissionStepKind,
    MissionStepStatus,
    MissionVerdict,
    ProposedAction,
)
from effect_browser.mission import (
    MissionCoordinator,
    ResearchEvidence,
    ResponsesMissionPlanner,
    ResponsesResearcher,
    ResponsesSynthesizer,
    SynthesisEvidence,
)
from effect_browser.providers.base import ProviderError
from effect_browser.store import ConflictError
from tests.conftest import TENANT


@dataclass
class StaticPlanner:
    mission_plan: MissionPlan
    name: str = "test"

    def plan(self, query: str, *, external_commit_authorized: bool) -> MissionPlan:
        return self.mission_plan


@dataclass
class ConcurrentResearcher:
    lock: threading.Lock = field(default_factory=threading.Lock)
    active: int = 0
    maximum_active: int = 0
    calls: list[str] = field(default_factory=list)

    def search(self, query: str) -> ResearchEvidence:
        with self.lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            self.calls.append(query)
        time.sleep(0.08)
        with self.lock:
            self.active -= 1
        slug = query.casefold().replace(" ", "-")
        return ResearchEvidence(
            query=query,
            summary=f"Grounded result for {query}",
            citation_urls=(f"https://evidence.example/{slug}",),
            provider_response_sha256="a" * 64,
        )


@dataclass
class CitedSynthesizer:
    calls: int = 0

    def synthesize(
        self,
        instruction: str,
        dependency_outputs: tuple[dict[str, Any], ...],
    ) -> SynthesisEvidence:
        self.calls += 1
        citations = tuple(
            url for output in dependency_outputs for url in output["citation_urls"]
        )
        return SynthesisEvidence(
            answer=f"{instruction}: compared {len(dependency_outputs)} sources",
            citation_urls=citations,
            input_sha256="b" * 64,
        )


class NoBrowser:
    def execute(self, **_kwargs):
        raise AssertionError("research-only mission must not start a browser")

    def resume(self, **_kwargs):
        raise AssertionError("research-only mission must not resume a browser")


class ResultEnvelope:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return self.payload


@dataclass
class UnverifiedBrowser:
    calls: int = 0
    commit_authorized: bool | None = None

    def execute(self, **kwargs):
        self.calls += 1
        self.commit_authorized = kwargs["commit_authorized"]
        return ResultEnvelope(
            {
                "verdict": AutopilotVerdict.UNVERIFIED.value,
                "message": "no goal-specific receipt",
                "evidence": [],
            }
        )

    def resume(self, **_kwargs):
        raise AssertionError("no child task exists in this stub")


@dataclass
class PauseThenSucceedBrowser:
    execute_calls: int = 0
    resume_calls: int = 0

    def execute(self, **_kwargs):
        self.execute_calls += 1
        return ResultEnvelope(
            {
                "verdict": AutopilotVerdict.NEEDS_INPUT.value,
                "message": "verified profile fact required",
                "evidence": [],
            }
        )

    def resume(self, **_kwargs):
        self.resume_calls += 1
        return ResultEnvelope(
            {
                "verdict": AutopilotVerdict.VERIFIED_SUCCESS.value,
                "message": "receipt captured",
                "evidence": [
                    {
                        "kind": "submit",
                        "external_id": "receipt-1",
                        "evidence_sha256": "f" * 64,
                    }
                ],
            }
        )


@dataclass
class QueryCaptureBrowser:
    query: str | None = None

    def execute(self, **kwargs):
        self.query = kwargs["query"]
        return ResultEnvelope(
            {
                "verdict": AutopilotVerdict.VERIFIED_SUCCESS.value,
                "message": "receipt captured",
                "evidence": [
                    {
                        "kind": "submit",
                        "external_id": "receipt-1",
                        "evidence_sha256": "f" * 64,
                    }
                ],
            }
        )

    def resume(self, **_kwargs):
        raise AssertionError("no child task exists in this stub")


def research_plan() -> MissionPlan:
    return MissionPlan(
        summary="Research three independent facts, then synthesize.",
        steps=(
            MissionPlanStep(
                key="pricing",
                kind=MissionStepKind.RESEARCH,
                instruction="official pricing",
            ),
            MissionPlanStep(
                key="reliability",
                kind=MissionStepKind.RESEARCH,
                instruction="official reliability",
            ),
            MissionPlanStep(
                key="limits",
                kind=MissionStepKind.RESEARCH,
                instruction="official limits",
            ),
            MissionPlanStep(
                key="comparison",
                kind=MissionStepKind.SYNTHESIS,
                instruction="Compare the evidence",
                depends_on=("pricing", "reliability", "limits"),
            ),
        ),
    )


def test_mission_runs_independent_searches_concurrently_and_persists_citations(
    store,
) -> None:
    researcher = ConcurrentResearcher()
    synthesizer = CitedSynthesizer()
    heartbeat_threads: list[str] = []
    original_renew = store.renew_mission_lease

    def tracked_renew(**kwargs):
        if threading.current_thread().name.startswith("mission-heartbeat"):
            heartbeat_threads.append(threading.current_thread().name)
        return original_renew(**kwargs)

    store.renew_mission_lease = tracked_renew
    coordinator = MissionCoordinator(
        store=store,
        autopilot=NoBrowser(),
        settings=Settings(),
        plan_provider=StaticPlanner(research_plan()),
        researcher=researcher,
        synthesizer=synthesizer,
        max_parallel_research=3,
        lease_heartbeat_seconds=0.02,
    )

    result = coordinator.execute(
        tenant_id=TENANT,
        query="Research pricing, reliability, and limits, then compare them.",
    )

    assert result.verdict is MissionVerdict.COMPLETED
    assert result.mission.status is MissionStatus.SUCCEEDED
    assert researcher.maximum_active >= 2
    assert heartbeat_threads
    assert set(researcher.calls) == {
        "official pricing",
        "official reliability",
        "official limits",
    }
    assert synthesizer.calls == 1
    assert len(result.citation_urls) == 3
    assert all(step.status is MissionStepStatus.SUCCEEDED for step in result.steps)
    assert all(step.output_sha256 is not None for step in result.steps)
    assert store.verify_audit(TENANT).valid is True
    assert {event.kind for event in store.mission_events(TENANT, result.mission.id)} >= {
        "mission.created",
        "mission.step_started",
        "mission.step_completed",
        "mission.succeeded",
    }


def test_resume_retries_only_interrupted_read_step_and_keeps_completed_output(
    store,
) -> None:
    plan = MissionPlan(
        summary="Two durable searches.",
        steps=(
            MissionPlanStep(
                key="first",
                kind=MissionStepKind.RESEARCH,
                instruction="first source",
            ),
            MissionPlanStep(
                key="second",
                kind=MissionStepKind.RESEARCH,
                instruction="second source",
            ),
        ),
    )
    mission = store.create_mission(
        mission_id=uuid4(),
        tenant_id=TENANT,
        query="Research two facts.",
        provider="test",
        plan=plan,
        external_commit_authorized=False,
    )
    store.claim_mission(
        tenant_id=TENANT,
        mission_id=mission.id,
        owner="crashed-worker",
        lease_seconds=0,
    )
    first, second = store.list_mission_steps(TENANT, mission.id)
    store.start_mission_steps(
        tenant_id=TENANT,
        mission_id=mission.id,
        step_ids=(first.id,),
    )
    first_output = ResearchEvidence(
        query="first source",
        summary="already complete",
        citation_urls=("https://evidence.example/first",),
        provider_response_sha256="c" * 64,
    ).model_dump(mode="json")
    store.complete_mission_step(
        tenant_id=TENANT,
        mission_id=mission.id,
        step_id=first.id,
        output=first_output,
    )
    store.start_mission_steps(
        tenant_id=TENANT,
        mission_id=mission.id,
        step_ids=(second.id,),
    )
    researcher = ConcurrentResearcher()
    coordinator = MissionCoordinator(
        store=store,
        autopilot=NoBrowser(),
        settings=Settings(),
        plan_provider=StaticPlanner(plan),
        researcher=researcher,
    )

    result = coordinator.run(tenant_id=TENANT, mission_id=mission.id)

    assert result.verdict is MissionVerdict.COMPLETED
    assert researcher.calls == ["second source"]
    assert result.steps[0].output == first_output
    lease_events = [
        event
        for event in store.mission_events(TENANT, mission.id)
        if event.kind == "mission.lease_acquired"
    ]
    assert lease_events[-1].payload["recovered_running_steps"] == 1


@pytest.mark.parametrize(
    ("steps", "match"),
    [
        (
            (
                MissionPlanStep(
                    key="search",
                    kind=MissionStepKind.RESEARCH,
                    instruction="research only",
                ),
            ),
            "exactly one browser step",
        ),
        (
            (
                MissionPlanStep(
                    key="one",
                    kind=MissionStepKind.BROWSER,
                    instruction="submit first",
                ),
                MissionPlanStep(
                    key="two",
                    kind=MissionStepKind.BROWSER,
                    instruction="submit second",
                ),
            ),
            "exactly one browser step",
        ),
    ],
)
def test_decomposition_cannot_remove_or_multiply_parent_commit_authority(
    store,
    steps,
    match,
) -> None:
    coordinator = MissionCoordinator(
        store=store,
        autopilot=NoBrowser(),
        settings=Settings(),
        plan_provider=StaticPlanner(
            MissionPlan(summary="Invalid authority plan.", steps=steps)
        ),
    )

    with pytest.raises(ProviderError, match=match):
        coordinator.execute(
            tenant_id=TENANT,
            query="Submit the requested operation.",
        )

    assert store.list_missions(TENANT) == []


def test_read_only_mission_still_allows_at_most_one_browser_child(store) -> None:
    plan = MissionPlan(
        summary="Invalid browser fan-out.",
        steps=(
            MissionPlanStep(
                key="one",
                kind=MissionStepKind.BROWSER,
                instruction="Inspect one page.",
            ),
            MissionPlanStep(
                key="two",
                kind=MissionStepKind.BROWSER,
                instruction="Inspect another page.",
            ),
        ),
    )
    coordinator = MissionCoordinator(
        store=store,
        autopilot=NoBrowser(),
        settings=Settings(),
        plan_provider=StaticPlanner(plan),
    )

    with pytest.raises(ProviderError, match="at most one browser step"):
        coordinator.execute(
            tenant_id=TENANT,
            query="Inspect two pages without submitting.",
        )


def test_unverified_browser_child_blocks_parent_without_claiming_success(
    store,
) -> None:
    browser = UnverifiedBrowser()
    plan = MissionPlan(
        summary="Inspect a dynamic page.",
        steps=(
            MissionPlanStep(
                key="inspect",
                kind=MissionStepKind.BROWSER,
                instruction="Inspect the rendered result.",
            ),
        ),
    )
    coordinator = MissionCoordinator(
        store=store,
        autopilot=browser,
        settings=Settings(),
        plan_provider=StaticPlanner(plan),
    )

    result = coordinator.execute(
        tenant_id=TENANT,
        query="Inspect https://example.com without submitting anything.",
    )

    assert result.verdict is MissionVerdict.BLOCKED
    assert result.mission.status is MissionStatus.BLOCKED
    assert result.steps[0].status is MissionStepStatus.BLOCKED
    assert result.steps[0].output["verdict"] == "unverified"
    assert browser.calls == 1
    assert browser.commit_authorized is False


def test_planner_browser_instruction_cannot_inject_the_child_target(
    store,
) -> None:
    browser = QueryCaptureBrowser()
    parent_query = "Submit the operation."
    plan = MissionPlan(
        summary="One browser operation.",
        steps=(
            MissionPlanStep(
                key="submit",
                kind=MissionStepKind.BROWSER,
                instruction="Use https://planner-injected.example/commit",
            ),
        ),
    )
    coordinator = MissionCoordinator(
        store=store,
        autopilot=browser,
        settings=Settings(),
        plan_provider=StaticPlanner(plan),
    )

    result = coordinator.execute(tenant_id=TENANT, query=parent_query)

    assert result.verdict is MissionVerdict.VERIFIED_EFFECT
    assert browser.query == parent_query
    assert "planner-injected" not in browser.query


def test_paused_browser_mission_can_resume_after_underlying_gate_changes(
    store,
) -> None:
    browser = PauseThenSucceedBrowser()
    plan = MissionPlan(
        summary="One resumable browser operation.",
        steps=(
            MissionPlanStep(
                key="submit",
                kind=MissionStepKind.BROWSER,
                instruction="Submit after verified input exists.",
            ),
        ),
    )
    coordinator = MissionCoordinator(
        store=store,
        autopilot=browser,
        settings=Settings(),
        plan_provider=StaticPlanner(plan),
    )
    first = coordinator.execute(
        tenant_id=TENANT,
        query="Submit the operation at https://example.com.",
    )
    child_task_id = first.steps[0].child_task_id
    assert child_task_id is not None
    store.create_task(
        task_id=child_task_id,
        tenant_id=TENANT,
        instruction="Synthetic paused child.",
        start_url="https://example.com/",
        provider="test",
        actions=(
            ProposedAction(
                kind=ActionKind.FINISH,
                description="Synthetic terminal action.",
            ),
        ),
    )

    resumed = coordinator.run(
        tenant_id=TENANT,
        mission_id=first.mission.id,
    )

    assert first.verdict is MissionVerdict.NEEDS_INPUT
    assert resumed.verdict is MissionVerdict.VERIFIED_EFFECT
    assert resumed.mission.status is MissionStatus.SUCCEEDED
    assert browser.execute_calls == 1
    assert browser.resume_calls == 1
    assert any(
        event.kind == "mission.step_reopened"
        for event in store.mission_events(TENANT, first.mission.id)
    )


def test_failed_scheduler_can_resume_an_already_created_durable_child(store) -> None:
    plan = MissionPlan(
        summary="One interrupted browser operation.",
        steps=(
            MissionPlanStep(
                key="submit",
                kind=MissionStepKind.BROWSER,
                instruction="Submit once.",
            ),
        ),
    )
    mission = store.create_mission(
        mission_id=uuid4(),
        tenant_id=TENANT,
        query="Submit the operation at https://example.com.",
        provider="test",
        plan=plan,
        external_commit_authorized=True,
    )
    store.claim_mission(
        tenant_id=TENANT,
        mission_id=mission.id,
        owner="interrupted-worker",
    )
    step = store.list_mission_steps(TENANT, mission.id)[0]
    assert step.child_task_id is not None
    store.start_mission_steps(
        tenant_id=TENANT,
        mission_id=mission.id,
        step_ids=(step.id,),
    )
    store.create_task(
        task_id=step.child_task_id,
        tenant_id=TENANT,
        instruction=mission.query,
        start_url="https://example.com/",
        provider="test",
        actions=(
            ProposedAction(
                kind=ActionKind.FINISH,
                description="Synthetic child action.",
            ),
        ),
    )
    store.stop_mission_step(
        tenant_id=TENANT,
        mission_id=mission.id,
        step_id=step.id,
        status=MissionStepStatus.FAILED,
        error="worker interrupted after child creation",
    )
    store.finish_mission(
        tenant_id=TENANT,
        mission_id=mission.id,
        status=MissionStatus.FAILED,
        reason="synthetic scheduler interruption",
    )
    browser = PauseThenSucceedBrowser()
    coordinator = MissionCoordinator(
        store=store,
        autopilot=browser,
        settings=Settings(),
        plan_provider=StaticPlanner(plan),
    )

    result = coordinator.run(tenant_id=TENANT, mission_id=mission.id)

    assert result.verdict is MissionVerdict.VERIFIED_EFFECT
    assert browser.execute_calls == 0
    assert browser.resume_calls == 1


def test_scheduler_setup_failure_becomes_a_terminal_failed_mission(store) -> None:
    plan = MissionPlan(
        summary="Research cannot run without a remote provider.",
        steps=(
            MissionPlanStep(
                key="research",
                kind=MissionStepKind.RESEARCH,
                instruction="Search the web.",
            ),
        ),
    )
    coordinator = MissionCoordinator(
        store=store,
        autopilot=NoBrowser(),
        settings=Settings(),
        plan_provider=StaticPlanner(plan, name="deterministic"),
    )

    result = coordinator.execute(
        tenant_id=TENANT,
        query="Research one source.",
    )

    assert result.verdict is MissionVerdict.FAILED
    assert result.mission.status is MissionStatus.FAILED
    assert result.steps[0].status is MissionStepStatus.FAILED
    assert "grounded research requires" in result.steps[0].error


def test_reserved_browser_child_is_mission_owned_before_task_creation(store) -> None:
    plan = MissionPlan(
        summary="Reserve one browser child.",
        steps=(
            MissionPlanStep(
                key="browser",
                kind=MissionStepKind.BROWSER,
                instruction="Operate the site.",
            ),
        ),
    )
    mission = store.create_mission(
        mission_id=uuid4(),
        tenant_id=TENANT,
        query="Inspect the site.",
        provider="test",
        plan=plan,
        external_commit_authorized=False,
    )
    step = store.list_mission_steps(TENANT, mission.id)[0]

    assert step.child_task_id is not None
    assert store.mission_for_child_task(TENANT, step.child_task_id) == mission.id


def test_persisted_plan_tampering_is_rejected_before_resume(store) -> None:
    plan = MissionPlan(
        summary="One research step.",
        steps=(
            MissionPlanStep(
                key="research",
                kind=MissionStepKind.RESEARCH,
                instruction="Original instruction.",
            ),
        ),
    )
    mission = store.create_mission(
        mission_id=uuid4(),
        tenant_id=TENANT,
        query="Research one source.",
        provider="test",
        plan=plan,
        external_commit_authorized=False,
    )
    with store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE mission_steps SET instruction='Tampered instruction' "
                "WHERE mission_id=:mission_id"
            ),
            {"mission_id": str(mission.id)},
        )
    coordinator = MissionCoordinator(
        store=store,
        autopilot=NoBrowser(),
        settings=Settings(),
        plan_provider=StaticPlanner(plan),
        researcher=ConcurrentResearcher(),
    )

    with pytest.raises(ConflictError, match="does not match its audit commitment"):
        coordinator.run(tenant_id=TENANT, mission_id=mission.id)


def test_grounded_research_requires_tool_use_and_preserves_provider_citations(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "output": [
                    {"type": "web_search_call", "status": "completed"},
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {"summary": "Primary-source evidence."}
                                ),
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://docs.example/source",
                                    }
                                ],
                            }
                        ],
                    },
                ]
            },
        )

    monkeypatch.setenv("TEST_MISSION_API_KEY", "synthetic")
    researcher = ResponsesResearcher(
        ProviderRuntime(
            name="test",
            model="test-model",
            api_key_env="TEST_MISSION_API_KEY",
            base_url="https://provider.example/v1",
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = researcher.search("Find the official source.")

    assert captured["tools"] == [{"type": "web_search", "search_context_size": "medium"}]
    assert result.summary == "Primary-source evidence."
    assert result.citation_urls == ("https://docs.example/source",)


def test_responses_mission_planner_uses_a_strict_bounded_dag_schema(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "summary": "Research twice, then synthesize.",
                        "steps": [
                            {
                                "key": "first",
                                "kind": "research",
                                "instruction": "Find first evidence.",
                                "depends_on": [],
                            },
                            {
                                "key": "second",
                                "kind": "research",
                                "instruction": "Find second evidence.",
                                "depends_on": [],
                            },
                            {
                                "key": "answer",
                                "kind": "synthesis",
                                "instruction": "Compare both.",
                                "depends_on": ["first", "second"],
                            },
                        ],
                    }
                )
            },
        )

    monkeypatch.setenv("TEST_MISSION_API_KEY", "synthetic")
    planner = ResponsesMissionPlanner(
        ProviderRuntime(
            name="test",
            model="test-model",
            api_key_env="TEST_MISSION_API_KEY",
            base_url="https://provider.example/v1",
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    plan = planner.plan("Compare two sources.", external_commit_authorized=False)

    schema = captured["text"]["format"]["schema"]
    assert captured.get("tools") is None
    assert schema["properties"]["steps"]["maxItems"] == 8
    assert schema["additionalProperties"] is False
    assert [step.key for step in plan.steps] == ["first", "second", "answer"]


def test_synthesis_rejects_citations_not_present_in_dependency_evidence(
    monkeypatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "answer": "Unsupported conclusion.",
                        "citation_urls": ["https://invented.example/source"],
                    }
                )
            },
        )

    monkeypatch.setenv("TEST_MISSION_API_KEY", "synthetic")
    synthesizer = ResponsesSynthesizer(
        ProviderRuntime(
            name="test",
            model="test-model",
            api_key_env="TEST_MISSION_API_KEY",
            base_url="https://provider.example/v1",
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ProviderError, match="invented citations"):
        synthesizer.synthesize(
            "Summarize.",
            (
                {
                    "summary": "Grounded evidence.",
                    "citation_urls": ["https://allowed.example/source"],
                },
            ),
        )


def test_mission_plan_rejects_forward_dependencies() -> None:
    with pytest.raises(ValueError, match="topologically ordered"):
        MissionPlan(
            summary="Invalid graph.",
            steps=(
                MissionPlanStep(
                    key="summary",
                    kind=MissionStepKind.SYNTHESIS,
                    instruction="Summarize",
                    depends_on=("later",),
                ),
                MissionPlanStep(
                    key="later",
                    kind=MissionStepKind.RESEARCH,
                    instruction="Research later",
                ),
            ),
        )
