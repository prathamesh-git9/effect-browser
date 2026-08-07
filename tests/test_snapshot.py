from pathlib import Path

import pytest

from effect_browser.browser.snapshot import ScraplingSnapshotter
from effect_browser.domain import digest


def snapshotter(tmp_path: Path) -> ScraplingSnapshotter:
    return ScraplingSnapshotter(tmp_path / "elements.db")


def test_scrapling_extracts_candidate_bound_dynamic_controls(tmp_path: Path) -> None:
    html = """
    <html><body>
      <label for="country">Country</label>
      <select id="country" name="country" required>
        <option value="">Choose</option>
        <option value="IE">Ireland</option>
      </select>
      <button type="button">Continue</button>
      <button type="submit">Submit application</button>
      <a href="/privacy">Privacy</a>
      <label for="resume">Résumé</label>
      <input id="resume" name="resume" type="file" required>
      <input type="hidden" name="csrf" value="secret">
    </body></html>
    """

    snapshot = snapshotter(tmp_path).build(
        html=html,
        url="https://jobs.example.test/apply",
        title="Apply",
        state_sha256=digest(html),
    )

    assert [candidate.name for candidate in snapshot.candidates] == [
        "Country",
        "Continue",
        "Submit application",
        "Privacy",
        "Résumé",
    ]
    country, continue_button, submit, privacy, resume = snapshot.candidates
    assert country.options == (" | Choose", "IE | Ireland")
    assert country.required is True
    assert country.current_value == ""
    assert continue_button.interaction == "ambiguous"
    assert submit.interaction == "commit"
    assert privacy.interaction == "navigation"
    assert privacy.href == "https://jobs.example.test/privacy"
    assert resume.interaction == "upload"
    assert resume.input_type == "file"
    assert resume.current_value is None
    assert all(candidate.locator.selector for candidate in snapshot.candidates)
    assert all(candidate.locator.adaptive_id for candidate in snapshot.candidates)


def test_scrapling_extracts_cooperative_submission_contract(tmp_path: Path) -> None:
    html = """
    <form data-effect-reconciliation-url="/receipts?ref={effect_key}"
      data-effect-reconciliation-text="Stored {effect_key}"
      data-effect-receipt-test-id="receipt">
      <button type="submit">Submit</button>
    </form>
    """

    snapshot = snapshotter(tmp_path).build(
        html=html,
        url="https://example.test/apply",
        title="Apply",
        state_sha256="state",
    )

    assert snapshot.submission_contract is not None
    assert snapshot.submission_contract.url_template == "/receipts?ref={effect_key}"
    assert snapshot.submission_contract.receipt_test_id == "receipt"


def test_observed_tab_button_is_a_reversible_option(tmp_path: Path) -> None:
    html = """
    <ul role="tablist">
      <li><button role="tab" aria-selected="false" type="button">One way</button></li>
    </ul>
    """

    snapshot = snapshotter(tmp_path).build(
        html=html,
        url="https://carrier.example.test/book",
        title="Book",
        state_sha256=digest(html),
    )

    assert len(snapshot.candidates) == 1
    assert snapshot.candidates[0].role == "tab"
    assert snapshot.candidates[0].interaction == "option"


@pytest.mark.parametrize(
    "label",
    ["Reject all", "Cookie preferences", "Manage privacy choices"],
)
def test_privacy_preserving_consent_controls_are_deterministic_choices(
    tmp_path: Path,
    label: str,
) -> None:
    html = f'<button id="privacy-control" type="button">{label}</button>'

    snapshot = snapshotter(tmp_path).build(
        html=html,
        url="https://carrier.example.test/book",
        title="Privacy",
        state_sha256=digest(html),
    )

    assert snapshot.candidates[0].interaction == "consent"


def test_form_associated_privacy_label_keeps_commit_semantics(tmp_path: Path) -> None:
    html = '<form method="get"><button id="reject">Reject all</button></form>'

    snapshot = snapshotter(tmp_path).build(
        html=html,
        url="https://carrier.example.test/book",
        title="Privacy",
        state_sha256=digest(html),
    )

    assert snapshot.candidates[0].interaction == "commit"


def test_scrapling_relocates_saved_element_after_layout_drift(tmp_path: Path) -> None:
    parser = snapshotter(tmp_path)
    first = parser.build(
        html="""
        <html><body><main><button class="old primary">
        Continue application</button></main></body></html>
        """,
        url="https://jobs.example.test/apply",
        title="Apply",
        state_sha256="first",
    )
    original = first.candidates[0]

    relocated = parser.relocate(
        html="""
        <html><body><div class="new-shell"><section>
        <button class="new primary" data-version="2">
        Continue application</button></section></div></body></html>
        """,
        url="https://jobs.example.test/apply",
        adaptive_id=original.locator.adaptive_id or "",
    )

    assert relocated is not None
    assert "button" in relocated


def test_duplicate_numeric_ids_use_valid_attribute_selectors(tmp_path: Path) -> None:
    html = """
    <html><body>
      <section><label>First <input id="828933" name="first"></label></section>
      <div><label>Second <input id="828933" name="second"></label></div>
    </body></html>
    """

    snapshot = snapshotter(tmp_path).build(
        html=html,
        url="https://carrier.example.test/book",
        title="Book",
        state_sha256=digest(html),
    )

    selectors = [candidate.locator.selector for candidate in snapshot.candidates]
    assert len(selectors) == 2
    assert all(selector and "#828933" not in selector for selector in selectors)
    assert all(selector and '[id="828933"]' in selector for selector in selectors)
    assert len(set(selectors)) == 2
