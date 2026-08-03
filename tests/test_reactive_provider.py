from pathlib import Path

import pytest

from effect_browser.browser.snapshot import ScraplingSnapshotter
from effect_browser.domain import ActionKind, Locator, ProposedAction, StepChoice
from effect_browser.providers.reactive import bind_choice
from effect_browser.uploads import sha256_file


def page_snapshot(tmp_path: Path):
    return ScraplingSnapshotter(tmp_path / "elements.db").build(
        html="""
        <form data-effect-reconciliation-url="/receipt?ref={effect_key}"
          data-effect-reconciliation-text="Stored {effect_key}">
          <label for="email">Email</label><input id="email" type="email">
          <label for="insurance">Include insurance</label>
          <input id="insurance" type="checkbox">
          <label for="region">Region</label>
          <select id="region"><option value="ie">Ireland</option></select>
          <a href="/next-step">Continue to the next step</a>
          <a href="/manual.txt" download>Download manual</a>
          <label for="resume">Résumé</label><input id="resume" type="file">
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
        prior_actions=(
            ProposedAction(
                kind=ActionKind.FILL,
                locator=Locator(label="Email"),
                value="candidate@example.test",
                description="Fill verified email.",
                target_name="Email",
            ),
        ),
    )

    assert proposal.locator == submit.locator
    assert proposal.planned_from_sha256 == "fresh-state"
    assert proposal.effect_key == "EB-12345678"
    assert proposal.reconciliation is not None
    assert proposal.reconciliation.url.endswith("/receipt?ref=EB-12345678")
    assert proposal.outgoing_review is not None
    assert proposal.outgoing_review.fields[0].label == "Email"
    assert proposal.outgoing_review.fields[0].value == ""
    assert proposal.outgoing_review.observation_sha256 == snapshot.state_sha256


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


def test_read_only_finish_retains_goal_specific_rendered_evidence(
    tmp_path: Path,
) -> None:
    proposal = bind_choice(
        StepChoice(
            kind=ActionKind.FINISH,
            description="Finish only after observing the requested page evidence.",
            expected_outcome="Submit application",
        ),
        page_snapshot(tmp_path),
        effect_reference="EB-12345678",
    )

    assert proposal.expected_outcome == "Submit application"
    assert proposal.planned_from_sha256 == "fresh-state"


def test_upload_choice_binds_path_and_hash_without_exposing_path_in_snapshot(
    tmp_path: Path,
) -> None:
    document = tmp_path / "synthetic-resume.txt"
    document.write_bytes(b"synthetic resume fixture")
    snapshot = page_snapshot(tmp_path)
    candidate = next(item for item in snapshot.candidates if item.interaction == "upload")

    proposal = bind_choice(
        StepChoice(
            kind=ActionKind.UPLOAD,
            candidate_id=candidate.id,
            file_path=document.resolve(),
            document_sha256=sha256_file(document),
            description="Attach the approved synthetic document.",
        ),
        snapshot,
        effect_reference="EB-12345678",
    )

    assert proposal.locator == candidate.locator
    assert proposal.file_path == document.resolve()
    assert proposal.document_sha256 == sha256_file(document)
    assert document.name not in snapshot.model_dump_json()


def test_submit_review_includes_only_latest_uploaded_document_hash(
    tmp_path: Path,
) -> None:
    snapshot = page_snapshot(tmp_path)
    resume = next(item for item in snapshot.candidates if item.interaction == "upload")
    submit = next(item for item in snapshot.candidates if item.interaction == "commit")
    document = (tmp_path / "synthetic-resume.txt").resolve()
    old_upload = ProposedAction(
        kind=ActionKind.UPLOAD,
        locator=resume.locator,
        file_path=document,
        document_sha256="1" * 64,
        description="Attach an older document revision.",
        target_name=resume.name,
    )
    latest_upload = old_upload.model_copy(
        update={
            "document_sha256": "2" * 64,
            "description": "Attach the final document revision.",
        }
    )
    filled_snapshot = snapshot.model_copy(
        update={
            "candidates": tuple(
                item.model_copy(update={"filled": True}) if item.id == resume.id else item
                for item in snapshot.candidates
            )
        }
    )

    proposal = bind_choice(
        StepChoice(
            kind=ActionKind.SUBMIT,
            candidate_id=submit.id,
            description="Submit the reviewed application.",
            expected_outcome="One stored application.",
        ),
        filled_snapshot,
        effect_reference="EB-12345678",
        prior_actions=(old_upload, latest_upload),
    )

    assert proposal.outgoing_review is not None
    assert proposal.outgoing_review.document_sha256s == ("2" * 64,)


def test_generic_choices_bind_only_to_compatible_scrapling_candidates(
    tmp_path: Path,
) -> None:
    snapshot = page_snapshot(tmp_path)
    insurance = next(
        item for item in snapshot.candidates if item.name == "Include insurance"
    )
    region = next(item for item in snapshot.candidates if item.name == "Region")
    manual = next(item for item in snapshot.candidates if item.name == "Download manual")

    check = bind_choice(
        StepChoice(
            kind=ActionKind.CHECK,
            candidate_id=insurance.id,
            checked=True,
            description="Enable the observed checkbox.",
        ),
        snapshot,
        effect_reference="EB-12345678",
    )
    press = bind_choice(
        StepChoice(
            kind=ActionKind.PRESS,
            candidate_id=region.id,
            key="ArrowDown",
            description="Move through the observed select options.",
        ),
        snapshot,
        effect_reference="EB-12345678",
    )
    download = bind_choice(
        StepChoice(
            kind=ActionKind.DOWNLOAD,
            candidate_id=manual.id,
            description="Download the observed file.",
        ),
        snapshot,
        effect_reference="EB-12345678",
    )

    assert check.locator == insurance.locator
    assert check.checked is True
    assert press.locator == region.locator
    assert press.key == "ArrowDown"
    assert manual.interaction == "download"
    assert download.url == "https://jobs.example.test/manual.txt"

    submit = next(item for item in snapshot.candidates if item.interaction == "commit")
    with pytest.raises(ValueError, match="input candidate"):
        bind_choice(
            StepChoice(
                kind=ActionKind.PRESS,
                candidate_id=submit.id,
                key="Space",
                description="Try to activate a commit with a key.",
            ),
            snapshot,
            effect_reference="EB-12345678",
        )


def test_navigation_click_retains_observed_destination(tmp_path: Path) -> None:
    snapshot = page_snapshot(tmp_path)
    link = next(
        item for item in snapshot.candidates if item.name == "Continue to the next step"
    )

    proposal = bind_choice(
        StepChoice(
            kind=ActionKind.CLICK,
            candidate_id=link.id,
            description="Follow the observed next-step link.",
        ),
        snapshot,
        effect_reference="EB-12345678",
    )

    assert proposal.target_interaction == "navigation"
    assert proposal.url == "https://jobs.example.test/next-step"


def open_combobox_snapshot(tmp_path: Path):
    return ScraplingSnapshotter(tmp_path / "elements.db").build(
        html="""
        <label for="timezone">Timezone</label>
        <input id="timezone" role="combobox" aria-expanded="true"
          aria-controls="timezone-options" autocomplete="off">
        <div id="timezone-options" role="listbox">
          <div id="tz-dublin" role="option">Dublin</div>
          <div id="tz-lisbon" role="option">Lisbon</div>
          <div id="tz-commit" role="option">Submit application</div>
        </div>
        """,
        url="https://jobs.example.test/apply",
        title="Apply",
        state_sha256="fresh-state",
    )


def test_observed_combobox_option_click_binds_as_option_interaction(
    tmp_path: Path,
) -> None:
    snapshot = open_combobox_snapshot(tmp_path)
    combobox = next(item for item in snapshot.candidates if item.role == "combobox")
    dublin = next(item for item in snapshot.candidates if item.name == "Dublin")

    assert combobox.interaction == "input"
    assert dublin.interaction == "option"

    proposal = bind_choice(
        StepChoice(
            kind=ActionKind.CLICK,
            candidate_id=dublin.id,
            description="Choose the observed Dublin option.",
        ),
        snapshot,
        effect_reference="EB-12345678",
    )

    assert proposal.locator == dublin.locator
    assert proposal.target_interaction == "option"
    assert proposal.planned_from_sha256 == "fresh-state"


def test_option_role_takes_precedence_over_commit_wording(tmp_path: Path) -> None:
    snapshot = open_combobox_snapshot(tmp_path)
    confirm_option = next(
        item for item in snapshot.candidates if item.name == "Submit application"
    )

    assert confirm_option.role == "option"
    assert confirm_option.interaction == "option"
    proposal = bind_choice(
        StepChoice(
            kind=ActionKind.CLICK,
            candidate_id=confirm_option.id,
            description="Choose the observed confirm option without submitting.",
        ),
        snapshot,
        effect_reference="EB-12345678",
    )
    assert proposal.target_interaction == "option"
