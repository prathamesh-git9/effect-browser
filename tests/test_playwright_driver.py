import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from effect_browser.browser import playwright as playwright_module
from effect_browser.browser.playwright import PlaywrightDriver
from effect_browser.domain import ActionKind, ProposedAction, StepChoice
from effect_browser.policy import ActionPolicy
from effect_browser.providers.reactive import bind_choice
from effect_browser.transmission import TransmissionBlocked


def _browser_sandbox_enabled() -> bool:
    return os.getenv("EFFECT_BROWSER_BROWSER_SANDBOX", "true").casefold() not in {
        "0",
        "false",
        "no",
        "off",
    }


def test_failed_browser_launch_releases_playwright_runtime(
    monkeypatch, tmp_path: Path
) -> None:
    class FailingChromium:
        @staticmethod
        def launch(**_options):
            raise RuntimeError("synthetic launch failure")

    class FakePlaywright:
        chromium = FailingChromium()
        stopped = False

        def stop(self) -> None:
            self.stopped = True

    runtime = FakePlaywright()

    class FakeManager:
        @staticmethod
        def start():
            return runtime

    monkeypatch.setattr(playwright_module, "sync_playwright", FakeManager)

    with pytest.raises(RuntimeError, match="synthetic launch failure"):
        PlaywrightDriver(artifacts_directory=tmp_path / "artifacts")

    assert runtime.stopped is True


def test_navigation_redirect_cannot_cross_the_bound_origin(tmp_path: Path) -> None:
    class TargetHandler(BaseHTTPRequestHandler):
        hits = 0

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            type(self).hits += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"unexpected cross-origin target")

        def log_message(self, _format: str, *_args) -> None:
            return None

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_origin = f"http://127.0.0.1:{target.server_port}"

    class SourceHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            if self.path == "/start":
                body = b'<a id="next" href="/redirect">Continue</a>'
                self.send_response(200)
                self.send_header("content-type", "text/html")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("location", f"{target_origin}/final")
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, _format: str, *_args) -> None:
            return None

    source = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
    source_origin = f"http://127.0.0.1:{source.server_port}"
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (target, source)
    ]
    for thread in threads:
        thread.start()

    driver = PlaywrightDriver(
        headless=True,
        sandbox=_browser_sandbox_enabled(),
        artifacts_directory=tmp_path / "artifacts",
        allowed_origins=(source_origin,),
    )
    try:
        driver.execute(
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=f"{source_origin}/start",
                description="Open the source fixture.",
            )
        )
        snapshot = driver.snapshot()
        link = next(
            candidate for candidate in snapshot.candidates if candidate.name == "Continue"
        )
        action = bind_choice(
            StepChoice(
                kind=ActionKind.CLICK,
                candidate_id=link.id,
                description="Follow the observed source link.",
            ),
            snapshot,
            effect_reference="EB-NAVIGATION",
        )
        assert action.url == f"{source_origin}/redirect"
        assert ActionPolicy((source_origin,)).evaluate(action, snapshot.url).allowed

        with pytest.raises(TransmissionBlocked, match="origin boundary"):
            driver.execute(action)

        assert TargetHandler.hits == 0
    finally:
        driver.close()
        for server in (source, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


def test_allowed_redirect_is_routed_once_and_rendered(tmp_path: Path) -> None:
    class TargetHandler(BaseHTTPRequestHandler):
        hits = 0

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            type(self).hits += 1
            body = b"<!doctype html><title>Allowed</title><p>Allowed target rendered</p>"
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args) -> None:
            return None

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_origin = f"http://127.0.0.1:{target.server_port}"

    class SourceHandler(BaseHTTPRequestHandler):
        hits = 0

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            type(self).hits += 1
            self.send_response(302)
            self.send_header("location", f"{target_origin}/final")
            self.end_headers()

        def log_message(self, _format: str, *_args) -> None:
            return None

    source = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
    source_origin = f"http://127.0.0.1:{source.server_port}"
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (target, source)
    ]
    for thread in threads:
        thread.start()
    driver = PlaywrightDriver(
        headless=True,
        sandbox=_browser_sandbox_enabled(),
        artifacts_directory=tmp_path / "artifacts",
        allowed_origins=(source_origin, target_origin),
    )
    try:
        driver.execute(
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=f"{source_origin}/start",
                description="Follow one configured redirect.",
            )
        )
        snapshot = driver.snapshot()

        assert snapshot.url == f"{target_origin}/final"
        assert "Allowed target rendered" in snapshot.text_excerpt
        assert SourceHandler.hits == 1
        assert TargetHandler.hits == 1
    finally:
        driver.close()
        for server in (source, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


def test_navigation_keeps_page_background_writes_blocked_without_failing(
    tmp_path: Path,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        post_hits = 0

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            body = b"""
            <!doctype html><title>Telemetry page</title>
            <p>Rendered evidence remains available.</p>
            <script>fetch('/telemetry', {method: 'POST', body: 'page-load'});</script>
            """
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            type(self).post_hits += 1
            self.send_response(204)
            self.end_headers()

        def log_message(self, _format: str, *_args) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    driver = PlaywrightDriver(
        headless=True,
        sandbox=_browser_sandbox_enabled(),
        artifacts_directory=tmp_path / "artifacts",
        allowed_origins=(origin,),
    )
    try:
        driver.execute(
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=f"{origin}/page",
                description="Read a page that emits blocked background telemetry.",
            )
        )
        snapshot = driver.snapshot()

        assert "Rendered evidence remains available" in snapshot.text_excerpt
        assert Handler.post_hits == 0
        assert driver._unreviewed_writes == [f"POST {origin}/telemetry"]
    finally:
        driver.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_privacy_rejection_closes_wall_while_telemetry_stays_blocked(
    tmp_path: Path,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        post_hits = 0

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            body = b"""
            <!doctype html><title>Cookie wall</title>
            <div id="wall"><p>We use optional cookies.</p>
              <button id="reject" type="button">Reject all</button>
            </div>
            <p id="content">Booking form ready</p>
            <script>
              document.querySelector('#reject').onclick = () => {
                document.querySelector('#wall').remove();
                fetch('/consent-telemetry', {method: 'POST', body: 'rejected'});
              };
            </script>
            """
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            type(self).post_hits += 1
            self.send_response(204)
            self.end_headers()

        def log_message(self, _format: str, *_args) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    driver = PlaywrightDriver(
        headless=True,
        sandbox=_browser_sandbox_enabled(),
        artifacts_directory=tmp_path / "artifacts",
        allowed_origins=(origin,),
    )
    try:
        driver.execute(
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=f"{origin}/page",
                description="Open the controlled cookie wall.",
            )
        )
        candidate = next(
            item for item in driver.snapshot().candidates if item.name == "Reject all"
        )
        with pytest.raises(TransmissionBlocked, match="unreviewed write"):
            driver.execute(
                ProposedAction(
                    kind=ActionKind.CLICK,
                    locator=candidate.locator,
                    description="Reject optional cookies.",
                    target_interaction=candidate.interaction,
                    target_name=candidate.name,
                )
            )

        assert driver._page.locator("#wall").count() == 0
        assert Handler.post_hits == 0
        assert driver._unreviewed_writes == [f"POST {origin}/consent-telemetry"]
    finally:
        driver.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
