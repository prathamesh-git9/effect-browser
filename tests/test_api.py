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
