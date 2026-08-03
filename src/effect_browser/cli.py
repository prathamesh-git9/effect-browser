from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import UUID

import httpx
import typer
import uvicorn
from rich.console import Console

from effect_browser.autopilot import AutopilotCoordinator
from effect_browser.browser.playwright import PlaywrightDriver
from effect_browser.capabilities import capability_catalog
from effect_browser.config import get_settings
from effect_browser.domain import (
    ActionState,
    AutonomyMode,
    AutonomyScope,
    MissionVerdict,
    canonical_json,
)
from effect_browser.engine import (
    CrashAfterCommitDriver,
    EffectBrowserService,
    SimulatedProcessCrash,
)
from effect_browser.mission import MissionCoordinator
from effect_browser.policy import ActionPolicy
from effect_browser.providers import (
    DeterministicPlanner,
    GrokPlanner,
    GrokReactivePlanner,
    OpenAIPlanner,
    OpenAIReactivePlanner,
    ReactiveBootstrapPlanner,
)
from effect_browser.research import capture_research
from effect_browser.session import available_session_state_protector
from effect_browser.store import DatabaseStore

app = typer.Typer(
    no_args_is_help=True,
    help="Durable multi-search and crash-safe browser operations.",
)
console = Console()


def _ascii_safe_json(value: object) -> str:
    """Return one machine-readable JSON value safe for legacy Windows consoles."""

    parsed = json.loads(value) if isinstance(value, str) else value
    return json.dumps(
        parsed,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _print_json(value: object) -> None:
    typer.echo(_ascii_safe_json(value))


def _service() -> EffectBrowserService:
    settings = get_settings()
    store = DatabaseStore(settings.database_url)
    store.initialize()
    return EffectBrowserService(
        store,
        ActionPolicy(settings.allowed_origins, settings.allowed_upload_roots),
        step_planners={
            "openai-reactive": OpenAIReactivePlanner(settings.openai_model),
            "grok-reactive": GrokReactivePlanner(settings.grok_model),
        },
        session_protector=available_session_state_protector(
            encryption_key=settings.session_encryption_key,
            max_bytes=settings.session_state_max_bytes,
        ),
        session_retention_hours=settings.session_retention_hours,
    )


def _planner(name: str):
    settings = get_settings()
    values = {
        "deterministic": DeterministicPlanner(),
        "openai": OpenAIPlanner(settings.openai_model),
        "openai-reactive": ReactiveBootstrapPlanner("openai-reactive"),
        "grok": GrokPlanner(settings.grok_model),
        "grok-reactive": ReactiveBootstrapPlanner("grok-reactive"),
    }
    if name not in values:
        raise typer.BadParameter(
            "provider must be deterministic, openai-reactive, grok-reactive, "
            "openai, or grok"
        )
    return values[name]


def _driver(
    extra_allowed_origins: tuple[str, ...] = (),
    *,
    task_id: UUID | None = None,
    tenant_id: UUID | None = None,
) -> PlaywrightDriver:
    settings = get_settings()
    return PlaywrightDriver(
        executable_path=settings.browser_executable,
        headless=settings.browser_headless,
        sandbox=settings.browser_sandbox,
        artifacts_directory=settings.artifacts_directory,
        allowed_upload_roots=settings.allowed_upload_roots,
        allowed_upload_origins=settings.allowed_upload_origins,
        allowed_origins=(*settings.allowed_origins, *extra_allowed_origins),
    )


def _absolute_document_path(document_path: Path | None) -> Path | None:
    if document_path is None:
        return None
    if not document_path.is_absolute():
        raise typer.BadParameter("document_path must be absolute")
    return document_path.resolve()


@app.command("init")
def initialize() -> None:
    """Create missing database tables."""
    _service()
    console.print("[green]Effect Browser database is ready.[/green]")


@app.command("capabilities")
def capabilities() -> None:
    """Print the executor's actual typed capability and guarantee catalog."""
    _print_json([item.model_dump(mode="json") for item in capability_catalog()])


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000, min=1, max=65535),
    reload: bool = typer.Option(False),
) -> None:
    """Run the API, dashboard, and bundled demo portal."""
    uvicorn.run("effect_browser.api:app", host=host, port=port, reload=reload)


