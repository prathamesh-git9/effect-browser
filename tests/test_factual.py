from __future__ import annotations

from pathlib import Path

from effect_browser.domain import (
    ActionKind,
    AnswerSensitivity,
    AnswerSource,
    AnswerSourceKind,
    ElementCandidate,
    Locator,
    PlanningFact,
    VerificationState,
)
from effect_browser.factual import (
    canonical_field_name,
    deterministic_required_choice,
)


def candidate(
    name: str,
    *,
    interaction: str = "input",
    input_type: str | None = "text",
) -> ElementCandidate:
    return ElementCandidate(
        id="C001",
        tag="input",
        role="textbox",
        name=name,
        input_type=input_type,
        required=True,
        interaction=interaction,
        locator=Locator(
            selector="body > input",
            adaptive_id="candidate-input:0",
        ),
    )


def fact(
    field_name: str,
    value: str | None,
    state: VerificationState,
) -> PlanningFact:
    return PlanningFact(
        field_name=field_name,
        value=value,
        source=AnswerSource(kind=AnswerSourceKind.USER),
        sensitivity=AnswerSensitivity.CONSEQUENTIAL,
        verification_state=state,
    )


def test_canonical_field_name_is_stable_for_human_labels() -> None:
    assert canonical_field_name("Résumé / CV") == "resume_cv"
    assert canonical_field_name("Years using Python") == "years_using_python"


def test_missing_or_unverified_consequential_fact_requires_handoff() -> None:
    missing = deterministic_required_choice(
        text_excerpt="Application",
        candidates=(candidate("Work authorization"),),
        facts=(),
        document_path=None,
        document_sha256=None,
    )
    unverified = deterministic_required_choice(
        text_excerpt="Application",
        candidates=(candidate("Work authorization"),),
        facts=(
            fact(
                "work_authorization",
                None,
                VerificationState.UNVERIFIED,
            ),
        ),
        document_path=None,
        document_sha256=None,
    )

    assert missing is not None
    assert missing.kind is ActionKind.HANDOFF
    assert "no value was invented" in missing.description
    assert unverified is not None
    assert unverified.kind is ActionKind.HANDOFF


def test_verified_fact_and_task_document_become_local_deterministic_choices(
    tmp_path: Path,
) -> None:
    fill = deterministic_required_choice(
        text_excerpt="Application",
        candidates=(candidate("Country"),),
        facts=(
            fact(
                "country",
                "Ireland",
                VerificationState.VERIFIED,
            ),
        ),
        document_path=None,
        document_sha256=None,
    )
    document_path = (tmp_path / "synthetic-resume.txt").resolve()
    upload = deterministic_required_choice(
        text_excerpt="Application",
        candidates=(
            candidate(
                "Résumé document",
                interaction="upload",
                input_type="file",
            ),
        ),
        facts=(),
        document_path=document_path,
        document_sha256="a" * 64,
    )

    assert fill is not None
    assert fill.kind is ActionKind.FILL
    assert fill.value == "Ireland"
    assert upload is not None
    assert upload.kind is ActionKind.UPLOAD
    assert upload.file_path == document_path
    assert upload.document_sha256 == "a" * 64


def test_human_challenge_stops_before_any_candidate_action() -> None:
    choice = deterministic_required_choice(
        text_excerpt="Please verify you are human with reCAPTCHA",
        candidates=(candidate("Email", input_type="email"),),
        facts=(
            fact(
                "email",
                "synthetic@example.test",
                VerificationState.VERIFIED,
            ),
        ),
        document_path=None,
        document_sha256=None,
    )

    assert choice is not None
    assert choice.kind is ActionKind.HANDOFF
    assert "Human verification" in choice.description
