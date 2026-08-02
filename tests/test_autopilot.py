from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from effect_browser.autopilot import (
    AutopilotCoordinator,
    AutopilotVerdict,
    GroundedTargetResolver,
    ProviderRuntime,
    ResolvedTarget,
    TargetSource,
    decide_commit_authority,
    extract_url,
    resolve_document,
    validate_target_url,
)
from effect_browser.config import Settings
from effect_browser.domain import (
    ActionKind,
    AutonomyMode,
    AutonomyScope,
    PageSnapshot,
    PlanRequest,
    ProposedAction,
)
from effect_browser.policy import ActionPolicy
from effect_browser.providers import DeterministicPlanner
from effect_browser.providers.base import ProviderError
from effect_browser.store import ConflictError
from tests.conftest import BASE_URL, TENANT, FakeDriver, RemoteSystem


class FinishOnlyPlanner:
    name = "deterministic"

    def plan(self, request: PlanRequest) -> tuple[ProposedAction, ...]:
        return (
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=request.start_url,
                description="Open the target.",
            ),
            ProposedAction(
                kind=ActionKind.FINISH,
                description="Stop without performing the requested commit.",
            ),
        )


class ExpectedFinishPlanner:
    name = "deterministic"

    def plan(self, request: PlanRequest) -> tuple[ProposedAction, ...]:
        return (
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=request.start_url,
                description="Open the target.",
            ),
            ProposedAction(
                kind=ActionKind.FINISH,
                description="Verify the rendered service state.",
                expected_outcome="Service operational",
            ),
        )


class RenderedEvidenceDriver(FakeDriver):
    def snapshot(self) -> PageSnapshot:
        observation = self.observe()
        return PageSnapshot(
            url=observation.url,
            title="Service status",
            state_sha256=observation.state_sha256,
            text_excerpt="Service operational in all regions.",
            candidates=(),
            captured_at=observation.captured_at,
        )


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        allowed_origins=(BASE_URL,),
        artifacts_directory=tmp_path / "artifacts",
        provider="auto",
    )


INCIDENTAL_COMMIT_QUERIES = (
    "Research the best laptop in order to compare prices",
    "Research shipping costs and post the results in a table",
    "Compare three vendors and send me a summary",
    "Apply filters on the pricing page and read the results",
    "Find the best book about Rust and summarize it",
    "Read the train schedule",
    "Explain the sign of a failing disk",
    "Find the delete key documentation",
    "Research cancel culture",
    "Register the trend over time",
    "Review purchase order terminology",
    "Compare payment providers",
    "Research publishing platforms",
    "Check registration requirements",
    "Review reservation policies",
    "Compare scheduling software",
    "Inspect submission guidelines",
    "Research this company",
)


def test_query_only_run_proves_a_real_external_effect(
    service,
    tmp_path: Path,
) -> None:
    remote = RemoteSystem()
    coordinator = AutopilotCoordinator(
        service=service,
        settings=settings_for(tmp_path),
        planner_factory=lambda _runtime: DeterministicPlanner(),
        driver_factory=lambda _origins: FakeDriver(remote),
    )

    result = coordinator.execute(
        tenant_id=TENANT,
        query=f"Order three backup drives at {BASE_URL} without duplicates.",
        allow_external_commit=True,
    )

    assert result.verdict is AutopilotVerdict.VERIFIED_SUCCESS
    assert result.resolution.external_commit_granted is True
    assert result.resolution.commit_intent_detected is True
    assert result.resolution.external_commit_authorized is True
    assert result.resolution.target_source is TargetSource.USER_URL
    assert [item.kind for item in result.evidence].count(ActionKind.SUBMIT) == 1
    assert remote.commits == 1


