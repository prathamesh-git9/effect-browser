from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from effect_browser.domain import (
    ActionKind,
    ElementCandidate,
    PlanningFact,
    ProfileAnswer,
    StepChoice,
    VerificationState,
)

CONSEQUENTIAL_TERMS = re.compile(
    r"\b(name|email|phone|country|location|address|authori[sz](?:ation|ed)|"
    r"sponsor|sponsorship|visa|salary|compensation|experience|years|age|gender|"
    r"race|ethnicity|disability|veteran|legal(?:ly)?|pronoun|education|employment|"
    r"resume|cv|birth|dob|citizenship|nationality|social security|tax|marital|"
    r"religion|postal|linkedin|website|company|employer|title|privacy|consent|"
    r"school|degree)\b",
    re.IGNORECASE,
)
HUMAN_CHALLENGE_TERMS = re.compile(
    r"\b(captcha|recaptcha|hcaptcha|verify you are human|enter (?:a |the )?"
    r"one[- ]time (?:code|password)|enter (?:a |the )?verification code|"
    r"(?:multi|two)[- ]factor authentication required)\b",
    re.IGNORECASE,
)
HUMAN_CHALLENGE_FIELD_TERMS = re.compile(
    r"\b(one[- ]time code|verification code|authenticator code|password|passcode|"
    r"mfa code|otp)\b",
    re.IGNORECASE,
)


def canonical_field_name(label: str) -> str:
    ascii_label = (
        unicodedata.normalize("NFKD", label)
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )
    normalized = re.sub(r"[^a-z0-9]+", "_", ascii_label).strip("_")
    return normalized or "unnamed_field"


def planning_facts(answers: list[ProfileAnswer]) -> tuple[PlanningFact, ...]:
    return tuple(
        PlanningFact(
            field_name=answer.field_name,
            value=(
                answer.value
                if answer.verification_state is VerificationState.VERIFIED
                else None
            ),
            source=answer.source,
            sensitivity=answer.sensitivity,
            verification_state=answer.verification_state,
        )
        for answer in answers
    )


def deterministic_required_choice(
    *,
    text_excerpt: str,
    candidates: tuple[ElementCandidate, ...],
    facts: tuple[PlanningFact, ...],
    document_path: Path | None,
    document_sha256: str | None,
) -> StepChoice | None:
    if HUMAN_CHALLENGE_TERMS.search(text_excerpt) or any(
        candidate.input_type == "password"
        or HUMAN_CHALLENGE_FIELD_TERMS.search(candidate.name)
        for candidate in candidates
    ):
        return StepChoice(
            kind=ActionKind.HANDOFF,
            description=(
                "Human verification, MFA, or a one-time code is present. "
                "Automatic execution stopped."
            ),
        )
    facts_by_name = {fact.field_name: fact for fact in facts}
    expanded = next(
        (
            candidate
            for candidate in candidates
            if candidate.role == "combobox" and candidate.expanded
        ),
        None,
    )
    if expanded is not None and _is_consequential(expanded):
        field_name = canonical_field_name(expanded.name)
        fact = facts_by_name.get(field_name)
        if (
            fact is None
            or fact.verification_state is not VerificationState.VERIFIED
            or fact.value is None
        ):
            return StepChoice(
                kind=ActionKind.HANDOFF,
                description=(
                    f"Verified profile answer {field_name!r} is required for "
                    f"{expanded.name!r}; no option was guessed."
                ),
            )
        wanted = fact.value.casefold().strip()
        matches = [
            candidate
            for candidate in candidates
            if candidate.interaction == "option"
            and (
                candidate.name.casefold().strip() == wanted
                or candidate.name.casefold().startswith(f"{wanted} ")
                or candidate.name.casefold().startswith(f"{wanted}+")
            )
        ]
        if len(matches) != 1:
            return StepChoice(
                kind=ActionKind.HANDOFF,
                description=(
                    f"Exactly one observed option matching verified fact "
                    f"{field_name!r} was required; found {len(matches)}."
                ),
            )
        return StepChoice(
            kind=ActionKind.CLICK,
            candidate_id=matches[0].id,
            description=(
                f"Choose observed option {matches[0].name} for verified fact "
                f"{field_name}."
            ),
        )
    for candidate in candidates:
        if candidate.disabled or candidate.filled:
            continue
        if candidate.interaction == "upload":
            if document_path is None or document_sha256 is None:
                return StepChoice(
                    kind=ActionKind.HANDOFF,
                    description=(
                        f"Required document for {candidate.name!r} is not attached "
                        "to this task."
                    ),
                )
            return StepChoice(
                kind=ActionKind.UPLOAD,
                candidate_id=candidate.id,
                file_path=document_path,
                document_sha256=document_sha256,
                description=f"Attach the task-bound document to {candidate.name}.",
            )
        if candidate.input_type in {"checkbox", "radio"}:
            continue
        if candidate.interaction != "input" or not _is_consequential(candidate):
            continue
        field_name = canonical_field_name(candidate.name)
        fact = facts_by_name.get(field_name)
        if (
            fact is None
            or fact.verification_state is not VerificationState.VERIFIED
            or fact.value is None
        ):
            return StepChoice(
                kind=ActionKind.HANDOFF,
                description=(
                    f"Verified profile answer {field_name!r} is required for "
                    f"{candidate.name!r}; no value was invented."
                ),
            )
        if candidate.role == "combobox":
            if candidate.tag.casefold() == "select":
                return StepChoice(
                    kind=ActionKind.FILL,
                    candidate_id=candidate.id,
                    value=fact.value,
                    description=(
                        f"Select {candidate.name} from verified profile fact "
                        f"{field_name}."
                    ),
                )
            return StepChoice(
                kind=ActionKind.PRESS,
                candidate_id=candidate.id,
                key="ArrowDown",
                description=(
                    f"Open {candidate.name} so Scrapling can observe its options."
                ),
            )
        return StepChoice(
            kind=ActionKind.FILL,
            candidate_id=candidate.id,
            value=fact.value,
            description=f"Fill {candidate.name} from verified profile fact {field_name}.",
        )
    return None


def _is_consequential(candidate: ElementCandidate) -> bool:
    return candidate.input_type in {"email", "tel"} or bool(
        CONSEQUENTIAL_TERMS.search(candidate.name)
    )
