# Operations runbook

## Deployment

Effect Browser is local-first. `effect-browser serve` binds to loopback, and the sample
Compose file publishes only on `127.0.0.1`. For shared use, terminate TLS and establish
tenant/actor identity in an authenticated reverse proxy. The built-in identity headers
are not an authentication mechanism.

Use PostgreSQL for more than one worker. SQLite is suitable for the local demo and a
single operator process. Run `effect-browser init` once against a new database. Startup
applies the release's additive approval-hash migration without deleting task history.
Back up the database before upgrading; destructive and type-changing migrations are
deliberately unsupported.

Persist the database and Playwright artifact directory. Browser traces and screenshots
may contain sensitive page data, so apply retention, encryption, and access controls
outside the application. The durable observation stores only hashes and URLs.

## Required configuration

| Variable | Purpose |
| --- | --- |
| `EFFECT_BROWSER_DATABASE_URL` | SQLAlchemy SQLite or PostgreSQL URL. |
| `EFFECT_BROWSER_ALLOWED_ORIGINS` | Comma-separated exact origins the browser may use. |
| `EFFECT_BROWSER_ALLOWED_UPLOAD_ROOTS` | Comma-separated local directories from which files may be attached; empty disables uploads. |
| `EFFECT_BROWSER_ALLOWED_UPLOAD_ORIGINS` | Exact origins allowed to receive a hash-verified multipart file-change upload; empty blocks auto-upload writes. |
| `EFFECT_BROWSER_PROVIDER` | `auto`, `openai-reactive`, or `grok-reactive` for one-query public-web tasks. |
| `EFFECT_BROWSER_DEFAULT_PROFILE_ID` | Optional tenant profile selected by one-query mode. |
| `EFFECT_BROWSER_DEFAULT_DOCUMENT_PATH` | Optional absolute default document; normal upload allowlist/hash rules still apply. |
| `EFFECT_BROWSER_BROWSER_HEADLESS` | Headless execution; defaults to `true`. |
| `EFFECT_BROWSER_BROWSER_SANDBOX` | Chromium sandbox; defaults on, disabled in the sample container. |
| `EFFECT_BROWSER_ARTIFACTS_DIRECTORY` | Trace and screenshot destination. |
| `OPENAI_API_KEY` / `XAI_API_KEY` | Needed only for the matching planner. |

## One-query operation

Run `effect-browser do "QUERY"` or `POST {"query":"..."}` to `/v1/autopilot`. An
explicit URL is used directly after network-boundary validation. URL-free queries use
provider-hosted web search and fail when no grounded target can be established.

The query may pre-authorize at most one external commit. An abort-first submit review
is persisted, then dispatch resumes in a fresh browser session so preview-mutated DOM
state cannot weaken approval binding. The result is `verified_success` only when the
receipt contract matches; visible page text alone is insufficient. See
[AUTOPILOT.md](AUTOPILOT.md).

## Factual profiles and task documents

Create verified answers through `/v1/profiles` before starting a personal reactive
workflow. Field keys are normalized accessible labels: for example, `Full name` becomes
`full_name` and `Years using Python` becomes `years_using_python`. Unverified values are
never placed in a planning request. A missing consequential key moves the task to
`awaiting_input`; update and verify the profile, then call the action's `resume-input`
endpoint or CLI command to permit fresh re-planning.

Task document paths must be absolute, inside an allowed upload root, and paired with the
raw file SHA-256. Paths remain in the local database because the browser worker needs
them, but are excluded from task responses, model prompts, and the audit ledger. Protect
the database accordingly.

An application allow list is not a network sandbox. Enforce outbound network policy at
the container or host layer as well. The sample container disables Chromium's sandbox
because typical Docker runtimes block its user namespace; compensate with a non-root
container, seccomp/AppArmor, dropped capabilities, and isolated egress.

## Health and telemetry

- `GET /healthz` proves the process is responsive.
- `GET /readyz` verifies schema access.
- `GET /metrics` exposes Prometheus request counters.
- Every response includes `X-Request-ID`; caller-supplied IDs are propagated.
- `GET /v1/audit/verify` recomputes the tenant event chain and checks its durable head.

Alert on repeated `409` conflicts, failed audit verification, tasks in
`awaiting_recovery` or `awaiting_input`, and leases that expire while an action is
`dispatching`.

## Read-only research

`POST /v1/research` and `effect-browser research` capture rendered evidence from up to
five allowlisted origins. The response includes the observed URL, title, bounded text
excerpt, page-state hash, and evidence hash. This mode has no submit or booking path;
do not interpret a captured excerpt as independently verified truth. Configure
`EFFECT_BROWSER_ALLOWED_ORIGINS` narrowly and use an authenticated proxy for shared
deployments.

## Unknown-outcome recovery

1. Do not click the target control manually or restart the action blindly.
2. Inspect the action effect key, expected outcome, target URL, and bound hashes.
3. Use **Reconcile target receipt** or `effect-browser reconcile ACTION_ID`.
4. If deterministic evidence exists, Effect Browser records the receipt and advances.
5. If no evidence exists, investigate the target independently. Only an operator may
   mark the effect `not_committed`; that resets the action and requires a new approval.
6. Never mark `not_committed` merely because the success page was lost.

## Bounded unattended tasks

Bounded mode is an upfront authority envelope, not a global approval bypass. It may
select only the exact task document and may dispatch no more than the recorded number
of external commits. Each submit still requires one aborted request preview and an
authoritative reconciliation contract. Run `effect-browser worker` for durable polling;
it resumes bounded tasks that stopped between preparation and scoped authorization.

Do not grant bounded commit authority to a task whose target semantics you do not
understand. Keep allowed origins narrow and start with `max_external_commits=1`.
Ambiguous clicks, CAPTCHA, MFA, missing facts, payment/secret fields, and unknown
outcomes still stop the worker. See
[BOUNDED_AUTONOMY.md](BOUNDED_AUTONOMY.md) for the exact contract.

## Backup and restore

Back up PostgreSQL with the platform's normal consistent snapshot mechanism. Restore the
database before browser artifacts; the database is authoritative. After restore, call
the audit verification endpoint. Any action restored in `dispatching` is intentionally
converted to `outcome_unknown` on its next run.

## Known limits

- Exactly-once is impossible against an arbitrary portal. The strong result requires a
  target idempotency key or a uniquely queryable business reference.
- Ambiguous generic clicks are never auto-authorized, and auto-saving forms require a
  workflow-specific policy.
- File selection always requires exact operator or task-scope authority. Auto-upload
  writes are allowed only to configured upload origins when one multipart file exactly
  matches the approved content hash; unconfigured, extra, raw, or changed writes fail
  closed. Restart replay remains write-blocked and therefore cannot reconstruct an ATS
  whose file input always retransmits without a new explicit upload action.
- Submit preview blocks service workers and WebSockets, intercepts the click-generated
  request, and aborts it before network transmission. The approved request is allowed
  only when its regenerated URL/body fingerprint is identical.
- Known Google/reCAPTCHA token requests may execute as browser-security support traffic;
  they are never counted as the application commit. Refreshed security-token values are
  presence-bound while applicant fields stay value-bound. A visible CAPTCHA still
  stops for handoff.
- Exact request review accepts one JSON, URL-encoded, or canonicalized multipart request
  no larger than 12 MiB. Nested multipart, streaming, and multi-write submits are
  blocked rather than shown as reviewed.
- The MVP has no credential vault, OIDC, or built-in artifact encryption.
- Origin checks do not replace OS/container egress isolation.
