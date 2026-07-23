from __future__ import annotations

import json
from uuid import uuid4

import httpx

from effect_browser.domain import PlanRequest
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
