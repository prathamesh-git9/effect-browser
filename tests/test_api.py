from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from effect_browser import api
from effect_browser.config import get_settings
from effect_browser.domain import (
    ActionKind,
    MissionPlan,
    MissionPlanStep,
    MissionStepKind,
    ProposedAction,
)


def client_for(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv(
        "EFFECT_BROWSER_DATABASE_URL",
        f"sqlite:///{tmp_path / 'api.db'}",
    )
    get_settings.cache_clear()
    api.get_store.cache_clear()
    return TestClient(api.app)


def test_health_ui_and_request_id(tmp_path: Path, monkeypatch) -> None:
    with client_for(tmp_path, monkeypatch) as client:
        health = client.get("/healthz", headers={"X-Request-ID": "known-request"})
        dashboard = client.get("/")

    assert health.json() == {"status": "ok"}
    assert health.headers["X-Request-ID"] == "known-request"
    assert "Say the outcome" in dashboard.text


def test_unmatched_metrics_paths_use_one_bounded_label(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with client_for(tmp_path, monkeypatch) as client:
        client.get("/random-scanner-path-one")
        client.get("/random-scanner-path-two")
        metrics = client.get("/metrics")

    assert 'path="<unmatched>"' in metrics.text
    assert "random-scanner-path-one" not in metrics.text
    assert "random-scanner-path-two" not in metrics.text


def test_create_list_detail_and_audit(tmp_path: Path, monkeypatch) -> None:
    with client_for(tmp_path, monkeypatch) as client:
        created = client.post(
            "/v1/tasks",
            json={
                "instruction": "Create exactly one demo order.",
                "start_url": "http://127.0.0.1:8000",
                "provider": "deterministic",
            },
        )
        task_id = created.json()["id"]
        listed = client.get("/v1/tasks")
        detail = client.get(f"/v1/tasks/{task_id}")
        audit = client.get("/v1/audit/verify")

    assert created.status_code == 201
    assert created.json()["autonomy"] == {
        "mode": "supervised",
        "allow_query_target_origin": False,
        "allow_file_uploads": False,
        "allow_external_commits": False,
        "max_external_commits": 0,
    }
    assert len(listed.json()) == 1
    assert len(detail.json()["actions"]) == 6
    assert audit.json()["valid"] is True


def test_api_records_bounded_authority_and_exposes_capabilities(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with client_for(tmp_path, monkeypatch) as client:
        created = client.post(
            "/v1/tasks",
            json={
                "instruction": "Run one reviewed synthetic commit unattended.",
                "provider": "deterministic",
                "autonomy": {
                    "mode": "bounded",
                    "allow_file_uploads": False,
                    "allow_external_commits": True,
                    "max_external_commits": 1,
                },
            },
        )
        capabilities = client.get("/v1/capabilities")
        invalid = client.post(
            "/v1/tasks",
            json={
                "instruction": "Invalid unbounded authority.",
                "provider": "deterministic",
                "autonomy": {
                    "mode": "supervised",
                    "allow_external_commits": True,
                    "max_external_commits": 1,
                },
            },
        )
        reserved_origin = client.post(
            "/v1/tasks",
            json={
                "instruction": "Try to bypass target validation.",
                "start_url": "http://127.0.0.1:9999/private",
                "provider": "deterministic",
                "autonomy": {
                    "mode": "bounded",
                    "allow_query_target_origin": True,
                },
            },
        )

    assert created.status_code == 201
    assert created.json()["autonomy"]["mode"] == "bounded"
    assert created.json()["autonomy"]["max_external_commits"] == 1
    assert capabilities.status_code == 200
    assert {item["kind"] for item in capabilities.json()} >= {
        "check",
        "press",
        "scroll",
        "wait",
        "download",
        "submit",
    }
    assert invalid.status_code == 422
    assert reserved_origin.status_code == 422
    assert "reserved for the validated" in reserved_origin.text


def test_autopilot_api_defaults_read_only_and_forwards_explicit_grant(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class StubCoordinator:
        def execute(self, *, tenant_id, query, allow_external_commit):
            return {
                "tenant_id": str(tenant_id),
                "query": query,
                "allow_external_commit": allow_external_commit,
                "verdict": "verified_success",
            }

    api.app.dependency_overrides[api.get_autopilot] = lambda: StubCoordinator()
    try:
        with client_for(tmp_path, monkeypatch) as client:
            response = client.post(
                "/v1/autopilot",
                json={"query": "Inspect the status at https://example.com/status."},
            )
            granted = client.post(
                "/v1/autopilot",
                json={
                    "query": "Submit at https://example.com/form.",
                    "allow_external_commit": True,
                },
            )
    finally:
        api.app.dependency_overrides.pop(api.get_autopilot, None)

    assert response.status_code == 200
    assert response.json()["query"].startswith("Inspect the status")
    assert response.json()["allow_external_commit"] is False
    assert response.json()["verdict"] == "verified_success"
    assert granted.status_code == 200
    assert granted.json()["allow_external_commit"] is True


def test_mission_api_accepts_one_query_and_returns_the_persisted_dag(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class StubCoordinator:
        def execute(self, *, tenant_id, query, allow_external_commit):
            return {
                "mission": {
                    "id": "40000000-0000-0000-0000-000000000004",
                    "tenant_id": str(tenant_id),
                    "query": query,
                    "status": "succeeded",
                },
                "allow_external_commit": allow_external_commit,
                "steps": [
                    {
                        "key": "source_one",
                        "kind": "research",
                        "status": "succeeded",
                    },
                    {
                        "key": "source_two",
                        "kind": "research",
                        "status": "succeeded",
                    },
                ],
                "verdict": "completed",
            }

    api.app.dependency_overrides[api.get_mission_coordinator] = lambda: StubCoordinator()
    try:
        with client_for(tmp_path, monkeypatch) as client:
            response = client.post(
                "/v1/missions",
                json={"query": "Research two official sources and compare them."},
            )
    finally:
        api.app.dependency_overrides.pop(api.get_mission_coordinator, None)

    assert response.status_code == 200
    assert response.json()["allow_external_commit"] is False
    assert response.json()["verdict"] == "completed"
    assert [step["kind"] for step in response.json()["steps"]] == [
        "research",
        "research",
    ]


def test_mission_api_rejects_commit_grant_that_contradicts_query(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with client_for(tmp_path, monkeypatch) as client:
        response = client.post(
            "/v1/missions",
            json={
                "query": (
                    "Prepare only; do not submit at http://127.0.0.1:8000/demo-shop."
                ),
                "allow_external_commit": True,
            },
        )
        missions = client.get("/v1/missions")

    assert response.status_code == 422
    assert "contradicts" in response.json()["error"]["detail"]
    assert missions.json() == []


def test_mission_owned_child_cannot_run_through_generic_task_endpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    with client_for(tmp_path, monkeypatch) as client:
        store = api.get_store()
        mission = store.create_mission(
            mission_id=uuid4(),
            tenant_id=get_settings().default_tenant_id,
            query="Inspect the target.",
            provider="test",
            plan=MissionPlan(
                summary="One owned child.",
                steps=(
                    MissionPlanStep(
                        key="browser",
                        kind=MissionStepKind.BROWSER,
                        instruction="Inspect the target.",
                    ),
                ),
            ),
            external_commit_authorized=False,
        )
        step = store.list_mission_steps(
            get_settings().default_tenant_id,
            mission.id,
        )[0]
        assert step.child_task_id is not None
        store.create_task(
            task_id=step.child_task_id,
            tenant_id=get_settings().default_tenant_id,
            instruction="Synthetic child.",
            start_url="https://example.com/",
            provider="test",
            actions=(
                ProposedAction(
                    kind=ActionKind.FINISH,
                    description="Synthetic finish.",
                ),
            ),
        )

        detail = client.get(f"/v1/tasks/{step.child_task_id}")
        run = client.post(f"/v1/tasks/{step.child_task_id}/run")

    assert detail.status_code == 200
    assert detail.json()["parent_mission_id"] == str(mission.id)
    assert run.status_code == 409
    assert "resume the parent mission" in run.text


def test_cross_tenant_task_is_hidden(tmp_path: Path, monkeypatch) -> None:
    with client_for(tmp_path, monkeypatch) as client:
        created = client.post(
            "/v1/tasks",
            json={"instruction": "Plan only.", "provider": "deterministic"},
        )
        response = client.get(
            f"/v1/tasks/{created.json()['id']}",
            headers={"X-Tenant-ID": "30000000-0000-0000-0000-000000000003"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_profile_api_preserves_answer_metadata_and_hides_cross_tenant(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stranger = {"X-Tenant-ID": "30000000-0000-0000-0000-000000000003"}
    with client_for(tmp_path, monkeypatch) as client:
        created = client.post("/v1/profiles", json={"name": "Synthetic facts"})
        profile_id = created.json()["id"]
        answer = client.put(
            f"/v1/profiles/{profile_id}/answers/work_authorization",
            headers={"X-Actor-ID": "test-user"},
            json={
                "value": "synthetic-authorized",
                "source": {
                    "kind": "document",
                    "reference": "synthetic-document-001",
                },
                "sensitivity": "consequential",
                "verification_state": "verified",
            },
        )
        detail = client.get(f"/v1/profiles/{profile_id}")
        listed = client.get("/v1/profiles")
        hidden = client.get(f"/v1/profiles/{profile_id}", headers=stranger)
        blocked_write = client.put(
            f"/v1/profiles/{profile_id}/answers/country",
            headers=stranger,
            json={
                "value": "synthetic-country",
                "source": {"kind": "user"},
                "sensitivity": "personal",
            },
        )

    assert created.status_code == 201
    assert answer.status_code == 200
    assert answer.json()["source"] == {
        "kind": "document",
        "reference": "synthetic-document-001",
    }
    assert answer.json()["sensitivity"] == "consequential"
    assert answer.json()["verification_state"] == "verified"
    assert answer.json()["verified_by"] == "test-user"
    assert detail.json()["answers"] == [answer.json()]
    assert [item["id"] for item in listed.json()] == [profile_id]
    assert hidden.status_code == 404
    assert blocked_write.status_code == 404


def test_task_binds_tenant_profile_and_allowlisted_document(
    tmp_path: Path,
    monkeypatch,
) -> None:
    document = tmp_path / "synthetic-resume.txt"
    document.write_bytes(b"synthetic profile document")
    document_sha256 = hashlib.sha256(document.read_bytes()).hexdigest()
    monkeypatch.setenv("EFFECT_BROWSER_ALLOWED_UPLOAD_ROOTS", str(tmp_path))
    stranger = {"X-Tenant-ID": "30000000-0000-0000-0000-000000000003"}

    with client_for(tmp_path, monkeypatch) as client:
        profile = client.post("/v1/profiles", json={"name": "Synthetic task facts"})
        created = client.post(
            "/v1/tasks",
            json={
                "instruction": "Prepare a synthetic application.",
                "provider": "openai-reactive",
                "profile_id": profile.json()["id"],
                "document_path": str(document.resolve()),
                "document_sha256": document_sha256,
            },
        )
        cross_tenant = client.post(
            "/v1/tasks",
            headers=stranger,
            json={
                "instruction": "Must not borrow another tenant's facts.",
                "provider": "openai-reactive",
                "profile_id": profile.json()["id"],
            },
        )

    assert created.status_code == 201
    assert created.json()["profile_id"] == profile.json()["id"]
    assert "document_path" not in created.json()
    assert cross_tenant.status_code == 404
