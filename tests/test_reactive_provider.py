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
