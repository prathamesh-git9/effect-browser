from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from effect_browser import api
from effect_browser.autopilot import AutopilotResult, AutopilotVerdict
from effect_browser.browser.playwright import PlaywrightDriver
from effect_browser.config import get_settings
from effect_browser.domain import (
    ActionKind,
    ActionState,
    MissionPlan,
    MissionPlanStep,
    MissionStepKind,
    MissionVerdict,
    ProposedAction,
    TaskStatus,
)
from effect_browser.engine import CrashAfterCommitDriver, SimulatedProcessCrash
from effect_browser.mission import (
    MissionCoordinator,
    MissionResult,
    ResearchEvidence,
    SynthesisEvidence,
)
from effect_browser.policy import ActionPolicy
from effect_browser.providers import DeterministicPlanner
from effect_browser.uploads import sha256_file


def free_port() -> int:
    # Chromium refuses a legacy blocklist of ports even on loopback. Letting the
    # OS occasionally select one made otherwise deterministic E2E tests flaky.
    unsafe = {
        2049,
        3659,
        4045,
        5060,
        5061,
        6000,
        6566,
        6665,
        6666,
        6667,
        6668,
        6669,
        6697,
        10080,
    }
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in unsafe:
            return port


def edge_executable() -> str | None:
    configured = os.getenv("EFFECT_BROWSER_BROWSER_EXECUTABLE")
    if configured:
        return configured
    candidates = (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    )
    return str(next((path for path in candidates if path.exists()), "")) or None


def wait_until_ready(base_url: str) -> None:
    deadline = time.monotonic() + 10
    last_response: httpx.Response | None = None
    while time.monotonic() < deadline:
        try:
            last_response = httpx.get(f"{base_url}/readyz", timeout=1)
            if last_response.status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.05)
    detail = last_response.text if last_response is not None else "no HTTP response"
    raise RuntimeError(f"test server did not become ready: {detail}")


@pytest.mark.e2e
def test_real_browser_upload_is_allowlisted_hash_bound_and_path_redacted(
    tmp_path: Path,
) -> None:
    upload_root = tmp_path / "approved"
    upload_root.mkdir()
    document = upload_root / "synthetic-resume.txt"
    document.write_bytes(b"synthetic resume fixture")
    form = tmp_path / "upload-form.html"
    form.write_text(
        """
        <!doctype html><title>Upload fixture</title>
        <label for="resume">Résumé</label>
        <input id="resume" name="resume" type="file" required>
        """,
        encoding="utf-8",
    )
    driver = PlaywrightDriver(
        executable_path=edge_executable(),
        headless=True,
        sandbox=os.getenv(
            "EFFECT_BROWSER_BROWSER_SANDBOX",
            "true",
        ).casefold()
        not in {"0", "false", "no", "off"},
        artifacts_directory=tmp_path / "artifacts",
        allowed_upload_roots=(upload_root,),
    )
    try:
        driver.execute(
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=form.resolve().as_uri(),
                description="Open the local synthetic upload fixture.",
            )
        )
        candidate = driver.snapshot().candidates[0]
        receipt = driver.execute(
            ProposedAction(
                kind=ActionKind.UPLOAD,
                locator=candidate.locator,
                file_path=document.resolve(),
                document_sha256=sha256_file(document),
                description="Attach the approved synthetic document.",
            )
        )
        attached = driver.snapshot().candidates[0]
    finally:
        driver.close()

    assert receipt.external_id == "local-upload"
    assert attached.filled is True
    assert attached.current_value is None
    assert document.name not in attached.model_dump_json()


