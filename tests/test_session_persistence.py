import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from effect_browser.browser.playwright import PlaywrightDriver
from effect_browser.domain import ActionKind, ProposedAction


def _browser_sandbox_enabled() -> bool:
    return os.getenv("EFFECT_BROWSER_BROWSER_SANDBOX", "true").casefold() not in {
        "0",
        "false",
        "no",
        "off",
    }


class _SessionHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/login":
            body = b"""
            <!doctype html><title>Signed in</title>
            <span id="result">Session established</span>
            <script>localStorage.setItem('effect-auth-state', 'persisted');</script>
            """
            self.send_response(200)
            self.send_header(
                "set-cookie",
                "effect_auth=session-token; Path=/; HttpOnly; SameSite=Lax",
            )
        elif self.path == "/private":
            authenticated = "effect_auth=session-token" in self.headers.get("cookie", "")
            outcome = "Authenticated after restart" if authenticated else "Signed out"
            body = f"""
            <!doctype html><title>Private work</title>
            <span id="cookie">{outcome}</span>
            <span id="storage"></span>
            <script>
              document.querySelector('#storage').textContent =
                localStorage.getItem('effect-auth-state') === 'persisted'
                  ? 'Local state restored'
                  : 'Local state missing';
            </script>
            """.encode()
            self.send_response(200)
        else:
            body = b"not found"
            self.send_response(404)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        return None


@pytest.mark.e2e
def test_cookie_and_local_storage_survive_a_browser_restart(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SessionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    first = PlaywrightDriver(
        artifacts_directory=tmp_path / "first-artifacts",
        allowed_origins=(origin,),
        sandbox=_browser_sandbox_enabled(),
    )
    try:
        first.execute(
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=f"{origin}/login",
                description="Establish one controlled authenticated session.",
            )
        )
        storage_state = first.export_storage_state()
    finally:
        first.close()

    second = PlaywrightDriver(
        artifacts_directory=tmp_path / "second-artifacts",
        allowed_origins=(origin,),
        sandbox=_browser_sandbox_enabled(),
    )
    try:
        second.restore_storage_state(storage_state, checkpoint_ordinal=1)
        second.execute(
            ProposedAction(
                kind=ActionKind.NAVIGATE,
                url=f"{origin}/private",
                description="Resume private work after the worker restart.",
            )
        )
        snapshot = second.snapshot()
        assert "Authenticated after restart" in snapshot.text_excerpt
        assert "Local state restored" in snapshot.text_excerpt
        assert second.restored_checkpoint_ordinal == 1
    finally:
        second.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