def test_commit_language_without_explicit_grant_cannot_write(
    service,
    tmp_path: Path,
) -> None:
    remote = RemoteSystem()
    result = AutopilotCoordinator(
        service=service,
        settings=settings_for(tmp_path),
        planner_factory=lambda _runtime: DeterministicPlanner(),
        driver_factory=lambda _origins: FakeDriver(remote),
    ).execute(
        tenant_id=TENANT,
        query=f"Order three backup drives at {BASE_URL} without duplicates.",
    )

    created = next(
        event
        for event in service.store.events(TENANT, result.task.id)
        if event.kind == "task.created"
    )
    assert result.verdict is AutopilotVerdict.NEEDS_AUTHORITY
    assert result.resolution.external_commit_granted is False
    assert result.resolution.commit_intent_detected is True
    assert result.resolution.external_commit_authorized is False
    assert result.task.autonomy.max_external_commits == 0
    assert created.payload["authority_context"]["authorized"] is False
    assert created.payload["authority_context"]["caller_granted"] is False
    assert remote.commits == 0


@pytest.mark.parametrize(
    ("query", "caller_granted", "expected_grant", "expected_intent"),
    [
        ("Submit the form", False, False, True),
        ("Inspect the form", True, True, False),
    ],
)
def test_resume_reports_the_persisted_two_key_authority_decision(
    service,
    tmp_path: Path,
    query: str,
    caller_granted: bool,
    expected_grant: bool,
    expected_intent: bool,
) -> None:
    coordinator = AutopilotCoordinator(
        service=service,
        settings=settings_for(tmp_path),
        planner_factory=lambda _runtime: FinishOnlyPlanner(),
        driver_factory=lambda _origins: FakeDriver(RemoteSystem()),
    )
    first = coordinator.execute(
        tenant_id=TENANT,
        query=f"{query} at {BASE_URL}.",
        allow_external_commit=caller_granted,
    )

    resumed = coordinator.resume(tenant_id=TENANT, task_id=first.task.id)

    assert resumed.resolution.external_commit_granted is expected_grant
    assert resumed.resolution.commit_intent_detected is expected_intent
    assert resumed.resolution.external_commit_authorized is False


def test_resume_uses_effective_scope_for_legacy_task_without_authority_context(
    service,
    tmp_path: Path,
) -> None:
    task = service.create_task(
        tenant_id=TENANT,
        instruction=f"Submit the form at {BASE_URL}.",
        start_url=BASE_URL,
        planner=FinishOnlyPlanner(),
        autonomy=AutonomyScope(
            mode=AutonomyMode.BOUNDED,
            allow_query_target_origin=True,
            allow_external_commits=True,
            max_external_commits=1,
        ),
    )
    coordinator = AutopilotCoordinator(
        service=service,
        settings=settings_for(tmp_path),
        planner_factory=lambda _runtime: FinishOnlyPlanner(),
        driver_factory=lambda _origins: FakeDriver(RemoteSystem()),
    )

    resumed = coordinator.resume(tenant_id=TENANT, task_id=task.id)

    assert resumed.resolution.external_commit_granted is True
    assert resumed.resolution.commit_intent_detected is True
    assert resumed.resolution.external_commit_authorized is True


def test_resume_rejects_tampered_persisted_task_authority(
    service,
    tmp_path: Path,
) -> None:
    coordinator = AutopilotCoordinator(
        service=service,
        settings=settings_for(tmp_path),
        planner_factory=lambda _runtime: FinishOnlyPlanner(),
        driver_factory=lambda _origins: FakeDriver(RemoteSystem()),
    )
    first = coordinator.execute(
        tenant_id=TENANT,
        query=f"Inspect the form at {BASE_URL}.",
        allow_external_commit=True,
    )
    created = next(
        event
        for event in service.store.events(TENANT, first.task.id)
        if event.kind == "task.created"
    )
    payload = dict(created.payload)
    payload["authority_context"] = {
        **payload["authority_context"],
        "reason": "tampered authority decision",
    }
    with service.store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE audit_events SET payload=:payload "
                "WHERE task_id=:task_id AND kind='task.created'"
            ),
            {"payload": json.dumps(payload), "task_id": str(first.task.id)},
        )

    with pytest.raises(ConflictError, match="does not match its durable scope"):
        coordinator.resume(tenant_id=TENANT, task_id=first.task.id)


