from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from effect_browser.domain import (
    ElementCandidate,
    Locator,
    PageSnapshot,
    PlanRequest,
    StepRequest,
    utc_now,
)
from effect_browser.providers.base import ProviderError
from effect_browser.providers.http import (
    PLAN_SCHEMA,
    STEP_SCHEMA,
    OpenAIPlanner,
    OpenAIReactivePlanner,
)


def test_responses_planner_returns_typed_plan(monkeypatch) -> None:
    action = {
        "kind": "navigate",
        "url": "https://example.test/start",
        "description": "Open the start page.",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["text"]["format"]["type"] == "json_schema"
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            200, json={"output_text": json.dumps({"actions": [action]})}
        )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    planner = OpenAIPlanner(
        "test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = planner.plan(
        PlanRequest(
            task_id=uuid4(),
            instruction="Open the start page.",
            start_url="https://example.test",
        )
    )

    assert planner.name == "openai"
    assert result[0].url == "https://example.test/start"


def test_provider_schema_closes_every_object() -> None:
    def objects(value):
        if isinstance(value, dict):
            if value.get("type") == "object" or "properties" in value:
                yield value
            for child in value.values():
                yield from objects(child)
        elif isinstance(value, list):
            for child in value:
                yield from objects(child)

    for schema in objects(PLAN_SCHEMA):
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema.get("properties", {}))


def test_provider_schema_references_resolve_at_root() -> None:
    references: list[str] = []

    def collect(value):
        if isinstance(value, dict):
            if "$ref" in value:
                references.append(value["$ref"])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(PLAN_SCHEMA)
    definitions = PLAN_SCHEMA.get("$defs", {})

    assert references
    assert all(reference.startswith("#/$defs/") for reference in references)
    assert all(
        reference.removeprefix("#/$defs/") in definitions for reference in references
    )

    references.clear()
    collect(STEP_SCHEMA)
    definitions = STEP_SCHEMA.get("$defs", {})
    assert references
    assert all(
        reference.removeprefix("#/$defs/") in definitions for reference in references
    )


def test_provider_http_error_is_safe_and_actionable(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "schema rejected without secret material"}},
        )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    planner = OpenAIPlanner(
        "test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ProviderError, match="HTTP 400: schema rejected"):
        planner.plan(
            PlanRequest(
                task_id=uuid4(),
                instruction="Plan safely.",
                start_url="https://example.test",
            )
        )


def test_reactive_provider_selects_one_fresh_candidate(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "choice": {
                            "kind": "click",
                            "candidate_id": "C001",
                            "value": None,
                            "url": None,
                            "description": "Open the application.",
                            "expected_outcome": None,
                        }
                    }
                )
            },
        )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    planner = OpenAIReactivePlanner(
        "test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    snapshot = PageSnapshot(
        url="https://jobs.example.test",
        title="Jobs",
        state_sha256="fresh",
        text_excerpt="Platform Reliability Engineer",
        candidates=(
            ElementCandidate(
                id="C001",
                tag="a",
                role="link",
                name="Apply",
                href="https://jobs.example.test/apply",
                interaction="navigation",
                locator=Locator(
                    selector="body > a",
                    adaptive_id="candidate-apply:0",
                ),
            ),
        ),
        captured_at=utc_now(),
    )

    choice = planner.choose(
        StepRequest(
            task_id=uuid4(),
            instruction="Open the application.",
            start_url=snapshot.url,
            step_number=1,
            effect_reference="EB-TEST",
            previous_actions=(),
            snapshot=snapshot,
        )
    )

    assert choice.candidate_id == "C001"
    assert choice.kind.value == "click"


def test_remote_plan_cannot_authorize_a_local_upload(monkeypatch, tmp_path) -> None:
    local_path = (tmp_path / "resume.pdf").resolve()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "actions": [
                            {
                                "kind": "upload",
                                "locator": {"label": "Résumé"},
                                "file_path": str(local_path),
                                "document_sha256": "a" * 64,
                                "description": "Attach a local document.",
                            }
                        ]
                    }
                )
            },
        )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    planner = OpenAIPlanner(
        "test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(
        ProviderError, match="remote providers cannot authorize local file uploads"
    ):
        planner.plan(
            PlanRequest(
                task_id=uuid4(),
                instruction="Apply without exposing local file authority.",
                start_url="https://jobs.example.test",
            )
        )


def test_remote_reactive_choice_cannot_authorize_a_local_upload(
    monkeypatch, tmp_path
) -> None:
    local_path = (tmp_path / "resume.pdf").resolve()

    def handler(request: httpx.Request) -> httpx.Response:
        request_body = json.loads(request.content)
        user_content = request_body["input"][1]["content"]
        assert str(local_path) not in user_content
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "choice": {
                            "kind": "upload",
                            "candidate_id": "C001",
                            "value": None,
                            "file_path": str(local_path),
                            "document_sha256": "b" * 64,
                            "url": None,
                            "description": "Attach a local document.",
                            "expected_outcome": None,
                        }
                    }
                )
            },
        )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    planner = OpenAIReactivePlanner(
        "test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    snapshot = PageSnapshot(
        url="https://jobs.example.test/apply",
        title="Apply",
        state_sha256="fresh",
        text_excerpt="Attach your résumé.",
        candidates=(
            ElementCandidate(
                id="C001",
                tag="input",
                role="button",
                name="Résumé",
                input_type="file",
                interaction="upload",
                locator=Locator(
                    selector="body > input",
                    adaptive_id="candidate-resume:0",
                ),
            ),
        ),
        captured_at=utc_now(),
    )

    with pytest.raises(
        ProviderError, match="remote providers cannot authorize local file uploads"
    ):
        planner.choose(
            StepRequest(
                task_id=uuid4(),
                instruction="Attach the operator-selected document.",
                start_url=snapshot.url,
                step_number=1,
                effect_reference="EB-TEST",
                previous_actions=(),
                snapshot=snapshot,
            )
        )
