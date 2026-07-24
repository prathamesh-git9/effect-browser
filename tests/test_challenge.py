from __future__ import annotations

from effect_browser.challenge import HandoffKind, detect_challenge
from effect_browser.domain import ElementCandidate, Locator, PageSnapshot, utc_now


def _candidate(
    *,
    id: str = "c1",
    role: str = "textbox",
    name: str = "Field",
    input_type: str | None = None,
    disabled: bool = False,
    label: str = "field",
) -> ElementCandidate:
    return ElementCandidate(
        id=id,
        tag="input",
        role=role,
        name=name,
        input_type=input_type,
        disabled=disabled,
        interaction="input",
        locator=Locator(label=label),
    )


def _snapshot(
    *,
    title: str = "Job application",
    text_excerpt: str = "Complete the fields below.",
    candidates: tuple[ElementCandidate, ...] = (),
) -> PageSnapshot:
    return PageSnapshot(
        url="https://example.test/apply",
        title=title,
        state_sha256="0" * 64,
        text_excerpt=text_excerpt,
        candidates=candidates,
        captured_at=utc_now(),
    )


def test_clear_page_has_no_challenge() -> None:
    snapshot = _snapshot(candidates=(_candidate(name="Full name"),))
    assert detect_challenge(snapshot) is None


def test_captcha_marker_in_candidate_is_detected() -> None:
    snapshot = _snapshot(
        candidates=(_candidate(id="turnstile", name="cf-turnstile", label="captcha"),),
    )
    challenge = detect_challenge(snapshot)
    assert challenge is not None
    assert challenge.kind is HandoffKind.CAPTCHA
    assert "captcha" in challenge.evidence.lower() or "turnstile" in challenge.evidence


def test_captcha_marker_in_page_text_is_detected() -> None:
    snapshot = _snapshot(text_excerpt="Please verify you are human before continuing.")
    challenge = detect_challenge(snapshot)
    assert challenge is not None
    assert challenge.kind is HandoffKind.CAPTCHA


def test_mfa_marker_is_detected() -> None:
    snapshot = _snapshot(
        title="Two-factor authentication",
        text_excerpt="Enter the verification code we sent to your phone.",
    )
    challenge = detect_challenge(snapshot)
    assert challenge is not None
    assert challenge.kind is HandoffKind.MFA


def test_captcha_takes_precedence_over_mfa() -> None:
    snapshot = _snapshot(
        title="Verify you are human",
        text_excerpt="Also enter the verification code we sent to you.",
    )
    challenge = detect_challenge(snapshot)
    assert challenge is not None
    assert challenge.kind is HandoffKind.CAPTCHA


def test_disabled_candidate_markers_are_ignored() -> None:
    snapshot = _snapshot(
        candidates=(
            _candidate(id="hidden", name="hcaptcha", label="captcha", disabled=True),
        ),
    )
    assert detect_challenge(snapshot) is None


def test_ordinary_login_fields_do_not_trip_mfa() -> None:
    snapshot = _snapshot(
        text_excerpt="Enter your email and password to sign in.",
        candidates=(
            _candidate(id="pw", name="Password", input_type="password", label="password"),
        ),
    )
    assert detect_challenge(snapshot) is None