def test_contradictory_grant_is_rejected_before_document_or_target_resolution(
    service,
    tmp_path: Path,
) -> None:
    missing_document = tmp_path / "missing.pdf"
    coordinator = AutopilotCoordinator(
        service=service,
        settings=settings_for(tmp_path),
        planner_factory=lambda _runtime: FinishOnlyPlanner(),
        driver_factory=lambda _origins: FakeDriver(RemoteSystem()),
    )

    with pytest.raises(ValueError, match="contradicts"):
        coordinator.execute(
            tenant_id=TENANT,
            query=(
                f'Prepare only; do not submit using "{missing_document}" at {BASE_URL}.'
            ),
            allow_external_commit=True,
        )

    assert service.store.list_tasks(TENANT) == []


def test_model_finish_cannot_fake_a_requested_commit(service, tmp_path: Path) -> None:
    coordinator = AutopilotCoordinator(
        service=service,
        settings=settings_for(tmp_path),
        planner_factory=lambda _runtime: FinishOnlyPlanner(),
        driver_factory=lambda _origins: FakeDriver(RemoteSystem()),
    )

    result = coordinator.execute(
        tenant_id=TENANT,
        query=f"Submit the backup drive application at {BASE_URL}.",
        allow_external_commit=True,
    )

    assert result.task.status.value == "succeeded"
    assert result.verdict is AutopilotVerdict.UNVERIFIED
    assert "no authoritative external-effect receipt" in result.message


def test_model_finish_cannot_fake_a_read_only_outcome(service, tmp_path: Path) -> None:
    result = AutopilotCoordinator(
        service=service,
        settings=settings_for(tmp_path),
        planner_factory=lambda _runtime: FinishOnlyPlanner(),
        driver_factory=lambda _origins: FakeDriver(RemoteSystem()),
    ).execute(
        tenant_id=TENANT,
        query=f"Inspect the backup drive demo at {BASE_URL}.",
    )

    finish = next(item for item in result.evidence if item.kind is ActionKind.FINISH)
    assert result.verdict is AutopilotVerdict.UNVERIFIED
    assert finish.url == BASE_URL
    assert "no deterministic goal-specific receipt" in result.message


def test_expected_finish_with_rendered_evidence_is_verified(
    service, tmp_path: Path
) -> None:
    result = AutopilotCoordinator(
        service=service,
        settings=settings_for(tmp_path),
        planner_factory=lambda _runtime: ExpectedFinishPlanner(),
        driver_factory=lambda _origins: RenderedEvidenceDriver(RemoteSystem()),
    ).execute(
        tenant_id=TENANT,
        query=(
            f"Inspect the service status at {BASE_URL} and verify Service operational."
        ),
    )

    finish = next(item for item in result.evidence if item.kind is ActionKind.FINISH)
    assert result.verdict is AutopilotVerdict.VERIFIED_SUCCESS
    assert finish.external_id == "local-finish"
    assert "receipt-backed rendered evidence" in result.message


def test_finish_evidence_cannot_be_copied_from_untrusted_page_content(
    service,
    tmp_path: Path,
) -> None:
    coordinator = AutopilotCoordinator(
        service=service,
        settings=settings_for(tmp_path),
        planner_factory=lambda _runtime: ExpectedFinishPlanner(),
        driver_factory=lambda _origins: RenderedEvidenceDriver(RemoteSystem()),
    )

    with pytest.raises(ValueError, match="exact phrase from the user instruction"):
        coordinator.execute(
            tenant_id=TENANT,
            query=f"Inspect the service status at {BASE_URL}.",
        )


def test_browser_start_failure_returns_blocked_verdict(service, tmp_path: Path) -> None:
    def fail_to_start(_origins):
        raise RuntimeError("synthetic browser launch failure")

    result = AutopilotCoordinator(
        service=service,
        settings=settings_for(tmp_path),
        planner_factory=lambda _runtime: FinishOnlyPlanner(),
        driver_factory=fail_to_start,
    ).execute(
        tenant_id=TENANT,
        query=f"Inspect the backup drive demo at {BASE_URL}.",
    )

    assert result.verdict is AutopilotVerdict.BLOCKED
    assert result.task.status.value == "blocked"
    assert "success is not claimed" in result.message


