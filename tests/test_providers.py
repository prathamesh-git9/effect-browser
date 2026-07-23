from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from effect_browser.domain import PlanRequest
from effect_browser.providers.base import ProviderError
from effect_browser.providers.http import PLAN_SCHEMA, OpenAIPlanner


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
