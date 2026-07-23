from __future__ import annotations

import json
import time
from uuid import UUID

import httpx
import typer
import uvicorn
from rich.console import Console

from effect_browser.browser.playwright import PlaywrightDriver
from effect_browser.config import get_settings
from effect_browser.domain import ActionState
from effect_browser.engine import (
    CrashAfterCommitDriver,
    EffectBrowserService,
    SimulatedProcessCrash,
)
from effect_browser.policy import ActionPolicy
from effect_browser.providers import DeterministicPlanner, GrokPlanner, OpenAIPlanner
from effect_browser.store import DatabaseStore

app = typer.Typer(no_args_is_help=True, help="Crash-safe browser operations.")
console = Console()


def _service() -> EffectBrowserService:
    settings = get_settings()
    store = DatabaseStore(settings.database_url)
    store.initialize()
    return EffectBrowserService(store, ActionPolicy(settings.allowed_origins))


def _planner(name: str):
    settings = get_settings()
    values = {
        "deterministic": DeterministicPlanner(),
        "openai": OpenAIPlanner(settings.openai_model),
        "grok": GrokPlanner(settings.grok_model),
    }
    if name not in values:
        raise typer.BadParameter("provider must be deterministic, openai, or grok")
    return values[name]


def _driver() -> PlaywrightDriver:
    settings = get_settings()
    return PlaywrightDriver(
        executable_path=settings.browser_executable,
        headless=settings.browser_headless,
        sandbox=settings.browser_sandbox,
        artifacts_directory=settings.artifacts_directory,
    )


@app.command("init")
def initialize() -> None:
    """Create missing database tables."""
    _service()
    console.print("[green]Effect Browser database is ready.[/green]")


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
) -> None:
    """Plan and persist a browser task without executing it."""
    settings = get_settings()
    task = _service().create_task(
        tenant_id=settings.default_tenant_id,
        instruction=instruction,
        start_url=start_url,
        planner=_planner(provider),
    )
    console.print_json(task.model_dump_json())


@app.command("run")
def run_task(task_id: UUID) -> None:
    """Run safe actions until approval, recovery, failure, or completion."""
    settings = get_settings()
    browser = _driver()
    try:
        result = _service().run(
            tenant_id=settings.default_tenant_id,
            task_id=task_id,
            driver=browser,
        )
    finally:
        browser.close()
    console.print_json(result.model_dump_json())


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
    console.print_json(result.model_dump_json())


@app.command()
def reconcile(action_id: UUID) -> None:
    """Look up deterministic target evidence for an unknown outcome."""
    settings = get_settings()
    browser = _driver()
    try:
        receipt = _service().reconcile(
            tenant_id=settings.default_tenant_id,
            action_id=action_id,
            driver=browser,
        )
    finally:
        browser.close()
    console.print_json(
        receipt.model_dump_json() if receipt else json.dumps({"found": False})
    )


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
    first = _driver()
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

    crashing = CrashAfterCommitDriver(_driver())
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

    recovery = _driver()
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

    final_browser = _driver()
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
    """Run queued work autonomously, stopping at approvals and unknown outcomes."""
    settings = get_settings()
    service = _service()
    console.print(
        "[green]Worker started; approval and recovery gates remain enforced.[/green]"
    )
    while True:
        runnable = [
            task
            for task in service.store.list_tasks(settings.default_tenant_id)
            if task.status.value in {"queued", "running"}
        ]
        for task in runnable:
            browser = _driver()
            try:
                result = service.run(
                    tenant_id=settings.default_tenant_id,
                    task_id=task.id,
                    driver=browser,
                )
                console.print(f"{task.id}: {result.message}")
            finally:
                browser.close()
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
