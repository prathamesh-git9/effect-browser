# Effect Browser

Effect Browser is a crash-safe control plane for AI-driven browser operations. It is
not another “click things with an LLM” wrapper. It focuses on the failure ordinary
browser agents handle badly: the target may commit an external action just before the
browser or worker crashes.

Models propose typed actions. Effect Browser persists them, auto-runs safe navigation,
requires exact action-bound authority for external commits, and records
`OUTCOME_UNKNOWN` instead of blindly clicking twice. Authority can come from an
operator at the commit boundary or from a bounded scope recorded when the task is
created. A deterministic reconciler can close the gap when the target exposes a stable
business reference or receipt.

Read the [research decision](docs/RESEARCH.md) and [technical spec](docs/SPEC.md).
Deployment and recovery procedures are in the [operations runbook](docs/OPERATIONS.md).

## Honest guarantee

This project does **not** promise exactly-once effects against arbitrary web pages. That
is impossible without cooperation or uniquely queryable target state.

- With a target idempotency key or deterministic receipt lookup: one observable effect.
- Without one: at-most-one dispatch, then explicit manual resolution if the outcome is
  ambiguous.
- After page drift: the old authorization is invalid. Bounded mode re-observes,
  re-previews, and re-binds it at most three times; supervised mode pauses.
- Ambiguous generic clicks never consume bounded authority because their effect cannot
  be reviewed or reconciled.
- Form fills are safe only for validated workflows that do not auto-save on change.

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev,mcp]"
playwright install chromium
effect-browser init
effect-browser serve
```

Open <http://127.0.0.1:8000>. Use the deterministic provider and bundled demo shop with
no API keys.

The bundled dynamic job harness is at <http://127.0.0.1:8000/demo-jobs>. It uses
asynchronous form hydration, a conditional question, server-side validation, an
authoritative application ledger, and a deliberately deceptive fake-success mode. Run:

```powershell
pytest tests/test_job_harness_e2e.py
```

The domain-neutral dynamic capability lab is at
<http://127.0.0.1:8000/demo-capabilities>. Its real Edge test proves checkbox/radio
state, select keyboard input, scrolling, bounded waits, downloads with SHA-256
receipts, and Scrapling relocation after the page replaces the observed controls:

```powershell
pytest tests/test_capability_harness_e2e.py
```

The honest test result is documented in
[docs/JOB_APPLICATION_HARNESS.md](docs/JOB_APPLICATION_HARNESS.md). The deterministic
workflow adapter passes; the current one-shot OpenAI planner does **not** generalize to
an unfamiliar job page because it does not observe the DOM before planning.

Use `openai-reactive` or `grok-reactive` for the adaptive path. It renders the live
page, uses Scrapling to produce candidate-bound controls, asks the provider for one
action, executes it through deterministic policy, and observes again. The legacy
`openai` and `grok` providers remain only as explicit one-shot baselines.

Factual profiles are tenant-scoped durable records at `/v1/profiles`. Each answer keeps
its source, sensitivity class, user-verification state, verifier, and optimistic
version. Answer values and source references are intentionally absent from the
hash-chained audit payload. Reactive tasks bind an optional profile ID. Consequential
visible fields are filled locally only from an exactly named, verified answer; missing
or unverified facts create `INPUT_REQUIRED` before a provider call. Verified facts are
included in the selected remote provider's step request, so operators must treat that
provider as a data processor.

Local file inputs are disabled unless `EFFECT_BROWSER_ALLOWED_UPLOAD_ROOTS` names one or
more directories. Every upload action binds an absolute path and the raw-byte SHA-256;
policy checks the allowlist and content, and the browser executor checks both again
immediately before attaching the file. Snapshots expose only whether a file is selected,
never its local path or filename. Every file selection requires action-bound authority:
an operator decision or a bounded task scope that names the exact task document hash.
The browser installs a route before selecting the file. A file-change POST is permitted
only when `EFFECT_BROWSER_ALLOWED_UPLOAD_ORIGINS` explicitly names its origin and the
multipart body contains exactly the approved document SHA-256. Every other write is
blocked before transmission. For an authorized final submit, the exact guard is
installed before the file input is replayed after a browser restart.

The dashboard's `job-harness` provider is deliberately synthetic. Put a non-personal
fixture named `synthetic-resume.txt` in one configured upload root; task creation fails
clearly if that fixture is absent. Never point the harness at an employer.

Reactive tasks may bind an absolute document path and raw SHA-256. The allowlist and
bytes are checked at task creation and again at browser execution. The local path is
stored for the worker but is excluded from task API responses, prompts, and audit
events. Upload candidates are converted to local deterministic actions; a remote model
never receives or chooses the path.

Read-only research is available through `POST /v1/research` or
`effect-browser research`. It visits one to five configured HTTP(S) origins, captures
the rendered title and excerpt, and returns a state hash plus evidence hash per source.
Research never clicks, fills, uploads, logs in, submits, or books anything. It is a
source-capture primitive, not a claim that page text is true; consequential actions
remain behind the normal approval and authoritative-receipt gates.

Submit approval is based on an abort-first network preview, not only the visible DOM.
Effect Browser routes the click-generated request, records its method, redacted target,
URL and wire-body evidence, canonical body hash, content type, parsed outgoing fields,
and raw document hashes, then aborts it before the network. Approval binds that request
fingerprint, the action, and the page state. On dispatch, a changed first request is
blocked before transmission. The current exact preview supports one JSON, URL-encoded,
or multipart request up to 12 MiB. Multipart comparison ignores only the regenerated
boundary; filename, MIME type, size, document bytes, field order, and values remain
bound. Known reCAPTCHA token endpoints may run while the application request remains
blocked/reviewed; volatile anti-abuse token values are presence-bound so token refresh
does not invalidate unchanged applicant data. The route remains armed for delayed
JavaScript submits. Streaming and multi-write application flows fail closed.

Scrapling's role and limitations are recorded in
[docs/SCRAPLING_RESEARCH.md](docs/SCRAPLING_RESEARCH.md). The measurable completion
contract is [docs/AUTONOMOUS_ROADMAP.md](docs/AUTONOMOUS_ROADMAP.md).
The unattended authority contract is
[docs/BOUNDED_AUTONOMY.md](docs/BOUNDED_AUTONOMY.md).

Run the durable polling worker separately when tasks should progress without an open
dashboard. Supervised tasks stop at commit approval. Bounded tasks automatically
consume their recorded upload/commit authority, but still stop at unknown outcomes,
missing facts, challenges, ambiguous clicks, or an exhausted commit limit:

```powershell
effect-browser worker
```

```powershell
effect-browser killer-demo
```

## Providers

The default deterministic planner drives the demo and tests. OpenAI and Grok implement
the same JSON plan contract:

```powershell
$env:OPENAI_API_KEY = "..."
$env:EFFECT_BROWSER_PROVIDER = "openai"

$env:XAI_API_KEY = "..."
$env:EFFECT_BROWSER_PROVIDER = "grok"
```

Provider output never directly invokes Playwright. It is validated, stored, classified
by deterministic policy, and executed by the durable engine.

## Deployment boundary

The built-in identity headers are for local evaluation, not Internet-facing
authentication. The CLI binds to `127.0.0.1` by default and Docker Compose publishes on
loopback only. Put an authenticated identity-aware proxy in front of the API before any
shared deployment; do not expose it directly.

## Verification

```powershell
ruff check .
ruff format --check .
pytest
docker compose config
```

MIT licensed.
