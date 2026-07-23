from pathlib import Path

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
    ]
    country, continue_button, submit, privacy = snapshot.candidates
    assert country.options == (" | Choose", "IE | Ireland")
    assert country.required is True
    assert country.current_value == ""
    assert continue_button.interaction == "ambiguous"
    assert submit.interaction == "commit"
    assert privacy.interaction == "navigation"
    assert privacy.href == "https://jobs.example.test/privacy"
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