@pytest.mark.e2e
def test_real_browser_snapshot_exposes_embedded_human_challenge(
    tmp_path: Path,
) -> None:
    page = tmp_path / "iframe-challenge.html"
    page.write_text(
        """
        <!doctype html><title>Challenge fixture</title>
        <h1>Continue application</h1>
        <iframe title="Verification"
          srcdoc="<p>Please verify you are human with reCAPTCHA</p>"></iframe>
        """,
        encoding="utf-8",
    )
    driver = PlaywrightDriver(
        executable_path=edge_executable(),
        headless=True,
        sandbox=os.getenv(
            "EFFECT_BROWSER_BROWSER_SANDBOX",
            "true",
        ).casefold()
        not in {"0", "false", "no", "off"},
        artifacts_directory=tmp_path / "artifacts",
    )
    try:
        driver.execute(
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=page.resolve().as_uri(),
                description="Open a local embedded challenge fixture.",
            )
        )
        snapshot = driver.snapshot()
    finally:
        driver.close()

    assert "verify you are human" in snapshot.text_excerpt
    assert "reCAPTCHA" in snapshot.text_excerpt


@pytest.mark.e2e
def test_real_browser_crash_reconciles_one_order(tmp_path: Path, monkeypatch) -> None:
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv(
        "EFFECT_BROWSER_DATABASE_URL",
        f"sqlite:///{tmp_path / 'browser-e2e.db'}",
    )
    monkeypatch.setenv("EFFECT_BROWSER_ALLOWED_ORIGINS", base_url)
    monkeypatch.setenv("EFFECT_BROWSER_ARTIFACTS_DIRECTORY", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    api.get_store.cache_clear()
    server = uvicorn.Server(
        uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_until_ready(base_url)

    settings = get_settings()
    service = api.get_service()
    service.policy = ActionPolicy((base_url,))
    tenant = settings.default_tenant_id
    executable = edge_executable()

    def browser() -> PlaywrightDriver:
        return PlaywrightDriver(
            executable_path=executable,
            headless=True,
            sandbox=settings.browser_sandbox,
            artifacts_directory=settings.artifacts_directory,
        )

    try:
        task = service.create_task(
            tenant_id=tenant,
            instruction="Order once; never duplicate after a crash.",
            start_url=base_url,
            planner=DeterministicPlanner(),
        )
        first = browser()
        try:
            paused = service.run(tenant_id=tenant, task_id=task.id, driver=first)
        finally:
            first.close()
        action = paused.next_action
        assert action is not None
        assert action.state is ActionState.APPROVAL_REQUIRED
        service.store.approve_action(
            tenant_id=tenant,
            action_id=action.id,
            expected_version=action.version,
            actor_id="e2e-operator",
        )

        crashing = CrashAfterCommitDriver(browser())
        try:
            with pytest.raises(SimulatedProcessCrash):
                service.run(tenant_id=tenant, task_id=task.id, driver=crashing)
        finally:
            crashing.close()

        recovery = browser()
        try:
            stopped = service.run(tenant_id=tenant, task_id=task.id, driver=recovery)
            assert stopped.next_action is not None
            assert stopped.next_action.state is ActionState.OUTCOME_UNKNOWN
            receipt = service.reconcile(
                tenant_id=tenant,
                action_id=action.id,
                driver=recovery,
            )
        finally:
            recovery.close()
        assert receipt is not None

        final_browser = browser()
        try:
            final = service.run(
                tenant_id=tenant,
                task_id=task.id,
                driver=final_browser,
            )
        finally:
            final_browser.close()
        orders = httpx.get(f"{base_url}/demo-shop/api/orders", timeout=5).json()
        matching = [
            row for row in orders if row["reference"] == action.proposal.effect_key
        ]

        assert final.task.status is TaskStatus.SUCCEEDED
        assert len(matching) == 1
        assert matching[0]["duplicate_attempts"] == 0
        assert service.store.verify_audit(tenant).valid is True
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_one_query_autopilot_proves_real_browser_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv(
        "EFFECT_BROWSER_DATABASE_URL",
        f"sqlite:///{tmp_path / 'autopilot-e2e.db'}",
    )
    monkeypatch.setenv("EFFECT_BROWSER_ALLOWED_ORIGINS", base_url)
    monkeypatch.setenv(
        "EFFECT_BROWSER_ARTIFACTS_DIRECTORY",
        str(tmp_path / "artifacts"),
    )
    if executable := edge_executable():
        monkeypatch.setenv("EFFECT_BROWSER_BROWSER_EXECUTABLE", executable)
    get_settings.cache_clear()
    api.get_store.cache_clear()
    server = uvicorn.Server(
        uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_until_ready(base_url)

    try:
        response = httpx.post(
            f"{base_url}/v1/autopilot",
            json={
                "query": (
                    "Order three encrypted backup drives at "
                    f"{base_url} without a duplicate order."
                )
            },
            timeout=90,
        )
        assert response.status_code == 200
        result = AutopilotResult.model_validate(response.json())
        submit_evidence = [
            item for item in result.evidence if item.kind is ActionKind.SUBMIT
        ]
        orders = httpx.get(f"{base_url}/demo-shop/api/orders", timeout=5).json()
        matching = [
            row
            for row in orders
            if submit_evidence and row["reference"] == submit_evidence[0].effect_key
        ]

        assert result.verdict is AutopilotVerdict.VERIFIED_SUCCESS
        assert result.task.status is TaskStatus.SUCCEEDED
        assert len(submit_evidence) == 1
        assert len(matching) == 1
        assert matching[0]["duplicate_attempts"] == 0
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_multi_search_mission_gates_one_real_browser_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv(
        "EFFECT_BROWSER_DATABASE_URL",
        f"sqlite:///{tmp_path / 'mission-browser-e2e.db'}",
    )
    monkeypatch.setenv("EFFECT_BROWSER_ALLOWED_ORIGINS", base_url)
    monkeypatch.setenv(
        "EFFECT_BROWSER_ARTIFACTS_DIRECTORY",
        str(tmp_path / "artifacts"),
    )
    if executable := edge_executable():
        monkeypatch.setenv("EFFECT_BROWSER_BROWSER_EXECUTABLE", executable)
    get_settings.cache_clear()
    api.get_store.cache_clear()

    class Planner:
        name = "deterministic"

        def plan(self, query, *, external_commit_authorized):
            assert external_commit_authorized is True
            return MissionPlan(
                summary="Check two independent facts before one browser commit.",
                steps=(
                    MissionPlanStep(
                        key="catalog",
                        kind=MissionStepKind.RESEARCH,
                        instruction="Inspect the demo catalog.",
                    ),
                    MissionPlanStep(
                        key="existing_orders",
                        kind=MissionStepKind.RESEARCH,
                        instruction="Inspect existing demo orders.",
                    ),
                    MissionPlanStep(
                        key="order",
                        kind=MissionStepKind.BROWSER,
                        instruction="Create the requested order exactly once.",
                        depends_on=("catalog", "existing_orders"),
                    ),
                ),
            )

    class Researcher:
        def search(self, query):
            suffix = "demo-shop" if "catalog" in query else "demo-shop/api/orders"
            return ResearchEvidence(
                query=query,
                summary=f"Captured read-only evidence for {query}",
                citation_urls=(f"{base_url}/{suffix}",),
                provider_response_sha256="d" * 64,
            )

    def mission_coordinator() -> MissionCoordinator:
        settings = get_settings()
        service = api.get_service()
        return MissionCoordinator(
            store=service.store,
            autopilot=api.get_autopilot(),
            settings=settings,
            plan_provider=Planner(),
            researcher=Researcher(),
            max_parallel_research=2,
        )

    api.app.dependency_overrides[api.get_mission_coordinator] = mission_coordinator
    server = uvicorn.Server(
        uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_until_ready(base_url)

    try:
        response = httpx.post(
            f"{base_url}/v1/missions",
            json={
                "query": (
                    "Order three encrypted backup drives at "
                    f"{base_url} without a duplicate order."
                )
            },
            timeout=90,
        )
        assert response.status_code == 200
        result = MissionResult.model_validate(response.json())
        browser_step = next(
            step for step in result.steps if step.kind is MissionStepKind.BROWSER
        )
        child = api.get_store().get_task(
            result.mission.tenant_id,
            browser_step.child_task_id,
        )
        orders = httpx.get(f"{base_url}/demo-shop/api/orders", timeout=5).json()

        assert result.verdict is MissionVerdict.VERIFIED_EFFECT
        assert [step.status.value for step in result.steps] == [
            "succeeded",
            "succeeded",
            "succeeded",
        ]
        assert child.status is TaskStatus.SUCCEEDED
        assert len(orders) == 1
        assert orders[0]["duplicate_attempts"] == 0
        assert api.get_store().verify_audit(result.mission.tenant_id).valid is True
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        api.app.dependency_overrides.pop(api.get_mission_coordinator, None)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_dashboard_renders_multi_search_dag_and_cited_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv(
        "EFFECT_BROWSER_DATABASE_URL",
        f"sqlite:///{tmp_path / 'mission-dashboard-e2e.db'}",
    )
    get_settings.cache_clear()
    api.get_store.cache_clear()

    class Planner:
        name = "deterministic"

        def plan(self, query, *, external_commit_authorized):
            assert external_commit_authorized is False
            return MissionPlan(
                summary="Compare two independent evidence streams.",
                steps=(
                    MissionPlanStep(
                        key="source_a",
                        kind=MissionStepKind.RESEARCH,
                        instruction="Research source A.",
                    ),
                    MissionPlanStep(
                        key="source_b",
                        kind=MissionStepKind.RESEARCH,
                        instruction="Research source B.",
                    ),
                    MissionPlanStep(
                        key="comparison",
                        kind=MissionStepKind.SYNTHESIS,
                        instruction="Compare both cited sources.",
                        depends_on=("source_a", "source_b"),
                    ),
                ),
            )

    class Researcher:
        def search(self, query):
            slug = "a" if query.endswith("A.") else "b"
            return ResearchEvidence(
                query=query,
                summary=f"Evidence {slug.upper()}",
                citation_urls=(f"https://evidence.example/{slug}",),
                provider_response_sha256=slug * 64,
            )

    class Synthesizer:
        def synthesize(self, instruction, dependency_outputs):
            return SynthesisEvidence(
                answer="A and B were compared from two persisted citations.",
                citation_urls=tuple(
                    url
                    for output in dependency_outputs
                    for url in output["citation_urls"]
                ),
                input_sha256="e" * 64,
            )

    def mission_coordinator() -> MissionCoordinator:
        settings = get_settings()
        service = api.get_service()
        return MissionCoordinator(
            store=service.store,
            autopilot=api.get_autopilot(),
            settings=settings,
            plan_provider=Planner(),
            researcher=Researcher(),
            synthesizer=Synthesizer(),
            max_parallel_research=2,
        )

    api.app.dependency_overrides[api.get_mission_coordinator] = mission_coordinator
    server = uvicorn.Server(
        uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_until_ready(base_url)

    try:
        with sync_playwright() as playwright:
            launch_args = {"headless": True}
            if executable := edge_executable():
                launch_args["executable_path"] = executable
            browser = playwright.chromium.launch(**launch_args)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(base_url, wait_until="networkidle")
            page.locator("#autopilot-query").fill(
                "Research source A and source B, then compare the evidence."
            )
            page.get_by_role("button", name="Plan and run mission").click()
            page.locator("#mission-view").wait_for(state="visible")

            assert page.locator("#mission-status").inner_text().casefold() == "completed"
            assert page.locator("#mission-steps .mission-step").count() == 3
            assert "A and B were compared" in page.locator("#mission-answer").inner_text()
            assert page.locator("#mission-answer .source-link").count() == 2
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        api.app.dependency_overrides.pop(api.get_mission_coordinator, None)
        api.get_store.cache_clear()
        get_settings.cache_clear()
