from pathlib import Path

import pytest

from effect_browser.browser.snapshot import ScraplingSnapshotter
from effect_browser.domain import ActionKind, StepChoice
from effect_browser.providers.reactive import bind_choice


def page_snapshot(tmp_path: Path):
    return ScraplingSnapshotter(tmp_path / "elements.db").build(
        html="""
        <form data-effect-reconciliation-url="/receipt?ref={effect_key}"
          data-effect-reconciliation-text="Stored {effect_key}">
          <label for="email">Email</label><input id="email" type="email">
          <button type="submit">Submit application</button>
        </form>
        """,
        url="https://jobs.example.test/apply",
        title="Apply",
        state_sha256="fresh-state",
    )


def test_step_choice_is_bound_to_fresh_candidate_and_receipt_contract(
    tmp_path: Path,
) -> None:
    snapshot = page_snapshot(tmp_path)
    submit = next(
        candidate
        for candidate in snapshot.candidates
        if candidate.interaction == "commit"
    )

    proposal = bind_choice(
        StepChoice(
            kind=ActionKind.SUBMIT,
            candidate_id=submit.id,
            description="Submit the reviewed application.",
            expected_outcome="One stored application.",
        ),
        snapshot,
        effect_reference="EB-12345678",
    )

    assert proposal.locator == submit.locator
    assert proposal.planned_from_sha256 == "fresh-state"
    assert proposal.effect_key == "EB-12345678"
    assert proposal.reconciliation is not None
    assert proposal.reconciliation.url.endswith("/receipt?ref=EB-12345678")


def test_step_choice_cannot_invent_candidate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing or disabled"):
        bind_choice(
            StepChoice(
                kind=ActionKind.FILL,
                candidate_id="C999",
                value="invented",
                description="Try to fill a made-up field.",
            ),
            page_snapshot(tmp_path),
            effect_reference="EB-12345678",
        )
