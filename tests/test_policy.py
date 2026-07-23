from __future__ import annotations

from pathlib import Path

from effect_browser.domain import (
    ActionKind,
    Locator,
    OutgoingReview,
    ProposedAction,
    digest,
)
from effect_browser.policy import ActionPolicy
from effect_browser.transmission import fingerprint_request
from effect_browser.uploads import sha256_file

from .conftest import BASE_URL


def test_ambiguous_generic_click_is_rejected() -> None:
    action = ProposedAction(
        kind=ActionKind.CLICK,
        locator=Locator(role="button", name="Continue"),
        description="Click an ambiguously named control.",
    )

    decision = ActionPolicy((BASE_URL,)).evaluate(action, f"{BASE_URL}/form")

    assert decision.allowed is False
    assert "ambiguous" in decision.reason


def test_sensitive_fill_is_rejected_by_accessible_name() -> None:
    action = ProposedAction(
        kind=ActionKind.FILL,
        locator=Locator(role="textbox", name="API secret"),
        value="not-a-real-secret",
        description="Attempt to fill a secret field.",
    )

    decision = ActionPolicy((BASE_URL,)).evaluate(action, f"{BASE_URL}/form")

    assert decision.allowed is False
    assert "credential" in decision.reason


def upload_action(path: Path, sha256: str) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.UPLOAD,
        locator=Locator(label="Résumé"),
        file_path=path,
        document_sha256=sha256,
        description="Attach the operator-supplied document.",
    )


def test_upload_requires_allowlisted_unchanged_content(tmp_path: Path) -> None:
    upload_root = tmp_path / "approved"
    upload_root.mkdir()
    document = upload_root / "synthetic-resume.txt"
    document.write_bytes(b"synthetic resume fixture")
    action = upload_action(document.resolve(), sha256_file(document))

    allowed = ActionPolicy((BASE_URL,), (upload_root,)).evaluate(
        action,
        f"{BASE_URL}/form",
    )
    document.write_bytes(b"changed after planning")
    changed = ActionPolicy((BASE_URL,), (upload_root,)).evaluate(
        action,
        f"{BASE_URL}/form",
    )

    assert allowed.allowed is True
    assert allowed.risk.value == "external_commit"
    assert allowed.requires_approval is True
    assert changed.allowed is False
    assert "no longer matches" in changed.reason
    assert document.name not in changed.reason


def test_upload_outside_allowlist_fails_without_disclosing_path(
    tmp_path: Path,
) -> None:
    upload_root = tmp_path / "approved"
    upload_root.mkdir()
    outside = tmp_path / "private-document.txt"
    outside.write_bytes(b"not approved")

    decision = ActionPolicy((BASE_URL,), (upload_root,)).evaluate(
        upload_action(outside.resolve(), sha256_file(outside)),
        f"{BASE_URL}/form",
    )

    assert decision.allowed is False
    assert "outside" in decision.reason
    assert outside.name not in decision.reason


def review_for(
    observation_sha256: str,
    request_url: str = f"{BASE_URL}/applications",
) -> OutgoingReview:
    body = {
        "fields": [],
        "document_sha256s": [],
        "observation_sha256": observation_sha256,
    }
    base = OutgoingReview(
        observation_sha256=observation_sha256,
        payload_sha256=digest(body),
    )
    request = fingerprint_request(
        method="POST",
        url=request_url,
        headers={"content-type": "application/json"},
        body=b"{}",
    )
    return base.bind_requests((request,))


def reactive_submit(review: OutgoingReview | None) -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.SUBMIT,
        locator=Locator(role="button", name="Submit application"),
        description="Submit the reviewed application.",
        effect_key="EB-TEST",
        expected_outcome="One application.",
        planned_from_sha256="fresh-observation",
        outgoing_review=review,
    )


def test_reactive_submit_requires_observed_payload_review() -> None:
    decision = ActionPolicy((BASE_URL,)).evaluate(
        reactive_submit(None),
        f"{BASE_URL}/form",
    )

    assert decision.allowed is False
    assert "missing" in decision.reason


def test_reactive_submit_review_must_match_planned_observation() -> None:
    decision = ActionPolicy((BASE_URL,)).evaluate(
        reactive_submit(review_for("different-observation")),
        f"{BASE_URL}/form",
    )

    assert decision.allowed is False
    assert "not bound" in decision.reason


def test_bound_reactive_submit_still_requires_human_approval() -> None:
    decision = ActionPolicy((BASE_URL,)).evaluate(
        reactive_submit(review_for("fresh-observation")),
        f"{BASE_URL}/form",
    )

    assert decision.allowed is True
    assert decision.requires_approval is True


def test_reviewed_submit_cannot_write_to_a_different_origin() -> None:
    decision = ActionPolicy((BASE_URL,)).evaluate(
        reactive_submit(
            review_for(
                "fresh-observation",
                "https://collector.example.test/applications",
            )
        ),
        f"{BASE_URL}/form",
    )

    assert decision.allowed is False
    assert "origin is not allowed" in decision.reason
