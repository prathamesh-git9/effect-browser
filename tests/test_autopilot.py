from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from effect_browser.autopilot import (
    AutopilotCoordinator,
    AutopilotVerdict,
    GroundedTargetResolver,
    ProviderRuntime,
    ResolvedTarget,
    TargetSource,
    extract_url,
    query_authorizes_commit,
    resolve_document,
    validate_target_url,
)
from effect_browser.config import Settings
from effect_browser.domain import ActionKind, PlanRequest, ProposedAction
from effect_browser.policy import ActionPolicy
from effect_browser.providers import DeterministicPlanner
from effect_browser.providers.base import ProviderError
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


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        allowed_origins=(BASE_URL,),
        artifacts_directory=tmp_path / "artifacts",
        provider="auto",
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
    )

    assert result.verdict is AutopilotVerdict.VERIFIED_SUCCESS
    assert result.resolution.external_commit_authorized is True
    assert result.resolution.target_source is TargetSource.USER_URL
    assert [item.kind for item in result.evidence].count(ActionKind.SUBMIT) == 1
    assert remote.commits == 1


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
    ("query", "expected"),
    [
        ("Apply to this role", True),
        ("Book the flight", True),
        ("Prepare only; do not submit", False),
        ("Fill it, but do not actually submit it", False),
        ("Do not, under any circumstances, submit it", False),
        ("Research this company", False),
    ],
)
def test_query_commit_authority_is_narrow(query: str, expected: bool) -> None:
    assert query_authorizes_commit(query) is expected


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
