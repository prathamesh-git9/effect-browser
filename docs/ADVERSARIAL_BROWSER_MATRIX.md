# Adversarial browser matrix

Status: 10/10 passing on 2026-07-25 with a real Chromium-family browser and local
HTTP targets.

This is not a benchmark of whether a model can guess the right click. It tests whether
the executor preserves effect boundaries when the page is actively inconvenient or
deceptive. Every write scenario has a server-side counter. A green UI is never the
oracle.

Claude Fable 5, high effort, was run with skipped permission prompts as an independent
read-only hostile reviewer. Its output was treated as hypotheses, not truth. The
resulting gaps were reproduced or narrowed before changes were accepted.

## The ten scenarios

| # | Scenario | Required result | Independent oracle |
|---:|---|---|---|
| 1 | Identity-only SPA replacement | A changed element identity changes the observation hash even when text and values are unchanged. | Two captured state hashes differ. |
| 2 | Nested same-origin iframes | Scrapling exposes the inner control with an exact two-frame locator and the driver fills that control. | Live value read from the inner frame. |
| 3 | Open shadow DOM | Observation and candidate extraction traverse the same open shadow root. | Live shadow input value after candidate-bound fill. |
| 4 | Popup handoff | A single candidate-bound popup becomes the active page and the stale opener is closed. | Active title plus one remaining context page. |
| 5 | Popup write and delayed retry | Immediate and delayed POST attempts from a popup are both blocked. | Server write count remains zero after 4.5 seconds. |
| 6 | “Reversible” fill with hidden autosave | Filling an input cannot secretly POST data. | `TransmissionBlocked` plus zero server writes. |
| 7 | Submit delayed beyond preview window | A timer firing after the preview timeout stays intercepted. | Preview reports zero captured requests and the server still records zero. |
| 8 | Security-header TOCTOU | Adding `X-Forward-To` after approval changes the request fingerprint and blocks dispatch. | Zero server writes. |
| 9 | File bytes laundered into a text field | An approved file hash cannot disappear from the multipart file-part set. | Preview fails closed and the server records zero. |
| 10 | Download integrity | The receipt hash is computed from the saved bytes, not page text or filename. | Receipt SHA-256 equals the server fixture SHA-256. |

Run only this matrix:

```powershell
python -m pytest tests/test_adversarial_matrix_e2e.py -q
```

The final clean campaign run completed in 73.7 seconds:

```text
.......... [100%]
10 passed
```

## Changes forced by the matrix

- Actionable DOM identity is part of `Observation.state_sha256`.
- The permanent context route blocks unreviewed writes from inputs, clicks, timers,
  popups, and rehydration. Exact upload and submit routes can bypass it only after
  matching their bound fingerprint.
- Same-origin nested iframe controls have frame-qualified locators. Foreign-origin
  frame text is excluded unless its origin is explicitly configured.
- Open shadow roots get separate Scrapling passes, with stable selectors that
  Playwright can resolve through the root.
- Popup adoption is explicit; multiple or off-origin popups fail closed.
- Preview interception survives the fixed observation window.
- Pages capable of retrying a reviewed commit are destroyed while the exact route
  remains armed; reconciliation runs from a fresh page.
- Security-sensitive routing, authorization, and browser-managed cookie headers are
  fingerprint-bound through Playwright's complete request-header view.
- A reviewed document set cannot become an empty or different captured file-part set.
- Reconciliation is origin-checked in both normal execution and recovery.
- Downloads reject failed/off-origin transfers and stored artifacts over 25 MiB.
- The test server no longer selects Chromium-blocklisted loopback ports.

## What remains unsolved

Two Fable findings are real but not honestly solved by this matrix:

1. A same-origin page still proposes its reconciliation contract. Same-origin origin
   checks, a receipt test ID, and a separate durable lookup defeat ordinary fake
   success, but they do not establish cryptographic independence from a malicious
   first-party server. Production needs configured receipt adapters or signed receipts.
2. Exact anti-abuse token field names remain presence-bound so reCAPTCHA refresh does
   not break legitimate dispatch. The suffix exemption was removed, and security
   headers are bound, but a production-grade policy should scope token exceptions by
   origin and schema.

Those are not reasons to call the browser broken. They are reasons not to market it as
universal or exactly-once against a malicious destination. The defensible guarantee is
bounded execution with blocked unreviewed writes, durable action state, and explicit
unknown outcomes.
