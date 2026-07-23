from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from effect_browser import api
from effect_browser.config import get_settings
from effect_browser.domain import ActionKind, ActionState, StepChoice, StepRequest
from effect_browser.engine import EffectBrowserService
from effect_browser.policy import ActionPolicy
from effect_browser.providers import ReactiveBootstrapPlanner

from .test_job_harness_e2e import browser, start_harness


class GenericFormStepPlanner:
    """Test planner that uses semantics only; it has no site URLs or selectors."""

    name = "test-reactive"

    def __init__(self, resume_path: Path) -> None:
        self.resume_path = resume_path
        self.resume_sha256 = hashlib.sha256(resume_path.read_bytes()).hexdigest()

    def choose(self, request: StepRequest) -> StepChoice:
        if "Verified application" in request.snapshot.text_excerpt:
            return StepChoice(
                kind=ActionKind.FINISH,
                description="The independently verified application receipt is visible.",
            )
        values = {
            "Full name": "Synthetic Reactive Candidate",
            "Email": "reactive@example.test",
            "Country": "Ireland",
            "Work authorization": "authorized",
            "Years using Python": "6",
            "Resume summary": (
                "Synthetic profile with production Python, distributed systems, "
                "durable execution, and observability experience."
            ),
            "Why this role?": (
                "This is a synthetic reactive browser test, not a real application."
            ),
            "Application reference": request.effect_reference,
        }
        for candidate in request.snapshot.candidates:
            if candidate.interaction != "input" or candidate.filled:
                continue
            if candidate.name not in values:
                raise AssertionError(f"test profile has no fact for {candidate.name!r}")
            return StepChoice(
                kind=ActionKind.FILL,
                candidate_id=candidate.id,
                value=values[candidate.name],
                description=f"Fill the verified synthetic {candidate.name} value.",
            )
        for candidate in request.snapshot.candidates:
            if candidate.interaction == "upload" and not candidate.filled:
                return StepChoice(
                    kind=ActionKind.UPLOAD,
                    candidate_id=candidate.id,
                    file_path=self.resume_path,
                    document_sha256=self.resume_sha256,
                    description="Attach the approved synthetic résumé fixture.",
                )
        for candidate in request.snapshot.candidates:
            if candidate.interaction == "navigation":
                return StepChoice(
                    kind=ActionKind.CLICK,
                    candidate_id=candidate.id,
                    description=f"Open {candidate.name}.",
                )
        for candidate in request.snapshot.candidates:
            if candidate.interaction == "commit":
                return StepChoice(
                    kind=ActionKind.SUBMIT,
                    candidate_id=candidate.id,
                    description="Submit the reviewed synthetic application.",
                    expected_outcome="One durable synthetic job application.",
                )
        raise AssertionError("generic planner found no supported next action")


def reactive_service(base_url: str) -> EffectBrowserService:
    settings = get_settings()
    resume_path = (settings.allowed_upload_roots[0] / "synthetic-resume.txt").resolve()
    return EffectBrowserService(
        api.get_store(),
        ActionPolicy((base_url,), settings.allowed_upload_roots),
        step_planners={"test-reactive": GenericFormStepPlanner(resume_path)},
    )


def prepare_reactive_task(
    base_url: str,
    start_url: str | None = None,
    *,
    approve_submit: bool = True,
):
    settings = get_settings()
    service = reactive_service(base_url)
    task = service.create_task(
        tenant_id=settings.default_tenant_id,
        instruction=(
            "Apply to the Platform Reliability Engineer role using the supplied "
            "synthetic profile."
        ),
        start_url=start_url or f"{base_url}/demo-jobs",
        planner=ReactiveBootstrapPlanner("test-reactive"),
    )
    first = browser()
    try:
        paused = service.run(
            tenant_id=settings.default_tenant_id,
            task_id=task.id,
            driver=first,
        )
    finally:
        first.close()
    upload = paused.next_action
    assert upload is not None
    assert upload.proposal.kind is ActionKind.UPLOAD
    assert upload.state is ActionState.APPROVAL_REQUIRED
    service.store.approve_action(
        tenant_id=settings.default_tenant_id,
        action_id=upload.id,
        expected_version=upload.version,
        actor_id="reactive-test-operator",
    )
    upload_runner = browser()
    try:
        paused = service.run(
            tenant_id=settings.default_tenant_id,
            task_id=task.id,
            driver=upload_runner,
        )
    finally:
        upload_runner.close()
    submit = paused.next_action
    assert submit is not None
    assert submit.proposal.kind is ActionKind.SUBMIT
    assert submit.state is ActionState.APPROVAL_REQUIRED
    assert submit.proposal.reconciliation is not None
    assert submit.proposal.outgoing_review is not None
    assert len(submit.proposal.outgoing_review.fields) == 8
    assert len(submit.proposal.outgoing_review.document_sha256s) == 1
    assert len(submit.proposal.outgoing_review.requests) == 1
    outgoing = submit.proposal.outgoing_review.requests[0]
    outgoing_fields = {field.name: field.value for field in outgoing.fields}
    assert outgoing.method == "POST"
    assert outgoing.target == f"{base_url}/demo-jobs/api/applications"
    assert outgoing.content_type == "multipart/form-data"
    assert outgoing.document_sha256s == (submit.proposal.outgoing_review.document_sha256s)
    assert outgoing_fields["job_slug"] == "platform-reliability-engineer"
    assert outgoing_fields["years_python"] == "6"
    assert (
        httpx.get(
            f"{base_url}/demo-jobs/api/applications",
            timeout=5,
        ).json()
        == []
    )
    if approve_submit:
        service.store.approve_action(
            tenant_id=settings.default_tenant_id,
            action_id=submit.id,
            expected_version=submit.version,
            actor_id="reactive-test-operator",
        )
    return service, task, submit


