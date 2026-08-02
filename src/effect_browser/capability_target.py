from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response


def create_capability_router() -> APIRouter:
    """Synthetic dynamic page for proving generic browser primitives."""
    router = APIRouter()

    @router.get("/demo-capabilities", response_class=HTMLResponse)
    def capability_lab() -> HTMLResponse:
        return HTMLResponse(
            """
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Dynamic capability lab</title></head>
<body>
  <h1>Dynamic capability lab</h1>
  <p>This page hydrates and can replace its controls after observation.</p>
  <main id="app" aria-live="polite"><p>Hydrating controls...</p></main>
  <script>
    const app = document.querySelector("#app");
    function controls(drifted = false) {
      const suffix = drifted ? "-v2" : "";
      app.innerHTML = `
        <section class="${drifted ? "replacement-shell" : "initial-shell"}">
          <label for="region${suffix}">Region</label>
          <select id="region${suffix}" name="region">
            <option value="">Choose</option>
            <option value="ie">Ireland</option>
            <option value="pt">Portugal</option>
          </select>
          <label for="insurance${suffix}">Include insurance</label>
          <input id="insurance${suffix}" name="insurance" type="checkbox">
          <fieldset><legend>Delivery speed</legend>
            <label for="standard${suffix}">Standard delivery</label>
            <input id="standard${suffix}" name="speed" value="standard" type="radio">
            <label for="express${suffix}">Express delivery</label>
            <input id="express${suffix}" name="speed" value="express" type="radio">
          </fieldset>
          <label for="notes${suffix}">Notes</label>
          <textarea id="notes${suffix}" name="notes"></textarea>
          <label for="timezone${suffix}">Timezone</label>
          <div class="demo-select-control">
            <div class="demo-single-value" hidden></div>
            <input id="timezone${suffix}" name="timezone" role="combobox"
                   aria-expanded="false" aria-autocomplete="list"
                   aria-controls="timezone-options${suffix}" autocomplete="off">
          </div>
          <div id="timezone-options${suffix}"></div>
          <button id="reveal${suffix}" type="button">Reveal details</button>
          <button id="drift${suffix}" type="button">Rebuild controls</button>
          <a id="manual${suffix}" href="/demo-capabilities/manual.txt"
             download="capability-manual.txt">Download manual</a>
          <div style="height: 1300px"></div>
          <p id="bottom${suffix}">Bottom marker</p>
          <output id="status${suffix}">Ready</output>
        </section>`;
      document.querySelector(`#reveal${suffix}`).addEventListener("click", () => {
        document.querySelector(`#status${suffix}`).textContent = "Details revealed";
      });
      document.querySelector(`#drift${suffix}`).addEventListener("click", () => {
        controls(true);
      });
      // React-like ARIA combobox: options enter the DOM only after opening.
      const combo = document.querySelector(`#timezone${suffix}`);
      const listbox = document.querySelector(`#timezone-options${suffix}`);
      const selected = combo.parentElement.querySelector(".demo-single-value");
      combo.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowDown") return;
        combo.setAttribute("aria-expanded", "true");
        listbox.setAttribute("role", "listbox");
        listbox.innerHTML = `
          <div id="tz-dublin${suffix}" role="option">Dublin</div>
          <div id="tz-lisbon${suffix}" role="option">Lisbon</div>`;
        listbox.querySelectorAll("[role=option]").forEach((option) => {
          option.addEventListener("click", () => {
            selected.textContent = option.textContent;
            selected.hidden = false;
            combo.value = "";
            combo.setAttribute("aria-expanded", "false");
            listbox.removeAttribute("role");
            listbox.innerHTML = "";
          });
        });
      });
    }
    setTimeout(() => controls(false), 175);
  </script>
</body>
</html>
            """.strip()
        )

    @router.get("/demo-capabilities/manual.txt")
    def capability_manual() -> Response:
        content = b"Effect Browser generic capability harness.\n"
        return Response(
            content=content,
            media_type="text/plain",
            headers={
                "Content-Disposition": ('attachment; filename="capability-manual.txt"')
            },
        )

    return router
