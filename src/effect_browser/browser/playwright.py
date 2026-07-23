from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import Locator as PWLocator

from effect_browser.domain import (
    ActionKind,
    BrowserReceipt,
    Observation,
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

    def observe(self) -> Observation:
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
            self._page.wait_for_load_state("domcontentloaded")
        else:
            raise ValueError(f"unsupported browser action: {action.kind.value}")
        return self._receipt(action.effect_key or f"local-{action.kind.value}")

    def reconcile(self, spec: ReconciliationSpec) -> BrowserReceipt | None:
        self._page.goto(spec.url, wait_until="domcontentloaded")
        matches = self._page.get_by_text(spec.expected_text, exact=False)
        if matches.count() == 0:
            return None
        return self._receipt(spec.external_reference)

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
        return self._page.get_by_role(locator.role or "", name=locator.name, exact=True)

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
