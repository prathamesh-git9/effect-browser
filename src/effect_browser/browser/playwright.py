from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from playwright.sync_api import (
    Browser,
    Frame,
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
    Locator,
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

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
NAVIGATION_PREFLIGHT_TIMEOUT_MS = 20_000
MAX_NAVIGATION_REDIRECTS = 5


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
        allowed_origins: tuple[str, ...] = (),
    ) -> None:
        artifacts_directory.mkdir(parents=True, exist_ok=True)
        self.artifacts_directory = artifacts_directory
        self._upload_guard = UploadGuard(allowed_upload_roots)
        self._allowed_upload_origins = {_origin(item) for item in allowed_upload_origins}
        self._allowed_origins = {_origin(item) for item in allowed_origins}
        self._navigation_origins: set[str] = set()
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
        self._snapshotter = ScraplingSnapshotter(
            artifacts_directory / "scrapling-elements.db"
        )
        self._closed = False
        self.restored_checkpoint_ordinal = 0

        def deny_unreviewed_writes(route: Route) -> None:
            method = route.request.method.upper()
            if method in {"GET", "HEAD", "OPTIONS"} or _is_browser_security_write(
                method,
                route.request.url,
            ):
                route.continue_()
                return
            parsed = urlsplit(route.request.url)
            self._unreviewed_writes.append(
                f"{method} {parsed.scheme}://{parsed.netloc}{parsed.path}"
            )
            route.abort("blockedbyclient")

        self._deny_unreviewed_writes = deny_unreviewed_writes
        try:
            self._create_context()
        except BaseException:
            self._browser.close()
            self._playwright.stop()
            raise

    def _create_context(self, storage_state: dict[str, object] | None = None) -> None:
        context_options: dict[str, object] = {
            "viewport": {"width": 1440, "height": 900},
            "service_workers": "block",
        }
        if storage_state is not None:
            context_options["storage_state"] = storage_state
        self._context = self._browser.new_context(**context_options)
        self._context.route_web_socket("**/*", lambda socket: socket.close())
        self._unreviewed_writes: list[str] = []
        # This route is deliberately permanent. Exact upload/submit handlers are
        # registered later and use continue_ only after their bound review matches.
        self._context.route("**/*", self._deny_unreviewed_writes)
        self._context.tracing.start(screenshots=True, snapshots=True)
        self._page = self._context.new_page()
        self._rehydration_handler = None
        self._rehydration_origin: str | None = None
        self._rehydration_contains_upload = False
        self._rehydration_violations: list[str] = []
        self._armed_review: OutgoingReview | None = None
        self._preview_handler = None

    def restore_storage_state(
        self,
        storage_state: dict[str, object],
        checkpoint_ordinal: int,
    ) -> None:
        """Install authenticated browser state before any target navigation."""

        if self._closed:
            raise RuntimeError("browser driver is closed")
        if self._page.url != "about:blank":
            raise RuntimeError("storage state must be restored before target traffic")
        if checkpoint_ordinal < 0:
            raise ValueError("checkpoint ordinal must not be negative")
        self._context.tracing.stop()
        self._context.close()
        self._create_context(storage_state)
        self.restored_checkpoint_ordinal = checkpoint_ordinal

    def export_storage_state(self) -> dict[str, object]:
        """Return storage state in memory for encrypted durable checkpointing."""

        if self._closed:
            raise RuntimeError("browser driver is closed")
        return self._context.storage_state(indexed_db=True)

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
        structure = self._dom_structure_evidence()
        state_sha256 = digest(
            {
                "url": url,
                "title": title,
                "body": _normalize(body),
                "controls": controls,
                "frames": frames,
                "structure": structure,
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
        combined_candidates = list(snapshot.candidates)
        contracts = [snapshot.submission_contract]
        for index, shadow_html in enumerate(self._shadow_root_html()):
            shadow = self._snapshotter.build(
                html=shadow_html,
                url=f"{observation.url}#shadow-root-{index}",
                title=observation.title,
                state_sha256=observation.state_sha256,
                save_adaptive=False,
            )
            combined_candidates.extend(shadow.candidates)
            contracts.append(shadow.submission_contract)
        for frame, frame_path in self._candidate_frames():
            try:
                frame_html = frame.content()
            except PlaywrightError:
                continue
            frame_url = (
                frame.url
                if frame.url.startswith(("http://", "https://"))
                else observation.url
            )
            framed = self._snapshotter.build(
                html=frame_html,
                url=frame_url,
                title=observation.title,
                state_sha256=observation.state_sha256,
                save_adaptive=False,
            )
            combined_candidates.extend(
                candidate.model_copy(
                    update={
                        "locator": candidate.locator.model_copy(
                            update={"frame_path": frame_path}
                        )
                    }
                )
                for candidate in framed.candidates
            )
            contracts.append(framed.submission_contract)
        live_contracts = [contract for contract in contracts if contract is not None]
        snapshot = snapshot.model_copy(
            update={
                "candidates": tuple(
                    candidate.model_copy(update={"id": f"C{index:03d}"})
                    for index, candidate in enumerate(combined_candidates, start=1)
                ),
                "submission_contract": (
                    live_contracts[0] if len(live_contracts) == 1 else None
                ),
            }
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
            target = self._raw_locator(candidate.locator)
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
        legacy_request_fingerprint = bool(
            base_review is not None
            and base_review.requests
            and all(
                request.security_headers_sha256 is None
                for request in base_review.requests
            )
        )
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
        include_security_headers = not legacy_request_fingerprint

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
                        headers=route.request.all_headers(),
                        body=route.request.post_data_buffer,
                        include_security_headers=include_security_headers,
                    )
                )
            except (TransmissionReviewError, ValueError) as exc:
                failures.append(str(exc))
            route.abort("blockedbyclient")

        self._context.route("**/*", abort_and_capture)
        # Keep this abort route installed for the lifetime of the browser context.
        # A debounced timer must not become a real write merely because the fixed
        # preview observation window ended.
        self._preview_handler = abort_and_capture
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
            # Intentionally not unregistered. A later exact-dispatch route is added
            # after this route and may bypass it only when the fingerprint matches.
            pass

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
        blocked_before = len(self._unreviewed_writes)
        if action.kind is ActionKind.NAVIGATE:
            planned_origin = _origin(action.url or "")
            allowed_origins = self._top_level_navigation_origins() | {planned_origin}
            handler, violations = self._arm_navigation_guard(allowed_origins)
            navigation_error: PlaywrightError | None = None
            try:
                self._page.goto(action.url or "", wait_until="domcontentloaded")
            except PlaywrightError as exc:
                navigation_error = exc
            finally:
                self._context.unroute("**/*", handler=handler)
            if violations:
                raise TransmissionBlocked(
                    "navigation redirect crossed the configured origin boundary "
                    f"({violations[0]})"
                )
            if navigation_error is not None:
                raise navigation_error
            self._navigation_origins.add(planned_origin)
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
            allowed_origins = {
                _origin(self._page.url),
                *self._allowed_origins,
                *self._navigation_origins,
            }
            redirect_violations: list[str] = []

            def constrain_download(route: Route) -> None:
                if (
                    route.request.method.upper() == "GET"
                    and _origin(route.request.url) not in allowed_origins
                ):
                    redirect_violations.append(_origin(route.request.url))
                    route.abort("blockedbyclient")
                    return
                route.fallback()

            self._context.route("**/*", constrain_download)
            try:
                with self._page.expect_download(timeout=10_000) as pending:
                    self._locator(action).click()
                download = pending.value
            finally:
                self._context.unroute("**/*", handler=constrain_download)
            if redirect_violations:
                raise TransmissionBlocked(
                    "download redirected outside the configured origin boundary"
                )
            failure = download.failure()
            if failure is not None:
                raise TransmissionReviewError(f"download failed: {failure}")
            if not self._origin_allowed(download.url):
                download.cancel()
                raise TransmissionBlocked(
                    "download redirected outside the configured origin boundary"
                )
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", download.suggested_filename)
            destination = (
                self.artifacts_directory
                / "downloads"
                / f"{self.session_id}-{uuid4()}-{safe_name}"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            download.save_as(destination)
            if destination.stat().st_size > MAX_DOWNLOAD_BYTES:
                destination.unlink()
                raise TransmissionReviewError("download exceeds the 25 MiB limit")
            if len(self._unreviewed_writes) != blocked_before:
                raise TransmissionBlocked(
                    "download activation triggered an unreviewed write"
                )
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
            self._click_and_adopt_popup(action)
        else:
            raise ValueError(f"unsupported browser action: {action.kind.value}")
        if action.kind in {
            ActionKind.NAVIGATE,
            ActionKind.FILL,
            ActionKind.CHECK,
            ActionKind.PRESS,
            ActionKind.SCROLL,
            ActionKind.WAIT,
            ActionKind.CLICK,
        }:
            self._page.wait_for_timeout(350)
            blocked_write = len(self._unreviewed_writes) != blocked_before
            if blocked_write and action.kind is not ActionKind.NAVIGATE:
                raise TransmissionBlocked(
                    "a supposedly reversible browser action triggered an "
                    "unreviewed write; the request was blocked"
                )
            # A freshly loaded document can emit analytics or bot-detection POSTs
            # before the user has supplied any page data. Those requests remain
            # aborted by the permanent route, but they must not make a read-only
            # top-level GET unusable. Interactions and rehydration stay fail-closed.
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
                    headers=route.request.all_headers(),
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
                    headers=route.request.all_headers(),
                    body=route.request.post_data_buffer,
                    include_security_headers=(
                        expected.security_headers_sha256 is not None
                    ),
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
                # The authoritative reconciliation lookup does not depend on the
                # page-controlled success UI. Destroy every page that could retain
                # a delayed retry timer while the strict route is still armed.
                action_pages = list(self._context.pages)
                replacement = self._context.new_page()
                for page in action_pages:
                    if not page.is_closed():
                        page.close()
                self._page = replacement
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
        allowed_origins = {
            _origin(self._page.url),
            _origin(spec.url),
            *self._allowed_origins,
            *self._navigation_origins,
        }
        redirect_violations: list[str] = []

        def constrain_reconciliation(route: Route) -> None:
            if (
                route.request.method.upper() in {"GET", "HEAD"}
                and _origin(route.request.url) not in allowed_origins
            ):
                redirect_violations.append(_origin(route.request.url))
                route.abort("blockedbyclient")
                return
            route.fallback()

        self._context.route("**/*", constrain_reconciliation)
        try:
            try:
                self._page.goto(spec.url, wait_until="domcontentloaded")
            except PlaywrightError:
                if not redirect_violations:
                    raise
        finally:
            self._context.unroute("**/*", handler=constrain_reconciliation)
        if redirect_violations:
            return None
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
        if self._closed:
            return
        self._closed = True
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
        scope = self._locator_scope(locator)
        if locator.test_id:
            return scope.get_by_test_id(locator.test_id)
        if locator.label:
            return scope.get_by_label(locator.label, exact=False)
        if locator.selector:
            target = scope.locator(locator.selector)
            if target.count() == 0:
                try:
                    target.wait_for(state="attached", timeout=5_000)
                except PlaywrightTimeoutError:
                    pass
            if target.count() == 0 and locator.adaptive_id and not locator.frame_path:
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
        return scope.get_by_role(locator.role or "", name=locator.name, exact=True)

    def _locator_scope(self, locator: Locator):
        scope = self._page
        for frame_selector in locator.frame_path:
            scope = scope.frame_locator(frame_selector)
        return scope

    def _raw_locator(self, locator: Locator) -> PWLocator:
        scope = self._locator_scope(locator)
        if locator.test_id:
            return scope.get_by_test_id(locator.test_id)
        if locator.label:
            return scope.get_by_label(locator.label, exact=False)
        if locator.selector:
            return scope.locator(locator.selector)
        return scope.get_by_role(locator.role or "", name=locator.name, exact=True)

    def _click_and_adopt_popup(self, action: ProposedAction) -> None:
        allowed_origins = self._top_level_navigation_origins()
        if (
            action.target_interaction == "navigation"
            and action.url is not None
            and _origin(action.url) not in allowed_origins
        ):
            raise TransmissionBlocked(
                "planned navigation crosses the configured origin boundary"
            )
        handler, violations = self._arm_navigation_guard(allowed_origins)
        click_error: PlaywrightError | None = None
        try:
            self._click_and_adopt_popup_unchecked(action)
        except PlaywrightError as exc:
            click_error = exc
        finally:
            self._context.unroute("**/*", handler=handler)
        if violations:
            raise TransmissionBlocked(
                "navigation redirect crossed the configured origin boundary "
                f"({violations[0]})"
            )
        if click_error is not None:
            raise click_error
        if _origin(self._page.url) not in allowed_origins:
            raise TransmissionBlocked(
                "navigation finished outside the configured origin boundary"
            )

    def _click_and_adopt_popup_unchecked(self, action: ProposedAction) -> None:
        before = set(self._context.pages)
        self._locator(action).click(no_wait_after=True)
        deadline = time.monotonic() + 0.75
        opened: list[Page] = []
        while time.monotonic() < deadline:
            opened = [page for page in self._context.pages if page not in before]
            if opened:
                break
            self._page.wait_for_timeout(50)
        if len(opened) > 1:
            for page in opened:
                page.close()
            raise TransmissionBlocked(
                "one click opened multiple unreviewed browser pages"
            )
        if opened:
            popup = opened[0]
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=10_000)
            except PlaywrightTimeoutError:
                pass
            if not self._origin_allowed(popup.url):
                popup.close()
                raise TransmissionBlocked("popup crossed the configured origin boundary")
            old_page = self._page
            self._page = popup
            if old_page is not popup and not old_page.is_closed():
                old_page.close()
            return
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except PlaywrightTimeoutError:
            pass

    def _top_level_navigation_origins(self) -> set[str]:
        return {
            _origin(self._page.url),
            *self._allowed_origins,
            *self._navigation_origins,
        }

    def _arm_navigation_guard(
        self,
        allowed_origins: set[str],
    ) -> tuple[object, list[str]]:
        violations: list[str] = []
        followed_redirects = 0

        def constrain_navigation(route: Route) -> None:
            nonlocal followed_redirects
            request = route.request
            navigation_document = (
                request.is_navigation_request() or request.resource_type == "document"
            )
            origin = _origin(request.url)
            if navigation_document and origin not in allowed_origins:
                violations.append(origin)
                route.abort("blockedbyclient")
                return
            if navigation_document and urlsplit(request.url).scheme not in {
                "http",
                "https",
            }:
                # Playwright's request preflight cannot fetch file:// documents.
                # Their origin has already been checked, and they cannot issue an
                # HTTP Location response; script-driven cross-origin navigation is
                # routed again and remains fenced below.
                route.fallback()
                return
            if navigation_document and request.method.upper() in {"GET", "HEAD"}:
                try:
                    response = route.fetch(
                        max_redirects=0,
                        timeout=NAVIGATION_PREFLIGHT_TIMEOUT_MS,
                    )
                except PlaywrightError:
                    violations.append("navigation-response-unavailable")
                    route.abort("blockedbyclient")
                    return
                location = response.headers.get("location")
                if 300 <= response.status < 400 and location:
                    redirect_url = urljoin(request.url, location)
                    redirect_origin = _origin(redirect_url)
                    followed_redirects += 1
                    if (
                        redirect_origin not in allowed_origins
                        or followed_redirects > MAX_NAVIGATION_REDIRECTS
                    ):
                        violations.append(
                            redirect_origin
                            if followed_redirects <= MAX_NAVIGATION_REDIRECTS
                            else "redirect-budget-exhausted"
                        )
                        route.abort("blockedbyclient")
                        return
                    # Playwright does not re-route an HTTP Location hop after
                    # route.fulfill(response=...). Replace the reviewed redirect
                    # with a same-document transition so the destination is routed
                    # and checked before its request is transmitted.
                    route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=(
                            "<!doctype html><meta charset=utf-8>"
                            "<script>location.replace("
                            f"{json.dumps(redirect_url, ensure_ascii=True)}"
                            ")</script>"
                        ),
                    )
                    return
                route.fulfill(response=response)
                return
            route.fallback()

        self._context.route("**/*", constrain_navigation)
        return constrain_navigation, violations

    def _origin_allowed(self, url: str) -> bool:
        target = _origin(url)
        current = _origin(self._page.url)
        return (
            target == current
            or target in self._allowed_origins
            or target in self._navigation_origins
        )

    def _dom_structure_evidence(self) -> list[dict[str, str]]:
        """Hash actionable identity, not only visible text and current values."""
        return self._page.locator(
            "input, textarea, select, button, a[href], [role=button], "
            "[role=link], [role=option], [contenteditable=true]"
        ).evaluate_all(
            """
            elements => elements.map(element => ({
              tag: element.tagName.toLowerCase(),
              id: element.id || '',
              name: element.getAttribute('name') || '',
              type: element.getAttribute('type') || '',
              role: element.getAttribute('role') || '',
              testid: element.getAttribute('data-testid') || '',
              aria: element.getAttribute('aria-label') || ''
            }))
            """
        )

    def _shadow_root_html(self) -> list[str]:
        return self._page.locator("html").evaluate(
            """
            root => {
              const roots = [];
              const visit = node => {
                for (const element of node.querySelectorAll('*')) {
                  if (!element.shadowRoot) continue;
                  roots.push(element.shadowRoot.innerHTML);
                  visit(element.shadowRoot);
                }
              };
              visit(root);
              return roots;
            }
            """
        )

    def _candidate_frames(self) -> list[tuple[Frame, tuple[str, ...]]]:
        candidates = []
        for frame in self._page.frames[1:]:
            if not self._frame_allowed(frame):
                continue
            path = self._frame_path(frame)
            if path is not None:
                candidates.append((frame, path))
        return candidates

    def _frame_allowed(self, frame: Frame) -> bool:
        if frame.url.startswith(("about:blank", "about:srcdoc")):
            parent = frame.parent_frame
            return parent is not None and (
                parent == self._page.main_frame or self._frame_allowed(parent)
            )
        target = _origin(frame.url)
        return (
            target == _origin(self._page.url)
            or target in self._allowed_origins
            or target in self._navigation_origins
        )

    def _frame_path(self, frame: Frame) -> tuple[str, ...] | None:
        parent = frame.parent_frame
        if parent is None:
            return ()
        handle = frame.frame_element()
        selector = None
        for attribute in ("data-testid", "id", "title", "name", "src"):
            value = handle.get_attribute(attribute)
            if not value:
                continue
            try:
                escaped = handle.evaluate(
                    "(element, name) => CSS.escape(element.getAttribute(name))",
                    attribute,
                )
                candidate = f"iframe[{attribute}={escaped}]"
                if parent.locator(candidate).count() == 1:
                    selector = candidate
                    break
            except PlaywrightError:
                continue
        if selector is None:
            return None
        if parent == self._page.main_frame:
            return (selector,)
        parent_path = self._frame_path(parent)
        if parent_path is None:
            return None
        return (*parent_path, selector)

    def _stabilize(self) -> None:
        try:
            self._page.wait_for_load_state("networkidle", timeout=3_000)
        except PlaywrightTimeoutError:
            # Long-polling pages may never become idle. The state hash still protects
            # execution: later drift invalidates the action instead of weakening safety.
            pass

    def _frame_evidence(self) -> list[dict[str, object]]:
        evidence = []
        for frame in self._page.frames[1:]:
            if not self._frame_allowed(frame):
                continue
            try:
                body = frame.locator("body").inner_text(timeout=1_000)
                controls = frame.locator("input, textarea, select").evaluate_all(
                    """
                    elements => elements.map(element => ({
                      id: element.id || '',
                      name: element.getAttribute('name') || '',
                      type: element.getAttribute('type') || '',
                      value: element.type === 'file'
                        ? Boolean(element.value)
                        : (
                          ['checkbox', 'radio'].includes(element.type)
                            ? Boolean(element.checked)
                            : element.value
                        )
                    }))
                    """
                )
                structure = frame.locator(
                    "input, textarea, select, button, a[href], [role=button], "
                    "[role=link], [role=option], [contenteditable=true]"
                ).evaluate_all(
                    """
                    elements => elements.map(element => ({
                      tag: element.tagName.toLowerCase(),
                      id: element.id || '',
                      name: element.getAttribute('name') || '',
                      type: element.getAttribute('type') || '',
                      role: element.getAttribute('role') || '',
                      testid: element.getAttribute('data-testid') || ''
                    }))
                    """
                )
            except PlaywrightError:
                body = ""
                controls = []
                structure = []
            evidence.append(
                {
                    "url": frame.url,
                    "body": _normalize(body)[:2_000],
                    "controls": controls,
                    "structure": structure,
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
