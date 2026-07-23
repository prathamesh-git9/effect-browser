# Operations runbook

## Deployment

Effect Browser is local-first. `effect-browser serve` binds to loopback, and the sample
Compose file publishes only on `127.0.0.1`. For shared use, terminate TLS and establish
tenant/actor identity in an authenticated reverse proxy. The built-in identity headers
are not an authentication mechanism.

Use PostgreSQL for more than one worker. SQLite is suitable for the local demo and a
single operator process. Run `effect-browser init` once against a new database. Version
0.1 creates its initial schema directly; it does not claim a zero-downtime migration
path from prerelease databases.

Persist the database and Playwright artifact directory. Browser traces and screenshots
may contain sensitive page data, so apply retention, encryption, and access controls
outside the application. The durable observation stores only hashes and URLs.

## Required configuration

| Variable | Purpose |
| --- | --- |
| `EFFECT_BROWSER_DATABASE_URL` | SQLAlchemy SQLite or PostgreSQL URL. |
| `EFFECT_BROWSER_ALLOWED_ORIGINS` | Comma-separated exact origins the browser may use. |
| `EFFECT_BROWSER_BROWSER_HEADLESS` | Headless execution; defaults to `true`. |
| `EFFECT_BROWSER_ARTIFACTS_DIRECTORY` | Trace and screenshot destination. |
| `OPENAI_API_KEY` / `XAI_API_KEY` | Needed only for the matching planner. |

An application allow list is not a network sandbox. Enforce outbound network policy at
the container or host layer as well.

## Health and telemetry

- `GET /healthz` proves the process is responsive.
- `GET /readyz` verifies schema access.
- `GET /metrics` exposes Prometheus request counters.
- Every response includes `X-Request-ID`; caller-supplied IDs are propagated.
- `GET /v1/audit/verify` recomputes the tenant event chain and checks its durable head.

Alert on repeated `409` conflicts, failed audit verification, tasks in
`awaiting_recovery`, and leases that expire while an action is `dispatching`.

## Unknown-outcome recovery

1. Do not click the target control manually or restart the action blindly.
2. Inspect the action effect key, expected outcome, target URL, and bound hashes.
3. Use **Reconcile target receipt** or `effect-browser reconcile ACTION_ID`.
4. If deterministic evidence exists, Effect Browser records the receipt and advances.
5. If no evidence exists, investigate the target independently. Only an operator may
   mark the effect `not_committed`; that resets the action and requires a new approval.
6. Never mark `not_committed` merely because the success page was lost.

## Backup and restore

Back up PostgreSQL with the platform's normal consistent snapshot mechanism. Restore the
database before browser artifacts; the database is authoritative. After restore, call
the audit verification endpoint. Any action restored in `dispatching` is intentionally
converted to `outcome_unknown` on its next run.

## Known limits

- Exactly-once is impossible against an arbitrary portal. The strong result requires a
  target idempotency key or a uniquely queryable business reference.
- Generic clicks are rejected, and auto-saving forms require a workflow-specific policy.
- The MVP has no credential vault, OIDC, or built-in artifact encryption.
- Origin checks do not replace OS/container egress isolation.