def test_explicit_document_is_hashed_later_and_removed_from_provider_prompt(
    tmp_path: Path,
) -> None:
    document = tmp_path / "private resume.pdf"
    document.write_bytes(b"synthetic")

    instruction, selected = resolve_document(
        f'Apply using "{document}" at https://example.com/jobs/1',
        None,
    )

    assert selected == document.resolve()
    assert str(document) not in instruction
    assert "[approved local document]" in instruction


def test_url_free_resolver_never_receives_the_local_document_path(
    service,
    tmp_path: Path,
    monkeypatch,
) -> None:
    document = tmp_path / "private resume.pdf"
    document.write_bytes(b"synthetic")
    seen: list[str] = []
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    service.policy = ActionPolicy((BASE_URL,), (tmp_path,))

    class CapturingResolver:
        def resolve(self, query: str) -> ResolvedTarget:
            seen.append(query)
            return ResolvedTarget(
                start_url="https://example.com/jobs/1",
                source=TargetSource.GROUNDED_SEARCH,
                reason="synthetic grounded target",
            )

    result = AutopilotCoordinator(
        service=service,
        settings=Settings(
            _env_file=None,
            allowed_origins=(BASE_URL,),
            allowed_upload_roots=(tmp_path,),
            artifacts_directory=tmp_path / "artifacts",
            provider="grok-reactive",
        ),
        planner_factory=lambda _runtime: FinishOnlyPlanner(),
        driver_factory=lambda _origins: FakeDriver(RemoteSystem()),
        resolver_factory=lambda _runtime: CapturingResolver(),
    ).execute(
        tenant_id=TENANT,
        query=f'Apply using "{document}" to the example role.',
    )

    assert result.verdict is AutopilotVerdict.UNVERIFIED
    assert seen and str(document) not in seen[0]
    assert "[approved local document]" in seen[0]


def test_relative_document_path_is_not_silently_resolved_from_working_directory() -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        resolve_document('Apply using "resume.pdf".', None)


def test_generic_file_verb_does_not_attach_default_document(
    service,
    tmp_path: Path,
) -> None:
    document = tmp_path / "default resume.pdf"
    document.write_bytes(b"synthetic")
    result = AutopilotCoordinator(
        service=service,
        settings=Settings(
            _env_file=None,
            allowed_origins=(BASE_URL,),
            default_document_path=document,
            artifacts_directory=tmp_path / "artifacts",
        ),
        planner_factory=lambda _runtime: FinishOnlyPlanner(),
        driver_factory=lambda _origins: FakeDriver(RemoteSystem()),
    ).execute(
        tenant_id=TENANT,
        query=f"File a backup drive complaint at {BASE_URL}.",
    )

    assert result.resolution.document_sha256 is None
    assert result.task.autonomy.allow_file_uploads is False


def test_sole_profile_is_not_disclosed_without_query_or_config_opt_in(
    service,
    tmp_path: Path,
) -> None:
    service.store.create_profile(tenant_id=TENANT, name="Private profile")
    result = AutopilotCoordinator(
        service=service,
        settings=settings_for(tmp_path),
        planner_factory=lambda _runtime: FinishOnlyPlanner(),
        driver_factory=lambda _origins: FakeDriver(RemoteSystem()),
    ).execute(
        tenant_id=TENANT,
        query=f"Book a backup drive demonstration at {BASE_URL}.",
    )

    assert result.resolution.profile_id is None
    assert result.task.profile_id is None


@pytest.mark.parametrize(
    "query",
    [
        "Apply to this role",
        "Book the flight",
        *INCIDENTAL_COMMIT_QUERIES,
    ],
)
def test_language_alone_never_grants_external_commit_authority(query: str) -> None:
    decision = decide_commit_authority(query, caller_granted=False)

    assert decision.caller_granted is False
    assert decision.authorized is False


