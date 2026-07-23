# Outgoing request review

## Why DOM review was insufficient

Dynamic application pages routinely transform form state in JavaScript. The bundled
synthetic ATS adds `job_slug` and `mode` and sends multipart form data with a résumé.
A review made only from visible inputs therefore cannot describe the actual write.

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
5. Persist only the display-safe target, parsed fields, URL and wire-body evidence,
   canonical body hash, raw document hashes, and request fingerprint. Token-like values
   are redacted but their hashes remain bound.
6. Require action-time operator approval. The approval row and hash-chained audit event
   bind the action, observation, payload, and request hashes.
7. Rebuild the browser state. At dispatch, route the first request again. Continue it
   only if its fingerprint is identical; otherwise abort it before transmission.
8. Treat any error after a matching request is continued as outcome-unknown and never
   retry automatically. Independent target reconciliation is still required for
   verified success.

## Supported boundary

The current gate supports exactly one JSON, URL-encoded, or multipart request with a
body no larger than 12 MiB. The request target must share an allowlisted origin. Query
parameters, duplicate URL-encoded keys, JSON-added fields, and JSON type conversions are
included.

Multipart fingerprinting canonicalizes field order and values plus each file's name,
MIME type, byte count, and raw SHA-256. It excludes the browser-generated boundary from
the comparison because Chromium regenerates that value on every request. The preview
wire-body hash is retained as evidence but is not the semantic dispatch key. Streaming,
nested multipart, and submit actions that generate more than one write fail closed.

File selection itself runs under a write-blocking route. If a page auto-uploads on the
file input's `change` event, that request is aborted and the action fails. Effect Browser
does not pretend that an unreviewed upload succeeded.

## Proof in the synthetic ATS

The E2E harness asserts that preview leaves the authoritative application ledger empty,
then verifies that the approved multipart request creates exactly one durable
application whose résumé hash matches review. A payload-drift mode generates a new
JavaScript nonce after browser reconstruction. The second fingerprint differs, the
route aborts it, the task reports failure, and the ledger remains empty. An auto-upload
mode proves the server receives zero file-change requests. A stronger model cannot
reproduce this guarantee because enforcement lives at the browser/network boundary.
