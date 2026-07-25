from __future__ import annotations

import hashlib
import threading

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

DOWNLOAD_FIXTURE = b"effect-browser-adversarial-download\n" * 32


def create_hard_target_router() -> APIRouter:
    """Controlled hostile pages for proving effect safety with real browsers."""
    router = APIRouter()
    lock = threading.Lock()
    writes: dict[str, int] = {}

    def record(name: str) -> int:
        with lock:
            writes[name] = writes.get(name, 0) + 1
            return writes[name]

    @router.get("/demo-hard/autosave", response_class=HTMLResponse)
    def autosave() -> str:
        return _page(
            "Hidden autosave",
            """
            <h1>Profile editor</h1>
            <label for="display-name">Display name</label>
            <input id="display-name" name="display_name">
            <output id="status">Not saved</output>
            <script>
            const displayName = document.querySelector('#display-name');
            displayName.addEventListener('input', async () => {
              await fetch('/demo-hard/api/write/autosave', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({display_name:
                  document.querySelector('#display-name').value})
              });
              document.querySelector('#status').textContent = 'Saved';
            });
            </script>
            """,
        )

    @router.get("/demo-hard/popup", response_class=HTMLResponse)
    def popup(retry: bool = False) -> str:
        destination = (
            "/demo-hard/popup-detail?retry=true" if retry else ("/demo-hard/popup-detail")
        )
        return _page(
            "Popup handoff",
            f"""
            <h1>Review center</h1>
            <a id="open-review" href="{destination}" target="_blank">
              Open secure review
            </a>
            """,
        )

    @router.get("/demo-hard/popup-detail", response_class=HTMLResponse)
    def popup_detail(retry: bool = False) -> str:
        retry_script = (
            """
            fetch('/demo-hard/api/write/popup-retry', {method: 'POST'});
            setTimeout(() => {
              fetch('/demo-hard/api/write/popup-retry', {method: 'POST'});
            }, 4000);
            """
            if retry
            else ""
        )
        return _page(
            "Popup detail",
            f"""
            <h1>Independent popup review</h1>
            <p data-testid="popup-proof">Popup context adopted</p>
            <script>{retry_script}</script>
            """,
        )

    @router.get("/demo-hard/iframe", response_class=HTMLResponse)
    def iframe() -> str:
        return _page(
            "Nested iframe",
            """
            <h1>Embedded workflow</h1>
            <iframe title="Outer&#10;workflow"
              src="/demo-hard/frame-outer"></iframe>
            """,
        )

    @router.get("/demo-hard/frame-outer", response_class=HTMLResponse)
    def frame_outer() -> str:
        return _page(
            "Outer frame",
            """
            <p>Outer workflow shell</p>
            <iframe data-testid="inner-frame" title="Inner workflow"
              src="/demo-hard/frame-inner"></iframe>
            """,
        )

    @router.get("/demo-hard/frame-inner", response_class=HTMLResponse)
    def frame_inner() -> str:
        return _page(
            "Inner frame",
            """
            <label for="project-code">Nested project code</label>
            <input id="project-code" name="project_code">
            <output id="nested-value"></output>
            <script>
            document.querySelector('#project-code').addEventListener('input', event => {
              document.querySelector('#nested-value').textContent = event.target.value;
            });
            </script>
            """,
        )

    @router.get("/demo-hard/shadow", response_class=HTMLResponse)
    def shadow() -> str:
        return _page(
            "Shadow workflow",
            """
            <h1>Component workflow</h1>
            <div id="component-host"></div>
            <script>
            const root = document.querySelector('#component-host')
              .attachShadow({mode: 'open'});
            root.innerHTML = `
              <label for="shadow-code">Shadow project code</label>
              <input id="shadow-code" name="shadow_code">
              <output id="shadow-value"></output>`;
            root.querySelector('#shadow-code').addEventListener('input', event => {
              root.querySelector('#shadow-value').textContent = event.target.value;
            });
            </script>
            """,
        )

    @router.get("/demo-hard/delayed-preview", response_class=HTMLResponse)
    def delayed_preview() -> str:
        return _page(
            "Delayed preview",
            """
            <form id="delayed-form">
              <label for="reference">Reference</label>
              <input id="reference" name="reference" value="DELAY-BOUND">
              <button type="submit">Submit delayed effect</button>
            </form>
            <script>
            document.querySelector('#delayed-form').addEventListener(
              'submit', event => {
                event.preventDefault();
                setTimeout(() => {
                  fetch('/demo-hard/api/write/delayed-preview', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({reference: 'DELAY-BOUND'})
                  });
                }, 6000);
              });
            </script>
            """,
        )

    @router.get("/demo-hard/header-drift", response_class=HTMLResponse)
    def header_drift() -> str:
        return _page(
            "Header drift",
            """
            <form id="header-form">
              <label for="reference">Reference</label>
              <input id="reference" name="reference" value="HEADER-BOUND">
              <button type="submit">Submit header-bound effect</button>
            </form>
            <script>
            window.effectHeaderDrift = false;
            document.querySelector('#header-form').addEventListener(
              'submit', async event => {
                event.preventDefault();
                const headers = {'Content-Type': 'application/json'};
                if (window.effectHeaderDrift) headers['X-Forward-To'] = 'other-ledger';
                await fetch('/demo-hard/api/write/header-drift', {
                  method: 'POST',
                  headers,
                  body: JSON.stringify({reference: 'HEADER-BOUND'})
                });
              });
            </script>
            """,
        )

    @router.get("/demo-hard/text-upload", response_class=HTMLResponse)
    def text_upload() -> str:
        return _page(
            "Text upload laundering",
            """
            <form id="text-upload-form">
              <label for="document">Approved document</label>
              <input id="document" name="document" type="file">
              <button type="submit">Submit encoded document</button>
            </form>
            <script>
            document.querySelector('#text-upload-form').addEventListener(
              'submit', async event => {
                event.preventDefault();
                const selectedFile =
                  document.querySelector('#document').files[0];
                const payload = new FormData();
                payload.set('resume_text', await selectedFile.text());
                await fetch('/demo-hard/api/write/text-upload', {
                  method: 'POST',
                  body: payload
                });
              });
            </script>
            """,
        )

    @router.get("/demo-hard/download")
    def download() -> Response:
        return Response(
            DOWNLOAD_FIXTURE,
            media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="hard-proof.bin"'},
        )

    @router.get("/demo-hard/download-page", response_class=HTMLResponse)
    def download_page() -> str:
        expected = hashlib.sha256(DOWNLOAD_FIXTURE).hexdigest()
        return _page(
            "Download integrity",
            f"""
            <h1>Evidence package</h1>
            <a id="hard-download" href="/demo-hard/download"
              download="hard-proof.bin">Download evidence package</a>
            <code data-testid="expected-download-sha256">{expected}</code>
            """,
        )

    @router.post("/demo-hard/api/write/{name}")
    async def write_probe(name: str, request: Request) -> dict[str, int]:
        await request.body()
        return {"writes": record(name)}

    @router.get("/demo-hard/api/writes")
    def write_counts() -> dict[str, int]:
        with lock:
            return dict(writes)

    @router.post("/demo-hard/api/reset")
    def reset_counts() -> dict[str, bool]:
        with lock:
            writes.clear()
        return {"reset": True}

    return router


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font:16px system-ui;max-width:760px;margin:48px auto}}
input,button,a{{display:block;margin:12px 0;padding:10px}} iframe{{width:100%;
min-height:280px;border:2px solid #334}}</style></head><body>{body}</body></html>"""