@app.command("create")
def create_task(
    instruction: str = typer.Argument(...),
    start_url: str = typer.Option("http://127.0.0.1:8000"),
    provider: str = typer.Option("deterministic"),
    profile_id: UUID | None = typer.Option(None),
    document_path: Path | None = typer.Option(None),
    document_sha256: str | None = typer.Option(None),
    autonomy_mode: AutonomyMode = typer.Option(AutonomyMode.SUPERVISED),
    allow_file_uploads: bool = typer.Option(False),
    allow_external_commits: bool = typer.Option(False),
    max_external_commits: int = typer.Option(0, min=0, max=3),
) -> None:
    """Plan and persist a browser task without executing it."""
    settings = get_settings()
    task = _service().create_task(
        tenant_id=settings.default_tenant_id,
        instruction=instruction,
        start_url=start_url,
        planner=_planner(provider),
        profile_id=profile_id,
        document_path=_absolute_document_path(document_path),
        document_sha256=document_sha256,
        autonomy=AutonomyScope(
            mode=autonomy_mode,
            allow_file_uploads=allow_file_uploads,
            allow_external_commits=allow_external_commits,
            max_external_commits=max_external_commits,
        ),
    )
    _print_json(task.model_dump_json())


@app.command("do")
def do_browser_task(
    query: str = typer.Argument(...),
    commit: bool = typer.Option(
        False,
        "--commit",
        help="Explicitly grant at most one reviewed external commit.",
    ),
) -> None:
    """Decompose and run one multi-search/browser mission from one query."""
    settings = get_settings()
    service = _service()
    result = MissionCoordinator(
        store=service.store,
        autopilot=AutopilotCoordinator(service=service, settings=settings),
        settings=settings,
        max_parallel_research=settings.mission_max_parallel_research,
    ).execute(
        tenant_id=settings.default_tenant_id,
        query=query,
        allow_external_commit=commit,
    )
    _print_json(result.model_dump_json())
    if result.verdict not in {
        MissionVerdict.COMPLETED,
        MissionVerdict.VERIFIED_EFFECT,
    }:
        raise typer.Exit(2)


@app.command("mission")
def run_mission(
    query: str = typer.Argument(...),
    commit: bool = typer.Option(
        False,
        "--commit",
        help="Explicitly grant at most one reviewed external commit.",
    ),
) -> None:
    """Alias for `do`; retained to make the durable mission boundary explicit."""
    do_browser_task(query, commit)


@app.command("replay-mission")
def replay_mission(mission_id: UUID) -> None:
    """Print a deterministic, redacted parent/child audit timeline."""
    settings = get_settings()
    timeline = _service().store.mission_timeline(
        settings.default_tenant_id,
        mission_id,
    )
    _print_json(canonical_json(timeline))
    if not timeline["audit"]["valid"]:
        raise typer.Exit(2)


@app.command("run")
def run_task(task_id: UUID) -> None:
    """Run safe actions until approval, recovery, failure, or completion."""
    settings = get_settings()
    service = _service()
    task = service.store.get_task(settings.default_tenant_id, task_id)
    if mission_id := service.store.mission_for_child_task(
        settings.default_tenant_id,
        task_id,
    ):
        raise typer.BadParameter(
            f"task is owned by mission {mission_id}; run the parent mission"
        )
    extra_origins = (task.start_url,) if task.autonomy.allow_query_target_origin else ()
    browser = _driver(
        extra_origins,
        task_id=task.id,
        tenant_id=settings.default_tenant_id,
    )
    try:
        result = service.run(
            tenant_id=settings.default_tenant_id,
            task_id=task_id,
            driver=browser,
        )
    finally:
        browser.close()
    _print_json(result.model_dump_json())


