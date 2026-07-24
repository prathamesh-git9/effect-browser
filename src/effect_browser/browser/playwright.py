from __future__ import annotations

import hashlib
import mimetypes
import re
import time
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
        allowed_upload_origins: tuple[str, ...] = (),
    ) -> None:
        artifacts_directory.mkdir(parents=True, exist_ok=True)
        self.artifacts_directory = artifacts_directory
        self._upload_guard = UploadGuard(allowed_upload_roots)
        self._allowed_upload_origins = {_origin(item) for item in allowed_upload_origins}
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
        self._rehydration_handler = None
        self._rehydration_origin: str | None = None
        self._rehydration_contains_upload = False
        self._rehydration_violations: list[str] = []
        self._armed_review: OutgoingReview | None = None

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
                        {"file_selected": bool(value)}
                        if input_type == "file"
                        else (
                            {"checked": item.is_checked()}
                            if input_type in {"checkbox", "radio"}
                            else value
                        )
                    ),
                }
            )
        frames = self._frame_evidence()
        state_sha256 = digest(
            {
                "url": url,
                "title": title,
                "body": _normalize(body),
                "controls": controls,
                "frames": frames,
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
        frame_text = " ".join(
            f"Embedded frame {frame['url']} {frame['body']}"
            for frame in self._frame_evidence()
        )
        if frame_text:
            snapshot = snapshot.model_copy(
                update={
                    "text_excerpt": (
                        f"{snapshot.text_excerpt} {frame_text}".strip()[:8_000]
                    )
                }
            )
        visible = []
        for candidate in snapshot.candidates:
            target = self._page.locator(candidate.locator.selector or "")
            if target.count() == 1 and target.is_visible():
                filled = False
                current_value = None
                if candidate.interaction in {"input", "upload"}:
                    try:
                        if candidate.input_type in {"checkbox", "radio"}:
                            filled = target.is_checked()
                            current_value = str(filled).lower()
                        else:
                            live_value = target.input_value()
                            if candidate.role == "combobox" and not live_value:
                                live_value = (
                                    target.evaluate(
                                        """
                                        element => {
                                          const control = element.closest(
                                            '[class*="control"]'
                                          );
                                          const selected = control?.querySelector(
                                            '[class*="single-value"],'
                                            + '[data-selected-value]'
                                          );
                                          return selected?.textContent?.trim() || '';
                                        }
                                        """
                                    )
                                    or ""
                                )
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
        # Parse beyond the provider-facing cap because dynamic widgets often retain
        # hundreds of zero-size role=option nodes. Capping before live visibility
        # filtering can hide every form control that follows such a widget.
        return snapshot.model_copy(update={"candidates": tuple(visible[:120])})

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
            rebound_body = {
                "fields": [field.model_dump(mode="json") for field in base_review.fields],
                "document_sha256s": list(base_review.document_sha256s),
                "observation_sha256": observation_sha256,
            }
            base_review = OutgoingReview(
                fields=base_review.fields,
                document_sha256s=base_review.document_sha256s,
                observation_sha256=observation_sha256,
                payload_sha256=digest(rebound_body),
            )
        self.assert_rehydration_safe()
        captured = []
        failures: list[str] = []

        def abort_and_capture(route: Route) -> None:
            method = route.request.method.upper()
            if method in {"GET", "HEAD", "OPTIONS"} or _is_browser_security_write(
                method,
                route.request.url,
            ):
                route.continue_()
                return
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
        self._remove_rehydration_guard()
        try:
            try:
                self._locator(action).click(timeout=5_000)
                self._page.wait_for_timeout(3_000)
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
        post_preview = self.observe()
        rebound_observation_sha256 = (
            observation_sha256
            if post_preview.url.startswith("chrome-error://")
            else post_preview.state_sha256
        )
        rebound_body = {
            "fields": [field.model_dump(mode="json") for field in base_review.fields],
            "document_sha256s": list(base_review.document_sha256s),
            "observation_sha256": rebound_observation_sha256,
        }
        rebound = OutgoingReview(
            fields=base_review.fields,
            document_sha256s=base_review.document_sha256s,
            observation_sha256=rebound_observation_sha256,
            payload_sha256=digest(rebound_body),
        )
        return rebound.bind_requests(tuple(captured))

    def arm_reviewed_submit(
        self,
        review: OutgoingReview,
        allowed_origin_url: str,
    ) -> None:
        if len(review.requests) != 1:
            raise TransmissionReviewError(
                "submit rehydration requires one approved outgoing request"
            )
        if self._rehydration_handler is not None:
            raise TransmissionReviewError("submit rehydration is already armed")
        self._install_upload_guard(allowed_origin_url)
        self._armed_review = review

    def _install_upload_guard(self, allowed_origin_url: str) -> None:
        self._rehydration_origin = _origin(allowed_origin_url)
        self._rehydration_contains_upload = False
        self._rehydration_violations = []

        def guard(route: Route) -> None:
            method = route.request.method.upper()
            parsed = urlsplit(route.request.url)
            safe_before_upload = method in {"GET", "HEAD", "OPTIONS"}
            safe_after_upload = (
                safe_before_upload
                and _origin(route.request.url) == self._rehydration_origin
                and not parsed.query
            )
            if (
                safe_after_upload
                if self._rehydration_contains_upload
                else safe_before_upload
            ):
                route.continue_()
                return
            self._rehydration_violations.append(
                f"{method} {parsed.scheme}://{parsed.netloc}{parsed.path}"
            )
            route.abort("blockedbyclient")

        self._rehydration_handler = guard
        self._context.route("**/*", guard)

    def assert_rehydration_safe(self) -> None:
        if self._rehydration_violations:
            violations = ", ".join(self._rehydration_violations[:3])
            raise TransmissionBlocked(
                "approved file selection triggered an unapproved request; "
                f"the request was blocked ({violations})"
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
        elif action.kind is ActionKind.CHECK:
            self._locator(action).set_checked(bool(action.checked))
        elif action.kind is ActionKind.PRESS:
            self._locator(action).press(action.key or "")
        elif action.kind is ActionKind.SCROLL:
            self._page.mouse.wheel(0, action.scroll_y or 0)
            self._page.wait_for_timeout(200)
        elif action.kind is ActionKind.WAIT:
            self._page.wait_for_timeout(action.wait_ms or 100)
        elif action.kind is ActionKind.DOWNLOAD:
            with self._page.expect_download(timeout=10_000) as pending:
                self._locator(action).click()
            download = pending.value
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", download.suggested_filename)
            destination = (
                self.artifacts_directory
                / "downloads"
                / f"{self.session_id}-{uuid4()}-{safe_name}"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            download.save_as(destination)
            return BrowserReceipt(
                external_id=safe_name,
                url=self._page.url,
                evidence_sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
                captured_at=utc_now(),
            )
        elif action.kind is ActionKind.UPLOAD:
            upload = self._upload_guard.validate(
                action.file_path or Path(),
                action.document_sha256 or "",
            )
            if self._rehydration_handler is None:
                return self._execute_guarded_upload(action, upload)
            self._rehydration_contains_upload = True
            self._set_input_file(action, upload)
            # Rehydration is not a new upload authorization. A file input can be
            # restored only when doing so produces no write request.
            self._page.wait_for_timeout(350)
            self.assert_rehydration_safe()
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

    def _set_input_file(self, action: ProposedAction, upload) -> None:
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

    def _execute_guarded_upload(self, action: ProposedAction, upload) -> BrowserReceipt:
        """Permit at most one hash-bound multipart upload to an explicit origin."""
        sent_request = None
        sent_url: str | None = None
        response_statuses: list[int] = []
        violations: list[str] = []

        def capture_response(response) -> None:
            if (
                sent_url is not None
                and response.request.method.upper() == "POST"
                and response.request.url == sent_url
            ):
                response_statuses.append(response.status)

        def verify_and_dispatch(route: Route) -> None:
            nonlocal sent_request, sent_url
            method = route.request.method.upper()
            if method in {"GET", "HEAD", "OPTIONS"}:
                route.continue_()
                return
            target_origin = _origin(route.request.url)
            if sent_request is not None:
                violations.append(f"additional {method} request")
                route.abort("blockedbyclient")
                return
            if method != "POST" or target_origin not in self._allowed_upload_origins:
                violations.append(f"{method} {target_origin}")
                route.abort("blockedbyclient")
                return
            try:
                reviewed = fingerprint_request(
                    method=route.request.method,
                    url=route.request.url,
                    headers=route.request.headers,
                    body=route.request.post_data_buffer,
                )
            except (TransmissionReviewError, ValueError) as exc:
                violations.append(str(exc))
                route.abort("blockedbyclient")
                return
            expected_document = action.document_sha256 or ""
            if (
                reviewed.content_type != "multipart/form-data"
                or reviewed.document_sha256s != (expected_document,)
            ):
                violations.append(
                    "write request was not one multipart transfer of the "
                    "approved document hash"
                )
                route.abort("blockedbyclient")
                return
            sent_request = reviewed
            sent_url = route.request.url
            route.continue_()

        self._page.on("response", capture_response)
        self._context.route("**/*", verify_and_dispatch)
        try:
            self._set_input_file(action, upload)
            self._page.wait_for_timeout(1_500)
        finally:
            self._context.unroute("**/*", handler=verify_and_dispatch)
            self._page.remove_listener("response", capture_response)

        if sent_request is None:
            if violations:
                raise TransmissionBlocked(
                    "file selection produced an unauthorized write; it was blocked "
                    f"({violations[0]})"
                )
            return self._receipt("local-upload")
        if violations:
            raise TransmissionReviewError(
                "an additional unapproved write followed the document upload"
            )
        if not any(200 <= status < 300 for status in response_statuses):
            raise TransmissionReviewError(
                "document upload was transmitted but no 2xx response was observed"
            )
        return BrowserReceipt(
            external_id=f"upload-{sent_request.request_sha256[:16]}",
            url=sent_request.target,
            evidence_sha256=sent_request.request_sha256,
            captured_at=utc_now(),
        )

    def _execute_reviewed_submit(self, action: ProposedAction) -> None:
        review = action.outgoing_review
        if review is None or len(review.requests) != 1:
            raise TransmissionReviewError(
                "reviewed submit requires exactly one approved request"
            )
        expected = review.requests[0]
        if self._armed_review is not None:
            if self._armed_review.payload_sha256 != review.payload_sha256:
                raise TransmissionBlocked(
                    "the dispatch review differs from the rehydration guard"
                )
            self.assert_rehydration_safe()
        sent = False
        mismatch: list[str] = []
        unexpected_requests: list[str] = []

        def compare_and_dispatch(route: Route) -> None:
            nonlocal sent
            method = route.request.method.upper()
            if method in {"GET", "HEAD", "OPTIONS"} or _is_browser_security_write(
                method,
                route.request.url,
            ):
                route.continue_()
                return
            if sent:
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

        # Register the stricter dispatch handler before removing the rehydration
        # guard. Playwright invokes the most recently registered handler first, so
        # there is no unguarded event-loop turn between file replay and submission.
        self._context.route("**/*", compare_and_dispatch)
        self._remove_rehydration_guard()
        click_error: PlaywrightError | None = None
        try:
            try:
                self._locator(action).click(timeout=10_000, no_wait_after=True)
            except PlaywrightError as exc:
                click_error = exc
            deadline = time.monotonic() + 20
            while (
                not sent
                and not mismatch
                and not unexpected_requests
                and time.monotonic() < deadline
            ):
                self._page.wait_for_timeout(100)
            if sent:
                # Keep the route guard armed while the accepted request settles and
                # the confirmation navigation renders. This also catches any
                # additional unapproved writes after the exact commit.
                self._page.wait_for_timeout(2_000)
                try:
                    self._page.wait_for_load_state("networkidle", timeout=10_000)
                except PlaywrightTimeoutError:
                    pass
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

    def _remove_rehydration_guard(self) -> None:
        handler = self._rehydration_handler
        if handler is not None:
            self._context.unroute("**/*", handler=handler)
        self._rehydration_handler = None
        self._rehydration_origin = None
        self._rehydration_contains_upload = False
        self._armed_review = None

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

    def _frame_evidence(self) -> list[dict[str, str]]:
        evidence = []
        for frame in self._page.frames[1:]:
            try:
                body = frame.locator("body").inner_text(timeout=1_000)
            except PlaywrightError:
                body = ""
            evidence.append(
                {
                    "url": frame.url,
                    "body": _normalize(body)[:2_000],
                }
            )
        return evidence

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


def _is_browser_security_write(method: str, url: str) -> bool:
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}".casefold()
    return (
        method == "POST"
        and origin in {"https://www.google.com", "https://www.recaptcha.net"}
        and parsed.path.startswith("/recaptcha/")
    )


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}".casefold()
