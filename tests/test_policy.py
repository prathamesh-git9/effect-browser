from __future__ import annotations

from effect_browser.domain import ActionKind, Locator, ProposedAction
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
