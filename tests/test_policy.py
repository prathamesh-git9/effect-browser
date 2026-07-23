from __future__ import annotations

from effect_browser.domain import (
    ActionKind,
    Locator,
    OutgoingReview,
    ProposedAction,
    digest,
)
from effect_browser.policy import ActionPolicy

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


def review_for(observation_sha256: str) -> OutgoingReview:
    body = {
        "fields": [],
        "document_sha256s": [],
        "observation_sha256": observation_sha256,
    }
    return OutgoingReview(
        observation_sha256=observation_sha256,
        payload_sha256=digest(body),
    )


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
