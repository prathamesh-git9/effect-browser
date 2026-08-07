from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import BaseModel, Field, model_validator

from effect_browser.autopilot import AutopilotCoordinator
from effect_browser.browser.playwright import PlaywrightDriver
from effect_browser.capabilities import capability_catalog
from effect_browser.capability_target import create_capability_router
from effect_browser.config import get_settings
from effect_browser.demo_target import create_demo_router
from effect_browser.domain import (
    AnswerSensitivity,
    AnswerSource,
    AutonomyScope,
    BrowserReceipt,
    Resolution,
    VerificationState,
)
from effect_browser.engine import EffectBrowserService
from effect_browser.hard_target import create_hard_target_router
from effect_browser.job_target import create_demo_job_router
from effect_browser.mission import MissionCoordinator
from effect_browser.policy import ActionPolicy
from effect_browser.providers import (
    DeterministicPlanner,
    GrokPlanner,
    GrokReactivePlanner,
    JobHarnessPlanner,
    OpenAIPlanner,
    OpenAIReactivePlanner,
    ProviderError,
    ReactiveBootstrapPlanner,
)
from effect_browser.research import capture_research
from effect_browser.session import available_session_state_protector
from effect_browser.store import ConflictError, DatabaseStore, NotFoundError

REQUESTS = Counter(
    "effect_browser_http_requests_total",
    "HTTP requests handled by Effect Browser",
    ["method", "path", "status"],
)
HTTP_LOG = logging.getLogger("effect_browser.http")


class Identity(BaseModel):
    tenant_id: UUID
    actor_id: str


def identity(
    x_tenant_id: Annotated[UUID | None, Header()] = None,
    x_actor_id: Annotated[str | None, Header()] = None,
) -> Identity:
    settings = get_settings()
    return Identity(
        tenant_id=x_tenant_id or settings.default_tenant_id,
        actor_id=x_actor_id or settings.default_actor_id,
    )


@lru_cache
def get_store() -> DatabaseStore:
    store = DatabaseStore(get_settings().database_url)
    store.initialize()
    return store


