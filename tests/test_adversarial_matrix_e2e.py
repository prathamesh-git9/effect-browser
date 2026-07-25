from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from effect_browser import api
from effect_browser.config import get_settings
from effect_browser.domain import (
    ActionKind,
    Locator,
    OutgoingReview,
    ProposedAction,
    digest,
)
from effect_browser.hard_target import DOWNLOAD_FIXTURE
from effect_browser.transmission import TransmissionBlocked, TransmissionReviewError

from .test_job_harness_e2e import browser, start_harness


def _candidate(runner, name: str):
    return next(item for item in runner.snapshot().candidates if item.name == name)


def _navigate(runner, url: str) -> None:
    runner.execute(
        ProposedAction(
            kind=ActionKind.NAVIGATE,
            url=url,
            description="Open one controlled adversarial browser fixture.",
        )
    )


def _writes(base_url: str) -> dict[str, int]:
    return httpx.get(f"{base_url}/demo-hard/api/writes", timeout=5).json()


@pytest.mark.e2e
def test_hard_01_identity_only_spa_drift_changes_observation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    runner = browser()
    try:
        _navigate(runner, f"{base_url}/demo-capabilities")
        first = runner.observe()
        runner._page.get_by_role("button", name="Rebuild controls").click()
        second = runner.observe()

        assert first.state_sha256 != second.state_sha256
    finally:
        runner.close()
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_hard_02_nested_iframe_controls_are_candidate_bound(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    runner = browser()
    try:
        _navigate(runner, f"{base_url}/demo-hard/iframe")
        nested = _candidate(runner, "Nested project code")
        assert len(nested.locator.frame_path) == 2
        assert nested.locator.adaptive_id is None

        runner.execute(
            ProposedAction(
                kind=ActionKind.FILL,
                locator=nested.locator,
                value="FRAME-BOUND",
                description="Fill the exact nested-frame control.",
            )
        )

        assert _candidate(runner, "Nested project code").current_value == "FRAME-BOUND"
    finally:
        runner.close()
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_hard_03_open_shadow_dom_uses_same_observe_and_candidate_traversal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    runner = browser()
    try:
        _navigate(runner, f"{base_url}/demo-hard/shadow")
        shadow = _candidate(runner, "Shadow project code")
        assert shadow.locator.adaptive_id is None
        runner.execute(
            ProposedAction(
                kind=ActionKind.FILL,
                locator=shadow.locator,
                value="SHADOW-BOUND",
                description="Fill the Scrapling-observed open-shadow control.",
            )
        )

        assert _candidate(runner, "Shadow project code").current_value == "SHADOW-BOUND"
    finally:
        runner.close()
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_hard_04_popup_context_is_adopted_and_old_page_is_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    runner = browser()
    try:
        _navigate(runner, f"{base_url}/demo-hard/popup")
        link = _candidate(runner, "Open secure review")
        runner.execute(
            ProposedAction(
                kind=ActionKind.CLICK,
                locator=link.locator,
                description="Open the candidate-bound review popup.",
                target_interaction="navigation",
            )
        )

        assert runner.observe().title == "Popup detail"
        assert len(runner._context.pages) == 1
    finally:
        runner.close()
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_hard_05_popup_immediate_and_delayed_writes_never_escape(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    runner = browser()
    try:
        _navigate(runner, f"{base_url}/demo-hard/popup?retry=true")
        link = _candidate(runner, "Open secure review")
        with pytest.raises(TransmissionBlocked, match="reversible browser action"):
            runner.execute(
                ProposedAction(
                    kind=ActionKind.CLICK,
                    locator=link.locator,
                    description="Open a popup that attempts hidden writes.",
                    target_interaction="navigation",
                )
            )
        runner._page.wait_for_timeout(4_500)

        assert _writes(base_url).get("popup-retry", 0) == 0
    finally:
        runner.close()
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_hard_06_reversible_fill_cannot_secretly_autosave(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    runner = browser()
    try:
        _navigate(runner, f"{base_url}/demo-hard/autosave")
        field = _candidate(runner, "Display name")
        with pytest.raises(TransmissionBlocked, match="unreviewed write"):
            runner.execute(
                ProposedAction(
                    kind=ActionKind.FILL,
                    locator=field.locator,
                    value="Must not leave the browser",
                    description="Exercise a hidden auto-save listener.",
                )
            )

        assert _writes(base_url).get("autosave", 0) == 0
    finally:
        runner.close()
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_hard_07_delayed_preview_timer_remains_write_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    runner = browser()
    try:
        _navigate(runner, f"{base_url}/demo-hard/delayed-preview")
        observation = runner.observe()
        action = ProposedAction(
            kind=ActionKind.SUBMIT,
            locator=Locator(role="button", name="Submit delayed effect"),
            description="Preview a delayed external effect.",
            effect_key="DELAY-BOUND",
            expected_outcome="One delayed probe write.",
        )
        with pytest.raises(TransmissionReviewError, match="produced 0"):
            runner.preview_submit(action, observation.state_sha256)
        runner._page.wait_for_timeout(3_500)

        assert _writes(base_url).get("delayed-preview", 0) == 0
    finally:
        runner.close()
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_hard_08_security_header_drift_is_blocked_before_transmission(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    runner = browser()
    try:
        _navigate(runner, f"{base_url}/demo-hard/header-drift")
        observation = runner.observe()
        action = ProposedAction(
            kind=ActionKind.SUBMIT,
            locator=Locator(role="button", name="Submit header-bound effect"),
            description="Submit one header-bound controlled effect.",
            effect_key="HEADER-BOUND",
            expected_outcome="One durable header-bound write.",
        )
        review = runner.preview_submit(action, observation.state_sha256)
        approved = action.model_copy(update={"outgoing_review": review})
        runner._context.add_cookies(
            [{"name": "effect_session", "value": "changed", "url": base_url}]
        )

        with pytest.raises(TransmissionBlocked, match="changed after approval"):
            runner.execute(approved)

        assert _writes(base_url).get("header-drift", 0) == 0
    finally:
        runner.close()
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_hard_09_document_bytes_cannot_be_laundered_as_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    document = tmp_path / "approved-document.txt"
    document.write_text("controlled approved document bytes", encoding="utf-8")
    document_sha256 = hashlib.sha256(document.read_bytes()).hexdigest()
    runner = browser()
    try:
        _navigate(runner, f"{base_url}/demo-hard/text-upload")
        upload = _candidate(runner, "Approved document")
        runner.execute(
            ProposedAction(
                kind=ActionKind.UPLOAD,
                locator=upload.locator,
                file_path=document.resolve(),
                document_sha256=document_sha256,
                description="Select the exact approved controlled document.",
            )
        )
        observation = runner.observe()
        review_body = {
            "fields": [],
            "document_sha256s": [document_sha256],
            "observation_sha256": observation.state_sha256,
        }
        base_review = OutgoingReview(
            document_sha256s=(document_sha256,),
            observation_sha256=observation.state_sha256,
            payload_sha256=digest(review_body),
        )
        action = ProposedAction(
            kind=ActionKind.SUBMIT,
            locator=Locator(role="button", name="Submit encoded document"),
            description="Try to submit a text-laundered document.",
            effect_key="DOCUMENT-BOUND",
            expected_outcome="One exact file-part transfer.",
            outgoing_review=base_review,
        )

        with pytest.raises(ValueError, match="document bytes do not match"):
            runner.preview_submit(action, observation.state_sha256)

        assert _writes(base_url).get("text-upload", 0) == 0
    finally:
        runner.close()
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()


@pytest.mark.e2e
def test_hard_10_download_receipt_hash_matches_server_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    runner = browser()
    try:
        _navigate(runner, f"{base_url}/demo-hard/download-page")
        download = _candidate(runner, "Download evidence package")
        receipt = runner.execute(
            ProposedAction(
                kind=ActionKind.DOWNLOAD,
                locator=download.locator,
                url=download.href,
                description="Download and hash the controlled evidence package.",
            )
        )

        assert receipt.evidence_sha256 == hashlib.sha256(DOWNLOAD_FIXTURE).hexdigest()
    finally:
        runner.close()
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()