@pytest.mark.e2e
def test_reactive_agent_completes_dynamic_job_from_fresh_snapshots(
    tmp_path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    try:
        service, task, submit = prepare_reactive_task(base_url)
        runner = browser()
        try:
            result = service.run(
                tenant_id=get_settings().default_tenant_id,
                task_id=task.id,
                driver=runner,
            )
        finally:
            runner.close()
        ledger = httpx.get(
            f"{base_url}/demo-jobs/api/applications",
            timeout=5,
        ).json()
        receipt = service.store.get_receipt(
            get_settings().default_tenant_id,
            submit.id,
        )

        assert result.task.status.value == "succeeded"
        assert len(ledger) == 1
        assert ledger[0]["reference"] == submit.proposal.effect_key
        assert ledger[0]["duplicate_attempts"] == 0
        assert ledger[0]["resume_sha256"] in (
            submit.proposal.outgoing_review.document_sha256s
        )
        assert receipt is not None
        assert receipt.external_id == ledger[0]["id"]
        assert (
            len(service.store.list_actions(get_settings().default_tenant_id, task.id))
            == 14
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_dashboard_shows_exact_multipart_fields_and_document_hash(
    tmp_path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    try:
        _service, task, submit = prepare_reactive_task(
            base_url,
            approve_submit=False,
        )
        assert submit.proposal.outgoing_review is not None
        document_sha256 = submit.proposal.outgoing_review.document_sha256s[0]
        dashboard = browser()
        try:
            dashboard._page.goto(base_url, wait_until="networkidle")
            dashboard._page.locator(f'[data-task="{task.id}"]').click()
            dashboard._page.get_by_text(
                "ABORTED NETWORK PREVIEW",
                exact=False,
            ).wait_for()
            text = dashboard._page.locator("body").inner_text()
            dashboard._page.screenshot(
                path=str(
                    get_settings().artifacts_directory
                    / "exact-multipart-review-dashboard.png"
                ),
                full_page=True,
            )
        finally:
            dashboard.close()

        assert "NOTHING SENT" in text
        assert "Canonical body SHA-256" in text
        assert "Preview wire-body SHA-256" in text
        assert "resume" in text
        assert "full_name" in text
        assert document_sha256 in text
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_reactive_agent_never_accepts_fake_application_success(
    tmp_path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    try:
        service, task, submit = prepare_reactive_task(
            base_url,
            (
                f"{base_url}/demo-jobs/jobs/"
                "platform-reliability-engineer/apply?mode=fake_success"
            ),
        )
        runner = browser()
        try:
            result = service.run(
                tenant_id=get_settings().default_tenant_id,
                task_id=task.id,
                driver=runner,
            )
        finally:
            runner.close()
        ledger = httpx.get(
            f"{base_url}/demo-jobs/api/applications",
            timeout=5,
        ).json()

        assert result.task.status.value == "awaiting_recovery"
        assert result.next_action is not None
        assert result.next_action.state is ActionState.OUTCOME_UNKNOWN
        assert "unverified" in result.message
        assert ledger == []
        assert (
            service.store.get_receipt(
                get_settings().default_tenant_id,
                submit.id,
            )
            is None
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_changed_javascript_payload_is_blocked_before_transmission(
    tmp_path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    try:
        service, task, _submit = prepare_reactive_task(
            base_url,
            (
                f"{base_url}/demo-jobs/jobs/"
                "platform-reliability-engineer/apply?mode=payload_drift"
            ),
        )
        runner = browser()
        try:
            result = service.run(
                tenant_id=get_settings().default_tenant_id,
                task_id=task.id,
                driver=runner,
            )
        finally:
            runner.close()
        ledger = httpx.get(
            f"{base_url}/demo-jobs/api/applications",
            timeout=5,
        ).json()

        assert result.task.status.value == "failed"
        assert result.next_action is not None
        assert result.next_action.state is ActionState.FAILED
        assert "blocked before transmission" in result.message
        assert ledger == []
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_file_change_auto_upload_is_blocked_before_server_receives_it(
    tmp_path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    try:
        settings = get_settings()
        service = reactive_service(base_url)
        task = service.create_task(
            tenant_id=settings.default_tenant_id,
            instruction="Exercise a synthetic ATS that auto-uploads a résumé.",
            start_url=(
                f"{base_url}/demo-jobs/jobs/"
                "platform-reliability-engineer/apply?mode=auto_upload"
            ),
            planner=ReactiveBootstrapPlanner("test-reactive"),
        )
        first = browser()
        try:
            paused = service.run(
                tenant_id=settings.default_tenant_id,
                task_id=task.id,
                driver=first,
            )
        finally:
            first.close()
        upload = paused.next_action
        assert upload is not None
        assert upload.proposal.kind is ActionKind.UPLOAD
        assert upload.state is ActionState.APPROVAL_REQUIRED
        service.store.approve_action(
            tenant_id=settings.default_tenant_id,
            action_id=upload.id,
            expected_version=upload.version,
            actor_id="reactive-test-operator",
        )

        guarded = browser()
        try:
            result = service.run(
                tenant_id=settings.default_tenant_id,
                task_id=task.id,
                driver=guarded,
            )
        finally:
            guarded.close()

        attempts = httpx.get(
            f"{base_url}/demo-jobs/api/auto-upload-attempts",
            timeout=5,
        ).json()
        ledger = httpx.get(
            f"{base_url}/demo-jobs/api/applications",
            timeout=5,
        ).json()
        assert result.task.status.value == "failed"
        assert result.next_action is not None
        assert result.next_action.state is ActionState.FAILED
        assert "blocked before transmission" in result.message
        assert attempts == {"attempts": 0}
        assert ledger == []
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()
