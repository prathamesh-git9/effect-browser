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
    required: bool = True,
) -> ElementCandidate:
    return ElementCandidate(
        id="C001",
        tag="input",
        role="textbox",
        name=name,
        input_type=input_type,
        required=required,
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


def test_privacy_rejection_is_selected_before_remote_planning() -> None:
    choice = deterministic_required_choice(
        text_excerpt="We value your privacy and use cookies.",
        candidates=(
            candidate("One way", interaction="option", input_type=None),
            candidate("Reject all", interaction="consent", input_type=None),
        ),
        facts=(),
        document_path=None,
        document_sha256=None,
    )

    assert choice is not None
    assert choice.kind is ActionKind.CLICK
    assert choice.candidate_id == "C001"
    assert "without accepting optional cookies" in choice.description


def test_optional_profile_field_is_left_to_reactive_planning() -> None:
    optional_newsletter = candidate(
        "Newsletter email",
        input_type="email",
        required=False,
    )

    missing = deterministic_required_choice(
        text_excerpt="Get occasional travel offers.",
        candidates=(optional_newsletter,),
        facts=(),
        document_path=None,
        document_sha256=None,
    )
    verified = deterministic_required_choice(
        text_excerpt="Get occasional travel offers.",
        candidates=(optional_newsletter,),
        facts=(
            fact(
                "newsletter_email",
                "traveller@example.test",
                VerificationState.VERIFIED,
            ),
        ),
        document_path=None,
        document_sha256=None,
    )

    assert missing is None
    assert verified is None


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


def test_verified_combobox_fact_opens_then_selects_one_observed_option() -> None:
    combobox = ElementCandidate(
        id="C001",
        tag="input",
        role="combobox",
        name="Country *",
        input_type="text",
        required=True,
        interaction="input",
        locator=Locator(
            selector="#country",
            adaptive_id="candidate-country:0",
        ),
    )
    country_fact = fact("country", "Ireland", VerificationState.VERIFIED)

    open_choice = deterministic_required_choice(
        text_excerpt="Application",
        candidates=(combobox,),
        facts=(country_fact,),
        document_path=None,
        document_sha256=None,
    )
    option = ElementCandidate(
        id="C002",
        tag="div",
        role="option",
        name="Ireland +353",
        interaction="option",
        locator=Locator(
            selector="#country-ie",
            adaptive_id="candidate-country-ie:0",
        ),
    )
    select_choice = deterministic_required_choice(
        text_excerpt="Application",
        candidates=(combobox.model_copy(update={"expanded": True}), option),
        facts=(country_fact,),
        document_path=None,
        document_sha256=None,
    )

    assert open_choice is not None
    assert open_choice.kind is ActionKind.PRESS
    assert open_choice.key == "ArrowDown"
    assert select_choice is not None
    assert select_choice.kind is ActionKind.CLICK
    assert select_choice.candidate_id == option.id


def test_native_select_uses_exact_verified_value() -> None:
    native = ElementCandidate(
        id="C001",
        tag="select",
        role="combobox",
        name="Country",
        required=True,
        interaction="input",
        locator=Locator(
            selector="#country",
            adaptive_id="candidate-country:0",
        ),
    )

    choice = deterministic_required_choice(
        text_excerpt="Application",
        candidates=(native,),
        facts=(fact("country", "Ireland", VerificationState.VERIFIED),),
        document_path=None,
        document_sha256=None,
    )

    assert choice is not None
    assert choice.kind is ActionKind.FILL
    assert choice.value == "Ireland"


def test_legally_authorized_combobox_is_consequential() -> None:
    authorization = ElementCandidate(
        id="C001",
        tag="input",
        role="combobox",
        name="Are you legally authorized to work in Ireland? *",
        input_type="text",
        required=True,
        interaction="input",
        locator=Locator(
            selector="#work-authorization",
            adaptive_id="candidate-work-authorization:0",
        ),
    )

    choice = deterministic_required_choice(
        text_excerpt="Application",
        candidates=(authorization,),
        facts=(
            fact(
                "are_you_legally_authorized_to_work_in_ireland",
                "Yes",
                VerificationState.VERIFIED,
            ),
        ),
        document_path=None,
        document_sha256=None,
    )

    assert choice is not None
    assert choice.kind is ActionKind.PRESS
    assert choice.key == "ArrowDown"


def test_verified_combobox_fact_never_guesses_an_unobserved_option() -> None:
    combobox = ElementCandidate(
        id="C001",
        tag="input",
        role="combobox",
        name="Data Privacy & Protection *",
        input_type="text",
        required=True,
        expanded=True,
        interaction="input",
        locator=Locator(
            selector="#privacy",
            adaptive_id="candidate-privacy:0",
        ),
    )

    choice = deterministic_required_choice(
        text_excerpt="Application",
        candidates=(combobox,),
        facts=(
            fact(
                "data_privacy_protection",
                "Acknowledge & Confirm",
                VerificationState.VERIFIED,
            ),
        ),
        document_path=None,
        document_sha256=None,
    )

    assert choice is not None
    assert choice.kind is ActionKind.HANDOFF
    assert "found 0" in choice.description