def get_service() -> EffectBrowserService:
    settings = get_settings()
    return EffectBrowserService(
        get_store(),
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    if get_store.cache_info().currsize:
        get_store().close()
        get_store.cache_clear()


def planner(name: str):
    settings = get_settings()
    harness_resume = next(
        (
            candidate
            for root in settings.allowed_upload_roots
            if (candidate := (root / "synthetic-resume.txt").resolve()).is_file()
        ),
        None,
    )
    planners = {
        "deterministic": DeterministicPlanner(),
        "job-harness": JobHarnessPlanner(resume_path=harness_resume),
        "openai": OpenAIPlanner(settings.openai_model),
        "openai-reactive": ReactiveBootstrapPlanner("openai-reactive"),
        "grok": GrokPlanner(settings.grok_model),
        "grok-reactive": ReactiveBootstrapPlanner("grok-reactive"),
    }
    if name not in planners:
        raise ValueError(
            "provider must be deterministic, job-harness, openai-reactive, "
            "grok-reactive, openai, or grok"
        )
    return planners[name]


def driver(
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


def get_autopilot() -> AutopilotCoordinator:
    return AutopilotCoordinator(service=get_service(), settings=get_settings())


def get_mission_coordinator() -> MissionCoordinator:
    settings = get_settings()
    return MissionCoordinator(
        store=get_store(),
        autopilot=get_autopilot(),
        settings=settings,
        max_parallel_research=settings.mission_max_parallel_research,
    )


class CreateTaskBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=4_000)
    start_url: str = "http://127.0.0.1:8000"
    provider: str = "deterministic"
    profile_id: UUID | None = None
    document_path: Path | None = None
    document_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    autonomy: AutonomyScope = Field(default_factory=AutonomyScope)

    @model_validator(mode="after")
    def document_fields_are_paired(self) -> CreateTaskBody:
        if self.autonomy.allow_query_target_origin:
            raise ValueError(
                "allow_query_target_origin is reserved for the validated "
                "/v1/autopilot path"
            )
        if (self.document_path is None) != (self.document_sha256 is None):
            raise ValueError(
                "document_path and document_sha256 must be supplied together"
            )
        if self.document_path is not None and not self.document_path.is_absolute():
            raise ValueError("document_path must be absolute")
        return self


class DecisionBody(BaseModel):
    expected_version: int = Field(ge=1)


class ResolutionBody(BaseModel):
    expected_version: int = Field(ge=1)
    resolution: Resolution
    external_id: str | None = None
    url: str | None = None
    evidence_sha256: str | None = None


class CreateProfileBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class PutProfileAnswerBody(BaseModel):
    value: str = Field(min_length=1, max_length=10_000)
    source: AnswerSource
    sensitivity: AnswerSensitivity
    verification_state: VerificationState = VerificationState.UNVERIFIED
    expected_version: int | None = Field(default=None, ge=1)


class ResearchBody(BaseModel):
    question: str = Field(min_length=1, max_length=2_000)
    urls: tuple[str, ...] = Field(min_length=1, max_length=5)


class AutopilotBody(BaseModel):
    query: str = Field(min_length=1, max_length=4_000)
    allow_external_commit: bool = False


app = FastAPI(
    title="Effect Browser",
    version="0.4.1",
    description=(
        "Durable multi-search and browser missions with honest effect semantics."
    ),
    lifespan=lifespan,
)


@app.middleware("http")
async def request_log(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", "").strip()[:128] or str(uuid4())
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "<unmatched>"
        REQUESTS.labels(request.method, path, status).inc()
        HTTP_LOG.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": path,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )


@app.exception_handler(NotFoundError)
async def not_found(_request: Request, exc: NotFoundError):
    return _error(404, "not_found", str(exc))


@app.exception_handler(ConflictError)
async def conflict(_request: Request, exc: ConflictError):
    return _error(409, "conflict", str(exc))


@app.exception_handler(ValueError)
async def invalid(_request: Request, exc: ValueError):
    return _error(422, "validation_error", str(exc))


@app.exception_handler(ProviderError)
async def provider_failure(_request: Request, exc: ProviderError):
    return _error(502, "provider_error", str(exc))


def _error(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "detail": detail}},
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    get_store().initialize()
    return {"status": "ready"}


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/v1/capabilities")
def capabilities():
    return capability_catalog()


@app.post("/v1/tasks", status_code=201)
def create_task(
    body: CreateTaskBody,
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
):
    return service.create_task(
        tenant_id=who.tenant_id,
        instruction=body.instruction,
        start_url=body.start_url,
        planner=planner(body.provider),
        profile_id=body.profile_id,
        document_path=body.document_path,
        document_sha256=body.document_sha256,
        autonomy=body.autonomy,
    )


@app.post("/v1/autopilot")
def run_autopilot(
    body: AutopilotBody,
    who: Annotated[Identity, Depends(identity)],
    coordinator: Annotated[AutopilotCoordinator, Depends(get_autopilot)],
):
    """Resolve, persist, execute, and verify one natural-language browser task."""
    return coordinator.execute(
        tenant_id=who.tenant_id,
        query=body.query,
        allow_external_commit=body.allow_external_commit,
    )


@app.post("/v1/missions")
def run_mission(
    body: AutopilotBody,
    who: Annotated[Identity, Depends(identity)],
    coordinator: Annotated[MissionCoordinator, Depends(get_mission_coordinator)],
):
    """Decompose, persist, execute, and truthfully assess one bounded mission."""
    return coordinator.execute(
        tenant_id=who.tenant_id,
        query=body.query,
        allow_external_commit=body.allow_external_commit,
    )


@app.get("/v1/missions")
def list_missions(
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
):
    return service.store.list_missions(who.tenant_id)


@app.get("/v1/missions/{mission_id}")
def mission_detail(
    mission_id: UUID,
    who: Annotated[Identity, Depends(identity)],
    coordinator: Annotated[MissionCoordinator, Depends(get_mission_coordinator)],
) -> dict[str, Any]:
    result = coordinator.inspect(tenant_id=who.tenant_id, mission_id=mission_id)
    return {
        "result": result,
        "events": coordinator.store.mission_events(who.tenant_id, mission_id),
    }


@app.post("/v1/missions/{mission_id}/run")
def resume_mission(
    mission_id: UUID,
    who: Annotated[Identity, Depends(identity)],
    coordinator: Annotated[MissionCoordinator, Depends(get_mission_coordinator)],
):
    return coordinator.run(tenant_id=who.tenant_id, mission_id=mission_id)


@app.get("/v1/tasks")
def list_tasks(
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
):
    return service.store.list_tasks(who.tenant_id)


@app.post("/v1/profiles", status_code=201)
def create_profile(
    body: CreateProfileBody,
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
):
    return service.store.create_profile(
        tenant_id=who.tenant_id,
        name=body.name,
    )


@app.get("/v1/profiles")
def list_profiles(
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
):
    return service.store.list_profiles(who.tenant_id)


@app.get("/v1/profiles/{profile_id}")
def profile_detail(
    profile_id: UUID,
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
) -> dict[str, Any]:
    return {
        "profile": service.store.get_profile(who.tenant_id, profile_id),
        "answers": service.store.list_profile_answers(who.tenant_id, profile_id),
        "events": service.store.profile_events(who.tenant_id, profile_id),
    }


@app.put("/v1/profiles/{profile_id}/answers/{field_name}")
def put_profile_answer(
    profile_id: UUID,
    field_name: str,
    body: PutProfileAnswerBody,
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
):
    return service.store.put_profile_answer(
        tenant_id=who.tenant_id,
        profile_id=profile_id,
        field_name=field_name,
        value=body.value,
        source=body.source,
        sensitivity=body.sensitivity,
        verification_state=body.verification_state,
        expected_version=body.expected_version,
        actor_id=who.actor_id,
    )


@app.get("/v1/tasks/{task_id}")
def task_detail(
    task_id: UUID,
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
) -> dict[str, Any]:
    task = service.store.get_task(who.tenant_id, task_id)
    actions = service.store.list_actions(who.tenant_id, task_id)
    return {
        "task": task,
        "parent_mission_id": service.store.mission_for_child_task(
            who.tenant_id,
            task_id,
        ),
        "actions": [
            {
                **action.model_dump(mode="json"),
                "approval": (
                    approval.model_dump(mode="json")
                    if (
                        approval := service.store.latest_approval(
                            who.tenant_id, action.id
                        )
                    )
                    else None
                ),
                "receipt": (
                    receipt.model_dump(mode="json")
                    if (receipt := service.store.get_receipt(who.tenant_id, action.id))
                    else None
                ),
            }
            for action in actions
        ],
        "events": service.store.events(who.tenant_id, task_id),
    }


@app.post("/v1/tasks/{task_id}/run")
def run_task(
    task_id: UUID,
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
):
    task = service.store.get_task(who.tenant_id, task_id)
    if mission_id := service.store.mission_for_child_task(who.tenant_id, task_id):
        raise ConflictError(
            f"task is owned by mission {mission_id}; resume the parent mission"
        )
    extra_origins = (task.start_url,) if task.autonomy.allow_query_target_origin else ()
    browser = driver(extra_origins, task_id=task.id, tenant_id=who.tenant_id)
    try:
        return service.run(tenant_id=who.tenant_id, task_id=task_id, driver=browser)
    finally:
        browser.close()


@app.post("/v1/research")
def research(
    body: ResearchBody,
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
):
    """Capture allowlisted rendered sources without executing browser effects."""
    browser = driver()
    try:
        return capture_research(
            question=body.question,
            urls=body.urls,
            driver=browser,
            policy=service.policy,
        )
    finally:
        browser.close()


@app.post("/v1/actions/{action_id}/approve")
def approve_action(
    action_id: UUID,
    body: DecisionBody,
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
):
    return service.store.approve_action(
        tenant_id=who.tenant_id,
        action_id=action_id,
        expected_version=body.expected_version,
        actor_id=who.actor_id,
    )


@app.post("/v1/actions/{action_id}/resume-input")
def resume_input(
    action_id: UUID,
    body: DecisionBody,
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
):
    return service.store.resolve_input(
        tenant_id=who.tenant_id,
        action_id=action_id,
        expected_version=body.expected_version,
        actor_id=who.actor_id,
    )


@app.post("/v1/actions/{action_id}/reject")
def reject_action(
    action_id: UUID,
    body: DecisionBody,
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
):
    return service.store.reject_action(
        tenant_id=who.tenant_id,
        action_id=action_id,
        expected_version=body.expected_version,
        actor_id=who.actor_id,
    )


@app.post("/v1/actions/{action_id}/reconcile")
def reconcile_action(
    action_id: UUID,
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
):
    action = service.store.get_action(who.tenant_id, action_id)
    task = service.store.get_task(who.tenant_id, action.task_id)
    extra_origins = (task.start_url,) if task.autonomy.allow_query_target_origin else ()
    browser = driver(extra_origins, task_id=task.id, tenant_id=who.tenant_id)
    try:
        receipt = service.reconcile(
            tenant_id=who.tenant_id,
            action_id=action_id,
            driver=browser,
        )
        return {"reconciled": receipt is not None, "receipt": receipt}
    finally:
        browser.close()


@app.post("/v1/actions/{action_id}/resolve")
def resolve_action(
    action_id: UUID,
    body: ResolutionBody,
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
):
    receipt = None
    if body.resolution is Resolution.SUCCEEDED:
        if not body.external_id or not body.url or not body.evidence_sha256:
            raise ValueError("succeeded resolution requires complete receipt evidence")
        from effect_browser.domain import utc_now

        receipt = BrowserReceipt(
            external_id=body.external_id,
            url=body.url,
            evidence_sha256=body.evidence_sha256,
            captured_at=utc_now(),
        )
    return service.resolve_not_committed(
        tenant_id=who.tenant_id,
        action_id=action_id,
        expected_version=body.expected_version,
        actor_id=who.actor_id,
        resolution=body.resolution,
        receipt=receipt,
    )


@app.get("/v1/audit/verify")
def verify_audit(
    who: Annotated[Identity, Depends(identity)],
    service: Annotated[EffectBrowserService, Depends(get_service)],
):
    return service.store.verify_audit(who.tenant_id)


app.include_router(create_demo_router(get_store))
app.include_router(create_demo_job_router(get_store))
app.include_router(create_capability_router())
app.include_router(create_hard_target_router())

web_dir = Path(__file__).parent / "web"
app.mount("/assets", StaticFiles(directory=web_dir), name="assets")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(web_dir / "index.html")