@app.command("research")
def research(
    question: str = typer.Argument(...),
    urls: list[str] = typer.Argument(..., help="One to five allowlisted HTTP(S) URLs."),
) -> None:
    """Capture cited rendered source evidence without submitting forms."""
    browser = _driver()
    try:
        report = capture_research(
            question=question,
            urls=tuple(urls),
            driver=browser,
            policy=_service().policy,
        )
    finally:
        browser.close()
    _print_json(report.model_dump_json())


@app.command()
def approve(
    action_id: UUID,
    expected_version: int = typer.Option(..., min=1),
    actor: str = typer.Option("cli-operator"),
) -> None:
    """Approve the exact prepared action and bound page observation."""
    settings = get_settings()
    result = _service().store.approve_action(
        tenant_id=settings.default_tenant_id,
        action_id=action_id,
        expected_version=expected_version,
        actor_id=actor,
    )
    _print_json(result.model_dump_json())


@app.command("resume-input")
def resume_input(
    action_id: UUID,
    expected_version: int = typer.Option(..., min=1),
    actor: str = typer.Option("cli-operator"),
) -> None:
    """Resume re-planning after the required profile fact or human step is resolved."""
    settings = get_settings()
    result = _service().store.resolve_input(
        tenant_id=settings.default_tenant_id,
        action_id=action_id,
        expected_version=expected_version,
        actor_id=actor,
    )
    _print_json(result.model_dump_json())


@app.command()
def reconcile(action_id: UUID) -> None:
    """Look up deterministic target evidence for an unknown outcome."""
    settings = get_settings()
    service = _service()
    action = service.store.get_action(settings.default_tenant_id, action_id)
    task = service.store.get_task(settings.default_tenant_id, action.task_id)
    extra_origins = (task.start_url,) if task.autonomy.allow_query_target_origin else ()
    browser = _driver(
        extra_origins,
        task_id=task.id,
        tenant_id=settings.default_tenant_id,
    )
    try:
        receipt = service.reconcile(
            tenant_id=settings.default_tenant_id,
            action_id=action_id,
            driver=browser,
        )
    finally:
        browser.close()
    _print_json(receipt.model_dump_json() if receipt else {"found": False})


@app.command("killer-demo")
def killer_demo(
    base_url: str = typer.Option("http://127.0.0.1:8000"),
) -> None:
    """Prove a crash after remote commit does not cause a duplicate submit."""
    settings = get_settings()
    service = _service()
    task = service.create_task(
        tenant_id=settings.default_tenant_id,
        instruction="Order three encrypted backup drives without a duplicate order.",
        start_url=base_url,
        planner=DeterministicPlanner(),
    )
    first = _driver(
        task_id=task.id,
        tenant_id=settings.default_tenant_id,
    )
    try:
        paused = service.run(
            tenant_id=settings.default_tenant_id,
            task_id=task.id,
            driver=first,
        )
    finally:
        first.close()
    action = paused.next_action
    if action is None or action.state is not ActionState.APPROVAL_REQUIRED:
        raise RuntimeError("demo did not stop at the commit boundary")
    console.print(f"[yellow]Paused before commit:[/yellow] {action.action_sha256[:16]}…")
    service.store.approve_action(
        tenant_id=settings.default_tenant_id,
        action_id=action.id,
        expected_version=action.version,
        actor_id="killer-demo-operator",
    )

    crashing = CrashAfterCommitDriver(
        _driver(
            task_id=task.id,
            tenant_id=settings.default_tenant_id,
        )
    )
    try:
        service.run(
            tenant_id=settings.default_tenant_id,
            task_id=task.id,
            driver=crashing,
        )
    except SimulatedProcessCrash:
        console.print("[red]Injected crash after the portal committed.[/red]")
    finally:
        crashing.close()

    recovery = _driver(
        task_id=task.id,
        tenant_id=settings.default_tenant_id,
    )
    try:
        stopped = service.run(
            tenant_id=settings.default_tenant_id,
            task_id=task.id,
            driver=recovery,
        )
        unknown = stopped.next_action
        if unknown is None or unknown.state is not ActionState.OUTCOME_UNKNOWN:
            raise RuntimeError("interrupted dispatch did not become outcome_unknown")
        console.print(
            "[yellow]Restart refused to click again; reconciling receipt.[/yellow]"
        )
        receipt = service.reconcile(
            tenant_id=settings.default_tenant_id,
            action_id=unknown.id,
            driver=recovery,
        )
    finally:
        recovery.close()
    if receipt is None:
        raise RuntimeError("target receipt could not be reconciled")

    final_browser = _driver(
        task_id=task.id,
        tenant_id=settings.default_tenant_id,
    )
    try:
        final = service.run(
            tenant_id=settings.default_tenant_id,
            task_id=task.id,
            driver=final_browser,
        )
    finally:
        final_browser.close()
    orders = httpx.get(f"{base_url.rstrip('/')}/demo-shop/api/orders", timeout=10).json()
    matching = [
        item for item in orders if item["reference"] == action.proposal.effect_key
    ]
    console.print(
        f"[bold green]Result:[/bold green] status={final.task.status.value}, "
        f"orders={len(matching)}, duplicate_attempts={matching[0]['duplicate_attempts']}"
    )
    if len(matching) != 1 or matching[0]["duplicate_attempts"] != 0:
        raise typer.Exit(1)


