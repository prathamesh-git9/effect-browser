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
    PAYMENT = "payment"


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

_PAYMENT_CONTEXT_MARKERS: tuple[str, ...] = (
    "billing details",
    "card details",
    "complete payment",
    "credit card",
    "debit card",
    "payment details",
    "payment method",
    "secure payment",
)
_PAYMENT_CONTROL_MARKERS: tuple[str, ...] = (
    "complete payment",
    "confirm and pay",
    "make payment",
    "pay now",
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


def _payment_field_categories(text: str) -> set[str]:
    normalized = " ".join(text.replace("_", " ").replace("-", " ").split())
    compact = normalized.replace(" ", "")
    categories: set[str] = set()
    if "card number" in normalized or "cardnumber" in compact or "ccnumber" in compact:
        categories.add("card_number")
    if any(marker in normalized for marker in ("cvv", "cvc", "card security code")):
        categories.add("card_security_code")
    if any(
        marker in normalized
        for marker in ("card expiry", "card expiration", "expiry date", "expiration date")
    ):
        categories.add("card_expiry")
    if "name on card" in normalized or "cardholder name" in normalized:
        categories.add("cardholder")
    return categories


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
    page_has_payment_context = (
        _first_marker(page_text, _PAYMENT_CONTEXT_MARKERS) is not None
    )
    payment_categories: set[str] = set()
    payment_control = False
    for candidate in snapshot.candidates:
        if candidate.disabled:
            continue
        candidate_text = _candidate_text(candidate)
        payment_categories.update(_payment_field_categories(candidate_text))
        if candidate.interaction in {"ambiguous", "commit"} and _first_marker(
            candidate_text,
            _PAYMENT_CONTROL_MARKERS,
        ):
            payment_control = True
    if (
        "card_number" in payment_categories
        or len(payment_categories) >= 2
        or (page_has_payment_context and payment_categories)
        or (page_has_payment_context and payment_control)
    ):
        signals = sorted(payment_categories)
        if payment_control:
            signals.append("payment_commit_control")
        return HandoffChallenge(
            kind=HandoffKind.PAYMENT,
            reason=("the payment boundary was reached; payment entry is not authorized"),
            evidence=("payment boundary signals matched " + ",".join(signals)),
        )
    return None
