from __future__ import annotations

import hashlib

import pytest

from effect_browser import api
from effect_browser.config import get_settings
from effect_browser.domain import ActionKind, ProposedAction, RiskClass, StepChoice
from effect_browser.policy import ActionPolicy
from effect_browser.providers.reactive import bind_choice

from .test_job_harness_e2e import browser, start_harness


def candidate(snapshot, name: str):
    return next(item for item in snapshot.candidates if item.name == name)


@pytest.mark.e2e
def test_scrapling_drives_generic_dynamic_browser_primitives(
    tmp_path,
    monkeypatch,
) -> None:
    base_url, server, thread = start_harness(tmp_path, monkeypatch)
    runner = browser()
    try:
        runner.execute(
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=f"{base_url}/demo-capabilities",
                description="Open the dynamic capability lab.",
            )
        )
        snapshot = runner.snapshot()
        assert len(snapshot.candidates) >= 8
        assert all(
            item.locator.selector and item.locator.adaptive_id
            for item in snapshot.candidates
        )

        insurance = candidate(snapshot, "Include insurance")
        old_selector = insurance.locator.selector
        runner._page.get_by_role("button", name="Rebuild controls").click()
        assert runner._page.locator(old_selector or "").count() == 0
        runner.execute(
            ProposedAction(
                kind=ActionKind.CHECK,
                locator=insurance.locator,
                checked=True,
                description="Enable insurance after the live DOM is replaced.",
            )
        )
        assert candidate(runner.snapshot(), "Include insurance").current_value == "true"

        current = runner.snapshot()
        runner.execute(
            ProposedAction(
                kind=ActionKind.FILL,
                locator=candidate(current, "Region").locator,
                value="ie",
                description="Select Ireland.",
            )
        )
        runner.execute(
            ProposedAction(
                kind=ActionKind.PRESS,
                locator=candidate(runner.snapshot(), "Region").locator,
                key="ArrowDown",
                description="Move to the next select option.",
            )
        )
        assert candidate(runner.snapshot(), "Region").current_value == "pt"

        closed = runner.snapshot()
        assert runner._page.locator("[role=option]").count() == 0
        assert all(item.role != "option" for item in closed.candidates)
        timezone = candidate(closed, "Timezone")
        assert timezone.role == "combobox"
        runner.execute(
            bind_choice(
                StepChoice(
                    kind=ActionKind.PRESS,
                    candidate_id=timezone.id,
                    key="ArrowDown",
                    description="Open the combobox listbox.",
                ),
                closed,
                effect_reference="EB-CAPABILITY",
            )
        )
        opened = runner.snapshot()
        options = [item for item in opened.candidates if item.role == "option"]
        assert {item.name for item in options} == {"Dublin", "Lisbon"}
        assert all(item.interaction == "option" for item in options)
        choose = bind_choice(
            StepChoice(
                kind=ActionKind.CLICK,
                candidate_id=next(item.id for item in options if item.name == "Dublin"),
                description="Choose the observed Dublin option.",
            ),
            opened,
            effect_reference="EB-CAPABILITY",
        )
        decision = ActionPolicy((base_url,)).evaluate(choose, opened.url)
        assert decision.allowed is True
        assert decision.risk is RiskClass.INPUT
        assert decision.requires_approval is False
        runner.execute(choose)
        assert candidate(runner.snapshot(), "Timezone").current_value == "Dublin"

        runner.execute(
            ProposedAction(
                kind=ActionKind.SCROLL,
                scroll_y=1_200,
                description="Scroll to the lower dynamic content.",
            )
        )
        assert runner._page.evaluate("window.scrollY") > 0
        runner.execute(
            ProposedAction(
                kind=ActionKind.WAIT,
                wait_ms=150,
                description="Wait for a bounded dynamic update.",
            )
        )

        manual = candidate(runner.snapshot(), "Download manual")
        receipt = runner.execute(
            ProposedAction(
                kind=ActionKind.DOWNLOAD,
                locator=manual.locator,
                url=manual.href,
                description="Download the observed manual.",
            )
        )
        assert receipt.external_id == "capability-manual.txt"
        assert (
            receipt.evidence_sha256
            == hashlib.sha256(b"Effect Browser generic capability harness.\n").hexdigest()
        )
    finally:
        runner.close()
        server.should_exit = True
        thread.join(timeout=10)
        api.get_store.cache_clear()
        get_settings.cache_clear()
