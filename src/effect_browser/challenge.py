"""Deterministic detection of CAPTCHA and MFA challenges that require a human.

Effect Browser never attempts to solve an anti-automation or second-factor
challenge. When a fresh snapshot exposes one, the reactive loop stops and hands
the task to a human instead of guessing an action. Detection is a pure function
of the observed snapshot so it is auditable and reproducible.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from effect_browser.domain import ElementCandidate, PageSnapshot


class HandoffKind(StrEnum):
    CAPTCHA = "captcha"
    MFA = "mfa"


class HandoffChallenge(BaseModel):
    """An observed challenge that requires an explicit human handoff."""

    model_config = ConfigDict(frozen=True)

    kind: HandoffKind
    reason: str
    evidence: str


# CAPTCHA and bot-check markers. These block every interaction on the page, so
# they are checked before any second-factor markers.
_CAPTCHA_MARKERS: tuple[str, ...] = (
    "recaptcha",
    "hcaptcha",
    "captcha",
    "cf-turnstile",
    "turnstile",
    "i'm not a robot",
    "im not a robot",
    "not a robot",
    "are you human",
    "verify you are human",
    "verify you're human",
    "verify you are a human",
)

# Multi-factor / one-time-code markers. Kept specific to avoid matching ordinary
# password or login fields.
_MFA_MARKERS: tuple[str, ...] = (
    "verification code",
    "one-time code",
    "one time code",
    "one-time passcode",
    "one time passcode",
    "two-factor",
    "two factor",
    "multi-factor",
    "multi factor",
    "authenticator app",
    "authentication code",
    "security code",
    "code we sent",
    "code sent to",
    "enter the code",
    "6-digit code",
    "six-digit code",
)


def _first_marker(text: str, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        if marker in text:
            return marker
    return None


def _candidate_text(candidate: ElementCandidate) -> str:
    parts = (
        candidate.id,
        candidate.role,
        candidate.name,
        candidate.input_type or "",
        candidate.locator.label or "",
        candidate.locator.name or "",
        candidate.locator.test_id or "",
    )
    return " ".join(parts).lower()


def detect_challenge(snapshot: PageSnapshot) -> HandoffChallenge | None:
    """Return the challenge blocking a snapshot, or ``None`` when it is clear.

    CAPTCHA challenges take precedence over MFA because they gate every element
    on the page, including any code entry field.
    """

    haystacks: list[tuple[str, str]] = []
    for candidate in snapshot.candidates:
        if candidate.disabled:
            continue
        haystacks.append((_candidate_text(candidate), f"candidate {candidate.id!r}"))
    page_text = f"{snapshot.title}\n{snapshot.text_excerpt}".lower()
    haystacks.append((page_text, "page text"))

    for text, where in haystacks:
        marker = _first_marker(text, _CAPTCHA_MARKERS)
        if marker is not None:
            return HandoffChallenge(
                kind=HandoffKind.CAPTCHA,
                reason="a CAPTCHA challenge requires a human to proceed",
                evidence=f"{where} matched {marker!r}",
            )
    for text, where in haystacks:
        marker = _first_marker(text, _MFA_MARKERS)
        if marker is not None:
            return HandoffChallenge(
                kind=HandoffKind.MFA,
                reason=("a multi-factor authentication step requires a human to proceed"),
                evidence=f"{where} matched {marker!r}",
            )
    return None
