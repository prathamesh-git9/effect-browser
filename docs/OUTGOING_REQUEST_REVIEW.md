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
   canonical body hash, raw document hashes, and request fingerprint. Secrets are
   redacted. Volatile CSRF, fingerprint, request-token, and reCAPTCHA values are
   presence-bound rather than value-bound; ordinary applicant values remain hash-bound.
6. Require action-time operator approval. The approval row and hash-chained audit event
   bind the action, observation, payload, and request hashes.
7. Rebuild the browser state. At dispatch, keep the route armed while delayed
   JavaScript and browser-security token acquisition complete. Continue the application
   request only if its fingerprint is identical; otherwise abort it before transmission.
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

File selection itself runs under a write-verifying route. If a page auto-uploads on the
file input's `change` event, the request is allowed only when its exact origin is
configured and one multipart part matches the approved document SHA-256. The receipt
records the request fingerprint. Any extra, unconfigured, raw, or changed write is
aborted. Effect Browser does not pretend that a blocked upload succeeded.

Invisible reCAPTCHA Enterprise flows can make supporting POSTs before the application
request. Only known reCAPTCHA paths on Google or recaptcha.net may continue; the
application endpoint is still the sole reviewed commit. Visible CAPTCHA text or a human
challenge remains a hard handoff.

## Proof in the synthetic ATS

The E2E harness asserts that preview leaves the authoritative application ledger empty,
then verifies that the approved multipart request creates exactly one durable
application whose résumé hash matches review. A payload-drift mode generates a new
JavaScript nonce after browser reconstruction. The second fingerprint differs, the
route aborts it, the task reports failure, and the ledger remains empty. An auto-upload
mode proves the server receives zero file-change requests without an upload-origin
allowlist and exactly one with an allowlist plus matching document hash. A delayed-submit
mode proves the route remains armed until the reviewed request appears. A stronger model
cannot reproduce this guarantee because enforcement lives at the browser/network
boundary.
