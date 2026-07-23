from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator as PWLocator
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from effect_browser.browser.snapshot import ScraplingSnapshotter
from effect_browser.domain import (
    ActionKind,
    BrowserReceipt,
    Observation,
    OutgoingReview,
    PageSnapshot,
    ProposedAction,
    ReconciliationSpec,
    digest,
    utc_now,
)
from effect_browser.transmission import (
    TransmissionBlocked,
    TransmissionReviewError,
    fingerprint_request,
)
from effect_browser.uploads import UploadGuard


class PlaywrightDriver:
    def __init__(
        self,
        *,
        executable_path: str | None = None,
        headless: bool = True,
        sandbox: bool = True,
        artifacts_directory: Path = Path("artifacts"),
        allowed_upload_roots: tuple[Path, ...] = (),
    ) -> None:
        artifacts_directory.mkdir(parents=True, exist_ok=True)
        self.artifacts_directory = artifacts_directory
        self._upload_guard = UploadGuard(allowed_upload_roots)
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
        try:
            self._browser: Browser = self._playwright.chromium.launch(**options)
        except BaseException:
            # A failed launch still owns Playwright's event-loop/greenlet state.
            # Release it or every later sync driver in this process fails with the
            # misleading "inside the asyncio loop" error.
            self._playwright.stop()
            raise
        self._context: BrowserContext = self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            service_workers="block",
        )
        self._context.route_web_socket("**/*", lambda socket: socket.close())
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
            input_type = item.get_attribute("type")
            value = item.input_value()
            controls.append(
                {
                    "name": item.get_attribute("name"),
                    "type": input_type,
                    "value": (
                        {"file_selected": bool(value)} if input_type == "file" else value
                    ),
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
                current_value = None
                if candidate.interaction in {"input", "upload"}:
                    try:
                        live_value = target.input_value()
                        filled = bool(live_value.strip())
                        current_value = (
                            live_value if candidate.interaction == "input" else None
                        )
                    except PlaywrightTimeoutError:
                        filled = False
                        current_value = None
                visible.append(
                    candidate.model_copy(
                        update={"filled": filled, "current_value": current_value}
                    )
                )
        return snapshot.model_copy(update={"candidates": tuple(visible)})

    def preview_submit(
        self,
        action: ProposedAction,
        observation_sha256: str,
    ) -> OutgoingReview:
        if action.kind is not ActionKind.SUBMIT:
            raise ValueError("only submit actions have an outgoing request preview")
        base_review = action.outgoing_review
        if base_review is None:
            body = {
                "fields": [],
                "document_sha256s": [],
                "observation_sha256": observation_sha256,
            }
            base_review = OutgoingReview(
                observation_sha256=observation_sha256,
                payload_sha256=digest(body),
            )
        if base_review.observation_sha256 != observation_sha256:
            raise TransmissionReviewError(
                "outgoing review does not match the previewed page state"
            )
        captured = []
        failures: list[str] = []

        def abort_and_capture(route: Route) -> None:
            try:
                captured.append(
                    fingerprint_request(
                        method=route.request.method,
                        url=route.request.url,
                        headers=route.request.headers,
                        body=route.request.post_data_buffer,
                    )
                )
            except (TransmissionReviewError, ValueError) as exc:
                failures.append(str(exc))
            route.abort("blockedbyclient")

        self._context.route("**/*", abort_and_capture)
        try:
            try:
                self._locator(action).click(timeout=5_000)
                self._page.wait_for_timeout(350)
            except PlaywrightError:
                # Aborting a native form navigation is expected. Validation below
                # still requires one fully captured request.
                pass
        finally:
            self._context.unroute("**/*", handler=abort_and_capture)

        if failures:
            raise TransmissionReviewError(failures[0])
        if len(captured) != 1:
            raise TransmissionReviewError(
                "exact review requires one outgoing request; "
                f"the submit produced {len(captured)}"
            )
        return base_review.bind_requests(tuple(captured))

    def execute(self, action: ProposedAction) -> BrowserReceipt:
        if action.kind is ActionKind.NAVIGATE:
            self._page.goto(action.url or "", wait_until="domcontentloaded")
        elif action.kind is ActionKind.FILL:
            target = self._locator(action)
            if target.evaluate("element => element.tagName === 'SELECT'"):
                target.select_option(action.value or "")
            else:
                target.fill(action.value or "")
        elif action.kind is ActionKind.UPLOAD:
            upload = self._upload_guard.validate(
                action.file_path or Path(),
                action.document_sha256 or "",
            )
            self._locator(action).set_input_files(
                {
                    "name": upload.path.name,
                    "mimeType": (
                        mimetypes.guess_type(upload.path.name)[0]
                        or "application/octet-stream"
                    ),
                    "buffer": upload.content,
                }
            )
        elif (
            action.kind is ActionKind.SUBMIT
            and action.outgoing_review is not None
            and action.outgoing_review.requests
        ):
            self._execute_reviewed_submit(action)
        elif action.kind in {ActionKind.CLICK, ActionKind.SUBMIT}:
            self._locator(action).click()
            wait_state = (
                "networkidle" if action.kind is ActionKind.SUBMIT else "domcontentloaded"
            )
            self._page.wait_for_load_state(wait_state, timeout=10_000)
        else:
            raise ValueError(f"unsupported browser action: {action.kind.value}")
        return self._receipt(action.effect_key or f"local-{action.kind.value}")

    def _execute_reviewed_submit(self, action: ProposedAction) -> None:
        review = action.outgoing_review
        if review is None or len(review.requests) != 1:
            raise TransmissionReviewError(
                "reviewed submit requires exactly one approved request"
            )
        expected = review.requests[0]
        sent = False
        mismatch: list[str] = []
        unexpected_requests: list[str] = []

        def compare_and_dispatch(route: Route) -> None:
            nonlocal sent
            if sent:
                method = route.request.method.upper()
                if method in {"GET", "HEAD", "OPTIONS"} and _origin(
                    route.request.url
                ) == _origin(expected.target):
                    route.continue_()
                else:
                    unexpected_requests.append(method)
                    route.abort("blockedbyclient")
                return
            try:
                actual = fingerprint_request(
                    method=route.request.method,
                    url=route.request.url,
                    headers=route.request.headers,
                    body=route.request.post_data_buffer,
                )
            except (TransmissionReviewError, ValueError) as exc:
                mismatch.append(str(exc))
                route.abort("blockedbyclient")
                return
            if actual.request_sha256 != expected.request_sha256:
                mismatch.append(
                    "outgoing request changed after approval; transmission blocked"
                )
                route.abort("blockedbyclient")
                return
            sent = True
            route.continue_()

        self._context.route("**/*", compare_and_dispatch)
        click_error: PlaywrightError | None = None
        try:
            try:
                self._locator(action).click(timeout=10_000)
                self._page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightError as exc:
                click_error = exc
        finally:
            self._context.unroute("**/*", handler=compare_and_dispatch)

        if mismatch or not sent:
            raise TransmissionBlocked(
                mismatch[0]
                if mismatch
                else "approved outgoing request was not produced; nothing was sent"
            )
        if unexpected_requests:
            raise TransmissionReviewError(
                "an additional unapproved request followed the approved request"
            )
        if click_error is not None:
            raise click_error

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


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}".casefold()
