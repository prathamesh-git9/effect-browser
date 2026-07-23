from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from effect_browser import api
from effect_browser.config import get_settings


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
    assert "The ledger decides" in dashboard.text


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
    assert len(listed.json()) == 1
    assert len(detail.json()["actions"]) == 6
    assert audit.json()["valid"] is True


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
