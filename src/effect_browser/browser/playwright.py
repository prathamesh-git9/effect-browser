from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import Locator as PWLocator
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from effect_browser.browser.snapshot import ScraplingSnapshotter
from effect_browser.domain import (
    ActionKind,
    BrowserReceipt,
    Observation,
    PageSnapshot,
    ProposedAction,
    ReconciliationSpec,
    digest,
    utc_now,
)


class PlaywrightDriver:
    def __init__(
        self,
        *,
        executable_path: str | None = None,
        headless: bool = True,
        sandbox: bool = True,
        artifacts_directory: Path = Path("artifacts"),
    ) -> None:
        artifacts_directory.mkdir(parents=True, exist_ok=True)
        self.artifacts_directory = artifacts_directory
        self.session_id = str(uuid4())
        self._playwright: Playwright = sync_playwright().start()
        options = {
            "headless": headless,
            "chromium_sandbox": sandbox,
            "env": {},
            "args": ["--disable-extensions", "--disable-file-system"],
        }
        if executable_path:
            options["executable_path"] = executable_path
        self._browser: Browser = self._playwright.chromium.launch(**options)
        self._context: BrowserContext = self._browser.new_context(
            viewport={"width": 1440, "height": 900}
        )
        self._context.tracing.start(screenshots=True, snapshots=True)
        self._page: Page = self._context.new_page()
        self._snapshotter = ScraplingSnapshotter(
            artifacts_directory / "scrapling-elements.db"
        )

    def observe(self) -> Observation:
        self._stabilize()
        title = self._page.title()
        url = self._page.url
        body = self._page.locator("body").inner_text() if url != "about:blank" else ""
        controls = []
        locator = self._page.locator("input, textarea, select")
        for index in range(locator.count()):
            item = locator.nth(index)
            controls.append(
                {
                    "name": item.get_attribute("name"),
                    "type": item.get_attribute("type"),
                    "value": item.input_value(),
                }
            )
        state_sha256 = digest(
            {
                "url": url,
                "title": title,
                "body": _normalize(body),
                "controls": controls,
            }
        )
        screenshot = self.artifacts_directory / f"{self.session_id}-{uuid4()}.png"
        self._page.screenshot(path=str(screenshot), full_page=True)
        return Observation(
            url=url,
            title=title,
            state_sha256=state_sha256,
            captured_at=utc_now(),
            screenshot_path=str(screenshot),
        )

    def snapshot(self) -> PageSnapshot:
        observation = self.observe()
        if observation.url == "about:blank":
            return PageSnapshot(
                url=observation.url,
                title=observation.title,
                state_sha256=observation.state_sha256,
                text_excerpt="",
                candidates=(),
                captured_at=observation.captured_at,
            )
        snapshot = self._snapshotter.build(
            html=self._page.content(),
            url=observation.url,
            title=observation.title,
            state_sha256=observation.state_sha256,
        )
        visible = []
        for candidate in snapshot.candidates:
            target = self._page.locator(candidate.locator.selector or "")
            if target.count() == 1 and target.is_visible():
                filled = False
                if candidate.interaction == "input":
                    try:
                        filled = bool(target.input_value().strip())
                    except PlaywrightTimeoutError:
                        filled = False
                visible.append(candidate.model_copy(update={"filled": filled}))
        return snapshot.model_copy(update={"candidates": tuple(visible)})

    def execute(self, action: ProposedAction) -> BrowserReceipt:
        if action.kind is ActionKind.NAVIGATE:
            self._page.goto(action.url or "", wait_until="domcontentloaded")
        elif action.kind is ActionKind.FILL:
            target = self._locator(action)
            if target.evaluate("element => element.tagName === 'SELECT'"):
                target.select_option(action.value or "")
            else:
                target.fill(action.value or "")
        elif action.kind in {ActionKind.CLICK, ActionKind.SUBMIT}:
            self._locator(action).click()
            wait_state = (
                "networkidle" if action.kind is ActionKind.SUBMIT else "domcontentloaded"
            )
            self._page.wait_for_load_state(wait_state, timeout=10_000)
        else:
            raise ValueError(f"unsupported browser action: {action.kind.value}")
        return self._receipt(action.effect_key or f"local-{action.kind.value}")

    def reconcile(self, spec: ReconciliationSpec) -> BrowserReceipt | None:
        self._page.goto(spec.url, wait_until="domcontentloaded")
        matches = (
            self._page.get_by_test_id(spec.receipt_test_id)
            if spec.receipt_test_id
            else self._page.get_by_text(spec.expected_text, exact=False)
        )
        if matches.count() == 0:
            return None
        text = matches.first.inner_text()
        if spec.expected_text not in text:
            return None
        external_id = (
            matches.first.get_attribute("data-external-id") or spec.external_reference
        )
        return self._receipt(external_id)

    def close(self) -> None:
        trace = self.artifacts_directory / f"{self.session_id}-trace.zip"
        try:
            self._context.tracing.stop(path=str(trace))
        finally:
            self._context.close()
            self._browser.close()
            self._playwright.stop()

    def _locator(self, action: ProposedAction) -> PWLocator:
        locator = action.locator
        if locator is None:
            raise ValueError("action has no locator")
        if locator.test_id:
            return self._page.get_by_test_id(locator.test_id)
        if locator.label:
            return self._page.get_by_label(locator.label, exact=False)
        if locator.selector:
            target = self._page.locator(locator.selector)
            if target.count() == 0:
                try:
                    target.wait_for(state="attached", timeout=5_000)
                except PlaywrightTimeoutError:
                    pass
            if target.count() == 0 and locator.adaptive_id:
                relocated = self._snapshotter.relocate(
                    html=self._page.content(),
                    url=self._page.url,
                    adaptive_id=locator.adaptive_id,
                )
                if relocated:
                    target = self._page.locator(relocated)
            if target.count() != 1:
                raise ValueError(
                    "candidate selector must resolve to exactly one live element"
                )
            return target
        return self._page.get_by_role(locator.role or "", name=locator.name, exact=True)

    def _stabilize(self) -> None:
        try:
            self._page.wait_for_load_state("networkidle", timeout=3_000)
        except PlaywrightTimeoutError:
            # Long-polling pages may never become idle. The state hash still protects
            # execution: later drift invalidates the action instead of weakening safety.
            pass

    def _receipt(self, external_id: str) -> BrowserReceipt:
        body = self._page.locator("body").inner_text()
        return BrowserReceipt(
            external_id=external_id,
            url=self._page.url,
            evidence_sha256=digest(
                {
                    "url": self._page.url,
                    "title": self._page.title(),
                    "body": _normalize(body),
                }
            ),
            captured_at=utc_now(),
        )


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