@pytest.mark.parametrize("query", INCIDENTAL_COMMIT_QUERIES)
def test_explicit_grant_does_not_authorize_incidental_research_language(
    query: str,
) -> None:
    decision = decide_commit_authority(query, caller_granted=True)

    assert decision.caller_granted is True
    assert decision.commit_intent_detected is False
    assert decision.authorized is False
    assert decision.matched_commit is None


@pytest.mark.parametrize(
    "query",
    [
        "Apply to this role",
        "Book the flight",
        "Order three backup drives",
        "Submit the completed form",
    ],
)
def test_explicit_grant_and_commit_intent_are_both_required(query: str) -> None:
    decision = decide_commit_authority(query, caller_granted=True)

    assert decision.caller_granted is True
    assert decision.commit_intent_detected is True
    assert decision.authorized is True


@pytest.mark.parametrize(
    "query",
    [
        "Prepare only; do not submit",
        "Fill it, but do not actually submit it",
        "Do not, under any circumstances, submit it",
        "Research laptops and buy nothing",
    ],
)
def test_explicit_grant_cannot_override_read_only_language(query: str) -> None:
    with pytest.raises(ValueError, match="contradicts"):
        decide_commit_authority(query, caller_granted=True)


def test_url_extraction_and_network_boundary() -> None:
    assert extract_url("Open https://example.com/jobs/1).") == (
        "https://example.com/jobs/1"
    )
    assert validate_target_url("https://example.com", ()) == "https://example.com/"
    with pytest.raises(ValueError, match="private or reserved"):
        validate_target_url("https://127.0.0.1:9000/private", ())
    assert validate_target_url(f"{BASE_URL}/local", (BASE_URL,)).endswith("/local")


def test_grounded_target_requires_web_search_and_keeps_citations(monkeypatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    payload = {
        "output": [
            {"type": "web_search_call", "status": "completed"},
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "resolved": True,
                                "start_url": "https://example.com/jobs/1",
                                "reason": "official listing found",
                            }
                        ),
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://example.com/jobs/1",
                            }
                        ],
                    }
                ],
            },
        ]
    }

    def respond(request: httpx.Request) -> httpx.Response:
        posted = json.loads(request.content)
        assert posted["tools"] == [{"type": "web_search", "search_context_size": "low"}]
        return httpx.Response(200, json=payload)

    resolver = GroundedTargetResolver(
        ProviderRuntime(
            name="grok-reactive",
            model="grok-test",
            api_key_env="XAI_API_KEY",
            base_url="https://api.x.ai/v1",
        ),
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    resolved = resolver.resolve("Apply to the example engineering role.")

    assert resolved.start_url == "https://example.com/jobs/1"
    assert resolved.source is TargetSource.GROUNDED_SEARCH
    assert resolved.research_urls == ("https://example.com/jobs/1",)


def test_grounded_target_rejects_model_memory_without_search(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    response = httpx.Response(
        200,
        json={
            "output_text": json.dumps(
                {
                    "resolved": True,
                    "start_url": "https://guessed.example",
                    "reason": "guess",
                }
            ),
            "output": [],
        },
    )
    resolver = GroundedTargetResolver(
        ProviderRuntime(
            name="openai-reactive",
            model="model",
            api_key_env="OPENAI_API_KEY",
            base_url="https://api.openai.com/v1",
        ),
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: response)),
    )

    with pytest.raises(ProviderError, match="without grounded"):
        resolver.resolve("Find the right site.")


def test_grounded_target_rejects_uncited_or_mismatched_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    response = httpx.Response(
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
                                {
                                    "resolved": True,
                                    "start_url": "https://guessed.example/task",
                                    "reason": "claimed result",
                                }
                            ),
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://different.example/source",
                                }
                            ],
                        }
                    ],
                },
            ]
        },
    )
    resolver = GroundedTargetResolver(
        ProviderRuntime(
            name="openai-reactive",
            model="model",
            api_key_env="OPENAI_API_KEY",
            base_url="https://api.openai.com/v1",
        ),
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: response)),
    )

    with pytest.raises(ProviderError, match="does not match"):
        resolver.resolve("Find the right site.")
