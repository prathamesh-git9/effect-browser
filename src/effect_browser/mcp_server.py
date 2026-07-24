from __future__ import annotations

from pathlib import Path
from uuid import UUID

from effect_browser.api import driver, get_service, planner
from effect_browser.capabilities import capability_catalog
from effect_browser.domain import AutonomyScope


def create_mcp_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("install effect-browser[mcp] to run the MCP server") from exc

    server = FastMCP("effect-browser")

    @server.tool()
    def create_browser_task(
        tenant_id: str,
        instruction: str,
        start_url: str,
        provider: str = "deterministic",
        profile_id: str | None = None,
        document_path: str | None = None,
        document_sha256: str | None = None,
        autonomy_mode: str = "supervised",
        allow_file_uploads: bool = False,
        allow_external_commits: bool = False,
        max_external_commits: int = 0,
    ) -> dict:
        """Plan and persist a browser task; this does not execute browser actions."""
        task = get_service().create_task(
            tenant_id=UUID(tenant_id),
            instruction=instruction,
            start_url=start_url,
            planner=planner(provider),
            profile_id=UUID(profile_id) if profile_id else None,
            document_path=Path(document_path).resolve() if document_path else None,
            document_sha256=document_sha256,
            autonomy=AutonomyScope(
                mode=autonomy_mode,
                allow_file_uploads=allow_file_uploads,
                allow_external_commits=allow_external_commits,
                max_external_commits=max_external_commits,
            ),
        )
        return task.model_dump(mode="json")

    @server.tool()
    def get_browser_task(tenant_id: str, task_id: str) -> dict:
        """Inspect durable task, action, approval, receipt, and audit state."""
        service = get_service()
        tenant = UUID(tenant_id)
        task = service.store.get_task(tenant, UUID(task_id))
        return {
            "task": task.model_dump(mode="json"),
            "actions": [
                item.model_dump(mode="json")
                for item in service.store.list_actions(tenant, task.id)
            ],
            "events": [
                item.model_dump(mode="json")
                for item in service.store.events(tenant, task.id)
            ],
        }

    @server.tool()
    def list_browser_tasks(tenant_id: str) -> list[dict]:
        """List task status without granting execution or approval authority."""
        return [
            item.model_dump(mode="json")
            for item in get_service().store.list_tasks(UUID(tenant_id))
        ]

    @server.tool()
    def list_browser_capabilities() -> list[dict]:
        """Return the executor primitives, approval rules, and honest guarantees."""
        return [item.model_dump(mode="json") for item in capability_catalog()]

    @server.tool()
    def run_browser_task(tenant_id: str, task_id: str) -> dict:
        """Run until completion or an honest authority, input, or recovery blocker."""
        service = get_service()
        browser = driver()
        try:
            result = service.run(
                tenant_id=UUID(tenant_id),
                task_id=UUID(task_id),
                driver=browser,
            )
        finally:
            browser.close()
        return result.model_dump(mode="json")

    return server


def run() -> None:
    create_mcp_server().run()
