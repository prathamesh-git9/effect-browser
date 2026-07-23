# Effect Browser

Effect Browser is a crash-safe control plane for AI-driven browser operations. It is
not another “click things with an LLM” wrapper. It focuses on the failure ordinary
browser agents handle badly: the target may commit an external action just before the
browser or worker crashes.

Models propose typed actions. Effect Browser persists them, auto-runs safe navigation,
requires exact action-bound approval for external commits, and records `OUTCOME_UNKNOWN`
instead of blindly clicking twice. A deterministic reconciler can close the gap when the
target exposes a stable business reference or receipt.

Read the [research decision](docs/RESEARCH.md) and [technical spec](docs/SPEC.md).
Deployment and recovery procedures are in the [operations runbook](docs/OPERATIONS.md).

## Honest guarantee

This project does **not** promise exactly-once effects against arbitrary web pages. That
is impossible without cooperation or uniquely queryable target state.

- With a target idempotency key or deterministic receipt lookup: one observable effect.
- Without one: at-most-one dispatch, then explicit manual resolution if the outcome is
  ambiguous.
- After page drift: approval is invalid and must be reviewed again.
- Generic clicks are rejected because the policy cannot prove whether they are read-only.
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
hash-chained audit payload. This storage layer is not yet wired into reactive planning:
until verified-answer enforcement lands, use only synthetic profile facts in demos.

Scrapling's role and limitations are recorded in
[docs/SCRAPLING_RESEARCH.md](docs/SCRAPLING_RESEARCH.md). The measurable completion
contract is [docs/AUTONOMOUS_ROADMAP.md](docs/AUTONOMOUS_ROADMAP.md).

Run the durable polling worker separately when tasks should progress without an open
dashboard. It auto-runs only queued safe work and still stops at approval and recovery
gates:

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
