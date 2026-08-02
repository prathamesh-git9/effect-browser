# Bounded unattended execution

## What “autonomous” means here

Effect Browser can run a task without a person approving each step. It cannot honestly
or safely mean “the model may do anything on any website.” A task receives a durable
authority scope before execution:

- `mode`: `supervised` or `bounded`.
- `allow_file_uploads`: permits only the task document path and SHA-256 already bound
  at task creation.
- `allow_external_commits`: permits only exact reviewed requests with an independent
  reconciliation contract.
- `max_external_commits`: a hard limit from one to three.

The default is supervised with zero unattended external commits.

## Mandatory Scrapling loop

Reactive execution has no non-Scrapling targeting fallback:

1. Playwright renders the live page.
2. Scrapling parses that rendered DOM, saves each candidate, and assigns its selector
   and adaptive relocation ID.
3. The provider may choose only one listed candidate ID and a typed action.
4. Deterministic binding rejects an incompatible action/candidate pair.
5. Policy checks the origin, target semantics, key allowlist, upload hash, and effect
   contract.
6. Playwright performs the typed action and the loop observes again.

If Scrapling cannot produce a unique candidate, the model does not get a raw click
escape hatch.

## How unattended commits remain auditable

Policy still moves every upload and submit through `APPROVAL_REQUIRED`. Bounded mode
then creates a normal approval record with an actor derived from the stored task scope.
That approval binds:

- the exact serialized action hash;
- the current rendered-page state hash;
- for submits, the abort-first outgoing request payload hash.

The application request preview is aborted before transmission. Dispatch permits one
application write only if its regenerated fingerprint matches, and the route stays
armed while delayed JavaScript or known reCAPTCHA token traffic completes. Success
requires the configured authoritative receipt lookup. A page change discards the stale
request preview and action hash before bounded mode can authorize a fresh attempt.

## What never auto-authorizes

- An ambiguous button or generic click with no exact request/reconciliation contract.
- Credential, password, secret, payment-card, PIN, OTP, or MFA entry.
- CAPTCHA or bot-challenge bypass.
- Missing or unverified consequential facts.
- A file other than the task-bound allowlisted path and SHA-256.
- A submit that produces zero, multiple, changed, streaming, or unsupported requests.
- A submit without an authoritative reconciliation contract.
- A commit after `max_external_commits` is exhausted.
- Any action after dispatch has an unknown outcome.

An unknown outcome is not retried. The worker stops in `AWAITING_RECOVERY`.

## Running it

Create an explicit bounded task, then leave the polling worker running:

```powershell
effect-browser create "Perform the authorized task" `
  --start-url "https://allowed.example" `
  --provider grok-reactive `
  --autonomy-mode bounded `
  --allow-external-commits `
  --max-external-commits 1

effect-browser worker
```

For a document workflow, also provide `--document-path`, `--document-sha256`, and
`--allow-file-uploads`. The origin and upload root must already be configured in the
environment.

The dashboard’s **Bounded unattended** mode starts the run immediately after task
creation. MCP clients can create the same scope with `create_browser_task` and execute
it with `run_browser_task`.

## Proved behavior

`tests/test_job_harness_e2e.py` runs a dynamically hydrated multi-page form in real
Edge, attaches an exact hashed document, previews and dispatches one multipart request,
reconciles the application ledger, and finishes without a per-action approval call.
It asserts one stored effect, zero duplicate attempts, scoped approval actors, and a
valid audit chain.

`tests/test_capability_harness_e2e.py` is domain-neutral. It proves Scrapling-backed
candidate extraction and adaptive relocation plus check, press, scroll, wait, and
download primitives against a page that hydrates and replaces its controls.
