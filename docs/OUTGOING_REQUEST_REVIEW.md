# Outgoing request review

## Why DOM review was insufficient

Dynamic application pages routinely transform form state in JavaScript. The bundled
synthetic ATS adds `job_slug` and `mode`, converts `years_python` from a string to a
number, and sends JSON. A review made only from visible inputs therefore cannot describe
the actual write.

Playwright exposes the raw request body and lets a browser context route either abort or
continue a request. Its documentation also warns that service workers bypass normal
request routing unless they are blocked. Effect Browser uses those primitives and blocks
new WebSocket connections so the supported submit path cannot bypass the review:

- [Playwright network routing](https://playwright.dev/python/docs/network)
- [Request body API](https://playwright.dev/python/docs/api/class-request)
- [Browser-context routing and service-worker warning](https://playwright.dev/python/docs/api/class-browsercontext)

## Protocol

1. Stabilize and hash the rendered page.
2. Install a browser-context route before clicking submit.
3. Parse and fingerprint the one click-generated request.
4. Abort that route with `blockedbyclient`; no target request is sent.
5. Persist only the display-safe target, parsed fields, raw URL/body hashes, and request
   fingerprint. Token-like values are redacted but their hashes remain bound.
6. Require action-time operator approval. The approval row and hash-chained audit event
   bind the action, observation, payload, and request hashes.
7. Rebuild the browser state. At dispatch, route the first request again. Continue it
   only if its fingerprint is identical; otherwise abort it before transmission.
8. Treat any error after a matching request is continued as outcome-unknown and never
   retry automatically. Independent target reconciliation is still required for
   verified success.

## Supported boundary

The current gate supports exactly one JSON or URL-encoded request with a body no larger
than 12 MiB. The request target must share an allowlisted origin. Query parameters,
duplicate URL-encoded keys, JSON-added fields, and JSON type conversions are included.

Multipart bodies, streaming requests, and submit actions that generate more than one
write are not claimed as reviewed. They fail closed. This means upload selection exists
as a safe primitive, but a complete multipart résumé application remains unfinished
until the adversarial multi-page harness work proves that path.

## Proof in the synthetic ATS

The E2E harness asserts that preview leaves the authoritative application ledger empty,
then verifies that the approved request creates exactly one durable application. A
payload-drift mode generates a new JavaScript nonce after browser reconstruction. The
second request fingerprint differs, the route aborts it, the task reports failure, and
the ledger remains empty. A stronger model cannot reproduce this guarantee because the
guarantee is enforced at the browser/network boundary, not inferred from model output.
