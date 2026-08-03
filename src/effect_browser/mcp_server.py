from __future__ import annotations

from pathlib import Path
from uuid import UUID

from effect_browser.api import driver, get_service, planner
from effect_browser.autopilot import AutopilotCoordinator
from effect_browser.capabilities import capability_catalog
from effect_browser.config import get_settings
from effect_browser.domain import AutonomyScope
from effect_browser.mission import MissionCoordinator
from effect_browser.store import ConflictError


def _absolute_document_path(document_path: str | None) -> Path | None:
    if document_path is None:
        return None
    path = Path(document_path)
    if not path.is_absolute():
        raise ValueError("document_path must be absolute")
    return path.resolve()


def create_mcp_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("install effect-browser[mcp] to run the MCP server") from exc

    server = FastMCP("effect-browser")

    @server.tool()
    def do_browser_task(
        query: str,
        allow_external_commit: bool = False,
    ) -> dict:
        """Decompose and run one cited multi-search/browser mission."""
        settings = get_settings()
        service = get_service()
        result = MissionCoordinator(
            store=service.store,
            autopilot=AutopilotCoordinator(service=service, settings=settings),
            settings=settings,
            max_parallel_research=settings.mission_max_parallel_research,
        ).execute(
            tenant_id=settings.default_tenant_id,
            query=query,
            allow_external_commit=allow_external_commit,
        )
        return result.model_dump(mode="json")

    @server.tool()
    def get_browser_mission(mission_id: str) -> dict:
        """Inspect durable mission steps and hash-chained mission audit events."""
        settings = get_settings()
        service = get_service()
        coordinator = MissionCoordinator(
            store=service.store,
            autopilot=AutopilotCoordinator(service=service, settings=settings),
            settings=settings,
            max_parallel_research=settings.mission_max_parallel_research,
        )
        mission = UUID(mission_id)
        result = coordinator.inspect(
            tenant_id=settings.default_tenant_id,
            mission_id=mission,
        )
        return {
            "result": result.model_dump(mode="json"),
            "events": [
                event.model_dump(mode="json")
                for event in service.store.mission_events(
                    settings.default_tenant_id,
                    mission,
                )
            ],
        }

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
        resolved_document_path = _absolute_document_path(document_path)
        task = get_service().create_task(
            tenant_id=UUID(tenant_id),
            instruction=instruction,
            start_url=start_url,
            planner=planner(provider),
            profile_id=UUID(profile_id) if profile_id else None,
            document_path=(
                resolved_document_path.resolve()
                if resolved_document_path is not None
                else None
            ),
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
        tenant = UUID(tenant_id)
        task = service.store.get_task(tenant, UUID(task_id))
        if mission_id := service.store.mission_for_child_task(tenant, task.id):
            raise ConflictError(
                f"task is owned by mission {mission_id}; run the parent mission"
            )
        extra_origins = (
            (task.start_url,) if task.autonomy.allow_query_target_origin else ()
        )
        browser = driver(extra_origins, task_id=task.id, tenant_id=tenant)
        try:
            result = service.run(
                tenant_id=tenant,
                task_id=task.id,
                driver=browser,
            )
        finally:
            browser.close()
        return result.model_dump(mode="json")

    return server


def run() -> None:
    create_mcp_server().run()