@app.command()
def worker(
    poll_seconds: float = typer.Option(2.0, min=0.1),
    once: bool = typer.Option(False, help="Run one polling cycle and exit."),
) -> None:
    """Run queued missions and tasks; every recorded authority gate remains active."""
    settings = get_settings()
    service = _service()
    missions = MissionCoordinator(
        store=service.store,
        autopilot=AutopilotCoordinator(service=service, settings=settings),
        settings=settings,
        max_parallel_research=settings.mission_max_parallel_research,
    )
    console.print(
        "[green]Worker started; approval and recovery gates remain enforced.[/green]"
    )
    while True:
        runnable_missions = [
            mission
            for mission in service.store.list_missions(settings.default_tenant_id)
            if mission.status.value in {"queued", "running"}
        ]
        for mission in runnable_missions:
            try:
                result = missions.run(
                    tenant_id=settings.default_tenant_id,
                    mission_id=mission.id,
                )
                console.print(f"mission {mission.id}: {result.message}")
            except Exception as exc:
                console.print(
                    f"[red]mission {mission.id}: {type(exc).__name__}: {exc}[/red]"
                )
        runnable = [
            task
            for task in service.store.list_tasks(settings.default_tenant_id)
            if service.store.mission_for_child_task(
                settings.default_tenant_id,
                task.id,
            )
            is None
            and (
                task.status.value in {"queued", "running"}
                or (
                    task.status.value == "awaiting_approval"
                    and task.autonomy.mode is AutonomyMode.BOUNDED
                )
            )
        ]
        for task in runnable:
            extra_origins = (
                (task.start_url,) if task.autonomy.allow_query_target_origin else ()
            )
            try:
                browser = _driver(
                    extra_origins,
                    task_id=task.id,
                    tenant_id=settings.default_tenant_id,
                )
                try:
                    result = service.run(
                        tenant_id=settings.default_tenant_id,
                        task_id=task.id,
                        driver=browser,
                    )
                    console.print(f"{task.id}: {result.message}")
                finally:
                    browser.close()
            except Exception as exc:
                console.print(f"[red]{task.id}: {type(exc).__name__}: {exc}[/red]")
        if once:
            return
        time.sleep(poll_seconds)


@app.command()
def mcp() -> None:
    """Run the safe stdio MCP server."""
    from effect_browser.mcp_server import run

    run()


if __name__ == "__main__":
    app()
